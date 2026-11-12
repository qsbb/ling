"""Durable file recognition worker and trusted conversation completion."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import and_, func, or_, select
from starlette.requests import Request

from ling.api.deps import AuthContext
from ling.domains.conversation.service import ConversationService
from ling.domains.media.assets import GeneratedMediaService
from ling.domains.speech.audio_input import prepare_audio
from ling.domains.speech.transcription import TranscriptionError, lock_scope, require_authorized
from ling.infra.asr_jobs import AsrCallReservation, AsrJob
from ling.infra.db import session_scope
from ling.infra.models import Message
from ling.infra.security import load_signing_root_key
from ling.ports.speech import AsrRequest, SpeechCall, SpeechError
from ling.settings.service import ConfigService, effective_from_payload

logger = logging.getLogger(__name__)

_TEXT_FALLBACK_STATES = ("failed", "unknown", "unintelligible", "no_speech")


def media_service(session, app) -> GeneratedMediaService:
    return GeneratedMediaService(session, data_dir=app.state.settings.data_dir,
                                 root_key=load_signing_root_key(app.state.settings),
                                 actor_id=app.state.settings.actor_id)


async def check_access(app, session, job: AsrJob) -> None:
    await require_authorized(session, job)
    if job.payload.get("envelope"):
        from ling.kernel.access import check_access as check_channel
        from ling.kernel.visibility import group_id_from_window_ref
        env = job.payload["envelope"]
        settings = app.state.settings
        access = check_channel(env.get("account_ref"), mode=settings.access_mode,
                               allow_list=settings.access_list,
                               group_id=group_id_from_window_ref(env["window_ref"]),
                               allow_groups=settings.access_groups)
        if not access.observable:
            raise TranscriptionError("forbidden", "当前账号已失去渠道访问资格", 403)


async def _claim(job_id: str | None = None) -> tuple[str, int] | None:
    async with session_scope() as session:
        await lock_scope(session, "asr:workers")
        now = datetime.now(UTC)
        stale = (await session.scalars(select(AsrJob).where(
            AsrJob.state == "running", AsrJob.lease_until < now
        ).with_for_update())).all()
        for stale_job in stale:
            stale_job.state = "unknown"
            stale_job.error_code = "unknown"
            stale_job.error_message = "识别连接中断，调用结果尚未核实，可手动重试"
        active = (await session.scalars(select(AsrJob.window_id).where(
            AsrJob.state == "running", AsrJob.lease_until > now
        ))).all()
        if len(active) >= 2:
            return None
        query = select(AsrJob).where(AsrJob.state == "queued")
        if job_id:
            query = query.where(AsrJob.id == job_id)
        if active:
            query = query.where(AsrJob.window_id.not_in(active))
        row = await session.scalar(query.order_by(AsrJob.queued_at).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if (now - row.queued_at.replace(tzinfo=UTC)).total_seconds() > 60:
            row.state = "expired"
            row.error_code = "expired"
            row.error_message = "语音识别排队超时，请重试"
            return None
        row.state = "running"
        row.attempt += 1
        row.lease_until = now + timedelta(seconds=180)
        return row.id, row.attempt


async def _load_provider_config(app, job_id: str, attempt: int):
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        if job is None or job.state != "running" or job.attempt != attempt:
            return None
        await check_access(app, session, job)
        payload = dict(job.payload)
        config = effective_from_payload(
            job.config_payload,
            revision=job.config_revision,
            persona_default="",
            log_denials_default=True,
        )
        call = SpeechCall(job.id, f"{job.id}:{attempt}", job.window_id, None, job.config_revision)
        return job, payload, config, call


async def _process_audio(
    app, *, job_id: str, attempt: int, audio_index: int, item: dict, payload: dict,
    config, binding, call: SpeechCall, settings,
    leases: list[tuple[str, str]],
) -> dict[str, Any]:
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id, populate_existing=True)
        if job is None or job.state != "running" or job.attempt != attempt:
            raise TranscriptionError("cancelled", "语音识别已取消")
        await check_access(app, session, job)
        if "asset_id" in item:
            media = media_service(session, app)
            lease = await media.acquire_lease(
                item["asset_id"], requester_actor=job.actor_id,
                requester_window=job.window_id, ttl_seconds=180,
            )
            leases.append((item["asset_id"], lease))
            raw = await media.read_for_window(
                item["asset_id"], requester_actor=job.actor_id,
                requester_window=job.window_id,
            )
        else:
            from ling.domains.media.service import MediaEvidenceService
            evidence = MediaEvidenceService(session, actor_id=job.actor_id)
            await evidence.ensure_description_authorized(
                item["evidence_id"], window_id=job.window_id
            )
            raw = None

    if raw is None:
        from ling.worker import make_media_fetcher
        channel = payload.get("channel", "onebot")
        connector = getattr(app.state, f"{channel}_connector", None)
        envelope = payload.get("envelope") or {}
        if connector is None or connector.instance_id != envelope.get("connector_instance"):
            raise TranscriptionError("unavailable", "收到语音的连接器实例当前不可用")
        fetch = make_media_fetcher(
            connector, media_root=settings.data_dir / "media", max_bytes=10 * 1024 * 1024
        )
        raw, _ = await fetch(item["ref"])

    prepared = await prepare_audio(raw)
    if prepared.silent:
        return {"text": "", "state": "no_speech", "usage": None}

    async with session_scope() as session:
        job = await session.get(AsrJob, job_id, populate_existing=True)
        if job is None or job.state != "running" or job.attempt != attempt:
            raise TranscriptionError("cancelled", "语音识别已取消")
        await check_access(app, session, job)
        await lock_scope(session, f"asr:budget:{binding.provider_id}")
        provider = config.providers[binding.provider_id]
        daily_limit = float(provider.extra.get("asr_daily_seconds") or 0)
        day = app.state.quota.today()
        total = await session.scalar(select(func.sum(AsrCallReservation.seconds)).where(
            AsrCallReservation.provider_id == binding.provider_id, AsrCallReservation.day == day
        )) or 0
        if daily_limit > 0 and total + prepared.duration_seconds > daily_limit:
            raise TranscriptionError("quota", "今天的语音识别额度已用完")
        job.provider_id = binding.provider_id
        session.add(AsrCallReservation(
            job_id=job_id, attempt=attempt, audio_index=audio_index,
            provider_id=binding.provider_id, day=day, seconds=prepared.duration_seconds,
        ))
        job.charged_seconds += prepared.duration_seconds
        job.lease_until = datetime.now(UTC) + timedelta(seconds=120)

    # An interrupted request stays charged because the provider may have accepted it.
    async with asyncio.timeout(90):
        result = await binding.provider.transcribe(AsrRequest(binding.model_id, prepared.audio), call=call)
    recognized = result.text.strip()
    if len(recognized) > 8000:
        raise TranscriptionError("too_long", "识别文字过长，请缩短语音后再发送", 422)
    return {
        "text": recognized,
        "language": result.language,
        "confidence": result.confidence,
        "provider_request_id": result.provider_request_id,
        "usage": asdict(result.usage),
    }


async def process_job(app, job_id: str | None = None) -> bool:
    claimed = await _claim(job_id)
    if claimed is None:
        return False
    job_id, attempt = claimed
    leases: list[tuple[str, str]] = []
    try:
        initial = await _load_provider_config(app, job_id, attempt)
        if initial is None:
            return True
        job, payload, config, call = initial
        from ling.domains.speech.asr_providers import build_asr_provider

        async def secret_reader(provider_id: str) -> str | None:
            async with session_scope() as session:
                return await ConfigService(session, app.state.secret_box).read_secret(provider_id)

        binding = await build_asr_provider(config, secret_reader=secret_reader)
        if not binding.ok:
            raise TranscriptionError("not_configured", binding.reason, 422)
        settings = app.state.settings
        results: list[dict[str, Any]] = []
        for audio_index, item in enumerate(payload["audio"]):
            results.append(await _process_audio(
                app, job_id=job_id, attempt=attempt, audio_index=audio_index, item=item, payload=payload,
                config=config, binding=binding, call=call, settings=settings, leases=leases,
            ))

        async with session_scope() as session:
            job = await session.scalar(select(AsrJob).where(
                AsrJob.id == job_id
            ).with_for_update())
            if job is None or job.state != "running" or job.attempt != attempt:
                return True
            await check_access(app, session, job)
            job.text = "\n".join(item["text"] for item in results if item["text"])
            if len(job.text) > 8000:
                raise TranscriptionError("too_long", "识别文字过长，请缩短语音后再发送", 422)
            job.result = {
                "audio": results,
                "origin": "asr",
                "estimated_audio_seconds": job.charged_seconds,
            }
            job.state = "succeeded" if (job.text or payload.get("text", "").strip()) else "no_speech"
            job.revision += 1
        await submit_job(app, job_id, automatic=True)
    except (SpeechError, TranscriptionError, TimeoutError) as exc:
        kind = getattr(exc, "code", getattr(getattr(exc, "kind", None), "value", "timeout"))
        submit_text = False
        async with session_scope() as session:
            job = await session.scalar(select(AsrJob).where(
                AsrJob.id == job_id
            ).with_for_update())
            if job is not None and job.state == "running" and job.attempt == attempt:
                job.state = (
                    "unknown" if kind in {"timeout", "network", "unavailable"}
                    else "unintelligible" if kind == "unintelligible"
                    else "failed"
                )
                job.error_code = kind
                job.error_message = str(exc)[:300]
                job.revision += 1
                if job.mode == "send" and job.payload.get("text", "").strip():
                    job.text = job.payload["text"].strip()
                    submit_text = True
        if submit_text:
            await submit_job(app, job_id, automatic=True)
    except Exception:
        logger.exception("语音识别任务失败 job=%s", job_id)
        async with session_scope() as session:
            job = await session.get(AsrJob, job_id)
            if job is not None and job.state == "running" and job.attempt == attempt:
                job.state = "unknown" if job.charged_seconds else "failed"
                job.error_code = "internal"
                job.error_message = "语音处理失败，请重试或输入文字"
    finally:
        for _, lease_id in leases:
            async with session_scope() as session:
                await media_service(session, app).release_lease(lease_id)
    return True


async def submit_job(app, job_id: str, *, automatic: bool = False,
                     revision: int | None = None, edited_text: str | None = None) -> None:
    async with session_scope() as session:
        await lock_scope(session, f"asr:submit:{job_id}")
        job = await session.scalar(select(AsrJob).where(AsrJob.id == job_id).with_for_update())
        if job is None:
            return
        if job.submission == "committed":
            return
        if (job.submission == "dispatching" and job.lease_until
                and job.lease_until.replace(tzinfo=UTC) > datetime.now(UTC)):
            return
        if automatic and job.mode == "draft":
            return
        if not _can_submit(job):
            return
        if revision is not None and job.revision != revision:
            raise TranscriptionError("stale_revision", "转写结果已更新，请刷新后再发送")
        await check_access(app, session, job)
        if edited_text is not None:
            if not edited_text.strip() or len(edited_text) > 8000:
                raise TranscriptionError("invalid_request", "发送文字长度应为 1 到 8000 字符", 422)
            job.edited_text = edited_text.strip()
        body = job.edited_text if job.edited_text is not None else job.text
        input_text = job.payload.get("text", "").strip()
        text = input_text if body == input_text else "\n".join(x for x in (input_text, body) if x)
        if len(text) > 8000:
            raise TranscriptionError("too_long", "消息文字超过 8000 字符", 422)
        if job.source_message_id is None:
            msg = await ConversationService(session, actor_id=job.actor_id).add_message(
                job.window_id, role="user", text="", sender_subject_id=job.subject_id, handled_as="pending_asr")
            job.source_message_id = msg.message_id
        source = await session.get(Message, job.source_message_id)
        if source is None or source.retracted_at is not None:
            raise TranscriptionError("cancelled", "原消息已删除或撤回")
        source.text = text
        later = await session.scalar(select(Message.id).where(
            Message.window_id == job.window_id, Message.sequence > source.sequence).limit(1))
        if later:
            source.handled_as = "ignored"
            source.handled_reason = "语音识别稍后完成，已附回原消息"
            source.attention_state = "unseen"
            job.submission = "committed"
            job.submitted_at = datetime.now(UTC)
            return
        job.submission = "dispatching"
        job.lease_until = datetime.now(UTC) + timedelta(seconds=450)
        payload = dict(job.payload)
        source_id, subject_id, window_id = source.id, job.subject_id, job.window_id
    # Recheck cancellation and access immediately before the normal chat entry.
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id, populate_existing=True)
        if job is None or not _can_submit(job) or job.submission != "dispatching":
            return
        await check_access(app, session, job)
    # Reuse normal dialogue entry points after committing the source message.
    if payload.get("envelope"):
        from ling.contracts.v1 import InputPart, InteractionEnvelope, Modality
        from ling.kernel.inbound_intake import process_inbound_payload
        original = InteractionEnvelope.model_validate(payload["envelope"])
        env = original.model_copy(update={"parts": [InputPart(modality=Modality.text, text=text)]})
        connector = getattr(app.state, f"{payload.get("channel", "onebot")}_connector", None)
        if connector is None or connector.instance_id != original.connector_instance:
            raise TranscriptionError("unavailable", "收到语音的连接器实例当前不可用")
        await process_inbound_payload(app, connector, {}, _asr_resume=(env, source_id))
    else:
        from ling.api.routes.chat import SendTurnBody, send_transcribed_turn
        async with session_scope() as session:
            settings = app.state.settings
            request = Request({"type": "http", "app": app, "path": "/v1/asr/internal", "headers": []})
            response = await send_transcribed_turn(
                window_id, SendTurnBody(client_turn_id=f"asr:{job_id}", text=text), request,
                cast(AuthContext, SimpleNamespace(user=SimpleNamespace(subject_id=subject_id))),
                ConversationService(session, actor_id=settings.actor_id), app.state.router,
                ConfigService(session, app.state.secret_box, test_mode_default=settings.test_mode),
                settings, source_message_id=source_id)
            async for _ in response.body_iterator:
                pass
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        if job is None:
            return
        if job.submission == "dispatching":
            job.submission = "committed"
            job.submitted_at = datetime.now(UTC)


def _can_submit(job: AsrJob) -> bool:
    if job.state == "succeeded":
        return True
    has_input_text = bool(job.payload.get("text", "").strip())
    return has_input_text and job.state in _TEXT_FALLBACK_STATES


async def worker_loop(app) -> None:
    while True:
        try:
            # Recover durable recognition results whose conversation submission was interrupted.
            async with session_scope() as session:
                pending = (await session.scalars(select(AsrJob.id).where(
                    or_(AsrJob.state == "succeeded", and_(
                        AsrJob.state.in_(_TEXT_FALLBACK_STATES),
                        func.trim(AsrJob.payload["text"].as_string()) != "",
                    )),
                    AsrJob.mode == "send", AsrJob.submission.in_(["pending", "dispatching"]),
                ).order_by(AsrJob.created_at).limit(10))).all()
            for job_id in pending:
                try:
                    await submit_job(app, job_id, automatic=True)
                except TranscriptionError as exc:
                    async with session_scope() as session:
                        job = await session.get(AsrJob, job_id, populate_existing=True)
                        if job is not None and job.submission != "committed":
                            job.submission = "suppressed"
                            job.state = "cancelled" if exc.code in {"forbidden", "cancelled", "unavailable"} else job.state
                            job.error_code = exc.code
                            job.error_message = str(exc)[:300]
                            job.text = ""
                            job.edited_text = None
                            job.result = {}
            if not await process_job(app):
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("语音任务轮询失败")
            await asyncio.sleep(2)


async def stop_worker(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
