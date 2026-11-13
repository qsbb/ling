"""Proactive voice through the real worker, durable claim and instance loader.

Both channels must reach TTS after claiming an opportunity. A competing worker
and a later sweep must add no requests. Archiving the source window during TTS
must discard the late audio. Only vendor HTTP and OneBot codec boundaries are
simulated. The criteria were recorded before the state-gate correction.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from connectors.onebot import connector as onebot_connector
from connectors.onebot import voice as onebot_voice
from connectors.onebot.silk import SilkPayload
from sqlalchemy import select

from ling import worker
from ling.domains.conversation.service import ConversationService
from ling.domains.proactive.dispatch import PROACTIVE_DISPATCH_SCOPE
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetGrantRow
from ling.infra.models import (
    IdempotencyKey,
    Message,
    ProactiveOpportunity,
    SpeechSynthesisJob,
    Window,
)
from tests.integration.conftest import TestEnv
from tests.integration.test_connector_runtime_multi_acceptance import (
    USER_ID,
    ProtocolWire,
    _create,
    _save,
    _values,
)
from tests.integration.test_connector_runtime_multi_acceptance import (
    protocol_wire as protocol_wire,
)
from tests.integration.test_voice_reply_acceptance import (
    _assert_audio_playback,
    _install_voice_runtime,
    _publish_voice_settings,
    _VoiceModelEndpoint,
    _VoiceTtsEndpoint,
    _wav_bytes,
)

pytestmark = pytest.mark.w0_web


@dataclass(frozen=True)
class _Candidate:
    opportunity_id: str
    window_id: str
    canonical: str
    fallback_window_id: str | None


class _FixtureSilkCodec:
    def readiness(self) -> tuple[bool, str]:
        return True, ""

    def to_silk(self, _source: object) -> SilkPayload:
        return SilkPayload(
            data=b"\x02#!SILK_V3fixture-proactive",
            duration_seconds=0.1,
            source_duration_seconds=0.1,
        )


async def _prepare_candidate(
    env: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    *,
    channel: str,
    model: _VoiceModelEndpoint,
    tts: _VoiceTtsEndpoint,
) -> _Candidate:
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    draft = await env.client.put(
        "/v1/settings/draft",
        json={
            "dialogue": {
                "proactive_delivery_enabled": True,
                "proactive_daily_limit": 5,
                "quiet_start_minute": 0,
                "quiet_end_minute": 0,
            }
        },
    )
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish",
        json={"revision": draft.json()["revision"]},
    )
    assert published.status_code == 200, published.text
    monkeypatch.setattr(worker, "get_settings", lambda: env.settings)
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)

    title = "主动语音 Web 验收"
    if channel == "onebot":
        # The worker builds a new connector from published DB configuration.
        # app.state.onebot_connector alone would bypass that selection path.
        monkeypatch.setattr(onebot_connector, "default_codec", _FixtureSilkCodec)
        monkeypatch.setattr(onebot_voice, "default_codec", _FixtureSilkCodec)
        monkeypatch.setenv("LING_SPEECH_TRANSPORT", "local_file")
        await _create(env, "onebot", "alpha")
        values = _values("onebot", "alpha")
        values["LING_ONEBOT_WS_URL"] = ""
        values["LING_ONEBOT_WS_TOKEN"] = ""
        saved = await _save(env, "onebot", "alpha", values)
        assert saved.status_code == 202, saved.text
        assert saved.json()["state"] == "active", saved.text
        title = f"onebot-alpha:private:{USER_ID}"

    opportunity_id = f"opp_voice_worker_{uuid.uuid4().hex}"
    canonical = f"主动语音草稿-{uuid.uuid4().hex}"
    moment = datetime.now(UTC)
    async with session_scope() as session:
        conversation = ConversationService(
            session,
            actor_id=env.settings.actor_id,
            persona_id=env.settings.persona_id,
        )
        window = await conversation.create_window(
            subject_id=env.subject_id,
            title=title,
            channel=channel,
        )
        fallback_window_id = None
        if channel == "onebot":
            fallback = await conversation.create_window(
                subject_id=env.subject_id,
                title="主动语音 Web 回退反例",
                channel="web",
            )
            fallback_window_id = fallback.id
        session.add(
            ProactiveOpportunity(
                id=opportunity_id,
                actor_id=env.settings.actor_id,
                subject_id=env.subject_id,
                kind="share",
                reason="真实 worker 语音认领验收",
                state="candidate",
                output_modality="voice",
                draft_text=canonical,
                created_at=moment,
                expires_at=moment + timedelta(hours=1),
            )
        )
        return _Candidate(opportunity_id, window.id, canonical, fallback_window_id)


async def _wait_for_tts(task: asyncio.Task[None], tts: _VoiceTtsEndpoint) -> None:
    started = asyncio.create_task(tts.started.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, started},
            timeout=10,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
        assert tts.started.is_set(), (
            "真实主动语音 worker 未到达 TTS。已认领机会不能被 candidate 状态门控拒绝"
        )
    finally:
        started.cancel()
        await asyncio.gather(started, return_exceptions=True)


async def _snapshot(
    candidate: _Candidate,
) -> tuple[
    ProactiveOpportunity,
    IdempotencyKey,
    list[Message],
    list[SpeechSynthesisJob],
]:
    # Every observation uses a fresh session, including the mid-request claim.
    async with session_scope() as session:
        opportunity = await session.get(ProactiveOpportunity, candidate.opportunity_id)
        claim = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
                IdempotencyKey.key == candidate.opportunity_id,
            )
        )
        messages = list(
            (
                await session.scalars(
                    select(Message).where(
                        Message.turn_id == f"proactive_{candidate.opportunity_id}",
                        Message.role == "actor",
                    )
                )
            ).all()
        )
        jobs = list(
            (
                await session.scalars(
                    select(SpeechSynthesisJob).where(
                        SpeechSynthesisJob.message_id.in_([message.id for message in messages]),
                    )
                )
            ).all()
        )
        assert opportunity is not None
        assert claim is not None
        return opportunity, claim, messages, jobs


async def _assert_no_web_fallback(candidate: _Candidate) -> None:
    if candidate.fallback_window_id is not None:
        async with session_scope() as session:
            messages = list(
                (
                    await session.scalars(
                        select(Message).where(
                            Message.window_id == candidate.fallback_window_id,
                            Message.role == "actor",
                        )
                    )
                ).all()
            )
        assert messages == [], "OneBot 路由失效后把语音草稿投到了 Web 回退窗口"


@pytest.mark.parametrize("channel", ["web", "onebot"])
async def test_worker_claimed_voice_reaches_tts_and_dispatches_once(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    protocol_wire: ProtocolWire,
    channel: str,
) -> None:
    env = logged_in
    model = _VoiceModelEndpoint("此路径不应调用草稿模型")
    tts = _VoiceTtsEndpoint(pause=True)
    candidate = await _prepare_candidate(
        env,
        monkeypatch,
        channel=channel,
        model=model,
        tts=tts,
    )
    protocol_wire.calls.clear()
    first = asyncio.create_task(worker.handle_proactive_dispatch({}))
    try:
        await _wait_for_tts(first, tts)
        opportunity, claim, messages, jobs = await _snapshot(candidate)
        assert opportunity.state == "dispatching"
        assert claim.result_ref == "v1:in_progress"
        assert len(messages) == len(jobs) == 1
        assert messages[0].window_id == candidate.window_id
        assert jobs[0].state == "running"
        assert len(tts.calls) == 1
        assert tts.calls[0]["text"] == candidate.canonical

        # The first provider request remains paused while a second real worker
        # completes its own DB session and observes the durable reservation.
        await asyncio.wait_for(worker.handle_proactive_dispatch({}), timeout=10)
        assert len(tts.calls) == 1
        assert protocol_wire.sends() == []
        assert len((await _snapshot(candidate))[2]) == 1

        tts.release.set()
        await asyncio.wait_for(first, timeout=20)
    finally:
        tts.release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    opportunity, claim, messages, jobs = await _snapshot(candidate)
    assert opportunity.state == "sent"
    assert opportunity.delivery_state == "delivered"
    assert opportunity.sent_at is not None
    assert claim.result_ref == "v1:sent"
    assert len(messages) == len(jobs) == 1
    message = messages[0]
    assert message.text == candidate.canonical
    assert message.output_modality == "voice"
    assert message.speech_state == "ready"
    assert message.speech_delivery_state == "delivered"
    assert message.audio_asset_id
    assert jobs[0].state == "ready"
    assert model.requests == []

    sends = protocol_wire.sends()
    if channel == "onebot":
        assert len(sends) == 1, sends
        assert sends[0]["host"] == "alpha.onebot.example.test"
        assert sends[0]["path"] == "/send_private_msg"
        assert str(sends[0]["body"]["user_id"]) == USER_ID
        segments = sends[0]["body"]["message"]
        assert len(segments) == 1
        assert segments[0]["type"] == "record"
        assert segments[0]["data"]["file"].startswith("base64://")
        assert candidate.canonical not in json.dumps(sends, ensure_ascii=False)
    else:
        assert sends == []
    await _assert_no_web_fallback(candidate)
    await _assert_audio_playback(env, message.id, _wav_bytes())
    history = await env.client.get(f"/v1/sessions/{candidate.window_id}/messages")
    assert history.status_code == 200, history.text
    assert candidate.canonical not in history.text
    operator = await env.client.get(f"/v1/speech/messages/{message.id}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == candidate.canonical

    await worker.handle_proactive_dispatch({})
    assert len(tts.calls) == 1
    assert protocol_wire.sends() == sends
    opportunity, _claim, repeated_messages, repeated_jobs = await _snapshot(candidate)
    assert opportunity.state == "sent"
    assert [row.id for row in repeated_messages] == [message.id]
    assert [row.id for row in repeated_jobs] == [jobs[0].id]


@pytest.mark.parametrize("channel", ["web", "onebot"])
async def test_worker_archived_window_during_tts_discards_audio(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    protocol_wire: ProtocolWire,
    channel: str,
) -> None:
    env = logged_in
    model = _VoiceModelEndpoint("此路径不应调用草稿模型")
    tts = _VoiceTtsEndpoint(pause=True)
    candidate = await _prepare_candidate(
        env,
        monkeypatch,
        channel=channel,
        model=model,
        tts=tts,
    )
    protocol_wire.calls.clear()
    task = asyncio.create_task(worker.handle_proactive_dispatch({}))
    try:
        await _wait_for_tts(task, tts)
        opportunity, claim, messages, jobs = await _snapshot(candidate)
        assert opportunity.state == "dispatching"
        assert claim.result_ref == "v1:in_progress"
        assert len(messages) == len(jobs) == 1
        assert tts.calls[0]["text"] == candidate.canonical
        async with session_scope() as session:
            window = await session.get(Window, candidate.window_id)
            assert window is not None
            window.archived = True
        tts.release.set()
        await asyncio.wait_for(task, timeout=20)
    finally:
        tts.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    opportunity, claim, messages, jobs = await _snapshot(candidate)
    assert opportunity.state == "failed"
    assert opportunity.delivery_state == "failed"
    assert opportunity.sent_at is None
    assert claim.result_ref == "v1:failed"
    assert len(tts.calls) == len(messages) == len(jobs) == 1
    assert messages[0].output_modality == "voice"
    assert messages[0].speech_state == "cancelled"
    assert messages[0].audio_asset_id is None
    assert messages[0].speech_delivery_state != "delivered"
    assert jobs[0].state == "cancelled"
    assert model.requests == []
    assert protocol_wire.sends() == []
    async with session_scope() as session:
        grants = list(
            (
                await session.scalars(
                    select(MediaAssetGrantRow).where(
                        MediaAssetGrantRow.message_id == messages[0].id,
                    )
                )
            ).all()
        )
    assert len(grants) == 1, "必须实际保存过迟到音频，才能证明取消清理分支生效"
    assert grants[0].deleted_at is not None
    await _assert_no_web_fallback(candidate)

    await worker.handle_proactive_dispatch({})
    assert len(tts.calls) == 1
    assert protocol_wire.sends() == []
    assert len((await _snapshot(candidate))[2]) == 1
