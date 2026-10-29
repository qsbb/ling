"""模型供应商错误分类：401 必须能与其他错误区分。

实际抓到的缺陷：`ModelErrorKind.auth` 与 `.error` 都被设成 "provider_error"，
于是 auth 成了 error 的**别名**：
    ModelErrorKind.auth is ModelErrorKind.error  → True
401（密钥无效/无权限）被当成通用 provider 错误，经 ErrorCode(kind.value)
映射后丢掉"鉴权失败"语义，用户拿不到"去检查密钥"这类可执行提示。

本测试用本地假服务器返回真实状态码，验证分类链路端到端正确。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import ErrorCode
from ling.ports.model import ModelError, ModelErrorKind

pytestmark = pytest.mark.w0_web


def test_401_maps_to_unauthorized_not_generic() -> None:
    """核心断言：401 的分类不能再和通用错误混同。"""
    assert ModelErrorKind.auth is not ModelErrorKind.error
    assert ModelErrorKind.auth.value != ModelErrorKind.error.value
    assert ErrorCode(ModelErrorKind.auth.value) is ErrorCode.unauthorized
    assert ErrorCode(ModelErrorKind.error.value) is ErrorCode.provider_error


def test_auth_error_is_not_retriable() -> None:
    """密钥错了重试没用——必须标记不可重试，否则会反复打平台。"""
    error = ModelError(ModelErrorKind.auth, "鉴权失败", retriable=False)
    assert error.retriable is False


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        (ModelErrorKind.unavailable, ErrorCode.provider_unavailable),
        (ModelErrorKind.timeout, ErrorCode.provider_timeout),
        (ModelErrorKind.error, ErrorCode.provider_error),
        (ModelErrorKind.auth, ErrorCode.unauthorized),
        (ModelErrorKind.missing_secret, ErrorCode.secret_missing),
        (ModelErrorKind.unsupported, ErrorCode.unsupported_capability),
    ],
)
def test_every_kind_maps_to_a_distinct_error_code(
    kind: ModelErrorKind, expected_code: ErrorCode
) -> None:
    """每种失败都必须映射到明确且有意义的错误码。"""
    assert ErrorCode(kind.value) is expected_code


def test_all_kinds_map_to_unique_codes() -> None:
    """不同失败类别不能被压成同一个错误码。"""
    codes = [ErrorCode(k.value) for k in ModelErrorKind]
    assert len(set(codes)) == len(codes), f"有重复映射: {codes}"


async def test_provider_401_raises_auth_kind() -> None:
    """端到端：真正发出请求并收到 401 时，kind 必须是 auth。

    用本地假服务器返回 401，不走公网。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_api_key"}')

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from ling.models.openai_compatible import OpenAICompatibleProvider
        from ling.ports.model import ChatMessageIn, ModelRequest

        provider = OpenAICompatibleProvider(
            provider_id="fake401",
            base_url=f"http://127.0.0.1:{port}",
            api_key="sk-not-valid",
            timeout_seconds=5.0,
        )
        request = ModelRequest(
            model_id="test-model",
            messages=[ChatMessageIn(role="user", content="hi")],
        )
        with pytest.raises(ModelError) as exc:
            async for _ in provider.stream(request):
                pass
        assert exc.value.kind is ModelErrorKind.auth, (
            f"401 被分类成 {exc.value.kind.name}（值 {exc.value.kind.value}）"
        )
        assert exc.value.retriable is False
    finally:
        server.shutdown()
