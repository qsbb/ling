"""Forget a sourced memory and the material derived from its conversation turn."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, cast, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, array, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.goals.service import commitment_source_available, forget_conversation_commitments
from ling.domains.memory.astrbot_import import lock_archive_account
from ling.domains.memory.purge import PURGEABLE_TOPICS
from ling.domains.memory.service import active_source
from ling.infra.models import (
    AwarenessItemRow,
    CommitmentRow,
    EntityRow,
    HandoffCapsuleRow,
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryEpisode,
    MemoryImportForgetMarker,
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

_FORGET_COMMAND = re.compile(r"(?:请)?(?:帮我)?忘(?:掉|记)(?P<target>[^\r\n]{1,240})")
_QUOTED_SOURCE = re.compile(
    r"(?:我(?:在这段对话里|在这里|刚才)?说过的)?"
    r"(?:『([^』]+)』|「([^」]+)」|“([^”]+)”)"
    r"(?:这(?:句|条)(?:话|记忆)?)?"
)
_FACT_TARGETS = {
    "住址": "lives_in", "地址": "lives_in", "我住哪": "lives_in", "住在哪里": "lives_in",
    "工作地点": "works_at", "工作单位": "works_at",
}
FORGET_CLARIFICATION = "请引用要忘掉的那句原话，或在记忆库里选中一条记忆。"


@dataclass(frozen=True)
class ForgetRequest:
    quote: str | None = None
    slot: str | None = None


def parse_forget_request(text: str) -> ForgetRequest | None:
    """Recognize a standalone command. Only a quoted source or a named fact can delete."""
    match = _FORGET_COMMAND.fullmatch(text.strip().rstrip("。！!"))
    if match is None or "\x00" in text:
        return None
    target = match["target"].strip()
    if target.endswith(("?", "？", "吗", "么")):
        return None
    if quoted := _QUOTED_SOURCE.fullmatch(target):
        phrase = next(part.strip() for part in quoted.groups() if part is not None)
        if len(phrase) >= 2:
            return ForgetRequest(quote=phrase)
    return ForgetRequest(slot=_FACT_TARGETS.get(target.removeprefix("我的")))


async def forget_from_conversation(
    session: AsyncSession, *, request: ForgetRequest,
    subject_id: str, window_id: str, turn_id: str,
) -> str | None:
    """Remove exactly one earlier source in the caller's private conversation."""
    window = await session.get(Window, window_id)
    if (
        window is None or window.subject_id != subject_id or window.archived
        or window.visibility not in {"subject_private", "window_private"}
    ):
        return None
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    now = utcnow()
    command = await session.scalar(select(Message).where(
        Message.turn_id == turn_id, Message.window_id == window_id,
        Message.role == "user", Message.sender_subject_id == subject_id,
        Message.retracted_at.is_(None),
    ).order_by(Message.sequence.desc()).limit(1).with_for_update())
    if command is None or command.memory_hidden_at is not None:
        return None
    command.memory_hidden_at = now
    if request.quote is None and request.slot is None:
        return None

    facts = select(MemoryRow.id).where(
        MemoryRow.source_message_id == Message.id, MemoryRow.subject_id == subject_id,
        MemoryRow.source_window_id == window_id,
        MemoryRow.retracted.is_(False), MemoryRow.archived_at.is_(None),
    )
    sources = select(Message).where(
        Message.window_id == window_id, Message.role == "user",
        Message.sender_subject_id == subject_id, Message.turn_id.is_not(None),
        Message.sequence < command.sequence, Message.retracted_at.is_(None),
        Message.memory_hidden_at.is_(None),
    )
    if request.quote is not None:
        episode = select(MemoryEpisode.id).where(
            MemoryEpisode.source_message_id == Message.id,
            MemoryEpisode.subject_id == subject_id, MemoryEpisode.window_id == window_id,
            MemoryEpisode.archived_at.is_(None),
        )
        sources = sources.where(
            Message.text.contains(request.quote, autoescape=True), or_(facts.exists(), episode.exists()),
        )
    else:
        sources = sources.where(facts.where(MemoryRow.slot == request.slot).exists())
    matches = (await session.scalars(sources.limit(2).with_for_update(of=Message))).all()
    if len(matches) != 1:
        return None
    source = matches[0]
    await apply_source_forget(
        session, subject_id=subject_id, message_id=source.id,
        window_id=window_id, turn_id=source.turn_id, forgotten_at=now,
    )
    return source.id


async def forget_source(
    session: AsyncSession, *, subject_id: str, memory_id: str,
) -> bool:
    """Remove one source cluster. Independent messages remain eligible for learning."""
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    target = await session.scalar(select(MemoryRow).where(
        MemoryRow.id == memory_id, MemoryRow.subject_id == subject_id,
    ))
    if target is not None and target.import_key:
        await lock_archive_account(session, target.import_source["owner_qq"])
        if await session.scalar(select(MemoryRow.id).where(
            MemoryRow.id == memory_id, MemoryRow.subject_id == subject_id, active_source(MemoryRow),
        )) is None:
            return False
        await apply_import_forget(
            session, source_key=target.import_key, subject_id=subject_id, forgotten_at=utcnow(),
        )
        return True
    episode = None
    if target is None:
        episode = await session.scalar(select(MemoryEpisode).where(
            MemoryEpisode.id == memory_id, MemoryEpisode.subject_id == subject_id,
        ).with_for_update())
    record = target or episode
    if record is None:
        plan = await session.scalar(select(CommitmentRow).where(
            CommitmentRow.id == memory_id, CommitmentRow.subject_id == subject_id, commitment_source_available(),
        ))
        if plan is None:
            return False
        reply = await session.get(Message, plan.source_event_ids[0].removeprefix("message:"))
        if reply is None or reply.window_id != plan.window_id or not reply.turn_id:
            return False
        anchors = list(await session.scalars(select(Message).where(
            Message.turn_id == reply.turn_id, Message.window_id == reply.window_id, Message.role == "user",
            Message.sender_subject_id == subject_id, Message.retracted_at.is_(None),
            Message.memory_hidden_at.is_(None),
        ).limit(2).with_for_update()))
        if len(anchors) != 1:
            return False
        await apply_source_forget(
            session, subject_id=subject_id, message_id=anchors[0].id,
            window_id=reply.window_id, turn_id=reply.turn_id, forgotten_at=utcnow(),
        )
        return True
    if not record.source_message_id:
        return False
    window_id = target.source_window_id if target else episode.window_id
    source = await session.get(Message, record.source_message_id, with_for_update=True)
    if (
        source is None or source.role != "user" or source.window_id != window_id
        or source.sender_subject_id != subject_id or not source.turn_id
    ):
        return False

    await apply_source_forget(
        session, subject_id=subject_id, message_id=source.id,
        window_id=source.window_id, turn_id=source.turn_id, forgotten_at=utcnow(),
    )
    return True


async def apply_import_forget(
    session: AsyncSession, *, source_key: str, subject_id: str, forgotten_at: datetime,
) -> None:
    """Remove an archive record and its corrections, including newly restored copies."""
    await session.execute(insert(MemoryImportForgetMarker).values(
        source_key=source_key, subject_id=subject_id, forgotten_at=forgotten_at,
    ).on_conflict_do_nothing(index_elements=[MemoryImportForgetMarker.source_key]))
    rows = (await session.execute(delete(MemoryRow).where(
        MemoryRow.import_key == source_key,
    ).returning(MemoryRow.id, MemoryRow.subject_id))).all()
    await _delete_memory_derivatives(session, [row.id for row in rows], [])
    for owner_id in {row.subject_id for row in rows}:
        await _clear_window_derivatives(session, owner_id)


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
    await forget_conversation_commitments(session, subject_id=subject_id, window_id=window_id, turn_id=turn_id)
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

    await _delete_memory_derivatives(session, assertion_ids, episode_ids)
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
    await _clear_window_derivatives(session, subject_id, window_id)


async def _delete_memory_derivatives(
    session: AsyncSession, memory_ids: list[str], episode_ids: list[str],
) -> None:
    if not memory_ids and not episode_ids:
        return
    links = or_(
        and_(MemoryEntityRow.record_type == "memory", MemoryEntityRow.memory_id.in_(memory_ids)),
        and_(MemoryEntityRow.record_type == "experience", MemoryEntityRow.memory_id.in_(episode_ids)),
    )
    entity_ids = list(await session.scalars(
        delete(MemoryEntityRow).where(links).returning(MemoryEntityRow.entity_id)
    ))
    if entity_ids:
        await session.execute(delete(EntityRow).where(
            EntityRow.id.in_(entity_ids),
            ~select(MemoryEntityRow.entity_id).where(MemoryEntityRow.entity_id == EntityRow.id).exists(),
        ))
    await session.execute(delete(MemoryEmbeddingRow).where(
        MemoryEmbeddingRow.memory_id.in_([*memory_ids, *episode_ids]),
    ))


async def _clear_window_derivatives(
    session: AsyncSession, subject_id: str, window_id: str | None = None,
) -> None:
    windows = select(Window.id).where(or_(Window.subject_id == subject_id, Window.id == window_id))
    await session.execute(delete(WindowContext).where(WindowContext.window_id.in_(windows)))
    await session.execute(delete(PromptSnapshot).where(PromptSnapshot.window_id.in_(windows)))
    await session.execute(delete(MemoryRetrievalLog).where(MemoryRetrievalLog.window_id.in_(windows)))
    await session.execute(delete(HandoffCapsuleRow).where(
        HandoffCapsuleRow.subject_id == subject_id,
        HandoffCapsuleRow.source_window_id.in_(windows),
    ))
