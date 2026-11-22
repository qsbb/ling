"""整包备份的清单生成。

输入是已经由 ``deploy/backup/backup.sh`` 产生的恢复点和一组受控材料，输出是
``manifest.json`` 与打包目录。清单里的数据库 dump、媒体、密钥、配置和版本产物
都写 sha256；口令与密钥内容不进清单。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    FORMAT_VERSION,
    MANIFEST_NAME,
    ROOT_KEY_FILE,
    RUNTIME_FILES,
    BackupRestoreError,
    current_runtime_arch,
    estimate_required_bytes,
    preflight_package,
    sha256_file,
)


def _copy(source: Path, destination: Path) -> str:
    if not source.is_file():
        raise BackupRestoreError(f"源文件不存在：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    return f"sha256:{sha256_file(destination)}"


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
    runtime_archive: Path,
    app_version: str,
    commit: str,
    migration: str,
    config_revision: str,
    artifact: dict[str, Any],
    scope: dict[str, Any],
    tombstones_state: str = "ok",
    secrets: dict[str, Any] | None = None,
) -> Path:
    """汇集宿主已有的备份、配置和发行产物，校验后公布包目录。"""
    if output.exists():
        raise BackupRestoreError(f"输出目录已存在：{output}")
    mode = artifact.get("mode")
    if not isinstance(mode, str) or mode not in RUNTIME_FILES:
        raise BackupRestoreError("artifact.mode 必须是 image 或 source")
    payload = {
        "database.dump": dump,
        "media.tar": media_tar,
        "config/deploy.env": deploy_env,
        "config/compose.yaml": compose_file,
        "state/tool-plugins.json": tool_plugins,
        "state/persona.json": persona_file,
        RUNTIME_FILES[mode]: runtime_archive,
    }
    if root_key is not None:
        payload[ROOT_KEY_FILE] = root_key

    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
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
        "host": {"arch": platform.machine(), "os": platform.system()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".backup-") as stage_dir:
        stage = Path(stage_dir) / "package"
        stage.mkdir(mode=0o700)
        files = {name: _copy(source, stage / name) for name, source in payload.items()}
        descriptor = stage / "artifacts" / "app-image.json"
        descriptor.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        files["artifacts/app-image.json"] = f"sha256:{sha256_file(descriptor)}"
        manifest["files"] = files
        manifest["resources"] = {
            "required_bytes": estimate_required_bytes(stage),
            "platform": current_runtime_arch(),
        }
        (stage / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = preflight_package(stage)
        if not report.ok:
            raise BackupRestoreError("无法生成完整包：" + "；".join(report.errors))
        stage.rename(output)
    return output
