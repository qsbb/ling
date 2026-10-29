"""认证路由：登录、登出、当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.domains.identity.service import IdentityService

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    subject_id: str
    display_name: str


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
    result = await identity.login(
        body.username, body.password, user_agent=request.headers.get("user-agent")
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
