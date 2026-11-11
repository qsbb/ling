"""语音 provider 适配器（ASR / TTS）与语音决策：**按工作流建模**，不按单一 model_id。

设计文档 `docs/design/SPEECH-IO-ARCHITECTURE.md` §3.2 是这份端口的权威来源；
本模块是它的落地，并补上 §12.2 要求的作业字段与 §13.1 的语音决策。

## 只依赖标准库

编排层负责鉴权、解析媒体引用、限流、存储与重试；provider 只接收**已授权**
的音频数据。所以这里没有 httpx、没有 SDK 类型——把厂商 SDK 类型泄漏到领域层，
等于让领域依赖某个供应商的发版节奏。

## 第一阶段只做文件闭环，不假装有实时能力

- TTS 始终 ``TtsStart → TtsChunk* → TtsEnd``，**至少一个非空 chunk 才算成功**。
  即使供应商一次性返回完整音频，也可以只发一个 chunk——但**不能因此标榜
  "低延迟流式"**：供应商流式、ling 的客户端流式、平台语音条发送是三件事。
- ``TtsEnd`` 只说明**上游生成完成**，不说明已持久化、已投递或已播放。
- 实时（麦克风双向）是后续阶段的可选 Protocol，第一阶段不伪装具备它。

## 与旧接口的关系

旧版本这里只有 ``AudioRef`` / ``TtsOptions`` / ``AsrPartial``，且 TTS 要求
无条件 streaming。第一阶段改为「synthesize 必需、streaming/cancellable 可选」，
旧符号保留但标注为过渡期兼容，避免一次性删净把别处打挂。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# 失败分类
# --------------------------------------------------------------------------- #


class SpeechErrorKind(StrEnum):
    """语音失败的**确定性**分类。上层据此决定"能不能重试、要不要告诉用户"。

    与渠道的 ``ChannelErrorKind`` 分开：语音失败发生在"生成"阶段，
    投递失败发生在"发送"阶段。混成一套会让"合成超时"和"发出去没回执"
    无法区分，而这两种情况的正确处置完全相反（前者可换供应商重试，
    后者绝不能重发）。

    **这是唯一的对外失败协议。** 三家 adapter 内部可以有自己的错误类型
    （`models/tts_common.py::TtsError`），但必须在 adapter 边界**映射一次**
    到这里，不形成两套对外协议。前七类的取值与 `TtsErrorKind` 逐字一致，
    映射是一张一眼能对上的表，不是有损转换。
    """

    # --- 七类：与 adapter 内部 TtsErrorKind 逐字对齐 ---
    authentication = "authentication"
    """密钥无效/无权限（401/403）。**不可重试**，且必须提示"去检查密钥"——
    归到 unavailable 里会让用户去查网络和地址，方向完全错。"""
    rate_limited = "rate_limited"
    """上游限流（429）。可重试，但要退避，不是立刻重打。"""
    invalid_request = "invalid_request"
    """请求不合法（400/422）：音色 ID 格式错、文本过长、格式不支持。
    **不可重试**——重发同一个请求只会得到同一个 400。"""
    unavailable = "unavailable"
    """供应商侧故障（5xx）或服务不可达。可重试。"""
    timeout = "timeout"
    """超时（连接/读取）。可重试。"""
    protocol = "protocol"
    """对方 200 了但响应不符合协议（缺字段、hex 解不开、不是音频）。

    **必须与 unavailable 分开**：它是"对方没按约定说话"，往往是版本变更，
    而不是我们这边的参数错或者网络问题。"""
    not_configured = "not_configured"
    """没配密钥/没配音色/没配参考素材。**不可重试**，提示去设置里补。"""

    # --- 以下是语音领域独有的（adapter 内部没有对应项）---
    unsupported = "unsupported_capability"
    """这个 provider / 渠道做不到被要求的这件事（例如没声明 streaming）。"""
    invalid_audio = "invalid_audio"
    """输入音频解不开 / 格式不支持（ASR、参考素材、转码）。重试无用。"""
    too_large = "too_large"
    """超出配置的大小或时长上限。重试无用，且**不许静默截断**。"""
    no_speech = "no_speech"
    """识别成功但没有人声。**不是错误**，但也不该启动一次空对话。"""
    cancelled = "cancelled"
    """本地取消。**不保证上游免计费**——不要对用户声称没花钱。"""


class SpeechError(Exception):
    """语音调用失败。**对外只抛这一种。**

    ``retriable`` 是给编排层看的建议，不是许可：``rate_limited`` 可重试但
    要退避，``invalid_request`` 重发只会得到同样的 400。真正的重试决策还要
    看预算与是否已经产出过音频。
    """

    def __init__(self, kind: SpeechErrorKind, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable


# --------------------------------------------------------------------------- #
# 音频描述
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioSpec:
    """一段音频的**技术规格**。

    ``container`` 与 ``codec`` 必须分开：WAV 是个容器，里面可以是 pcm_s16le；
    OGG 也是容器，里面可以是 opus。合称"格式"会在转码时出错——把 mp3 的字节
    塞进 .wav 扩展名，播放器解不开但文件"看起来存在"。

    压缩音频在**解析前**允许 `sample_rate_hz` / `channels` 为 None：
    谎报一个 44100 只会让下游按错误的参数处理。
    """

    mime_type: str
    container: str
    """容器：wav / mp3 / ogg / webm / raw。**不与 codec 混用。**"""
    codec: str
    """编码：pcm_s16le / pcm_f32le / opus / mp3 / ..."""
    sample_rate_hz: int | None
    channels: int | None
    bitrate_bps: int | None = None
    """统一用 bit/s。未知或无此参数时为 None（mp3 有，wav 通常无）。"""


@dataclass(frozen=True)
class AudioRef:
    """受限媒体引用。原始音频不进领域对象，只存引用与哈希。"""

    media_ref: str
    """形如 ``asset:<id>``。**不是**外部 URL，也不是客户端文件路径。"""
    mime_type: str
    duration_ms: int | None
    sha256: str


@dataclass(frozen=True)
class AudioInput:
    """交给 provider 的**已授权**音频字节。"""

    content: bytes
    filename: str
    """服务端生成、带真实容器扩展名。不用用户上传的原始文件名（可能含路径）。"""
    spec: AudioSpec


# --------------------------------------------------------------------------- #
# 调用上下文与用量
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpeechCall:
    """一次语音调用的**因果上下文**，随调用透传，用于日志、幂等和取消判定。

    ``config_revision`` 必须冻结：一次调用跨越多条消息重试时，不能中途
    换了供应商配置却还记在同一次操作下。
    """

    operation_id: str
    attempt_id: str
    window_id: str
    turn_id: str | None
    config_revision: int


@dataclass(frozen=True)
class SpeechUsage:
    """用量与计费依据。

    各厂商计量口径不同：有的按字符（MiniMax），有的按音频秒，有的按 token。
    ``unit_kind`` 说明 ``input_units`` 到底是哪种，**不把字符伪装成 token**
    ——那会让成本估算和预算闸门全部失真。

    ``usage_source`` 必须诚实：``estimated`` 是我们自己按文本长度算的，
    ``provider`` 是上游返回的，``billing`` 是账单回执。三者可信度不同。
    """

    audio_seconds: float | None = None
    characters: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_units: float | None = None
    unit_kind: (
        Literal["provider_characters", "utf8_bytes", "audio_seconds", "tokens"] | None
    ) = None
    estimated_cost: Decimal | None = None
    currency: str | None = None
    """ISO 4217。**不在这里自动换汇**——汇率是另一件事，混进来会让审计算不清。"""
    usage_source: Literal["estimated", "provider", "billing", "unknown"] = "unknown"


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TtsCapabilities:
    """一家 TTS 供应商**实际**能做什么（§17.2）。

    ``streaming`` 用三值而不是布尔：``native`` 真增量、``buffered`` 只是
    上游一次性返回、``none`` 完全没有下行增量。把 ``buffered`` 说成
    "支持流式"是本项目最容易自我欺骗的地方——用户看到的是"边说边出"，
    而 ``buffered`` 意味着说完最后一个字才开始有音频。
    """

    formats: frozenset[str]
    """实际支持的 ``response_format`` 子集：wav / mp3 / pcm16。"""
    streaming: Literal["native", "buffered", "none"]
    cancellable_transport: bool
    """本地连接可取消。**不代表云端退费**——取消不等于没计费。"""


@dataclass(frozen=True)
class MiniMaxVoice:
    """MiniMax 的发音参数。全部是该厂商自己的字段名。"""

    voice_id: str
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0
    emotion: str | None = None
    """有多少种、哪些模型支持都不一样（2.8 不支持 whisper）。
    默认不发：猜一个 emotion 会让语音听起来莫名其妙。"""


@dataclass(frozen=True)
class FishAudioVoice:
    """FishAudio 用参考音色 ID 指定声音，没有 speed/vol/pitch 那套参数。"""

    reference_id: str


@dataclass(frozen=True)
class MimoReferenceVoice:
    """小米 MiMo 走 voiceclone：把参考音频以 data URL 放进请求体。

    ``audio`` 是**已授权、已解码校验**的参考素材。保存配置时只存
    ``reference_asset_id``，**不存 AudioInput 本身**——把字节塞进配置库
    会让配置体积失控，也会让"授权"变成"字节还在就算有权限"。
    """

    audio: AudioInput
    context: str = ""
    """user 侧上下文（她"听到"的那句话）。可空。"""
    style_tags: str = ""
    """MiMo 专属、独立于展示正文。首期默认空。
    **不能冒充三家通用情绪参数**——另两家没有这个概念。"""


TtsVoice = MiniMaxVoice | FishAudioVoice | MimoReferenceVoice
"""必须与 provider 的 kind 一致。

**没有统一的 ``voice_key``**（§17.2 撤销了跨厂商映射表）：同一个"温柔"在
三家是三种完全不同的东西，强行对应会产生"换了一家听起来完全变了"还不报错。
所以音色是**每个 provider 自己的配置**，请求里带的也是那家自己的值对象。
"""


@dataclass(frozen=True)
class TtsRequest:
    """一次合成请求（§17.2 定案签名）。"""

    model_id: str
    text: str
    """**去掉表达控制之后**的实际话语（`[speak]` 已被剥掉）。"""
    voice: TtsVoice
    response_format: Literal["wav", "mp3", "pcm16"]
    output_spec: AudioSpec | None = None
    """**期望**的规格。允许与实际不同——探测前采样率未知，
    绝不能把未知的 rate 谎报成 24k。"""
    stream: bool = False
    """要求下行增量。**不等于**平台实时播放，也不保证本机能边播边收。"""


@dataclass(frozen=True)
class TtsStart:
    """合成开始。``spec`` 描述**实际**输出，不伪报达到请求参数。

    压缩格式在探测前 ``sample_rate_hz`` 允许为 None。
    """

    spec: AudioSpec


@dataclass(frozen=True)
class TtsChunk:
    """一段音频字节。

    ``sequence`` 从 0 **连续**递增。允许切在 MP3 帧／PCM sample 边界中间，
    所以消费方必须**按序缓冲后整体解码**，不能每块独立解码。
    """

    sequence: int
    audio: bytes


@dataclass(frozen=True)
class TtsEnd:
    """上游生成**完成**。

    不等于已持久化、已投递或已播放——那三件事各有自己的状态。
    没有厂商请求 ID 时 ``provider_request_id`` 为 None，**不伪造远端 ID**。
    """

    spec: AudioSpec
    """adapter 已知的最终规格。上游归一化后**还要再验证一次**——
    adapter 说 24k 不代表它真的重采样了。"""
    provider_request_id: str | None
    usage: SpeechUsage


TtsEvent = TtsStart | TtsChunk | TtsEnd


@dataclass(frozen=True)
class SynthesisJobInput:
    """一次合成作业的完整输入。

    边界冻结：``output_spec`` 只在 ``request`` 里，作业外层不再存一份目标
    格式——两份必然不同步。也**没有 ``voice_key``**：音色属于 provider 配置。
    """

    synthesis_id: str
    provider_id: str
    call: SpeechCall
    request: TtsRequest
    max_bytes: int
    max_duration_ms: int


@runtime_checkable
class TtsProvider(Protocol):
    """文本转语音。**整个仓库只有这一个 TTS 端口。**

    调用方式：``async for event in provider.synthesize(request, call=call)``
    ——``synthesize`` 是**普通 def 返回异步迭代器**，不是先 await 再迭代。

    事件流：``TtsStart(spec) → TtsChunk* → TtsEnd``。一次成功必须满足
    Start 在前、至少一个**非空** Chunk、End 收尾；失败抛 ``SpeechError``，
    **不补 End**，不发布完整产物。
    """

    provider_id: str
    """**配置实例 ID**，不是固定厂商名。用户可能配两个 MiniMax 账号。"""

    def capabilities(self, model_id: str) -> TtsCapabilities:
        """这家（这个模型）实际能做什么。调用前用它挡掉不可能的请求，
        而不是发出去等 400。"""
        ...

    def synthesize(self, request: TtsRequest, *, call: SpeechCall) -> AsyncIterator[TtsEvent]:
        ...

    async def cancel(self, *, attempt_id: str) -> None:
        """取消**这一次尝试**：关响应、停转码、清 staging、拒收迟到块。

        只影响对应 attempt。**不声称供应商没有计费。**"""
        ...

    async def aclose(self) -> None:
        """关闭共享 client。只在实例停用／应用关闭时调用。"""
        ...


# --------------------------------------------------------------------------- #
# ASR
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AsrRequest:
    model_id: str
    audio: AudioInput
    language: str | None = None
    vocabulary: tuple[str, ...] = ()
    task: Literal["transcribe", "translate_en"] = "transcribe"
    """``translate_en`` 第一阶段**默认关闭**，只有 profile 明确支持才可用。

    不把翻译结果当原语言转写——那会把"她听到的是英文"和"她听到中文但被译了"
    混成一种事实。
    """


@dataclass(frozen=True)
class AsrResult:
    text: str
    language: str | None
    confidence: float | None
    """上游没给就是 None。**不把 logprob 冒充成 confidence**——两者含义不同。"""
    provider_request_id: str | None
    usage: SpeechUsage


@dataclass(frozen=True)
class AsrPartial:
    """一段识别结果。``is_final=True`` 才允许进入主链。"""

    text: str
    is_final: bool
    confidence: float | None = None


@dataclass(frozen=True)
class AsrEvent:
    """流式识别事件。``text`` 是**当前句的完整快照**，不是增量。

    让 provider 内部归并 delta：把增量拼接的责任交给消费方，等于每个
    消费方都要重写一遍"丢了第一片怎么办"。
    """

    utterance_id: str
    revision: int
    text: str
    is_final: bool
    """本句稳态，**不等于**整个麦克风会话结束。"""


@dataclass(frozen=True)
class AudioFrame:
    sequence: int
    data: bytes


@dataclass(frozen=True)
class StreamingAsrOptions:
    model_id: str
    spec: AudioSpec
    language: str | None = None


@runtime_checkable
class AsrProvider(Protocol):
    """整段/文件识别。成功只返回终态；空白结果用 ``no_speech``。"""

    provider_id: str

    async def transcribe(self, request: AsrRequest, *, call: SpeechCall) -> AsrResult:
        ...


@runtime_checkable
class FileStreamingAsrProvider(Protocol):
    """文件级流式（上传一个文件，边解边出）。与实时麦克风**不是**一回事。"""

    provider_id: str

    def transcribe_stream(
        self, request: AsrRequest, *, call: SpeechCall
    ) -> AsyncIterator[AsrEvent]:
        ...


@runtime_checkable
class StreamingAsrProvider(Protocol):
    """实时麦克风识别（后续阶段）。第一阶段不实现，也不伪装具备。"""

    provider_id: str

    def recognize_stream(
        self,
        frames: AsyncIterator[AudioFrame],
        options: StreamingAsrOptions,
        *,
        call: SpeechCall,
    ) -> AsyncIterator[AsrEvent]:
        ...


# --------------------------------------------------------------------------- #
# 表达选择：她自己决定这条用文字还是语音
# --------------------------------------------------------------------------- #

SPEAK_TAG = "[speak]"
"""她选择语音时，写在**最终回复最前面**的标签（§17.4）。

为什么用标签而不是让她调一个 TTS 工具：普通语音回复只是"已定正文的呈现
方式"，她不需要观察执行结果才能继续写正文。判据见
`docs/design/TOOL-CALLING-ARCHITECTURE.md:37-45`。

解析规则（由 `capabilities/expression_choice.py` 实现，此处只冻结字面量）：

- 只认**本轮 actor 最终输出的起始控制区**，允许前导空白；
- 只允许一个**无参数**的 `[speak]`，不携带 provider/voice/路径；
- 不从用户内容、引用、工具结果、代码块或正文中间提取；
- 去标签后若为空、重复或非法控制 → 明确无有效外发，
  **不沿用旧"剥离为空就恢复标签"的兜底**。
"""


@dataclass(frozen=True)
class ExpressionIntent:
    """她本轮的表达选择。**是意图，不是结果。**

    ``source="model"`` 表示这是她自己在回复里选的；确定性执行层之后仍可
    因不可用/预算/权限拒绝（记 ``voice_rejected``），但**不能替她抛硬币**，
    也不能强制 all-voice。
    """

    output_modality: Literal["text", "voice"]
    source: Literal["model", "default"]
    config_revision: int
    voice_config_hash: str | None = None
    """冻结那一刻的音色配置哈希。**不含明文 key。**

    为什么要哈希而不是记 voice_id：事后要能回答"这条是用哪套音色配出来的"，
    但把 voice_id/参考素材 id 抄进日志会让"换个音色"这类隐私动作留痕。"""


# --------------------------------------------------------------------------- #
# 职责绑定与能力声明
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpeechProfile:
    """一个语音职责的绑定（asr / tts / realtime）。"""

    profile: str
    provider_id: str
    model_id: str
    streaming: bool
    languages: list[str] = field(default_factory=lambda: ["zh-CN"])
    max_audio_seconds: int = 120


# 已知职责与必需能力。
#
# **`speech.tts` 不再强制 `streaming`**（§12.1 的契约变更）：第一阶段是文件
# 闭环，"基础合成"不需要流式；真正需要流式的只有实时角色。旧表要求 TTS
# 无条件 streaming+cancellable，会让所有只支持一次性返回整段音频的厂商
# （包括这三家的基础接口）永远绑不上。
#
# 实时职责仍然强制流式/打断/重连——那是它的定义，不是可选项。
SPEECH_PROFILE_REQUIREMENTS: dict[str, dict[str, bool]] = {
    "speech.asr": {"produces_final": True},
    "speech.tts": {"produces_audio": True},
    "realtime.voice": {"streaming": True, "barge_in": True, "reconnect": True},
}


def validate_speech_profile(profile: SpeechProfile, capabilities: dict[str, bool]) -> list[str]:
    """校验一个语音职责是否满足必需能力。返回缺失项（空表示通过）。"""
    required = SPEECH_PROFILE_REQUIREMENTS.get(profile.profile)
    if required is None:
        return [f"未知语音职责: {profile.profile}"]
    missing = [name for name, needed in required.items() if needed and not capabilities.get(name)]
    if profile.profile == "realtime.voice" and not capabilities.get("event_mirror"):
        # 实时 provider 的 session 只是媒体会话，必须能把事件镜像回 ling
        missing.append("event_mirror")
    return missing


class UnconfiguredSpeechProvider:
    """未配置语音供应商时的占位实现。

    所有调用都明确失败并给出可执行原因，**不伪造音频**，
    也不假装"已经说给你听"。
    """

    provider_id = "speech-unconfigured"

    def __init__(self, reason: str = "尚未配置语音供应商") -> None:
        self._reason = reason

    def capabilities(self, model_id: str) -> TtsCapabilities:
        """**什么都不支持**。

        没有这个方法时，一个声明为 `TtsProvider` 的占位实现至少缺一个成员，
        调用方还能在静态检查里看出来。补上它是为了让"占位"完整地满足端口——
        也让 `isinstance(x, TtsProvider)` 不再是"能用"的判据
        （那只是结构符合，见 `domains/speech/providers.TtsBindingView.ok`）。
        """
        return TtsCapabilities(formats=frozenset(), streaming="none", cancellable_transport=False)

    async def cancel(self, *, attempt_id: str) -> None:
        """没有在途请求，取消是空操作。"""
        return None

    async def aclose(self) -> None:
        """没有共享 client。"""
        return None

    async def transcribe(self, request: AsrRequest, *, call: SpeechCall) -> AsrResult:
        raise SpeechError(SpeechErrorKind.not_configured, self._reason, retriable=False)

    async def synthesize(self, request: TtsRequest, *, call: SpeechCall) -> AsyncIterator[TtsEvent]:
        """明确失败。**不 yield 伪造音频**——那等于告诉她"我已经说给你听了"。

        声明成异步生成器是为了满足 `TtsProvider` 协议的签名；函数体第一句
        就抛异常，所以调用方拿到迭代器后第一次 `__anext__` 立刻失败。
        """
        raise SpeechError(SpeechErrorKind.not_configured, self._reason, retriable=False)
        yield  # pragma: no cover - 让 mypy 认出这是异步生成器；永不可达


__all__ = [
    "SPEAK_TAG",
    "SPEECH_PROFILE_REQUIREMENTS",
    "AsrEvent",
    "AsrPartial",
    "AsrProvider",
    "AsrRequest",
    "AsrResult",
    "AudioFrame",
    "AudioInput",
    "AudioRef",
    "AudioSpec",
    "ExpressionIntent",
    "FileStreamingAsrProvider",
    "FishAudioVoice",
    "MimoReferenceVoice",
    "MiniMaxVoice",
    "SpeechCall",
    "SpeechError",
    "SpeechErrorKind",
    "SpeechProfile",
    "SpeechUsage",
    "StreamingAsrOptions",
    "StreamingAsrProvider",
    "SynthesisJobInput",
    "TtsCapabilities",
    "TtsChunk",
    "TtsEnd",
    "TtsEvent",
    "TtsProvider",
    "TtsRequest",
    "TtsStart",
    "TtsVoice",
    "UnconfiguredSpeechProvider",
    "validate_speech_profile",
]
