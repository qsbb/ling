from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.identity.service import IdentityService, normalize_display_name
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
from ling.kernel.live.events import CustomEvent, account_namespace, event_is_fresh, plan_from_config

ACTIVE = ("preview", "starting", "live", "stopping")
MAX_WAITING = 8
MAX_RECENT_IDS = 512
MAX_VIEWERS = 40
MAX_HISTORY = 8
LIGHT_COMMENTS = {"你好", "来了", "哈哈", "哈哈哈", "贴贴", "晚安", "6", "666"}


class LiveConflict(ValueError):
    pass


class LiveForbidden(PermissionError):
    pass


class LiveNotFound(LookupError):
    pass


def observed_platform(runtime: dict, now: datetime) -> bool | None:
    observed = runtime.get("platform_observed_at")
    if observed and now - datetime.fromisoformat(observed) <= timedelta(seconds=30):
        return runtime.get("platform_live")
    return None


def stage_is_fresh(runtime: dict, now: datetime) -> bool:
    seen = runtime.get("stage_seen_at")
    return bool(seen and now - datetime.fromisoformat(seen) <= timedelta(seconds=10))


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
    runtime = row.runtime
    now = utcnow()
    stage_seen = runtime.get("stage_seen_at")
    platform_seen = runtime.get("platform_observed_at")
    ended = row.state == "ended"
    stage_fresh = stage_is_fresh(runtime, now)
    return {
        "id": row.id,
        "connector_id": row.connector_id,
        "room_id": row.room_id,
        "state": row.state,
        "mode": runtime["mode"],
        "role": runtime.get("role", "host"),
        "topic": runtime.get("topic", ""),
        "host_name": runtime.get("host_name", ""),
        "autonomous": runtime.get("autonomous", False),
        "confirmed_live_at": runtime.get("confirmed_live_at"),
        "stopping_at": runtime.get("stopping_at"),
        "ended_at": runtime.get("ended_at"),
        "deadline_at": row.deadline_at.isoformat(),
        "obs_active": runtime.get("obs_active") if ended or stage_fresh else None,
        "obs_reconnecting": runtime.get("obs_reconnecting") if not ended and stage_fresh else None,
        "stage_ready": not ended and stage_fresh and runtime.get("stage_ready", False),
        "owns_output": not ended and stage_fresh and runtime.get("owns_output", False),
        "stop_output_confirmed": runtime.get("stop_output_confirmed", False),
        "stop_platform_confirmed": runtime.get("stop_platform_confirmed", False),
        "platform_live": runtime.get("platform_live") if ended else observed_platform(runtime, now),
        "stage_seen_at": stage_seen,
        "platform_seen_at": platform_seen,
        "pending_comments": len(fresh_comments(runtime, now)),
        "viewers": list(runtime.get("interacted", {}).values()),
        "counts": runtime.get("counts", {}),
        "played_count": runtime.get("played_count", 0),
        "replies": [item["reply"] for item in runtime.get("history", [])],
        "detail": runtime.get("detail"),
    }


async def open_session(
    session: AsyncSession,
    connector: ConnectorInstance,
    *,
    actor_id: str,
    persona_id: str,
    subject_id: str,
    mode: str,
    device_id: str | None = None,
    ends_at: datetime | None = None,
) -> LiveSessionRow:
    config = connector.custom_config or {}
    plan = plan_from_config(config)
    if not connector.enabled or not config.get("stage_token_hash"):
        raise LiveConflict("请启用接入器并配置舞台端令牌")
    if mode == "broadcast" and plan.role == "host" and not config.get("obs_control"):
        raise LiveForbidden("这个房间未授权控制 OBS 推流")
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"live:{actor_id}"))))
    old = await active_session(session, actor_id, lock=True)
    now = utcnow()
    if ends_at is not None and ends_at <= now:
        raise LiveConflict("自主开播授权时段已结束")
    if old is not None and now >= old.deadline_at and not (
        old.runtime.get("device_id") or old.runtime.get("start_operation")
    ):
        finish_session(old, detail="本场已到期")
        await session.flush()
        old = None
    if old is not None:
        if old.connector_id != connector.instance_id or old.state == "stopping":
            raise LiveConflict("已有直播场次正在运行或停止")
        if old.runtime["mode"] == mode:
            return old
        if old.state != "preview" or mode != "broadcast" or ends_at is not None:
            raise LiveConflict("已有直播场次正在运行")
        finish_session(old, detail="预览结束，已切换到正式场次")
        await session.flush()
    deadline = now + timedelta(minutes=plan.max_minutes)
    window = Window(
        id=new_id("win"), actor_id=actor_id, subject_id=subject_id, persona_id=persona_id,
        channel="livestream", visibility="public", title=connector.display_name, archived=True,
    )
    session.add(window)
    run = LiveSessionRow(
        id=new_id("live"), actor_id=actor_id, connector_id=connector.instance_id,
        room_id=config["room_id"], window_id=window.id,
        state="preview" if mode == "preview" else "starting",
        runtime={
            "mode": mode, "role": plan.role, "topic": plan.public_topic,
            "host_name": plan.host_name, "companion_auto_reply": plan.companion_auto_reply,
            "queue": [], "device_id": device_id, "next_host_at": now.isoformat(),
            "authorized_scene": (config.get("autonomous_binding") or {}).get("scene") if ends_at else None,
        },
        deadline_at=min(deadline, ends_at) if ends_at else deadline,
        created_at=now,
    )
    session.add(run)
    if mode == "broadcast" and "live_plan" in config:
        connector.custom_config = {
            **config, "plan_pending": False,
            "autonomous_state": "started" if plan.autonomous_window else None,
        }
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
        if event.occurred_at < run.created_at or (last and event.occurred_at <= datetime.fromisoformat(last)):
            return "expired"
        runtime["platform_live"] = event.kind == "live_start"
        runtime["platform_observed_at"] = event.occurred_at.isoformat()
    elif run.state == "stopping":
        return "stopping"
    else:
        counts = dict(runtime.get("counts", {}))
        counts[event.kind] = counts.get(event.kind, 0) + 1
        runtime["counts"] = counts
        if event.kind == "comment" and event.text and event.text.strip(" !！。~～") not in LIGHT_COMMENTS:
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
        raise LiveNotFound("本场没有这位观众的已播出互动")
    namespace = account_namespace(run.connector_id)
    # The same account can be selected simultaneously in separate runs.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"{namespace}:{account_ref}"))))
    identity = IdentityService(session)
    name = candidate["display_name"] or "直播观众"
    source = await identity.resolve_or_provision(namespace, account_ref, display_name=name)
    if source is None:
        raise LiveConflict("账号目前不可关联")
    if merge_into and await identity.canonical_subject(source) != merge_into:
        if await session.get(Subject, merge_into) is None:
            raise LiveNotFound("目标自然人不存在")
        try:
            await identity.merge_subjects(source, merge_into)
        except ValueError as exc:
            raise LiveConflict(str(exc)) from exc
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
        runtime["played_count"] = runtime.get("played_count", 0) + 1
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
    host = runtime.get("role", "host") == "host"
    elapsed = datetime.fromisoformat(runtime.get("next_host_at") or run.created_at.isoformat()) <= now
    host_due = host and runtime["mode"] == "broadcast" and runtime["topic"] and elapsed
    can_select_comment = bool(queue) and (host or (runtime.get("companion_auto_reply") and elapsed))
    cue = runtime.pop("cue", None)
    if cue and datetime.fromisoformat(cue["at"]) < now - timedelta(seconds=30):
        cue = None
    if cue or can_select_comment or host_due:
        interacted = runtime.get("interacted", {})
        followup = next((item for item in queue if item["account_ref"] in interacted), None)
        if cue:
            comment = cue
        elif can_select_comment:
            comment = followup if followup and not runtime.get("last_was_followup") else queue[0]
            runtime["queue"] = [item for item in queue if item["id"] != comment["id"]]
        else:
            comment = {"display_name": None, "account_ref": None, "text": ""}
        runtime["next_host_at"] = (now + timedelta(seconds=45 if host else 30)).isoformat()
        runtime["last_was_followup"] = comment["account_ref"] in interacted
        speech_id = new_id("speech")
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
            "mode", "role", "topic", "host_name", "autonomous", "interacted", "counts",
            "obs_active", "platform_live", "stage_seen_at", "platform_observed_at",
            "confirmed_live_at", "played_count", "history", "stop_output_confirmed", "stop_platform_confirmed",
        }
    } | {"detail": detail, "ended_at": utcnow().isoformat()}


def request_stop(run: LiveSessionRow) -> None:
    if run.state != "stopping":
        run.state = "stopping"
        run.runtime = {**run.runtime, "stopping_at": utcnow().isoformat()}


def cue_speech(run: LiveSessionRow, text: str) -> None:
    now = utcnow()
    if run.state not in {"preview", "live"} or now >= run.deadline_at:
        raise LiveConflict("本场当前无法安排发言")
    runtime = dict(run.runtime)
    previous_cue = runtime.get("cue")
    speech = runtime.get("speech")
    if (previous_cue and now - datetime.fromisoformat(previous_cue["at"]) <= timedelta(seconds=30)) or (
        speech and datetime.fromisoformat(speech["expires_at"]) > now
    ):
        raise LiveConflict("上一段发言尚未结束")
    runtime.pop("speech", None)
    run.runtime = {
        **runtime,
        "cue": {
            "display_name": None, "account_ref": None, "text": text,
            "at": now.isoformat(), "operator_cue": True,
        },
    }
