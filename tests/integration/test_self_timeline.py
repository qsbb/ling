"""自我时间线：角色「做过什么」。

核心不是"能记住"，而是**不能编造**：

1. 只有**已完成且有完成证据**的活动才能派生经历
2. 计划/打算/想象永远不进自我经历
3. 同一件活动只派生一条（去重键）
4. 群聊不注入自我经历
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling.domains.activity.service import ActivityService
from ling.domains.memory.self_timeline import (
    SelfTimelineService,
    dedupe_key_for,
)
from ling.infra.db import session_scope
from ling.infra.models import SelfExperienceRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _completed_activity(
    env: TestEnv, *, evidence: str = "读完了第二章", record: bool = True
) -> str:
    """造一个已完成的活动；``record=True`` 时按生产链路派生经历。

    生产里由 `self.timeline` handler 的 backfill 完成派生，
    所以涉及"读时间线"的测试必须先派生——只完成活动是不会有经历的。
    """
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
        await svc.transition(act.activity_id, "completed", completion_evidence=evidence)
    if record:
        async with session_scope() as session:
            await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(
                act.activity_id
            )
    return act.activity_id


# ------------------------------------------------------------------ 证据规则


async def test_completed_with_evidence_creates_experience(logged_in: TestEnv) -> None:
    activity_id = await _completed_activity(logged_in)
    async with session_scope() as session:
        view = await SelfTimelineService(
            session, actor_id=ACTOR
        ).record_completed_activity(activity_id)
    assert view is not None
    assert "读" in view.summary
    assert view.origin == "simulated"


async def test_running_activity_not_recorded(logged_in: TestEnv) -> None:
    """还在进行的活动不算"做过"。"""
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
    async with session_scope() as session:
        view = await SelfTimelineService(
            session, actor_id=ACTOR
        ).record_completed_activity(act.activity_id)
    assert view is None, "进行中的活动被写成了经历"


async def test_paused_activity_not_recorded(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
        await svc.transition(act.activity_id, "paused")
    async with session_scope() as session:
        assert (
            await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(
                act.activity_id
            )
            is None
        )


async def test_completed_without_evidence_not_recorded(logged_in: TestEnv) -> None:
    """没有完成证据的历史数据也不能写成经历。"""
    from sqlalchemy import update

    from ling.infra.models import ActivityRow

    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
    # 绕过服务直接标 completed 且无证据（模拟历史脏数据）
    async with session_scope() as session:
        await session.execute(
            update(ActivityRow)
            .where(ActivityRow.id == act.activity_id)
            .values(state="completed", completion_evidence=None)
        )
    async with session_scope() as session:
        assert (
            await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(
                act.activity_id
            )
            is None
        ), "没有证据的活动被写成了经历"


# ------------------------------------------------------------------ 去重


async def test_dedupe_key_uses_activity_id(logged_in: TestEnv) -> None:
    assert dedupe_key_for("act_1") == "activity:act_1"


async def test_same_activity_recorded_once(logged_in: TestEnv) -> None:
    activity_id = await _completed_activity(logged_in)
    async with session_scope() as session:
        await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(activity_id)
    async with session_scope() as session:
        await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(activity_id)
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(SelfExperienceRow).where(
                        SelfExperienceRow.dedupe_key == dedupe_key_for(activity_id)
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1, f"同一活动派生了 {len(rows)} 条经历"


async def test_backfill_is_idempotent(logged_in: TestEnv) -> None:
    """补齐历史：第一次补出经历，第二次一条都不补。

    这里刻意不派生（`record=False`）——否则测的就不是 backfill 了。
    """
    await _completed_activity(logged_in, evidence="写了两段", record=False)
    async with session_scope() as session:
        first = await SelfTimelineService(session, actor_id=ACTOR).backfill()
    async with session_scope() as session:
        second = await SelfTimelineService(session, actor_id=ACTOR).backfill()
    assert first >= 1, "backfill 没有补出任何经历"
    assert second == 0, "重复补齐又产生了经历"


# ------------------------------------------------------------------ 时间线读取


async def test_today_returns_recent(logged_in: TestEnv) -> None:
    await _completed_activity(logged_in, evidence="读完了第三章")
    async with session_scope() as session:
        today = await SelfTimelineService(session, actor_id=ACTOR).today()
    assert today, "今天的经历为空"


async def test_recent_respects_limit(logged_in: TestEnv) -> None:
    for i in range(5):
        await _completed_activity(logged_in, evidence=f"第{i}件事")
    async with session_scope() as session:
        recent = await SelfTimelineService(session, actor_id=ACTOR).recent(days=1, limit=2)
    assert len(recent) <= 2


# ------------------------------------------------------------------ 注入


async def test_injected_in_private_chat(logged_in: TestEnv) -> None:
    from ling.kernel.self_view import SelfViewBuilder

    await _completed_activity(logged_in, evidence="读完了第二章")
    async with session_scope() as session:
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id="win_self",
        ).build()
    assert "self_timeline" in view.sources()
    text = view.render()
    assert "模拟" in text, "自我经历没有标明是模拟生活"


async def test_not_injected_in_group(logged_in: TestEnv) -> None:
    from ling.kernel.self_view import SelfViewBuilder

    await _completed_activity(logged_in, evidence="读完了第二章")
    async with session_scope() as session:
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id="win_group_self",
            is_group=True,
        ).build()
    assert "self_timeline" not in view.sources()
    assert any("群聊不注入自我经历" in w for _, w in view.dropped)


async def test_nothing_injected_when_no_experience(logged_in: TestEnv) -> None:
    from ling.kernel.self_view import SelfViewBuilder

    async with session_scope() as session:
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id="win_empty_self",
        ).build()
    assert "self_timeline" not in view.sources()

async def test_plan_window_end_completes_activity(logged_in: TestEnv) -> None:
    """计划窗口结束后，模拟活动必须能收尾并派生经历。

    真实缺口：`life.tick` 原来只会"暂停"，活动永远不会 completed，
    自我时间线就永远是空的——功能写了但永远没数据。
    """
    from datetime import UTC, datetime, timedelta

    from ling.domains.agenda.service import AgendaService
    from ling.worker import handle_life_tick

    now = datetime.now(UTC)
    async with session_scope() as session:
        agenda = AgendaService(session, actor_id=ACTOR)
        await agenda.set_routine(timezone="Asia/Shanghai", wake_minute=0, rest_minute=1439)
        await agenda.propose_plan(
            kind="reading",
            title="读会儿书",
            start_at=now - timedelta(minutes=30),
            end_at=now - timedelta(minutes=1),  # 窗口已过
        )
    # 先开活动（用未过期的窗口）
    async with session_scope() as session:
        agenda = AgendaService(session, actor_id=ACTOR)
        plans = await agenda.upcoming()
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE agenda_plans SET start_at = :s, end_at = :e WHERE id = :i"
            ),
            {"s": now - timedelta(minutes=1), "e": now + timedelta(minutes=30), "i": plans[0].plan_id},
        )
    await handle_life_tick({})
    # 把窗口推到过去，再推进一次 → 应该收尾
    async with session_scope() as session:
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE agenda_plans SET start_at = :s, end_at = :e"
            ),
            {"s": now - timedelta(minutes=40), "e": now - timedelta(minutes=1)},
        )
    await handle_life_tick({})

    from ling.domains.activity.service import ActivityService

    async with session_scope() as session:
        current = await ActivityService(session, actor_id=ACTOR).current()
    assert current is None or current.state != "running", "窗口结束后活动仍在运行"

    async with session_scope() as session:
        created = await SelfTimelineService(session, actor_id=ACTOR).backfill()
    assert created >= 1, "活动收尾后没有派生自我经历"
