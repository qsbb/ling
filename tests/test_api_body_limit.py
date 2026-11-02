"""请求体大小上限中间件的回归测试（`ling/api/body_limit.py`）。

这些用例**不碰数据库、不起真实服务**：直接用 ASGI 传输把请求打给一个只挂了
这个中间件的最小应用。覆盖：

- 超过上限 → 413，且响应是项目统一的 ``ErrorResponse`` 形状；
- 未超上限 → 正常进入下游，body 原样可读（证明中间件没有吞 body）；
- 无 ``Content-Length`` 的 GET → 放行（否则所有页面加载都会被误拒）；
- 无 ``Content-Length`` 的 POST（chunked）→ 上限内放行，超限同样 413；
- 畸形 ``Content-Length`` → 413，不当成"缺失"；
- 流式响应不被破坏（SSE 的关键约束：中间件绝不能缓冲响应，chunked 请求下也一样）。

上限故意设得很小（64 字节），这样"超限"用不着构造 32MB 的请求体。
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from ling.api.body_limit import BodySizeLimitMiddleware

LIMIT = 64


def _app(*, echo_body: bool = True, max_body_bytes: int = LIMIT) -> FastAPI:
    """最小应用：一个回显路由 + 两条流式路由。"""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> JSONResponse:
        raw = await request.body()
        return JSONResponse({"len": len(raw), "body": raw.decode("utf-8") if echo_body else ""})

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            for index in range(3):
                yield f"data: {index}\n\n".encode()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/stream")
    async def stream_after_body(request: Request) -> StreamingResponse:
        """先读完请求体（chunked 时走计数 receive），再流式返回。"""
        raw = await request.body()

        async def gen():
            for index in range(3):
                yield f"data: {index}:{len(raw)}\n\n".encode()

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=max_body_bytes)
    return app


async def _post(app: FastAPI, body: bytes, headers: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/echo", content=body, headers=headers)


async def _get(app: FastAPI, path: str = "/stream", headers: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


def _whole(chunks: tuple[bytes, ...]):
    """把定长分块包成异步生成器 —— httpx 遇到生成器就发 chunked（无 Content-Length）。"""

    async def gen():
        for chunk in chunks:
            yield chunk

    return gen()


async def _post_chunked(app: FastAPI, chunks: tuple[bytes, ...], path: str = "/echo"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, content=_whole(chunks))


# ---------------------------------------------------------------- 超限 / 未超限


async def test_oversized_body_returns_413_with_contract_shape() -> None:
    app = _app()
    response = await _post(app, b"x" * (LIMIT + 1))

    assert response.status_code == 413
    payload = response.json()
    # 与项目其它错误同一个形状（ling/contracts/v1.py 的 ErrorResponse）。
    assert payload["schema_version"] == "v1"
    assert payload["error"]["code"] == "invalid_request"
    assert payload["error"]["retriable"] is False
    assert "超过上限" in payload["error"]["detail"]
    assert str(LIMIT) in payload["error"]["detail"]
    # 下游路由根本没被调用（否则会看到 echo 的 len）。
    assert "len" not in payload


async def test_exactly_at_limit_is_allowed() -> None:
    """边界是 `>`，不是 `>=`：正好等于上限必须放行。"""
    app = _app()
    response = await _post(app, b"x" * LIMIT)

    assert response.status_code == 200
    assert response.json()["len"] == LIMIT


async def test_under_limit_body_reaches_handler_intact() -> None:
    """未超限时 body 必须原样到达路由 —— 证明中间件没有吞掉/截断 body。"""
    app = _app()
    body = b'{"hello": "ling"}'
    assert len(body) < LIMIT

    response = await _post(app, body)

    assert response.status_code == 200
    assert response.json()["len"] == len(body)
    assert json.loads(response.json()["body"]) == {"hello": "ling"}


async def test_empty_body_is_allowed() -> None:
    app = _app()
    response = await _post(app, b"")

    assert response.status_code == 200
    assert response.json()["len"] == 0


# ------------------------------------------------------- 缺失 / 畸形 Content-Length


async def test_get_without_content_length_is_allowed() -> None:
    """GET 本来就没有 body：缺 Content-Length 绝不能拒（否则页面全 413）。"""
    app = _app()
    response = await _get(app)

    assert response.status_code == 200
    # 流式响应没有被中间件破坏。
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_chunked_post_without_content_length_is_allowed() -> None:
    """chunked 小 body（上限之内）正常通过：计数路径放行，body 原样交给下游。

    这里同时断言**中间件没有预读 body**：放行时它只是把 receive 原样转给
    下游，自己不消费任何 chunk。用"分块发送 + 下游正常读到全部内容"来证明。
    """
    app = _app()

    async def chunks():
        yield b"a" * 8
        yield b"b" * 8

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=chunks())

    # httpx 用生成器发送时会走 chunked，因此没有 Content-Length。
    assert response.status_code == 200
    assert response.json()["len"] == 16


async def test_chunked_oversized_body_returns_413() -> None:
    """chunked 累计字节超限 → 413，形状与 Content-Length 超限那条完全一致。"""
    app = _app(max_body_bytes=8)

    # 两块 8 字节 = 16 > 8：中间件在第二块到手时就必须中断。
    response = await _post_chunked(app, (b"a" * 8, b"b" * 8))
    assert response.status_code == 413

    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    assert "请求体过大" in payload["error"]["message"]
    assert "超过上限" in payload["error"]["detail"]
    # 下游路由没被走到（否则会看到 echo 的 len 字段）。
    assert "len" not in payload

    # 与 Content-Length 路径共用同一套 ErrorResponse 形状：字段集合逐层一致。
    cl_payload = (await _post(app, b"x" * 9)).json()
    assert set(payload) == set(cl_payload)
    assert set(payload["error"]) == set(cl_payload["error"])
    for field in ("code", "message", "retriable"):
        assert payload["error"][field] == cl_payload["error"][field]


async def test_chunked_under_limit_body_reaches_handler_intact() -> None:
    """chunked 未超限 → 200，且下游拿到的长度等于全部 chunk 之和。"""
    app = _app()
    chunks = (b"hello, ", b"ling", b"!")
    total = sum(len(chunk) for chunk in chunks)
    assert total < LIMIT

    response = await _post_chunked(app, chunks)

    assert response.status_code == 200
    assert response.json()["len"] == total
    assert response.json()["body"] == b"".join(chunks).decode()


@pytest.mark.parametrize("bad", ["-1", "abc", "1.5", "", "  "])
async def test_malformed_content_length_is_rejected(bad: str) -> None:
    """畸形 Content-Length 一律 413，**不**当成"缺失"放行。"""
    app = _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=b"x", headers={"Content-Length": bad})

    assert response.status_code == 413
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    assert "Content-Length 请求头不合法" in payload["error"]["message"]


# ------------------------------------------- 直接喂 ASGI scope（不经过 httpx）


def _scope(method: str, path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    """一份最小但**完整**的 ASGI http scope（FastAPI 会读 query_string 等字段）。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }


async def _drive(scope: dict, app: FastAPI) -> tuple[list[dict], list[bytes]]:
    """把一个 ASGI scope 直接喂给 app，返回 (发出去的 message, 被读过的 body)。"""
    received: list[dict] = []
    consumed: list[bytes] = []

    async def receive() -> dict:
        if consumed:
            return {"type": "http.disconnect"}
        consumed.append(b"x")
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message: dict) -> None:
        received.append(message)

    await app(scope, receive, send)
    return received, consumed


async def test_scope_without_content_length_does_not_touch_body() -> None:
    """缺 Content-Length 时中间件**一个字节都不读**：body 只由下游读。

    这是 SSE/流式能被保住的关键：中间件只做同步的头判断，绝不预读 body。
    """
    app = _app()
    scope = _scope("POST", "/echo", [(b"transfer-encoding", b"chunked")])
    received, consumed = await _drive(scope, app)

    assert received[0]["status"] == 200
    # 下游自己读了一次 body；中间件在放行**之前**没有读（否则会是 2 次）。
    assert consumed == [b"x"]


async def test_scope_with_oversized_content_length_never_calls_downstream() -> None:
    """超限时下游完全不被调用，body 也不会被读进内存。"""
    app = _app()
    scope = _scope("POST", "/echo", [(b"content-length", str(LIMIT + 1).encode())])
    received, consumed = await _drive(scope, app)

    assert received[0]["status"] == 413
    assert consumed == []


async def test_bodiless_method_without_content_length_skips_downstream_body_read() -> None:
    """GET 无 Content-Length：连下游都不读 body，直接走路由。"""
    app = _app()
    scope = _scope("GET", "/stream", [])
    received, _ = await _drive(scope, app)

    assert received[0]["status"] == 200


# ---------------------------------------------------------------- SSE 不被破坏


async def test_streaming_response_is_not_buffered() -> None:
    """中间件绝不能缓冲响应：流式回复必须逐条到达。"""
    app = _app()
    transport = ASGITransport(app=app)
    received: list[bytes] = []
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream("GET", "/stream") as response,
    ):
        assert response.status_code == 200
        async for chunk in response.aiter_raw():
            if chunk:
                received.append(chunk)

    joined = b"".join(received)
    for index in range(3):
        assert f"data: {index}".encode() in joined


async def test_chunked_request_does_not_break_streaming_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """关键回归：包 ``receive`` 只碰请求体，绝不能牵连响应流。

    请求侧走 chunked（无 ``Content-Length``，即计数 receive 那条路），响应侧
    要 ``text/event-stream`` 逐条完整到达 —— 两端同时被包装也互不干扰。
    """
    app = _app()
    chunks = (b"a" * 8, b"b" * 8)
    total = sum(len(chunk) for chunk in chunks)

    received: list[bytes] = []
    with caplog.at_level(logging.INFO, logger="ling.api.body_limit"):
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("POST", "/stream", content=_whole(chunks)) as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            async for chunk in response.aiter_raw():
                if chunk:
                    received.append(chunk)

    # 证明这条用例真的走了 chunked 计数路径，而不是 Content-Length 快路。
    assert any("chunked" in record.getMessage() for record in caplog.records)

    joined = b"".join(received)
    for index in range(3):
        assert f"data: {index}:{total}\n\n".encode() in joined


# ---------------------------------------------------------------- 默认上限


def test_default_limit_matches_former_nginx_setting() -> None:
    """默认值必须等于原 nginx 的 `client_max_body_size 32m`。"""
    from ling.config import Settings

    assert Settings().max_body_bytes == 32 * 1024 * 1024


def test_env_override_changes_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from ling.config import Settings

    monkeypatch.setenv("LING_MAX_BODY_BYTES", "1024")
    assert Settings().max_body_bytes == 1024


# ------------------------------------------------- app 级：中间件顺序与 trace_id
#
# 上面所有用例打的是"只挂了这个中间件"的最小应用 —— 它们证明不了它和
# observability 的**注册顺序**。顺序反了上面照样全绿，但 413 会丢掉 trace_id
# 和访问日志，而线上排查"上传为什么失败"只能靠这两样。下面用 `create_app()`
# 造真实 app（全部中间件 + 真实路由表），把顺序锁死。


def test_observability_is_outermost_middleware() -> None:
    """observability 必须在 BodySizeLimit **外层**（= 后注册）。

    Starlette 的 `add_middleware` 是 `user_middleware.insert(0, …)`，后注册的
    排在列表最前，又在 `build_middleware_stack` 里最后被包 —— 所以**列表越靠前
    越靠外**。先注册 BodySizeLimit、后注册 observability，顺序就对。
    """
    from ling.app import create_app
    from ling.config import Settings

    app = create_app(Settings(max_body_bytes=LIMIT))
    names = [middleware.cls.__name__ for middleware in app.user_middleware]

    assert "BodySizeLimitMiddleware" in names, names
    assert "BaseHTTPMiddleware" in names, names
    assert names.index("BaseHTTPMiddleware") < names.index("BodySizeLimitMiddleware"), (
        f"observability 必须包在 BodySizeLimit 外层（列表更靠前），实际顺序 {names}："
        "反过来时 413 会绕过 trace_id 与访问日志"
    )


async def test_app_413_carries_trace_id_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实 app 的 413 必须带 `X-Trace-Id`，错误体 trace_id 非 null，且进访问日志。"""
    from ling.api.observability import TRACE_HEADER
    from ling.app import create_app
    from ling.config import Settings

    app = create_app(Settings(max_body_bytes=LIMIT))

    with caplog.at_level(logging.INFO, logger="ling.api.access"):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/sessions", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    trace_id = response.headers.get(TRACE_HEADER)
    assert trace_id, "413 没有 X-Trace-Id：用户报「上传失败」时无从定位"
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    # 响应头与错误体是同一个 id，否则用户贴出来的 id 在日志里查不到。
    assert payload["error"]["trace_id"] == trace_id

    # 访问日志里必须有一条 413：超大请求正是最该被记录的事件。
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "http_request_completed"
        and (getattr(record, "attributes", None) or {}).get("http.response.status_code") == 413
    ]
    assert completed, [record.getMessage() for record in caplog.records]
