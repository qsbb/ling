"""请求级可观测性：给每个请求一个 trace_id，并让它出现在响应头与日志里。

## 为什么需要

排查"她为什么没回这条消息"时，最先遇到的困难不是没有日志，而是**日志和
用户看到的请求对不上**：用户说"我刚才发消息报错了"，日志里有几十条记录，
没法确定哪几条属于他那次请求。

## trace_id 的规则

- 客户端可以带 `X-Trace-Id` 进来（便于前端把 ID 打在错误页上再回来查），
  但只接受 8–64 位十六进制的值；其它一切字符串都丢弃、由服务端生成。
  不校验的话，用户可以在 header 里塞任意长字符串，把日志字段撑爆。
- 生成后写回响应头 `X-Trace-Id`，错误响应体里也带 `trace_id`。
- 只在请求处理期间绑定到 contextvars，请求结束就 reset——**绝不让上一个
  请求的身份字段漏进下一个请求**。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from ling.logging_setup import (
    bind_context,
    current_context,
    emit_event,
    new_trace_id,
    reset_context,
)

logger = logging.getLogger("ling.api.access")

TRACE_HEADER = "X-Trace-Id"
_VALID_TRACE = re.compile(r"^[0-9a-fA-F]{8,64}$")

# 这些路径每个健康检查都会打，没必要逐条记
_QUIET_PATHS = frozenset(
    {"/healthz", "/readyz", "/v1/auth/me", "/v1/health", "/v1/health/model"}
)


def _trace_from_request(request: Request) -> str:
    incoming = request.headers.get(TRACE_HEADER) or ""
    if _VALID_TRACE.match(incoming):
        return incoming.lower()
    return new_trace_id()


async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    trace_id = _trace_from_request(request)
    request.state.trace_id = trace_id

    # 用**路由模板**而不是原始路径作为字段：`/v1/sessions/{id}` 才是可聚合的，
    # 原始路径每个 ID 都不同，按它统计会得到一堆只出现一次的"接口"。
    token = bind_context(
        trace_id=trace_id,
        process_role="api",
        http_method=request.method,
        http_route=request.url.path,
    )
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 - 记录后交给上层异常处理器
        emit_event(
            logger,
            logging.ERROR,
            event_name="http_request_failed",
            message="请求处理抛出异常",
            attributes={
                "http.request.method": request.method,
                "http.request.path": request.url.path,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "error_type": type(exc).__name__,
            },
        )
        # 错误响应由异常处理器生成，trace 头在那边补（见 errors.py）
        raise
    finally:
        reset_context(token)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers[TRACE_HEADER] = trace_id

    if request.url.path not in _QUIET_PATHS:
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        emit_event(
            logger,
            level,
            event_name="http_request_completed",
            message="请求完成",
            attributes={
                "http.request.method": request.method,
                "http.request.path": request.url.path,
                "http.response.status_code": response.status_code,
                "duration_ms": elapsed_ms,
            },
        )
    return response


def trace_id_of(request: Request) -> str | None:
    """取当前请求的 trace_id（没有就返回 None，不编一个）。"""
    value = getattr(request.state, "trace_id", None)
    if isinstance(value, str):
        return value
    return current_context().get("trace_id")


__all__ = ["TRACE_HEADER", "observability_middleware", "trace_id_of"]
