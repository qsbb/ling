"""Agenda 域：软作息、计划，以及「计划不是事实」。

三条硬规则的测试：
1. 作息是**软**的：它是宽窗口，不强制；跨午夜要能表达
2. 计划**永远不能**被写成已完成——完成必须来自 Activity 且有证据
3. tentative 与 confirmed 用词不同（用户据此判断能不能当约定依赖）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.activity.service import ActivityService
from ling.domains.agenda.service import (
    MAX_LIVE_PLANS,
    AgendaService,
    RoutineView,
)
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


def _svc(session):  # noqa: ANN001, ANN202
    return AgendaService(session, actor_id=ACTOR)


# ------------------------------------------------------------------ 作息


def test_awake_window_normal() -> None:
    """普通作息：08:00 - 23:00（上海）。"""
    r = RoutineView(timezone="Asia/Shanghai", wake_minute=480, rest_minute=1380, source="default")
    assert r.is_awake(datetime(2026, 10, 27, 3, 0, tzinfo=UTC)) is True   # 上海 11:00
    assert r.is_awake(datetime(2026, 10, 27, 15, 0, tzinfo=UTC)) is False  # 上海 23:00


def test_awake_window_cross_midnight() -> None:
    """跨午夜：10:00 起、02:00 睡（wake > rest）。"""
    r = RoutineView(timezone="Asia/Shanghai", wake_minute=600, rest_minute=120, source="x")
    assert r.is_awake(datetime(2026, 10, 27, 1, 0, tzinfo=UTC)) is False  # 上海 09:00
    assert r.is_awake(datetime(2026, 10, 27, 5, 0, tzinfo=UTC)) is True   # 上海 13:00
    # 上海 01:00 = UTC 17:00 → 应在 [0,120) 内 → 醒着
    assert r.is_awake(datetime(2026, 10, 27, 17, 0, tzinfo=UTC)) is True


def test_equal_minutes_means_always_awake() -> None:
    """未配置有效窗口时默认醒着——不要凭空造出"在睡觉"。"""
    r = RoutineView(timezone="Asia/Shanghai", wake_minute=0, rest_minute=0, source="x")
    assert r.is_awake(datetime(2026, 10, 27, 4, 0, tzinfo=UTC)) is True


def test_invalid_timezone_falls_back_to_utc(logged_in: TestEnv) -> None:
    """非法时区退回 UTC，**不用服务器本地时区**。

    用本地时区会让"我的早上"变成机房所在时区的早上，
    在跨时区部署时表现为作息整体漂移。
    """
    r = RoutineView(timezone="Not/AZone", wake_minute=480, rest_minute=1380, source="x")
    assert r.is_awake(datetime(2026, 10, 27, 9, 0, tzinfo=UTC)) is True


async def test_set_routine_validates_range(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        with pytest.raises(ValueError):
            await _svc(session).set_routine(wake_minute=1440)


async def test_set_routine_persists(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        await _svc(session).set_routine(timezone="Asia/Tokyo", wake_minute=420, rest_minute=1320)
    async with session_scope() as session:
        routine = await _svc(session).routine()
    assert routine.wake_minute == 420
    assert routine.timezone == "Asia/Tokyo"
    assert routine.source == "configured"


async def test_default_routine_is_not_aggressive(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        routine = await _svc(session).routine()
    assert routine.source == "default"
    assert 0 < routine.wake_minute < routine.rest_minute < 1440


# ------------------------------------------------------------------ 计划


async def test_propose_plan_and_list(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading",
            title="读会儿书",
            start_at=now + timedelta(minutes=10),
            end_at=now + timedelta(minutes=40),
        )
    assert plan is not None
    async with session_scope() as session:
        upcoming = await _svc(session).upcoming()
    assert any(p.plan_id == plan.plan_id for p in upcoming)


async def test_live_plans_are_capped(logged_in: TestEnv) -> None:
    """超过上限不再追加——不能让角色像在念行程表。"""
    now = datetime.now(UTC)
    results = []
    async with session_scope() as session:
        svc = _svc(session)
        for i in range(MAX_LIVE_PLANS + 2):
            results.append(
                await svc.propose_plan(
                    kind="reading",
                    title=f"小事{i}",
                    start_at=now + timedelta(minutes=10 + i),
                    end_at=now + timedelta(minutes=40 + i),
                )
            )
    assert sum(1 for r in results if r is not None) == MAX_LIVE_PLANS
    assert results[-1] is None


async def test_due_plans_respects_start_time(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        await _svc(session).propose_plan(
            kind="reading",
            title="还没到点",
            start_at=now + timedelta(hours=2),
            end_at=now + timedelta(hours=3),
        )
    async with session_scope() as session:
        due = await _svc(session).due_plans(now=now)
    assert due == []


async def test_end_before_start_rejected(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        with pytest.raises(ValueError):
            await _svc(session).propose_plan(
                kind="reading",
                title="时间反了",
                start_at=now + timedelta(hours=2),
                end_at=now + timedelta(hours=1),
            )


# ------------------------------------------------------------------ 计划 ≠ 事实


async def test_plan_cannot_be_marked_done_directly(logged_in: TestEnv) -> None:
    """**核心约束**：计划不能直接被标成 done。

    能标的话，代码里很快就会出现"把计划写完当成做过了"，
    角色会开始描述从未发生过的人生。
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading",
            title="读书",
            start_at=now,
            end_at=now + timedelta(minutes=30),
        )
    async with session_scope() as session:
        with pytest.raises(ValueError, match="完成必须来自 Activity"):
            await _svc(session).set_state(plan.plan_id, "done")


async def test_activity_completion_marks_plan_done(logged_in: TestEnv) -> None:
    """有活动完成后，计划才能通过专用入口转 done。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading",
            title="读书",
            start_at=now,
            end_at=now + timedelta(minutes=30),
        )
    async with session_scope() as session:
        await _svc(session).on_activity_completed(plan.plan_id)
    async with session_scope() as session:
        upcoming = await _svc(session).upcoming()
    assert all(p.plan_id != plan.plan_id for p in upcoming), "done 的计划仍出现在待办里"


async def test_tentative_uses_hedged_wording(logged_in: TestEnv) -> None:
    """tentative 只能说"可能"，不能说成约好。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading",
            title="读书",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            commitment_level="tentative",
        )
    text = plan.describe()
    assert "可能" in text
    assert "约好" not in text


async def test_confirmed_uses_firm_wording(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading",
            title="读书",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            commitment_level="confirmed",
        )
    assert "约好" in plan.describe()


async def test_activity_requires_evidence_for_completion(logged_in: TestEnv) -> None:
    """活动标完成必须有证据——这是"计划不是事实"的第二道闸。"""
    from ling.domains.activity.service import InvalidTransition

    async with session_scope() as session:
        act = await ActivityService(session, actor_id=ACTOR).propose(kind="reading")
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        await svc.transition(act.activity_id, "running")
        with pytest.raises(InvalidTransition):
            await svc.transition(act.activity_id, "completed")


# ------------------------------------------------------------------ 边界


async def test_next_boundary_is_in_future(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        routine = await _svc(session).routine()
        svc = _svc(session)
    nxt = svc.next_boundary(routine, now=now)
    assert nxt > now
    assert (nxt - now) < timedelta(hours=25)

async def test_active_plan_stays_visible(logged_in: TestEnv) -> None:
    """计划被推进成 active 后**不能从待办里消失**。

    真实缺陷：`upcoming()` 只查 `planned`，于是活动一开始，
    计划就从上下文里消失——角色明明在做，却说"我没在读什么"。
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        plan = await _svc(session).propose_plan(
            kind="reading", title="读那本老书", start_at=now, end_at=now + timedelta(minutes=30)
        )
    async with session_scope() as session:
        await _svc(session).set_state(plan.plan_id, "active")
    async with session_scope() as session:
        upcoming = await _svc(session).upcoming()
    assert any(p.plan_id == plan.plan_id for p in upcoming), "active 的计划消失了"
    active = next(p for p in upcoming if p.plan_id == plan.plan_id)
    assert "正在" in active.describe()
