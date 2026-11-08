"""聊天里的临时提议：识别候选 → 代码硬门 → 模型只决定"去不去" → 局部修订。

设计 §4 的完整链路是::

    聊天事件 / turn_id → 识别为提议（不自动承诺）→ 硬门 → 意愿模型
    → 重新校验 → 同事务提交 revision+1 → 对用户说明决定

本模块实现"识别 + 硬门 + 意愿 + 局部修订"。它**不**改 ``life.schedule`` 的调度
主体：聊天侧只投一条 ``schedule.replan`` outbox 事件，真正干活的是
``worker.handle_schedule_replan``。

## 三条硬规则（设计反复强调，每一条都有测试钉住）

1. **计划不是事实**：接受一次提议只改 ``schedule_segments``（旧段置
   ``superseded``、不物理删），**绝不**写 ``activities`` / ``self_experiences``。
   "答应去看电影"不等于"看过电影"。
2. **完成必须有证据**：本模块不产生任何完成状态；活动开始/结束仍由活动域
   按自己的证据链写。
3. **状态不影响权利**：精力/饱腹只用来判断**可选外出**可不可行，从来不能
   用来拒绝聊天、隐私或已确认的承诺。

## 谁能改计划：四层，越界即拒

| 层 | 谁决定 | 例子 |
| --- | --- | --- |
| 识别 | 关键词 / 结构化信号**只当候选** | "去看电影吧"命中候选，**不代表**用户确认 |
| 硬门 | **代码** | 睡眠窗内、撞 confirmed 承诺、``rest_minute``、体力不够、预算/频率 |
| 意愿 + 怎么改 | 模型（严格 JSON） | "愿不愿意去"、"去的话 19–21 点改成什么" |
| 落库 | **代码**（裁剪/拒收 + CAS） | 越界段整份拒收；区间外的段逐字段不动 |

模型只能在代码算好的作用域内改，它给的段一律再过
:func:`validate_replan_segments` 与 ``timeline.replace_scope_multi``；**越界是
拒收，不是裁剪**——裁剪等于替模型编一个它没说的安排。

## 外部动作边界（写进代码，也有测试）

**日程接受 ≠ 工具执行授权。** 本模块只动时间轴：不买票、不发消息、不调任何
连接器。用户说"去看电影，你买两张票"时，日程可以接受"去"，购票仍要走各自的
工具权限与人工确认。所以这里没有、也不该有任何 ticket/message 写入口。

## 频率与预算（设计 §0 的"临时重排"那一行）

- 同一 ``turn_id`` **只一次决定**：``schedule_change_requests`` 的
  ``UNIQUE(actor_id, source_turn_id)``；第二次直接返回原决定；
- 同一聊天窗口**每小时 ≤1 次**、全 actor **每天 ≤2 次**重排模型调用；
- 额度用满/预算耗尽时**仍然记录这次提议与 defer 决定**，并明确告诉用户
  "暂未改动"——不许假称同意（设计 §4）。

## 跨午夜

``[22:40, 01:10)`` 这种提议要落在**两份** ``day_plans`` 上。设计 §4 要求"分成
相邻两日的原子修订"：这里按 ``plan_day`` 升序取行锁（``FOR UPDATE``），把作用域
按本地午夜切格，再交给 ``timeline.replace_scope_multi``（一个 savepoint）。
其中一日不可改就整单回滚，于是"今天改了、明天没改"这种半截状态不可能出现。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.agenda.timeline import (
    MODEL_KINDS,
    Anchor,
    OutlineValidationError,
    ScopedDraft,
    ScopeEdit,
    ScopeError,
    ScopeReplacement,
    SegmentDraft,
    StaleRevisionError,
    _aware,
    _parse_iso_minute,
    _routine_snapshot,
    behavior_model_ready,
    call_behavior_model,
    day_behavior_call_totals,
    finish_behavior_call,
    load_commitment_anchors,
    load_day_plan,
    overlaps,
    plan_day_for,
    plan_day_window,
    replace_scope_multi,
    reserve_behavior_call,
    resolve_plan_timezone,
    scope_movable,
    sleep_anchors_for_day,
)
from ling.infra.models import (
    ActivityRow,
    BehaviorModelCallRow,
    DayPlanRow,
    OutboxEvent,
    PresenceState,
    ScheduleChangeRequestRow,
    ScheduleIntentionRow,
    ScheduleSegmentRow,
    utcnow,
)
from ling.infra.security import new_id

logger = logging.getLogger("ling.domains.agenda.replan")

# ---------------------------------------------------------------- 预算与频率
#
# 设计 §0 的"临时重排"一行：1,500 输入 / 800 输出、**2 次**、4,600 token
# per actor per 计划日。与大纲（2 次 / 7,000）、细化（4 次 / 7,200）各算各的账，
# 共用 `behavior_model_calls` 但按 `kind` 分开计。
REPLAN_KIND = "replan"
# 与大纲同一个职责：设计 §0 只有 `memory.extract` 一个行为模型职责，
# 不为重排另开槽位（那会让"配好模型却配漏一个职责"变成静默失败）。
REPLAN_PROFILE = "memory.extract"
REPLAN_MAX_CALLS_PER_DAY = 2
REPLAN_RESERVED_INPUT_TOKENS = 1500
REPLAN_RESERVED_OUTPUT_TOKENS = 800
REPLAN_DAILY_TOKEN_BUDGET = 4600
REPLAN_LEASE_SECONDS = 120.0

# 频率（设计 §4）：同一聊天窗口每小时最多一次**重排模型调用**；全 actor 每天
# 最多 2 次。它限制的是"改计划"，不是"回话"——她照样可以回应、拒绝、把提议
# 记下来（被记账的那一行就是"记下来了"）。
REPLAN_WINDOW_HOURLY_MAX = 1
REPLAN_WINDOW_HOURS = 1
REPLAN_DAILY_MAX = 2

# 作用域太小就不改：为塞一段 5 分钟的空档去重排一次时间轴，是拿成本换噪声。
REPLAN_MIN_SCOPE_MINUTES = 20
# 一次最多换几段（模型输出上限，也是"局部"的护栏）。
REPLAN_MAX_SEGMENTS = 4
REPLAN_TITLE_CHARS = 60
REPLAN_REASON_CHARS = 48
# prompt 里最多列几段现有安排 / 几条锚点：局部重排的输入必须**有界**。
REPLAN_MAX_INPUT_SEGMENTS = 8
REPLAN_MAX_INPUT_ANCHORS = 6
# 只影响**可选外出**的体力阈值（设计 §4：精力/饱腹只限制可选活动，绝不能用来
# 拒绝聊天/隐私/承诺）。工程初值，不是医学结论。
REPLAN_MIN_ENERGY = 25
REPLAN_MIN_SATIETY = 20
# 一段提议的默认时长（话里没给结束时间时用）：只决定**作用域有多宽**，
# 不写进任何段——段的时长一律由模型给、由代码校验。
REPLAN_DEFAULT_WINDOW_MINUTES = 120

# ---------------------------------------------------------------- 原因码
#
# 一律落 `schedule_change_requests.reason_code`（唯一的决定记录处）。
# 不写 `day_plans.last_error_code`：那个字段是"生成失败/降级/待复核"，
# 往里写"这次提议被 defer"会让只读时间轴把整天显示成 failed。
REASON_NO_PROPOSAL = "no_proposal"
REASON_DUPLICATE_TURN = "already_decided"
REASON_IN_PROGRESS = "in_progress"
REASON_AMBIGUOUS_QUESTION = "ambiguous_question"
REASON_AMBIGUOUS_NEGATION = "ambiguous_negation"
REASON_AMBIGUOUS_HYPOTHETICAL = "ambiguous_hypothetical"
REASON_PLAN_MISSING = "plan_missing"
REASON_SLEEP_WINDOW = "sleep_window"
REASON_REST_WINDOW = "rest_window"
REASON_ACTIVITY_RUNNING = "activity_running"
REASON_COMMITMENT_CONFLICT = "commitment_conflict"
REASON_LOW_ENERGY = "low_energy"
REASON_SCOPE_TOO_SMALL = "scope_too_small"
REASON_CROSS_MIDNIGHT_MISSING = "cross_midnight_plan_missing"
REASON_WINDOW_HOURLY_LIMIT = "window_hourly_limit"
REASON_DAILY_LIMIT = "daily_limit"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_MODEL_INVALID = "model_invalid"
REASON_MODEL_ERROR = "model_error"
REASON_EVENT_APPLIED = "replan_applied"
REASON_STALE_REVISION = "stale_revision"

# 决定枚举（与 `schedule_change_requests` 的 CHECK 一致）。
DECISIONS: tuple[str, ...] = ("accept", "decline", "defer")

# ---------------------------------------------------------------- 识别层
#
# **关键词只是候选。** 命中之后仍要过硬门、仍要模型说"愿意去"；命中本身不等于
# 用户确认，也不等于她会答应。这里刻意只收"明确邀请"的常见说法：宁可漏掉
# （她照常聊天，计划不动），也不要把"我昨天看电影了"这类陈述误判成改计划。
PROPOSAL_PHRASES: tuple[tuple[str, str], ...] = (
    ("看电影", "movie"),
    ("看场电影", "movie"),
    ("看展", "outing"),
    ("看演出", "outing"),
    ("听演唱会", "outing"),
    ("一起吃饭", "meal"),
    ("出去吃", "meal"),
    ("出去转转", "outing"),
    ("出去玩", "outing"),
    ("出去走走", "outing"),
    ("去散步", "walk"),
    ("逛逛", "outing"),
    ("喝一杯", "outing"),
    ("一起打游戏", "together"),
)
# 可选**外出**类别：只有它们受体力/饱腹约束。聊天、隐私、承诺永不受。
_OPTIONAL_OUTDOOR_KINDS: frozenset[str] = frozenset(
    {"movie", "outing", "meal", "walk", "together"}
)
# 问句标记：出现就按"歧义"处理（defer，不改计划）。
_QUESTION_MARKERS: tuple[str, ...] = (
    "？",
    "?",
    "要不要",
    "去不去",
    "好不好",
    "行不行",
    "是不是",
    "吗",
    "么",
)
# 否定/取消标记。
_NEGATION_MARKERS: tuple[str, ...] = (
    "不去了",
    "不想去",
    "不去",
    "算了",
    "别去",
    "没空",
    "取消",
    "再说吧",
)
# 假设/将来条件标记：**今天**的计划不该因为这些标记改动。
_HYPOTHETICAL_MARKERS: tuple[str, ...] = (
    "如果",
    "要是",
    "假如",
    "下次",
    "改天",
    "以后",
    "哪天",
    "可能会",
    "也许",
    "或许",
)

# 从话里抽"她要在哪个点做什么"：**只认数字钟点**（``19:00`` / ``19点`` / ``19点半``）。
# 中文数字（"七点"）不支持——宁可解析不出来（作用域退回默认范围），也不猜。
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)")
_OCLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*点\s*(半|\d{1,2}\s*分)?")


@dataclass(frozen=True)
class ProposalSignal:
    """一次"值得按提议处理"的候选（**还不是**决定）。

    ``source`` 说明它从哪儿来：``"keyword"`` 是关键词候选，``"intention"`` 是
    结构化信号（已有的意向链路里的事件）。结构化信号优先——它是权威来源，
    不需要再靠关键词猜。
    """

    kind: str
    phrase: str
    source: str = "keyword"
    source_ref: str | None = None
    interrogative: bool = False
    negated: bool = False
    hypothetical: bool = False
    requested_start: datetime | None = None
    requested_end: datetime | None = None

    @property
    def outdoor(self) -> bool:
        """是不是"可选外出"（只有这类才受体力/饱腹约束）。"""
        return self.kind in _OPTIONAL_OUTDOOR_KINDS

    @property
    def ambiguity(self) -> str | None:
        """歧义原因码；不是歧义返回 ``None``。

        顺序有讲究：先问句、再否定、最后假设。"去不去"里含"不去"，先判否定
        会把它误记成"她拒绝了"——那是两件完全不同的事。
        """
        if self.interrogative:
            return REASON_AMBIGUOUS_QUESTION
        if self.negated:
            return REASON_AMBIGUOUS_NEGATION
        if self.hypothetical:
            return REASON_AMBIGUOUS_HYPOTHETICAL
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "source_ref": self.source_ref,
            "ambiguous": self.ambiguity,
            "requested_start": (
                self.requested_start.isoformat() if self.requested_start else None
            ),
            "requested_end": (
                self.requested_end.isoformat() if self.requested_end else None
            ),
        }


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def detect_proposal(text: str) -> ProposalSignal | None:
    """纯函数：这段话里有没有**候选**提议？歧义标记一并标出来。

    返回 ``None`` 表示没有候选（正常聊天，什么都不做）；返回
    :class:`ProposalSignal` 表示有候选，**但还不代表确认**——硬门与模型在后面。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # 只看最后 200 字：一句长消息里真正"在邀请"的通常是最后那句。
    body = raw[-200:]
    for phrase, kind in PROPOSAL_PHRASES:
        if phrase not in body:
            continue
        return ProposalSignal(
            kind=kind,
            phrase=phrase,
            interrogative=_has_any(body, _QUESTION_MARKERS),
            negated=_has_any(body, _NEGATION_MARKERS),
            hypothetical=_has_any(body, _HYPOTHETICAL_MARKERS),
        )
    return None


def parse_requested_window(
    text: str, *, plan_day: date, timezone: str
) -> tuple[datetime, datetime] | None:
    """从话里抽"她指的时间段"（只认数字钟点；抽不到返回 ``None``）。

    抽不到不是错误：作用域退回设计 §4 的默认范围
    ``[max(now, 起点), 下一个不可移动锚点)``。抽到了就**收窄**作用域——
    用户明确说了 19:00，就没有理由去动 21:00 的安排。
    """
    from ling.domains.agenda.timeline import local_minute_instant

    raw = text or ""
    minute: int | None = None
    match = _CLOCK_RE.search(raw)
    if match is not None:
        hour, minute_part = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute_part <= 59:
            minute = hour * 60 + minute_part
    if minute is None:
        match = _OCLOCK_RE.search(raw)
        if match is not None:
            hour = int(match.group(1))
            suffix = (match.group(2) or "").strip()
            if suffix == "半":
                offset = 30
            elif suffix:
                offset = int(suffix.replace("分", "").strip() or 0)
            else:
                offset = 0
            if 0 <= hour <= 23 and 0 <= offset < 60:
                minute = hour * 60 + offset
    if minute is None:
        return None
    start = local_minute_instant(plan_day, minute, timezone)
    return start, start + timedelta(minutes=REPLAN_DEFAULT_WINDOW_MINUTES)


async def structured_proposal(
    session: AsyncSession, *, actor_id: str, turn_id: str
) -> ProposalSignal | None:
    """已经结构化的信号优先：这一轮有没有落一条**指向它的意向**。

    设计 §4 的链路起点是"意图提取为提议（不自动承诺）"。当上游（意图接入层）
    已经按 ``source_ref = turn:<turn_id>`` 写下一条 ``proposed``/``planned``
    意向时，它就是权威候选，不必再靠关键词猜——"想去看那个电影"没有命中任何
    关键词，意思却是明确的。

    今天**还没有**写入方按这个形状落意向（1.75a 的确认/撤回链路先落地），
    所以这条查询多数时候返回 ``None``，由关键词候选兜底。这里不猜不存在的行：
    查不到就是查不到。
    """
    row = (
        await session.execute(
            select(ScheduleIntentionRow)
            .where(
                ScheduleIntentionRow.actor_id == actor_id,
                ScheduleIntentionRow.source_ref == f"turn:{turn_id}",
                ScheduleIntentionRow.state.in_(("proposed", "planned")),
            )
            .order_by(ScheduleIntentionRow.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return ProposalSignal(
        kind=str(row.kind),
        phrase=str(row.title)[:REPLAN_TITLE_CHARS],
        source="intention",
        source_ref=f"intention:{row.id}",
    )


async def note_schedule_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    window_id: str | None,
    turn_id: str,
    text: str,
    now: datetime | None = None,
) -> str | None:
    """会话层的**唯一**接入点：命中候选就投一条 ``schedule.replan``。

    只做两件事：候选判定（纯函数）+ 幂等投递。**不改任何计划、不调模型**：
    真正的判定在 worker 里（硬门 → 意愿 → 重新校验 → 提交），在这里抢先动手
    就等于让"聊天里提一句"直接改时间轴。

    幂等键是 ``replan:<turn_id>``：同一轮重复落库（重试、双写）只会有一条事件。
    **不把聊天原文塞进 payload**：worker 按 ``turn_id`` 回读那一条消息即可，
    outbox 里少存一份正文就少一处泄漏面。
    """
    signal = detect_proposal(text)
    if signal is None:
        return None
    moment = _aware(now or utcnow())
    event_id = f"replan:{turn_id}"
    if await session.get(OutboxEvent, event_id) is not None:
        return None
    try:
        async with session.begin_nested():
            session.add(
                OutboxEvent(
                    id=event_id,
                    topic="schedule.replan",
                    payload={
                        "actor_id": actor_id,
                        "turn_id": turn_id,
                        "window_id": window_id or "",
                        "signal_kind": signal.kind,
                        "enqueued_at": moment.isoformat(),
                    },
                    available_at=moment,
                    created_at=moment,
                )
            )
            await session.flush()
    except IntegrityError:
        # 另一处同时投了同一条：主键兜底，不是错误。
        return None
    return event_id


async def turn_user_text(session: AsyncSession, *, turn_id: str) -> str:
    """回读这一轮用户说的话（worker 侧用；outbox 里因此不必存正文）。"""
    from ling.infra.models import Message

    rows = (
        await session.execute(
            select(Message.text)
            .where(Message.turn_id == turn_id, Message.role == "user")
            .order_by(Message.sequence)
        )
    ).all()
    return "\n".join(str(text) for (text,) in rows if text)


# ---------------------------------------------------------------- 硬门
#
# 这一层**全部是代码**，模型碰不到。顺序即设计 §4 列的顺序。


@dataclass(frozen=True)
class GateVerdict:
    """硬门的结论。``ok=False`` 时 ``decision`` 说明该说"拒绝"还是"暂缓"。"""

    ok: bool
    reason_code: str | None = None
    decision: str = "defer"
    detail: str = ""
    plan_day: date | None = None
    plan_id: str | None = None
    scope_start: datetime | None = None
    scope_end: datetime | None = None
    anchors: tuple[Anchor, ...] = ()
    # 作用域内**会被替换**的段（给 prompt，也给"这次改了哪几段"的审计）。
    targets: tuple[ScheduleSegmentRow, ...] = ()
    # 作用域内**不能换**的段：已开始（历史/进行中）或不可移动（睡眠/承诺）。
    # 必须告诉她，否则模型会往这一段上排东西，然后被代码拒收（白调一次）。
    blocked: tuple[ScheduleSegmentRow, ...] = ()
    # 作用域覆盖到的计划日（跨午夜时两格，按升序）。
    days: tuple[date, ...] = ()
    # 判定时用的**计划时区**：提交时按午夜切格必须用它，不能用另一个
    # （两套时区会切出不同的日界，跨午夜那一格就对不上了）。
    plan_timezone: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason_code": self.reason_code,
            "decision": self.decision,
            "plan_day": self.plan_day.isoformat() if self.plan_day else None,
            "scope_start": self.scope_start.isoformat() if self.scope_start else None,
            "scope_end": self.scope_end.isoformat() if self.scope_end else None,
            "targets": [str(item.id) for item in self.targets],
            "blocked": [str(item.id) for item in self.blocked],
            "days": [item.isoformat() for item in self.days],
        }


def _minutes_between(start: datetime, end: datetime) -> int:
    return max(0, int((_aware(end) - _aware(start)).total_seconds() // 60))


def _within(moment: datetime, anchor: Anchor) -> bool:
    return _aware(anchor.start_at) <= _aware(moment) < _aware(anchor.end_at)


async def _running_activity(
    session: AsyncSession, *, actor_id: str
) -> ActivityRow | None:
    return (
        await session.execute(
            select(ActivityRow)
            .where(ActivityRow.actor_id == actor_id, ActivityRow.state == "running")
            .order_by(ActivityRow.started_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _presence(session: AsyncSession, *, actor_id: str) -> tuple[int, int] | None:
    row = await session.get(PresenceState, actor_id)
    if row is None:
        return None
    return int(row.energy), int(row.satiety)


async def _immovable_starts(
    session: AsyncSession,
    *,
    row: DayPlanRow,
    after: datetime,
    anchors: tuple[Anchor, ...],
) -> list[datetime]:
    """这份计划里 ``after`` 之后的"不可移动边界（起点）"们。

    两类来源合起来才完整：**段**（``anchor_kind != ''`` 的睡眠/承诺段、
    ``commitment_level=confirmed``）与**锚点**（``CommitmentRow`` / confirmed
    ``agenda_plans`` 的保护窗，见 ``timeline.load_commitment_anchors``）。
    只看段会漏掉"还没落成段的新承诺"，只看锚点会漏掉"预计睡眠"。
    """
    window_start = _aware(row.window_start_at)
    window_end = _aware(row.window_end_at)
    starts: list[datetime] = []
    rows = list(
        (
            await session.execute(
                select(ScheduleSegmentRow).where(
                    ScheduleSegmentRow.plan_id == str(row.id),
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.end_at > window_start,
                    ScheduleSegmentRow.start_at < window_end,
                )
            )
        ).scalars()
    )
    for segment in rows:
        if scope_movable(segment):
            continue
        start = _aware(segment.start_at)
        if start > after:
            starts.append(start)
    for anchor in anchors:
        if _aware(anchor.start_at) > after:
            starts.append(_aware(anchor.start_at))
    return sorted(set(starts))


def _split_days(
    start: datetime, end: datetime, timezone: str
) -> list[tuple[date, datetime, datetime]]:
    """把 ``[start, end)`` 按本地午夜切成逐日窗口（左闭右开）。"""
    pieces: list[tuple[date, datetime, datetime]] = []
    cursor = _aware(start)
    guard = 0
    while cursor < _aware(end) and guard < 7:
        guard += 1
        day = plan_day_for(cursor, timezone)
        _day_start, day_end = plan_day_window(day, timezone)
        piece_end = min(_aware(end), _aware(day_end))
        if piece_end <= cursor:
            break
        pieces.append((day, cursor, piece_end))
        cursor = piece_end
    return pieces


async def _scope_targets(
    session: AsyncSession,
    *,
    plan_ids: list[str],
    scope_start: datetime,
    scope_end: datetime,
    now: datetime,
) -> tuple[tuple[ScheduleSegmentRow, ...], tuple[ScheduleSegmentRow, ...]]:
    """作用域内的段分成两堆：``(会被替换的, 不能换的)``。

    判定与 ``timeline._replace_scope_in_txn`` 保持一致（可动 + 未开始 + 相交），
    否则 prompt 里给她看的"现有安排"和真正会被换掉的段会是两批。第二堆
    （已开始的历史/进行中、以及睡眠与 confirmed 段）要**明确告诉她不能碰**，
    这样她就不会把新安排直接压在正在吃的晚饭上、再被代码拒收。
    """
    ids = [item for item in plan_ids if item]
    if not ids:
        return (), ()
    rows = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id.in_(ids),
                    ScheduleSegmentRow.status == "current",
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars()
    )
    floor = _aware(now)
    inside = [
        item
        for item in rows
        if overlaps(
            _aware(item.start_at), _aware(item.end_at), scope_start, scope_end
        )
    ]
    replaceable = tuple(
        item
        for item in inside
        if scope_movable(item) and _aware(item.start_at) >= floor
    )
    blocked = tuple(item for item in inside if item not in replaceable)
    return replaceable, blocked


async def evaluate_gates(
    session: AsyncSession,
    *,
    actor_id: str,
    signal: ProposalSignal,
    now: datetime,
    timezone: str,
) -> GateVerdict:
    """硬门：**代码**说能不能改，模型没有发言权。

    逐项（顺序即设计 §4 的顺序，越前面越"不能商量"）：

    1. 歧义（问句/否定/假设）→ defer，**不改计划**，也还没到模型；
    2. 计划行不存在 / 计划日已结束 → defer；
    3. **睡眠窗内**（作息 ``rest_minute``→``wake_minute`` 求交，一天至多两段）
       → decline：她睡了，不因为她睡着就给她排一段外出；
    4. 此刻正在**已确认承诺**的保护窗里 → decline ``commitment_conflict``；
       请求的时间撞承诺 → decline；撞**预计睡眠** → decline ``rest_window``
       （可选活动不能压缩预计睡眠，设计 §4）；
    5. 有**进行中**（``activities.state=running``）的活动 → decline：那是正在
       发生的事实，重排不能把它抹掉；
    6. 体力/饱腹：**只对可选外出**生效。聊天、隐私、承诺**一律不看**这两项——
       模拟状态永远不能变成拒绝用户的理由；
    7. 作用域太小（< ``REPLAN_MIN_SCOPE_MINUTES``）→ defer；
    8. 频率与预算 → 见 :func:`check_limits`（这里不查：预算要按 ``plan_day``
       和窗口 id 才查得到）。
    """
    moment = _aware(now)
    ambiguity = signal.ambiguity
    if ambiguity is not None:
        return GateVerdict(
            ok=False,
            reason_code=ambiguity,
            decision="defer",
            detail="话里有歧义（问句/否定/假设），先不改计划",
        )

    plan_day = plan_day_for(moment, timezone)
    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    if row is None:
        return GateVerdict(
            ok=False,
            reason_code=REASON_PLAN_MISSING,
            decision="defer",
            detail="这一计划日还没有计划行，没有可改的时间轴",
            plan_day=plan_day,
        )
    plan_timezone = str(row.timezone or timezone)
    window_start = _aware(row.window_start_at)
    window_end = _aware(row.window_end_at)
    if moment >= window_end:
        return GateVerdict(
            ok=False,
            reason_code=REASON_PLAN_MISSING,
            decision="defer",
            detail="这一计划日已经结束",
            plan_day=plan_day,
            plan_id=str(row.id),
            days=(plan_day,),
        )

    routine = await _routine_snapshot(session, actor_id)
    sleep_anchors = tuple(sleep_anchors_for_day(plan_day, plan_timezone, routine))
    next_day = plan_day + timedelta(days=1)
    next_row = await load_day_plan(session, actor_id=actor_id, plan_day=next_day)
    next_sleep = tuple(sleep_anchors_for_day(next_day, plan_timezone, routine))

    # ③ 睡眠窗内 → 直接拒绝（最有说服力的"她不在"）。
    if any(_within(moment, anchor) for anchor in sleep_anchors):
        return GateVerdict(
            ok=False,
            reason_code=REASON_SLEEP_WINDOW,
            decision="decline",
            detail="现在在她的预计睡眠窗里",
            plan_day=plan_day,
            plan_id=str(row.id),
            days=(plan_day,),
        )

    # ④ 承诺锚点：今天到明天的已确认约定（含保护窗）。
    horizon_end = _aware(next_row.window_end_at) if next_row is not None else window_end
    anchors = tuple(
        await load_commitment_anchors(
            session,
            actor_id=actor_id,
            window_start=window_start,
            window_end=horizon_end,
        )
    )
    for anchor in anchors:
        if _within(moment, anchor):
            return GateVerdict(
                ok=False,
                reason_code=REASON_COMMITMENT_CONFLICT,
                decision="decline",
                detail="此刻正在一个已确认承诺的保护窗里，重排不能把它挤走",
                plan_day=plan_day,
                plan_id=str(row.id),
                anchors=anchors,
                days=(plan_day,),
            )

    # ④b 正在进行的**不可移动段**（confirmed / 睡眠）：与锚点同理，方向反过来。
    # 只查锚点会漏掉"已经落成段的确认约定"：它盖住此刻时，这一段重排必须在
    # 模型之前就拒掉，而不是等提交前复核才发现（那会白调一次模型）。
    covering = (
        await session.execute(
            select(ScheduleSegmentRow).where(
                ScheduleSegmentRow.plan_id == str(row.id),
                ScheduleSegmentRow.status == "current",
                ScheduleSegmentRow.start_at <= moment,
                ScheduleSegmentRow.end_at > moment,
            )
        )
    ).scalars()
    for segment in covering:
        if scope_movable(segment):
            continue
        return GateVerdict(
            ok=False,
            reason_code=(
                REASON_REST_WINDOW
                if str(segment.anchor_kind) == "sleep"
                else REASON_COMMITMENT_CONFLICT
            ),
            decision="decline",
            detail="此刻正处在一段不可移动的已确认安排里",
            plan_day=plan_day,
            plan_id=str(row.id),
            anchors=anchors,
            days=(plan_day,),
        )

    # ⑤ 进行中的活动。
    if await _running_activity(session, actor_id=actor_id) is not None:
        return GateVerdict(
            ok=False,
            reason_code=REASON_ACTIVITY_RUNNING,
            decision="decline",
            detail="有活动正在进行，重排不覆盖正在发生的事实",
            plan_day=plan_day,
            plan_id=str(row.id),
            anchors=anchors,
            days=(plan_day,),
        )

    # 作用域：默认 `[max(now, 窗口起点), 下一个不可移动锚点)`，最远到次日窗口末。
    scope_start = max(moment, window_start)
    edges = await _immovable_starts(
        session, row=row, after=scope_start, anchors=anchors
    )
    if next_row is not None:
        edges += await _immovable_starts(
            session, row=next_row, after=scope_start, anchors=anchors
        )
    edges += [a.start_at for a in (*sleep_anchors, *next_sleep) if a.start_at > scope_start]
    cap = horizon_end
    scope_end = min([*edges, cap])
    if scope_end <= scope_start:
        scope_end = cap

    # 用户给了时间就收窄到那一段（抽不到就用默认范围）。
    requested = (signal.requested_start, signal.requested_end)
    if requested[0] is not None and requested[1] is not None:
        req_start = _aware(requested[0])
        req_end = _aware(requested[1])
        for anchor in anchors:
            if overlaps(
                req_start, req_end, _aware(anchor.start_at), _aware(anchor.end_at)
            ):
                return GateVerdict(
                    ok=False,
                    reason_code=REASON_COMMITMENT_CONFLICT,
                    decision="decline",
                    detail="请求的时间与已确认承诺重叠",
                    plan_day=plan_day,
                    plan_id=str(row.id),
                    scope_start=req_start,
                    scope_end=req_end,
                    anchors=anchors,
                    days=(plan_day,),
                )
        for anchor in (*sleep_anchors, *next_sleep):
            if overlaps(
                req_start, req_end, _aware(anchor.start_at), _aware(anchor.end_at)
            ):
                return GateVerdict(
                    ok=False,
                    reason_code=REASON_REST_WINDOW,
                    decision="decline",
                    detail="请求的时间压到预计睡眠",
                    plan_day=plan_day,
                    plan_id=str(row.id),
                    scope_start=req_start,
                    scope_end=req_end,
                    anchors=anchors,
                    days=(plan_day,),
                )
        if req_end > scope_start and req_start < scope_end:
            scope_start = max(scope_start, req_start)
            scope_end = min(scope_end, req_end)

    if _minutes_between(scope_start, scope_end) < REPLAN_MIN_SCOPE_MINUTES:
        return GateVerdict(
            ok=False,
            reason_code=REASON_SCOPE_TOO_SMALL,
            decision="defer",
            detail="可改的范围太小：改一次不值当，也不该为了答应去挪别的安排",
            plan_day=plan_day,
            plan_id=str(row.id),
            scope_start=scope_start,
            scope_end=scope_end,
            anchors=anchors,
            days=(plan_day,),
        )

    # ⑥ 体力/饱腹：只对**可选外出**。聊天/隐私/承诺不看这两项。
    if signal.outdoor:
        presence = await _presence(session, actor_id=actor_id)
        if presence is not None:
            energy, satiety = presence
            if energy < REPLAN_MIN_ENERGY or satiety < REPLAN_MIN_SATIETY:
                return GateVerdict(
                    ok=False,
                    reason_code=REASON_LOW_ENERGY,
                    decision="decline",
                    detail="精力/饱腹过低，这次可选外出不合适（不影响聊天与承诺）",
                    plan_day=plan_day,
                    plan_id=str(row.id),
                    scope_start=scope_start,
                    scope_end=scope_end,
                    anchors=anchors,
                    days=(plan_day,),
                )

    days = _split_days(scope_start, scope_end, plan_timezone)
    if len(days) > 2:
        # 设计 §4 只给"相邻两日"。更长的跨日提议不在本阶段范围内：不做，
        # 也不假装做了——defer 并把原因写下来。
        return GateVerdict(
            ok=False,
            reason_code=REASON_SCOPE_TOO_SMALL,
            decision="defer",
            detail="跨超过两天的提议不在局部重排范围里",
            plan_day=plan_day,
            plan_id=str(row.id),
            scope_start=scope_start,
            scope_end=scope_end,
            anchors=anchors,
            days=tuple(day for day, _start, _end in days),
        )

    plan_ids = [
        str(row.id) if day == plan_day else str(next_row.id) if next_row is not None else ""
        for day, _start, _end in days
    ]
    targets, blocked = await _scope_targets(
        session,
        plan_ids=plan_ids,
        scope_start=scope_start,
        scope_end=scope_end,
        now=moment,
    )
    return GateVerdict(
        ok=True,
        plan_day=plan_day,
        plan_id=str(row.id),
        scope_start=scope_start,
        scope_end=scope_end,
        anchors=anchors,
        targets=targets,
        blocked=blocked,
        days=tuple(day for day, _start, _end in days),
        plan_timezone=plan_timezone,
    )


async def check_limits(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    window_id: str | None,
    now: datetime,
) -> GateVerdict | None:
    """频率与预算（设计 §0/§4）。超限返回一条 ``ok=False`` 的裁决。

    三个闸分开报，因为它们对用户说的话不一样：

    - 同一聊天窗口每小时 ≤1 次（**只**限制重排模型调用，不限制她回话）；
    - 全 actor 每天 ≤2 次；
    - 当日 token 预算（2 × 2300 = 4,600）。

    注意这些闸**不阻止**"记录这次提议"：调用方仍会写一行 ``defer``，用户看到
    "暂未改动"而不是"她没听见"。
    """
    moment = _aware(now)
    since = moment - timedelta(hours=REPLAN_WINDOW_HOURS)
    window_stmt = (
        select(func.count())
        .select_from(BehaviorModelCallRow)
        .where(
            BehaviorModelCallRow.actor_id == actor_id,
            BehaviorModelCallRow.kind == REPLAN_KIND,
            BehaviorModelCallRow.created_at >= since,
        )
    )
    # 没有窗口 id 的提议归到同一个桶里（它同样属于"同一个来源"）。
    window_stmt = window_stmt.where(
        BehaviorModelCallRow.window_id == (window_id or "")
    )
    used_in_window = int((await session.execute(window_stmt)).scalar_one() or 0)
    if used_in_window >= REPLAN_WINDOW_HOURLY_MAX:
        return GateVerdict(
            ok=False,
            reason_code=REASON_WINDOW_HOURLY_LIMIT,
            decision="defer",
            detail="这个聊天窗口一小时内已经重排过一次",
            plan_day=plan_day,
        )
    calls, spent = await day_behavior_call_totals(
        session, actor_id=actor_id, plan_day=plan_day, kind=REPLAN_KIND
    )
    if calls >= min(REPLAN_MAX_CALLS_PER_DAY, REPLAN_DAILY_MAX):
        return GateVerdict(
            ok=False,
            reason_code=REASON_DAILY_LIMIT,
            decision="defer",
            detail="今天已经重排过两次",
            plan_day=plan_day,
        )
    reserved = REPLAN_RESERVED_INPUT_TOKENS + REPLAN_RESERVED_OUTPUT_TOKENS
    if spent + reserved > REPLAN_DAILY_TOKEN_BUDGET:
        return GateVerdict(
            ok=False,
            reason_code=REASON_BUDGET_EXHAUSTED,
            decision="defer",
            detail="今天重排的 token 预算用完了",
            plan_day=plan_day,
        )
    return None


# ---------------------------------------------------------------- 模型输出

_ALLOWED_KEYS = {"decision", "reason_code", "proposed_segments"}
_ALLOWED_SEGMENT_KEYS = {"kind", "title", "start_at", "end_at", "place_id"}
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ReplanValidationError(ValueError):
    """模型输出不合约：带**逐条**原因（不带原文），供日志与"这次没改成"解释。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ReplanProposal:
    """模型解析后的结论：去不去（+ 怎么改）。"""

    decision: str
    reason_code: str | None = None
    segments: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "segments": len(self.segments),
        }


def parse_replan_output(raw: str) -> ReplanProposal:
    """严格解析：只接受 ``{"decision", "reason_code", "proposed_segments"}``。

    **不做宽容修**：宽容会把"模型没按契约输出"变成"我们猜它的意思"。
    """
    text = (raw or "").strip()
    if not text:
        raise ReplanValidationError(["输出为空"])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplanValidationError([f"不是合法 JSON（{exc.msg} @ {exc.pos}）"]) from exc
    if not isinstance(payload, dict):
        raise ReplanValidationError(["顶层必须是 JSON 对象"])
    extra = set(payload) - _ALLOWED_KEYS
    if extra:
        raise ReplanValidationError([f"顶层有未定义字段：{sorted(extra)}"])
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ReplanValidationError([f"decision 必须是 {DECISIONS} 之一：{decision!r}"])
    reason = payload.get("reason_code")
    if reason is not None:
        if not isinstance(reason, str) or not _REASON_RE.match(reason.strip()):
            raise ReplanValidationError(["reason_code 必须是短蛇形标识（小写字母/数字/_）"])
        if len(reason.strip()) > REPLAN_REASON_CHARS:
            raise ReplanValidationError(
                [f"reason_code 不能超过 {REPLAN_REASON_CHARS} 个字符"]
            )
    segments = payload.get("proposed_segments", [])
    if not isinstance(segments, list):
        raise ReplanValidationError(["proposed_segments 必须是数组"])
    return ReplanProposal(
        decision=decision,
        reason_code=(reason.strip() if isinstance(reason, str) else None),
        segments=tuple(item for item in segments if isinstance(item, dict)),
    )


def validate_replan_segments(
    segments: tuple[dict[str, object], ...],
    *,
    scope_start: datetime,
    scope_end: datetime,
    valid_place_ids: frozenset[str] = frozenset(),
    decision: str = "accept",
) -> list[ScopedDraft]:
    """把模型给的段校验成草稿：**越界/冲突/坏字段一律整份拒收**。

    - ``kind`` 只能在 ``MODEL_KINDS`` 里（**不含 sleep**：睡眠只能由作息锚点产生）；
    - 起止必须带时区偏移、精确到分钟、``start < end``；
    - 必须完全落在作用域内（越界不裁剪——裁剪等于替模型编内容）；
    - 不许跨本地午夜（段从不跨午夜，跨日部分必须拆成两段落在两份计划上）；
    - ``accept`` 至少一段；``decline``/``defer`` 必须没有段（拒绝了还改时间轴，
      是自相矛盾）。
    """
    problems: list[str] = []
    if decision != "accept":
        if segments:
            raise ReplanValidationError(
                [f"decision={decision} 时 proposed_segments 必须为空"]
            )
        return []
    if not segments:
        raise ReplanValidationError(["decision=accept 但没给任何段"])
    if len(segments) > REPLAN_MAX_SEGMENTS:
        problems.append(f"段数 {len(segments)} 超过上限 {REPLAN_MAX_SEGMENTS}")

    drafts: list[ScopedDraft] = []
    for index, raw in enumerate(segments):
        label = f"proposed_segments[{index}]"
        unknown = set(raw) - _ALLOWED_SEGMENT_KEYS
        if unknown:
            problems.append(f"{label} 有未定义字段：{sorted(unknown)}")
            continue
        kind = raw.get("kind")
        title = raw.get("title")
        if not isinstance(kind, str) or kind not in MODEL_KINDS:
            problems.append(f"{label}.kind 不在白名单：{kind!r}")
            continue
        if not isinstance(title, str):
            problems.append(f"{label}.title 必须是字符串")
            continue
        clean_title = " ".join(title.split())
        if not clean_title or len(clean_title) > REPLAN_TITLE_CHARS:
            problems.append(f"{label}.title 长度必须在 1..{REPLAN_TITLE_CHARS}")
            continue
        try:
            start = _parse_iso_minute(raw.get("start_at"), f"{label}.start_at")
            end = _parse_iso_minute(raw.get("end_at"), f"{label}.end_at")
        except OutlineValidationError as exc:
            problems.extend(exc.problems)
            continue
        if end <= start:
            problems.append(f"{label} 的 end_at 必须晚于 start_at")
            continue
        if start < _aware(scope_start) or end > _aware(scope_end):
            problems.append(
                f"{label} 越出了可改范围 "
                f"[{_aware(scope_start).isoformat()}, {_aware(scope_end).isoformat()})"
            )
            continue
        place = raw.get("place_id")
        if place is not None and (
            not isinstance(place, str) or place not in valid_place_ids
        ):
            problems.append(f"{label}.place_id 是未知地点：{place!r}")
            continue
        drafts.append(
            ScopedDraft(
                draft=SegmentDraft(
                    kind=kind,
                    title=clean_title,
                    start_at=start,
                    end_at=end,
                    place_id=place if isinstance(place, str) else None,
                    layer="block",
                    is_leaf=True,
                    source="model",
                )
            )
        )
    if problems:
        raise ReplanValidationError(problems)
    return drafts


def build_replan_prompt(
    *,
    signal: ProposalSignal,
    verdict: GateVerdict,
    now: datetime,
    timezone: str,
    presence: tuple[int, int] | None,
    valid_places: list[dict[str, str]] | None = None,
) -> str:
    """拼一次局部重排的 prompt（**有界**，不整段转发聊天原文）。

    设计 §3 对行为模型 prompt 的要求同样适用于这里：固定模板 + 结构化输入，
    只把"她要在哪个范围改什么"给她看。用户的整段话**不进** prompt——进来的是
    识别层抽出的类别与短片段（≤60 字），这样既够判断"愿不愿去"，也不把
    聊天隐私搬进模型审计链。
    """
    scope_start = verdict.scope_start or now
    scope_end = verdict.scope_end or now
    lines: list[str] = [
        "你在扮演一个有自己的生活节奏的角色。现在聊天里出现了一次临时邀请，",
        "你要先决定**去不去**，再决定**去的话把接下来这段时间怎么改**。",
        "",
        f"## 现在\n- 现在：{_aware(now).astimezone(UTC).isoformat()}（时区 {timezone}）",
        f"- 计划日：{verdict.plan_day.isoformat() if verdict.plan_day else '未知'}",
        f"- 邀请类别：{signal.kind}；话里的大意：「{signal.phrase}」",
        f"- 可改范围：[{_aware(scope_start).isoformat()}, {_aware(scope_end).isoformat()})"
        "（到下一个不可移动的安排为止）",
    ]
    if presence is not None:
        lines.append(f"- 你的状态：精力 {presence[0]}/100，饱腹 {presence[1]}/100")
    lines.append("")
    lines.append("## 可改范围内现在的安排（可以换掉）")
    targets = list(verdict.targets)[:REPLAN_MAX_INPUT_SEGMENTS]
    if targets:
        for item in targets:
            lines.append(
                f"- {_aware(item.start_at).isoformat()} → {_aware(item.end_at).isoformat()}"
                f" {item.title}（{item.kind}）"
            )
    else:
        lines.append("- （空档，没有已安排的事）")
    lines.append("")
    lines.append("## 不可移动（绝对不能压上去，也不能改）")
    anchors = list(verdict.anchors)[:REPLAN_MAX_INPUT_ANCHORS]
    if anchors:
        for item in anchors:
            lines.append(
                f"- {_aware(item.start_at).isoformat()} → {_aware(item.end_at).isoformat()}"
                f" {item.title}"
            )
    for item in list(verdict.blocked)[:REPLAN_MAX_INPUT_SEGMENTS]:
        lines.append(
            f"- {_aware(item.start_at).isoformat()} → {_aware(item.end_at).isoformat()}"
            f" {item.title}（{item.kind}，已开始或已确认，不能碰）"
        )
    if not anchors and not verdict.blocked:
        lines.append("- （这一带没有已确认的约定）")
    lines.append("")
    if valid_places:
        names = "、".join(str(item["name"]) for item in valid_places[:12])
        lines.append(f"## 可用地点\n- {names}")
        lines.append("")
    lines.extend(
        [
            "## 输出（严格 JSON，不要多余文字、不要代码块）",
            '{"decision": "accept|decline|defer", "reason_code": "短蛇形标识",',
            ' "proposed_segments": [{"kind": "leisure", "title": "看电影",',
            '  "start_at": "…带时区偏移…", "end_at": "…带时区偏移…", "place_id": null}]}',
            "",
            "规则：",
            "- 只在可改范围内改；越界的段会被代码**拒收**（不是商量，也不是裁剪）。",
            "- 时间左闭右开、带时区偏移、精确到分钟；不能跨本地午夜。",
            f"- kind 只能是：{', '.join(sorted(MODEL_KINDS))}。不要写 sleep。",
            "- accept 至少给 1 段；decline/defer 时 proposed_segments 必须是空数组。",
            "- 你不能改作息、不能改承诺、不能把计划说成已经发生的事。",
            "- 不想去就直接 decline 并给原因；不为了迎合而答应。",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 决定记录


@dataclass(frozen=True)
class ReplanOutcome:
    """一次重排作业的结论（不含正文，可直接进日志/审计）。"""

    state: str  # no_proposal | duplicate | deferred | declined | accepted
    turn_id: str
    decision: str | None = None
    reason_code: str | None = None
    plan_day: date | None = None
    plan_id: str | None = None
    revision_before: int | None = None
    revision_after: int | None = None
    scope_start: datetime | None = None
    scope_end: datetime | None = None
    superseded: int = 0
    created: int = 0
    calls: int = 0
    notice: str = ""
    signal: ProposalSignal | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "turn_id": self.turn_id,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "plan_day": self.plan_day.isoformat() if self.plan_day else None,
            "plan_id": self.plan_id,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "scope_start": self.scope_start.isoformat() if self.scope_start else None,
            "scope_end": self.scope_end.isoformat() if self.scope_end else None,
            "superseded": self.superseded,
            "created": self.created,
            "calls": self.calls,
            "signal": self.signal.as_dict() if self.signal else None,
        }


def _notice(decision: str | None, reason_code: str | None) -> str:
    """给用户看的一句话（不含原文、不含内部 id）。

    设计 §4："超预算后用确定性答复'现在不重新安排/晚些再决定'，不得假称同意"。

    **这句话是审计用的**：它把原因码原样带出来（``这次不去了（sleep_window）``），
    便于在行为日志里回答"为什么没改"。真正说给用户听的那一句在
    :func:`notice_for`（原因码不进聊天）。
    """
    if decision == "accept":
        return "好，那就去；时间轴已经改过来了。"
    if decision == "decline":
        return f"这次不去了（{reason_code or '不想去'}），安排不变。"
    return f"现在不重新安排（{reason_code or '时机不合适'}），今天的时间轴没有改动。"


# ---------------------------------------------------------------- 回一句话
#
# 缺口（设计 §4 把这条列为**最大的产品级缺口**）：一次临时提议被拒绝或被推迟
# 时，原来只往 `schedule_change_requests` 与行为日志里记一笔，用户那边**一点
# 回话都没有**——提议像掉进了黑洞，看起来和"她没听见"一模一样。
#
# 下面这段文案**全部是代码固定的**，三条理由：
#
# 1. **回一句"这次没改"不需要调模型**：为一句固定短话花一次调用、一份预算，
#    还会让同一句话每次都不同；
# 2. **也不能让模型自由发挥**：它很容易顺势承诺"明天一定去"——**计划不是
#    事实**，没排进时间轴的事不能先说出来；
# 3. **不提预算/额度/token**：用户关心的是"安排动了没有"，不是我们的账本；
#    说出账本也解释不了为什么"这次"不行。
REPLAN_NOTICE_MAX_CHARS = 30

# 每个原因码一句。**同一族的门用同一句话**：频率/预算那三个闸、模型那几个
# 故障，对用户来说是同一件事（"今天不重排"/"这次没改成"），分开说只会把
# 内部机制漏进聊天。
REPLAN_NOTICE_TEXTS: dict[str, str] = {
    # —— 硬门：她自己此刻不可用（不是不想理你）——
    REASON_SLEEP_WINDOW: "这个点我该睡了，今天先不折腾",
    REASON_REST_WINDOW: "那个点我该睡了，先不动了",
    REASON_COMMITMENT_CONFLICT: "那个时间我答应过别的事，不挪了",
    REASON_ACTIVITY_RUNNING: "手上这件事还没完，先不改安排",
    REASON_LOW_ENERGY: "今天有点累，不想再挪了",
    # —— 硬门：这句话还不算"要改计划"（问句/否定/假设）——
    REASON_AMBIGUOUS_QUESTION: "这句我先当聊天，安排不动",
    REASON_AMBIGUOUS_NEGATION: "这句我先当聊天，安排不动",
    REASON_AMBIGUOUS_HYPOTHETICAL: "这句我先当聊天，安排不动",
    # —— 改不动：没有可改的时间轴、空档太小、跨日超出局部范围 ——
    REASON_PLAN_MISSING: "今天的安排还没排好，先不改了",
    REASON_CROSS_MIDNIGHT_MISSING: "另一天的安排还没排好，先不改了",
    REASON_SCOPE_TOO_SMALL: "这会儿腾不出空档，先不改了",
    # —— 频率与预算：对用户是同一件事 ——
    REASON_WINDOW_HOURLY_LIMIT: "今天安排就不动了",
    REASON_DAILY_LIMIT: "今天安排就不动了",
    REASON_BUDGET_EXHAUSTED: "今天安排就不动了",
    # —— 模型侧故障 / 版本冲突：对用户也是同一件事 ——
    REASON_MODEL_UNAVAILABLE: "这次没改成，回头再说",
    REASON_MODEL_INVALID: "这次没改成，回头再说",
    REASON_MODEL_ERROR: "这次没改成，回头再说",
    REASON_STALE_REVISION: "这次没改成，回头再说",
}

# 原因码落在上表之外时怎么办。两种结局**必须分开**：
# - ``declined`` 是"她不想去"（例如模型自己 decline 并给了自己的原因码），
#   说"没改成"会把她的选择说成技术故障；
# - ``deferred`` 是"现在动不了"，说"不去了"等于替她拒绝。
# 模型给的 ``reason_code`` 是任意短标识，我们**不猜它什么意思**，走兜底。
_NOTICE_DECLINED_FALLBACK = "这次就不去了，安排照旧"
_NOTICE_DEFERRED_FALLBACK = "这次没改成，回头再说"


def notice_for(outcome: ReplanOutcome) -> str | None:
    """这次要不要**回一句话**；要的话是哪一句（纯函数，零 IO）。

    ## 什么时候不说

    只对真的**提过建议**、而且**没改成**的轮次回话：

    - ``no_proposal``：绝大多数闲聊走这里。她本来就在聊天，再补一句
      "没改计划"只是噪声，一句话都不能多；
    - ``accepted``：她这一轮的回复本来就在谈这件事，时间轴也已经改好；
    - ``duplicate``：同一 ``turn_id`` 已经回过一次。幂等键就是 ``turn_id``，
      重复消费**不许再回一条**（否则重投会把同一句话刷两遍）。

    其余（``declined`` / ``deferred``）各回一句——"拒绝/推迟"必须让用户听见，
    而"记账"只有我们自己看得见。
    """
    if outcome.state not in ("declined", "deferred"):
        return None
    text = REPLAN_NOTICE_TEXTS.get(str(outcome.reason_code or ""))
    if text is not None:
        return text
    if outcome.state == "declined":
        return _NOTICE_DECLINED_FALLBACK
    return _NOTICE_DEFERRED_FALLBACK


async def _claim_decision(
    session: AsyncSession,
    *,
    actor_id: str,
    turn_id: str,
    window_id: str | None,
    now: datetime,
) -> tuple[ScheduleChangeRequestRow, bool]:
    """同一 ``turn_id`` 只一次决定：先插占位行，撞唯一键就返回原决定。

    占位行的 ``reason_code='in_progress'`` 是**如实**的：决定还没出来。真正的
    决定随后就地更新到同一行。并发/重投时第二个调用看到这行就返回它，
    于是"同一轮决策两次"在结构上不可能发生。

    占位行与整个 handler 在同一个事务里：handler 崩了，占位行一起回滚，
    下次重投等于重新开始——不会留下一个永远 ``in_progress`` 的幽灵记录。
    """
    row = ScheduleChangeRequestRow(
        id=new_id("schg"),
        actor_id=actor_id,
        source_turn_id=turn_id,
        window_id=window_id or None,
        plan_id=None,
        base_revision=0,
        decision="defer",
        reason_code=REASON_IN_PROGRESS,
        range_start_at=None,
        range_end_at=None,
        committed_revision=None,
        created_at=now,
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(ScheduleChangeRequestRow).where(
                    ScheduleChangeRequestRow.actor_id == actor_id,
                    ScheduleChangeRequestRow.source_turn_id == turn_id,
                )
            )
        ).scalar_one()
        return existing, True
    return row, False


def _record(
    row: ScheduleChangeRequestRow,
    *,
    decision: str,
    reason_code: str | None,
    now: datetime,
    plan_id: str | None = None,
    base_revision: int | None = None,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    committed_revision: int | None = None,
) -> None:
    row.decision = decision
    row.reason_code = reason_code
    if plan_id is not None:
        row.plan_id = plan_id
    if base_revision is not None:
        row.base_revision = base_revision
    if range_start is not None:
        row.range_start_at = range_start
    if range_end is not None:
        row.range_end_at = range_end
    if committed_revision is not None:
        row.committed_revision = committed_revision
    row.updated_at = now


async def _lock_plans(
    session: AsyncSession, *, actor_id: str, days: list[date]
) -> dict[date, DayPlanRow]:
    """按 ``plan_day`` **升序**锁住这两份计划（跨午夜修订的唯一入口）。

    顺序是死要求：另一个 worker 也在跨午夜时，如果一边锁 D 再锁 D+1、另一边
    锁 D+1 再锁 D，就会互相等到超时。排序后两边拿锁顺序一致，最坏也只是
    排队。``FOR UPDATE`` 保证"读到的 revision 与写下去时一致"。
    """
    rows = list(
        (
            await session.execute(
                select(DayPlanRow)
                .where(DayPlanRow.actor_id == actor_id, DayPlanRow.plan_day.in_(days))
                .order_by(DayPlanRow.plan_day)
                .with_for_update()
            )
        ).scalars()
    )
    return {day: row for row in rows for day in [row.plan_day]}


async def _commit_replan(
    session: AsyncSession,
    *,
    actor_id: str,
    verdict: GateVerdict,
    drafts: list[ScopedDraft],
    now: datetime,
    source_ids: tuple[str, ...],
    timezone: str,
) -> list[ScopeReplacement]:
    """把校验过的段落进时间轴：重新取锁 → 重新校验 → 一个 savepoint 提交。

    设计 §4："入库时重新读取最新状态，在 ``day_plans`` 行锁下检查 revision 与
    所有锚点"。所以这里**再查一遍**：模型跑的这几十秒里可能刚确认了一条约定，
    那这条重排就必须整单失败（defer），而不是把新承诺压掉。
    """
    scope_start = verdict.scope_start or now
    scope_end = verdict.scope_end or now
    pieces = _split_days(scope_start, scope_end, timezone)
    days = sorted({day for day, _start, _end in pieces})
    rows = await _lock_plans(session, actor_id=actor_id, days=days)

    # 逐日切片：段从不跨午夜，所以一段必须整段落在某一格内。
    by_day: dict[date, list[ScopedDraft]] = {day: [] for day in days}
    for scoped in drafts:
        start = _aware(scoped.draft.start_at)
        day = plan_day_for(start, timezone)
        if day not in by_day:
            raise ScopeError(REASON_CROSS_MIDNIGHT_MISSING, "段落在作用域之外的计划日")
        by_day[day].append(scoped)

    edits: list[ScopeEdit] = []
    for day, piece_start, piece_end in pieces:
        row = rows.get(day)
        if row is None:
            raise ScopeError(
                REASON_CROSS_MIDNIGHT_MISSING,
                f"{day.isoformat()} 没有计划行：跨午夜修订不能只改一半",
            )
        day_drafts = by_day.get(day, [])
        if not day_drafts:
            # 这一天不需要动：不动它就是最安全的"改法"。
            continue
        # 锁内重新校验：这一格里不能出现新的不可移动安排。
        await _recheck_immovables(
            session,
            actor_id=actor_id,
            row=row,
            start=piece_start,
            end=piece_end,
        )
        edits.append(
            ScopeEdit(
                row=row,
                scope_start=piece_start,
                scope_end=piece_end,
                drafts=tuple(day_drafts),
                reason_code=REASON_EVENT_APPLIED,
                source_ids=source_ids,
            )
        )
    if not edits:
        raise ScopeError(REASON_CROSS_MIDNIGHT_MISSING, "没有需要改的计划日")
    return await replace_scope_multi(session, edits, now=now)


async def _recheck_immovables(
    session: AsyncSession,
    *,
    actor_id: str,
    row: DayPlanRow,
    start: datetime,
    end: datetime,
) -> None:
    """锁内复核：这一步在模型返回**之后**跑，专门抓"期间新确认的安排"。"""
    segments = list(
        (
            await session.execute(
                select(ScheduleSegmentRow).where(
                    ScheduleSegmentRow.plan_id == str(row.id),
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.start_at < end,
                    ScheduleSegmentRow.end_at > start,
                )
            )
        ).scalars()
    )
    for segment in segments:
        if scope_movable(segment):
            continue
        raise ScopeError(
            REASON_COMMITMENT_CONFLICT,
            f"作用域里出现了不可移动的段 {segment.id}（期间刚确认的安排）",
        )
    anchors = await load_commitment_anchors(
        session, actor_id=actor_id, window_start=start, window_end=end
    )
    if anchors:
        raise ScopeError(
            REASON_COMMITMENT_CONFLICT, "作用域里出现了新确认的承诺保护窗"
        )


async def _valid_place_ids(session: AsyncSession, *, actor_id: str) -> frozenset[str]:
    from ling.infra.models import WorldPlace

    rows = (
        await session.execute(
            select(WorldPlace.id).where(WorldPlace.actor_id == actor_id)
        )
    ).all()
    return frozenset(str(item) for (item,) in rows)


async def _valid_places(session: AsyncSession, *, actor_id: str) -> list[dict[str, str]]:
    from ling.infra.models import WorldPlace

    rows = list(
        (
            await session.execute(
                select(WorldPlace)
                .where(WorldPlace.actor_id == actor_id)
                .order_by(WorldPlace.name)
            )
        ).scalars()
    )
    return [{"id": str(item.id), "name": str(item.name)} for item in rows]


# ---------------------------------------------------------------- 编排


async def replan_work(
    session: AsyncSession,
    *,
    actor_id: str,
    turn_id: str,
    user_text: str,
    window_id: str | None = None,
    now: datetime | None = None,
    router: object | None = None,
    config_reader: object | None = None,
    lease_owner: str | None = None,
) -> ReplanOutcome:
    """跑一次"临时提议 → 局部重排"作业。调用方负责 commit/rollback。

    分支顺序（每一步都可能是 0 次模型调用）：

    1. 没有候选提议 → ``no_proposal``，**什么都不写**（正常聊天）；
    2. 这一轮已经决定过 → ``duplicate``，**返回原决定**（不重复调模型）；
    3. 硬门不过 → 记录 ``decline``/``defer``，明确告诉用户"没有改动"；
    4. 频率/预算满 → 记录 ``defer``（**仍然记录**，用户看到"暂未改动"）；
    5. 模型不可用 → ``defer/model_unavailable``，0 次调用、不占预算行；
    6. 模型输出非法 / 越界 → ``defer``，**整份拒收**，旧计划原样保留；
    7. 接受 → 重新取锁、重新校验、CAS 提交 ``revision+1``；CAS 失败 →
       ``defer/stale_revision``（旧响应不覆盖新计划）。

    与大纲/细化一样，模型调用发生在 handler 的会话里、**不在** outbox 的认领
    事务里（见 `worker` 的"outbox 事务边界"）。
    """
    moment = _aware(now or utcnow())
    timezone, _source = await resolve_plan_timezone(session)
    signal = await structured_proposal(session, actor_id=actor_id, turn_id=turn_id)
    if signal is None:
        signal = detect_proposal(user_text)
    if signal is None:
        return ReplanOutcome(
            state="no_proposal",
            turn_id=turn_id,
            reason_code=REASON_NO_PROPOSAL,
            notice="",
        )
    plan_day = plan_day_for(moment, timezone)
    requested = parse_requested_window(user_text, plan_day=plan_day, timezone=timezone)
    if requested is not None:
        signal = replace(
            signal, requested_start=requested[0], requested_end=requested[1]
        )

    row, duplicate = await _claim_decision(
        session, actor_id=actor_id, turn_id=turn_id, window_id=window_id, now=moment
    )
    if duplicate:
        existing_decision = str(row.decision)
        return ReplanOutcome(
            state="duplicate",
            turn_id=turn_id,
            decision=existing_decision,
            reason_code=str(row.reason_code or "") or REASON_DUPLICATE_TURN,
            plan_day=plan_day,
            plan_id=row.plan_id,
            revision_before=int(row.base_revision or 0),
            revision_after=row.committed_revision,
            superseded=0,
            created=0,
            notice="这一轮已经决定过了，不会重复改计划。",
            signal=signal,
        )

    verdict = await evaluate_gates(
        session, actor_id=actor_id, signal=signal, now=moment, timezone=timezone
    )
    if not verdict.ok:
        _record(
            row,
            decision=verdict.decision,
            reason_code=verdict.reason_code,
            now=moment,
            plan_id=verdict.plan_id,
            base_revision=None,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        logger.info(
            "schedule.replan.gated actor=%s turn=%s reason=%s decision=%s",
            actor_id,
            turn_id,
            verdict.reason_code,
            verdict.decision,
        )
        return ReplanOutcome(
            state="declined" if verdict.decision == "decline" else "deferred",
            turn_id=turn_id,
            decision=verdict.decision,
            reason_code=verdict.reason_code,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=0,
            notice=_notice(verdict.decision, verdict.reason_code),
            signal=signal,
        )

    limited = await check_limits(
        session,
        actor_id=actor_id,
        plan_day=verdict.plan_day or plan_day,
        window_id=window_id,
        now=moment,
    )
    if limited is not None:
        _record(
            row,
            decision="defer",
            reason_code=limited.reason_code,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        logger.info(
            "schedule.replan.limited actor=%s turn=%s reason=%s",
            actor_id,
            turn_id,
            limited.reason_code,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=limited.reason_code,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=0,
            notice=_notice("defer", limited.reason_code),
            signal=signal,
        )

    if not await behavior_model_ready(router, config_reader):
        _record(
            row,
            decision="defer",
            reason_code=REASON_MODEL_UNAVAILABLE,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=REASON_MODEL_UNAVAILABLE,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=0,
            notice=_notice("defer", REASON_MODEL_UNAVAILABLE),
            signal=signal,
        )

    reservation = await reserve_behavior_call(
        session,
        actor_id=actor_id,
        plan_day=verdict.plan_day or plan_day,
        idempotency_key=f"replan:{turn_id}",
        now=moment,
        lease_owner=lease_owner or "replan",
        kind=REPLAN_KIND,
        max_calls=REPLAN_MAX_CALLS_PER_DAY,
        daily_token_budget=REPLAN_DAILY_TOKEN_BUDGET,
        reserved_input_tokens=REPLAN_RESERVED_INPUT_TOKENS,
        reserved_output_tokens=REPLAN_RESERVED_OUTPUT_TOKENS,
        lease_seconds=REPLAN_LEASE_SECONDS,
        window_id=window_id or "",
    )
    if reservation is None:
        _record(
            row,
            decision="defer",
            reason_code=REASON_BUDGET_EXHAUSTED,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=REASON_BUDGET_EXHAUSTED,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=0,
            notice=_notice("defer", REASON_BUDGET_EXHAUSTED),
            signal=signal,
        )
    call_id = str(getattr(reservation, "call_id", ""))

    presence = await _presence(session, actor_id=actor_id)
    places = await _valid_places(session, actor_id=actor_id)
    prompt = build_replan_prompt(
        signal=signal,
        verdict=verdict,
        now=moment,
        timezone=timezone,
        presence=presence,
        valid_places=places,
    )
    try:
        raw = await call_behavior_model(
            router=router, config_reader=config_reader, prompt=prompt
        )
    except Exception as exc:  # noqa: BLE001 - 模型侧任何失败都是"这次没改成"
        await finish_behavior_call(
            session, call_id=call_id, state="failed", error_code=REASON_MODEL_ERROR
        )
        logger.warning(
            "schedule.replan.model_error actor=%s turn=%s error=%s",
            actor_id,
            turn_id,
            type(exc).__name__,
        )
        _record(
            row,
            decision="defer",
            reason_code=REASON_MODEL_ERROR,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=REASON_MODEL_ERROR,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=1,
            notice=_notice("defer", REASON_MODEL_ERROR),
            signal=signal,
        )

    try:
        proposal = parse_replan_output(raw)
        drafts = validate_replan_segments(
            proposal.segments,
            scope_start=verdict.scope_start or moment,
            scope_end=verdict.scope_end or moment,
            valid_place_ids=await _valid_place_ids(session, actor_id=actor_id),
            decision=proposal.decision,
        )
    except ReplanValidationError as exc:
        await finish_behavior_call(
            session, call_id=call_id, state="failed", error_code=REASON_MODEL_INVALID
        )
        logger.info(
            "schedule.replan.invalid actor=%s turn=%s problems=%s",
            actor_id,
            turn_id,
            exc.problems,
        )
        _record(
            row,
            decision="defer",
            reason_code=REASON_MODEL_INVALID,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=REASON_MODEL_INVALID,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=1,
            notice=_notice("defer", REASON_MODEL_INVALID),
            signal=signal,
        )

    await finish_behavior_call(session, call_id=call_id, state="committed")

    if proposal.decision != "accept":
        _record(
            row,
            decision=proposal.decision,
            reason_code=proposal.reason_code or REASON_NO_PROPOSAL,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="declined" if proposal.decision == "decline" else "deferred",
            turn_id=turn_id,
            decision=proposal.decision,
            reason_code=proposal.reason_code,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=1,
            notice=_notice(proposal.decision, proposal.reason_code),
            signal=signal,
        )

    source_ids = (f"turn:{turn_id}", *(filter(None, (signal.source_ref,))))
    try:
        replacements = await _commit_replan(
            session,
            actor_id=actor_id,
            verdict=verdict,
            drafts=drafts,
            now=moment,
            source_ids=source_ids,
            timezone=verdict.plan_timezone or timezone,
        )
    except ScopeError as exc:
        _record(
            row,
            decision="defer",
            reason_code=exc.reason_code,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        logger.info(
            "schedule.replan.rejected actor=%s turn=%s reason=%s detail=%s",
            actor_id,
            turn_id,
            exc.reason_code,
            exc.detail,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=exc.reason_code,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=1,
            notice=_notice("defer", exc.reason_code),
            signal=signal,
        )
    except StaleRevisionError:
        _record(
            row,
            decision="defer",
            reason_code=REASON_STALE_REVISION,
            now=moment,
            plan_id=verdict.plan_id,
            range_start=verdict.scope_start,
            range_end=verdict.scope_end,
        )
        return ReplanOutcome(
            state="deferred",
            turn_id=turn_id,
            decision="defer",
            reason_code=REASON_STALE_REVISION,
            plan_day=verdict.plan_day or plan_day,
            plan_id=verdict.plan_id,
            scope_start=verdict.scope_start,
            scope_end=verdict.scope_end,
            calls=1,
            notice=_notice("defer", REASON_STALE_REVISION),
            signal=signal,
        )

    first = replacements[0]
    last = replacements[-1]
    total_created = sum(len(item.created_ids) for item in replacements)
    total_superseded = sum(len(item.superseded_ids) for item in replacements)
    _record(
        row,
        decision="accept",
        reason_code=proposal.reason_code or REASON_EVENT_APPLIED,
        now=moment,
        plan_id=first.plan_id,
        base_revision=first.revision_before,
        range_start=verdict.scope_start,
        range_end=verdict.scope_end,
        committed_revision=last.revision_after,
    )
    logger.info(
        "schedule.replan.accepted actor=%s turn=%s days=%s superseded=%s created=%s",
        actor_id,
        turn_id,
        [item.plan_day.isoformat() for item in replacements],
        total_superseded,
        total_created,
    )
    return ReplanOutcome(
        state="accepted",
        turn_id=turn_id,
        decision="accept",
        reason_code=proposal.reason_code or REASON_EVENT_APPLIED,
        plan_day=first.plan_day,
        plan_id=first.plan_id,
        revision_before=first.revision_before,
        revision_after=last.revision_after,
        scope_start=verdict.scope_start,
        scope_end=verdict.scope_end,
        superseded=total_superseded,
        created=total_created,
        calls=1,
        notice=_notice("accept", None),
        signal=signal,
    )


__all__ = [
    "REASON_ACTIVITY_RUNNING",
    "REASON_AMBIGUOUS_HYPOTHETICAL",
    "REASON_AMBIGUOUS_NEGATION",
    "REASON_AMBIGUOUS_QUESTION",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_COMMITMENT_CONFLICT",
    "REASON_CROSS_MIDNIGHT_MISSING",
    "REASON_DAILY_LIMIT",
    "REASON_DUPLICATE_TURN",
    "REASON_LOW_ENERGY",
    "REASON_MODEL_ERROR",
    "REASON_MODEL_INVALID",
    "REASON_MODEL_UNAVAILABLE",
    "REASON_NO_PROPOSAL",
    "REASON_PLAN_MISSING",
    "REASON_REST_WINDOW",
    "REASON_SCOPE_TOO_SMALL",
    "REASON_SLEEP_WINDOW",
    "REASON_STALE_REVISION",
    "REASON_WINDOW_HOURLY_LIMIT",
    "REPLAN_DAILY_MAX",
    "REPLAN_DAILY_TOKEN_BUDGET",
    "REPLAN_KIND",
    "REPLAN_MAX_CALLS_PER_DAY",
    "REPLAN_MAX_SEGMENTS",
    "REPLAN_MIN_ENERGY",
    "REPLAN_MIN_SCOPE_MINUTES",
    "REPLAN_NOTICE_MAX_CHARS",
    "REPLAN_NOTICE_TEXTS",
    "REPLAN_PROFILE",
    "REPLAN_RESERVED_INPUT_TOKENS",
    "REPLAN_RESERVED_OUTPUT_TOKENS",
    "REPLAN_WINDOW_HOURLY_MAX",
    "GateVerdict",
    "ProposalSignal",
    "ReplanOutcome",
    "ReplanProposal",
    "ReplanValidationError",
    "build_replan_prompt",
    "check_limits",
    "detect_proposal",
    "evaluate_gates",
    "notice_for",
    "note_schedule_proposal",
    "parse_replan_output",
    "parse_requested_window",
    "replan_work",
    "structured_proposal",
    "turn_user_text",
    "validate_replan_segments",
]
