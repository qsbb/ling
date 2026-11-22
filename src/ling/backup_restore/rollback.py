"""宿主执行器的最小动作实现。

这些函数只操作**隔离目标位置**，默认没有能力自动碰生产 Compose 项目；
真实切换由显式传入的 install_dir / rollback_dir 决定。这样即使服务起不来，
管理员也能用单独的宿主命令完成恢复，而网页只能触发受控请求。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from pathlib import Path

from . import MANIFEST_NAME, BackupRestoreError, load_manifest, preflight_package


def create_rollback_point(install_dir: Path, rollback_dir: Path) -> dict[str, str]:
    """把当前目标目录按硬链接方式记录为回退点。"""
    if not install_dir.is_dir():
        raise BackupRestoreError(f"安装目录不存在：{install_dir}")
    rollback_dir.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    for path in sorted(install_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(install_dir)
        target = rollback_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
        files[str(rel)] = hashlib.sha256(target.read_bytes()).hexdigest()
    (rollback_dir / "rollback-manifest.json").write_text(
        __import__("json").dumps({"files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return files


def restore_media_archive(media_tar: Path, target_dir: Path) -> list[str]:
    """把包内媒体归档展开到隔离目标目录的 data/media。"""
    if not media_tar.is_file():
        raise BackupRestoreError(f"媒体归档不存在：{media_tar}")
    data_dir = target_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(media_tar, "r:") as archive:
        members = archive.getnames()
        for member in members:
            if member.startswith("/") or ".." in Path(member).parts:
                raise BackupRestoreError(f"归档含不安全路径：{member}")
        archive.extractall(data_dir, filter="data")
    return [name for name in members if not name.endswith("/")]


def switch_artifact(package_dir: Path, target_dir: Path) -> dict[str, object]:
    """把包内受控配置和凭据写入目标目录；不执行容器命令。"""
    report = preflight_package(package_dir)
    if not report.ok:
        raise BackupRestoreError("切换前预检失败：" + "；".join(report.errors))
    manifest = report.manifest or load_manifest(package_dir)[0]
    target_dir.mkdir(parents=True, exist_ok=True)
    for rel in ("config", "state", "artifacts"):
        source = package_dir / rel
        if source.is_dir():
            destination = target_dir / "restored" / rel
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
    secrets = package_dir / "secrets"
    if secrets.is_dir():
        destination = target_dir / "restored" / "secrets"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(secrets, destination)
        for path in destination.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
    return {
        "mode": manifest.get("artifact", {}).get("mode"),
        "version": manifest.get("version", {}).get("app"),
        "target": str(target_dir / "restored"),
    }
