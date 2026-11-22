"""Verify backup member integrity before the Docker runtime is involved."""

import json
from zipfile import ZipFile

import pytest
from deploy.backup import restore_full
from deploy.backup.restore_full import (
    Stack,
    restore_snapshot,
    unpack_snapshot,
    verify_rollback_target,
)
from deploy.cli.ling import ComposeGroup

from ling.backup_restore import FILES, FORMAT, BackupError, file_digest


def test_verified_members_are_the_bytes_written_to_the_staging_area(tmp_path):
    bundle_path = tmp_path / "fixture.ling-backup"
    material = tmp_path / "material"
    material.write_bytes(b"fixture")
    manifest = {
        "format": FORMAT,
        "files": dict.fromkeys(FILES, file_digest(material)),
        "images": dict.fromkeys(("api", "db", "worker"), "sha256:" + "b" * 64),
        "version": "1.2.3",
        "commit": "fixture",
        "migration": "0001",
        "created_at": "2026-01-01T12:00:00Z",
    }
    stage = tmp_path / "stage"
    stage.mkdir()
    for contents in (b"fixture", b"changed"):
        with ZipFile(bundle_path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name in FILES:
                archive.writestr(name, contents if name == "database.dump" else b"fixture")
        if contents == b"fixture":
            assert unpack_snapshot(bundle_path, stage) == manifest
            assert (stage / "database.dump").read_bytes() == contents
        else:
            with pytest.raises(BackupError, match="database.dump"):
                unpack_snapshot(bundle_path, stage)


def test_compose_config_reports_effective_environment(monkeypatch, tmp_path):
    stack = Stack(ComposeGroup("fixture", (), tmp_path / ".env"))
    model = {"services": {"api": {"environment": {"LING_ROOT_KEY": "fixture$$suffix"}}}}
    monkeypatch.setattr(Stack, "run", lambda *args: json.dumps(model))

    assert stack.config()["services"]["api"]["environment"]["LING_ROOT_KEY"] == "fixture$suffix"


@pytest.mark.parametrize(
    ("containers", "message"), [("existing-container\n", "已有容器"), ("", "备份包已损坏")],
)
def test_missing_env_checks_project_before_loading_archive(monkeypatch, tmp_path, containers, message):
    stack = Stack(ComposeGroup("fixture", (), tmp_path / ".env"))
    archive = tmp_path / "fixture.ling-backup"
    archive.write_bytes(b"invalid archive")
    calls = []

    def docker(*args):
        calls.append(args)
        return containers

    monkeypatch.setattr(restore_full, "docker", docker)
    with pytest.raises(BackupError, match=message):
        restore_snapshot(stack, archive)

    assert calls == [("ps", "-aq", "--filter", "label=com.docker.compose.project=fixture")]
    assert not stack.group.env_file.exists()


@pytest.mark.parametrize(
    ("old_setting", "compose_setting", "matches"),
    [(None, "1", True), ("", "1", True), (None, "0", False), ("0", "0", True)],
)
def test_rollback_accepts_effective_source_migrations_setting(
    monkeypatch, tmp_path, old_setting, compose_setting, matches,
):
    stack = Stack(ComposeGroup("fixture", (), tmp_path / ".env"))
    image = "fixture-api"
    volume = "fixture_app-data"
    setting = "LING_RUN_MIGRATIONS"
    original = {"api": {
        "Image": image,
        "Mounts": [{"Destination": "/data", "Name": volume}],
        "Config": {"Env": [] if old_setting is None else [f"{setting}={old_setting}"]},
    }}
    model = {
        "services": {"api": {"image": image, "environment": {setting: compose_setting}}},
        "volumes": {"app-data": {"name": volume}},
    }
    monkeypatch.setattr(restore_full, "local_image_id", lambda ref: ref)
    monkeypatch.setattr(Stack, "config", lambda self: model)

    if matches:
        verify_rollback_target(stack, original)
    else:
        with pytest.raises(BackupError, match="运行状态与当前 Compose 配置不同"):
            verify_rollback_target(stack, original)
