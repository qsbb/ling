"""供应商的模型清单：存得住、改不丢、停用要说清。

模型清单不是一个装饰性字段：设置页的「哪些模型可用」「哪个职责指向了已停用的
模型」都读它。所以这里守三条：

1. 发布后读回来必须一字不差（前端不能自己造一份）；
2. 老客户端保存供应商时不带 ``models``，**不能**把已有清单清空；
3. 停用模型/供应商后旧绑定仍然存在——发布不拦截（用户可能只是临时停用），
   但必须给出 warning，让设置页能说「需要检查」。
"""

from __future__ import annotations

from datetime import datetime

from tests.integration.conftest import TestEnv


async def _publish_models(logged_in: TestEnv) -> dict:
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {
                    "provider_id": "fake",
                    "kind": "fake",
                    "enabled": True,
                    "models": [
                        {"model_id": "deterministic", "note": "确定性测试回复"},
                        {"model_id": "vision-1", "note": "支持图片", "enabled": False},
                    ],
                }
            ],
            "profiles": [
                {
                    "profile": "dialogue.fast",
                    "provider_id": "fake",
                    "model_id": "deterministic",
                }
            ],
            "test_mode": True,
        },
    )
    body = draft.json()
    assert not [e for e in body["validation_errors"] if e["severity"] == "error"], body
    published = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert published.status_code == 200, published.text
    return body


async def test_models_round_trip(logged_in: TestEnv) -> None:
    await _publish_models(logged_in)
    config = (await logged_in.client.get("/v1/settings/config")).json()
    provider = next(p for p in config["providers"] if p["provider_id"] == "fake")
    assert provider["models"] == [
        {"model_id": "deterministic", "note": "确定性测试回复", "enabled": True},
        {"model_id": "vision-1", "note": "支持图片", "enabled": False},
    ]


async def test_omitting_models_keeps_the_existing_list(logged_in: TestEnv) -> None:
    """旧版控制台保存时只传 provider_id/kind/enabled——不能把清单清空。"""
    await _publish_models(logged_in)
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
            ],
        },
    )
    body = draft.json()
    assert not [e for e in body["validation_errors"] if e["severity"] == "error"], body
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})
    config = (await logged_in.client.get("/v1/settings/config")).json()
    provider = next(p for p in config["providers"] if p["provider_id"] == "fake")
    assert [m["model_id"] for m in provider["models"]] == ["deterministic", "vision-1"]


async def test_binding_a_disabled_model_warns_but_publishes(logged_in: TestEnv) -> None:
    await _publish_models(logged_in)
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "profiles": [
                {
                    "profile": "dialogue.fast",
                    "provider_id": "fake",
                    "model_id": "vision-1",
                }
            ],
        },
    )
    body = draft.json()
    warnings = [e for e in body["validation_errors"] if e["severity"] == "warning"]
    assert any("vision-1" in e["message"] for e in warnings), body
    assert not [e for e in body["validation_errors"] if e["severity"] == "error"], body
    published = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert published.status_code == 200, published.text


async def test_multiple_enabled_providers_warn(logged_in: TestEnv) -> None:
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {
                    "provider_id": "other",
                    "kind": "fake",
                    "enabled": True,
                },
            ],
        },
    )
    body = draft.json()
    warnings = [e for e in body["validation_errors"] if e["severity"] == "warning"]
    assert any("同时启用了 2 家" in e["message"] for e in warnings), body


async def test_probe_reports_checked_at(logged_in: TestEnv) -> None:
    await _publish_models(logged_in)
    response = await logged_in.client.post("/v1/settings/providers/fake/probe")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["latency_ms"], int)
    assert datetime.fromisoformat(body["checked_at"])
