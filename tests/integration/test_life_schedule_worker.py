"""生活调度与 outbox 事务边界（真 PostgreSQL）。

这一组守四件事，全部要求**真实数据库**（SQLite 的锁行为与 PG 不同，
"不持锁等模型"那条在 SQLite 上根本测不出来）：

1. `life.schedule` 只查 DB 到期边界：21:00 前不投递明天的 outline，
   21:00 后投递一次，重复扫描不重复投递；
2. 崩溃恢复：租约过期的 in-flight 事件会被重新领取；凌晨的 catch-up、
   醒点后的 late 都会被投递，且 **late 相位零模型调用**；
3. 梦与跨夜结算：窗口内一晚只生成一份，结算在醒后补做且幂等；
4. outbox 的认领事务**不持锁等 handler**：一条 handler 睡 3 秒时，
   另一个连接能同时领走另一条事件。

另外验证旧 `life.plan` 对新时间轴已接管的 actor 不再自动生成，而旧
`agenda_plans` 的手工小事仍然可用。

时间一律用固定值（`LOCAL_DAY` + 本地分钟 → UTC），不依赖"现在几点"。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.service import AgendaService
from ling.domains.agenda.timeline import (
    REASON_DEGRADED_LATE,
    local_minute_instant,
    plan_day_window,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActorRoutineRow,
    AgendaPlanRow,
    DayPlanRow,
    DreamReportRow,
    OutboxEvent,
    SleepSettlementRow,
)
from ling.infra.security import new_id
from ling.models.router import StreamOutcome
from ling.ports.config import (
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
)
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
WAKE_MINUTE = 8 * 60
REST_MINUTE = 23 * 60
LOCAL_DAY = date(2026, 11, 8)
TOMORROW = LOCAL_DAY + timedelta(days=1)
YESTERDAY = LOCAL_DAY - timedelta(days=1)


def _at(day: date, minute: int) -> datetime:
    """本地墙钟 day+minute 的 UTC 瞬时。"""
    return local_minute_instant(day, minute, TZ)


async def _seed_routine(revision: int = 1) -> None:
    async with session_scope() as session:
        session.add(
            ActorRoutineRow(
                actor_id=ACTOR,
                timezone=TZ,
                wake_minute=WAKE_MINUTE,
                rest_minute=REST_MINUTE,
                source="configured",
                revision=revision,
            )
        )


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """把 worker 的 `utcnow` 固定住，并钉住规范时区（不读开发机 shell）。"""
    from ling import worker

    monkeypatch.setattr(worker, "utcnow", lambda: moment)
    monkeypatch.setenv("LING_USER_TIMEZONE", TZ)


async def _outbox(topic: str | None = None) -> list[OutboxEvent]:
    async with session_scope() as session:
        stmt = select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
        if topic is not None:
            stmt = stmt.where(OutboxEvent.topic == topic)
        return list((await session.execute(stmt)).scalars())


async def _count(model, **filters: object) -> int:
    async with session_scope() as session:
        stmt = select(func.count()).select_from(model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        return int((await session.execute(stmt)).scalar_one())


async def _day_plan(plan_day: date) -> DayPlanRow | None:
    async with session_scope() as session:
        return (
            await session.execute(
                select(DayPlanRow).where(
                    DayPlanRow.actor_id == ACTOR, DayPlanRow.plan_day == plan_day
                )
            )
        ).scalar_one_or_none()


async def _insert_day_plan(
    plan_day: date, *, state: str, revision: int, error_code: str | None = None
) -> str:
    """直接落一行计划头（模拟"夜里跑过、但停在某个状态"的现场）。"""
    start, end = plan_day_window(plan_day, TZ)
    plan_id = new_id("dayplan")
    async with session_scope() as session:
        session.add(
            DayPlanRow(
                id=plan_id,
                actor_id=ACTOR,
                plan_day=plan_day,
                timezone=TZ,
                timezone_source="env",
                timezone_revision=0,
                routine_revision=1,
                window_start_at=start,
                window_end_at=end,
                revision=revision,
                state=state,
                generation_source="model" if state == "ready" else "rule",
                last_error_code=error_code,
                created_at=start,
                updated_at=start,
            )
        )
    return plan_id


# ---------------------------------------------------------------- 假模型


class _CountingRouter:
    """记录被调用次数；任何一次调用都会被断言捕获。"""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self.calls = 0

    async def stream(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls += 1
        text = self._text
        yield ModelChunk(delta=text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=text)


class _StubConfig:
    """可用的 `memory.extract` 绑定：证明"零调用"不是"没有模型"。"""

    def __init__(self, config: EffectiveConfig) -> None:
        self._config = config

    async def effective(self) -> EffectiveConfig:
        return self._config


def _usable_config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "fake-main": ProviderConfig(
                provider_id="fake-main",
                kind="fake",
                models=(ProviderModel("plan-lite"),),
            )
        },
        profiles={
            "memory.extract": ProfileBinding(
                profile="memory.extract",
                provider_id="fake-main",
                model_id="plan-lite",
            )
        },
    )


def _valid_outline(day: date) -> str:
    """一段落在清醒时段（09:00–10:00）、不压睡眠锚点的大纲。"""
    return json.dumps(
        {
            "segments": [
                {
                    "kind": "work",
                    "title": "写代码",
                    "start_at": local_minute_instant(day, 9 * 60, TZ).isoformat(),
                    "end_at": local_minute_instant(day, 10 * 60, TZ).isoformat(),
                    "place_id": None,
                    "flexibility": "flexible",
                }
            ]
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- 21:00 到期


async def test_no_outline_delivered_before_2100(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地 20:00：明天的 21:00 还没到，不投递任何大纲/接缝作业。

    梦与结算**不**受 21:00 约束，所以断言只针对调度类 topic；
    20:00 已过醒点、又没有结算行，`sleep.settle` 该有——那是另一条独立的事。
    """
    from ling import worker

    await _seed_routine()
    now = _at(LOCAL_DAY, 20 * 60)
    _freeze(monkeypatch, now)

    await worker.handle_life_schedule({})

    scheduled = await _outbox("schedule.outline")
    seams = await _outbox("schedule.seam")
    assert scheduled == [], "21:00 前就投递了大纲作业"
    assert seams == [], "还没有计划行就投递了接缝校验"
    assert all(
        row.payload.get("plan_day") != TOMORROW.isoformat() for row in scheduled
    )
    # 结算与 21:00 无关：醒后没有结算行就该补（昨天、今天各一条）。
    settle = {row.payload["sleep_date"] for row in await _outbox("sleep.settle")}
    assert settle == {YESTERDAY.isoformat(), LOCAL_DAY.isoformat()}


async def test_outline_delivered_once_after_2100(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地 21:05：为**明天**投递一次 due；重复扫描不重复投递。

    投完以后真的跑一遍 handler，确认它建出 D 的计划行（模型不可用时降级，
    这不是失败——`degraded` 加规则锚点才是设计里的兜底）。
    """
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 21 * 60 + 5))

    # 扫描本身**结构上**不该调模型：给一个可用的绑定 + 计数 router，
    # 扫描后计数必须还是 0。
    router = _CountingRouter(text=_valid_outline(TOMORROW))
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(
        worker, "_config_reader", lambda _session: _StubConfig(_usable_config())
    )

    await worker.handle_life_schedule({})
    await worker.handle_life_schedule({})  # 同一分钟再扫一次
    assert router.calls == 0, f"扫描里调了 {router.calls} 次模型"

    events = await _outbox("schedule.outline")
    assert len(events) == 1, f"重复扫描投了 {len(events)} 条大纲作业"
    payload = events[0].payload
    assert payload["plan_day"] == TOMORROW.isoformat()
    assert payload["phase"] == "due"

    await worker.handle_schedule_outline(payload)

    row = await _day_plan(TOMORROW)
    assert row is not None, "大纲 handler 没有为明天建出计划行"
    assert row.revision >= 1
    # 这次给的是**可用**绑定，所以它真的调了一次模型（证明"扫描里 0 次"不是
    # 因为根本没有模型可用）。
    assert router.calls == 1
    assert row.state == "ready"
    assert row.generation_source == "model"
    assert row.window_start_at == plan_day_window(TOMORROW, TZ)[0]

    # 跑完以后再扫：不该再投（快照没变、行已提交）。
    await worker.handle_life_schedule({})
    assert len(await _outbox("schedule.outline")) == 1


async def test_catchup_is_delivered_after_midnight(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凌晨 02:00、今天还没有计划行：补跑今天（catchup，只排 now 之后）。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 2 * 60))

    await worker.handle_life_schedule({})
    events = await _outbox("schedule.outline")
    assert len(events) == 1
    assert events[0].payload["plan_day"] == LOCAL_DAY.isoformat()
    assert events[0].payload["phase"] == "catchup"

    await worker.handle_schedule_outline(events[0].payload)
    row = await _day_plan(LOCAL_DAY)
    assert row is not None

    # 可恢复失败（模型不可用）在退避窗口内不立刻重试。
    await worker.handle_life_schedule({})
    assert len(await _outbox("schedule.outline")) == 1


async def test_seam_is_delivered_once_for_current_day(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当前本地日已有计划 → 投一次零模型接缝校验，重复扫描不重复投。"""
    from ling import worker

    await _seed_routine()
    await _insert_day_plan(LOCAL_DAY, state="ready", revision=1)
    _freeze(monkeypatch, _at(LOCAL_DAY, 0 * 60 + 30))

    await worker.handle_life_schedule({})
    await worker.handle_life_schedule({})

    events = await _outbox("schedule.seam")
    assert len(events) == 1
    assert events[0].payload["plan_day"] == LOCAL_DAY.isoformat()

    # handler 必须能在**零模型**下跑完（seam_check 没有 router 参数）。
    await worker.handle_schedule_seam(events[0].payload)
    assert len(await _outbox("schedule.outline")) == 0


async def test_late_phase_makes_zero_model_calls(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """醒点后、计划还停在 pending：投 late，**一次模型调用都没有**。

    这里刻意给一个**可用**的 `memory.extract` 绑定和一个会计数的 router：
    如果实现里没有把 late 的模型入口关死，"零调用"就只是"恰好没有模型"。
    """
    from ling import worker

    await _seed_routine()
    plan_id = await _insert_day_plan(LOCAL_DAY, state="pending", revision=0)
    _freeze(monkeypatch, _at(LOCAL_DAY, 10 * 60))

    router = _CountingRouter()
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_config_reader", lambda _session: _StubConfig(_usable_config()))

    await worker.handle_life_schedule({})
    events = await _outbox("schedule.outline")
    assert len(events) == 1
    assert events[0].payload["plan_day"] == LOCAL_DAY.isoformat()
    assert events[0].payload["phase"] == "late"

    await worker.handle_schedule_outline(events[0].payload)

    assert router.calls == 0, f"late 相位调了 {router.calls} 次模型"
    row = await _day_plan(LOCAL_DAY)
    assert row is not None and row.id == plan_id
    assert row.state == "degraded"
    assert row.last_error_code == REASON_DEGRADED_LATE
    assert row.revision == 1

    # 终态原因码不再重投。
    await worker.handle_life_schedule({})
    assert len(await _outbox("schedule.outline")) == 1


async def test_delayed_due_event_downgrades_to_late(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """21:05 投的 `due` 事件如果拖到第二天 10:00 才跑，必须降成零模型 late。

    重启补投、退避重试都会让事件迟到。相位只能**更保守**：拿着一个过期的
    `due` 去调模型，等于醒着还在为早晨烧钱，而且生成的时间轴一提交就过期。
    """
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 21 * 60 + 5))
    await worker.handle_life_schedule({})
    events = await _outbox("schedule.outline")
    assert len(events) == 1
    assert events[0].payload["phase"] == "due"

    # 事件没被消费，worker 停了一夜：第二天上午 10:00 才轮到它。
    _freeze(monkeypatch, _at(TOMORROW, 10 * 60))
    router = _CountingRouter(text=_valid_outline(TOMORROW))
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(
        worker, "_config_reader", lambda _session: _StubConfig(_usable_config())
    )

    await worker.handle_schedule_outline(events[0].payload)

    assert router.calls == 0, "迟到的大纲作业仍然调了模型（相位没有降级）"
    row = await _day_plan(TOMORROW)
    assert row is not None
    assert row.state == "degraded"
    assert row.last_error_code == REASON_DEGRADED_LATE


# ---------------------------------------------------------------- 梦与结算


async def test_dream_generated_once_per_night(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """梦窗口内只投一次；handler 跑两遍也只有一份 `dream_reports`。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 7 * 60 + 30))  # 醒前 30 分钟

    await worker.handle_life_schedule({})
    await worker.handle_life_schedule({})

    events = await _outbox("dream.generate")
    assert len(events) == 1
    assert events[0].payload["sleep_date"] == LOCAL_DAY.isoformat()
    assert events[0].payload["phase"] == "window"

    await worker.handle_dream_generate(events[0].payload)
    await worker.handle_dream_generate(events[0].payload)  # 重复投递

    assert await _count(DreamReportRow, actor_id=ACTOR, sleep_date=LOCAL_DAY) == 1
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DreamReportRow).where(
                    DreamReportRow.actor_id == ACTOR,
                    DreamReportRow.sleep_date == LOCAL_DAY,
                )
            )
        ).scalar_one()
    # 没有可入梦的素材时写 `no_dream`：这不是失败，也不该让模型把空白补成剧情。
    assert row.status in {"no_dream", "failed", "missed"}
    assert row.origin == "simulated"
    assert row.visibility == "actor_private"

    # 已有记录后不再投递。
    await worker.handle_life_schedule({})
    assert len(await _outbox("dream.generate")) == 1


async def test_dream_not_scheduled_outside_window(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """醒前 2 小时还没到 generate_from：不投（省一次注定 too_early 的投递）。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 6 * 60))

    await worker.handle_life_schedule({})
    assert await _outbox("dream.generate") == []


async def test_settlement_backfilled_after_wake(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """醒后补做结算：只一行、重复执行不变（幂等）。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 11 * 60))

    await worker.handle_life_schedule({})
    events = await _outbox("sleep.settle")
    tonight = next(
        row for row in events if row.payload["sleep_date"] == LOCAL_DAY.isoformat()
    )

    await worker.handle_sleep_settle(tonight.payload)
    await worker.handle_sleep_settle(tonight.payload)

    rows = await _count(SleepSettlementRow, actor_id=ACTOR, sleep_date=LOCAL_DAY)
    assert rows == 1

    async with session_scope() as session:
        row = (
            await session.execute(
                select(SleepSettlementRow).where(
                    SleepSettlementRow.actor_id == ACTOR,
                    SleepSettlementRow.sleep_date == LOCAL_DAY,
                )
            )
        ).scalar_one()
    assert row.wake_at == _at(LOCAL_DAY, WAKE_MINUTE)
    assert row.bed_at == _at(YESTERDAY, REST_MINUTE)

    # 已有结算行：不再投递（重复扫描不该反复尝试）。
    await worker.handle_life_schedule({})
    again = {
        row.payload["sleep_date"] for row in await _outbox("sleep.settle")
    }
    assert again == {YESTERDAY.isoformat(), LOCAL_DAY.isoformat()}


async def test_settlement_is_backfilled_for_yesterday_too(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地今天−1 的那一晚也能补结算（worker 停机一整晚的场景）。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 11 * 60))

    await worker.handle_life_schedule({})

    dates = sorted(row.payload["sleep_date"] for row in await _outbox("sleep.settle"))
    assert dates == [YESTERDAY.isoformat(), LOCAL_DAY.isoformat()]


# ---------------------------------------------------------------- 旧路径收口


async def test_life_plan_stops_for_actor_owned_by_new_timeline(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新时间轴接管后，旧 `life.plan` 不再自动生成；手工小事仍可用。"""
    from ling import worker

    await _seed_routine()
    await _insert_day_plan(LOCAL_DAY, state="ready", revision=1)
    _freeze(monkeypatch, _at(LOCAL_DAY, 15 * 60))

    router = _CountingRouter(
        text='[{"title":"翻两页书","start":"16:00","minutes":60}]'
    )
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)

    await worker.handle_life_plan({})

    assert router.calls == 0, "旧 life.plan 在已接管的 actor 上仍然调了模型"
    assert await _count(AgendaPlanRow, actor_id=ACTOR) == 0

    # 旧表仍然可读，手工小事仍然能落库（走 AgendaService，不经过 worker）。
    start = _at(LOCAL_DAY, 16 * 60)
    async with session_scope() as session:
        view = await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="simulated",
            title="手工排的一件小事",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            commitment_level="tentative",
        )
    assert view is not None
    assert await _count(AgendaPlanRow, actor_id=ACTOR) == 1


async def test_life_plan_still_runs_when_new_timeline_absent(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 `day_plans` 行时旧路径照常：迁移期不该把她的日常突然清零。"""
    from ling import worker

    await _seed_routine()
    _freeze(monkeypatch, _at(LOCAL_DAY, 15 * 60))

    router = _CountingRouter(
        text='[{"title":"翻两页书","start":"16:00","minutes":60}]'
    )
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)

    await worker.handle_life_plan({})

    assert router.calls == 1
    assert await _count(AgendaPlanRow, actor_id=ACTOR) == 1


# ---------------------------------------------------------------- outbox 锁


async def test_outbox_claim_does_not_hold_lock_during_handler(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """handler 睡 3 秒时，另一个连接必须能立刻领走另一条事件。

    旧实现把整批 `FOR UPDATE SKIP LOCKED` 包在一个事务里、在里面跑 handler：
    两个连接下，第二个连接会**阻塞到第一个 handler 跑完**（这里约 3 秒）。
    现在认领是短事务、提交后就在事务外跑 handler，所以第二个连接应当毫秒级
    领走另一条。
    """
    from ling import worker

    calls: list[str] = []

    async def _slow(payload: dict) -> None:
        calls.append("slow_start")
        await asyncio.sleep(3.0)
        calls.append("slow_end")

    async def _fast(payload: dict) -> None:
        calls.append("fast")

    monkeypatch.setitem(worker.HANDLERS, "test.slow", _slow)
    monkeypatch.setitem(worker.HANDLERS, "test.fast", _fast)

    base = _at(LOCAL_DAY, 9 * 60)
    _freeze(monkeypatch, base)
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id="obx_slow_for_lock_test",
                topic="test.slow",
                payload={},
                available_at=base,
                created_at=base,
            )
        )
        session.add(
            OutboxEvent(
                id="obx_fast_for_lock_test",
                topic="test.fast",
                payload={},
                available_at=base,
                created_at=base + timedelta(seconds=1),
            )
        )

    first = asyncio.create_task(worker.drain_outbox(limit=2))
    for _ in range(200):
        if "slow_start" in calls:
            break
        await asyncio.sleep(0.01)
    assert "slow_start" in calls, "慢 handler 没有被认领并开始执行"

    # 慢 handler 正在跑：此刻 outbox 行是 in_flight（租约归本实例），
    # 但**行锁已经释放**——另一条事件应当马上能领走。
    async with session_scope() as session:
        running = await session.get(OutboxEvent, "obx_slow_for_lock_test")
    assert running is not None
    assert running.state == "in_flight"
    assert running.processed_by == worker.WORKER_ID
    assert running.available_at > base  # available_at 在 in_flight 时是租约到期

    # 最直接的一条证据：另开一个**真连接**对这条行加排他锁并 NOWAIT。
    # 旧实现（认领事务里 await handler）此时行锁还在，这里会抛
    # `lock_not_available`；现在认领已提交，锁早就放了，所以必须能拿到。
    from sqlalchemy import text as sql_text

    async with session_scope() as session:
        await session.execute(
            sql_text("SELECT id FROM outbox_events WHERE id = :eid FOR UPDATE NOWAIT"),
            {"eid": "obx_slow_for_lock_test"},
        )

    started = time.perf_counter()
    processed = await asyncio.wait_for(worker.drain_outbox(limit=1), timeout=1.5)
    elapsed = time.perf_counter() - started

    assert processed == 1, "第二个连接没有领到另一条事件"
    assert "fast" in calls
    assert elapsed < 1.5, f"第二个连接被慢 handler 挡住了 {elapsed:.2f} 秒"
    assert not first.done(), "第一个 drain 不该已经结束（它还在等慢 handler）"

    assert await asyncio.wait_for(first, timeout=5.0) == 1

    async with session_scope() as session:
        slow_row = await session.get(OutboxEvent, "obx_slow_for_lock_test")
        fast_row = await session.get(OutboxEvent, "obx_fast_for_lock_test")
    assert slow_row is not None and slow_row.state == "done"
    assert fast_row is not None and fast_row.state == "done"


async def test_expired_lease_is_reclaimed_after_restart(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启后：租约过期的 in-flight 事件被重新领取（并正常完成）。"""
    from ling import worker

    calls: list[str] = []

    async def _record(payload: dict) -> None:
        calls.append(str(payload.get("tag")))

    monkeypatch.setitem(worker.HANDLERS, "test.reclaim", _record)

    now = _at(LOCAL_DAY, 9 * 60)
    _freeze(monkeypatch, now)
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id="obx_lease_expired",
                topic="test.reclaim",
                payload={"tag": "reclaimed"},
                state="in_flight",
                attempts=1,
                # 租约早就过了：上一个进程死了，没来得及确认
                available_at=now - timedelta(minutes=1),
                processed_by="dead-worker:9999",
                created_at=now - timedelta(minutes=30),
            )
        )

    processed = await worker.drain_outbox(limit=5)
    assert processed == 1
    assert calls == ["reclaimed"]

    async with session_scope() as session:
        row = await session.get(OutboxEvent, "obx_lease_expired")
    assert row is not None
    assert row.state == "done"
    assert row.processed_by == worker.WORKER_ID


async def test_unexpired_lease_is_not_stolen(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """别人的租约还没到期：不得抢（多副本同时跑同一条会重复写）。"""
    from ling import worker

    calls: list[str] = []

    async def _record(payload: dict) -> None:
        calls.append("x")

    monkeypatch.setitem(worker.HANDLERS, "test.reclaim", _record)

    now = _at(LOCAL_DAY, 9 * 60)
    _freeze(monkeypatch, now)
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id="obx_lease_alive",
                topic="test.reclaim",
                payload={},
                state="in_flight",
                available_at=now + timedelta(minutes=5),
                processed_by="other-worker:1",
                created_at=now - timedelta(minutes=1),
            )
        )

    assert await worker.drain_outbox(limit=5) == 0
    assert calls == []

    async with session_scope() as session:
        row = await session.get(OutboxEvent, "obx_lease_alive")
    assert row is not None
    assert row.state == "in_flight"
    assert row.processed_by == "other-worker:1"
