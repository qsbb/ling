"""从已发布的 speech.asr 绑定装配文件识别供应商。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ling.models.dashscope_asr import DEFAULT_BASE as DASHSCOPE_BASE
from ling.models.dashscope_asr import DashScopeAsr
from ling.models.siliconflow_asr import DEFAULT_BASE as SILICONFLOW_BASE
from ling.models.siliconflow_asr import SiliconFlowAsr
from ling.ports.config import EffectiveConfig, ProviderConfig
from ling.ports.model import ProviderKind, normalize_provider_kind
from ling.ports.speech import AsrCapabilities, AsrProvider, UnconfiguredSpeechProvider

ASR_PROFILE = "speech.asr"
ASR_KINDS = {
    ProviderKind.siliconflow_asr.value: "SiliconFlow",
    ProviderKind.dashscope_asr.value: "百炼 Qwen",
}
_UNAVAILABLE_CAPABILITIES = AsrCapabilities(formats=frozenset(), file_transcription=False)


@dataclass(frozen=True)
class AsrResolution:
    provider_id: str = ""
    model_id: str = ""
    kind: str = ""
    reason: str = ""
    provider: ProviderConfig | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        return self.provider is not None and not self.reason


@dataclass(frozen=True)
class AsrBindingView:
    provider: AsrProvider = field(repr=False)
    ok: bool
    reason: str = ""
    provider_id: str = ""
    model_id: str = ""
    kind: str = ""
    capabilities: AsrCapabilities = _UNAVAILABLE_CAPABILITIES

    @property
    def configured(self) -> bool:
        return self.ok


def resolve_asr(config: EffectiveConfig | None = None) -> AsrResolution:
    """解析绑定只读取配置，不发请求。"""
    if config is None:
        return AsrResolution(reason="尚无已发布配置，请先绑定语音识别模型")
    binding = config.binding(ASR_PROFILE)
    if binding is None:
        return AsrResolution(reason="没有绑定 speech.asr，请先选择语音识别模型")
    resolved = config.resolve_profile(ASR_PROFILE)
    provider_id = resolved.provider_id or binding.provider_id or ""
    model_id = resolved.model_id or binding.model_id or ""
    provider = config.providers.get(provider_id)
    kind = normalize_provider_kind(provider.kind) if provider else ""
    if not resolved.usable or not resolved.enabled:
        return AsrResolution(
            provider_id=provider_id,
            model_id=model_id,
            kind=kind,
            reason="语音识别绑定不可用，请检查供应商、模型及职责的启用状态",
        )
    if provider is None or kind not in ASR_KINDS:
        return AsrResolution(
            provider_id=provider_id,
            model_id=model_id,
            kind=kind,
            reason="speech.asr 需要支持文件识别的供应商类型",
        )
    return AsrResolution(
        provider_id=provider_id,
        model_id=model_id,
        kind=kind,
        provider=provider,
    )


async def build_asr_provider(
    config: EffectiveConfig | None = None,
    *,
    secret_reader: Callable[[str], Awaitable[str | None]] | None = None,
) -> AsrBindingView:
    resolution = resolve_asr(config)
    reason = resolution.reason
    secret = None
    if resolution.usable:
        secret = await secret_reader(resolution.provider_id) if secret_reader else None
        if not secret:
            reason = "语音识别供应商尚无可用密钥，请先保存密钥"
    if reason:
        return AsrBindingView(
            provider=UnconfiguredSpeechProvider(reason),
            ok=False,
            reason=reason,
            provider_id=resolution.provider_id,
            model_id=resolution.model_id,
            kind=resolution.kind,
        )
    provider = resolution.provider
    assert provider is not None and secret is not None
    instance: SiliconFlowAsr | DashScopeAsr
    if resolution.kind == ProviderKind.siliconflow_asr.value:
        instance = SiliconFlowAsr(
            provider_id=resolution.provider_id,
            api_key=secret,
            base_url=provider.base_url or SILICONFLOW_BASE,
            timeout_seconds=provider.timeout_seconds,
        )
    else:
        instance = DashScopeAsr(
            provider_id=resolution.provider_id,
            api_key=secret,
            base_url=provider.base_url or DASHSCOPE_BASE,
            timeout_seconds=provider.timeout_seconds,
        )
    return AsrBindingView(
        provider=instance,
        ok=True,
        provider_id=resolution.provider_id,
        model_id=resolution.model_id,
        kind=resolution.kind,
        capabilities=instance.capabilities(resolution.model_id),
    )


__all__ = [
    "ASR_KINDS",
    "ASR_PROFILE",
    "AsrBindingView",
    "AsrResolution",
    "build_asr_provider",
    "resolve_asr",
]
