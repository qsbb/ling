"""Ling 整包备份与导入恢复的核心逻辑。

这个模块只做可被普通 Python 验证的事：描述完整包清单、校验包内容、
生成预检报告、构造隔离恢复命令。任何会启动容器/覆盖数据的动作都集中在
``commands`` 里生成命令并由宿主执行器调用，核心函数本身不碰生产状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

FORMAT_VERSION = "ling-full-backup/v1"
MANIFEST_NAME = "manifest.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_FILES = (
    "database.dump",
    "media.tar",
    "config/deploy.env",
    "config/compose.yaml",
    "state/tool-plugins.json",
    "state/persona.json",
    "artifacts/app-image.json",
)
OPTIONAL_SECRETS = ("secrets/root.key",)
SENSITIVE_ENV_KEYS = (
    "LING_ROOT_KEY",
    "POSTGRES_PASSWORD",
    "LING_BOOTSTRAP_ADMIN_PASSWORD",
    "LING_QQBOT_CLIENT_SECRET",
    "LING_QQBOT_BOT_SECRET",
    "LING_ONEBOT_ACCESS_TOKEN",
    "LING_ONEBOT_SECRET",
    "LING_EMBEDDING_API_KEY",
    "LING_RERANK_API_KEY",
    "LING_BRAVE_SEARCH_API_KEY",
)


class BackupRestoreError(ValueError):
    """清单或备份包不满足可恢复性判据。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") and len(line) > 7:
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


def redact_env(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        key = stripped.split("=", 1)[0].split("export ", 1)[-1].strip()
        lines.append(f"{key}=<redacted>" if key in SENSITIVE_ENV_KEYS else line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def detect_artifact_mode(manifest: dict[str, Any]) -> str:
    artifact = manifest.get("artifact") or {}
    mode = artifact.get("mode")
    if mode in {"image", "source"}:
        return mode
    raise BackupRestoreError("artifact.mode 必须是 image 或 source")


@dataclass
class PreflightReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


def load_manifest(package_dir: Path) -> tuple[dict[str, Any], PreflightReport]:
    path = package_dir / MANIFEST_NAME
    report = PreflightReport(ok=False)
    if not path.is_file():
        report.errors.append(f"缺少 {MANIFEST_NAME}")
        return {}, report
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        report.errors.append(f"清单无法解析：{exc}")
        return {}, report
    if not isinstance(manifest, dict):
        report.errors.append("清单顶层必须是 JSON 对象")
        return {}, report
    return manifest, report


def preflight_package(
    package_dir: Path,
    *,
    target_version: str | None = None,
    target_arch: str | None = None,
    free_bytes: int | None = None,
    available_image_digests: Iterable[str] = (),
) -> PreflightReport:
    """在切换任何实例之前校验包；只读，不执行外部命令。"""
    manifest, report = load_manifest(package_dir)
    report.manifest = manifest
    if not manifest:
        return report

    if manifest.get("format_version") != FORMAT_VERSION:
        report.errors.append(
            f"format_version 不是 {FORMAT_VERSION}：{manifest.get('format_version')!r}"
        )

    files = manifest.get("files")
    if not isinstance(files, dict):
        report.errors.append("files 必须是文件名 → sha256 的对象")
        files = {}

    for name in REQUIRED_FILES:
        path = package_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            report.errors.append(f"必需文件缺失或为空：{name}")
            continue
        expected = files.get(name)
        if not isinstance(expected, str) or not DIGEST_RE.fullmatch(expected):
            report.errors.append(f"files.{name} 必须是 sha256:<64 hex>")
            continue
        actual = sha256_file(path)
        if actual != expected.split(":", 1)[1]:
            report.errors.append(f"哈希不匹配：{name}")

    for name in OPTIONAL_SECRETS:
        expected = files.get(name)
        if expected is None:
            continue
        path = package_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            report.errors.append(f"清单声明的密钥材料缺失或为空：{name}")
        elif not isinstance(expected, str) or not DIGEST_RE.fullmatch(expected):
            report.errors.append(f"files.{name} 必须是 sha256:<64 hex>")
        elif sha256_file(path) != expected.split(":", 1)[1]:
            report.errors.append(f"密钥材料哈希不匹配：{name}")

    secrets = manifest.get("secrets") or {}
    if not isinstance(secrets, dict):
        report.errors.append("secrets 必须是对象")
    else:
        if secrets.get("root_key_source") not in {"file", "env", "managed"}:
            report.errors.append("secrets.root_key_source 必须是 file/env/managed")
        if not isinstance(secrets.get("credential_scope"), list):
            report.errors.append("secrets.credential_scope 必须是数组")

    version = manifest.get("version") or {}
    if not isinstance(version, dict):
        report.errors.append("version 必须是对象")
    else:
        for key in ("app", "commit", "migration", "config_revision"):
            if not version.get(key):
                report.errors.append(f"version.{key} 缺失")
        if target_version and str(version.get("app")) != target_version:
            report.errors.append(
                f"版本不匹配：清单 {version.get('app')!r}，目标 {target_version!r}"
            )

    artifact = manifest.get("artifact") or {}
    if not isinstance(artifact, dict):
        report.errors.append("artifact 必须是对象")
    else:
        mode = detect_artifact_mode(manifest)
        if mode == "image":
            digest = artifact.get("digest")
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                report.errors.append("镜像 artifact.digest 必须是 sha256:<64 hex>")
            arch = artifact.get("platform")
            if not arch:
                report.errors.append("镜像 artifact.platform 缺失")
            elif target_arch and str(arch) != target_arch:
                report.errors.append(f"架构不匹配：清单 {arch!r}，目标 {target_arch!r}")
            available = set(available_image_digests)
            if available and digest not in available:
                report.errors.append(f"镜像 digest 不可用：{digest}")
        else:
            for key in ("release_id", "commit", "sha256"):
                if not artifact.get(key):
                    report.errors.append(f"源码 artifact.{key} 缺失")

    scope = manifest.get("scope") or {}
    if not isinstance(scope, dict):
        report.errors.append("scope 必须是对象")
    else:
        for key in ("database", "media", "plugins", "persona", "config"):
            if key not in scope:
                report.errors.append(f"scope.{key} 缺失")
        external = scope.get("external_services") or []
        if not isinstance(external, list):
            report.errors.append("scope.external_services 必须是数组")

    requested = (manifest.get("resources") or {}).get("required_bytes")
    if isinstance(requested, int) and requested > 0:
        if free_bytes is not None and free_bytes < requested:
            report.errors.append(f"空间不足：需要 {requested}，可用 {free_bytes}")
        elif free_bytes is None:
            report.warnings.append("未提供目标可用空间，无法校验空间预检")

    integrity = manifest.get("integrity") or {}
    if integrity.get("tombstones_state") != "ok":
        report.errors.append("撤回墓碑导出状态不是 ok")

    report.ok = not report.errors
    return report


def _shell_join(args: Iterable[str]) -> str:
    import shlex

    return " ".join(shlex.quote(str(arg)) for arg in args)


def build_restore_plan(
    package_dir: Path,
    *,
    install_dir: Path,
    compose_files: list[Path],
    env_file: Path,
    project: str,
    target_db: str,
    rollback_dir: Path,
) -> dict[str, Any]:
    """生成宿主单命令恢复入口使用的步骤；不执行任何步骤。"""
    if not SAFE_NAME.fullmatch(project):
        raise BackupRestoreError(f"非法 Compose 项目名：{project}")
    if not SAFE_NAME.fullmatch(target_db):
        raise BackupRestoreError(f"非法目标库名：{target_db}")
    file_group: list[str] = []
    for path in compose_files:
        file_group.extend(["-f", str(path)])
    group = ["docker", "compose", "-p", project, *file_group, "--env-file", str(env_file)]
    dump = package_dir / "database.dump"
    media = package_dir / "media.tar"
    steps = [
        {
            "name": "preflight",
            "command": _shell_join(
                [
                    "python3",
                    "-m",
                    "ling.backup_restore.cli",
                    "preflight",
                    str(package_dir),
                ]
            ),
        },
        {
            "name": "lock",
            "command": f"mkdir {_shell_join([str(install_dir / '.restore.lock')])}",
        },
        {
            "name": "rollback-point",
            "command": _shell_join(
                [
                    "python3",
                    "-m",
                    "ling.backup_restore.rollback",
                    "create",
                    str(install_dir),
                    str(rollback_dir),
                ]
            ),
        },
        {
            "name": "restore-database",
            "command": _shell_join(
                [
                    *group,
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--clean",
                    "--if-exists",
                    "-U",
                    "ling",
                    "-d",
                    target_db,
                ]
            )
            + f" < {_shell_join([str(dump)])}",
        },
        {
            "name": "restore-media",
            "command": _shell_join(
                [
                    "python3",
                    "-m",
                    "ling.backup_restore.restore_volume",
                    str(media),
                    str(install_dir),
                ]
            ),
        },
        {
            "name": "switch-artifact",
            "command": _shell_join(
                [
                    "python3",
                    "-m",
                    "ling.backup_restore.switch_artifact",
                    str(package_dir),
                    str(install_dir),
                ]
            ),
        },
        {"name": "start", "command": _shell_join([*group, "up", "-d"])},
        {
            "name": "health",
            "command": _shell_join([*group, "ps"]),
        },
    ]
    return {
        "package": str(package_dir),
        "install_dir": str(install_dir),
        "project": project,
        "target_db": target_db,
        "rollback_dir": str(rollback_dir),
        "steps": steps,
    }


def estimate_required_bytes(package_dir: Path) -> int:
    total = 0
    for path in package_dir.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    # 恢复需要同时容纳解包后的数据卷、数据库和回退点；按包大小的三倍估算，
    # 下限 1 GiB，确保即使小包也不会给出过于乐观的空间判据。
    return max(total * 3, 1024 * 1024 * 1024)


def current_runtime_arch() -> str:
    machine = platform.machine().lower()
    normalized = {"arm64": "linux/arm64", "aarch64": "linux/arm64", "x86_64": "linux/amd64"}
    return normalized.get(machine, f"linux/{machine}")
