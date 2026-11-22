"""整包备份与恢复核心逻辑的单元测试。

这些测试不依赖 Docker；它们证明清单、预检和隔离恢复动作能拒绝假备份，
并保护用户要求的反例语义。
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from ling.backup_restore import (
    FORMAT_VERSION,
    BackupRestoreError,
    build_restore_plan,
    current_runtime_arch,
    preflight_package,
    redact_env,
)
from ling.backup_restore.package import build_full_package
from ling.backup_restore.rollback import (
    create_rollback_point,
    restore_media_archive,
    switch_artifact,
)


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


@pytest.fixture
def source(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "dump": tmp_path / "src" / "database.dump",
        "media": tmp_path / "src" / "media.tar",
        "root": tmp_path / "src" / "root.key",
        "env": tmp_path / "src" / "deploy.env",
        "compose": tmp_path / "src" / "compose.yaml",
        "plugins": tmp_path / "src" / "tool-plugins.json",
        "persona": tmp_path / "src" / "persona.json",
    }
    for name, path in paths.items():
        _write(path, f"{name}-content\n")
    with tarfile.open(paths["media"], "w") as archive:
        info = tarfile.TarInfo("media/sample.txt")
        payload = b"media-bytes"
        info.size = len(payload)
        archive.addfile(info, __import__("io").BytesIO(payload))
    return paths


@pytest.fixture
def package(tmp_path: Path, source: dict[str, Path]) -> Path:
    return build_full_package(
        output=tmp_path / "package",
        dump=source["dump"],
        media_tar=source["media"],
        root_key=source["root"],
        deploy_env=source["env"],
        compose_file=source["compose"],
        tool_plugins=source["plugins"],
        persona_file=source["persona"],
        app_version="0.0.14",
        commit="bd5ac914388abce6b7ae2be721dd58d64d7b7ce7",
        migration="0082_live_sessions",
        config_revision="cfg-7",
        artifact={
            "mode": "image",
            "digest": "sha256:" + "a" * 64,
            "platform": current_runtime_arch(),
        },
        scope={"external_services": ["model-provider"]},
    )


def test_preflight_accepts_a_complete_package(package: Path) -> None:
    report = preflight_package(
        package,
        target_version="0.0.14",
        target_arch=current_runtime_arch(),
        free_bytes=10 * 1024 * 1024 * 1024,
        available_image_digests=["sha256:" + "a" * 64],
    )
    assert report.ok, report.errors
    assert "model-provider" in report.manifest["scope"]["external_services"]


def test_preflight_rejects_missing_file(package: Path) -> None:
    (package / "state" / "tool-plugins.json").unlink()
    report = preflight_package(package)
    assert not report.ok
    assert any("state/tool-plugins.json" in error for error in report.errors)


def test_preflight_rejects_hash_mismatch(package: Path) -> None:
    (package / "database.dump").write_bytes(b"tampered")
    report = preflight_package(package)
    assert not report.ok
    assert any("哈希不匹配：database.dump" in error for error in report.errors)


def test_preflight_rejects_version_mismatch(package: Path) -> None:
    report = preflight_package(package, target_version="0.0.15")
    assert not report.ok
    assert any("版本不匹配" in error for error in report.errors)


def test_preflight_rejects_arch_mismatch(package: Path) -> None:
    wrong = "linux/amd64" if current_runtime_arch() != "linux/amd64" else "linux/arm64"
    report = preflight_package(package, target_arch=wrong)
    assert not report.ok
    assert any("架构不匹配" in error for error in report.errors)


def test_preflight_rejects_unavailable_digest(package: Path) -> None:
    report = preflight_package(
        package, available_image_digests=["sha256:" + "b" * 64]
    )
    assert not report.ok
    assert any("镜像 digest 不可用" in error for error in report.errors)


def test_preflight_rejects_low_disk(package: Path) -> None:
    report = preflight_package(package, free_bytes=1)
    assert not report.ok
    assert any("空间不足" in error for error in report.errors)


def test_preflight_rejects_missing_tombstone_state(package: Path) -> None:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest["integrity"]["tombstones_state"] = "FAILED"
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = preflight_package(package)
    assert not report.ok
    assert any("墓碑" in error for error in report.errors)


def test_redact_env_only_by_field_name() -> None:
    text = "LING_ROOT_KEY=custom-secret\nPOSTGRES_PASSWORD=pw\nPLAIN_LABEL=sk-looking\n"
    redacted = redact_env(text)
    assert "custom-secret" not in redacted
    assert "pw" not in redacted
    assert "PLAIN_LABEL=sk-looking" in redacted


def test_restore_plan_contains_preflight_lock_rollback_and_health(package: Path, tmp_path: Path) -> None:
    plan = build_restore_plan(
        package,
        install_dir=tmp_path / "install",
        compose_files=[tmp_path / "compose.yaml"],
        env_file=tmp_path / ".env",
        project="ling_drill",
        target_db="ling_drill",
        rollback_dir=tmp_path / "rollback",
    )
    names = [step["name"] for step in plan["steps"]]
    assert names == [
        "preflight",
        "lock",
        "rollback-point",
        "restore-database",
        "restore-media",
        "switch-artifact",
        "start",
        "health",
    ]
    assert "--clean" in plan["steps"][3]["command"]
    assert "ling_drill" == plan["project"] == plan["target_db"]


def test_rollback_point_records_hashes(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _write(install / "data" / "root.key", "key")
    files = create_rollback_point(install, tmp_path / "rollback")
    assert files["data/root.key"] == _hex(b"key")
    assert (tmp_path / "rollback" / "rollback-manifest.json").is_file()


def test_restore_media_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"bad"
        info.size = len(payload)
        tar.addfile(info, __import__("io").BytesIO(payload))
    with pytest.raises(BackupRestoreError):
        restore_media_archive(archive, tmp_path / "target")


def test_switch_artifact_requires_successfully_preflighted_package(
    package: Path, tmp_path: Path
) -> None:
    (package / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BackupRestoreError):
        switch_artifact(package, tmp_path / "target")


def test_manifest_uses_current_format(package: Path) -> None:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["version"]["migration"] == "0082_live_sessions"
