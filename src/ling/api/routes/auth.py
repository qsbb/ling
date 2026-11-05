"""认证路由：登录、登出、当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.api.errors import LingError
from ling.audit import audit_event
from ling.contracts.v1 import ErrorCode
from ling.domains.identity.service import IdentityService, LoginRateLimiter

router = APIRouter(tags=["auth"])


def _login_limiter(request: Request) -> LoginRateLimiter:
    """取本进程的登录限速计数器（首次访问时惰性挂到 ``app.state``）。

    为什么要惰性：``app.state.login_limiter`` 在 ``lifespan`` 里创建，
    而少数测试夹具直接用 ``create_app()`` 的 ASGI transport、不走 lifespan。
    惰性兜底让两边行为一致，且仍然**每个 app 一份**（不是模块全局）。
    """
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = LoginRateLimiter()
        request.app.state.login_limiter = limiter
    return limiter


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    subject_id: str
    display_name: str
    # 首次安装的随机口令还没改过时为 True；前端据此强制跳改密页
    must_change_password: bool = False


class LoginOut(BaseModel):
    schema_version: str = "v1"
    user: UserOut
    config_revision: int
    test_mode: bool
    csrf_token: str


@router.post("/auth/login", response_model=LoginOut)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    config: ConfigDep,
) -> LoginOut:
    identity = IdentityService(session)
    # TTL 必须**从配置派生并写进库里那一列**，不能只在 cookie 的 max_age 上生效。
    #
    # 服务端判定会话是否有效（`IdentityService.resolve_token`）读的是
    # `auth_sessions.expires_at`。原来 service 里硬编码 +14 天，于是：
    # 把 LING_SESSION_TTL 调小只是让浏览器不再自动带 cookie —— 持有 token
    # 的人（被窃 cookie / 重放 / 直接带 `Authorization: Bearer`，`deps.py`
    # 支持这种用法）仍能用满 14 天；调大则完全无效（DB 会先到期）。
    # 现在 cookie 的 max_age 与库里那一列同源。
    #
    # 失败限速由 `IdentityService.login` 内部处理；这里只把**来源 IP**与
    # **进程内计数器**传下去：
    # - 计数器挂在 `app.state` 上，语义是「本进程内」。多副本部署时各副本
    #   独立计数（实际阈值 ≈ 副本数 × 阈值），不假装是全局限速，
    #   见 `LoginRateLimiter` 的类注释。
    # - IP 取自 `request.client.host`。uvicorn 按 `--proxy-headers` 解析
    #   `X-Forwarded-For`，**没有可信反代时这个值可被伪造**，per-IP 那一路
    #   就失效；per-username 那一路仍然有效。
    limiter = _login_limiter(request)
    result = await identity.login(
        body.username,
        body.password,
        user_agent=request.headers.get("user-agent"),
        ttl_seconds=settings.session_ttl_seconds,
        client_ip=request.client.host if request.client else None,
        limiter=limiter,
    )
    response.set_cookie(
        key=settings.cookie_name,
        value=result.token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    effective = await config.effective()
    return LoginOut(
        user=UserOut(**result.user.__dict__),
        config_revision=effective.revision,
        test_mode=effective.test_mode,
        csrf_token="cookie-samesite-lax",
    )


class PasswordBody(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=8, max_length=256)


@router.post("/auth/password", status_code=204)
async def change_password(
    body: PasswordBody,
    auth: CurrentAuth,
    session: DbSession,
) -> Response:
    """改密码。改完清掉"必须先改"标记，并吊销其它会话（保留当前这次登录）。"""
    if body.current == body.new:
        raise LingError(
            ErrorCode.invalid_request, "新密码不能与当前密码相同", status_code=422
        )
    await IdentityService(session).change_password(
        auth.user.id,
        current=body.current,
        new=body.new,
        keep_token=auth.token,
    )
    audit_event(
        "password_changed",
        actor=auth.user.id,
        target_type="user",
        target_id=auth.user.id,
    )
    return Response(status_code=204)


class AccountBody(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=3, max_length=64)


@router.patch("/auth/account")
async def change_account(body: AccountBody, auth: CurrentAuth, session: DbSession) -> dict:
    """改登录名。**要求当前口令**，改完吊销其它会话。

    为什么不在 GET /v1/auth/me 里做：那是个只读探测，混进写操作会让
    前端的"刷新登录态"变成有副作用的请求。
    """
    name = await IdentityService(session).change_username(
        auth.user.id,
        current=body.current,
        username=body.username,
        keep_token=auth.token,
    )
    audit_event(
        "account_renamed",
        actor=auth.user.id,
        target_type="user",
        target_id=auth.user.id,
    )
    return {"username": name}


@router.post("/auth/logout")
async def logout(
    auth: CurrentAuth, session: DbSession, response: Response, settings: SettingsDep
) -> dict[str, bool]:
    await IdentityService(session).logout(auth.token)
    # 必须用配置里的 cookie 名：自定义 LING_COOKIE_NAME 时硬编码会清不掉
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/auth/me", response_model=LoginOut)
async def me(auth: CurrentAuth, config: ConfigDep) -> LoginOut:
    effective = await config.effective()
    return LoginOut(
        user=UserOut(**auth.user.__dict__),
        config_revision=effective.revision,
        test_mode=effective.test_mode,
        csrf_token="cookie-samesite-lax",
    )
