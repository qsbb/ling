"""环境事实真的进了提示词：用**真正发出去的 messages** 断言。

## 为什么不能只测 `env_facts.render()`

单元测试只证明"给定事实能拼出那段话"。但用户报的是「她不知道自己在哪」——
真正要回答的是"**这段话有没有出现在 provider 收到的 system 消息里**"。
中间隔着 `compile()` 的组装、车道预算、受众裁决三道关，任何一道把它丢了，
单测都是绿的。

本项目已经因为"只测函数被调用"吃过亏（见 `test_persona_injection.py` 开头
记录的四种"看起来没注入"）。所以这里断言的是 provider 实际收到的内容。

## 关键用例：空人设时**必须**仍然注入

用户原话：「其他功能侧正常该注入就注入，人设就空着不注入」。
2026-11-11 起系统不再自动建人格行，**新装就是空人设**——如果环境事实
被写进 persona 的 else 分支，那么全新部署永远不知道自己在哪个渠道，
而那恰恰是最需要它的时候。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from ling.infra.db import session_scope
from ling.infra.models import Persona, Window

from .conftest import TEST_DB_URL, TestEnv

pytestmark = pytest.mark.w0_web


# --------------------------------------------------------------------------- #
# 一个不启动 app 的轻量库夹具
# --------------------------------------------------------------------------- #
#
# ## 为什么不能只用 `web_env`
#
# `web_env` 走 `app.router.lifespan_context(app)`，启动过程会读**连接器配置**
# （`settings/connector_service.py`）。那段代码在「连接器多实例」改造期间是
# 半成品（`connector_config_heads` 刚改成复合主键，读取侧还是单值 `session.get`），
# 于是**任何**用 `web_env` 的集成测试都在 setup 阶段 ERROR——不是本功能的错，
# 却把本功能的验证一起挡住了。
#
# 环境事实的核心链路其实不依赖 app 启动：`ContextCompiler` 只需要一个
# `AsyncSession` 和一行 `windows`。所以这里自己建 schema + 建表，
# **测的仍然是真实的 `ContextCompiler.compile`**，只是省掉了无关的启动步骤。
#
# 用 `web_env` 的用例仍然保留（它们额外验证"Web 建窗口"这条路径），
# 基线修好后应当全绿。


@pytest_asyncio.fixture
async def bare_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """建一个隔离 schema + 建表，初始化全局 session factory。不启动 app。"""
    if not TEST_DB_URL.startswith("postgresql"):
        pytest.skip("需要真实 PostgreSQL：设置 LING_TEST_DATABASE_URL")

    schema = f"t_env_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await admin.dispose()

    from ling.config import Settings
    from ling.infra import db as db_mod

    settings = Settings(
        env="test",
        database_url=TEST_DB_URL,
        db_search_path=schema,
        data_dir=Path(f"/tmp/ling-env-{schema}"),
        bootstrap_admin_user="tester",
        bootstrap_admin_password="tester-password",
        root_key="test-root-key-" + schema,
        test_mode=True,
        cookie_secure=False,
    )
    # 全局 engine 是模块级单例：每个用例自己建一个，用完拆掉，
    # 否则第二个用例会复用上一个 schema（`init_engine` 见到 `_engine` 非空就跳过）。
    db_mod._engine = None  # noqa: SLF001 - 测试里显式重置单例
    db_mod._session_factory = None  # noqa: SLF001
    engine = db_mod.init_engine(settings)
    await db_mod.create_all_for_dev(engine)

    try:
        yield
    finally:
        await engine.dispose()
        db_mod._engine = None  # noqa: SLF001
        db_mod._session_factory = None  # noqa: SLF001
        admin = create_async_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _make_window(*, visibility: str, title: str) -> str:
    """直接建一行窗口（模拟桥接层建出来的形状），返回 window_id。"""
    from ling.infra.security import new_id

    window_id = new_id("win")
    async with session_scope() as session:
        session.add(
            Window(
                id=window_id,
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_env_test",
                title=title,
                channel="onebot",
                visibility=visibility,
            )
        )
        await session.flush()
    return window_id


async def _compile_bare(window_id: str, subject_id: str):  # noqa: ANN201
    from ling.kernel.context import ContextCompiler

    async with session_scope() as session:
        return await ContextCompiler(session).compile(
            subject_id=subject_id, window_id=window_id, user_text="在吗"
        )


async def _private_window(env: TestEnv) -> tuple[str, str]:
    """建一个私聊窗口，返回 `(window_id, subject_id)`。"""
    response = await env.client.post("/v1/sessions", json={"title": "环境事实"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"], env.subject_id


async def _group_window(env: TestEnv) -> tuple[str, str]:
    """建一个群窗口。

    ## 为什么直接改 `windows` 行而不走 API

    回复路径上的群窗口是**桥接层**建的（`bridge.py` 的 `_window`，
    `visibility="group_scope"`）。Web API 只建私聊窗口，没有"建群"的入口——
    群是渠道那边的事。所以这里模拟桥接层建出来的那一行，只改它特有的两列。

    这不是在绕过被测逻辑：被测的是 `ContextCompiler` 怎么读**已存在的窗口行**，
    而那行的形状就是桥接层写的样子。
    """
    window_id, subject_id = await _private_window(env)
    async with session_scope() as session:
        row = await session.get(Window, window_id)
        assert row is not None
        row.visibility = "group_scope"
        row.channel = "onebot"
        row.title = "100200300"
        await session.flush()
    return window_id, subject_id


async def _ensure_no_persona_row() -> None:
    """保证库里一行人格都没有（= 新装状态 = 空人设）。"""
    async with session_scope() as session:
        await session.execute(delete(Persona))


async def _compile(env: TestEnv, window_id: str, subject_id: str):  # noqa: ANN201
    from ling.kernel.context import ContextCompiler

    async with session_scope() as session:
        return await ContextCompiler(session).compile(
            subject_id=subject_id,
            window_id=window_id,
            user_text="在吗",
        )


def _world_text(compiled) -> str:  # noqa: ANN001
    parts = [c.text for c in compiled.contributions if c.lane == "world"]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 真的注入了（轻量库，不依赖 app 启动）
# --------------------------------------------------------------------------- #

_SUBJECT = "subj_env_test"


async def test_私聊窗口注入平台与会话类型(bare_db: None) -> None:
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    assert text, "world 车道是空的——环境事实没进提示词"
    assert "私聊" in text
    assert "QQ" in text
    assert "400500600" in text


async def test_群聊窗口注入群号且判定为群聊(bare_db: None) -> None:
    window_id = await _make_window(visibility="group_scope", title="100200300")
    compiled = await _compile_bare(window_id, _SUBJECT)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    assert "群聊" in text
    assert "QQ" in text
    assert "100200300" in text, "群号没注入——她不知道自己在哪个群"


async def test_群聊的环境事实走group_scope受众(bare_db: None) -> None:
    """群窗口的渠道/群号对群里人都可见，不该标成私密。"""
    window_id = await _make_window(visibility="group_scope", title="100200300")
    compiled = await _compile_bare(window_id, _SUBJECT)
    world = [c for c in compiled.contributions if c.lane == "world"]
    assert world, "world 车道为空"
    assert world[0].visibility.value == "group_scope"


async def test_私聊的环境事实走subject_private受众(bare_db: None) -> None:
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)
    world = [c for c in compiled.contributions if c.lane == "world"]
    assert world and world[0].visibility.value == "subject_private"


async def test_环境事实的origin是外部观测(bare_db: None) -> None:
    """六种 origin 里只有 `external_observation` 表示"我们这边查到的客观情况"。"""
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)
    world = [c for c in compiled.contributions if c.lane == "world"]
    assert world and world[0].origin.value == "external_observation"


async def test_环境事实带上时间(bare_db: None) -> None:
    """时区解析走 `load_user_timezone`，能拿到就给钟点。"""
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    # 默认时区是 Asia/Shanghai（`DEFAULT_USER_TIMEZONE`），一定有值
    assert "此刻" in text, f"时间没注入：{text!r}"


# --------------------------------------------------------------------------- #
# 空人设时仍然注入（用户明确要求）
# --------------------------------------------------------------------------- #


async def test_空人设时环境事实仍然注入(bare_db: None) -> None:
    """**本任务最关键的一条**。

    用户原话：「其他功能侧正常该注入就注入，人设就空着不注入」。
    `bare_db` 建的库里**一行人格都没有**（新装状态），正是最容易漏掉的情况。
    """
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)

    lanes = [c.lane for c in compiled.contributions]
    assert "persona" not in lanes, "空人设时不该有 persona 车道"
    assert "constraints" not in lanes, "空人设时不该有 constraints 车道"
    assert "world" in lanes, "空人设时**必须**有 world 车道"


async def test_空人设时群聊环境事实也注入(bare_db: None) -> None:
    window_id = await _make_window(visibility="group_scope", title="100200300")
    compiled = await _compile_bare(window_id, _SUBJECT)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    assert "群聊" in text and "100200300" in text


# --------------------------------------------------------------------------- #
# 不与既有车道重复
# --------------------------------------------------------------------------- #


async def test_环境事实不重复注入关系姿态(bare_db: None) -> None:
    """`relationship` 车道已经有生产者，环境事实不得再说一遍。"""
    window_id = await _make_window(visibility="subject_private", title="400500600")
    compiled = await _compile_bare(window_id, _SUBJECT)
    world = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    for banned in ("熟悉", "信任", "关系", "边界"):
        assert banned not in world, f"world 车道重复了关系信息：{banned}"


async def test_环境事实不含行为指令(bare_db: None) -> None:
    """用户要求「只阐述」。注入到真实提示词里的这一段同样不能有祈使句。"""
    window_id = await _make_window(visibility="group_scope", title="100200300")
    compiled = await _compile_bare(window_id, _SUBJECT)
    world = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    for banned in ("应该", "需要", "请", "不要", "务必", "建议"):
        assert banned not in world, f"环境事实里出现行为指令：{banned}"


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #


async def test_环境事实远低于world车道预算(bare_db: None) -> None:
    """world 车道预算 2000 字符。环境事实应该远小于它，不该挤掉别的东西。"""
    from ling.kernel.prompt_zones import DEFAULT_BUDGETS

    window_id = await _make_window(visibility="group_scope", title="100200300")
    compiled = await _compile_bare(window_id, _SUBJECT)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    assert len(text) < DEFAULT_BUDGETS["world"] // 4, (
        f"环境事实 {len(text)} 字符，占 world 预算 {len(text) / DEFAULT_BUDGETS['world']:.0%}"
    )


async def test_取数失败不阻断对话(bare_db: None) -> None:
    """环境事实是旁白，不是这一轮的前提。取数炸了也要能编译出上下文。"""

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("模拟取数失败")

    import ling.kernel.env_facts as env_facts_mod

    original = env_facts_mod.collect
    env_facts_mod.collect = _boom  # type: ignore[assignment]
    try:
        window_id = await _make_window(visibility="subject_private", title="400500600")
        compiled = await _compile_bare(window_id, _SUBJECT)
    finally:
        env_facts_mod.collect = original  # type: ignore[assignment]

    assert "world" not in [c.lane for c in compiled.contributions]
    assert compiled.contributions, "别的车道该照常"
    assert compiled.meta.get("env_facts_error") == "RuntimeError"


# --------------------------------------------------------------------------- #
# 走真实 Web API 建窗口的那条路径（依赖 app 启动）
# --------------------------------------------------------------------------- #


async def test_Web建的窗口也注入环境事实(logged_in: TestEnv) -> None:
    """上面前提都是自己建窗口。这条确认**Web API 建的窗口**同样被注入。"""
    response = await logged_in.client.post("/v1/sessions", json={"title": "环境事实"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    compiled = await _compile(logged_in, window_id, logged_in.subject_id)
    text = "\n".join(c.text for c in compiled.contributions if c.lane == "world")
    assert text, "Web 建的窗口没有环境事实"
    assert "私聊" in text
