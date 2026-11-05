"""供应商注册表：把生效配置翻译成可调用的 provider 实例。

本模块只负责“根据 config revision 造 provider”，不做路由决策（那是 Router 的职责），
也不做设置持久化（那是 ConfigService 的职责）。
"""

from __future__ import annotations

from collections.abc import Callable

from ling.models.fake import FailingFakeProvider, FakeProvider
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from ling.ports.model import ModelProvider, ProviderKind


class UnknownProviderError(LookupError):
    pass


class ProviderRegistry:
    """按 provider_id 构造并缓存 provider 实例。

    密钥通过 ``secret_reader`` 回调按需解密，避免把明文长期留在内存映射里。
    """

    def __init__(
        self,
        secret_reader: Callable[[str], str | None],
        secret_hint: Callable[[str], str] | None = None,
    ) -> None:
        self._secret_reader = secret_reader
        # 可选：解释"为什么拿不到密钥"（没存过 / 解不开），用于错误提示
        self._secret_hint = secret_hint
        self._cache: dict[str, ModelProvider] = {}
        self._signatures: dict[str, str] = {}

    def _signature(self, cfg: ProviderConfig) -> str:
        return "|".join(
            [
                cfg.provider_id,
                cfg.kind,
                cfg.base_url or "",
                cfg.secret_last4,
                str(cfg.enabled),
                repr(sorted(cfg.extra.items())),
            ]
        )

    def get(self, cfg: ProviderConfig) -> ModelProvider:
        sig = self._signature(cfg)
        cached = self._cache.get(cfg.provider_id)
        if cached is not None and self._signatures.get(cfg.provider_id) == sig:
            return cached
        provider = self._build(cfg)
        self._cache[cfg.provider_id] = provider
        self._signatures[cfg.provider_id] = sig
        return provider

    def _build(self, cfg: ProviderConfig) -> ModelProvider:
        kind = cfg.kind
        if kind == ProviderKind.fake.value:
            if cfg.extra.get("mode") == "failing":
                return FailingFakeProvider()
            return FakeProvider(
                chunk_delay=float(cfg.extra.get("chunk_delay", 0.05)),
                fail_after_chunks=cfg.extra.get("fail_after_chunks"),
            )
        if kind in {ProviderKind.openai_compatible.value, ProviderKind.local.value}:
            # 延迟 import：避免未使用 provider 时引入 httpx 初始化开销
            from ling.models.openai_compatible import OpenAICompatibleProvider

            secret = self._secret_reader(cfg.provider_id)
            return OpenAICompatibleProvider(
                provider_id=cfg.provider_id,
                base_url=cfg.base_url or "https://api.openai.com/v1",
                api_key=secret,
                kind=ProviderKind(kind),
                secret_hint=self._secret_hint,
            )
        raise UnknownProviderError(f"未知 provider 类型: {kind}")

    def provider_for(self, config: EffectiveConfig, binding: ProfileBinding) -> ModelProvider:
        cfg = config.providers.get(binding.provider_id)
        if cfg is None:
            raise UnknownProviderError(f"provider 未登记: {binding.provider_id}")
        if not cfg.enabled:
            raise UnknownProviderError(f"provider 已停用: {binding.provider_id}")
        return self.get(cfg)

    def invalidate(self, provider_id: str | None = None) -> None:
        if provider_id is None:
            self._cache.clear()
            self._signatures.clear()
            return
        self._cache.pop(provider_id, None)
        self._signatures.pop(provider_id, None)


def bootstrap_config(*, test_mode: bool = True, persona_id: str = "persona_ling") -> EffectiveConfig:
    """没有任何已发布修订时的引导配置。

    测试模式下所有对话职责绑定 fake provider；非测试模式则不绑定任何真实模型，
    因此对话会明确失败并提示“尚未配置模型”，而不是偷偷用假回复冒充模型。
    """
    if not test_mode:
        return EffectiveConfig(revision=0, providers={}, profiles={}, test_mode=False,
                               persona_id=persona_id)
    providers = {
        "fake": ProviderConfig(
            provider_id="fake",
            kind=ProviderKind.fake.value,
            enabled=True,
            has_secret=False,
            extra={"chunk_delay": 0.04},
        )
    }
    profiles: dict[str, ProfileBinding] = {}
    for profile in ("dialogue.fast", "dialogue.deep", "dialogue.group"):
        profiles[profile] = ProfileBinding(
            profile=profile,
            provider_id="fake",
            model_id="deterministic",
            temperature=0.8,
            timeout_seconds=30.0,
        )
    return EffectiveConfig(revision=0, providers=providers, profiles=profiles, test_mode=True,
                           persona_id=persona_id)


__all__ = ["ProviderRegistry", "UnknownProviderError", "bootstrap_config"]
