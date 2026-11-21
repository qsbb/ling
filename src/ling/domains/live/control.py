from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.activity.service import ActivityService
from ling.domains.live.room import finish_session, next_speech, settle_speech
from ling.infra.models import LiveSessionRow, Operation, utcnow
from ling.infra.security import new_id


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
    control: Literal["started", "stopped", "blocked", "uncertain"] | None = None
    playback: PlaybackReport | None = None


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
    base = {"session_id": run.id, "deadline_at": run.deadline_at.isoformat()}
    if runtime.get("device_id") not in {None, report.device_id}:
        return {**base, "action": "hold"}
    # A device first learns the session ID, then reports evidence for that exact run.
    if report.session_id != run.id:
        return {**base, "action": "hold"}
    run.runtime = {
        **runtime, "device_id": report.device_id, "stage_seen_at": now.isoformat(),
        "obs_active": report.obs_active, "obs_reconnecting": report.obs_reconnecting,
    }
    broadcast = runtime["mode"] == "broadcast"
    if now >= run.deadline_at:
        run.state = "stopping"
    if run.state == "live" and runtime.get("platform_live") is False:
        run.state = "stopping"
    if report.control == "started" and runtime.get("start_operation") and report.obs_active:
        run.runtime = {**run.runtime, "owns_output": True}
    if report.control in {"blocked", "uncertain"} and run.state == "starting":
        await _operation(session, run, "start", "uncertain" if report.control == "uncertain" else "failed")
        run.state = "stopping"
        reason = "开播状态待核实" if report.control == "uncertain" else "OBS 已有其他推流或配置未就绪"
        run.runtime = {**run.runtime, "detail": reason}
    if report.playback:
        settle_speech(run, speech_id=report.playback.speech_id, state=report.playback.state)

    owns_output = bool(run.runtime.get("owns_output"))
    if run.state == "stopping":
        if report.control == "stopped" and not report.obs_active:
            run.runtime = {**run.runtime, "owns_output": False}
            owns_output = False
        # Keep the output and platform receipts separate even during a failed start.
        awaiting_platform = broadcast and run.runtime.get("platform_live") is True
        if (owns_output and report.obs_active) or awaiting_platform:
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
            finish_session(run, detail=run.runtime.get("detail") or "本场已结束")
        return {**base, "action": "hold"}

    if run.state == "starting":
        if owns_output and report.obs_active and not report.obs_reconnecting and runtime.get("platform_live") is True:
            activity = ActivityService(session, actor_id=run.actor_id)
            view = await activity.propose(
                kind="livestream", origin="tool_receipt", visibility="public",
                source_event_ids=[run.id],
            )
            await activity.transition(view.activity_id, "running")
            await _operation(session, run, "start", "succeeded")
            run.runtime = {**run.runtime, "activity_id": view.activity_id}
            run.state = "live"
        elif not runtime.get("start_operation") and not report.obs_active and report.ready:
            await _operation(session, run, "start", "running")
            run.runtime = {**run.runtime, "start_requested_at": now.isoformat()}
            return {**base, "action": "start"}
        elif not runtime.get("start_operation") and report.obs_active:
            run.runtime = {**run.runtime, "detail": "OBS 已有其他推流"}
        elif not owns_output and runtime.get("start_requested_at"):
            if now - datetime.fromisoformat(runtime["start_requested_at"]) > timedelta(seconds=25):
                await _operation(session, run, "start", "uncertain")
                run.state = "stopping"
                run.runtime = {**run.runtime, "detail": "开播回执超时，请核实 OBS"}

    if report.ready and (
        (run.state == "preview" and not report.obs_active)
        or (run.state == "live" and owns_output and report.obs_active and not report.obs_reconnecting)
    ):
        speech = next_speech(session, run, now=now)
        return {**base, **(speech or {"action": "ready"})}
    return {**base, "action": "hold"}
