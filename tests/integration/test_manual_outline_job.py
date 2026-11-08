"""手动生成日程（真 PostgreSQL）：入队、幂等、force、预算闸、并发。

需求原话是「在日程界面，添加一个按钮可以手动生成日程」。这一组用例守的是
**按钮背后那件事必须诚实**：

1. 第一次点 → 202 且**真的投了一条** `schedule.outline`（与 21:00 自动生成
   同一条链路），计划行只有一行，revision 只增 1；
2. 重复点 → 幂等（不新增 revision、不多一次模型调用），返回同一个作业；
3. 已生成过且非 `force` → 200 + 「已经生成过」，vault 不动；
4. `force` → 新作业、revision+1，**旧的未开始段被 superseded**，已开始/已过去
   的段保留（计划不许改写历史）；
5. 预算不足 / 无可用模型 → **409** 且零模型调用、零 outbox 事件
   （不许静默降级成一份"看起来生成成功的"规则日程）；
6. 生成中再点 → 202 且复用同一个作业 id，不对同一天产生第二条在途事件；
7. 作业状态可查，`note` 是人话，原因码不进 note。

链路跑法：入队用真 API（cookie 登录），跑作业用 worker 的
``handle_schedule_outline``（同一进程内直接调），模型用 stub router——
这样"路由 → outbox → worker → 日程域"整条链路都被覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda import outline_jobs as jobs
from ling.domains.agenda.timeline import (
    load_day_plan,
    load_segments,
    plan_day_window,
    resolve_plan_timezone,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActorRoutineRow,
    BehaviorModelCallRow,
    DayPlanRow,
    OutboxEvent,
    ScheduleSegmentRow,
)
from ling.models.router import StreamOutcome
from ling.ports.config import (
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
)
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
OUTLINE_PROFILE = "memory.extract"

# 固定"现在"：**目标日 D 窗口开始前 1 小时**（上海 2026-11-06 23:00）。
#
# 这个时刻很关键：相位由"现在"与 D 的醒点相对位置决定——醒来之后（上午、
# 下午、半夜都算）是 ``late``，**零模型**，手动生成立刻退化成规则锚点，那种
# 用例验不出"按钮真的调了模型"。选在 D 窗口开始前，目标日就是 due 相位，
# 与生产里"晚上 21 点手动给明天排日程"完全同构。
TARGET_DAY = date(2026, 11, 7)
FIXED_NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)  # 上海 11-06 23:00


def _shanghai_now() -> datetime:
    """上海本地 2026-11-06 23:00（= UTC 15:00），即 D=11-07 的 due 相位。"""
    from zoneinfo import ZoneInfo

    return datetime(2026, 11, 6, 23, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


# ---------------------------------------------------------------- 种子与金手指


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


async def _today() -> tuple[date, str]:
    """这次要生成的目标日与角色时区。

    目标日取 ``TARGET_DAY``（钉死）：固定时刻下"明天"就是它，相位是 due
    （会调模型）。用真实时钟推"今天"会让用例在夜里跑成 late。
    """
    async with session_scope() as session:
        timezone, _source = await resolve_plan_timezone(session)
    return TARGET_DAY, timezone


def _iso(plan_day: date, minute: int, timezone: str) -> str:
    from ling.domains.agenda.timeline import local_minute_instant

    return local_minute_instant(plan_day, minute, timezone).isoformat()


def _valid_outline(plan_day: date, timezone: str) -> str:
    """两段清醒时段的模型输出（不压睡眠锚点）。"""
    return json.dumps(
        {
            "segments": [
                {
                    "kind": "meal",
                    "title": "吃早饭",
                    "start_at": _iso(plan_day, 9 * 60, timezone),
                    "end_at": _iso(plan_day, 10 * 60, timezone),
                    "place_id": None,
                    "flexibility": "flexible",
                },
                {
                    "kind": "work",
                    "title": "写代码",
                    "start_at": _iso(plan_day, 13 * 60, timezone),
                    "end_at": _iso(plan_day, 15 * 60, timezone),
                    "place_id": None,
                    "flexibility": "fixed",
                },
            ]
        },
        ensure_ascii=False,
    )


class _StubStream:
    """stub router：每次调用吐一段合法 JSON，记录调用次数。"""

    def __init__(self, plan_day: date, timezone: str) -> None:
        self.calls = 0
        self._payload = _valid_outline(plan_day, timezone)

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.calls += 1
        yield ModelChunk(delta=self._payload), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._payload)


def _config(*, with_outline_model: bool) -> EffectiveConfig:
    """有效的模型配置；``with_outline_model=False`` 时大纲职责没绑定。"""
    providers = {
        "fake-main": ProviderConfig(
            provider_id="fake-main",
            kind="fake",
            models=(ProviderModel("plan-lite"),),
        )
    }
    profiles = (
        {
            OUTLINE_PROFILE: ProfileBinding(
                profile=OUTLINE_PROFILE,
                provider_id="fake-main",
                model_id="plan-lite",
            )
        }
        if with_outline_model
        else {}
    )
    return EffectiveConfig(revision=1, providers=providers, profiles=profiles)


class _StubConfigReader:
    def __init__(self, config: EffectiveConfig) -> None:
        self._config = config

    async def effective(self) -> EffectiveConfig:
        return self._config


def _install_interfaces(
    env: TestEnv, *, with_outline_model: bool = True, monkeypatch: pytest.MonkeyPatch
) -> tuple[_StubStream, _StubConfigReader]:
    """把 app.state 上的 router / 配置换成桩，让预算预检与 worker 都走它。

    真实装配下 `ConfigDep` 读的是 secret_box 解密出的已发布修订；测试里没有
    已发布修订，所以这里把同一个 reader 同时给路由（依赖覆盖）与 worker。

    **必须用 ``monkeypatch``**：``worker._MEMORY_ROUTER`` / ``_config_reader``
    是**进程级全局**，直接赋值会漏到后面的用例里。曾经正是这里漏了——
    下一个模块的记忆提取读到本模块的桩 router，断言"应提取名字"直接变空，
    而那条用例单独跑永远是通过的（这类跨模块污染最难查）。
    """
    router = _StubStream(*(_seed_today_pair()))
    reader = _StubConfigReader(_config(with_outline_model=with_outline_model))
    env.app.dependency_overrides[_config_dep()] = lambda: reader
    env.app.dependency_overrides[_router_dep()] = lambda: router
    import ling.worker as worker_module

    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker_module, "_config_reader", lambda session: reader)
    return router, reader


def _seed_today_pair() -> tuple[date, str]:
    """同步版本的 (今天, 时区)：桩在构造时要一个日期，这里用固定日期算。

    固定日期与 `FIXED_NOW` 一致（2026-11-07 上海 = 计划日 2026-11-07）。
    """
    return date(2026, 11, 7), TZ


def _config_dep():  # noqa: ANN202
    from ling.api.deps import config_service

    return config_service


def _router_dep():  # noqa: ANN202
    from ling.api.deps import get_router

    return get_router


async def _post(env: TestEnv, *, plan_day: date, force: bool = False):  # noqa: ANN201
    return await env.client.post(
        "/v1/life/outline-jobs",
        json={"plan_day": plan_day.isoformat(), "force": force},
    )


async def _run_job(job_id: str) -> None:
    """跑一次真实链路：认领 → handler → 确认（与 worker 循环完全一样）。

    刻意不手写状态：``claim_next_outbox_event`` / ``settle_outbox_event``
    就是生产里那两个短事务，绕开它们等于在测一个不存在的执行器。
    """
    import ling.worker as worker_module

    claim = await worker_module.claim_next_outbox_event()
    assert claim is not None, "没有可认领的事件"
    assert claim.event_id == job_id, f"认领到了别的事件：{claim.event_id}"
    await worker_module.handle_schedule_outline(claim.payload)
    await worker_module.settle_outbox_event(claim, ok=True)


async def _plan(plan_day: date) -> DayPlanRow | None:
    async with session_scope() as session:
        return await load_day_plan(session, actor_id=ACTOR, plan_day=plan_day)


async def _outline_events(plan_day: date) -> list[OutboxEvent]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.topic == "schedule.outline",
                        # JSON 列在非 JSONB 上不能 .astext：按事件 id 前缀过滤，
                        # 前缀里就带日期（与生产同一条判据）。
                        OutboxEvent.id.like(
                            f"%:{plan_day.isoformat()}:%"
                        ),
                    )
                    .order_by(OutboxEvent.created_at)
                )
            ).scalars()
        )


async def _model_calls(plan_day: date) -> int:
    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(BehaviorModelCallRow)
                    .where(BehaviorModelCallRow.plan_day == plan_day)
                )
            ).scalar_one()
        )


async def _leaves(plan_day: date) -> list[ScheduleSegmentRow]:
    row = await _plan(plan_day)
    assert row is not None
    async with session_scope() as session:
        return await load_segments(session, plan_id=str(row.id))


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _reset_job_state() -> None:
    """限流器与"现在"都是进程级全局：用例之间必须互不影响。"""
    jobs.MANUAL_OUTLINE_LIMITER.reset()
    jobs.MANUAL_OUTLINE_LIMITER.window_seconds = 60.0
    jobs.MANUAL_OUTLINE_LIMITER.limit = 6


def _fixed_now() -> datetime:
    """这个用例文件里的"现在"（固定值，让相位可预测）。

    不改成真的时钟：设计里"今天"由**角色时区**算，而测试要能在任何时间跑。
    所以把三个读时钟的地方都钉住：日程域的 ``utcnow``（相位判断）、
    worker handler 的 ``_schedule_now``、手动生成入队时用的时间戳。
    """
    return _shanghai_now()


@pytest.fixture(autouse=True)
def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"现在"钉在计划日窗口起点之后，让相位可预测（due/catchup）。"""
    import ling.domains.agenda.outline_jobs as jobs_module
    import ling.domains.agenda.timeline as timeline_module
    import ling.worker as worker_module

    monkeypatch.setattr(timeline_module, "utcnow", _fixed_now)
    monkeypatch.setattr(jobs_module, "_now", _fixed_now)
    # handler 里的"现在"也要钉住：相位由它与计划日窗口的相对位置决定。
    monkeypatch.setattr(worker_module, "_schedule_now", _fixed_now)


# ---------------------------------------------------------------- ① 首次生成


async def test_first_manual_outline_creates_plan_and_one_revision(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_routine()
    router, _reader = _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, timezone = await _today()

    response = await _post(web_env, plan_day=today)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "queued"
    assert body["enqueued"] is True
    assert body["plan_day"] == today.isoformat()
    job_id = body["job_id"]

    # 只是入队：还没生成、没有计划行
    assert await _plan(today) is None
    events = await _outline_events(today)
    assert [str(e.id) for e in events] == [job_id]
    assert events[0].topic == "schedule.outline"
    assert events[0].payload["manual"] is True

    # 跑作业（与 worker 循环同一调用）
    await _run_job(job_id)

    plan = await _plan(today)
    assert plan is not None, "生成之后必须有计划行"
    assert plan.state == "ready"
    assert plan.revision == 1, "第一次生成 revision 只能是 1"
    assert router.calls == 1
    assert await _model_calls(today) == 1

    # 作业状态可查：终态 + 人话
    status = await web_env.client.get(f"/v1/life/outline-jobs/{job_id}")
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["state"] == "ready"
    assert payload["revision"] == 1
    assert "日程" in payload["note"]
    # 前端不许显示原因码：note 里不能出现下划线原因码
    assert "budget_exhausted" not in payload["note"]


# ---------------------------------------------------------------- ② 重复点


async def test_second_click_is_idempotent(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_routine()
    router, _reader = _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()

    first = await _post(web_env, plan_day=today)
    assert first.status_code == 202
    job_id = first.json()["job_id"]

    # 作业还在跑（queued）时再点：202 + 同一个 job id，不投第二条
    again = await _post(web_env, plan_day=today)
    assert again.status_code == 202, again.text
    assert again.json()["job_id"] == job_id
    assert again.json()["enqueued"] is False
    assert len(await _outline_events(today)) == 1, "在途时不许再投一条"

    await _run_job(job_id)
    calls_after_first = router.calls
    assert calls_after_first == 1

    # 生成完之后再点（非 force）：200 + 已生成过，不增 revision、不调模型
    third = await _post(web_env, plan_day=today)
    assert third.status_code == 200, third.text
    body = third.json()
    assert body["state"] == "unchanged"
    assert body["enqueued"] is False
    assert "已经生成过" in body["note"]

    plan = await _plan(today)
    assert plan is not None and plan.revision == 1, "重复点不许增加 revision"
    assert router.calls == calls_after_first, "重复点不许再调模型"
    assert len(await _outline_events(today)) == 1, "重复点不许投事件"


async def test_manual_click_reuses_an_inflight_automatic_job(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """21:00 自动生成正在跑时点按钮：复用**那一条**，不另投。

    这是"手动与自动共享同一条链路"最容易做错的地方：如果手动用另一套幂等键，
    同一天就会有两个作业并行、两次模型调用、两个 revision。
    """
    from ling.infra.security import new_id

    await _seed_routine()
    _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()

    # 模拟自动扫描已经投过一条（与 `_schedule_outline` 同形的事件 id）
    auto_id = f"outline:{ACTOR}:{today.isoformat()}:t0:r1:a1"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=auto_id,
                topic="schedule.outline",
                payload={
                    "actor_id": ACTOR,
                    "plan_day": today.isoformat(),
                    "phase": "due",
                    "timezone": TZ,
                    "attempt": 1,
                    "scanned_at": datetime.now(UTC).isoformat(),
                },
                available_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

    response = await _post(web_env, plan_day=today, force=True)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"] == auto_id, "必须复用自动作业，而不是另起一条"
    assert body["enqueued"] is False
    assert len(await _outline_events(today)) == 1
    assert new_id("x")  # 只是为了让 new_id 的导入不被 ruff 当成未使用


async def test_same_request_id_is_deduplicated_even_after_completion(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一次点击超时重发（带同一个 request_id）：不许重排第二版。

    这条比"在途复用"更严：作业可能已经跑完，此时再投一次 force 就会
    把日程重排一版——用户只点了一下。所以 request_id 的判据跨状态。
    """
    await _seed_routine()
    router, _reader = _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()

    first = await web_env.client.post(
        "/v1/life/outline-jobs",
        json={"plan_day": today.isoformat(), "request_id": "click-1"},
    )
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    await _run_job(job_id)
    assert (await _plan(today)).revision == 1
    calls = router.calls

    retry = await web_env.client.post(
        "/v1/life/outline-jobs",
        json={"plan_day": today.isoformat(), "request_id": "click-1", "force": True},
    )
    assert retry.status_code in (200, 202), retry.text
    assert retry.json()["job_id"] == job_id
    assert retry.json()["enqueued"] is False
    assert len(await _outline_events(today)) == 1
    assert (await _plan(today)).revision == 1, "重发不许再推进一版"
    assert router.calls == calls, "重发不许再调模型"


# ---------------------------------------------------------------- ③ force


async def test_force_regenerates_and_supersedes_only_future_segments(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_routine()
    router, _reader = _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, timezone = await _today()

    job_id = (await _post(web_env, plan_day=today)).json()["job_id"]
    await _run_job(job_id)
    before = await _leaves(today)
    revision_before = (await _plan(today)).revision

    # 已开始的段：把某一段的起点挪到过去，模拟"这一天已经过了一部分"
    window_start, _end = plan_day_window(today, timezone)
    async with session_scope() as session:
        started = (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.actor_id == ACTOR,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.is_leaf.is_(True),
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars().first()
        assert started is not None
        started.start_at = window_start - timedelta(hours=1)
        started.end_at = window_start - timedelta(minutes=10)
        await session.commit()
        started_id = str(started.id)

    forced = await _post(web_env, plan_day=today, force=True)
    assert forced.status_code == 202, forced.text
    forced_body = forced.json()
    assert forced_body["enqueued"] is True
    assert forced_body["job_id"] != job_id, "force 是一个新作业"
    await _run_job(forced_body["job_id"])

    plan = await _plan(today)
    assert plan is not None
    assert plan.revision == revision_before + 1, "force 应当且只应当推进一版"

    # 未开始的旧段被 superseded；已开始的那段保留（计划不改写历史）
    after = await _leaves(today)
    current_ids = {str(row.id) for row in after if row.status == "current"}
    superseded = [row for row in before if row.status == "current"]
    superseded_now = [
        row
        for row in superseded
        if row.start_at >= window_start and str(row.id) not in current_ids
    ]
    assert superseded_now, "旧的未开始段必须被 superseded"
    # 已开始时段的原行仍在（同一 plan 里 superseded/current 都可查到）
    async with session_scope() as session:
        still = await session.get(ScheduleSegmentRow, started_id)
        assert still is not None, "已经开始的段不许被删"
        assert still.status == "current", "已开始的段不能被新版推倒"


# ---------------------------------------------------------------- ④ 预算闸


async def test_budget_exhausted_returns_409_and_makes_no_model_call(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_routine()
    router, _reader = _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()

    # 先把当日大纲额度用满（真实写行为模型调用行）。
    from ling.infra.security import new_id

    async with session_scope() as session:
        for _ in range(2):  # OUTLINE_MAX_CALLS = 2
            session.add(
                BehaviorModelCallRow(
                    id=new_id("bcall"),
                    actor_id=ACTOR,
                    plan_day=today,
                    kind="outline",
                    idempotency_key=new_id("key"),
                    attempt=1,
                    state="committed",
                    reserved_input_tokens=2500,
                    reserved_output_tokens=1000,
                    actual_tokens=1000,
                    lease_until=datetime.now(UTC),
                )
            )
        await session.commit()

    response = await _post(web_env, plan_day=today)

    assert response.status_code == 409, response.text
    body = response.json()
    # 人话：说清"用完了"和"去哪里改"，不出现原因码
    message = json.dumps(body, ensure_ascii=False)
    assert "用完了" in message
    assert "budget_exhausted" not in message
    assert router.calls == 0, "预算不足时一次模型都不许调"
    assert await _outline_events(today) == [], "预算不足时不许投递作业"
    assert await _plan(today) is None, "不许静默降级成一份规则日程"


async def test_no_outline_model_returns_409(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有可用模型时不投递、不生成规则日程——手动生成要的是模型版。"""
    await _seed_routine()
    _install_interfaces(web_env, with_outline_model=False, monkeypatch=monkeypatch)
    today, _tz = await _today()

    response = await _post(web_env, plan_day=today)
    assert response.status_code == 409, response.text
    assert "模型" in json.dumps(response.json(), ensure_ascii=False)
    assert await _outline_events(today) == []


# ---------------------------------------------------------------- ⑤ 日期与权限


async def test_out_of_range_day_is_422(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()
    # 允许范围是 [角色今天, 角色今天+1]；固定时钟下"今天"是 TARGET_DAY-1。
    for bad in (today - timedelta(days=2), today + timedelta(days=1)):
        response = await _post(web_env, plan_day=bad)
        assert response.status_code == 422, (bad, response.text)


async def test_member_cannot_trigger_generation(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from ling.infra.security import hash_password

    _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()
    async with session_scope() as session:
        from ling.infra.models import User
        from ling.infra.security import new_id

        session.add(
            User(
                id=new_id("user"),
                username="member2",
                password_hash=hash_password("member-password-2"),
                role="member",
                subject_id="subj_member2",
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=web_env.app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "member2", "password": "member-password-2"},
        )
        assert login.status_code == 200, login.text
        response = await client.post(
            "/v1/life/outline-jobs", json={"plan_day": today.isoformat()}
        )
    assert response.status_code == 403, response.text
    assert await _outline_events(today) == []


async def test_rate_limit_returns_429(
    web_env: TestEnv,
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_interfaces(web_env, monkeypatch=monkeypatch)
    today, _tz = await _today()
    jobs.MANUAL_OUTLINE_LIMITER.limit = 2

    codes = [(await _post(web_env, plan_day=today)).status_code for _ in range(3)]
    assert codes[-1] == 429, codes


# ---------------------------------------------------------------- ⑥ 状态映射


def test_job_state_mapping_is_honest() -> None:
    """状态合成必须能区分"在跑""跑成了""跑失败""降级了"。"""
    assert jobs.job_state(outbox_state="pending", plan_state=None, plan_revision=0) == "queued"
    assert jobs.job_state(outbox_state="in_flight", plan_state=None, plan_revision=0) == "running"
    assert jobs.job_state(outbox_state="done", plan_state="ready", plan_revision=1) == "ready"
    assert (
        jobs.job_state(outbox_state="done", plan_state="degraded", plan_revision=1)
        == "degraded"
    )
    # 跑过但没落版 = 失败，不是"生成中"
    assert jobs.job_state(outbox_state="done", plan_state=None, plan_revision=0) == "failed"
    assert jobs.job_state(outbox_state="dead", plan_state="ready", plan_revision=1) == "failed"


def test_failure_notes_are_human_readable() -> None:
    """原因码不进界面，且未知码也有兜底文案。"""
    assert jobs.failure_note("budget_exhausted") != jobs.failure_note("unknown_code")
    assert "budget_exhausted" not in jobs.failure_note("budget_exhausted")
    assert jobs.failure_note(None)
    assert "重新" in jobs.note_for("unchanged", already_planned=True)
