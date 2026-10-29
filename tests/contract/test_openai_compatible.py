"""OpenAI 兼容适配器的协议层测试。

用一个**本地假 OpenAI 兼容服务**（不是真实模型）验证适配器代码路径：
流式增量解析、[DONE] 终止、401/429/5xx 分类、`/models` 探测、缺密钥。

这不等于"真实模型对话已验证"：真实供应商的鉴权、限流、模型 ID 与
用量回执仍需用真实凭据实测，此处只验证请求与响应形状。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ling.models.openai_compatible import OpenAICompatibleProvider
from ling.ports.model import ChatMessageIn, ModelError, ModelErrorKind, ModelRequest

pytestmark = pytest.mark.w0_web


def _request(**overrides) -> ModelRequest:
    base = {
        "model_id": "test-model",
        "messages": [ChatMessageIn(role="user", content="你好")],
        "profile": "dialogue.fast",
    }
    base.update(overrides)
    return ModelRequest(**base)  # type: ignore[arg-type]


def _sse_lines(*chunks: str) -> bytes:
    lines = []
    for chunk in chunks:
        payload = {
            "choices": [{"delta": {"content": chunk}, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


@pytest.mark.asyncio
async def test_streams_deltas_from_compatible_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "test-model"
        return httpx.Response(
            200,
            content=_sse_lines("你", "好", "呀"),
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    provider = OpenAICompatibleProvider(
        provider_id="compat",
        base_url="http://stub.local/v1",
        api_key="sk-test",
    )
    # 用 MockTransport 替换内部 client：通过 monkeypatch AsyncClient 实现
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        chunks = [c async for c in provider.stream(_request())]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    text = "".join(c.delta for c in chunks)
    assert text == "你好呀"
    assert chunks[-1].done is True
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_missing_secret_is_explicit() -> None:
    provider = OpenAICompatibleProvider(
        provider_id="compat", base_url="http://stub.local/v1", api_key=None
    )
    with pytest.raises(ModelError) as exc:
        [c async for c in provider.stream(_request())]
    assert exc.value.kind is ModelErrorKind.missing_secret
    assert exc.value.retriable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_kind", "retriable"),
    [
        (401, ModelErrorKind.auth, False),
        (429, ModelErrorKind.unavailable, True),
        (500, ModelErrorKind.error, True),
        (400, ModelErrorKind.error, False),
    ],
)
async def test_http_errors_are_classified(status: int, expected_kind, retriable: bool) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b'{"error":"nope"}')

    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        provider = OpenAICompatibleProvider(
            provider_id="compat", base_url="http://stub.local/v1", api_key="sk-test"
        )
        with pytest.raises(ModelError) as exc:
            [c async for c in provider.stream(_request())]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert exc.value.kind is expected_kind
    assert exc.value.retriable is retriable


@pytest.mark.asyncio
async def test_probe_lists_models() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        provider = OpenAICompatibleProvider(
            provider_id="compat", base_url="http://stub.local/v1", api_key="sk-test"
        )
        result = await provider.probe()
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert result.ok is True
    assert result.models == ["m1", "m2"]


@pytest.mark.asyncio
async def test_probe_reports_auth_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        provider = OpenAICompatibleProvider(
            provider_id="compat", base_url="http://stub.local/v1", api_key="sk-bad"
        )
        result = await provider.probe()
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert result.ok is False
    assert "鉴权" in result.detail
