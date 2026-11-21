"""Read the records behind the personal memory library."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.purge import has_open_purge
from ling.infra.models import MemoryEpisode, MemoryRow, OutboxEvent


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
        **({"import_source": row.import_source} if row.import_source is not None else {}),
    }


def _episode_record(row: MemoryEpisode) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "kind": "episode",
        "text": row.summary,
        "origin": row.origin,
        "visibility": row.visibility,
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
    }


async def read_library(
    session: AsyncSession, subject_id: str, *,
    limit: int, include: str | None, source: str | None = None, offset: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "memories": [], "limit": limit, "include": include, "total": 0, "has_more": False,
        "extraction": None,
    }
    if source is not None:
        result.update(source=source, offset=offset)
    if await has_open_purge(session, subject_id=subject_id):
        result["extraction"] = {"state": "purging", "at": None}
        return result

    memory_query = select(MemoryRow).where(MemoryRow.subject_id == subject_id)
    episode_query = select(MemoryEpisode).where(MemoryEpisode.subject_id == subject_id)
    if source is not None:
        memory_query = memory_query.where(MemoryRow.import_source["source"].as_string() == source)
    if include not in {"retracted", "all"}:
        memory_query = memory_query.where(MemoryRow.retracted.is_(False))
    if include not in {"archived", "all"}:
        memory_query = memory_query.where(MemoryRow.archived_at.is_(None))
        episode_query = episode_query.where(MemoryEpisode.archived_at.is_(None))

    memories = (await session.scalars(memory_query.order_by(
        MemoryRow.created_at.desc(), MemoryRow.id.desc(),
    ).offset(offset).limit(limit))).all()
    page = [
        {**memory_record(row), "can_edit": True, "record_type": "memory"}
        for row in memories
    ]
    total = await session.scalar(select(func.count()).select_from(memory_query.subquery())) or 0
    if source is None:
        episodes = await session.scalars(episode_query.order_by(
            MemoryEpisode.created_at.desc(), MemoryEpisode.id.desc(),
        ).limit(limit))
        page.extend(_episode_record(row) for row in episodes)
        page.sort(key=lambda r: (r["created_at"] or "", r["memory_id"]), reverse=True)
        total += await session.scalar(select(func.count()).select_from(episode_query.subquery())) or 0

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
        **result, "memories": page[:limit], "total": total,
        "has_more": total > offset + limit, "extraction": extraction,
    }
