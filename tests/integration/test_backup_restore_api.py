"""Test backup status, file inspection and host actions over authenticated HTTP."""

import tarfile
from pathlib import Path

import pytest

from ling.backup_restore import current_runtime_arch
from ling.backup_restore.package import build_full_package
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
_BASE = "/v1/backup-restore"


@pytest.fixture
def backup_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    placeholder = source / "material"
    placeholder.write_bytes(b"fixture")
    env = source / ".env"
    env.write_text("PROVIDER_TEST_KEY=private-test-value\n", encoding="utf-8")
    archive = source / "runtime.tar"
    with tarfile.open(archive, "w") as bundle:
        bundle.add(placeholder, arcname="test-runtime-file")
    root = tmp_path / "packages"
    monkeypatch.setenv("LING_BACKUP_RESTORE_PACKAGES_DIR", str(root))
    return build_full_package(
        output=root / "ling-acceptance",
        dump=placeholder,
        media_tar=archive,
        deploy_env=env,
        compose_file=placeholder,
        root_key=placeholder,
        tool_plugins=placeholder,
        persona_file=placeholder,
        runtime_archive=archive,
        app_version="0.0.14",
        commit="bd5ac914388abce6b7ae2be721dd58d64d7b7ce7",
        migration="0082_live_sessions",
        config_revision="test-revision",
        artifact={"mode": "image", "digest": "sha256:" + "a" * 64,
                  "platform": current_runtime_arch()},
        scope={},
    )


async def test_owner_can_validate_files_without_receiving_secret_configuration(
    logged_in: TestEnv, backup_package: Path
) -> None:
    client = logged_in.client
    listing = await client.get(f"{_BASE}/status")
    assert listing.status_code == 200, listing.text
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()["packages"][0]["name"] == backup_package.name
    assert listing.json()["operations_available"] is False

    inspect = await client.get(f"{_BASE}/packages/{backup_package.name}/inspect")
    assert inspect.status_code == 200, inspect.text
    assert inspect.headers["cache-control"] == "no-store"
    assert inspect.json()["ok"] is True
    assert "deploy_env" not in inspect.json() and "private-test-value" not in inspect.text

    result = await client.post(
        f"{_BASE}/imports/preflight", json={"package": str(backup_package)}
    )
    assert result.status_code == 422
    for endpoint, body in (
        ("/backups", {"note": "sample"}),
        ("/imports/confirm", {"package": backup_package.name, "confirm": True}),
    ):
        result = await client.post(_BASE + endpoint, json=body)
        assert result.status_code == 503, result.text
        assert result.json()["error"]["code"] == "unsupported_capability"


async def test_backup_reads_require_owner(web_env: TestEnv, backup_package: Path) -> None:
    client = web_env.client
    paths = (
        f"{_BASE}/status",
        f"{_BASE}/packages/{backup_package.name}/inspect",
    )
    for path in paths:
        assert (await client.get(path)).status_code == 401
    password = "test-account-password"
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="backup_member", password=password, role="member"
        )
    login = await client.post(
        "/v1/auth/login", json={"username": "backup_member", "password": password}
    )
    assert login.status_code == 200, login.text
    for path in paths:
        assert (await client.get(path)).status_code == 403
