from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.activity.service import ActivityService
from ling.infra.models import ConnectorInstance, LiveSessionRow, Operation, utcnow
from ling.infra.security import new_id
from ling.kernel.live.room import (
    finish_session,
    next_speech,
    observed_platform,
    request_stop,
    settle_speech,
)


class PlaybackReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speech_id: str = Field(max_length=64)
    state: Literal["completed", "interrupted", "failed"]


class StageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    session_id: str | None = Field(default=None, max_length=64)
    obs_active: bool
    obs_reconnecting: bool = False
    ready: bool = True
    broadcast_ready: bool = False
    scene: str | None = Field(default=None, max_length=256)
    attachment_id: str | None = Field(default=None, max_length=64)
    control: Literal["started", "stopped", "blocked", "uncertain", "detached"] | None = None
    playback: PlaybackReport | None = None


def record_stage_observation(connector: ConnectorInstance, report: StageStatus) -> None:
    config = connector.custom_config or {}
    previous = config.get("stage_observation") or {}
    now = utcnow()
    status = {
        "device_id": report.device_id, "scene": report.scene, "obs_active": report.obs_active,
        "obs_reconnecting": report.obs_reconnecting,
    }
    seen = previous.get("seen_at")
    if seen and now - datetime.fromisoformat(seen) < timedelta(seconds=5) and all(
        previous.get(key) == value for key, value in status.items()
    ):
        return
    connector.custom_config = {**config, "stage_observation": {**status, "seen_at": now.isoformat()}}


async def _operation(
    session: AsyncSession, run: LiveSessionRow, verb: str, state: str,
) -> None:
    key = f"{verb}_operation"
    operation_id = run.runtime.get(key)
    if operation_id:
        row = await session.get(Operation, operation_id)
        if row is not None:
            row.state = state
        return
    operation_id = new_id("op")
    session.add(Operation(
        id=operation_id, actor_id=run.actor_id, kind=f"livestream.{verb}", state=state,
        payload={"session_id": run.id, "connector_id": run.connector_id, "room_id": run.room_id},
    ))
    run.runtime = {**run.runtime, key: operation_id}


async def update_stage(session: AsyncSession, run: LiveSessionRow, report: StageStatus) -> dict:
    now = utcnow()
    runtime = run.runtime
    role = runtime.get("role", "host")
    base = {
        "session_id": run.id, "deadline_at": run.deadline_at.isoformat(),
        "role": role, "mode": runtime["mode"],
    }
    if runtime.get("device_id") not in {None, report.device_id}:
        return {**base, "action": "hold"}
    # A device first learns the session ID, then reports evidence for that exact run.
    if report.session_id != run.id:
        return {**base, "action": "hold"}
    if run.state == "starting" and runtime.get("authorized_scene") not in {None, report.scene}:
        request_stop(run)
        run.runtime = {**run.runtime, "detail": "OBS 场景与本场开播授权不一致"}
        runtime = run.runtime
    run.runtime = {
        **runtime, "device_id": report.device_id, "stage_seen_at": now.isoformat(),
        "obs_active": report.obs_active, "obs_reconnecting": report.obs_reconnecting,
        "stage_ready": report.ready,
    }
    broadcast = runtime["mode"] == "broadcast"
    platform_live = observed_platform(runtime, now)
    if now >= run.deadline_at:
        request_stop(run)
    if run.state == "live" and (
        platform_live is False
        or (not report.obs_active and not report.obs_reconnecting)
        or report.control == "uncertain"
    ):
        request_stop(run)
    if role == "companion":
        if run.state == "live" and runtime.get("attachment_id") != report.attachment_id:
            request_stop(run)
        if run.state == "stopping":
            if report.control == "detached":
                finish_session(run, detail="陪伴已结束")
                return {"session_id": None, "action": "hold"}
            return {**base, "action": "detach"}
        if (
            run.state == "starting" and report.ready and report.attachment_id
            and report.obs_active and not report.obs_reconnecting and platform_live is True
        ):
            run.state = "live"
            run.runtime = {
                **run.runtime, "confirmed_live_at": now.isoformat(), "attachment_id": report.attachment_id,
            }
        if not report.ready or not (
            (run.state == "preview" and not report.obs_active and platform_live is not True)
            or (run.state == "live" and report.obs_active and not report.obs_reconnecting and platform_live is True)
        ):
            return {**base, "action": "hold"}
        if report.playback:
            settle_speech(run, speech_id=report.playback.speech_id, state=report.playback.state)
        return {**base, **(next_speech(session, run, now=now) or {"action": "ready"})}
    if report.control == "started" and runtime.get("start_operation") and report.obs_active:
        run.runtime = {**run.runtime, "owns_output": True}
    if not report.obs_active and not report.obs_reconnecting or report.control == "uncertain":
        run.runtime = {**run.runtime, "owns_output": False}
    if report.control in {"blocked", "uncertain"} and run.state == "starting":
        await _operation(session, run, "start", "uncertain" if report.control == "uncertain" else "failed")
        request_stop(run)
        reason = "开播状态待核实" if report.control == "uncertain" else "OBS 已有其他推流或配置未就绪"
        run.runtime = {**run.runtime, "detail": reason}
    if report.playback:
        settle_speech(run, speech_id=report.playback.speech_id, state=report.playback.state)

    owns_output = bool(run.runtime.get("owns_output"))
    if run.state == "stopping":
        obs_stopped = not report.obs_active and not report.obs_reconnecting
        if report.control == "stopped" and obs_stopped:
            run.runtime = {**run.runtime, "owns_output": False}
            owns_output = False
        # Keep the output and platform receipts separate even during a failed start.
        stop_at = datetime.fromisoformat(run.runtime["stopping_at"])
        platform_at = runtime.get("platform_observed_at")
        platform_stopped = platform_live is False and platform_at and datetime.fromisoformat(platform_at) >= stop_at
        awaiting_platform = broadcast and not platform_stopped and (
            runtime.get("platform_live") is True or runtime.get("activity_id")
            or runtime.get("start_operation") and report.control != "blocked"
        )
        run.runtime = {
            **run.runtime, "stop_output_confirmed": obs_stopped,
            "stop_platform_confirmed": bool(platform_stopped),
        }
        if report.obs_reconnecting or (owns_output and report.obs_active) or awaiting_platform:
            await _operation(session, run, "stop", "running")
            return {**base, "action": "stop"} if owns_output and report.obs_active else {**base, "action": "hold"}
        if report.obs_active and broadcast and runtime.get("start_operation"):
            run.runtime = {**run.runtime, "detail": "OBS 仍在推流，请在舞台端核实归属"}
        else:
            if activity := runtime.get("activity_id"):
                await ActivityService(session, actor_id=run.actor_id).transition(
                    activity, "completed", completion_evidence="舞台与平台状态已结束",
                    evidence_kind="observed",
                )
            if runtime.get("stop_operation"):
                await _operation(session, run, "stop", "succeeded")
            if runtime.get("start_operation") and not runtime.get("activity_id"):
                operation = await session.get(Operation, runtime["start_operation"])
                if operation is not None and operation.state == "running":
                    operation.state = "cancelled"
            detail = run.runtime.get("detail")
            if not detail and broadcast and run.runtime.get("platform_live") is None:
                detail = "OBS 已停止，平台房间状态待核实"
            finish_session(run, detail=detail or "本场已结束")
        return {**base, "action": "hold"}

    if run.state == "starting":
        if owns_output and report.obs_active and not report.obs_reconnecting and platform_live is True:
            activity = ActivityService(session, actor_id=run.actor_id)
            view = await activity.propose(
                kind="livestream", origin="tool_receipt", visibility="public",
                source_event_ids=[run.id],
            )
            await activity.transition(view.activity_id, "running")
            await _operation(session, run, "start", "succeeded")
            run.runtime = {**run.runtime, "activity_id": view.activity_id, "confirmed_live_at": now.isoformat()}
            run.state = "live"
        elif not runtime.get("start_operation") and not report.obs_active and not report.obs_reconnecting and report.ready:
            await _operation(session, run, "start", "running")
            run.runtime = {**run.runtime, "start_requested_at": now.isoformat()}
            return {**base, "action": "start"}
        elif not runtime.get("start_operation") and report.obs_active:
            run.runtime = {**run.runtime, "detail": "OBS 已有其他推流"}
        elif not owns_output and runtime.get("start_requested_at"):
            if now - datetime.fromisoformat(runtime["start_requested_at"]) > timedelta(seconds=25):
                await _operation(session, run, "start", "uncertain")
                request_stop(run)
                run.runtime = {**run.runtime, "detail": "开播回执超时，请核实 OBS"}

    if report.ready and (
        (run.state == "preview" and not report.obs_active and platform_live is not True)
        or (run.state == "live" and owns_output and report.obs_active and not report.obs_reconnecting
            and platform_live is True)
    ):
        speech = next_speech(session, run, now=now)
        return {**base, **(speech or {"action": "ready"})}
    return {**base, "action": "hold"}
