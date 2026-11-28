"""Participant controls and sourced expressions for a relationship."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from unicodedata import normalize

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import (
    Message,
    RelationshipPreference,
    Subject,
    UserPreferenceRow,
    Window,
    utcnow,
)
from ling.infra.security import new_id

PreferenceState = Literal["pending", "active", "disabled"]
PreferenceKind = Literal["boundary", "expression"]

_BOUNDARY = re.compile(
    r"^(?:请|以后|下次|麻烦)?(?:不(?:要|想)再(?:提及?|谈|说|聊)|"
    r"别(?:再|跟我|主动)*(?:提及?|聊|谈|说)|停止提及?)\s*[：:\s]*([^。！!？?，,\n]{2,80})"
)
_DONT_NUDGE = re.compile(
    r"^(?:请|以后|下次|麻烦|我不喜欢(?:突然|临时)?被)?"
    r"(?:不要(?:再|临时|一直)?|别(?:再|老|临时)?|)催(?:我|促)?[啊呀了。！!\s]*$"
)
_EXPRESSION = re.compile(
    r"^(?:我们(?:在这里)?(?:常|平时|以后)?(?:把|就把)|咱们(?:就)?把)"
    r"([^。？！?!，,\n]{2,40})(?:简称为|叫作|叫做|叫)\s*[「“]?([^。？！?!，,\n」”]{2,32})"
)


def normalize_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", normalize("NFKC", text).casefold())


def boundary_from_message(text: str) -> str | None:
    if _DONT_NUDGE.fullmatch(text.strip()):
        return "催促"
    match = _BOUNDARY.match(text.strip())
    if match is None:
        return None
    topic = match.group(1).strip(" 「」“”")
    return topic.removesuffix("的话题").removesuffix("这个话题").removesuffix("了").strip() or None


def blocks_topic(topic: str, boundaries: list[str]) -> bool:
    text = normalize_key(topic)
    return any(len(key) >= 2 and key in text for label in boundaries if (key := normalize_key(label)))


def preference_scope(window: Window) -> str:
    return window.id if window.visibility == "group_scope" else "private"


@dataclass(frozen=True)
class SharePause:
    paused: bool
    until: datetime | None


async def share_pause(
    session: AsyncSession, subject_id: str, *, now: datetime | None = None,
) -> SharePause:
    pref = await session.get(UserPreferenceRow, subject_id, populate_existing=True)
    if pref is None or not pref.share_paused:
        return SharePause(False, None)
    until = pref.share_paused_until
    return SharePause(until is None or until > (now or utcnow()), until)


async def set_share_pause(
    session: AsyncSession, subject_id: str, *, paused: bool, until: datetime | None = None,
) -> SharePause:
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    values = {"share_paused": paused, "share_paused_until": until if paused else None, "updated_at": utcnow()}
    await session.execute(insert(UserPreferenceRow).values(
        subject_id=subject_id, **values,
    ).on_conflict_do_update(index_elements=[UserPreferenceRow.subject_id], set_=values))
    return SharePause(paused, until if paused else None)


async def save_preference(
    session: AsyncSession,
    *,
    actor_id: str,
    subject_id: str,
    scope: str,
    kind: PreferenceKind,
    text: str,
    state: PreferenceState,
    source_message_id: str | None = None,
) -> RelationshipPreference:
    key = normalize_key(text)
    if len(key) < 2 or len(text) > 160:
        raise ValueError("请输入具体的话题或表达。")
    now = utcnow()
    insert_row = insert(RelationshipPreference).values(
        id=new_id("pref"), actor_id=actor_id, subject_id=subject_id, scope=scope,
        kind=kind, key=key, text=text, state=state, source_message_id=source_message_id,
        created_at=now, updated_at=now,
    )
    # A learned suggestion cannot reactivate a dismissed or approved item.
    if state == "pending":
        await session.execute(insert_row.on_conflict_do_nothing(constraint="uq_relationship_preference"))
    else:
        await session.execute(insert_row.on_conflict_do_update(
            constraint="uq_relationship_preference",
            set_={
                "text": text, "state": state, "source_message_id": source_message_id, "updated_at": now,
            },
        ))
    return (await session.scalars(select(RelationshipPreference).where(
        RelationshipPreference.actor_id == actor_id, RelationshipPreference.subject_id == subject_id,
        RelationshipPreference.scope == scope, RelationshipPreference.kind == kind,
        RelationshipPreference.key == key,
    ).execution_options(populate_existing=True))).one()


async def note_expression(
    session: AsyncSession, *, window: Window, subject_id: str, source: Message, text: str,
) -> None:
    match = _EXPRESSION.match(text.strip())
    if match is not None:
        topic, name = match.groups()
        await save_preference(
            session, actor_id=window.actor_id, subject_id=subject_id, scope=preference_scope(window),
            kind="expression", text=f"{topic.strip()}称作「{name.strip()}」",
            state="pending", source_message_id=source.id,
        )


async def preferences(
    session: AsyncSession, *,
    actor_id: str, subject_id: str, scope: str | None = None, active_only: bool = False,
) -> list[RelationshipPreference]:
    query = select(RelationshipPreference).outerjoin(Message, Message.id == RelationshipPreference.source_message_id)
    query = query.where(
        RelationshipPreference.actor_id == actor_id, RelationshipPreference.subject_id == subject_id,
        or_(
            RelationshipPreference.source_message_id.is_(None),
            and_(
                Message.role == "user", Message.sender_subject_id == subject_id,
                Message.retracted_at.is_(None), Message.memory_hidden_at.is_(None),
                or_(RelationshipPreference.scope == "private", RelationshipPreference.scope == Message.window_id),
            ),
        ),
    )
    if scope is not None:
        query = query.where(RelationshipPreference.scope == scope)
    if active_only:
        query = query.where(RelationshipPreference.state == "active")
    return list(await session.scalars(
        query.order_by(RelationshipPreference.updated_at.desc(), RelationshipPreference.id).limit(100)
    ))


def preference_record(row: RelationshipPreference) -> dict[str, str | None]:
    return {
        "id": row.id, "kind": row.kind, "text": row.text, "scope": row.scope,
        "state": row.state, "source_message_id": row.source_message_id,
    }
