"""Group arrangements grounded in explicitly linked, retained messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin, Visibility
from ling.domains.identity.service import IdentityService
from ling.domains.memory.purge import claim_still_valid, open_purge_subjects
from ling.domains.memory.service import MemoryHit
from ling.infra.models import (
    Message,
    OutboxEvent,
    SharedArrangement,
    SharedArrangementEvidence,
    Subject,
    Window,
    utcnow,
)

_DATE = r"(?:本周|这周|下周|周|星期)[一二三四五六日天](?:上午|下午|晚上)?(?:[0-9一二三四五六七八九十]{1,3}点(?:半)?)?"
_PLACE = r"[\u4e00-\u9fffA-Za-z0-9]{1,16}(?:图书馆|咖啡馆|公园|广场|车站|书店|东门|西门|南门|北门)"
_PROPOSE = re.compile(
    rf"(?:提议|建议|我们)[：: ]?(?P<when>{_DATE})[，, ]?在(?P<place>{_PLACE})(?:见面|集合)[。！!？?]?"
)
_RESCHEDULE = re.compile(
    rf"(?:改到|改为)(?P<when>{_DATE})[，, ]?在(?P<place>{_PLACE})(?:见面|集合)[。！!]?"
)
_CONFIRM = re.compile(r"(?:确认|我确认|好[，,]?就这么定|就这么定)[。！!]?")
_CANCEL = re.compile(r"(?:取消|取消这次约定|这次约定取消)[。！!]?")
_COMPLETE = re.compile(r"(?:我们已经见面了|已经见面了|我们已经到了|已经完成这次约定)[。！!]?")
_QUESTION = re.compile(r"约在?哪|约去哪|安排在?哪|约的(?:时间|地点)|原定|约到|最后(?:去|到)|约好|约定|(?:什么时候|几点)见面")
_ORIGINAL = re.compile(r"最初|原来|原定|第一次|一开始")
_COMPLETION = re.compile(r"最后|去过|去了|到了|完成")


@dataclass(frozen=True)
class Contribution:
    action: str
    when: str | None = None
    place: str | None = None


def read_contribution(text: str, *, linked: bool) -> Contribution | None:
    text = text.strip()
    if not linked:
        match = _PROPOSE.fullmatch(text)
        if match and not text.endswith(("?", "？")):
            return Contribution("proposed", match["when"], match["place"])
        return None
    match = _RESCHEDULE.fullmatch(text)
    if match:
        return Contribution("rescheduled", match["when"], match["place"])
    for pattern, action in (
        (_CONFIRM, "confirmed"), (_CANCEL, "cancelled"), (_COMPLETE, "completed"),
    ):
        if pattern.fullmatch(text):
            return Contribution(action)
    return None


async def record_contribution(
    session: AsyncSession, *, message_id: str, previous_id: str | None, subject_id: str,
    connector_instance: str, account_ref: str, contribution: Contribution,
) -> None:
    await session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
    if not await claim_still_valid(session):
        return
    source = await session.get(Message, message_id, with_for_update=True)
    if (
        source is None or source.role != "user" or source.sender_subject_id != subject_id
        or source.retracted_at is not None or source.memory_hidden_at is not None
    ):
        return
    window = await session.get(Window, source.window_id)
    if window is None or window.archived or window.visibility != "group_scope":
        return
    if await session.get(SharedArrangementEvidence, message_id) is not None:
        return
    # The accepted group sender has a stable platform account even while its
    # optional cross-platform identity proof is pending.
    if await IdentityService(session).resolve_account(
        connector_instance, account_ref, require_proven=False,
    ) != subject_id:
        return
    if await open_purge_subjects(session, subject_ids=[subject_id]):
        return
    arrangement: SharedArrangement | None
    if contribution.action == "proposed":
        arrangement = SharedArrangement(id=message_id, window_id=source.window_id, created_at=source.created_at)
        session.add(arrangement)
        await session.flush()
    else:
        previous = await session.get(SharedArrangementEvidence, previous_id) if previous_id else None
        if previous is None:
            if previous_id and await session.scalar(select(OutboxEvent.id).where(
                OutboxEvent.topic == "memory.shared_arrangement",
                OutboxEvent.state.in_(("pending", "in_flight")),
                OutboxEvent.payload["message_id"].as_string() == previous_id,
            ).limit(1)):
                raise RuntimeError("shared_arrangement_parent_pending")
            return
        if previous.sequence >= source.sequence:
            return
        arrangement = await session.get(SharedArrangement, previous.arrangement_id, with_for_update=True)
        if arrangement is None or arrangement.window_id != source.window_id:
            return
        root = await session.get(SharedArrangementEvidence, arrangement.id)
        if root is None or (contribution.action == "confirmed" and root.subject_id == subject_id):
            return
        if contribution.action != "confirmed" and not await session.scalar(
            select(SharedArrangementEvidence.message_id).where(
                SharedArrangementEvidence.arrangement_id == arrangement.id,
                SharedArrangementEvidence.subject_id == subject_id,
                SharedArrangementEvidence.action.in_(("proposed", "confirmed")),
            ).limit(1)
        ):
            return
    session.add(SharedArrangementEvidence(
        message_id=source.id, arrangement_id=arrangement.id, previous_id=previous_id,
        subject_id=subject_id, connector_instance=connector_instance, account_ref=account_ref,
        action=contribution.action, scheduled_time=contribution.when, place=contribution.place,
        sequence=source.sequence, recorded_at=source.created_at, turn_id=source.turn_id,
    ))
    await session.flush()


async def recall_arrangements(
    session: AsyncSession, *, window_id: str, query: str, whole_group_source_ids: frozenset[str],
    retention_days: int, limit: int, reply_message_id: str | None = None,
) -> list[MemoryHit]:
    if not whole_group_source_ids or not (_QUESTION.search(query) or reply_message_id):
        return []
    cutoff = utcnow() - timedelta(days=retention_days)
    # Load bounded event timelines inside the source group. Do not rank or render
    # any event until every required source passes the whole-audience check.
    roots = select(SharedArrangement.id).where(
        SharedArrangement.window_id == window_id,
        SharedArrangement.created_at > cutoff,
        SharedArrangement.id.in_(whole_group_source_ids),
    ).order_by(SharedArrangement.created_at.desc()).limit(50)
    if reply_message_id is not None:
        roots = roots.where(SharedArrangement.id.in_(select(
            SharedArrangementEvidence.arrangement_id
        ).where(SharedArrangementEvidence.message_id == reply_message_id)))
    records = (await session.execute(
        select(SharedArrangementEvidence, Message).outerjoin(
            Message, Message.id == SharedArrangementEvidence.message_id,
        ).where(SharedArrangementEvidence.arrangement_id.in_(roots))
        .order_by(SharedArrangementEvidence.sequence, SharedArrangementEvidence.message_id)
        .limit(1001)
    )).all()
    # Never project a truncated event stream.
    if len(records) == 1001:
        return []
    timelines: dict[str, list[SharedArrangementEvidence]] = {}
    excluded: set[str] = set()
    identity = IdentityService(session)
    subject_ids = {evidence.subject_id for evidence, _ in records}
    purging = set(await open_purge_subjects(session, subject_ids=list(subject_ids)))
    bindings: dict[tuple[str, str], str | None] = {}
    for evidence, source in records:
        if (
            source is None or evidence.message_id not in whole_group_source_ids
            or source.window_id != window_id or source.sender_subject_id != evidence.subject_id
            or source.retracted_at is not None or source.memory_hidden_at is not None
            or evidence.subject_id in purging
        ):
            excluded.add(evidence.arrangement_id)
            continue
        key = (evidence.connector_instance, evidence.account_ref)
        if key not in bindings:
            bindings[key] = await identity.resolve_account(*key, require_proven=False)
        if bindings[key] != evidence.subject_id:
            excluded.add(evidence.arrangement_id)
            continue
        timelines.setdefault(evidence.arrangement_id, []).append(evidence)

    hits: list[MemoryHit] = []
    for arrangement_id, evidence in timelines.items():
        if arrangement_id in excluded:
            continue
        hit = _project(arrangement_id, evidence, window_id, query)
        if hit is not None:
            hits.append(hit)
    hits.sort(key=lambda hit: (-hit.score, hit.memory_id))
    if len(hits) > 1 and hits[0].score == hits[1].score and reply_message_id is None:
        return [MemoryHit(
            memory_id="arrangement:ambiguous", kind="shared_arrangement",
            text="有多个符合的约定。请先询问对方指的是哪个时间或地点。",
            origin=Origin.external_observation, visibility=Visibility.group_scope,
            confidence=1.0, source_event_ids=[], score=hits[0].score,
            valid_until=None, source_window_id=window_id,
        )]
    return hits[:limit]


def _project(
    arrangement_id: str, evidence: list[SharedArrangementEvidence], window_id: str, query: str,
) -> MemoryHit | None:
    original = evidence[0]
    if original.action != "proposed" or original.message_id != arrangement_id:
        return None
    schedule = original
    state = original
    for item in evidence[1:]:
        if state.action in {"cancelled", "completed"}:
            break
        if state.action == "proposed" and item.action in {"rescheduled", "completed"}:
            continue
        if item.action == "rescheduled":
            schedule = item
        state = item

    if state.action == "proposed" and not _ORIGINAL.search(query):
        text = "只有提议，尚未取得确认。"
        used = [original]
    else:
        text, used = _answer(original, schedule, state, query)
    terms = {item for row in evidence for item in (row.place, row.scheduled_time) if item}
    score = 0.02 + 0.01 * sum(term in query for term in terms)
    return MemoryHit(
        memory_id=arrangement_id, kind="shared_arrangement", text=text,
        origin=Origin.external_observation, visibility=Visibility.group_scope, confidence=1.0,
        source_event_ids=list(dict.fromkeys(f"message:{row.message_id}" for row in [original, *used])),
        score=score, valid_until=None, source_window_id=window_id,
    )


def _answer(
    original: SharedArrangementEvidence, schedule: SharedArrangementEvidence,
    state: SharedArrangementEvidence, query: str,
) -> tuple[str, list[SharedArrangementEvidence]]:
    if _ORIGINAL.search(query):
        text = f"最初提议：{original.scheduled_time}在{original.place}见面。"
        used = [original]
    elif _COMPLETION.search(query):
        outcome = {
            "completed": "当事人报告已经见面。",
            "cancelled": "约定已取消，未取得完成依据。",
        }.get(state.action, "尚无完成证据。")
        text = f"{schedule.scheduled_time}在{schedule.place}见面的约定。{outcome}"
        used = [schedule, state]
    else:
        outcome = {
            "confirmed": "由另一位发言者确认。",
            "rescheduled": "有明确改期记录。",
            "completed": "当事人报告已经见面。",
            "cancelled": "约定已取消。",
        }[state.action]
        text = f"有依据的安排：{schedule.scheduled_time}在{schedule.place}见面。{outcome}"
        used = [schedule, state]
    return text, used
