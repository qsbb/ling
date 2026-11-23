"""Schedule one authorized Notepad attempt and record verified life activity."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.desktop import DesktopActivityGrant, TaskReason, TaskState
from ling.infra.models import DesktopDeviceRow, DesktopTaskRow

HEARTBEAT_TTL = timedelta(seconds=10)
QUEUE_TTL = timedelta(seconds=15)
ACTIVE = ("queued", "running")


async def lock_device(session: AsyncSession, actor_id: str) -> DesktopDeviceRow | None:
    """Serialize task, grant, and companion changes on the paired device."""
    return await session.get(DesktopDeviceRow, actor_id, with_for_update=True)


def finish(task: DesktopTaskRow, state: TaskState, reason: TaskReason | None, now: datetime) -> None:
    task.state = state
    task.reason = reason
    task.finished_at = now
    task.lease_hash = None


async def active_task(
    session: AsyncSession, device: DesktopDeviceRow, now: datetime,
) -> DesktopTaskRow | None:
    task = await session.scalar(select(DesktopTaskRow).where(
        DesktopTaskRow.actor_id == device.actor_id, DesktopTaskRow.state.in_(ACTIVE),
    ))
    if task is None:
        return None
    expired_grant = task.authorized_until is not None and task.authorized_until <= now
    if task.expires_at > now and not expired_grant:
        return task
    if task.state == "queued":
        finish(task, "cancelled", "authorization_expired" if expired_grant else "queue_expired", now)
    else:
        finish(task, "interrupted", "authorization_expired" if expired_grant else "lease_expired", now)
        if not expired_grant:
            device.paused = True
    return None


def ready(device: DesktopDeviceRow, now: datetime) -> bool:
    return bool(device.ready and device.last_seen_at and now - device.last_seen_at < HEARTBEAT_TTL)


def store_grant(device: DesktopDeviceRow, grant: DesktopActivityGrant) -> None:
    device.activity_grant = grant.model_dump(mode="json")


def current_grant(device: DesktopDeviceRow, now: datetime) -> DesktopActivityGrant | None:
    if device.activity_grant is None:
        return None
    grant = DesktopActivityGrant.model_validate(device.activity_grant)
    if grant.decision == "pending" and grant.end_at <= now:
        grant.decision = "expired"
        store_grant(device, grant)
    return grant


async def revoke_grant(
    session: AsyncSession, device: DesktopDeviceRow, now: datetime,
) -> DesktopActivityGrant | None:
    grant = current_grant(device, now)
    if grant is None:
        return None
    task = await active_task(session, device, now)
    if task is not None and task.source == "life" and task.id == str(grant.task_id):
        state: TaskState = "interrupted" if task.state == "running" else "cancelled"
        finish(task, state, "authorization_revoked", now)
    if grant.decision in {"pending", "queued"}:
        grant.decision = "revoked"
        store_grant(device, grant)
    return grant


async def advance_grant(
    session: AsyncSession, *, actor_id: str, now: datetime,
    rest: bool = False, writing_slot: tuple[str, datetime] | None = None,
) -> None:
    device = await lock_device(session, actor_id)
    if device is None or device.activity_grant is None or not device.token_hash:
        return
    grant = current_grant(device, now)
    if grant is None or grant.decision != "pending" or now < grant.start_at or device.paused:
        return
    if rest:
        grant.decision = "rest"
        store_grant(device, grant)
        return
    if writing_slot is None:
        return
    if await active_task(session, device, now) is not None or device.paused or not ready(device, now):
        return

    task_id = str(grant.request_id)
    if await session.get(DesktopTaskRow, (actor_id, task_id)) is not None:
        grant.decision = "revoked"
        store_grant(device, grant)
        return
    segment_id, segment_end = writing_slot
    until = min(grant.end_at, segment_end)
    session.add(DesktopTaskRow(
        id=task_id, actor_id=actor_id, text=grant.text, state="queued", created_at=now,
        expires_at=min(until, now + QUEUE_TTL), source="life", source_segment_id=segment_id,
        authorized_until=until,
    ))
    grant.decision = "queued"
    grant.task_id = grant.request_id
    store_grant(device, grant)
