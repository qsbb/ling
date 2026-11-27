"""Check whether a retained model input can still use its recorded sources."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.orm import aliased

from ling.infra.models import (
    KnowledgeItem,
    MediaEvidenceRow,
    MemoryPurgeTombstone,
    Message,
    WindowContext,
    utcnow,
)


def context_sources_available(
    owner: type[Message] | type[WindowContext], *, group_sources: Collection[str] = (),
) -> ColumnElement[bool]:
    from ling.domains.knowledge.service import current_knowledge, knowledge_visible_to
    from ling.domains.media.service import readable_media_evidence

    now = utcnow()
    refs = func.unnest(owner.context_sources).table_valued("ref").render_derived(name="context_source")
    kind = func.split_part(refs.c.ref, ":", 1)
    source_id = func.split_part(refs.c.ref, ":", 2)
    message = aliased(Message)
    forgotten = select(MemoryPurgeTombstone.id).where(
        MemoryPurgeTombstone.subject_id == message.sender_subject_id,
        MemoryPurgeTombstone.requested_at >= message.created_at,
    ).correlate(message).exists()
    message_available = select(message.id).where(
        message.id == source_id, message.memory_hidden_at.is_(None),
        message.retracted_at.is_(None), message.recalled_at.is_(None), ~forgotten,
    ).correlate(refs).exists()
    media_available = select(MediaEvidenceRow.id).where(
        MediaEvidenceRow.id == source_id, readable_media_evidence(now=now),
        or_(MediaEvidenceRow.expires_at.is_(None), MediaEvidenceRow.expires_at > now),
    ).correlate(refs).exists()
    knowledge_available = select(KnowledgeItem.id).where(
        KnowledgeItem.id == source_id, knowledge_visible_to(KnowledgeItem.owner_subject_id),
        current_knowledge(now),
    ).correlate(refs).exists()
    valid = case(
        (kind == "message", message_available),
        (kind == "shared", message_available & source_id.in_(group_sources)),
        (kind == "media", media_available),
        (kind == "knowledge", knowledge_available),
        else_=False,
    )
    invalid = select(refs.c.ref).where(~valid).correlate(owner).exists()
    return owner.context_sources.is_(None) | ~invalid
