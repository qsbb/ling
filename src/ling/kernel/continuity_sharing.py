"""Connect simulated activity, conversation evidence, and proactive delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService
from ling.domains.evidence import EvidenceKind, activity_evidence_kind
from ling.domains.identity.service import IdentityService
from ling.domains.memory.experience import active_message
from ling.domains.memory.purge import has_open_purge
from ling.domains.proactive.binding import target_fingerprint
from ling.domains.proactive.service import ShareDecision
from ling.domains.proactive.sources import REASON_ACTIVITY_DONE, REASON_COMMITMENT_DUE, Candidate
from ling.domains.relationship.preferences import blocks_topic, preferences, share_pause
from ling.infra.models import (
    ActivityRow,
    DeliveryReceiptRow,
    MemoryEpisode,
    MemoryRow,
    Message,
    Operation,
    ProactiveOpportunity,
    RelationshipState,
    SpeakLeaseRow,
    Window,
    WorldPlace,
    utcnow,
)
from ling.kernel.audience import AudienceSnapshot
from ling.settings.service import published_memory_policy

SHARE_LIFETIME = timedelta(hours=16)
ACTIVE_CONVERSATION = timedelta(minutes=8)
_PLACE_ACTIVITY = re.compile(r"散步|走走|经过|路过|去一趟|逛|街景|外出|^walking$|^outing$")
_REFERENTIAL_PLACE = re.compile(r"(?:这|那)(?:条街|个地方|一带|里)")


@dataclass(frozen=True)
class ShareEvidence:
    activity: ActivityRow
    place: WorldPlace
    memory: MemoryRow
    source: Message
    window: Window


async def _source(
    session: AsyncSession, *, actor_id: str, now: datetime, retention: int,
    activity_id: str | None = None, memory_id: str | None = None, subject_id: str | None = None,
    window_id: str | None = None,
) -> list[ShareEvidence]:
    # The source itself supplies the private target. Session order or a similar
    # display name must never pick a different recipient.
    from sqlalchemy.orm import aliased

    previous = aliased(Message)
    title = ActivityRow.simulation_action
    query = (
        select(ActivityRow, WorldPlace, MemoryRow, Message, Window)
        .select_from(ActivityRow)
        .join(WorldPlace, WorldPlace.id == ActivityRow.place_id)
        .join(
            MemoryRow, (MemoryRow.slot == "likes") & (MemoryRow.kind == "assertion"),
        )
        .join(Message, Message.id == MemoryRow.source_message_id)
        .join(Window, Window.id == MemoryRow.source_window_id)
        .outerjoin(MemoryEpisode, MemoryEpisode.source_message_id == Message.id)
        .outerjoin(previous, previous.id == MemoryEpisode.previous_message_id)
        .where(
            ActivityRow.actor_id == actor_id, WorldPlace.actor_id == actor_id,
            ActivityRow.origin == "simulated", ActivityRow.state == "completed",
            ActivityRow.completion_evidence.startswith("kind=simulated_outcome;"),
            ActivityRow.simulation_observed_at.is_not(None),
            ActivityRow.ended_at <= now,
            ActivityRow.visibility.in_(("subject_private", "public")),
            or_(
                ActivityRow.kind.op("~")(_PLACE_ACTIVITY.pattern),
                title.op("~")(_PLACE_ACTIVITY.pattern),
                (func.strpos(title, WorldPlace.name) > 0)
                & title.op("~")("去|看见|见到|到达|取|走访|拜访"),
            ),
            Window.actor_id == actor_id, Window.subject_id == MemoryRow.subject_id,
            Window.visibility == "subject_private", Window.archived.is_(False),
            Window.channel.in_(("web", "onebot", "qqbot")),
            MemoryRow.visibility == "subject_private", MemoryRow.source_visibility == "subject_private",
            MemoryRow.retracted.is_(False), MemoryRow.archived_at.is_(None),
            MemoryRow.valid_from <= ActivityRow.ended_at,
            or_(MemoryRow.valid_until.is_(None), MemoryRow.valid_until > now),
            Message.created_at >= now - timedelta(days=retention),
            Message.created_at >= now - func.make_interval(0, 0, 0, MemoryRow.retention_days),
            Message.window_id == Window.id, Message.role == "user", Message.sender_subject_id == Window.subject_id,
            active_message(), func.length(WorldPlace.name) >= 2,
            or_(
                func.strpos(func.lower(MemoryRow.value), func.lower(WorldPlace.name)) > 0,
                (MemoryRow.value.op("~")(_REFERENTIAL_PLACE.pattern))
                & (previous.window_id == Window.id) & (previous.sender_subject_id == Window.subject_id)
                & (previous.role == "user") & previous.retracted_at.is_(None)
                & previous.memory_hidden_at.is_(None)
                & (func.strpos(func.lower(previous.text), func.lower(WorldPlace.name)) > 0),
            ),
        )
    )
    if activity_id is None:
        query = query.where(ActivityRow.ended_at > now - SHARE_LIFETIME)
    else:
        query = query.where(ActivityRow.id == activity_id)
    if memory_id is not None:
        query = query.where(MemoryRow.id == memory_id)
    if subject_id is not None:
        query = query.where(MemoryRow.subject_id == subject_id)
    if window_id is not None:
        query = query.where(Window.id == window_id)
    rows = await session.execute(query.order_by(
        ActivityRow.ended_at.desc(), MemoryRow.valid_from.desc(), MemoryRow.id,
    ).limit(80))
    return [
        ShareEvidence(activity, place, memory, source, window)
        for activity, place, memory, source, window in rows
        if activity_evidence_kind(activity) is EvidenceKind.simulated_outcome
    ]


async def _owns_target(session: AsyncSession, *, window: Window, subject_id: str) -> bool:
    if window.channel == "web":
        return window.subject_id == subject_id
    return await IdentityService(session).owns_private_window(window, subject_id)


async def activity_candidates(session: AsyncSession, *, actor_id: str, now: datetime) -> list[Candidate]:
    policy = await published_memory_policy(session)
    if policy.max_injected_memories <= 0:
        return []
    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for item in await _source(session, actor_id=actor_id, now=now, retention=policy.max_retention_days):
        activity, place, memory, window = item.activity, item.place, item.memory, item.window
        key = (activity.id, memory.subject_id)
        if key in seen or activity.ended_at is None:
            continue
        if not await _owns_target(session, window=window, subject_id=memory.subject_id):
            continue
        grant = await ConsentService(session).get(memory.subject_id, "proactive_contact")
        if not grant.granted or (grant.valid_until is not None and grant.valid_until <= now):
            continue
        audience = await AudienceSnapshot.freeze(
            session, subject_id=memory.subject_id, window_id=window.id, window_visibility=window.visibility,
        )
        if not audience.allows_long_term_memory() or await has_open_purge(session, subject_id=memory.subject_id):
            continue
        seen.add(key)
        candidates.append(Candidate(
            subject_id=memory.subject_id, reason_code=REASON_ACTIVITY_DONE, source_event_id=f"activity:{activity.id}",
            reason="活动地点与此前的对话有关", not_before=activity.ended_at,
            best_until=activity.ended_at + SHARE_LIFETIME, related_memory_id=memory.id,
            target_window_id=window.id,
            source_consent_revision=grant.revision, source_target_fingerprint=target_fingerprint(window),
            draft_text=f"刚才在{place.name}附近，想起我们之前聊过这里。",
        ))
    return candidates


async def share_evidence(
    session: AsyncSession, row: ProactiveOpportunity, *, now: datetime,
) -> ShareEvidence | None:
    activity_id = (row.source_event_id or "").removeprefix("activity:")
    if not (activity_id and row.related_memory_id and row.target_window_id):
        return None
    if await has_open_purge(session, subject_id=row.subject_id):
        return None
    grant = await ConsentService(session).get(row.subject_id, "proactive_contact")
    if (not grant.granted or grant.revision != row.source_consent_revision
            or (grant.valid_until is not None and grant.valid_until <= now)):
        return None
    policy = await published_memory_policy(session)
    if policy.max_injected_memories <= 0:
        return None
    items = await _source(
        session, actor_id=row.actor_id, now=now, retention=policy.max_retention_days,
        activity_id=activity_id, memory_id=row.related_memory_id, subject_id=row.subject_id,
        window_id=row.target_window_id,
    )
    if not items:
        return None
    item = items[0]
    audience = await AudienceSnapshot.freeze(
        session, subject_id=row.subject_id, window_id=item.window.id, window_visibility=item.window.visibility,
    )
    if (
        row.source_target_fingerprint != target_fingerprint(item.window) or not audience.allows_long_term_memory()
        or not await _owns_target(
        session, window=item.window, subject_id=row.subject_id,
        )
    ):
        return None
    return item


async def share_decision(
    session: AsyncSession, row: ProactiveOpportunity, *, now: datetime, in_conversation: bool = False,
) -> ShareDecision | None:
    if row.reason_code == REASON_COMMITMENT_DUE or row.kind in {"commitment_due", "user_asked_later"}:
        return None
    if (await share_pause(session, row.subject_id, now=now)).paused:
        return ShareDecision("用户已暂停主动分享")
    rules = await preferences(
        session, actor_id=row.actor_id, subject_id=row.subject_id, scope="private", active_only=True,
    )
    boundaries = [rule.text for rule in rules if rule.kind == "boundary"]
    if legacy := await session.scalar(select(RelationshipState.boundaries).where(
        RelationshipState.actor_id == row.actor_id, RelationshipState.subject_id == row.subject_id,
    )):
        boundaries += [label for label in legacy if isinstance(label, str)]
    topic = f"{row.reason} {row.draft_text or ''}"
    if row.reason_code != REASON_ACTIVITY_DONE:
        return ShareDecision("用户已设定话题边界", True) if blocks_topic(topic, boundaries) else None
    grant = await ConsentService(session).get(row.subject_id, "proactive_contact")
    if (not grant.granted or grant.revision != row.source_consent_revision
            or (grant.valid_until is not None and grant.valid_until <= now)):
        return ShareDecision("主动联系授权已失效", True)
    item = await share_evidence(session, row, now=now)
    if item is None:
        return ShareDecision("共同经历已不可用", True)
    if blocks_topic(f"{topic} {item.place.name} {item.source.text}", boundaries):
        return ShareDecision("用户已设定话题边界", True)
    if in_conversation:
        return None
    recent = await session.scalar(select(Message.created_at).join(Window, Window.id == Message.window_id).where(
        Window.actor_id == row.actor_id, Window.subject_id == row.subject_id,
        Window.visibility == "subject_private", Message.role == "user",
        Message.created_at > now - ACTIVE_CONVERSATION, Message.retracted_at.is_(None),
    ).order_by(Message.created_at.desc()).limit(1))
    busy = await session.scalar(select(SpeakLeaseRow.window_id).join(Window, Window.id == SpeakLeaseRow.window_id).where(
        SpeakLeaseRow.actor_id == row.actor_id, Window.subject_id == row.subject_id, SpeakLeaseRow.expires_at > now,
    ).limit(1))
    if recent or busy:
        return ShareDecision("用户正在对话，稍后复核")
    last = await session.scalar(select(ProactiveOpportunity).where(
        ProactiveOpportunity.actor_id == row.actor_id, ProactiveOpportunity.subject_id == row.subject_id,
        ProactiveOpportunity.id != row.id, ProactiveOpportunity.reason_code == REASON_ACTIVITY_DONE,
        ProactiveOpportunity.state == "sent", ProactiveOpportunity.sent_at > now - timedelta(hours=20),
    ).order_by(ProactiveOpportunity.sent_at.desc()).limit(1))
    if last is not None and last.acknowledged_message_id is None:
        return ShareDecision("上次分享尚无回应，稍后复核")
    return None


async def _receipt(
    session: AsyncSession, row: ProactiveOpportunity, *, window: Window,
) -> DeliveryReceiptRow | None:
    if row.state != "sent" or not row.delivery_operation_id:
        return None
    return await session.scalar(select(DeliveryReceiptRow).join(
        Operation, Operation.id == DeliveryReceiptRow.operation_id,
    ).where(
        Operation.id == row.delivery_operation_id, Operation.kind == "delivery",
        Operation.actor_id == row.actor_id, Operation.subject_id == row.subject_id,
        Operation.payload["source"].as_string() == "proactive",
        Operation.payload["opportunity_id"].as_string() == row.id,
        Operation.payload["target_window_id"].as_string() == window.id,
        DeliveryReceiptRow.channel == window.channel, DeliveryReceiptRow.state.in_(("sent", "delivered")),
    ).order_by(DeliveryReceiptRow.observed_at.desc()).limit(1))


async def _matching_shares(
    session: AsyncSession, *, window: Window, subject_id: str, text: str, now: datetime,
    allow_quote: bool = False,
) -> list[tuple[ProactiveOpportunity, ShareEvidence]]:
    if window.visibility != "subject_private" or window.subject_id != subject_id or window.archived:
        return []
    rows = await session.scalars(select(ProactiveOpportunity).where(
        ProactiveOpportunity.actor_id == window.actor_id, ProactiveOpportunity.subject_id == subject_id,
        ProactiveOpportunity.target_window_id == window.id,
        ProactiveOpportunity.reason_code == REASON_ACTIVITY_DONE,
        ProactiveOpportunity.created_at >= now - timedelta(days=14),
    ).order_by(ProactiveOpportunity.created_at.desc(), ProactiveOpportunity.id).limit(8))
    result = []
    for row in rows:
        item = await share_evidence(session, row, now=now)
        if item is not None and (allow_quote or item.place.name in text):
            result.append((row, item))
    return result


async def share_context(
    session: AsyncSession, *, window: Window, subject_id: str, text: str,
) -> str:
    now = utcnow()
    matches = await _matching_shares(session, window=window, subject_id=subject_id, text=text, now=now)
    for row, item in matches:
        if await share_decision(session, row, now=now, in_conversation=True) is not None:
            continue
        web_saved = row.state == "sent" and window.channel == "web" and row.rejected_reason == "web"
        if web_saved or await _receipt(session, row, window=window) is not None:
            acknowledgement = None
            if row.acknowledged_message_id:
                acknowledgement = await session.scalar(select(Message.id).where(
                    Message.id == row.acknowledged_message_id, Message.window_id == window.id,
                    Message.sender_subject_id == subject_id, Message.role == "user", active_message(),
                ))
            status = {
                "received": "对方说已收到",
                "misunderstood": "对方描述了与分享理由不同的理解",
                "agreed": "对方明确认可了本次分享",
            }.get(row.acknowledgement or "", "对方是否阅读、理解或认可仍未知") if acknowledgement else (
                "对方是否阅读、理解或认可仍未知"
            )
            return f"关于{item.place.name}的分享已确认发出。{status}。"
        if row.state in {"failed", "uncertain"}:
            status = "未送达" if row.state == "failed" else "投递结果未知"
            return f"稍早想分享{item.place.name}，{status}。避免把尝试当作已经发过。"
        if row.state == "candidate" and row.expires_at > now:
            return (
                f"刚在模拟活动中经过{item.place.name}，当时想把这个经历说给对方。"
                "可按眼前话题自然带一句，或者继续聊眼前的事。"
            )
    return ""


async def acknowledge_share(
    session: AsyncSession, *, window: Window, subject_id: str, source: Message, quoted_platform_id: str | None,
) -> None:
    raw = source.text.strip()
    if re.search(r"我(?:之前|刚才)?(?:理解错了|误会了|以为)", raw):
        level = "misunderstood"
    elif re.search(r"我认同|我同意|我明白你的意思|我懂你为什么", raw):
        level = "agreed"
    elif re.search(r"(?:收|看)到(?:了|你发)|我看过了", raw):
        level = "received"
    else:
        return
    now = utcnow()
    matches = await _matching_shares(
        session, window=window, subject_id=subject_id, text=raw, now=now,
        allow_quote=quoted_platform_id is not None,
    )
    confirmed = []
    for row, _item in matches:
        if row.state != "sent" or not row.sent_at or source.created_at < row.sent_at:
            continue
        receipt = await _receipt(session, row, window=window)
        if quoted_platform_id is not None:
            if receipt is None or receipt.platform_message_id != quoted_platform_id:
                continue
        elif receipt is None and not (window.channel == "web" and row.rejected_reason == "web"):
            continue
        confirmed.append(row.id)
    if len(confirmed) == 1:
        await session.execute(update(ProactiveOpportunity).where(
            ProactiveOpportunity.id == confirmed[0], ProactiveOpportunity.state == "sent",
        ).values(acknowledged_message_id=source.id, acknowledgement=level))


async def note_natural_continuation(
    session: AsyncSession, *, window: Window, subject_id: str, turn_id: str, reply: str,
) -> None:
    if window.visibility != "subject_private" or not re.search(r"想起|刚才|刚在|刚走|刚路过", reply):
        return
    latest = await session.scalar(select(Message.text).where(
        Message.window_id == window.id, Message.turn_id == turn_id, Message.role == "user", active_message(),
    ).order_by(Message.sequence.desc()).limit(1))
    if not latest:
        return
    now = utcnow()
    for row, item in await _matching_shares(
        session, window=window, subject_id=subject_id, text=latest, now=now,
    ):
        if row.state != "candidate" or row.expires_at <= now or item.place.name not in reply:
            continue
        if await share_decision(session, row, now=now, in_conversation=True) is None:
            await session.execute(update(ProactiveOpportunity).where(
                ProactiveOpportunity.id == row.id, ProactiveOpportunity.state == "candidate",
            ).values(state="continued", rejected_reason="已在对话中分享"))
