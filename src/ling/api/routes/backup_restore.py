"""整包备份与导入恢复的 Web 控制入口。

这里刻意只做两件事：

1. 只读当前受控目录里的备份列表、包清单和预检结果；
2. owner 通过同源 POST 提交“创建/导入预检/确认恢复”请求，API 把请求原子写进
   宿主执行器目录，不直接执行 docker，也不把密钥内容回给浏览器。

真正的宿主单命令入口是 ``deploy/backup/restore_full.py``；即使 Web 不可用，
管理员也能在宿主上完成恢复。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import CurrentAuth, SettingsDep
from ling.api.errors import LingError
from ling.api.guards import OwnerWrite
from ling.backup_restore import (
    BackupRestoreError,
    current_runtime_arch,
    estimate_required_bytes,
    preflight_package,
    redact_env,
)

router = APIRouter(tags=["backup-restore"])

REQUESTS_ENV = "LING_BACKUP_RESTORE_REQUESTS_DIR"
DEFAULT_REQUESTS_DIRNAME = ".run/backup-restore/requests"
DEFAULT_PACKAGE_DIRNAME = ".data/backup-packages"


def _requests_dir(settings: SettingsDep) -> Path:
    configured = os.environ.get(REQUESTS_ENV, "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else settings.data_dir.parent / ".run" / "backup-restore" / "requests"
    )


def _package_dir(settings: SettingsDep) -> Path:
    return settings.data_dir.parent / ".data" / "backup-packages"


def _disk_free(path: Path) -> int | None:
    try:
        return shutil_disk_usage(path).free
    except OSError:
        return None


def shutil_disk_usage(path: Path):
    import shutil

    return shutil.disk_usage(path)


def _request_files(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append({"id": path.stem, **payload})
    return rows


def _write_request(directory: Path, action: str, payload: dict[str, Any]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    request_id = f"{int(time.time() * 1000)}-{action}"
    target = directory / f"{request_id}.json"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".request-", delete=False
    )
    try:
        json.dump({"action": action, **payload}, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, target)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
    return request_id


class BackupRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ImportRequest(BaseModel):
    package: str = Field(min_length=1, max_length=1000)
    target_version: str | None = Field(default=None, max_length=200)
    confirm: bool = False


@router.get("/backup-restore/status")
async def status(settings: SettingsDep, response: Request) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    root = _package_dir(settings)
    packages: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            manifest = path / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            packages.append(
                {
                    "name": path.name,
                    "version": (data.get("version") or {}).get("app"),
                    "commit": (data.get("version") or {}).get("commit"),
                    "migration": (data.get("version") or {}).get("migration"),
                    "artifact": (data.get("artifact") or {}).get("digest")
                    or (data.get("artifact") or {}).get("release_id"),
                }
            )
    return {
        "package_dir": str(root),
        "packages": packages,
        "requests": _request_files(_requests_dir(settings)),
        "target_arch": current_runtime_arch(),
    }


@router.get("/backup-restore/packages/{name}/inspect")
async def inspect_package(name: str, settings: SettingsDep, response: Request) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise LingError("invalid_request", "非法包名", status_code=422)
    package = _package_dir(settings) / name
    report = preflight_package(
        package,
        target_arch=current_runtime_arch(),
        free_bytes=_disk_free(package.parent),
    )
    env_file = package / "config" / "deploy.env"
    env_preview = redact_env(env_file.read_text(encoding="utf-8")) if env_file.is_file() else ""
    return {
        **report.as_dict(),
        "deploy_env": env_preview,
        "required_bytes": estimate_required_bytes(package) if package.is_dir() else 0,
    }


@router.post("/backup-restore/backups")
async def request_backup(
    body: BackupRequest,
    auth: OwnerWrite,
    settings: SettingsDep,
    response: Request,
) -> dict[str, Any]:
    del auth
    response.headers["Cache-Control"] = "no-store"
    request_id = _write_request(
        _requests_dir(settings), "create_backup", {"note": body.note}
    )
    return {"request_id": request_id, "status": "queued"}


@router.post("/backup-restore/imports/preflight")
async def request_import_preflight(
    body: ImportRequest,
    auth: OwnerWrite,
    settings: SettingsDep,
    response: Request,
) -> dict[str, Any]:
    del auth
    response.headers["Cache-Control"] = "no-store"
    package = Path(body.package)
    if not package.is_absolute():
        package = _package_dir(settings) / package
    request_id = _write_request(
        _requests_dir(settings),
        "preflight_import",
        {
            "package": str(package),
            "target_version": body.target_version,
            "confirm": False,
        },
    )
    return {"request_id": request_id, "status": "queued"}


@router.post("/backup-restore/imports/confirm")
async def request_import_confirm(
    body: ImportRequest,
    auth: OwnerWrite,
    settings: SettingsDep,
    response: Request,
) -> dict[str, Any]:
    del auth
    response.headers["Cache-Control"] = "no-store"
    if not body.confirm:
        raise LingError("invalid_request", "确认恢复必须显式 confirm=true", status_code=422)
    package = Path(body.package)
    if not package.is_absolute():
        package = _package_dir(settings) / package
    report = preflight_package(
        package,
        target_version=body.target_version,
        target_arch=current_runtime_arch(),
        free_bytes=_disk_free(package.parent),
    )
    if not report.ok:
        raise LingError(
            "invalid_request",
            "预检未通过：" + "；".join(report.errors[:8]),
            status_code=422,
        )
    request_id = _write_request(
        _requests_dir(settings),
        "confirm_restore",
        {
            "package": str(package),
            "target_version": body.target_version,
            "confirm": True,
        },
    )
    return {"request_id": request_id, "status": "queued", "preflight": report.as_dict()}
