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


async def ling_error_handler(_: Request, exc: LingError) -> JSONResponse:
    return error_response(
        exc.code,
        exc.message,
        status_code=exc.status_code,
        retriable=exc.retriable,
        detail=exc.detail,
    )


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
