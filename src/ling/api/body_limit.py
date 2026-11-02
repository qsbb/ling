"""请求体大小上限（原来是 nginx 的 ``client_max_body_size 32m``）。

## 为什么需要

删掉 edge/nginx 之后，那条"单个请求体最多 32MB"的限制跟着消失了。uvicorn
会把请求体读进**内存**，所以没有上限意味着一个超大上传（或一个恶意的
``Content-Length``）就能把 api 进程打爆 —— 而且容器根文件系统只读 + tmpfs，
连 swap 都指望不上。因此这条限制必须由应用自己承担，**不能依赖前置反代**：
默认部署里没有反代，api 端口直接对外。

## 两条路径

1. **有 ``Content-Length``**：解析后 O(1) 判断，在调用下游 app 之前就能拒绝，
   请求体一个字节都不会被读进内存。这是最优路径。
2. **没有 ``Content-Length``**（``Transfer-Encoding: chunked``，或 GET/HEAD 这类
   本来没有 body 的请求）：包一层 ``receive``，按实际到手的 chunk 累加字节数，
   超过上限立即中断读取。

包 ``receive`` **只涉及请求体**，不缓存、不改写：每条 message 原样透传给下游，
中间件只在旁边累加 ``len(body)``。SSE 是**响应**流（走 ``send``），与请求体的
读取路径互不相干，所以这个包装对响应流零影响（回归测试里有专门的 SSE 用例）。

无 ``Content-Length`` 不等于放行：

- 无 body 语义的方法（GET/HEAD/OPTIONS/DELETE/TRACE）本来就该缺这个头，计数
  路径对它们只是空转；
- 带 body 语义的方法（POST/PUT/PATCH）缺这个头只可能是 chunked，计数路径会
  真的拦下超限请求。

## chunked 超限时怎么拒

计数超限时在包装后的 ``receive`` 里抛 ``_BodyTooLarge``，由本中间件接住：

- **响应还没开始**（下游尚未发出 ``http.response.start``）→ 返回 413，形状与
  ``Content-Length`` 路径完全一致；
- **响应已经开始**（下游一边读 body 一边已经写出了响应头，极少见）→ 状态码
  改不了了，直接 ``raise`` 让 ASGI 服务器断开连接，不把半截响应当成成功。

## ``Content-Length`` 的严格性

存在但不是合法非负整数（负数、小数、非 ASCII 数字、空串）一律**拒绝**，不当成
"缺失"：畸形头本来就该被拒，放行等于让不同实现的解析差异变成绕过点。

## 响应形状

413 用项目统一的 ``ErrorResponse``（见 ``ling/api/errors.py``），错误码取
``invalid_request`` —— 契约 ``ErrorCode`` 里没有专门的"too_large"，新增枚举
会改变对外契约。``detail`` 写清上限和实际值，``retriable`` 为假（重试同一个
请求不会变小）。
"""

from __future__ import annotations

import logging

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ling.contracts.v1 import ErrorCode

logger = logging.getLogger("ling.api.body_limit")

# 这些方法按 HTTP 规范**不该**带请求体：缺 Content-Length 时不必当作 chunked
# 报警，计数路径对它们也只是空转。
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "TRACE"})

_CONTENT_LENGTH = b"content-length"
_TRANSFER_ENCODING = b"transfer-encoding"


class _BodyTooLarge(Exception):
    """chunked 累计字节超限（中间件内部信号，不对外暴露）。"""

    def __init__(self, received: int) -> None:
        super().__init__(f"累计 {received} 字节，超过上限")
        self.received = received


def _header(scope: Scope, name: bytes) -> str | None:
    """从 ASGI scope 里取一个请求头（同名多值取最后一个，与 Starlette 一致）。"""
    found: str | None = None
    for key, raw in scope.get("headers") or []:
        if key.lower() == name:
            found = raw.decode("latin-1")
    return found


def _parse_length(raw: str) -> int | None:
    """把 ``Content-Length`` 解析成非负整数；畸形值返回 ``None``（调用方拒绝）。"""
    text = raw.strip()
    if not text or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def _too_large(scope: Scope, reason: str, detail: str) -> Response:
    """构造 413，形状与项目其它错误一致（``ErrorResponse``）。"""
    from ling.api.errors import error_response

    trace_id = (scope.get("state") or {}).get("trace_id")
    return error_response(
        ErrorCode.invalid_request,
        reason,
        status_code=413,
        detail=detail,
        trace_id=trace_id if isinstance(trace_id, str) else None,
    )


class BodySizeLimitMiddleware:
    """纯 ASGI 中间件：``Content-Length`` 直接判，chunked 靠包 ``receive`` 计数。

    为什么是纯 ASGI 而不是 ``BaseHTTPMiddleware``：

    - ``BaseHTTPMiddleware`` 会给每个请求套一层任务组/流包装，对流式响应是
      额外风险；这里只在 ASGI 层包 ``receive``/``send``，不建任务组、不缓冲；
    - 有 ``Content-Length`` 时仍能在调用下游 app 之前返回，body 一个字节都不会
      被读进内存 —— 这正是这条限制想要的效果。
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        raw = _header(scope, _CONTENT_LENGTH)
        if raw is None:
            # 无 Content-Length：chunked 或本来无 body，改由计数路径兜底。
            await self._app_with_counted_receive(scope, receive, send)
            return

        length = _parse_length(raw)
        if length is None:
            response = _too_large(
                scope,
                "Content-Length 请求头不合法",
                f"Content-Length={raw[:32]!r}",
            )
            await response(scope, receive, send)
            return

        if length > self.max_body_bytes:
            response = _too_large(
                scope,
                "请求体过大",
                f"Content-Length={length} 超过上限 {self.max_body_bytes} 字节",
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _app_with_counted_receive(self, scope: Scope, receive: Receive, send: Send) -> None:
        """没有 ``Content-Length``：包 ``receive`` 累加字节，超限即中断。"""
        method = str(scope.get("method", "GET")).upper()
        if method not in _BODYLESS_METHODS and _header(scope, _TRANSFER_ENCODING):
            logger.info(
                "请求体无 Content-Length（chunked），按实际读取字节计数上限：%s %s",
                method,
                scope.get("path"),
            )

        limit = self.max_body_bytes
        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body") or b""
                if body:
                    received += len(body)
                    if received > limit:
                        raise _BodyTooLarge(received)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge as exc:
            if response_started:
                # 响应头已发出，改不了状态码；抛出去让 ASGI 服务器断开连接。
                logger.warning(
                    "请求体超过上限 %s 字节（累计 %s），但响应已开始，断开连接：%s %s",
                    limit,
                    exc.received,
                    method,
                    scope.get("path"),
                )
                raise
            logger.warning(
                "请求体超过上限 %s 字节（累计 %s，无 Content-Length/chunked），返回 413：%s %s",
                limit,
                exc.received,
                method,
                scope.get("path"),
            )
            response = _too_large(
                scope,
                "请求体过大",
                f"已读取 {exc.received} 字节 超过上限 {limit} 字节（无 Content-Length，chunked）",
            )
            # 用原始 receive：不再经过计数包装，避免构造 413 时二次抛错。
            await response(scope, receive, send)


__all__ = ["BodySizeLimitMiddleware"]
