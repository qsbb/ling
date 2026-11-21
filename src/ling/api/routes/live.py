"""Custom event ingress and owner-controlled live sessions."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from ling.api.deps import DbSession, SettingsDep
from ling.api.errors import LingError, not_found
from ling.api.guards import OwnerAuth, OwnerWrite, OwnerWriteAction
from ling.contracts.v1 import ErrorCode
from ling.domains.live.autonomy import prepare_autonomy
from ling.domains.live.control import StageStatus, update_stage
from ling.domains.live.ingress import (
    CUSTOM_SCOPE,
    CustomConfig,
    CustomEvent,
    configure_custom,
    custom_view,
    require_custom,
    require_ingress,
    require_stage,
)
from ling.domains.live.room import (
    ACTIVE,
    active_session,
    open_session,
    receive_event,
    save_viewer,
    session_view,
)
from ling.domains.media.assets import AssetError, GeneratedMediaService
from ling.infra.models import ConnectorInstance, LiveSessionRow, LiveViewerRow, utcnow
from ling.infra.security import load_signing_root_key

router = APIRouter(tags=["livestream"])


def _config_key(value: str, *, field: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,15}", value) is None or value == "default":
        raise LingError(
            ErrorCode.invalid_request, "请输入 1 到 16 位小写字母、数字、下划线或连字符",
            status_code=422, field=field,
        )
    return value


@router.get("/settings/custom-connectors")
async def list_custom(_auth: OwnerAuth, session: DbSession) -> dict:
    rows = await session.scalars(select(ConnectorInstance).where(
        ConnectorInstance.scope == CUSTOM_SCOPE, ConnectorInstance.deleted_at.is_(None),
    ).order_by(ConnectorInstance.created_at))
    return {"connectors": [custom_view(row) for row in rows]}


@router.put("/settings/custom-connectors/{instance_id}")
async def set_custom(
    instance_id: str, body: CustomConfig, request: Request, auth: OwnerWrite, session: DbSession,
) -> dict:
    row = await configure_custom(
        session, request, _config_key(instance_id, field="id"), body, created_by=auth.user.id,
    )
    return custom_view(row)


@router.put("/settings/live-features/{feature}/{instance_id}")
async def enable_feature(
    feature: str, instance_id: str, body: CustomConfig, request: Request,
    auth: OwnerWrite, session: DbSession,
) -> dict:
    if feature != "bilibili":
        raise not_found("直播功能不存在")
    row = await configure_custom(
        session, request, _config_key(instance_id, field="id"), body,
        created_by=auth.user.id, feature=feature,
    )
    return custom_view(row)


@router.get("/live/sessions/current")
async def current_live(_auth: OwnerAuth, session: DbSession, settings: SettingsDep) -> dict:
    run = await active_session(session, settings.actor_id)
    if run is None:
        run = await session.scalar(select(LiveSessionRow).where(
            LiveSessionRow.actor_id == settings.actor_id,
        ).order_by(LiveSessionRow.created_at.desc()).limit(1))
    return {"session": session_view(run) if run else None}


class OpenLive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=16)
    mode: Literal["preview", "broadcast"]


@router.post("/live/sessions", status_code=201)
async def start_live(
    body: OpenLive, auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> dict:
    row = await require_custom(session, body.connector_id, lock=True)
    run = await open_session(
        session, row, actor_id=settings.actor_id, persona_id=settings.persona_id,
        subject_id=auth.user.subject_id, mode=body.mode,
    )
    return session_view(run)


async def _get_session(
    session: DbSession, settings: SettingsDep, session_id: str, *, lock: bool = False,
) -> LiveSessionRow:
    run = await session.get(LiveSessionRow, session_id, with_for_update=lock, populate_existing=lock)
    if run is None or run.actor_id != settings.actor_id:
        raise not_found("直播场次不存在")
    return run


@router.post("/live/sessions/{session_id}/stop")
async def stop_live(
    session_id: str, _auth: OwnerWriteAction, session: DbSession, settings: SettingsDep,
) -> dict:
    run = await _get_session(session, settings, session_id, lock=True)
    if run.state in ACTIVE:
        from ling.domains.live.room import finish_session

        if not run.runtime.get("device_id") and not run.runtime.get("start_operation"):
            finish_session(run, detail="本场已结束")
        else:
            run.state = "stopping"
    return session_view(run)


class SaveViewer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_ref: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=300)
    merge_into: str | None = Field(default=None, max_length=64)


@router.put("/live/sessions/{session_id}/viewers")
async def remember_viewer(
    session_id: str, body: SaveViewer, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> dict:
    run = await _get_session(session, settings, session_id, lock=True)
    row = await save_viewer(
        session, run, account_ref=body.account_ref, note=body.note, merge_into=body.merge_into,
    )
    return {"account_ref": row.account_ref, "display_name": row.display_name, "note": row.note}


@router.get("/live/viewers/{instance_id}")
async def list_viewers(instance_id: str, _auth: OwnerAuth, session: DbSession) -> dict:
    await require_custom(session, instance_id)
    rows = await session.scalars(select(LiveViewerRow).where(LiveViewerRow.connector_id == instance_id))
    return {"viewers": [
        {"account_ref": row.account_ref, "display_name": row.display_name, "note": row.note}
        for row in rows
    ]}


@router.delete("/live/viewers/{instance_id}")
async def forget_viewer(
    instance_id: str, account_ref: str, _auth: OwnerWriteAction, session: DbSession,
) -> dict:
    connector = await require_custom(session, instance_id)
    row = await session.get(
        LiveViewerRow, (instance_id, connector.custom_config["room_id"], account_ref),
    )
    if row is None:
        raise not_found("没有保存这位观众的房间备注")
    await session.delete(row)
    return {"deleted": True}


@router.get("/custom/{instance_id}/session")
async def source_session(
    instance_id: str, request: Request, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id)
    require_ingress(request, connector, b"")
    run = await active_session(session, settings.actor_id)
    return {
        "session_id": run.id if run and run.connector_id == instance_id else None,
        "room_id": connector.custom_config["room_id"],
    }


@router.post("/custom/{instance_id}/events")
async def push_custom_event(
    instance_id: str, request: Request, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id, lock=True)
    body = await request.body()
    require_ingress(request, connector, body)
    try:
        event = CustomEvent.model_validate_json(body)
    except ValidationError as exc:
        raise LingError(ErrorCode.invalid_request, "事件格式有误", status_code=422) from exc
    now = utcnow()
    config = connector.custom_config or {}
    if event.room_id != config["room_id"]:
        raise LingError(ErrorCode.forbidden, "事件房间与接入器配置不一致", status_code=403)
    last = config.get("last_received_at")
    if not last or now - datetime.fromisoformat(last) >= timedelta(seconds=5):
        connector.custom_config = {**config, "last_received_at": now.isoformat()}
    run = await active_session(session, settings.actor_id, lock=True)
    if run is None or run.connector_id != instance_id:
        return {"status": "no_session"}
    return {"status": receive_event(run, event, now=now)}


@router.post("/custom/{instance_id}/stage")
async def stage_tick(
    instance_id: str, body: StageStatus, request: Request, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id, lock=True)
    require_stage(request, connector.custom_config or {})
    run = await active_session(session, settings.actor_id, lock=True)
    if run is None and not body.obs_active and not body.obs_reconnecting:
        action = prepare_autonomy(
            session, connector, device_id=body.device_id, broadcast_ready=body.broadcast_ready,
        )
        return {"session_id": None, "action": action}
    if run is None or run.connector_id != instance_id:
        return {"session_id": None, "action": "hold"}
    return await update_stage(session, run, body)


@router.get("/custom/{instance_id}/stage/audio/{session_id}/{speech_id}")
async def stage_audio(
    instance_id: str, session_id: str, speech_id: str, request: Request,
    session: DbSession, settings: SettingsDep,
) -> Response:
    connector = await require_custom(session, instance_id)
    require_stage(request, connector.custom_config or {})
    run = await _get_session(session, settings, session_id)
    pending = run.runtime.get("speech")
    if (
        run.connector_id != instance_id or run.state not in {"preview", "live"}
        or utcnow() >= run.deadline_at
        or not pending or pending["id"] != speech_id or pending["phase"] != "sent"
        or datetime.fromisoformat(pending["expires_at"]) <= utcnow()
    ):
        raise not_found("这段语音已失效")
    media = GeneratedMediaService(
        session, data_dir=settings.data_dir, root_key=load_signing_root_key(settings),
        actor_id=settings.actor_id,
    )
    try:
        raw = await media.read_for_window(
            pending["asset"], requester_actor=settings.actor_id, requester_window=run.window_id,
        )
    except AssetError as exc:
        raise not_found("这段语音不可用") from exc
    return Response(raw, media_type=pending["mime_type"], headers={"Cache-Control": "no-store"})
