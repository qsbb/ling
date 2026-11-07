"""嵌入/重排的装配优先级：**网页绑定 → 环境变量 → 未配置**。

## 为什么这些用例必须存在

这两项能力的配置原来**只有**环境变量（`LING_EMBEDDING_*` / `LING_RERANK_*`）。
把它们接进职责绑定体系时，最容易犯的错不是"绑定不生效"，而是**升级后把
已有部署的 env 配置静默作废**：

- 症状：语义检索不再工作，检索退回字面匹配；
- 表现：她还在聊天，只是"换个说法就想不起你说过什么"；
- 界面上：没有任何报错，甚至看不出是配置问题。

所以这里盯三件事：

1. 没有绑定、但环境变量配着 → **行为与升级前完全一致**（连派生出来的
   默认 base_url 都不能变）；
2. 有绑定 → 用绑定，且模型来自绑定、连接参数来自供应商条目；
3. 绑定坏了（供应商没了/没密钥/kind 不对）→ **不偷偷退回环境变量**：
   绑定是用户的显式选择，偷偷换回旧的会让人以为新配置已经生效。
"""

from __future__ import annotations

import pytest

from ling.domains.memory.providers import (
    SOURCE_BINDING,
    SOURCE_ENV,
    build_embedding,
    build_reranker,
    embedding_source,
    rerank_source,
)
from ling.models.cloudflare_embedding import CloudflareEmbedding
from ling.models.siliconflow_rerank import SiliconFlowRerank
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig

# 环境变量的完整组合：只留测试关心的几项，避免开发机 shell 漏进来。
EMBED_ENV = {
    "LING_EMBEDDING_API_KEY": "env-embed-key",
    "LING_EMBEDDING_ACCOUNT_ID": "env-account",
    "LING_EMBEDDING_MODEL": "@cf/baai/bge-m3",
}
RERANK_ENV = {
    "LING_RERANK_API_KEY": "env-rerank-key",
    "LING_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
    "LING_RERANK_API_BASE": "https://api.siliconflow.cn/v1",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认**全部清空**：让"没配置"成为基线，而不是开发机的状态。"""
    for name in (
        "LING_EMBEDDING_API_KEY",
        "LING_EMBEDDING_ACCOUNT_ID",
        "LING_EMBEDDING_MODEL",
        "LING_EMBEDDING_API_BASE",
        "LING_RERANK_API_KEY",
        "LING_RERANK_MODEL",
        "LING_RERANK_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_env(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    # 只给 key 不给 account/base 时装配会返回 None，这里补上另一半，
    # 保证"env 配好了"是一个完整可用的配置。
    if "LING_EMBEDDING_API_KEY" in values and not (
        "LING_EMBEDDING_ACCOUNT_ID" in values or "LING_EMBEDDING_API_BASE" in values
    ):
        monkeypatch.setenv("LING_EMBEDDING_ACCOUNT_ID", "env-account")
    if "LING_RERANK_API_KEY" in values and "LING_RERANK_MODEL" not in values:
        monkeypatch.setenv("LING_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def _embed_config(
    *,
    account_id: str = "cf-account",
    base_url: str | None = None,
    model: str = "@cf/baai/bge-m3",
    secret: bool = True,
    enabled: bool = True,
    provider_kind: str = "cloudflare_embedding",
    timeout_seconds: float = 12.0,
) -> EffectiveConfig:
    return EffectiveConfig(
        revision=7,
        providers={
            "cf": ProviderConfig(
                provider_id="cf",
                kind=provider_kind,
                base_url=base_url,
                has_secret=secret,
                enabled=enabled,
                extra={"account_id": account_id} if account_id else {},
                timeout_seconds=timeout_seconds,
            )
        },
        profiles={
            "memory.embed": ProfileBinding(
                profile="memory.embed", provider_id="cf", model_id=model
            )
        },
    )


def _rerank_config(
    *,
    base_url: str | None = "https://api.siliconflow.cn/v1",
    model: str = "BAAI/bge-reranker-v2-m3",
    secret: bool = True,
    provider_kind: str = "siliconflow_rerank",
    timeout_seconds: float = 15.0,
) -> EffectiveConfig:
    return EffectiveConfig(
        revision=8,
        providers={
            "sf": ProviderConfig(
                provider_id="sf",
                kind=provider_kind,
                base_url=base_url,
                has_secret=secret,
                extra={},
                timeout_seconds=timeout_seconds,
            )
        },
        profiles={
            "memory.rerank": ProfileBinding(
                profile="memory.rerank", provider_id="sf", model_id=model
            )
        },
    )


# ---------------------------------------------------------------- 嵌入：三种情况


def test_build_embedding_uses_binding_when_present() -> None:
    """有绑定 → 用绑定：模型来自绑定，account_id / 超时来自供应商条目。"""
    config = _embed_config(model="@cf/baai/bge-m3", timeout_seconds=12.0)
    embedder = build_embedding(config, secret_reader=lambda pid: f"sk-{pid}")

    assert isinstance(embedder, CloudflareEmbedding)
    assert embedder.model_id == "@cf/baai/bge-m3"
    assert embedder.dim == 1024
    # 私有字段：这是唯一能证明连接参数确实来自**供应商条目**（而不是
    # 环境变量）的地方，所以宁可在这里读一次私有属性。
    assert embedder._account == "cf-account"
    assert embedder._base == (
        "https://api.cloudflare.com/client/v4/accounts/cf-account/ai/v1"
    )
    assert embedder._timeout == 12.0
    assert embedder._key == "sk-cf"


def test_build_embedding_prefers_binding_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """绑定与环境变量同时存在时以绑定为准——否则新供应商永远不生效。"""
    _set_env(monkeypatch, EMBED_ENV)
    config = _embed_config(account_id="binding-account", model="@cf/binding-model")

    embedder = build_embedding(config, secret_reader=lambda pid: "sk-binding")

    assert isinstance(embedder, CloudflareEmbedding)
    assert embedder.model_id == "@cf/binding-model"
    assert embedder._account == "binding-account"
    assert embedder._key == "sk-binding"


def test_build_embedding_uses_env_when_no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """无绑定但有环境变量 → 走环境变量，**行为与升级前逐项一致**。"""
    _set_env(monkeypatch, EMBED_ENV)

    embedder = build_embedding()

    assert isinstance(embedder, CloudflareEmbedding)
    assert embedder.model_id == "@cf/baai/bge-m3"
    assert embedder._account == "env-account"
    # 这条是新旧实现最容易漂移的一处：base_url 由 account_id 派生。
    assert embedder._base == (
        "https://api.cloudflare.com/client/v4/accounts/env-account/ai/v1"
    )
    assert embedder._key == "env-embed-key"


def test_build_embedding_uses_env_when_published_config_lacks_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**升级场景**：已发布配置存在，但没有 memory.embed 绑定。

    这正是所有既有 env 部署升级后的状态：他们有已发布修订（甚至绑了
    dialogue.fast），只是从没绑过嵌入。这条若挂了，他们的语义检索会在
    升级后无声失效。
    """
    _set_env(monkeypatch, EMBED_ENV)
    config = EffectiveConfig(
        revision=3,
        providers={
            "main": ProviderConfig(
                provider_id="main", kind="openai_compatible", base_url="https://x/v1"
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="main", model_id="m"
            )
        },
    )

    embedder = build_embedding(config, secret_reader=lambda pid: "sk-main")

    assert isinstance(embedder, CloudflareEmbedding)
    assert embedder._account == "env-account"
    assert embedder._key == "env-embed-key"


def test_build_embedding_returns_none_when_nothing_configured() -> None:
    """绑定和环境变量都没有 → None（检索退回 BM25+RRF）。"""
    assert build_embedding() is None
    assert build_embedding(EffectiveConfig(revision=1)) is None


def test_broken_binding_does_not_fall_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """绑定存在但拿不到密钥 → None，**不退回环境变量**。

    绑定是显式选择。偷偷用旧的 env 配置会让人误以为新供应商已经生效，
    而这种"看起来配好了"比"没配"更难排查。
    """
    _set_env(monkeypatch, EMBED_ENV)
    config = _embed_config(secret=False)

    assert build_embedding(config, secret_reader=lambda pid: None) is None
    assert embedding_source(config) is None


def test_binding_without_account_and_base_cannot_be_built() -> None:
    """既没 account_id 也没 base_url：装配不出来，不猜一个地址。"""
    config = _embed_config(account_id="", base_url=None)

    assert build_embedding(config, secret_reader=lambda pid: "sk-cf") is None


def test_binding_with_only_base_url_is_allowed() -> None:
    """只给 base_url 不给 account_id 是合法配置（环境变量那套一直支持）。"""
    config = _embed_config(
        account_id="", base_url="https://proxy.example/cf/ai/v1"
    )

    embedder = build_embedding(config, secret_reader=lambda pid: "sk-cf")

    assert isinstance(embedder, CloudflareEmbedding)
    assert embedder._base == "https://proxy.example/cf/ai/v1"


# ---------------------------------------------------------------- 重排：三种情况


def test_build_reranker_uses_binding_when_present() -> None:
    config = _rerank_config(model="BAAI/bge-reranker-v2-m3", timeout_seconds=15.0)
    reranker = build_reranker(config, secret_reader=lambda pid: "sk-sf")

    assert isinstance(reranker, SiliconFlowRerank)
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"
    assert reranker._base == "https://api.siliconflow.cn/v1"
    assert reranker._timeout == 15.0
    assert reranker._key == "sk-sf"


def test_build_reranker_uses_env_when_no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, RERANK_ENV)

    reranker = build_reranker()

    assert isinstance(reranker, SiliconFlowRerank)
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"
    assert reranker._base == "https://api.siliconflow.cn/v1"
    assert reranker._key == "env-rerank-key"


def test_build_reranker_returns_none_when_nothing_configured() -> None:
    assert build_reranker() is None


def test_rerank_binding_defaults_base_url_when_omitted() -> None:
    """绑定没给 base_url 时用 SiliconFlow 官方端点，而不是空字符串。"""
    config = _rerank_config(base_url=None)

    reranker = build_reranker(config, secret_reader=lambda pid: "sk-sf")

    assert isinstance(reranker, SiliconFlowRerank)
    assert reranker._base == "https://api.siliconflow.cn/v1"


def test_build_reranker_returns_none_when_nothing_configured_with_config() -> None:
    """有配置但没有 rerank 绑定、也没有 env → None。"""
    assert build_reranker(_embed_config()) is None


# ---------------------------------------------------------------- 来源标记


def test_source_reports_where_the_capability_comes_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源标记是设置页判断"该不该报降级"的唯一依据。"""
    assert embedding_source() is None
    assert rerank_source() is None

    _set_env(monkeypatch, {**EMBED_ENV, **RERANK_ENV})
    assert embedding_source() == SOURCE_ENV
    assert rerank_source() == SOURCE_ENV

    config = _embed_config()
    assert embedding_source(config) == SOURCE_BINDING


# ---------------------------------------------------------------- 服务层接线


def test_memory_service_keeps_env_embedder_when_nothing_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env-only 部署升级后，MemoryService 里的 embedder 必须还在。

    这是「静默失效」最直接的观测点：没有 embedder，检索就只剩字面匹配，
    而任何日志都不会说"你少配了一样东西"。
    """
    from ling.domains.memory.service import MemoryService

    _set_env(monkeypatch, {**EMBED_ENV, **RERANK_ENV})
    service = MemoryService(None)  # 构造时不查库，session 只被方法用到

    assert service._embedder is not None
    assert service._embedder.model_id == "@cf/baai/bge-m3"
    assert service._reranker is not None
    assert service._reranker.model_id == "BAAI/bge-reranker-v2-m3"


def test_memory_service_uses_binding_when_given_config() -> None:
    """传了配置就用绑定：模型是设置页选的那个。"""
    from ling.domains.memory.service import MemoryService

    service = MemoryService(
        None, config=_embed_config(model="@cf/binding-model"), secret_reader=lambda pid: "k"
    )

    assert service._embedder is not None
    assert service._embedder.model_id == "@cf/binding-model"


def test_context_compiler_threads_config_and_secret_reader() -> None:
    """接线闸：ContextCompiler 必须把配置和密钥读取器传进 MemoryService。

    不传的话，设置页绑定的嵌入模型永远不会被用上——而编译器照样能跑，
    检索照样返回结果（只是没有向量），**没有任何报错**。
    """
    from ling.kernel.context import ContextCompiler

    compiler = ContextCompiler(
        None,  # type: ignore[arg-type] - 构造不查库
        model_config=_embed_config(model="@cf/binding-model"),
        secret_reader=lambda pid: f"sk-{pid}",
    )

    assert compiler._memory._embedder is not None
    assert compiler._memory._embedder.model_id == "@cf/binding-model"
    assert compiler._memory._embedder._key == "sk-cf"
