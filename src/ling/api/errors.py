"""错误模型：把内部异常翻译成契约里的 ErrorResponse。

确定性代码决定 ``retriable``，不由模型或前端猜测。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ling.contracts.v1 import ErrorBody, ErrorCode, ErrorResponse


class LingError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        retriable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retriable = retriable
        self.detail = detail


def error_response(
    code: ErrorCode,
    message: str,
    *,
    status_code: int = 400,
    retriable: bool = False,
    detail: str | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, retriable=retriable, detail=detail, trace_id=trace_id
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def ling_error_handler(request: Request, exc: LingError) -> JSONResponse:
    """错误响应里带上 trace_id，并回写同名响应头。

    没有它的话，用户只能报"刚才报错了"；有了它，把界面上的 ID 发过来就能
    在日志里定位到那一次请求。错误体里也带一份，前端可以直接显示。
    """
    from ling.api.observability import TRACE_HEADER, trace_id_of

    trace_id = trace_id_of(request)
    response = error_response(
        exc.code,
        exc.message,
        status_code=exc.status_code,
        retriable=exc.retriable,
        detail=exc.detail,
        trace_id=trace_id,
    )
    if trace_id:
        response.headers[TRACE_HEADER] = trace_id
    return response


# 常用错误构造器


def unauthorized(message: str = "需要登录") -> LingError:
    return LingError(ErrorCode.unauthorized, message, status_code=401)


def forbidden(message: str = "无权访问该资源") -> LingError:
    return LingError(ErrorCode.forbidden, message, status_code=403)


def not_found(message: str = "资源不存在") -> LingError:
    return LingError(ErrorCode.not_found, message, status_code=404)


def invalid(message: str, detail: str | None = None) -> LingError:
    return LingError(ErrorCode.invalid_request, message, status_code=422, detail=detail)


__all__ = [
    "LingError",
    "error_response",
    "forbidden",
    "invalid",
    "ling_error_handler",
    "not_found",
    "unauthorized",
]
