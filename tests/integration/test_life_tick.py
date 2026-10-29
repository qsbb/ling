"""生活推进器（`life.tick`）：只在边界或到点计划时动手。

要验证的三件事：
1. 醒着 + 有到点计划 → 开活动并提交场景
2. 该歇了 → 模拟活动暂停（**不是完成**，没做完就不算做完）
3. 醒着但没有计划 → 场景收回到 unknown，**不硬编**"我在看书"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.activity.service import ActivityService
from ling.domains.agenda.service import AgendaService
from ling.domains.presence.service import PresenceService
from ling.infra.db import session_scope
from ling.worker import handle_life_tick
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _routine(env: TestEnv, *, wake: int, rest: int) -> None:
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).set_routine(
            timezone="Asia/Shanghai", wake_minute=wake, rest_minute=rest
        )


async def _scene(env: TestEnv):  # noqa: ANN202
    async with session_scope() as session:
        return await PresenceService(session, actor_id=ACTOR).scene()


async def _current_activity(env: TestEnv):  # noqa: ANN202
    async with session_scope() as session:
        return await ActivityService(session, actor_id=ACTOR).current()


async def test_awake_with_due_plan_starts_activity(logged_in: TestEnv) -> None:
    """醒着 + 计划到点 → 开活动、场景切 focus。"""
    await _routine(logged_in, wake=0, rest=1439)  # 全天醒着
    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading",
            title="读会儿书",
            start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(minutes=30),
        )

    await handle_life_tick({})

    activity = await _current_activity(logged_in)
    assert activity is not None, "计划到点却没有开始任何活动"
    assert activity.state == "running"
    scene = await _scene(logged_in)
    assert scene.state == "focus"
    assert scene.origin == "simulated", "模拟生活必须如实标为模拟"


async def test_resting_pauses_not_completes(logged_in: TestEnv) -> None:
    """该歇了：暂停，**不能标完成**。

    标完成等于凭空造出"我做完了"的经历。
    """
    await _routine(logged_in, wake=0, rest=1439)
    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading", title="读书", start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(minutes=30),
        )
    await handle_life_tick({})
    assert (await _current_activity(logged_in)) is not None

    # 切成"现在该睡了"（醒窗只覆盖 UTC 当前时刻之外）
    local = datetime.now(UTC).astimezone()
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).set_routine(
            timezone="Asia/Shanghai",
            wake_minute=0,
            rest_minute=1,  # 只有一个极小窗口醒着
        )
    _ = local
    await handle_life_tick({})

    activity = await _current_activity(logged_in)
    assert activity is not None
    assert activity.state != "completed", "休息时把活动标成了已完成（凭空造经历）"


async def test_no_plan_does_not_fabricate_scene(logged_in: TestEnv) -> None:
    """醒着但没有计划：场景回到 unknown，不硬编一个"我在看书"。"""
    await _routine(logged_in, wake=0, rest=1439)
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(state="focus")

    await handle_life_tick({})

    scene = await _scene(logged_in)
    assert scene.state == "unknown", f"没有依据却保留了场景: {scene.state}"
    assert scene.describe() == ""


async def test_tick_is_idempotent(logged_in: TestEnv) -> None:
    """连续推进两次不会开出两个活动。"""
    await _routine(logged_in, wake=0, rest=1439)
    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading", title="读书", start_at=now - timedelta(minutes=1),
            end_at=now + timedelta(minutes=30),
        )
    await handle_life_tick({})
    first = await _current_activity(logged_in)
    await handle_life_tick({})
    second = await _current_activity(logged_in)
    assert first is not None and second is not None
    assert first.activity_id == second.activity_id, "重复推进开出了第二个活动"


async def test_tick_never_fails_on_empty_db(logged_in: TestEnv) -> None:
    """空库上跑不应报错（真实场景：刚部署还没配作息）。"""
    await handle_life_tick({})
    await handle_life_tick({})

async def test_completion_evidence_uses_readable_label(logged_in: TestEnv) -> None:
    """完成证据里不能出现 `reading` 这种内部槽位名。

    它会一路漏进回复（实测出现过「模拟活动「reading」的计划时段已结束」），
    立刻显得机械。
    """
    from ling.worker import _activity_label

    assert _activity_label("reading") == "读书"
    assert _activity_label("unknown_kind") == "手头的事"
    assert "reading" not in _activity_label("reading")
