"""模型路由端口与设置快照契约。

ConfigService 实现本协议；Router 只读生效快照，不读草稿。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from ling.ports.model import SLOT_LABELS, ModelTarget, slot_for_profile


class ResolutionSource(StrEnum):
    """生效模型是怎么来的。

    ``legacy_fallback`` 是**旧版兼容**：这两个职责在槽位体系之前就有"未绑定时
    借主对话模型代跑"的行为（``memory.extract`` 提炼、``dialogue.group`` 群聊）。
    删掉它会让老部署升级后静默降级——提炼变成正则、群里直接不回，而页面上只
    显示"不可用"，用户不知道曾经能用。

    它**不是槽位链的一环**：槽位之间仍然只回退一层（见
    :meth:`EffectiveConfig.resolve_profile` 的规则 4），这一档是另一条轴，
    单独列出来正是为了让界面能说出"这个模型不是你要的，是旧版行为在兜"。
    """

    explicit = "explicit"
    slot = "slot"
    legacy_fallback = "legacy_fallback"
    unavailable = "unavailable"


# 旧版兼容：这两个职责在槽位体系之前就有"未绑定就借主对话模型代跑"的行为。
# 只列这两个，且借的永远是 `dialogue.fast`——**不递归**：如果连
# `dialogue.fast` 自己都解析不出可用模型，就老老实实报不可用。
#
# 为什么必须留着：删掉它，老部署升级后提炼会掉成正则、群聊会直接不回，
# 而页面只显示"不可用"——用户不知道这个能力曾经在。这是"界面说的必须等于
# 实际发生的"的另一面：实际变差了，界面也得如实说出来。
LEGACY_BORROW_FROM: dict[str, str] = {
    "memory.extract": "dialogue.fast",
    "dialogue.group": "dialogue.fast",
}


class BindingMode(StrEnum):
    """职责怎么拿到模型：自己指定，或跟随默认槽位。

    两种状态**只能用一个显式字段表达**，不能靠"`provider_id` 是不是空的"
    来猜：空字符串在这个 schema 里已经有别的含义（"没绑过"），
    再叠一层就会让"没绑"和"跟随默认"再也分不开——而这两件事在界面上
    长得完全不一样（一个是需要处理，一个是正常状态）。
    """

    explicit = "explicit"
    slot = "slot"


@dataclass(frozen=True)
class ProfileBinding:
    """一个职责 profile 的绑定：主模型 + 兼容回退。

    这里**只有"交给谁"**。超时、输出上限、每日额度都属于供应商
    （见 ``ProviderConfig``）：它们是"这家怎么连、这家一天能用多少"，
    不是"这件事该怎么答"。原来把三者放在这里，同一家供应商下的五个职责
    各有各的超时和额度，改一次要改五处，而且默认值会悄悄覆盖供应商的设置。
    """

    profile: str
    provider_id: str
    model_id: str
    fallback_provider_id: str | None = None
    fallback_model_id: str | None = None
    enabled: bool = True
    # ``slot`` = 跟随该职责所属的默认槽位（见 `ports.model.PROFILE_SLOTS`），
    # 此时 ``provider_id`` / ``model_id`` 不参与解析。
    #
    # 默认 ``explicit``：旧修订里没有这个键，而它们全都是显式绑定，
    # 所以"缺键 = 显式"让旧配置一字不改地继续生效。
    mode: BindingMode = BindingMode.explicit

    @property
    def follows_slot(self) -> bool:
        return self.mode is BindingMode.slot


@dataclass(frozen=True)
class ModelSlot:
    """一个默认槽位：`(provider_id, model_id)`。

    槽位和职责绑定是**同一层的东西**（都只是"交给谁"），所以不放密钥、
    不放超时——那些跟着供应商走。
    """

    slot: str
    provider_id: str = ""
    model_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.provider_id and self.model_id)


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
    # 每小时**恢复**的精力：只在精力低到"该歇了"的时候生效。
    #
    # 没有这一项的话，精力只减不增，跑到一定小时数就永远是 resting，
    # 于是她**永久不接话**——表现是"她突然不理我了"，日志里却只有
    # 一条正常的 `skip_code=resting`。（实测开发库跑到第三天精力 18，
    # 私聊被静默。）有了恢复项，状态变成一个循环：累了→歇→回血→能聊。
    energy_recover_per_hour: float = 8.0
    # 每轮对话增加的社交负担。
    social_load_per_turn: float = 3.0
    # 每小时恢复的社交负担。
    social_load_decay_per_hour: float = 6.0
    # 同时最多挂几件小事。排太多会让"在忙"变成常态。
    max_live_plans: int = 4


VoiceWillingness = Literal["reserved", "natural", "expressive"]

VoiceKind = Literal["minimax_tts", "fishaudio_tts", "mimo_tts"]
"""可以配**音色**的三家。取值与 `ports/model.ProviderKind` 逐字一致。

**没有统一的 `voice_key`**（§17.2 撤销了跨厂商映射表）：同一个"温柔"在
三家是三种完全不同的东西。所以 `voice_kind` 只回答"这套音色参数是给谁的"，
不承担"把一家的音色翻译成另一家的"那种职责——那件事代码不替用户做。
"""

VOICE_KINDS: frozenset[str] = frozenset({"minimax_tts", "fishaudio_tts", "mimo_tts"})
"""她愿不愿意开口说话的三个**语义档**（§17.4）。

用 `Literal` 而不是 `StrEnum`：这个值要原样进配置 JSON、进 API 校验、
进前端联合类型，用字符串字面量能让 pydantic 的 `Literal` 直接做"非法值拒绝"
（§17.4 要求「`DialoguePolicyIn` 同值校验」）。枚举成员在序列化时要多一层
转换，而这个字段恰恰是"必须贯穿全链路"的那个——少一层转换少一处出错点。
"""


# 三档各自注入提示词的**确定文本**（§17.4 原文，逐字）。
#
# 为什么是"整句话"而不是枚举值：档位要影响她的表达，唯一可靠的落地方式就是
# 把倾向说进提示词。放成一张表而不是散在拼提示词的各处，是为了让
# "这几档到底告诉了模型什么"能一次看全、能被逐字测试。
#
# 三条共同的底线（都写在文本里，不靠代码强制）：
# 1. 任何档位都允许她选择任一种表达——reserved 不是"禁止语音"，
#    expressive 也不是"必须语音"。
# 2. 对方的明确语境（"请文字" / "不方便听" / "读给我听"）优先于默认倾向，
#    由她理解，不靠关键词强制器替她决定。
# 3. 不为了用语音而改变要说的话。
VOICE_WILLINGNESS_PROMPTS: dict[VoiceWillingness, str] = {
    "reserved": (
        "你通常偏向文字。对方希望听你说、安慰或情绪表达用声音更合适时，"
        "可以自然选择语音；无需为用语音凑理由。"
    ),
    "natural": (
        "你根据当下交流自然选择文字或语音。短句、亲近交流或声音更能传达情绪时可用语音；"
        "需要复制、列信息或不方便收听时偏向文字。"
    ),
    "expressive": (
        "在适合听语音的情境下，你更愿意开口说话。仍需顾及对方是否方便、内容是否适合阅读；"
        "不要每条都发语音，也不必为了使用语音改变要说的话。"
    ),
}


@dataclass(frozen=True)
class VoiceSetting:
    """她要用的**音色**。设置页「⑤ 对话与主动性」里的一段。

    ## 为什么是"共用字段 + 一个 kind"，而不是三家的联合对象

    三家的音色形状完全不同（``MiniMaxVoice`` 有 speed/vol/pitch/emotion，
    ``FishAudioVoice`` 只有一个 ``reference_id``，``MimoReferenceVoice`` 要
    **参考音频字节**）。运行时当然要变成那三个值对象之一，但**配置里不能**：

    - MiMo 的 ``AudioInput`` 是已授权、已解码的字节。存进配置库会让配置体积
      失控，还会把"授权"变成"字节还在就算有权限"（`ports/speech.py` 的原话）。
      所以配置里只存 ``reference_asset_id``，运行时再去媒体服务换字节。
    - 三家的参数并集很小（就下面这几个），拆成三个嵌套 dict 只会让"缺哪个"
      更难被单独表达——一个 ``None`` 就能说清"这套里的这一项没填"。

    ## 每个字段各自缺失，各自表达

    ``kind`` 为空 = 还没选音色（不是"用默认嗓音"，运行时会降级发文字）。
    ``voice_id`` / ``reference_asset_id`` 谁该填由 ``kind`` 决定（见
    `voice_requirement_issue`）；speed/vol/pitch 只为 MiniMax 存在，
    填了别的家也不报错——它们对那家没有意义，运行时也不会发出去。
    """

    kind: VoiceKind | None = None
    voice_id: str | None = None
    """MiniMax 的 ``voice_id``，或 FishAudio 的 ``reference_id``。

    一个字段装两种 id 是有意的：它们都是"厂商那边一个已有音色的字符串"，
    由 ``kind`` 决定它是哪一种。分两个字段会让"填错了格子"变成静默忽略。
    """
    reference_asset_id: str | None = None
    """MiMo 的参考素材 id（`media_assets.digest`）。**不是字节**。"""
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0
    emotion: str | None = None
    """默认 None = 不发。猜一个 emotion 会让语音听起来莫名其妙。"""


def voice_requirement_issue(voice: VoiceSetting) -> str | None:
    """这套音色还缺什么。返回**给用户直接看的中文**，或 ``None`` 表示可用。

    这是"缺哪个"的**唯一判定**：设置页的校验（`settings/service.py`）与
    运行时的降级（`worker._voice_for_tts`）都调它，所以不会出现"保存时说过
    没问题、运行起来降级"这种两套规则。

    ``kind`` 为空时返回 ``None``：那是"还没配"，不是"配错了"——校验不该逼
    每个用户都先选一个音色，而降级由运行时按"没有音色"处理。
    """
    kind = voice.kind
    if kind is None:
        return None
    if kind not in VOICE_KINDS:
        return f"未知的音色类型：{kind}（支持：{'、'.join(sorted(VOICE_KINDS))}）"
    if kind == "mimo_tts":
        if not (voice.reference_asset_id or "").strip():
            return "音色类型是 mimo_tts，但没填参考音频（reference_asset_id）"
        return None
    if not (voice.voice_id or "").strip():
        label = "voice_id" if kind == "minimax_tts" else "reference_id"
        return f"音色类型是 {kind}，但没填 {label}"
    return None


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
    # 免打扰窗（当天分钟数）。**默认关闭**：起点等于终点时恒不成立
    # （见 domains/proactive/service.py 的 `_in_quiet_hours`）。
    #
    # 为什么不给个"看起来合理"的默认值：免打扰是**替用户决定**"她不该在
    # 这时候说话"，而这恰恰是人格该有的判断力——她说一句"这么晚还没睡？"
    # 也是陪伴。默认给个 23:00-08:00 反而会让调试时莫名其妙没有主动消息，
    # 且用户未必想被代码替他划这条线。要静音，自己填一个窗口即可。
    quiet_start_minute: int = 0
    quiet_end_minute: int = 0

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

    group_reply_delivery_enabled: bool = False
    """群聊里「决定回了」之后是否**真的生成并发送**。

    默认 False：只做决策与记录（`decision=replied` 表示"她决定回"，
    不等于"已经发出去了"），不花生成调用、不在群里开口。
    打开后才走「生成 → 投递」，投递失败会如实记成 `reply_failed`。

    与 ``proactive_delivery_enabled`` 同一条理由：一旦真的发出去，
    群里的人就看见了——所以默认关，由用户自己打开。
    """

    rest_may_skip: bool = True
    """她在休息时，这条消息是否可以先不进入决策（"没空回"）。

    默认开启——用户要求"忙/休息时没空回消息"。
    紧急内容（求助、出事）永远不受此影响。
    """

    tools_enabled: bool = False
    """她能不能在**回复的当场**调用工具（同轮回灌）。

    **默认 False**，与 ``proactive_delivery_enabled`` 同一条理由：打开它就
    等于把所有普通对话塞进一条新路径——每次请求多一个 ``tools`` 字段、多一种
    可能的 400。`kernel/turn.py` 从落地起就是默认关（见那里的注释），本字段
    是给它补的**显式设置项**，不是改默认。

    打开之后仍然**不一定**有工具：`kernel/turn.py::_tool_ledger_for_turn`
    还要过角色白名单、适配器 kind、实例声明、清单四道闸，每一道失败都带着
    自己的 ``reason``（``no_registry`` / ``profile_not_wired`` /
    ``kind_has_no_native_tools`` / ``instance_has_no_native_tools`` /
    ``no_model_tools``）。所以"开了却没反应"在日志里查得到是哪一环。

    这一项只影响**同一轮**里的工具调用。不在任何工具白名单里的工具
    （``shell.readonly`` / ``message.recall``）即使打开也不会被模型看到——
    见 ``worker.py`` 的注册表构造与 `kernel/turn.py` 的 ``tool_allowlist``。
    """

    voice_willingness: VoiceWillingness = "natural"
    """她**愿不愿意开口说话**的倾向档位。设置页「⑤ 对话与主动性」。
    设计文档 §17.4。

    **这是"倾向"，不是概率。** 用户的原话是「倾向，提供几个档位选择」——
    要档位，不要数字滑块。所以：

    - 三档之间**没有** 0.3 / 0.7 这样的映射；
    - **不抽随机数**，不设"每 N 条必须语音"，也不统计命中比例来判定成败；
    - 每档都只转成一段**提示词倾向**（见 `VOICE_WILLINGNESS_PROMPTS`），
      由她自己结合情境决定这一条说还是打。

    谁决定：**她自己**，在每次最终回复里用开头的 `[speak]` 表达。
    确定性代码只负责能力、授权、预算、投递可行性——不替她抛硬币。
    她选了语音之后，执行层可以因为不可用/超预算/没权限而拒绝
    （记 `voice_rejected`），但**不能**强制改成发声或改成文字。

    默认 ``natural``：不配置时行为与"她现在这样"最接近，
    也不会把她突然变成"每句都发语音"。
    """

    voice: VoiceSetting = field(default_factory=VoiceSetting)
    """她要用的音色。

    默认空 = 还没配。**代码不替她选嗓音**：没有音色时 `[speak]` 降级发文字，
    日志里说清缺的是哪一环（见 `worker._voice_for_tts`）。给一个"听起来还行"
    的默认值，会让"配错了"和"没配"在用户那里长得一模一样。
    """

    @property
    def quiet_start(self) -> tuple[int, int]:
        return divmod(self.quiet_start_minute, 60)

    @property
    def quiet_end(self) -> tuple[int, int]:
        return divmod(self.quiet_end_minute, 60)

    def voice_prompt(self) -> str:
        """本档位要注入提示词的那段确定文本（§17.4）。

        这是 `voice_willingness` 的**唯一消费入口**：档位不是概率，落地的
        方式就是一段话告诉她"你倾向于怎么说话"。其余全部交给她判断。

        返回的是**固定文本**，可按档位逐字断言——这样"改了文案"和
        "改了行为"能被区分开。
        """
        return VOICE_WILLINGNESS_PROMPTS[self.voice_willingness]


@dataclass(frozen=True)
class ProviderModel:
    """供应商目录里的一个模型。

    这是**给人看的清单**，不是路由白名单：``enabled=False`` 的模型仍然
    可以被旧修订里的职责绑定，路由照旧会调它——但在设置页里它不会再被
    推荐、也不能被新绑定选中。要真正停掉一个模型，请把绑定改到别的模型。

    ``note`` 是自由文本（例如「上下文 128K · 支持图片」），服务端不解析；
    它只用来在设置页解释"这个模型是干什么的"。
    """

    model_id: str
    note: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"model_id": self.model_id, "note": self.note, "enabled": self.enabled}


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    kind: str  # fake | openai_compatible | anthropic | chat_responses | local
    base_url: str | None = None
    has_secret: bool = False
    secret_last4: str = ""
    enabled: bool = True
    extra: dict = field(default_factory=dict)
    models: tuple[ProviderModel, ...] = ()
    # ---------------------------------------------------- 传输与额度
    #
    # 这三项**属于供应商，不属于职责**。原来的位置是 `ProfileBinding`，
    # 结果是同一家供应商下的五个职责各有一份超时和额度，改一次要改五处，
    # 而它们本来就该是"这家怎么连、这家一天能用多少"。
    #
    # 温度不在这里：它已经交给上游模型的默认值（`ModelRequest.temperature=None`
    # 时不发这个字段）。留一个只影响措辞、不影响成本的旋钮，比没有更糟。
    timeout_seconds: float = 60.0
    # 单次请求的输出上限。-1（或 None）= 不限制，不往请求里放 max_tokens。
    max_output_tokens: int | None = None
    # 每日 token 额度。-1（或 None）= 不限制。超了之后这一家不再被调用，
    # 见 `ling.models.quota`。
    daily_token_limit: int | None = None

    def max_output_effective(self) -> int | None:
        """把 -1 / None 归一成"不限制"（返回 None）。"""
        if self.max_output_tokens is None or self.max_output_tokens < 0:
            return None
        return self.max_output_tokens or None

    def daily_limit_effective(self) -> int | None:
        if self.daily_token_limit is None or self.daily_token_limit < 0:
            return None
        return self.daily_token_limit or None


@dataclass(frozen=True)
class ResolvedProfile:
    """一个职责**最终会跑在哪个模型上**，以及这个结论是怎么来的。

    这是全系统唯一的裁决结果：设置页显示它、路由调用它、worker 判断
    "能不能提炼"也看它。任何地方再算一遍"那它到底用哪个"，
    迟早会和这里不一致——界面上写着 A、实际跑着 B，是这个仓库反复踩过的坑。

    ``source`` 四种取值：

    - ``explicit``：用用户单独指定的那个 `(provider, model)`；
    - ``slot``：跟随时用它所属槽位的模型（也包括"单独指定的那个坏了，
      于是落回槽位"——同一条路，只是 `requested` 有值、界面标成「已回退」）；
    - ``legacy_fallback``：旧版兼容，借主对话模型代跑（只有 ``memory.extract``
      和 ``dialogue.group`` 会走到这里，见 :data:`LEGACY_BORROW_FROM`）；
    - ``unavailable``：没有可用模型，``reason`` 说明是哪一步断的。
    """

    profile: str
    source: ResolutionSource
    provider_id: str | None = None
    model_id: str | None = None
    # 该职责所属的槽位（没有默认槽位的角色为 None）。
    slot: str | None = None
    # 用户单独指定过什么。只有"指定了但没用上"时才非空——界面靠它把
    # 「继承」（从来没单独选过）和「已回退」（选了，但那个坏了）分开。
    requested_provider_id: str | None = None
    requested_model_id: str | None = None
    # ``unavailable`` 的具体原因。**给用户直接看的中文**，不要 "ERR_X"。
    reason: str = ""
    # 这个职责是否被启用。界面上「已停用」和「不可用」是两回事：
    # 前者是用户主动关的（别去"修"它），后者是配置断了（该去补）。
    enabled: bool = True

    @property
    def usable(self) -> bool:
        return self.source is not ResolutionSource.unavailable

    @property
    def fell_back(self) -> bool:
        """单独指定过、但最后没用上——界面上要标「已回退」。"""
        return self.source is ResolutionSource.slot and self.requested_provider_id is not None


@dataclass(frozen=True)
class EffectiveConfig:
    """一个 config revision 的生效快照。一次轮次固定用它。"""

    revision: int
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    profiles: dict[str, ProfileBinding] = field(default_factory=dict)
    # 四个默认槽位，键是 `ProviderSlot` 的值。缺键 = 那个槽位没配。
    slots: dict[str, ModelSlot] = field(default_factory=dict)
    test_mode: bool = False
    # 入站白名单拒绝是否逐条写日志。**只影响日志量，不影响拒绝本身**：
    # `check_access` 照常裁决，被拒消息照常不进对话链路，拒绝次数由进程内
    # 计数器单独累加（`GET /v1/logs` 与 `GET /v1/onebot/health` 暴露）。
    #
    # 与 `test_mode` 一样是**实例级**开关（不属于记忆/生活/对话任何一个领域），
    # 所以放在顶层。默认 True = 改动前的行为。
    #
    # 为什么在配置修订里而不是纯环境变量：设置页的开关必须是**真的**——
    # 按了没反应、或者要重启才生效的开关，比没有这个开关更糟。环境变量
    # `LING_LOG_ACCESS_DENIALS` 提供的是"还没有任何已发布修订时"的默认值
    # （见 `Settings.log_access_denials`），与 `test_mode` 同一个模式。
    log_access_denials: bool = True
    # **当前激活的人格**（`personas.id`）。这是"同一时间只有一套人格生效"的
    # 唯一真相来源：所有取值点都经由
    # `domains/persona/current.py::effective_persona_id` 读它，
    # 不要在各模块里再写一个写死的 id（原来 13 处硬编码 ``"persona_ling"``
    # 就是"设置改了、运行没改"的来源）。
    #
    # 默认值保持 ``"persona_ling"`` 只为兼容：没有已发布修订时（新装），
    # `ConfigService.effective()` 用 `LING_PERSONA_ID` 覆盖它；而新装又没有
    # `persona_ling` 这一行，于是"指向一个不存在的 id" = 暂无人设，
    # 该行为与老库里有这一行的情况一致（有行就有人格）。
    persona_id: str = "persona_ling"
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    life: LifePolicy = field(default_factory=LifePolicy)
    dialogue: DialoguePolicy = field(default_factory=DialoguePolicy)

    def binding(self, profile: str) -> ProfileBinding | None:
        return self.profiles.get(profile)

    # ------------------------------------------------------------ 可用性

    def is_usable(self, provider_id: str | None, model_id: str | None) -> bool:
        """**「可用」的定义**：供应商 ``enabled`` **且** 模型 ``enabled``。

        全系统只认这一个判据。设置页的绿点、槽位能不能用、回退要不要触发，
        全都是它——各写一份的结果是"界面说能用、调用报 provider 已停用"。

        两个刻意的例外，都是为了让旧配置继续工作：

        - 模型 ID 不在该供应商的清单里时按**可用**处理。清单是给人看的目录，
          不是路由白名单（见 ``ProviderModel`` 的注释）：把 id 写进绑定却忘了
          加进目录，是常见状态，路由一直照调不误。判成"不可用"会让设置页
          凭空红一片，而实际调用完全正常。
        - 供应商不存在 / 被停用 / 模型被明确标了 ``enabled=False`` → 真的不可用。
        """
        if not provider_id or not model_id:
            return False
        cfg = self.providers.get(provider_id)
        if cfg is None or not cfg.enabled:
            return False
        for model in cfg.models:
            if model.model_id == model_id:
                return model.enabled
        return True

    def slot_of(self, profile: str) -> str | None:
        key = slot_for_profile(profile)
        return key.value if key is not None else None

    def resolve_profile(self, profile: str) -> ResolvedProfile:
        """职责 → 最终生效的模型。**槽位之间只回退一层，不允许套娃。**

        判定顺序（每一步都写在这里，没有别处再判一遍）：

        1. 单独指定了、且那个 `(provider, model)` 可用 → 用它（``explicit``）。
        2. 否则用它**所属槽位**的模型（``slot``）。单独指定的坏了也走这一格。
        3. ``memory.extract`` / ``dialogue.group`` 在 1、2 都落空时，
           **借主对话模型代跑**（``legacy_fallback``）——这是它们在槽位体系
           之前就有的行为，删掉会让老部署静默降级。
        4. 仍然没有 → ``unavailable``，``reason`` 说清断在哪。
        5. **到此为止**。不会再去试别的槽位。

        第 5 条是硬约束，不是偷懒：允许槽位之间二级跳转的话，用户看到的
        "生效模型"会依赖一条他看不见的链条，出问题时既没法解释也没法复现。

        注意第 3 条**不是**槽位链的一环，是另一条轴：它只对两个具名职责生效、
        只借 `dialogue.fast`、且不递归（主对话自己都解析不出来就报不可用）。
        用独立的 ``legacy_fallback`` 标出来，界面才能说清"这个模型不是你要的，
        是旧版行为在兜"。

        ``mode=slot`` 的绑定直接跳过第 1 步：它明确表示"我要跟着槽位走"，
        此时 ``provider_id`` / ``model_id`` 里的残留值不该有任何作用。
        """
        resolved = self._explicit_or_slot(profile)
        # 职责被用户主动停用是一个独立决定，不该被兼容逻辑"修好"。
        if resolved.source is not ResolutionSource.unavailable or resolved.enabled is False:
            return resolved

        donor_profile = LEGACY_BORROW_FROM.get(profile)
        if donor_profile is None:
            return resolved
        donor = self._explicit_or_slot(donor_profile)
        if donor.source is ResolutionSource.unavailable or not donor.model_id:
            # 连兜底的那个角色都没有可用模型 —— 老实报不可用，不再往下借。
            return resolved
        return ResolvedProfile(
            profile=profile,
            source=ResolutionSource.legacy_fallback,
            provider_id=donor.provider_id,
            model_id=donor.model_id,
            slot=resolved.slot,
            requested_provider_id=resolved.requested_provider_id,
            requested_model_id=resolved.requested_model_id,
            reason=(
                f"没配模型，暂时借「{donor_profile}」的模型代跑"
                "（旧版行为；给它配一个槽位或单独指定后就不再借用）"
            ),
        )


    def _explicit_or_slot(self, profile: str) -> ResolvedProfile:
        """显式 → 槽位这两步（**不含**旧版兼容那一档）。

        判定顺序（每一步都写在这里，没有别处再判一遍）：

        1. 单独指定了、且那个 `(provider, model)` 可用 → 用它（``explicit``）。
        2. 否则用它**所属槽位**的模型（``slot``）。单独指定的坏了也走这一格。
        3. 槽位没配 / 不可用 → ``unavailable``，``reason`` 说清断在哪。
        4. **到此为止**。不会再去试别的槽位、也不会借另一个角色的模型。

        第 4 条是硬约束，不是偷懒：允许二级跳转的话，用户看到的"生效模型"
        会依赖一条他看不见的链条，出问题时既没法解释也没法复现。宁可报
        「不可用」让他去补一个槽位，也不要猜他想用哪个。

        ``mode=slot`` 的绑定直接跳过第 1 步：它明确表示"我要跟着槽位走"，
        此时 ``provider_id`` / ``model_id`` 里的残留值不该有任何作用。
        """
        binding = self.profiles.get(profile)
        slot_key = slot_for_profile(profile)
        slot_name = slot_key.value if slot_key is not None else None
        slot = self.slots.get(slot_name) if slot_name is not None else None

        # 职责被停用是一个**独立决定**，不是"不可用"：用户主动关掉它，
        # 不该被任何回退"修好"。
        if binding is not None and not binding.enabled:
            return ResolvedProfile(
                profile=profile,
                source=ResolutionSource.unavailable,
                slot=slot_name,
                reason="这个职责被停用了：在「职责绑定」里重新打开它",
                enabled=False,
            )

        # 只有 `mode=explicit` 的绑定才参与第 1 步。`mode=slot` 的绑定里
        # 即使残留着 model_id 也不该有任何作用（那是它切过来之前的样子）。
        explicit = (
            binding
            if binding is not None and binding.mode is BindingMode.explicit
            else None
        )
        requested_provider_id = explicit.provider_id if explicit else None
        requested_model_id = explicit.model_id if explicit else None

        # 1. 单独指定，且可用。
        if explicit is not None and self.is_usable(
            explicit.provider_id, explicit.model_id
        ):
            return ResolvedProfile(
                profile=profile,
                source=ResolutionSource.explicit,
                provider_id=explicit.provider_id,
                model_id=explicit.model_id,
                slot=slot_name,
            )

        # 2. 该职责的槽位。**只有这一个**，不再往下找。
        if slot is not None and self.is_usable(slot.provider_id, slot.model_id):
            return ResolvedProfile(
                profile=profile,
                source=ResolutionSource.slot,
                provider_id=slot.provider_id,
                model_id=slot.model_id,
                slot=slot_name,
                requested_provider_id=requested_provider_id,
                requested_model_id=requested_model_id,
            )

        # 3. 到这里就没有了。
        label = SLOT_LABELS[slot_key] if slot_key is not None else ""
        if slot_name is None:
            reason = "这个职责只能单独指定模型：它没有默认槽位"
        elif slot is None or not slot.configured:
            reason = f"「{label}」槽位还没有配模型"
        else:
            reason = (
                f"「{label}」槽位配的 {slot.provider_id} · {slot.model_id} 不可用"
                "（供应商或模型被停用了）"
            )
        if explicit is not None and explicit.provider_id:
            reason += "，你单独指定的模型也不可用"
        return ResolvedProfile(
            profile=profile,
            source=ResolutionSource.unavailable,
            slot=slot_name,
            requested_provider_id=requested_provider_id,
            requested_model_id=requested_model_id,
            reason=reason,
        )


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
    "BindingMode",
    "ConfigReader",
    "DialoguePolicy",
    "EffectiveConfig",
    "LifePolicy",
    "MemoryPolicy",
    "ModelRouter",
    "ModelSlot",
    "ProfileBinding",
    "ProviderConfig",
    "ResolvedProfile",
    "ResolutionSource",
]
