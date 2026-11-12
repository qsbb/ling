"""Persist transcription requests, authorization and idempotent submission."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import new_id
from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import ConversationService
from ling.infra.asr_jobs import AsrJob
from ling.infra.models import Message, Window
from ling.ports.config import EffectiveConfig
from ling.settings.service import payload_from_effective


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


async def lock_scope(session: AsyncSession, key: str) -> None:
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


async def require_authorized(session: AsyncSession, job: AsrJob) -> Window:
    window = await session.get(Window, job.window_id, populate_existing=True)
    if window is None or window.archived:
        raise TranscriptionError("unavailable", "会话不存在或已归档", 404)
    subjects = {job.subject_id, window.subject_id}
    service = ConsentService(session)
    if not any([await service.is_active(s, "media.inbound") for s in subjects if s]):
        raise TranscriptionError("forbidden", "入站媒体授权已撤销，无法识别语音", 403)
    if job.source_message_id:
        msg = await session.get(Message, job.source_message_id, populate_existing=True)
        if msg is None or msg.retracted_at is not None:
            raise TranscriptionError("cancelled", "原消息已删除或撤回")
    if job.state == "cancelled":
        raise TranscriptionError("cancelled", "语音识别已取消")
    return window


async def create_job(
    session: AsyncSession, *, window_id: str, subject_id: str, actor_id: str,
    request_key: str, mode: str, payload: dict, config: EffectiveConfig,
    source_message_id: str | None = None,
) -> AsrJob:
    fingerprint = hashlib.sha256(json.dumps({"payload": payload, "mode": mode}, sort_keys=True).encode()).hexdigest()
    await lock_scope(session, f"asr:create:{window_id}:{subject_id}:{request_key}")
    existing = await session.scalar(select(AsrJob).where(
        AsrJob.window_id == window_id, AsrJob.subject_id == subject_id, AsrJob.request_key == request_key
    ))
    if existing:
        if existing.fingerprint != fingerprint:
            raise TranscriptionError("conflict", "该请求标识已用于其他语音内容")
        return existing
    binding = config.resolve_profile("speech.asr")
    if not binding.usable or not binding.enabled:
        raise TranscriptionError("not_configured", "请先在设置中绑定并启用语音识别模型", 422)
    job = AsrJob(
        id=new_id("asr"), window_id=window_id, subject_id=subject_id, actor_id=actor_id,
        request_key=request_key, fingerprint=fingerprint, mode=mode, payload=payload,
        state="queued", submission="pending", revision=1, attempt=0,
        config_revision=config.revision, config_payload=payload_from_effective(config),
        provider_id=binding.provider_id or "", source_message_id=source_message_id,
        result={}, text="", charged_seconds=0,
    )
    await require_authorized(session, job)
    if mode == "send" and source_message_id is None:
        conv = ConversationService(session, actor_id=actor_id)
        msg = await conv.add_message(window_id, role="user", text=payload.get("text", ""),
                                     sender_subject_id=subject_id, handled_as="pending_asr")
        job.source_message_id = msg.message_id
    session.add(job)
    await session.flush()
    return job


async def cancel_job(session: AsyncSession, job: AsrJob) -> None:
    if job.submission == "committed":
        raise TranscriptionError("committed", "该语音已经发送，请通过消息操作撤回")
    if job.submission == "dispatching":
        raise TranscriptionError("dispatching", "语音正在进入对话，稍后可通过消息操作撤回")
    job.state = "cancelled"
    job.submission = "suppressed"
    job.revision += 1
    job.text = ""
    job.edited_text = None
    job.result = {}
    if job.source_message_id:
        msg = await session.get(Message, job.source_message_id)
        if msg and msg.handled_as == "pending_asr":
            msg.handled_as = "ignored"
            msg.handled_reason = "语音识别已取消"
    await session.flush()


def job_view(job: AsrJob) -> dict:
    return {
        "job_id": job.id, "state": job.state, "submission": job.submission,
        "revision": job.revision, "text": job.edited_text if job.edited_text is not None else job.text,
        "mode": job.mode, "message_id": job.source_message_id,
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        "result": job.result, "created_at": job.created_at.isoformat(),
        "charged_seconds": job.charged_seconds,
    }
