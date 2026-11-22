"""Ling 整包备份的清单、文件校验与恢复计划。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = "ling-full-backup/v1"
MANIFEST_NAME = "manifest.json"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUNTIME_FILES = {"image": "artifacts/app-image.tar", "source": "artifacts/release.tar"}
REQUIRED_FILES = (
    "database.dump",
    "media.tar",
    "config/deploy.env",
    "config/compose.yaml",
    "state/tool-plugins.json",
    "state/persona.json",
    "artifacts/app-image.json",
)
ROOT_KEY_FILE = "secrets/root.key"
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
    artifact = manifest.get("artifact")
    mode = artifact.get("mode") if isinstance(artifact, dict) else None
    if isinstance(mode, str) and mode in RUNTIME_FILES:
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


def _package_file(root: Path, name: str) -> Path:
    path = root
    if root.is_symlink():
        raise BackupRestoreError("备份包目录不能是符号链接")
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink():
            raise BackupRestoreError(f"备份包内不能使用符号链接：{name}")
    return path


def load_manifest(package_dir: Path) -> tuple[dict[str, Any], PreflightReport]:
    report = PreflightReport(ok=False)
    try:
        path = _package_file(package_dir, MANIFEST_NAME)
        if not path.is_file():
            report.errors.append(f"缺少 {MANIFEST_NAME}")
            return {}, report
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.errors.append(f"清单无法解析：{exc}")
        return {}, report
    if not isinstance(manifest, dict):
        report.errors.append("清单顶层必须是 JSON 对象")
        return {}, report
    return manifest, report


def _manifest_object(report: PreflightReport, name: str) -> dict[str, Any]:
    value = report.manifest.get(name)
    if isinstance(value, dict):
        return value
    report.errors.append(f"{name} 必须是对象")
    return {}


def _verify_file(
    package_dir: Path, name: str, digest: object, report: PreflightReport
) -> Path | None:
    try:
        path = _package_file(package_dir, name)
        if not path.is_file() or path.stat().st_size == 0:
            raise BackupRestoreError(f"必需文件缺失或为空：{name}")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise BackupRestoreError(f"files.{name} 必须是 sha256:<64 hex>")
        if f"sha256:{sha256_file(path)}" != digest:
            raise BackupRestoreError(f"哈希不匹配：{name}")
    except (OSError, BackupRestoreError) as exc:
        report.errors.append(str(exc))
        return None
    return path


def _check_runtime_archive(path: Path, report: PreflightReport) -> None:
    try:
        with tarfile.open(path) as archive:
            if any(entry.isfile() for entry in archive):
                return
    except (OSError, tarfile.TarError):
        pass
    report.errors.append("版本产物不是包含文件的 tar 归档")


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
    if report.errors:
        return report

    if manifest.get("format_version") != FORMAT_VERSION:
        report.errors.append(
            f"format_version 不是 {FORMAT_VERSION}：{manifest.get('format_version')!r}"
        )

    files = _manifest_object(report, "files")
    secrets = _manifest_object(report, "secrets")
    root_source = secrets.get("root_key_source")
    if root_source not in ("file", "env"):
        report.errors.append("包内必须包含生效的根密钥材料（file 或 env）")
    if not isinstance(secrets.get("credential_scope"), list):
        report.errors.append("secrets.credential_scope 必须是数组")

    version = _manifest_object(report, "version")
    for key in ("app", "commit", "migration", "config_revision"):
        if not isinstance(version.get(key), str) or not version[key].strip():
            report.errors.append(f"version.{key} 缺失")
    if target_version and version.get("app") != target_version:
        report.errors.append("版本不匹配")

    artifact = _manifest_object(report, "artifact")
    try:
        mode = detect_artifact_mode(manifest)
    except BackupRestoreError as exc:
        report.errors.append(str(exc))
        mode = None

    required = list(REQUIRED_FILES)
    if mode is not None:
        required.append(RUNTIME_FILES[mode])
    if root_source == "file" or ROOT_KEY_FILE in files:
        required.append(ROOT_KEY_FILE)
    if files.keys() - set(required):
        report.errors.append("清单包含未支持的文件路径")
    paths = {}
    for name in required:
        path = _verify_file(package_dir, name, files.get(name), report)
        if path is not None:
            paths[name] = path

    if mode and (runtime_path := paths.get(RUNTIME_FILES[mode])):
        _check_runtime_archive(runtime_path, report)

    env_path = paths.get("config/deploy.env")
    if env_path is not None and root_source in ("file", "env"):
        try:
            has_env_key = bool(_read_env(env_path).get("LING_ROOT_KEY"))
        except (OSError, UnicodeError):
            report.errors.append("无法读取部署配置")
        else:
            if has_env_key != (root_source == "env"):
                report.errors.append("清单密钥来源与部署配置不一致")

    if mode == "image":
        digest = artifact.get("digest")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            report.errors.append("镜像 artifact.digest 必须是 sha256:<64 hex>")
        elif (available := set(available_image_digests)) and digest not in available:
            report.errors.append("镜像 digest 不可用")
        arch = artifact.get("platform")
        if not isinstance(arch, str) or not arch:
            report.errors.append("镜像 artifact.platform 缺失")
        elif target_arch and arch != target_arch:
            report.errors.append("镜像架构不匹配")
    elif mode == "source":
        if not isinstance(artifact.get("release_id"), str) or not artifact["release_id"]:
            report.errors.append("源码 artifact.release_id 缺失")
        if artifact.get("commit") != version.get("commit"):
            report.errors.append("源码产物 commit 与版本清单不一致")
        if not files.get(RUNTIME_FILES[mode]) or artifact.get("sha256") != files[RUNTIME_FILES[mode]]:
            report.errors.append("源码产物 SHA256 与文件清单不一致")

    metadata = paths.get("artifacts/app-image.json")
    if metadata is not None:
        try:
            matches = json.loads(metadata.read_text(encoding="utf-8")) == artifact
        except (OSError, ValueError):
            matches = False
        if not matches:
            report.errors.append("产物描述与清单不一致")

    scope = _manifest_object(report, "scope")
    for key in ("database", "media", "plugins", "persona", "config"):
        if key not in scope:
            report.errors.append(f"scope.{key} 缺失")
    if not isinstance(scope.get("external_services", []), list):
        report.errors.append("scope.external_services 必须是数组")

    requested = _manifest_object(report, "resources").get("required_bytes")
    try:
        measured = max(sum(path.stat().st_size for path in paths.values()) * 3, 1024**3)
    except OSError:
        report.errors.append("校验时备份包发生变化，请重新检查")
        return report
    required_bytes = max(requested, measured) if type(requested) is int else measured
    if free_bytes is not None and free_bytes < required_bytes:
        report.errors.append(f"空间不足：需要 {required_bytes}，可用 {free_bytes}")
    elif free_bytes is None:
        report.warnings.append("未提供目标可用空间")

    if _manifest_object(report, "integrity").get("tombstones_state") != "ok":
        report.errors.append("撤回墓碑导出状态不是 ok")

    report.ok = not report.errors
    return report


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
    """列出恢复所需的阶段和目标，供部署时复核。"""
    if not SAFE_NAME.fullmatch(project):
        raise BackupRestoreError(f"非法 Compose 项目名：{project}")
    if not SAFE_NAME.fullmatch(target_db):
        raise BackupRestoreError(f"非法目标库名：{target_db}")
    return {
        "package": str(package_dir),
        "install_dir": str(install_dir),
        "compose_files": list(map(str, compose_files)),
        "env_file": str(env_file),
        "project": project,
        "target_db": target_db,
        "rollback_dir": str(rollback_dir),
        "apply_ready": False,
        "steps": [
            "preflight",
            "install-lock",
            "rollback-point",
            "stage-database",
            "stage-app-data",
            "verify-runtime",
            "switch-and-check-health",
            "rollback-on-failure",
        ],
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
