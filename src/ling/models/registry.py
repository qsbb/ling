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
                # 超时变了必须重建：它写在 provider 的 httpx client 上，
                # 缓存住旧实例等于"改了设置但请求还是按老超时跑"。
                str(cfg.timeout_seconds),
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
                # 兼容协议的端点需单独声明工具支持。仅接受显式布尔值。
                native_tools=cfg.extra.get("native_tools") is True,
                timeout_seconds=cfg.timeout_seconds,
                secret_hint=self._secret_hint,
            )
        if kind == ProviderKind.anthropic.value:
            from ling.models.anthropic import AnthropicProvider

            return AnthropicProvider(
                provider_id=cfg.provider_id,
                base_url=cfg.base_url or "https://api.anthropic.com/v1",
                api_key=self._secret_reader(cfg.provider_id),
                timeout_seconds=cfg.timeout_seconds,
                secret_hint=self._secret_hint,
            )
        if kind == ProviderKind.chat_responses.value:
            from ling.models.chat_responses import ChatResponsesProvider

            return ChatResponsesProvider(
                provider_id=cfg.provider_id,
                base_url=cfg.base_url or "https://api.openai.com/v1",
                api_key=self._secret_reader(cfg.provider_id),
                timeout_seconds=cfg.timeout_seconds,
                secret_hint=self._secret_hint,
            )
        if kind in {
            ProviderKind.cloudflare_embedding.value,
            ProviderKind.siliconflow_rerank.value,
        }:
            # 这两种不是对话供应商：它们服务于 memory.embed / memory.rerank，
            # 由 `domains/memory/providers.py` 装配，**不经过 ModelRouter**
            # （chat 的流式协议它们一条都不满足）。说清这一点，比让用户看到
            # 「未知 provider 类型」然后去怀疑自己拼错了 kind 要有用。
            raise UnknownProviderError(
                f"{kind} 不是对话供应商：它服务于记忆的嵌入/重排，不经过模型路由"
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


def _initial_persona_id() -> str:
    """还没有任何已发布修订"时的初始人格（``LING_PERSONA_ID``）。

    读的是 `Settings`，不是直接读环境变量：这样"没配环境变量时是什么"
    只有 `config.py` 一处定义。读不到配置（例如在极早期导入阶段）时返回空串
    = "没有配置任何人格"，调用方拿到的是"暂无人设"，而不是一个编出来的 id。
    """
    try:
        from ling.config import get_settings

        return str(get_settings().persona_id or "")
    except Exception:  # noqa: BLE001 - 早期导入 / 配置损坏时按"没配"处理
        return ""


def bootstrap_config(
    *,
    test_mode: bool = True,
    # ``None`` = "调用方没指定，用环境变量里的初始人格"（`LING_PERSONA_ID`）。
    # 原来这里写死 ``"persona_ling"``，于是"没发布过修订"的部署永远忽略
    # 环境变量里配的人格——`ConfigService` 每次都显式传值所以看不出来，
    # 但直接调 `bootstrap_config()` 的地方（测试、脚本）会拿到错的 id。
    persona_id: str | None = None,
    log_access_denials: bool = True,
) -> EffectiveConfig:
    """没有任何已发布修订时的引导配置。

    测试模式下所有对话职责绑定 fake provider；非测试模式则不绑定任何真实模型，
    因此对话会明确失败并提示“尚未配置模型”，而不是偷偷用假回复冒充模型。

    ``log_access_denials`` 来自环境变量（``LING_LOG_ACCESS_DENIALS``），
    作为"还没发布过配置修订"时的默认值；一旦设置页保存并发布了修订，
    以修订里的值为准（与 ``test_mode`` 同一个模式）。

    没传 ``persona_id`` 时才现读 `Settings.persona_id`（``LING_PERSONA_ID``，
    默认在 `config.py` 里，**全仓唯一的那个字面量**）。刻意不在签名里写死：
    签名默认值在导入时求值，环境变量改了也拿不到。
    """
    if persona_id is None:
        persona_id = _initial_persona_id()
    # 显式收窄成非空 str：`_initial_persona_id()` 的返回类型是 `str`，但 mypy
    # 看不到"None 分支已经处理完"这件事会传导到下面两个构造点（参数本身仍
    # 声明为 `str | None`，因为调用方确实可以传 None 表示"用环境变量"）。
    # 不写成 `assert` 是因为它在 `-O` 下会被优化掉，而这里不需要断言语义。
    resolved_persona_id: str = persona_id
    if not test_mode:
        return EffectiveConfig(revision=0, providers={}, profiles={}, test_mode=False,
                               persona_id=resolved_persona_id,
                               log_access_denials=log_access_denials)
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
    # 只绑**真会被请求**的角色。`dialogue.deep` 曾在这里，但全仓没有任何
    # 代码按它发起请求（见 docs/MODEL-ROLES-AUDIT.md），绑了等于没有：
    # 引导配置里"已绑定"的样子会让设置页显示它已就绪。它现在在
    # `settings/service.py::RESERVED_PROFILES`。
    #
    # `memory.extract` 也在这一组里，理由不是"它很常见"，而是**不绑就会
    # 掉能力**：从前它没绑时 worker 会偷偷退回 `dialogue.fast` 代跑；撤掉
    # 那条跨角色回退之后（见 `EffectiveConfig.resolve_profile`），测试模式
    # 下它会直接落到正则提炼，和"测试模式把能跑的路径都跑一遍"相悖。
    for profile in ("dialogue.fast", "dialogue.group", "memory.extract"):
        profiles[profile] = ProfileBinding(
            profile=profile,
            provider_id="fake",
            model_id="deterministic",
        )
    return EffectiveConfig(revision=0, providers=providers, profiles=profiles, test_mode=True,
                           persona_id=resolved_persona_id,
                           log_access_denials=log_access_denials)


__all__ = ["ProviderRegistry", "UnknownProviderError", "bootstrap_config"]
