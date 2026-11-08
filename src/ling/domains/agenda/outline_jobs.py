"""手动生成日程：把"她这一天的计划"这件事做成**可查询的作业**。

## 为什么需要这一层

21:00 的自动生成是"到点没人看"，失败了也没人知道；用户要的是"我现在就想
让她把明天的日程排出来，并且知道它跑完没有"。所以手动入口不是"再调一次
`ensure_day_plan`"，而是一个**作业**：

- **入队**：投一条 ``schedule.outline`` outbox 事件（与自动链路**同一条**）；
- **状态**：按事件 id 就能回答"排队中 / 生成中 / 已完成 / 失败（为什么）"。

## 幂等的三个来源（缺一条就会双生成）

1. **事件 id 是幂等键**：同一个 ``(actor, plan_day, 快照, 尝试号)`` 只有一条
   outbox 行（`outbox_events.id` 主键）。手动点十次 = 一条事件。
2. **在途检查**：已有 ``pending`` / ``in_flight`` 的事件就**不重复投**，
   直接返回那条的 id（202）。
3. **日程域自己的幂等**：``ensure_day_plan`` 在"快照没变、已生成过"时返回
   ``unchanged``，**不增 revision、不调模型**。所以即使事件被重投（退避重试、
   重启补投），也不会把已确认的段推倒。

## 为什么 ``force`` 不是"跳过一切"

``force`` 只表示"就算已经生成过，也按当前快照重排一版"。它**仍然**要过：

- 预算闸（``OUTLINE_MAX_CALLS`` / 当日 token 预算）——超了就是 409，
  **不许**静默降级成"生成成功"；
- lease（同一 attempt 只有一个赢家）；
- 已有段的替代规则（只动"还没开始"的当前段，已开始/已过去的保留）。

## 为什么作业身份要带 ``run`` 而不只是 ``(actor, day)``

一个计划日可能被手动生成过、又被 21:00 自动补跑、再被 force 重排。如果作业
id 只有 ``actor:day``，后一次的状态会覆盖前一次的记录，用户点"重新生成"后
看到的可能还是上一棒的结果。带上 ``run``（第几次投递）之后每次投递是可区分
的，而"当前在途那一条"仍然唯一（由在途检查保证）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import OutboxEvent
from ling.kernel.outbox import like_prefix as _like_prefix

logger = logging.getLogger("ling.domains.agenda.outline_jobs")

OUTLINE_TOPIC = "schedule.outline"

#: 载荷里"这次要按当前快照重排"的标记。
#:
#: 为什么不只用 ``phase=catchup`` 表达 force：相位是**能否调模型、从哪儿开始
#: 排**的判据（日程域的语义），把"用户点了重新生成"塞进相位会让相位说谎。
#: 所以 force 是载荷里的独立布尔，由 worker 原样交给 ``outline_work``。
FORCE_KEY = "force"

#: outbox 里还没被 worker 认领的状态。
QUEUED_STATES: frozenset[str] = frozenset({"pending"})
#: worker 已认领、还在跑。
RUNNING_STATES: frozenset[str] = frozenset({"in_flight"})

#: 作业对外状态。与 outbox 状态 + 计划行状态**合成**得出，不新增表：
#:   queued    → outbox pending
#:   running   → outbox in_flight
#:   ready     → outbox done 且计划行 ready（模型版）
#:   degraded  → outbox done 且计划行 degraded（规则兜底）
#:   failed    → outbox dead，或 outbox done 但计划行 pending（跑过但没落版）
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_READY = "ready"
JOB_DEGRADED = "degraded"
JOB_FAILED = "failed"
#: 已经生成过、这次没有重新生成。
JOB_UNCHANGED = "unchanged"
#: 入队被预算闸拦下（不会真的跑）。
JOB_BLOCKED = "blocked"

#: 人话状态说明（前端直接进 JSX；原因码只留在 `reason_code` 给日志与测试）。
JOB_NOTES: dict[str, str] = {
    JOB_QUEUED: "已经排上队了，她正在准备，稍等一下再看",
    JOB_RUNNING: "她正在排这一天的日程",
    JOB_READY: "这一天的日程已经排好了",
    JOB_DEGRADED: "模型这次没用上，已经按规则铺了睡眠和已确认的安排",
    JOB_FAILED: "这一天的日程没排出来，可以再试一次",
    JOB_UNCHANGED: "这一天已经排过了，这次没有重新生成",
    JOB_BLOCKED: "今天用来生成日程的调用次数用完了",
}

#: 失败原因码 → 人话。**前端不许显示原因码**（设计 §0 的底线）。
FAILURE_NOTES: dict[str, str] = {
    "budget_exhausted": "今天用来生成日程的调用次数或额度用完了，可以在设置里调整预算上限",
    "model_unavailable": "现在没有可用的模型来排日程，可以先按规则安排，等模型恢复后再生成",
    "outline_invalid": "模型给出的安排不合规，已经改按规则安排",
    "degraded_late": "这一天已经开始一段时间了，只能补接下来的确定安排",
    "needs_review": "有几个安排互相冲突，需要有人看一眼",
    "stale_revision": "另一边正好也在改这一天，这一版没写进去，可以重试",
    "lease_busy": "已经有一次生成正在跑",
}

#: 失败原因的通用兜底（未知原因码不给前端看码）。
FAILURE_FALLBACK = "这一天的日程没排出来，可以再试一次"

#: 手动生成允许的目标日范围：角色本地今天、明天（与自动链路一致）。
#: 更早的窗口已经结束，补出来只是"给过去编时间轴"；更远的手动预生成没有
#: 意义（21:00 会自动排），而且会让 prompt 里的"未来意向"预算失真。
MANUAL_MAX_AHEAD_DAYS = 1


def _now() -> datetime:
    """当前时刻（单独一个函数，测试里能钉住它）。"""
    return datetime.now(UTC)


def current_plan_day(timezone: str) -> date:
    """角色"今天"是哪个计划日。路由与测试都走这里，别各自读时钟。"""
    from ling.domains.agenda.timeline import plan_day_for

    return plan_day_for(_now(), timezone)


def job_event_id(*, actor_id: str, plan_day: date, run: int) -> str:
    """手动生成的 outbox 事件 id（= 作业 id = 幂等键）。

    前缀必须与自动事件**同形**（``outline:{actor}:{day}:``）：在途检查、run
    计数、可查询性全都按这个前缀查同一天的事件。这里曾经把 ``manual`` 塞在
    actor 前面，结果手动事件对"前缀查询"隐身——在途检查看不见它，第二次点击
    会再投一条。形状对齐之后，``manual:`` 只是后缀里的一个标记。
    """
    return f"{job_like_prefix(actor_id=actor_id, plan_day=plan_day)}manual:a{run}"


def job_like_prefix(*, actor_id: str, plan_day: date) -> str:
    """某天所有（自动 + 手动）大纲事件的 LIKE 前缀，用来判断"是否在途"。"""
    return f"outline:{actor_id}:{plan_day.isoformat()}:"


def failure_note(reason_code: str | None) -> str:
    """原因码 → 人话。未知码也不把码丢给用户。"""
    if not reason_code:
        return FAILURE_FALLBACK
    return FAILURE_NOTES.get(str(reason_code), FAILURE_FALLBACK)


def note_for(
    state: str, *, reason_code: str | None = None, already_planned: bool = False
) -> str:
    """作业状态 → 人话说明（输入框、按钮文案都用它）。"""
    if state in {JOB_FAILED, JOB_BLOCKED}:
        return failure_note(reason_code)
    if state in {JOB_READY, JOB_UNCHANGED} and already_planned:
        return "这一天的日程已经生成过了；要按现在的情况重新排一版，选「重新生成」"
    return JOB_NOTES.get(state, "")


def job_state(
    *, outbox_state: str | None, plan_state: str | None, plan_revision: int
) -> str:
    """把 outbox 状态与计划行状态合成对外作业状态。

    ``plan_state`` 为 ``None`` 表示"还没有计划行"（还没跑或跑失败没落版）。
    """
    if outbox_state is None:
        return JOB_FAILED
    if outbox_state in QUEUED_STATES:
        return JOB_QUEUED
    if outbox_state in RUNNING_STATES:
        return JOB_RUNNING
    if outbox_state == "dead":
        return JOB_FAILED
    if outbox_state != "done":
        # unknown / 人工改过的值：不猜成成功
        return JOB_FAILED
    if plan_state is None or plan_revision <= 0:
        return JOB_FAILED
    if plan_state == "ready":
        return JOB_READY
    if plan_state == "degraded":
        return JOB_DEGRADED
    return JOB_FAILED


@dataclass(frozen=True)
class OutlineJob:
    """一个可查询的生成作业（不落新表：身份就是 outbox 事件 id）。"""

    job_id: str
    plan_day: date
    state: str
    note: str
    revision: int = 0
    plan_state: str | None = None
    reason_code: str | None = None
    created_at: datetime | None = None
    #: 这次投递是否刚刚真的入队（False = 复用了已有作业）。
    enqueued: bool = False

    @property
    def is_terminal(self) -> bool:
        """终态：前端可以停止轮询、重新读时间轴。"""
        return self.state in {
            JOB_READY,
            JOB_DEGRADED,
            JOB_FAILED,
            JOB_UNCHANGED,
            JOB_BLOCKED,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "v1",
            "job_id": self.job_id,
            "plan_day": self.plan_day.isoformat(),
            "state": self.state,
            "note": self.note,
            "revision": self.revision,
            "plan_state": self.plan_state,
            # 原因码只给日志与测试；界面显示 `note`（设计 §0：界面不出现原因码）
            "reason_code": self.reason_code,
            "enqueued": self.enqueued,
        }


# ---------------------------------------------------------------- 查询


@dataclass(frozen=True)
class DayJobSnapshot:
    """某一天的"现在能不能生成 / 生成到哪了"。

    ``events`` 是当天的全部大纲事件 ``(id, state, payload)``：手动生成要么靠它
    判在途，要么靠 ``request_id`` 判"同一次点击重试"——两种判据都在这一份
    快照里，不额外查库。
    """

    inflight_job_id: str | None
    latest_job_id: str | None
    latest_outbox_state: str | None
    plan_state: str | None
    plan_revision: int
    events: tuple[tuple[str, str, dict], ...] = ()


async def day_job_snapshot(
    session: AsyncSession, *, actor_id: str, plan_day: date
) -> DayJobSnapshot:
    """读某天所有大纲事件的现状（自动 + 手动一视同仁）。

    **自动链路也算**：21:00 那条 ``schedule.outline`` 正在跑时用户点按钮，
    必须看到"正在生成"，而不是又排一次。
    """
    from ling.domains.agenda.timeline import load_day_plan

    rows = list(
        (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.topic == OUTLINE_TOPIC,
                    OutboxEvent.id.like(
                        _like_prefix(job_like_prefix(actor_id=actor_id, plan_day=plan_day)),
                        escape="\\",
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        ).scalars()
    )
    plan = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    inflight = next(
        (
            str(row.id)
            for row in rows
            if str(row.state) in (QUEUED_STATES | RUNNING_STATES)
        ),
        None,
    )
    latest = rows[-1] if rows else None
    return DayJobSnapshot(
        inflight_job_id=inflight,
        latest_job_id=str(latest.id) if latest is not None else None,
        latest_outbox_state=str(latest.state) if latest is not None else None,
        plan_state=str(plan.state) if plan is not None else None,
        plan_revision=int(plan.revision) if plan is not None else 0,
        events=tuple(
            (str(row.id), str(row.state), dict(row.payload or {})) for row in rows
        ),
    )


async def load_job(session: AsyncSession, *, job_id: str) -> OutlineJob | None:
    """按作业 id 读状态。不存在返回 ``None``（路由据此 404）。"""
    from ling.domains.agenda.timeline import load_day_plan

    row = await session.get(OutboxEvent, job_id)
    if row is None:
        return None
    plan_day = _payload_date(dict(row.payload or {}))
    if plan_day is None:
        return None
    plan = await load_day_plan(session, actor_id=str(dict(row.payload or {}).get("actor_id")
                                                   or ""), plan_day=plan_day)
    state = job_state(
        outbox_state=str(row.state),
        plan_state=str(plan.state) if plan is not None else None,
        plan_revision=int(plan.revision) if plan is not None else 0,
    )
    reason = str(plan.last_error_code) if plan is not None and plan.last_error_code else None
    return OutlineJob(
        job_id=str(row.id),
        plan_day=plan_day,
        state=state,
        note=note_for(state, reason_code=reason, already_planned=state == JOB_READY),
        revision=int(plan.revision) if plan is not None else 0,
        plan_state=str(plan.state) if plan is not None else None,
        reason_code=reason,
        created_at=row.created_at,
    )


def _payload_date(payload: dict) -> date | None:
    raw = str(payload.get("plan_day") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- 预算预检


@dataclass(frozen=True)
class BudgetDecision:
    """这次生成还能不能调模型。"""

    allowed: bool
    reason_code: str | None = None


async def outline_budget_decision(
    session: AsyncSession, *, actor_id: str, plan_day: date, router: object | None,
    config_reader: object | None,
) -> BudgetDecision:
    """**事前**判"能不能调模型"：模型可用性 + 当日调用数与 token 预算。

    为什么不等到 worker 里再判：worker 里预算耗尽也只会落成 ``degraded``
    （规则大纲），用户会以为"生成成功"了。手动入口要的是**明确失败**：
    "今天的生成次数用完了" —— 409，而不是一份看起来排好的规则日程。

    判据与 ``timeline._reserve_call`` 完全一致（这里读的是同一张
    ``behavior_model_calls`` 的同一组常量），只是提前看一眼，**不占额度**。
    """
    from ling.domains.agenda.timeline import (
        OUTLINE_DAILY_TOKEN_BUDGET,
        OUTLINE_MAX_CALLS,
        OUTLINE_PROFILE,
        OUTLINE_RESERVED_INPUT_TOKENS,
        OUTLINE_RESERVED_OUTPUT_TOKENS,
        behavior_model_ready,
    )

    if not await behavior_model_ready(router, config_reader):
        # 没有可用模型：**零调用**也**零额度**，但手动生成的意义就是"要模型
        # 排一版"，所以这里明确拒绝，而不是悄悄给一份规则日程。
        return BudgetDecision(allowed=False, reason_code="model_unavailable")

    from ling.domains.agenda.timeline import _day_call_totals  # noqa: PLC2701

    calls, used = await _day_call_totals(session, actor_id=actor_id, plan_day=plan_day)
    reserved = OUTLINE_RESERVED_INPUT_TOKENS + OUTLINE_RESERVED_OUTPUT_TOKENS
    if calls >= OUTLINE_MAX_CALLS or used + reserved > OUTLINE_DAILY_TOKEN_BUDGET:
        return BudgetDecision(allowed=False, reason_code="budget_exhausted")
    logger.debug(
        "大纲预算预检通过（profile=%s calls=%d/%d tokens=%d/%d）",
        OUTLINE_PROFILE,
        calls,
        OUTLINE_MAX_CALLS,
        used,
        OUTLINE_DAILY_TOKEN_BUDGET,
    )
    return BudgetDecision(allowed=True)


# ---------------------------------------------------------------- 入队


@dataclass(frozen=True)
class ManualOutlinePlan:
    """按"这一天现在是什么状态"决定这次点击该做什么（**只读，不写库**）。

    - ``reuse``：已经有在途作业（自动或手动）→ 返回那条，202；
    - ``unchanged``：已生成过且没要求重排 → 200，不增 revision、不调模型；
    - ``enqueue``：需要投递一条新作业（预算预检与写入由调用方接着做）。
    """

    action: str
    job_id: str | None
    already_planned: bool
    plan_state: str | None
    plan_revision: int

    @property
    def should_enqueue(self) -> bool:
        return self.action == ACTION_ENQUEUE


ACTION_REUSE = "reuse"
ACTION_UNCHANGED = "unchanged"
ACTION_ENQUEUE = "enqueue"


async def plan_manual_outline(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    force: bool,
    request_id: str | None = None,
) -> ManualOutlinePlan:
    """决定"这次点击是复用、已生成、还是真的投递"——不写任何东西。

    顺序是刻意的：**先看在途，再看已生成，最后才看预算**。
    已经有作业在跑时不该因为"预算刚好用完"把用户挡回去——那一条已经在
    预算里了，用户看到的应该是它。
    """
    snapshot = await day_job_snapshot(session, actor_id=actor_id, plan_day=plan_day)
    already_planned = snapshot.plan_revision > 0
    if request_id:
        # 同一次点击重试（网络超时、前端自动重发）：**不管它跑到哪一步**，
        # 都返回同一条作业。光靠在途检查不够——作业可能已经跑完，此时再投
        # 一次 force 就是"用户只点了一下，日程被重排两版"。
        for event_id, _state, payload in snapshot.events:
            if str(payload.get("request_id") or "") == request_id:
                return ManualOutlinePlan(
                    action=ACTION_REUSE,
                    job_id=event_id,
                    already_planned=already_planned,
                    plan_state=snapshot.plan_state,
                    plan_revision=snapshot.plan_revision,
                )
    if snapshot.inflight_job_id is not None:
        return ManualOutlinePlan(
            action=ACTION_REUSE,
            job_id=snapshot.inflight_job_id,
            already_planned=already_planned,
            plan_state=snapshot.plan_state,
            plan_revision=snapshot.plan_revision,
        )
    if already_planned and not force:
        return ManualOutlinePlan(
            action=ACTION_UNCHANGED,
            job_id=snapshot.latest_job_id,
            already_planned=True,
            plan_state=snapshot.plan_state,
            plan_revision=snapshot.plan_revision,
        )
    return ManualOutlinePlan(
        action=ACTION_ENQUEUE,
        job_id=None,
        already_planned=already_planned,
        plan_state=snapshot.plan_state,
        plan_revision=snapshot.plan_revision,
    )


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    enqueued: bool
    state: str
    note: str
    already_planned: bool


async def enqueue_manual_outline(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    force: bool,
    request_id: str | None = None,
    now: datetime | None = None,
) -> EnqueueResult:
    """把"手动生成这一天"投递成一次作业；并发重复点返回已有作业。

    调用方负责预算预检（:func:`outline_budget_decision`）与 commit。

    并发安全靠**事件 id 主键**：两个请求同时算出同一个 ``run`` 时，只有一条
    能提交，另一条在 flush 时撞主键——那一条直接读回已存在的事件，返回同一
    个 job id。这不是错误，是"两个浏览器点了同一个按钮"的正常结局。
    """
    from sqlalchemy.exc import IntegrityError

    from ling.domains.agenda.timeline import PHASE_DUE, load_day_plan

    moment = now or _now()
    plan = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    already_planned = plan is not None and int(plan.revision) > 0

    # 新的投递：run = 已有同日前缀事件数 + 1。这样 force 重排、失败重试各自
    # 都有可区分的作业 id，而不会互相覆盖状态。
    prior = list(
        (
            await session.execute(
                select(OutboxEvent.id).where(
                    OutboxEvent.topic == OUTLINE_TOPIC,
                    OutboxEvent.id.like(
                        _like_prefix(job_like_prefix(actor_id=actor_id, plan_day=plan_day)),
                        escape="\\",
                    ),
                )
            )
        ).scalars()
    )
    job_id = job_event_id(actor_id=actor_id, plan_day=plan_day, run=len(prior) + 1)

    try:
        async with session.begin_nested():
            session.add(
                OutboxEvent(
                    id=job_id,
                    topic=OUTLINE_TOPIC,
                    payload={
                        "actor_id": actor_id,
                        "plan_day": plan_day.isoformat(),
                        "phase": PHASE_DUE,
                        "manual": True,
                        "request_id": (request_id or ""),
                        FORCE_KEY: bool(force),
                        "scanned_at": moment.isoformat(),
                    },
                    available_at=moment,
                    created_at=moment,
                )
            )
            await session.flush()
    except IntegrityError:
        # 另一个请求同时投了同一条：直接复用那条作业（同一 job id）。
        return EnqueueResult(
            job_id=job_id,
            enqueued=False,
            state=JOB_QUEUED,
            note=note_for(JOB_QUEUED, already_planned=already_planned),
            already_planned=already_planned,
        )

    return EnqueueResult(
        job_id=job_id,
        enqueued=True,
        state=JOB_QUEUED,
        note=note_for(JOB_QUEUED, already_planned=already_planned),
        already_planned=already_planned,
    )


# ---------------------------------------------------------------- 路由辅助


def allowed_plan_days(*, today: date, max_ahead: int = MANUAL_MAX_AHEAD_DAYS) -> tuple[date, date]:
    """手动生成允许的目标日区间 ``[今天, 今天+N]``（含两端）。"""
    return today, today + timedelta(days=max_ahead)


__all__ = [
    "FAILURE_NOTES",
    "JOB_BLOCKED",
    "JOB_DEGRADED",
    "JOB_FAILED",
    "JOB_NOTES",
    "JOB_QUEUED",
    "JOB_READY",
    "JOB_RUNNING",
    "JOB_UNCHANGED",
    "MANUAL_MAX_AHEAD_DAYS",
    "OUTLINE_TOPIC",
    "BudgetDecision",
    "DayJobSnapshot",
    "ACTION_ENQUEUE",
    "ACTION_REUSE",
    "ACTION_UNCHANGED",
    "EnqueueResult",
    "ManualOutlinePlan",
    "plan_manual_outline",
    "FORCE_KEY",
    "MANUAL_OUTLINE_LIMITER",
    "OutlineJob",
    "OutlineRateLimiter",
    "allowed_plan_days",
    "current_plan_day",
    "day_job_snapshot",
    "enqueue_manual_outline",
    "failure_note",
    "job_event_id",
    "job_like_prefix",
    "job_state",
    "load_job",
    "note_for",
    "outline_budget_decision",
]


# ---------------------------------------------------------------- 路由限流


class OutlineRateLimiter:
    """手动生成的**进程内**限流（per subject，滑动窗口）。

    ## 为什么另有一层限流，而不只靠预算闸

    预算闸管的是"模型调用"，判据是**计划日**的额度——但它挡不住一种情况：
    用户狂点按钮但每次都因"已生成过（unchanged）"或"已在途"而**不调模型**，
    于是预算闸永远不触发，而每次点击都在打库、拼 prompt 输入。限流挡的是
    **请求**，与预算是两件事。

    ## 为什么是进程内

    与 `identity` 的显示名缓存同一个理由：这是节流，不是审计账；多副本各算
    一份最多多放行几次，代价是幂等投递（同一天的事件 id 只有一条）。
    重启清零可以接受。
    """

    def __init__(self, *, window_seconds: float = 60.0, limit: int = 6) -> None:
        self.window_seconds = window_seconds
        self.limit = limit
        self._hits: dict[str, list[float]] = {}
        self._clock = time.monotonic
        self._max_keys = 4096

    def allow(self, key: str) -> bool:
        """记一次并回答"这次放不放行"。超限返回 False（调用方回 429）。"""
        now = self._clock()
        floor = now - self.window_seconds
        hits = [stamp for stamp in self._hits.get(key, []) if stamp > floor]
        allowed = len(hits) < self.limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > self._max_keys:
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
            for stale, _ in ordered[: len(self._hits) - self._max_keys]:
                self._hits.pop(stale, None)
        return allowed

    def retry_after(self, key: str) -> int:
        """还要等几秒才能再点（给 429 的 `Retry-After`）。"""
        hits = self._hits.get(key) or []
        if not hits:
            return 0
        return max(1, int(self.window_seconds - (self._clock() - hits[-1])) + 1)

    def reset(self) -> None:
        self._hits.clear()


#: 进程级单例（API 进程内共享；测试可 `reset()`）。
MANUAL_OUTLINE_LIMITER = OutlineRateLimiter()
