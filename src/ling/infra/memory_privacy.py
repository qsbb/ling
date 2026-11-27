"""Read-only predicates for the shared memory cleanup ledger."""

from __future__ import annotations

from sqlalchemy import ColumnElement, SQLColumnExpression, select

from ling.infra.models import MemoryPurgeTombstone

OPEN_PURGE_STATES = ("pending", "running", "failed")


def open_purge_exists(subject_id: str | SQLColumnExpression[str | None]) -> ColumnElement[bool]:
    return select(MemoryPurgeTombstone.id).where(
        MemoryPurgeTombstone.subject_id == subject_id,
        MemoryPurgeTombstone.state.in_(OPEN_PURGE_STATES),
    ).correlate_except(MemoryPurgeTombstone).exists()
