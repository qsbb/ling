"""Portable Ling snapshots and the filesystem channel used by the backup page."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zipfile import BadZipFile, ZipFile

FORMAT = "ling-backup/1"
SUFFIX = ".ling-backup"
FILES = {"database.dump", "data.tar", "images.tar", "settings.json"}
MAX_MANIFEST_BYTES = 64 * 1024
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
SERVICES = ("db", "api", "worker")


class BackupError(ValueError):
    pass


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(bundle: ZipFile) -> dict[str, Any]:
    """Validate the package layout before displaying it or streaming any members."""
    try:
        entry = bundle.getinfo("manifest.json")
        if entry.file_size > MAX_MANIFEST_BYTES:
            raise BackupError("备份清单过大")
        manifest = json.loads(bundle.read(entry))
    except (KeyError, ValueError, BadZipFile, OSError) as exc:
        raise BackupError("无法读取备份清单") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("备份格式不受支持")
    files = manifest.get("files")
    images = manifest.get("images")
    if not isinstance(files, dict) or not isinstance(images, dict) or set(images) != set(SERVICES):
        raise BackupError("备份清单缺少文件或服务信息")
    if set(files) not in (FILES, FILES | {"web.tar"}):
        raise BackupError("备份包的文件清单不完整")
    if sorted(bundle.namelist()) != sorted([*files, "manifest.json"]):
        raise BackupError("备份包的文件与清单不一致")
    if not all(isinstance(value, str) and IMAGE_ID.fullmatch(value) for value in images.values()):
        raise BackupError("备份镜像身份无效")
    for name, digest in files.items():
        if not isinstance(digest, str) or not IMAGE_ID.fullmatch(f"sha256:{digest}"):
            raise BackupError(f"文件校验值无效：{name}")
    for field in ("version", "commit", "migration", "created_at"):
        if not isinstance(manifest.get(field), str) or len(manifest[field]) > 128:
            raise BackupError(f"备份清单版本字段无效：{field}")
    return manifest


def backup_details(path: Path) -> dict[str, Any]:
    try:
        with ZipFile(path) as bundle:
            manifest = read_manifest(bundle)
    except (BadZipFile, OSError, NotImplementedError, RuntimeError) as exc:
        raise BackupError("无法打开备份包") from exc
    return {
        **{key: manifest[key] for key in ("version", "commit", "migration", "created_at")},
        "release_image": manifest.get("release_image"),
        "name": path.name,
        "bytes": path.stat().st_size,
    }


def backup_path(directory: Path, name: str) -> Path:
    if not NAME.fullmatch(name) or not name.endswith(SUFFIX):
        raise BackupError("备份文件名无效")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise BackupError("备份文件不存在")
    return path


def write_channel_json(path: Path, value: dict[str, Any]) -> None:
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o660)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def worker_available(channel: Path) -> bool:
    try:
        return time.time() - (channel / "worker.json").stat().st_mtime < 15
    except OSError:
        return False
