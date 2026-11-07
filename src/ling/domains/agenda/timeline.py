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
``schedule.outline`` / ``schedule.seam`` 两个 outbox handler。本模块只提供可调用
入口，没有定时器/循环。

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
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Visibility
from ling.domains.agenda.intents import (
    IntentError,
    IntentService,
    IntentValidationError,
    IntentView,
    attach_preparation,
    ensure_target_window,
)
from ling.domains.evidence import fact_eligible_experiences
from ling.infra.models import (
    INTENTION_KINDS,
    ActorRoutineRow,
    AgendaPlanRow,
    BehaviorModelCallRow,
    CommitmentRow,
    CoreMemoryRow,
    DayPlanRow,
    MemoryRow,
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
# 准备段"落不下去"：意向够条件但 D 没有够用的空档（设计 §3A 规定的原因码）。
# 只记原因、不挪任何约定；由对话/控制台提示，不在这里取消承诺。
REASON_PREPARATION_UNPLACED = "preparation_unplaced"

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

# ---------------------------------------------------------------- 长程意向区（§3A）
#
# 设计 §3A：每晚取 `[D, D+N)` 的意向，按 confirmed > planned > proposed 的稳定
# 顺序，**每天最多 2 条、总计最多 10 条**进入 prompt；更远/更多的只报数量，
# 不传标题。`N=7` 与"每天 2 条 / 总 10 条"都是**工程初值**，不是研究结论。
FUTURE_INTENT_DAYS = 7
FUTURE_INTENT_PER_DAY = 2
FUTURE_INTENT_LIMIT = 10
FUTURE_INTENT_TITLE_CHARS = 40
# 意向区预算：设计 §3A 给多日展望的输入增量上界就是 **600 token**（10 条短字段
# 的上界，实测 10 条约 510）。记忆区另给 400。两块叠加仍由
# `build_outline_prompt` 的**总量兜底**压到 `OUTLINE_RESERVED_INPUT_TOKENS`
# 以内——预算表不用改，也就不会把"同预算内修复一次"挤掉
# （2 次 × 3500 = 7000 = 当日上限）。
FUTURE_INTENT_TOKEN_LIMIT = 600

# ---------------------------------------------------------------- 记忆区
#
# 四桶：已过事实闸的自我经历 / 核心记忆 / 关系定性摘要 / owner 已授权的结构化
# 摘要。它们都**不是**聊天原文：经历是摘要、核心记忆是 owner 写的短条目、
# 关系只给定性标签、结构化摘要只给 `slot：value`。
MEMORY_TOKEN_LIMIT = 400
MEMORY_MAX_CORE = 6
MEMORY_MAX_RELATIONSHIP = 1
MEMORY_MAX_OWNER_SUMMARY = 4
MEMORY_CORE_ITEM_CHARS = 48
MEMORY_OWNER_ITEM_CHARS = 40
# 记忆区的受众过滤：只让**比窗口更宽**的范围进按 actor 归并的夜间计划。
# - `window_private` / `group_scope`：属于某个窗口/群的受众，进了 actor 级计划
#   就会跟着段标题被带到别的群里（旧段 `visibility=NULL` 对所有人可见）；
# - `admin_only`：永不进普通可读集（与 `kernel.audience.readable_visibilities` 同口径）；
# - **不在白名单里的一律不进**（含未知取值）：过滤用 `IN (...)`，fail closed。
MEMORY_PROMPT_VISIBILITIES: frozenset[str] = frozenset(
    {
        Visibility.actor_private.value,
        Visibility.subject_private.value,
        Visibility.public.value,
    }
)
# 意向区的受众过滤：意向进 prompt 的只是短字段（无正文、无来源引用），
# 所以只排除"永不进普通可读集"的 `admin_only`。
INTENT_PROMPT_VISIBILITIES: frozenset[str] = frozenset(
    {
        Visibility.actor_private.value,
        Visibility.subject_private.value,
        Visibility.window_private.value,
        Visibility.group_scope.value,
        Visibility.public.value,
    }
)

# ---------------------------------------------------------------- 准备段（§3A）
#
# "为某目标做准备"的段必须同时满足：意向 `confirmed`、另有**可信截止依据**
# （`prep_source_ref` 非空，来自承诺/确认事件，**不是**模型自己给的 prep_hint）、
# D 里真有够用的空档。空档太碎（< 30 分钟）不算"有空档"——那是塞进去的碎片。
PREPARATION_MIN_GAP_MINUTES = 30

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

# ---------------------------------------------------------------- 声称与短 ID
#
# 这一小节全是**纯函数**：不碰 DB、不调模型，单测直接打表。

# "完成/承诺"声称：模型把这些词组写进段标题，等于把"可能"说成"已定/已做"。
# 清单是**工程初值**——它只能挡住最常见的措辞，不能证明语义上没在声称；所以
# 引用了意向的段还必须在标题里用"可能/打算"语气，并只许引用输入给出的短 ID。
# 否定形式（"不一定"/"不保证"）不算声称。
_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("答应了", re.compile(r"答应了")),
    ("已经", re.compile(r"已经")),
    ("一定", re.compile(r"(?<!不)一定")),
    ("必须", re.compile(r"(?<!不)必须")),
    ("说好了", re.compile(r"说好了")),
    ("约好了", re.compile(r"约好了")),
    ("已确认", re.compile(r"已确认")),
    ("保证", re.compile(r"(?<!不)保证")),
    ("买好了", re.compile(r"买好了")),
    ("做完了", re.compile(r"做完了")),
    ("完成了", re.compile(r"完成了")),
)
# "可能/打算"语气词：引用 `proposed` / `planned` 的候选时标题必须带上其中一个。
_HEDGE_MARKERS: tuple[str, ...] = (
    "可能",
    "也许",
    "或许",
    "打算",
    "考虑",
    "说不定",
    "没准",
    "大概",
    "想",
)


def find_claim_marker(title: str) -> str | None:
    """标题里"完成/承诺"声称的命中词；没有命中返回 ``None``。"""
    text = " ".join(str(title).split())
    for marker, pattern in _CLAIM_PATTERNS:
        if pattern.search(text):
            return marker
    return None


def has_hedge_marker(title: str) -> bool:
    """标题有没有"可能/打算"这类语气词（引用候选意向时必须带）。"""
    text = " ".join(str(title).split())
    return any(marker in text for marker in _HEDGE_MARKERS)


def intention_short_id(intention_id: str) -> str:
    """内部意向 id → prompt 里的**短 ID**（稳定、可回查、够短）。

    设计 §3A 要求模型只能引用输入里给出的 ID。完整 id（``intent_`` + 24 hex）
    每晚要白吃几百 token；用"按 id 求稳定哈希取 6 位"后，同一个目标在不同夜晚
    编号不变（模型、修复重试、日志都能对上），并且能在同一次输入里回查到原行。
    """
    digest = zlib.crc32(str(intention_id).encode("utf-8")) & 0xFFFFFF
    return f"i{digest:06x}"


def _alias_many(intention_ids: list[str]) -> list[str]:
    """批量分配短 ID；哈希撞了就补后缀（12 条上限下几乎不可能撞）。"""
    taken: set[str] = set()
    aliases: list[str] = []
    for index, value in enumerate(intention_ids, start=1):
        candidate = intention_short_id(value)
        while candidate in taken:
            candidate = f"{intention_short_id(value)}-{index}"
        taken.add(candidate)
        aliases.append(candidate)
    return aliases


_OUTLINE_INSTRUCTIONS = """你是角色「{name}」的日计划器。

现在要为她规划**计划日 {plan_day}**（本地时区 {timezone}）的粗粒度安排。
计划日窗口是本地 {window_local_start} → {window_local_end}；当前本地时间是
{now_local}。可变部分从 {not_before_local} 开始（过去的时间不排）。

硬规则（违反任何一条整份结果作废）：
1. 只输出 JSON 对象，形如
   {{"segments": [{{"kind": "...", "title": "...", "start_at": "ISO 带偏移",
   "end_at": "ISO 带偏移", "place_id": null 或地点 ID, "flexibility": "flexible",
   "intention_id": null 或输入里给出的意向短 ID}}],
   "intention_proposals": [{{"theme": "...", "target_start_day": "YYYY-MM-DD",
   "target_end_day": "YYYY-MM-DD", "source_ref": "输入里的短 ID"}}]}}
   `intention_proposals` 可省略；**不要**输出解释、Markdown 代码块或其它字段。
2. 5–8 个大块为目标；可以更少，但**不能为空**。这些是**意向**，不是已经发生的事。
3. 时间必须是带时区偏移的 ISO-8601，精确到分钟；`end_at` 必须晚于 `start_at`。
4. 不得写过去；不得早于上面说的可变起点；不得跨出计划日窗口。
5. 段与段之间**不得重叠**（可以有空档，空档表示还没决定，不是你随便填的理由）。
6. 不得与下面给出的**不可移动锚点**（睡眠/已确认约定）重叠，也不要写 `sleep`。
7. `kind` 只能取：{kinds}。
8. `place_id` 只能取 {places} 中的一个，或 null（表示不需要具体地点）。
9. 不要写"做了什么/吃到了什么"这类完成事实，也不要写梦境；只写打算。
10. `future_intents` 是**未来的目标**，不是今天的时段：它们不占用今天的钟点，
    也不能因为写在里面就当已定。`proposed`（候选）只能写成"可能/打算/也许"，
    不许写成"已经/答应了/一定/必须"；`confirmed` 也只是"已确认想去某段日期"，
    具体时段仍待定。
11. 引用意向只能在 `intention_id` 里填 `future_intents.items[].id` 给出的**短 ID**；
    不要编造 ID。只有出现在 `future_intents.prep_ready` 里（已确认且有可信准备
    依据）的意向，才允许为她写"为它做准备"的段；其它意向最多影响她的措辞。
12. `intention_proposals` 至多 2 条，只能是她**新的候选**（系统只会把它们记成
    `proposed`）：不得给状态/优先级/可见范围；`source_ref` 必须是输入里给出的
    短 ID（`future_intents.items[].id` 或 `memory.evidenced_experiences[].id`），
    不能写自由文本。
13. `memory` 只是背景（已经证实发生过的经历、核心记忆、相处偏好、owner 明确
    授权的事实）：**不得**把它写成今天的新事情，也不要引用其中的原文。

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
    """校验通过的一个可变块（还是意向，不是活动）。

    ``intention_short_id`` 是模型引用的**输入短 ID**（已校验必须存在于本次输入的
    未来意向区）；``intention_id`` 是回查出来的内部 id。``preparation`` 说明这块
    能不能变成"为它做准备"的段：只有"已确认 + 另有可信截止依据"的意向才有这个
    资格（见 :func:`intent_prep_ready`）。候选（``proposed`` / ``planned``）的引用
    只留审计足迹（``source_event_ids`` 里的 ``intention:<id>``），**不会**变成
    ``schedule_segments.intention_id`` 关联——那个列只给准备段用（设计 §6 的
    CHECK 要求它与 ``preparation_for_day`` 同空或同非空）。
    """

    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None
    flexibility: str
    intention_short_id: str | None = None
    intention_id: str | None = None
    preparation: bool = False


@dataclass(frozen=True)
class IntentProposal:
    """模型**新提议**的一条未来意向（只会落 ``proposed``，设计 §3A）。

    ``upstream_intention_id`` / ``source_ref`` 是**代码**从模型给的短 ID 回查出来的：
    短 ID 指向上游意向时，落库的 ``source_ref`` 用上游**内部 id**（这样
    :meth:`IntentService.revoke` 沿 ``source_ref`` 的递归撤回才接得上）；
    指向记忆条目时用 ``outline:<plan_day>:<短 ID>``（短 ID 本身是哈希，不含正文）。
    """

    kind: str
    theme: str
    target_start_day: date
    target_end_day: date
    source_ref: str
    upstream_intention_id: str | None = None


_ALLOWED_KEYS = {
    "kind",
    "title",
    "start_at",
    "end_at",
    "place_id",
    "flexibility",
    "intention_id",
}
_ALLOWED_PROPOSAL_KEYS = {
    "kind",
    "theme",
    "target_start_day",
    "target_end_day",
    "source_ref",
    # 设计 §3A 的 schema 里 `prep_hint` 是**候选提示**：允许被模型给出，但
    # **永远不**作为 `prep_source_ref`（用户确认目标不顺带确认模型猜的准备事项）。
    "prep_hint",
}
MAX_INTENTION_PROPOSALS = 2
PROPOSAL_THEME_CHARS = 120
PROPOSAL_HINT_CHARS = 60


@dataclass(frozen=True)
class OutlineOutput:
    """模型输出解析后的两半：今天的段 + 未来的候选意向。"""

    segments: list[dict[str, object]]
    proposals: list[dict[str, object]]


def parse_outline_output(raw: str) -> OutlineOutput:
    """严格解析模型输出：只接受 ``{"segments": [...], "intention_proposals": [...]}``。

    ``intention_proposals`` 可缺省（等价于空）；两半都是数组，其它顶层字段一律
    非法。**不做宽容修**：宽容会把"模型没按契约输出"变成"我们猜它的意思"。
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
    extra = set(payload) - {"segments", "intention_proposals"}
    if extra:
        raise OutlineValidationError([f"顶层有未定义字段：{sorted(extra)}"])
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise OutlineValidationError(["segments 必须是数组"])
    proposals = payload.get("intention_proposals", [])
    if not isinstance(proposals, list):
        raise OutlineValidationError(["intention_proposals 必须是数组"])
    return OutlineOutput(segments=segments, proposals=proposals)


def parse_outline_payload(raw: str) -> list[dict[str, object]]:
    """只要 ``segments``（旧名保留：既有调用点与测试按数组用）。"""
    return parse_outline_output(raw).segments


def validate_outline(
    segments: list[object],
    *,
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
    anchors: list[Anchor] | tuple[Anchor, ...],
    valid_place_ids: set[str] | frozenset[str],
    future_intents: FutureIntentZone | None = None,
) -> list[PlannedBlock]:
    """把模型输出校验成可入库的块；任何一条不合就整份拒收。

    拒收的类别（设计 §3 / §8 / §3A）：

    1. 过去时间、越界、互相重叠、压锚点、未知地点、字段/枚举不合；
    2. **把"可能"说成"已定"**：模型段永远不是 confirmed 锚点，所以标题里出现
       "答应了/已经/一定/必须"这类完成或承诺声称就整份拒收（规则兜底的锚点段
       不走本函数，它们才是"来源是 confirmed 锚点"的那一类）；
    3. **引用意向只能用它给出的短 ID**：``intention_id`` 必须出现在本次输入的
       ``future_intents.items[].id`` 里；编造的 ID 与自由文本都拒收；
    4. **引用 ``proposed`` / ``planned`` 的候选时，标题必须带"可能/打算"语气**
       （无语气词就拒收，因为那等于把候选写成了已定）。

    **不"只提交前半段"**：半份时间轴比没有更难解释。
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

    known = {
        item.short_id: item for item in (future_intents.items if future_intents else ())
    }

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
        claim = find_claim_marker(clean_title)
        if claim is not None:
            problems.append(
                f"{label}.title 把意向写成了完成或承诺（出现「{claim}」）："
                "计划段不是已发生的事，也不是对外承诺；请改成打算/可能的语气"
            )
            continue
        flexibility = raw.get("flexibility", "flexible")
        if not isinstance(flexibility, str) or flexibility not in FLEXIBILITIES:
            problems.append(f"{label}.flexibility 必须是 {FLEXIBILITIES}")
            continue
        cited_raw = raw.get("intention_id")
        cited: FutureIntent | None = None
        if cited_raw is not None:
            if not isinstance(cited_raw, str) or not cited_raw.strip():
                problems.append(f"{label}.intention_id 必须是输入给出的意向短 ID")
                continue
            cited = known.get(cited_raw.strip())
            if cited is None:
                problems.append(
                    f"{label}.intention_id 不在本次输入里：{cited_raw!r}"
                    "（只能引用 future_intents.items[].id 给出的短 ID）"
                )
                continue
            if cited.state == "confirmed" and not cited.prep_ready:
                # 已确认的目标被引用，只可能是"给它做准备"——但用户确认目标**不**
                # 顺带确认准备事项（设计 §3A）：还要另有 prep_source_ref，且今天
                # 确实到了准备截止日。两条缺一条都不能写准备段。
                problems.append(
                    f"{label}.intention_id 指向已确认意向「{cited.title}」，但它还没有"
                    "准备资格（需要可信的 prep_source_ref 且到准备截止日）：不能为它写"
                    "准备段；最多用「可能/打算」影响措辞，不要引用 ID"
                )
                continue
            if cited.state != "confirmed" and not has_hedge_marker(clean_title):
                problems.append(
                    f"{label}.title 引用了{_STATE_WORD.get(cited.state, '候选')}意向"
                    f"「{cited.title}」，但没有用「可能/打算」这类语气："
                    "候选不能写成既成事实"
                )
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
                intention_short_id=cited.short_id if cited else None,
                intention_id=cited.intention_id if cited else None,
                preparation=bool(cited and cited.state == "confirmed"),
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


_STATE_WORD: dict[str, str] = {
    "proposed": "候选",
    "planned": "想做的",
    "confirmed": "已确认的",
}


def validate_intention_proposals(
    proposals: list[object],
    *,
    future_intents: FutureIntentZone | None,
    memory: MemoryZone | None,
    plan_day: date,
    today: date,
) -> list[IntentProposal]:
    """校验模型的**新意向提议**（设计 §3A：最多 2 条，只能落 ``proposed``）。

    模型能做的只有"提议一个候选目标"：不能给状态、不能给优先级/可见范围、
    不能引用自由文本来源。``source_ref`` 必须是本次输入里给出的短 ID
    （未来意向的 ``i…`` 或已证实经历的 ``e…``）：那是**可回查的来源**，
    而自由文本来源等于让模型自己给自己背书。

    日期区间复用 :func:`ensure_target_window`（不反向、跨度 ≤90 天、不落过去、
    不超 90 天展望）——与 :meth:`IntentService.create` 是**同一条规则**，
    不在这里另写一套阈值。
    """
    problems: list[str] = []
    if not isinstance(proposals, list):
        raise OutlineValidationError(["intention_proposals 必须是数组"])
    if len(proposals) > MAX_INTENTION_PROPOSALS:
        problems.append(
            f"intention_proposals 有 {len(proposals)} 条，超过上限 "
            f"{MAX_INTENTION_PROPOSALS}"
        )
        raise OutlineValidationError(problems)

    known_intents = {
        item.short_id: item.intention_id
        for item in (future_intents.items if future_intents else ())
    }
    known_memory = set(memory.reference_ids if memory else ())

    accepted: list[IntentProposal] = []
    for index, raw in enumerate(proposals):
        label = f"intention_proposals[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} 不是对象")
            continue
        unknown = set(raw) - _ALLOWED_PROPOSAL_KEYS
        if unknown:
            problems.append(
                f"{label} 有未定义字段：{sorted(unknown)}（模型不能给状态/优先级/受众）"
            )
            continue
        theme = raw.get("theme")
        if not isinstance(theme, str):
            problems.append(f"{label}.theme 必须是字符串")
            continue
        clean_theme = " ".join(theme.split())
        if not clean_theme or len(clean_theme) > PROPOSAL_THEME_CHARS:
            problems.append(f"{label}.theme 长度必须在 1..{PROPOSAL_THEME_CHARS}")
            continue
        claim = find_claim_marker(clean_theme)
        if claim is not None:
            problems.append(
                f"{label}.theme 把候选写成了完成或承诺（出现「{claim}」）："
                "只能是她可能的想法"
            )
            continue
        kind = raw.get("kind", "other")
        if not isinstance(kind, str) or kind not in INTENTION_KINDS:
            problems.append(f"{label}.kind 不在白名单：{kind!r}")
            continue
        source_ref = raw.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.strip():
            problems.append(f"{label}.source_ref 必须是输入里的短 ID")
            continue
        key = source_ref.strip()
        if key not in known_intents and key not in known_memory:
            problems.append(
                f"{label}.source_ref 不是本次输入里的短 ID：{source_ref!r}"
                "（自由文本来源不通过）"
            )
            continue
        try:
            start_day = _parse_iso_day(
                raw.get("target_start_day"), f"{label}.target_start_day"
            )
            end_day = _parse_iso_day(raw.get("target_end_day"), f"{label}.target_end_day")
        except OutlineValidationError as exc:
            problems.extend(exc.problems)
            continue
        try:
            ensure_target_window(start_day=start_day, end_day=end_day, today=today)
        except IntentValidationError as exc:
            problems.append(f"{label} 的目标区间不合法：{exc.message}")
            continue
        hint = raw.get("prep_hint")
        if hint is not None:
            if not isinstance(hint, str):
                problems.append(f"{label}.prep_hint 必须是字符串")
                continue
            if len(hint.strip()) > PROPOSAL_HINT_CHARS:
                problems.append(
                    f"{label}.prep_hint 超过 {PROPOSAL_HINT_CHARS} 字符"
                )
                continue
            # prep_hint 只作为候选提示留痕（不落库、更不会成为 prep_source_ref）：
            # 用户确认目标**不**顺带确认模型猜的准备事项（设计 §3A）。
        upstream = known_intents.get(key)
        accepted.append(
            IntentProposal(
                kind=kind,
                theme=clean_theme,
                target_start_day=start_day,
                target_end_day=end_day,
                source_ref=upstream or f"outline:{plan_day.isoformat()}:{key}",
                upstream_intention_id=upstream,
            )
        )

    if problems:
        raise OutlineValidationError(problems)
    return accepted


def _parse_iso_day(value: object, label: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise OutlineValidationError([f"{label} 必须是 YYYY-MM-DD"])
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise OutlineValidationError([f"{label} 不是合法日期：{exc}"]) from exc


# ---------------------------------------------------------------- 准备段（§3A）


@dataclass(frozen=True)
class PreparationLink:
    """一条"为未来目标做准备"的关联（落库时交给 :func:`attach_preparation`）。"""

    short_id: str
    intention_id: str
    preparation_for_day: date
    block_index: int


def free_gaps(
    anchors: Sequence[Anchor],
    *,
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
) -> list[tuple[datetime, datetime]]:
    """D 里被锚点切出来的空档（只算可变起点之后）。

    用 :func:`_subtract_interval` 的半开区间减法，**不**假设锚点已排序、
    也不假设空档连续：睡眠、承诺、保护窗都可能把一天切得很碎。
    """
    start = max(_aware(not_before), _aware(window_start))
    end = _aware(window_end)
    cuts = [
        (_aware(anchor.start_at), _aware(anchor.end_at))
        for anchor in anchors
        if _aware(anchor.end_at) > start and _aware(anchor.start_at) < end
    ]
    return _subtract_interval(start, end, cuts)


def plan_preparations(
    blocks: Sequence[PlannedBlock],
    *,
    future_intents: FutureIntentZone | None,
    anchors: Sequence[Anchor],
    window_start: datetime,
    window_end: datetime,
    not_before: datetime,
) -> tuple[list[PreparationLink], str | None]:
    """决定哪些段能成为"为未来目标做准备"的段（纯函数，**代码判定**）。

    设计 §3A 的三道门同时成立才允许落准备段：

    1. 意向 ``confirmed`` 且**另有**可信截止依据（``prep_source_ref``）——
       见 :func:`intent_prep_ready`；模型的 ``prep_hint`` 永远不够；
    2. **D 里真有够用的空档**：锚点切出来的空档要 ≥
       :data:`PREPARATION_MIN_GAP_MINUTES`，且这段本身落在某个这样的空档里
       （不重叠只是必要条件，碎片缝不算"有空档"）；
    3. 段本身是可变的模型段——``validate_outline`` 已保证不压锚点、不互相重叠。

    返回 ``(可绑定的关联, 没落下的原因码)``。有空档但这条没落进去、或引用了
    不具备资格的意向时返回 ``preparation_unplaced``；由调用方写到段的
    ``source_event_ids`` 上（设计 §6 的原因码落点），**不**挪任何已有约定。
    """
    if future_intents is None:
        return [], None
    gaps = free_gaps(
        anchors,
        window_start=window_start,
        window_end=window_end,
        not_before=not_before,
    )
    usable = [
        gap for gap in gaps if _minutes(gap[0], gap[1]) >= PREPARATION_MIN_GAP_MINUTES
    ]
    ready = {item.short_id: item for item in future_intents.items if item.prep_ready}
    links: list[PreparationLink] = []
    unplaced = False
    for index, block in enumerate(blocks):
        # 父段与子段带**同一目标**：引用关系挂在父段上，子段（`parent_id` 指向它）
        # 由 :func:`_attach_preparations` 在落库时一起继承。
        if block.intention_id is None or not block.preparation:
            continue
        intent = ready.get(block.intention_short_id or "")
        if intent is None:
            unplaced = True
            continue
        if not any(
            contains(gap[0], gap[1], block.start_at, block.end_at) for gap in usable
        ):
            unplaced = True
            continue
        links.append(
            PreparationLink(
                short_id=intent.short_id,
                intention_id=intent.intention_id,
                preparation_for_day=intent.target_start_day,
                block_index=index,
            )
        )
    return links, (REASON_PREPARATION_UNPLACED if unplaced else None)


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
    # 设计 §3A 的三项可解释计数：进了 prompt 的意向、挂上目标关联的准备段、
    # 落成 ``proposed`` 的新提议。都只是数字，不夹带标题/正文。
    intents_in_prompt: int = 0
    preparations: int = 0
    proposed_intentions: int = 0


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


# ---------------------------------------------------------------- 未来意向输入（§3A）


@dataclass(frozen=True)
class FutureIntent:
    """一条进 prompt / 进校验的未来意向（**短字段**，不含正文与来源引用）。

    ``prep_ready`` 由**代码**判定，不是模型说了算：只有"已确认 + 另有可信截止
    依据（`prep_source_ref`）"的意向才允许生成"为它做准备"的段。设计 §3A：
    确认"目标"不顺带确认模型猜的准备事项。
    """

    short_id: str
    intention_id: str
    kind: str
    title: str
    target_start_day: date
    target_end_day: date
    state: str
    source_kind: str
    priority: int
    prep_ready: bool = False

    def as_dict(self) -> dict[str, object]:
        """给 prompt 的形状：只有设计 §3A 允许的短字段（无可见范围/证据/正文）。"""
        return {
            "id": self.short_id,
            "kind": self.kind,
            "title": self.title,
            "target_start_day": self.target_start_day.isoformat(),
            "target_end_day": self.target_end_day.isoformat(),
            "state": self.state,
            "source_kind": self.source_kind,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class FutureIntentZone:
    """prompt 里的"未来意向区"（有界、可解释；更远的只报数量）。"""

    items: tuple[FutureIntent, ...] = ()
    omitted: int = 0
    horizon_start: date | None = None
    horizon_end: date | None = None
    token_estimate: int = 0
    token_limit: int = FUTURE_INTENT_TOKEN_LIMIT
    truncated: bool = False

    @property
    def prep_ready_ids(self) -> tuple[str, ...]:
        return tuple(item.short_id for item in self.items if item.prep_ready)

    def as_dict(self) -> dict[str, object]:
        window: dict[str, object] = {}
        if self.horizon_start is not None and self.horizon_end is not None:
            window = {
                "from": self.horizon_start.isoformat(),
                "to": self.horizon_end.isoformat(),
            }
        return {
            "window": window,
            "items": [item.as_dict() for item in self.items],
            # 准备段的白名单单独给：`items[].*` 只放短字段，不往里塞布尔值。
            "prep_ready": list(self.prep_ready_ids),
            "omitted": int(self.omitted),
        }


def intent_prep_ready(view: IntentView, *, plan_day: date) -> bool:
    """能不能为这条意向生成"准备"段（设计 §3A 的准备门，**代码判定**）。

    三条同时成立才行：

    1. ``state == "confirmed"``——`proposed` / `planned` 只是候选，不生专属准备段；
    2. ``prep_source_ref`` 非空且 ``prep_due_day`` 有值——用户确认"目标"**不会**
       顺带确认模型猜的准备事项，必须另有承诺/确认事件给出的截止依据；
    3. ``prep_due_day <= plan_day``——**今天确实到了该准备的时候**。提前十几天
       就开始"为它做准备"既没有依据，也会把每天都塞满同一个目标。
    """
    if view.state != "confirmed":
        return False
    if not view.prep_source_ref or view.prep_due_day is None:
        return False
    if view.prep_due_day > plan_day:
        return False
    # 目标必须还在未来：准备段永远不落在目标开始当天或之后。
    return view.target_start_day > plan_day


def fit_future_intents(
    items: list[FutureIntent],
    *,
    omitted: int = 0,
    horizon_start: date | None = None,
    horizon_end: date | None = None,
    token_limit: int = FUTURE_INTENT_TOKEN_LIMIT,
) -> FutureIntentZone:
    """按**固定优先级**把意向压进 token 上限（纯函数，单测可直接打表）。

    优先级由 :meth:`IntentService.list_active` 的排序给出（confirmed > planned >
    proposed，同状态里 priority 高的先、目标日早的先）。超预算时**从尾部整条删**，
    不把每条都截成半句——半句标题比没有标题更容易被模型误读。
    """

    def cost(values: list[FutureIntent]) -> int:
        return _estimate_json_tokens([item.as_dict() for item in values])

    kept = list(items)
    truncated = False
    while kept and cost(kept) > token_limit:
        kept.pop()
        truncated = True
    return FutureIntentZone(
        items=tuple(kept),
        omitted=int(omitted) + (len(items) - len(kept)),
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        token_estimate=cost(kept),
        token_limit=token_limit,
        truncated=truncated,
    )


async def load_future_intents(
    session: AsyncSession,
    *,
    actor_id: str,
    plan_day: date,
    now: datetime,
) -> FutureIntentZone:
    """夜间读：未来意向区的**唯一**装配入口（零模型）。

    设计 §3A：「每日 21:00 取 ``[D, D+7]`` 内的可见候选，按 confirmed > planned >
    proposed 的稳定顺序取最多 2 条/未来日、总计最多 10 条进入 prompt；更早/更远
    目标以一条"还有 N 条"概括，不传私密标题」。

    窗口包含 D 自己（"与 D 相交"），因为今天就是目标日的意向同样会影响今天的
    措辞；每天 2 条、总 10 条的上限把总量钉死，不随窗口多一天膨胀。"每天"按
    ``max(target_start_day, D)`` 归桶：跨多日的意向只占**开始那天**的名额，
    所以一条持续一周的目标不会挤掉整周的其它候选。

    过期清理**不在这里**做：它是作业级的前置步骤，由 :func:`outline_work` 在
    进 prompt 之前调 :meth:`IntentService.expire_due`（零模型、纯状态机）完成。
    本函数只负责"读当前有效的行、做有界截断"。
    """
    service = IntentService(session, actor_id=actor_id)
    horizon_end = plan_day + timedelta(days=FUTURE_INTENT_DAYS)
    views = await service.list_active(start_day=plan_day, end_day=horizon_end)

    eligible = [
        view for view in views if str(view.visibility) in INTENT_PROMPT_VISIBILITIES
    ]
    # 受众过滤掉的行**不**计入 "还有 N 条"：那是"她看不到内容的私有目标"，
    # 连条数都不该被拿去猜（与时间轴只报 `has_private_content` 同口径）。
    aliases = _alias_many([view.intention_id for view in eligible])

    per_day: dict[date, int] = {}
    chosen: list[FutureIntent] = []
    for alias, view in zip(aliases, eligible, strict=True):
        if len(chosen) >= FUTURE_INTENT_LIMIT:
            break
        bucket = max(view.target_start_day, plan_day)
        if per_day.get(bucket, 0) >= FUTURE_INTENT_PER_DAY:
            continue
        per_day[bucket] = per_day.get(bucket, 0) + 1
        chosen.append(
            FutureIntent(
                short_id=alias,
                intention_id=view.intention_id,
                kind=view.kind,
                title=_short(view.title, FUTURE_INTENT_TITLE_CHARS),
                target_start_day=view.target_start_day,
                target_end_day=view.target_end_day,
                state=view.state,
                source_kind=view.source_kind,
                priority=view.priority,
                prep_ready=intent_prep_ready(view, plan_day=plan_day),
            )
        )
    omitted = len(eligible) - len(chosen)
    return fit_future_intents(
        chosen,
        omitted=omitted,
        horizon_start=plan_day,
        horizon_end=horizon_end,
    )


# ---------------------------------------------------------------- 记忆区


@dataclass(frozen=True)
class MemoryItem:
    """记忆区的一条（带短 ID，方便模型在 `intention_proposals.source_ref` 里引用）。"""

    short_id: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"id": self.short_id, "text": self.text}


@dataclass(frozen=True)
class MemoryZone:
    """夜间 prompt 的**记忆区**：有界、按受众过滤、按固定优先级压缩。

    四桶的优先级（预算不够时先删后面的）：

    1. ``evidenced_experiences``——已过事实闸的自我经历，解释"今天从哪儿接着过"；
    2. ``relationship_notes``——只给定性标签（熟悉度/边界例子），不带他人标识；
    3. ``core_memories``——owner 写的短条目；与人设投影高度重叠，预算紧时让位；
    4. ``owner_summaries``——owner 明确授权且未过期的结构化事实（``slot：value``）。

    ``truncated`` 表示**有内容因为预算没进来**（不是"本来就没有"）。
    """

    evidenced_experiences: tuple[MemoryItem, ...] = ()
    relationship_notes: tuple[MemoryItem, ...] = ()
    core_memories: tuple[MemoryItem, ...] = ()
    owner_summaries: tuple[MemoryItem, ...] = ()
    token_estimate: int = 0
    token_limit: int = MEMORY_TOKEN_LIMIT
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "evidenced_experiences": [item.as_dict() for item in self.evidenced_experiences],
            "relationship_notes": [item.as_dict() for item in self.relationship_notes],
            "core_memories": [item.as_dict() for item in self.core_memories],
            "owner_summaries": [item.as_dict() for item in self.owner_summaries],
            "truncated": self.truncated,
        }

    @property
    def reference_ids(self) -> tuple[str, ...]:
        """模型可以在 `intention_proposals.source_ref` 里引用的短 ID。"""
        return tuple(item.short_id for item in self.evidenced_experiences)


def fit_memory_zone(
    *,
    experiences: list[str],
    relationship_notes: list[str],
    core_memories: list[str],
    owner_summaries: list[str],
    token_limit: int = MEMORY_TOKEN_LIMIT,
) -> MemoryZone:
    """按上面的固定优先级把四桶装进 token 上限（纯函数，单测可直接打表）。

    超预算时**整条不装**而不是截半句：记忆区是背景，半句背景比没有更容易被
    模型编成事实。
    """
    buckets: dict[str, list[MemoryItem]] = {
        "evidenced_experiences": [],
        "relationship_notes": [],
        "core_memories": [],
        "owner_summaries": [],
    }
    prefixes = {
        "evidenced_experiences": "e",
        "relationship_notes": "r",
        "core_memories": "c",
        "owner_summaries": "s",
    }
    sources = {
        "evidenced_experiences": (experiences, MAX_EXPERIENCE_SUMMARIES),
        "relationship_notes": (relationship_notes, MEMORY_MAX_RELATIONSHIP),
        "core_memories": (core_memories, MEMORY_MAX_CORE),
        "owner_summaries": (owner_summaries, MEMORY_MAX_OWNER_SUMMARY),
    }

    def payload() -> dict[str, list[dict[str, object]]]:
        return {key: [item.as_dict() for item in value] for key, value in buckets.items()}

    def cost() -> int:
        return _estimate_json_tokens(payload())

    overflowed = False
    truncated = False
    for key, (values, cap) in sources.items():
        for index, text in enumerate([str(item) for item in values[:cap]], start=1):
            if overflowed:
                truncated = True
                break
            buckets[key].append(MemoryItem(short_id=f"{prefixes[key]}{index}", text=text))
            if cost() > token_limit:
                buckets[key].pop()
                overflowed = True
                truncated = True
    return MemoryZone(
        evidenced_experiences=tuple(buckets["evidenced_experiences"]),
        relationship_notes=tuple(buckets["relationship_notes"]),
        core_memories=tuple(buckets["core_memories"]),
        owner_summaries=tuple(buckets["owner_summaries"]),
        token_estimate=cost(),
        token_limit=token_limit,
        truncated=truncated,
    )


async def _owner_core_memories(session: AsyncSession) -> list[str]:
    """owner 的核心记忆（短条目）：只取属于 owner subject 的活跃行。

    `core_memories` 由 owner 显式维护、没有 `visibility` 列（只有 `scope`），
    所以本区的门槛是"属于 owner + active"；它的条数上限是 owner 自己定的。
    """
    from ling.settings.user_preferences import owner_subject_id

    owner = await owner_subject_id(session)
    if not owner:
        return []
    rows = (
        await session.execute(
            select(CoreMemoryRow)
            .where(
                CoreMemoryRow.subject_id == owner,
                CoreMemoryRow.active.is_(True),
            )
            .order_by(CoreMemoryRow.priority.asc(), CoreMemoryRow.label)
            .limit(MEMORY_MAX_CORE)
        )
    ).scalars()
    return [
        _short(f"{row.label}：{row.text}", MEMORY_CORE_ITEM_CHARS)
        for row in rows
        if str(row.text or "").strip()
    ]


async def _owner_structured_summaries(
    session: AsyncSession, *, now: datetime
) -> list[str]:
    """owner 已授权、未过期的**结构化**摘要（`memories` 的 `slot：value` 对）。

    条件都是读侧可判定的：属于 owner、`kind=assertion`、未撤回、未归档、没被
    取代、`valid_until` 未过、可见范围允许进夜间计划。只给 `slot：value`，
    **不给整段 `text`**——结构化摘要的意义就是"短、可核对"。
    """
    from ling.settings.user_preferences import owner_subject_id

    owner = await owner_subject_id(session)
    if not owner:
        return []
    rows = (
        await session.execute(
            select(MemoryRow)
            .where(
                MemoryRow.subject_id == owner,
                MemoryRow.kind == "assertion",
                MemoryRow.retracted.is_(False),
                MemoryRow.archived_at.is_(None),
                MemoryRow.superseded_by.is_(None),
                MemoryRow.slot.is_not(None),
                MemoryRow.value.is_not(None),
                MemoryRow.visibility.in_(sorted(MEMORY_PROMPT_VISIBILITIES)),
                or_(MemoryRow.valid_until.is_(None), MemoryRow.valid_until > now),
            )
            .order_by(MemoryRow.confidence.desc(), MemoryRow.updated_at.desc())
            .limit(MEMORY_MAX_OWNER_SUMMARY * 3)
        )
    ).scalars()
    summaries: list[str] = []
    for row in rows:
        slot = _short(str(row.slot or ""), 24)
        value = _short(str(row.value or ""), MEMORY_OWNER_ITEM_CHARS)
        if slot and value:
            summaries.append(f"{slot}：{value}")
        if len(summaries) >= MEMORY_MAX_OWNER_SUMMARY:
            break
    return summaries


async def build_memory_zone(
    session: AsyncSession,
    *,
    actor_id: str,
    now: datetime,
) -> MemoryZone:
    """夜间读：记忆区的**唯一**装配入口（零模型，四桶 + 受众过滤 + 上限）。

    自我经历走 :func:`_experience_summary`（已过事实闸）；关系摘要复用
    :func:`_persona_relationships`（只给定性标签，不带他人标识）。
    """
    experiences = await _experience_summary(session, actor_id=actor_id, now=now)
    relationship_notes = await _persona_relationships(session, actor_id)
    core_memories = await _owner_core_memories(session)
    owner_summaries = await _owner_structured_summaries(session, now=now)
    return fit_memory_zone(
        experiences=experiences,
        relationship_notes=relationship_notes,
        core_memories=core_memories,
        owner_summaries=owner_summaries,
    )


def _estimate_json_tokens(payload: object) -> int:
    """粗略 token 估算：与预算裁剪同一把尺子（见 ``PERSONA_ESTIMATE_METHOD``）。"""
    return _estimate_text_tokens(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- 预算与租约


@dataclass(frozen=True)
class _Reservation:
    call_id: str
    attempt: int
    provider_id: str | None = None


async def _day_call_totals(
    session: AsyncSession, *, actor_id: str, plan_day: date, kind: str = OUTLINE_KIND
) -> tuple[int, int]:
    """当日该 actor 的 ``kind``（默认大纲）调用次数与已预留/已用 token 上界。

    细化（``refine``）与大纲**各算各的账**：设计 §0 的预算表把它们列成两行，
    共用一张 ``behavior_model_calls`` 但按 ``kind`` 分开计。
    """
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
                BehaviorModelCallRow.kind == kind,
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
    kind: str = OUTLINE_KIND,
    max_calls: int = OUTLINE_MAX_CALLS,
    daily_token_budget: int = OUTLINE_DAILY_TOKEN_BUDGET,
    reserved_input_tokens: int = OUTLINE_RESERVED_INPUT_TOKENS,
    reserved_output_tokens: int = OUTLINE_RESERVED_OUTPUT_TOKENS,
    lease_seconds: float = OUTLINE_LEASE_SECONDS,
    attempt: int | None = None,
) -> _Reservation | None:
    """预约一次行为模型调用（预算闸 + lease），拿不到返回 ``None``。

    默认参数就是**大纲**的口径（既有调用点行为不变）；细化传自己的
    ``kind``/上限/预算/租约——设计 §0 把它们列成两张额度。

    并发安全三层（设计 §3）：``UNIQUE(actor,kind,key,attempt)`` 让同一 attempt
    只有一个赢家；``lease_until`` 让过期抢占只能开新 attempt；预算表落库，
    重启后旧预留仍计入当日上限，避免重复烧钱。
    """
    calls, budget = await _day_call_totals(
        session, actor_id=actor_id, plan_day=plan_day, kind=kind
    )
    if calls >= max_calls:
        return None
    reserved = reserved_input_tokens + reserved_output_tokens
    if budget + reserved > daily_token_budget:
        return None
    if attempt is None:
        attempt = calls + 1
    call_id = new_id("bcall")
    try:
        async with session.begin_nested():
            session.add(
                BehaviorModelCallRow(
                    id=call_id,
                    actor_id=actor_id,
                    plan_day=plan_day,
                    kind=kind,
                    window_id="",
                    idempotency_key=idempotency_key,
                    attempt=attempt,
                    state="reserved",
                    reserved_input_tokens=reserved_input_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                    lease_owner=lease_owner,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    # 显式写调用时刻：滚动额度（细化每 3 小时一次）要按**这次**
                    # 调用的时间算，不能用"行被 flush 的那一瞬"（测试固定时钟时
                    # 两者会不同）。生产里 `now` 就是真实时钟，行为不变。
                    created_at=now,
                    updated_at=now,
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
    intents: FutureIntentZone | None = None,
    memory: MemoryZone | None = None,
) -> str:
    """把大纲输入快照拼成提示词。

    输入契约按设计 §3：``plan_day / timezone / now_utc / routine /
    immutable_anchors / persona_summary / memory / current_energy_satiety /
    valid_places``，外加设计 §3A 的 ``future_intents``。**不塞聊天原文、不塞梦、
    不塞计划正文**。

    ``intents`` / ``memory`` 缺省时退化成本模块早期的形状（只有经历摘要、
    没有未来意向）——那是为了不打破直接调用本函数的旧入口；调度路径
    :func:`ensure_day_plan` 两个都会给。``memory`` 给了以后它**已经**包含经历
    摘要（见 :func:`build_memory_zone`），``experiences`` 参数不再重复注入，
    否则同一份经历在 prompt 里会出现两次。两块各自有 token 上限（见
    :data:`FUTURE_INTENT_TOKEN_LIMIT` / :data:`MEMORY_TOKEN_LIMIT`），
    压缩**发生在装配时**，不在拼字符串时——超预算的内容根本不会出现在这里。
    """
    zone = memory or fit_memory_zone(
        experiences=list(experiences),
        relationship_notes=[],
        core_memories=[],
        owner_summaries=[],
    )
    intent_zone = intents or FutureIntentZone()

    def render(intent_view: FutureIntentZone, memory_view: MemoryZone) -> str:
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
            "memory": memory_view.as_dict(),
            "future_intents": intent_view.as_dict(),
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

    prompt = render(intent_zone, zone)
    # 总输入预算是**硬**上限：两块各自没超，也可能和锚点/人设/地点叠加后超过
    # `OUTLINE_RESERVED_INPUT_TOKENS`（预算行按它预留，超了就等于拿"应该够"当
    # 事实）。超了就对半压这两块——先砍低优先级内容，绝不砍锚点与人设。
    for _round in range(4):
        if _estimate_text_tokens(prompt) <= OUTLINE_RESERVED_INPUT_TOKENS:
            break
        intent_zone = fit_future_intents(
            list(intent_zone.items),
            omitted=intent_zone.omitted,
            horizon_start=intent_zone.horizon_start,
            horizon_end=intent_zone.horizon_end,
            token_limit=int(intent_zone.token_limit) // 2,
        )
        zone = _refit_memory_zone(zone, token_limit=int(zone.token_limit) // 2)
        prompt = render(intent_zone, zone)
    return prompt


def _refit_memory_zone(memory: MemoryZone, *, token_limit: int) -> MemoryZone:
    """用**已有**条目重装一遍记忆区（给总量兜底用：短 ID 按原顺序重排即可）。"""
    return fit_memory_zone(
        experiences=[item.text for item in memory.evidenced_experiences],
        relationship_notes=[item.text for item in memory.relationship_notes],
        core_memories=[item.text for item in memory.core_memories],
        owner_summaries=[item.text for item in memory.owner_summaries],
        token_limit=max(0, token_limit),
    )


def _estimate_text_tokens(text: str) -> int:
    """粗略 token 估算（与预算裁剪同一把尺子；见 ``PERSONA_ESTIMATE_METHOD``）。"""
    from ling.kernel.context import estimate_tokens

    return estimate_tokens(text)


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
    """待落库的一段（校验/规则大纲共同的中间形态）。

    ``intention_id`` 只给"为未来目标做准备"的段落值：落库后由
    :func:`ling.domains.agenda.intents.attach_preparation` 补
    ``preparation_for_day`` 与可见范围（两个列由 CHECK 绑成同空/同非空，
    所以这里**不**先写一半）。
    """

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
    intention_id: str | None = None
    preparation_for_day: date | None = None
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
) -> tuple[int, int, list[tuple[ScheduleSegmentRow, SegmentDraft]]]:
    """CAS 提交一版：段先 supersede 未来段，再插新段，最后抬 ``revision``。

    CAS 失败（另一个 worker 已提交）抛 ``StaleRevisionError``，整个嵌套事务
    回滚——**不会**留下半版时间轴。

    返回 ``(新 revision, 实际插入段数, [(新段行, 对应草稿)])``：调用方要拿
    "段 id ↔ 草稿" 的对应去挂准备段关联（``intention_id`` 由
    :func:`~ling.domains.agenda.intents.attach_preparation` 补，不在这里直写）。
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
        created: list[tuple[ScheduleSegmentRow, SegmentDraft]] = []
        for draft in drafts:
            segment = ScheduleSegmentRow(
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
            session.add(segment)
            created.append((segment, draft))
        await session.flush()
    await session.refresh(row)
    return new_revision, len(drafts), created


class StaleRevisionError(RuntimeError):
    """提交时 ``revision`` 已被别人推进（CAS 失败）。"""


# ---------------------------------------------------------------- 准备段落库


def _prep_source_visibilities() -> tuple[str, ...]:
    """准备段的第三个可见性来源：``prep_source_ref`` 指向的承诺/确认事件。

    **按最严处理**（``actor_private``）。理由不是偷懒：设计 §6 没给
    ``commitments`` / 确认事件加可见范围列，所以“来源可见性”在这里**不可解析**；
    而 :func:`strictest_visibility` 的原则是"不知道就不能放宽"。宁可让
    "为周五的旅行做准备"只对 owner 可见，也不因为意向被标成 ``public`` 就把
    她的准备事项带到别人眼前。来源表以后有了可见范围列，改这一处即可。
    """
    return (Visibility.actor_private.value,)


async def _mark_preparation_unplaced(
    session: AsyncSession,
    segment: ScheduleSegmentRow,
    *,
    now: datetime,
    reason_code: str,
) -> None:
    """准备段挂不上时**只记原因码**，不失败整份大纲、也不挪任何约定。

    原因码落在段的 ``source_event_ids``（设计 §6 的两处落点之一），与
    `seam_check` / `on_new_confirmed_anchor` 同一格式。
    """
    segment.source_event_ids = _append_unique(
        segment.source_event_ids,
        f"{REASON_TAG_PREFIX}{REASON_PREPARATION_UNPLACED}",
        f"{REASON_TAG_PREFIX}{reason_code}",
    )
    segment.updated_at = now
    await session.flush()


async def _attach_preparations(
    session: AsyncSession,
    *,
    actor_id: str,
    created: list[tuple[ScheduleSegmentRow, SegmentDraft]],
    now: datetime,
) -> int:
    """把"为未来目标做准备"的段关联到意向（设计 §3A / §6）。

    落库走 :func:`~ling.domains.agenda.intents.attach_preparation`——**只**它写
    ``intention_id`` / ``preparation_for_day`` / 最严可见范围，并留下
    ``intention:<id>`` 与确认来源的审计足迹。这里不另写一套校验。

    **父子段带同一目标**：子段（``parent_id`` 指向刚挂上的准备段）继承同一目标，
    否则撤回父段后子段会留在时间轴上指着一个已撤回的目标。当前大纲只产出根段，
    这个继承是给"局部重排"（阶段 3）准备的，逻辑一致、不留半棵树。

    失败（例如意向在校验后被撤回）**不**让整份大纲失败：段保留为普通可选块，
    原因码 ``preparation_unplaced`` 记在段上。
    """
    pending: dict[str, tuple[str, date]] = {}
    for segment, draft in created:
        if draft.intention_id and draft.preparation_for_day is not None:
            pending[str(segment.id)] = (draft.intention_id, draft.preparation_for_day)
    if not pending:
        return 0

    attached: dict[str, str] = {}
    for segment, _draft in created:
        key = str(segment.id)
        if key not in pending:
            continue
        intention_id, preparation_for_day = pending[key]
        try:
            await attach_preparation(
                session,
                intention_id=intention_id,
                segment_id=key,
                actor_id=actor_id,
                preparation_for_day=preparation_for_day,
                source_visibilities=_prep_source_visibilities(),
                now=now,
            )
        except IntentError as exc:
            logger.warning(
                "schedule.outline.preparation_unplaced actor=%s segment=%s reason=%s",
                actor_id,
                key,
                exc.reason_code,
            )
            await _mark_preparation_unplaced(
                session, segment, now=now, reason_code=exc.reason_code
            )
            continue
        attached[key] = intention_id

    # 子段继承父段的目标关联（同一棵子树指向同一个目标）。
    for segment, draft in created:
        key = str(segment.id)
        parent_id = str(segment.parent_id or "")
        if key in attached or draft.intention_id is not None or not parent_id:
            continue
        intention_id = attached.get(parent_id)
        if intention_id is None:
            continue
        try:
            await attach_preparation(
                session,
                intention_id=intention_id,
                segment_id=key,
                actor_id=actor_id,
                source_visibilities=_prep_source_visibilities(),
                now=now,
            )
        except IntentError as exc:
            logger.warning(
                "schedule.outline.preparation_child_unplaced actor=%s segment=%s reason=%s",
                actor_id,
                key,
                exc.reason_code,
            )
            await _mark_preparation_unplaced(
                session, segment, now=now, reason_code=exc.reason_code
            )
            continue
        attached[key] = intention_id
    return len(attached)


async def _create_intention_proposals(
    session: AsyncSession,
    *,
    actor_id: str,
    proposals: Sequence[IntentProposal],
    plan_day: date,
    now: datetime,
) -> int:
    """把模型提议落成 ``proposed``（**唯一**能落的状态），失败只记日志。

    设计 §3A：夜间 D 大纲可顺带提议至多 2 条候选；代码按同 actor + 来源 + 目标
    区间去重（``IntentService.create`` 的唯一键），**只**写 ``proposed``——
    ``source_kind="model"`` 在服务层就已经堵死了 ``planned``。

    满额（``intention_capacity``）是**预期结果**：拒收新提议、不淘汰任何已确认的
    来源，也**不**因此把今天的大纲降级（设计 §3A「不得挤掉用户确认的承诺」）。

    返回值是"被服务层**接受**的提议条数"：同来源 + 同区间的重放会拿到原行
    （幂等），也计入接受——调用方关心的是"这次输入有几条被受理"，不是新插了几行。
    """
    if not proposals:
        return 0
    service = IntentService(session, actor_id=actor_id)
    created = 0
    for proposal in proposals:
        try:
            await service.create(
                kind=proposal.kind,
                title=proposal.theme,
                target_start_day=proposal.target_start_day,
                target_end_day=proposal.target_end_day,
                source_kind="model",
                source_ref=proposal.source_ref,
                state="proposed",
                now=now,
            )
        except IntentError as exc:
            logger.info(
                "schedule.outline.proposal_rejected actor=%s plan_day=%s reason=%s",
                actor_id,
                plan_day.isoformat(),
                exc.reason_code,
            )
            continue
        created += 1
    return created


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

    **意向的过期清理不在这里**：它属于作业级前置步骤，由 :func:`outline_work`
    在装配输入之前完成（设计 §3A）。直接调本函数不会顺手改意向状态。
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
    # 设计 §3A 的两块夜间输入：未来意向（短字段、每天 2 条/总 10 条）与记忆
    # （经历/核心记忆/关系/owner 授权摘要）。两块都在装配时压到 token 上限，
    # 过期清理由作业入口 `outline_work` 先做（零模型）。
    intents = await load_future_intents(
        session, actor_id=actor_id, plan_day=plan_day, now=now
    )
    memory = await build_memory_zone(session, actor_id=actor_id, now=now)

    idempotency_key = f"outline:{actor_id}:{plan_day.isoformat()}:{timezone_revision}"
    owner = lease_owner or f"timeline:{new_id('lease')}"

    drafts: list[SegmentDraft] = []
    state = "degraded"
    source = "rule"
    reason_code: str | None = None
    calls = 0
    model_provider: str | None = None
    proposals: list[IntentProposal] = []

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
        intents=intents,
        memory=memory,
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
            output = parse_outline_output(raw)
            blocks = validate_outline(
                output.segments,
                window_start=window_start,
                window_end=window_end,
                not_before=not_before,
                anchors=anchors,
                valid_place_ids=place_ids,
                future_intents=intents,
            )
            proposals = validate_intention_proposals(
                output.proposals,
                future_intents=intents,
                memory=memory,
                plan_day=plan_day,
                today=plan_day_for(now, timezone),
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
            prep_links, unplaced_reason = plan_preparations(
                blocks,
                future_intents=intents,
                anchors=anchors,
                window_start=window_start,
                window_end=window_end,
                not_before=not_before,
            )
            by_index = {link.block_index: link for link in prep_links}
            drafts = []
            for index, block in enumerate(blocks):
                link = by_index.get(index)
                # 引用了意向的段留下 `intention:<id>` 审计足迹（不带标题）：
                # 准备段还会由 `attach_preparation` 追加确认来源。
                source_event_ids: list[str] = []
                if block.intention_id:
                    source_event_ids.append(f"intention:{block.intention_id}")
                if link is None and block.preparation and unplaced_reason:
                    # 本来要当准备段却没落成（没空档/资格在调用间变了）：只记原因码，
                    # 段本身仍是普通可选块，不冒充"今天要准备"。
                    source_event_ids.append(f"{REASON_TAG_PREFIX}{unplaced_reason}")
                drafts.append(
                    SegmentDraft(
                        kind=block.kind,
                        title=block.title,
                        start_at=block.start_at,
                        end_at=block.end_at,
                        place_id=block.place_id,
                        source="model",
                        intention_id=link.intention_id if link else None,
                        preparation_for_day=link.preparation_for_day if link else None,
                        source_event_ids=source_event_ids,
                    )
                )
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
        new_revision, inserted, created = await _commit_revision(
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
    # 设计 §3A：段的准备关联与"模型新提议的意向"都在**同一事务**里落库。
    # 两者的失败都**不**推翻已经提交的这一版时间轴——原因码/日志留痕即可。
    preparations = await _attach_preparations(
        session, actor_id=actor_id, created=created, now=now
    )
    proposed = await _create_intention_proposals(
        session,
        actor_id=actor_id,
        proposals=proposals,
        plan_day=plan_day,
        now=now,
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
        intents_in_prompt=len(intents.items),
        preparations=preparations,
        proposed_intentions=proposed,
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

    进 prompt 之前先做**确定性惰性过期**（:meth:`IntentService.expire_due`，
    零模型、纯时间比较）：目标日已过或提议超过 TTL 的意向不该再影响今天，
    设计 §3A 明确"每次夜间读与事件处理先过期，不另起模型清扫任务"。
    放在相位判断**之前**，``late`` 也就顺手把活跃集清干净（仍然零模型）。
    """
    moment = _aware(now or utcnow())
    if phase not in _PHASE_SEVERITY:
        raise ValueError(f"未知的大纲相位：{phase!r}（只能是 due/catchup/late）")
    if timezone is None:
        timezone, timezone_source = await resolve_plan_timezone(session)
    else:
        timezone_source = timezone_source or "explicit"
    routine = await _routine_snapshot(session, actor_id)
    await IntentService(session, actor_id=actor_id).expire_due(now=moment)
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


# ---------------------------------------------------------------- 近窗细化（阶段 2）
#
# 设计 §3：大块是"打算做什么"，小时/分钟是"若无新事，可能怎样做"。全天分钟级
# 既烧钱又会被现实频繁推翻，所以只在**近窗**细化：距最早未细化段开始不足
# `REFINE_TRIGGER_MINUTES`、且未来 `REFINE_LOOKAHEAD_HOURS` 小时里确有粗段时才跑。
#
# 下面每个数字都是**工程初值**（设计 §0 的预算表），不是研究结论。
# 每个父段的子段数、子段最短时长、递归层级、每 3 小时滚动次数都有**硬上限**；
# 上限在代码里判，不交给模型自律。

REFINE_KIND = "refine"
# 与大纲共用 `memory.extract` 职责：不新开 `planning` 槽位（设计 §0）。
REFINE_PROFILE = OUTLINE_PROFILE

# 触发与窗口：距最早未细化段开始 ≤ 60 分钟才值得跑；只看未来 3 小时。
REFINE_TRIGGER_MINUTES = 60
REFINE_LOOKAHEAD_HOURS = 3
# 滚动额度：每 3 小时最多 1 次细化调用（独立于"每计划日 4 次"的总量上限）。
REFINE_ROLLING_HOURS = 3
REFINE_ROLLING_MAX_CALLS = 1
# 每计划日上限：设计 §0 的 4 次 / 7,200 token。
REFINE_MAX_CALLS_PER_DAY = 4
REFINE_RESERVED_INPUT_TOKENS = 1000
REFINE_RESERVED_OUTPUT_TOKENS = 800
REFINE_DAILY_TOKEN_BUDGET = 7200
# 超时沿用 `OUTLINE_TIMEOUT_SECONDS`（60 秒）：同一个职责、同一个上限，
# 不另设一个"细化超时"制造第二套口径；租约比它长，留出提交与回滚的时间。
REFINE_LEASE_SECONDS = 120.0

# 单个父段的硬上限：一次最多 4 个子段，每段至少 5 分钟（"5–15 分钟叶段"的下界），
# 最长不超过 3 小时。递归层级也在这里封死：block → hour → minute，
# minute 不再细化（`REFINE_LAYER_STEPS` 里没有它）。
REFINE_MAX_CHILDREN = 4
REFINE_MIN_CHILD_MINUTES = 5
REFINE_MAX_CHILD_MINUTES = 180
REFINE_CHILD_TITLE_CHARS = 40
REFINE_LAYER_STEPS: dict[str, str] = {"block": "hour", "hour": "minute"}
REFINE_COARSE_LAYERS: tuple[str, ...] = ("block", "hour")

# 同一窗口只尝试**一次**：失败/空结果当次不重试（设计 §3："CAS 失败丢弃旧响应，
# 未来窗口再试"）。所以同一幂等键最多一行预算记录，下一轮扫描看到它就跳过。
REFINE_MAX_ATTEMPTS_PER_WINDOW = 1
# 剩余窗口太碎（< 30 分钟）就不细化：为塞碎片而调模型是把成本换成噪声。
REFINE_MIN_WINDOW_MINUTES = 30

REASON_REFINE_INVALID = "refine_invalid"
REASON_REFINE_EMPTY = "refine_empty"
REASON_REFINE_STALE = "stale_revision"
REASON_REFINE_ATTEMPTED = "window_attempted"
REASON_REFINE_ROLLING = "rolling_limit"
REASON_REFINE_STALE_TARGET = "stale_target"


def _ceil_minute(value: datetime) -> datetime:
    """向上取整到分钟（窗口起点用它：既稳定成幂等键，又不会落进过去）。"""
    floored = _aware(value).replace(second=0, microsecond=0)
    return floored if floored == _aware(value) else floored + timedelta(minutes=1)


@dataclass(frozen=True)
class CoarseSegment:
    """一个"粗段"：block/hour 层、可变、还有未来部分。

    **"未细化"的操作定义**是"没有 current 子段"：大纲产出的模型大块落库时
    ``is_leaf=True``（它自己就能启动活动），一旦被细化过就翻成 ``False``
    并长出子段。所以判据不能只看 ``is_leaf``——否则普通一天永远不会被细化。
    """

    segment_id: str
    layer: str
    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None = None


@dataclass(frozen=True)
class RefineTarget:
    """一次细化作业的目标：**一个**父段 + 一个未来窗口（设计 §3）。"""

    plan_id: str
    plan_day: date
    revision: int
    parent: CoarseSegment
    window_start: datetime
    window_end: datetime
    child_layer: str
    actor_id: str = ""
    timezone: str = "UTC"
    anchors: tuple[Anchor, ...] = ()
    place_ids: frozenset[str] = frozenset()

    @property
    def idempotency_key(self) -> str:
        return refine_key(self.plan_id, self.revision, self.window_start)


def refine_key(plan_id: str, revision: int, window_start: datetime) -> str:
    """幂等键：``refine:{plan_id}:{revision}:{window_start_utc}``（设计 §3）。

    窗口起点用**分钟精度的 UTC**：扫描每 60 秒一次，同一分钟的多次扫描必须
    落到同一个键上，否则 outbox 与预算表会各记一份，等于每 5 分钟重烧一次。
    """
    stamp = _aware(window_start).astimezone(UTC).strftime("%Y%m%dT%H%MZ")
    return f"{REFINE_KIND}:{plan_id}:{int(revision)}:{stamp}"


@dataclass(frozen=True)
class RefineChild:
    """校验通过的一个子段（还是意向，不是活动）。"""

    layer: str
    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None = None


@dataclass(frozen=True)
class RefineOutcome:
    """一次近窗细化的结果。字段可解释，不含 prompt/正文。"""

    state: str  # refined / no_target / unchanged / blocked / failed / busy / stale_target
    plan_id: str | None = None
    plan_day: date | None = None
    parent_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    revision_before: int = 0
    revision_after: int = 0
    children: int = 0
    calls: int = 0
    reason_code: str | None = None
    provider_id: str | None = None
    idempotency_key: str | None = None


class RefineValidationError(ValueError):
    """细化输出不合约：带**逐条**原因，供日志/行为事件使用（不带原文）。

    与大纲不同，细化**不做修复重试**（设计 §0：每窗口只尝试一次）：
    非法就保留粗段，把原因记在预算行上。
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = problems


_REFINE_OUTPUT_KEYS = {"parent_id", "children"}
_REFINE_CHILD_KEYS = {"layer", "start_at", "end_at", "title", "kind", "place_id"}


def refine_trigger_due(now: datetime, *, start_at: datetime) -> bool:
    """这个粗段现在值得细化吗（距开始 ≤ 60 分钟）。

    只判"最早未细化段"：调用方按 ``start_at`` 升序取第一个候选，
    它不够近就整轮不跑——**不允许**跳过它去细化更远的段，否则每 5 分钟
    都会换一个父段重烧调用。
    """
    return _aware(start_at) <= _ceil_minute(now) + timedelta(
        minutes=REFINE_TRIGGER_MINUTES
    )


def refine_window(
    now: datetime, *, start_at: datetime, end_at: datetime
) -> tuple[datetime, datetime] | None:
    """这一次要细化的**未来窗口**：``[max(now, 父段起点), min(父段终点, now+3h))``。

    返回 ``None`` 表示"不细化"：父段已经过去（过去永不回填），
    或剩余窗口太碎（< 30 分钟）。
    """
    moment = _ceil_minute(now)
    parent_end = _aware(end_at)
    if parent_end <= moment:
        return None
    window_start = max(moment, _aware(start_at))
    window_end = min(parent_end, moment + timedelta(hours=REFINE_LOOKAHEAD_HOURS))
    if window_end - window_start < timedelta(minutes=REFINE_MIN_WINDOW_MINUTES):
        return None
    return window_start, window_end


async def load_coarse_segments(
    session: AsyncSession, *, plan_id: str, now: datetime
) -> list[CoarseSegment]:
    """某版计划里**还没细化**的粗段，按 ``start_at`` 升序（只读）。

    "未细化"= ``layer ∈ {block, hour}``、可变（``anchor_kind=''``）、
    ``end_at > now``（过去的不追补）、**没有 current 子段**。

    ``is_leaf`` 不参与判定：大纲产出的模型大块是 ``is_leaf=True``（自己就能启动
    活动），细化后翻成 ``False`` 并长出子段。只看 ``is_leaf`` 会让普通一天永远
    没有候选；只看"有没有子段"才是"细化过没有"的可核对事实。
    """
    moment = _aware(now)
    rows = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == plan_id,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.layer.in_(REFINE_COARSE_LAYERS),
                    ScheduleSegmentRow.anchor_kind == "",
                    ScheduleSegmentRow.end_at > moment,
                )
                .order_by(ScheduleSegmentRow.start_at, ScheduleSegmentRow.id)
            )
        ).scalars()
    )
    if not rows:
        return []
    refined_parent_ids = {
        str(parent_id)
        for (parent_id,) in (
            await session.execute(
                select(ScheduleSegmentRow.parent_id)
                .where(
                    ScheduleSegmentRow.plan_id == plan_id,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.parent_id.is_not(None),
                )
                .distinct()
            )
        ).all()
        if parent_id
    }
    return [
        CoarseSegment(
            segment_id=str(item.id),
            layer=str(item.layer),
            kind=str(item.kind),
            title=str(item.title),
            start_at=_aware(item.start_at),
            end_at=_aware(item.end_at),
            place_id=item.place_id,
        )
        for item in rows
        if str(item.id) not in refined_parent_ids
    ]


async def load_refine_target(
    session: AsyncSession,
    *,
    actor_id: str,
    now: datetime,
    plan_day: date | None = None,
    timezone: str | None = None,
) -> RefineTarget | None:
    """按**固定规则**选这一次要细化的父段与窗口；没有合格目标返回 ``None``。

    选择规则（写死在代码里，不让模型决定）：

    1. 只看**当前计划日**（``now`` 落在该计划窗口内）的 ``current`` 段；
    2. 候选 = ``layer ∈ {block, hour}``、``anchor_kind = ''``（睡眠/承诺锚点不可
       细化）、``end_at > now``（过去的不追补）、**没有 current 子段**（= 未细化）；
    3. 按 ``start_at`` 升序取**第一个**；它距开始还超过 60 分钟就整轮不跑；
    4. 子段层 = 父层下一层（block→hour、hour→minute）；minute 不再细化；
    5. 窗口 = :func:`refine_window`；不够 30 分钟就不细化。

    第 3 条就是"避免每 5 分钟反复选同一父段白烧调用"的固定规则：
    幂等键固定，且同一键只允许一次尝试（见 :data:`REFINE_MAX_ATTEMPTS_PER_WINDOW`）。
    """
    moment = _aware(now)
    if plan_day is None:
        if timezone is None:
            timezone, _source = await resolve_plan_timezone(session)
        plan_day = plan_day_for(moment, timezone)
    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    if row is None:
        return None
    plan_timezone = str(row.timezone or timezone or "UTC")
    window_start_at, window_end_at = plan_day_window(plan_day, plan_timezone)
    if not (window_start_at <= moment < window_end_at):
        # 不在这一计划日的窗口内（例如 23:59 之后）：不跨日细化。
        return None

    candidates = await load_coarse_segments(session, plan_id=str(row.id), now=moment)
    if not candidates:
        return None

    parent = candidates[0]
    if not refine_trigger_due(moment, start_at=parent.start_at):
        return None  # 最早的一段还远：现在细化只会白烧调用
    child_layer = REFINE_LAYER_STEPS.get(parent.layer)
    if child_layer is None:
        return None  # 已经是最细层
    bounds = refine_window(moment, start_at=parent.start_at, end_at=parent.end_at)
    if bounds is None:
        return None
    window_start, window_end = bounds

    anchor_rows = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == row.id,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.anchor_kind != "",
                    ScheduleSegmentRow.start_at < window_end,
                    ScheduleSegmentRow.end_at > window_start,
                )
                .order_by(ScheduleSegmentRow.start_at)
            )
        ).scalars()
    )
    anchors = tuple(
        Anchor(
            kind=str(item.anchor_kind),
            start_at=_aware(item.start_at),
            end_at=_aware(item.end_at),
            title=str(item.title),
            protective=str(item.anchor_kind) != SLEEP_ANCHOR,
        )
        for item in anchor_rows
    )
    places = {str(item["id"]) for item in await _valid_places(session, actor_id)}
    return RefineTarget(
        plan_id=str(row.id),
        plan_day=plan_day,
        revision=int(row.revision),
        parent=parent,
        window_start=window_start,
        window_end=window_end,
        child_layer=child_layer,
        actor_id=str(row.actor_id),
        timezone=plan_timezone,
        anchors=anchors,
        place_ids=frozenset(places),
    )


def parse_refine_output(raw: str) -> tuple[str, list[object]]:
    """严格解析 ``{"parent_id": "...", "children": [...]}``；不做宽容修。"""
    text = (raw or "").strip()
    if not text:
        raise RefineValidationError(["输出为空"])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefineValidationError(
            [f"不是合法 JSON（{exc.msg} @ {exc.pos}）"]
        ) from exc
    if not isinstance(payload, dict):
        raise RefineValidationError(["顶层必须是 JSON 对象"])
    extra = set(payload) - _REFINE_OUTPUT_KEYS
    if extra:
        raise RefineValidationError([f"顶层有未定义字段：{sorted(extra)}"])
    parent_id = payload.get("parent_id")
    if not isinstance(parent_id, str) or not parent_id.strip():
        raise RefineValidationError(["parent_id 必须是非空字符串"])
    children = payload.get("children")
    if not isinstance(children, list):
        raise RefineValidationError(["children 必须是数组"])
    return parent_id.strip(), children


def validate_refine_children(
    children: list[object],
    *,
    parent: CoarseSegment,
    child_layer: str,
    window_start: datetime,
    window_end: datetime,
    anchors: Sequence[Anchor] = (),
    valid_place_ids: set[str] | frozenset[str] = frozenset(),
) -> list[RefineChild]:
    """把模型输出校验成可入库的子段；任何一条不合就整份拒收。

    硬约束（设计 §3"只允许落在父块与未来窗口内"）：

    1. 条数 1..``REFINE_MAX_CHILDREN``，层必须正好是父层的下一层（递归层级硬上限）；
    2. 每条 5..180 分钟，且完整落在**父块**与**未来窗口**的双重交叠内；
    3. 兄弟之间不重叠（半开区间）；**不要求铺满父块**——没想好的部分保持粗粒度；
    4. 不压不可移动锚点；地点必须在有效地点表里；
    5. 标题不许出现"已经/答应了/一定"这类完成或承诺声称（计划不是事实）；
    6. 未知字段、非分钟精度、缺时区偏移一律拒收。

    返回空列表表示"模型没给出可用的子段"（调用方按 ``refine_empty`` 处理，
    同样不重试）：不为了填满窗口而造段。
    """
    problems: list[str] = []
    if len(children) > REFINE_MAX_CHILDREN:
        problems.append(
            f"子段数 {len(children)} 超过上限 {REFINE_MAX_CHILDREN}（不要写流水账）"
        )
    accepted: list[RefineChild] = []
    for index, raw in enumerate(children):
        label = f"children[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} 不是对象")
            continue
        unknown = set(raw) - _REFINE_CHILD_KEYS
        if unknown:
            problems.append(f"{label} 有未定义字段：{sorted(unknown)}")
            continue
        layer = raw.get("layer")
        if not isinstance(layer, str) or layer != child_layer:
            problems.append(f"{label}.layer 必须是 {child_layer}（递归层级硬上限）")
            continue
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in MODEL_KINDS:
            problems.append(f"{label}.kind 不在白名单：{kind!r}")
            continue
        title = raw.get("title")
        if not isinstance(title, str):
            problems.append(f"{label}.title 必须是字符串")
            continue
        clean_title = " ".join(title.split())
        if not clean_title or len(clean_title) > REFINE_CHILD_TITLE_CHARS:
            problems.append(
                f"{label}.title 长度必须在 1..{REFINE_CHILD_TITLE_CHARS}"
            )
            continue
        claim = find_claim_marker(clean_title)
        if claim is not None:
            problems.append(
                f"{label}.title 把意向写成了完成或承诺（出现「{claim}」）："
                "子段仍是计划，不是已发生的事"
            )
            continue
        place = raw.get("place_id")
        if place is not None and (
            not isinstance(place, str) or place not in valid_place_ids
        ):
            problems.append(f"{label}.place_id 是未知地点：{place!r}")
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
        duration = end - start
        if duration < timedelta(minutes=REFINE_MIN_CHILD_MINUTES):
            problems.append(
                f"{label} 只有 {int(duration.total_seconds() // 60)} 分钟，"
                f"短于下限 {REFINE_MIN_CHILD_MINUTES} 分钟"
            )
            continue
        if duration > timedelta(minutes=REFINE_MAX_CHILD_MINUTES):
            problems.append(
                f"{label} 超过上限 {REFINE_MAX_CHILD_MINUTES} 分钟"
            )
            continue
        if start < parent.start_at or end > parent.end_at:
            problems.append(f"{label} 越出父块 [{parent.start_at.isoformat()}, "
                            f"{parent.end_at.isoformat()})")
            continue
        if start < window_start or end > window_end:
            problems.append(
                f"{label} 越出未来窗口 [{window_start.isoformat()}, "
                f"{window_end.isoformat()})"
            )
            continue
        if any(
            overlaps(start, end, anchor.start_at, anchor.end_at)
            for anchor in anchors
        ):
            problems.append(f"{label} 压到了不可移动锚点")
            continue
        accepted.append(
            RefineChild(
                layer=layer,
                kind=kind,
                title=clean_title,
                start_at=start,
                end_at=end,
                place_id=place,
            )
        )

    ordered = sorted(accepted, key=lambda item: item.start_at)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if overlaps(previous.start_at, previous.end_at, current.start_at, current.end_at):
            problems.append(
                "子段之间重叠："
                f"{previous.start_at.isoformat()}–{previous.end_at.isoformat()} 与 "
                f"{current.start_at.isoformat()}–{current.end_at.isoformat()}"
            )
    if problems:
        raise RefineValidationError(problems)
    return ordered


_REFINE_INSTRUCTIONS = """你是角色「{name}」的日程细化器：把一个**已有的粗块**拆成更小的子段。

只输出一个 JSON 对象（不要解释、不要 Markdown 代码块）：
{{"parent_id":"{parent_id}","children":[{{"layer":"{child_layer}","start_at":"<带时区偏移的 ISO-8601>","end_at":"<同上>","title":"<不超过 40 字>","kind":"<白名单里的一个>","place_id":null}}]}}

硬约束（违反任一条整份输出作废）：
1. 只允许 {child_layer} 层；最多 {max_children} 个子段，每个至少 {min_minutes} 分钟；
2. 每个子段必须完整落在父块 [{parent_start}, {parent_end}) 与未来窗口 [{window_start}, {window_end}) 之内；
3. 子段之间不许重叠；**不要求铺满父块**——没想好的部分保持粗粒度；
4. 不许压到不可移动锚点（睡眠/已确认约定）；
5. 不要重复父块本身，不要写睡眠，不要写"已经做完/答应了/一定"这类完成或承诺；
6. 时间必须带时区偏移、精确到分钟（秒必须为 0），不要写本地朴素时间；
7. kind 只能取：{kinds}；place_id 只能取输入给出的地点 id 或 null。

输入：
{snapshot}
"""


def build_refine_prompt(
    *,
    target: RefineTarget,
    now: datetime,
    persona_name: str = "她",
) -> str:
    """拼一次细化的提示词（设计 §0：输入 ≤ 1,000 token）。

    只给**单个父块、近窗与锚点**：不塞人设全文、不塞聊天原文、不塞梦。
    超预算时先砍地点、再砍锚点标题长度——**绝不**砍窗口边界与硬约束。
    """
    def render(*, with_places: bool, anchor_title_chars: int) -> str:
        snapshot: dict[str, object] = {
            "now_utc": _aware(now).astimezone(UTC).isoformat(),
            "plan_day": target.plan_day.isoformat(),
            "timezone": target.timezone,
            "parent": {
                "id": target.parent.segment_id,
                "layer": target.parent.layer,
                "kind": target.parent.kind,
                "title": _short(target.parent.title, REFINE_CHILD_TITLE_CHARS),
                "start_at": target.parent.start_at.isoformat(),
                "end_at": target.parent.end_at.isoformat(),
                "local": [
                    local_hhmm(target.parent.start_at, target.timezone),
                    local_hhmm(target.parent.end_at, target.timezone),
                ],
                "place_id": target.parent.place_id,
            },
            "future_window": {
                "start_at": target.window_start.isoformat(),
                "end_at": target.window_end.isoformat(),
                "local": [
                    local_hhmm(target.window_start, target.timezone),
                    local_hhmm(target.window_end, target.timezone),
                ],
            },
            "immutable_anchors": [
                {
                    "kind": anchor.kind,
                    "title": _short(anchor.title, anchor_title_chars),
                    "start_at": anchor.start_at.isoformat(),
                    "end_at": anchor.end_at.isoformat(),
                }
                for anchor in target.anchors
            ],
        }
        if with_places:
            snapshot["valid_places"] = sorted(target.place_ids)
        return _REFINE_INSTRUCTIONS.format(
            name=persona_name,
            parent_id=target.parent.segment_id,
            child_layer=target.child_layer,
            max_children=REFINE_MAX_CHILDREN,
            min_minutes=REFINE_MIN_CHILD_MINUTES,
            kinds=", ".join(sorted(MODEL_KINDS)),
            parent_start=target.parent.start_at.isoformat(),
            parent_end=target.parent.end_at.isoformat(),
            window_start=target.window_start.isoformat(),
            window_end=target.window_end.isoformat(),
            snapshot=json.dumps(snapshot, ensure_ascii=False, indent=2),
        )

    prompt = render(with_places=True, anchor_title_chars=24)
    if _estimate_text_tokens(prompt) <= REFINE_RESERVED_INPUT_TOKENS:
        return prompt
    prompt = render(with_places=False, anchor_title_chars=16)
    if _estimate_text_tokens(prompt) <= REFINE_RESERVED_INPUT_TOKENS:
        return prompt
    # 还超：连锚点标题也省略（约束仍在，模型可保守不压任何时间）。
    return render(with_places=False, anchor_title_chars=4)


async def _refine_attempts_for_key(
    session: AsyncSession, *, actor_id: str, key: str
) -> int:
    """这个幂等键已经尝试过几次（失败/空结果也算一次，不退款）。"""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(BehaviorModelCallRow)
                .where(
                    BehaviorModelCallRow.actor_id == actor_id,
                    BehaviorModelCallRow.kind == REFINE_KIND,
                    BehaviorModelCallRow.idempotency_key == key,
                )
            )
        ).scalar_one()
    )


async def _recent_refine_calls(
    session: AsyncSession, *, actor_id: str, now: datetime
) -> int:
    """滚动窗口（3 小时）内已经花掉的细化调用次数（含失败的那次）。"""
    since = _aware(now) - timedelta(hours=REFINE_ROLLING_HOURS)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(BehaviorModelCallRow)
                .where(
                    BehaviorModelCallRow.actor_id == actor_id,
                    BehaviorModelCallRow.kind == REFINE_KIND,
                    BehaviorModelCallRow.created_at >= since,
                )
            )
        ).scalar_one()
    )


async def _commit_refinement(
    session: AsyncSession,
    *,
    target: RefineTarget,
    children: list[RefineChild],
    now: datetime,
) -> int:
    """CAS 提交一次细化：**父段 is_leaf 翻转 + 子段插入在同一事务**。

    - ``day_plans.revision`` 抬一版（WHERE revision = 期望值）；不是期望值就抛
      :class:`StaleRevisionError`，整个嵌套事务回滚——旧响应不会覆盖新计划；
    - 父段 ``is_leaf`` 翻成 ``False``（它现在有子段了，只有叶段能启动活动）；
      父段本身**保留**（不删除、不改时间），所以"父块剩余部分保持粗粒度"成立；
    - 子段是 ``current``、``is_leaf=True``，``revision_created`` = 新版本号。
      其它段的 ``revision_created`` 不动：它们仍是 current，只是没被这次改动碰。
    """
    new_revision = target.revision + 1
    async with session.begin_nested():
        result = await session.execute(
            update(DayPlanRow)
            .where(
                DayPlanRow.id == target.plan_id,
                DayPlanRow.revision == target.revision,
            )
            .values(revision=new_revision, updated_at=now)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise StaleRevisionError(target.revision)
        parent_result = await session.execute(
            update(ScheduleSegmentRow)
            .where(
                ScheduleSegmentRow.id == target.parent.segment_id,
                ScheduleSegmentRow.plan_id == target.plan_id,
                ScheduleSegmentRow.status == "current",
            )
            .values(is_leaf=False, updated_at=now)
        )
        if int(getattr(parent_result, "rowcount", 0) or 0) != 1:
            # 父段在模型调用期间被改版/取消：整份细化丢弃，不写半套子段。
            raise StaleRevisionError(target.revision)
        for child in children:
            session.add(
                ScheduleSegmentRow(
                    id=new_id("seg"),
                    plan_id=target.plan_id,
                    actor_id=target.actor_id,
                    parent_id=target.parent.segment_id,
                    parent_key=target.parent.segment_id,
                    layer=child.layer,
                    kind=child.kind,
                    title=child.title,
                    start_at=child.start_at,
                    end_at=child.end_at,
                    place_id=child.place_id,
                    commitment_level="tentative",
                    status="current",
                    is_leaf=True,
                    anchor_kind="",
                    source="model",
                    revision_created=new_revision,
                    revision_ended=None,
                    source_event_ids=[f"{REFINE_KIND}:{target.parent.segment_id}"],
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
    return new_revision


async def refine_work(
    session: AsyncSession,
    *,
    actor_id: str,
    now: datetime | None = None,
    plan_day: date | None = None,
    timezone: str | None = None,
    router: object | None = None,
    config_reader: object | None = None,
    lease_owner: str | None = None,
    expect_key: str | None = None,
) -> RefineOutcome:
    """跑一次**近窗细化**作业（设计 §3 的 ``refine:`` 事件）。

    一次只细化一个父段、只覆盖未来窗口；**失败一律保留粗段**：

    - 模型不可用 → 0 次调用、不占预算行；
    - 超时/报错/非法输出/空结果 → 记在预算行上，**当次不重试**；
    - CAS 失败（别人先提交了新版本）→ 丢弃这次响应，未来窗口再试；
    - 过去的时间永不回填：窗口起点取"现在向上取整到分钟"与父段起点的较大者。

    调用方负责 commit/rollback；本函数内部只在"预留"和"提交"两处用嵌套事务。
    """
    moment = _aware(now or utcnow())
    target = await load_refine_target(
        session, actor_id=actor_id, now=moment, plan_day=plan_day, timezone=timezone
    )
    if target is None:
        return RefineOutcome(state="no_target")
    key = target.idempotency_key
    base = RefineOutcome(
        state="unchanged",
        plan_id=target.plan_id,
        plan_day=target.plan_day,
        parent_id=target.parent.segment_id,
        window_start=target.window_start,
        window_end=target.window_end,
        revision_before=target.revision,
        revision_after=target.revision,
        idempotency_key=key,
    )
    if expect_key is not None and expect_key != key:
        # 扫描时选的目标已经变了（版本/窗口都不同）：丢弃旧事件，不调模型。
        return replace(base, state="stale_target", reason_code=REASON_REFINE_STALE_TARGET)
    attempts = await _refine_attempts_for_key(session, actor_id=actor_id, key=key)
    if attempts >= REFINE_MAX_ATTEMPTS_PER_WINDOW:
        return replace(base, reason_code=REASON_REFINE_ATTEMPTED)
    if await _recent_refine_calls(session, actor_id=actor_id, now=moment) >= (
        REFINE_ROLLING_MAX_CALLS
    ):
        return replace(base, state="blocked", reason_code=REASON_REFINE_ROLLING)
    if not await _outline_model_ready(router, config_reader):
        return replace(base, state="blocked", reason_code=REASON_MODEL_UNAVAILABLE)

    owner = lease_owner or f"refine:{new_id('lease')}"
    reservation = await _reserve_call(
        session,
        actor_id=actor_id,
        plan_day=target.plan_day,
        idempotency_key=key,
        now=moment,
        lease_owner=owner,
        kind=REFINE_KIND,
        max_calls=REFINE_MAX_CALLS_PER_DAY,
        daily_token_budget=REFINE_DAILY_TOKEN_BUDGET,
        reserved_input_tokens=REFINE_RESERVED_INPUT_TOKENS,
        reserved_output_tokens=REFINE_RESERVED_OUTPUT_TOKENS,
        lease_seconds=REFINE_LEASE_SECONDS,
        attempt=attempts + 1,
    )
    if reservation is None:
        return replace(base, state="blocked", reason_code=REASON_BUDGET_EXHAUSTED)

    prompt = build_refine_prompt(target=target, now=moment)
    raw = ""
    try:
        raw = await _call_outline_model(
            router=router, config_reader=config_reader, prompt=prompt
        )
        parent_id, raw_children = parse_refine_output(raw)
        if parent_id != target.parent.segment_id:
            raise RefineValidationError(
                [f"parent_id 必须是本次给出的父段 id {target.parent.segment_id}"]
            )
        children = validate_refine_children(
            raw_children,
            parent=target.parent,
            child_layer=target.child_layer,
            window_start=target.window_start,
            window_end=target.window_end,
            anchors=target.anchors,
            valid_place_ids=target.place_ids,
        )
        if not children:
            # 空结果是**失败**而不是"无事可做"：按一次尝试记账，当次不重试。
            await _finish_call(
                session,
                call_id=reservation.call_id,
                state="failed",
                error_code=REASON_REFINE_EMPTY,
            )
            return replace(
                base, state="failed", calls=1, reason_code=REASON_REFINE_EMPTY
            )
    except RefineValidationError:
        await _finish_call(
            session,
            call_id=reservation.call_id,
            state="failed",
            error_code=REASON_REFINE_INVALID,
        )
        return replace(
            base,
            state="failed",
            calls=1,
            reason_code=REASON_REFINE_INVALID,
        )
    except (TimeoutError, ModelError, Exception) as exc:  # noqa: BLE001
        reason_code = _model_error_code(exc)
        await _finish_call(
            session,
            call_id=reservation.call_id,
            state="failed",
            error_code=reason_code,
        )
        return replace(base, state="failed", calls=1, reason_code=reason_code)

    try:
        revision_after = await _commit_refinement(
            session, target=target, children=children, now=moment
        )
    except StaleRevisionError:
        await _finish_call(
            session,
            call_id=reservation.call_id,
            state="failed",
            error_code=REASON_REFINE_STALE,
        )
        return replace(
            base, state="busy", calls=1, reason_code=REASON_REFINE_STALE
        )

    provider_id = _resolved_provider_id(
        await config_reader.effective(), REFINE_PROFILE  # type: ignore[attr-defined]
    )
    await _finish_call(
        session,
        call_id=reservation.call_id,
        state="committed",
        actual_tokens=_estimate_tokens(prompt, raw),
        provider_id=provider_id,
    )
    return replace(
        base,
        state="refined",
        revision_after=revision_after,
        children=len(children),
        calls=1,
        reason_code=None,
        provider_id=provider_id,
    )


# ---------------------------------------------------------------- 活动边界（阶段 2）
#
# 段边界只是**计划**，不是"她真做完了"。本模块在这里只做**只读**投影：
# 回答"此刻哪些段/锚点覆盖 now""那条活动来源段是不是已经结束"。
# 活动的唯一写入方是 `ActivityService`、场景的唯一写入方是 `PresenceService`；
# 域边界闸门不允许 agenda 直接 import 它们，所以状态变更留在 worker 的组合根。


@dataclass(frozen=True)
class BoundarySegment:
    """此刻与活动边界相关的一个段（只读投影，不含任何状态写入）。"""

    segment_id: str
    plan_id: str
    layer: str
    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    place_id: str | None
    anchor_kind: str
    commitment_level: str
    is_leaf: bool
    # 段的受众范围（旧段为 NULL=未知）。开活动时**只许收窄**，不许放宽。
    visibility: str | None = None

    @property
    def is_sleep(self) -> bool:
        return self.anchor_kind == SLEEP_ANCHOR

    @property
    def is_confirmed_anchor(self) -> bool:
        return self.anchor_kind == COMMITMENT_ANCHOR or (
            self.anchor_kind == "" and self.commitment_level == "confirmed"
        )


@dataclass(frozen=True)
class BoundaryView:
    """``now`` 时刻的时间轴投影（**零模型、零状态写入**）。"""

    plan_id: str | None = None
    plan_day: date | None = None
    revision: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    covering: tuple[BoundarySegment, ...] = ()
    sleep: BoundarySegment | None = None
    active_leaf: BoundarySegment | None = None
    commitment: BoundarySegment | None = None

    def covers_segment(self, segment_id: str | None) -> bool:
        """这条段此刻是否还覆盖 ``now``（覆盖 = 活动还没到边界）。"""
        if not segment_id:
            return False
        return any(item.segment_id == segment_id for item in self.covering)

    @property
    def has_plan(self) -> bool:
        return self.plan_id is not None


async def boundary_view(
    session: AsyncSession,
    *,
    actor_id: str,
    now: datetime,
    plan_day: date | None = None,
    timezone: str | None = None,
) -> BoundaryView:
    """取 ``now`` 时刻的段边界投影（当前计划日）。

    - ``covering``：``current`` 且 ``start_at <= now < end_at`` 的段（含锚点）；
    - ``sleep``：覆盖此刻的睡眠锚点（有它就不开活动）；
    - ``active_leaf``：可以启动活动的叶段（``is_leaf`` 且非睡眠、非锚点）；
      多条重叠时按 ``start_at`` 取**最早**的一条，保证重启后选同一条；
    - ``commitment``：覆盖此刻的已确认约定（只用于暂停/场景提示，不创建活动）。
    """
    moment = _aware(now)
    if plan_day is None:
        if timezone is None:
            timezone, _source = await resolve_plan_timezone(session)
        plan_day = plan_day_for(moment, timezone)
    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    if row is None:
        return BoundaryView()
    rows = list(
        (
            await session.execute(
                select(ScheduleSegmentRow)
                .where(
                    ScheduleSegmentRow.plan_id == row.id,
                    ScheduleSegmentRow.status == "current",
                    ScheduleSegmentRow.start_at <= moment,
                    ScheduleSegmentRow.end_at > moment,
                )
                .order_by(ScheduleSegmentRow.start_at, ScheduleSegmentRow.id)
            )
        ).scalars()
    )
    covering = tuple(_boundary_segment(item) for item in rows)
    sleep = next((item for item in covering if item.is_sleep), None)
    commitment = next((item for item in covering if item.is_confirmed_anchor), None)
    active_leaf = next(
        (
            item
            for item in covering
            if item.is_leaf and item.anchor_kind == "" and item.kind != SLEEP_ANCHOR
        ),
        None,
    )
    return BoundaryView(
        plan_id=str(row.id),
        plan_day=row.plan_day,
        revision=int(row.revision),
        window_start=_aware(row.window_start_at),
        window_end=_aware(row.window_end_at),
        covering=covering,
        sleep=sleep,
        active_leaf=active_leaf,
        commitment=commitment,
    )


def _boundary_segment(row: ScheduleSegmentRow) -> BoundarySegment:
    return BoundarySegment(
        segment_id=str(row.id),
        plan_id=str(row.plan_id),
        layer=str(row.layer),
        kind=str(row.kind),
        title=str(row.title),
        start_at=_aware(row.start_at),
        end_at=_aware(row.end_at),
        place_id=row.place_id,
        anchor_kind=str(row.anchor_kind or ""),
        commitment_level=str(row.commitment_level),
        is_leaf=bool(row.is_leaf),
        visibility=row.visibility,
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
    intents_in_prompt: int = 0,
    preparations: int = 0,
    proposed_intentions: int = 0,
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
        intents_in_prompt=intents_in_prompt,
        preparations=preparations,
        proposed_intentions=proposed_intentions,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "CATCH_UP_MIN_MINUTES_BEFORE_REST",
    "COMMITMENT_ANCHOR",
    "COMMITMENT_PROTECTIVE_MINUTES",
    "FUTURE_INTENT_DAYS",
    "FUTURE_INTENT_LIMIT",
    "FUTURE_INTENT_PER_DAY",
    "FUTURE_INTENT_TOKEN_LIMIT",
    "MAX_EXPERIENCE_SUMMARIES",
    "MEMORY_TOKEN_LIMIT",
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
    "PREPARATION_MIN_GAP_MINUTES",
    "REASON_DEGRADED_LATE",
    "REASON_NEEDS_REVIEW",
    "REASON_PREPARATION_UNPLACED",
    "REASON_REFINE_ATTEMPTED",
    "REASON_REFINE_INVALID",
    "REASON_REFINE_ROLLING",
    "REASON_REFINE_STALE",
    "REASON_REFINE_STALE_TARGET",
    "REASON_ROUTINE_CONFLICT",
    "REASON_SLEEP_BOUNDARY",
    "REFINE_CHILD_TITLE_CHARS",
    "REFINE_COARSE_LAYERS",
    "REFINE_DAILY_TOKEN_BUDGET",
    "REFINE_KIND",
    "REFINE_LAYER_STEPS",
    "REFINE_LOOKAHEAD_HOURS",
    "REFINE_MAX_ATTEMPTS_PER_WINDOW",
    "REFINE_MAX_CALLS_PER_DAY",
    "REFINE_MAX_CHILDREN",
    "REFINE_MAX_CHILD_MINUTES",
    "REFINE_MIN_CHILD_MINUTES",
    "REFINE_MIN_WINDOW_MINUTES",
    "REFINE_PROFILE",
    "REFINE_ROLLING_HOURS",
    "REFINE_ROLLING_MAX_CALLS",
    "REFINE_TRIGGER_MINUTES",
    "REPLAN_TODO_TAG",
    "SLEEP_ANCHOR",
    "Anchor",
    "AnchorConflict",
    "BoundarySegment",
    "BoundaryView",
    "CoarseSegment",
    "FutureIntent",
    "FutureIntentZone",
    "IntentProposal",
    "MemoryItem",
    "MemoryZone",
    "OutlineOutcome",
    "OutlineOutput",
    "OutlineValidationError",
    "PersonaProjection",
    "PlannedBlock",
    "PreparationLink",
    "RefineChild",
    "RefineOutcome",
    "RefineTarget",
    "RefineValidationError",
    "ReplanTodo",
    "RoutineSnapshot",
    "SeamReport",
    "SegmentDraft",
    "StaleRevisionError",
    "boundary_view",
    "build_memory_zone",
    "build_outline_prompt",
    "build_persona_projection",
    "build_refine_prompt",
    "build_repair_prompt",
    "contains",
    "display_day_for",
    "display_day_window",
    "ensure_day_plan",
    "find_claim_marker",
    "fit_future_intents",
    "fit_memory_zone",
    "free_gaps",
    "has_hedge_marker",
    "intent_prep_ready",
    "intention_short_id",
    "load_commitment_anchors",
    "load_coarse_segments",
    "load_day_plan",
    "load_future_intents",
    "load_refine_target",
    "load_segments",
    "local_hhmm",
    "local_minute_instant",
    "on_new_confirmed_anchor",
    "outline_due_at",
    "outline_due_decision",
    "outline_work",
    "overlaps",
    "parse_outline_output",
    "parse_outline_payload",
    "parse_refine_output",
    "plan_day_for",
    "plan_day_length",
    "plan_day_window",
    "plan_preparations",
    "refine_key",
    "refine_trigger_due",
    "refine_window",
    "refine_work",
    "resolve_anchor_conflicts",
    "resolve_plan_timezone",
    "resolve_zone",
    "rule_outline",
    "run_due_outline",
    "seam_check",
    "sleep_anchors_for_day",
    "validate_intention_proposals",
    "validate_outline",
    "validate_refine_children",
]
