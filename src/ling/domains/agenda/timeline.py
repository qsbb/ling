"""她的一天（阶段 1.5）：两个"日"的边界、时间轴校验与每日大纲生成。

## 两个日，一个真实时钟

- **展示日**：用户有效时区的 ``[D 00:00, D+1 00:00)``；
- **计划日**：角色规范时区的 ``[D 00:00, D+1 00:00)``，以起点日期 D 命名。

计划日按**午夜**切分：角色本地的日历日期就是 ``plan_day``，没有"凌晨 4 点前算
前一天"的旧分支（那会让 D 的 00:00–04:00 必须由 D−1 的计划承担，与"前夜为 D 的
整日生成"不符）。

计划日时区取 ``ling/settings/user_preferences.py`` 的**唯一解析入口**
（DB 偏好 → ``LING_USER_TIMEZONE`` → 默认），不读服务器时区、也不偷用
``actor_routines.timezone`` 另算一套"今天"。作息表里的时区只作为**旧值**：
若它与规范时区不一致，大纲仍按规范时区排，并把冲突记在 outcome 上（不静默）。

**生成时刻与日界是两件事**：D 的大纲在角色本地 **D−1 21:00** 到期
（:func:`outline_due_at`），21:00–24:00 仍属于 D−1 的生活，所以 21:00 不是日界，
只是后台批处理时刻。D 的这一行负责 D 的**整个** ``[D 00:00, D+1 00:00)``，
包括 D 凌晨的预计睡眠：睡眠锚点由作息"非清醒区间"与 D 的午夜窗口**求交**，
得到至多两段、都完全归属 D 的规则锚点（:func:`sleep_anchors_for_day`）。
D 的 ``[00:00,08:00)`` 由 D 自己的计划直接产出，**不再**继承 D−1 的跨夜锚点，
也没有任何段越过午夜。

## 时间轴语义（全部左闭右开、UTC 入库）

- 任意分钟粒度，不强制整点；允许空档（空档 = 自由/未决定，UI 不得虚构活动）；
- 大块 / 小时 / 分钟三层都是**意向**，父子段有意重叠；只有叶段能启动活动；
- 睡眠与 confirmed 承诺是**不可移动锚点**，模型段不得压上去；
- 段从不物理删除：改版时旧段置 ``superseded`` 并记 ``revision_ended``。

## 阶段 1.5 做了哪些

- 接缝校验 :func:`seam_check`：D 00:00（或恢复后首次检查）**零模型**复核
  D−1 晚上新入库的已确认锚点、以及两日睡眠边界；冲突的可选段置
  ``superseded`` 并留原因码，不重生成。
- 新确认锚点落地 :func:`on_new_confirmed_anchor`：即时复核同一计划日，
  只登记"需要局部重排"的待办；真正的模型局部重排属阶段 3。
- catch-up 三档 :func:`outline_due_decision` / :func:`outline_work`：
  D−1 21:00 到期（``due``）→ 醒前补跑（``catchup``）→ 到时仍无 ready
  则 ``degraded_late``（只补确定性锚点，不再调模型）。
- 锚点裁剪与冲突 :func:`resolve_anchor_conflicts`：保护窗裁到 D 内、
  同级硬约定冲突都保留、承诺与预计睡眠冲突时保留承诺并标原因码。
- 人设投影 :func:`build_persona_projection`：确定性结构化、关系只给定性摘要
  （不带他人标识/私聊细节）、**≤400 输入 token**（粗略估算，见
  ``PersonaProjection.estimate_method``），完整 ``system_prompt`` 不进日程输入。

调度接线在 ``worker.py`` 的 ``life.schedule``（只查 DB 到期边界）+
``life.outline`` / ``life.seam`` 两个 outbox handler。本模块只提供可调用入口，
没有定时器/循环。

接线时按这三步取入口（本模块**不**替你决定"现在该不该跑"，只回答"跑哪一个
目标日、能不能调模型"）：

.. code-block:: python

    timezone, _source = await resolve_plan_timezone(session)
    routine = ...          # ActorRoutineRow 的 wake_minute/rest_minute
    target, phase = outline_due_decision(now, timezone=timezone,
                                        wake_minute=routine.wake_minute)
    outcome = await outline_work(session, target, phase, actor_id=actor_id,
                                router=router, config_reader=cfg, now=now)

    seam = await seam_check(session, plan_day, actor_id=actor_id, now=now)   # 零模型
    todo = await on_new_confirmed_anchor(session, plan_day, anchor, ...)     # 零模型
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.evidence import fact_eligible_experiences
from ling.infra.models import (
    ActorRoutineRow,
    AgendaPlanRow,
    BehaviorModelCallRow,
    CommitmentRow,
    DayPlanRow,
    Persona,
    PresenceState,
    RelationshipState,
    SceneCommitRow,
    ScheduleSegmentRow,
    SelfExperienceRow,
    WorldPlace,
    utcnow,
)
from ling.infra.security import new_id
from ling.ports.model import ChatMessageIn, ModelError

logger = logging.getLogger("ling.domains.agenda.timeline")

# 计划日的起点：角色本地**午夜**（设计 §2）。计划日是本地 ``[D 00:00, D+1 00:00)``，
# 本地日历日期就是 ``plan_day``；不再有"< 04:00 算前一天"的旧日界。
PLAN_DAY_START_HOUR = 0
PLAN_DAY_START_MINUTE = PLAN_DAY_START_HOUR * 60

# 大纲的**生成到期时刻**：目标日 D 的**前一天**本地 21:00（设计 §2）。
# 生成时刻与日界分离：21:00–24:00 仍属于 D−1 的生活，所以 21:00 不是日界。
# 按本地墙钟组合再转 UTC，**不能**用"午夜 − 3 小时"推（DST 当天会偏）。
OUTLINE_DUE_HOUR = 21
OUTLINE_DUE_MINUTE = OUTLINE_DUE_HOUR * 60

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
# 不是"她答应做一小时"——库里没有那个事实，不能编。保护窗会被裁到计划日窗口内
# （否则 D−1 23:30 的承诺会伸进 D 的凌晨，成为跨午夜声明）。
COMMITMENT_PROTECTIVE_MINUTES = 60

# 最小修复（P0.5）：阶段 1.5 改版时这条常量被删掉，但 `run_due_outline` 里
# 仍在引用（`__all__` 也还导出它），于是 `NameError` 让
# `tests/integration/test_schedule_outline.py` 的显式目标日用例直接红。
# 值沿用改版前的 40 分钟；**本文件其余逻辑不动**，catch-up 三档判据的重写
# 归下一步（`outline_due_decision` / `outline_work`）。
CATCH_UP_MIN_MINUTES_BEFORE_REST = 40

# 睡眠保护下限（分钟）。设计 §4 的工程初值 420 = 7 小时：低于它只标
# ``needs_review``，**不**因此取消已确认承诺，也不擅自改作息。
# `LifePolicy` 里目前没有这一项（那里是生活节奏，不是作息），阶段 1.5
# 先作为 agenda 域常量；要做成可配置项时再显式加进 `LifePolicy`。
MIN_SLEEP_MINUTES = 420

# 原因码（设计 §6：`degraded_late` / `needs_review` 是**原因码**，不是新的
# `day_plans.state` 枚举）。落点只有两处，避免加列/加表：
#   - `schedule_segments.source_event_ids`：`reason:<code>` 与来源 id；
#   - `day_plans.last_error_code`：只放"最近一次失败/降级/待复核"的单个码。
REASON_NEEDS_REVIEW = "needs_review"
REASON_ROUTINE_CONFLICT = "routine_conflict"
REASON_MIDNIGHT_BOUNDARY = "midnight_boundary"
REASON_SLEEP_BOUNDARY = "sleep_boundary_conflict"
REASON_DEGRADED_LATE = "degraded_late"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_OUTLINE_INVALID = "outline_invalid"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

# catch-up 何时还值得再试一次（设计 §3「catch-up」）：
#   - 可恢复：模型暂不可用/超时/报错 —— 醒前可退避补试；
#   - 终态：非法输出已用满两次预算、预算耗尽、到醒点后的 degraded_late。
# 不在终态里反复调模型（那是白烧钱，也会让"今天没安排"永远不稳定）。
RETRYABLE_REASON_CODES: frozenset[str] = frozenset(
    {REASON_MODEL_UNAVAILABLE, "model_timeout", "model_error"}
)
FINAL_REASON_CODES: frozenset[str] = frozenset(
    {REASON_OUTLINE_INVALID, REASON_BUDGET_EXHAUSTED, REASON_DEGRADED_LATE}
)

REASON_TAG_PREFIX = "reason:"
# "需要局部重排"的待办标记。阶段 1.5 只留标记（不加表）：真正的模型局部
# 重排属阶段 3，谁在阶段 3 接上就从这些标记 + `ReplanTodo` 里取范围。
REPLAN_TODO_TAG = "todo:replan"

# 大纲相位（纯计算，worker 的 `life.schedule` 与 `run_due_outline` 共用）。
PHASE_DUE = "due"  # D−1 21:00 之后的正常到期
PHASE_CATCHUP = "catchup"  # 错过 21:00，但还没到 D 的醒点
PHASE_LATE = "late"  # 已到/过了 D 的醒点：只补确定性锚点，不再调模型

# 一段经历/一条承诺进 prompt 的上限：prompt 有硬上限，宁可少给。
MAX_EXPERIENCE_SUMMARIES = 5
MAX_PLACES = 12
EXPERIENCE_LOOKBACK_HOURS = 72

# ---------------------------------------------------------------- 人设投影限额
#
# 设计 §3：人设进大纲 prompt 的硬上限是 **400 输入 token**，边界与作息相关习惯
# 优先，超预算时先删可选场景与口癖细节。这些条数是**工程初值**，不是研究结论。
PERSONA_TOKEN_LIMIT = 400
PERSONA_MAX_ITEMS = 4  # 每类先限条数，再按优先级压 token
PERSONA_MAX_ITEM_CHARS = 60  # 单条长度上限（先截断再加长裁剪）
PERSONA_MAX_RELATIONSHIPS = 2
PERSONA_MAX_SCENARIO_RULES = 2
# 缺字段的占位。**不能**从空白处编人格。
PERSONA_UNKNOWN = "未知"
# token 估算口径：复用项目里预算裁剪那把"尺子"，并**如实**说明它只是粗略估算。
# 继续用它的理由：如果这里另写一套估算，"预算裁剪说放得下、记账说超了"会互相矛盾。
PERSONA_ESTIMATE_METHOD = (
    "粗略估算：ling.kernel.context.estimate_tokens（中文≈1 token/字，"
    "英文≈4 字符/token），不是 provider tokenizer"
)

# 人设投影的裁剪顺序（设计 §3：先删可选场景与口癖细节，绝不先挤边界/核心价值观/
# 作息相关习惯）。列表越靠前越先被删。
_PERSONA_TRIM_ORDER: tuple[str, ...] = (
    "scenario_rules",
    "interests",
    "relationships",
    "situation",
    "personality",
)

# traits 是**使用者自由填的 dict**（人设页给的是 JSON 编辑器），所以只按稳定的
# 键名关键词归类，不假定它有固定 schema：认不出来的一律归"性格/风格"，
# 宁可少解释一条，也不丢一条。
_TRAIT_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "values",
        (
            "value",
            "principle",
            "belief",
            "ethic",
            "boundar",
            "价值观",
            "原则",
            "信念",
            "在意",
            "禁忌",
        ),
    ),
    (
        "habits",
        (
            "habit",
            "routine",
            "sleep",
            "wake",
            "作息",
            "习惯",
            "生活",
            "睡",
            "起床",
            "夜猫",
            "晨",
        ),
    ),
    (
        "interests",
        (
            "interest",
            "hobby",
            "like",
            "love",
            "taste",
            "兴趣",
            "爱好",
            "喜欢",
            "口癖",
            "口头禅",
        ),
    ),
)

# 场景状态 → 人能读的一句话（"当前处境"用）。未知/过期一律"未知"。
_SCENE_LABELS: dict[str, str] = {
    "chat": "在聊天",
    "focus": "在做需要专注的事",
    "rest": "在休息",
    "errand": "在外面办事",
    "unknown": "不知道在做什么",
}

# 场景键 → 展示标签（只影响 prompt 那行字怎么读，不是判定规则）。
# 这里**不 import persona 域**：域边界闸门不允许跨域 import 实现。
_SCENARIO_LABELS: dict[str, str] = {
    "private": "私聊",
    "group": "群聊",
    "group_mentioned": "群里被点名",
    "group_unaddressed": "群里没被点名",
}
# 稳定顺序：先取最贴近日程的场景覆盖，其余按键名排序（确定性优先）。
_SCENARIO_ORDER: tuple[str, ...] = (
    "private",
    "group",
    "group_mentioned",
    "group_unaddressed",
)

# 关系数值只做**定性**投影：设计 §3 明确"不含信任数值"。
_FAMILIARITY_LEVELS: tuple[tuple[float, str], ...] = (
    (0.7, "很熟"),
    (0.4, "比较熟"),
    (0.15, "有点熟"),
)
_FAMILIARITY_DEFAULT = "还生疏"

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
    """计划日的 UTC 半开区间 ``[D 00:00, D+1 00:00)``（按本地墙钟组合）。"""
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


def outline_due_at(plan_day: date, timezone: str) -> datetime:
    """目标计划日 D 的大纲**到期时刻**：D−1 本地 21:00 的 UTC 瞬时。

    与日界分开：21:00–24:00 仍属于 D−1 的生活，所以这不是"新的一天开始"。
    按 D−1 的**本地墙钟**组合再转 UTC，**不做** ``window_start − 3h`` 或
    ``−24h`` 的算术：DST 当天的 21:00 与次日 00:00 之间的偏移关系不由加法决定。
    DST 规则与 :func:`_local_minute_instant` 一致：本地时刻不存在就顺延到该日
    下一个有效分钟，重复则取第一次出现（``fold=0``）。
    """
    zone = resolve_zone(timezone)
    due_day = plan_day - timedelta(days=1)
    return _local_minute_instant(due_day, OUTLINE_DUE_MINUTE, zone)


def plan_day_for(instant: datetime, timezone: str | None) -> date:
    """某个瞬时落在哪个**计划日**（角色本地日历日期 = plan_day）。"""
    return _aware(instant).astimezone(resolve_zone(timezone)).date()


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


def outline_due_decision(
    now: datetime,
    *,
    timezone: str,
    wake_minute: int | None,
    plan_day: date | None = None,
) -> tuple[date, str]:
    """现在该为**哪个目标日 D** 生成大纲、处在哪个相位（纯计算，不碰数据库）。

    三档（设计 §3「catch-up」）：

    - ``PHASE_DUE``：本地 21:00 已到，正常为**明天**生成（``D = 本地日 + 1``）；
      21:00–24:00 仍属于 D−1 的生活，所以这是生成时刻，不是日界；
    - ``PHASE_CATCHUP``：错过了 21:00，但还在 D 的 ``wake_minute`` **之前**
      ——可以用同一作业补跑，且只排 ``now`` 之后（过午夜不回填）；
    - ``PHASE_LATE``：已到/过了 D 的醒点（含 ``wake_minute=None``，
      即作息没有有效睡眠窗、午夜之后就算晚到）：只补确定性锚点，**零模型**。

    ``plan_day`` 显式传入时**只按 D 自己的边界分档**（供补跑/测试）：窗口起点
    之前算 ``due``、窗口起点到醒点之间算 ``catchup``、醒点及以后算 ``late``。
    注意：这里是"能不能调模型/从哪儿开始排"的判据，**不是**"现在该不该跑"的
    判据——是否到期由调度（``life.schedule`` 查 DB 边界）决定。

    ``wake_minute=None`` 表示没有有效睡眠窗（``wake == rest``）：午夜之后没有
    "醒前"可言，直接算晚到，不编一个不存在的醒点。
    """
    moment = _aware(now)
    zone = resolve_zone(timezone)
    local_day = moment.astimezone(zone).date()

    if plan_day is None:
        # 21:00 一过就为**明天**到期；没到 21:00 时，最近一个待补的目标日就是今天
        # （它的 21:00 是昨天，早过了）。
        if moment >= outline_due_at(local_day + timedelta(days=1), timezone):
            return local_day + timedelta(days=1), PHASE_DUE
        target = local_day
    else:
        target = plan_day

    window_start, _window_end = plan_day_window(target, timezone)
    if moment < window_start:
        # 目标日窗口还没开始：要么正是 D−1 21:00 的正常到期点，要么调用方显式
        # 要求提前排 D。两种都属于"可以正常调模型"。
        return target, PHASE_DUE
    if wake_minute is None:
        return target, PHASE_LATE
    wake = _local_minute_instant(target, int(wake_minute) % (24 * 60), zone)
    if moment < wake:
        return target, PHASE_CATCHUP
    return target, PHASE_LATE


# ---------------------------------------------------------------- 锚点


@dataclass(frozen=True)
class Anchor:
    """不可移动锚点：睡眠或已确认的约定。

    ``protective`` 说明这个区间是**保护窗**还是真实时长：
    - 睡眠来自作息，区间就是作息窗；
    - 承诺只有到期时刻，区间是 ``[due, due+60)`` 的保护窗，不代表"她要花一小时"。

    ``source_id`` 是**可追溯的来源 id**（``commitment:<id>`` /
    ``agenda_plan:<id>``），``reason_codes`` 是这条锚点自带的冲突原因码
    （``routine_conflict`` / ``needs_review``）：同级硬约定冲突时两边都留、
    两边都带码，由时间轴显示，**不让模型选一个赢家**。
    """

    kind: str  # sleep | commitment
    start_at: datetime
    end_at: datetime
    title: str
    protective: bool = False
    source_id: str = ""
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "title": self.title,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
        }
        if self.reason_codes:
            payload["conflict"] = list(self.reason_codes)
        return payload


@dataclass(frozen=True)
class AnchorConflict:
    """一处被**代码**判定（而非模型挑选）的锚点冲突。"""

    reason_code: str
    anchor_ids: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "anchors": list(self.anchor_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AnchorPlan:
    """一版锚点集合：裁剪后的睡眠 + 全部硬约定，以及它们的冲突清单。"""

    anchors: list[Anchor]
    conflicts: list[AnchorConflict]
    sleep_minutes: int
    sleep_minutes_before_trim: int

    @property
    def reason_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        for item in self.conflicts:
            if item.reason_code not in codes:
                codes.append(item.reason_code)
        return tuple(codes)

    @property
    def needs_review(self) -> bool:
        return REASON_NEEDS_REVIEW in self.reason_codes


def sleep_anchors_for_day(
    plan_day: date, timezone: str, routine: RoutineSnapshot
) -> list[Anchor]:
    """D 的预计睡眠锚点：作息"非清醒区间"与 D 的**午夜窗口求交**，至多两段。

    设计 §2：D 这一行负责 D 的整日 ``[D 00:00, D+1 00:00)``，D 凌晨的预计睡眠也
    由 D 自己产出。因此返回的每一段都**完全归属 D**，任何一段都不越过午夜
    （旧实现给 D 夜写一个跨到 D+1 的锚点，会让 D+1 凌晨同时被两行声明，
    正是改版要消除的重叠来源）。

    - ``wake=08:00, rest=23:00`` → ``[00:00,08:00)`` 与 ``[23:00,24:00)``；
    - ``wake=10:00, rest=02:00``（即 rest < wake）→ 只有 ``[02:00,10:00)``；
      午夜到 02:00 是清醒时间，不造睡眠；
    - ``wake == rest`` → 作息没有有效睡眠窗，**不造**任何睡眠段
      （不假定"她一定睡了"，也不补一个全天睡眠）。

    区间左闭右开：``rest=23:00`` 的 ``end`` 是**次日 00:00** 的瞬时，
    不是 ``D 23:59``；``start < end`` 的单段才保留（DST 极端情况下可能出现
    两分钟落在同一瞬时，那段直接丢弃，不写零长度锚点）。
    """
    wake = int(routine.wake_minute) % (24 * 60)
    rest = int(routine.rest_minute) % (24 * 60)
    if wake == rest:
        # 现有作息语义：wake == rest 表示"没有有效睡眠窗"，不是"睡了 24 小时"。
        return []
    # 非清醒区间 [rest, wake)：rest < wake 时它本身就在同一天内，与 D 的交集就是
    # 它自己；rest > wake 时它跨过午夜，与 D 的午夜窗口求交拆成两段
    # （午夜后的头段 + 午夜前的尾段），两段各自都完全归属 D。
    bounds = [(rest, wake)] if rest < wake else [(0, wake), (rest, 24 * 60)]
    zone = resolve_zone(timezone)
    anchors: list[Anchor] = []
    for start_minute, end_minute in bounds:
        start = _local_minute_instant(plan_day, start_minute, zone)
        end = _local_minute_instant(plan_day, end_minute, zone)
        if start >= end:
            continue
        anchors.append(
            Anchor(
                kind=SLEEP_ANCHOR,
                start_at=start,
                end_at=end,
                title="睡觉",
                protective=False,
            )
        )
    return anchors


def _anchor_key(anchor: Anchor) -> str:
    """锚点的可追溯标签：优先来源 id，否则用"种类@起点"（不含正文）。"""
    if anchor.source_id:
        return anchor.source_id
    return f"{anchor.kind}@{anchor.start_at.isoformat()}"


def _with_reason_codes(anchor: Anchor, *codes: str) -> Anchor:
    """给锚点加上原因码（去重、保序）；没有新码时原样返回。"""
    merged = tuple(dict.fromkeys((*anchor.reason_codes, *codes)))
    if merged == anchor.reason_codes:
        return anchor
    return replace(anchor, reason_codes=merged)


def _subtract_interval(
    start: datetime, end: datetime, cuts: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """从 ``[start,end)`` 里挖掉若干区间，返回按时间排序的剩余片段。"""
    pieces: list[tuple[datetime, datetime]] = [(_aware(start), _aware(end))]
    for cut_start, cut_end in cuts:
        remaining: list[tuple[datetime, datetime]] = []
        for piece_start, piece_end in pieces:
            if cut_end <= piece_start or cut_start >= piece_end:
                remaining.append((piece_start, piece_end))
                continue
            if piece_start < cut_start:
                remaining.append((piece_start, cut_start))
            if cut_end < piece_end:
                remaining.append((cut_end, piece_end))
        pieces = remaining
    return sorted((item for item in pieces if item[0] < item[1]), key=lambda item: item[0])


def _minutes(start: datetime, end: datetime) -> int:
    return max(0, int((_aware(end) - _aware(start)).total_seconds() // 60))


def resolve_anchor_conflicts(
    anchors: list[Anchor] | tuple[Anchor, ...],
    *,
    window_start: datetime,
    window_end: datetime,
    routine: RoutineSnapshot,
) -> tuple[list[Anchor], list[AnchorConflict]]:
    """把候选锚点裁决成 D 的可用集合，并**由代码**记下所有冲突。

    裁决规则全部来自设计 §3「约定/安排输入与优先级」，这里不做任何模型判断：

    1. **承诺保护窗裁到 D 内**：``[due, due+60)`` 伸进次日凌晨的部分被裁掉，
       否则 D−1 23:30 的约定会变成对 D 凌晨的跨午夜声明。
       （完全落在 D 外的锚点直接丢弃：它不属于这一天。）
    2. **同级硬约定冲突：两边都留**、两边都带 ``needs_review``，冲突清单里
       记下两个来源 id。不让代码或模型选一个"赢家"，履约由各自状态机解决。
       完全同一区间的两条视为同一条约定的两种来源，**不报冲突**（否则每晚
       都会因为同一件事被记一次）。
    3. **承诺 × 预计睡眠：保留承诺**，预计睡眠缩短/分裂并标 ``routine_conflict``；
       若剩余睡眠 < :data:`MIN_SLEEP_MINUTES` 再加 ``needs_review``。
       承诺本身既不动、也不带冲突码——"睡眠下限"不能当成暗中撤销承诺的理由。
    4. ``wake == rest``（没有有效睡眠窗）时**不造**睡眠锚点，也不假造一个醒点。

    睡眠锚点必须由调用方用**规范时区**算好传进来（:func:`sleep_anchors_for_day`）。
    这里不按 ``routine.timezone`` 现场推算：那是作息表里的旧值，不参与日界计算。

    返回 ``(anchors, conflicts)``；``anchors`` 按开始时间排序，冲突码同时挂在
    相关锚点上（落库时进段的 ``source_event_ids``）和冲突清单里（供日报/审计）。
    """
    conflicts: list[AnchorConflict] = []
    wake = int(routine.wake_minute) % (24 * 60)
    rest = int(routine.rest_minute) % (24 * 60)
    no_sleep_window = wake == rest

    sleep_inputs: list[Anchor] = []
    hard: list[Anchor] = []
    for anchor in anchors:
        start = _aware(anchor.start_at)
        end = _aware(anchor.end_at)
        if anchor.kind == SLEEP_ANCHOR:
            if no_sleep_window or start >= end:
                # 作息没有有效睡眠窗：不造睡眠。调用方若传进来，也不照抄。
                continue
            sleep_inputs.append(replace(anchor, start_at=start, end_at=end))
            continue
        clipped_start = max(start, _aware(window_start))
        clipped_end = min(end, _aware(window_end))
        if clipped_start >= clipped_end:
            continue
        hard.append(
            anchor
            if (clipped_start, clipped_end) == (start, end)
            else replace(anchor, start_at=clipped_start, end_at=clipped_end)
        )

    # 同级硬约定：两两相交就两边都留、两边都带 needs_review。
    for index in range(len(hard)):
        for other in range(index + 1, len(hard)):
            left, right = hard[index], hard[other]
            if (left.start_at, left.end_at) == (right.start_at, right.end_at):
                continue
            if not overlaps(left.start_at, left.end_at, right.start_at, right.end_at):
                continue
            hard[index] = _with_reason_codes(hard[index], REASON_NEEDS_REVIEW)
            hard[other] = _with_reason_codes(hard[other], REASON_NEEDS_REVIEW)
            conflicts.append(
                AnchorConflict(
                    reason_code=REASON_NEEDS_REVIEW,
                    anchor_ids=(_anchor_key(left), _anchor_key(right)),
                    detail=f"同级硬约定重叠，两边都保留：{left.title}｜{right.title}",
                )
            )

    # 承诺 × 预计睡眠：承诺留着，睡眠被挖短/挖断。
    trimmed: list[tuple[Anchor, list[tuple[datetime, datetime]], list[Anchor]]] = []
    before_trim = 0
    for sleep in sleep_inputs:
        before_trim += _minutes(sleep.start_at, sleep.end_at)
        cuts = [
            item
            for item in hard
            if overlaps(sleep.start_at, sleep.end_at, item.start_at, item.end_at)
        ]
        pieces = (
            _subtract_interval(
                sleep.start_at, sleep.end_at, [(item.start_at, item.end_at) for item in cuts]
            )
            if cuts
            else [(sleep.start_at, sleep.end_at)]
        )
        trimmed.append((sleep, pieces, cuts))
        for cut in cuts:
            remaining = sum(_minutes(start, end) for start, end in pieces)
            conflicts.append(
                AnchorConflict(
                    reason_code=REASON_ROUTINE_CONFLICT,
                    anchor_ids=(_anchor_key(sleep), _anchor_key(cut)),
                    detail=f"承诺压在预计睡眠上，保留承诺：{cut.title}；睡眠剩余 {remaining} 分钟",
                )
            )

    sleep_minutes = sum(
        _minutes(start, end) for _sleep, pieces, _cuts in trimmed for start, end in pieces
    )
    had_routine_conflict = any(cuts for _sleep, _pieces, cuts in trimmed)
    below_floor = had_routine_conflict and sleep_minutes < MIN_SLEEP_MINUTES
    if below_floor:
        conflicts.append(
            AnchorConflict(
                reason_code=REASON_NEEDS_REVIEW,
                anchor_ids=tuple(
                    _anchor_key(sleep) for sleep, _pieces, cuts in trimmed if cuts
                ),
                detail=(
                    f"承诺占用后剩余预计睡眠 {sleep_minutes} 分钟 < 下限 "
                    f"{MIN_SLEEP_MINUTES} 分钟，标 needs_review（不改作息、不取消承诺）"
                ),
            )
        )

    resolved: list[Anchor] = []
    for sleep, pieces, cuts in trimmed:
        extra: list[str] = []
        if cuts:
            extra.append(REASON_ROUTINE_CONFLICT)
        if below_floor:
            # 这一天的预计睡眠已经被压到保护下限以下：**剩下的睡眠段也要带码**，
            # 否则被完全覆盖的那一段什么都不剩，原因码就无处可查。
            extra.append(REASON_NEEDS_REVIEW)
        for start, end in pieces:
            if (start, end) == (sleep.start_at, sleep.end_at) and not extra:
                resolved.append(sleep)
                continue
            resolved.append(
                replace(
                    _with_reason_codes(sleep, *extra), start_at=start, end_at=end
                )
            )
    resolved.extend(hard)
    resolved.sort(key=lambda item: (item.start_at, item.end_at))

    plan = AnchorPlan(
        anchors=resolved,
        conflicts=conflicts,
        sleep_minutes=sleep_minutes,
        sleep_minutes_before_trim=before_trim,
    )
    return plan.anchors, plan.conflicts


async def load_commitment_anchors(
    session: AsyncSession, *, actor_id: str, window_start: datetime, window_end: datetime
) -> list[Anchor]:
    """窗口内（含保护窗）的不可移动约定。

    来源两类，都是**已经存在的事实**，不是这次大纲要生成的内容：
    - ``commitments``（对人的承诺，``state=active``）；
    - ``agenda_plans`` 里 ``commitment_level=confirmed`` 的约定。

    每条锚点都带可追溯的 ``source_id``（``commitment:<id>`` /
    ``agenda_plan:<id>``）：冲突清单与段的 ``source_event_ids`` 要能指回来源。
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
                source_id=f"commitment:{row.id}",
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
                source_id=f"agenda_plan:{row.id}",
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
    # 本次走的是哪个相位（due/catchup/late）。`ensure_day_plan` 直接调时不填。
    phase: str = ""


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


@dataclass(frozen=True)
class PersonaProjection:
    """写进大纲 prompt 的**结构化**人设投影（不是完整 ``system_prompt``）。

    ``token_estimate`` 是粗略估算（见 :data:`PERSONA_ESTIMATE_METHOD`）：
    它用来保证不超 :data:`PERSONA_TOKEN_LIMIT`，不冒充 provider 的精确计数。
    缺字段一律 :data:`PERSONA_UNKNOWN`，不从空白处编人格。
    """

    identity: str = PERSONA_UNKNOWN
    name: str = ""
    personality: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    habits: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    scenario_rules: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()
    situation: tuple[str, ...] = ()
    token_estimate: int = 0
    token_limit: int = PERSONA_TOKEN_LIMIT
    estimate_method: str = PERSONA_ESTIMATE_METHOD
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        """给提示词用的普通 dict（``name`` 保留给模板里的 ``{name}``）。"""
        return {
            "name": self.name,
            "identity": self.identity,
            "personality": list(self.personality),
            "values": list(self.values),
            "habits": list(self.habits),
            "interests": list(self.interests),
            "boundaries": list(self.boundaries),
            "scenario_rules": list(self.scenario_rules),
            "relationships": list(self.relationships),
            "situation": list(self.situation),
        }


_PERSONA_SECTION_KEYS: tuple[str, ...] = (
    "personality",
    "values",
    "habits",
    "interests",
    "boundaries",
    "scenario_rules",
    "relationships",
    "situation",
)


def _estimate_projection_tokens(payload: dict[str, object]) -> int:
    """粗略 token 估算：与预算裁剪同一把尺子（见 ``PERSONA_ESTIMATE_METHOD``）。"""
    from ling.kernel.context import estimate_tokens

    return estimate_tokens(json.dumps(payload, ensure_ascii=False))


def _trait_bucket(key: str) -> str:
    lowered = key.lower()
    for bucket, keywords in _TRAIT_BUCKETS:
        if any(word in lowered for word in keywords):
            return bucket
    # 认不出来的键归"性格/风格"：宁可少解释一条，也不丢一条。
    return "personality"


def _familiarity_level(value: float) -> str:
    for threshold, label in _FAMILIARITY_LEVELS:
        if value >= threshold:
            return label
    return _FAMILIARITY_DEFAULT


def _scenario_rules(overrides: dict[str, str]) -> list[str]:
    """``scenario_prompts`` 里**写过的**场景覆盖 → 短规则（只取前 N 条）。

    这里**不 import persona 域**（域边界闸门不允许跨域 import 实现），所以只有
    一份"键 → 展示标签"的映射：它只影响 prompt 里那行字怎么读，不影响任何判定。
    未知键直接用键名，不丢信息。
    """
    if not overrides:
        return []
    ordered = [key for key in _SCENARIO_ORDER if key in overrides]
    ordered.extend(
        sorted(key for key in overrides if key not in _SCENARIO_ORDER)
    )
    rules: list[str] = []
    for key in ordered:
        text = overrides[key].strip()
        if not text:
            continue
        rules.append(
            f"{_SCENARIO_LABELS.get(key, key)}：{_short(text, PERSONA_MAX_ITEM_CHARS)}"
        )
        if len(rules) >= PERSONA_MAX_SCENARIO_RULES:
            break
    return rules


async def _persona_relationships(session: AsyncSession, actor_id: str) -> list[str]:
    """1–2 段重要关系的**定性**摘要（不含他人标识、私聊细节与信任数值）。"""
    from ling.settings.user_preferences import owner_subject_id

    owner = await owner_subject_id(session)
    rows = list(
        (
            await session.execute(
                select(RelationshipState)
                .where(RelationshipState.actor_id == actor_id)
                .order_by(
                    RelationshipState.familiarity.desc(),
                    RelationshipState.trust.desc(),
                    RelationshipState.subject_id,
                )
                .limit(PERSONA_MAX_RELATIONSHIPS)
            )
        ).scalars()
    )
    summaries: list[str] = []
    for row in rows:
        label = (
            "当前主要对话者"
            if owner and str(row.subject_id) == owner
            else "另一位亲近的人"
        )
        bits = [f"{label}：{_familiarity_level(float(row.familiarity or 0.0))}"]
        boundaries = [
            str(item).strip() for item in (row.boundaries or []) if str(item).strip()
        ]
        if boundaries:
            bits.append(f"边界 {len(boundaries)} 条，例如 {_short(boundaries[0], 24)}")
        summaries.append("；".join(bits))
    return summaries


async def _persona_situation(
    session: AsyncSession, actor_id: str, now: datetime
) -> list[str]:
    """当前处境：软作息 + 此刻场景（**模拟状态**，过期就是未知）。"""
    lines: list[str] = []
    routine = await session.get(ActorRoutineRow, actor_id)
    if routine is not None:
        lines.append(
            f"作息：{_minute_hhmm(int(routine.wake_minute))} 醒 / "
            f"{_minute_hhmm(int(routine.rest_minute))} 歇（软作息，可被打断）"
        )
    scene = await session.get(SceneCommitRow, actor_id)
    if scene is not None:
        valid_until = scene.valid_until
        if valid_until is None or _aware(valid_until) > now:
            state = str(scene.state)
            lines.append(f"此刻：{_SCENE_LABELS.get(state, PERSONA_UNKNOWN)}（模拟）")
        else:
            lines.append("此刻：未知（场景已过期）")
    return lines


async def build_persona_projection(
    session: AsyncSession,
    persona_id: str,
    *,
    actor_id: str | None = None,
    now: datetime | None = None,
) -> PersonaProjection:
    """人设 → 有界、确定性的结构化投影（设计 §3「persona_summary 构造」）。

    几点是**硬约束**，不是风格选择：

    - **不塞完整 ``system_prompt``**：正文可能几千字，每晚全文截断喂进去既超预算、
      又把安全边界和写作风格混在一起。关键设定应由人设编辑/迁移提取成可审计的
      结构化短项（``traits`` / ``boundaries`` / ``scenario_prompts``）。
    - **缺字段写"未知"**：空白处不编人格。
    - **先限条数、单条长度，再压到 ≤400 输入 token**：超预算时按固定优先级
      先删可选场景与口癖细节，**绝不先挤掉**边界、价值观与作息相关习惯。
    - ``traits`` 是使用者自由填的 dict，所以只按稳定的键名关键词归类；
      认不出的键归"性格/风格"（不丢信息）。
    - 关系只做定性摘要：不带他人标识、私聊细节或信任数值（设计 §3 关系上限）。
    - token 数是**粗略估算**并如实标注（:data:`PERSONA_ESTIMATE_METHOD`）。
    """
    moment = _aware(now or utcnow())
    actor = actor_id or _settings_actor_id()
    persona = await session.get(Persona, persona_id)

    name = ""
    traits: dict[str, object] = {}
    boundary_items: list[str] = []
    scenario_overrides: dict[str, str] = {}
    if persona is not None:
        name = _short(str(persona.display_name or persona.name or "").strip(), 24)
        if isinstance(persona.traits, dict):
            traits = {str(key): value for key, value in persona.traits.items()}
        boundary_items = [
            _short(str(item).strip(), PERSONA_MAX_ITEM_CHARS)
            for item in (persona.boundaries or [])
            if str(item).strip()
        ]
        if isinstance(persona.scenario_prompts, dict):
            scenario_overrides = {
                str(key): str(value)
                for key, value in persona.scenario_prompts.items()
                if str(value).strip()
            }

    identity = f"称呼：{name}" if name else PERSONA_UNKNOWN

    buckets: dict[str, list[str]] = {
        "personality": [],
        "values": [],
        "habits": [],
        "interests": [],
    }
    for key in sorted(traits):
        value = traits[key]
        if value is None or value == "":
            continue
        text = _short(str(value).strip(), PERSONA_MAX_ITEM_CHARS)
        if not text:
            continue
        bucket = _trait_bucket(key)
        if len(buckets[bucket]) >= PERSONA_MAX_ITEMS:
            continue
        buckets[bucket].append(f"{_short(key, 16)}：{text}")

    sections: dict[str, list[str]] = {
        "personality": buckets["personality"],
        "values": buckets["values"],
        "habits": buckets["habits"],
        "interests": buckets["interests"],
        "boundaries": boundary_items[:PERSONA_MAX_ITEMS],
        "scenario_rules": _scenario_rules(scenario_overrides),
        "relationships": await _persona_relationships(session, actor),
        "situation": await _persona_situation(session, actor, moment),
    }
    for key in _PERSONA_SECTION_KEYS:
        if not sections[key]:
            sections[key] = [PERSONA_UNKNOWN]

    def payload() -> dict[str, object]:
        return {"name": name, "identity": identity, **{k: list(v) for k, v in sections.items()}}

    estimate = _estimate_projection_tokens(payload())
    truncated = False

    # ① 先按固定优先级删**条数**（每类至少留一条，留的是优先级最高的那条）。
    for key in _PERSONA_TRIM_ORDER:
        while estimate > PERSONA_TOKEN_LIMIT and len(sections[key]) > 1:
            sections[key] = sections[key][:-1]
            truncated = True
            estimate = _estimate_projection_tokens(payload())

    # ② 再压**单条长度**（可选段先压，边界/价值观/习惯最后压，但一样要压）：
    #    极端的长文本只能截断，不能靠"删掉边界"来省预算。
    for limit in (40, 24, 12):
        if estimate <= PERSONA_TOKEN_LIMIT:
            break
        for key in (*_PERSONA_TRIM_ORDER, "values", "habits", "boundaries"):
            sections[key] = [_short(item, limit) for item in sections[key]]
        truncated = True
        estimate = _estimate_projection_tokens(payload())

    # ③ 极端兜底（例如边界本身就超长）：每类留一条压到 12 字，称呼也压短。
    #    保证"≤400"是硬保证，而不是"大概能过"。
    if estimate > PERSONA_TOKEN_LIMIT:
        for key in _PERSONA_SECTION_KEYS:
            sections[key] = [_short(sections[key][0], 12)]
        name = _short(name, 8)
        identity = _short(identity, 16)
        truncated = True
        estimate = _estimate_projection_tokens(payload())

    return PersonaProjection(
        identity=identity,
        name=name,
        personality=tuple(sections["personality"]),
        values=tuple(sections["values"]),
        habits=tuple(sections["habits"]),
        interests=tuple(sections["interests"]),
        boundaries=tuple(sections["boundaries"]),
        scenario_rules=tuple(sections["scenario_rules"]),
        relationships=tuple(sections["relationships"]),
        situation=tuple(sections["situation"]),
        token_estimate=estimate,
        truncated=truncated,
    )


async def _persona_summary(session: AsyncSession, persona_id: str) -> dict[str, object]:
    """旧名转发：阶段 1.5 起人设摘要走 :func:`build_persona_projection`。

    保留这个名字是因为 ``build_outline_prompt`` 与既有调用点都按 dict 用；
    真正的内容（结构化、有界、缺字段写"未知"）在投影里。
    """
    return (await build_persona_projection(session, persona_id)).as_dict()


async def _experience_summary(
    session: AsyncSession, *, actor_id: str, now: datetime
) -> list[str]:
    """已证实经历的粗摘要（只取**事实级**的 ``self_experiences``）。

    没有证据就没有经历——计划、打算、梦都不能出现在这里。
    P0.5：只有"证据存在"也还不够。``clock_only``（典型：模拟活动的计划时段结束）
    不是事实，必须挡在 prompt 之外。判定走
    :func:`ling.domains.evidence.fact_eligible_experiences`——与自我时间线、
    控制台事实层、梦境候选同一把闸，并且会回查来源活动的证据种类。
    """
    since = now - timedelta(hours=EXPERIENCE_LOOKBACK_HOURS)
    rows = list(
        (
            await session.execute(
                select(SelfExperienceRow)
                .where(
                    SelfExperienceRow.actor_id == actor_id,
                    SelfExperienceRow.occurred_at >= since,
                )
                .order_by(SelfExperienceRow.occurred_at.desc())
                # 多取一些：被读侧排除的行（历史 clock_only）不能占满摘要配额。
                .limit(MAX_EXPERIENCE_SUMMARIES * 4)
            )
        ).scalars()
    )
    eligible = await fact_eligible_experiences(
        session, rows, limit=MAX_EXPERIENCE_SUMMARIES
    )
    return [_short(str(item.summary), 120) for item in eligible]


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

    锚点自带的 ``source_id`` 与原因码（``routine_conflict`` / ``needs_review``）
    写进段的 ``source_event_ids``（``reason:<code>``）：这是设计 §6 规定的
    **原因码落点之一**，不加列、不加表。已经**完全过去**的锚点不写
    （"过去永不回填"）；部分落在起点之前的锚点整段保留——它描述的是作息
    预期（预计睡眠），不是"已经做过的事"。
    """
    drafts: list[SegmentDraft] = []
    for anchor in anchors:
        if not overlaps(anchor.start_at, anchor.end_at, window_start, window_end):
            continue
        if anchor.end_at <= not_before:
            # 整段都在可变起点之前：当作历史，不再写进新版本。
            continue
        source_event_ids: list[str] = []
        if anchor.source_id:
            source_event_ids.append(anchor.source_id)
        source_event_ids.extend(f"{REASON_TAG_PREFIX}{code}" for code in anchor.reason_codes)
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
                source_event_ids=source_event_ids,
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
    # 来源 id 与 ``reason:<code>``。原因码只落这里和 `day_plans.last_error_code`
    # （设计 §6），不新增列/表。
    source_event_ids: list[str] = field(default_factory=list)


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
        # 锚点若已开始（当天凌晨那段预计睡眠）也保留，避免把"正在睡"翻成缺口。
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
        # 已经开始的段（凌晨的预计睡眠、过去的大块）保留原样：改版只排未来。
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
        # 改规范时区使日界平移时，旧窗口留下的睡眠锚点会与新锚点重叠——
        # 保留已开始部分，接缝由 00:00 的零模型校验修补（设计 §2）。
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
                    source_event_ids=list(draft.source_event_ids),
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
    force: bool = False,
) -> OutlineOutcome:
    """确保某个**计划日**有一版可用大纲（幂等）。

    这是阶段 1 唯一的大纲写入入口，`AgendaService.ensure_day_plan` 只是转发。
    调用方负责 commit/rollback；本函数内部只用嵌套事务保护"抢占/提交"这两处。

    ``plan_day`` 是**显式目标日 D**：传了就用它，窗口取 D 的本地午夜区间
    （:func:`plan_day_window`），并允许 ``now < window_start`` 的**预生成**
    （D−1 21:00 的正常路径，可变起点 = ``max(now, window_start)``）。
    不传才按 ``now`` 推当前计划日。

    ``force=True`` 跳过"快照没变就返回 unchanged"的短路，用于醒前 catch-up
    补试（设计 §3：仅当原因是**可恢复**且预算仍有余量时才补试）。默认 ``False``
    时行为与阶段 1 完全一致：同一计划日同一快照只生成一次。

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
    if (
        row.state in ("ready", "degraded")
        and int(row.revision) > 0
        and snapshot_same
        and not force
    ):
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

    commitment_anchors = await load_commitment_anchors(
        session, actor_id=actor_id, window_start=window_start, window_end=window_end
    )
    # D 的睡眠锚点：作息与 D 的午夜窗口求交，至多两段，都在 D 内。
    # 冲突裁决**由代码做**（设计 §3）：承诺保护窗裁到 D 内、同级硬约定两边都留、
    # 承诺压睡眠时保留承诺并标原因码。裁决结果同时进 prompt 与段的原因码。
    anchors, anchor_conflicts = resolve_anchor_conflicts(
        [*sleep_anchors_for_day(plan_day, timezone, routine), *commitment_anchors],
        window_start=window_start,
        window_end=window_end,
        routine=routine,
    )

    # 可变起点：过去永不回填；catch-up 也从 now 开始。
    not_before = max(now, window_start)
    places = await _valid_places(session, actor_id)
    place_ids = {item["id"] for item in places}
    persona = (
        await build_persona_projection(
            session, await _resolve_persona_id(session), actor_id=actor_id, now=now
        )
    ).as_dict()
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

    # 锚点冲突需要人工/系统复核时，用 `day_plans.last_error_code` 留一个码
    # （设计 §6 的原因是"不加列不加表"）。只放 needs_review：`routine_conflict`
    # 是局部取舍，已经落在段的 `reason:` 上，不必把整天标成"有错"。
    if reason_code is None and any(
        item.reason_code == REASON_NEEDS_REVIEW for item in anchor_conflicts
    ):
        reason_code = REASON_NEEDS_REVIEW

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


# 相位的"严重度"：late > catchup > due。相位只允许**减少**模型使用，
# 不允许把过期的 due 当成"还能正常调模型"——否则醒到一半还在为早上烧钱。
_PHASE_SEVERITY: dict[str, int] = {
    PHASE_DUE: 0,
    PHASE_CATCHUP: 1,
    PHASE_LATE: 2,
}


def _effective_phase(*, requested: str, actual: str) -> str:
    """取请求相位与实际相位里**更保守**的那个（相位不能凭空放宽）。"""
    if requested not in _PHASE_SEVERITY:
        raise ValueError(f"未知的大纲相位：{requested!r}（只能是 due/catchup/late）")
    if _PHASE_SEVERITY[requested] >= _PHASE_SEVERITY[actual]:
        return requested
    return actual


async def _tag_degraded_late(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    now: datetime,
    outcome: OutlineOutcome,
) -> OutlineOutcome:
    """醒点及以后仍没有 ready：把原因码钉成 ``degraded_late``（不增 revision）。

    只改 ``day_plans.last_error_code`` 这一个落点：时间轴形状没变，所以不需要
    新版本；已经 ready 的计划**不**降级（它不需要这个码）。
    """
    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    if row is None or str(row.state) == "ready":
        return outcome
    if str(row.last_error_code or "") != REASON_DEGRADED_LATE:
        row.last_error_code = REASON_DEGRADED_LATE
        row.updated_at = now
        await session.flush()
    return replace(outcome, reason_code=REASON_DEGRADED_LATE)


async def outline_work(
    session: AsyncSession,
    plan_day: date,
    phase: str,
    *,
    actor_id: str,
    router: object | None = None,
    config_reader: object | None = None,
    now: datetime | None = None,
    timezone: str | None = None,
    timezone_source: str | None = None,
    lease_owner: str | None = None,
) -> OutlineOutcome:
    """按**目标日 + 相位**跑一次大纲作业（调度唯一该调的入口）。

    这是把 `ensure_day_plan` 的"取输入 → 调用 → 校验 → 提交/降级"包成可复用
    入口的那一层，相位只决定"能不能调模型、从哪儿开始排"：

    - ``due``（D−1 21:00 正常到期）：正常调模型，可变起点 = ``max(now, D 00:00)``；
    - ``catchup``（错过 21:00、但还没到 D 的醒点）：可以补跑，但**只排 ``now``
      之后**（过午夜不回填）。若前一次失败的原因是**可恢复**的
      （:data:`RETRYABLE_REASON_CODES`）且预算仍有剩余，会真正补试一次；
      非法输出/预算耗尽等终态不重试（设计 §3「catch-up」）。
    - ``late``（已到/过了 D 的醒点）：**零模型**，只补未过去的确定性锚点与空档，
      原因码钉成 ``degraded_late``；醒后新约定走事件局部重排，不在这里补造早晨。

    相位只会**更保守**：调用方给 ``due``、但按 ``now`` 其实已经过了醒点，会
    自动降成 ``late``（不烧一次注定过期的模型调用）。反过来不会"升级"。
    """
    moment = _aware(now or utcnow())
    if phase not in _PHASE_SEVERITY:
        raise ValueError(f"未知的大纲相位：{phase!r}（只能是 due/catchup/late）")
    if timezone is None:
        timezone, timezone_source = await resolve_plan_timezone(session)
    else:
        timezone_source = timezone_source or "explicit"
    routine = await _routine_snapshot(session, actor_id)
    _target, actual = outline_due_decision(
        moment,
        timezone=timezone,
        wake_minute=routine.wake_minute,
        plan_day=plan_day,
    )
    effective = _effective_phase(requested=phase, actual=actual)

    if effective == PHASE_LATE:
        # 零模型：把 router/config_reader 显式置空，结构上堵死模型调用。
        outcome = await ensure_day_plan(
            session,
            actor_id=actor_id,
            router=None,
            config_reader=None,
            plan_day=plan_day,
            now=moment,
            timezone=timezone,
            timezone_source=timezone_source,
            lease_owner=lease_owner,
        )
        tagged = await _tag_degraded_late(
            session, actor_id=actor_id, plan_day=plan_day, now=moment, outcome=outcome
        )
        return replace(tagged, phase=effective)

    force = False
    if effective == PHASE_CATCHUP:
        row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
        if (
            row is not None
            and str(row.state) != "ready"
            and str(row.last_error_code or "") in RETRYABLE_REASON_CODES
        ):
            # 醒前补试：只在"原因可恢复"时重跑；同一作业、同一预算闸。
            force = True

    outcome = await ensure_day_plan(
        session,
        actor_id=actor_id,
        router=router,
        config_reader=config_reader,
        plan_day=plan_day,
        now=moment,
        timezone=timezone,
        timezone_source=timezone_source,
        lease_owner=lease_owner,
        force=force,
    )
    return replace(outcome, phase=effective)


async def run_due_outline(
    session: AsyncSession,
    *,
    actor_id: str,
    router: object | None = None,
    config_reader: object | None = None,
    plan_day: date | None = None,
    now: datetime | None = None,
    lease_owner: str | None = None,
) -> OutlineOutcome | None:
    """worker 将来要调的函数：在到期边界做一次"确保 D 有大纲"。

    ``plan_day`` 是**显式目标日 D**：传了就按 D 的午夜窗口
    （:func:`plan_day_window`）算，且允许在 ``now < window_start`` 时**预生成**
    ——那正是 D−1 21:00（:func:`outline_due_at`）的正常路径。不传则退回
    "按 ``now`` 推当前计划日"的旧行为，未到窗口起点直接返回 ``None``
    （不提前烧钱）。调度接线不在本模块（下一轮）。

    其余分支（下一轮接调度时细化）：
    - 当前计划日已过窗口起点但没生成：生成从 ``now`` 开始的剩余部分（catch-up）；
    - 离休息不足 40 分钟：只写规则睡眠锚点并标 ``degraded``（不调模型）；
    - 当前计划日已 ready/degraded 且快照没变：幂等返回 unchanged。

    这里**没有**定时器/循环，也没有 outbox：只提供可调用的入口。

    **阶段 1.5b 之后新代码不要用它**：调度应改成"先
    :func:`outline_due_decision` 定目标日/相位，再 :func:`outline_work`"，
    这样 21:00 到期、醒前补跑、醒后零模型三档才有明确判据（尤其"距 rest 40
    分钟"这条旧判据与设计 §3 的"截止理由是 D 的醒点"不符）。本函数保留是为了
    不悄悄改掉既有调用与测试的语义。
    """
    now = _aware(now or utcnow())
    timezone, timezone_source = await resolve_plan_timezone(session)
    explicit_target = plan_day is not None
    if plan_day is None:
        plan_day = plan_day_for(now, timezone)
    window_start, window_end = plan_day_window(plan_day, timezone)
    if not explicit_target and now < window_start:
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


# ---------------------------------------------------------------- 接缝与新锚点


@dataclass(frozen=True)
class SeamReport:
    """一次午夜接缝校验的结果（**零模型**，字段都是可解释的）。"""

    plan_day: date
    state: str = "unchanged"  # no_plan / unchanged / superseded / annotated / busy
    plan_id: str = ""
    revision_before: int = 0
    revision_after: int = 0
    # 被置为 superseded 的可选段（冲突且还没开始的那部分）
    superseded_segment_ids: tuple[str, ...] = ()
    # 与锚点冲突、但**已经开始/已过去**的段：不可动，只能报出来等人工处置
    frozen_segment_ids: tuple[str, ...] = ()
    # 硬的确认锚点（来源 id）
    anchor_ids: tuple[str, ...] = ()
    # 两日睡眠边界在 UTC 上重叠的描述（理论上是时区变更后的旧值残留）
    boundary_conflicts: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    # 结构性零模型：这里没有 router 参数，永远 0 次。
    calls: int = 0

    @property
    def changed(self) -> bool:
        """这一遍**有没有改动计划**（版本推进，或给段补了睡眠边界码）。"""
        return self.state in ("superseded", "annotated") or (
            self.revision_after != self.revision_before
        )


def _movable_segment(segment: ScheduleSegmentRow) -> bool:
    """能不能被锚点冲突"动到"：只有**未确认的可选段**才可动。

    睡眠/承诺锚点（``anchor_kind`` 非空）与 confirmed 的约定都不是可替换候选：
    它们本身就是不可移动锚点，或者属于别的状态机。
    """
    return str(segment.anchor_kind) == "" and str(segment.commitment_level) != "confirmed"


async def _sleep_boundary_overlaps(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    my_segments: list[ScheduleSegmentRow],
) -> list[tuple[ScheduleSegmentRow, str]]:
    """D−1 与 D 的**预计睡眠**在 UTC 上是否重叠（零模型，只读检查）。

    正常情况下两天的睡眠段各自落在自己的午夜窗口内，不会重叠；能重叠的只有
    "旧时区/旧作息留下的段还没被清掉"这类接缝残留。这里**不移动任何段**，
    只把重叠报出来（并给 D 侧那段补一个原因码）。
    """
    previous = await load_day_plan(
        session, actor_id=actor_id, plan_day=plan_day - timedelta(days=1)
    )
    if previous is None:
        return []
    previous_segments = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == previous.id,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.anchor_kind == SLEEP_ANCHOR,
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars()
    )
    mine = [item for item in my_segments if str(item.anchor_kind) == SLEEP_ANCHOR]
    overlaps_found: list[tuple[ScheduleSegmentRow, str]] = []
    for day_before in previous_segments:
        for current in mine:
            if not overlaps(
                _aware(day_before.start_at),
                _aware(day_before.end_at),
                _aware(current.start_at),
                _aware(current.end_at),
            ):
                continue
            detail = (
                f"D−1 的预计睡眠 {_aware(day_before.start_at).isoformat()}→"
                f"{_aware(day_before.end_at).isoformat()} 与 D 的 "
                f"{_aware(current.start_at).isoformat()}→"
                f"{_aware(current.end_at).isoformat()} 重叠"
            )
            overlaps_found.append((current, detail))
    return overlaps_found


async def seam_check(
    session: AsyncSession,
    plan_day: date,
    *,
    actor_id: str | None = None,
    now: datetime | None = None,
) -> SeamReport:
    """午夜接缝校验：**零模型**复核 D−1 晚上新入库的已确认锚点与 D 现有段。

    设计 §2「21:00–24:00 接缝」+ §3 的 ``seam:{actor}:{plan_day}:…`` 作业：
    21:00 之后新确认的约定、时区/作息变化一旦入库，D 的计划可能已经和它冲突；
    00:00 这一遍只做**确定性**修补，不重新生成：

    - 与硬的确认锚点（``CommitmentRow`` active / confirmed ``agenda_plans``）
      冲突、且**还没开始**的可选段 → ``status=superseded`` +
      ``source_event_ids`` 追加 ``reason:needs_review``，整日 ``revision+1``；
    - 已经开始的段**不动**（过去/进行中的是历史），只在报告里列出来；
    - 校验 D−1 与 D 的预计睡眠边界：重叠只补 ``reason:sleep_boundary_conflict``
      并报出来，**不移动段**（动睡眠段需要人/阶段 3 的判断）；
    - 没有变化就不写任何东西：**不增 revision**、不写重复原因码，重复调用结果相同。

    这里没有 router 参数：接缝校验**结构上**不可能调模型（``calls`` 恒为 0）。

    实现上**不复刻"当晚新增"这个时间窗判据**：D 窗口内所有 active 的
    ``CommitmentRow`` 与 confirmed 的 ``agenda_plans`` 都参与比对。理由——能被
    新锚点碰撞的段，21:00 的生成本来就排不进去（那时锚点已存在），所以两种取法
    效果等价；按 `created_at` 先过滤反而会漏掉"恢复/迁移后才确认"的迟到锚点。
    """
    moment = _aware(now or utcnow())
    actor = actor_id or _settings_actor_id()
    row = await load_day_plan(session, actor_id=actor, plan_day=plan_day)
    if row is None:
        # 还没有这一天的计划：没有"现有段"可复核。21:00 作业会按当时的锚点生成。
        return SeamReport(plan_day=plan_day, state="no_plan")

    window_start = _aware(row.window_start_at)
    window_end = _aware(row.window_end_at)
    routine = await _routine_snapshot(session, actor)
    commitment_anchors = await load_commitment_anchors(
        session, actor_id=actor, window_start=window_start, window_end=window_end
    )
    hard, _anchor_conflicts = resolve_anchor_conflicts(
        commitment_anchors,
        window_start=window_start,
        window_end=window_end,
        routine=routine,
    )

    segments = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == row.id,
                    ScheduleSegmentRow.status == "current",
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars()
    )
    floor = max(moment, window_start)

    supersede: list[tuple[ScheduleSegmentRow, Anchor]] = []
    frozen: list[str] = []
    for segment in segments:
        if not _movable_segment(segment):
            continue
        hit = next(
            (
                anchor
                for anchor in hard
                if overlaps(
                    _aware(segment.start_at),
                    _aware(segment.end_at),
                    anchor.start_at,
                    anchor.end_at,
                )
            ),
            None,
        )
        if hit is None:
            continue
        if _aware(segment.start_at) < floor:
            frozen.append(str(segment.id))
            continue
        supersede.append((segment, hit))

    boundary = await _sleep_boundary_overlaps(
        session, actor_id=actor, plan_day=plan_day, my_segments=segments
    )
    # 只把**新**的重叠当成变化：同一个原因码已经写过就不再写（幂等）。
    boundary_new = [
        (segment, detail)
        for segment, detail in boundary
        if f"{REASON_TAG_PREFIX}{REASON_SLEEP_BOUNDARY}"
        not in {
            str(item) for item in (segment.source_event_ids or [])
        }
    ]

    expected = int(row.revision)
    reason_codes: list[str] = []
    state = "unchanged"
    if not supersede and not boundary_new:
        # 没有变化：**不写任何东西**（不刷 updated_at、不增 revision、不重复写码）。
        # 已有的睡眠边界重叠仍然报出来，方便排障，但它不构成本次改动。
        return SeamReport(
            plan_day=plan_day,
            state="unchanged",
            plan_id=str(row.id),
            revision_before=expected,
            revision_after=expected,
            frozen_segment_ids=tuple(frozen),
            anchor_ids=tuple(_anchor_key(item) for item in hard),
            boundary_conflicts=tuple(detail for _segment, detail in boundary),
        )

    new_revision = expected + 1 if supersede else expected
    try:
        async with session.begin_nested():
            if supersede:
                result = await session.execute(
                    update(DayPlanRow)
                    .where(DayPlanRow.id == row.id, DayPlanRow.revision == expected)
                    .values(
                        revision=new_revision,
                        last_error_code=REASON_NEEDS_REVIEW,
                        updated_at=moment,
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    raise StaleRevisionError(expected)
                for segment, anchor in supersede:
                    segment.status = "superseded"
                    segment.revision_ended = new_revision
                    segment.source_event_ids = _append_unique(
                        segment.source_event_ids,
                        f"{REASON_TAG_PREFIX}{REASON_NEEDS_REVIEW}",
                        *_anchor_source_ids(anchor),
                    )
                    segment.updated_at = moment
            for segment, _detail in boundary_new:
                segment.source_event_ids = _append_unique(
                    segment.source_event_ids,
                    f"{REASON_TAG_PREFIX}{REASON_SLEEP_BOUNDARY}",
                )
                segment.updated_at = moment
            if not supersede:
                # 只有睡眠边界注解：段身份/形状没变，不增 revision，只留原因码。
                await session.execute(
                    update(DayPlanRow)
                    .where(DayPlanRow.id == row.id, DayPlanRow.revision == expected)
                    .values(last_error_code=REASON_SLEEP_BOUNDARY, updated_at=moment)
                )
            await session.flush()
    except StaleRevisionError:
        return SeamReport(
            plan_day=plan_day,
            state="busy",
            plan_id=str(row.id),
            revision_before=expected,
            revision_after=expected,
            superseded_segment_ids=(),
            frozen_segment_ids=tuple(frozen),
            anchor_ids=tuple(_anchor_key(item) for item in hard),
            reason_codes=("stale_revision",),
        )

    await session.refresh(row)
    if supersede:
        state = "superseded"
        reason_codes.append(REASON_NEEDS_REVIEW)
    if boundary_new:
        state = "superseded" if supersede else "annotated"
        reason_codes.append(REASON_SLEEP_BOUNDARY)
    return SeamReport(
        plan_day=plan_day,
        state=state,
        plan_id=str(row.id),
        revision_before=expected,
        revision_after=int(row.revision),
        superseded_segment_ids=tuple(str(item.id) for item, _anchor in supersede),
        frozen_segment_ids=tuple(frozen),
        anchor_ids=tuple(_anchor_key(item) for item in hard),
        boundary_conflicts=tuple(detail for _segment, detail in boundary),
        reason_codes=tuple(reason_codes),
    )


def _anchor_source_ids(anchor: Anchor) -> tuple[str, ...]:
    return (anchor.source_id,) if anchor.source_id else ()


def _replan_candidate(segment: ScheduleSegmentRow) -> bool:
    """哪些段属于"局部重排"的候选。

    - 硬约定（``anchor_kind=commitment``）不是候选：它本身就是不可移动锚点；
    - 预计睡眠（``anchor_kind=sleep``）**是**候选：承诺压上去时它要缩短/分裂
      （只是不能被"取消"——保留承诺是硬规则）；
    - 其余只有未确认的可选段可以被换掉。
    """
    kind = str(segment.anchor_kind)
    if kind == COMMITMENT_ANCHOR:
        return False
    if kind == SLEEP_ANCHOR:
        return True
    return str(segment.commitment_level) != "confirmed"


@dataclass(frozen=True)
class ReplanTodo:
    """"需要局部重排"的待办（阶段 1.5 只登记，不加表、不调模型）。

    真正做局部重排的是阶段 3：它按 ``plan_id + segment_ids + [range_start,
    range_end)`` 取范围，模型只能改这段，范围外的段和硬锚点原样保留。
    """

    plan_day: date
    plan_id: str
    anchor_ids: tuple[str, ...]
    segment_ids: tuple[str, ...]
    range_start: datetime
    range_end: datetime
    reason_codes: tuple[str, ...] = ()
    tag: str = REPLAN_TODO_TAG
    registered: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_day": self.plan_day.isoformat(),
            "plan_id": self.plan_id,
            "anchor_ids": list(self.anchor_ids),
            "segment_ids": list(self.segment_ids),
            "range_start": self.range_start.isoformat(),
            "range_end": self.range_end.isoformat(),
            "reason_codes": list(self.reason_codes),
            "tag": self.tag,
            "registered": self.registered,
        }


def _replan_range(
    anchor: Anchor,
    affected: list[ScheduleSegmentRow],
    hard: list[Anchor],
    *,
    floor: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime]:
    """要局部重排的那一段：**受影响段 ∪ 新锚点**，再被相邻硬锚点界住。

    局部性是硬要求（设计 §2/§4）：不扩展到整天。所以范围从"与新锚点相交的段"
    出发，而不是从"这一天的空档"出发；相邻硬约定只用来**收窄**范围——已经
    确认的约定、睡前的空档都不归这次重排管。
    """
    start = min(
        [_aware(anchor.start_at), *(_aware(item.start_at) for item in affected)]
    )
    end = max([_aware(anchor.end_at), *(_aware(item.end_at) for item in affected)])
    start = max(start, floor)
    end = min(end, window_end)
    for other in hard:
        if (other.start_at, other.end_at) == (anchor.start_at, anchor.end_at):
            continue
        if other.end_at <= _aware(anchor.start_at):
            start = max(start, other.end_at)
        elif other.start_at >= _aware(anchor.end_at):
            end = min(end, other.start_at)
    return start, end


async def on_new_confirmed_anchor(
    session: AsyncSession,
    plan_day: date,
    anchor: Anchor,
    *,
    actor_id: str | None = None,
    now: datetime | None = None,
) -> ReplanTodo | None:
    """新确认的约定落地后，**同一事务里**即时复核同一计划日（零模型）。

    设计 §2：21:00–24:00 之间用户确认了"D 07:00 的事"，事件处理器要立刻找
    受影响的 D 计划——不然只能等到 00:00 的接缝校验，会丢掉及时修订。
    阶段 1.5 这里**只登记待办**：给受影响的可选段（含会被裁剪的预计睡眠）打上
    ``todo:replan`` 标记并返回 :class:`ReplanTodo`，真正的模型局部重排属阶段 3。

    定位方式不加表：标记写在段自己的 ``source_event_ids`` 里（都是 ``JSON`` 列），
    阶段 3 读 D 的当前段时按标记过滤即可拿到范围。

    - ``anchor`` 是**已确认**的锚点（``load_commitment_anchors`` 的形状）；
      完全落在 D 窗口外的锚点返回 ``None``（它不影响这一天）。
    - 没有计划行、或没有任何会被影响的段 → ``None``：没有局部重排可做
      （21:00 的作业/接缝校验会按当时锚点生成）。
    - **不增 revision、不调模型、不改段状态**：登记待办不是"已经改过计划"。
    """
    moment = _aware(now or utcnow())
    actor = actor_id or _settings_actor_id()
    row = await load_day_plan(session, actor_id=actor, plan_day=plan_day)
    if row is None:
        return None
    window_start = _aware(row.window_start_at)
    window_end = _aware(row.window_end_at)
    clipped = _clip_anchor(anchor, window_start=window_start, window_end=window_end)
    if clipped is None:
        return None

    routine = await _routine_snapshot(session, actor)
    commitment_anchors = await load_commitment_anchors(
        session, actor_id=actor, window_start=window_start, window_end=window_end
    )
    hard, conflicts = resolve_anchor_conflicts(
        [*commitment_anchors, clipped],
        window_start=window_start,
        window_end=window_end,
        routine=routine,
    )
    segments = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == row.id,
                    ScheduleSegmentRow.status == "current",
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars()
    )
    floor = max(moment, window_start)
    affected = [
        segment
        for segment in segments
        if _replan_candidate(segment)
        and _aware(segment.end_at) > floor
        and overlaps(
            _aware(segment.start_at),
            _aware(segment.end_at),
            clipped.start_at,
            clipped.end_at,
        )
    ]
    if not affected:
        return None

    touched = False
    for segment in affected:
        merged = _append_unique(
            segment.source_event_ids,
            REPLAN_TODO_TAG,
            *_anchor_source_ids(clipped),
        )
        if merged == [str(item) for item in (segment.source_event_ids or [])]:
            # 已经登记过：重复调用**不写**（幂等，连 updated_at 都不动）。
            continue
        segment.source_event_ids = merged
        segment.updated_at = moment
        touched = True
    if touched:
        await session.flush()

    start, end = _replan_range(
        clipped, affected, hard, floor=floor, window_end=window_end
    )
    reason_codes = tuple(
        dict.fromkeys(item.reason_code for item in conflicts)
    )
    anchor_ids = tuple(
        dict.fromkeys(
            _anchor_key(item)
            for item in hard
            if (item.start_at, item.end_at) == (clipped.start_at, clipped.end_at)
        )
    )
    return ReplanTodo(
        plan_day=plan_day,
        plan_id=str(row.id),
        anchor_ids=anchor_ids or _anchor_source_ids(clipped),
        segment_ids=tuple(str(item.id) for item in affected),
        range_start=start,
        range_end=end,
        reason_codes=reason_codes,
    )


def _clip_anchor(
    anchor: Anchor, *, window_start: datetime, window_end: datetime
) -> Anchor | None:
    """把锚点裁到 D 内；与 D 没有交集时返回 ``None``。"""
    start = max(_aware(anchor.start_at), _aware(window_start))
    end = min(_aware(anchor.end_at), _aware(window_end))
    if start >= end:
        return None
    if (start, end) == (_aware(anchor.start_at), _aware(anchor.end_at)):
        return anchor
    return replace(anchor, start_at=start, end_at=end)


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


def _settings_actor_id() -> str:
    """角色 actor id 的单一默认值（``LING_ACTOR_ID``，默认 ``actor_ling``）。

    只有"没有显式传 actor_id"的入口才用它：``seam_check`` / ``on_new_confirmed_anchor``
    这类按计划日工作的函数，单角色部署里 actor 就是配置里的那一个。
    """
    from ling.config import get_settings

    try:
        return get_settings().actor_id
    except Exception:  # noqa: BLE001 - 配置不可读时用默认 id
        return "actor_ling"


def _append_unique(existing: object, *values: str) -> list[str]:
    """把值追加到 JSON 列表里（去重、保序），用于 ``source_event_ids``。

    "不写重复原因码"是接缝幂等的判据之一：同一个 ``reason:<code>`` 只留一次。
    """
    items = [str(item) for item in existing] if isinstance(existing, list) else []
    merged = list(items)
    for value in values:
        if value and value not in merged:
            merged.append(value)
    return merged


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
    "MIN_SLEEP_MINUTES",
    "MODEL_KINDS",
    "OUTLINE_DAILY_TOKEN_BUDGET",
    "OUTLINE_DUE_MINUTE",
    "OUTLINE_KIND",
    "OUTLINE_MAX_BLOCKS",
    "OUTLINE_MAX_CALLS",
    "OUTLINE_PROFILE",
    "OUTLINE_TIMEOUT_SECONDS",
    "PERSONA_ESTIMATE_METHOD",
    "PERSONA_TOKEN_LIMIT",
    "PERSONA_UNKNOWN",
    "PHASE_CATCHUP",
    "PHASE_DUE",
    "PHASE_LATE",
    "PLAN_DAY_START_MINUTE",
    "REASON_DEGRADED_LATE",
    "REASON_NEEDS_REVIEW",
    "REASON_ROUTINE_CONFLICT",
    "REASON_SLEEP_BOUNDARY",
    "REPLAN_TODO_TAG",
    "SLEEP_ANCHOR",
    "Anchor",
    "AnchorConflict",
    "OutlineOutcome",
    "OutlineValidationError",
    "PersonaProjection",
    "PlannedBlock",
    "ReplanTodo",
    "RoutineSnapshot",
    "SeamReport",
    "SegmentDraft",
    "StaleRevisionError",
    "build_outline_prompt",
    "build_persona_projection",
    "build_repair_prompt",
    "contains",
    "display_day_for",
    "display_day_window",
    "ensure_day_plan",
    "load_commitment_anchors",
    "load_day_plan",
    "load_segments",
    "local_hhmm",
    "local_minute_instant",
    "on_new_confirmed_anchor",
    "outline_due_at",
    "outline_due_decision",
    "outline_work",
    "overlaps",
    "parse_outline_payload",
    "plan_day_for",
    "plan_day_length",
    "plan_day_window",
    "resolve_anchor_conflicts",
    "resolve_plan_timezone",
    "resolve_zone",
    "rule_outline",
    "run_due_outline",
    "seam_check",
    "sleep_anchors_for_day",
    "validate_outline",
]
