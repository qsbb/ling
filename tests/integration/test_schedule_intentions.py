"""长程意向（阶段 1.75a）：真 PostgreSQL 上的状态机、容量与撤回。

这组用例守设计 §3A/§9 的可程序化验收：

1. 同名不同来源/不同区间**可以并存**；同来源重放返回原 ID（幂等）；
2. 确认"周五想旅行"**不产生任何段**——确认目标是目标，不是时段；
3. 容量满（proposed ≤4、活跃 ≤12）时拒收新提议，`intention_capacity` 原因码，
   **不淘汰**用户已确认的来源，也不自动淘汰；
4. 目标日已过、无证据 → `expired_unknown`（**不是** missed）；有证据才 `missed`；
5. 撤回递归取消未来准备段子树与下游意向，过去/已开始段不动；
6. 终态不能被复活（再 confirm/再撤回都报错）；
7. 准备段只在"已确认 + 有 prep_source_ref + 目标日晚于段所在计划日"时才能挂，
   可见范围继承来源里最严的那个。

`web_env` 每个用例建一个独立 schema（`create_all`，真 PG），不会互相污染。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.intents import (
    CONFIRM_ENTRY_COMMITMENT,
    CONFIRM_ENTRY_USER,
    PROPOSAL_TTL,
    REASON_EXPIRED_TARGET_DAY,
    REASON_EXPIRED_TTL,
    REASON_INTENTION_CAPACITY,
    IntentCapacityError,
    IntentEvidenceError,
    IntentService,
    IntentStateError,
    IntentTerminalError,
    IntentValidationError,
    attach_preparation,
    intention_for_segment,
    segments_for_intention,
)
from ling.domains.agenda.timeline import local_minute_instant, plan_day_window
from ling.infra.db import session_scope
from ling.infra.models import (
    INTENTION_TERMINAL_STATES,
    ActivityRow,
    CommitmentRow,
    DayPlanRow,
    ScheduleIntentionRow,
    ScheduleSegmentRow,
)
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
# 2026-11-08 12:00 上海：测试里所有"现在"都显式传，不依赖真实时钟。
NOW = datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
TODAY = date(2026, 11, 8)
FRIDAY = date(2026, 11, 13)


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "travel",
        "title": "周五想旅行",
        "target_start_day": FRIDAY,
        "target_end_day": FRIDAY,
        "source_kind": "user",
        "source_ref": "turn:intent-1",
        "state": "planned",
        "target_timezone": TZ,
        "now": NOW,
    }
    base.update(overrides)
    return base


async def _create(**overrides: object):  # noqa: ANN201
    async with session_scope() as session:
        return await IntentService(session, actor_id=ACTOR).create(**_kwargs(**overrides))


async def _confirm(intention_id: str, *, entry: str = CONFIRM_ENTRY_USER, evidence: str = "turn:ok"):  # noqa: ANN201
    async with session_scope() as session:
        return await IntentService(session, actor_id=ACTOR).confirm(
            intention_id, entry=entry, evidence_ref=evidence, now=NOW
        )


async def _terminal(intention_id: str, *, state: str, evidence: str | None):  # noqa: ANN201
    async with session_scope() as session:
        return await IntentService(session, actor_id=ACTOR).reach_terminal(
            intention_id, state=state, evidence_ref=evidence, now=NOW
        )


async def _revoke(intention_id: str, *, evidence: str = "turn:withdraw", now: datetime = NOW):  # noqa: ANN201
    async with session_scope() as session:
        return await IntentService(session, actor_id=ACTOR).revoke(
            intention_id, evidence_ref=evidence, now=now
        )


async def _expire(*, now: datetime):  # noqa: ANN201
    async with session_scope() as session:
        return await IntentService(session, actor_id=ACTOR).expire_due(now=now)


async def _rows() -> list[ScheduleIntentionRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ScheduleIntentionRow).order_by(ScheduleIntentionRow.created_at)
                )
            ).scalars()
        )


async def _count(model: type) -> int:
    async with session_scope() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _add_plan(plan_day: date, *, revision: int = 1) -> DayPlanRow:
    start, end = plan_day_window(plan_day, TZ)
    row = DayPlanRow(
        id=new_id("plan"),
        actor_id=ACTOR,
        plan_day=plan_day,
        timezone=TZ,
        timezone_source="preference",
        timezone_revision=1,
        routine_revision=1,
        window_start_at=start,
        window_end_at=end,
        revision=revision,
        state="ready",
        generation_source="model",
        created_at=NOW,
        updated_at=NOW,
    )
    async with session_scope() as session:
        session.add(row)
    return row


async def _add_segment(
    plan: DayPlanRow,
    *,
    start_minute: int,
    end_minute: int,
    layer: str = "block",
    parent_id: str | None = None,
    intention_id: str | None = None,
    preparation_for_day: date | None = None,
    title: str = "可选安排",
) -> ScheduleSegmentRow:
    row = ScheduleSegmentRow(
        id=new_id("seg"),
        plan_id=str(plan.id),
        actor_id=ACTOR,
        parent_id=parent_id,
        parent_key=parent_id or "",
        layer=layer,
        kind="chore",
        title=title,
        start_at=local_minute_instant(plan.plan_day, start_minute, TZ),
        end_at=local_minute_instant(plan.plan_day, end_minute, TZ),
        place_id=None,
        commitment_level="tentative",
        status="current",
        is_leaf=True,
        anchor_kind="",
        source="model",
        revision_created=plan.revision,
        revision_ended=None,
        source_event_ids=[],
        intention_id=intention_id,
        preparation_for_day=preparation_for_day,
        visibility=None,
        created_at=NOW,
        updated_at=NOW,
    )
    async with session_scope() as session:
        session.add(row)
    return row


# ---------------------------------------------------------------- 幂等与并存


async def test_same_title_different_sources_and_windows_coexist(web_env: TestEnv) -> None:  # noqa: ARG001
    first = await _create(source_kind="user", source_ref="turn:a", state="planned")
    second = await _create(source_kind="user", source_ref="turn:b", state="planned")
    third = await _create(
        source_kind="user",
        source_ref="turn:a",
        state="planned",
        target_start_day=date(2026, 11, 14),
        target_end_day=date(2026, 11, 15),
    )

    assert len({first.intention_id, second.intention_id, third.intention_id}) == 3
    assert {first.title, second.title, third.title} == {"周五想旅行"}
    assert await _count(ScheduleIntentionRow) == 3


async def test_same_source_replay_returns_original_id(web_env: TestEnv) -> None:  # noqa: ARG001
    first = await _create(source_kind="user", source_ref="turn:replay", state="planned")
    # 同来源 + 同区间重放：返回**原 ID**，标题不同也不新建（标题不参与唯一性）。
    again = await _create(
        source_kind="user",
        source_ref="turn:replay",
        state="planned",
        title="改了个标题",
    )
    assert again.intention_id == first.intention_id
    assert again.title == first.title
    assert again.revision == first.revision
    assert await _count(ScheduleIntentionRow) == 1


async def test_same_source_after_terminal_still_returns_original(web_env: TestEnv) -> None:  # noqa: ARG001
    """重放不能把终态"复活"成新行，更不能改回 planned。"""
    first = await _create(source_kind="user", source_ref="turn:once", state="planned")
    await _terminal(first.intention_id, state="missed", evidence="turn:user-said-no")

    replay = await _create(source_kind="user", source_ref="turn:once", state="planned")
    assert replay.intention_id == first.intention_id
    assert replay.state == "missed"


# ---------------------------------------------------------------- 确认 ≠ 时段


async def test_confirming_a_trip_creates_no_segments_or_facts(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(source_kind="user", source_ref="turn:trip", state="planned")
    confirmed = await _confirm(intent.intention_id, evidence="turn:trip-ok")

    assert confirmed.state == "confirmed"
    assert confirmed.is_hard_anchor is False
    assert confirmed.confirmation_source_ref == "turn:trip-ok"
    assert confirmed.resolution_evidence_ref == "turn:trip-ok"
    assert confirmed.confirmed_at is not None
    assert confirmed.revision == intent.revision + 1

    # 确认"目标"不产生任何时段占用，也不制造活动/承诺/事实。
    assert await _count(ScheduleSegmentRow) == 0
    assert await _count(DayPlanRow) == 0
    assert await _count(ActivityRow) == 0
    assert await _count(CommitmentRow) == 0


async def test_confirmed_target_without_prep_source_cannot_get_preparation(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(source_kind="user", source_ref="turn:trip", state="planned")
    await _confirm(intent.intention_id, evidence="turn:trip-ok")
    plan = await _add_plan(date(2026, 11, 10))
    segment = await _add_segment(plan, start_minute=600, end_minute=660)

    async with session_scope() as session:
        with pytest.raises(IntentStateError) as excinfo:
            await attach_preparation(
                session,
                intention_id=intent.intention_id,
                segment_id=str(segment.id),
                actor_id=ACTOR,
                now=NOW,
            )
    assert excinfo.value.reason_code == "intention_preparation_requires_source"
    assert await _count(ScheduleSegmentRow) == 1


# ---------------------------------------------------------------- 容量


async def test_capacity_rejects_new_proposals_without_evicting_confirmed(web_env: TestEnv) -> None:  # noqa: ARG001
    proposals = [
        await _create(
            source_kind="model",
            source_ref=f"outline:{index}",
            state="proposed",
            title=f"候选想法 {index}",
        )
        for index in range(4)
    ]
    # 确认其中一条：它仍是活跃意向，但不再是 proposed。
    confirmed = await _confirm(
        proposals[0].intention_id, entry=CONFIRM_ENTRY_COMMITMENT, evidence="cmt:1"
    )
    assert confirmed.state == "confirmed"

    # 空出一个 proposed 名额后还能再进一条。
    last = await _create(source_kind="model", source_ref="outline:4", state="proposed")
    assert await _count(ScheduleIntentionRow) == 5

    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        verdict = await service.capacity_check(state="proposed")
    assert not verdict.allowed
    assert verdict.reason_code == REASON_INTENTION_CAPACITY
    assert verdict.axis == "proposed"
    assert verdict.proposed_count == 4

    async with session_scope() as session:
        with pytest.raises(IntentCapacityError) as excinfo:
            await IntentService(session, actor_id=ACTOR).create(
                **_kwargs(source_kind="model", source_ref="outline:rejected", state="proposed")
            )
    assert excinfo.value.reason_code == REASON_INTENTION_CAPACITY
    assert excinfo.value.verdict.axis == "proposed"

    rows = await _rows()
    assert len(rows) == 5, "满额只拒收新提议，不淘汰任何已有行"
    assert {row.id for row in rows} == {
        *(item.intention_id for item in proposals),
        last.intention_id,
    }
    assert next(row for row in rows if row.id == confirmed.intention_id).state == "confirmed"

    # 满额时**重放**已有来源仍返回原 ID（幂等优先于容量）。
    async with session_scope() as session:
        replay = await IntentService(session, actor_id=ACTOR).create(
            **_kwargs(source_kind="model", source_ref="outline:0", state="proposed")
        )
    assert replay.intention_id == proposals[0].intention_id


async def test_active_capacity_blocks_planned_rows_too(web_env: TestEnv) -> None:  # noqa: ARG001
    for index in range(11):
        await _create(
            source_kind="user",
            source_ref=f"turn:active-{index}",
            state="planned",
            title=f"计划 {index}",
        )
    await _create(source_kind="user", source_ref="turn:twelfth", state="planned")

    async with session_scope() as session:
        verdict = await IntentService(session, actor_id=ACTOR).capacity_check(state="planned")
    assert not verdict.allowed
    assert verdict.axis == "active"

    async with session_scope() as session:
        with pytest.raises(IntentCapacityError):
            await IntentService(session, actor_id=ACTOR).create(
                **_kwargs(source_kind="user", source_ref="turn:thirteenth", state="planned")
            )
    assert await _count(ScheduleIntentionRow) == 12


async def test_concurrent_same_source_creates_one_row(web_env: TestEnv) -> None:  # noqa: ARG001
    """并发重放同一来源：唯一键 + actor 锁只留一行，两边拿到同一个 ID。"""

    async def make():  # noqa: ANN202
        async with session_scope() as session:
            return await IntentService(session, actor_id=ACTOR).create(
                **_kwargs(source_kind="user", source_ref="turn:concurrent", state="planned")
            )

    first, second = await asyncio.gather(make(), make())
    assert first.intention_id == second.intention_id
    assert await _count(ScheduleIntentionRow) == 1


async def test_capacity_is_enforced_under_concurrency(web_env: TestEnv) -> None:  # noqa: ARG001
    """3 条已有提议时并发提两条：只准一条进（否则 4 条上限会被锁外竞态击穿）。"""
    for index in range(3):
        await _create(
            source_kind="model",
            source_ref=f"outline:race-{index}",
            state="proposed",
            title=f"候选 {index}",
        )

    async def make(index: int):  # noqa: ANN202
        async with session_scope() as session:
            return await IntentService(session, actor_id=ACTOR).create(
                **_kwargs(source_kind="model", source_ref=f"outline:race-new-{index}", state="proposed")
            )

    results = await asyncio.gather(make(1), make(2), return_exceptions=True)
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1, results
    assert isinstance(errors[0], IntentCapacityError)
    assert await _count(ScheduleIntentionRow) == 4


# ---------------------------------------------------------------- 过期


async def test_passed_target_without_evidence_is_unknown_not_missed(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="user",
        source_ref="turn:past",
        state="planned",
        target_start_day=date(2026, 11, 9),
        target_end_day=date(2026, 11, 9),
    )
    report = await _expire(now=datetime(2026, 11, 10, 4, 0, tzinfo=UTC))

    assert intent.intention_id in report.expired_ids
    assert dict(report.reasons)[intent.intention_id] == REASON_EXPIRED_TARGET_DAY
    rows = await _rows()
    assert rows[0].state == "expired_unknown", "无证据只能是 unknown，不能写 missed"
    assert rows[0].resolution_evidence_ref is None
    assert rows[0].resolved_at is not None


async def test_proposal_ttl_expires_to_unknown(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="model",
        source_ref="outline:ttl",
        state="proposed",
        target_start_day=date(2026, 11, 20),
        target_end_day=date(2026, 11, 20),
    )
    before = await _expire(now=NOW + PROPOSAL_TTL - timedelta(minutes=1))
    assert before.expired_ids == ()

    after = await _expire(now=NOW + PROPOSAL_TTL + timedelta(minutes=1))
    assert intent.intention_id in after.expired_ids
    assert dict(after.reasons)[intent.intention_id] == REASON_EXPIRED_TTL

    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, intent.intention_id)
    assert row.state == "expired_unknown"


async def test_confirmed_target_expires_too_not_missed(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="user",
        source_ref="turn:confirmed-past",
        state="planned",
        target_start_day=date(2026, 11, 9),
        target_end_day=date(2026, 11, 9),
    )
    await _confirm(intent.intention_id, evidence="turn:ok")
    await _expire(now=datetime(2026, 11, 10, 4, 0, tzinfo=UTC))

    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, intent.intention_id)
    assert row.state == "expired_unknown"
    assert row.confirmation_source_ref == "turn:ok", "确认来源不能被过期抹掉"


async def test_missed_requires_explicit_evidence(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(source_kind="user", source_ref="turn:missed", state="planned")

    async with session_scope() as session:
        with pytest.raises(IntentEvidenceError) as excinfo:
            await IntentService(session, actor_id=ACTOR).reach_terminal(
                intent.intention_id, state="missed", now=NOW
            )
    assert excinfo.value.reason_code == "intention_resolution_evidence_required"

    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, intent.intention_id)
    assert row.state == "planned"

    missed = await _terminal(
        intent.intention_id, state="missed", evidence="turn:user-said-did-not-go"
    )
    assert missed.state == "missed"
    assert missed.resolution_evidence_ref == "turn:user-said-did-not-go"


async def test_completed_requires_planned_or_confirmed(web_env: TestEnv) -> None:  # noqa: ARG001
    """一个从未成为计划的候选不能直接"完成了"。"""
    intent = await _create(source_kind="model", source_ref="outline:raw", state="proposed")
    async with session_scope() as session:
        with pytest.raises(IntentStateError):
            await IntentService(session, actor_id=ACTOR).reach_terminal(
                intent.intention_id,
                state="completed",
                evidence_ref="activity:act_1",
                now=NOW,
            )


async def test_expired_unknown_cannot_carry_evidence(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(source_kind="user", source_ref="turn:unknown", state="planned")
    async with session_scope() as session:
        with pytest.raises(IntentEvidenceError):
            await IntentService(session, actor_id=ACTOR).reach_terminal(
                intent.intention_id,
                state="expired_unknown",
                evidence_ref="turn:something",
                now=NOW,
            )


async def test_expired_unknown_requires_target_day_to_pass(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="user",
        source_ref="turn:future-unknown",
        state="planned",
        target_start_day=date(2026, 11, 9),
        target_end_day=date(2026, 11, 9),
    )
    async with session_scope() as session:
        with pytest.raises(IntentStateError):
            await IntentService(session, actor_id=ACTOR).reach_terminal(
                intent.intention_id, state="expired_unknown", now=NOW
            )


# ---------------------------------------------------------------- 终态不可复活


async def test_terminal_cannot_be_revived(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(source_kind="user", source_ref="turn:done", state="planned")
    await _confirm(intent.intention_id, evidence="turn:ok")
    completed = await _terminal(
        intent.intention_id, state="completed", evidence="activity:act_done"
    )
    assert completed.state == "completed"

    with pytest.raises(IntentTerminalError):
        await _confirm(intent.intention_id, evidence="turn:again")
    with pytest.raises(IntentTerminalError):
        await _terminal(intent.intention_id, state="missed", evidence="turn:late")
    with pytest.raises(IntentTerminalError):
        await _revoke(intent.intention_id)

    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, intent.intention_id)
    assert row.state == "completed"
    assert row.resolution_evidence_ref == "activity:act_done"
    assert row.confirmation_source_ref == "turn:ok"


# ---------------------------------------------------------------- 准备段


async def test_preparation_requires_confirmed_and_basis(web_env: TestEnv) -> None:  # noqa: ARG001
    proposed = await _create(source_kind="model", source_ref="outline:prep", state="proposed")
    plan = await _add_plan(date(2026, 11, 10))
    segment = await _add_segment(plan, start_minute=600, end_minute=660)

    async with session_scope() as session:
        with pytest.raises(IntentStateError) as excinfo:
            await attach_preparation(
                session,
                intention_id=proposed.intention_id,
                segment_id=str(segment.id),
                now=NOW,
            )
    assert excinfo.value.reason_code == "intention_preparation_requires_confirmed"

    # 有 prep_source_ref 的确认意向才允许挂准备段
    ready = await _create(
        source_kind="user",
        source_ref="turn:prep-ok",
        state="planned",
        target_start_day=date(2026, 11, 13),
        target_end_day=date(2026, 11, 13),
        prep_source_ref="note:buy-tickets",
        prep_due_day=date(2026, 11, 11),
    )
    await _confirm(ready.intention_id, evidence="turn:prep-confirmed")
    async with session_scope() as session:
        attached = await attach_preparation(
            session,
            intention_id=ready.intention_id,
            segment_id=str(segment.id),
            actor_id=ACTOR,
            preparation_for_day=date(2026, 11, 13),
            source_visibilities=("public",),
            now=NOW,
        )
    assert attached.intention_id == ready.intention_id
    assert attached.preparation_for_day == date(2026, 11, 13)
    # 段本身 visibility 是 NULL（旧段/未设置），意向是 actor_private → 取最严
    assert attached.visibility == "actor_private"
    assert f"intention:{ready.intention_id}" in attached.source_event_ids
    assert "turn:prep-confirmed" in attached.source_event_ids

    async with session_scope() as session:
        assert (await intention_for_segment(session, str(segment.id))).id == ready.intention_id
        listed = await segments_for_intention(session, ready.intention_id)
    assert [item.id for item in listed] == [segment.id]


async def test_preparation_rejects_bad_target_days(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="user",
        source_ref="turn:prep-day",
        state="planned",
        prep_source_ref="note:prep",
        prep_due_day=date(2026, 11, 11),
    )
    await _confirm(intent.intention_id, evidence="turn:ok")

    # 准备段所在的计划日 == 目标日：这不是"前置准备"
    same_day_plan = await _add_plan(FRIDAY)
    same_day_segment = await _add_segment(same_day_plan, start_minute=600, end_minute=660)
    async with session_scope() as session:
        with pytest.raises(IntentStateError) as excinfo:
            await attach_preparation(
                session,
                intention_id=intent.intention_id,
                segment_id=str(same_day_segment.id),
                now=NOW,
            )
    assert excinfo.value.reason_code == "intention_preparation_target_not_future"

    # preparation_for_day 超出目标区间
    plan = await _add_plan(date(2026, 11, 10))
    segment = await _add_segment(plan, start_minute=600, end_minute=660)
    async with session_scope() as session:
        with pytest.raises(IntentStateError) as excinfo:
            await attach_preparation(
                session,
                intention_id=intent.intention_id,
                segment_id=str(segment.id),
                preparation_for_day=date(2026, 11, 20),
                now=NOW,
            )
    assert excinfo.value.reason_code == "intention_preparation_day_out_of_range"


async def test_anchor_or_confirmed_segment_cannot_be_preparation(web_env: TestEnv) -> None:  # noqa: ARG001
    intent = await _create(
        source_kind="user",
        source_ref="turn:prep-anchor",
        state="planned",
        prep_source_ref="note:prep",
        prep_due_day=date(2026, 11, 11),
    )
    await _confirm(intent.intention_id, evidence="turn:ok")
    plan = await _add_plan(date(2026, 11, 10))
    segment = await _add_segment(plan, start_minute=600, end_minute=660)
    async with session_scope() as session:
        row = await session.get(ScheduleSegmentRow, segment.id)
        row.commitment_level = "confirmed"
    async with session_scope() as session:
        with pytest.raises(IntentStateError) as excinfo:
            await attach_preparation(
                session,
                intention_id=intent.intention_id,
                segment_id=str(segment.id),
                now=NOW,
            )
    assert excinfo.value.reason_code == "intention_preparation_not_optional"


# ---------------------------------------------------------------- 撤回递归


async def test_revoke_cancels_future_preparation_subtree_and_downstream(web_env: TestEnv) -> None:  # noqa: ARG001
    parent = await _create(
        source_kind="user",
        source_ref="turn:revoke-parent",
        state="planned",
        prep_source_ref="note:prep",
        prep_due_day=date(2026, 11, 11),
    )
    await _confirm(parent.intention_id, evidence="turn:parent-ok")
    downstream = await _create(
        source_kind="model",
        source_ref=parent.intention_id,
        state="proposed",
        target_start_day=date(2026, 11, 15),
        target_end_day=date(2026, 11, 15),
        title="为旅行准备的周末",
    )

    plan = await _add_plan(date(2026, 11, 10))
    prep = await _add_segment(plan, start_minute=600, end_minute=660)
    child = await _add_segment(
        plan,
        start_minute=620,
        end_minute=640,
        layer="minute",
        parent_id=str(prep.id),
    )
    # 第三层（block → hour → minute）：只取消一层会把这个孙段留在时间轴上。
    grandchild = await _add_segment(
        plan,
        start_minute=625,
        end_minute=630,
        layer="minute",
        parent_id=str(child.id),
    )
    # 已经开始的段：撤回**不动**它（历史/正在进行的事实）
    past_plan = await _add_plan(date(2026, 11, 8))
    past = await _add_segment(
        past_plan,
        start_minute=60,
        end_minute=120,
        intention_id=parent.intention_id,
        preparation_for_day=FRIDAY,
    )

    async with session_scope() as session:
        await attach_preparation(
            session,
            intention_id=parent.intention_id,
            segment_id=str(prep.id),
            now=NOW,
        )

    report = await _revoke(parent.intention_id, evidence="turn:user-withdrew")

    assert report.root_id == parent.intention_id
    assert parent.intention_id in report.withdrawn_ids
    assert downstream.intention_id in report.withdrawn_ids, "下游意向要跟着撤回"
    assert {prep.id, child.id, grandchild.id} <= set(report.segment_ids)
    assert past.id not in report.segment_ids
    assert str(plan.id) in report.plan_ids

    async with session_scope() as session:
        parent_row = await session.get(ScheduleIntentionRow, parent.intention_id)
        child_row = await session.get(ScheduleIntentionRow, downstream.intention_id)
        prep_row = await session.get(ScheduleSegmentRow, prep.id)
        child_segment_row = await session.get(ScheduleSegmentRow, child.id)
        grandchild_row = await session.get(ScheduleSegmentRow, grandchild.id)
        past_row = await session.get(ScheduleSegmentRow, past.id)
        plan_row = await session.get(DayPlanRow, plan.id)

    assert parent_row.state == "abandoned"
    assert parent_row.resolution_evidence_ref == "turn:user-withdrew"
    assert child_row.state == "abandoned"
    assert child_row.resolution_evidence_ref == "turn:user-withdrew"
    assert prep_row.status == "superseded"
    assert child_segment_row.status == "superseded", "准备段的子段要递归取消"
    assert grandchild_row.status == "superseded", "孙段（第三层）也要递归取消"
    assert prep_row.revision_ended == plan_row.revision == 2
    assert past_row.status == "current", "已开始的段不能被撤回改写"
    assert "reason:user_withdrew" in prep_row.source_event_ids
    # 撤回之后不能再挂新的准备段
    with pytest.raises(IntentStateError):
        async with session_scope() as session2:
            await attach_preparation(
                session2,
                intention_id=parent.intention_id,
                segment_id=str(past.id),
                now=NOW,
            )


async def test_revoke_by_source_propagates_without_touching_other_sources(web_env: TestEnv) -> None:  # noqa: ARG001
    first = await _create(source_kind="user", source_ref="cmt_42", state="planned", title="第一件")
    second = await _create(
        source_kind="user",
        source_ref="cmt_42",
        state="planned",
        target_start_day=date(2026, 11, 14),
        target_end_day=date(2026, 11, 14),
        title="第二件",
    )
    unrelated = await _create(source_kind="user", source_ref="cmt_43", state="planned")

    async with session_scope() as session:
        report = await IntentService(session, actor_id=ACTOR).revoke_by_source(
            source_kind="user",
            source_ref="cmt_42",
            evidence_ref="commitment:cancelled",
            now=NOW,
        )
    assert set(report.withdrawn_ids) == {first.intention_id, second.intention_id}

    async with session_scope() as session:
        assert (await session.get(ScheduleIntentionRow, unrelated.intention_id)).state == "planned"
        for item in (first, second):
            assert (await session.get(ScheduleIntentionRow, item.intention_id)).state == "abandoned"


# ---------------------------------------------------------------- 创建规则


async def test_create_cannot_confirm_or_let_model_plan(web_env: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        with pytest.raises(IntentStateError):
            await service.create(**_kwargs(state="confirmed", source_kind="user"))
        with pytest.raises(IntentStateError):
            await service.create(**_kwargs(state="planned", source_kind="model"))

    assert await _count(ScheduleIntentionRow) == 0


async def test_create_validates_fields_and_window(web_env: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        with pytest.raises(IntentValidationError):
            await service.create(**_kwargs(kind="sleep"))
        with pytest.raises(IntentValidationError):
            await service.create(**_kwargs(title="   "))
        with pytest.raises(IntentValidationError):
            await service.create(**_kwargs(target_start_day=FRIDAY, target_end_day=TODAY))
        with pytest.raises(IntentValidationError):
            await service.create(
                **_kwargs(
                    target_start_day=FRIDAY,
                    target_end_day=FRIDAY + timedelta(days=91),
                )
            )
        with pytest.raises(IntentValidationError):
            await service.create(**_kwargs(target_timezone="Mars/Olympus"))
        with pytest.raises(IntentValidationError):
            await service.create(**_kwargs(priority=101))
    assert await _count(ScheduleIntentionRow) == 0


# ---------------------------------------------------------------- 展望读取


async def test_list_active_filters_by_window_and_orders_by_state(web_env: TestEnv) -> None:  # noqa: ARG001
    proposed = await _create(
        source_kind="model", source_ref="outline:h1", state="proposed", title="候选"
    )
    planned = await _create(
        source_kind="user", source_ref="turn:h2", state="planned", title="考虑中"
    )
    confirmed = await _create(
        source_kind="user", source_ref="turn:h3", state="planned", title="已确认"
    )
    await _confirm(confirmed.intention_id, evidence="turn:h3-ok")
    finished = await _create(
        source_kind="user", source_ref="turn:h4", state="planned", title="已完成"
    )
    await _terminal(finished.intention_id, state="completed", evidence="activity:x")
    outside = await _create(
        source_kind="user",
        source_ref="turn:h5",
        state="planned",
        target_start_day=date(2026, 11, 30),
        target_end_day=date(2026, 11, 30),
        title="窗口外",
    )

    async with session_scope() as session:
        views = await IntentService(session, actor_id=ACTOR).list_active(
            start_day=date(2026, 11, 12), end_day=date(2026, 11, 13)
        )
    assert [view.intention_id for view in views] == [
        confirmed.intention_id,
        planned.intention_id,
        proposed.intention_id,
    ]
    assert outside.intention_id not in {view.intention_id for view in views}

    async with session_scope() as session:
        limited = await IntentService(session, actor_id=ACTOR).list_active(
            start_day=date(2026, 11, 12), end_day=date(2026, 11, 13), limit=2
        )
    assert [view.intention_id for view in limited] == [
        confirmed.intention_id,
        planned.intention_id,
    ]


def test_terminal_states_are_the_documented_four() -> None:
    """文档口径必须和表里的终态白名单一致（防止悄悄多一个终态）。"""
    assert set(INTENTION_TERMINAL_STATES) == {
        "completed",
        "missed",
        "abandoned",
        "expired_unknown",
    }
