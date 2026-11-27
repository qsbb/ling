"""Evidence navigation for conversations and user-pinned messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.recall_query import (
    needs_recent_topic,
    opens_new_topic,
    should_skip_automatic_recall,
)
from ling.domains.memory.service import MemoryService, _topic_tokens
from ling.infra.models import (
    CommitmentRow,
    MemoryEpisode,
    MemoryRow,
    Message,
    Subject,
    Turn,
    Window,
    utcnow,
)

PINNED_SLOT = "pinned_message"
PRIVATE_VISIBILITIES = ("subject_private", "window_private")
MAX_SOURCE_TURNS = 5
MAX_ORIGINAL_CHARS = 300
_PREFERENCE_REVISION = re.compile(r"(?:其实|更正一下[，, ]*|说错了[，, ]*|我)*(?:更?喜欢的?是|不再喜欢)")
_REPAIR = re.compile(r"^(?:我)?(?:刚才|之前|上次)?(?:我)?(?:说错了|更正一下|纠正一下)")
_QUOTE_WORD = re.compile(r"原话|原文(?!字)")
_QUOTE_REQUEST = re.compile(r"回查|查找|查查|查一下|找回|找出|找一下|找到|翻找|翻出|翻一下|回忆|记得|想知道|说说|复述")
_QUOTE_QUESTION = re.compile(r"\s*(?:是(?:什么|哪(?:一)?[句条段])|[吗呢]?[？?])")
_ORIGINALS = re.compile(
    r"(?:最早|最初|一开始|第一次|前天|昨天|周[一二三四五六日天]|上次|之前|当时).{0,12}"
    r"(?:怎么说|说了什么|说过什么|说的是什么)"
    r"|后来.{0,10}(?:怎样|怎么样|怎么了|如何)|那件事.{0,8}(?:怎样|怎么样)"
)
_VERBATIM = re.compile(r"原话|原文(?!字)|怎么说|说了什么|说过什么|说的是什么")
_FOLLOWUP = re.compile(r"(?:后来|现在|现在的|目前|最终|最后).{0,10}(?:怎样|怎么|如何|什么|哪|更正)")
_OTHER_SPEAKER = re.compile(r"(?:你|Ling)(?:当时|上次|之前|最早|最初|一开始){0,2}(?:的?原话|怎么说|说[的了过])")
_SELF_PREFERENCE = re.compile(r"我(?:现在|目前|最后|以前|过去|曾经|原来|确认|最|更)*喜欢")
_PREFERENCE_QUESTION = re.compile(
    r"喜欢(?:的?是)?(?:什么|哪(?:一)?[个条本部位首家种份套餐座只道杯朵])"
    r"(?P<category>[^？?。；;，,]{0,8})[？?。]?\s*$"
)


def asks_for_originals(text: str) -> bool:
    if _ORIGINALS.search(text) or (_QUOTE_WORD.search(text) and _QUOTE_REQUEST.search(text)):
        return True
    return any(_QUOTE_QUESTION.match(text, match.end()) for match in _QUOTE_WORD.finditer(text))


def asks_for_earliest(text: str) -> bool:
    return any(word in text for word in ("最早", "最初", "第一次", "一开始"))


def preference_revision(text: str) -> bool:
    """Require a correction or an explicit update, not an ordinary additional like."""
    return bool(
        _PREFERENCE_REVISION.match(text.strip())
        or (_REPAIR.match(text.strip()) and "喜欢" in text)
        or re.search(r"(?:改成|现在更)喜欢", text)
    )


def shares_topic(left: str, right: str) -> bool:
    tokens = _topic_tokens(left) & _topic_tokens(right)
    # A single two-character Chinese token can easily be incidental.
    return len(tokens) >= 2 or any(len(token) >= 4 for token in tokens)


def active_message() -> ColumnElement[bool]:
    return and_(
        Message.retracted_at.is_(None), Message.memory_hidden_at.is_(None),
        Message.recalled_at.is_(None), Message.state == "completed", Message.text != "",
    )


def user_in(window: Window, subject_id: str) -> ColumnElement[bool]:
    return and_(
        Message.role == "user",
        or_(
            Message.sender_subject_id == subject_id,
            and_(
                literal(window.visibility in PRIVATE_VISIBILITIES and window.subject_id == subject_id),
                Message.sender_subject_id.is_(None),
            ),
        ),
    )


def actor_delivered(window: Window) -> ColumnElement[bool]:
    return and_(
        Message.role == "actor",
        or_(
            and_(
                literal(window.channel in {"web", "", None}), Message.output_modality == "text",
            ),
            and_(Message.output_modality == "voice", Message.speech_delivery_state == "delivered"),
            Message.delivery_confirmed_at.is_not(None),
        ),
    )


async def preceding_message(
    session: AsyncSession,
    *,
    window: Window,
    subject_id: str,
    source: Message,
) -> Message | None:
    """Link only earlier, related statements in the same conversation."""
    if should_skip_automatic_recall(source.text) or opens_new_topic(source.text):
        return None
    preference = preference_revision(source.text)
    correction = _REPAIR.match(source.text.strip()) is not None
    since = source.created_at - (timedelta(days=3) if preference or correction else timedelta(minutes=45))
    rows = await session.scalars(
        select(Message).where(
            Message.window_id == window.id, user_in(window, subject_id), active_message(),
            Message.sequence < source.sequence, Message.created_at >= since,
        ).order_by(Message.sequence.desc()).limit(12)
    )
    for previous in rows:
        if opens_new_topic(previous.text):
            break
        if should_skip_automatic_recall(previous.text):
            continue
        if preference and re.search(r"我(?:其实|真的|很|更|以前|曾经)?喜欢", previous.text):
            return previous
        if needs_recent_topic(source.text) or shares_topic(source.text, previous.text):
            return previous
    return None


async def pin_message(
    session: AsyncSession,
    *,
    subject_id: str,
    window: Window,
    message_id: str,
    service: MemoryService,
) -> str | None:
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    if window.archived or await has_open_purge(session, subject_id=subject_id):
        return None
    private = window.visibility in PRIVATE_VISIBILITIES and window.subject_id == subject_id
    if not private and window.visibility != "group_scope":
        return None
    eligibility = user_in(window, subject_id)
    if private:
        eligibility = or_(eligibility, actor_delivered(window))
    message = await session.scalar(select(Message).where(
        Message.id == message_id, Message.window_id == window.id, eligibility, active_message(),
    ).with_for_update())
    if message is None:
        return None
    existing = await session.scalar(select(MemoryRow.id).where(
        MemoryRow.subject_id == subject_id, MemoryRow.source_message_id == message.id,
        MemoryRow.slot == PINNED_SLOT, MemoryRow.retracted.is_(False),
        MemoryRow.archived_at.is_(None),
    ).limit(1))
    if existing:
        return existing
    origin = Origin.user_statement if message.role == "user" else Origin.external_observation
    visibility = Visibility(window.visibility)
    return await service.propose(
        MemoryProposal(
            proposal_id=f"pin:{message.id}", kind="episode", text=message.text[:4000],
            origin=origin, source_event_ids=[f"message:{message.id}"], visibility=visibility,
            confidence=1.0,
        ),
        subject_id=subject_id, source_window_id=window.id, source_visibility=window.visibility,
        source_message_id=message.id, slot=PINNED_SLOT, occurred_at=message.created_at,
    )


async def related_evidence(
    session: AsyncSession,
    *,
    subject_id: str,
    window: Window,
    anchor: Message,
    cutoff: datetime,
    group_source_ids: frozenset[str] = frozenset(),
) -> tuple[list[Message], list[CommitmentRow]]:
    """Walk bounded episode links, then load independently verified replies and commitments."""
    if window.visibility == "group_scope":
        if anchor.id not in group_source_ids or anchor.sender_subject_id != subject_id:
            return [], []
    elif window.visibility not in PRIVATE_VISIBILITIES or window.subject_id != subject_id:
        return [], []
    if anchor.window_id != window.id or not anchor.text or anchor.created_at < cutoff:
        return [], []
    ids = {anchor.id}
    if anchor.role == "user":
        for _ in range(MAX_SOURCE_TURNS - 1):
            changed = False
            links = (await session.execute(
                select(MemoryEpisode.source_message_id, MemoryEpisode.previous_message_id)
                .join(Message, Message.id == MemoryEpisode.source_message_id)
                .where(
                    MemoryEpisode.subject_id == subject_id, MemoryEpisode.window_id == window.id,
                    MemoryEpisode.source_visibility == window.visibility,
                    MemoryEpisode.archived_at.is_(None), MemoryEpisode.occurred_at >= cutoff,
                    Message.window_id == window.id, active_message(), user_in(window, subject_id),
                    or_(
                        MemoryEpisode.source_message_id.in_(ids),
                        MemoryEpisode.previous_message_id.in_(ids),
                    ),
                ).order_by(MemoryEpisode.occurred_at, MemoryEpisode.id).limit(MAX_SOURCE_TURNS)
            )).all()
            for current, previous in links:
                for candidate in (current, previous):
                    if candidate and candidate not in ids and len(ids) < MAX_SOURCE_TURNS:
                        ids.add(candidate)
                        changed = True
            if not changed:
                break
    evidence = select(Message).where(
        Message.window_id == window.id, Message.id.in_(ids), active_message(),
        Message.created_at >= cutoff, or_(user_in(window, subject_id), actor_delivered(window)),
    )
    if window.visibility == "group_scope":
        evidence = evidence.where(Message.id.in_(group_source_ids))
    rows = list(await session.scalars(evidence.order_by(Message.sequence)))
    turns = {row.turn_id for row in rows if row.turn_id and row.role == "user"}
    if not turns or window.visibility == "group_scope":
        return rows, []
    replies = await session.scalars(
        select(Message).join(Turn, Turn.id == Message.turn_id).where(
            Message.window_id == window.id, Message.turn_id.in_(turns), active_message(),
            actor_delivered(window), Message.attempt == Turn.attempt, Turn.state == "completed",
        ).order_by(Message.sequence).limit(MAX_SOURCE_TURNS)
    )
    commitments = list(await session.scalars(select(CommitmentRow).where(
        CommitmentRow.actor_id == window.actor_id,
        CommitmentRow.subject_id == subject_id, CommitmentRow.window_id == window.id,
        or_(*(CommitmentRow.source_event_ids[0].as_string() == f"turn:{turn}" for turn in turns)),
    ).order_by(CommitmentRow.due_at, CommitmentRow.id).limit(MAX_SOURCE_TURNS)))
    return sorted([*rows, *replies], key=lambda row: row.sequence), commitments


def evidence_items(rows: list[Message], commitments: list[CommitmentRow]) -> list[dict[str, str]]:
    items = [{
        "message_id": row.id, "window_id": row.window_id, "role": row.role,
        "text": row.text[:MAX_ORIGINAL_CHARS], "created_at": row.created_at.isoformat(),
    } for row in rows]
    items.extend({
        "commitment_id": row.id, "role": "commitment", "text": row.text[:160],
        "state": row.state, "created_at": row.due_at.isoformat(),
    } for row in commitments)
    return items


@dataclass(frozen=True)
class OriginalSearch:
    state: Literal["hit", "not_extracted", "not_visible", "no_match"]
    items: list[dict[str, str]]
    first_message_id: str | None = None


def needs_direct_original(result: OriginalSearch, question: str) -> bool:
    """Reserve an evidence-only reply for a single, explicit question about wording."""
    return bool(
        not question.lstrip().startswith(("/", "／"))
        and asks_for_originals(question)
        and _VERBATIM.search(question)
        and not _FOLLOWUP.search(question)
        and not _OTHER_SPEAKER.search(question)
        and (result.first_message_id or (
            result.state == "no_match" and (_QUOTE_WORD.search(question) or asks_for_earliest(question))
        ))
    )


def original_answer(text: str | None) -> str:
    if text is None:
        return "我没找到能核对的那句原话。你还记得当时提过哪个词吗？"
    if len(text) > MAX_ORIGINAL_CHARS:
        return f"我查到最早那句的开头是「{text[:MAX_ORIGINAL_CHARS]}…」"
    return f"我查到你最早说的是「{text}」"


def recall_topic(text: str) -> str:
    return re.sub(
        r"最早|最初|第一次|一开始|原话|原文|之前|上次|后来|哪次|说了什么|怎么说的?|"
        r"上周|周[一二三四五六日天]|前天|昨天|什么|怎样|那件事|"
        r"还记得|记不记得|请找一下|找一下|想知道|我们|我|你|说过|提过|提到|聊过|关于|的",
        " ", text,
    ).strip(" ？，。?!：:")


def fact_query(text: str) -> tuple[str, Literal["current", "past"]] | None:
    if asks_for_originals(text):
        return None
    if _SELF_PREFERENCE.search(text) and ("什么" in text or "哪" in text):
        if re.search(r"最后|现在|目前|确认", text):
            return "likes", "current"
        if re.search(r"以前|过去|曾经|原来", text):
            return "likes", "past"
    return None


async def search_facts(
    session: AsyncSession,
    *, window: Window, subject_id: str, slot: str, mode: Literal["current", "past"],
    retention_days: int, group_source_ids: frozenset[str] = frozenset(),
    at: datetime | None = None,
) -> OriginalSearch:
    now = utcnow()
    if (
        window.archived or await has_open_purge(session, subject_id=subject_id)
        or (window.visibility == "group_scope" and not group_source_ids)
        or (window.visibility != "group_scope"
            and (window.visibility not in PRIVATE_VISIBILITIES or window.subject_id != subject_id))
    ):
        return OriginalSearch("not_visible", [])
    query = select(MemoryRow, Message).join(Message, Message.id == MemoryRow.source_message_id).where(
        MemoryRow.subject_id == subject_id, MemoryRow.slot == slot, MemoryRow.kind == "assertion",
        MemoryRow.origin == Origin.user_statement.value,
        MemoryRow.visibility == window.visibility, MemoryRow.source_visibility == window.visibility,
        MemoryRow.source_window_id == window.id, Message.window_id == window.id, user_in(window, subject_id),
        MemoryRow.retracted.is_(False), MemoryRow.archived_at.is_(None), active_message(),
        MemoryRow.created_at >= now - timedelta(days=retention_days),
        MemoryRow.created_at >= now - func.make_interval(0, 0, 0, MemoryRow.retention_days),
    )
    if window.visibility == "group_scope":
        query = query.where(Message.id.in_(group_source_ids))
    if mode == "past" and at is None:
        query = query.where(MemoryRow.valid_until <= now)
    else:
        when = at if mode == "past" and at is not None else now
        query = query.where(
            MemoryRow.valid_from <= when,
            or_(MemoryRow.valid_until.is_(None), MemoryRow.valid_until > when),
        )
    items = [{
        "memory_id": row.id, "message_id": source.id, "window_id": window.id,
        "role": "past_fact" if mode == "past" else "current_fact",
        "text": row.text[:200], "value": row.value or "", "created_at": source.created_at.isoformat(),
    } for row, source in await session.execute(
        query.order_by(Message.created_at.desc(), Message.id).limit(MAX_SOURCE_TURNS)
    )]
    return OriginalSearch("hit" if items else "no_match", items)


def single_preference_answer(result: OriginalSearch, question: str) -> str | None:
    if len(result.items) != 1 or not (value := result.items[0].get("value")):
        return None
    match = _PREFERENCE_QUESTION.search(question)
    if match is None or match["category"].strip().rstrip("吗呢") not in value:
        return None
    if result.items[0]["role"] == "current_fact":
        return f"你现在喜欢的是{value}。"
    if result.items[0]["role"] == "past_fact":
        return f"我查到你以前喜欢的是{value}。"
    return None


def render_experience(result: OriginalSearch) -> str:
    if not result.items:
        return (
            "没有找到这项问题的可用证据。对未确认的细节保留判断。"
            "请告诉对方你暂时没找到能核对的记录，可以请对方补充一条线索。其他话题的记录不能作为这件事的原话。"
        )
    labels = {
        "user": "用户原话", "actor": "Ling 已发出的回复", "commitment": "承诺",
        "past_fact": "曾经有效的本人陈述", "current_fact": "目前有效的本人陈述",
    }
    lines = []
    for item in result.items:
        first = item.get("message_id") == result.first_message_id and result.first_message_id is not None
        label = "查到的最早本人原话" if first else labels[item["role"]]
        state = f"（状态 {item['state']}）" if "state" in item else ""
        lines.append(f"- {item['created_at']} {label}{state}：{item['text']}")
    if result.first_message_id is not None:
        lines.insert(0, "询问最初怎么说时，逐字引用标为「查到的最早本人原话」的那一条。以后补充或更正的话单独说明。")
    return "\n".join(lines)


async def search_originals(
    session: AsyncSession,
    *,
    window: Window,
    subject_id: str,
    query: str,
    retention_days: int,
    group_source_ids: frozenset[str] = frozenset(),
    since: datetime | None = None,
    before: datetime | None = None,
    earliest: bool = False,
    current_turn_id: str | None = None,
    now: datetime | None = None,
) -> OriginalSearch:
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days)
    if (
        window.archived or await has_open_purge(session, subject_id=subject_id)
        or (window.visibility == "group_scope" and not group_source_ids)
        or (window.visibility != "group_scope"
            and (window.visibility not in PRIVATE_VISIBILITIES or window.subject_id != subject_id))
    ):
        return OriginalSearch("not_visible", [])
    base = select(Message).where(
        Message.window_id == window.id, user_in(window, subject_id), active_message(),
        Message.created_at >= max(cutoff, since) if since else Message.created_at >= cutoff,
        Message.created_at < (before or now),
    )
    if window.visibility == "group_scope":
        base = base.where(Message.id.in_(group_source_ids))
    if current_turn_id is not None:
        base = base.where(Message.turn_id.is_distinct_from(current_turn_id))
    terms = sorted(_topic_tokens(recall_topic(query)), key=lambda term: (-len(term), term))[:12]
    rank: ColumnElement[int] = literal(0)
    if terms:
        matches = [Message.text.icontains(term, autoescape=True) for term in terms]
        for match in matches:
            rank += case((match, 1), else_=0)
        base = base.where(rank >= (len(terms) + 2) // 3)
    indexed = base.join(MemoryEpisode, MemoryEpisode.source_message_id == Message.id).where(
        MemoryEpisode.subject_id == subject_id, MemoryEpisode.window_id == window.id,
        MemoryEpisode.source_visibility == window.visibility,
        MemoryEpisode.archived_at.is_(None),
    )
    ordered = Message.sequence.asc() if earliest else Message.sequence.desc()
    ordering = (ordered, rank.desc()) if earliest else (rank.desc(), ordered)
    candidates = await session.scalars(indexed.order_by(*ordering).limit(1))
    anchor = candidates.first()
    if anchor is None:
        anchor = await session.scalar(base.order_by(*ordering).limit(1))
        if anchor is None:
            return OriginalSearch("no_match", [])
        rows, commitments = await related_evidence(
            session, subject_id=subject_id, window=window, anchor=anchor, cutoff=cutoff,
            group_source_ids=group_source_ids,
        )
        if not commitments:
            return OriginalSearch("not_extracted", [])
        return OriginalSearch("hit", evidence_items(rows, commitments), anchor.id if earliest else None)
    rows, commitments = await related_evidence(
        session, subject_id=subject_id, window=window, anchor=anchor, cutoff=cutoff,
        group_source_ids=group_source_ids,
    )
    return OriginalSearch("hit", evidence_items(rows, commitments), anchor.id if earliest else None)
