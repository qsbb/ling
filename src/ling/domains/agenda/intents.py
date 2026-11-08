"""长程意向：有日期的目标（不是时段），状态与授权的**唯一**写入方。

设计来源：``docs/DREAM-SCHEDULE-DESIGN.md`` §3A（多日展望与长程意向）与 §6
（数据模型）。本模块对应设计 §9 的 **1.75a：长程存储与滚动**。

## 五条硬规则（注释与测试一起钉住）

1. **确认"目标" ≠ 确认"时段"**。``confirmed`` 只表示"用户对这个目标/日期作过
   明确确认"，仍是高优先**软偏好**；只有带精确时段且有确认来源的记录
   （``commitments`` / confirmed ``agenda_plans`` / 周期 occurrence）才是硬锚点。
   所以 :meth:`IntentService.confirm` **不会**创建任何 ``schedule_segments``：
   "周五想旅行"确认后不能凭空堵住任何时段。
2. **过期 = 不知道**。目标日已过、又没有完成/失败证据 → ``expired_unknown``；
   ``missed`` 只在有明确证据（用户说没成行、承诺链路失败）时成立。
3. **谁批准**。夜间模型最多创建 ``proposed``；升级到 ``confirmed`` 只有两条
   入口：用户明确确认（:data:`CONFIRM_ENTRY_USER`）与承诺链路成功
   （:data:`CONFIRM_ENTRY_COMMITMENT`）。反复进入夜间 prompt/展望**不构成**
   升级证据——本模块根本没有"被看到/被重放就升级"的路径。
4. **撤回要递归**。撤回一条意向会连带取消引用它的**未来**准备段子树，并沿
   ``source_ref = <上游 intention_id>`` 递归撤回下游意向。
5. **容量**。``proposed`` ≤ 4、活跃（未终态）≤ 12；满额时拒收新提议并记
   ``intention_capacity``，**不淘汰**任何已确认来源，也没有自动淘汰。

## 写入边界（ADR-0001）

- 状态只能经本模块改；模型输出或其它域直接写 ``state`` 一律视为越权。
- 本模块**不做时间轴生成**。与 ``schedule_segments`` 只提供两个小函数：
  :func:`attach_preparation`（给某条已确认意向挂一个**准备**段）与
  :func:`segments_for_intention` / :func:`intention_for_segment`（查引用）。
- 不写 ``activities`` / ``self_experiences``：``completed`` 只保存证据引用，
  事实入库仍归活动 + 完成证据链。意向不是经历。
- 承诺/确认安排的状态机仍在各自域：``confirm(entry="commitment")`` 只接受
  承诺链路给出的成功回执，**不复核也不写** ``commitments`` 表。

## 与"24 小时外的目标"的衔接

D+1…D+7 的展望是**确定性投影**：P4 用 :meth:`IntentService.list_active` 取
目标日与 D 窗口相交的意向，再做"每天 2 条、总 10 条"的截断；本模块不装配
prompt、不调模型、不做按日分组。目标日到了也**不会**自动变成 ``day_plans``
硬锚点——那一步永远要按当时来源重新裁决。

## 撤回为什么要动 ``day_plans.revision``

准备段的"失效"必须让读侧**立刻**看不到它（``status=superseded``），否则撤回
之后控制台还在建议"为周五的旅行做准备"。段属于某一版计划头，所以这里按
``seam_check`` 的同一做法：CAS 抬 ``day_plans.revision``，再把要撤的段写成
``superseded`` + ``revision_ended``。CAS 失败（另一个 worker 正在提交新版本）
时不硬覆盖，退回"用当前 revision 记历史"并把 ``stale_revision`` 报给调用方。
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Visibility
from ling.infra.models import (
    INTENTION_KINDS,
    INTENTION_SOURCE_KINDS,
    INTENTION_STATES,
    INTENTION_TERMINAL_STATES,
    DayPlanRow,
    ScheduleIntentionRow,
    ScheduleSegmentRow,
    utcnow,
)
from ling.infra.security import new_id

logger = logging.getLogger("ling.domains.agenda.intents")

# ---------------------------------------------------------------- 授权入口
#
# 设计 §3A：升级到 `confirmed` 只有两条入口。**没有第三条**——模型输出、
# 反复进入 prompt、被夜间展望读到的次数都不构成确认证据。
CONFIRM_ENTRY_USER = "user"
CONFIRM_ENTRY_COMMITMENT = "commitment"
CONFIRM_ENTRIES: tuple[str, ...] = (CONFIRM_ENTRY_USER, CONFIRM_ENTRY_COMMITMENT)

# 可被 create 直接创建的状态：模型只能 proposed，带来源的愿望可以 planned。
# `confirmed` 永远不能由 create 直接给出（见 `IntentService.create`）。
CREATABLE_STATES: tuple[str, ...] = ("proposed", "planned")

# ---------------------------------------------------------------- 容量（工程初值）
#
# 设计 §3A：「每 actor 同时最多 12 条未终态意向，其中 proposed 最多 4 条」。
# 满额时**拒收新的模型提议**，不淘汰用户已确认的来源，也不自动淘汰。
MAX_ACTIVE_INTENTIONS = 12
MAX_PROPOSED_INTENTIONS = 4

# 目标日最多看 90 天、单条跨度最多 90 天（设计 §3A/§6）。
MAX_TARGET_HORIZON_DAYS = 90
MAX_TARGET_SPAN_DAYS = 90

# 模型提议 72 小时无进一步证据 → expired_unknown（设计 §3A「滚动、清理和中断」）。
PROPOSAL_TTL = timedelta(hours=72)

MAX_TITLE_CHARS = 120
MAX_REF_CHARS = 160
MAX_CODE_CHARS = 48

# 撤回递归的深度/数量上限：异常数据（A→B→A）不能把一次撤回变成无限循环。
MAX_REVOKE_NODES = 64
# 一次撤回最多遍历多少个段（block→hour→minute 三层，正常远小于这个数）。
MAX_SEGMENT_TREE_NODES = 512

# ---------------------------------------------------------------- 原因码
#
# 只记 ID/原因码，不记标题（设计 §7：私密主题不进日志）。
REASON_INTENTION_CAPACITY = "intention_capacity"
REASON_EVIDENCE_REQUIRED = "intention_resolution_evidence_required"
REASON_EVIDENCE_INVALID = "intention_resolution_evidence_invalid"
REASON_TERMINAL = "intention_terminal"
REASON_ILLEGAL_TRANSITION = "intention_illegal_transition"
REASON_CONFIRM_ENTRY = "intention_confirm_entry"
REASON_PREPARATION_REQUIRES_CONFIRMED = "intention_preparation_requires_confirmed"
REASON_PREPARATION_REQUIRES_SOURCE = "intention_preparation_requires_source"
REASON_PREPARATION_NOT_OPTIONAL = "intention_preparation_not_optional"
REASON_PREPARATION_IN_PAST = "intention_preparation_in_past"
REASON_PREPARATION_TARGET_NOT_FUTURE = "intention_preparation_target_not_future"
REASON_PREPARATION_DAY_OUT_OF_RANGE = "intention_preparation_day_out_of_range"
REASON_PREPARATION_ALREADY_BOUND = "intention_preparation_already_bound"
REASON_EXPIRED_TARGET_DAY = "intention_expired_target_day"
REASON_EXPIRED_TTL = "intention_expired_proposal_ttl"
REASON_USER_WITHDREW = "user_withdrew"
REASON_SOURCE_CANCELLED = "source_cancelled"
REASON_UPSTREAM_WITHDRAWN = "upstream_withdrawn"
# 段上的原因标签（与 timeline 的 `reason:<code>` 同格式，读侧不必认识新格式）。
REASON_TAG_PREFIX = "reason:"

# 展望排序：硬约定（在别的表里）> 已确认 > planned > proposed（设计 §3A）。
STATE_ORDER: dict[str, int] = {"confirmed": 0, "planned": 1, "proposed": 2}

# 可见范围从宽到严。`admin_only` 最严（并且永远不在普通可读集里）；
# 未知取值按最严处理（fail closed），不能因为拼错就放宽。
VISIBILITY_RANK: dict[str, int] = {
    Visibility.public.value: 0,
    Visibility.group_scope.value: 1,
    Visibility.subject_private.value: 2,
    Visibility.window_private.value: 3,
    Visibility.actor_private.value: 4,
    Visibility.admin_only.value: 5,
}


# ---------------------------------------------------------------- 异常


class IntentError(Exception):
    """长程意向的领域错误基类。``reason_code`` 只放枚举/原因码，不放标题。"""

    reason_code: str = "intention_error"

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code or type(self).reason_code
        self.message = message


class IntentValidationError(IntentError):
    """输入/日期/枚举不合法（调用方的错，不是数据状态冲突）。"""

    reason_code = "intention_invalid_input"


class IntentNotFound(IntentError):
    """意向不存在或不属于当前 actor（不区分两者，避免泄露"这个 id 存在"）。"""

    reason_code = "intention_not_found"


class IntentStateError(IntentError):
    """状态转换不被允许。"""

    reason_code = REASON_ILLEGAL_TRANSITION


class IntentTerminalError(IntentStateError):
    """终态不能被复活/再次确认/再次撤回。"""

    reason_code = REASON_TERMINAL


class IntentEvidenceError(IntentError):
    """终态缺证据，或给 ``expired_unknown`` 传了证据。"""

    reason_code = REASON_EVIDENCE_REQUIRED


class IntentCapacityError(IntentError):
    """容量满：拒收新意向（不淘汰任何已有行）。"""

    reason_code = REASON_INTENTION_CAPACITY

    def __init__(self, message: str, *, verdict: CapacityVerdict) -> None:
        super().__init__(message, reason_code=REASON_INTENTION_CAPACITY)
        self.verdict = verdict


# ---------------------------------------------------------------- 纯规则


def is_terminal(state: str) -> bool:
    return state in INTENTION_TERMINAL_STATES


def ensure_confirm_entry(entry: str) -> str:
    """校验"谁批准"的入口。只有两条，别的一律拒收。"""
    value = str(entry or "").strip()
    if value not in CONFIRM_ENTRIES:
        raise IntentStateError(
            f"升级到 confirmed 只允许 {CONFIRM_ENTRIES} 两条入口，收到 {value!r}",
            reason_code=REASON_CONFIRM_ENTRY,
        )
    return value


def ensure_transition(*, current: str, target: str) -> None:
    """状态白名单 + 转换表。服务层与数据库 CHECK 各判一次。

    - 终态**任何**转换都拒（"终态不能被复活"）；新来源要新 ID/审计链；
    - ``completed`` 只能从 ``planned`` / ``confirmed`` 来：一个从未成为计划的
      候选不能直接"完成了"；
    - ``proposed → planned`` 允许（用户明确表达愿望），``proposed → confirmed``
      也允许（一次明确确认，设计 §3A）。
    """
    if current not in INTENTION_STATES:
        raise IntentStateError(f"未知的意向状态: {current!r}")
    if target not in INTENTION_STATES:
        raise IntentStateError(f"未知的目标状态: {target!r}")
    if is_terminal(current):
        raise IntentTerminalError(
            f"意向已处于终态 {current}，不允许再转 {target}；新来源请用新 ID",
            reason_code=REASON_TERMINAL,
        )
    allowed = {
        "proposed": {"planned", "confirmed", "missed", "abandoned", "expired_unknown"},
        "planned": {"confirmed", "completed", "missed", "abandoned", "expired_unknown"},
        "confirmed": {"completed", "missed", "abandoned", "expired_unknown"},
    }[current]
    if target not in allowed:
        raise IntentStateError(f"不允许的意向转换: {current} → {target}")


def ensure_resolution_evidence(*, state: str, evidence_ref: str | None) -> None:
    """终态必须带证据引用；**只有** ``expired_unknown`` 例外。

    设计 §3A：「其余到目标区间末端仍无证据的转 ``expired_unknown``，不擅自写
    "没做"」。反过来，``expired_unknown`` **不接受**证据：有证据就该是
    ``missed`` / ``completed``，把两者混在一起等于把"不知道"洗成"知道"。
    """
    if state not in INTENTION_TERMINAL_STATES:
        raise IntentValidationError(f"不是终态: {state!r}")
    if state == "expired_unknown":
        if evidence_ref:
            raise IntentEvidenceError(
                "expired_unknown 表示「没有证据」，不接受证据引用；"
                "有证据请用 missed/completed",
                reason_code=REASON_EVIDENCE_INVALID,
            )
        return
    if not evidence_ref:
        raise IntentEvidenceError(
            f"终态 {state} 必须带证据引用（resolution_evidence_ref）",
            reason_code=REASON_EVIDENCE_REQUIRED,
        )


def evaluate_capacity(
    *, active_count: int, proposed_count: int, state: str
) -> CapacityVerdict:
    """容量判定（纯函数，方便单测；`IntentService.capacity_check` 只负责取数）。

    两条独立的闸：
    - 活跃（未终态）总数 ≥ 12：**任何**新行都拒（人工/用户入口应先提示清理，
      或按明确来源更新同一条意向）；
    - ``proposed`` ≥ 4：拒收新的候选提议。

    判定只说"能不能再建一条"，**从不**选择淘汰谁。
    """
    if state not in CREATABLE_STATES:
        raise IntentValidationError(
            f"只有 {CREATABLE_STATES} 可以直接创建，收到 {state!r}"
        )
    if active_count >= MAX_ACTIVE_INTENTIONS:
        return CapacityVerdict(
            allowed=False,
            reason_code=REASON_INTENTION_CAPACITY,
            axis="active",
            active_count=active_count,
            proposed_count=proposed_count,
        )
    if state == "proposed" and proposed_count >= MAX_PROPOSED_INTENTIONS:
        return CapacityVerdict(
            allowed=False,
            reason_code=REASON_INTENTION_CAPACITY,
            axis="proposed",
            active_count=active_count,
            proposed_count=proposed_count,
        )
    return CapacityVerdict(
        allowed=True,
        reason_code=None,
        axis="",
        active_count=active_count,
        proposed_count=proposed_count,
    )


def ensure_target_window(
    *, start_day: date, end_day: date, today: date | None = None
) -> None:
    """目标区间校验：不反向、跨度 ≤ 90 天、不落在过去、起点 ≤ 今天 + 90 天。"""
    if end_day < start_day:
        raise IntentValidationError(
            f"目标结束日 {end_day} 早于开始日 {start_day}"
        )
    if (end_day - start_day).days > MAX_TARGET_SPAN_DAYS:
        raise IntentValidationError(
            f"目标跨度超过 {MAX_TARGET_SPAN_DAYS} 天: {start_day}→{end_day}"
        )
    if today is not None:
        if end_day < today:
            raise IntentValidationError(
                f"目标日 {start_day}→{end_day} 已经过去（今天 {today}）"
            )
        if (start_day - today).days > MAX_TARGET_HORIZON_DAYS:
            raise IntentValidationError(
                f"目标日 {start_day} 超出 {MAX_TARGET_HORIZON_DAYS} 天展望范围"
                f"（今天 {today}）"
            )


def strictest_visibility(*values: str | None) -> str:
    """取若干来源里**最严**的可见范围（准备段默认继承它）。

    未知取值按最严处理（fail closed）：拼错一个范围名不该让内容变宽。
    """
    ranked: int | None = None
    for value in values:
        if value is None:
            continue
        name = str(value).strip()
        if not name:
            continue
        rank = VISIBILITY_RANK.get(name, VISIBILITY_RANK["actor_private"])
        ranked = rank if ranked is None else max(ranked, rank)
    if ranked is None:
        # 一个来源都没有：宁可最严，也不默认公开。
        return Visibility.actor_private.value
    for name, rank in VISIBILITY_RANK.items():
        if rank == ranked:
            return name
    return Visibility.actor_private.value


# ---------------------------------------------------------------- 视图/报告


@dataclass(frozen=True)
class CapacityVerdict:
    """一次容量判定的结果（`axis` 说明是哪条闸拦下的）。"""

    allowed: bool
    reason_code: str | None
    axis: str
    active_count: int
    proposed_count: int
    max_active: int = MAX_ACTIVE_INTENTIONS
    max_proposed: int = MAX_PROPOSED_INTENTIONS


@dataclass(frozen=True)
class IntentView:
    """只读投影：给服务/UI/测试看，不替代 ORM 行。"""

    intention_id: str
    actor_id: str
    kind: str
    title: str
    state: str
    source_kind: str
    source_ref: str
    target_start_day: date
    target_end_day: date
    target_timezone: str
    priority: int
    visibility: str
    revision: int
    horizon_rev: int
    prep_source_ref: str | None
    prep_due_day: date | None
    confirmation_source_ref: str | None
    confirmed_at: datetime | None
    resolution_evidence_ref: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    @property
    def is_hard_anchor(self) -> bool:
        """意向**永远**不是硬锚点：`confirmed` 只是高优先软偏好（§3A）。"""
        return False


@dataclass(frozen=True)
class ExpiryReport:
    """一次惰性过期的结果。``reasons`` 是 ``(intention_id, reason_code)``。"""

    expired_ids: tuple[str, ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.expired_ids)


@dataclass(frozen=True)
class RevokeReport:
    """一次撤回的结果：被撤回的意向（含递归下游）与被取消的未来准备段。"""

    root_id: str
    reason_code: str
    withdrawn_ids: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()
    skipped_segment_ids: tuple[str, ...] = ()
    plan_ids: tuple[str, ...] = ()
    stale_plan_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------- 工具


def _aware(moment: datetime | None) -> datetime:
    """统一成 UTC aware；naive 输入按 UTC 解释（与 timeline 的 `_aware` 同规则）。"""
    value = moment or utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # 坏时区退回 UTC 而不是服务器本地时区：本地时区会让"目标日过了没"
        # 随部署机器的时区漂移。
        return ZoneInfo("UTC")


def local_date(moment: datetime, timezone: str) -> date:
    return _aware(moment).astimezone(_zone(timezone)).date()


def _clean_text(value: str | None, *, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise IntentValidationError(f"{label} 不能为空")
    if len(text) > limit:
        raise IntentValidationError(f"{label} 超过 {limit} 字符（实际 {len(text)}）")
    return text


def _optional_ref(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_REF_CHARS:
        raise IntentValidationError(f"{label} 超过 {MAX_REF_CHARS} 字符")
    return text


def _clean_code(value: str | None, *, label: str = "reason_code") -> str:
    return _clean_text(value, label=label, limit=MAX_CODE_CHARS)


def _append_unique(existing: Sequence[object] | None, *values: str) -> list[str]:
    """往段的 ``source_event_ids``（``JSON`` 列，读出来是列表）里追加去重后的值。"""
    items = [str(item) for item in (existing or [])]
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _movable_segment(segment: ScheduleSegmentRow) -> bool:
    """能不能被撤回动到：只有**未确认的可选段**（与 timeline 同一判据）。

    睡眠/承诺锚点与 confirmed 的约定不是可替换候选——它们属于别的状态机，
    撤回一条意向不该顺手把用户确认的事一起取消。
    """
    return str(segment.anchor_kind) == "" and str(segment.commitment_level) != "confirmed"


# ---------------------------------------------------------------- 服务


class IntentService:
    """`schedule_intentions` 的**唯一**写入入口。

    用法（P4/接线层）::

        async with session_scope() as session:
            svc = IntentService(session, actor_id="actor_ling")
            await svc.expire_due(now=now)                      # 确定性惰性过期
            view = await svc.create(
                kind="travel", title="去杭州看展",
                target_start_day=date(2026, 11, 13),
                target_end_day=date(2026, 11, 13),
                source_kind="model", source_ref="outline:2026-11-12",
            )
            await svc.confirm(view.intention_id, entry=CONFIRM_ENTRY_USER,
                              evidence_ref="turn:abc")

    所有方法都在当前事务里工作，不自己 commit；并发由 actor 级
    advisory lock + 唯一键 + 行状态共同保护。
    """

    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    # ------------------------------------------------------------ 读

    async def get(self, intention_id: str) -> IntentView:
        row = await self._load(intention_id)
        return self._view(row)

    async def list_active(
        self,
        *,
        start_day: date | None = None,
        end_day: date | None = None,
        states: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[IntentView]:
        """取未终态（或指定状态）的意向，按展望优先级排序。

        ``start_day`` / ``end_day`` 是**闭区间**的日窗口，取"目标区间与它相交"
        的行——D+1…D+7 的展望就是这么读的。**截断留给调用方**（设计 §3A：
        每天 2 条、总 10 条），本模块不装配 prompt。
        """
        if states is None:
            stmt = select(ScheduleIntentionRow).where(
                ScheduleIntentionRow.actor_id == self._actor_id,
                ScheduleIntentionRow.state.notin_(INTENTION_TERMINAL_STATES),
            )
        else:
            chosen = tuple(str(state) for state in states)
            unknown = [state for state in chosen if state not in INTENTION_STATES]
            if unknown:
                raise IntentValidationError(f"未知的意向状态: {unknown}")
            stmt = select(ScheduleIntentionRow).where(
                ScheduleIntentionRow.actor_id == self._actor_id,
                ScheduleIntentionRow.state.in_(chosen),
            )
        if start_day is not None:
            stmt = stmt.where(ScheduleIntentionRow.target_end_day >= start_day)
        if end_day is not None:
            stmt = stmt.where(ScheduleIntentionRow.target_start_day <= end_day)
        rows = list((await self._session.execute(stmt)).scalars())
        rows.sort(
            key=lambda row: (
                STATE_ORDER.get(str(row.state), 9),
                -int(row.priority or 0),
                row.target_start_day,
                str(row.id),
            )
        )
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        return [self._view(row) for row in rows]

    # ------------------------------------------------------------ 容量

    async def capacity_check(self, *, state: str = "proposed") -> CapacityVerdict:
        """判定"还能不能再建一条状态为 state 的意向"。只读，不改任何行。"""
        active_count, proposed_count = await self._capacity_counts()
        return evaluate_capacity(
            active_count=active_count, proposed_count=proposed_count, state=state
        )

    # ------------------------------------------------------------ 创建

    async def create(
        self,
        *,
        kind: str,
        title: str,
        target_start_day: date,
        target_end_day: date,
        source_kind: str,
        source_ref: str,
        state: str = "proposed",
        target_timezone: str | None = None,
        priority: int = 50,
        visibility: str = Visibility.actor_private.value,
        prep_source_ref: str | None = None,
        prep_due_day: date | None = None,
        now: datetime | None = None,
    ) -> IntentView:
        """创建一条意向。**只能**建 ``proposed`` 或带来源的 ``planned``。

        - 模型只能 ``proposed``（夜间大纲顺带提议）；``planned`` 需要明确来源
          （用户说"想/可能"、角色自己表达的愿望、周期规则）；
        - 同来源 + 同区间重放返回**原 ID**（幂等），即使那条已经是终态；
        - 容量满时抛 :class:`IntentCapacityError`（reason_code 恒为
          ``intention_capacity``），**不淘汰**任何已有行。

        日期一律按 ``target_timezone`` 的本地日解释；建好后不再随角色规范时区
        变化而悄悄挪动（时区变更只影响投影/提示歧义）。
        """
        moment = _aware(now)
        chosen_kind = _clean_text(kind, label="kind", limit=32).lower()
        if chosen_kind not in INTENTION_KINDS:
            raise IntentValidationError(
                f"未知的意向类别 {chosen_kind!r}；允许 {INTENTION_KINDS}"
            )
        chosen_source = _clean_text(source_kind, label="source_kind", limit=16)
        if chosen_source not in INTENTION_SOURCE_KINDS:
            raise IntentValidationError(
                f"未知的来源类别 {chosen_source!r}；允许 {INTENTION_SOURCE_KINDS}"
            )
        if state not in CREATABLE_STATES:
            raise IntentStateError(
                f"create 只能建 {CREATABLE_STATES}；confirmed 必须走 confirm()",
                reason_code=REASON_ILLEGAL_TRANSITION,
            )
        if chosen_source == "model" and state != "proposed":
            raise IntentStateError(
                "模型只能创建 proposed；planned/confirmed 需要用户或角色自己的来源",
                reason_code=REASON_ILLEGAL_TRANSITION,
            )
        clean_title = _clean_text(title, label="title", limit=MAX_TITLE_CHARS)
        clean_ref = _clean_text(source_ref, label="source_ref", limit=MAX_REF_CHARS)
        tz_name = (target_timezone or "").strip() or await self._default_timezone()
        if not _valid_timezone(tz_name):
            raise IntentValidationError(f"不是合法的 IANA 时区: {tz_name!r}")
        vis = _clean_text(visibility, label="visibility", limit=32)
        if vis not in {item.value for item in Visibility}:
            raise IntentValidationError(f"未知的可见范围: {vis!r}")
        if not 0 <= int(priority) <= 100:
            raise IntentValidationError(f"priority 必须在 0-100: {priority}")

        prep_ref = _optional_ref(prep_source_ref, label="prep_source_ref")
        if (prep_due_day is None) != (prep_ref is None):
            raise IntentValidationError("prep_due_day 与 prep_source_ref 必须同时给或同时不给")
        if prep_due_day is not None and prep_due_day >= target_start_day:
            raise IntentValidationError(
                f"准备截止日 {prep_due_day} 必须早于目标开始日 {target_start_day}"
            )

        async with self._lock_actor():
            existing = await self._find_by_source(
                source_kind=chosen_source,
                source_ref=clean_ref,
                start_day=target_start_day,
                end_day=target_end_day,
            )
            if existing is not None:
                # 同来源重放：返回原 ID，**不改**原行（终态也不复活）。放在"目标日是否
                # 已过"之前：重放一条过期/终态的旧来源仍要拿到原 ID，而不是先报日期错。
                return self._view(existing)

            ensure_target_window(
                start_day=target_start_day,
                end_day=target_end_day,
                today=local_date(moment, tz_name),
            )

            verdict = await self.capacity_check(state=state)
            if not verdict.allowed:
                logger.info(
                    "schedule.intentions.capacity_rejected actor=%s reason=%s axis=%s "
                    "active=%s proposed=%s",
                    self._actor_id,
                    REASON_INTENTION_CAPACITY,
                    verdict.axis,
                    verdict.active_count,
                    verdict.proposed_count,
                )
                raise IntentCapacityError(
                    f"意向容量已满（{verdict.axis}）：拒收新{'提议' if state == 'proposed' else '意向'}，"
                    "不淘汰任何已有行",
                    verdict=verdict,
                )

            row = ScheduleIntentionRow(
                id=new_id("intent"),
                actor_id=self._actor_id,
                target_start_day=target_start_day,
                target_end_day=target_end_day,
                target_timezone=tz_name,
                kind=chosen_kind,
                title=clean_title,
                state=state,
                source_kind=chosen_source,
                source_ref=clean_ref,
                resolution_evidence_ref=None,
                confirmation_source_ref=None,
                confirmed_at=None,
                resolved_at=None,
                prep_source_ref=prep_ref,
                prep_due_day=prep_due_day,
                priority=int(priority),
                horizon_rev=1,
                visibility=vis,
                revision=1,
                created_at=moment,
                updated_at=moment,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(row)
                    await self._session.flush()
            except IntegrityError:
                # 并发重放：唯一键输给了另一个请求。回查并返回原行（幂等），
                # 而不是把一条 duplicate 报给调用方。
                existing = await self._find_by_source(
                    source_kind=chosen_source,
                    source_ref=clean_ref,
                    start_day=target_start_day,
                    end_day=target_end_day,
                )
                if existing is None:
                    raise
                return self._view(existing)

            logger.info(
                "schedule.intentions.created actor=%s intention=%s state=%s source=%s "
                "target=%s..%s",
                self._actor_id,
                row.id,
                row.state,
                row.source_kind,
                row.target_start_day,
                row.target_end_day,
            )
            return self._view(row)

    # ------------------------------------------------------------ 升级

    async def confirm(
        self,
        intention_id: str,
        *,
        entry: str,
        evidence_ref: str,
        confirmation_source_ref: str | None = None,
        now: datetime | None = None,
    ) -> IntentView:
        """把 ``proposed`` / ``planned`` 升级为 ``confirmed``。

        ``entry`` 必须是 ``user``（用户明确确认）或 ``commitment``（承诺链路
        成功）。``evidence_ref`` 是那次确认/回执的引用，写进
        ``resolution_evidence_ref``；确认来源另存 ``confirmation_source_ref``
        （终态会覆盖前者，不能把授权足迹弄丢）。

        **不产生任何段**：确认的是"目标/日期"，具体时段仍待定（§3A）。
        已经是 ``confirmed`` 时按幂等返回原行（不改确认来源与版本）。
        """
        chosen_entry = ensure_confirm_entry(entry)
        evidence = _clean_text(evidence_ref, label="evidence_ref", limit=MAX_REF_CHARS)
        source_ref = _optional_ref(confirmation_source_ref, label="confirmation_source_ref")
        moment = _aware(now)

        async with self._lock_actor():
            row = await self._load(intention_id)
            if row.state == "confirmed":
                # 幂等：第一次确认的来源就是唯一来源，重复确认不改写它。
                return self._view(row)
            ensure_transition(current=str(row.state), target="confirmed")
            row.state = "confirmed"
            row.confirmed_at = moment
            row.confirmation_source_ref = source_ref or evidence
            row.resolution_evidence_ref = evidence
            row.revision = int(row.revision) + 1
            row.horizon_rev = int(row.horizon_rev) + 1
            row.updated_at = moment
            await self._session.flush()

        logger.info(
            "schedule.intentions.confirmed actor=%s intention=%s entry=%s revision=%s",
            self._actor_id,
            row.id,
            chosen_entry,
            row.revision,
        )
        return self._view(row)

    async def mark_planned(
        self,
        intention_id: str,
        *,
        evidence_ref: str,
        now: datetime | None = None,
    ) -> IntentView:
        """``proposed → planned``：用户明确表达了"想/可能"。

        这**不是**确认：它只把候选升到"考虑中"，``confirmed`` 仍必须走
        :meth:`confirm` 的两条授权入口。重复调用幂等。
        """
        evidence = _clean_text(evidence_ref, label="evidence_ref", limit=MAX_REF_CHARS)
        moment = _aware(now)
        async with self._lock_actor():
            row = await self._load(intention_id)
            if row.state == "planned":
                return self._view(row)
            ensure_transition(current=str(row.state), target="planned")
            row.state = "planned"
            row.resolution_evidence_ref = evidence
            row.revision = int(row.revision) + 1
            row.horizon_rev = int(row.horizon_rev) + 1
            row.updated_at = moment
            await self._session.flush()
        logger.info(
            "schedule.intentions.planned actor=%s intention=%s revision=%s",
            self._actor_id,
            row.id,
            row.revision,
        )
        return self._view(row)

    # ------------------------------------------------------------ 终态

    async def reach_terminal(
        self,
        intention_id: str,
        *,
        state: str,
        evidence_ref: str | None = None,
        now: datetime | None = None,
    ) -> IntentView:
        """进入终态。**终态必须带证据**，唯一例外是 ``expired_unknown``。

        - ``completed``：需要活动/事实证据引用（非 clock-only 的校验在活动那侧）；
        - ``missed``：用户明确说没成行、或承诺链路失败；
        - ``abandoned``：明确撤回/改主意（也可以用 :meth:`revoke`，它还会处理
          未来准备段与下游意向）；
        - ``expired_unknown``：目标日过了**且没有证据**——"不知道"，不是"没做"。

        目标日还没过就来调 ``expired_unknown`` 会被拒：过期是时间事实，不是
        调用方的一句话。终态重复设置（含 confirm）一律
        :class:`IntentTerminalError`。
        """
        chosen = _clean_text(state, label="state", limit=24)
        evidence = _optional_ref(evidence_ref, label="evidence_ref")
        ensure_resolution_evidence(state=chosen, evidence_ref=evidence)
        moment = _aware(now)

        async with self._lock_actor():
            row = await self._load(intention_id)
            ensure_transition(current=str(row.state), target=chosen)
            if chosen == "expired_unknown":
                local_today = local_date(moment, str(row.target_timezone))
                if local_today <= row.target_end_day:
                    raise IntentStateError(
                        f"目标日 {row.target_end_day} 还没过（目标时区今天 {local_today}），"
                        "不能转 expired_unknown；请等目标日结束",
                        reason_code=REASON_ILLEGAL_TRANSITION,
                    )
            self._terminate(row, state=chosen, evidence_ref=evidence, now=moment)
            await self._session.flush()

        logger.info(
            "schedule.intentions.terminal actor=%s intention=%s state=%s revision=%s",
            self._actor_id,
            row.id,
            row.state,
            row.revision,
        )
        return self._view(row)

    async def expire_due(
        self, *, now: datetime | None = None, proposal_ttl: timedelta | None = PROPOSAL_TTL
    ) -> ExpiryReport:
        """确定性惰性过期：目标日已过、或模型提议超过 TTL 且无进一步证据。

        设计 §3A：「每次夜间读与事件处理先确定性惰性过期，不另起模型清扫任务」。
        两种情况都只转 ``expired_unknown``——**不会**写 ``missed``。
        """
        moment = _aware(now)
        async with self._lock_actor():
            rows = list(
                (
                    await self._session.execute(
                        select(ScheduleIntentionRow).where(
                            ScheduleIntentionRow.actor_id == self._actor_id,
                            ScheduleIntentionRow.state.notin_(INTENTION_TERMINAL_STATES),
                        )
                    )
                ).scalars()
            )
            expired: list[tuple[str, str]] = []
            for row in rows:
                local_today = local_date(moment, str(row.target_timezone))
                reason: str | None = None
                if local_today > row.target_end_day:
                    reason = REASON_EXPIRED_TARGET_DAY
                elif (
                    row.state == "proposed"
                    and proposal_ttl is not None
                    and _aware(row.created_at) + proposal_ttl <= moment
                ):
                    reason = REASON_EXPIRED_TTL
                if reason is None:
                    continue
                self._terminate(
                    row, state="expired_unknown", evidence_ref=None, now=moment
                )
                expired.append((str(row.id), reason))
            if expired:
                await self._session.flush()
        if expired:
            logger.info(
                "schedule.intentions.expired_unknown actor=%s count=%s",
                self._actor_id,
                len(expired),
            )
        return ExpiryReport(
            expired_ids=tuple(item[0] for item in expired),
            reasons=tuple(expired),
        )

    # ------------------------------------------------------------ 撤回

    async def revoke(
        self,
        intention_id: str,
        *,
        evidence_ref: str,
        reason_code: str = REASON_USER_WITHDREW,
        now: datetime | None = None,
    ) -> RevokeReport:
        """撤回一条意向：**递归**处理未来准备段子树与下游意向。

        - 意向本身 → ``abandoned``（带撤回证据）；
        - 引用它的**未来**可选段（含子段）→ ``superseded``，并抬所在
          ``day_plans.revision`` 把这次撤回记成一次局部改版；
        - 已开始/已过去/confirmed/锚点段**不动**（历史与别人的状态机）；
        - ``source_ref == 本意向 ID`` 的下游意向递归撤回（深度/数量有上限）。

        终态意向不能再撤回：新来源要新 ID/审计链（终态不可复活）。
        """
        evidence = _clean_text(evidence_ref, label="evidence_ref", limit=MAX_REF_CHARS)
        code = _clean_code(reason_code)
        moment = _aware(now)
        async with self._lock_actor():
            root = await self._load(intention_id)
            report = await self._revoke_tree(
                root, evidence_ref=evidence, reason_code=code, now=moment
            )
        logger.info(
            "schedule.intentions.revoked actor=%s root=%s reason=%s withdrawn=%s segments=%s",
            self._actor_id,
            root.id,
            code,
            len(report.withdrawn_ids),
            len(report.segment_ids),
        )
        return report

    async def revoke_by_source(
        self,
        *,
        source_kind: str,
        source_ref: str,
        evidence_ref: str,
        reason_code: str = REASON_SOURCE_CANCELLED,
        now: datetime | None = None,
    ) -> RevokeReport:
        """按**来源**传播撤销（承诺/周期规则被取消时用）。

        设计 §3A：「取消原始承诺、周期规则或用户撤回时，用来源 ID 传播撤销，
        先使关联意向终止/降级，再同步撤销未来可选准备段」。本方法只按
        ``source_kind + source_ref`` 找到未终态意向，然后逐条走 :meth:`revoke`
        的同一递归路径；来源表本身由它自己的域处置。
        """
        chosen_kind = _clean_text(source_kind, label="source_kind", limit=16)
        if chosen_kind not in INTENTION_SOURCE_KINDS:
            raise IntentValidationError(f"未知的来源类别: {chosen_kind!r}")
        chosen_ref = _clean_text(source_ref, label="source_ref", limit=MAX_REF_CHARS)
        evidence = _clean_text(evidence_ref, label="evidence_ref", limit=MAX_REF_CHARS)
        code = _clean_code(reason_code)
        moment = _aware(now)

        withdrawn: list[str] = []
        segments: list[str] = []
        skipped: list[str] = []
        plans: list[str] = []
        stale: list[str] = []
        async with self._lock_actor():
            rows = list(
                (
                    await self._session.execute(
                        select(ScheduleIntentionRow).where(
                            ScheduleIntentionRow.actor_id == self._actor_id,
                            ScheduleIntentionRow.source_kind == chosen_kind,
                            ScheduleIntentionRow.source_ref == chosen_ref,
                            ScheduleIntentionRow.state.notin_(INTENTION_TERMINAL_STATES),
                        )
                    )
                ).scalars()
            )
            for row in rows:
                if is_terminal(str(row.state)):
                    # 同一来源的另一条意向可能刚被上一条递归撤回。
                    continue
                report = await self._revoke_tree(
                    row, evidence_ref=evidence, reason_code=code, now=moment
                )
                withdrawn.extend(report.withdrawn_ids)
                segments.extend(report.segment_ids)
                skipped.extend(report.skipped_segment_ids)
                plans.extend(report.plan_ids)
                stale.extend(report.stale_plan_ids)
            if rows:
                await self._session.flush()
        return RevokeReport(
            root_id="",
            reason_code=code,
            withdrawn_ids=tuple(dict.fromkeys(withdrawn)),
            segment_ids=tuple(dict.fromkeys(segments)),
            skipped_segment_ids=tuple(dict.fromkeys(skipped)),
            plan_ids=tuple(dict.fromkeys(plans)),
            stale_plan_ids=tuple(dict.fromkeys(stale)),
        )

    # ------------------------------------------------------------ 内部

    async def _load(self, intention_id: str) -> ScheduleIntentionRow:
        row = await self._session.get(ScheduleIntentionRow, intention_id)
        if row is None or str(row.actor_id) != self._actor_id:
            raise IntentNotFound(f"意向不存在: {intention_id}")
        return row

    async def _find_by_source(
        self, *, source_kind: str, source_ref: str, start_day: date, end_day: date
    ) -> ScheduleIntentionRow | None:
        return (
            await self._session.execute(
                select(ScheduleIntentionRow).where(
                    ScheduleIntentionRow.actor_id == self._actor_id,
                    ScheduleIntentionRow.source_kind == source_kind,
                    ScheduleIntentionRow.source_ref == source_ref,
                    ScheduleIntentionRow.target_start_day == start_day,
                    ScheduleIntentionRow.target_end_day == end_day,
                )
            )
        ).scalar_one_or_none()

    async def _capacity_counts(self) -> tuple[int, int]:
        rows = await self._session.execute(
            select(ScheduleIntentionRow.state, func.count())
            .where(
                ScheduleIntentionRow.actor_id == self._actor_id,
                ScheduleIntentionRow.state.notin_(INTENTION_TERMINAL_STATES),
            )
            .group_by(ScheduleIntentionRow.state)
        )
        counts = {str(state): int(count) for state, count in rows}
        return sum(counts.values()), counts.get("proposed", 0)

    async def _default_timezone(self) -> str:
        """没有显式给目标时区时，用角色规范时区的**唯一解析入口**。"""
        from ling.domains.agenda.timeline import resolve_plan_timezone

        name, _source = await resolve_plan_timezone(self._session)
        return name

    def _view(self, row: ScheduleIntentionRow) -> IntentView:
        return IntentView(
            intention_id=str(row.id),
            actor_id=str(row.actor_id),
            kind=str(row.kind),
            title=str(row.title),
            state=str(row.state),
            source_kind=str(row.source_kind),
            source_ref=str(row.source_ref),
            target_start_day=row.target_start_day,
            target_end_day=row.target_end_day,
            target_timezone=str(row.target_timezone),
            priority=int(row.priority),
            visibility=str(row.visibility),
            revision=int(row.revision),
            horizon_rev=int(row.horizon_rev),
            prep_source_ref=row.prep_source_ref,
            prep_due_day=row.prep_due_day,
            confirmation_source_ref=row.confirmation_source_ref,
            confirmed_at=row.confirmed_at,
            resolution_evidence_ref=row.resolution_evidence_ref,
            resolved_at=row.resolved_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _terminate(
        self,
        row: ScheduleIntentionRow,
        *,
        state: str,
        evidence_ref: str | None,
        now: datetime,
    ) -> None:
        row.state = state
        row.resolution_evidence_ref = evidence_ref
        row.resolved_at = now
        row.revision = int(row.revision) + 1
        row.horizon_rev = int(row.horizon_rev) + 1
        row.updated_at = now

    async def _downstream(self, intention_id: str) -> list[ScheduleIntentionRow]:
        """``source_ref == 上游 intention_id`` 的未终态意向（派生关系约定）。"""
        return list(
            (
                await self._session.execute(
                    select(ScheduleIntentionRow).where(
                        ScheduleIntentionRow.actor_id == self._actor_id,
                        ScheduleIntentionRow.source_ref == intention_id,
                        ScheduleIntentionRow.state.notin_(INTENTION_TERMINAL_STATES),
                    )
                )
            ).scalars()
        )

    async def _revoke_tree(
        self,
        root: ScheduleIntentionRow,
        *,
        evidence_ref: str,
        reason_code: str,
        now: datetime,
    ) -> RevokeReport:
        if is_terminal(str(root.state)):
            raise IntentTerminalError(
                f"意向 {root.id} 已是终态 {root.state}，不能撤回（终态不可复活）",
                reason_code=REASON_TERMINAL,
            )

        visited: set[str] = set()
        stack: list[tuple[ScheduleIntentionRow, str]] = [(root, reason_code)]
        withdrawn: list[str] = []
        segment_ids: list[str] = []
        skipped: list[str] = []
        plan_ids: list[str] = []
        stale: list[str] = []
        while stack and len(visited) < MAX_REVOKE_NODES:
            row, code = stack.pop()
            if str(row.id) in visited:
                continue
            if is_terminal(str(row.state)):
                # 下游已经是终态：不复活、不重写，跳过即可。
                continue
            visited.add(str(row.id))
            self._terminate(row, state="abandoned", evidence_ref=evidence_ref, now=now)
            withdrawn.append(str(row.id))
            for child in await self._downstream(str(row.id)):
                stack.append((child, REASON_UPSTREAM_WITHDRAWN))
            (
                cancelled,
                untouched,
                plans,
                stale_plans,
            ) = await self._cancel_future_segments(
                intention_ids=[str(row.id)], now=now, reason_code=code
            )
            segment_ids.extend(cancelled)
            skipped.extend(untouched)
            plan_ids.extend(plans)
            stale.extend(stale_plans)
        if stack:
            logger.warning(
                "schedule.intentions.revoke_truncated actor=%s root=%s remaining=%s",
                self._actor_id,
                root.id,
                len(stack),
            )
        return RevokeReport(
            root_id=str(root.id),
            reason_code=reason_code,
            withdrawn_ids=tuple(withdrawn),
            segment_ids=tuple(segment_ids),
            skipped_segment_ids=tuple(skipped),
            plan_ids=tuple(dict.fromkeys(plan_ids)),
            stale_plan_ids=tuple(dict.fromkeys(stale)),
        )

    async def _cancel_future_segments(
        self, *, intention_ids: Sequence[str], now: datetime, reason_code: str
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """取消引用这些意向的**未来**可选段（含直接子段）。

        返回 ``(已取消段 ID, 未动段 ID, 受影响 plan ID, CAS 失败的 plan ID)``。
        抬 ``day_plans.revision`` 的做法与 ``agenda/timeline.seam_check`` 一致：
        只改这一版里的受影响段，不重生成整天。
        """
        ids = [str(item) for item in intention_ids if item]
        if not ids:
            return [], [], [], []
        roots = list(
            (
                await self._session.execute(
                    select(ScheduleSegmentRow).where(
                        ScheduleSegmentRow.actor_id == self._actor_id,
                        ScheduleSegmentRow.intention_id.in_(ids),
                        ScheduleSegmentRow.status == "current",
                        ScheduleSegmentRow.start_at >= now,
                    )
                )
            ).scalars()
        )
        if not roots:
            return [], [], [], []
        # 段树是 block → hour → minute 三层（设计 §2），所以子段要**逐层**收集：
        # 只查 "parent_id IN 根段" 会漏掉孙段，撤回后时间轴里还留着一个 minute
        # 子段指向已撤回的准备大块。
        candidates: dict[str, ScheduleSegmentRow] = {
            str(item.id): item for item in roots
        }
        frontier = list(candidates)
        guard = 0
        while frontier and guard < MAX_SEGMENT_TREE_NODES:
            guard += len(frontier)
            children = list(
                (
                    await self._session.execute(
                        select(ScheduleSegmentRow).where(
                            ScheduleSegmentRow.actor_id == self._actor_id,
                            ScheduleSegmentRow.parent_id.in_(frontier),
                            ScheduleSegmentRow.status == "current",
                        )
                    )
                ).scalars()
            )
            frontier = []
            for child in children:
                key = str(child.id)
                if key in candidates:
                    continue
                candidates[key] = child
                frontier.append(key)
        if frontier:
            logger.warning(
                "schedule.intentions.segment_tree_truncated actor=%s nodes=%s",
                self._actor_id,
                len(candidates),
            )

        cancelled = [item for item in candidates.values() if _movable_segment(item)]
        untouched = [item for item in candidates.values() if not _movable_segment(item)]

        new_revisions: dict[str, int | None] = {}
        stale: list[str] = []
        for plan_id in {str(item.plan_id) for item in cancelled}:
            plan = await self._session.get(DayPlanRow, plan_id)
            if plan is None:
                new_revisions[plan_id] = None
                continue
            expected = int(plan.revision)
            result = await self._session.execute(
                update(DayPlanRow)
                .where(DayPlanRow.id == plan_id, DayPlanRow.revision == expected)
                .values(revision=expected + 1, updated_at=now)
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                new_revisions[plan_id] = expected + 1
            else:
                # 另一个 worker 正在提交新版本：不硬覆盖，用当前 revision 记历史，
                # 让调用方知道这次撤回是"降级完成"的。
                new_revisions[plan_id] = expected
                stale.append(plan_id)

        for segment in cancelled:
            segment.status = "superseded"
            segment.revision_ended = new_revisions.get(str(segment.plan_id))
            segment.source_event_ids = _append_unique(
                segment.source_event_ids,
                f"{REASON_TAG_PREFIX}{reason_code}",
                *[f"intention:{item}" for item in ids],
            )
            segment.updated_at = now
        await self._session.flush()
        return (
            [str(item.id) for item in cancelled],
            [str(item.id) for item in untouched],
            sorted({str(item.plan_id) for item in cancelled}),
            sorted(set(stale)),
        )

    def _lock_actor(self) -> _ActorLock:
        return _ActorLock(self._session, self._actor_id)


class _ActorLock:
    """actor 级的 PostgreSQL 事务级 advisory lock（其它方言下退化为无锁）。

    多 worker 下容量判定必须串行：两个请求同时看到 3 条 proposed，各自插入
    就会变成 5 条。锁的 key 只由 actor 决定，不包含目标日——容量是 actor 级
    事实。非 PostgreSQL（本地 sqlite 开发库）没有 advisory lock，此时并发
    写由唯一键兜底，容量上限在单进程内仍然成立。
    """

    def __init__(self, session: AsyncSession, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    async def __aenter__(self) -> None:
        bind = self._session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        key = zlib.crc32(f"schedule_intentions:{self._actor_id}".encode()) % 2_147_483_648
        await self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
        )

    async def __aexit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------- 与时间轴的接缝


def _valid_timezone(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


async def attach_preparation(
    session: AsyncSession,
    *,
    intention_id: str,
    segment_id: str,
    actor_id: str | None = None,
    preparation_for_day: date | None = None,
    source_visibilities: Sequence[str] = (),
    now: datetime | None = None,
) -> ScheduleSegmentRow:
    """给一条**已确认且有前置依据**的意向挂一个准备段（§3A/§6）。

    只做关联与校验，**不生成时间轴**：段由 ``agenda/timeline.py`` 生成，本函数
    拒绝把普通段/锚点段/已过去的段借给它当"准备"。校验顺序：

    1. 意向存在、同 actor、``state == confirmed``；
    2. 意向有 ``prep_source_ref``（等价地也有 ``prep_due_day``）——用户确认
       "目标"**不顺带确认**模型猜的准备事项；
    3. 段同 actor、``current``、未开始、``source == model``、``tentative``、
       非锚点、还没有绑定别的意向；
    4. 目标开始日**晚于**段所在 ``plan_day``（准备不能在同一天/过去）；
    5. ``preparation_for_day`` 落在目标区间内且晚于 ``plan_day``。

    成功后写入 ``intention_id`` / ``preparation_for_day``，并把三个来源里
    **最严**的可见范围写到段的 ``visibility``；``source_event_ids`` 留下意向
    ID 与确认来源，作为"这条段为什么存在"的审计。
    """
    moment = _aware(now)
    intention = await session.get(ScheduleIntentionRow, intention_id)
    if intention is None or (actor_id is not None and str(intention.actor_id) != actor_id):
        raise IntentNotFound(f"意向不存在: {intention_id}")
    # 与 confirm/revoke 共用同一把 actor 锁：撤回/确认不能和"挂准备"交错，
    # 否则刚被撤回的意向还能被挂上一个新的准备段。
    async with _ActorLock(session, str(intention.actor_id)):
        await session.refresh(intention)
        if str(intention.state) != "confirmed":
            raise IntentStateError(
                f"只有 confirmed 的意向可以挂准备段（当前 {intention.state}）："
                "proposed/planned 不得生成专属准备段",
                reason_code=REASON_PREPARATION_REQUIRES_CONFIRMED,
            )
        if not intention.prep_source_ref:
            raise IntentStateError(
                "这条意向没有 prep_source_ref：用户确认目标不等于确认准备事项，"
                "不得生成专属准备段",
                reason_code=REASON_PREPARATION_REQUIRES_SOURCE,
            )

        segment = await session.get(ScheduleSegmentRow, segment_id)
        if segment is None or str(segment.actor_id) != str(intention.actor_id):
            raise IntentNotFound(f"段不存在: {segment_id}")
        if str(segment.status) != "current":
            raise IntentStateError(
                f"只有 current 的段可以挂准备（当前 {segment.status}）",
                reason_code=REASON_PREPARATION_NOT_OPTIONAL,
            )
        if _aware(segment.start_at) < moment:
            raise IntentStateError(
                "已经开始的段不能挂准备（只撤/改未来可选段）",
                reason_code=REASON_PREPARATION_IN_PAST,
            )
        if (
            str(segment.source) != "model"
            or str(segment.commitment_level) == "confirmed"
            or str(segment.anchor_kind) != ""
        ):
            raise IntentStateError(
                "准备段必须是可选的模型段（source=model、tentative、非锚点）",
                reason_code=REASON_PREPARATION_NOT_OPTIONAL,
            )
        if segment.intention_id is not None and str(segment.intention_id) != intention_id:
            raise IntentStateError(
                f"段 {segment.id} 已绑定到另一条意向: {segment.intention_id}",
                reason_code=REASON_PREPARATION_ALREADY_BOUND,
            )

        plan = await session.get(DayPlanRow, str(segment.plan_id))
        if plan is None:
            raise IntentNotFound(f"段所属计划不存在: {segment.plan_id}")
        target_day = preparation_for_day or intention.target_start_day
        if intention.target_start_day <= plan.plan_day:
            raise IntentStateError(
                f"目标开始日 {intention.target_start_day} 不晚于准备段所在计划日 "
                f"{plan.plan_day}：这不是前置准备",
                reason_code=REASON_PREPARATION_TARGET_NOT_FUTURE,
            )
        if not (intention.target_start_day <= target_day <= intention.target_end_day):
            raise IntentStateError(
                f"准备段的目标日 {target_day} 不在意向区间 "
                f"{intention.target_start_day}..{intention.target_end_day}",
                reason_code=REASON_PREPARATION_DAY_OUT_OF_RANGE,
            )
        if target_day <= plan.plan_day:
            raise IntentStateError(
                f"准备段的目标日 {target_day} 必须晚于段所在计划日 {plan.plan_day}",
                reason_code=REASON_PREPARATION_DAY_OUT_OF_RANGE,
            )

        segment.intention_id = intention_id
        segment.preparation_for_day = target_day
        segment.visibility = strictest_visibility(
            segment.visibility,
            intention.visibility,
            *source_visibilities,
        )
        segment.source_event_ids = _append_unique(
            segment.source_event_ids,
            f"intention:{intention_id}",
            str(intention.confirmation_source_ref or ""),
        )
        segment.updated_at = moment
        await session.flush()
        logger.info(
            "schedule.intentions.preparation_attached actor=%s intention=%s segment=%s "
            "preparation_for=%s visibility=%s",
            intention.actor_id,
            intention_id,
            segment.id,
            target_day,
            segment.visibility,
        )
        return segment


async def segments_for_intention(
    session: AsyncSession,
    intention_id: str,
    *,
    statuses: Sequence[str] = ("current",),
    future_from: datetime | None = None,
) -> list[ScheduleSegmentRow]:
    """查某条意向引用到的段（默认只看 ``current``），按开始时间排序。"""
    stmt = select(ScheduleSegmentRow).where(
        ScheduleSegmentRow.intention_id == intention_id
    )
    chosen = tuple(str(item) for item in statuses)
    if chosen:
        stmt = stmt.where(ScheduleSegmentRow.status.in_(chosen))
    if future_from is not None:
        stmt = stmt.where(ScheduleSegmentRow.start_at >= _aware(future_from))
    stmt = stmt.order_by(ScheduleSegmentRow.start_at)
    return list((await session.execute(stmt)).scalars())


async def intention_for_segment(
    session: AsyncSession, segment_id: str
) -> ScheduleIntentionRow | None:
    """段引用的意向（没有绑定就返回 ``None``，不猜来源）。"""
    segment = await session.get(ScheduleSegmentRow, segment_id)
    if segment is None or not segment.intention_id:
        return None
    return await session.get(ScheduleIntentionRow, str(segment.intention_id))


__all__ = [
    "CONFIRM_ENTRIES",
    "CONFIRM_ENTRY_COMMITMENT",
    "CONFIRM_ENTRY_USER",
    "CREATABLE_STATES",
    "MAX_ACTIVE_INTENTIONS",
    "MAX_PROPOSED_INTENTIONS",
    "MAX_TARGET_HORIZON_DAYS",
    "MAX_TARGET_SPAN_DAYS",
    "PROPOSAL_TTL",
    "REASON_EVIDENCE_REQUIRED",
    "REASON_EXPIRED_TARGET_DAY",
    "REASON_EXPIRED_TTL",
    "REASON_INTENTION_CAPACITY",
    "REASON_SOURCE_CANCELLED",
    "REASON_UPSTREAM_WITHDRAWN",
    "REASON_USER_WITHDREW",
    "CapacityVerdict",
    "ExpiryReport",
    "IntentCapacityError",
    "IntentError",
    "IntentEvidenceError",
    "IntentNotFound",
    "IntentService",
    "IntentStateError",
    "IntentTerminalError",
    "IntentValidationError",
    "IntentView",
    "RevokeReport",
    "attach_preparation",
    "ensure_confirm_entry",
    "ensure_resolution_evidence",
    "ensure_target_window",
    "ensure_transition",
    "evaluate_capacity",
    "intention_for_segment",
    "is_terminal",
    "local_date",
    "segments_for_intention",
    "strictest_visibility",
]
