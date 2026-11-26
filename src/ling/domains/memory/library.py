"""Read the records behind the personal memory library."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from unicodedata import normalize

from sqlalchemy import and_, func, literal, literal_column, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.objects import linked_record_ids
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.self_timeline import SelfTimelineService
from ling.domains.memory.service import active_source
from ling.infra.models import (
    ActivityRow,
    CoreMemoryRow,
    EntityRow,
    MemoryEntityRow,
    MemoryEpisode,
    MemoryRow,
    OutboxEvent,
    SelfExperienceRow,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def memory_record(row: MemoryRow) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "kind": row.kind,
        "text": row.text,
        "origin": row.origin,
        "visibility": row.visibility,
        "confidence": row.confidence,
        "retracted": row.retracted,
        "retract_reason": row.retract_reason,
        "supersedes": row.supersedes,
        "superseded_by": row.superseded_by,
        "source_event_ids": row.source_event_ids or [],
        "valid_until": _iso(row.valid_until),
        "created_at": _iso(row.created_at),
        "archived_at": _iso(row.archived_at),
        **({
            "source_message_id": row.source_message_id,
            "source_window_id": row.source_window_id,
            "source_account": row.source_account,
            "source_role": "user_correction" if row.supersedes and not row.turn_id else "self_report",
            "valid_from": _iso(row.valid_from),
        } if row.source_message_id is not None else {}),
        **({"import_source": row.import_source} if row.import_source is not None else {}),
    }


def _episode_record(row: MemoryEpisode) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "kind": "episode",
        "text": row.summary,
        "origin": row.origin,
        "visibility": row.visibility,
        "source_visibility": row.source_visibility,
        "confidence": None,
        "retracted": False,
        "retract_reason": None,
        "supersedes": None,
        "superseded_by": None,
        "source_event_ids": row.source_event_ids or [],
        "valid_until": None,
        "created_at": _iso(row.created_at),
        "archived_at": _iso(row.archived_at),
        "can_edit": False,
        "record_type": "experience",
        **({
            "source_message_id": row.source_message_id,
            "source_window_id": row.window_id,
            "source_role": "conversation",
            "observed_at": _iso(row.occurred_at),
        } if row.source_message_id is not None else {}),
    }


def _self_experience_record(row: SelfExperienceRow, activity: ActivityRow) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "kind": "episode",
        "text": row.summary,
        "origin": "user_statement" if activity.origin == "user_stated" else activity.origin,
        "visibility": (
            "public" if row.visibility == activity.visibility == "public" else "actor_private"
        ),
        "confidence": None,
        "retracted": False,
        "retract_reason": None,
        "supersedes": None,
        "superseded_by": None,
        "source_event_ids": row.source_event_ids or [],
        "valid_until": None,
        "created_at": _iso(row.created_at),
        "archived_at": None,
        "can_edit": False,
        "record_type": "self_experience",
        "occurred_at": _iso(row.occurred_at),
    }


async def read_library(
    session: AsyncSession, subject_id: str, *,
    actor_id: str, allow_actor_private: bool,
    limit: int, include: str | None, source: str | None = None, offset: int = 0,
    query: str = "", object_id: str | None = None,
    conversation_scope: Literal["private", "group"] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "memories": [], "limit": limit, "include": include, "total": 0, "has_more": False,
        "extraction": None,
    }
    if source is not None:
        result.update(source=source)
    if source is not None or query or object_id or conversation_scope:
        result.update(offset=offset)
    if await has_open_purge(session, subject_id=subject_id):
        result["extraction"] = {"state": "purging", "at": None}
        return result

    if object_id == "self":
        if source is not None or conversation_scope is not None:
            return result
        records = await SelfTimelineService(session, actor_id=actor_id).library_records(
            allow_actor_private=allow_actor_private,
        )
        terms = normalize("NFKC", query).casefold().split()
        matches = [
            (row, activity) for row, activity in records
            if all(term in normalize("NFKC", row.summary).casefold() for term in terms)
        ]
        return {
            **result,
            "total": len(matches),
            "has_more": len(matches) > offset + limit,
            "memories": [
                _self_experience_record(row, activity)
                for row, activity in matches[offset:offset + limit]
            ],
        }

    memory_query = select(MemoryRow).where(MemoryRow.subject_id == subject_id, active_source(MemoryRow))
    episode_query = select(MemoryEpisode).where(MemoryEpisode.subject_id == subject_id, active_source(MemoryEpisode))
    if source is not None:
        memory_query = memory_query.where(MemoryRow.import_source["source"].as_string() == source)
    if include not in {"retracted", "all"}:
        memory_query = memory_query.where(MemoryRow.retracted.is_(False))
    if include not in {"archived", "all"}:
        memory_query = memory_query.where(MemoryRow.archived_at.is_(None))
        episode_query = episode_query.where(MemoryEpisode.archived_at.is_(None))
    if conversation_scope is not None:
        group = "group_scope"
        memory_group = or_(MemoryRow.source_visibility == group, MemoryRow.visibility == group)
        episode_group = or_(MemoryEpisode.source_visibility == group, MemoryEpisode.visibility == group)
        memory_query = memory_query.where(memory_group if conversation_scope == "group" else ~memory_group)
        episode_query = episode_query.where(episode_group if conversation_scope == "group" else ~episode_group)
    if object_id:
        memory_query = memory_query.where(
            MemoryRow.id.in_(linked_record_ids(subject_id, object_id, "memory")),
        )
        episode_query = episode_query.where(
            MemoryEpisode.id.in_(linked_record_ids(subject_id, object_id, "experience")),
        )

    if query:
        terms = tuple(dict.fromkeys(normalize("NFKC", query).split()))
        memory_text = func.normalize(MemoryRow.text, literal_column("NFKC"))
        episode_text = func.normalize(MemoryEpisode.summary, literal_column("NFKC"))
        memory_query = memory_query.where(
            *(memory_text.icontains(term, autoescape=True) for term in terms),
        )
        episode_query = episode_query.where(
            *(episode_text.icontains(term, autoescape=True) for term in terms),
        )

    memory_keys = memory_query.with_only_columns(
        MemoryRow.id, MemoryRow.created_at, literal("memory").label("record_type"),
    )
    episode_keys = episode_query.with_only_columns(
        MemoryEpisode.id, MemoryEpisode.created_at, literal("experience").label("record_type"),
    )
    matches = (
        memory_keys.union_all(episode_keys) if source is None else memory_keys
    ).cte("library_matches")
    page_keys = select(matches).order_by(
        matches.c.created_at.desc(), matches.c.id.desc(), matches.c.record_type.desc(),
    ).offset(offset).limit(limit).subquery()
    counts = select(func.count().label("total")).select_from(matches).subquery()

    # Page and count use one snapshot. Only the selected keys load full records.
    rows = (await session.execute(
        select(MemoryRow, MemoryEpisode, counts.c.total)
        .select_from(counts)
        .outerjoin(page_keys, true())
        .outerjoin(MemoryRow, and_(page_keys.c.record_type == "memory", MemoryRow.id == page_keys.c.id))
        .outerjoin(MemoryEpisode, and_(
            page_keys.c.record_type == "experience", MemoryEpisode.id == page_keys.c.id,
        ))
        .order_by(page_keys.c.created_at.desc(), page_keys.c.id.desc(), page_keys.c.record_type.desc())
    )).all()
    total = rows[0][2]
    record_ids = [memory.id for memory, _, _ in rows if memory is not None]
    pins = set(await session.scalars(select(CoreMemoryRow.source_memory_id).where(
        CoreMemoryRow.subject_id == subject_id, CoreMemoryRow.active.is_(True),
        CoreMemoryRow.source_memory_id.in_(record_ids),
    ))) if record_ids else set()
    speakers: dict[str, list[dict[str, str]]] = {}
    live_ids = [memory.id for memory, _, _ in rows
                if memory is not None and not memory.retracted and memory.archived_at is None]
    if live_ids:
        speaker_rows = await session.execute(select(
            MemoryEntityRow.memory_id, MemoryEntityRow.entity_id,
            MemoryEntityRow.source_reference, MemoryEntityRow.source_label,
        ).join(EntityRow, EntityRow.id == MemoryEntityRow.entity_id).where(
            EntityRow.subject_id == subject_id, MemoryEntityRow.memory_id.in_(live_ids),
            MemoryEntityRow.record_type == "memory", MemoryEntityRow.role == "speaker",
            MemoryEntityRow.source_reference.is_not(None), MemoryEntityRow.source_label.is_not(None),
        ).order_by(MemoryEntityRow.source_label, MemoryEntityRow.entity_id))
        for link in speaker_rows:
            speakers.setdefault(link.memory_id, []).append({
                "entity_id": link.entity_id, "name": link.source_label, "source_event_id": link.source_reference,
            })
    page = []
    for memory, episode, _ in rows:
        if memory is not None:
            page.append({
                **memory_record(memory), "can_edit": True, "record_type": "memory",
                "source_visibility": memory.source_visibility,
                **({"pinned": True} if memory.id in pins else {}),
                **({"archive_speakers": speakers[memory.id]} if memory.id in speakers else {}),
            })
        elif episode is not None:
            page.append(_episode_record(episode))

    latest = (await session.execute(
        select(OutboxEvent.state, OutboxEvent.attempts, OutboxEvent.created_at, OutboxEvent.processed_at)
        .where(OutboxEvent.topic == "memory.extract", or_(
            OutboxEvent.subject_id == subject_id,
            and_(OutboxEvent.subject_id.is_(None), OutboxEvent.payload["subject_id"].as_string() == subject_id),
        ))
        .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc()).limit(1)
    )).one_or_none()
    extraction = None
    if latest is not None:
        state, attempts, created_at, processed_at = latest
        extraction = {
            "state": "retrying" if state == "pending" and attempts else state,
            "at": _iso(processed_at or created_at),
        }
    return {
        **result, "memories": page, "total": total,
        "has_more": total > offset + limit, "extraction": extraction,
    }
