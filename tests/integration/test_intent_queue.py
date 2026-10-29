"""意图队列：她自己产生的跨轮念头。

「回不回」已经是选择了，但那只覆盖当下这一轮。真人还有跨轮的念头：
「等她上线要问那个事」。没有这一层，她每轮从零开始想，
记不住自己刚决定"稍后再说"。

三条硬规则，本文件逐条钉住：

1. **没有来源的意图不许产生**——说不出"为什么会想到这个"的念头，
   正是路线图排除的假自主
2. **念头不是承诺**——过期只是 expired，不补发、不道歉
3. **同一件事只产生一条**——dedupe_key 唯一
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.intent.service import (
    IntentError,
    IntentService,
    record_followup,
)
from ling.domains.proactive.sources import REASON_INTENT_DUE
from ling.infra.db import session_scope
from ling.infra.models import IntentRow, ProactiveOpportunity
from ling.worker import handle_intent_tick
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _clean() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(delete(IntentRow).where(IntentRow.actor_id == ACTOR))
        await session.execute(
            delete(ProactiveOpportunity).where(
                ProactiveOpportunity.reason_code == REASON_INTENT_DUE
            )
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


# ---------------------------------------------------------------- 硬规则 1：来源


async def test_intent_without_source_is_refused(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有来源的念头不许产生。

    "凭空想打个招呼"没有 source_ref，所以进不来——这是防假自主的最后一道闸。
    """
    async with session_scope() as session:
        with pytest.raises(IntentError, match="来源"):
            await IntentService(session, actor_id=ACTOR).propose(
                goal="想找她说句话", source_kind="self", source_ref=""
            )


async def test_intent_with_source_is_created(logged_in: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        view = await IntentService(session, actor_id=ACTOR).propose(
            goal="问问她那件事进展如何",
            source_kind="message",
            source_ref="msg_abc",
            subject_id=logged_in.subject_id,
            rationale="她说下周有结果，到点该问了",
        )
        assert view is not None
        assert view.source_ref == "msg_abc"


async def test_unknown_kind_is_refused(logged_in: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        with pytest.raises(IntentError):
            await IntentService(session, actor_id=ACTOR).propose(
                goal="x", source_kind="message", source_ref="m1", kind="hologram"
            )


# ---------------------------------------------------------------- 硬规则 3：去重


async def test_same_intent_is_not_duplicated(logged_in: TestEnv) -> None:  # noqa: ARG001
    """同一件事反复想只产生一条。"""
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        first = await service.propose(
            goal="问问那件事", source_kind="message", source_ref="msg_dup"
        )
        second = await service.propose(
            goal="问问那件事", source_kind="message", source_ref="msg_dup"
        )
        assert first is not None
        assert second is None, "同一件事产生了第二条意图"


# ---------------------------------------------------------------- 时机与过期


async def test_due_respects_not_before(logged_in: TestEnv) -> None:  # noqa: ARG001
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        await service.propose(
            goal="还没到点",
            source_kind="message",
            source_ref="m_future",
            not_before=now + timedelta(hours=1),
            expires_at=now + timedelta(hours=5),
        )
        await service.propose(
            goal="已经到点",
            source_kind="message",
            source_ref="m_now",
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=5),
        )
        due = [v.goal for v in await service.due(now=now)]
    assert due == ["已经到点"], due


async def test_expired_intent_leaves_the_queue(logged_in: TestEnv) -> None:  # noqa: ARG001
    """过期是放弃，不是延后——不补发、不道歉。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        await service.propose(
            goal="过期的念头",
            source_kind="message",
            source_ref="m_old",
            not_before=now - timedelta(hours=3),
            expires_at=now - timedelta(minutes=1),
        )
        assert await service.due(now=now) == []
        assert await service.expire_overdue(now=now) == 1
        again = (
            await session.execute(
                select(IntentRow).where(IntentRow.source_ref == "m_old")
            )
        ).scalar_one()
        assert again.state == "expired"


# ---------------------------------------------------------------- 兑现


async def test_due_intent_becomes_proactive_opportunity(logged_in: TestEnv) -> None:  # noqa: ARG001
    """到期念头变成主动**候选**，不是直接发消息。

    仍然要过 gate（勿扰/日限额/渠道能力）——自主性不能绕过硬门。
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        await IntentService(session, actor_id=ACTOR).propose(
            goal="问她周五的交材料顺不顺利",
            source_kind="message",
            source_ref="msg_due",
            subject_id=logged_in.subject_id,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=3),
        )

    await handle_intent_tick({})

    async with session_scope() as session:
        opps = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.reason_code == REASON_INTENT_DUE
                )
            )
        ).scalars().all()
        assert len(opps) == 1, f"没有变成候选：{len(opps)}"
        assert "交材料" in opps[0].reason
        # 来源必须能追回到具体意图
        assert opps[0].source_event_id.startswith("intent:")

        intent = (
            await session.execute(
                select(IntentRow).where(IntentRow.source_ref == "msg_due")
            )
        ).scalar_one()
        assert intent.state == "consumed"


async def test_intent_without_subject_is_dropped(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有当事人的念头投不出去，直接丢弃而不是建一条没人收的候选。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        await IntentService(session, actor_id=ACTOR).propose(
            goal="没头没脑的念头",
            source_kind="message",
            source_ref="msg_nosub",
            subject_id=None,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=3),
        )
    await handle_intent_tick({})
    async with session_scope() as session:
        intent = (
            await session.execute(
                select(IntentRow).where(IntentRow.source_ref == "msg_nosub")
            )
        ).scalar_one()
    assert intent.state == "dropped"


async def test_tick_is_idempotent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """重复跑不该堆出多条候选——defer_count 无限增长就是骚扰。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        await IntentService(session, actor_id=ACTOR).propose(
            goal="只该出现一次",
            source_kind="message",
            source_ref="msg_once",
            subject_id=logged_in.subject_id,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=3),
        )
    await handle_intent_tick({})
    await handle_intent_tick({})
    async with session_scope() as session:
        opps = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.reason_code == REASON_INTENT_DUE
                )
            )
        ).scalars().all()
    assert len(opps) == 1, f"重复跑出了 {len(opps)} 条"


# ---------------------------------------------------------------- 从对话产生


async def test_record_followup_uses_message_as_source(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型说"稍后再提"时，来源必须是那条具体消息。"""
    from ling.kernel.autonomy import Followup

    async with session_scope() as session:
        view = await record_followup(
            session,
            actor_id=ACTOR,
            followup=Followup(after_minutes=30, goal="问问她考虑得怎么样"),
            subject_id=logged_in.subject_id,
            window_id="win_x",
            source_ref="msg:msg_123",
        )
        assert view is not None
        assert view.source_ref == "msg_123" or view.source_ref == "msg:msg_123"
        assert view.not_before is not None


async def test_record_followup_ignores_empty(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有目标或时间不合理时不产生意图。"""
    from ling.kernel.autonomy import Followup

    async with session_scope() as session:
        assert (
            await record_followup(
                session,
                actor_id=ACTOR,
                followup=Followup(after_minutes=0, goal="x"),
                subject_id=logged_in.subject_id,
                window_id=None,
                source_ref="msg:1",
            )
            is None
        )
        assert (
            await record_followup(
                session,
                actor_id=ACTOR,
                followup=Followup(after_minutes=30, goal="  "),
                subject_id=logged_in.subject_id,
                window_id=None,
                source_ref="msg:2",
            )
            is None
        )
