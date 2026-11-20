"""Translate framework HTTP failures to the v1 error contract."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response

from ling.api.errors import LingError, ling_error_handler
from ling.contracts.v1 import ErrorCode

_HTTP_ERRORS: dict[int, tuple[ErrorCode, str]] = {
    401: (ErrorCode.unauthorized, "需要登录"),
    403: (ErrorCode.forbidden, "无权访问该资源"),
    404: (ErrorCode.not_found, "资源不存在"),
    405: (ErrorCode.invalid_request, "请求方法不受支持"),
    409: (ErrorCode.conflict, "请求与当前状态冲突"),
    429: (ErrorCode.rate_limited, "请求过于频繁"),
}


class PublicValidationError(ValueError):
    """Opt a validator into a client hint using only constant, server-owned text."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def is_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    if not is_v1_path(request.url.path):
        return await request_validation_exception_handler(request, exc)

    errors = exc.errors()
    first = errors[0] if errors else {}
    public_hint = first.get("ctx", {}).get("error")
    if isinstance(public_hint, PublicValidationError):
        field, message = public_hint.field, str(public_hint)
    else:
        loc = first.get("loc", ())
        # Nested locations can contain caller-provided object keys. Report the
        # outer field, never parser text, the input, or an unknown top-level key.
        location = loc[0] if loc else "body"
        name = loc[1] if len(loc) > 1 else None
        if first.get("type") == "extra_forbidden" and len(loc) == 2:
            name = None
        field = name if isinstance(name, str) and name else str(location)
        label = "请求体" if field == "body" else field
        message = f"{label} 参数不合法"
    return await ling_error_handler(
        request,
        LingError(
            ErrorCode.invalid_request,
            message,
            status_code=422,
            detail=field,
            field=field,
        ),
    )


async def v1_http_error_handler(request: Request, exc: HTTPException) -> Response:
    status = exc.status_code
    if not is_v1_path(request.url.path) or status < 400:
        return await http_exception_handler(request, exc)
    code, message = _HTTP_ERRORS.get(
        status,
        (ErrorCode.internal, "服务端内部错误")
        if status >= 500 else (ErrorCode.invalid_request, "请求不合法"),
    )
    response = await ling_error_handler(
        request, LingError(code, message, status_code=status, retriable=status >= 500 or status == 429)
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    # ServerErrorMiddleware still reports the original exception to the server.
    if not is_v1_path(request.url.path):
        return PlainTextResponse("Internal Server Error", status_code=500)
    return await ling_error_handler(
        request, LingError(ErrorCode.internal, "服务端内部错误", status_code=500, retriable=True)
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LingError, ling_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, v1_http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
