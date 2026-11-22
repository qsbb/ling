"""Owner backup operations through the normal HTTP routes."""

import io
import json
import zipfile
from pathlib import Path

import pytest

from ling.backup_restore import FILES, FORMAT, write_channel_json
from tests.integration.conftest import TestEnv

BASE = "/v1/backup-restore"
pytestmark = pytest.mark.w0_web


@pytest.fixture
def backup_channel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LING_UPDATE_REQUESTS_DIR", str(tmp_path))
    monkeypatch.setenv("LING_MAX_BODY_BYTES", "4096")
    channel = tmp_path / "backup-restore"
    (channel / "archives").mkdir(parents=True)
    (channel / "jobs").mkdir()
    write_channel_json(channel / "worker.json", {"status": "ready"})
    return channel


def archive_fixture() -> bytes:
    manifest = {
        "format": FORMAT,
        "files": dict.fromkeys(FILES, "a" * 64),
        "images": dict.fromkeys(("api", "db", "worker"), "sha256:" + "b" * 64),
        "version": "1.2.3",
        "commit": "fixture",
        "migration": "0001",
        "created_at": "2026-01-01T12:00:00Z",
    }
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name in FILES:
            archive.writestr(name, b"fixture-stream" * 512)
    return target.getvalue()


async def test_owner_import_download_and_queue_restore(
    backup_channel: Path, logged_in: TestEnv
) -> None:
    client = logged_in.client
    data = archive_fixture()
    over_limit = await client.post("/v1/settings/draft", content=data)
    assert over_limit.status_code == 413
    invalid = await client.post(f"{BASE}/upload", content=b"incomplete file")
    assert invalid.status_code == 422
    assert not list((backup_channel / "archives").iterdir())
    response = await client.post(f"{BASE}/upload", content=data)
    assert response.status_code == 201, response.text
    archive = response.json()
    assert archive["version"] == "1.2.3"
    assert archive["bytes"] == len(data)

    download = await client.get(f"{BASE}/archives/{archive['name']}")
    assert download.status_code == 200
    assert download.content == data
    restore = await client.post(f"{BASE}/jobs", json={"action": "restore", "name": archive["name"]})
    assert restore.status_code == 202, restore.text
    status = await client.get(f"{BASE}/status")
    assert status.json() == {"available": True, "archives": [archive], "jobs": [restore.json()]}
    assert status.headers["cache-control"] == "no-store"


async def test_backup_actions_require_owner_and_active_host(
    backup_channel: Path, web_env: TestEnv
) -> None:
    client = web_env.client
    assert (await client.get(f"{BASE}/status")).status_code == 401
    assert (await client.post(f"{BASE}/upload", content=archive_fixture())).status_code == 401
    login = await client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert login.status_code == 200
    assert (await client.get(f"{BASE}/status")).json()["available"]
    (backup_channel / "worker.json").unlink()
    response = await client.post(f"{BASE}/jobs", json={"action": "create"})
    assert response.status_code == 503
    assert not list((backup_channel / "jobs").iterdir())
