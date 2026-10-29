"""API 依赖：会话认证、作用域解析与共享服务构造。

客户端提交的 actor_id/subject_id 一律忽略；作用域由已认证会话重新解析。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import unauthorized
from ling.config import Settings
from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService, UserView
from ling.domains.persona.service import PersonaService
from ling.infra.db import session_scope
from ling.models.router import ModelRouter
from ling.settings.service import ConfigService


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_router(request: Request) -> ModelRouter:
    return request.app.state.router


@dataclass(frozen=True)
class AuthContext:
    user: UserView
    token: str


async def current_auth(request: Request, session: DbSession) -> AuthContext:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.cookie_name) or ""
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        raise unauthorized()
    user = await IdentityService(session).resolve_token(token)
    if user is None:
        raise unauthorized("登录已过期，请重新登录")
    return AuthContext(user=user, token=token)


CurrentAuth = Annotated[AuthContext, Depends(current_auth)]


def conversation_service(request: Request, session: DbSession) -> ConversationService:
    settings: Settings = request.app.state.settings
    return ConversationService(
        session, actor_id=settings.actor_id, persona_id=settings.persona_id
    )


def persona_service(request: Request, session: DbSession) -> PersonaService:
    settings: Settings = request.app.state.settings
    return PersonaService(session, settings.persona_id)


def config_service(request: Request, session: DbSession) -> ConfigService:
    settings: Settings = request.app.state.settings
    return ConfigService(
        session,
        request.app.state.secret_box,
        test_mode_default=settings.test_mode,
        persona_id=settings.persona_id,
    )


ConversationDep = Annotated[ConversationService, Depends(conversation_service)]
PersonaDep = Annotated[PersonaService, Depends(persona_service)]
ConfigDep = Annotated[ConfigService, Depends(config_service)]
RouterDep = Annotated[ModelRouter, Depends(get_router)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
