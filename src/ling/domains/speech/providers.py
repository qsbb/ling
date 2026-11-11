"""出站 TTS 的 provider 装配：设置里的绑定 → 真实适配器。

**这个模块只回答「按当前已发布配置，`speech.tts` 该用哪个实现」**，
不回答「什么时候说话」。

## 它解决的是什么问题

在它之前，`TtsProvider` 全仓只有 `UnconfiguredSpeechProvider` 一个实现
（`ports/speech.py`），`GET /v1/speech` 里**硬编码**构造它并返回写死的
`configured: False`。也就是说三家适配器（`models/minimax_tts.py`、
`fish_audio.py`、`mimo_tts.py`）写好了、编排层（`orchestration.py`）也写好了，
中间那一步「按用户绑定挑一家、拿密钥、造实例」不存在——绑一个模型不会有
任何代码去读它。

## 它**没有**解决的是什么

出站触发链路仍然是断的：`[speak]` 能被 `capabilities/expression_choice.py`
解析成 `ExpressionIntent`，但**没有任何生产代码把解析结果交给
`SpeechOrchestrator.synthesize()`**（那个方法在 `src/` 里零调用点，
只有 `tests/integration/test_speech_orchestration.py` 手工构造它）。

所以 `speech.tts` **仍留在 `RESERVED_PROFILES` 里**：用户绑定了模型、
这个模块也真的会造出那家的适配器，但她依然不会说话。把这个入口摆进
设置页，等于把「绑上就能听见」当成承诺——那正是 `settings/service.py`
那段注释要消灭的假承诺。等出站链路接上，再把这条挪回可绑清单。

## 为什么音色不在构造期解决

三家的音色是**三家各自的值对象**（`MiniMaxVoice` / `FishAudioVoice` /
`MimoReferenceVoice`，§17.2 撤销了跨厂商映射表），而 MiMo 的音色是**每次
请求随体上传的参考音频**（`MimoReferenceVoice.audio`）。把音色塞进构造参数
会让「换音色」变成「换实例」，也会把授权过的音频字节钉进进程内存。
所以构造只负责「这家怎么连」，音色留给 `TtsRequest.voice`。

## 密钥

本模块只接收**已解密**的密钥，经 `secret_reader` 回调（与
`domains/memory/providers.py` 同一约定）。它不落库、不写日志、不进返回值。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ling.models.tts_common import SynthesizedAudio, TtsErrorKind
from ling.ports.config import EffectiveConfig, ProviderConfig
from ling.ports.model import ProviderKind, normalize_provider_kind
from ling.ports.speech import (
    AudioSpec,
    FishAudioVoice,
    MimoReferenceVoice,
    MiniMaxVoice,
    SpeechCall,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
    TtsCapabilities,
    TtsChunk,
    TtsEnd,
    TtsEvent,
    TtsProvider,
    TtsRequest,
    TtsStart,
    UnconfiguredSpeechProvider,
)

#: 出站合成的职责名。与 `ports/model.PROFILE_CAPABILITIES` 的键一致。
TTS_PROFILE = "speech.tts"

#: 能服务 `speech.tts` 的 kind → 人类可读的厂商名。
#:
#: **必须与 `models/` 下的适配器一一对应**：这张表里有、`_build` 里没有的
#: kind 会在装配期抛 RuntimeError（见 `_build` 的 else 分支），而不是静默造出
#: 一个用不了的实例。
TTS_KINDS: dict[str, str] = {
    ProviderKind.minimax_tts.value: "MiniMax",
    ProviderKind.fishaudio_tts.value: "FishAudio",
    ProviderKind.mimo_tts.value: "小米 MiMo",
}


@dataclass(frozen=True)
class TtsBindingView:
    """一次装配的**结论**：造出了什么、为什么没造出来。

    只给排障与状态展示用，**不含密钥**。``ok`` 为真时 ``provider`` 一定不是
    `UnconfiguredSpeechProvider`；为假时 ``reason`` 说明断在哪一步，
    ``provider`` 是占位实现（调用会被明确拒绝，不伪造音频）。
    """

    provider: TtsProvider
    ok: bool
    reason: str = ""
    #: 绑定指向的供应商 id（没绑定时为空）。
    provider_id: str = ""
    #: 绑定的模型 id（没绑定时为空）。
    model_id: str = ""
    #: 规范化之后的 kind（别名已折算成当前值）。
    kind: str = ""
    #: 走的是哪一档解析：``explicit`` / ``slot`` / ``unavailable``。
    source: str = ""
    #: 构造出来的实现类名，排障用（不做 isinstance 判据）。
    implementation: str = ""

    @property
    def configured(self) -> bool:
        """真的有一个能合成的实现（不是占位）。"""
        return self.ok


@dataclass(frozen=True)
class TtsResolution:
    """``speech.tts`` 的**解析结论**，以及（可能还没拿到的）装配结果。

    拆成两步是为了不在同步工厂里做 IO：判定"该用哪一家、缺什么"是纯函数，
    拿密钥是异步的。`:meth:`assemble` 只在真的需要实例时才 await。
    """

    provider_id: str = ""
    model_id: str = ""
    kind: str = ""
    source: str = ""
    #: 解析不出来 / 构造不了时的原因。**给用户直接看的中文。**
    reason: str = ""
    #: 供应商条目；为 None 表示断在解析阶段（没绑定 / 供应商不存在）。
    provider: ProviderConfig | None = None
    #: 解析阶段就断定装配不出来（不存在、已停用、kind 不对、没有配置）。
    #: 为真时 `provider` 一定为 None，且 `reason` 非空。
    broken: bool = False

    @property
    def usable(self) -> bool:
        """解析阶段没有断定它坏掉（**不代表密钥已经拿到**）。"""
        return not self.broken


def resolve_tts(config: EffectiveConfig | None = None) -> TtsResolution:
    """解析 `speech.tts` 该用哪一家、哪种 kind。**纯函数，不碰数据库、不碰密钥。**

    判定顺序与 `EffectiveConfig.resolve_profile` 同一条，不在这里另判一遍：

    1. 没绑定 / 被停用 / 解析不出来 → ``broken``，理由说清断在哪。
    2. 绑定指向的供应商不存在 / 已停用 → ``broken``。
    3. kind 不是三家之一 → ``broken``（**不猜**，也绝不拿聊天供应商去合成：
       那个端点根本没有 tts 能力。发布校验已拦过，这里是旧修订与手工改库的
       第二道防线）。

    密钥**不在这一步判**：它要从库里解密，而解析要纯、要能同步展示。
    """
    from ling.ports.config import ResolutionSource

    if config is None:
        return TtsResolution(
            reason="还没有已发布的配置修订，无法确定 speech.tts 用哪一家",
            broken=True,
        )

    resolved = config.resolve_profile(TTS_PROFILE)
    source = _source_name(resolved.source)
    provider_id = (resolved.provider_id or "").strip()
    model_id = (resolved.model_id or "").strip()

    # 供应商被停用/不存在时，`resolve_profile` 只会说"不可用"，而它自己也
    # 不知道断在哪一步。**先把绑定读出来**：用户要的是"我能去修什么"，
    # 不是一句"不可用"。
    binding = config.binding(TTS_PROFILE)
    bound_pid = (binding.provider_id or "").strip() if binding is not None else ""

    if not resolved.enabled or resolved.source is ResolutionSource.unavailable:
        return TtsResolution(
            provider_id=provider_id or bound_pid,
            model_id=model_id or (binding.model_id if binding is not None else "") or "",
            source=source,
            reason=_explain_unavailable(config, binding, resolved.reason),
            broken=True,
        )

    provider = config.providers.get(provider_id)
    if provider is None or not provider.enabled:
        return TtsResolution(
            provider_id=provider_id,
            model_id=model_id,
            source=source,
            reason=f"speech.tts 指向的供应商 {provider_id or '（空）'} 不存在或已停用",
            broken=True,
        )

    kind = normalize_provider_kind(provider.kind)
    if kind not in TTS_KINDS:
        return TtsResolution(
            provider_id=provider_id,
            model_id=model_id,
            kind=kind,
            source=source,
            reason=(
                f"供应商 {provider_id} 的类型是 {provider.kind}，不是可合成的 TTS 供应商"
                f"（支持：{'、'.join(sorted(TTS_KINDS.values()))}）"
            ),
            broken=True,
        )

    return TtsResolution(
        provider_id=provider_id,
        model_id=model_id,
        kind=kind,
        source=source,
        provider=provider,
    )


async def assemble_tts(
    resolution: TtsResolution,
    *,
    secret_reader: Callable[[str], Awaitable[str | None]] | None = None,
) -> TtsBindingView:
    """把解析结果变成**真的实例**。拿密钥是唯一一次 IO。

    ``secret_reader`` 不传就等于「装配不出来」：本项目没有 TTS 的环境变量
    配置入口，也不该悄悄有。读密钥失败（根密钥轮换过、密文损坏）按"没有
    密钥"处理——理由由 ``reason`` 说清，不伪造一个能用的实例。
    """
    if resolution.broken:
        return TtsBindingView(
            provider=UnconfiguredSpeechProvider(resolution.reason),
            ok=False,
            reason=resolution.reason,
            provider_id=resolution.provider_id,
            model_id=resolution.model_id,
            kind=resolution.kind,
            source=resolution.source,
            implementation=UnconfiguredSpeechProvider.__name__,
        )

    provider = resolution.provider
    assert provider is not None  # `broken=False` 已保证

    secret = await secret_reader(resolution.provider_id) if secret_reader else None
    if not secret:
        reason = f"供应商 {resolution.provider_id} 还没有可用的密钥：保存密钥后再试"
        return TtsBindingView(
            provider=UnconfiguredSpeechProvider(reason),
            ok=False,
            reason=reason,
            provider_id=resolution.provider_id,
            model_id=resolution.model_id,
            kind=resolution.kind,
            source=resolution.source,
            implementation=UnconfiguredSpeechProvider.__name__,
        )

    try:
        instance = _build(resolution.kind, provider, model_id=resolution.model_id, api_key=secret)
    except Exception as exc:  # noqa: BLE001 - 装配失败说出来，别装成「未配置」
        reason = f"供应商 {resolution.provider_id} 装配失败：{type(exc).__name__}"
        return TtsBindingView(
            provider=UnconfiguredSpeechProvider(reason),
            ok=False,
            reason=reason,
            provider_id=resolution.provider_id,
            model_id=resolution.model_id,
            kind=resolution.kind,
            source=resolution.source,
            implementation=UnconfiguredSpeechProvider.__name__,
        )

    return TtsBindingView(
        provider=PortTtsProvider(instance, kind=resolution.kind),
        ok=True,
        provider_id=resolution.provider_id,
        model_id=resolution.model_id,
        kind=resolution.kind,
        source=resolution.source,
        implementation=type(instance).__name__,
    )


async def build_tts_provider(
    config: EffectiveConfig | None = None,
    *,
    secret_reader: Callable[[str], Awaitable[str | None]] | None = None,
) -> TtsBindingView:
    """解析 + 装配一步到位。调用方只需要它。"""
    return await assemble_tts(resolve_tts(config), secret_reader=secret_reader)


# --------------------------------------------------------------------------- #
# 适配器 → 端口
# --------------------------------------------------------------------------- #


class PortTtsProvider:
    """把三家的**原生** `synthesize(text, **音色参数)` 包成端口形态。

    ## 为什么需要这一层

    三家的 `synthesize` 是 `(text, *, voice_id=...)`，端口是
    `(TtsRequest, *, call=SpeechCall)`。`SpeechOrchestrator._call_provider`
    已经能翻译这两种形态，所以**纯粹为了让编排层认得**并不需要这一层。

    它解决的是另一件事：**音色类型与 provider 类型必须一致**。`TtsVoice` 是
    联合类型，传错不会报错——只会拿那家的默认嗓音合成，听起来像"音色没生效"。
    在这里一次判掉，编排层就只面对一种形态，也只有一个地方需要维护
    「谁要哪些关键字参数」。

    ## 它**不做**的事

    不猜音色、不降级成默认嗓音。类型对不上就抛
    `SpeechError(invalid_request)`：让她用别人的声音说话、而日志里看不出
    异常，比一次明确的失败糟得多。
    """

    def __init__(self, adapter: object, *, kind: str) -> None:
        self._adapter = adapter
        self._kind = normalize_provider_kind(kind)
        self.provider_id = str(getattr(adapter, "provider_id", "") or self._kind)

    def capabilities(self, model_id: str) -> TtsCapabilities:
        """三家都是**一次性返回整段音频**，没有下行增量。

        所以是 ``buffered`` 而不是 ``native``——把 buffered 说成"支持流式"
        是 `ports/speech.py` 明令要避免的那类自我欺骗：用户以为"边说边出"，
        实际是说完最后一个字才开始有音频。
        """
        return TtsCapabilities(
            formats=_SUPPORTED_FORMATS[self._kind],
            streaming="buffered",
            # 本地连接可取消；**不代表云端退费**。
            cancellable_transport=True,
        )

    def synthesize(self, request: TtsRequest, *, call: SpeechCall) -> AsyncIterator[TtsEvent]:
        """端口签名。翻译成那家的原生调用，再包成 `Start → Chunk → End`。

        **是普通 def**：端口规定「普通 def 返回异步迭代器，不是先 await 再迭代」。
        """
        return self._stream(request, call)

    async def _stream(self, request: TtsRequest, call: SpeechCall) -> AsyncIterator[TtsEvent]:
        from ling.models.tts_common import TtsError

        kwargs = _voice_kwargs(request.voice, expected_kind=self._kind)
        try:
            result = await self._adapter.synthesize(  # type: ignore[attr-defined]
                request.text, **kwargs
            )
        except TtsError as exc:
            # 适配器的内部类型 → 端口类型。**在边界映射一次**，
            # 不让两套失败协议同时对外（见 `tts_common` 模块注释）。
            raise SpeechError(
                _PORT_ERROR_KIND[exc.kind],
                exc.message,
                retriable=exc.retriable,
            ) from exc

        spec = _spec_of(result)
        yield TtsStart(spec=spec)
        # 空音频不 yield：端口规定「至少一个非空 chunk」，空块会让下游
        # 以为"有音频但解不开"。
        if result.audio:
            yield TtsChunk(sequence=0, audio=result.audio)
        yield TtsEnd(
            spec=spec,
            provider_request_id=result.provider_request_id,
            usage=_usage_of(request.text),
        )

    async def cancel(self, *, attempt_id: str) -> None:
        """单个 attempt 的取消。三家都是一次性 HTTP 请求，**没有共享句柄可关**。

        明确写出来而不是假装成功：真正取消要等那一层有了可取消的传输。
        """
        return None

    async def aclose(self) -> None:
        """关闭共享 client。当前三家适配器每次请求自建 client，没有可关的。"""
        return None


#: 各家 `output_format` 允许的取值。**从适配器自己的常量取**，不在这里再抄
#: 一份：抄一份的话，适配器加了格式而这里没加，结果是"能合成但能力声明说不行"。
#:
#: 延迟导入放在模块底的 `_load_supported_formats()`，避免在 import 期把三家
#: 适配器（以及它们的 httpx）拖进每一条用到本模块的路径。
_SUPPORTED_FORMATS: dict[str, frozenset[str]] = {}


def _load_supported_formats() -> None:
    from ling.models.fish_audio import SUPPORTED_FORMATS as FISH
    from ling.models.mimo_tts import SUPPORTED_FORMATS as MIMO
    from ling.models.minimax_tts import DEFAULT_SAMPLE_RATE as MINIMAX_RATES

    _SUPPORTED_FORMATS.update(
        {
            ProviderKind.minimax_tts.value: frozenset(MINIMAX_RATES),
            ProviderKind.fishaudio_tts.value: frozenset(FISH),
            ProviderKind.mimo_tts.value: frozenset(MIMO),
        }
    )


#: `TtsErrorKind` → `SpeechErrorKind`。
#:
#: 两个枚举**成员同名**（`ports/speech.py` 的注释写明"前七类的取值逐字一致"），
#: 但**仍然查表而不是按名字转换**：一旦哪边改了名，查表会 KeyError（响亮地
#: 失败），按名字猜会静默落到某个默认档——把"去换密钥"说成"稍后重试"。
_PORT_ERROR_KIND: dict[str, SpeechErrorKind] = {
    TtsErrorKind.authentication: SpeechErrorKind.authentication,
    TtsErrorKind.rate_limited: SpeechErrorKind.rate_limited,
    TtsErrorKind.invalid_request: SpeechErrorKind.invalid_request,
    TtsErrorKind.unavailable: SpeechErrorKind.unavailable,
    TtsErrorKind.timeout: SpeechErrorKind.timeout,
    TtsErrorKind.protocol: SpeechErrorKind.protocol,
    TtsErrorKind.not_configured: SpeechErrorKind.not_configured,
}


def _spec_of(result: SynthesizedAudio) -> AudioSpec:
    """`SynthesizedAudio` → 端口的 `AudioSpec`。

    容器与编码**分开**写：适配器嗅探出的是**容器**（wav/mp3/pcm/flac），
    编码按容器给一个确定值。认不出容器时用请求时的 mime 兜底，仍然未知就
    说 ``raw``——**不谎报一个看起来合理的编码**。
    """
    codec = _CODEC_BY_CONTAINER.get(result.container, "unknown")
    return AudioSpec(
        mime_type=_MIME_BY_CONTAINER.get(result.container, "application/octet-stream"),
        container=result.container or "raw",
        codec=codec,
        sample_rate_hz=result.sample_rate,
        channels=1,
    )


_CODEC_BY_CONTAINER: dict[str, str] = {
    "wav": "pcm_s16le",
    "pcm": "pcm_s16le",
    "mp3": "mp3",
    "flac": "flac",
    "ogg": "opus",
}

_MIME_BY_CONTAINER: dict[str, str] = {
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}


def _voice_kwargs(voice: object, *, expected_kind: str) -> dict[str, object]:
    """按**值对象的类型**翻成那家的关键字参数，并核对它与 kind 是否一致。

    三家各要各的（MiniMax `voice_id` / Fish `reference_id` / MiMo 参考音频字节），
    没有统一的 `voice_key`（§17.2 撤销了跨厂商映射）。传错类型时**明确失败**。
    """
    kind_of_voice = _KIND_OF_VOICE.get(type(voice))
    if kind_of_voice is None:
        raise SpeechError(
            SpeechErrorKind.invalid_request,
            f"不认识的音色类型：{type(voice).__name__}",
            retriable=False,
        )
    if kind_of_voice != normalize_provider_kind(expected_kind):
        raise SpeechError(
            SpeechErrorKind.invalid_request,
            f"音色类型与供应商不匹配（{type(voice).__name__} 给了 {expected_kind}）",
            retriable=False,
        )

    if isinstance(voice, MiniMaxVoice):
        return {"voice_id": voice.voice_id}
    if isinstance(voice, FishAudioVoice):
        return {"reference_id": voice.reference_id}
    assert isinstance(voice, MimoReferenceVoice)  # 上面三种已穷举
    return {
        "voice_audio": voice.audio.content,
        "voice_mime": voice.audio.spec.mime_type,
        "style_context": voice.context,
    }


_KIND_OF_VOICE: dict[type, str] = {
    MiniMaxVoice: ProviderKind.minimax_tts.value,
    FishAudioVoice: ProviderKind.fishaudio_tts.value,
    MimoReferenceVoice: ProviderKind.mimo_tts.value,
}


def _usage_of(text: str) -> SpeechUsage:
    """用量口径诚实：我们只知道**输入**多少字，不知道花了多少秒。

    ``usage_source="estimated"`` 而不是 ``provider``：数字是我们按文本长度
    算的，不是上游回执。把估算标成回执会让成本核对建立在一个假来源上。
    """
    return SpeechUsage(
        characters=len(text),
        input_units=float(len(text)),
        unit_kind="provider_characters",
        usage_source="estimated",
    )


def _explain_unavailable(
    config: EffectiveConfig,
    binding: object,
    fallback_reason: str,
) -> str:
    """把"不可用"翻译成**用户能去修的那一句话**。

    `resolve_profile` 只回答"最后有没有可用模型"。对 `speech.tts` 来说，
    最常见的断点有两个，而它给的句子都太泛：

    - 根本没绑 → "只能单独指定模型：它没有默认槽位"不像一句提示；
    - 绑了但供应商被停用 → "不可用（供应商或模型被停用了）"还要用户自己猜
      是哪一头。

    这里只**补充说明**，不改变判定：判定仍然只认 `resolve_profile`。
    """
    pid = str(getattr(binding, "provider_id", "") or "").strip() if binding else ""
    if binding is None or not pid:
        return "没有绑定 speech.tts：请在设置里指定一家 TTS 供应商"
    provider = config.providers.get(pid)
    if provider is None:
        return f"speech.tts 指向的供应商 {pid} 不存在：请重新选一家"
    if not provider.enabled:
        return f"speech.tts 指向的供应商 {pid} 已停用：重新启用它或换一家"
    return fallback_reason or f"speech.tts 绑定的 {pid} 当前不可用"


def _source_name(source: object) -> str:
    """`ResolutionSource` → 字符串（契约值，给响应与日志用）。"""
    value = getattr(source, "value", source)
    return str(value or "")


def _build(kind: str, provider: ProviderConfig, *, model_id: str, api_key: str) -> Any:
    """按 kind 造实例。**只做构造，不发请求、不碰网络。**"""
    extra = provider.extra if isinstance(provider.extra, dict) else {}
    output_format = str(extra.get("output_format") or "").strip()

    if kind == ProviderKind.minimax_tts.value:
        from ling.models.minimax_tts import MiniMaxTts

        # MiniMax 的 voice_id / 情绪属于**音色**（请求级），不在这里给：
        # 构造时钉死会让"换个声音"变成"改配置再发布"。
        return MiniMaxTts(
            api_key=api_key,
            model=model_id,
            base_url=provider.base_url or "",
            output_format=output_format or "mp3",
            timeout_seconds=provider.timeout_seconds,
        )

    if kind == ProviderKind.fishaudio_tts.value:
        from ling.models.fish_audio import FishAudioTts

        return FishAudioTts(
            api_key=api_key,
            model=model_id,
            base_url=provider.base_url or "",
            output_format=output_format or "mp3",
            timeout_seconds=provider.timeout_seconds,
        )

    if kind == ProviderKind.mimo_tts.value:
        from ling.models.mimo_tts import MimoTts

        # 参考音频属于**音色**（每次请求随体上传），构造期没有它是对的：
        # `MimoReferenceVoice.audio` 由编排层从受限资产加载后传进
        # `synthesize`。构造只负责「这家怎么连」。
        return MimoTts(
            api_key=api_key,
            model=model_id,
            base_url=provider.base_url or "",
            output_format=output_format or "wav",
            style_context=str(extra.get("style_context") or ""),
            timeout_seconds=provider.timeout_seconds,
        )

    # `TTS_KINDS` 里有、这里没有 → 编程错误，不是用户配置错误。
    raise RuntimeError(f"{kind} 在 TTS_KINDS 里登记了，但 _build 不知道该怎么造")


__all__ = [
    "TTS_KINDS",
    "TTS_PROFILE",
    "PortTtsProvider",
    "TtsBindingView",
    "TtsResolution",
    "assemble_tts",
    "build_tts_provider",
    "resolve_tts",
]

# 三家的 `SUPPORTED_FORMATS` 与 MiniMax 的采样率表要在**导入本模块之后**
# 才能取（它们在 `ling.models.*`，那里依赖 httpx）。放在模块底而不是顶部：
# 顶部导入会把三家适配器拖进每一条 `import ling.domains.speech.providers`
# 的路径，包括只用 `resolve_tts` 做状态展示的地方。
_load_supported_formats()
