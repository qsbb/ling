"""供应商能力 + 记忆角色绑定：设置页能看见什么、什么组合会被拦下。

## 这条链路原来断在哪

`memory.embed` / `memory.rerank` 是真在调用模型的角色，但配置路径完全在
绑定体系之外（只有 `LING_EMBEDDING_*` / `LING_RERANK_*` 环境变量）。设置页
看不到它们配没配、用的什么模型，用户以为配好了，其实语义检索一直没生效。

接进绑定体系之后，必须同时满足两件事：

1. **选不到调不通的组合**：嵌入/重排与 chat 是不同协议，混在一个下拉里必然
   选出"发布成功、调用必失败"的配对。所以供应商带能力、角色要能力。
2. **env 部署不能被骗**：`memory_sources` 要说清能力来自绑定还是环境变量，
   否则界面会把"用环境变量配好的"报成语义检索失效——那会让人去乱改配置。

`dialogue.deep` 的移除也在本文件里守一条：它不能回到可绑清单。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CLOUDFLARE = {
    "provider_id": "cf",
    "kind": "cloudflare_embedding",
    "enabled": True,
    "models": [{"model_id": "@cf/baai/bge-m3", "note": "1024 维 · 多语言"}],
}
SILICONFLOW = {
    "provider_id": "sf",
    "kind": "siliconflow_rerank",
    "base_url": "https://api.siliconflow.cn/v1",
    "enabled": True,
    "models": [{"model_id": "BAAI/bge-reranker-v2-m3"}],
}


async def _draft(env: TestEnv, body: dict) -> dict:
    response = await env.client.put("/v1/settings/draft", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _errors(body: dict) -> list[dict]:
    return [e for e in body["validation_errors"] if e["severity"] == "error"]


def _warnings(body: dict) -> list[dict]:
    return [e for e in body["validation_errors"] if e["severity"] == "warning"]


async def _publish(env: TestEnv, revision: int) -> None:
    response = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert response.status_code == 200, response.text


# ------------------------------------------------------------ 配置面（能力信息）


async def test_config_advertises_capabilities_and_profile_requirements(
    logged_in: TestEnv,
) -> None:
    """`/settings/config` 必须把"谁能干什么""谁要什么"讲清楚。

    前端的绑定下拉就靠这两个字段过滤。少了任何一半，过滤都无从下手，
    只能继续把全部供应商列给全部角色。
    """
    body = (await logged_in.client.get("/v1/settings/config")).json()

    # 引导配置里只有 fake（chat）。
    fake = next(p for p in body["providers"] if p["provider_id"] == "fake")
    assert fake["capabilities"] == ["chat"]

    capabilities = body["profile_capabilities"]
    assert capabilities["dialogue.fast"] == "chat"
    assert capabilities["perception.vision"] == "chat"
    assert capabilities["memory.embed"] == "embed"
    assert capabilities["memory.rerank"] == "rerank"

    kinds = set(body["known_kinds"])
    assert {"cloudflare_embedding", "siliconflow_rerank"} <= kinds, kinds


async def test_memory_roles_are_offerable_and_dialogue_deep_is_not(
    logged_in: TestEnv,
) -> None:
    """`memory.embed` / `memory.rerank` 可绑；`dialogue.deep` 退到保留名单。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()

    offerable = set(body["known_profiles"])
    reserved = body["reserved_profiles"]

    assert {"memory.embed", "memory.rerank"} <= offerable
    assert not ({"memory.embed", "memory.rerank"} & set(reserved))

    assert "dialogue.deep" not in offerable, (
        "dialogue.deep 又出现在可绑清单里：全仓没有代码按它请求模型，"
        "列着它等于承诺一个不存在的能力"
    )
    assert "dialogue.deep" in reserved
    assert "尚无实现" in reserved["dialogue.deep"]


# ------------------------------------------------------------ 必填字段差异


async def test_cloudflare_embedding_requires_account_id(logged_in: TestEnv) -> None:
    """Cloudflare 要 `extra.account_id`（没有它拼不出官方端点）。"""
    body = await _draft(
        logged_in,
        {
            "providers": [CLOUDFLARE],
            "profiles": [
                {
                    "profile": "memory.embed",
                    "provider_id": "cf",
                    "model_id": "@cf/baai/bge-m3",
                }
            ],
        },
    )

    fields = {e["field"] for e in _errors(body)}
    assert "providers.cf.extra.account_id" in fields, body

    # 发布也必须被拦下：没 account_id 的嵌入供应商发布出去，语义检索会
    # 在第一次检索时静默失效（装配返回 None）。
    blocked = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert blocked.status_code == 422, blocked.text


async def test_cloudflare_embedding_with_account_id_publishes(logged_in: TestEnv) -> None:
    """补上 account_id 就能通过，并且读回来的供应商带 `embed` 能力。"""
    provider = dict(CLOUDFLARE, extra={"account_id": "acc-123"})
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {
                    "profile": "memory.embed",
                    "provider_id": "cf",
                    "model_id": "@cf/baai/bge-m3",
                }
            ],
        },
    )
    assert not _errors(body), body
    await _publish(logged_in, body["revision"])

    config = (await logged_in.client.get("/v1/settings/config")).json()
    cf = next(p for p in config["providers"] if p["provider_id"] == "cf")
    assert cf["capabilities"] == ["embed"]
    assert cf["extra"] == {"account_id": "acc-123"}
    bound = {p["profile"] for p in config["profiles"]}
    assert "memory.embed" in bound, "绑定没有落进已发布配置"


async def test_cloudflare_embedding_accepts_base_url_instead_of_account_id(
    logged_in: TestEnv,
) -> None:
    """只给 base_url 也合法——环境变量那套一直支持这种配法。"""
    provider = dict(CLOUDFLARE, base_url="https://proxy.example/cf/ai/v1")
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {
                    "profile": "memory.embed",
                    "provider_id": "cf",
                    "model_id": "@cf/baai/bge-m3",
                }
            ],
        },
    )
    assert not _errors(body), body


async def test_siliconflow_rerank_requires_base_url(logged_in: TestEnv) -> None:
    """SiliconFlow 要 base_url（它没有"按账号拼端点"的规则），但不要 account_id。"""
    provider = {k: v for k, v in SILICONFLOW.items() if k != "base_url"}
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {
                    "profile": "memory.rerank",
                    "provider_id": "sf",
                    "model_id": "BAAI/bge-reranker-v2-m3",
                }
            ],
        },
    )

    fields = {e["field"] for e in _errors(body)}
    assert "providers.sf.base_url" in fields, body
    # 必填项的差异必须真的不同：Cloudflare 那条要 account_id，这条不要。
    assert "providers.sf.extra.account_id" not in fields, body


async def test_siliconflow_rerank_with_base_url_warns_only_about_secret(
    logged_in: TestEnv,
) -> None:
    """必填项齐了就只差密钥：warning，不阻断发布。"""
    body = await _draft(
        logged_in,
        {
            "providers": [SILICONFLOW],
            "profiles": [
                {
                    "profile": "memory.rerank",
                    "provider_id": "sf",
                    "model_id": "BAAI/bge-reranker-v2-m3",
                }
            ],
        },
    )
    assert not _errors(body), body
    warnings = _warnings(body)
    assert any(
        e["field"] == "providers.sf.secret" and "密钥" in e["message"] for e in warnings
    ), body


# ------------------------------------------------------------ 能力错配


async def test_binding_memory_embed_to_chat_provider_is_blocked(
    logged_in: TestEnv,
) -> None:
    """把嵌入绑到对话供应商必须**在发布前**被拒。

    放过去的话，发布成功、设置页显示"已绑定"，而装配时只会拿到 None：
    语义检索静默失效。这正是这次要修的毛病。
    """
    body = await _draft(
        logged_in,
        {
            "profiles": [
                {"profile": "memory.embed", "provider_id": "fake", "model_id": "m"}
            ],
        },
    )

    blocking = _errors(body)
    assert any(
        e["field"] == "profiles.memory.embed.provider_id" and "embed" in e["message"]
        for e in blocking
    ), body


async def test_binding_chat_role_to_embedding_provider_is_blocked(
    logged_in: TestEnv,
) -> None:
    """反方向同样拦下：对话角色不能落在只做嵌入的供应商上。"""
    provider = dict(CLOUDFLARE, extra={"account_id": "acc-123"})
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "cf", "model_id": "x"}
            ],
        },
    )

    assert any(
        e["field"] == "profiles.dialogue.fast.provider_id" and "chat" in e["message"]
        for e in _errors(body)
    ), body


async def test_fallback_provider_must_have_the_same_capability(
    logged_in: TestEnv,
) -> None:
    """回退供应商也要能力对得上：换一家"能连但不具备该能力"的等于换一种失败。"""
    provider = dict(CLOUDFLARE, extra={"account_id": "acc-123"})
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {
                    "profile": "memory.embed",
                    "provider_id": "cf",
                    "model_id": "@cf/baai/bge-m3",
                    "fallback_provider_id": "fake",
                    "fallback_model_id": "m",
                }
            ],
        },
    )

    assert any(
        e["field"] == "profiles.memory.embed.fallback_provider_id"
        and "embed" in e["message"]
        for e in _errors(body)
    ), body


# ------------------------------------------------------------ env 部署的回归


async def test_env_only_deployment_is_not_reported_as_degraded(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**关键回归**：只用环境变量配好的部署，升级后不能被报成"没配"。

    症状会很恶劣：没有绑定 → `unbound_degradations` 说"语义检索不可用"，
    用户去设置页绑定，反而绕开了本来能用的 env 配置。所以这里必须按**来源**
    判断，而不是按"有没有绑定"判断。
    """
    monkeypatch.setenv("LING_EMBEDDING_API_KEY", "env-key")
    monkeypatch.setenv("LING_EMBEDDING_ACCOUNT_ID", "env-acc")
    monkeypatch.setenv("LING_RERANK_API_KEY", "env-rerank-key")

    body = (await logged_in.client.get("/v1/settings/config")).json()

    assert body["memory_sources"] == {
        "memory.embed": "env",
        "memory.rerank": "env",
    }
    assert "memory.embed" not in body["unbound_degradations"]
    assert "memory.rerank" not in body["unbound_degradations"]
    # 别的降级提示不能被这条过滤顺手抹掉（否则就是另一种假绿）。
    assert "perception.vision" in body["unbound_degradations"]


async def test_unconfigured_memory_roles_say_what_degrades(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定和环境变量都没有 → 必须说清失去什么。"""
    for name in (
        "LING_EMBEDDING_API_KEY",
        "LING_EMBEDDING_ACCOUNT_ID",
        "LING_EMBEDDING_API_BASE",
        "LING_RERANK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    body = (await logged_in.client.get("/v1/settings/config")).json()

    assert body["memory_sources"] == {"memory.embed": None, "memory.rerank": None}
    assert "向量" in body["unbound_degradations"]["memory.embed"]
    assert "精排" in body["unbound_degradations"]["memory.rerank"]


async def test_published_memory_binding_reports_binding_source(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定成了之后，来源要变成 `binding`，降级提示随之消失。"""
    monkeypatch.delenv("LING_EMBEDDING_API_KEY", raising=False)
    provider = dict(CLOUDFLARE, extra={"account_id": "acc-123"})
    body = await _draft(
        logged_in,
        {
            "providers": [provider],
            "profiles": [
                {
                    "profile": "memory.embed",
                    "provider_id": "cf",
                    "model_id": "@cf/baai/bge-m3",
                }
            ],
        },
    )
    await _publish(logged_in, body["revision"])
    # 密钥单独保存（不在草稿里），保存后会同步 has_secret 并升一个修订。
    saved = await logged_in.client.put(
        "/v1/settings/providers/cf/secret", json={"secret": "cf-secret-value"}
    )
    assert saved.status_code == 200, saved.text

    config = (await logged_in.client.get("/v1/settings/config")).json()
    assert config["memory_sources"]["memory.embed"] == "binding"
    assert "memory.embed" not in config["unbound_degradations"]
