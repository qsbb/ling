"""上游（日程 / 场景）不可用时，两个消费点回退到自身默认参数。

用户原话：「当看手机时间什么的这种依赖日程什么的上游的，当上游依赖不可用时
回退到自己的默认参数」。设计见 `docs/UX-FIXES-2026-11-09.md` §4。

这里用**真 PostgreSQL** 覆盖四件事：

1. 日程 `pending` / `failed` / `empty` / `unavailable` → 看手机节奏用本域默认，
   且响应里 `fallback.used=true` 带人话理由；
2. 日程 `ready` → 用上游参数，`fallback.used=false`；
3. 场景过期 / 未知 → 回退到 `unknown`（不冒充"她还在做 X"）；
4. **负向**：回退路径不新增 `activities` / `self_experiences`，不改
   `day_plans` / `scene_commits`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.phone import DEFAULT_IDLE_MINUTES, PhoneService, schedule_dependency
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    DayPlanRow,
    SceneCommitRow,
    SelfExperienceRow,
)
from tests.integration.conftest import TestEnv, logged_in, web_env  # noqa: F401

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"


def _today() -> datetime:
    return datetime.now(UTC)


async def _seed_plan(*, state: str, error_code: str | None = None) -> str:
    """建/改今天的 `day_plans` 行（plan_day 按角色时区的计划日算）。"""
    from ling.domains.agenda.timeline import plan_day_for, plan_day_window

    moment = _today()
    plan_day = plan_day_for(moment, TZ)
    start, end = plan_day_window(plan_day, TZ)
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DayPlanRow).where(
                    DayPlanRow.actor_id == ACTOR, DayPlanRow.plan_day == plan_day
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = DayPlanRow(
                id=f"plan_qa_{plan_day.isoformat()}",
                actor_id=ACTOR,
                plan_day=plan_day,
                timezone=TZ,
                window_start_at=start,
                window_end_at=end,
                state=state,
                generation_source="rule",
                last_error_code=error_code,
            )
            session.add(row)
        else:
            row.state = state
            row.last_error_code = error_code
        await session.commit()
        return str(row.id)


async def _clear_plans() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(delete(DayPlanRow).where(DayPlanRow.actor_id == ACTOR))
        await session.commit()


async def _dashboard(env: TestEnv) -> dict:
    response = await env.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def _phone_section(body: dict) -> dict:
    return body["sections"]["phone"]


async def _counts() -> dict[str, int]:
    async with session_scope() as session:
        activities = (
            await session.execute(
                select(func.count()).select_from(ActivityRow).where(
                    ActivityRow.actor_id == ACTOR
                )
            )
        ).scalar_one()
        experiences = (
            await session.execute(
                select(func.count()).select_from(SelfExperienceRow).where(
                    SelfExperienceRow.actor_id == ACTOR
                )
            )
        ).scalar_one()
    return {"activities": int(activities), "experiences": int(experiences)}


# ---------------------------------------------------------------- 1. 不可用 → 默认


@pytest.mark.parametrize(
    ("state", "error_code"),
    [
        ("pending", None),  # 还没到 21:00，大纲没生成
        ("pending", "outline_rejected"),  # 生成过但失败
        # 规则兜底但此刻**没有段**可读：状态可用、值缺失 → 仍然回退
        ("degraded", "model_unavailable"),
    ],
)
async def test_phone_uses_domain_default_when_schedule_not_usable(
    logged_in: TestEnv,  # noqa: F811
    state: str,
    error_code: str | None,
) -> None:
    """日程没生成 / 生成失败 / 只是规则兜底 → 看手机节奏用自己的默认值。"""
    await _seed_plan(state=state, error_code=error_code)
    body = await _dashboard(logged_in)

    section = _phone_section(body)
    assert section["status"] in {"ok", "empty"}, section
    fallback = section["data"]["fallback"]
    assert fallback["used"] is True, fallback
    assert fallback["source"] == "domain_default", fallback
    assert fallback["upstream"] == "schedule", fallback
    assert fallback["reason"], "回退必须带人话理由"
    assert section["data"]["next_look_interval_minutes"] == DEFAULT_IDLE_MINUTES


async def test_phone_falls_back_when_there_is_no_plan_row(logged_in: TestEnv) -> None:  # noqa: F811
    """今天根本没有计划行（empty）也是**正常状态**，不是故障：照样回退。"""
    await _clear_plans()
    body = await _dashboard(logged_in)
    fallback = _phone_section(body)["data"]["fallback"]
    assert fallback["used"] is True
    assert fallback["state"] == "empty"
    assert "还没有日程" in fallback["reason"]


async def test_phone_falls_back_when_schedule_read_times_out(
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游读超时（有界预算）→ unavailable 回退，整页仍然可用。

    这里把预算调到 1 ms 并让读取睡 50 ms，走的是**真实**的 `read_within_budget`
    超时分支（不是手搓一个 DependencyValue）。
    """
    from ling.api.routes import dashboard as dashboard_module

    real = dashboard_module.load_actor_day

    async def slow(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        # 真实读取但慢 50 ms；1 ms 预算下 phone 那条必然超时，
        # 而 today_outline（不走预算）仍然能正常出结果——两边互不影响。
        await asyncio.sleep(0.05)
        return await real(*args, **kwargs)

    monkeypatch.setattr(dashboard_module, "load_actor_day", slow)
    monkeypatch.setattr(dashboard_module, "SCHEDULE_READ_BUDGET_SECONDS", 0.001)

    body = await _dashboard(logged_in)
    section = _phone_section(body)
    assert section["status"] in {"ok", "empty"}, section
    fallback = section["data"]["fallback"]
    assert fallback["used"] is True, fallback
    assert fallback["state"] == "unavailable", fallback
    # 响应里给的是**人话**（"读不到"），预算细节留在日志/单测里——
    # 用户不需要知道 300 ms，排查的人需要（见 test_read_within_budget_*）。
    assert "读不到" in (fallback["reason"] or ""), fallback
    assert section["data"]["next_look_interval_minutes"] == DEFAULT_IDLE_MINUTES


async def test_applying_the_interval_logs_one_human_line(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真正**采用**这个节奏时（写 next_look_at），回退要留一条人话行为日志。"""
    from ling.domains.fallback import DependencyState, DependencyValue

    async with session_scope() as session:
        svc = PhoneService(session, actor_id=ACTOR)
        with caplog.at_level("INFO"):
            view, interval = await svc.schedule_next_from_upstream(
                DependencyValue(state=DependencyState.pending, upstream="schedule"),
                now=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
            )

    assert interval.used_default is True
    assert interval.minutes == DEFAULT_IDLE_MINUTES
    assert view.next_look_at == datetime(2026, 11, 9, 10, 10, tzinfo=UTC)
    lines = [r.getMessage() for r in caplog.records if r.name == "ling.behavior"]
    assert any("这次用了默认参数" in line and "日程" in line for line in lines), lines


async def test_applying_upstream_interval_does_not_log_fallback(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """上游可用时不记回退日志（否则日志会被"一切正常"刷满）。"""
    from ling.domains.fallback import DependencyState, DependencyValue
    from ling.domains.phone.service import ScheduleNow

    async with session_scope() as session:
        svc = PhoneService(session, actor_id=ACTOR)
        with caplog.at_level("INFO"):
            view, interval = await svc.schedule_next_from_upstream(
                DependencyValue(
                    state=DependencyState.ready,
                    upstream="schedule",
                    value=ScheduleNow(kind="focus", title="写代码", ends_at=None),
                ),
                now=datetime(2026, 11, 9, 11, 0, tzinfo=UTC),
            )

    assert interval.used_default is False
    assert interval.minutes == 45
    assert view.next_look_at == datetime(2026, 11, 9, 11, 45, tzinfo=UTC)
    assert not [r for r in caplog.records if r.name == "ling.behavior"]


async def test_phone_uses_degraded_plan_as_reference(logged_in: TestEnv) -> None:  # noqa: F811
    """规则兜底的日程可以调**行为参数**（`source=upstream`），但状态如实写 degraded。

    这是一条边界：`degraded` 能当"计划参考"，不能当"她正在做 X"的事实。
    所以这里允许它影响看手机节奏，同时在标记里保留 `state=degraded`。
    """
    await _seed_ready_segment(kind="work", title="上班")
    # `_seed_plan` 已建了今天的行；这里只把状态改成规则兜底，段不动
    async with session_scope() as session:
        plan = (
            await session.execute(
                select(DayPlanRow).where(DayPlanRow.actor_id == ACTOR)
            )
        ).scalars().first()
        plan.state = "degraded"
        plan.last_error_code = "model_unavailable"
        await session.commit()

    body = await _dashboard(logged_in)
    data = _phone_section(body)["data"]
    assert data["fallback"]["used"] is False, data["fallback"]
    assert data["fallback"]["state"] == "degraded"
    assert data["fallback"]["source"] == "upstream"
    assert data["next_look_interval_minutes"] == 45


# ---------------------------------------------------------------- 2. 可用 → 用上游


async def _seed_ready_segment(*, kind: str, title: str) -> None:
    """建一条覆盖"此刻"的 ready 段（叶段，`current`）。"""
    from ling.domains.agenda.timeline import plan_day_for, plan_day_window
    from ling.infra.models import ScheduleSegmentRow

    await _seed_plan(state="ready")
    moment = _today()
    plan_day = plan_day_for(moment, TZ)
    start, end = plan_day_window(plan_day, TZ)
    async with session_scope() as session:
        plan = (
            await session.execute(
                select(DayPlanRow).where(
                    DayPlanRow.actor_id == ACTOR, DayPlanRow.plan_day == plan_day
                )
            )
        ).scalar_one()
        existing = (
            await session.execute(
                select(ScheduleSegmentRow).where(ScheduleSegmentRow.id == "seg_qa_now")
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ScheduleSegmentRow(
                    id="seg_qa_now",
                    plan_id=str(plan.id),
                    actor_id=ACTOR,
                    parent_id=None,
                    parent_key="",
                    layer="block",
                    kind=kind,
                    title=title,
                    start_at=moment - timedelta(minutes=5),
                    end_at=moment + timedelta(minutes=55),
                    commitment_level="tentative",
                    status="current",
                    is_leaf=True,
                    revision_created=int(plan.revision or 0),
                    source="rule",
                )
            )
        else:
            existing.kind = kind
            existing.title = title
            existing.start_at = moment - timedelta(minutes=5)
            existing.end_at = moment + timedelta(minutes=55)
        await session.commit()
    assert start != end


async def test_phone_uses_upstream_when_schedule_ready(logged_in: TestEnv) -> None:  # noqa: F811
    """日程 ready 且此刻有段 → 用上游的节奏，`fallback.used=false`。"""
    await _seed_ready_segment(kind="focus", title="写代码")

    body = await _dashboard(logged_in)
    data = _phone_section(body)["data"]
    fallback = data["fallback"]
    assert fallback["used"] is False, fallback
    assert fallback["source"] == "upstream"
    assert fallback["state"] == "ready"
    assert data["next_look_interval_minutes"] == 45, (
        "focus 的节奏应来自上游（45 分钟），而不是默认 10 分钟"
    )


# ---------------------------------------------------------------- 3. 场景过期


async def test_scene_expired_falls_back_to_unknown(logged_in: TestEnv) -> None:  # noqa: F811
    """`valid_until` 过期 → unknown（**不**沿用旧场景），并标出回退。"""
    moment = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(SceneCommitRow, ACTOR)
        if row is None:
            row = SceneCommitRow(actor_id=ACTOR)
            session.add(row)
        row.state = "focus"
        row.origin = "simulated"
        row.committed_at = moment - timedelta(minutes=30)
        row.valid_until = moment - timedelta(minutes=20)
        await session.commit()

    body = await _dashboard(logged_in)
    data = body["sections"]["presence"]["data"]
    assert data["scene"]["state"] == "unknown", data["scene"]
    assert data["scene"]["expired"] is True
    assert data["fallback"]["used"] is True
    assert data["fallback"]["upstream"] == "scene"
    assert "场景" in data["fallback"]["reason"]


async def test_scene_fresh_uses_upstream(logged_in: TestEnv) -> None:  # noqa: F811
    moment = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(SceneCommitRow, ACTOR)
        if row is None:
            row = SceneCommitRow(actor_id=ACTOR)
            session.add(row)
        row.state = "chat"
        row.origin = "simulated"
        row.committed_at = moment
        row.valid_until = moment + timedelta(minutes=10)
        await session.commit()

    data = (await _dashboard(logged_in))["sections"]["presence"]["data"]
    assert data["scene"]["state"] == "chat"
    assert data["fallback"]["used"] is False


# ---------------------------------------------------------------- 4. 负向断言


async def test_fallback_writes_no_facts(logged_in: TestEnv) -> None:  # noqa: F811
    """回退只影响本次参数：**不**新增 activities / self_experiences，**不**改 day_plans。"""
    await _seed_plan(state="pending")  # 不可用的上游
    before = await _counts()
    async with session_scope() as session:
        before_plans = (
            await session.execute(
                select(DayPlanRow.id, DayPlanRow.state, DayPlanRow.revision).where(
                    DayPlanRow.actor_id == ACTOR
                )
            )
        ).all()

    body = await _dashboard(logged_in)
    assert _phone_section(body)["data"]["fallback"]["used"] is True

    after = await _counts()
    async with session_scope() as session:
        after_plans = (
            await session.execute(
                select(DayPlanRow.id, DayPlanRow.state, DayPlanRow.revision).where(
                    DayPlanRow.actor_id == ACTOR
                )
            )
        ).all()

    assert after == before, f"回退路径写了事实：{before} → {after}"
    assert after_plans == before_plans, "回退路径改了 day_plans"
    assert after["activities"] == before["activities"]
    assert after["experiences"] == before["experiences"]


async def test_schedule_dependency_reads_only_current_plan_segments(web_env: TestEnv) -> None:  # noqa: F811
    """纯函数层：只有 ready/degraded 才取段；pending 一律不带值（不拿旧段凑）。"""
    from types import SimpleNamespace

    from ling.contracts.v1 import TimelineState

    moment = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    seg = SimpleNamespace(
        kind="work",
        title="上班",
        start_at=moment - timedelta(hours=1),
        end_at=moment + timedelta(hours=1),
    )
    pending = schedule_dependency(
        SimpleNamespace(state=TimelineState.pending, segments=(seg,)), now=moment
    )
    assert pending.state.value == "pending"
    assert pending.value is None, "pending 的段不能拿来当'此刻在做什么'"

    ready = schedule_dependency(
        SimpleNamespace(state=TimelineState.ready, segments=(seg,)), now=moment
    )
    assert ready.value is not None
    assert ready.value.kind == "work"


async def test_phone_interval_default_is_not_old_value(web_env: TestEnv) -> None:  # noqa: F811
    """`interval_from_schedule` 的默认值来自本域常量，**不是**上一次读到的上游。"""
    from ling.domains.fallback import DependencyState, DependencyValue

    async with session_scope() as session:
        svc = PhoneService(session, actor_id=ACTOR)
        # 如果实现偷偷缓存了上一次上游给的值（focus → 45），这里会拿到 45
        interval = svc.interval_from_schedule(
            DependencyValue(state=DependencyState.failed, upstream="schedule")
        )
    assert interval.minutes == DEFAULT_IDLE_MINUTES
    assert interval.used_default is True
