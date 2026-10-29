"""模型路由端口与设置快照契约。

ConfigService 实现本协议；Router 只读生效快照，不读草稿。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ling.ports.model import ModelTarget


@dataclass(frozen=True)
class ProfileBinding:
    """一个职责 profile 的绑定：主模型 + 兼容回退。"""

    profile: str
    provider_id: str
    model_id: str
    fallback_provider_id: str | None = None
    fallback_model_id: str | None = None
    enabled: bool = True
    max_tokens: int | None = None
    temperature: float = 0.8
    timeout_seconds: float = 60.0
    daily_cost_limit: float | None = None


@dataclass(frozen=True)
class MemoryPolicy:
    """记忆的召回与保留策略。

    这些原来是模块级常量（``service.py`` 里的 ``RELATIVE_SCORE_FLOOR`` 等）。
    抽成策略对象是因为它们是**用户会想调的东西**：觉得角色记性太好/太差、
    翻旧账太频繁，都该能在设置里改，而不是改代码重部署。

    默认值 = 原来的常量，所以不配置时行为完全不变。
    """

    # 相对分数下限：低于此比例的召回项被丢掉。调高 = 只想起最相关的。
    relative_score_floor: float = 0.35
    # 重排头部条数。调高 = 更准但更慢更贵。
    rerank_top_n: int = 20
    # 时间衰减半衰期（天）。调低 = 更偏向近期。
    recency_half_life_days: float = 120.0
    # 记忆最长保留天数。超过的必被硬门挡掉。
    max_retention_days: int = 3650


@dataclass(frozen=True)
class LifePolicy:
    """角色的生活节奏：场景能挂多久、精力掉多快、社交负担恢复多快。

    这些原来是 ``presence/service.py`` 与 ``agenda/service.py`` 里的模块级常量。
    抽出来是因为它们直接决定「像不像人」——精力掉太快显得虚弱，
    社交负担恢复太慢会一直不想说话。用户应该能调，而不是改代码。

    默认值 = 原常量，不配置时行为完全不变。

    **不包含作息（几点醒/几点睡）**：那已经按 actor 存在
    ``actor_routines`` 表里，再在配置里放一份就是第二份权威，
    两边不一致时没人说得清以谁为准。
    """

    # 一次场景状态的有效期（分钟）。过了就回到 unknown。
    scene_ttl_minutes: int = 15
    # 每小时消耗的精力（0-100 量表）。
    energy_decay_per_hour: float = 1.5
    # 每轮对话增加的社交负担。
    social_load_per_turn: float = 3.0
    # 每小时恢复的社交负担。
    social_load_decay_per_hour: float = 6.0
    # 同时最多挂几件小事。排太多会让"在忙"变成常态。
    max_live_plans: int = 4


@dataclass(frozen=True)
class DialoguePolicy:
    """对话节奏与主动联系的门槛。

    两组参数分别对应「要不要接话」和「要不要主动找你」：

    - ``group_turn_budget``：群聊里角色近期已发言几次就不再插话。调大 = 更爱说。
    - ``proactive_daily_limit``：每天最多主动联系几次。调大会变成骚扰。
    - ``quiet_*_minute``：免打扰时段（按**用户**时区）。跨午夜是常态，
      所以用"起止分钟"而不是排序好的区间。

    默认值 = 原常量，不配置时行为完全不变。
    """

    group_turn_budget: int = 3
    proactive_daily_limit: int = 2
    # 免打扰起点（当天分钟数）。1380 = 23:00
    quiet_start_minute: int = 1380
    # 免打扰终点。480 = 08:00
    quiet_end_minute: int = 480

    # ---------------------------------------------------------------- 自主度
    # 这两项控制「她能不能自己决定不回」。默认值 = 现在的行为（必回），
    # 所以打开这个功能不会改变任何既有表现——要主动打开才会更自主。
    #
    # 为什么要做成开关而不是直接改默认：被 @ 了却不理你，在私人助手
    # 场景下可能是不能接受的。让使用者自己决定这个风险。
    private_may_stay_silent: bool = True
    """私聊里她能否选择不回应。

    **默认 True**：用户明确要求"人聊天不是句句有回应，私聊的沉默可真沉默"。
    关闭它会退回"私聊必回"的旧行为。沉默时消息仍然落库、仍然被她"看见"。
    """

    group_mention_requires_reply: bool = True
    """群里被 @ / 叫名字时是否必须回。默认必须。关掉之后 @ 只是强倾向。"""

    group_attention_deferred: bool = True
    """群里的**普通**消息是否先不即时决策（等她自己"刷到"再看）。

    默认开启——用户要求"人不是整天都看着群，有人 @ 就看一下，
    或者刷到刚好就看一下"。被 @ / 叫名字 / 紧急内容仍然立刻处理。
    """

    proactive_delivery_enabled: bool = False
    """主动联系是否**真的发出去**。

    默认 False：前面几步（收集、门控、生成草稿）都能在影子模式下验证，
    但投递一旦发生就是"她突然找你"。所以默认只生成不发送，
    用户先看几天草稿、确认语气合适再自己打开。
    """

    rest_may_skip: bool = True
    """她在休息时，这条消息是否可以先不进入决策（"没空回"）。

    默认开启——用户要求"忙/休息时没空回消息"。
    紧急内容（求助、出事）永远不受此影响。
    """

    @property
    def quiet_start(self) -> tuple[int, int]:
        return divmod(self.quiet_start_minute, 60)

    @property
    def quiet_end(self) -> tuple[int, int]:
        return divmod(self.quiet_end_minute, 60)


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    kind: str  # fake | openai_compatible | local
    base_url: str | None = None
    has_secret: bool = False
    secret_last4: str = ""
    enabled: bool = True
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EffectiveConfig:
    """一个 config revision 的生效快照。一次轮次固定用它。"""

    revision: int
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    profiles: dict[str, ProfileBinding] = field(default_factory=dict)
    test_mode: bool = False
    persona_id: str = "persona_ling"
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    life: LifePolicy = field(default_factory=LifePolicy)
    dialogue: DialoguePolicy = field(default_factory=DialoguePolicy)

    def binding(self, profile: str) -> ProfileBinding | None:
        return self.profiles.get(profile)


@runtime_checkable
class ConfigReader(Protocol):
    async def effective(self) -> EffectiveConfig: ...


@runtime_checkable
class ModelRouter(Protocol):
    async def resolve(
        self, profile: str, *, require_stream: bool = True, allow_fallback: bool = True
    ) -> ModelTarget: ...

    async def stream(self, target: ModelTarget, request: object): ...


__all__ = [
    "ConfigReader",
    "DialoguePolicy",
    "EffectiveConfig",
    "LifePolicy",
    "MemoryPolicy",
    "ModelRouter",
    "ProfileBinding",
    "ProviderConfig",
]
