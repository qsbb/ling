"""她的一天（阶段 1）：两个"日"的边界、时间轴校验与每日大纲生成。

## 两个日，一个真实时钟

- **展示日**：用户有效时区的 ``[D 00:00, D+1 00:00)``；
- **计划日**：角色规范时区的 ``[D 04:00, D+1 04:00)``，以起点日期 D 命名。

计划日时区取 ``ling/settings/user_preferences.py`` 的**唯一解析入口**
（DB 偏好 → ``LING_USER_TIMEZONE`` → 默认），不读服务器时区、也不偷用
``actor_routines.timezone`` 另算一套"今天"。作息表里的时区只作为**旧值**：
若它与规范时区不一致，大纲仍按规范时区排，并把冲突记在 outcome 上（不静默）。

D 04:00 生成的是 **D 04:00 → D+1 04:00 的可变部分**，外加 D 夜里的跨夜睡眠锚点
（可越过 D+1 04:00）。**D 的 00:00–04:00 只来自 D−1 计划日的跨夜段，绝不回填**：
"04:00 补排今天"不等于"今天 00:00–04:00 的睡眠是刚生成的"。

## 时间轴语义（全部左闭右开、UTC 入库）

- 任意分钟粒度，不强制整点；允许空档（空档 = 自由/未决定，UI 不得虚构活动）；
- 大块 / 小时 / 分钟三层都是**意向**，父子段有意重叠；只有叶段能启动活动；
- 睡眠与 confirmed 承诺是**不可移动锚点**，模型段不得压上去；
- 段从不物理删除：改版时旧段置 ``superseded`` 并记 ``revision_ended``。

## 阶段 1 的边界

只做「日界 + 大纲 + 幂等/预算/兜底」，**不做**近窗细化、提议重排、梦、跨夜情绪
结算。本模块没有 worker 定时接线，唯一的调度入口是 :func:`run_due_outline`，
测试里直接调它。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import (
    ActorRoutineRow,
    AgendaPlanRow,
    BehaviorModelCallRow,
    CommitmentRow,
    DayPlanRow,
    Persona,
    PresenceState,
    ScheduleSegmentRow,
    SelfExperienceRow,
    WorldPlace,
    utcnow,
)
from ling.infra.security import new_id
from ling.ports.model import ChatMessageIn, ModelError

logger = logging.getLogger("ling.domains.agenda.timeline")

# 计划日的起点小时（本地墙钟）。04:00 是**后台排程截点**，不是角色"醒来思考"
# 的剧情事件：睡眠场景与梦另算（设计 §0）。
PLAN_DAY_START_HOUR = 4
PLAN_DAY_START_MINUTE = PLAN_DAY_START_HOUR * 60

# 大纲只用 `memory.extract` 一个职责（设计 §0：不借 `dialogue.fast`）。
OUTLINE_PROFILE = "memory.extract"
OUTLINE_KIND = "outline"

# 预算：设计 §0 的表。上界 2 次调用 / 7,000 token per actor per 计划日。
OUTLINE_MAX_CALLS = 2
OUTLINE_RESERVED_INPUT_TOKENS = 2500
OUTLINE_RESERVED_OUTPUT_TOKENS = 1000
OUTLINE_DAILY_TOKEN_BUDGET = 7000

# 模型超时 60 秒、lease 90 秒（设计 §3）。这里 lease 取 120 秒留余量：
# lease 只管"别让两个 worker 同时烧钱"，不需要卡在超时边界上。
OUTLINE_TIMEOUT_SECONDS = 60.0
OUTLINE_LEASE_SECONDS = 120.0

# 目标 5–8 个大块；少一点也可以（"今天暂时没什么特别安排"是合法结果），
# 多到 10 个以上就说明模型在写流水账，直接拒收。
OUTLINE_MIN_BLOCKS = 1
OUTLINE_MAX_BLOCKS = 10

# 模型可选的活动类别（白名单）。**不含 sleep**：睡眠只能由规则/作息锚点产生。
MODEL_KINDS: frozenset[str] = frozenset(
    {
        "meal",
        "work",
        "study",
        "reading",
        "exercise",
        "walk",
        "chore",
        "errand",
        "leisure",
        "social",
        "hobby",
        "rest",
        "commute",
        "free",
    }
)
FLEXIBILITIES: tuple[str, ...] = ("fixed", "flexible")

SLEEP_ANCHOR = "sleep"
COMMITMENT_ANCHOR = "commitment"
# 承诺只有"到期时刻"，没有时长。锚点用 [due, due+60) 作为**保护窗**，
# 不是"她答应做一小时"——库里没有那个事实，不能编。
COMMITMENT_PROTECTIVE_MINUTES = 60

# 同一计划日最多追一次（catch-up）：离休息不足这个余量时只写规则睡眠锚点。
CATCH_UP_MIN_MINUTES_BEFORE_REST = 40

# 一段经历/一条承诺进 prompt 的上限：prompt 有硬上限，宁可少给。
MAX_EXPERIENCE_SUMMARIES = 5
MAX_PLACES = 12
EXPERIENCE_LOOKBACK_HOURS = 72

_OUTLINE_INSTRUCTIONS = """你是角色「{name}」的日计划器。

现在要为她规划**计划日 {plan_day}**（本地时区 {timezone}）的粗粒度安排。
计划日窗口是本地 {window_local_start} → {window_local_end}；当前本地时间是
{now_local}。可变部分从 {not_before_local} 开始（过去的时间不排）。

硬规则（违反任何一条整份结果作废）：
1. 只输出 JSON 对象，形如
   {{"segments": [{{"kind": "...", "title": "...", "start_at": "ISO 带偏移",
   "end_at": "ISO 带偏移", "place_id": null 或地点 ID, "flexibility": "flexible"}}]}}
   **不要**输出解释、Markdown 代码块或任何其它字段。
2. 5–8 个大块为目标；可以更少，但**不能为空**。这些是**意向**，不是已经发生的事。
3. 时间必须是带时区偏移的 ISO-8601，精确到分钟；`end_at` 必须晚于 `start_at`。
4. 不得写过去；不得早于上面说的可变起点；不得跨出计划日窗口。
5. 段与段之间**不得重叠**（可以有空档，空档表示还没决定，不是你随便填的理由）。
6. 不得与下面给出的**不可移动锚点**（睡眠/已确认约定）重叠，也不要写 `sleep`。
7. `kind` 只能取：{kinds}。
8. `place_id` 只能取 {places} 中的一个，或 null（表示不需要具体地点）。
9. 不要写"做了什么/吃到了什么"这类完成事实，也不要写梦境；只写打算。

输入快照（JSON）：
{snapshot}
"""

_REPAIR_INSTRUCTIONS = """上一次输出没有通过校验，请**只重新输出**完整的 JSON 对象。
校验失败原因（逐条修正，不要解释）：
{problems}

仍然遵守全部硬规则；不要重复上次的错误。
"""


# ---------------------------------------------------------------- 纯时间函数
#
# 这些函数不碰数据库、不读全局，单测直接打表（设计 §9 的程序化验收）。


def resolve_zone(name: str | None) -> ZoneInfo:
    """IANA 名 → ``ZoneInfo``；非法/为空退回 UTC（不是服务器本地时区）。

    服务器时区与用户无关：按它算"今天"会让同一份数据在不同机器上落到不同日期。
    """
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _local_minute_instant(day: date, minute: int, zone: ZoneInfo) -> datetime:
    """本地墙钟 ``day + minute`` → UTC 瞬时，处理 DST 的不存在/重复时刻。

    - **不存在**（春季跳变跳过的那一小时）：顺延到该日本地下一个有效分钟；
    - **重复**（秋季回拨）：取**第一次**出现的瞬时（``fold=0``）并记录在调用方
      的 offset 里；校验与去重全用 UTC 半开区间，不用 ``HH:MM`` 字符串。

    不能用 ``本地时刻 + 24h`` 求次日边界：DST 当天只有 23 或 25 小时。
    """
    naive = datetime.combine(day, time.min) + timedelta(minutes=int(minute))
    for _ in range(24 * 60):
        candidate = naive.replace(tzinfo=zone, fold=0)
        back = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if back == naive:
            return candidate.astimezone(UTC)
        # 这个本地时刻在该日不存在：往后挪一分钟再试。
        naive += timedelta(minutes=1)
    raise ValueError(f"本地时刻无法解析：{day} + {minute} 分钟（{zone.key}）")


def plan_day_window(plan_day: date, timezone: str) -> tuple[datetime, datetime]:
    """计划日的 UTC 半开区间 ``[D 04:00, D+1 04:00)``（按本地墙钟组合）。"""
    zone = resolve_zone(timezone)
    start = _local_minute_instant(plan_day, PLAN_DAY_START_MINUTE, zone)
    end = _local_minute_instant(plan_day + timedelta(days=1), PLAN_DAY_START_MINUTE, zone)
    return start, end


def display_day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    """展示日的 UTC 半开区间 ``[D 00:00, D+1 00:00)``。"""
    zone = resolve_zone(timezone)
    start = _local_minute_instant(day, 0, zone)
    end = _local_minute_instant(day + timedelta(days=1), 0, zone)
    return start, end


def local_minute_instant(day: date, minute: int, timezone: str | None) -> datetime:
    """公开版本：本地墙钟 ``day + minute`` 的 UTC 瞬时（DST 规则同上）。"""
    return _local_minute_instant(day, minute, resolve_zone(timezone))


def plan_day_for(instant: datetime, timezone: str | None) -> date:
    """某个瞬时落在哪个**计划日**（``local < 04:00`` 算前一天）。"""
    local = _aware(instant).astimezone(resolve_zone(timezone))
    day = local.date()
    if local.hour * 60 + local.minute < PLAN_DAY_START_MINUTE:
        day -= timedelta(days=1)
    return day


def display_day_for(instant: datetime, timezone: str | None) -> date:
    """某个瞬时落在哪个**展示日**（用户本地日历日）。"""
    return _aware(instant).astimezone(resolve_zone(timezone)).date()


def plan_day_length(plan_day: date, timezone: str) -> timedelta:
    """计划日的真实长度：DST 当天是 23 / 25 小时，不是固定 24。"""
    start, end = plan_day_window(plan_day, timezone)
    return end - start


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """两个半开区间是否相交（端点相接不算重叠）。"""
    return a_start < b_end and b_start < a_end


def contains(outer_start: datetime, outer_end: datetime, inner_start: datetime, inner_end: datetime) -> bool:
    """``inner`` 是否完整落在 ``outer`` 内（父子段的包含判据）。"""
    return outer_start <= inner_start and inner_end <= outer_end


def local_hhmm(instant: datetime, timezone: str) -> str:
    """本地 ``HH:MM`` —— 只用于**展示/提示词**，校验与去重都用 UTC 区间。"""
    return _aware(instant).astimezone(resolve_zone(timezone)).strftime("%H:%M")


# ---------------------------------------------------------------- 锚点


@dataclass(frozen=True)
class Anchor:
    """不可移动锚点：睡眠或已确认的约定。

    ``protective`` 说明这个区间是**保护窗**还是真实时长：
    - 睡眠来自作息，区间就是作息窗；
    - 承诺只有到期时刻，区间是 ``[due, due+60)`` 的保护窗，不代表"她要花一小时"。
    """

    kind: str  # sleep | commitment
    start_at: datetime
    end_at: datetime
    title: str
    protective: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "title": self.title,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
        }


def build_sleep_anchor(
    plan_day: date, *, timezone: str, rest_minute: int, wake_minute: int
) -> Anchor | None:
    """计划日夜里的跨夜睡眠锚点：``D rest`` → ``D+1 wake``（可越过 D+1 04:00）。

    存的是完整跨夜区间，不按计划日窗口裁剪：D+1 00:00–04:00 的展示要能显示
    "在睡觉"，而 D+1 04:00 生成的日计划**不能**回填这一段。

    ``rest < 04:00`` 时跳过：那段睡眠属于**D−1** 计划日的夜间锚点，
    由 D−1 的行负责；D 的 00:00–04:00 不再补一个"已经睡过"。
    """
    if rest_minute == wake_minute:
        # 作息没有有效窗口（全天醒着或没有睡眠段）：不造睡眠锚点，
        # 也不假定"她一定睡了"。
        return None
    if rest_minute < PLAN_DAY_START_MINUTE:
        return None
    start = _local_minute_instant(plan_day, rest_minute, resolve_zone(timezone))
    wake_day = plan_day + timedelta(days=1)
    end = _local_minute_instant(wake_day, wake_minute, resolve_zone(timezone))
    if end <= start:
        return None
    return Anchor(
        kind=SLEEP_ANCHOR,
        start_at=start,
        end_at=end,
        title="睡觉",
        protective=False,
    )


async def load_commitment_anchors(
    session: AsyncSession, *, actor_id: str, window_start: datetime, window_end: datetime
) -> list[Anchor]:
    """窗口内（含保护窗）的不可移动约定。

    来源两类，都是**已经存在的事实**，不是这次大纲要生成的内容：
    - ``commitments``（对人的承诺，``state=active``）；
    - ``agenda_plans`` 里 ``commitment_level=confirmed`` 的约定。
    """
    anchors: list[Anchor] = []
    pad = timedelta(minutes=COMMITMENT_PROTECTIVE_MINUTES)
    rows = (
        await session.execute(
            select(CommitmentRow).where(
                CommitmentRow.actor_id == actor_id,
                CommitmentRow.state == "active",
                CommitmentRow.due_at >= window_start - pad,
                CommitmentRow.due_at < window_end + pad,
            )
        )
    ).scalars()
    for row in rows:
        due = _aware(row.due_at)
        anchors.append(
            Anchor(
                kind=COMMITMENT_ANCHOR,
                start_at=due,
                end_at=due + pad,
                title=f"约定：{_short(str(row.text), 40)}",
                protective=True,
            )
        )
    plans = (
        await session.execute(
            select(AgendaPlanRow).where(
                AgendaPlanRow.actor_id == actor_id,
                AgendaPlanRow.commitment_level == "confirmed",
                AgendaPlanRow.state.in_(["planned", "active"]),
                AgendaPlanRow.end_at > window_start,
                AgendaPlanRow.start_at < window_end,
            )
        )
    ).scalars()
    for row in plans:
        anchors.append(
            Anchor(
                kind=COMMITMENT_ANCHOR,
                start_at=_aware(row.start_at),
                end_at=_aware(row.end_at),
                title=f"约定：{_short(str(row.title), 40)}",
            )
        )
    anchors.sort(key=lambda item: item.start_at)
    return anchors


# ---------------------------------------------------------------- 模型输出校验


class OutlineValidationError(ValueError):
    """模型输出不合约：带**逐条**原因，供修复重试使用（不带原文）。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class PlannedBlock:
    """校验通过的一个可变块（还是意向，不是活动）。"""

    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None
    flexibility: str


_ALLOWED_KEYS = {"kind", "title", "start_at", "end_at", "place_id", "flexibility"}


def parse_outline_payload(raw: str) -> list[dict[str, object]]:
    """严格解析模型输出：只接受 ``{"segments": [...]}`` 这一个形状。

    代码围栏、解释文字、额外顶层字段、``segments`` 不是数组——全部按非法处理。
    **不做宽容修**：宽容会把"模型没按契约输出"变成"我们猜它的意思"，
    而猜错的代价是往时间轴上写一段不存在的事。
    """
    text = raw.strip()
    if not text:
        raise OutlineValidationError(["输出为空"])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OutlineValidationError([f"不是合法 JSON（{exc.msg} @ {exc.pos}）"]) from exc
    if not isinstance(payload, dict):
        raise OutlineValidationError(["顶层必须是 JSON 对象"])
    extra = set(payload) - {"segments"}
    if extra:
        raise OutlineValidationError([f"顶层有未定义字段：{sorted(extra)}"])
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise OutlineValidationError(["segments 必须是数组"])
    return segments


def validate_outline(
    segments: list[object],
    *,
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
    anchors: list[Anchor] | tuple[Anchor, ...],
    valid_place_ids: set[str] | frozenset[str],
) -> list[PlannedBlock]:
    """把模型输出校验成可入库的块；任何一条不合就整份拒收。

    拒收的六类（设计 §3 / §8）：过去时间、越界、互相重叠、压锚点、
    未知地点、字段/枚举不合。**不"只提交前半段"**：半份时间轴比没有更难解释。
    """
    problems: list[str] = []
    if not isinstance(segments, list):
        raise OutlineValidationError(["segments 必须是数组"])
    if len(segments) < OUTLINE_MIN_BLOCKS:
        raise OutlineValidationError(["segments 为空：至少要有 1 个意向块"])
    if len(segments) > OUTLINE_MAX_BLOCKS:
        problems.append(
            f"段数 {len(segments)} 超过上限 {OUTLINE_MAX_BLOCKS}（不要写流水账）"
        )

    blocks: list[PlannedBlock] = []
    for index, raw in enumerate(segments):
        label = f"segments[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} 不是对象")
            continue
        unknown = set(raw) - _ALLOWED_KEYS
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
        if not clean_title or len(clean_title) > 120:
            problems.append(f"{label}.title 长度必须在 1..120")
            continue
        flexibility = raw.get("flexibility", "flexible")
        if not isinstance(flexibility, str) or flexibility not in FLEXIBILITIES:
            problems.append(f"{label}.flexibility 必须是 {FLEXIBILITIES}")
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
        if start < not_before:
            problems.append(f"{label} 写到了过去/早于可变起点（{start.isoformat()}）")
            continue
        if start < window_start or end > window_end:
            problems.append(f"{label} 越出了计划日窗口")
            continue
        place = raw.get("place_id")
        if place is not None and (not isinstance(place, str) or place not in valid_place_ids):
            problems.append(f"{label}.place_id 是未知地点：{place!r}")
            continue
        blocks.append(
            PlannedBlock(
                kind=kind,
                title=clean_title,
                start_at=start,
                end_at=end,
                place_id=place,
                flexibility=flexibility,
            )
        )

    # 互相重叠：排序后相邻比较即可（半开区间）。
    ordered = sorted(blocks, key=lambda item: item.start_at)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if overlaps(previous.start_at, previous.end_at, current.start_at, current.end_at):
            problems.append(
                "段之间重叠："
                f"{previous.start_at.isoformat()}–{previous.end_at.isoformat()} 与 "
                f"{current.start_at.isoformat()}–{current.end_at.isoformat()}"
            )

    # 压不可移动锚点：睡眠/confirmed 承诺不可移动。
    for block in blocks:
        for anchor in anchors:
            if overlaps(block.start_at, block.end_at, anchor.start_at, anchor.end_at):
                problems.append(
                    f"段 {block.title!r}（{block.start_at.isoformat()}）压到了不可移动锚点"
                    f"「{anchor.title}」"
                )

    if problems:
        raise OutlineValidationError(problems)
    return blocks


def _parse_iso_minute(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutlineValidationError([f"{label} 必须是 ISO-8601 字符串"])
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise OutlineValidationError([f"{label} 不是合法 ISO-8601：{exc}"]) from exc
    if parsed.tzinfo is None:
        raise OutlineValidationError([f"{label} 必须带时区偏移（不要写本地朴素时间）"])
    if parsed.second or parsed.microsecond:
        raise OutlineValidationError([f"{label} 必须精确到分钟（秒/微秒必须为 0）"])
    return parsed.astimezone(UTC)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 运行期快照


@dataclass(frozen=True)
class RoutineSnapshot:
    """作息快照（wake/rest 分钟按**规范时区**解释）。

    ``timezone`` 是旧作息表里的时区：仅用于检测冲突，不参与日界计算。
    """

    wake_minute: int
    rest_minute: int
    revision: int
    timezone: str

    @property
    def sleep_window_minutes(self) -> int:
        return (self.wake_minute - self.rest_minute) % 1440 or 1440


@dataclass(frozen=True)
class OutlineOutcome:
    """一次"确保今天有大纲"的结果。字段都是可解释的，不含 prompt/正文。"""

    plan_id: str
    plan_day: date
    state: str  # ready / degraded / unchanged / busy
    revision: int
    segments: int
    source: str  # model / rule / baseline
    calls: int = 0
    reason_code: str | None = None
    timezone: str = ""
    timezone_source: str = ""
    timezone_revision: int = 0
    routine_revision: int = 0
    # 作息表里的时区与规范时区不一致（旧值冲突）。不阻断，但要能看见。
    routine_timezone_conflict: bool = False


async def _routine_snapshot(session: AsyncSession, actor_id: str) -> RoutineSnapshot:
    from ling.domains.agenda.service import DEFAULT_REST_MINUTE, DEFAULT_WAKE_MINUTE

    row = await session.get(ActorRoutineRow, actor_id)
    if row is None:
        return RoutineSnapshot(
            wake_minute=DEFAULT_WAKE_MINUTE,
            rest_minute=DEFAULT_REST_MINUTE,
            revision=0,
            timezone="",
        )
    return RoutineSnapshot(
        wake_minute=int(row.wake_minute),
        rest_minute=int(row.rest_minute),
        revision=int(row.revision or 0),
        timezone=str(row.timezone),
    )


async def resolve_plan_timezone(session: AsyncSession) -> tuple[str, str]:
    """角色规范时区的唯一解析入口 → ``(IANA 名, 来源)``。

    取 owner subject 的用户偏好（DB > env > 默认）。没有登录主体（单角色部署 /
    测试）时同一函数退到 env/默认，行为一致。
    """
    from ling.config import get_settings
    from ling.settings.user_preferences import load_user_timezone, owner_subject_id

    owner = await owner_subject_id(session)
    name, source = await load_user_timezone(
        session, subject_id=owner, env=get_settings().user_timezone
    )
    return name, source.value


async def _persona_summary(session: AsyncSession, persona_id: str) -> dict[str, object]:
    persona = await session.get(Persona, persona_id)
    if persona is None:
        return {}
    traits = persona.traits if isinstance(persona.traits, dict) else {}
    return {
        "name": str(persona.display_name or persona.name or "她"),
        "traits": {str(k): v for k, v in list(traits.items())[:8]},
    }


async def _experience_summary(
    session: AsyncSession, *, actor_id: str, now: datetime
) -> list[str]:
    """已证实经历的粗摘要（只取有证据的 ``self_experiences``）。

    没有证据就没有经历——计划、打算、梦都不能出现在这里。
    """
    since = now - timedelta(hours=EXPERIENCE_LOOKBACK_HOURS)
    rows = (
        await session.execute(
            select(SelfExperienceRow.summary)
            .where(
                SelfExperienceRow.actor_id == actor_id,
                SelfExperienceRow.occurred_at >= since,
            )
            .order_by(SelfExperienceRow.occurred_at.desc())
            .limit(MAX_EXPERIENCE_SUMMARIES)
        )
    ).scalars()
    return [_short(str(item), 120) for item in rows]


async def _presence_snapshot(session: AsyncSession, actor_id: str) -> dict[str, int] | None:
    row = await session.get(PresenceState, actor_id)
    if row is None:
        return None
    # 只读当前值：结算由 PresenceService 负责，这里不写任何状态。
    return {"energy": int(row.energy), "satiety": int(row.satiety)}


async def _valid_places(session: AsyncSession, actor_id: str) -> list[dict[str, str]]:
    rows = (
        await session.execute(
            select(WorldPlace)
            .where(WorldPlace.actor_id == actor_id)
            .order_by(WorldPlace.name)
            .limit(MAX_PLACES)
        )
    ).scalars()
    return [{"id": str(row.id), "name": str(row.name)} for row in rows]


# ---------------------------------------------------------------- 预算与租约


@dataclass(frozen=True)
class _Reservation:
    call_id: str
    attempt: int
    provider_id: str | None = None


async def _day_call_totals(
    session: AsyncSession, *, actor_id: str, plan_day: date
) -> tuple[int, int]:
    """当日该 actor 的 ``outline`` 调用次数与已预留/已用 token 上界。"""
    rows = (
        await session.execute(
            select(
                BehaviorModelCallRow.state,
                BehaviorModelCallRow.reserved_input_tokens,
                BehaviorModelCallRow.reserved_output_tokens,
                BehaviorModelCallRow.actual_tokens,
            ).where(
                BehaviorModelCallRow.actor_id == actor_id,
                BehaviorModelCallRow.plan_day == plan_day,
                BehaviorModelCallRow.kind == OUTLINE_KIND,
                BehaviorModelCallRow.state.in_(["reserved", "committed", "failed"]),
            )
        )
    ).all()
    calls = len(rows)
    budget = sum(
        int(input_tokens)
        + int(output_tokens)
        + (0 if actual is None else max(0, int(actual) - int(input_tokens) - int(output_tokens)))
        for _state, input_tokens, output_tokens, actual in rows
    )
    return calls, budget


async def _reserve_call(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    idempotency_key: str,
    now: datetime,
    lease_owner: str,
) -> _Reservation | None:
    """预约一次大纲调用（预算闸 + lease），拿不到返回 ``None``。

    并发安全三层（设计 §3）：``UNIQUE(actor,kind,key,attempt)`` 让同一 attempt
    只有一个赢家；``lease_until`` 让过期抢占只能开新 attempt；预算表落库，
    重启后旧预留仍计入当日上限，避免重复烧钱。
    """
    calls, budget = await _day_call_totals(session, actor_id=actor_id, plan_day=plan_day)
    if calls >= OUTLINE_MAX_CALLS:
        return None
    reserved = OUTLINE_RESERVED_INPUT_TOKENS + OUTLINE_RESERVED_OUTPUT_TOKENS
    if budget + reserved > OUTLINE_DAILY_TOKEN_BUDGET:
        return None
    attempt = calls + 1
    call_id = new_id("bcall")
    try:
        async with session.begin_nested():
            session.add(
                BehaviorModelCallRow(
                    id=call_id,
                    actor_id=actor_id,
                    plan_day=plan_day,
                    kind=OUTLINE_KIND,
                    window_id="",
                    idempotency_key=idempotency_key,
                    attempt=attempt,
                    state="reserved",
                    reserved_input_tokens=OUTLINE_RESERVED_INPUT_TOKENS,
                    reserved_output_tokens=OUTLINE_RESERVED_OUTPUT_TOKENS,
                    lease_owner=lease_owner,
                    lease_until=now + timedelta(seconds=OUTLINE_LEASE_SECONDS),
                )
            )
            await session.flush()
    except IntegrityError:
        # 另一个 worker 抢先占了同一个 attempt：本次不做（下一轮再试）。
        return None
    return _Reservation(call_id=call_id, attempt=attempt)


async def _finish_call(
    session: AsyncSession,
    *,
    call_id: str,
    state: str,
    actual_tokens: int | None = None,
    provider_id: str | None = None,
    error_code: str | None = None,
) -> None:
    await session.execute(
        update(BehaviorModelCallRow)
        .where(BehaviorModelCallRow.id == call_id)
        .values(
            state=state,
            actual_tokens=actual_tokens,
            provider_id=provider_id,
            error_code=error_code,
            lease_until=None,
            updated_at=utcnow(),
        )
    )


# ---------------------------------------------------------------- 模型调用


async def _call_outline_model(
    *,
    router: object,
    config_reader: object,
    prompt: str,
) -> str:
    """调一次 ``memory.extract`` 职责，返回完整文本（严格一次调用）。"""
    effective = await config_reader.effective()  # type: ignore[attr-defined]
    # 再问一次「这个职责有没有可用模型」：配置可能在预检之后被换掉。
    # 没有就抛专用异常，让上层走规则大纲 + ``model_unavailable``，
    # 而不是发一个注定失败的请求，也不是把配置问题报成"模型不支持"。
    resolved = effective.resolve_profile(OUTLINE_PROFILE)
    if not resolved.usable:
        raise OutlineModelUnavailable(
            resolved.reason or f"职责 {OUTLINE_PROFILE} 没有可用模型"
        )
    messages = [ChatMessageIn(role="user", content=prompt)]

    async def _collect() -> str:
        chunks: list[str] = []
        async for chunk, _outcome in router.stream(  # type: ignore[attr-defined]
            effective,
            profile=OUTLINE_PROFILE,
            messages=messages,
            # 失败就走规则兜底；不要换一家模型悄悄重试（预算与可解释性都要留痕）。
            allow_fallback=False,
        ):
            if chunk.delta:
                chunks.append(chunk.delta)
        return "".join(chunks)

    return await asyncio.wait_for(_collect(), timeout=OUTLINE_TIMEOUT_SECONDS)


async def _outline_model_ready(
    router: object | None, config_reader: object | None
) -> bool:
    """这个职责现在能不能调模型（未绑定 / 被停用 / 槽位没配都算不能）。

    预检放在**预留预算之前**：设计 §0 明确"无可用模型"是一次 0 调用，
    不该为注定失败的请求先占掉当日 quota；也更不能偷偷换 `dialogue.fast`
    （那是另一个职责的额度与口径）。
    """
    if router is None or config_reader is None:
        return False
    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
        resolved = effective.resolve_profile(OUTLINE_PROFILE)
    except Exception:  # noqa: BLE001 - 配置读不出来等于"现在不能调"
        return False
    return bool(resolved.usable and resolved.provider_id and resolved.model_id)


class OutlineModelUnavailable(RuntimeError):
    """``memory.extract`` 职责没有可用模型。

    与 provider 报错分开：这是**配置结论**，不是那家模型不肯干活，
    所以对外一律记 ``model_unavailable``（不把内部 error kind 泄成原因码）。
    """


# ---------------------------------------------------------------- 提示词


def build_outline_prompt(
    *,
    persona: dict[str, object],
    plan_day: date,
    timezone: str,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
    routine: RoutineSnapshot,
    anchors: list[Anchor],
    experiences: list[str],
    presence: dict[str, int] | None,
    places: list[dict[str, str]],
) -> str:
    """把大纲输入快照拼成提示词。

    输入契约按设计 §3：``plan_day / timezone / now_utc / routine /
    immutable_anchors / persona_summary / evidenced_experience_summary /
    current_energy_satiety / valid_places``。**不塞聊天原文、不塞梦、不塞计划正文**。
    """
    snapshot = {
        "plan_day": plan_day.isoformat(),
        "timezone": timezone,
        "now_utc": now.astimezone(UTC).isoformat(),
        "routine": {
            "wake_local": _minute_hhmm(routine.wake_minute),
            "rest_local": _minute_hhmm(routine.rest_minute),
        },
        "immutable_anchors": [anchor.as_dict() for anchor in anchors],
        "persona_summary": persona,
        "evidenced_experience_summary": experiences,
        "current_energy_satiety": presence,
        "valid_places": places,
    }
    return _OUTLINE_INSTRUCTIONS.format(
        name=str(persona.get("name") or "她"),
        plan_day=plan_day.isoformat(),
        timezone=timezone,
        window_local_start=local_hhmm(window_start, timezone),
        window_local_end=local_hhmm(window_end, timezone),
        now_local=local_hhmm(now, timezone),
        not_before_local=local_hhmm(not_before, timezone),
        kinds=", ".join(sorted(MODEL_KINDS)),
        places=[item["id"] for item in places] or "[]",
        snapshot=json.dumps(snapshot, ensure_ascii=False, indent=2),
    )


def build_repair_prompt(base_prompt: str, problems: list[str]) -> str:
    return base_prompt + "\n" + _REPAIR_INSTRUCTIONS.format(
        problems="\n".join(f"- {item}" for item in problems)
    )


def _minute_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


# ---------------------------------------------------------------- 规则大纲


def rule_outline(
    *,
    anchors: list[Anchor],
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
) -> list[SegmentDraft]:
    """确定性兜底：**只**用作息与已确认约定铺时间轴，不编可选活动。

    "今天暂时没什么特别安排"是合法结果——比拿模型幻觉填满更像人。
    """
    drafts: list[SegmentDraft] = []
    for anchor in anchors:
        if not overlaps(anchor.start_at, anchor.end_at, window_start, window_end):
            continue
        if anchor.end_at <= not_before:
            # 整段都在可变起点之前：当作历史，不再写进新版本。
            continue
        drafts.append(
            SegmentDraft(
                kind=SLEEP_ANCHOR if anchor.kind == SLEEP_ANCHOR else "commitment",
                title=anchor.title,
                start_at=anchor.start_at,
                end_at=anchor.end_at,
                place_id=None,
                layer="block",
                is_leaf=False,
                anchor_kind=anchor.kind,
                commitment_level="confirmed",
                source="baseline" if anchor.kind == SLEEP_ANCHOR else "rule",
            )
        )
    return drafts


@dataclass(frozen=True)
class SegmentDraft:
    """待落库的一段（校验/规则大纲共同的中间形态）。"""

    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None
    layer: str = "block"
    is_leaf: bool = True
    anchor_kind: str = ""
    commitment_level: str = "tentative"
    source: str = "model"
    parent_id: str | None = None


# ---------------------------------------------------------------- 计划行


async def _load_or_create_plan(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    timezone: str,
    timezone_source: str,
    routine: RoutineSnapshot,
    now: datetime,
) -> DayPlanRow:
    """按 ``(actor, plan_day)`` 取或建计划行；并发建行只留一行。"""
    window_start, window_end = plan_day_window(plan_day, timezone)
    row = (
        await session.execute(
            select(DayPlanRow).where(
                DayPlanRow.actor_id == actor_id,
                DayPlanRow.plan_day == plan_day,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = DayPlanRow(
        id=new_id("dayplan"),
        actor_id=actor_id,
        plan_day=plan_day,
        timezone=timezone,
        timezone_source=timezone_source,
        timezone_revision=0,
        routine_revision=routine.revision,
        window_start_at=window_start,
        window_end_at=window_end,
        revision=0,
        state="pending",
        generation_source="rule",
        created_at=now,
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # 另一个 worker 同时建了这一天：用它的那一行（不重复建日期行）。
        return (
            await session.execute(
                select(DayPlanRow).where(
                    DayPlanRow.actor_id == actor_id,
                    DayPlanRow.plan_day == plan_day,
                )
            )
        ).scalar_one()
    return row


async def load_day_plan(
    session: AsyncSession, *, actor_id: str, plan_day: date
) -> DayPlanRow | None:
    """只读：取某天的计划头（给测试/后续只读端点用）。"""
    return (
        await session.execute(
            select(DayPlanRow).where(
                DayPlanRow.actor_id == actor_id, DayPlanRow.plan_day == plan_day
            )
        )
    ).scalar_one_or_none()


async def load_segments(
    session: AsyncSession, *, plan_id: str, revision: int | None = None
) -> list[ScheduleSegmentRow]:
    """只读：某版（默认最新 revision）的段，按开始时间排序。"""
    stmt = select(ScheduleSegmentRow).where(ScheduleSegmentRow.plan_id == plan_id)
    if revision is not None:
        stmt = stmt.where(ScheduleSegmentRow.revision_created == revision)
    stmt = stmt.order_by(ScheduleSegmentRow.start_at)
    return list((await session.execute(stmt)).scalars())


async def _commit_revision(
    session: AsyncSession,
    *,
    row: DayPlanRow,
    segments: list[SegmentDraft],
    state: str,
    source: str,
    error_code: str | None,
    now: datetime,
    routine: RoutineSnapshot,
    timezone: str,
    timezone_source: str,
    timezone_revision: int,
) -> tuple[int, int]:
    """CAS 提交一版：段先 supersede 未来段，再插新段，最后抬 ``revision``。

    CAS 失败（另一个 worker 已提交）抛 ``StaleRevisionError``，整个嵌套事务
    回滚——**不会**留下半版时间轴。
    """
    expected = int(row.revision)
    new_revision = expected + 1
    floor = max(now, _aware(row.window_start_at))

    async with session.begin_nested():
        # 只动"还没开始"的当前段：已过去的、已经开始的段是历史，不改写。
        # 锚点若已开始（跨夜睡眠的尾部）也保留，避免把"正在睡"翻成缺口。
        await session.execute(
            update(ScheduleSegmentRow)
            .where(
                ScheduleSegmentRow.plan_id == row.id,
                ScheduleSegmentRow.status == "current",
                ScheduleSegmentRow.revision_created < new_revision,
                ScheduleSegmentRow.start_at >= floor,
            )
            .values(status="superseded", revision_ended=new_revision)
        )
        # 已经开始的段（跨夜睡眠的尾部、过去的大块）保留原样：改版只排未来。
        kept = list(
            (
                await session.execute(
                    select(ScheduleSegmentRow).where(
                        ScheduleSegmentRow.plan_id == row.id,
                        ScheduleSegmentRow.status == "current",
                        ScheduleSegmentRow.start_at < floor,
                    )
                )
            ).scalars()
        )
        # 新草稿若压到保留段就丢弃：宁可这一版少一块，也不写重叠。
        # 时区变更跨过 D+1 04:00 时，旧夜睡眠锚点会与新夜锚点重叠——
        # 这正是设计 §2 说的"跨夜锚点冲突：保留已开始部分、未来接缝规则修补"。
        drafts = [
            draft
            for draft in segments
            if not any(
                overlaps(draft.start_at, draft.end_at, _aware(item.start_at), _aware(item.end_at))
                for item in kept
            )
        ]
        result = await session.execute(
            update(DayPlanRow)
            .where(DayPlanRow.id == row.id, DayPlanRow.revision == expected)
            .values(
                revision=new_revision,
                state=state,
                generation_source=source,
                last_error_code=error_code,
                timezone=timezone,
                timezone_source=timezone_source,
                timezone_revision=timezone_revision,
                routine_revision=routine.revision,
                window_start_at=row.window_start_at,
                window_end_at=row.window_end_at,
                updated_at=now,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise StaleRevisionError(expected)
        for draft in drafts:
            session.add(
                ScheduleSegmentRow(
                    id=new_id("seg"),
                    plan_id=row.id,
                    actor_id=row.actor_id,
                    parent_id=draft.parent_id,
                    parent_key=draft.parent_id or "",
                    layer=draft.layer,
                    kind=draft.kind,
                    title=draft.title,
                    start_at=draft.start_at,
                    end_at=draft.end_at,
                    place_id=draft.place_id,
                    commitment_level=draft.commitment_level,
                    status="current",
                    is_leaf=draft.is_leaf,
                    anchor_kind=draft.anchor_kind,
                    source=draft.source,
                    revision_created=new_revision,
                    revision_ended=None,
                    source_event_ids=[],
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
    await session.refresh(row)
    return new_revision, len(drafts)


class StaleRevisionError(RuntimeError):
    """提交时 ``revision`` 已被别人推进（CAS 失败）。"""


# ---------------------------------------------------------------- 主入口


async def ensure_day_plan(
    session: AsyncSession,
    *,
    actor_id: str,
    router: object | None = None,
    config_reader: object | None = None,
    plan_day: date | None = None,
    now: datetime | None = None,
    timezone: str | None = None,
    timezone_source: str | None = None,
    lease_owner: str | None = None,
) -> OutlineOutcome:
    """确保某个**计划日**有一版可用大纲（幂等）。

    这是阶段 1 唯一的大纲写入入口，`AgendaService.ensure_day_plan` 只是转发。
    调用方负责 commit/rollback；本函数内部只用嵌套事务保护"抢占/提交"这两处。

    流程：解析规范时区 → 取/建计划行 → 快照没变就直接返回原结论 → 预算+lease
    → 调模型 → 严格校验（非法修一次）→ 失败写规则大纲 + ``degraded`` →
    CAS 提交一版。

    ``router`` / ``config_reader`` 为 ``None`` 时不调模型，直接规则兜底
    （测试与"模型池没就绪"的 worker 都走这条路）。
    """
    now = _aware(now or utcnow())
    if timezone is None:
        timezone, timezone_source = await resolve_plan_timezone(session)
    else:
        timezone_source = timezone_source or "explicit"
    routine = await _routine_snapshot(session, actor_id)
    if plan_day is None:
        plan_day = plan_day_for(now, timezone)
    window_start, window_end = plan_day_window(plan_day, timezone)

    row = await _load_or_create_plan(
        session,
        actor_id=actor_id,
        plan_day=plan_day,
        timezone=timezone,
        timezone_source=timezone_source,
        routine=routine,
        now=now,
    )

    # 快照没变且已生成过 → 幂等返回。**不重排**：同一计划日只生成一次，
    # 重排是时区/作息变化或阶段 3 的提议触发的事。
    snapshot_same = (
        str(row.timezone) == timezone
        and int(row.routine_revision) == routine.revision
    )
    if row.state in ("ready", "degraded") and int(row.revision) > 0 and snapshot_same:
        return _outcome(
            row,
            state="unchanged",
            segments=await _count_current_segments(session, row.id),
            source=str(row.generation_source),
            reason_code=row.last_error_code,
            timezone=timezone,
            timezone_source=str(row.timezone_source),
            routine=routine,
        )

    # 时区/作息变了：抬快照版本再重排（只重排 max(now, 窗口起点) 之后的可变段）。
    timezone_revision = int(row.timezone_revision)
    if not snapshot_same:
        timezone_revision += 1

    anchors = await load_commitment_anchors(
        session, actor_id=actor_id, window_start=window_start, window_end=window_end
    )
    sleep_anchor = build_sleep_anchor(
        plan_day,
        timezone=timezone,
        rest_minute=routine.rest_minute,
        wake_minute=routine.wake_minute,
    )
    if sleep_anchor is not None:
        anchors.insert(0, sleep_anchor)

    # 可变起点：过去永不回填；catch-up 也从 now 开始。
    not_before = max(now, window_start)
    places = await _valid_places(session, actor_id)
    place_ids = {item["id"] for item in places}
    persona = await _persona_summary(session, persona_id=await _resolve_persona_id(session))
    experiences = await _experience_summary(session, actor_id=actor_id, now=now)
    presence = await _presence_snapshot(session, actor_id)

    idempotency_key = f"outline:{actor_id}:{plan_day.isoformat()}:{timezone_revision}"
    owner = lease_owner or f"timeline:{new_id('lease')}"

    drafts: list[SegmentDraft] = []
    state = "degraded"
    source = "rule"
    reason_code: str | None = None
    calls = 0
    model_provider: str | None = None

    prompt = build_outline_prompt(
        persona=persona,
        plan_day=plan_day,
        timezone=timezone,
        now=now,
        window_start=window_start,
        window_end=window_end,
        not_before=not_before,
        routine=routine,
        anchors=anchors,
        experiences=experiences,
        presence=presence,
        places=places,
    )

    model_ready = await _outline_model_ready(router, config_reader)
    for _attempt in range(OUTLINE_MAX_CALLS):
        if not model_ready:
            # 没有可用模型：**一次都不调**（设计 §0：无可用模型 = 0 次），
            # 也不占预算行——否则"没绑职责"会白吃掉当日 quota。
            reason_code = "model_unavailable"
            break
        reservation = await _reserve_call(
            session,
            actor_id=actor_id,
            plan_day=plan_day,
            idempotency_key=idempotency_key,
            now=now,
            lease_owner=owner,
        )
        if reservation is None:
            # 预算耗尽或另一个 worker 正持有同一 attempt 的 lease。
            reason_code = reason_code or "budget_exhausted"
            break
        calls += 1
        try:
            raw = await _call_outline_model(
                router=router, config_reader=config_reader, prompt=prompt
            )
            blocks = validate_outline(
                parse_outline_payload(raw),
                window_start=window_start,
                window_end=window_end,
                not_before=not_before,
                anchors=anchors,
                valid_place_ids=place_ids,
            )
        except OutlineValidationError as exc:
            reason_code = "outline_invalid"
            await _finish_call(
                session, call_id=reservation.call_id, state="failed", error_code=reason_code
            )
            # 同预算内修复一次：把逐条原因带回去，再要一份完整 JSON。
            prompt = build_repair_prompt(prompt, exc.problems)
            continue
        except OutlineModelUnavailable:
            # 预检之后配置被换掉：照样走规则大纲，且**原因码保持语义化**，
            # 不让 `unsupported_capability` 冒充"模型不支持这个任务"。
            reason_code = "model_unavailable"
            await _finish_call(
                session,
                call_id=reservation.call_id,
                state="failed",
                error_code=reason_code,
            )
            break
        except (TimeoutError, ModelError, Exception) as exc:  # noqa: BLE001
            reason_code = _model_error_code(exc)
            await _finish_call(
                session,
                call_id=reservation.call_id,
                state="failed",
                error_code=reason_code,
            )
            break
        else:
            await _finish_call(
                session,
                call_id=reservation.call_id,
                state="committed",
                actual_tokens=_estimate_tokens(prompt, raw),
                provider_id=_resolved_provider_id(await config_reader.effective(), OUTLINE_PROFILE),
            )
            model_provider = _resolved_provider_id(
                await config_reader.effective(), OUTLINE_PROFILE
            )
            drafts = [
                SegmentDraft(
                    kind=block.kind,
                    title=block.title,
                    start_at=block.start_at,
                    end_at=block.end_at,
                    place_id=block.place_id,
                    source="model",
                )
                for block in blocks
            ]
            state = "ready"
            source = "model"
            reason_code = None
            break

    if state != "ready":
        # 兜底：只铺锚点（睡眠/已确认约定），没有可选活动就是"没什么安排"。
        drafts = rule_outline(
            anchors=anchors,
            window_start=window_start,
            window_end=window_end,
            not_before=not_before,
        )
        state = "degraded"
        source = "rule"
        if reason_code is None:
            reason_code = "model_unavailable"

    # 锚点总是入库（模型版也要有睡眠/承诺段），且与模型段不重叠（已校验）。
    anchor_drafts = rule_outline(
        anchors=anchors,
        window_start=window_start,
        window_end=window_end,
        not_before=not_before,
    )
    merged = _merge_drafts(anchor_drafts, drafts)

    try:
        new_revision, inserted = await _commit_revision(
            session,
            row=row,
            segments=merged,
            state=state,
            source=source,
            error_code=reason_code,
            now=now,
            routine=routine,
            timezone=timezone,
            timezone_source=timezone_source,
            timezone_revision=timezone_revision,
        )
    except StaleRevisionError:
        return _outcome(
            row,
            state="busy",
            segments=await _count_current_segments(session, row.id),
            source=str(row.generation_source),
            reason_code="stale_revision",
            timezone=timezone,
            timezone_source=str(row.timezone_source),
            routine=routine,
            calls=calls,
        )
    return _outcome(
        row,
        state=state,
        segments=inserted,
        source=source,
        reason_code=reason_code,
        timezone=timezone,
        timezone_source=timezone_source,
        routine=routine,
        calls=calls,
        revision=new_revision,
        provider_id=model_provider,
    )


async def run_due_outline(
    session: AsyncSession,
    *,
    actor_id: str,
    router: object | None = None,
    config_reader: object | None = None,
    now: datetime | None = None,
    lease_owner: str | None = None,
) -> OutlineOutcome | None:
    """worker 将来要调的函数：**只在到期边界**做一次"确保今天有大纲"。

    - 未到 04:00：返回 ``None``（不排明天的计划，不提前烧钱）；
    - 已过 04:00 但没生成：生成从 ``now`` 开始的剩余部分（catch-up）；
    - 离休息不足 40 分钟：只写规则睡眠锚点并标 ``degraded``（不调模型）；
    - 当前计划日已 ready/degraded 且快照没变：幂等返回 unchanged。

    这里**没有**定时器/循环，也没有 outbox：阶段 1 只提供可调用的入口。
    """
    now = _aware(now or utcnow())
    timezone, timezone_source = await resolve_plan_timezone(session)
    plan_day = plan_day_for(now, timezone)
    window_start, window_end = plan_day_window(plan_day, timezone)
    if now < window_start:
        return None
    routine = await _routine_snapshot(session, actor_id)
    rest_instant = _local_minute_instant(
        plan_day, routine.rest_minute, resolve_zone(timezone)
    )
    if rest_instant > now and (rest_instant - now) < timedelta(
        minutes=CATCH_UP_MIN_MINUTES_BEFORE_REST
    ):
        # 太靠近休息：不追模型，只铺规则锚点（degraded_late）。
        outcome = await ensure_day_plan(
            session,
            actor_id=actor_id,
            router=None,
            config_reader=None,
            plan_day=plan_day,
            now=now,
            timezone=timezone,
            timezone_source=timezone_source,
            lease_owner=lease_owner,
        )
        if outcome.reason_code is None:
            outcome = OutlineOutcome(
                **{
                    **outcome.__dict__,
                    "reason_code": "degraded_late",
                }
            )
        return outcome
    return await ensure_day_plan(
        session,
        actor_id=actor_id,
        router=router,
        config_reader=config_reader,
        plan_day=plan_day,
        now=now,
        timezone=timezone,
        timezone_source=timezone_source,
        lease_owner=lease_owner,
    )


# ---------------------------------------------------------------- 内部工具


def _merge_drafts(anchors: list[SegmentDraft], blocks: list[SegmentDraft]) -> list[SegmentDraft]:
    """锚点 + 可变段；模型版里已经包含锚点校验，这里只合并去重。"""
    merged = list(anchors)
    seen = {(item.start_at, item.end_at, item.anchor_kind) for item in anchors}
    for block in blocks:
        key = (block.start_at, block.end_at, block.anchor_kind)
        if key in seen:
            continue
        merged.append(block)
    merged.sort(key=lambda item: (item.start_at, item.end_at))
    return merged


async def _count_current_segments(session: AsyncSession, plan_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == plan_id,
                    ScheduleSegmentRow.status == "current",
                )
            )
        ).scalar_one()
    )


async def _resolve_persona_id(session: AsyncSession) -> str:
    from ling.config import get_settings

    try:
        return get_settings().persona_id
    except Exception:  # noqa: BLE001 - 配置不可读时用默认 id
        return "persona_ling"


def _resolved_provider_id(effective: object, profile: str) -> str | None:
    try:
        resolved = effective.resolve_profile(profile)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    return resolved.provider_id


def _estimate_tokens(prompt: str, reply: str) -> int:
    """一次调用的大致 token 数：**输入 + 输出**，用 provider quota 的同一把尺子。

    ``estimate_tokens_for`` 的口径是 ``(messages, reply)``，不是单段文本——
    只传 prompt 会低估输出、只传 reply 会忽略输入，两种偏差都会让
    ``behavior_model_calls.actual_tokens`` 失去审计意义（设计 §0/§3）。
    """
    from ling.models.quota import estimate_tokens_for

    return estimate_tokens_for([ChatMessageIn(role="user", content=prompt)], reply)


def _model_error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "model_timeout"
    kind = getattr(getattr(exc, "kind", None), "value", None)
    if isinstance(kind, str) and kind:
        return f"model_{kind}"
    return "model_error"


def _outcome(
    row: DayPlanRow,
    *,
    state: str,
    segments: int,
    source: str,
    reason_code: str | None,
    timezone: str,
    timezone_source: str,
    routine: RoutineSnapshot,
    calls: int = 0,
    revision: int | None = None,
    provider_id: str | None = None,
) -> OutlineOutcome:
    return OutlineOutcome(
        plan_id=str(row.id),
        plan_day=row.plan_day,
        state=state,
        revision=int(row.revision) if revision is None else revision,
        segments=segments,
        source=source,
        calls=calls,
        reason_code=reason_code,
        timezone=timezone,
        timezone_source=timezone_source,
        timezone_revision=int(row.timezone_revision),
        routine_revision=routine.revision,
        routine_timezone_conflict=bool(
            routine.timezone and routine.timezone != timezone
        ),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "CATCH_UP_MIN_MINUTES_BEFORE_REST",
    "COMMITMENT_ANCHOR",
    "COMMITMENT_PROTECTIVE_MINUTES",
    "MODEL_KINDS",
    "OUTLINE_DAILY_TOKEN_BUDGET",
    "OUTLINE_KIND",
    "OUTLINE_MAX_BLOCKS",
    "OUTLINE_MAX_CALLS",
    "OUTLINE_PROFILE",
    "OUTLINE_TIMEOUT_SECONDS",
    "PLAN_DAY_START_MINUTE",
    "SLEEP_ANCHOR",
    "Anchor",
    "OutlineOutcome",
    "OutlineValidationError",
    "PlannedBlock",
    "RoutineSnapshot",
    "SegmentDraft",
    "StaleRevisionError",
    "build_outline_prompt",
    "build_repair_prompt",
    "build_sleep_anchor",
    "contains",
    "display_day_for",
    "display_day_window",
    "ensure_day_plan",
    "load_commitment_anchors",
    "load_day_plan",
    "load_segments",
    "local_hhmm",
    "local_minute_instant",
    "overlaps",
    "parse_outline_payload",
    "plan_day_for",
    "plan_day_length",
    "plan_day_window",
    "resolve_plan_timezone",
    "resolve_zone",
    "rule_outline",
    "run_due_outline",
    "validate_outline",
]
