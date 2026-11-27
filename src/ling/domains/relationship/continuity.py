"""Keep a character's impression separate from the participant's own clarification."""

from __future__ import annotations

import re

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin
from ling.domains.relationship.preferences import blocks_topic, preference_scope, preferences
from ling.domains.relationship.service import RelationshipService
from ling.infra.models import Message, RelationshipEvent, RelationshipState, Window

_IMPRESSION = re.compile(r"我(?:刚才)?(?:以为|还以为|感觉|觉得)你(?:可能|有点|好像|是)?(?:难过|失落|不高兴|生气|很累|在忙)")
_CLARIFICATION = re.compile(r"^(?:其实)?我(?:刚才|当时)?(?:不是|没有|没在?).{1,16}[，,。\s]*(?:只是|是因为|我在)")
_APOLOGY = re.compile(r"^(?:刚才|上次|真的|是我.{0,8}[，, ])?对不起|^我(?:向你)?道歉")
_FULFILLMENT = re.compile(r"^(?:我|你)(?:已经)?(?:按约定|照说好的).{1,55}(?:了|完成)")
_BREACH = re.compile(r"^你(?:又|还是).{1,35}(?:答应|说好|越界|说过|不遵守)")
_PRESSURE = re.compile(r"赶紧|快点|怎么还不|现在就去|还没做完吗")


def interaction_kind(text: str) -> str | None:
    plain = text.strip()
    for pattern, kind in (
        (_CLARIFICATION, "clarification"), (_APOLOGY, "apology"),
        (_FULFILLMENT, "fulfillment"), (_BREACH, "breach"),
    ):
        if pattern.search(plain):
            return kind
    return None


async def note_impression(
    session: AsyncSession, *, subject_id: str, window: Window, turn_id: str, reply: str,
) -> None:
    if window.visibility != "subject_private" or window.subject_id != subject_id:
        return
    match = _IMPRESSION.search(reply)
    if match is None:
        return
    source = await session.scalar(select(Message).where(
        Message.window_id == window.id, Message.turn_id == turn_id, Message.role == "actor",
        Message.state == "completed", Message.memory_hidden_at.is_(None),
        Message.recalled_at.is_(None), Message.text == reply,
    ).order_by(Message.attempt.desc()).limit(1))
    if source is None:
        return
    await RelationshipService(session, actor_id=window.actor_id).record_event(
        subject_id=subject_id, kind="impression", origin=Origin.model_inference,
        source_event_ids=[f"message:{source.id}"], occurred_at=source.created_at,
        note=match.group(), turn_id=turn_id, apply_to_projection=False,
    )


async def latest_understanding(
    session: AsyncSession, *, subject_id: str, window: Window,
) -> tuple[str, str] | None:
    if window.visibility != "subject_private" or window.subject_id != subject_id:
        return None
    row = (await session.execute(select(RelationshipEvent.kind, RelationshipEvent.note).join(
        Message, RelationshipEvent.source_event_ids[0].as_string() == (literal("message:") + Message.id),
    ).where(
        RelationshipEvent.actor_id == window.actor_id, RelationshipEvent.subject_id == subject_id,
        RelationshipEvent.kind.in_(("impression", "clarification")), RelationshipEvent.note.is_not(None),
        Message.window_id == window.id, Message.memory_hidden_at.is_(None), Message.retracted_at.is_(None),
        Message.recalled_at.is_(None),
    ).order_by(Message.created_at.desc(), Message.sequence.desc()).limit(1))).one_or_none()
    return (row.kind, row.note) if row is not None else None


async def reply_boundaries(
    session: AsyncSession, *, window: Window, subject_id: str,
) -> list[str]:
    out = [rule.text for rule in await preferences(
        session, actor_id=window.actor_id, subject_id=subject_id, scope=preference_scope(window),
        active_only=True,
    ) if rule.kind == "boundary"]
    if window.visibility != "group_scope" and window.subject_id == subject_id:
        legacy = await session.scalar(select(RelationshipState.boundaries).where(
            RelationshipState.actor_id == window.actor_id, RelationshipState.subject_id == subject_id,
        ))
        out += [item for item in legacy or [] if isinstance(item, str)]
    return out


def respect_boundaries(text: str, boundaries: list[str]) -> str:
    if blocks_topic(text, boundaries) or ("催促" in boundaries and _PRESSURE.search(text)):
        return "我们可以聊聊别的。"
    return text
