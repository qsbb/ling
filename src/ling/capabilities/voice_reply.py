"""Shared voice reply preparation, synthesis and receipt-backed delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ling.capabilities.delivery import DeliveryService
from ling.capabilities.speech_delivery import SpeechDelivery, SpeechDeliveryRequest
from ling.config import Settings
from ling.domains.media.assets import GeneratedMediaService
from ling.domains.speech.orchestration import (
    SYNTHESIS_SCOPE,
    SpeechArtifact,
    SpeechOrchestrator,
    SynthesisOutcome,
    SynthesisRequest,
    SynthesisState,
    encode_state,
)
from ling.domains.speech.providers import build_tts_provider
from ling.infra.models import IdempotencyKey, Message, SpeechSynthesisJob
from ling.infra.security import load_signing_root_key, new_id
from ling.logging_setup import emit_event
from ling.ports.channel import ChannelConnector
from ling.ports.config import EffectiveConfig, VoiceWillingness, voice_requirement_issue
from ling.ports.model import normalize_provider_kind
from ling.ports.speech import (
    AudioInput,
    AudioSpec,
    ExpressionIntent,
    FishAudioVoice,
    MimoReferenceVoice,
    MiniMaxVoice,
    TtsProvider,
    TtsVoice,
)

logger = logging.getLogger("ling.capabilities.voice_reply")


async def recover_synthesis_jobs(session: AsyncSession, *, cutoff: datetime) -> int:
    """Settle interrupted durable intents without repeating possibly billed calls."""
    jobs = list(
        (
            await session.scalars(
                select(SpeechSynthesisJob)
                .where(
                    SpeechSynthesisJob.state == "running",
                    SpeechSynthesisJob.created_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for job in jobs:
        job.state = "uncertain"
        message = await session.get(Message, job.message_id)
        if message is None:
            continue
        if message.output_modality == "voice" and message.speech_state in {
            "pending", "synthesizing"
        }:
            message.speech_state = "cancelled"
        claim = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == SYNTHESIS_SCOPE,
                IdempotencyKey.key == f"{message.turn_id or message.id}:{message.id}",
            )
        )
        if claim is not None:
            claim.result_ref = encode_state(SynthesisState.uncertain, detail="interrupted")
    await session.flush()
    return len(jobs)


async def record_voice_reply(session: AsyncSession, message: Message) -> None:
    """Expose canonical speech only through the restricted behavior-log surface."""
    job = await session.scalar(
        select(SpeechSynthesisJob).where(SpeechSynthesisJob.message_id == message.id)
    )
    emit_event(
        logger,
        logging.INFO,
        event_name="speech_reply",
        message=f"[语音] {message.text}",
        attributes={
            "log_kind": "behavior",
            "message_id": message.id,
            "turn_id": message.turn_id,
            "window_id": message.window_id,
            "job_id": job.id if job else None,
            "provider_id": job.provider_id if job else None,
            "speech_state": message.speech_state,
            "speech_delivery_state": message.speech_delivery_state,
        },
    )


def voice_help(willingness: VoiceWillingness) -> str:
    from ling.ports.config import VOICE_WILLINGNESS_PROMPTS

    preference = VOICE_WILLINGNESS_PROMPTS.get(willingness, VOICE_WILLINGNESS_PROMPTS["natural"])
    return (
        preference + "决定用语音时，在最终回复最前"
        "写一个 [speak]，后面只写真正要说的话。否则直接写文字，不解释标签。"
        "一次语音保持简短，不拆成语音和文字两份。"
    )


async def voice_for_kind(
    kind: str,
    *,
    effective: EffectiveConfig,
    read_reference: Callable[[str], Awaitable[AudioInput | None]],
) -> TtsVoice | None:
    voice = effective.dialogue.voice
    if voice_requirement_issue(voice) or voice.kind != normalize_provider_kind(kind):
        return None
    if voice.kind == "minimax_tts":
        return MiniMaxVoice(
            voice_id=cast(str, voice.voice_id),
            speed=voice.speed,
            vol=voice.vol,
            pitch=voice.pitch,
            emotion=voice.emotion,
        )
    if voice.kind == "fishaudio_tts":
        return FishAudioVoice(reference_id=cast(str, voice.voice_id))
    audio = await read_reference(cast(str, voice.reference_asset_id))
    return MimoReferenceVoice(audio=audio) if audio is not None else None


@dataclass(frozen=True)
class PreparedVoice:
    provider: TtsProvider
    voice: TtsVoice
    intent: ExpressionIntent
    provider_id: str
    model_id: str
    daily_characters: int = 0


class VoiceReplyService:
    """Keep canonical text on the message and publish audio through its source window."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.media = GeneratedMediaService(
            session,
            data_dir=settings.data_dir,
            root_key=load_signing_root_key(settings),
            actor_id=settings.actor_id,
        )

    async def prepare(
        self,
        effective: EffectiveConfig,
        *,
        secret_reader: Callable[[str], Awaitable[str | None]],
    ) -> PreparedVoice | None:
        if effective.dialogue.voice.kind is None:
            return None
        tts = await build_tts_provider(effective, secret_reader=secret_reader)
        if not tts.ok:
            return None
        voice = await voice_for_kind(
            tts.kind,
            effective=effective,
            read_reference=self._read_reference,
        )
        if voice is None:
            return None
        fingerprint = hashlib.sha256(
            json.dumps(
                asdict(effective.dialogue.voice),
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return PreparedVoice(
            provider=tts.provider,
            voice=voice,
            intent=ExpressionIntent(
                output_modality="voice",
                source="model",
                config_revision=effective.revision,
                voice_config_hash=fingerprint,
            ),
            provider_id=tts.provider_id,
            model_id=tts.model_id,
            daily_characters=int(
                effective.providers[tts.provider_id].extra.get("tts_daily_characters") or 0
            ),
        )

    async def _read_reference(self, asset_id: str) -> AudioInput | None:
        from ling.domains.media.assets import AssetError

        try:
            asset = await self.media.view(asset_id)
            if asset.actor_id != self.settings.actor_id or asset.purpose != "tts_reference":
                return None
            if asset.size_bytes > 10 * 1024 * 1024:
                return None
            raw = await self.media.read_for_window(
                asset_id,
                requester_actor=self.settings.actor_id,
                requester_window=asset.window_id,
            )
        except (AssetError, OSError):
            return None
        codec = {"wav": "pcm_s16le", "mp3": "mp3", "ogg": "opus"}.get(asset.format, "unknown")
        return AudioInput(
            content=raw,
            filename=f"reference.{asset.format}",
            spec=AudioSpec(
                mime_type=asset.mime_type,
                container=asset.format,
                codec=codec,
                sample_rate_hz=asset.sample_rate,
                channels=1,
            ),
        )

    async def synthesize(
        self,
        message: Message,
        *,
        prepared: PreparedVoice,
        subject_id: str | None,
        allowed: Callable[[], Awaitable[bool]],
    ) -> bool:
        message.output_modality = "voice"
        message.speech_state = "synthesizing"
        if not await allowed():
            message.speech_state = "cancelled"
            return False
        if len(message.text) > 1000:
            message.speech_state = "failed"
            return False
        job = await self.session.scalar(
            select(SpeechSynthesisJob).where(
                SpeechSynthesisJob.message_id == message.id,
            )
        )
        if job is not None:
            # A previous process may have paid the provider. Never blindly repeat it.
            message.speech_state = "failed"
            return False
        # Serialize reservations across workers without holding a lock during HTTP.
        if self.session.get_bind().dialect.name == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"tts-budget:{prepared.provider_id}"},
            )
        from zoneinfo import ZoneInfo

        from ling.settings.user_preferences import resolve_timezone

        timezone_name, _source = resolve_timezone(None, self.settings.user_timezone)
        today = (
            datetime.now(ZoneInfo(timezone_name))
            .replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .astimezone(UTC)
        )
        used = await self.session.scalar(
            select(func.coalesce(func.sum(SpeechSynthesisJob.input_characters), 0)).where(
                SpeechSynthesisJob.provider_id == prepared.provider_id,
                SpeechSynthesisJob.created_at >= today,
            )
        )
        if (
            prepared.daily_characters > 0
            and int(used or 0) + len(message.text) > prepared.daily_characters
        ):
            message.speech_state = "failed"
            return False
        job = SpeechSynthesisJob(
            id=new_id("tts"),
            message_id=message.id,
            provider_id=prepared.provider_id,
            model_id=prepared.model_id,
            config_revision=prepared.intent.config_revision,
            voice_config_hash=prepared.intent.voice_config_hash,
            text_hash=hashlib.sha256(message.text.encode()).hexdigest(),
            input_characters=len(message.text),
            state="running",
            usage_source="unknown",
        )
        self.session.add(job)
        await self.session.commit()
        try:
            outcome = await SpeechOrchestrator(
                self.session,
                tts=prepared.provider,
                media=self.media,
            ).synthesize(
                SynthesisRequest(
                    turn_id=message.turn_id or message.id,
                    message_id=message.id,
                    text=message.text,
                    window_id=message.window_id,
                    actor_id=self.settings.actor_id,
                    subject_id=subject_id,
                    intent=prepared.intent,
                    voice=prepared.voice,
                    model_id=prepared.model_id,
                )
            )
        except asyncio.CancelledError:
            # Cancellation cannot establish whether the provider billed the request.
            job.state = "uncertain"
            message.speech_state = "cancelled"
            await self.session.commit()
            raise
        if not await allowed():
            job.state = "cancelled"
            message.speech_state = "cancelled"
            if outcome.artifact is not None:
                await self.media.soft_delete(outcome.artifact.asset_id, reason="reply_cancelled")
            return False
        if not outcome.ready:
            job.state = "uncertain" if outcome.billed_uncertain else "failed"
            message.speech_state = "failed"
            return False
        artifact = cast(SpeechArtifact, outcome.artifact)
        from ling.domains.speech.audio_input import prepare_audio

        raw = await self.media.read_for_window(
            artifact.asset_id or artifact.digest,
            requester_actor=self.settings.actor_id,
            requester_window=message.window_id,
        )
        try:
            decoded = await prepare_audio(raw)
        except Exception:
            job.state = "failed"
            await self.media.soft_delete(
                artifact.asset_id or artifact.digest, reason="invalid_reply_audio"
            )
            message.speech_state = "failed"
            return False
        if artifact.size_bytes > 10 * 1024 * 1024 or (decoded.duration_seconds > 60):
            job.state = "failed"
            await self.media.soft_delete(artifact.asset_id, reason="reply_budget_exceeded")
            message.speech_state = "failed"
            return False
        message.audio_asset_id = artifact.asset_id or artifact.digest
        message.speech_state = "ready"
        job.state = "ready"
        # Provider usage often reports audio seconds, not its billable input unit.
        job.usage_source = "unknown"
        await self.session.flush()
        return True

    async def deliver(
        self,
        message: Message,
        *,
        connector: ChannelConnector,
        window_ref: str,
        subject_id: str | None,
        is_group: bool,
    ) -> bool:
        if message.output_modality != "voice" or message.speech_state != "ready":
            return False
        asset = await self.media.view_for_window(
            cast(str, message.audio_asset_id),
            requester_actor=self.settings.actor_id,
            requester_window=message.window_id,
        )
        intent = ExpressionIntent(
            output_modality="voice", source="model", config_revision=0,
        )
        artifact = SpeechArtifact(
            digest=asset.digest,
            asset_id=asset.asset_id,
            mime_type=asset.mime_type,
            format=asset.format,
            size_bytes=asset.size_bytes,
            duration_ms=asset.duration_ms,
            sample_rate=asset.sample_rate,
            reused=True,
        )
        capabilities = connector.capabilities()
        transport = os.environ.get("LING_SPEECH_TRANSPORT")
        if not transport:
            transport = (
                "local_file"
                if getattr(capabilities, "capability_id", "") == "onebot"
                else "base64"
            )
        result = await SpeechDelivery(
            self.session,
            delivery=DeliveryService(self.session),
            media=self.media,
            connector=connector,
        ).deliver_speech(
            request=SpeechDeliveryRequest(
                turn_id=cast(str, message.turn_id),
                message_id=message.id,
                window_ref=window_ref,
                window_id=message.window_id,
                text=message.text,
                is_group=is_group,
            ),
            outcome=SynthesisOutcome(artifact=artifact, intent=intent),
            intent=intent,
            actor_id=self.settings.actor_id,
            subject_id=subject_id,
            transport=transport,
        )
        message.speech_delivery_state = (
            "delivered"
            if result.delivered
            else ("uncertain" if result.state.value == "partial" else "failed")
        )
        await self.session.flush()
        await record_voice_reply(self.session, message)
        return result.delivered
