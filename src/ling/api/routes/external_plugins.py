"""Owner/admin management API for registered external plugins."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from ling.api.deps import CurrentAuth, DbSession, SettingsDep
from ling.api.errors import LingError, forbidden, invalid
from ling.capabilities.external_plugin_contract import RegisterBody
from ling.capabilities.external_plugins_service import (
    ExternalPluginService,
    PluginIdentityMismatch,
    PluginServiceError,
    to_public,
)
from ling.contracts.v1 import ErrorCode

router = APIRouter(tags=["external-plugins"])


class _EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class _SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    force: bool = False


def _admin(auth: CurrentAuth) -> None:
    if auth.user.role not in {"owner", "admin"}:
        raise forbidden("外部插件设置仅管理员可访问")


def _service(request: Request, session: DbSession) -> ExternalPluginService:
    return ExternalPluginService(session)


@router.post("/plugins/register")
async def register_plugin(
    body: RegisterBody,
    auth: CurrentAuth,
    request: Request,
    session: DbSession,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    try:
        row = await service.register(body, force=force)
    except PluginServiceError as exc:
        raise invalid(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - do not disclose endpoint internals
        raise LingError(
            ErrorCode.invalid_request, "插件登记失败，请检查端点是否可达且符合 v1 契约", status_code=422
        ) from exc
    return to_public(row)


@router.get("/plugins")
async def list_plugins(
    auth: CurrentAuth, request: Request, session: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    rows = await service.list()
    return {
        "plugins": [to_public(row) for row in rows],
        "legacy": await service.list_legacy(settings.data_dir),
    }


@router.get("/plugins/{plugin_id}/status")
async def plugin_status(
    plugin_id: str, auth: CurrentAuth, request: Request, session: DbSession
) -> dict[str, Any]:
    _admin(auth)
    try:
        return await _service(request, session).status(plugin_id)
    except PluginServiceError as exc:
        raise LingError(ErrorCode.not_found, str(exc), status_code=404) from exc


@router.post("/plugins/{plugin_id}/enable")
async def enable_plugin(
    plugin_id: str,
    body: _EnableBody,
    auth: CurrentAuth,
    request: Request,
    session: DbSession,
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    try:
        row = await service.set_enabled(plugin_id, body.enabled)
    except PluginIdentityMismatch as exc:
        # Persist the quarantine even though the requested enable failed.
        await session.commit()
        raise invalid(str(exc)) from exc
    except PluginServiceError as exc:
        raise invalid(str(exc)) from exc
    return to_public(row)


@router.post("/plugins/{plugin_id}/disable")
async def disable_plugin(
    plugin_id: str, auth: CurrentAuth, request: Request, session: DbSession
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    try:
        row = await service.set_enabled(plugin_id, False)
    except PluginServiceError as exc:
        raise invalid(str(exc)) from exc
    return to_public(row)


@router.post("/plugins/{plugin_id}/sync")
async def sync_plugin(
    plugin_id: str, auth: CurrentAuth, request: Request, session: DbSession
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    try:
        row = await service.sync(plugin_id)
    except PluginIdentityMismatch as exc:
        await session.commit()
        raise invalid(str(exc)) from exc
    except PluginServiceError as exc:
        raise invalid(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - same boundary as register
        raise LingError(
            ErrorCode.invalid_request, "插件同步失败，请检查端点是否可达且符合 v1 契约", status_code=422
        ) from exc
    return to_public(row)


@router.delete("/plugins/{plugin_id}")
async def unregister_plugin(
    plugin_id: str, auth: CurrentAuth, request: Request, session: DbSession
) -> dict[str, Any]:
    _admin(auth)
    service = _service(request, session)
    try:
        await service.unregister(plugin_id)
    except PluginServiceError as exc:
        raise LingError(ErrorCode.not_found, str(exc), status_code=404) from exc
    return {"id": plugin_id, "deleted": True}
