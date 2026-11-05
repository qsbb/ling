"""W0 Web 测试夹具：真实 PostgreSQL 数据库 + 真实 HTTP 客户端。

设计要点：
- 每个测试模块用独立 schema（``test_<uuid>``），避免相互污染；结束时删除。
- 测试库必须是 PostgreSQL（``LING_TEST_DATABASE_URL``），否则跳过——
  W0 的验收要求真实数据库，不用 sqlite 冒充。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ling.app import create_app
from ling.config import Settings
from ling.infra.db import dispose_engine, init_engine

TEST_DB_URL = os.environ.get(
    "LING_TEST_DATABASE_URL", "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
)

pytestmark_web = pytest.mark.w0_web


@dataclass
class TestEnv:
    __test__ = False

    client: AsyncClient
    settings: Settings
    schema: str
    app: object = None
    subject_id: str = ""


@pytest_asyncio.fixture(scope="function")
async def web_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestEnv]:
    if not TEST_DB_URL.startswith("postgresql"):
        pytest.skip("W0 需要真实 PostgreSQL：设置 LING_TEST_DATABASE_URL")

    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin_url = TEST_DB_URL
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await admin_engine.dispose()

    settings = Settings(
        env="test",
        database_url=TEST_DB_URL,
        db_search_path=schema,
        data_dir=__import__("pathlib").Path(f"/tmp/ling-test-{schema}"),
        bootstrap_admin_user="tester",
        bootstrap_admin_password="tester-password",
        root_key="test-root-key-" + schema,
        test_mode=True,
        cookie_secure=False,
        actor_id="actor_ling",
        persona_id="persona_ling",
        # **显式关掉出入许可**：测试不该依赖开发机 shell 里的
        # LING_ACCESS_MODE（那是给真实部署用的）。许可本身的测试
        # 会在用例里覆盖这一项。
        access_mode="off",
        access_list=frozenset(),
        # **测试不绑真实端口**：WS 服务端会占用 6799，和开发机上跑着的
        # 服务冲突（实测 `address already in use` 让整组测试 setup 失败）。
        # 需要测服务端行为时直接构造 OneBotWsServer，不经过 app 生命周期。
        onebot_ws_port=None,
        onebot_ws_url=None,
        # QQ 官方机器人是 Webhook 模式，**不绑端口**，但路由是否注册取决于
        # 这个开关——测试里打开它，才能走真实的 /v1/qqbot/events 入口。
        # 同时把凭据显式清空：否则开发机 shell 里的 LING_QQBOT_*（dev_env.sh
        # 会导出）会漏进测试，让用例意外对着真实平台发起请求。
        qqbot_enabled=True,
        qqbot_app_id=None,
        qqbot_client_secret=None,
        qqbot_bot_secret=None,
        qqbot_webhook_public_key=None,
        qqbot_api_base=None,
        qqbot_token_url=None,
        qqbot_instance_id="qqbot-test",
        qqbot_sandbox=True,
        qqbot_proactive=False,
        qqbot_allow_unsigned_validation=False,
    )

    await dispose_engine()
    engine = init_engine(settings)
    await engine.dispose()
    init_engine(settings)
    from ling.infra.db import create_all_for_dev

    await create_all_for_dev(engine)

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield TestEnv(client=client, settings=settings, schema=schema, app=app)

    await dispose_engine()
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin_engine.dispose()


@pytest_asyncio.fixture
async def logged_in(web_env: TestEnv) -> TestEnv:
    response = await web_env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert response.status_code == 200, response.text
    me = await web_env.client.get("/v1/auth/me")
    assert me.status_code == 200, me.text
    web_env.subject_id = me.json()["user"]["subject_id"]
    return web_env


async def read_sse(response) -> list[tuple[str, dict]]:
    """把 SSE 响应解析成 (event, data) 列表。"""
    import json

    events: list[tuple[str, dict]] = []
    buffer = ""
    event_name = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            for line in raw.splitlines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    events.append((event_name, json.loads(line[6:])))
    return events


__all__ = ["TestEnv", "logged_in", "read_sse", "web_env"]
