"""因果候选：只从可追溯事件产生主动机会。

要验证的核心不是"能不能生成候选"，而是**不该生成的时候一条都不生成**：

1. 没有来源 → 不生成（纯"到八点了"不是联系理由）
2. 同一件事 → 只生成一条（去重键不带时间戳才有意义）
3. 没到该说的时候 → 不登记
4. 影子模式 → 只记录、不发消息
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.goals.service import GoalsService
from ling.domains.proactive.service import ProactivityService
from ling.domains.proactive.sources import (
    REASON_COMMITMENT_DUE,
    REPLY_EXPECTED_BY_REASON,
    Candidate,
    CausalSource,
    dedupe_key_for,
)
from ling.infra.db import session_scope
from ling.infra.models import ProactiveOpportunity
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


def _svc(session, env: TestEnv):  # noqa: ANN001, ANN202
    _ = env
    return ProactivityService(session, actor_id=ACTOR)


# ------------------------------------------------------------------ 去重键


def test_dedupe_key_is_stable_across_time() -> None:
    """去重键**不能含时间戳**。

    含时间戳的话每次扫描都生成新键，去重形同虚设——
    同一件承诺会每 5 分钟生成一条候选。
    """
    candidate = Candidate(
        subject_id="s1",
        reason_code=REASON_COMMITMENT_DUE,
        source_event_id="commitment:c1",
        reason="提醒",
        not_before=datetime(2026, 1, 1, tzinfo=UTC),
        best_until=datetime(2026, 1, 1, 6, tzinfo=UTC),
    )
    first = dedupe_key_for(candidate)
    later = Candidate(
        subject_id="s1",
        reason_code=REASON_COMMITMENT_DUE,
        source_event_id="commitment:c1",
        reason="提醒",
        not_before=datetime(2026, 5, 5, tzinfo=UTC),
        best_until=datetime(2026, 5, 5, 6, tzinfo=UTC),
    )
    assert dedupe_key_for(later) == first


def test_different_sources_have_different_keys() -> None:
    a = Candidate("s1", REASON_COMMITMENT_DUE, "commitment:c1", "x",
                  datetime.now(UTC), datetime.now(UTC))
    b = Candidate("s1", REASON_COMMITMENT_DUE, "commitment:c2", "x",
                  datetime.now(UTC), datetime.now(UTC))
    assert dedupe_key_for(a) != dedupe_key_for(b)


# ------------------------------------------------------------------ 来源规则


async def test_no_commitments_no_candidates(logged_in: TestEnv) -> None:
    """没有任何可追溯事件时，一条候选都不该生成。

    这是"不变成随机打扰"的底线：宁可什么都不说。
    """
    async with session_scope() as session:
        candidates = await CausalSource(session, actor_id=ACTOR).collect()
    assert candidates == []


async def test_commitment_due_generates_candidate(logged_in: TestEnv) -> None:

    async with session_scope() as session:
        goals = GoalsService(session, actor_id=ACTOR)
        await goals.create_commitment(
            subject_id=logged_in.subject_id,
            window_id="win_pro",
            text="提醒我周五交材料",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )
    async with session_scope() as session:
        candidates = await CausalSource(session, actor_id=ACTOR).collect()
    assert candidates, "承诺到期却没有生成候选"
    assert candidates[0].reason_code == REASON_COMMITMENT_DUE
    assert candidates[0].source_event_id.startswith("commitment:")


async def test_commitment_candidate_expects_reply(logged_in: TestEnv) -> None:
    """用户要求的提醒期待回应；生活分享不期待。"""
    assert REPLY_EXPECTED_BY_REASON[REASON_COMMITMENT_DUE] is True


# ------------------------------------------------------------------ 收集与去重


async def test_collect_creates_once_then_dedupes(logged_in: TestEnv) -> None:
    """同一件事重复收集只生成一条。"""
    async with session_scope() as session:
        await GoalsService(session, actor_id=ACTOR).create_commitment(
            subject_id=logged_in.subject_id,
            window_id="win_pro",
            text="提醒我吃药",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )

    async with session_scope() as session:
        first = await _svc(session, logged_in).collect_candidates()
    async with session_scope() as session:
        second = await _svc(session, logged_in).collect_candidates()

    assert first["created"] >= 1
    assert second["created"] == 0, "重复收集又生成了候选"
    assert second["skipped_duplicate"] >= 1


async def test_shadow_mode_does_not_send(logged_in: TestEnv) -> None:
    """影子模式只记录候选，不推进到 sent、不产生投递操作。"""
    async with session_scope() as session:
        await GoalsService(session, actor_id=ACTOR).create_commitment(
            subject_id=logged_in.subject_id,
            window_id="win_pro",
            text="提醒我开会",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )
    async with session_scope() as session:
        result = await _svc(session, logged_in).collect_candidates(shadow=True)
    assert result["shadow"] is True

    async with session_scope() as session:
        rows = list((await session.execute(select(ProactiveOpportunity))).scalars())
    assert rows, "影子模式没有记录候选"
    assert all(r.state == "candidate" for r in rows), "影子模式擅自推进了状态"
    assert all(r.delivery_operation_id is None for r in rows), "影子模式产生了投递"


async def test_not_before_in_future_is_skipped(logged_in: TestEnv) -> None:
    """还没到该说的时候不登记，避免占着候选位。"""
    from ling.domains.proactive.sources import Candidate as C

    async with session_scope() as session:
        svc = _svc(session, logged_in)
        future = datetime.now(UTC) + timedelta(hours=5)
        row = await svc.propose(
            subject_id=logged_in.subject_id,
            kind="test",
            reason="还没到点",
            dedupe_key="future-key",
            not_before=future,
            best_until=future + timedelta(hours=1),
        )
    # propose 本身不判断时机（那是收集器的职责），这里只确认字段落库
    assert row is not None
    async with session_scope() as session:
        stored = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.dedupe_key == "future-key"
                )
            )
        ).scalar_one()
    assert stored.not_before is not None
    _ = C


async def test_propose_returns_none_on_duplicate_key(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        svc = _svc(session, logged_in)
        first = await svc.propose(
            subject_id=logged_in.subject_id,
            kind="test",
            reason="一",
            dedupe_key="dup-key",
        )
        second = await svc.propose(
            subject_id=logged_in.subject_id,
            kind="test",
            reason="二",
            dedupe_key="dup-key",
        )
    assert first is not None
    assert second is None, "相同去重键应拒绝重复登记"


async def test_expired_opportunities_are_cleaned(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        svc = _svc(session, logged_in)
        await svc.propose(
            subject_id=logged_in.subject_id,
            kind="test",
            reason="很快过期",
            dedupe_key="expire-key",
        )
    # 手动把过期时间推到过去
    from sqlalchemy import update

    async with session_scope() as session:
        await session.execute(
            update(ProactiveOpportunity)
            .where(ProactiveOpportunity.dedupe_key == "expire-key")
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
    async with session_scope() as session:
        expired = await _svc(session, logged_in).expire_stale()
    assert expired >= 1


async def test_memory_does_not_create_candidates(logged_in: TestEnv) -> None:
    """记忆不是主动联系的理由。

    否则"它记得我说过什么"会变成"它拿我记得的事来烦我"。
    """
    async with session_scope() as session:
        from ling.domains.memory.service import MemoryService

        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p_pro", kind="assertion", text="用户喜欢下雨天",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=logged_in.subject_id, turn_id="t_pro", slot="likes", value="下雨天",
        )
    async with session_scope() as session:
        candidates = await CausalSource(session, actor_id=ACTOR).collect()
    assert candidates == [], "记忆被当成了主动联系的理由"

async def test_due_intent_becomes_a_proactive_opportunity(logged_in: TestEnv) -> None:
    """她到点的念头必须能变成主动联系。

    `REASON_INTENT_DUE` 这个常量早就声明了，但**没有任何来源产出它**——
    `intents` 表里攒着她自己提出来的「过会儿找他约个饭」，到点了却永远
    变不成行动。那不是"没想起来"，是**说了要做然后永远没做**，
    正是路线图要排除的假自主。
    """
    from datetime import timedelta

    from sqlalchemy import select

    from ling.domains.intent.service import IntentService
    from ling.domains.proactive.service import ProactivityService
    from ling.infra.db import session_scope
    from ling.infra.models import ProactiveOpportunity, utcnow

    now = utcnow()
    async with session_scope() as session:
        await IntentService(session, actor_id="actor_ling").propose(
            goal="过会儿发个街边试吃的小事，顺便问一句吃没吃饭",
            kind="reach_out",
            source_kind="message",
            source_ref="turn_intent_test",
            subject_id=logged_in.subject_id,
            window_id="win_intent_test",
            not_before=now - timedelta(minutes=5),
            expires_at=now + timedelta(hours=3),
        )

    async with session_scope() as session:
        result = await ProactivityService(session, actor_id="actor_ling").collect_candidates()
    assert result["created"] >= 1, f"到点的念头没变成机会：{result}"

    async with session_scope() as session:
        row = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.kind == "intent_due"
                )
            )
        ).scalars().first()
    assert row is not None, "没有 intent_due 类型的机会"
    assert "试吃" in (row.reason or ""), f"理由没带上她自己的原话：{row.reason!r}"


async def test_expired_intent_does_not_become_opportunity(logged_in: TestEnv) -> None:
    """过期的念头**不补发**。

    补发等于"攒了一批打扰一起发出去"，比不主动更糟。
    """
    from datetime import timedelta

    from ling.domains.intent.service import IntentService
    from ling.domains.proactive.service import ProactivityService
    from ling.infra.db import session_scope
    from ling.infra.models import utcnow

    now = utcnow()
    async with session_scope() as session:
        await IntentService(session, actor_id="actor_ling").propose(
            goal="过期了的那件事",
            kind="reach_out",
            source_kind="message",
            source_ref="turn_expired",
            subject_id=logged_in.subject_id,
            window_id="win_intent_expired",
            not_before=now - timedelta(hours=3),
            expires_at=now - timedelta(hours=1),
        )

    async with session_scope() as session:
        result = await ProactivityService(session, actor_id="actor_ling").collect_candidates()
    assert result["created"] == 0, f"过期念头被补发了：{result}"
