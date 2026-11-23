"""Resolve the audio behind an explicit OneBot private-chat reply."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService
from ling.domains.media.service import (
    INBOUND_SCOPE,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.domains.speech.transcription import QuotedVoiceSource, quote_was_retracted
from ling.infra.asr_jobs import AsrJob
from ling.infra.models import MediaEvidenceRow
from ling.ports.model import ChatMessageIn, ModelError

if TYPE_CHECKING:
    from ling.models.router import ModelRouter
    from ling.ports.config import EffectiveConfig

_ONEBOT_ID = re.compile(r"-?[0-9]{1,20}")
VOICE_DECISION_PROMPT = (
    "对方引用了一条尚未转写的语音。根据这次发言，决定是否要实际听这条语音才能接话。"
    "需要内容时选择 listen。只讨论发送、引用状态或者表达与内容无关时选择 respond。"
    '只返回 JSON 对象 {"action":"listen"} 或 {"action":"respond"}。'
)


@dataclass(frozen=True)
class QuotedVoice:
    items: list[dict[str, str]]
    text: str = ""
    note: str | None = None


async def choose_to_listen(router: ModelRouter | None, config: EffectiveConfig, text: str) -> bool:
    """Let the dialogue model choose whether the quoted content is needed."""
    asr = config.resolve_profile("speech.asr")
    if router is None or not asr.usable or not asr.enabled:
        return False
    chunks: list[str] = []
    characters = 0
    try:
        async with asyncio.timeout(8):
            async for chunk, outcome in router.stream(
                config, profile="dialogue.fast", allow_fallback=False,
                messages=[
                    ChatMessageIn(role="system", content=VOICE_DECISION_PROMPT),
                    ChatMessageIn(role="user", content=text[:1000]),
                ],
            ):
                if outcome.error is not None:
                    return False
                if chunk.delta:
                    characters += len(chunk.delta)
                    if characters > 512:
                        return False
                    chunks.append(chunk.delta)
        packed = json.loads("".join(chunks))
    except (ModelError, TimeoutError, ValueError, RecursionError):
        return False
    return isinstance(packed, dict) and packed.get("action") == "listen"


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
