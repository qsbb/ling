"""`build_tts_provider` 的装配细节：**只有单元层才能造出来的那些输入**。

这里测的是集成测试那条 HTTP 路径**覆盖不到**的东西：

- kind 关卡：把 `speech.tts` 绑到一家只能对话的供应商。发布校验
  （`settings/service.py` 的 `kind_supports_profile`）会拦下这种配置，所以它
  只能来自旧修订或手工改库——而装配层仍必须失败关闭，不能拿一个没有 tts
  能力的端点去合成。
- 协议符合性：造出来的是 `TtsProvider`（结构），不是一个"看起来像"的对象。
- 参数去向：密钥、base_url、模型 id 真的传进去了（对应地，没有串到别家）。

**不在这里测"有人调用工厂"**：那件事只有走真实路由才能证明，见
`tests/integration/test_speech_tts_wiring.py`。
"""

from __future__ import annotations

import pytest

from ling.domains.speech.providers import (
    TTS_KINDS,
    TTS_PROFILE,
    build_tts_provider,
    resolve_tts,
)
from ling.ports.config import (
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
)
from ling.ports.speech import TtsProvider, UnconfiguredSpeechProvider

KEY = "sk-fake-not-a-real-key-0123456789"


def _config(
    *,
    kind: str = "minimax_tts",
    provider_id: str = "mm",
    base_url: str | None = "https://api.minimax.example",
    enabled: bool = True,
    extra: dict | None = None,
    binding: bool = True,
) -> EffectiveConfig:
    provider = ProviderConfig(
        provider_id=provider_id,
        kind=kind,
        base_url=base_url,
        has_secret=True,
        enabled=enabled,
        extra=dict(extra or {}),
        models=(ProviderModel(model_id="speech-2.8-hd"),),
        timeout_seconds=42.0,
    )
    config = EffectiveConfig(revision=7, providers={provider_id: provider})
    if binding:
        config.profiles[TTS_PROFILE] = ProfileBinding(
            profile=TTS_PROFILE, provider_id=provider_id, model_id="speech-2.8-hd"
        )
    return config


async def _secret(provider_id: str) -> str | None:
    return KEY


# ------------------------------------------------------------ 配了 → 真适配器


@pytest.mark.parametrize("kind", sorted(TTS_KINDS))
async def test_every_tts_kind_builds_a_real_provider(kind: str) -> None:
    """三家都能按绑定造出实例，且**满足 `TtsProvider` 结构**。

    注意 `isinstance(..., TtsProvider)` 在这里**信息量很小**：`Protocol` 是
    `runtime_checkable` 的，只查成员存在，而 `UnconfiguredSpeechProvider` 现在
    也完整实现了成员（见 `ports/speech.py`）。所以真正的判据是
    `view.ok` 与"包装里确实是那家的适配器"。
    """
    view = await build_tts_provider(_config(kind=kind, provider_id="p"), secret_reader=_secret)

    assert view.ok is True, view.reason
    assert not isinstance(view.provider, UnconfiguredSpeechProvider)
    assert isinstance(view.provider, TtsProvider)
    # 结构符合不是判据：真正要看到的是包装里的那家适配器。
    adapter = view.provider._adapter  # type: ignore[attr-defined]
    assert type(adapter).__name__ == _ADAPTER_BY_KIND[kind]
    assert view.kind == kind
    assert view.model_id == "speech-2.8-hd"
    assert view.provider_id == "p"
    assert view.implementation == _ADAPTER_BY_KIND[kind]


_ADAPTER_BY_KIND = {
    "minimax_tts": "MiniMaxTts",
    "fishaudio_tts": "FishAudioTts",
    "mimo_tts": "MimoTts",
}


async def test_minimax_receives_binding_values() -> None:
    """密钥 / base_url / 模型 / 超时确实来自**这条绑定**。

    写错成别家的参数（比如把 base_url 丢了、把模型写死）不会报错，
    只会静静打到错误的端点——所以这里逐项断言。

    断言打在**包装里的适配器**上（`PortTtsProvider._adapter`）：用户真正关心的
    是"请求发去哪一家、带哪个模型"，而不是包装类自己的属性。
    """
    view = await build_tts_provider(
        _config(base_url="https://api.minimax.example/"), secret_reader=_secret
    )

    assert view.ok is True, view.reason
    adapter = view.provider._adapter  # type: ignore[attr-defined]
    assert adapter.model == "speech-2.8-hd"
    assert adapter._base == "https://api.minimax.example"
    assert adapter._timeout == 42.0
    # 密钥只进实例，不进 `TtsBindingView`（要能安全地拿去打日志）。
    assert KEY not in repr(view)
    assert KEY not in repr(adapter)


async def test_fishaudio_alias_kind_is_normalized() -> None:
    """老配置里的 `fish_audio`（改名前的值）仍要能装配。

    `normalize_provider_kind` 把别名折成 `fishaudio_tts`；漏了这一步，
    老用户升级后绑定会静默变成"不支持的类型"。
    """
    view = await build_tts_provider(
        _config(kind="fish_audio", provider_id="f"), secret_reader=_secret
    )

    assert view.ok is True, view.reason
    assert view.kind == "fishaudio_tts"
    assert view.implementation == "FishAudioTts"


# ------------------------------------------------------------ 没配/配错 → 占位


async def test_no_config_is_unconfigured() -> None:
    view = await build_tts_provider(None, secret_reader=_secret)

    assert view.ok is False
    assert isinstance(view.provider, UnconfiguredSpeechProvider)
    assert "已发布的配置" in view.reason


async def test_no_binding_is_unconfigured_and_says_so() -> None:
    view = await build_tts_provider(_config(binding=False), secret_reader=_secret)

    assert view.ok is False
    assert isinstance(view.provider, UnconfiguredSpeechProvider)
    assert "没有绑定" in view.reason


async def test_chat_only_kind_is_rejected() -> None:
    """`speech.tts` 绑到只能对话的 kind → 失败关闭，**不去造适配器**。

    发布校验会拦这种配置，所以它只能来自旧修订/手工改库。这里断言装配层
    自己也不放行：否则运行时会出现"调用一个没有 tts 能力的端点"，
    报错要等到网络往返之后才出现，而且理由完全指错方向。
    """
    view = await build_tts_provider(
        _config(kind="openai_compatible", provider_id="chatty"),
        secret_reader=_secret,
    )

    assert view.ok is False
    assert isinstance(view.provider, UnconfiguredSpeechProvider)
    assert "不是可合成的 TTS 供应商" in view.reason


async def test_missing_secret_is_unconfigured() -> None:
    """没有密钥读取器 / 读不到密钥 → 占位，且理由指向密钥。

    **不传 `secret_reader` 不许当成"不用密钥"**：那会让"没接线"伪装成
    "配好了"，第一次真正合成时才 401。
    """

    async def _none(provider_id: str) -> str | None:
        return None

    for reader in (None, _none):
        view = await build_tts_provider(_config(), secret_reader=reader)
        assert view.ok is False, reader
        assert isinstance(view.provider, UnconfiguredSpeechProvider)
        assert "密钥" in view.reason, view.reason


async def test_disabled_provider_and_binding() -> None:
    """供应商停用、职责停用 → 都不造适配器，理由分别可辨认。"""
    disabled_provider = await build_tts_provider(_config(enabled=False), secret_reader=_secret)
    assert disabled_provider.ok is False
    assert "停用" in disabled_provider.reason

    config = _config()
    binding = config.profiles[TTS_PROFILE]
    config.profiles[TTS_PROFILE] = ProfileBinding(
        profile=TTS_PROFILE,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        enabled=False,
    )
    disabled_binding = await build_tts_provider(config, secret_reader=_secret)
    assert disabled_binding.ok is False
    assert "停用" in disabled_binding.reason


# ------------------------------------------------------------ 纯解析（无 IO）


def test_resolve_tts_is_pure_and_does_not_need_secret() -> None:
    """解析只看配置，不碰密钥——所以状态展示可以在没有密钥读取器时跑。

    这一条同时守住"两步拆分"：如果哪天有人把读密钥塞回 `resolve_tts`，
    它就变成 async 或者会在同步上下文里炸。
    """
    resolution = resolve_tts(_config())
    assert resolution.broken is False
    assert resolution.kind == "minimax_tts"
    assert resolution.provider is not None
    # 名字里有 "resolve" 但不是协程：这一步不该有 IO。
    assert not hasattr(resolution, "__await__")


def test_unconfigured_view_does_not_claim_configured() -> None:
    """占位实现的 `configured` 必须是 False。

    反例：`configured` 恒为 True 的实现会让 `/v1/speech` 把"什么都没配"
    报成"已配置"——用户去查网络，而问题在于没绑。
    """
    from ling.domains.speech.providers import TtsBindingView
    from ling.ports.speech import UnconfiguredSpeechProvider as Unconfigured

    view = TtsBindingView(provider=Unconfigured("x"), ok=False, reason="x")
    assert view.configured is False
    assert Unconfigured("x").provider_id == "speech-unconfigured"


# ------------------------------------------------------------ 端口包装的行为


class _FakeAdapter:
    """最小的"适配器"替身：记录被怎么调用，返回一段假音频。"""

    provider_id = "minimax_tts"

    def __init__(self, *, audio: bytes = b"RIFF" + b"\x00" * 40 + b"WAVE") -> None:
        self.audio = audio
        self.calls: list[tuple[str, dict]] = []

    async def synthesize(self, text: str, **kwargs: object):
        from ling.models.tts_common import SynthesizedAudio

        self.calls.append((text, dict(kwargs)))
        return SynthesizedAudio(
            audio=self.audio, container="wav", duration_ms=1234, sample_rate=24000
        )


def _request(voice: object, text: str = "你好呀") -> object:
    from ling.ports.speech import TtsRequest

    return TtsRequest(model_id="speech-2.8-hd", text=text, voice=voice, response_format="wav")


async def test_port_wrapper_emits_start_chunk_end_and_forwards_params() -> None:
    """`Start → Chunk → End` 的顺序、非空 chunk、以及转发下去的参数。

    反例：只发 End 的实现（"合成完了"）会让消费方拿不到规格；只发 Chunk 的
    实现会被编排层判成"没有 End，不发布不完整音频"——两种都该红。
    """
    from ling.domains.speech.providers import PortTtsProvider
    from ling.ports.speech import MiniMaxVoice, SpeechCall, TtsChunk, TtsEnd, TtsStart

    adapter = _FakeAdapter()
    provider = PortTtsProvider(adapter, kind="minimax_tts")
    call = SpeechCall(
        operation_id="op", attempt_id="1", window_id="w", turn_id=None, config_revision=1
    )

    events = [e async for e in provider.synthesize(_request(MiniMaxVoice("male-qn")), call=call)]

    assert [type(e) for e in events] == [TtsStart, TtsChunk, TtsEnd], events
    start, chunk, end = events
    assert isinstance(start, TtsStart) and start.spec.container == "wav"
    assert isinstance(chunk, TtsChunk) and chunk.sequence == 0 and chunk.audio
    assert isinstance(end, TtsEnd) and end.usage.usage_source == "estimated"
    # 音色确实以那家的关键字参数转发下去（不是被吞掉、用默认嗓音）。
    assert adapter.calls == [("你好呀", {"voice_id": "male-qn"})]


async def test_port_wrapper_refuses_mismatched_voice() -> None:
    """音色类型与 provider 不匹配 → `SpeechError(invalid_request)`，**不降级**。

    这条必须打在有区分力的输入上：MiniMax 的 provider 配 Fish 的音色。
    若实现"传不进去就按默认嗓音合成"，它会绿——因为它根本不报错。
    """
    from ling.domains.speech.providers import PortTtsProvider
    from ling.ports.speech import (
        FishAudioVoice,
        SpeechCall,
        SpeechError,
        SpeechErrorKind,
    )

    adapter = _FakeAdapter()
    provider = PortTtsProvider(adapter, kind="minimax_tts")
    call = SpeechCall(
        operation_id="op", attempt_id="1", window_id="w", turn_id=None, config_revision=1
    )

    with pytest.raises(SpeechError) as excinfo:
        [e async for e in provider.synthesize(_request(FishAudioVoice("ref-1")), call=call)]

    assert excinfo.value.kind is SpeechErrorKind.invalid_request
    assert adapter.calls == [], "参数不匹配时不该已经发出去一次合成"


async def test_port_wrapper_maps_adapter_errors_to_port_kinds() -> None:
    """适配器的 `TtsError` 在边界映射成 `SpeechError`，**kind 与可重试性都保留**。

    映射表漏一项会 KeyError（响亮），但映射成错误的一档不会报错——只会让
    "去换密钥"变成"稍后重试"。所以这里逐档核对。
    """
    from ling.domains.speech.providers import PortTtsProvider
    from ling.models.tts_common import TtsError, TtsErrorKind
    from ling.ports.speech import MiniMaxVoice, SpeechCall, SpeechError, SpeechErrorKind

    class _Failing:
        provider_id = "minimax_tts"

        def __init__(self, kind: TtsErrorKind) -> None:
            self.kind = kind

        async def synthesize(self, text: str, **kwargs: object):
            raise TtsError(self.kind, "上游说不行", retriable=False)

    call = SpeechCall(
        operation_id="op", attempt_id="1", window_id="w", turn_id=None, config_revision=1
    )
    for kind in TtsErrorKind:
        provider = PortTtsProvider(_Failing(kind), kind="minimax_tts")
        with pytest.raises(SpeechError) as excinfo:
            [e async for e in provider.synthesize(_request(MiniMaxVoice("v")), call=call)]
        assert excinfo.value.kind is SpeechErrorKind(kind.value), kind


async def test_port_wrapper_reports_buffered_not_native_streaming() -> None:
    """三家都是 buffered。说成 `native` 会让上层以为能边收边播。"""
    from ling.domains.speech.providers import PortTtsProvider

    provider = PortTtsProvider(_FakeAdapter(), kind="minimax_tts")
    caps = provider.capabilities("speech-2.8-hd")

    assert caps.streaming == "buffered"
    assert "wav" in caps.formats and "mp3" in caps.formats
