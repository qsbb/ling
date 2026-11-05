"""版本与手动 Release 检查的 API 行为。"""

from __future__ import annotations

import pytest

from ling.api.routes import version as version_routes
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_version_endpoints_require_auth(web_env: TestEnv) -> None:
    for path in ("/v1/version", "/v1/version/check-release"):
        response = await web_env.client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"


async def test_version_reports_runtime_metadata(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LING_COMMIT", "7b23702")
    response = await logged_in.client.get("/v1/version")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "v1"
    assert body["current"]
    assert body["commit"] == "7b23702"
    assert "web_manifest_sha256" in body
    assert response.headers["cache-control"] == "no-store"


async def test_version_commit_can_be_absent(logged_in: TestEnv, monkeypatch) -> None:
    monkeypatch.delenv("LING_COMMIT", raising=False)
    response = await logged_in.client.get("/v1/version")
    assert response.status_code == 200
    assert response.json()["commit"] is None


async def _patch_fetch(monkeypatch, result) -> None:
    async def fake():
        return result

    monkeypatch.setattr(version_routes, "_fetch_latest_release", fake)


async def test_check_release_no_release_is_not_500(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patch_fetch(monkeypatch, ("no_release", None))
    response = await logged_in.client.get("/v1/version/check-release")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "no_release"
    assert body["latest"] is None
    assert body["is_newer"] is None
    assert response.headers["cache-control"] == "no-store"


async def test_check_release_network_failure_is_sanitized(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom():
        raise RuntimeError("secret-token-and-upstream-detail")

    monkeypatch.setattr(version_routes, "_fetch_latest_release", boom)
    response = await logged_in.client.get("/v1/version/check-release")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"
    assert "secret-token" not in response.text
    assert "upstream-detail" not in response.text


async def test_check_release_newer_release(logged_in: TestEnv, monkeypatch) -> None:
    await _patch_fetch(
        monkeypatch,
        (
            "ok",
            {
                "tag_name": "v9.9.9",
                "published_at": "2026-10-30T00:00:00Z",
                "html_url": "https://github.com/qsbb/ling/releases/tag/v9.9.9",
            },
        ),
    )
    response = await logged_in.client.get("/v1/version/check-release")
    body = response.json()
    assert body["status"] == "ok"
    assert body["latest"] == "v9.9.9"
    assert body["is_newer"] is True
    assert body["published_at"] == "2026-10-30T00:00:00Z"
    assert body["html_url"] == "https://github.com/qsbb/ling/releases/tag/v9.9.9"


async def test_check_release_older_release_is_not_newer(logged_in: TestEnv, monkeypatch) -> None:
    await _patch_fetch(
        monkeypatch,
        ("ok", {"tag_name": "0.0.1", "published_at": None, "html_url": None}),
    )
    response = await logged_in.client.get("/v1/version/check-release")
    assert response.json()["is_newer"] is False


async def test_check_release_arbitrary_tag_is_unknown(logged_in: TestEnv, monkeypatch) -> None:
    await _patch_fetch(
        monkeypatch,
        ("ok", {"tag_name": "nightly-build", "published_at": None, "html_url": None}),
    )
    response = await logged_in.client.get("/v1/version/check-release")
    body = response.json()
    assert body["status"] == "ok"
    assert body["latest"] == "nightly-build"
    assert body["is_newer"] is None


async def test_check_release_rejects_foreign_link(logged_in: TestEnv, monkeypatch) -> None:
    await _patch_fetch(
        monkeypatch,
        (
            "ok",
            {
                "tag_name": "v9.9.9",
                "published_at": None,
                "html_url": "javascript:alert(1)",
            },
        ),
    )
    response = await logged_in.client.get("/v1/version/check-release")
    assert response.json()["html_url"] is None
