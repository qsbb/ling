"""Forget a sourced memory and the material derived from its conversation turn."""

from datetime import datetime

from sqlalchemy import case, cast, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, array, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.purge import PURGEABLE_TOPICS
from ling.infra.models import (
    AwarenessItemRow,
    EntityRow,
    HandoffCapsuleRow,
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryEpisode,
    MemoryRetrievalLog,
    MemoryRow,
    MemorySourceForgetMarker,
    Message,
    OutboxEvent,
    PromptSnapshot,
    RelationshipEvent,
    SharedArrangement,
    SharedArrangementEvidence,
    Subject,
    Window,
    WindowContext,
    utcnow,
)


async def forget_source(
    session: AsyncSession, *, subject_id: str, memory_id: str,
) -> bool:
    """Remove one source cluster. Independent messages remain eligible for learning."""
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    target = await session.scalar(select(MemoryRow).where(
        MemoryRow.id == memory_id, MemoryRow.subject_id == subject_id,
    ).with_for_update())
    if target is None or not target.source_message_id:
        return False
    source = await session.get(Message, target.source_message_id, with_for_update=True)
    if (
        source is None or source.role != "user" or source.window_id != target.source_window_id
        or source.sender_subject_id != subject_id or not source.turn_id
    ):
        return False

    await apply_source_forget(
        session, subject_id=subject_id, message_id=source.id,
        window_id=source.window_id, turn_id=source.turn_id, forgotten_at=utcnow(),
    )
    return True


async def apply_source_forget(
    session: AsyncSession, *, subject_id: str, message_id: str, window_id: str, turn_id: str,
    forgotten_at: datetime,
) -> None:
    """Apply an authenticated source marker to a live or staged database."""
    await session.execute(
        insert(MemorySourceForgetMarker).values(
            message_id=message_id, subject_id=subject_id, window_id=window_id,
            turn_id=turn_id, forgotten_at=forgotten_at,
        ).on_conflict_do_nothing(index_elements=[MemorySourceForgetMarker.message_id])
    )
    refs = array([f"turn:{turn_id}", f"message:{message_id}"])
    assertion_ids = list(await session.scalars(select(MemoryRow.id).where(
        MemoryRow.subject_id == subject_id,
        or_(
            MemoryRow.turn_id == turn_id,
            MemoryRow.source_message_id == message_id,
            cast(MemoryRow.source_event_ids, JSONB).has_any(refs),
        ),
    )))
    episode_ids = list(await session.scalars(select(MemoryEpisode.id).where(
        MemoryEpisode.subject_id == subject_id,
        or_(MemoryEpisode.turn_id == turn_id, MemoryEpisode.source_message_id == message_id),
    )))

    await session.execute(update(Message).where(
        Message.window_id == window_id, Message.turn_id == turn_id,
    ).values(memory_hidden_at=forgotten_at))
    await session.execute(delete(SharedArrangement).where(
        SharedArrangement.id.in_(select(SharedArrangementEvidence.arrangement_id).where(
            SharedArrangementEvidence.message_id == message_id,
        )),
    ))
    event_condition = or_(
        OutboxEvent.payload["turn_id"].as_string() == turn_id,
        OutboxEvent.payload["message_id"].as_string() == message_id,
    )
    await session.execute(update(OutboxEvent).where(
        event_condition, OutboxEvent.topic.in_(PURGEABLE_TOPICS),
    ).values(
        state=case(
            (OutboxEvent.state.in_(("pending", "in_flight")), "cancelled"),
            else_=OutboxEvent.state,
        ),
        payload={"purged": True}, last_error="memory_source_forgotten",
    ))

    entity_ids = list(await session.scalars(select(MemoryEntityRow.entity_id).where(
        MemoryEntityRow.memory_id.in_(assertion_ids),
    )))
    await session.execute(delete(MemoryEntityRow).where(MemoryEntityRow.memory_id.in_(assertion_ids)))
    if entity_ids:
        await session.execute(delete(EntityRow).where(
            EntityRow.id.in_(entity_ids),
            ~select(MemoryEntityRow.entity_id).where(MemoryEntityRow.entity_id == EntityRow.id).exists(),
        ))
    await session.execute(delete(MemoryEmbeddingRow).where(
        MemoryEmbeddingRow.memory_id.in_([*assertion_ids, *episode_ids]),
    ))
    await session.execute(delete(MemoryRow).where(MemoryRow.id.in_(assertion_ids)))
    await session.execute(delete(MemoryEpisode).where(MemoryEpisode.id.in_(episode_ids)))
    await session.execute(update(RelationshipEvent).where(
        RelationshipEvent.subject_id == subject_id, RelationshipEvent.turn_id == turn_id,
    ).values(note=None))
    await session.execute(delete(AwarenessItemRow).where(
        AwarenessItemRow.subject_id == subject_id,
        cast(AwarenessItemRow.source_event_ids, JSONB).has_any(refs),
    ))

    # These stores currently retain whole-window aggregates, with no per-claim
    # provenance. Clear the affected subject's cached copies and the source window.
    windows = select(Window.id).where(or_(Window.subject_id == subject_id, Window.id == window_id))
    await session.execute(delete(WindowContext).where(WindowContext.window_id.in_(windows)))
    await session.execute(delete(PromptSnapshot).where(PromptSnapshot.window_id.in_(windows)))
    await session.execute(delete(MemoryRetrievalLog).where(MemoryRetrievalLog.window_id.in_(windows)))
    await session.execute(delete(HandoffCapsuleRow).where(
        HandoffCapsuleRow.subject_id == subject_id,
        HandoffCapsuleRow.source_window_id.in_(windows),
    ))
