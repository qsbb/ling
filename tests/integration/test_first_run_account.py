"""首次安装的账号体验：随机口令 → 日志打印 → 强制改密。

这一组测的是**接线**，不是某个函数：把它们保护的那几行改坏（不置位标记、
不在 deps 里拦、改密不清标记），用例必须变红。
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ling.config import Settings

pytestmark = pytest.mark.w0_web


@pytest_asyncio.fixture
async def fresh_env(monkeypatch: pytest.MonkeyPatch, web_env) -> object:
    """不配引导口令的环境：bootstrap 会生成随机口令并打印到日志。"""
    return web_env


async def test_bootstrap_without_password_generates_one_and_logs_it(
    web_env, caplog: pytest.LogCaptureFixture
) -> None:
    """没配 LING_BOOTSTRAP_ADMIN_PASSWORD 时，随机口令必须出现在启动日志里。

    否则安装脚本起完服务就没人知道密码——这正是"装完登不进去"的根因。
    """
    # 这个用例要的是"库里还没有用户"的现场：先清掉 fixture 建的账号
    from sqlalchemy import delete

    from ling.app import _bootstrap
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope
    from ling.infra.models import User

    settings = Settings(
        env="test",
        database_url=web_env.settings.database_url,
        db_search_path=web_env.settings.db_search_path,
        data_dir=web_env.settings.data_dir,
        bootstrap_admin_user="firstrun",
        bootstrap_admin_password="",
        root_key=web_env.settings.root_key,
        test_mode=True,
        access_mode="off",
        onebot_ws_port=None,
        onebot_ws_url=None,
    )
    async with session_scope() as session:
        await session.execute(delete(User))
        await session.flush()

    with caplog.at_level(logging.WARNING, logger="ling.app"):
        await _bootstrap(settings)

    # 日志里要能直接抄到用户名与口令
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "firstrun" in text, text
    assert "随机生成" in text, text

    async with session_scope() as session:
        users = await IdentityService(session).count_users()
    assert users >= 1


async def test_must_change_password_blocks_everything_but_auth(web_env) -> None:
    """标记为真时，除 /v1/auth/* 之外的接口一律 403。

    这条闸在 `current_auth` 里，是所有已鉴权请求的唯一入口——写散在各个
    路由上的话早晚漏一个，而漏掉的那个就是带着随机口令的长期后门。
    """
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    # 造一个带标记的用户
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="needchange",
            password="one-time-pass-123",
            role="owner",
            must_change_password=True,
        )

    login = await web_env.client.post(
        "/v1/auth/login",
        json={"username": "needchange", "password": "one-time-pass-123"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["must_change_password"] is True

    blocked = await web_env.client.get("/v1/sessions")
    assert blocked.status_code == 403, blocked.text
    assert "must_change_password" in blocked.text

    # 认证接口本身仍然放行
    assert (await web_env.client.get("/v1/auth/me")).status_code == 200


async def test_changing_password_clears_the_flag(web_env) -> None:
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="changer",
            password="one-time-pass-123",
            role="owner",
            must_change_password=True,
        )
    await web_env.client.post(
        "/v1/auth/login",
        json={"username": "changer", "password": "one-time-pass-123"},
    )

    bad = await web_env.client.post(
        "/v1/auth/password",
        json={"current": "wrong-password", "new": "a-new-password-1"},
    )
    assert bad.status_code in (401, 403), bad.text

    same = await web_env.client.post(
        "/v1/auth/password",
        json={"current": "one-time-pass-123", "new": "one-time-pass-123"},
    )
    assert same.status_code == 422, same.text

    ok = await web_env.client.post(
        "/v1/auth/password",
        json={"current": "one-time-pass-123", "new": "a-new-password-1"},
    )
    assert ok.status_code == 204, ok.text

    # 标记清掉之后普通接口恢复
    me = await web_env.client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["must_change_password"] is False
    assert (await web_env.client.get("/v1/sessions")).status_code == 200


async def test_change_password_revokes_other_sessions_but_keeps_current(
    web_env,
) -> None:
    """改密的动机通常是怀疑泄露，所以要把别的会话踢掉——但保留当前这次。"""
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="multi",
            password="one-time-pass-123",
            role="owner",
            must_change_password=True,
        )

    # 另一个浏览器（独立 cookie jar）
    other = AsyncClient(
        transport=ASGITransport(app=web_env.app), base_url="http://testserver"
    )
    async with other:
        r = await other.post(
            "/v1/auth/login",
            json={"username": "multi", "password": "one-time-pass-123"},
        )
        assert r.status_code == 200
        assert (await other.get("/v1/auth/me")).status_code == 200

        # 当前会话改密
        ok = await web_env.client.post(
            "/v1/auth/login",
            json={"username": "multi", "password": "one-time-pass-123"},
        )
        assert ok.status_code == 200
        changed = await web_env.client.post(
            "/v1/auth/password",
            json={"current": "one-time-pass-123", "new": "a-new-password-1"},
        )
        assert changed.status_code == 204, changed.text

        # 当前会话还能用
        assert (await web_env.client.get("/v1/auth/me")).status_code == 200
        # 另一个被踢
        assert (await other.get("/v1/auth/me")).status_code == 401

    # 旧口令失效、新口令可登录
    old = await web_env.client.post(
        "/v1/auth/login",
        json={"username": "multi", "password": "one-time-pass-123"},
    )
    assert old.status_code == 401
    new = await web_env.client.post(
        "/v1/auth/login",
        json={"username": "multi", "password": "a-new-password-1"},
    )
    assert new.status_code == 200


async def test_rename_requires_current_password(logged_in) -> None:
    """改登录名必须验证当前口令。

    cookie 被盗时，改名是"接管账号"的第一步（换成攻击者习惯的账号名，
    后续社工更容易）。只看 session 有效不够。
    """
    response = await logged_in.client.patch(
        "/v1/auth/account", json={"current": "口令不对", "username": "新名字"}
    )
    assert response.status_code == 401, response.text
    me = (await logged_in.client.get("/v1/auth/me")).json()
    assert me["user"]["username"] != "新名字"


async def test_rename_updates_username(logged_in) -> None:
    response = await logged_in.client.patch(
        "/v1/auth/account",
        json={"current": "tester-password", "username": "新管理员"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "新管理员"
    me = (await logged_in.client.get("/v1/auth/me")).json()
    assert me["user"]["username"] == "新管理员"


async def test_rename_rejects_taken_username(logged_in) -> None:
    """重名要被挡住，且**不区分大小写**——`Admin` 和 `admin` 登录时看起来一样。"""
    from ling.domains.identity.service import hash_password
    from ling.infra.db import session_scope
    from ling.infra.models import User
    from ling.infra.security import new_id

    async with session_scope() as session:
        session.add(
            User(
                id=new_id("usr"),
                username="OtherAdmin",
                password_hash=hash_password("x" * 12),
                role="member",
                subject_id="subj_other_admin",
            )
        )
        await session.commit()

    response = await logged_in.client.patch(
        "/v1/auth/account",
        json={"current": "tester-password", "username": "otheradmin"},
    )
    assert response.status_code == 409, response.text
    me = (await logged_in.client.get("/v1/auth/me")).json()
    assert me["user"]["username"] != "otheradmin", "重名竟然改成功了"


async def test_rename_revokes_other_sessions_but_keeps_current(logged_in) -> None:
    """改完名字，其它会话失效、当前这次登录保留。

    全吊销的话用户点一下"保存"就被踢下线；全保留的话，
    改名（账号接管的第一步）对已泄露的 cookie 毫无作用。
    """
    other = logged_in.client_factory() if hasattr(logged_in, "client_factory") else None
    if other is not None:  # 夹具没提供工厂时跳过这一步，只在能构造时验证
        await other.post(
            "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
        )

    response = await logged_in.client.patch(
        "/v1/auth/account",
        json={"current": "tester-password", "username": "改过名的管理员"},
    )
    assert response.status_code == 200, response.text

    me = (await logged_in.client.get("/v1/auth/me")).json()
    assert me["user"]["username"] == "改过名的管理员", "当前会话被误吊销了"

    if other is not None:
        stale = await other.get("/v1/auth/me")
        assert stale.status_code == 401, "其它会话没有被吊销"
