"""渠道账号绑定 API。

**这是 QQ/OneBot 接入的前置条件**：入站事件要先解析成自然人，
未绑定会被拒。而原先绑定**没有任何用户入口**（无 API、无 CLI、文档没写），
用户根本无法使用 OneBot 集成——属于交付阻塞缺陷。
"""

from __future__ import annotations

import pytest

from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

INSTANCE = "onebot-test"
QQ = "20002"


async def test_bind_then_list(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/accounts/bind",
        json={
            "connector_instance": INSTANCE,
            "external_account_id": QQ,
            "proof": "proven",
        },
    )
    assert response.status_code == 200, response.text
    account_id = response.json()["account_id"]

    listing = await logged_in.client.get("/v1/accounts")
    accounts = listing.json()["accounts"]
    assert any(a["account_id"] == account_id for a in accounts)
    assert any(a["proof"] == "proven" for a in accounts)


async def test_bind_defaults_to_pending(logged_in: TestEnv) -> None:
    """默认 pending：绑定不等于证明。

    未证明的绑定不能用于身份解析，否则"自称"某账号就拿到那个人。
    """
    response = await logged_in.client.post(
        "/v1/accounts/bind",
        json={"connector_instance": INSTANCE, "external_account_id": "30003"},
    )
    assert response.status_code == 200
    assert response.json()["proof"] == "pending"


async def test_prove_upgrades_binding(logged_in: TestEnv) -> None:
    created = await logged_in.client.post(
        "/v1/accounts/bind",
        json={"connector_instance": INSTANCE, "external_account_id": "40004"},
    )
    account_id = created.json()["account_id"]
    proved = await logged_in.client.post(f"/v1/accounts/{account_id}/prove")
    assert proved.status_code == 200
    assert proved.json()["proof"] == "proven"


async def test_prove_unknown_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.post("/v1/accounts/acct_nope/prove")
    assert response.status_code == 404


async def test_unbind_revokes(logged_in: TestEnv) -> None:
    created = await logged_in.client.post(
        "/v1/accounts/bind",
        json={
            "connector_instance": INSTANCE,
            "external_account_id": "50005",
            "proof": "proven",
        },
    )
    account_id = created.json()["account_id"]
    deleted = await logged_in.client.delete(f"/v1/accounts/{account_id}")
    assert deleted.status_code == 200

    listing = await logged_in.client.get("/v1/accounts")
    match = next(a for a in listing.json()["accounts"] if a["account_id"] == account_id)
    assert match["revoked_at"] is not None, "解绑没有记录 revoked_at"


async def test_unbind_makes_resolution_fail(logged_in: TestEnv) -> None:
    """解绑后不能再解析成该自然人（这是解绑的意义）。"""
    from ling.domains.identity.service import IdentityService

    created = await logged_in.client.post(
        "/v1/accounts/bind",
        json={
            "connector_instance": INSTANCE,
            "external_account_id": "60006",
            "proof": "proven",
        },
    )
    account_id = created.json()["account_id"]
    async with session_scope() as session:
        assert (
            await IdentityService(session).resolve_account(INSTANCE, "60006")
            == logged_in.subject_id
        )
    await logged_in.client.delete(f"/v1/accounts/{account_id}")
    async with session_scope() as session:
        assert await IdentityService(session).resolve_account(INSTANCE, "60006") is None


async def test_pending_binding_does_not_resolve(logged_in: TestEnv) -> None:
    from ling.domains.identity.service import IdentityService

    await logged_in.client.post(
        "/v1/accounts/bind",
        json={"connector_instance": INSTANCE, "external_account_id": "70007"},
    )
    async with session_scope() as session:
        assert await IdentityService(session).resolve_account(INSTANCE, "70007") is None


async def test_bind_conflict_across_subjects(logged_in: TestEnv) -> None:
    """同一个渠道账号不能同时属于两个人。"""
    await logged_in.client.post(
        "/v1/accounts/bind",
        json={
            "connector_instance": INSTANCE,
            "external_account_id": "80008",
            "proof": "proven",
        },
    )
    # 换一个自然人去绑同一个账号
    from ling.domains.identity.service import IdentityService

    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="other_binder", password="pw-not-used"
        )
        other_subject = other.subject_id
    from ling.api.errors import LingError

    async with session_scope() as session:
        with pytest.raises(LingError) as exc:
            await IdentityService(session).bind_account(
                subject_id=other_subject,
                connector_instance=INSTANCE,
                external_account_id="80008",
                proof="proven",
            )
    assert exc.value.status_code == 409


async def test_invalid_proof_rejected(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/accounts/bind",
        json={
            "connector_instance": INSTANCE,
            "external_account_id": "90009",
            "proof": "whatever",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/accounts"),
        ("post", "/v1/accounts/bind"),
        ("post", "/v1/accounts/acct_x/prove"),
        ("delete", "/v1/accounts/acct_x"),
    ],
)
async def test_account_endpoints_require_auth(
    web_env: TestEnv, method: str, path: str
) -> None:
    handler = getattr(web_env.client, method)
    response = await handler(path, json={}) if method == "post" else await handler(path)
    assert response.status_code == 401, f"{method.upper()} {path} 未要求鉴权"
