"""梦境与跨夜情绪结算：**纯服务**（本轮不做 worker 接线）。

设计来源：``docs/DREAM-SCHEDULE-DESIGN.md`` §2/§5/§9 阶段 4，
文献边界见 ``docs/DREAM-SCHEDULE-RESEARCH.md`` §2。

## 四条硬规则（代码注释与测试都要钉住）

1. **梦不是事实**。只写 ``dream_reports``；绝不写 ``activities`` /
   ``self_experiences`` / 记忆或事实抽取 outbox，也不出现在事实检索里。
   ``dream_reports.status`` 的 ``ready/failed/missed/no_dream`` 是**产品记录
   状态**——``no_dream`` 不等于"她没做梦"，``ready`` 也不等于"她梦到的事发生过"。
2. **一晚一份梦、一次结算**：唯一键 ``(actor_id, sleep_date)``。
   ``sleep_date`` 是**醒来所在的角色本地日**；午夜把同一段预计睡眠切成两段，
   仍然只算**一晚**（设计 §2/§5），不会因为跨午夜跑两次。
3. **结算与梦解耦**：梦失败/没素材也照常结算；结算失败不影响梦的失败记录。
   结算只经 ``AffectService`` 改 ``affect_states`` 的 global 分量（唯一写入方）。
4. **不补编、不重试**：模型超时/不可用/输出不合约 → ``failed``，一次调用就是
   全部预算（设计 §3 的梦额度是 1 次 / 2,700 token），不写第二份 prompt、
   不留"她忘了梦"这种伪事实。

## 跨域只读投影

候选素材来自 ``self_experiences`` / ``activities`` / ``commitments`` /
``affect_signals`` / ``personas`` / ``relationship_states`` / ``presence_states``。
这些表通过 ``ling.infra.models`` 共享模型**只读**访问（ADR-0001：可以读投影，
不能写别人的表）；本模块**没有任何** INSERT/UPDATE/DELETE 打到这些表，
唯一的写目标是 ``dream_reports`` 与 ``sleep_settlements``。

## 与 P0.5「事实封堵」的对接点

入梦的自我经历**一律**先过 ``ling.domains.evidence.fact_eligible_experiences``：
``clock_only`` 证据（只来自时钟/计划边界）不算事实，既不进候选也不进 prompt，
而且会回查来源活动——只读派生行、不复核来源正是 P0.5 要堵的洞。

``DreamService(fact_filter=...)`` 还能再**叠加**一层收窄（测试/过渡期用）；
它只能过滤掉更多行，**不能放宽** P0.5 的闸。dream 里不复制证据判定规则。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.affect.service import AffectService
from ling.domains.dream.fragments import (
    DEFAULT_MAX_CUES,
    MIN_SOURCES,
    NEGATIVE_EMOTIONS,
    TRANSFORMS,
    Candidate,
    CueTuple,
    ScoredCue,
    ThreatPlan,
    TransformStep,
    action_label,
    plan_transforms,
    recites_source,
    select_cues,
    threat_plan,
)
from ling.domains.evidence import fact_eligible_experiences
from ling.infra.models import (
    ActivityRow,
    AffectSignalRow,
    CommitmentRow,
    DreamReportRow,
    Persona,
    PresenceState,
    RelationshipState,
    SelfExperienceRow,
    SleepSettlementRow,
    WorldPlace,
    utcnow,
)
from ling.infra.security import new_id
from ling.ports.model import ChatMessageIn

logger = logging.getLogger("ling.domains.dream.service")

# 梦与大纲共用 `memory.extract` 一个职责（设计 §0：不借 dialogue.fast）。
DREAM_PROFILE = "memory.extract"
# `behavior_model_calls.kind` 的取值；预算预留留给接线那一轮（P7b）。
DREAM_KIND = "dream"
DREAM_TIMEOUT_SECONDS = 60.0

# 设计 §3：梦每次输入/输出上限 2,000 / 700 token，每个计划日最多 1 次。
DREAM_INPUT_TOKEN_ESTIMATE = 2000
DREAM_OUTPUT_TOKEN_ESTIMATE = 700

# 生成窗口：醒前 30–60 分钟（设计 §5）。到期取中点，醒后 30 分钟仍未跑到即
# `missed`（设计 §3 的事件表）。三个值都是**工程初值**，不是文献结论。
DREAM_LEAD_MINUTES = 60
DREAM_DUE_BEFORE_WAKE_MINUTES = 45
DREAM_MISS_AFTER_WAKE_MINUTES = 30

# 候选来源的取数边界（工程初值）：设计 §5 说最近 72 小时，超出只留残余。
CANDIDATE_LOOKBACK_HOURS = 72
MAX_CANDIDATES_PER_SOURCE = 40
MAX_NARRATIVE_CHARS = 700
MAX_TITLE_CHARS = 40
MAX_MOOD_CHARS = 32
MAX_PERSONA_TRAITS = 8

# 结果状态。前四个是 ``dream_reports.status``（DB 白名单）；
# 后三个只出现在 outcome 上，表示"这次没写库"（没有可写的一晚）。
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_MISSED = "missed"
STATUS_NO_DREAM = "no_dream"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_NO_SLEEP_WINDOW = "no_sleep_window"
OUTCOME_TOO_EARLY = "too_early"
STATE_SETTLED = "settled"

REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_MODEL_TIMEOUT = "model_timeout"
REASON_MODEL_ERROR = "model_error"
REASON_DREAM_INVALID = "dream_invalid"
REASON_THREAT_MISMATCH = "dream_threat_mismatch"
REASON_RECITES_SOURCE = "dream_recites_source"


# ---------------------------------------------------------------- 睡眠窗


@dataclass(frozen=True)
class NightWindow:
    """一**晚**睡眠的 UTC 半开区间 ``[bed_at, wake_at)``。

    ``sleep_date`` 是**醒来所在的本地日**：跨午夜作息（23:00 睡、08:00 起）时
    bed 在前一天、wake 在当天，但它们仍是一晚。``wake_at`` 就是
    ``agenda.timeline.sleep_anchors_for_day(sleep_date, ...)`` 里那段
    ``[00:00, wake)`` 的右端——午夜拆分只影响**计划段**的存储，不把一晚拆成两晚。
    """

    sleep_date: date
    bed_at: datetime
    wake_at: datetime
    timezone: str
    routine_revision: int

    @property
    def minutes(self) -> int:
        return int((self.wake_at - self.bed_at).total_seconds() // 60)

    @property
    def generate_from(self) -> datetime:
        return self.wake_at - timedelta(minutes=DREAM_LEAD_MINUTES)

    @property
    def due_at(self) -> datetime:
        return self.wake_at - timedelta(minutes=DREAM_DUE_BEFORE_WAKE_MINUTES)

    @property
    def late_after(self) -> datetime:
        return self.wake_at + timedelta(minutes=DREAM_MISS_AFTER_WAKE_MINUTES)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_zone(name: str | None) -> ZoneInfo:
    """IANA 名 → ``ZoneInfo``；非法/为空退回 UTC（不用服务器本地时区）。"""
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_minute_instant(day: date, minute: int, timezone: str | None) -> datetime:
    """本地墙钟 ``day + minute`` 的 UTC 瞬时（DST 的"不存在/重复"同 agenda 规则）。

    与 ``agenda.timeline.local_minute_instant`` **逐字同规则**：不存在就顺延到
    该日下一个有效分钟，重复取 ``fold=0`` 的第一次出现。为什么不直接 import
    那个函数：跨域闸门（``tests/unit/test_domain_boundaries.py``）只允许
    import 邻居域的 ``service`` 模块，``agenda.timeline`` 不是 ``service``。
    等价性由 ``tests/unit/test_dream_service.py`` 对拍
    ``sleep_anchors_for_day`` 的 wake 瞬时来保证——哪天那边改了语义，这条会红。
    """
    zone = resolve_zone(timezone)
    naive = datetime.combine(day, time.min) + timedelta(minutes=int(minute) % (24 * 60))
    for _ in range(24 * 60):
        candidate = naive.replace(tzinfo=zone, fold=0)
        back = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if back == naive:
            return candidate.astimezone(UTC)
        naive += timedelta(minutes=1)
    raise ValueError(f"本地时刻无法解析：{day} + {minute} 分钟（{zone.key}）")


async def _canonical_timezone(session: AsyncSession) -> str:
    """角色的**规范时区**（DB > env > 默认）。

    与 agenda 的计划日同一口径：作息表里的 ``timezone`` 只用于冲突检测，
    不参与日界/醒点计算（设计 §2）。
    """
    from ling.config import get_settings
    from ling.settings.user_preferences import effective_user_timezone, owner_subject_id

    owner = await owner_subject_id(session)
    return await effective_user_timezone(
        session, subject_id=owner, env=get_settings().user_timezone
    )


async def _routine_snapshot(session: AsyncSession, actor_id: str) -> tuple[int, int, int, str]:
    """``(wake_minute, rest_minute, revision, routine_timezone)``。

    走 ``AgendaService.routine()``（agenda 的公开读取入口）拿 wake/rest/时区；
    ``revision`` 是 ``sleep_settlements.source_routine_revision`` 的快照来源，
    ``RoutineView`` 没有这个字段，所以只读 ``actor_routines.revision`` 一列。
    """
    from ling.domains.agenda.service import AgendaService
    from ling.infra.models import ActorRoutineRow

    routine = await AgendaService(session, actor_id=actor_id).routine()
    row = await session.get(ActorRoutineRow, actor_id)
    revision = int(row.revision or 0) if row is not None else 0
    return (
        int(routine.wake_minute) % (24 * 60),
        int(routine.rest_minute) % (24 * 60),
        revision,
        str(routine.timezone or ""),
    )


async def night_window(
    session: AsyncSession, *, actor_id: str, sleep_date: date, timezone: str | None = None
) -> NightWindow | None:
    """``sleep_date`` 这一晚的 ``[bed_at, wake_at)``；没有有效睡眠窗返回 ``None``。

    规则与 ``agenda.timeline.sleep_anchors_for_day`` 对齐：

    - ``wake == rest``：作息没有有效睡眠窗（不是"睡了 24 小时"）→ ``None``，
      **不排梦**、不结算；
    - ``rest > wake``（23:00 睡、08:00 起）：bed 在前一天本地 23:00，wake 在
      ``sleep_date`` 本地 08:00——跨午夜但仍是**一晚**；
    - ``rest < wake``（02:00 睡、10:00 起）：bed 与 wake 同在 ``sleep_date``，
      午夜到 02:00 是清醒时间。
    """
    wake_minute, rest_minute, revision, _routine_timezone = await _routine_snapshot(
        session, actor_id
    )
    if wake_minute == rest_minute:
        return None
    zone_name = timezone or await _canonical_timezone(session)
    wake_at = local_minute_instant(sleep_date, wake_minute, zone_name)
    rest_day = sleep_date if rest_minute < wake_minute else sleep_date - timedelta(days=1)
    bed_at = local_minute_instant(rest_day, rest_minute, zone_name)
    if bed_at >= wake_at:
        return None
    return NightWindow(
        sleep_date=sleep_date,
        bed_at=bed_at,
        wake_at=wake_at,
        timezone=zone_name,
        routine_revision=revision,
    )


# ---------------------------------------------------------------- 模型输出


class DreamValidationError(ValueError):
    """模型输出不合约：带**逐条**原因与一个稳定原因码（不含原文）。"""

    def __init__(self, problems: list[str], *, reason_code: str = REASON_DREAM_INVALID) -> None:
        super().__init__("；".join(problems))
        self.problems = problems
        self.reason_code = reason_code


@dataclass(frozen=True)
class DreamDraft:
    """校验通过的一份梦（模拟叙事，不是事实）。"""

    title: str
    narrative: str
    mood: str
    cue_ids: tuple[str, ...]
    transform_ids: tuple[str, ...]
    threat_present: bool


_DRAFT_KEYS = {
    "title",
    "narrative",
    "mood",
    "used_cue_ids",
    "transforms_used",
    "threat_present",
}


def parse_dream_payload(
    raw: str,
    *,
    cue_sources: Mapping[str, str],
    planned_transforms: Sequence[str],
    threat: ThreatPlan,
    source_texts: Sequence[str] = (),
) -> DreamDraft:
    """严格解析模型输出：只接受设计 §5 的六字段 JSON。

    **不做宽容修**：宽容会把"模型没按契约输出"变成"我们猜它的意思"，而猜错的
    代价是把一段编造的经历存成"她梦到的"。代码围栏、解释文字、额外字段一律
    按非法处理。校验同时钉住四件事：

    1. ``used_cue_ids`` ⊆ 输入，且至少来自**两个不同来源**（不能拿一条素材硬编）；
    2. ``transforms_used`` 非空且 ⊆ **代码计划的**算子（模型不能自选没计划的变形）；
    3. ``threat_present`` 必须等于这一晚的分布闸门（威胁 ≤1/4 晚是硬上限）；
    4. 叙事不得**逐字复述**任何素材原文（:func:`recites_source`）。
    """
    text = raw.strip()
    if not text:
        raise DreamValidationError(["输出为空"])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DreamValidationError([f"不是合法 JSON（{exc.msg} @ {exc.pos}）"]) from exc
    if not isinstance(payload, dict):
        raise DreamValidationError(["顶层必须是 JSON 对象"])
    unknown = set(payload) - _DRAFT_KEYS
    if unknown:
        raise DreamValidationError([f"顶层有未定义字段：{sorted(unknown)}"])

    problems: list[str] = []
    reason = REASON_DREAM_INVALID

    title_raw = payload.get("title")
    title = " ".join(str(title_raw).split()) if isinstance(title_raw, str) else ""
    if not title:
        problems.append("title 必须是非空字符串")
    elif len(title) > MAX_TITLE_CHARS:
        problems.append(f"title 超过 {MAX_TITLE_CHARS} 字")

    narrative_raw = payload.get("narrative")
    narrative = " ".join(str(narrative_raw).split()) if isinstance(narrative_raw, str) else ""
    if not narrative:
        problems.append("narrative 必须是非空字符串")
    elif len(narrative) > MAX_NARRATIVE_CHARS:
        problems.append(f"narrative 超过 {MAX_NARRATIVE_CHARS} 字")

    mood_raw = payload.get("mood")
    mood = " ".join(str(mood_raw).split()) if isinstance(mood_raw, str) else ""
    if not mood:
        problems.append("mood 必须是非空字符串")
    elif len(mood) > MAX_MOOD_CHARS:
        problems.append(f"mood 超过 {MAX_MOOD_CHARS} 字")

    used_raw = payload.get("used_cue_ids")
    used: list[str] = []
    if not isinstance(used_raw, list) or not used_raw:
        problems.append("used_cue_ids 必须是非空数组")
    else:
        for item in used_raw:
            if not isinstance(item, str) or item not in cue_sources:
                problems.append(f"used_cue_ids 里有未输入过的 id：{item!r}")
                continue
            if item not in used:
                used.append(item)
        distinct_sources = {cue_sources[item] for item in used}
        if len(distinct_sources) < MIN_SOURCES:
            problems.append(
                f"used_cue_ids 只来自 {len(distinct_sources)} 个来源，至少要 {MIN_SOURCES} 个"
            )

    transforms_raw = payload.get("transforms_used")
    transforms: list[str] = []
    planned = [str(item) for item in planned_transforms]
    if not isinstance(transforms_raw, list) or not transforms_raw:
        problems.append("transforms_used 必须是非空数组")
    else:
        for item in transforms_raw:
            if not isinstance(item, str) or item not in TRANSFORMS:
                problems.append(f"transforms_used 里有未知算子：{item!r}")
                continue
            if item not in planned:
                problems.append(f"transforms_used 用了没计划的算子：{item!r}")
                continue
            if item not in transforms:
                transforms.append(item)

    threat_raw = payload.get("threat_present")
    threat_present = bool(threat_raw) if isinstance(threat_raw, bool) else None
    if threat_present is None:
        problems.append("threat_present 必须是布尔值")
    elif threat_present != threat.present:
        problems.append(
            f"threat_present={threat_present} 与这一晚的威胁计划（{threat.present}）不一致"
        )
        reason = REASON_THREAT_MISMATCH

    if narrative and source_texts:
        hit = recites_source(narrative, source_texts)
        if hit is not None:
            problems.append("narrative 逐字复述了素材原文（梦只能是重组后的虚构叙事）")
            reason = REASON_RECITES_SOURCE

    if problems:
        raise DreamValidationError(problems, reason_code=reason)
    return DreamDraft(
        title=title,
        narrative=narrative,
        mood=mood,
        cue_ids=tuple(used),
        transform_ids=tuple(transforms),
        threat_present=bool(threat_present),
    )


def build_dream_prompt(
    *,
    name: str,
    sleep_date: date,
    timezone: str,
    cues: Sequence[ScoredCue],
    transforms: Sequence[TransformStep],
    threat: ThreatPlan,
    affect_summary: Mapping[str, float],
    persona: Mapping[str, object],
) -> str:
    """把**只含四元组**的输入快照拼成 prompt。

    设计 §5 的输入契约：``{persona_traits, sleep_date, affect_summary,
    cues:[{cue_id, tuple, score, provenance}], transform, prohibited_claims}``。
    这里**没有**任何素材原文、聊天原文、计划正文或用户标识——不是"我们记得
    删了"，而是这些字段根本没被读进来。
    """
    snapshot: dict[str, Any] = {
        "sleep_date": sleep_date.isoformat(),
        "timezone": timezone,
        "persona_traits": dict(persona),
        "affect_summary": {str(k): round(float(v), 3) for k, v in affect_summary.items()},
        "cues": [
            {
                "cue_id": item.cue.cue_id,
                "tuple": item.cue.as_tuple.as_dict(),
                "score": round(item.score.total, 4),
                "provenance": item.cue.provenance,
            }
            for item in cues
        ],
        "transform": [step.as_dict() for step in transforms],
        "threat": threat.as_dict(),
    }
    return _DREAM_INSTRUCTIONS.format(
        name=name,
        sleep_date=sleep_date.isoformat(),
        timezone=timezone,
        cue_ids=", ".join(item.cue.cue_id for item in cues),
        transform_ids=", ".join(step.transform_id for step in transforms),
        threat_present=str(threat.present).lower(),
        max_narrative=MAX_NARRATIVE_CHARS,
        max_title=MAX_TITLE_CHARS,
        max_mood=MAX_MOOD_CHARS,
        snapshot=json.dumps(snapshot, ensure_ascii=False, indent=2),
    )


_DREAM_INSTRUCTIONS = """你是角色「{name}」的梦境叙述者。

现在要写**{sleep_date} 这一晚**（{timezone}，醒来所在的本地日）的一个梦。
这是**虚构梦景**，不是活动回执，也不是对明天的预演。

硬规则（违反任何一条整份结果作废）：
1. 只输出一个 JSON 对象，字段恰好是：title、narrative、mood、used_cue_ids、
   transforms_used、threat_present。不要解释、不要 Markdown 代码块、不要额外字段。
2. 只能用下面 cues 里的**四元组碎片**（人/地点/动作/情绪）重组；不要引入
   素材里没有的具体事实、地点、人名或对话内容。
3. **禁止逐字复述任何素材原文**：梦是碎片重混，不是把经历改写一遍。
4. **必须**用上给定的 transform（至少一项），并在 transforms_used 里写出你实际
   用到的算子名；算子只能从给定的这几项里选。
5. used_cue_ids 只能取输入里的 cue_id，且**至少来自两个不同来源**。
6. threat_present 必须输出 {threat_present}（这一晚的威胁闸门）。为 true 时：
   威胁场景最多一处；除非输入说明可以脱身，否则写成"逃不掉/醒不过来/动不了"，
   不要每次都成功逃脱。
7. 第一人称、现在时；字数 ≤ {max_narrative}；title ≤ {max_title} 字；
   mood 是一个短词（≤ {max_mood} 字）。
8. 不要声称现实里真的发生过这件事，不要向任何人许下承诺或约定，不要写成预言。
9. 不要在叙事里写出任何真实标识（账号、昵称、群名、电话、地址）。

可用 cue_id：{cue_ids}
给定算子：{transform_ids}

输入快照（JSON，只有四元组与分数）：
{snapshot}
"""


# ---------------------------------------------------------------- 结果


@dataclass(frozen=True)
class DreamOutcome:
    """一次"生成这一晚的梦"的结果。字段可解释，不含 prompt 与正文。"""

    sleep_date: date
    status: str
    report_id: str | None = None
    calls: int = 0
    reason_code: str | None = None
    cue_ids: tuple[str, ...] = ()
    transform_ids: tuple[str, ...] = ()
    sources: int = 0
    threat: bool = False

    @property
    def wrote_row(self) -> bool:
        return self.status in (STATUS_READY, STATUS_FAILED, STATUS_MISSED, STATUS_NO_DREAM)


@dataclass(frozen=True)
class SettlementOutcome:
    """一次跨夜结算的结果。与梦的成败**无关**。"""

    sleep_date: date
    state: str  # settled / unchanged / no_sleep_window / too_early
    bed_at: datetime | None = None
    wake_at: datetime | None = None
    components: int = 0
    reason_code: str | None = None


# ---------------------------------------------------------------- 梦服务


#: 附加的收窄过滤器（只能过滤更多，不能放宽 P0.5 的事实闸）。
FactFilter = Callable[[Any], bool]


class DreamService:
    """一晚一份梦的唯一写入方。

    ``fact_filter`` 是 P0.5 事实闸之上的**附加**收窄器：它拿到的是一条
    ``SelfExperienceRow``，返回 ``False`` 表示这一晚连它也不许入梦。
    默认 ``None`` = 不加收窄；P0.5 的 ``fact_eligible_experiences`` 永远先生效。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str = "actor_ling",
        persona_id: str | None = None,
        fact_filter: FactFilter | None = None,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._persona_id = persona_id
        self._fact_filter = fact_filter

    # ---------------------------------------------------------- 入口

    async def generate(
        self,
        sleep_date: date,
        *,
        now: datetime | None = None,
        router: object | None = None,
        config_reader: object | None = None,
        fact_filter: FactFilter | None = None,
    ) -> DreamOutcome:
        """生成 ``sleep_date`` 这一晚的梦（幂等：同一晚只跑一次模型）。

        分支顺序就是"能不能花钱"的顺序：

        1. 这一晚已有记录（含 ``failed/no_dream/missed``）→ 原样返回，**0 次调用**；
        2. 没有有效睡眠窗 → 不写行、0 次调用（没有"一晚"可记录）；
        3. 还没到睡前（``now < bed_at``）→ 不写行、0 次调用；
        4. 已过醒后 30 分钟 → ``missed``，0 次调用（设计 §3 的事件表）；
        5. 候选不足两源 → ``no_dream``，**0 次调用**（不拿计划/人设补素材）；
        6. 模型不可用 → ``failed``（``model_unavailable``），0 次调用；
        7. 调用模型；异常 → ``failed``；输出不合约 → ``failed``；**都不重试**。
        """
        moment = _aware(now or utcnow())
        plan_filter = fact_filter if fact_filter is not None else self._fact_filter

        existing = await self._load_report(sleep_date)
        if existing is not None:
            return self._outcome_from_row(existing, calls=0, reason_code="existing")

        window = await night_window(self._session, actor_id=self._actor_id, sleep_date=sleep_date)
        if window is None:
            return DreamOutcome(
                sleep_date=sleep_date,
                status=OUTCOME_NO_SLEEP_WINDOW,
                reason_code="no_sleep_window",
            )
        if moment < window.bed_at:
            return DreamOutcome(
                sleep_date=sleep_date,
                status=OUTCOME_TOO_EARLY,
                reason_code="before_sleep",
            )
        if moment > window.late_after:
            row = await self._store(
                sleep_date=sleep_date,
                status=STATUS_MISSED,
                cue_ids=(),
                transform_ids=(),
            )
            return self._outcome_from_row(row, calls=0, reason_code="missed_window")

        cues = await self._select_cues(window, fact_filter=plan_filter)
        if not cues:
            row = await self._store(
                sleep_date=sleep_date,
                status=STATUS_NO_DREAM,
                cue_ids=(),
                transform_ids=(),
            )
            return self._outcome_from_row(row, calls=0, reason_code="insufficient_sources")

        transforms = plan_transforms(cues, actor_id=self._actor_id, sleep_date=sleep_date)
        planned = [step.transform_id for step in transforms]
        threat = threat_plan(self._actor_id, sleep_date)
        prompt = build_dream_prompt(
            name=await self._display_name(),
            sleep_date=sleep_date,
            timezone=window.timezone,
            cues=cues,
            transforms=transforms,
            threat=threat,
            affect_summary=self._affect_summary(cues),
            persona=await self._persona_traits(),
        )

        if not await _model_ready(router, config_reader):
            row = await self._store(
                sleep_date=sleep_date,
                status=STATUS_FAILED,
                cue_ids=tuple(item.cue.cue_id for item in cues),
                transform_ids=tuple(planned),
            )
            return self._outcome_from_row(
                row,
                calls=0,
                reason_code=REASON_MODEL_UNAVAILABLE,
                sources=len({item.cue.source_ref for item in cues}),
                threat=threat.present,
            )

        try:
            raw = await _call_dream_model(
                router=router, config_reader=config_reader, prompt=prompt
            )
        except Exception as exc:  # noqa: BLE001 - 模型故障一律 failed，不重试
            logger.info("梦生成失败（%s）", type(exc).__name__)
            row = await self._store(
                sleep_date=sleep_date,
                status=STATUS_FAILED,
                cue_ids=tuple(item.cue.cue_id for item in cues),
                transform_ids=tuple(planned),
            )
            return self._outcome_from_row(
                row,
                calls=1,
                reason_code=_model_error_code(exc),
                sources=len({item.cue.source_ref for item in cues}),
                threat=threat.present,
            )

        try:
            draft = parse_dream_payload(
                raw,
                cue_sources={item.cue.cue_id: item.cue.source_ref for item in cues},
                planned_transforms=planned,
                threat=threat,
                source_texts=[item.cue.text_hint for item in cues if item.cue.text_hint],
            )
        except DreamValidationError as exc:
            logger.info("梦输出被拒收：%s", exc.reason_code)
            row = await self._store(
                sleep_date=sleep_date,
                status=STATUS_FAILED,
                cue_ids=tuple(item.cue.cue_id for item in cues),
                transform_ids=tuple(planned),
            )
            return self._outcome_from_row(
                row,
                calls=1,
                reason_code=exc.reason_code,
                sources=len({item.cue.source_ref for item in cues}),
                threat=threat.present,
            )

        row = await self._store(
            sleep_date=sleep_date,
            status=STATUS_READY,
            title=draft.title,
            narrative=draft.narrative,
            mood=draft.mood,
            cue_ids=draft.cue_ids,
            transform_ids=draft.transform_ids,
        )
        return self._outcome_from_row(
            row,
            calls=1,
            reason_code=None,
            sources=len({item.cue.source_ref for item in cues}),
            threat=draft.threat_present,
        )

    # ---------------------------------------------------------- 落库

    async def _load_report(self, sleep_date: date) -> DreamReportRow | None:
        return (
            await self._session.execute(
                select(DreamReportRow).where(
                    DreamReportRow.actor_id == self._actor_id,
                    DreamReportRow.sleep_date == sleep_date,
                )
            )
        ).scalar_one_or_none()

    async def _store(
        self,
        *,
        sleep_date: date,
        status: str,
        title: str | None = None,
        narrative: str | None = None,
        mood: str | None = None,
        cue_ids: Sequence[str] = (),
        transform_ids: Sequence[str] = (),
    ) -> DreamReportRow:
        """写入唯一的一份记录；并发下谁先提交谁生效，另一份被丢弃。

        唯一键 ``(actor_id, sleep_date)`` 让 ``ready/failed/missed/no_dream``
        **都算占用**：一晚不会因为第一次失败而在重跑时变成第二份记录。
        """
        payload = {
            "id": new_id("dream"),
            "actor_id": self._actor_id,
            "sleep_date": sleep_date,
            "status": status,
            "title": title,
            "narrative": narrative,
            "mood": mood,
            "cue_ids": list(cue_ids),
            "transform_ids": list(transform_ids),
            # 结构上钉死：模拟叙事 + 角色私有（DB 也有 CHECK 兜底）。
            "origin": "simulated",
            "visibility": "actor_private",
            "plan_id": None,
        }
        await self._session.execute(
            pg_insert(DreamReportRow)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["actor_id", "sleep_date"])
        )
        await self._session.flush()
        row = await self._load_report(sleep_date)
        if row is None:  # pragma: no cover - 唯一键冲突后必然读得到
            raise RuntimeError("dream_reports 写入后读不到记录")
        return row

    # ---------------------------------------------------------- 候选素材

    async def _select_cues(
        self, window: NightWindow, *, fact_filter: FactFilter | None
    ) -> list[ScoredCue]:
        candidates: list[Candidate] = []
        signals = await self._signals(window)
        candidates.extend(
            await self._experience_candidates(window, signals, fact_filter=fact_filter)
        )
        candidates.extend(self._signal_candidates(signals))
        candidates.extend(await self._unfinished_candidates(window))
        candidates.extend(await self._persona_candidates())
        body = await self._body_candidate()
        if body is not None:
            candidates.append(body)
        return select_cues(
            candidates,
            actor_id=self._actor_id,
            sleep_date=window.sleep_date,
            bed_at=window.bed_at,
            max_cues=DEFAULT_MAX_CUES,
        )

    async def _experiences(
        self, window: NightWindow
    ) -> list[SelfExperienceRow]:
        """截至睡前的自我经历，**已经过 P0.5 的事实闸**。

        先多取 3 倍：``clock_only`` 证据的行会被过滤掉，不能让它们占满配额
        （P0.5 的 ``fact_eligible_experiences`` 模块注释里就是这样要求的）。
        """
        since = window.bed_at - timedelta(hours=CANDIDATE_LOOKBACK_HOURS)
        rows = list(
            (
                await self._session.execute(
                    select(SelfExperienceRow)
                    .where(
                        SelfExperienceRow.actor_id == self._actor_id,
                        SelfExperienceRow.archived_at.is_(None),
                        SelfExperienceRow.occurred_at <= window.bed_at,
                        SelfExperienceRow.occurred_at >= since,
                    )
                    .order_by(SelfExperienceRow.occurred_at.desc())
                    .limit(MAX_CANDIDATES_PER_SOURCE * 3)
                )
            ).scalars()
        )
        return await fact_eligible_experiences(
            self._session, rows, limit=MAX_CANDIDATES_PER_SOURCE
        )

    async def _signals(self, window: NightWindow) -> list[AffectSignalRow]:
        """只取 ``scope=global`` 的信号。

        ``scope=window`` 的信号带具体原因、**不跨窗口搬运**（设计 §5/§6）：
        梦虽然是 actor 私有，但把某个私聊窗口的峰值搬进另一个场景，等于让
        一段关系的情绪泄漏到别处。
        """
        since = window.bed_at - timedelta(hours=CANDIDATE_LOOKBACK_HOURS)
        return list(
            (
                await self._session.execute(
                    select(AffectSignalRow)
                    .where(
                        AffectSignalRow.actor_id == self._actor_id,
                        AffectSignalRow.scope == "global",
                        AffectSignalRow.occurred_at <= window.bed_at,
                        AffectSignalRow.occurred_at >= since,
                    )
                    .order_by(AffectSignalRow.intensity.desc())
                    .limit(MAX_CANDIDATES_PER_SOURCE)
                )
            ).scalars()
        )

    @staticmethod
    def _signal_negative(emotion: str, valence: float | None) -> bool:
        return (float(valence) < 0.0) if valence is not None else emotion in NEGATIVE_EMOTIONS

    async def _experience_candidates(
        self,
        window: NightWindow,
        signals: Sequence[AffectSignalRow],
        *,
        fact_filter: FactFilter | None,
    ) -> list[Candidate]:
        # 同一源事件的信号取最强的那个：一条经历只挂一个情绪碎片。
        by_event: dict[str, AffectSignalRow] = {}
        for signal in signals:
            event_id = str(signal.source_event_id or "")
            current = by_event.get(event_id)
            if current is None or float(signal.intensity) > float(current.intensity):
                by_event[event_id] = signal

        out: list[Candidate] = []
        for row in await self._experiences(window):
            if fact_filter is not None and not fact_filter(row):
                # 附加收窄（测试/过渡期）：不算事实的经历不能当梦的原料，
                # 也不能拿别的来源替它"补编"。
                continue
            matched = None
            for event_id in row.source_event_ids or []:
                matched = by_event.get(str(event_id))
                if matched is not None:
                    break
            emotion = str(matched.emotion) if matched is not None else ""
            intensity = float(matched.intensity) if matched is not None else 0.0
            negative = (
                self._signal_negative(emotion, matched.valence)
                if matched is not None
                else False
            )
            cue = Candidate(
                cue_id=f"cue:experience:{row.id}",
                kind="experience",
                provenance="self_experience",
                source_ref=f"self_experience:{row.id}",
                occurred_at=_aware(row.occurred_at),
                as_tuple=CueTuple(
                    person="自己",
                    action=action_label(str(row.kind)),
                    emotion=emotion,
                ),
                signaled_intensity=intensity,
                negative=negative,
                text_hint=str(row.summary or ""),
            )
            out.append(cue)
        return out

    @staticmethod
    def _signal_candidates(signals: Sequence[AffectSignalRow]) -> list[Candidate]:
        out: list[Candidate] = []
        for signal in signals:
            emotion = str(signal.emotion or "")
            cue = Candidate(
                cue_id=f"cue:signal:{signal.id}",
                kind="signal",
                provenance="affect_signal",
                source_ref=f"affect_signal:{signal.id}",
                occurred_at=_aware(signal.occurred_at),
                as_tuple=CueTuple(person="自己", emotion=emotion),
                signaled_intensity=float(signal.intensity),
                negative=DreamService._signal_negative(emotion, signal.valence),
            )
            out.append(cue)
        return out

    async def _places(self) -> dict[str, str]:
        rows = (
            await self._session.execute(
                select(WorldPlace).where(WorldPlace.actor_id == self._actor_id)
            )
        ).scalars()
        return {str(row.id): str(row.name) for row in rows}

    async def _unfinished_candidates(self, window: NightWindow) -> list[Candidate]:
        places = await self._places()
        out: list[Candidate] = []
        activities = list(
            (
                await self._session.execute(
                    select(ActivityRow)
                    .where(
                        ActivityRow.actor_id == self._actor_id,
                        ActivityRow.state.in_(("paused", "failed")),
                    )
                    .order_by(ActivityRow.started_at.desc())
                    .limit(5)
                )
            ).scalars()
        )
        for row in activities:
            started = row.ended_at or row.started_at or row.created_at
            occurred = _aware(started) if started is not None else None
            if occurred is not None and occurred > window.bed_at:
                continue
            place = places.get(str(row.place_id or ""), "")
            out.append(
                Candidate(
                    cue_id=f"cue:activity:{row.id}",
                    kind="unfinished",
                    provenance="activity",
                    source_ref=f"activity:{row.id}",
                    occurred_at=occurred,
                    as_tuple=CueTuple(
                        person="自己", place=place, action=action_label(str(row.kind))
                    ),
                    unfinished=1.0,
                    text_hint=str(row.completion_evidence or ""),
                )
            )
        commitments = list(
            (
                await self._session.execute(
                    select(CommitmentRow)
                    .where(
                        CommitmentRow.actor_id == self._actor_id,
                        CommitmentRow.state == "active",
                        CommitmentRow.due_at < window.bed_at,
                    )
                    .order_by(CommitmentRow.due_at.desc())
                    .limit(3)
                )
            ).scalars()
        )
        for row in commitments:
            # 承诺原文不进程 prompt：只留"有一件没做到的事"这个碎片。
            out.append(
                Candidate(
                    cue_id=f"cue:commitment:{row.id}",
                    kind="unfinished",
                    provenance="commitment",
                    source_ref=f"commitment:{row.id}",
                    occurred_at=_aware(row.due_at),
                    as_tuple=CueTuple(person="一个约好的人", action="没做到的事"),
                    unfinished=1.0,
                    text_hint=str(row.text or ""),
                )
            )
        return out

    async def _persona_candidates(self) -> list[Candidate]:
        persona_id = self._persona_id or await self._resolve_persona_id()
        out: list[Candidate] = []
        persona = await self._session.get(Persona, persona_id)
        if persona is not None:
            traits = persona.traits if isinstance(persona.traits, dict) else {}
            keys = [str(key) for key in list(traits)[:MAX_PERSONA_TRAITS]]
            if keys:
                out.append(
                    Candidate(
                        cue_id=f"cue:persona:{persona.id}",
                        kind="persona",
                        provenance="persona",
                        source_ref=f"persona:{persona.id}",
                        occurred_at=None,
                        as_tuple=CueTuple(person="自己", action="、".join(keys[:3])),
                        persona_relevance=1.0,
                        material=False,
                    )
                )
        best = (
            await self._session.execute(
                select(RelationshipState)
                .where(RelationshipState.actor_id == self._actor_id)
                .order_by(RelationshipState.familiarity.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if best is not None:
            out.append(
                Candidate(
                    cue_id=f"cue:relationship:{best.id}",
                    kind="persona",
                    provenance="relationship",
                    source_ref=f"relationship:{best.id}",
                    occurred_at=None,
                    as_tuple=CueTuple(person="一个熟悉的人", action="想起一个人"),
                    persona_relevance=min(1.0, max(0.0, float(best.familiarity))),
                    # 关系是底色：不能自己凑够"两个来源"。
                    material=False,
                )
            )
        return out

    async def _body_candidate(self) -> Candidate | None:
        row = await self._session.get(PresenceState, self._actor_id)
        if row is None:
            return None
        energy = float(row.energy)
        satiety = float(row.satiety)
        ambience = max(0.0, min(1.0, max((60.0 - energy) / 60.0, (60.0 - satiety) / 60.0)))
        if ambience <= 0.0:
            return None
        action = "身体很沉" if (60.0 - energy) >= (60.0 - satiety) else "胃里空空"
        return Candidate(
            cue_id=f"cue:body:{self._actor_id}",
            kind="body",
            provenance="presence",
            source_ref=f"presence:{self._actor_id}",
            occurred_at=None,
            as_tuple=CueTuple(person="自己", action=action),
            body_ambience=ambience,
            # 身体状态只是氛围色（调研 §2.2：身体刺激的对照实验是阴性），
            # 不能自己凑够"两个来源"。
            material=False,
        )

    # ---------------------------------------------------------- 小工具

    async def _resolve_persona_id(self) -> str:
        from ling.config import get_settings

        try:
            return get_settings().persona_id
        except Exception:  # noqa: BLE001 - 配置不可读时用默认 id
            return "persona_ling"

    async def _display_name(self) -> str:
        persona = await self._session.get(Persona, await self._resolve_persona_id())
        if persona is None:
            return "她"
        return str(persona.display_name or persona.name or "她")

    @staticmethod
    def _affect_summary(cues: Sequence[ScoredCue]) -> dict[str, float]:
        summary: dict[str, float] = {}
        for item in cues:
            emotion = item.cue.as_tuple.emotion
            if not emotion:
                continue
            value = float(item.cue.signaled_intensity)
            summary[emotion] = max(summary.get(emotion, 0.0), value)
        return summary

    async def _persona_traits(self) -> dict[str, object]:
        persona = await self._session.get(Persona, await self._resolve_persona_id())
        if persona is None:
            return {}
        traits = persona.traits if isinstance(persona.traits, dict) else {}
        return {str(key): traits[key] for key in list(traits)[:MAX_PERSONA_TRAITS]}

    def _outcome_from_row(
        self,
        row: DreamReportRow,
        *,
        calls: int,
        reason_code: str | None,
        sources: int = 0,
        threat: bool = False,
    ) -> DreamOutcome:
        return DreamOutcome(
            sleep_date=row.sleep_date,
            status=str(row.status),
            report_id=str(row.id),
            calls=calls,
            reason_code=reason_code,
            cue_ids=tuple(str(item) for item in (row.cue_ids or [])),
            transform_ids=tuple(str(item) for item in (row.transform_ids or [])),
            sources=sources,
            threat=threat,
        )



# ---------------------------------------------------------------- 模型调用


async def _model_ready(router: object | None, config_reader: object | None) -> bool:
    """这个职责现在能不能调模型（未绑定/被停用/槽位没配都算不能）。

    预检在**任何写库与花费之前**（候选选择只读）：设计 §8 明确"无可用模型"
    是一次 0 调用，不该先花 token 再发现没有模型。
    """
    if router is None or config_reader is None:
        return False
    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
        resolved = effective.resolve_profile(DREAM_PROFILE)
    except Exception:  # noqa: BLE001 - 配置读不出来等于"现在不能调"
        return False
    return bool(resolved.usable and resolved.provider_id and resolved.model_id)


async def _call_dream_model(
    *, router: object, config_reader: object, prompt: str
) -> str:
    """调一次 ``memory.extract`` 职责，返回完整文本（**严格一次调用**）。"""
    effective = await config_reader.effective()  # type: ignore[attr-defined]
    resolved = effective.resolve_profile(DREAM_PROFILE)
    if not resolved.usable:
        raise RuntimeError(resolved.reason or f"职责 {DREAM_PROFILE} 没有可用模型")
    messages = [ChatMessageIn(role="user", content=prompt)]

    async def _collect() -> str:
        chunks: list[str] = []
        async for chunk, _outcome in router.stream(  # type: ignore[attr-defined]
            effective,
            profile=DREAM_PROFILE,
            messages=messages,
            # 梦失败就记 failed，不换模型偷偷重试（预算与可解释性都要留痕）。
            allow_fallback=False,
        ):
            if chunk.delta:
                chunks.append(chunk.delta)
        return "".join(chunks)

    return await asyncio.wait_for(_collect(), timeout=DREAM_TIMEOUT_SECONDS)


def _model_error_code(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return REASON_MODEL_TIMEOUT
    kind = getattr(getattr(exc, "kind", None), "value", None)
    if isinstance(kind, str) and kind:
        return f"model_{kind}"
    return REASON_MODEL_ERROR


# ---------------------------------------------------------------- 跨夜结算


class SleepSettlementService:
    """跨夜情绪结算的唯一写入方（`sleep_settlements` 唯一键 ``(actor, sleep_date)``）。

    与梦**完全解耦**：这里不读 ``dream_reports``，也不因为梦失败而跳过结算。
    ``affect_states`` 的写入仍然只经 ``AffectService``（本类不自己写那张表）。
    """

    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def settle(
        self, sleep_date: date, *, now: datetime | None = None, timezone: str | None = None
    ) -> SettlementOutcome:
        """结算 ``sleep_date`` 这一晚（幂等：重复调用不会二次打折）。

        分支：
        - 已有行 → ``unchanged``，不碰 ``affect_states``（否则 arousal/valence
          每跑一次就再乘 0.6，重启几次就把情绪吃光）；
        - 没有有效睡眠窗 → ``no_sleep_window``，不写行；
        - 还没醒（``now < wake_at``）→ ``too_early``，不写行；
        - 否则：算 ``I_bed`` → 写 global 分量 → 落唯一键行。
        """
        moment = _aware(now or utcnow())
        existing = (
            await self._session.execute(
                select(SleepSettlementRow).where(
                    SleepSettlementRow.actor_id == self._actor_id,
                    SleepSettlementRow.sleep_date == sleep_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return SettlementOutcome(
                sleep_date=sleep_date,
                state=OUTCOME_UNCHANGED,
                bed_at=_aware(existing.bed_at) if existing.bed_at else None,
                wake_at=_aware(existing.wake_at) if existing.wake_at else None,
                reason_code="existing",
            )

        window = await night_window(
            self._session, actor_id=self._actor_id, sleep_date=sleep_date, timezone=timezone
        )
        if window is None:
            return SettlementOutcome(
                sleep_date=sleep_date,
                state=OUTCOME_NO_SLEEP_WINDOW,
                reason_code="no_sleep_window",
            )
        if moment < window.wake_at:
            return SettlementOutcome(
                sleep_date=sleep_date,
                state=OUTCOME_TOO_EARLY,
                bed_at=window.bed_at,
                wake_at=window.wake_at,
                reason_code="before_wake",
            )

        settlement = await AffectService(
            self._session, actor_id=self._actor_id
        ).apply_sleep_settlement(bed_at=window.bed_at, wake_at=window.wake_at)

        payload = {
            "actor_id": self._actor_id,
            "sleep_date": sleep_date,
            "bed_at": window.bed_at,
            "wake_at": window.wake_at,
            "global_affect_before": {
                "at": window.bed_at.isoformat(),
                "components": settlement.before,
            },
            "global_affect_after": {
                "at": window.wake_at.isoformat(),
                "components": settlement.after,
            },
            "source_routine_revision": window.routine_revision,
        }
        await self._session.execute(
            pg_insert(SleepSettlementRow)
            .values(**payload)
            .on_conflict_do_nothing(index_elements=["actor_id", "sleep_date"])
        )
        await self._session.flush()
        row = (
            await self._session.execute(
                select(SleepSettlementRow).where(
                    SleepSettlementRow.actor_id == self._actor_id,
                    SleepSettlementRow.sleep_date == sleep_date,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover - 唯一键冲突后必然读得到
            raise RuntimeError("sleep_settlements 写入后读不到记录")
        return SettlementOutcome(
            sleep_date=sleep_date,
            state=STATE_SETTLED if row.global_affect_before else OUTCOME_UNCHANGED,
            bed_at=window.bed_at,
            wake_at=window.wake_at,
            components=len(settlement.settled) + len(settlement.untouched),
        )


__all__ = [
    "CANDIDATE_LOOKBACK_HOURS",
    "DREAM_DUE_BEFORE_WAKE_MINUTES",
    "DREAM_INPUT_TOKEN_ESTIMATE",
    "DREAM_LEAD_MINUTES",
    "DREAM_MISS_AFTER_WAKE_MINUTES",
    "DREAM_OUTPUT_TOKEN_ESTIMATE",
    "DREAM_PROFILE",
    "DREAM_TIMEOUT_SECONDS",
    "DREAM_KIND",
    "DreamDraft",
    "DreamOutcome",
    "DreamService",
    "DreamValidationError",
    "NightWindow",
    "OUTCOME_NO_SLEEP_WINDOW",
    "OUTCOME_TOO_EARLY",
    "OUTCOME_UNCHANGED",
    "SettlementOutcome",
    "SleepSettlementService",
    "STATUS_FAILED",
    "STATUS_MISSED",
    "STATUS_NO_DREAM",
    "STATUS_READY",
    "build_dream_prompt",
    "local_minute_instant",
    "night_window",
    "parse_dream_payload",
    "resolve_zone",
]
