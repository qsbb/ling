"""模型端口（port）。领域与内核只依赖这里的协议，不依赖任何供应商 SDK。

关键约束（设计文档 §8）：
- 一次轮次固定一个 config revision 与一个 model target。
- 流式失败要保留已交付部分，不能用新模型重放已执行的工具。
- 供应商密钥只在服务端；这里只接收已解密的内存字符串。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderKind(StrEnum):
    fake = "fake"
    openai_compatible = "openai_compatible"
    # Anthropic 原生 Messages API（``/v1/messages``）。与 OpenAI 兼容接口
    # 的差别不只是路径：请求体、SSE 事件名、system 的位置、图片编码都不同，
    # 所以必须是一个独立适配器，而不是给兼容接口换个 base_url 就能顶替。
    anthropic = "anthropic"
    # OpenAI 的 Responses API（``/v1/responses``）。同样不是 /chat/completions
    # 的别名：事件流、输入结构、用量字段都不同。
    chat_responses = "chat_responses"
    local = "local"
    # ---------------------------------------------------- 非对话能力
    #
    # 这两个**不是** chat 协议的别名，也不是"给兼容接口换个 base_url"：
    # 请求体与返回结构完全不同（见 `models/cloudflare_embedding.py` 与
    # `models/siliconflow_rerank.py` 的模块注释）。放进同一个下拉里，
    # 用户会选出"发布成功、调用必失败"的组合，所以它们各自带**能力标记**，
    # 由绑定按能力过滤（见 `PROVIDER_CAPABILITIES`）。
    cloudflare_embedding = "cloudflare_embedding"
    siliconflow_rerank = "siliconflow_rerank"
    # ---------------------------------------------------- 语音合成（TTS）
    #
    # 三家**真实厂商**，各自一套 wire 协议，不能靠换 base_url 顶替：
    #
    # - ``minimax_tts``：MiniMax 专用 TTS 端点，JSON 进、hex 音频出。
    #   用户口语里叫「mimotts」，那只是叫法，不是另一个供应商。
    # - ``fish_audio``：FishAudio 专用端点，JSON 进、二进制音频出，
    #   音色用 reference_id，模型名走 header。
    # - ``mimo_chat`` / ``mimo_tts``：小米 MiMo，**同一个厂商、同一个
    #   base_url（``https://api.xiaomimimo.com/v1``），但拆成两个 kind**。
    #   理由见下方长注释——这是本模块最容易被误解的一处。
    minimax_tts = "minimax_tts"
    fishaudio_tts = "fishaudio_tts"
    """FishAudio。**对外稳定值用 `fishaudio_tts`**（§17.2 定名）。

    曾经写成 `fish_audio`，与**文件名** `models/fish_audio.py` 同形，但
    "文件名不必为此改名"，对外配置值统一成 `fishaudio_tts`。旧的
    `fish_audio` 保留在 `LEGACY_PROVIDER_KIND_ALIASES` 里做读取兼容——
    **不能同时发布两个同义 kind**，那会让设置页出现两个都能选、
    配出来行为一样的条目。
    """
    # MiMo 走的是 ``chat/completions``，不是专用 TTS 端点。所以它天然能
    # 同时承担「对话」和「合成」两种角色——但**绝不允许一个 kind 同时声明
    # 两种能力**。
    #
    # 为什么必须拆：`PROVIDER_CAPABILITIES` 是绑定校验的**唯一**能力闸
    # （`kind_supports_profile` 只看它）。若 `mimo` 同时持 `{chat, tts}`，
    # 那么 `speech.tts` 绑到它就会通过校验、`dialogue.fast` 绑到它也会通过，
    # 「把 TTS 供应商塞进聊天槽位」这类错配就再也拦不住——fail-closed 原则
    # 直接作废。能力表能表达的是"这个 kind 能服务哪些角色"，而"同一个人
    # 既能聊天又能合成"是**配置层面**的事实，应该用两个 provider 条目表达，
    # 不是一个 kind 带两个能力。
    #
    # 所以：用户在设置里加两条 provider，`base_url` 相同、`kind` 不同。
    # 好处是两类流量的密钥、健康状态、计费计量天然分开，也不会有人在聊天时
    # 误触发合成或反之。代价只是用户要多配一条——比"错绑拦不住"便宜得多。
    mimo_chat = "mimo_chat"
    mimo_tts = "mimo_tts"


class ModelCapability(StrEnum):
    """模型角色**需要**什么能力，也是供应商**提供**什么能力。

    一个 kind 声明一组能力，一个角色声明需要一种；设置页用
    `capabilities(kind) ⊇ {role.capability}` 过滤下拉，服务端用同一条
    判据拦住调不通的绑定（`settings/service.py::validate_payload`）。

    为什么要这层而不是"按 kind 手写 if"：kind 与角色的对应关系一旦散落
    在界面、校验、装配三处，加一个供应商就要改三处，漏一处就是静默错配。

    ``tts`` 现在**已有三家真实供应商**提供（见 `ProviderKind.minimax_tts` /
    `fish_audio` / `mimo_tts`）。``asr`` 仍然**只定义不启用**：识别 provider
    还没实现，任何 `speech.asr` 绑定都会被能力闸拦下——这是刻意的，
    在那之前任何"绑定成功"都是假的。
    """

    chat = "chat"
    embed = "embed"
    rerank = "rerank"
    tts = "tts"
    asr = "asr"


# 每个供应商 kind 提供哪些能力。**必须穷举** ProviderKind 的每个成员：
# 漏一个就等于那个 kind 不提供任何能力（`provider_capabilities` 返回空集，
# 任何角色绑上去都会被校验拦下），是失败关闭的方向，不是静默放行。
PROVIDER_CAPABILITIES: dict[ProviderKind, frozenset[ModelCapability]] = {
    ProviderKind.fake: frozenset({ModelCapability.chat}),
    ProviderKind.openai_compatible: frozenset({ModelCapability.chat}),
    ProviderKind.anthropic: frozenset({ModelCapability.chat}),
    ProviderKind.chat_responses: frozenset({ModelCapability.chat}),
    ProviderKind.local: frozenset({ModelCapability.chat}),
    ProviderKind.cloudflare_embedding: frozenset({ModelCapability.embed}),
    ProviderKind.siliconflow_rerank: frozenset({ModelCapability.rerank}),
    # 三家 TTS：能力都**只有** tts。一个 kind 声明两种能力会破坏绑定闸门
    # （见 `ProviderKind.mimo_chat` 的长注释）。
    ProviderKind.minimax_tts: frozenset({ModelCapability.tts}),
    ProviderKind.fishaudio_tts: frozenset({ModelCapability.tts}),
    ProviderKind.mimo_chat: frozenset({ModelCapability.chat}),
    ProviderKind.mimo_tts: frozenset({ModelCapability.tts}),
}

# 加 kind 时最容易犯的错是**忘了在上一张表里登记**。忘了的后果不是报错，
# 而是那个 kind 在设置页里静默不可选、绑上去只说"不支持"——很难查。
# 所以在导入时就断言穷举，让漏登记变成启动即崩的硬错误。
_missing_kinds = set(ProviderKind) - set(PROVIDER_CAPABILITIES)
if _missing_kinds:  # pragma: no cover - 只有在改漏时才触发
    raise RuntimeError(
        "PROVIDER_CAPABILITIES 漏登记了这些 ProviderKind："
        f"{sorted(k.value for k in _missing_kinds)}"
    )
del _missing_kinds


# 已经发出去过、后来改了名的 kind 值 → 现在的规范值。
#
# 为什么需要：`ProviderKind` 的取值是**公开契约**，会存进用户已有的配置
# revision 里。直接改名会让老配置在读取时变成"未知供应商类型"——
# 而那些配置本来是能用的。所以改名必须走别名，不能硬切。
#
# 为什么不能同时**发布**两个同义 kind：设置页会出现两个都能选、配出来行为
# 完全一样的条目，用户无法判断该选哪个。别名只用于**读取**，写入一律规范值。
LEGACY_PROVIDER_KIND_ALIASES: dict[str, str] = {
    # FishAudio 定名前用过 `fish_audio`（与文件名同形）。§17.2 统一成
    # `fishaudio_tts`，旧值保留为读取别名。
    "fish_audio": ProviderKind.fishaudio_tts.value,
}


def normalize_provider_kind(kind: str) -> str:
    """把可能的历史 kind 值规范成当前值。未知值原样返回（由调用方判失败）。

    **写入路径必须先过这个函数**，否则用户改一下老供应商就会把别名值
    又存回配置里，别名永远收不干净。
    """
    return LEGACY_PROVIDER_KIND_ALIASES.get(kind, kind)


# 角色 → 它需要的能力。键必须与 `contracts/v1.ModelRole` 的取值一致
# （`tests/unit/test_provider_capabilities.py` 盯着这一点）。
#
# 没有列进来的角色（reflection / planning / moderation.advisory /
# realtime.voice）意味着**服务端不对它们做能力约束**：它们既不在可绑清单里，
# 也没有任何代码请求，给它们编一个能力只会凭空多一条假规则。
PROFILE_CAPABILITIES: dict[str, ModelCapability] = {
    "dialogue.fast": ModelCapability.chat,
    "dialogue.deep": ModelCapability.chat,
    "dialogue.group": ModelCapability.chat,
    "perception.vision": ModelCapability.chat,
    # 看视频要的也是 ``chat``，**故意不新增 ``video`` 能力**。
    #
    # 理由：能力表是"供应商 kind 能不能服务这个角色"的唯一判据，而现有
    # 对话 kind 的能力集里没有、也不该凭空出现 ``video``。新增一个没人能
    # 提供的能力，等于把这一项变成"所有供应商都不匹配"——用户看到一张永远
    # 红的槽位卡，绑谁都说"这家不支持"，而实际上能不能看懂视频取决于
    # **具体模型**（同一个 kind 下既有纯文本模型也有多模态模型），
    # 不是取决于 kind。所以能力这里只表达"它走的是 chat 协议"，
    # "这个模型看不看得懂视频"由 SLOT_HINTS 说清楚，交给用户选。
    "perception.video": ModelCapability.chat,
    "memory.extract": ModelCapability.chat,
    "memory.embed": ModelCapability.embed,
    "memory.rerank": ModelCapability.rerank,
    # 语音两项：能力已定义，但**没有任何 kind 提供**（provider 尚未实现）。
    # 于是把 speech.* 绑到任何现有供应商都会被校验拦下——这正是我们要的：
    # 在那之前，任何"绑定成功"都是假的。
    "speech.tts": ModelCapability.tts,
    "speech.asr": ModelCapability.asr,
}


def provider_capabilities(kind: str) -> frozenset[ModelCapability]:
    """某个 kind 提供的能力。未知 kind → **空集**（失败关闭）。

    先过 `normalize_provider_kind`：老配置里可能还存着历史 kind 值，
    它们绑定时仍应正常匹配，否则用户升级后原本能用的绑定会突然"不支持"。
    """
    try:
        return PROVIDER_CAPABILITIES[ProviderKind(normalize_provider_kind(kind))]
    except ValueError:
        return frozenset()


def required_capability(profile: str) -> ModelCapability | None:
    """某个角色需要的能力；没声明约束的角色返回 None。"""
    return PROFILE_CAPABILITIES.get(profile)


def kind_supports_profile(kind: str, profile: str) -> bool:
    """这个 kind 能不能服务这个角色。没有能力要求的角色一律放行。"""
    need = required_capability(profile)
    if need is None:
        return True
    return need in provider_capabilities(kind)


# ---------------------------------------------------------------- 默认槽位
#
# 五个「默认模型」槽位：用户先给五件**常做的事**各挑一个模型，再决定每个
# 职责要不要单独指定。没单独指定的职责就跟着自己那个槽位走。
#
# 为什么不合并成一个"默认模型"：五件事对模型的要求真的不同——回消息要快，
# 看图片/看视频必须有对应的多模态能力，写东西值得用贵的。合成一个就得在
# 快与好之间二选一，而用户明明可以两个都要。
class ProviderSlot(StrEnum):
    """默认槽位的稳定键。**已发布的契约**：不要改成中文，也不要换拼写。"""

    fast = "fast"
    creation = "creation"
    vision = "vision"
    # 「看视频」与「识图」是两个槽位，不是同一个：
    # 同一个 kind 下既有"只认图片"的模型，也有能看视频的模型；动画（GIF/动图）
    # 走识图槽位还能凑合，真正的视频只有多模态视频模型才看得懂。合成一个
    # 槽位就必须在"看得清图片"和"看得懂视频"之间二选一。
    video = "video"
    daily = "daily"


# 展示顺序与**给人看的名字**。名字放服务端而不是前端，理由与
# `PROFILE_CAPABILITIES` 相同：同一个概念在界面、校验提示、错误文案里
# 出现三次，写在前端就会和服务端走散。前端只负责排版。
SLOT_ORDER: tuple[ProviderSlot, ...] = (
    ProviderSlot.fast,
    ProviderSlot.creation,
    ProviderSlot.vision,
    ProviderSlot.video,
    ProviderSlot.daily,
)

SLOT_LABELS: dict[ProviderSlot, str] = {
    ProviderSlot.fast: "快速",
    ProviderSlot.creation: "创作",
    ProviderSlot.vision: "识图",
    ProviderSlot.video: "视频",
    ProviderSlot.daily: "日常",
}

# 槽位名旁边的半句话。**要说清"它能干什么"而不是"它是什么"**：
# 「快速」与「日常」只差两个字，不解释的话用户分不出主对话该放哪个。
SLOT_HINTS: dict[ProviderSlot, str] = {
    ProviderSlot.fast: "回消息最常走的一条路，要快",
    ProviderSlot.creation: "想清楚再写：提炼、创作、要质量的活",
    ProviderSlot.vision: "必须能看图：把图片转成文字描述",
    # 与「识图」的区别要直接写出来，否则用户会以为"能看图就能看视频"。
    # 半句"她主动想看时才读"是给成本预期：视频描述不是逐条自动跑的。
    ProviderSlot.video: "必须能看懂视频：把视频转成文字描述（她主动想看时才读，不逐条自动读）",
    ProviderSlot.daily: "群里闲聊，可以慢一点、便宜一点",
}


# 角色 → 它跟随哪个默认槽位。**没有默认值的角色不要出现在这张表里**：
# 缺席就是"这个角色只能显式绑定"（`memory.embed` / `memory.rerank` 就是
# 这种：嵌入与重排是另一套协议，混进 chat 槽位只会选出调不通的组合）。
#
# 键必须与 `contracts/v1.ModelRole` 的取值一致
# （`tests/unit/test_default_slots.py` 盯着这一点）。
PROFILE_SLOTS: dict[str, ProviderSlot] = {
    "dialogue.fast": ProviderSlot.fast,
    "dialogue.group": ProviderSlot.daily,
    "perception.vision": ProviderSlot.vision,
    # 视频证据（kind=video）跟「视频」槽位。**注意**：媒体描述链路没有按 kind
    # 自动切职责（用户要求"她想看才看"，不逐条自动读），接线时由调用方显式
    # 指定 `VIDEO_PROFILE`。不跟「识图」走：用户可能只想给图片配一个便宜的
    # 视觉模型，看视频要另找能读多帧的模型——合成一个槽位就没法分别满足。
    "perception.video": ProviderSlot.video,
    # 记忆提炼放「创作」而不是「快速」：
    # - 「快速」是主回复链路，最在意延迟；提炼是每轮结束后的异步任务，
    #   慢一点没关系，但质量直接决定"她记不记得住"。
    # - `dialogue.deep` 被移出可绑清单之后，「创作」会一个成员都没有，
    #   等于留一个永远空着的槽位给用户看。
    # 代价见 `ports/config.py::EffectiveConfig.resolve_profile`：未绑定时
    # 提炼不再"借主对话模型代跑"，而是直接报不可用并退回正则。
    "memory.extract": ProviderSlot.creation,
}


def slot_for_profile(profile: str) -> ProviderSlot | None:
    """这个角色跟随哪个槽位；没有默认槽位的角色返回 None。"""
    return PROFILE_SLOTS.get(profile)


@dataclass(frozen=True)
class ImagePart:
    """一张图。``url`` 是 https 地址或 data URL（base64）。

    为什么不用 bytes：不同 provider 接受的形式不同（有的只收公网 URL，
    有的收 data URL），转换责任放在构造这一侧，端口只负责传递。
    用 ``from_bytes`` 从原始字节造 data URL，调用方不必自己拼格式。
    """

    url: str
    mime_type: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str = "image/jpeg") -> ImagePart:
        import base64

        encoded = base64.b64encode(data).decode("ascii")
        return cls(url=f"data:{mime_type};base64,{encoded}", mime_type=mime_type)


@dataclass(frozen=True)
class VideoPart:
    """一段视频（原生视频输入模式用）。

    与 ``ImagePart`` 同一个理由：端口只传递平台无关的引用（https 或 data URL），
    具体怎么塞进请求由适配器决定。

    原生视频输入**不是所有供应商都支持**（目前 Gemini / Qwen-VL / 豆包 /
    智谱 / Kimi 一类才收）。不支持时适配器必须明确报 ``unsupported``，
    让调用方退回抽帧——静默丢掉视频会让模型凭空回答"我看了"。
    """

    url: str
    mime_type: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str = "video/mp4") -> VideoPart:
        import base64

        encoded = base64.b64encode(data).decode("ascii")
        return cls(url=f"data:{mime_type};base64,{encoded}", mime_type=mime_type)


@dataclass(frozen=True)
class ChatMessageIn:
    role: str  # system | user | assistant
    content: str
    # 图像随消息一起送。空表示纯文本。
    # **不放进 content 里**：把 base64 塞进正文字符串会让日志、截断、
    # 字数预算全部失真，而且"这条消息有多长"变得没法算。
    images: tuple[ImagePart, ...] = ()
    # 视频同理，且**只有原生视频模式**才填。抽帧模式下视频被拆成
    # ``images`` 里的若干 JPEG，走的是原来那条路。
    videos: tuple[VideoPart, ...] = ()


def has_videos(messages: list[ChatMessageIn]) -> bool:
    return any(m.videos for m in messages)


@dataclass(frozen=True)
class ModelRequest:
    """一次模型调用的完整输入。"""

    model_id: str
    messages: list[ChatMessageIn]
    # ``None`` = **不发送**这个字段，让上游用自己的默认温度。
    #
    # 原来是固定 0.8：那时它是职责绑定里的可调项。现在温度不再是可配置项
    # （改它只影响措辞，不影响成本与能力，而各家默认值本来就是为对话调好的），
    # 所以默认**不发**，而不是继续发一个我们自己编的数字。
    temperature: float | None = None
    max_tokens: int | None = None
    # 追踪与复现：记录到 turn 上，便于审计“为什么这样说”
    profile: str = "dialogue.fast"
    config_revision: int = 0
    trace_id: str | None = None
    timeout_seconds: float = 60.0


def has_images(messages: list[ChatMessageIn]) -> bool:
    return any(m.images for m in messages)


class ModelErrorKind(StrEnum):
    unavailable = "provider_unavailable"
    timeout = "provider_timeout"
    error = "provider_error"
    # 必须与 error 不同的值。原先两者都是 "provider_error"，
    # 于是 `auth` 成了 `error` 的**别名**：
    #   ModelErrorKind.auth is ModelErrorKind.error  → True
    # 后果是 401（密钥无效/无权限）被当成通用 provider 错误，
    # 经 ErrorCode(kind.value) 映射后丢掉"鉴权失败"语义，
    # 用户拿不到"去检查密钥"这类可执行提示。
    auth = "unauthorized"
    missing_secret = "secret_missing"
    unsupported = "unsupported_capability"
    # 这一家供应商今天的 token 额度用完了。**必须与 error 分开**：
    # 它不是故障（换一家、等明天都能好），而"provider 出错"会让用户去
    # 检查密钥和地址，方向完全错。
    quota_exhausted = "quota_exhausted"


class ModelError(Exception):
    """provider 失败的确定性分类，内核据此给用户可执行的提示。"""

    def __init__(self, kind: ModelErrorKind, message: str, retriable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable


@dataclass
class ModelChunk:
    """流式增量。文本增量与结束原因是两个语义。"""

    delta: str = ""
    done: bool = False
    finish_reason: str | None = None


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    latency_ms: int | None = None
    models: list[str] = field(default_factory=list)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_estimate: float | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """所有 provider 适配器实现同一组方法。"""

    provider_id: str
    kind: ProviderKind

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """逐段产出文本（异步生成器）。实现方必须在取消时抛 asyncio.CancelledError。"""
        ...

    async def probe(self) -> ProbeResult:
        """连接检查：不做真实生成，只验证鉴权与端点可达。"""
        ...


@dataclass(frozen=True)
class ModelTarget:
    """路由结果：一次轮次实际使用的 provider/model。"""

    provider_id: str
    model_id: str
    profile: str
    config_revision: int
    fallback_used: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


__all__ = [
    "LEGACY_PROVIDER_KIND_ALIASES",
    "PROFILE_CAPABILITIES",
    "PROVIDER_CAPABILITIES",
    "ChatMessageIn",
    "VideoPart",
    "has_videos",
    "ModelCapability",
    "ModelChunk",
    "ModelError",
    "ModelErrorKind",
    "ModelProvider",
    "ModelRequest",
    "ModelTarget",
    "ProbeResult",
    "ProviderKind",
    "Usage",
    "kind_supports_profile",
    "normalize_provider_kind",
    "provider_capabilities",
    "required_capability",
]
