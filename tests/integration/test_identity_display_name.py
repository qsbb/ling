"""群名片/昵称：名字跟着消息走，变了才落库。

需求原话是"群名片不要固定不变"：AstrBot 给模型的人名是**每轮从事件现取**，
持久化只是旁路。ling 原来只在建档时写一次 ``subjects.display_name``，
之后永不更新——所以一个人改名之后，她永远叫旧名字。

这一组测的是"旁路同步"的四条硬约束：

1. **变了才写**：同名连着说不产生 UPDATE；
2. **空名不覆盖**：空名片回退昵称，两者都空就保持旧值；
3. **占位符不进库**：「群友」「N/A」这类不能当名字；
4. **失败不拒收**：写库出错只降级成 debug，消息照收。

外加"缓存命中不查库"——否则每条群消息都多一次 SELECT。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import (
    IdentityService,
    is_placeholder_display_name,
    normalize_display_name,
    reset_display_name_cache,
)
from ling.infra.db import session_scope
from ling.infra.models import Subject
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

CONNECTOR = "onebot-card"
QQ = "100200301"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """进程内 diff 缓存是全局的，用例之间必须互不影响。"""
    reset_display_name_cache()
    yield
    reset_display_name_cache()


async def _provision(name: str | None = "群友") -> str:
    async with session_scope() as session:
        subject_id = await IdentityService(session).resolve_or_provision(
            CONNECTOR, QQ, display_name=name
        )
        await session.commit()
        assert subject_id
        return subject_id


async def _display_name(subject_id: str) -> str:
    async with session_scope() as session:
        row = await session.get(Subject, subject_id)
        assert row is not None
        return row.display_name


async def _observe(**kwargs) -> str | None:  # noqa: ANN003
    async with session_scope() as session:
        name = await IdentityService(session).observe_display_name(
            CONNECTOR, QQ, **kwargs
        )
        await session.commit()
        return name


def _engine():  # noqa: ANN202 - 只为拿 sync_engine 挂监听器
    from ling.infra import db as db_module

    engine = db_module._engine
    assert engine is not None, "夹具没初始化引擎"
    return engine


@contextmanager
def _record_sql() -> Iterator[list[str]]:
    """记录这一小段里真正发出去的 SQL（证明"缓存命中=零查库"）。

    SQLAlchemy 的 ``before_cursor_execute`` 只在实际发给数据库的语句上触发；
    空会话建/关不发任何语句（已实测），所以可以断言列表为空。
    """
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        statements.append(" ".join(statement.split()))

    engine = _engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)


# ------------------------------------------------------------------ 变了才写


async def test_first_nickname_replaces_the_placeholder(logged_in: TestEnv) -> None:  # noqa: F811
    """建档时只有占位符「群友」→ 第一次拿到昵称就该换上。"""
    subject_id = await _provision()

    assert await _observe(nickname="阿岚") == "阿岚"
    assert await _display_name(subject_id) == "阿岚"


async def test_rename_takes_effect_on_the_next_message(logged_in: TestEnv) -> None:  # noqa: F811
    """改名后**下一条消息**就该是新名字（这就是需求本身）。"""
    subject_id = await _provision()
    await _observe(nickname="阿岚")

    assert await _observe(nickname="懒懒") == "懒懒"
    assert await _display_name(subject_id) == "懒懒"


async def test_same_nickname_is_not_written_again(logged_in: TestEnv) -> None:  # noqa: F811
    """连着说十句同名：只有第一次落库，后续连库都不碰。"""
    subject_id = await _provision()
    await _observe(nickname="阿岚")
    first = await _display_name(subject_id)

    for _ in range(5):
        assert await _observe(nickname="阿岚") is None

    assert await _display_name(subject_id) == first


async def test_cache_hit_touches_no_database(logged_in: TestEnv) -> None:  # noqa: F811
    """同名第二条：**一条 SQL 都不发**（进程内 diff 已经答了）。"""
    await _provision()
    assert await _observe(nickname="阿岚") == "阿岚"

    async with session_scope() as session:
        with _record_sql() as statements:
            assert (
                await IdentityService(session).observe_display_name(
                    CONNECTOR, QQ, nickname="阿岚"
                )
                is None
            )

    assert statements == [], f"同名第二条不该查库：{statements}"


async def test_rename_writes_exactly_one_update(logged_in: TestEnv) -> None:  # noqa: F811
    """改名只产生**一条** UPDATE：既不是 N 条，也不是"下一条消息再补"。"""
    await _provision()
    await _observe(nickname="阿岚")

    async with session_scope() as session:
        with _record_sql() as statements:
            assert (
                await IdentityService(session).observe_display_name(
                    CONNECTOR, QQ, nickname="懒懒"
                )
                == "懒懒"
            )
            await session.commit()

    updates = [s for s in statements if s.upper().startswith("UPDATE SUBJECTS")]
    assert len(updates) == 1, updates


# ------------------------------------------------------------------ 空名不覆盖


async def test_empty_card_falls_back_to_nickname(logged_in: TestEnv) -> None:  # noqa: F811
    """名片为空、昵称有值 → 用昵称（人级字段）。"""
    subject_id = await _provision()

    assert await _observe(nickname="阿岚", sender_name=None) == "阿岚"
    assert await _display_name(subject_id) == "阿岚"


async def test_all_empty_keeps_the_old_name(logged_in: TestEnv) -> None:  # noqa: F811
    """名片和昵称都空 → **保持旧值**（空名不是改名的信号）。"""
    subject_id = await _provision(name="阿岚")

    assert await _observe(nickname="", sender_name="  ") is None
    assert await _display_name(subject_id) == "阿岚"


@pytest.mark.parametrize("placeholder", ["群友", "用户", "N/A", "unknown", "   "])
async def test_placeholder_is_never_written(logged_in: TestEnv, placeholder: str) -> None:  # noqa: F811
    """占位符不许进库：一旦写进去，"变了才写"就永远治不好它。"""
    subject_id = await _provision(name="阿岚")

    assert await _observe(nickname=placeholder) is None
    assert await _display_name(subject_id) == "阿岚"


async def test_legacy_connector_without_nickname_still_works(logged_in: TestEnv) -> None:  # noqa: F811
    """旧连接器只发 ``sender_name``：退回用它，不报错。"""
    subject_id = await _provision()

    assert await _observe(sender_name="阿岚") == "阿岚"
    assert await _display_name(subject_id) == "阿岚"


# ------------------------------------------------------------------ 缓存与失败


async def test_lookup_failure_does_not_raise_and_retries(
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**查库**失败（resolve_account 的 SELECT 抛异常）：返回 None、不抛，下次重试。"""
    subject_id = await _provision()

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("db down")

    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(AsyncSession, "execute", _boom)

    async with session_scope() as session:
        svc = IdentityService(session)
        assert await svc.observe_display_name(CONNECTOR, QQ, nickname="阿岚") is None

    monkeypatch.undo()
    # 失败后缓存里不能留"已写过"的假状态，否则永远不再重试
    assert await _observe(nickname="阿岚") == "阿岚"
    assert await _display_name(subject_id) == "阿岚"


async def test_write_failure_does_not_poison_the_caller_session(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """**真写库失败**（UPDATE 抛 DB 错误）：只回滚这一步，调用方照常提交。

    这条守的是 SAVEPOINT。光 ``try/except`` 不够：一次失败的 flush 会把整个
    session 的事务标成待回滚，bridge 随后 commit 这条消息会拿到
    ``PendingRollbackError``——「改名失败把用户消息拒了」，正是需求里明确
    不允许的那一条。（已实测：去掉 SAVEPOINT 时下面这个 commit 会炸。）
    """
    subject_id = await _provision()
    fired: list[str] = []

    def _fail_update(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        if statement.lstrip().upper().startswith("UPDATE SUBJECTS"):
            fired.append(statement)
            raise IntegrityError(statement, parameters, Exception("boom"))

    engine = _engine()
    async with session_scope() as session:
        event.listen(engine.sync_engine, "before_cursor_execute", _fail_update)
        try:
            assert (
                await IdentityService(session).observe_display_name(
                    CONNECTOR, QQ, nickname="阿岚"
                )
                is None
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _fail_update)
        # 同一个 session 还能提交——这正是 SAVEPOINT 的作用
        await session.commit()

    assert fired, "用例没打到写库那一步"
    # 旧值（建档占位符）没被写坏；失败也没被记成"已写过"，下一条消息会重试
    assert await _display_name(subject_id) == "群友"
    assert await _observe(nickname="阿岚") == "阿岚"
    assert await _display_name(subject_id) == "阿岚"


async def test_unknown_account_is_not_provisioned_here(logged_in: TestEnv) -> None:  # noqa: F811
    """没建过号的账号：这里不越界建号（建号是 resolve_or_provision 的事）。"""
    from ling.infra.models import ActorAccount

    async with session_scope() as session:
        svc = IdentityService(session)
        assert await svc.observe_display_name(CONNECTOR, "999999", nickname="阿岚") is None
        accounts = (
            (
                await session.execute(
                    select(ActorAccount).where(
                        ActorAccount.external_account_id == "999999"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert accounts == [], "这里只同步名字，不该顺手建账号/建 subject"


# ------------------------------------------------------------------ 读侧效果


async def test_conversation_title_follows_the_rename(logged_in: TestEnv) -> None:  # noqa: F811
    """私聊会话标题（读侧 join subjects）在改名后立刻变——用户直接看得到的效果。"""
    subject_id = await _provision()
    await _observe(nickname="阿岚")
    async with session_scope() as session:
        window = await ConversationService(session).create_window(
            subject_id=subject_id, title=f"{CONNECTOR}:private:{QQ}", channel="onebot"
        )
        await session.commit()
        window_id = window.id

    async with session_scope() as session:
        window = await session.get(
            __import__("ling.infra.models", fromlist=["Window"]).Window, window_id
        )
        names = await ConversationService(session).peer_display_names([window])
    assert names[window_id] == "阿岚"

    await _observe(nickname="懒懒")
    async with session_scope() as session:
        window = await session.get(
            __import__("ling.infra.models", fromlist=["Window"]).Window, window_id
        )
        names = await ConversationService(session).peer_display_names([window])
    assert names[window_id] == "懒懒"


# ------------------------------------------------------------------ 纯函数


def test_normalize_display_name_rules() -> None:
    assert normalize_display_name("  阿岚 ") == "阿岚"
    assert normalize_display_name("") is None
    assert normalize_display_name(None) is None
    assert normalize_display_name("群友") is None
    assert normalize_display_name("x" * 200) is not None
    assert len(normalize_display_name("x" * 200) or "") == 128


def test_is_placeholder_display_name() -> None:
    assert is_placeholder_display_name("群友") is True
    assert is_placeholder_display_name("  ") is True
    assert is_placeholder_display_name(None) is True
    assert is_placeholder_display_name("阿岚") is False


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存有上限：长跑进程里不能按人数一直长。"""
    from ling.domains.identity import service as identity_service

    monkeypatch.setattr(identity_service, "_DISPLAY_NAME_CACHE_MAX", 3)
    reset_display_name_cache()
    for index in range(6):
        identity_service._remember_display_name(("c", str(index)), f"n{index}")
    assert len(identity_service._DISPLAY_NAME_CACHE) == 3
    # 留下的是最新的三条
    assert ("c", "0") not in identity_service._DISPLAY_NAME_CACHE
    assert ("c", "5") in identity_service._DISPLAY_NAME_CACHE


# ------------------------------------------------------------------ 称呼回落


async def test_context_never_addresses_someone_as_a_placeholder(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """库里只有占位符「群友」时，**不要**拿它当人名写进提示词。

    否则她会对着两个不同的人都说"群友"，既分不清谁是谁，还会把这个词
    当成对方的称呼说出来。"不知道名字"就不署名——不编。
    """
    from ling.kernel.context import ContextCompiler

    subject_id = await _provision(name="群友")
    async with session_scope() as session:
        window = await ConversationService(session).create_window(subject_id=subject_id)
        await session.commit()
        window_id = window.id
        await ConversationService(session).add_message(
            window_id, role="user", text="在吗", sender_subject_id=subject_id
        )
        await session.commit()

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id="actor_ling", persona_id="persona_ling"
        ).compile(
            subject_id=subject_id,
            window_id=window_id,
            user_text="在吗",
            is_group=True,
            history_limit=5,
        )
    text = "\n".join(message.content for message in compiled.messages)
    assert "群友" not in text, "占位符不该出现在提示词里的人名位置"


async def test_context_uses_the_real_name_after_rename(logged_in: TestEnv) -> None:  # noqa: F811
    """改名后，历史消息的署名与当前这句的署名都跟着变（读侧读的是同一列）。"""
    from ling.kernel.context import ContextCompiler

    subject_id = await _provision()
    await _observe(nickname="阿岚")
    async with session_scope() as session:
        window = await ConversationService(session).create_window(subject_id=subject_id)
        await session.commit()
        window_id = window.id
        await ConversationService(session).add_message(
            window_id, role="user", text="第一句", sender_subject_id=subject_id
        )
        await session.commit()

    await _observe(nickname="懒懒")

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id="actor_ling", persona_id="persona_ling"
        ).compile(
            subject_id=subject_id,
            window_id=window_id,
            user_text="第二句",
            is_group=True,
            history_limit=5,
        )
    text = "\n".join(message.content for message in compiled.messages)
    assert "懒懒" in text


async def test_current_sentence_uses_the_live_card_not_the_database(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """透传进来的"这条消息的称呼"优先于库里的旧名：**当前这句**立刻用新名片。

    这正是需求要的效果——改名不必先落库、也不必等重启；库里那列同步是旁路
    （``observe_display_name``），供会话标题、历史署名这些读侧用。
    """
    from ling.kernel.context import ContextCompiler

    subject_id = await _provision(name="旧名")
    async with session_scope() as session:
        window = await ConversationService(session).create_window(subject_id=subject_id)
        await session.commit()
        window_id = window.id
        await ConversationService(session).add_message(
            window_id, role="user", text="上一句", sender_subject_id=subject_id
        )
        await session.commit()

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id="actor_ling", persona_id="persona_ling"
        ).compile(
            subject_id=subject_id,
            window_id=window_id,
            user_text="这一句",
            is_group=True,
            history_limit=5,
            user_sender_name="新名片",
        )
    text = "\n".join(message.content for message in compiled.messages)
    assert "新名片：这一句" in text
    assert "旧名：这一句" not in text


# ------------------------------------------------------- 已知代价（跨群共享）


async def test_known_cost_legacy_fallback_is_shared_across_groups(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """已知代价：老连接器不带 ``sender_nickname`` 时退回 ``sender_name``（群名片）。

    于是 A 群的名片会写进**人级**的 ``subjects.display_name``，B 群与私聊看到的
    也是它——最新的群名片会覆盖其他群的称呼。真正的"每群一份名字"需要新表
    ``channel_member_names``（docs/GROUP-CARD-RESEARCH.md §C.5，属 P1），本轮不做。

    这条用例把这个取舍钉住：哪天改成按群隔离，它变红就是在提醒同步更新文档与
    本函数的 ``sender_name`` 回退规则。
    """
    subject_id = await _provision()
    assert await _observe(sender_name="A群名片") == "A群名片"
    assert await _observe(sender_name="B群名片") == "B群名片"
    assert await _display_name(subject_id) == "B群名片"
