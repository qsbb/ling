"""Owner-facing commands and the outbound Windows companion protocol."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ling.api.deps import DbSession, SettingsDep
from ling.api.errors import LingError, not_found, unauthorized
from ling.api.guards import OwnerAuth, OwnerWrite, OwnerWriteAction
from ling.contracts.desktop import (
    CreateDesktopTask,
    DesktopAssignment,
    DesktopControl,
    DesktopDevice,
    DesktopHeartbeat,
    DesktopPairing,
    DesktopPoll,
    DesktopReport,
    DesktopReportResult,
    DesktopStatus,
    DesktopTask,
    PairDesktop,
    TaskReason,
    TaskState,
    normalize_note,
)
from ling.contracts.v1 import ErrorCode
from ling.infra.models import DesktopDeviceRow, DesktopTaskRow, utcnow
from ling.infra.security import hash_token, new_token

router = APIRouter(prefix="/desktop", tags=["desktop"])
HEARTBEAT_TTL = timedelta(seconds=10)
QUEUE_TTL = timedelta(seconds=15)
LEASE_SECONDS = 20
ACTIVE = ("queued", "running")
NEXT_STEP = {None: "opened", "opened": "written", "written": "verified"}
INTERRUPTIONS = {"desktop_unavailable", "window_changed", "connection_lost", "lease_expired"}


async def _lock_device(session: DbSession, actor_id: str) -> DesktopDeviceRow | None:
    # Every mutation takes this lock first, so pause and a companion report
    # have one order even when they arrive at different API processes.
    return await session.get(DesktopDeviceRow, actor_id, with_for_update=True)


def _finish(
    task: DesktopTaskRow, state: TaskState, reason: TaskReason | None, now: datetime,
) -> None:
    task.state = state
    task.reason = reason
    task.finished_at = now
    task.lease_hash = None


async def _active_task(
    session: DbSession, device: DesktopDeviceRow, now: datetime,
) -> DesktopTaskRow | None:
    task = await session.scalar(select(DesktopTaskRow).where(
        DesktopTaskRow.actor_id == device.actor_id, DesktopTaskRow.state.in_(ACTIVE),
    ))
    if task is None or task.expires_at > now:
        return task
    if task.state == "queued":
        _finish(task, "cancelled", "queue_expired", now)
    else:
        _finish(task, "interrupted", "lease_expired", now)
        device.paused = True
    return None


def _ready(device: DesktopDeviceRow, now: datetime) -> bool:
    return bool(device.ready and device.last_seen_at and now - device.last_seen_at < HEARTBEAT_TTL)


def _view(task: DesktopTaskRow) -> DesktopTask:
    return DesktopTask.model_validate(task, from_attributes=True)


async def _status(
    session: DbSession, actor_id: str, device: DesktopDeviceRow | None, now: datetime,
) -> DesktopStatus:
    paired = None
    if device is not None and device.name and device.token_hash:
        paired = DesktopDevice(name=device.name, state="offline", last_seen_at=device.last_seen_at)
        if device.last_seen_at and now - device.last_seen_at < HEARTBEAT_TTL:
            paired.state = "ready" if device.ready else "unavailable"
    rows = await session.scalars(select(DesktopTaskRow).where(
        DesktopTaskRow.actor_id == actor_id,
    ).order_by(DesktopTaskRow.created_at.desc(), DesktopTaskRow.id).limit(20))
    result = DesktopStatus(
        device=paired, paused=(device.paused if paired and device else True),
        tasks=[_view(row) for row in rows],
    )
    await session.commit()
    return result


def _conflict(message: str, detail: str) -> LingError:
    return LingError(ErrorCode.conflict, message, status_code=409, detail=detail)


def _require_pairing(device: DesktopDeviceRow | None) -> DesktopDeviceRow:
    if device is None or not device.token_hash:
        raise _conflict("请先配对 Windows 电脑", "not_paired")
    return device


async def _companion(request: Request, session: DbSession, actor_id: str) -> DesktopDeviceRow:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or len(token) != 43:
        raise unauthorized("Windows 伴生端配对凭据无效")
    device = await _lock_device(session, actor_id)
    if device is None or not device.token_hash or not hmac.compare_digest(
        device.token_hash, hash_token(token),
    ):
        raise unauthorized("Windows 伴生端配对凭据无效")
    return device


@router.get("")
async def desktop_status(_auth: OwnerAuth, session: DbSession, settings: SettingsDep) -> DesktopStatus:
    device = await _lock_device(session, settings.actor_id)
    now = utcnow()
    if device is not None:
        await _active_task(session, device, now)
    return await _status(session, settings.actor_id, device, now)


@router.put("/pair")
async def pair_desktop(
    body: PairDesktop, response: Response, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> DesktopPairing:
    # Resolve even concurrent first-time pairing on the same row.
    await session.execute(insert(DesktopDeviceRow).values(
        actor_id=settings.actor_id,
    ).on_conflict_do_nothing(index_elements=["actor_id"]))
    device = await _lock_device(session, settings.actor_id)
    assert device is not None
    now = utcnow()
    task = await _active_task(session, device, now)
    if task is not None:
        _finish(task, "interrupted", "pairing_changed", now)
    token = new_token()
    device.name, device.token_hash = body.name, hash_token(token)
    device.paused, device.ready, device.last_seen_at = True, False, None
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return DesktopPairing(token=token)


@router.delete("/pair", status_code=204)
async def unpair_desktop(
    _auth: OwnerWriteAction, session: DbSession, settings: SettingsDep,
) -> Response:
    device = await _lock_device(session, settings.actor_id)
    if device is not None:
        now = utcnow()
        task = await _active_task(session, device, now)
        if task is not None:
            _finish(task, "interrupted", "pairing_changed", now)
        device.name, device.token_hash = None, None
        device.paused, device.ready, device.last_seen_at = True, False, None
        await session.commit()
    return Response(status_code=204)


@router.put("/control")
async def desktop_control(
    body: DesktopControl, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> DesktopStatus:
    device = _require_pairing(await _lock_device(session, settings.actor_id))
    now = utcnow()
    task = await _active_task(session, device, now)
    if not body.paused and not _ready(device, now):
        raise _conflict("请先连接伴生端并解锁 Windows 桌面", "desktop_not_ready")
    device.paused = body.paused
    if body.paused and task is not None:
        state: TaskState = "interrupted" if task.state == "running" else "cancelled"
        _finish(task, state, "owner_paused", now)
    return await _status(session, settings.actor_id, device, now)


@router.post("/tasks", status_code=201)
async def create_desktop_task(
    body: CreateDesktopTask, _auth: OwnerWrite, session: DbSession, settings: SettingsDep,
) -> DesktopTask:
    device = _require_pairing(await _lock_device(session, settings.actor_id))
    now = utcnow()
    active = await _active_task(session, device, now)
    task_id = str(body.request_id)
    task = await session.get(DesktopTaskRow, (settings.actor_id, task_id))
    if task is None:
        if device.paused:
            raise _conflict("桌面任务已暂停", "paused")
        if not _ready(device, now):
            raise _conflict("请先连接伴生端并解锁 Windows 桌面", "desktop_not_ready")
        if active:
            raise _conflict("已有一条桌面任务正在等待或执行", "task_in_progress")
        task = DesktopTaskRow(
            actor_id=settings.actor_id, id=task_id, text=body.text,
            state="queued", created_at=now, expires_at=now + QUEUE_TTL,
        )
        session.add(task)
    elif task.text != body.text:
        raise _conflict("这个提交编号已用于另一段文本", "request_id_conflict")
    await session.commit()
    return _view(task)


@router.post("/tasks/{task_id}/cancel")
async def cancel_desktop_task(
    task_id: UUID4, _auth: OwnerWriteAction, session: DbSession, settings: SettingsDep,
) -> DesktopTask:
    device = await _lock_device(session, settings.actor_id)
    task = await session.get(DesktopTaskRow, (settings.actor_id, str(task_id)))
    if device is None or task is None:
        raise not_found("桌面任务不存在")
    now = utcnow()
    await _active_task(session, device, now)
    if task.state in ACTIVE:
        _finish(task, "cancelled", "owner_cancelled", now)
    await session.commit()
    return _view(task)


@router.post("/companion/poll")
async def poll_desktop(
    body: DesktopHeartbeat, request: Request, response: Response,
    session: DbSession, settings: SettingsDep,
) -> DesktopPoll:
    device = await _companion(request, session, settings.actor_id)
    now = utcnow()
    task = await _active_task(session, device, now)
    device.ready, device.last_seen_at = body.ready, now
    assignment = None
    if task is not None and not body.ready:
        _finish(task, "interrupted", "desktop_unavailable", now)
        device.paused = True
    elif task is not None and task.state == "queued" and not device.paused:
        token = new_token()
        task.state = "running"
        task.started_at = now
        task.expires_at = now + timedelta(seconds=LEASE_SECONDS)
        task.lease_hash = hash_token(token)
        assignment = DesktopAssignment(
            id=UUID(task.id), text=task.text, lease_token=token, lease_seconds=LEASE_SECONDS,
        )
    # Commit the claim before a companion is permitted to open a window.
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return DesktopPoll(task=assignment)


@router.post("/companion/tasks/{task_id}/report")
async def report_desktop(
    task_id: UUID4, body: DesktopReport, request: Request, session: DbSession, settings: SettingsDep,
) -> DesktopReportResult:
    device = await _companion(request, session, settings.actor_id)
    now = utcnow()
    task = await _active_task(session, device, now)
    if (
        device.paused or task is None or task.id != str(task_id)
        or task.state != "running" or task.lease_hash is None
        or not hmac.compare_digest(task.lease_hash, hash_token(body.lease_token))
        or (body.step != "aborted" and body.step != NEXT_STEP.get(task.step))
    ):
        await session.commit()
        return DesktopReportResult(accepted=False)

    device.last_seen_at = now
    if body.step == "aborted":
        if body.reason in INTERRUPTIONS:
            device.paused = True
            if body.reason == "desktop_unavailable":
                device.ready = False
            _finish(task, "interrupted", body.reason, now)
        else:
            _finish(task, "failed", body.reason, now)
    elif body.step == "verified":
        if body.readback is not None and normalize_note(body.readback) == task.text:
            task.step = "verified"
            _finish(task, "succeeded", None, now)
        else:
            _finish(task, "failed", "readback_mismatch", now)
    else:
        task.step = body.step
    # The companion uses this acknowledgement to authorize the next step.
    await session.commit()
    return DesktopReportResult(accepted=True)
