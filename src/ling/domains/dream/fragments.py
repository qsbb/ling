"""梦境候选打分、碎片重混与威胁分布：**纯函数**，不碰数据库、不调模型。

依据 ``docs/DREAM-SCHEDULE-DESIGN.md`` §5 与 ``docs/DREAM-SCHEDULE-RESEARCH.md``
§2。**全部权重、半衰期、威胁占比都是工程初值**：文献只说明"哪些变量有影响、
方向如何"，从不给系数（调研文档开头原话）。改这些数字要有回放/盲评依据，
不能把它们写成"论文结论"。

三条比权重更硬的约束（来自文献）：

1. **不复述**：每条候选先抽成 ``(人, 地点, 动作, 情绪)`` 四元组，再跨条目
   重混（[Malinowski & Horton 2014](https://pubmed.ncbi.nlm.nih.gov/24635722/)：
   只有 ~0.5% 的梦含完整情节记忆，>80% 含自传体记忆碎片）。原始摘要只用于
   **事后复述检测**，永不进 prompt。
2. **威胁占比封顶**：约 1/4 的夜晚才允许含威胁，其中"成功脱身"≤ 1/3。
   这是**分布上限**，不是"每篇都必须带威胁"；也不是把"被追且逃掉"当默认基调
   （[Malcolm-Smith 2012](https://doi.org/10.1016/j.concog.2011.11.004) 实测
   约 20%，成功逃脱约 1/5）。
3. **情绪连续性**：候选按日间情绪峰值加权，负性 ×1.3
   （[Williams et al. 2026](https://pubmed.ncbi.nlm.nih.gov/41986553/) 只支持
   方向，不支持这个系数）。

设计上还有两条工程铁律：

- 候选必须来自**至少两个不同来源**才能生成梦；不足就 ``no_dream`` 且
  **零模型调用**（设计 §5）。这里的"来源"是来源事件（一条经历、一条承诺、
  一个情绪信号……），不是类别——两条经历同属"经历"类但仍是两个来源。
- 打分与重混必须是**确定性**的：同一 ``(actor_id, sleep_date)`` 重放得到同一
  份碎片与算子。唯一性靠 ``dream_reports`` 的唯一键兜底，但确定性让"到底用了
  什么素材"可以复现。
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime

# ---------------------------------------------------------------- 权重（工程初值）


WEIGHT_RECENCY = 0.30
WEIGHT_EMOTION = 0.25
WEIGHT_UNFINISHED = 0.20
WEIGHT_PERSONA = 0.15
WEIGHT_BODY = 0.05
WEIGHT_JITTER = 0.05

#: 三条硬约束里的第一、二条：最近 0–24h 的自我经历主导，48–72h 只留残余。
RECENCY_HALF_LIFE_HOURS = 18.0
RECENCY_FAR_AFTER_HOURS = 24.0
RECENCY_FAR_FACTOR = 0.3

#: 负性情绪的加权放大（调研 §2.3「负性 ×1.3」）。
NEGATIVE_EMOTION_MULTIPLIER = 1.3

NEGATIVE_EMOTIONS: frozenset[str] = frozenset({"uneasy", "tired"})

#: 取 top 6–8：太多会让 prompt 变成流水账，太少则没有可重混的素材。
DEFAULT_MAX_CUES = 8
MIN_SOURCES = 2

#: 活动类别 → 梦里的**动作词**。只做类别映射，绝不使用证据原文
#: （原文复述是设计 §5 明确禁止的）。
ACTION_LABELS: dict[str, str] = {
    "reading": "读书",
    "writing": "写字",
    "cooking": "做饭",
    "resting": "躺着",
    "exercise": "走路",
    "walk": "走路",
    "work": "做事",
    "study": "看书",
    "meal": "吃东西",
    "chore": "收拾东西",
    "errand": "出门办事",
    "leisure": "发呆",
    "social": "和人说话",
    "hobby": "摆弄小东西",
    "commute": "赶路",
    "free": "什么都不做",
}


def action_label(kind: str) -> str:
    """活动/经历的类别 → 动作词（未知类别退回"做事"，不猜细节）。"""
    return ACTION_LABELS.get(str(kind), "做事")


# ---------------------------------------------------------------- 四元组与候选


@dataclass(frozen=True)
class CueTuple:
    """梦的原料碎片：``(人, 地点, 动作, 情绪)``。

    四个字段都是**有限标签**，没有原文。空字符串表示这一维没有来源——
    "没有来源就记空，不能让模型补证据"（设计 §5）。
    """

    person: str = ""
    place: str = ""
    action: str = ""
    emotion: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "person": self.person,
            "place": self.place,
            "action": self.action,
            "emotion": self.emotion,
        }

    def is_empty(self) -> bool:
        return not any((self.person, self.place, self.action, self.emotion))


@dataclass(frozen=True)
class Candidate:
    """一条入梦候选（还没打分）。

    ``text_hint`` 是**唯一**持有原文的字段，且只用于
    :func:`recites_source` 的事后复述检测；它不会出现在 prompt 里
    （:func:`ling.domains.dream.service.build_dream_prompt` 只读四元组）。

    ``material`` 区分"有来源的素材"（经历/情绪信号/未完成的事/身体）与**底色**
    （人设、关系）。底色可以进梦、可以参与重混，但**不能自己凑够"至少两个
    来源"**：只有人设的夜晚说明这天没有可入梦的生活，应当 ``no_dream``，
    而不是拿人设编一段"梦"（设计 §5：人设是底色，不作为自传事实）。
    """

    cue_id: str
    kind: str  # experience / signal / unfinished / persona / body
    provenance: str  # self_experience / affect_signal / activity / commitment / persona / presence
    source_ref: str
    occurred_at: datetime | None
    as_tuple: CueTuple
    signaled_intensity: float = 0.0
    negative: bool = False
    unfinished: float = 0.0
    persona_relevance: float = 0.0
    body_ambience: float = 0.0
    text_hint: str = ""
    #: False = 只是底色（人设/关系/身体），不能自己凑够"至少两个来源"。
    material: bool = True


@dataclass(frozen=True)
class Score:
    """打分明细：每个字段是**原始因子**（0..1），``total`` 是加权和。"""

    recency: float
    emotion: float
    unfinished: float
    persona: float
    body: float
    jitter: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "recency": round(self.recency, 4),
            "emotion": round(self.emotion, 4),
            "unfinished": round(self.unfinished, 4),
            "persona": round(self.persona, 4),
            "body": round(self.body, 4),
            "jitter": round(self.jitter, 4),
            "total": round(self.total, 4),
        }


@dataclass(frozen=True)
class ScoredCue:
    """候选 + 得分（入 prompt 与落库都只看它）。"""

    cue: Candidate
    score: Score


# ---------------------------------------------------------------- 打分


def _digest(*parts: str) -> int:
    """稳定的 64 位整数（不依赖 ``hash()``：每个进程的盐不同）。"""
    raw = "|".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def recency_factor(occurred_at: datetime | None, *, bed_at: datetime) -> float:
    """``2 ^ (-age_hours / 18)``；超过 24h 再乘 0.3（设计 §5 的公式）。

    未来时间（``age < 0``）按 0 小时算：梦不该因为时钟漂移把"还没发生的事"
    当成刚发生的。没有时间来源（人设/身体）记 0——不能让它们冒充"最近的经历"。
    """
    if occurred_at is None:
        return 0.0
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=bed_at.tzinfo)
    age = (bed_at - occurred_at).total_seconds() / 3600.0
    age = max(0.0, age)
    value = math.pow(2.0, -age / RECENCY_HALF_LIFE_HOURS)
    if age > RECENCY_FAR_AFTER_HOURS:
        value *= RECENCY_FAR_FACTOR
    return value


def emotion_factor(signaled_intensity: float, *, negative: bool) -> float:
    """日间情绪峰值 → 0..1；负性 ×1.3 后仍然夹在 [0, 1]。"""
    value = max(0.0, float(signaled_intensity))
    if negative:
        value *= NEGATIVE_EMOTION_MULTIPLIER
    return min(1.0, value)


def stable_jitter(*, actor_id: str, sleep_date: date, cue_id: str) -> float:
    """固定种子扰动：同一 ``(actor, sleep_date, cue)`` 永远同一个值。

    梦的"怪异"主要来自重组算子，不是纯随机（调研 §2.3 最后一行）；
    这 0.05 只是打破同分候选的固定顺序，不改变可解释性。
    """
    digest = _digest(actor_id, sleep_date.isoformat(), cue_id, "jitter")
    return (digest % 1_000_003) / 1_000_003.0


def score_candidate(
    candidate: Candidate, *, actor_id: str, sleep_date: date, bed_at: datetime
) -> Score:
    """按设计 §5 的权重算一条候选的分。

    权重合计 1.0，所以每项因子都在 0..1 时 ``total`` 也在 0..1。
    """
    recency = recency_factor(candidate.occurred_at, bed_at=bed_at)
    emotion = emotion_factor(candidate.signaled_intensity, negative=candidate.negative)
    unfinished = min(1.0, max(0.0, float(candidate.unfinished)))
    persona = min(1.0, max(0.0, float(candidate.persona_relevance)))
    body = min(1.0, max(0.0, float(candidate.body_ambience)))
    jitter = stable_jitter(actor_id=actor_id, sleep_date=sleep_date, cue_id=candidate.cue_id)
    total = (
        WEIGHT_RECENCY * recency
        + WEIGHT_EMOTION * emotion
        + WEIGHT_UNFINISHED * unfinished
        + WEIGHT_PERSONA * persona
        + WEIGHT_BODY * body
        + WEIGHT_JITTER * jitter
    )
    return Score(
        recency=recency,
        emotion=emotion,
        unfinished=unfinished,
        persona=persona,
        body=body,
        jitter=jitter,
        total=total,
    )


def select_cues(
    candidates: list[Candidate] | tuple[Candidate, ...],
    *,
    actor_id: str,
    sleep_date: date,
    bed_at: datetime,
    max_cues: int = DEFAULT_MAX_CUES,
) -> list[ScoredCue]:
    """打分 → 按来源去重 → 取 top-k。

    **素材来源不足两个就返回空**：调用方必须据此落 ``no_dream`` 且**零模型调用**
    （设计 §5「至少 2 条非同源碎片才生成」）。返回空也意味着"不要拿计划补素材"。

    "来源"只数 ``material=True`` 的候选（经历/情绪信号/未完成的事/身体）；
    人设与关系是底色，可以出现在最终 fragments 里，但不能代替生活素材凑数。
    """
    scored: list[ScoredCue] = []
    for candidate in candidates:
        if candidate.as_tuple.is_empty():
            continue
        scored.append(
            ScoredCue(
                cue=candidate,
                score=score_candidate(
                    candidate, actor_id=actor_id, sleep_date=sleep_date, bed_at=bed_at
                ),
            )
        )
    # 先按分数降序、再按 cue_id 升序：同分时顺序也必须稳定，否则重放对不上。
    scored.sort(key=lambda item: (-item.score.total, item.cue.cue_id))

    by_source: dict[str, ScoredCue] = {}
    for item in scored:
        existing = by_source.get(item.cue.source_ref)
        if existing is None or item.score.total > existing.score.total:
            by_source[item.cue.source_ref] = item
    material_sources = {item.cue.source_ref for item in scored if item.cue.material}
    if len(material_sources) < MIN_SOURCES:
        return []

    unique = sorted(by_source.values(), key=lambda item: (-item.score.total, item.cue.cue_id))
    return unique[: max(1, int(max_cues))]


# ---------------------------------------------------------------- 重组算子
#
# 设计 §5：以固定种子跨**至少两段**交换地点或人物角色，再选"时间倒错/
# 物品拟人/场景拼接"之一。算子只能作用在四元组上——没有原文可复制。

CROSS_SOURCE_TRANSFORMS: tuple[str, ...] = ("swap_place", "swap_person")
SURREAL_TRANSFORMS: tuple[str, ...] = ("time_reverse", "object_animation", "scene_splice")
TRANSFORMS: tuple[str, ...] = (*CROSS_SOURCE_TRANSFORMS, *SURREAL_TRANSFORMS)


@dataclass(frozen=True)
class TransformStep:
    """一个重组算子及其作用的碎片（进 prompt 的可审计最小说明）。"""

    transform_id: str
    cue_ids: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "transform_id": self.transform_id,
            "cue_ids": list(self.cue_ids),
            "detail": self.detail,
        }


def _remix_rng(actor_id: str, sleep_date: date) -> random.Random:
    seed = _digest(actor_id, sleep_date.isoformat(), "remix")
    return random.Random(seed)


def plan_transforms(
    cues: list[ScoredCue] | tuple[ScoredCue, ...], *, actor_id: str, sleep_date: date
) -> list[TransformStep]:
    """确定性地选两个算子：一次跨来源交换 + 一次"怪异"变形。

    要求 ``cues`` 至少两源（:func:`select_cues` 已保证）。跨来源交换是硬要求：
    同源内交换等于把同一段经历换个说法，起不到"重混"的作用。
    """
    if len(cues) < MIN_SOURCES:
        raise ValueError("至少要两条候选才能重混")
    rng = _remix_rng(actor_id, sleep_date)
    primary = cues[0]
    partners = [item for item in cues[1:] if item.cue.source_ref != primary.cue.source_ref]
    partner = rng.choice(partners or list(cues[1:]))
    swap = rng.choice(CROSS_SOURCE_TRANSFORMS)
    surreal = rng.choice(SURREAL_TRANSFORMS)

    primary_action = primary.cue.as_tuple.action or "这段经历"
    if swap == "swap_place":
        place = partner.cue.as_tuple.place or "另一个地方"
        swap_detail = f"把「{primary_action}」的地点换成「{place}」"
    else:
        person = partner.cue.as_tuple.person or "另一个人"
        swap_detail = f"让「{person}」出现在「{primary_action}」里"
    surreal_detail = {
        "time_reverse": "把这段场景的时间顺序倒过来（先看到结果，再回到原因）",
        "object_animation": "让场景里的一件物品像人一样行动或说话",
        "scene_splice": "把两个不同来源的场景拼进同一个空间",
    }[surreal]
    pair = (primary.cue.cue_id, partner.cue.cue_id)
    return [
        TransformStep(transform_id=swap, cue_ids=pair, detail=swap_detail),
        TransformStep(transform_id=surreal, cue_ids=pair, detail=surreal_detail),
    ]


# ---------------------------------------------------------------- 威胁分布
#
# 设计 §5 / 调研 §2.3：威胁梦约 1/4 封顶、成功脱身 ≤ 1/3，按角色长期做**软分布
# 约束**，不把单晚写死，也不把"被追"当默认基调。
#
# 实现用"按日期取模 + 角色偏移"而不是每晚掷骰子：
# - 掷骰子会连续多晚出现威胁（违反"约 1/4"的直觉），也无法在测试里断言上限；
# - 取模对任意连续 N 晚给出**恰好** N/4 晚威胁，是真上限而不是期望值；
# - 角色偏移让不同角色不在同一晚集体做梦（仍然是确定性的）。

THREAT_CYCLE_NIGHTS = 4
THREAT_ESCAPE_EVERY = 3


@dataclass(frozen=True)
class ThreatPlan:
    """这一晚允不允许出现威胁场景，以及威胁里能不能脱身。"""

    present: bool
    escape_allowed: bool
    outcome: str  # none / escape / no_escape

    def as_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "outcome": self.outcome,
            "escape_allowed": self.escape_allowed,
        }


def threat_plan(actor_id: str, sleep_date: date) -> ThreatPlan:
    """威胁是否出现（≤1/4 晚）＋这一晚能不能脱身（≤1/3 的威胁晚）。

    这里只是**分布闸门**：它决定"允不允许"，不表示每篇都要写威胁；
    prompt 仍会写明"没有威胁计划时不要凭空加威胁"。
    """
    offset = _digest(actor_id, "threat-cycle") % THREAT_CYCLE_NIGHTS
    index = sleep_date.toordinal() + offset
    if index % THREAT_CYCLE_NIGHTS != 0:
        return ThreatPlan(present=False, escape_allowed=False, outcome="none")
    # 用"第几个威胁周期"决定能否脱身：每 3 个威胁晚有 1 个可脱身 → ≤ 1/3。
    cycle = (index // THREAT_CYCLE_NIGHTS) % THREAT_ESCAPE_EVERY
    escape = cycle == THREAT_ESCAPE_EVERY - 1
    return ThreatPlan(
        present=True,
        escape_allowed=escape,
        outcome="escape" if escape else "no_escape",
    )


# ---------------------------------------------------------------- 复述检测
#
# 结构上 prompt 里没有原文，所以复述本该不可能；这条是**事后防线**：
# 模型若凭训练数据/上下文"猜"出原句，或者我们哪天不小心把原文塞进 prompt，
# 它必须在这里被拒收，而不是被存成"梦到的回忆"。

RECITE_WINDOW_CHARS = 12


def normalize_text(text: str) -> str:
    """去掉空白，只留可比较的字符（标点保留，够用且不引入误伤）。"""
    return "".join(str(text).split())


def recites_source(
    narrative: str, sources: list[str] | tuple[str, ...], *, window: int = RECITE_WINDOW_CHARS
) -> str | None:
    """叙事里是否**逐字**复述了某段素材（≥ ``window`` 字连续相同）。

    返回命中的那段素材（便于测试与日志定位），没有命中返回 ``None``。
    短素材（< ``window``）用"整段出现"判定；太短（< 4 字）直接忽略，否则
    "睡觉"这种通用词会造成大量误伤。
    """
    target = normalize_text(narrative)
    if not target:
        return None
    for source in sources:
        text = normalize_text(source)
        if len(text) < 4:
            continue
        if len(text) < window:
            if text in target:
                return source
            continue
        for start in range(len(text) - window + 1):
            if text[start : start + window] in target:
                return source
    return None


__all__ = [
    "ACTION_LABELS",
    "CROSS_SOURCE_TRANSFORMS",
    "Candidate",
    "CueTuple",
    "DEFAULT_MAX_CUES",
    "MIN_SOURCES",
    "NEGATIVE_EMOTIONS",
    "NEGATIVE_EMOTION_MULTIPLIER",
    "RECENCY_FAR_FACTOR",
    "RECENCY_HALF_LIFE_HOURS",
    "RECITE_WINDOW_CHARS",
    "SURREAL_TRANSFORMS",
    "Score",
    "ScoredCue",
    "THREAT_CYCLE_NIGHTS",
    "THREAT_ESCAPE_EVERY",
    "TRANSFORMS",
    "ThreatPlan",
    "TransformStep",
    "WEIGHT_BODY",
    "WEIGHT_EMOTION",
    "WEIGHT_JITTER",
    "WEIGHT_PERSONA",
    "WEIGHT_RECENCY",
    "WEIGHT_UNFINISHED",
    "action_label",
    "emotion_factor",
    "normalize_text",
    "plan_transforms",
    "recency_factor",
    "recites_source",
    "score_candidate",
    "select_cues",
    "stable_jitter",
    "threat_plan",
]
