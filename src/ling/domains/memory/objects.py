"""Object browsing and evidence-anchored annotation for the memory library."""

from __future__ import annotations

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.entities import EntityService, ObjectKind, RecordType
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.self_timeline import SelfTimelineService
from ling.domains.memory.service import active_source
from ling.infra.models import EntityRow, MemoryEntityRow, MemoryEpisode, MemoryRow, Message, Subject


def linked_record_ids(subject_id: str, object_id: str, record_type: RecordType):
    return (
        select(MemoryEntityRow.memory_id)
        .join(EntityRow, EntityRow.id == MemoryEntityRow.entity_id)
        .where(
            EntityRow.subject_id == subject_id, EntityRow.id == object_id,
            MemoryEntityRow.record_type == record_type,
        )
    )


def _usable_records(model: type[MemoryRow] | type[MemoryEpisode], subject_id: str):
    query = select(model).where(
        model.subject_id == subject_id, active_source(model), model.archived_at.is_(None),
    )
    return query.where(MemoryRow.retracted.is_(False)) if model is MemoryRow else query


async def list_memory_objects(
    session: AsyncSession, subject_id: str, *, actor_id: str, allow_actor_private: bool,
) -> list[dict]:
    if await has_open_purge(session, subject_id=subject_id):
        return []
    records = _usable_records(MemoryRow, subject_id).with_only_columns(
        MemoryRow.id, MemoryRow.created_at, literal("memory").label("record_type"),
    ).union_all(
        _usable_records(MemoryEpisode, subject_id).with_only_columns(
            MemoryEpisode.id, MemoryEpisode.created_at, literal("experience").label("record_type"),
        )
    ).cte("object_records")
    linked_records = func.count()
    rows = (await session.execute(
        select(
            EntityRow.id, EntityRow.name, EntityRow.kind, EntityRow.aliases,
            linked_records.label("count"), func.max(records.c.created_at).label("last_recorded_at"),
        )
        .select_from(EntityRow)
        .join(MemoryEntityRow, MemoryEntityRow.entity_id == EntityRow.id)
        .join(records, and_(
            records.c.id == MemoryEntityRow.memory_id,
            records.c.record_type == MemoryEntityRow.record_type,
        ))
        .where(EntityRow.subject_id == subject_id)
        .group_by(EntityRow.id)
        .order_by(linked_records.desc(), EntityRow.name, EntityRow.id)
    )).all()
    objects = [
        {
            "entity_id": row.id,
            "name": row.name,
            "kind": row.kind,
            "aliases": row.aliases or [],
            "count": int(row.count),
            "last_recorded_at": row.last_recorded_at.isoformat(),
        }
        for row in rows
    ]
    self_records = await SelfTimelineService(session, actor_id=actor_id).library_records(
        allow_actor_private=allow_actor_private,
    )
    return [{
        "entity_id": "self", "name": "她自己", "kind": "self", "aliases": [], "count": len(self_records),
    }, *objects]


class ObjectAnnotationError(ValueError):
    pass


async def annotate_memory_object(
    session: AsyncSession, *, subject_id: str, memory_id: str, kind: ObjectKind,
    name: str, object_id: str | None = None,
) -> str:
    """Pin a literal name, optionally confirming it is the same object as another source."""
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    if await has_open_purge(session, subject_id=subject_id):
        raise ObjectAnnotationError("memory_unavailable")
    memory = await session.scalar(_usable_records(MemoryRow, subject_id).where(
        MemoryRow.id == memory_id,
    ).with_for_update())
    record_type: RecordType = "memory"
    if memory is not None:
        record, text, window_id = memory, memory.text, memory.source_window_id
    else:
        episode = await session.scalar(_usable_records(MemoryEpisode, subject_id).where(
            MemoryEpisode.id == memory_id,
        ).with_for_update())
        if episode is None:
            raise ObjectAnnotationError("memory_unavailable")
        record, text, window_id = episode, episode.summary, episode.window_id
        record_type = "experience"
    if not record.source_event_ids or name not in text:
        raise ObjectAnnotationError("literal_required")
    if record.source_message_id is not None:
        source = await session.get(Message, record.source_message_id, with_for_update=True)
        if source is None or source.retracted_at is not None or source.memory_hidden_at is not None:
            raise ObjectAnnotationError("memory_unavailable")
        if name not in source.text:
            raise ObjectAnnotationError("literal_required")

    entities = EntityService(session)
    if object_id is None:
        entity = await entities.evidence_label(
            subject_id=subject_id, memory_id=memory_id, record_type=record_type, name=name, kind=kind,
        )
    else:
        entity = await session.scalar(select(EntityRow).where(
            EntityRow.subject_id == subject_id, EntityRow.id == object_id,
            EntityRow.name == name, EntityRow.kind == kind,
        ))
        if entity is None:
            raise ObjectAnnotationError("object_unavailable")
        visible = False
        for model, linked_type, source_window in (
            (MemoryRow, "memory", MemoryRow.source_window_id),
            (MemoryEpisode, "experience", MemoryEpisode.window_id),
        ):
            related = _usable_records(model, subject_id).where(
                model.id.in_(linked_record_ids(subject_id, object_id, linked_type)),
                model.visibility == record.visibility, model.source_visibility == record.source_visibility,
            )
            if record.source_visibility in {"group_scope", "window_private"}:
                related = related.where(source_window == window_id)
            if await session.scalar(related.with_only_columns(model.id).limit(1)) is not None:
                visible = True
                break
        if not visible:
            raise ObjectAnnotationError("object_unavailable")
    await entities.link(memory_id=memory_id, entity_id=entity.id, role="mentioned", record_type=record_type)
    return entity.id
