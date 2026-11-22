"""所有者查看受控目录中的整包备份并校验文件。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ling.api.deps import SettingsDep
from ling.api.errors import LingError, invalid, not_found
from ling.api.guards import OwnerAuth, OwnerWrite
from ling.backup_restore import SAFE_NAME, current_runtime_arch, load_manifest, preflight_package
from ling.contracts.v1 import ErrorCode

router = APIRouter(tags=["backup-restore"])


def _package_dir(settings: SettingsDep) -> Path:
    configured = os.environ.get("LING_BACKUP_RESTORE_PACKAGES_DIR", "").strip()
    return Path(configured).expanduser() if configured else settings.data_dir / "backup-packages"


def _display_fields(manifest: dict[str, Any], section: str, keys: tuple[str, ...]) -> dict[str, str]:
    fields = manifest.get(section)
    if not isinstance(fields, dict):
        return {}
    return {key: fields[key] for key in keys if isinstance(fields.get(key), str)}


def _list_packages(root: Path) -> list[dict[str, str | None]]:
    packages = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not SAFE_NAME.fullmatch(path.name) or not path.is_dir() or path.is_symlink():
                continue
            manifest, report = load_manifest(path)
            if report.errors or not manifest:
                continue
            version = _display_fields(manifest, "version", ("app", "commit", "migration"))
            artifact = _display_fields(manifest, "artifact", ("digest", "release_id"))
            packages.append(
                {
                    "name": path.name,
                    "version": version.get("app"),
                    "commit": version.get("commit"),
                    "migration": version.get("migration"),
                    "artifact": artifact.get("digest") or artifact.get("release_id"),
                }
            )
    return packages


def _inspect(root: Path, name: str, target_version: str | None = None) -> dict[str, Any]:
    if not SAFE_NAME.fullmatch(name):
        raise invalid("请选择备份目录中的包名")
    package = root / name
    if package.is_symlink() or not package.is_dir():
        raise not_found("备份包不存在")
    report = preflight_package(
        package,
        target_version=target_version,
        target_arch=current_runtime_arch(),
        free_bytes=shutil.disk_usage(root).free,
    )
    return {
        "ok": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "manifest": {
            "version": _display_fields(report.manifest, "version", ("app", "commit", "migration")),
            "secrets": _display_fields(report.manifest, "secrets", ("root_key_source",)),
        },
    }


class BackupRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ImportRequest(BaseModel):
    package: str = Field(min_length=1, max_length=200)
    target_version: str | None = Field(default=None, max_length=200)
    confirm: bool = False


@router.get("/backup-restore/status")
async def status(auth: OwnerAuth, settings: SettingsDep, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "packages": await run_in_threadpool(_list_packages, _package_dir(settings)),
        "target_arch": current_runtime_arch(),
        "operations_available": False,
    }


@router.get("/backup-restore/packages/{name}/inspect")
async def inspect_package(
    name: str, auth: OwnerAuth, settings: SettingsDep, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_inspect, _package_dir(settings), name)


@router.post("/backup-restore/imports/preflight")
async def request_import_preflight(
    body: ImportRequest, auth: OwnerWrite, settings: SettingsDep, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_inspect, _package_dir(settings), body.package, body.target_version)


@router.post("/backup-restore/backups")
async def request_backup(body: BackupRequest, auth: OwnerWrite) -> None:
    raise LingError(
        ErrorCode.unsupported_capability, "宿主整包备份尚未接通", status_code=503
    )


@router.post("/backup-restore/imports/confirm")
async def request_import_confirm(body: ImportRequest, auth: OwnerWrite) -> None:
    raise LingError(
        ErrorCode.unsupported_capability, "宿主整包恢复尚未接通", status_code=503
    )
