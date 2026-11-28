"""Select a short private recap and resolve its evidence at read time."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import (
    LifeReviewEntry,
    Origin,
    SleepReviewItem,
    SleepReviewNight,
    SleepReviewSource,
    SleepReviewsOut,
    TurnState,
    Visibility,
)
from ling.domains.evidence import EvidenceKind, parse_completion_evidence
from ling.domains.memory.consolidate import recent_consolidations
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.service import MemoryAudience, MemoryHit, MemoryService
from ling.infra.models import (
    ActivityRow,
    DreamReportRow,
    IntentRow,
    MemoryConsolidationRow,
    MemoryRow,
    Message,
    RelationshipEvent,
    ScheduleSegmentRow,
    SleepSettlementRow,
    Turn,
    Window,
    utcnow,
)
from ling.ports.config import MemoryPolicy

_MAX_ITEMS = 5
_PRIVATE = Visibility.subject_private.value


def _eligible_rows(actor_id: str, subject_id: str, now: datetime) -> Select[tuple[MemoryRow, Window]]:
    return (
        select(MemoryRow, Window)
        .join(Window, Window.id == MemoryRow.source_window_id)
        .where(
            MemoryRow.subject_id == subject_id,
            MemoryRow.visibility == _PRIVATE,
            MemoryRow.source_visibility == _PRIVATE,
            MemoryRow.kind == "assertion",
            MemoryRow.origin == Origin.user_statement.value,
            MemoryRow.retracted.is_(False),
            MemoryRow.archived_at.is_(None),
            or_(MemoryRow.valid_from.is_(None), MemoryRow.valid_from <= now),
            or_(MemoryRow.valid_until.is_(None), MemoryRow.valid_until > now),
            Window.actor_id == actor_id,
            Window.subject_id == subject_id,
            Window.visibility == _PRIVATE,
            Window.archived.is_(False),
        )
    )


async def capture_review_ids(
    session: AsyncSession, *, actor_id: str, subject_id: str, bedtime: datetime, now: datetime,
) -> list[str]:
    """Index existing statements that can be traced to the preceding 24 hours."""
    if await has_open_purge(session, subject_id=subject_id):
        return []
    source_exists = select(Message.id).where(
        Message.turn_id == Turn.id, Message.window_id == Window.id, Message.role == "user",
        Message.state == TurnState.completed.value,
        Message.retracted_at.is_(None), Message.text != "",
    ).exists()
    rows = await session.scalars(
        _eligible_rows(actor_id, subject_id, now)
        .join(Turn, (Turn.id == MemoryRow.turn_id) & (Turn.window_id == Window.id))
        .where(
            Turn.state == TurnState.completed.value,
            Turn.created_at >= bedtime - timedelta(days=1), Turn.created_at < bedtime,
            source_exists,
        )
        .order_by(Turn.created_at.desc(), MemoryRow.id)
        .limit(_MAX_ITEMS * 4)
    )
    ids = [row.id for row in rows if f"turn:{row.turn_id}" in row.source_event_ids]
    return ids[:_MAX_ITEMS]


async def read_sleep_reviews(
    session: AsyncSession, *, actor_id: str, subject_id: str, policy: MemoryPolicy,
    audience_type: type[MemoryAudience], include_life: bool = False,
) -> SleepReviewsOut:
    nights = await recent_consolidations(session, actor_id=actor_id, subject_id=subject_id)
    if await has_open_purge(session, subject_id=subject_id):
        return SleepReviewsOut()
    ids = {memory_id for night in nights for memory_id in (night.review_memory_ids or [])}
    memory = MemoryService(session, policy=policy)
    readable: dict[str, SleepReviewItem] = {}
    if ids:
        rows = await session.execute(
            _eligible_rows(actor_id, subject_id, utcnow()).where(MemoryRow.id.in_(ids))
        )
        for row, window in rows:
            if row.turn_id is None:
                continue
            audience = await audience_type.freeze(
                session, subject_id=subject_id, window_id=window.id, window_visibility=window.visibility,
            )
            original = await memory.read_source(MemoryHit(
                memory_id=row.id, kind="assertion", text=row.text, origin=Origin.user_statement,
                visibility=Visibility.subject_private, confidence=row.confidence,
                source_event_ids=row.source_event_ids, score=0.0, valid_until=row.valid_until,
                turn_id=row.turn_id, source_window_id=window.id,
            ), audience=audience)
            if original:
                readable[row.id] = SleepReviewItem(
                    memory_id=row.id, text=row.text[:240], window_id=window.id, turn_id=row.turn_id,
                    original=[SleepReviewSource.model_validate(source) for source in original],
                )

    entries = []
    for night in nights:
        items = [readable[mid] for mid in (night.review_memory_ids or []) if mid in readable]
        status: Literal["ready", "empty", "unavailable"] = "empty"
        if night.review_memory_ids is None:
            status = "unavailable"
        elif items:
            status = "ready"
        entries.append(SleepReviewNight.model_validate({
            "sleep_date": night.sleep_date,
            "consolidation_state": night.state,
            "reason": night.reason,
            "groups_merged": night.groups_merged,
            "rows_archived": night.rows_archived,
            "review_status": status,
            "items": items,
            "life": await _night_life(session, night, audience_type=audience_type) if include_life else [],
        }))
    return SleepReviewsOut(nights=entries)


async def _night_life(
    session: AsyncSession, night: MemoryConsolidationRow, *, audience_type: type[MemoryAudience],
) -> list[LifeReviewEntry]:
    end = night.review_bedtime
    if end is None:
        settled = await session.get(SleepSettlementRow, (night.actor_id, night.sleep_date))
        end = settled.bed_at if settled is not None else None
    entries: list[LifeReviewEntry] = []
    dream = await session.scalar(select(DreamReportRow).where(
        DreamReportRow.actor_id == night.actor_id, DreamReportRow.sleep_date == night.sleep_date,
        DreamReportRow.status == "ready",
    ))
    if dream is not None and (dream.narrative or dream.title):
        entries.append(LifeReviewEntry(
            kind="dream", source_id=dream.id, text=(dream.narrative or dream.title or "")[:300], status="simulated",
        ))
    if end is None:
        return entries

    start = end - timedelta(days=1)
    activity_rows = await session.scalars(select(ActivityRow).where(
        ActivityRow.actor_id == night.actor_id,
        ActivityRow.ended_at >= start, ActivityRow.ended_at < end,
        ActivityRow.visibility.in_(("public", "subject_private", "actor_private")),
    ).order_by(ActivityRow.ended_at.desc(), ActivityRow.id).limit(12))
    for activity in activity_rows:
        evidence, details = parse_completion_evidence(activity.completion_evidence)
        if activity.state == "completed" and evidence is not EvidenceKind.clock_only:
            entries.append(LifeReviewEntry(
                kind="activity", source_id=activity.id, text=details, status=evidence.value, at=activity.ended_at,
            ))
        elif activity.state == "cancelled" and activity.schedule_segment_id:
            segment = await session.get(ScheduleSegmentRow, activity.schedule_segment_id)
            if segment is not None and segment.actor_id == night.actor_id:
                entries.append(LifeReviewEntry(
                    kind="plan", source_id=segment.id, text=segment.title, status="cancelled", at=activity.ended_at,
                ))
    statements = await session.execute(select(RelationshipEvent, Message).join(
        Message, RelationshipEvent.source_event_ids[0].as_string() == ("message:" + Message.id),
    ).join(Window, Window.id == Message.window_id).where(
        RelationshipEvent.actor_id == night.actor_id, RelationshipEvent.subject_id == night.subject_id,
        RelationshipEvent.kind.in_(("impression", "clarification")), RelationshipEvent.note.is_not(None),
        Window.actor_id == night.actor_id, Window.subject_id == night.subject_id,
        Window.visibility == _PRIVATE, Window.archived.is_(False), Message.created_at >= start,
        Message.created_at < end, Message.memory_hidden_at.is_(None), Message.retracted_at.is_(None),
        or_(
            (RelationshipEvent.kind == "impression") & (Message.role == "actor"),
            (RelationshipEvent.kind == "clarification") & (Message.role == "user")
            & (Message.sender_subject_id == night.subject_id),
        ),
        Message.recalled_at.is_(None),
    ).order_by(Message.created_at.desc(), Message.id).limit(4))
    for event, message in statements:
        kind: Literal["impression", "clarification"] = "impression" if event.kind == "impression" else "clarification"
        audience = await audience_type.freeze(
            session, subject_id=night.subject_id, window_id=message.window_id, window_visibility=_PRIVATE,
        )
        if event.note and audience.allows_long_term_memory():
            entries.append(LifeReviewEntry(
                kind=kind, source_id=message.id, window_id=message.window_id,
                text=event.note, status=event.origin, at=message.created_at,
            ))
    intentions = await session.scalars(select(IntentRow).where(
        IntentRow.actor_id == night.actor_id, IntentRow.subject_id.is_(None),
        IntentRow.visibility.in_(("actor_private", "public")), IntentRow.state == "open",
        IntentRow.created_at >= start, IntentRow.created_at < end,
        IntentRow.source_kind == "self",
    ).order_by(IntentRow.created_at.desc(), IntentRow.id).limit(3))
    entries.extend(LifeReviewEntry(
        kind="intention", source_id=intent.id, text=intent.goal[:240],
        status="open", at=intent.created_at,
    ) for intent in intentions)
    return entries
