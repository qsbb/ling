"""真实模型对话验证（需要已授权的供应商凭据，否则跳过）。

有凭据时做小范围真实验证；
没有凭据时**不得**把 fake provider 的回复当成模型能力。

运行方式（凭据只在环境变量里，不进仓库）：

    export LING_REAL_PROVIDER_BASE_URL=http://192.0.2.10:8080/v1
    export LING_REAL_PROVIDER_KEY=sk-...
    export LING_REAL_PROVIDER_MODEL=<你的模型名>
    pytest tests/integration/test_real_model.py -m w0_web -q

未设置 ``LING_REAL_PROVIDER_KEY`` 时整个模块跳过，不会伪造通过。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web

BASE_URL = os.environ.get("LING_REAL_PROVIDER_BASE_URL", "")
API_KEY = os.environ.get("LING_REAL_PROVIDER_KEY", "")
MODEL_ID = os.environ.get("LING_REAL_PROVIDER_MODEL", "")

requires_real = pytest.mark.skipif(
    not (BASE_URL and API_KEY and MODEL_ID),
    reason="需要 LING_REAL_PROVIDER_{BASE_URL,KEY,MODEL}（未配置则不做真实模型验证）",
)


async def _publish_real_profile(env: TestEnv) -> None:
    """把 dialogue.fast 绑定到真实供应商，并关闭测试模式。"""
    draft = await env.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {
                    "provider_id": "real",
                    "kind": "openai_compatible",
                    "base_url": BASE_URL,
                    "enabled": True,
                }
            ],
            # 非测试模式下所有已绑定的对话职责都必须指向真实供应商，
            # 否则校验会拦下（这是防止 fake 冒充模型能力的硬门）
            "profiles": [
                {
                    "profile": profile,
                    "provider_id": "real",
                    "model_id": MODEL_ID,
                    "temperature": 0.7,
                    "timeout_seconds": 90,
                }
                for profile in ("dialogue.fast", "dialogue.deep", "dialogue.group")
            ],
            "test_mode": False,
        },
    )
    assert draft.status_code == 200, draft.text
    saved = await env.client.put(
        "/v1/settings/providers/real/secret", json={"secret": API_KEY}
    )
    assert saved.status_code == 200, saved.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text


@requires_real
async def test_real_model_completes_and_is_labeled(logged_in: TestEnv) -> None:
    """真实模型能完成一轮，并且消息带真实 model_label（不是 fake）。"""
    await _publish_real_profile(logged_in)
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "真实模型"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "用一句话回答：你是做什么的？"},
    )
    assert response.status_code == 200
    events = await read_sse(response)
    assert events[-1][0] == "turn.completed", events[-1]
    data = events[-1][1]
    assert data["model_label"].startswith("real/"), data["model_label"]
    text = data["message"]["text"]
    assert text.strip(), "真实模型返回了空文本"
    # 绝不能出现 fake provider 的测试标记
    assert "测试模式·确定性" not in text


@requires_real
async def test_real_model_streams_multiple_deltas(logged_in: TestEnv) -> None:
    """真实模型必须逐段流式，而不是一次性返回整段。"""
    await _publish_real_profile(logged_in)
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "真实流式"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "数到十，用顿号分隔"},
    )
    events = await read_sse(response)
    deltas = [d["text"] for name, d in events if name == "delta"]
    assert len(deltas) >= 2, f"应有多段增量，实际 {len(deltas)} 段"
    assert "".join(deltas).strip()


@requires_real
async def test_real_model_keeps_conversation_context(logged_in: TestEnv) -> None:
    """多轮：第二轮能回答第一轮说过什么（服务端历史确实进了上下文）。"""
    await _publish_real_profile(logged_in)
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "真实多轮"})
    ).json()["session_id"]
    secret_word = "蓝鲸"
    first = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={
            "client_turn_id": str(uuid.uuid4()),
            "text": f"请记住这个词：{secret_word}。只回复“好”。",
        },
    )
    await read_sse(first)
    second = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "我刚才让你记住的词是什么？"},
    )
    events = await read_sse(second)
    assert events[-1][0] == "turn.completed"
    assert secret_word in events[-1][1]["message"]["text"]


@requires_real
async def test_bad_key_surfaces_actionable_error(logged_in: TestEnv) -> None:
    """无效密钥必须给出可执行错误，且**不得**回退到 fake provider。"""
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {
                    "provider_id": "real",
                    "kind": "openai_compatible",
                    "base_url": BASE_URL,
                    "enabled": True,
                }
            ],
            "profiles": [
                {
                    "profile": profile,
                    "provider_id": "real",
                    "model_id": MODEL_ID,
                    "timeout_seconds": 30,
                }
                for profile in ("dialogue.fast", "dialogue.deep", "dialogue.group")
            ],
            "test_mode": False,
        },
    )
    await logged_in.client.put(
        "/v1/settings/providers/real/secret", json={"secret": "sk-invalid-key-for-test"}
    )
    await logged_in.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "坏密钥"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "这条应该失败"},
    )
    events = await read_sse(response)
    assert events[-1][0] == "turn.failed", "无效密钥必须报错，不能静默成功"
    error = events[-1][1]["error"]
    assert error["code"] in {"provider_error", "provider_unavailable"}
    # 鉴权失败重试也没用：retriable 必须为 False（重试只会浪费额度）
    assert error["retriable"] is False, error
    # 关键：不能回退成功（那等于用假回复冒充模型）
    assert "测试模式·确定性" not in json.dumps(events, ensure_ascii=False)


async def test_fake_fallback_blocked_in_non_test_mode(logged_in: TestEnv) -> None:
    """纯本地规则：非测试模式下 fake provider 不能作为主模型或回退。

    这条不需要真实凭据，因此始终运行。
    """
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {
                    "provider_id": "real",
                    "kind": "openai_compatible",
                    "base_url": "http://127.0.0.1:9/v1",
                    "enabled": True,
                },
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "fake", "model_id": "deterministic"}
            ],
            "test_mode": False,
        },
    )
    body = draft.json()
    fields = [e["field"] for e in body["validation_errors"]]
    assert any("dialogue.fast" in f for f in fields), body
    published = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert published.status_code == 422, "非测试模式绑定 fake 必须被拒绝"


async def test_fake_fallback_rejected(logged_in: TestEnv) -> None:
    """非测试模式下把 fake 当回退也必须被拒绝。"""
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {
                    "provider_id": "real",
                    "kind": "openai_compatible",
                    "base_url": "http://127.0.0.1:9/v1",
                    "enabled": True,
                },
            ],
            "profiles": [
                {
                    "profile": "dialogue.fast",
                    "provider_id": "real",
                    "model_id": "m",
                    "fallback_provider_id": "fake",
                    "fallback_model_id": "deterministic",
                }
            ],
            "test_mode": False,
        },
    )
    fields = [e["field"] for e in draft.json()["validation_errors"]]
    assert any("fallback_provider_id" in f for f in fields), fields
