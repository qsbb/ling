"""Verify the shared control-plane gates through real authentication and HTTP."""

import pytest
from sqlalchemy import func, select

from ling.contracts.v1 import ErrorResponse
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import ConfigRevision
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

_DRAFT = "/v1/settings/draft"
_BODY = {"log_access_denials": False}


async def _draft_count() -> int:
    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(ConfigRevision))).scalar_one()


async def _login(env: TestEnv, role: str) -> None:
    password = "test-account-password"
    async with session_scope() as session:
        await IdentityService(session).create_user(username=f"test_{role}", password=password, role=role)
    response = await env.client.post(
        "/v1/auth/login", json={"username": f"test_{role}", "password": password}
    )
    assert response.status_code == 200 and response.json()["user"]["role"] == role


async def test_settings_write_only_uses_origin_for_the_selected_cookie(logged_in: TestEnv) -> None:
    client = logged_in.client
    token = client.cookies.get(logged_in.settings.cookie_name)
    assert token
    previous_count = await _draft_count()

    # A plain string body must not be accepted where a JSON object is required.
    wrong_format = await client.put(_DRAFT, content="{}", headers={"Content-Type": "text/plain"})
    assert wrong_format.status_code == 422
    assert ErrorResponse.model_validate(wrong_format.json()).error.detail == "content_type_required"

    # The same signed-in Cookie must not authorize a write with an absent or
    # different origin, even when the request carries a valid Bearer token too.
    missing = client.build_request("PUT", _DRAFT, json=_BODY)
    del missing.headers["Origin"]
    for response in (
        await client.send(missing),
        await client.put(
            _DRAFT, json=_BODY,
            headers={"Origin": "https://example.invalid", "Authorization": f"Bearer {token}"},
        ),
    ):
        assert response.status_code == 403
        assert ErrorResponse.model_validate(response.json()).error.detail == "bad_origin"
    assert await _draft_count() == previous_count

    cookie_write = await client.put(_DRAFT, json=_BODY)
    assert cookie_write.status_code == 200, cookie_write.text
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}"}
    del client.headers["Origin"]
    bearer_write = await client.put(_DRAFT, json=_BODY, headers=bearer)
    assert bearer_write.status_code == 200, bearer_write.text
    bearer_with_origin = await client.put(
        _DRAFT, json=_BODY, headers={**bearer, "Origin": "https://example.invalid"}
    )
    assert bearer_with_origin.status_code == 200, bearer_with_origin.text
    assert await _draft_count() == previous_count + 3


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_non_owner_retains_personal_apis_and_log_role(web_env: TestEnv, role: str) -> None:
    await _login(web_env, role)
    client = web_env.client
    previous_count = await _draft_count()
    forbidden = await client.put(_DRAFT, json=_BODY)
    assert forbidden.status_code == 403
    assert ErrorResponse.model_validate(forbidden.json()).error.detail == "not_owner"
    assert await _draft_count() == previous_count

    for path in ("/v1/health/ops", "/v1/health/model", "/v1/health/stats"):
        assert (await client.get(path)).status_code == 403
    assert (await client.get("/v1/logs")).status_code == (200 if role == "admin" else 403)

    del client.headers["Origin"]
    assert (await client.post("/v1/sessions", json={"title": "Test own session"})).status_code == 201
    assert (await client.put("/v1/settings/preferences", json={"timezone": "UTC"})).status_code == 200
    assert (await client.get("/v1/memory")).status_code == 200
