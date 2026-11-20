"""Authentication and browser-origin policies for the control plane."""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request

from ling.api.deps import AuthContext, CurrentAuth
from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode

DETAIL_CONTENT_TYPE_REQUIRED = "content_type_required"
DETAIL_ORIGIN_REQUIRED = "origin_required"
DETAIL_ORIGIN_MISMATCH = "origin_mismatch"


@dataclass(frozen=True)
class OwnerGuard:
    missing_message: str | None = None

    def __call__(self, auth: CurrentAuth) -> AuthContext:
        if auth.user.role == "owner":
            return auth
        if self.missing_message is not None:
            raise not_found(self.missing_message)
        raise LingError(ErrorCode.forbidden, "需要所有者权限", status_code=403, detail="not_owner")


require_owner = OwnerGuard()
OwnerAuth = Annotated[AuthContext, Depends(require_owner)]


def _origin_key(scheme: str, netloc: str) -> tuple[str, str, int] | None:
    scheme = scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    try:
        parsed = urlsplit(f"{scheme}://{netloc}")
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port) if host and 1 <= port <= 65535 else None


def _origin_problem(request: Request, auth: AuthContext) -> str | None:
    # Use the credential current_auth actually accepted. A Bearer header does
    # not exempt a request authenticated by its Cookie.
    if auth.source != "cookie":
        return None
    origin = request.headers.get("origin")
    if origin is None:
        return DETAIL_ORIGIN_REQUIRED
    if not origin or any(ch.isspace() for ch in origin):
        return DETAIL_ORIGIN_MISMATCH
    try:
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return DETAIL_ORIGIN_MISMATCH
    except ValueError:
        return DETAIL_ORIGIN_MISMATCH
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return DETAIL_ORIGIN_MISMATCH

    # Use the request URL as seen by the server, not raw forwarded headers.
    expected = _origin_key(request.url.scheme, request.url.netloc)
    if expected is None or _origin_key(parsed.scheme, parsed.netloc) != expected:
        return DETAIL_ORIGIN_MISMATCH
    return None


def _require_content_type(request: Request, expected: str) -> None:
    actual = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if actual != expected:
        raise LingError(
            ErrorCode.invalid_request,
            f"请求格式应为 {expected}",
            status_code=422,
            detail=DETAIL_CONTENT_TYPE_REQUIRED,
        )


def _require_json_content_type(request: Request) -> None:
    _require_content_type(request, "application/json")


@dataclass(frozen=True)
class OwnerWriteGuard:
    """Same owner and Cookie-origin rules, with an explicit request format."""

    content_type: str | None = "application/json"
    update_origin_details: bool = False

    def __call__(self, request: Request, auth: OwnerAuth) -> AuthContext:
        if self.content_type == "application/json":
            _require_json_content_type(request)
        elif self.content_type is not None:
            _require_content_type(request, self.content_type)
        problem = _origin_problem(request, auth)
        if problem is not None:
            raise LingError(
                ErrorCode.forbidden,
                "这个请求的来源不可信",
                status_code=403,
                detail=problem if self.update_origin_details else "bad_origin",
            )
        return auth


OwnerWrite = Annotated[AuthContext, Depends(OwnerWriteGuard())]
OwnerWriteAction = Annotated[AuthContext, Depends(OwnerWriteGuard(content_type=None))]
OwnerWriteUpload = Annotated[AuthContext, Depends(OwnerWriteGuard(content_type="multipart/form-data"))]
# Preserve the update client's two specific origin error details.
OwnerWriteUpdate = Annotated[AuthContext, Depends(OwnerWriteGuard(update_origin_details=True))]
