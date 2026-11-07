"""记忆检索的向量/重排提供方装配。

**优先级：网页绑定 → 环境变量 → 未配置（降级）**。

- 网页绑定：设置页把 `memory.embed` / `memory.rerank` 指到某个供应商，
  凭据来自供应商条目（密文解密后由调用方传进来，本模块不碰数据库）。
- 环境变量：`LING_EMBEDDING_*` / `LING_RERANK_*`。**必须保留**——已有部署
  是这么配的，升级后不能突然变成"未配置"：那会让语义检索静默失效，
  而症状只是"她好像想不起以前说过的话"，没人能从界面看出是没配模型。
- 都没有：返回 None，检索退回 BM25 + RRF（字面路径），rerank 跳过。

**有绑定就不再退回环境变量**（即使绑定是坏的）：绑定是用户的显式选择，
偷偷换成环境变量那套会让人误以为新供应商已经生效。绑定坏掉时这里只打
WARNING 并返回 None，设置页那边另有校验与降级提示。

**为什么不把密钥写进代码或配置快照**：provider 凭据泄漏一次就要全部轮换。
这里只接收已解密的字符串，不落库、不写日志。``env.example`` 里只留空占位。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from ling.ports.model import ProviderKind

logger = logging.getLogger(__name__)

# 来源标记。设置页据此说清"这份能力从哪来"，也用来判断该不该报降级：
# 配了环境变量的部署不该被告知"语义检索不可用"。
SOURCE_BINDING = "binding"
SOURCE_ENV = "env"

# 依次尝试的能力角色（与 `ports/model.PROFILE_CAPABILITIES` 一致）。
EMBED_PROFILE = "memory.embed"
RERANK_PROFILE = "memory.rerank"

_DEFAULT_EMBED_MODEL = "@cf/baai/bge-m3"
_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
_DEFAULT_RERANK_BASE = "https://api.siliconflow.cn/v1"


def _binding(config: EffectiveConfig | None, profile: str) -> ProfileBinding | None:
    """取一个**启用的**绑定；没有就返回 None（= 走环境变量）。"""
    if config is None:
        return None
    binding = config.profiles.get(profile)
    if binding is None or not binding.enabled:
        return None
    return binding


def _provider(
    config: EffectiveConfig | None, binding: ProfileBinding, kind: ProviderKind
) -> ProviderConfig | None:
    """绑定指向的供应商；不存在/已停用/kind 不对都返回 None。"""
    if config is None:
        return None
    cfg = config.providers.get(binding.provider_id)
    if cfg is None or not cfg.enabled or cfg.kind != kind.value:
        return None
    return cfg


def _unusable(profile: str, binding: ProfileBinding, reason: str) -> None:
    """绑定存在但装配不出来：说清楚，**并且不回退环境变量**。"""
    logger.warning(
        "%s 绑定了 %s，但%s；本能力停用（不会退回环境变量，绑定是显式选择）",
        profile,
        binding.provider_id,
        reason,
    )


# ------------------------------------------------------------------ 嵌入


def _embedding_from_env():  # noqa: ANN201 - 返回 CloudflareEmbedding | None
    key = os.environ.get("LING_EMBEDDING_API_KEY")
    account = os.environ.get("LING_EMBEDDING_ACCOUNT_ID")
    model = os.environ.get("LING_EMBEDDING_MODEL", _DEFAULT_EMBED_MODEL)
    base = os.environ.get("LING_EMBEDDING_API_BASE")
    if not key:
        return None
    try:
        from ling.models.cloudflare_embedding import CloudflareEmbedding

        if not account and not base:
            return None
        return CloudflareEmbedding(
            account_id=account or "", api_key=key, model=model, base_url=base
        )
    except Exception:  # noqa: BLE001 - 装配失败按未配置处理
        return None


def build_embedding(
    config: EffectiveConfig | None = None,
    *,
    secret_reader: Callable[[str], str | None] | None = None,
):  # noqa: ANN201 - 返回 CloudflareEmbedding | None
    """构造向量提供方：有绑定用绑定，没绑定退回环境变量，都没有返回 None。

    ``secret_reader`` 负责把 provider_id 换成已解密的密钥（API 进程读
    ``app.state.secret_cache``，worker 读自己的预热缓存）。**不传就等于
    "只有环境变量可用"**：一旦配了绑定，没有读取器就装配不出绑定那一份，
    也不会退回去用环境变量（原因见模块注释）。
    """
    binding = _binding(config, EMBED_PROFILE)
    if binding is not None:
        provider = _provider(config, binding, ProviderKind.cloudflare_embedding)
        if provider is None:
            _unusable(
                EMBED_PROFILE,
                binding,
                "供应商不存在、已停用或不是 cloudflare_embedding 类型",
            )
            return None
        secret = secret_reader(provider.provider_id) if secret_reader else None
        if not secret:
            _unusable(EMBED_PROFILE, binding, "还没有保存密钥")
            return None
        extra = provider.extra if isinstance(provider.extra, dict) else {}
        account_id = str(extra.get("account_id") or "").strip()
        if not account_id and not provider.base_url:
            # 校验会在发布前拦下这种配置；这里是第二道防线（旧修订、
            # 手工改库都可能绕过校验）。
            _unusable(EMBED_PROFILE, binding, "既没有 extra.account_id 也没有 base_url")
            return None
        try:
            from ling.models.cloudflare_embedding import CloudflareEmbedding

            # model 来自绑定（"哪个模型"就是设置页选的那个）；
            # account_id / base_url / 超时来自供应商（"怎么连"）。
            return CloudflareEmbedding(
                account_id=account_id,
                api_key=secret,
                model=(binding.model_id or "").strip() or _DEFAULT_EMBED_MODEL,
                base_url=provider.base_url,
                timeout_seconds=provider.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 装配失败要说出来，别装成"未配置"
            logger.warning("memory.embed 装配失败：%s", type(exc).__name__)
            return None
    return _embedding_from_env()


def embedding_source(config: EffectiveConfig | None = None) -> str | None:
    """这份嵌入能力从哪来：``binding`` / ``env`` / None。

    只做**声明级**判断（不读密钥内容，只看 ``has_secret``），所以设置页
    可以在每次读配置时调用它。为什么需要它：`unbound_degradations` 要说
    "语义检索不可用"，而环境变量配好的部署明明能用——不区分来源就会对着
    正常部署报警。
    """
    binding = _binding(config, EMBED_PROFILE)
    if binding is not None:
        provider = _provider(config, binding, ProviderKind.cloudflare_embedding)
        if provider is not None and provider.has_secret:
            return SOURCE_BINDING
        # 绑了但用不了：**不是**退回环境变量的情况（build_embedding 同理
        # 返回 None），所以这里也只能报 None——否则设置页会一边说"来自环境
        # 变量"，一边实际没有向量检索。
        return None
    if os.environ.get("LING_EMBEDDING_API_KEY") and (
        os.environ.get("LING_EMBEDDING_ACCOUNT_ID")
        or os.environ.get("LING_EMBEDDING_API_BASE")
    ):
        return SOURCE_ENV
    return None


# ------------------------------------------------------------------ 重排


def _reranker_from_env():  # noqa: ANN201 - 返回 SiliconFlowRerank | None
    key = os.environ.get("LING_RERANK_API_KEY")
    if not key:
        return None
    try:
        from ling.models.siliconflow_rerank import SiliconFlowRerank

        return SiliconFlowRerank(
            api_key=key,
            model=os.environ.get("LING_RERANK_MODEL", _DEFAULT_RERANK_MODEL),
            base_url=os.environ.get("LING_RERANK_API_BASE", _DEFAULT_RERANK_BASE),
        )
    except Exception:  # noqa: BLE001
        return None


def build_reranker(
    config: EffectiveConfig | None = None,
    *,
    secret_reader: Callable[[str], str | None] | None = None,
):  # noqa: ANN201 - 返回 SiliconFlowRerank | None
    """构造重排提供方。优先级与 ``build_embedding`` 相同。"""
    binding = _binding(config, RERANK_PROFILE)
    if binding is not None:
        provider = _provider(config, binding, ProviderKind.siliconflow_rerank)
        if provider is None:
            _unusable(
                RERANK_PROFILE,
                binding,
                "供应商不存在、已停用或不是 siliconflow_rerank 类型",
            )
            return None
        secret = secret_reader(provider.provider_id) if secret_reader else None
        if not secret:
            _unusable(RERANK_PROFILE, binding, "还没有保存密钥")
            return None
        try:
            from ling.models.siliconflow_rerank import SiliconFlowRerank

            return SiliconFlowRerank(
                api_key=secret,
                model=(binding.model_id or "").strip() or _DEFAULT_RERANK_MODEL,
                base_url=provider.base_url or _DEFAULT_RERANK_BASE,
                timeout_seconds=provider.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory.rerank 装配失败：%s", type(exc).__name__)
            return None
    return _reranker_from_env()


def rerank_source(config: EffectiveConfig | None = None) -> str | None:
    """这份重排能力从哪来：``binding`` / ``env`` / None。"""
    binding = _binding(config, RERANK_PROFILE)
    if binding is not None:
        provider = _provider(config, binding, ProviderKind.siliconflow_rerank)
        if provider is not None and provider.has_secret:
            return SOURCE_BINDING
        return None
    if os.environ.get("LING_RERANK_API_KEY"):
        return SOURCE_ENV
    return None


__all__ = [
    "EMBED_PROFILE",
    "RERANK_PROFILE",
    "SOURCE_BINDING",
    "SOURCE_ENV",
    "build_embedding",
    "build_reranker",
    "embedding_source",
    "rerank_source",
]
