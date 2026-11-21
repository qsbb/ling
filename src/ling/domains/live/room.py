from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.domains.identity.service import IdentityService, normalize_display_name
from ling.domains.live.ingress import CustomEvent, account_namespace, event_is_fresh
from ling.infra.models import (
    ConnectorInstance,
    LiveSessionRow,
    LiveViewerRow,
    OutboxEvent,
    Subject,
    Window,
    utcnow,
)
from ling.infra.security import new_id

ACTIVE = ("preview", "starting", "live", "stopping")
MAX_WAITING = 8
MAX_RECENT_IDS = 512
MAX_VIEWERS = 40
MAX_HISTORY = 8


async def active_session(
    session: AsyncSession, actor_id: str, *, lock: bool = False
) -> LiveSessionRow | None:
    query = select(LiveSessionRow).where(
        LiveSessionRow.actor_id == actor_id, LiveSessionRow.state.in_(ACTIVE),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


def session_view(row: LiveSessionRow) -> dict:
    return {
        "id": row.id,
        "connector_id": row.connector_id,
        "room_id": row.room_id,
        "state": row.state,
        "mode": row.runtime["mode"],
        "deadline_at": row.deadline_at.isoformat(),
        "obs_active": row.runtime.get("obs_active"),
        "obs_reconnecting": row.runtime.get("obs_reconnecting"),
        "platform_live": row.runtime.get("platform_live"),
        "stage_seen_at": row.runtime.get("stage_seen_at"),
        "pending_comments": len(row.runtime.get("queue", [])),
        "viewers": list(row.runtime.get("interacted", {}).values()),
        "counts": row.runtime.get("counts", {}),
        "detail": row.runtime.get("detail"),
    }


async def open_session(
    session: AsyncSession,
    connector: ConnectorInstance,
    *,
    actor_id: str,
    persona_id: str,
    subject_id: str,
    mode: str,
) -> LiveSessionRow:
    config = connector.custom_config or {}
    if not connector.enabled or not config.get("stage_token_hash"):
        raise LingError(ErrorCode.conflict, "请启用接入器并配置舞台端令牌", status_code=409)
    if mode == "broadcast" and not config.get("obs_control"):
        raise LingError(ErrorCode.forbidden, "这个房间未授权控制 OBS 推流", status_code=403)
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"live:{actor_id}"))))
    old = await active_session(session, actor_id, lock=True)
    if old is not None:
        if old.connector_id == connector.instance_id and old.runtime["mode"] == mode:
            return old
        raise LingError(ErrorCode.conflict, "已有直播场次正在运行或停止", status_code=409)
    now = utcnow()
    window = Window(
        id=new_id("win"), actor_id=actor_id, subject_id=subject_id, persona_id=persona_id,
        channel="livestream", visibility="public", title=connector.display_name, archived=True,
    )
    session.add(window)
    run = LiveSessionRow(
        id=new_id("live"), actor_id=actor_id, connector_id=connector.instance_id,
        room_id=config["room_id"], window_id=window.id,
        state="preview" if mode == "preview" else "starting",
        runtime={"mode": mode, "topic": config.get("public_topic", ""), "queue": []},
        deadline_at=now + timedelta(minutes=config.get("max_minutes", 30)),
        created_at=now,
    )
    session.add(run)
    await session.flush()
    return run


def receive_event(run: LiveSessionRow, event: CustomEvent, *, now: datetime) -> str:
    runtime = dict(run.runtime)
    if event.session_id != run.id or event.room_id != run.room_id:
        return "other_session"
    if not event_is_fresh(event, now) or event.occurred_at < run.created_at - timedelta(seconds=5):
        return "expired"
    recent = runtime.get("recent_ids", [])
    if event.event_id in recent:
        return "duplicate"
    if event.kind in {"live_start", "live_end"}:
        last = runtime.get("platform_observed_at")
        if last and event.occurred_at <= datetime.fromisoformat(last):
            return "expired"
        runtime["platform_live"] = event.kind == "live_start"
        runtime["platform_observed_at"] = event.occurred_at.isoformat()
    elif run.state == "stopping":
        return "stopping"
    else:
        counts = dict(runtime.get("counts", {}))
        counts[event.kind] = counts.get(event.kind, 0) + 1
        runtime["counts"] = counts
        if event.kind == "comment" and event.text:
            queue = fresh_comments(runtime, now)
            account = event.sender.id if event.sender else None
            # The latest comment per account occupies a single slot.
            queue = [
                item for item in queue
                if (not account or item["account_ref"] != account) and item["text"] != event.text
            ]
            runtime["queue"] = [
                *queue, {
                    "id": event.event_id,
                    "account_ref": account,
                    "display_name": normalize_display_name(event.sender.name if event.sender else None),
                    "text": event.text,
                    "at": event.occurred_at.isoformat(),
                },
            ][-MAX_WAITING:]
    runtime["recent_ids"] = [*recent, event.event_id][-MAX_RECENT_IDS:]
    run.runtime = runtime
    return "accepted"


def fresh_comments(runtime: dict, now: datetime) -> list[dict]:
    cutoff = now - timedelta(seconds=30)
    return [item for item in runtime.get("queue", []) if datetime.fromisoformat(item["at"]) >= cutoff]


async def save_viewer(
    session: AsyncSession,
    run: LiveSessionRow,
    *,
    account_ref: str,
    note: str,
    merge_into: str | None,
) -> LiveViewerRow:
    candidate = run.runtime.get("interacted", {}).get(account_ref)
    if not candidate:
        raise not_found("本场没有这位观众的已播出互动")
    namespace = account_namespace(run.connector_id)
    # The same account can be selected simultaneously in separate runs.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"{namespace}:{account_ref}"))))
    identity = IdentityService(session)
    name = candidate["display_name"] or "直播观众"
    source = await identity.resolve_or_provision(namespace, account_ref, display_name=name)
    if source is None:
        raise LingError(ErrorCode.conflict, "账号目前不可关联", status_code=409)
    if merge_into and await identity.canonical_subject(source) != merge_into:
        if await session.get(Subject, merge_into) is None:
            raise not_found("目标自然人不存在")
        try:
            await identity.merge_subjects(source, merge_into)
        except ValueError as exc:
            raise LingError(ErrorCode.conflict, str(exc), status_code=409) from exc
    key = (run.connector_id, run.room_id, account_ref)
    row = await session.get(LiveViewerRow, key)
    if row is None:
        row = LiveViewerRow(
            connector_id=run.connector_id, room_id=run.room_id, account_ref=account_ref,
        )
        session.add(row)
    row.display_name = name
    row.note = note
    await session.flush()
    return row


def settle_speech(run: LiveSessionRow, *, speech_id: str, state: str) -> None:
    runtime = dict(run.runtime)
    pending = runtime.get("speech")
    if not pending or pending["id"] != speech_id or pending["phase"] != "sent":
        return
    runtime.pop("speech")
    if (
        state == "completed" and run.state in {"preview", "live"}
        and utcnow() < min(run.deadline_at, datetime.fromisoformat(pending["expires_at"]))
    ):
        comment = pending["comment"]
        history = runtime.get("history", [])
        runtime["history"] = [*history, {
            "account_ref": comment["account_ref"],
            "display_name": comment["display_name"],
            "text": comment["text"],
            "reply": pending["text"],
        }][-MAX_HISTORY:]
        account = comment["account_ref"]
        if account:
            viewers = dict(runtime.get("interacted", {}))
            viewers.pop(account, None)
            viewers[account] = {
                "account_ref": account, "display_name": comment["display_name"],
            }
            runtime["interacted"] = dict(list(viewers.items())[-MAX_VIEWERS:])
    elif state == "failed":
        runtime["detail"] = "舞台未能播出本段语音"
    run.runtime = runtime


def next_speech(session: AsyncSession, run: LiveSessionRow, *, now: datetime) -> dict | None:
    runtime = dict(run.runtime)
    pending = runtime.get("speech")
    if pending and datetime.fromisoformat(pending["expires_at"]) <= now:
        pending = None
        runtime.pop("speech")
    if pending is not None:
        if pending["phase"] != "ready":
            return None
        runtime["speech"] = {**pending, "phase": "sent"}
        run.runtime = runtime
        return {"action": "speak", "speech_id": pending["id"], "text": pending["text"],
                "format": pending["format"], "expires_at": pending["expires_at"]}

    queue = fresh_comments(runtime, now)
    runtime["queue"] = queue
    if queue:
        interacted = runtime.get("interacted", {})
        followup = next((item for item in queue if item["account_ref"] in interacted), None)
        comment = followup if followup and not runtime.get("last_was_followup") else queue[0]
        runtime["last_was_followup"] = comment["account_ref"] in interacted
        speech_id = new_id("speech")
        runtime["queue"] = [item for item in queue if item["id"] != comment["id"]]
        runtime["speech"] = {
            "id": speech_id, "phase": "generating", "comment": comment,
            "expires_at": (now + timedelta(seconds=40)).isoformat(),
        }
        session.add(OutboxEvent(
            id=new_id("evt"), topic="livestream.speech",
            payload={"session_id": run.id, "speech_id": speech_id},
        ))
    run.runtime = runtime
    return None


def finish_session(run: LiveSessionRow, *, detail: str) -> None:
    run.state = "ended"
    run.runtime = {
        key: value for key, value in run.runtime.items()
        if key in {
            "mode", "detail", "interacted", "counts", "obs_active", "obs_reconnecting",
            "platform_live", "stage_seen_at",
        }
    } | {"detail": detail}
