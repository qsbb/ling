"""Owner-only access to the backup files and host-side job channel."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from ling.api.deps import SettingsDep
from ling.api.errors import LingError, invalid, not_found
from ling.api.guards import OwnerAuth, OwnerWrite, OwnerWriteAction
from ling.backup_restore import (
    SUFFIX,
    BackupError,
    backup_details,
    backup_path,
    worker_available,
    write_channel_json,
)
from ling.contracts.v1 import ErrorCode

router = APIRouter(tags=["backup-restore"])
UPLOAD_PATH = "/v1/backup-restore/upload"


def _channel(settings: SettingsDep) -> Path:
    return settings.update_requests_dir / "backup-restore"


def _require_worker(channel: Path) -> None:
    if not worker_available(channel):
        raise LingError(ErrorCode.unsupported_capability, "宿主备份程序未运行", status_code=503)


def _jobs(channel: Path) -> list[dict[str, Any]]:
    jobs = []
    for path in sorted((channel / "jobs").glob("*.json"), key=lambda item: item.stat().st_mtime,
                       reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            jobs.append(record)
            if len(jobs) == 20:
                break
    return jobs


def _archives(channel: Path) -> list[dict[str, Any]]:
    archives = []
    for path in (channel / "archives").glob(f"*{SUFFIX}"):
        try:
            archives.append(backup_details(backup_path(path.parent, path.name)))
        except (BackupError, OSError):
            continue
    return sorted(archives, key=lambda item: item["created_at"], reverse=True)


class BackupJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "restore"]
    name: str | None = None


@router.get("/backup-restore/status")
async def backup_status(auth: OwnerAuth, settings: SettingsDep, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    channel = _channel(settings)
    return {
        "available": worker_available(channel),
        "archives": await run_in_threadpool(_archives, channel),
        "jobs": await run_in_threadpool(_jobs, channel),
    }


@router.get("/backup-restore/archives/{name}")
async def download(name: str, auth: OwnerAuth, settings: SettingsDep) -> FileResponse:
    try:
        path = backup_path(_channel(settings) / "archives", name)
    except BackupError as exc:
        raise not_found("备份文件不存在") from exc
    return FileResponse(
        path, filename=path.name, media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/backup-restore/upload", status_code=201)
async def upload(request: Request, auth: OwnerWriteAction, settings: SettingsDep) -> dict[str, Any]:
    channel = _channel(settings)
    _require_worker(channel)
    directory = channel / "archives"
    target = directory / f"import-{uuid4().hex}{SUFFIX}"
    temporary = None
    try:
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            async for chunk in request.stream():
                await run_in_threadpool(stream.write, chunk)
            stream.flush()
            os.fchmod(stream.fileno(), 0o660)
        details = await run_in_threadpool(backup_details, temporary)
        temporary.replace(target)
        return {**details, "name": target.name}
    except BackupError as exc:
        raise invalid(str(exc)) from exc
    except OSError as exc:
        raise LingError(ErrorCode.internal, "无法写入备份文件，请检查宿主磁盘", status_code=507) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.post("/backup-restore/jobs", status_code=202)
async def queue_job(body: BackupJob, auth: OwnerWrite, settings: SettingsDep) -> dict[str, Any]:
    channel = _channel(settings)
    _require_worker(channel)
    if body.action == "restore":
        if body.name is None:
            raise invalid("请先选择备份文件")
        try:
            backup_path(channel / "archives", body.name)
        except BackupError as exc:
            raise invalid(str(exc)) from exc
    if any(job.get("status") in {"queued", "running"} for job in _jobs(channel)):
        raise LingError(ErrorCode.conflict, "已有备份任务正在处理", status_code=409)
    request = {"id": uuid4().hex, "action": body.action, "name": body.name, "status": "queued"}
    write_channel_json(channel / "jobs" / f"{request['id']}.json", request)
    return request
