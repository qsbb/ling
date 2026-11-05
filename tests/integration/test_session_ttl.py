"""登录会话 TTL 与服务端过期判定（B3）。

## 修的是什么

`IdentityService.login()` 原来硬编码 ``timedelta(days=14)``，而
``Settings.session_ttl_seconds``（``LING_SESSION_TTL``）只被用来设 cookie 的
``max_age``。服务端判定会话是否有效（``resolve_token``）读的是**库里那一列**
``auth_sessions.expires_at``，它永远是 +14 天。于是：

- 把 TTL 调小，只是让浏览器不再自动带 cookie。持有 token 的人
  （被窃 cookie / 重放 / 直接带 ``Authorization: Bearer``）**仍能用满 14 天**。
- 把 TTL 调大完全无效：DB 会先到期。
- 全仓**没有任何过期会话清理**，`auth_sessions` 只增不减。

下面每一条都**通过 HTTP 端点与数据库行**验证，而不是只看 cookie 头：
cookie 的 ``max_age`` 只是浏览器行为，服务端判定才是真正的安全边界。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from ling.infra.db import session_scope
from ling.infra.models import AuthSession
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CREDENTIALS = {"username": "tester", "password": "tester-password"}


@pytest.fixture
def short_ttl(monkeypatch: pytest.MonkeyPatch) -> int:
    """把 ``LING_SESSION_TTL`` 设成 1 秒。

    ``Settings`` 的这一项是 ``default_factory``，在**实例化时**读环境变量；
    ``web_env`` 夹具不显式传这一项，所以这里的 monkeypatch 会生效。
    fixture 的实例化顺序按参数顺序：``short_ttl`` 必须在 ``web_env`` 之前。
    """
    monkeypatch.setenv("LING_SESSION_TTL", "1")
    return 1


@pytest.fixture
def default_ttl(monkeypatch: pytest.MonkeyPatch) -> int:
    """清掉环境里的 ``LING_SESSION_TTL``，让默认值（14 天）生效。"""
    monkeypatch.delenv("LING_SESSION_TTL", raising=False)
    return 60 * 60 * 24 * 14


@pytest_asyncio.fixture
async def ttl_env(short_ttl: int, web_env: TestEnv) -> TestEnv:
    return web_env


@pytest_asyncio.fixture
async def default_env(default_ttl: int, web_env: TestEnv) -> TestEnv:
    return web_env


async def _user_id(env: TestEnv) -> str:
    """取引导管理员的 ``User.id``（``AuthSession.user_id`` 指向它，不是 subject）。"""
    from sqlalchemy import select

    from ling.infra.models import User

    async with session_scope() as session:
        return (
            await session.execute(
                select(User.id).where(User.username == CREDENTIALS["username"])
            )
        ).scalar_one()


async def _session_row(token_hash: str) -> AuthSession:
    from sqlalchemy import select

    async with session_scope() as session:
        return (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == token_hash)
            )
        ).scalar_one()


async def test_login_derives_db_expiry_from_settings(ttl_env: TestEnv) -> None:
    """配 ``LING_SESSION_TTL=1`` → 库里那一列就是 +1 秒，不是 +14 天。

    这是整个缺陷的核心：cookie 的 max_age 与服务端的到期时刻必须**同源**。
    """
    from ling.infra.security import hash_token

    assert ttl_env.settings.session_ttl_seconds == 1

    response = await ttl_env.client.post("/v1/auth/login", json=CREDENTIALS)
    assert response.status_code == 200, response.text
    token = response.json() and ttl_env.client.cookies.get(
        ttl_env.settings.cookie_name
    )
    assert token, "登录没有下发 cookie"

    row = await _session_row(hash_token(token))
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    delta = (expires - created).total_seconds()
    assert delta <= 2, (
        f"库里到期时间比登录时间晚 {delta:.0f} 秒，配置的 1 秒没有生效——"
        "服务端判定读的就是这一列，持有 token 的人仍能用满硬编码的 14 天"
    )


async def test_short_ttl_rejects_token_server_side(ttl_env: TestEnv) -> None:
    """TTL=1 秒：1 秒后**服务端**拒绝该 token（Bearer，绕开 cookie 过期）。"""
    from ling.infra.security import hash_token

    response = await ttl_env.client.post("/v1/auth/login", json=CREDENTIALS)
    assert response.status_code == 200, response.text
    token = ttl_env.client.cookies.get(ttl_env.settings.cookie_name)
    assert token

    # 立刻可用
    ok = await ttl_env.client.get(
        "/v1/auth/me", headers={"authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 200, ok.text

    await asyncio.sleep(1.3)

    # **显式带 Bearer**：这条路径不受 cookie 是否被浏览器丢弃影响，
    # 只有服务端读库里那一列才能拦住它。
    expired = await ttl_env.client.get(
        "/v1/auth/me", headers={"authorization": f"Bearer {token}"}
    )
    assert expired.status_code == 401, (
        f"超过 TTL 后 token 仍被接受（{expired.status_code}）——"
        "调小 LING_SESSION_TTL 只影响 cookie，服务端仍按 14 天放行"
    )
    row = await _session_row(hash_token(token))
    assert row.expires_at <= datetime.now(UTC), "库里那一列没有按配置到期"


async def test_default_ttl_is_fourteen_days(default_env: TestEnv) -> None:
    """不配 ``LING_SESSION_TTL`` 时仍是 14 天（默认值不能悄悄变）。"""
    from ling.infra.security import hash_token

    assert default_env.settings.session_ttl_seconds == 60 * 60 * 24 * 14

    response = await default_env.client.post("/v1/auth/login", json=CREDENTIALS)
    assert response.status_code == 200, response.text
    token = default_env.client.cookies.get(default_env.settings.cookie_name)
    assert token

    row = await _session_row(hash_token(token))
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    delta = expires - created
    assert abs(delta - timedelta(days=14)) < timedelta(minutes=1), (
        f"默认 TTL 变成了 {delta}，不是 14 天"
    )


async def test_expired_sessions_are_purged(logged_in: TestEnv) -> None:
    """过期会话能被清理，未到期的（含已吊销未到期的）一条都不许删。

    ``revoked_at`` 是审计事实（"谁在何时被改密踢下线"），它们到期后才一起清掉。
    """
    from ling.domains.identity.service import IdentityService
    from ling.infra.security import new_id

    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            AuthSession(
                id=new_id("sess"),
                user_id=await _user_id(logged_in),
                token_hash=f"deadbeef-{new_id('t')}",
                expires_at=now - timedelta(hours=1),
                user_agent="stale-agent",
            )
        )
        session.add(
            AuthSession(
                id=new_id("sess"),
                user_id=await _user_id(logged_in),
                token_hash=f"revoked-but-alive-{new_id('t')}",
                expires_at=now + timedelta(days=1),
                revoked_at=now - timedelta(minutes=5),
                user_agent="revoked-agent",
            )
        )

    async with session_scope() as session:
        removed = await IdentityService(session).purge_expired_sessions()

    assert removed >= 1, "过期会话没有被清理（表会无限增长）"

    from sqlalchemy import select

    async with session_scope() as session:
        remaining = list(
            (
                await session.execute(
                    select(AuthSession).where(
                        AuthSession.token_hash.like("revoked-but-alive-%")
                    )
                )
            ).scalars()
        )
        expired_left = list(
            (
                await session.execute(
                    select(AuthSession).where(
                        AuthSession.token_hash.like("deadbeef-%")
                    )
                )
            ).scalars()
        )
    assert not expired_left, "已过期的会话仍然留在库里"
    assert remaining, "清理把未到期的已吊销会话也删掉了（审计事实丢失）"


async def test_worker_purge_job_removes_expired_sessions(logged_in: TestEnv) -> None:
    """worker 的 ``session.purge`` 周期任务真的会清理（有派发源、能跑通）。"""
    from ling.infra.security import new_id
    from ling.worker import HANDLERS, PERIODIC_JOBS

    assert "session.purge" in HANDLERS, "清理任务没有注册 handler"
    assert any(job.topic == "session.purge" for job in PERIODIC_JOBS), (
        "清理任务注册了 handler 却没有派发源 —— 等于不存在"
    )

    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            AuthSession(
                id=new_id("sess"),
                user_id=await _user_id(logged_in),
                token_hash=f"purge-job-{new_id('t')}",
                expires_at=now - timedelta(days=3),
                user_agent="worker-purge",
            )
        )

    await HANDLERS["session.purge"]({})

    from sqlalchemy import select

    async with session_scope() as session:
        left = list(
            (
                await session.execute(
                    select(AuthSession).where(
                        AuthSession.token_hash.like("purge-job-%")
                    )
                )
            ).scalars()
        )
    assert not left, "worker 的清理任务跑完了，过期会话还在库里"
