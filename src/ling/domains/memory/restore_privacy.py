"""Replay content-free forgetting state against an offline restored database."""

from __future__ import annotations

import asyncio
import sys

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.forget_source import apply_import_forget, apply_source_forget
from ling.domains.memory.purge import purge_subject, request_purge
from ling.infra.models import MemorySourceForgetMarker, Message, Subject, Window


class ForgottenSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=64)
    window_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    forgotten_at: AwareDatetime


class ForgottenImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(min_length=1, max_length=64)
    forgotten_at: AwareDatetime


class ForgetState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[ForgottenSource] = Field(default_factory=list)
    imports: list[ForgottenImport] = Field(default_factory=list)
    generations: dict[str, int] = Field(default_factory=dict)


async def replay_forgetting(session: AsyncSession, state: ForgetState) -> None:
    """Keep restored rows behind the same forgetting boundary as the live database."""
    for subject_id, generation in state.generations.items():
        stored = await session.scalar(select(Subject.forget_generation).where(Subject.id == subject_id))
        if stored is None or stored >= generation:
            continue
        request = await request_purge(session, subject_id=subject_id)
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.execute(update(Subject).where(Subject.id == subject_id).values(
            forget_generation=generation,
        ))

    for marker in state.sources:
        saved = await session.get(MemorySourceForgetMarker, marker.message_id)
        source = await session.get(Message, marker.message_id)
        if source is not None:
            window = await session.get(Window, source.window_id)
            private = (
                window is not None and window.subject_id == marker.subject_id
                and window.visibility in {"subject_private", "window_private"}
            )
            owner = (
                source.role == "user" and source.sender_subject_id == marker.subject_id
            ) or (private and source.role in {"user", "actor"} and source.sender_subject_id is None)
            if not owner or source.window_id != marker.window_id or (source.turn_id or source.id) != marker.turn_id:
                raise ValueError("restored_memory_source_identity_mismatch")
        if saved is not None and (source is None or source.memory_hidden_at is not None):
            continue
        await apply_source_forget(session, **marker.model_dump())

    for imported in state.imports:
        await apply_import_forget(session, **imported.model_dump())


async def _run(state: ForgetState) -> None:
    from ling.config import Settings
    from ling.infra.db import dispose_engine, init_engine, session_scope

    init_engine(Settings())
    try:
        async with session_scope() as session:
            await replay_forgetting(session, state)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_run(ForgetState.model_validate_json(sys.stdin.buffer.read())))
