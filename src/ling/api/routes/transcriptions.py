"""Authenticated file transcription and draft submission endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from ling.api.deps import ConfigDep, ConversationDep, CurrentAuth, DbSession
from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.domains.consent.service import ConsentService
from ling.domains.media.assets import AssetError
from ling.domains.speech.asr_runtime import media_service
from ling.domains.speech.audio_input import MAX_INPUT_BYTES, prepare_audio
from ling.domains.speech.transcription import (
    TranscriptionError,
    cancel_job,
    create_job,
    job_view,
)
from ling.infra.asr_jobs import AsrJob
from ling.ports.speech import SpeechError

router = APIRouter(tags=["speech"])


def _error(exc: TranscriptionError) -> LingError:
    return LingError(ErrorCode.invalid_request, str(exc), status_code=exc.status)


class TranscriptionBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=96)
    client_request_id: str = Field(min_length=1, max_length=128)
    mode: Literal["send", "draft"] = "send"
    text: str = Field(default="", max_length=8000)


class CommitBody(BaseModel):
    result_revision: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=8000)


async def _job(session: DbSession, session_id: str, job_id: str, subject_id: str) -> AsrJob:
    job = await session.scalar(select(AsrJob).where(
        AsrJob.id == job_id, AsrJob.window_id == session_id, AsrJob.subject_id == subject_id
    ).with_for_update())
    if job is None:
        raise not_found("转写任务不存在")
    return job


@router.post("/sessions/{session_id}/audio-inputs", status_code=201)
async def upload_audio(session_id: str, request: Request, auth: CurrentAuth,
                       conversation: ConversationDep, session: DbSession,
                       file: UploadFile = File(...)) -> dict:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    if not await ConsentService(session).is_active(auth.user.subject_id, "media.inbound"):
        raise LingError(ErrorCode.forbidden, "请先授权处理入站媒体", status_code=403)
    data = await file.read(MAX_INPUT_BYTES + 1)
    await file.close()
    try:
        prepared = await prepare_audio(data)
    except SpeechError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    from ling.models.tts_common import sniff_container

    audio_format = sniff_container(data)
    if data.startswith((b"\x02#!SILK", b"#!SILK")):
        audio_format = "silk"
    elif data.startswith(b"\x1aE\xdf\xa3"):
        audio_format = "webm"
    elif data.startswith(b"#!AMR"):
        audio_format = "amr"
    if audio_format not in {"wav", "mp3", "ogg", "opus", "m4a", "flac", "silk", "webm", "amr"}:
        raise LingError(ErrorCode.invalid_request, "无法确认音频容器格式", status_code=422)
    saved = await media_service(session, request.app).save_bytes(
        data, actor_id=request.app.state.settings.actor_id,
        window_id=session_id, subject_id=auth.user.subject_id, fmt=audio_format,
        sample_rate=None, duration_ms=round(prepared.duration_seconds * 1000),
        purpose="asr_input", retention_seconds=24 * 3600,
    )
    return {"asset_id": saved.asset_id, "duration_seconds": prepared.duration_seconds,
            "mime": prepared.audio.spec.mime_type}


@router.post("/sessions/{session_id}/transcriptions", status_code=202)
async def transcribe(session_id: str, body: TranscriptionBody, request: Request,
                     auth: CurrentAuth, conversation: ConversationDep,
                     session: DbSession, config: ConfigDep) -> dict:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    try:
        view = await media_service(session, request.app).view_for_window(
            body.asset_id, requester_actor=request.app.state.settings.actor_id, requester_window=session_id)
        if view.purpose != "asr_input":
            raise TranscriptionError("invalid_asset", "请选择本会话上传的输入语音", 422)
        job = await create_job(
            session, window_id=session_id, subject_id=auth.user.subject_id,
            actor_id=request.app.state.settings.actor_id, request_key=body.client_request_id,
            mode=body.mode, payload={"audio": [{"asset_id": body.asset_id}], "text": body.text},
            config=await config.effective(),
        )
        return job_view(job)
    except TranscriptionError as exc:
        raise _error(exc) from exc
    except AssetError as exc:
        raise not_found("语音资产不存在或已失效") from exc


@router.get("/sessions/{session_id}/transcriptions/{job_id}")
async def get_transcription(session_id: str, job_id: str, request: Request, auth: CurrentAuth,
                            conversation: ConversationDep, session: DbSession) -> dict:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    job = await _job(session, session_id, job_id, auth.user.subject_id)
    from ling.domains.speech.transcription import require_authorized
    try:
        await require_authorized(session, job)
    except TranscriptionError as exc:
        if exc.code in {"forbidden", "cancelled", "unavailable"}:
            job.state = "cancelled"
            job.submission = "suppressed"
            job.text = ""
            job.edited_text = None
            job.result = {}
            job.error_code = exc.code
            job.error_message = str(exc)[:300]
            job.revision += 1
            await session.commit()
            return job_view(job)
        raise _error(exc) from exc
    return job_view(job)


@router.delete("/sessions/{session_id}/transcriptions/{job_id}")
async def delete_transcription(session_id: str, job_id: str, auth: CurrentAuth,
                               conversation: ConversationDep, session: DbSession) -> dict:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    job = await _job(session, session_id, job_id, auth.user.subject_id)
    try:
        await cancel_job(session, job)
    except TranscriptionError as exc:
        raise _error(exc) from exc
    return job_view(job)


@router.post("/sessions/{session_id}/transcriptions/{job_id}/commit")
async def commit_transcription(session_id: str, job_id: str, body: CommitBody,
                               request: Request, auth: CurrentAuth,
                               conversation: ConversationDep, session: DbSession) -> dict:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    job = await _job(session, session_id, job_id, auth.user.subject_id)
    from ling.domains.speech.transcription import require_authorized
    try:
        await require_authorized(session, job)
        if job.submission == "committed":
            return job_view(job)
        if job.state != "succeeded" or job.revision != body.result_revision:
            raise TranscriptionError("stale_revision", "转写尚未完成或已更新，请刷新后发送")
        job.edited_text = body.text.strip() if body.text is not None else None
        if job.edited_text == "":
            raise TranscriptionError("invalid_request", "消息不能为空", 422)
        job.mode = "send"
    except TranscriptionError as exc:
        raise _error(exc) from exc
    return job_view(job)


@router.post("/sessions/{session_id}/transcriptions/{job_id}/retry", status_code=202)
async def retry_transcription(session_id: str, job_id: str, auth: CurrentAuth,
                              conversation: ConversationDep, session: DbSession) -> dict:
    from ling.domains.speech.transcription import require_authorized
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    job = await _job(session, session_id, job_id, auth.user.subject_id)
    if job.state not in {"failed", "unknown", "expired", "unintelligible"} or job.submission == "committed":
        raise LingError(ErrorCode.invalid_request, "当前任务无法重试", status_code=409)
    try:
        # Expired queue jobs may be explicitly requeued after authorization is checked.
        previous = job.state
        job.state = "queued"
        await require_authorized(session, job)
        job.error_code = None
        job.error_message = None
        job.revision += 1
        from ling.infra.models import utcnow
        job.queued_at = utcnow()
    except TranscriptionError as exc:
        job.state = previous
        raise _error(exc) from exc
    return job_view(job)
