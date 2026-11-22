"""整包备份的清单生成。

输入是已经由 ``deploy/backup/backup.sh`` 产生的恢复点和一组受控材料，输出是
``manifest.json`` 与打包目录。清单里的数据库 dump、媒体、密钥、配置和版本产物
都写 sha256；口令与密钥内容不进清单。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from . import (
    FORMAT_VERSION,
    MANIFEST_NAME,
    BackupRestoreError,
    current_runtime_arch,
    estimate_required_bytes,
)


def _digest(path: Path) -> str:
    hash_value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_value.update(chunk)
    return f"sha256:{hash_value.hexdigest()}"


def _copy(source: Path, destination: Path, *, mode: int | None = None) -> str:
    if not source.is_file():
        raise BackupRestoreError(f"源文件不存在：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if mode is not None:
        os.chmod(destination, mode)
    return _digest(destination)


def build_full_package(
    *,
    output: Path,
    dump: Path,
    media_tar: Path,
    root_key: Path | None,
    deploy_env: Path,
    compose_file: Path,
    tool_plugins: Path,
    persona_file: Path,
    app_version: str,
    commit: str,
    migration: str,
    config_revision: str,
    artifact: dict[str, Any],
    scope: dict[str, Any],
    tombstones_state: str = "ok",
    secrets: dict[str, Any] | None = None,
) -> Path:
    """生成一个可被 :func:`preflight_package` 接受的完整包。"""
    if output.exists():
        raise BackupRestoreError(f"输出目录已存在：{output}")
    output.mkdir(parents=True)
    files: dict[str, str] = {}
    files["database.dump"] = _copy(dump, output / "database.dump")
    files["media.tar"] = _copy(media_tar, output / "media.tar")
    files["config/deploy.env"] = _copy(deploy_env, output / "config" / "deploy.env", mode=0o600)
    files["config/compose.yaml"] = _copy(compose_file, output / "config" / "compose.yaml")
    files["state/tool-plugins.json"] = _copy(
        tool_plugins, output / "state" / "tool-plugins.json"
    )
    files["state/persona.json"] = _copy(persona_file, output / "state" / "persona.json")
    artifact_path = output / "artifacts" / "app-image.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    files["artifacts/app-image.json"] = _digest(artifact_path)
    if root_key is not None:
        files["secrets/root.key"] = _copy(root_key, output / "secrets" / "root.key", mode=0o600)

    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "version": {
            "app": app_version,
            "commit": commit,
            "migration": migration,
            "config_revision": config_revision,
        },
        "artifact": artifact,
        "scope": {
            "database": "full",
            "media": "full",
            "plugins": "managed",
            "persona": "active-and-scenarios",
            "config": "published-and-effective",
            **scope,
        },
        "secrets": secrets or {"root_key_source": "file", "credential_scope": ["database"]},
        "integrity": {"tombstones_state": tombstones_state},
        "resources": {
            "required_bytes": estimate_required_bytes(output),
            "platform": current_runtime_arch(),
        },
        "files": files,
        "host": {"arch": platform.machine(), "os": platform.system()},
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output
