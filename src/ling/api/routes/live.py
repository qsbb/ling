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
from ling.api.routes.live_connector import (
    CustomConfig,
    configure_custom,
    custom_view,
    require_custom,
    require_ingress,
    require_stage,
)
from ling.contracts.v1 import ErrorCode
from ling.domains.media.assets import AssetError, GeneratedMediaService
from ling.infra.models import ConnectorInstance, LiveSessionRow, LiveViewerRow, utcnow
from ling.infra.security import load_signing_root_key
from ling.kernel.live.autonomy import prepare_autonomy
from ling.kernel.live.control import StageStatus, record_stage_observation, update_stage
from ling.kernel.live.events import CUSTOM_SCOPE, CustomEvent, LivePlan, plan_from_config
from ling.kernel.live.room import (
    ACTIVE,
    LiveConflict,
    LiveForbidden,
    LiveNotFound,
    active_session,
    cue_speech,
    finish_session,
    observed_platform,
    open_session,
    receive_event,
    request_stop,
    save_viewer,
    session_view,
    stage_is_fresh,
)

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
    active = run is not None
    if run is None:
        run = await session.scalar(select(LiveSessionRow).where(
            LiveSessionRow.actor_id == settings.actor_id,
        ).order_by(LiveSessionRow.created_at.desc()).limit(1))
    planned = await session.scalar(select(ConnectorInstance.instance_id).where(
        ConnectorInstance.scope == CUSTOM_SCOPE, ConnectorInstance.enabled.is_(True),
        ConnectorInstance.deleted_at.is_(None),
        ConnectorInstance.custom_config["plan_pending"].as_boolean().is_(True),
    ).order_by(ConnectorInstance.created_at).limit(1))
    recent = run and utcnow() - datetime.fromisoformat(
        run.runtime.get("ended_at") or run.created_at.isoformat(),
    ) < timedelta(hours=24)
    return {
        "session": session_view(run) if run else None,
        "planned_connector_id": planned,
        "navigation_visible": bool(planned or active or recent),
    }


@router.put("/live/plans/{instance_id}")
async def save_plan(
    instance_id: str, body: LivePlan, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id, lock=True)
    config = connector.custom_config
    if not connector.enabled:
        raise LingError(ErrorCode.conflict, "请先启用接入器", status_code=409)
    if body.autonomous_window and not config.get("obs_control"):
        raise LingError(ErrorCode.forbidden, "请先授权舞台控制 OBS 推流", status_code=403)
    run = await active_session(session, settings.actor_id, lock=True)
    if run and run.connector_id == instance_id:
        raise LingError(ErrorCode.conflict, "请结束当前场次再修改计划", status_code=409)
    plan = body.model_dump(mode="json")
    previous = plan_from_config(config)
    same_window = body.autonomous_window is not None and body.autonomous_window == previous.autonomous_window
    if same_window and body == previous and config.get("live_plan"):
        return custom_view(connector)
    if same_window and config.get("autonomous_state") == "started":
        raise LingError(ErrorCode.conflict, "该授权已使用，请选择新的开播时段", status_code=409)
    connector.custom_config = {
        **config, "live_plan": plan, "plan_pending": True,
        "autonomous_state": None, "autonomous_offer": None,
    }
    return custom_view(connector)


@router.delete("/live/plans/{instance_id}")
async def clear_plan(
    instance_id: str, _auth: OwnerWriteAction, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id, lock=True)
    connector.custom_config = {
        **connector.custom_config, "live_plan": None, "plan_pending": False,
        "autonomous_state": None, "autonomous_offer": None,
    }
    run = await active_session(session, settings.actor_id, lock=True)
    if run and run.connector_id == instance_id and run.runtime.get("autonomous"):
        request_stop(run)
    return custom_view(connector)


class OpenLive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=16)
    mode: Literal["preview", "broadcast"]


@router.post("/live/sessions", status_code=201)
async def start_live(
    body: OpenLive, auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> dict:
    row = await require_custom(session, body.connector_id, lock=True)
    try:
        run = await open_session(
            session, row, actor_id=settings.actor_id, persona_id=settings.persona_id,
            subject_id=auth.user.subject_id, mode=body.mode,
        )
    except LiveConflict as exc:
        raise LingError(ErrorCode.conflict, str(exc), status_code=409) from exc
    except LiveForbidden as exc:
        raise LingError(ErrorCode.forbidden, str(exc), status_code=403) from exc
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
        if not run.runtime.get("device_id") and not run.runtime.get("start_operation"):
            finish_session(run, detail="本场已结束")
        else:
            request_stop(run)
    return session_view(run)


class StageCue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=400)


@router.post("/live/sessions/{session_id}/cue", status_code=202)
async def cue_live(
    session_id: str, body: StageCue, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> dict:
    run = await _get_session(session, settings, session_id, lock=True)
    try:
        cue_speech(run, body.text)
    except LiveConflict as exc:
        raise LingError(ErrorCode.conflict, str(exc), status_code=409) from exc
    return {"accepted": True}


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
    try:
        row = await save_viewer(
            session, run, account_ref=body.account_ref, note=body.note, merge_into=body.merge_into,
        )
    except LiveNotFound as exc:
        raise not_found(str(exc)) from exc
    except LiveConflict as exc:
        raise LingError(ErrorCode.conflict, str(exc), status_code=409) from exc
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
    settling = run is not None and run.connector_id == instance_id and run.state == "stopping"
    if not connector.enabled and not settling:
        raise LingError(ErrorCode.conflict, "接入器已停用", status_code=409)
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
    run = await active_session(session, settings.actor_id, lock=True)
    matches = run is not None and run.connector_id == instance_id
    if not connector.enabled and not (
        matches and run.state == "stopping" and event.kind in {"live_start", "live_end"}
    ):
        raise LingError(ErrorCode.conflict, "接入器已停用", status_code=409)
    last = config.get("last_received_at")
    if not last or now - datetime.fromisoformat(last) >= timedelta(seconds=5):
        connector.custom_config = {**config, "last_received_at": now.isoformat()}
    if not matches:
        return {"status": "no_session"}
    return {"status": receive_event(run, event, now=now)}


@router.post("/custom/{instance_id}/stage")
async def stage_tick(
    instance_id: str, body: StageStatus, request: Request, session: DbSession, settings: SettingsDep,
) -> dict:
    connector = await require_custom(session, instance_id, lock=True)
    require_stage(request, connector.custom_config or {})
    run = await active_session(session, settings.actor_id, lock=True)
    if connector.enabled and (run is None or run.runtime.get("device_id") in {None, body.device_id}):
        record_stage_observation(connector, body)
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
    now = utcnow()
    if (
        not connector.enabled or run.connector_id != instance_id or run.state not in {"preview", "live"}
        or not stage_is_fresh(run.runtime, now) or not run.runtime.get("stage_ready")
        or run.runtime.get("obs_reconnecting")
        or run.runtime.get("obs_active") != (run.state == "live")
        or (run.state == "live" and observed_platform(run.runtime, now) is not True)
        or (run.state == "preview" and observed_platform(run.runtime, now) is True)
        or now >= run.deadline_at
        or not pending or pending["id"] != speech_id or pending["phase"] != "sent"
        or datetime.fromisoformat(pending["expires_at"]) <= now
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
