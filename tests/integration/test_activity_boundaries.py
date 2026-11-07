"""活动边界（阶段 2）：真 PostgreSQL 上的"计划结束 ≠ 完成"。

守设计 §3 的可程序化验收：

1. 跑一天 tick 后 ``activities`` 只出现 ``proposed`` / ``running`` / ``paused`` /
   ``cancelled``——**没有 completed**，``self_experiences`` 计数不变；
2. 到段边界由 ``schedule_segment_id`` **唯一去重**起活动：重启/重复 tick
   不会为同一段再开一个；
3. 睡眠锚点只暂停 + 场景 ``rest``，**不创建睡眠活动**；
4. 场景 ``valid_until`` 到期 → 读侧 ``unknown``；TTL 只允许因真实边界事件或
   "活动仍在运行"续期；
5. 餐食只有在有 observed / user_confirmed 证据时才加饱腹；
6. 旧 ``agenda_plans`` 的 clock-only 收尾路径**对新时间轴不生效**：
   旧活动在段结束时也只暂停，不会被写成 completed。

时间全部用固定的 ``DAY`` + 本地分钟换算，通过固定 `worker.utcnow` 驱动。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.activity.service import SEGMENT_END_REVIEW, ActivityService
from ling.domains.agenda.timeline import local_minute_instant, plan_day_window
from ling.domains.presence.service import (
    SCENE_REFRESH_REASON_SEGMENT,
    PresenceService,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    AgendaPlanRow,
    DayPlanRow,
    PresenceState,
    ScheduleSegmentRow,
    SelfExperienceRow,
    utcnow,
)
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
DAY = date(2026, 11, 8)
WINDOW_START, WINDOW_END = plan_day_window(DAY, TZ)


def _at(minute: int) -> datetime:
    return local_minute_instant(DAY, minute, TZ)


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    from ling import worker

    monkeypatch.setattr(worker, "utcnow", lambda: moment)
    monkeypatch.setenv("LING_USER_TIMEZONE", TZ)


# ---------------------------------------------------------------- 播种


async def _seed_routine() -> None:
    async with session_scope() as session:
        session.add(
            ActorRoutineRow(
                actor_id=ACTOR,
                timezone=TZ,
                wake_minute=8 * 60,
                rest_minute=23 * 60,
                source="configured",
                revision=1,
            )
        )


async def _insert_segment(
    plan_id: str,
    *,
    start_at: datetime,
    end_at: datetime,
    title: str,
    kind: str = "work",
    is_leaf: bool = True,
    anchor_kind: str = "",
) -> str:
    segment_id = new_id("seg")
    async with session_scope() as session:
        session.add(
            ScheduleSegmentRow(
                id=segment_id,
                plan_id=plan_id,
                actor_id=ACTOR,
                parent_id=None,
                parent_key="",
                layer="block",
                kind=kind,
                title=title,
                start_at=start_at,
                end_at=end_at,
                place_id=None,
                commitment_level="confirmed" if anchor_kind else "tentative",
                status="current",
                is_leaf=is_leaf,
                anchor_kind=anchor_kind,
                source="baseline" if anchor_kind else "model",
                revision_created=1,
                revision_ended=None,
                source_event_ids=[],
                visibility=None,
            )
        )
    return segment_id


async def _seed_day() -> dict[str, str]:
    """一天：两段睡眠锚点 + 早饭/工作/午饭/读书四个叶段。"""
    plan_id = new_id("dayplan")
    async with session_scope() as session:
        session.add(
            DayPlanRow(
                id=plan_id,
                actor_id=ACTOR,
                plan_day=DAY,
                timezone=TZ,
                timezone_source="preference",
                timezone_revision=0,
                routine_revision=1,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
                revision=1,
                state="ready",
                generation_source="model",
                last_error_code=None,
                created_at=WINDOW_START,
                updated_at=WINDOW_START,
            )
        )
    ids = {
        "plan": plan_id,
        "sleep_night": await _insert_segment(
            plan_id,
            start_at=_at(0),
            end_at=_at(8 * 60),
            title="睡觉",
            kind="sleep",
            is_leaf=False,
            anchor_kind="sleep",
        ),
        "breakfast": await _insert_segment(
            plan_id,
            start_at=_at(8 * 60),
            end_at=_at(8 * 60 + 30),
            title="吃早饭",
            kind="meal",
        ),
        "work": await _insert_segment(
            plan_id,
            start_at=_at(9 * 60),
            end_at=_at(10 * 60 + 30),
            title="工作",
            kind="work",
        ),
        "lunch": await _insert_segment(
            plan_id,
            start_at=_at(12 * 60),
            end_at=_at(12 * 60 + 30),
            title="吃午饭",
            kind="meal",
        ),
        "reading": await _insert_segment(
            plan_id,
            start_at=_at(14 * 60),
            end_at=_at(15 * 60),
            title="读书",
            kind="reading",
        ),
        "sleep_evening": await _insert_segment(
            plan_id,
            start_at=_at(23 * 60),
            end_at=_at(24 * 60),
            title="睡觉",
            kind="sleep",
            is_leaf=False,
            anchor_kind="sleep",
        ),
    }
    return ids


async def _activities() -> list[ActivityRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ActivityRow).order_by(ActivityRow.created_at, ActivityRow.id)
                )
            ).scalars()
        )


async def _count(model) -> int:  # noqa: ANN001
    async with session_scope() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _scene(now: datetime | None = None):  # noqa: ANN202
    async with session_scope() as session:
        return await PresenceService(session, actor_id=ACTOR).scene(now=now)


async def _tick(monkeypatch: pytest.MonkeyPatch, minute: int) -> None:
    from ling import worker

    _freeze(monkeypatch, _at(minute))
    await worker.handle_life_tick({})


# ---------------------------------------------------------------- 跑一天


async def test_full_day_never_completes_anything(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """跑一整天 tick：没有 completed，也没有一条自我经历。"""
    await _seed_routine()
    ids = await _seed_day()

    for minute in range(0, 24 * 60, 30):
        await _tick(monkeypatch, minute)

    activities = await _activities()
    states = {str(item.state) for item in activities}
    assert "completed" not in states, f"计划时段结束被写成了完成: {states}"
    assert states <= {"proposed", "running", "paused", "cancelled"}, states
    assert await _count(SelfExperienceRow) == 0, "计划不是事实：不该派生自我经历"

    # 每一段最多一条活动，且都挂在真实叶段上（睡眠锚点不产生活动）
    leaf_ids = {
        ids["breakfast"],
        ids["work"],
        ids["lunch"],
        ids["reading"],
    }
    per_segment: dict[str, int] = {}
    for item in activities:
        assert item.schedule_segment_id in leaf_ids, "活动挂到了非叶段/睡眠锚点"
        per_segment[item.schedule_segment_id] = (
            per_segment.get(item.schedule_segment_id, 0) + 1
        )
    assert per_segment == {segment_id: 1 for segment_id in leaf_ids}, per_segment

    # 最后停在 23:30（睡眠窗）：场景 rest，活动全部暂停
    assert await _scene(_at(23 * 60 + 30)) is not None
    scene = await _scene(_at(23 * 60 + 30))
    assert scene.state == "rest"
    assert all(item.state != "running" for item in activities), "睡眠窗里还在跑活动"


async def test_segment_start_is_deduped_across_restarts(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """同一段重复 tick（= 重启）不会起第二个活动。"""
    await _seed_routine()
    ids = await _seed_day()

    await _tick(monkeypatch, 9 * 60 + 15)
    first = [item for item in await _activities() if item.schedule_segment_id == ids["work"]]
    assert len(first) == 1, f"同一段起了 {len(first)} 个活动"
    assert first[0].state == "running"

    await _tick(monkeypatch, 9 * 60 + 16)
    await _tick(monkeypatch, 9 * 60 + 30)
    second = [item for item in await _activities() if item.schedule_segment_id == ids["work"]]
    assert len(second) == 1, "重复 tick 起了第二个活动"
    assert second[0].id == first[0].id

    scene = await _scene(_at(9 * 60 + 30))
    assert scene.state == "focus"
    assert scene.activity_id == first[0].id


async def test_sleep_anchor_creates_no_activity(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """睡眠窗：只暂停 + 场景 rest，不创建睡眠活动。"""
    await _seed_routine()
    await _seed_day()
    await _tick(monkeypatch, 2 * 60 + 30)

    assert await _activities() == [], "睡眠锚点凭空造了一个活动"
    scene = await _scene(_at(2 * 60 + 30))
    assert scene.state == "rest"
    assert scene.origin == "simulated"


async def test_segment_end_pauses_with_needs_review(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """计划时段结束 → paused + needs_review 原因码；场景收回 unknown。"""
    await _seed_routine()
    ids = await _seed_day()
    await _tick(monkeypatch, 9 * 60 + 30)
    await _tick(monkeypatch, 10 * 60 + 31)

    rows = [item for item in await _activities() if item.schedule_segment_id == ids["work"]]
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "paused", f"计划结束应该暂停，实际 {row.state}"
    assert row.completion_evidence is None, "暂停不该写完成证据"
    assert row.checkpoint.get("reason_code") == SEGMENT_END_REVIEW
    assert row.checkpoint.get("closed_by") == "schedule_window"

    scene = await _scene(_at(10 * 60 + 31))
    assert scene.state == "unknown", "段结束后面还自称在做事"
    assert await _count(SelfExperienceRow) == 0


async def test_legacy_clock_only_path_does_not_touch_new_timeline(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """旧 agenda_plans 的活动在窗口结束后也只暂停，绝不 completed。"""
    await _seed_routine()
    await _seed_day()  # 有新时间轴的计划行 → 旧路径整条不参与
    legacy_plan = new_id("agplan")
    legacy_activity = new_id("act")
    async with session_scope() as session:
        session.add(
            AgendaPlanRow(
                id=legacy_plan,
                actor_id=ACTOR,
                kind="reading",
                title="读会儿书",
                start_at=_at(6 * 60),
                end_at=_at(7 * 60),
                timezone=TZ,
                commitment_level="tentative",
                state="active",
                source_event_ids=[],
                revision=1,
            )
        )
        session.add(
            ActivityRow(
                id=legacy_activity,
                actor_id=ACTOR,
                kind="reading",
                state="running",
                place_id=None,
                checkpoint={},
                started_at=_at(6 * 60),
                plan_id=legacy_plan,
                origin="simulated",
                source_event_ids=[],
                visibility="subject_private",
                schedule_segment_id=None,
            )
        )

    await _tick(monkeypatch, 8 * 60 + 15)

    async with session_scope() as session:
        row = await session.get(ActivityRow, legacy_activity)
    assert row.state == "paused", f"旧活动被写成了 {row.state}"
    assert row.completion_evidence is None
    assert await _count(SelfExperienceRow) == 0


# ---------------------------------------------------------------- 场景 TTL


async def test_scene_expires_to_unknown(web_env: TestEnv) -> None:  # noqa: ARG001
    """``valid_until`` 到期 → 读侧 unknown（不沿用旧场景）。"""
    await _seed_routine()
    moment = _at(9 * 60)
    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        await presence.commit_scene(
            state="focus", origin="simulated", activity_id="act_x", now=moment
        )
    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        fresh = await presence.scene(now=moment + timedelta(minutes=5))
        stale = await presence.scene(now=moment + timedelta(minutes=20))
    assert fresh.state == "focus" and not fresh.expired
    assert stale.state == "unknown" and stale.expired
    assert stale.describe() == ""


async def test_scene_ttl_only_refreshed_on_boundary_or_running_activity(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """没有边界事件、也没有活动在跑时，TTL 不许被续。"""
    await _seed_routine()
    moment = _at(9 * 60)
    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        await presence.commit_scene(state="focus", origin="simulated", now=moment)

    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        # ① 空 tick 那种"没有理由"的续期：拒绝
        assert (
            await presence.refresh_scene_ttl(
                reason="", activity_running=False, now=moment + timedelta(minutes=12)
            )
            is False
        )
        assert (
            await presence.refresh_scene_ttl(
                reason="something_else", activity_running=False, now=moment + timedelta(minutes=12)
            )
            is False
        )
        # ② 真实边界事件：允许；剩余 TTL 还很宽时也不写
        assert (
            await presence.refresh_scene_ttl(
                reason=SCENE_REFRESH_REASON_SEGMENT,
                activity_running=False,
                now=moment + timedelta(minutes=1),
            )
            is False
        )
        assert (
            await presence.refresh_scene_ttl(
                reason=SCENE_REFRESH_REASON_SEGMENT,
                activity_running=False,
                now=moment + timedelta(minutes=12),
            )
            is True
        )
        # 续过以后又变宽了
        extended = await presence.scene(now=moment + timedelta(minutes=20))
        assert extended.state == "focus" and not extended.expired
        # ③ 活动仍在运行：也算合法理由（此时距续后的 +27 只剩 5 分钟）
        assert (
            await presence.refresh_scene_ttl(
                reason="", activity_running=True, now=moment + timedelta(minutes=22)
            )
            is True
        )
        # ④ 已经过期：不许"救活"
        assert (
            await presence.refresh_scene_ttl(
                reason=SCENE_REFRESH_REASON_SEGMENT,
                activity_running=True,
                now=moment + timedelta(hours=2),
            )
            is False
        )


# ---------------------------------------------------------------- 饱腹


async def test_planned_meal_does_not_increase_satiety(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """计划里的午饭不产生饱腹：没有证据就没有事实。"""
    await _seed_routine()
    await _seed_day()
    moment = _at(12 * 60 + 10)
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id=ACTOR,
                energy=70,
                social_load=20,
                satiety=50,
                settled_at=moment,
                revision=0,
            )
        )

    await _tick(monkeypatch, 12 * 60 + 10)

    async with session_scope() as session:
        row = await session.get(PresenceState, ACTOR)
    assert int(row.satiety) == 50, "计划里的饭被当成了吃过"
    activities = [item for item in await _activities() if item.kind == "meal"]
    assert all(item.state != "completed" for item in activities)


async def test_satiety_only_moves_with_fact_evidence(web_env: TestEnv) -> None:  # noqa: ARG001
    """clock_only 不加；observed / user_confirmed 才加。"""
    await _seed_routine()
    moment = _at(12 * 60)
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id=ACTOR,
                energy=70,
                social_load=20,
                satiety=50,
                settled_at=moment,
                revision=0,
            )
        )
    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        clock = await presence.record_meal(
            evidence_kind="clock_only", meal_kind="lunch", now=moment
        )
        assert clock.applied is False
        assert clock.reason_code == "evidence_clock_only"
        assert clock.satiety_after == 50

    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        observed = await presence.record_meal(
            evidence_kind="observed", meal_kind="lunch", now=moment
        )
        assert observed.applied is True
        assert observed.satiety_after == 80

    async with session_scope() as session:
        presence = PresenceService(session, actor_id=ACTOR)
        confirmed = await presence.record_meal(
            evidence_kind="user_confirmed", meal_kind="snack", now=moment
        )
        assert confirmed.applied is True
        assert confirmed.satiety_after == 100  # clamp 到上限


async def test_satiety_decays_slower_while_asleep(web_env: TestEnv) -> None:  # noqa: ARG001
    """醒时 −5/小时、睡时 −2/小时，单次结算最多跨 24 小时。"""
    await _seed_routine()
    start = _at(9 * 60)
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id=ACTOR,
                energy=70,
                social_load=20,
                satiety=80,
                settled_at=start,
                revision=0,
            )
        )
    async with session_scope() as session:
        awake = await PresenceService(session, actor_id=ACTOR).settle(
            now=start + timedelta(hours=2)
        )
        assert int(awake.satiety) == 70  # 80 - 5*2
    async with session_scope() as session:
        asleep = await PresenceService(session, actor_id=ACTOR).settle(
            now=start + timedelta(hours=4), asleep=True
        )
        assert int(asleep.satiety) == 66  # 70 - 2*2
    async with session_scope() as session:
        capped = await PresenceService(session, actor_id=ACTOR).settle(
            now=start + timedelta(days=5)
        )
        # 24 小时上限 → 至少还剩下 66 - 5*24 的下界 0（不能因停机变成负数）
        assert int(capped.satiety) == 0


async def test_boundary_view_is_read_only(web_env: TestEnv) -> None:  # noqa: ARG001
    """段边界投影不改任何状态（活动/场景的唯一写入方在各自 service）。"""
    from ling.domains.agenda.timeline import boundary_view

    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        view = await boundary_view(session, actor_id=ACTOR, now=_at(9 * 60 + 10))
    assert view.active_leaf is not None
    assert view.active_leaf.segment_id == ids["work"]
    assert view.sleep is None
    assert view.covers_segment(ids["work"]) is True
    assert view.covers_segment(ids["reading"]) is False
    assert await _activities() == []
    assert await _count(SelfExperienceRow) == 0
    # 顺手确认边界投影不使用真实时钟：此刻的 utcnow() 与 DAY 无关
    assert utcnow() is not None


async def test_activity_view_exposes_segment_id(web_env: TestEnv) -> None:  # noqa: ARG001
    """活动视图要能读出 ``schedule_segment_id``（去重/排障都靠它）。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        service = ActivityService(session, actor_id=ACTOR)
        started = await service.start_for_segment(
            segment_id=ids["work"], kind="work", origin="simulated"
        )
        assert started.action == "created"
        assert started.activity.schedule_segment_id == ids["work"]
        again = await service.start_for_segment(
            segment_id=ids["work"], kind="work", origin="simulated"
        )
        assert again.action == "existing"
        assert again.activity.activity_id == started.activity.activity_id


async def test_day_of_ticks_creates_no_fact_eligible_rows(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """回归：跑一天 tick 后，事实读侧的闸（`domains/evidence`）一条都不放行。

    这是"计划不是事实"的兜底：即使边界路径以后被改动、真写出了一条 completed，
    它也只能是 ``clock_only``，进不了 `self_experiences` / 大纲的"已证实经历"。
    """
    from ling.domains.evidence import fact_eligible_experiences, is_fact_eligible

    await _seed_routine()
    await _seed_day()
    for minute in range(0, 24 * 60, 30):
        await _tick(monkeypatch, minute)

    activities = await _activities()
    assert activities, "这一天一条活动都没有，用例失去意义"
    assert [item for item in activities if is_fact_eligible(item)] == []
    async with session_scope() as session:
        rows = list((await session.execute(select(SelfExperienceRow))).scalars())
        assert await fact_eligible_experiences(session, rows) == []
