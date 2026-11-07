"""Dream 域：有来源的虚构叙事 + 跨夜情绪结算（**纯服务**，无 worker 接线）。

边界（硬规则，别绕过）：

- 梦只写 ``dream_reports``。**绝不**写 ``activities`` / ``self_experiences`` /
  记忆或事实抽取 outbox；``dream_reports.status`` 是**产品记录状态**，
  不是"她做过/没做过"的事实断言。
- 跨夜结算只经 ``AffectService`` 改 ``affect_states`` 的 global 分量；
  梦的成败与结算互不影响。
"""

from ling.domains.dream.fragments import (
    DEFAULT_MAX_CUES,
    MIN_SOURCES,
    Candidate,
    CueTuple,
    Score,
    ScoredCue,
    ThreatPlan,
    TransformStep,
    plan_transforms,
    recency_factor,
    recites_source,
    score_candidate,
    select_cues,
    threat_plan,
)
from ling.domains.dream.service import (
    DreamOutcome,
    DreamService,
    DreamValidationError,
    NightWindow,
    SettlementOutcome,
    SleepSettlementService,
    build_dream_prompt,
    night_window,
    parse_dream_payload,
)

__all__ = [
    "DEFAULT_MAX_CUES",
    "MIN_SOURCES",
    "Candidate",
    "CueTuple",
    "DreamOutcome",
    "DreamService",
    "DreamValidationError",
    "NightWindow",
    "Score",
    "ScoredCue",
    "SettlementOutcome",
    "SleepSettlementService",
    "ThreatPlan",
    "TransformStep",
    "build_dream_prompt",
    "night_window",
    "parse_dream_payload",
    "plan_transforms",
    "recency_factor",
    "recites_source",
    "score_candidate",
    "select_cues",
    "threat_plan",
]
