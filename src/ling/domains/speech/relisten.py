"""Resolve the audio behind an explicit OneBot private-chat reply."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import PLATFORM_MESSAGE_ID_SCOPE
from ling.domains.media.service import (
    INBOUND_SCOPE,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.infra.asr_jobs import AsrJob
from ling.infra.models import IdempotencyKey, MediaEvidenceRow, Message

_LISTEN_REQUEST = re.compile(
    r"(?:请|麻烦)?(?:帮我|能不能|可以)?"
    r"(?:听(?:一下|一听|听)?这[条段](?:语音)?"
    r"|这[条段]语音(?:里)?(?:说了什么|说什么|讲了什么)"
    r"|(?:转写|识别)(?:一下)?这[条段](?:语音)?)(?:吗|吧)?"
)
_ONEBOT_ID = re.compile(r"-?[0-9]{1,20}")


class QuotedVoiceSource(TypedDict):
    event_id: str
    platform_id: str
    recall_key: str
    recall_scopes: list[str]


@dataclass(frozen=True)
class QuotedVoice:
    items: list[dict[str, str]]
    text: str = ""
    note: str | None = None


def wants_quoted_voice(text: str) -> bool:
    return _LISTEN_REQUEST.fullmatch(text.strip().rstrip("。？！?!").strip()) is not None


def make_quote_source(
    event_id: str, platform_id: str, namespace: str, *, recall_scopes: list[str],
) -> QuotedVoiceSource | None:
    self_id, sep, current_id = event_id.partition(":")
    if not sep or not all(_ONEBOT_ID.fullmatch(value) for value in (self_id, current_id, platform_id)):
        return None
    return {
        "event_id": f"{self_id}:{platform_id}",
        "platform_id": platform_id,
        "recall_key": f"{namespace}:recall:{platform_id}",
        "recall_scopes": recall_scopes,
    }


async def quote_was_retracted(
    session: AsyncSession, source: QuotedVoiceSource, *, window_id: str,
) -> bool:
    # A recall event also exists when the original audio had no text message.
    if await session.scalar(select(IdempotencyKey.id).where(
        IdempotencyKey.scope.in_(source["recall_scopes"]),
        IdempotencyKey.key == source["recall_key"],
    ).limit(1)) is not None:
        return True
    return await session.scalar(
        select(Message.id)
        .join(IdempotencyKey, IdempotencyKey.key == Message.id)
        .where(
            Message.window_id == window_id, Message.role == "user",
            Message.retracted_at.is_not(None),
            IdempotencyKey.scope == PLATFORM_MESSAGE_ID_SCOPE,
            IdempotencyKey.result_ref == source["platform_id"],
        ).limit(1)
    ) is not None


async def inspect_quoted_voice(
    session: AsyncSession, source: QuotedVoiceSource, *,
    actor_id: str, window_id: str, subject_id: str, namespace: str,
) -> QuotedVoice | None:
    """Read an existing transcript, or return the exact audio eligible for ASR."""
    evidence_ids = (await session.scalars(
        select(MediaEvidenceRow.id).where(
            MediaEvidenceRow.actor_id == actor_id, MediaEvidenceRow.window_id == window_id,
            MediaEvidenceRow.subject_id == subject_id, MediaEvidenceRow.kind == "audio",
            MediaEvidenceRow.source_event_id == source["event_id"],
        ).order_by(MediaEvidenceRow.created_at, MediaEvidenceRow.id)
    )).all()
    # Match either the original transcription or a previous explicit listen.
    quote_event_id = AsrJob.payload["quoted_voice"]["event_id"].as_string()
    jobs = (await session.execute(
        select(AsrJob.state, AsrJob.text)
        .where(
            AsrJob.actor_id == actor_id, AsrJob.window_id == window_id,
            AsrJob.subject_id == subject_id, AsrJob.mode == "send",
            AsrJob.payload["envelope"]["connector_instance"].as_string() == namespace,
            or_(
                and_(
                    quote_event_id.is_(None),
                    AsrJob.payload["envelope"]["external_event_id"].as_string() == source["event_id"],
                ),
                quote_event_id == source["event_id"],
            ),
            AsrJob.state.in_(("succeeded", "queued", "running")),
        ).order_by(AsrJob.queued_at.desc(), AsrJob.id.desc())
    )).all()
    if not evidence_ids and not jobs:
        return None
    if await quote_was_retracted(session, source, window_id=window_id):
        return QuotedVoice([], note="引用的那条语音已被撤回")
    if not await ConsentService(session).is_active(subject_id, INBOUND_SCOPE):
        return QuotedVoice([], note="需要先开启对入站语音的识别授权")
    for state, text in jobs:
        if state == "succeeded" and text:
            return QuotedVoice([], text=text)
    if any(state in {"queued", "running"} for state, _ in jobs):
        return QuotedVoice([], note="引用的那条语音正在识别")
    media = MediaEvidenceService(session, actor_id=actor_id)
    items: list[dict[str, str]] = []
    for evidence_id in evidence_ids:
        evidence = await media.get(evidence_id, window_id=window_id)
        if evidence is None:
            continue
        try:
            await media.ensure_description_authorized(evidence_id, window_id=window_id)
        except MediaEvidenceError:
            continue
        items.append({"evidence_id": evidence_id, "ref": evidence.media_ref})
    return QuotedVoice(items, note="当前没有可用的引用语音转写")
