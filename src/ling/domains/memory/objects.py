"""Object index for the memory library.

Only objects with at least one active memory are listed. Counts are computed
from the same active set as `/v1/memory?view=library`, so the object picker
never promises records the list will hide.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.service import active_source
from ling.infra.models import EntityRow, MemoryEntityRow, MemoryRow


async def list_memory_objects(session: AsyncSession, subject_id: str) -> list[dict]:
    rows = (await session.execute(
        select(
            EntityRow.id, EntityRow.name, EntityRow.kind, EntityRow.aliases,
            func.count(func.distinct(MemoryRow.id)).label("count"),
        )
        .select_from(EntityRow)
        .join(MemoryEntityRow, MemoryEntityRow.entity_id == EntityRow.id)
        .join(MemoryRow, MemoryRow.id == MemoryEntityRow.memory_id)
        .where(
            EntityRow.subject_id == subject_id,
            MemoryRow.subject_id == subject_id,
            active_source(MemoryRow),
            MemoryRow.retracted.is_(False),
            MemoryRow.archived_at.is_(None),
        )
        .group_by(EntityRow.id)
        .order_by(func.count(func.distinct(MemoryRow.id)).desc(), EntityRow.name.asc())
    )).all()
    objects = [
        {
            "entity_id": row.id,
            "name": row.name,
            "kind": row.kind,
            "aliases": row.aliases or [],
            "count": int(row.count),
        }
        for row in rows
    ]
    self_count = int(await session.scalar(
        select(func.count()).select_from(MemoryRow).where(
            MemoryRow.subject_id == subject_id,
            active_source(MemoryRow),
            MemoryRow.retracted.is_(False),
            MemoryRow.archived_at.is_(None),
        )
    ) or 0)
    return [{
        "entity_id": "self", "name": "她自己", "kind": "self", "aliases": [], "count": self_count,
    }, *objects]


__all__ = ["list_memory_objects"]
