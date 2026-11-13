"""Participant replay and crashed synthesis intents through real entry points.

Replay must hide voice canonical text after stream-log loss while preserving
ordinary text snapshots. Startup recovery must settle running intents strictly
older than the existing dangling-turn cutoff, preserving fresh work. The age
predicate is derived from DANGLING_TURN_MINUTES, independently of run timings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import ling.app as app_module
from ling import worker
from ling.contracts.v1 import TurnState
from ling.domains.conversation.service import ConversationService
from ling.infra import models
from ling.infra.db import session_scope
from ling.infra.models import Message, SpeechSynthesisJob, Turn
from tests.integration.conftest import TestEnv, read_sse
from tests.integration.test_voice_reply_acceptance import (
    ProjectionMessage,
    _install_voice_runtime,
    _publish_voice_settings,
    _seed_message,
    _VoiceModelEndpoint,
    _VoiceTtsEndpoint,
)

pytestmark = pytest.mark.w0_web

RECOVERY_NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)


async def _finish_projection(message: ProjectionMessage, state: str) -> None:
    assert message.turn_id is not None
    async with session_scope() as session:
        turn = await session.get(Turn, message.turn_id)
        row = await session.get(Message, message.message_id)
        assert turn is not None and row is not None
        turn.state = state
        row.state = state


async def _resume(env: TestEnv, message: ProjectionMessage) -> list[tuple[str, dict]]:
    assert message.turn_id is not None
    response = await env.client.get(f"/v1/turns/{message.turn_id}/resume")
    assert response.status_code == 200, response.text
    events = await read_sse(response)
    resumed = [body for name, body in events if name == "turn.resumed"]
    assert len(resumed) == 1, events
    assert resumed[0]["turn_id"] == message.turn_id
    assert resumed[0]["complete"] is False
    return events


def _snapshot_text(events: list[tuple[str, dict]]) -> list[str]:
    return [body["text"] for name, body in events if name == "turn.snapshot"]


@pytest.mark.parametrize(
    ("speech_state", "turn_state"),
    [("ready", "completed"), ("failed", "failed"), ("cancelled", "stopped")],
)
async def test_voice_resume_after_stream_log_loss_hides_canonical_and_keeps_text(
    logged_in: TestEnv,
    speech_state: str,
    turn_state: str,
) -> None:
    env = logged_in
    voice = await _seed_message(env, speech_state=speech_state, active=True)
    plain = await _seed_message(env, modality="text", active=True)
    for message in (voice, plain):
        await _finish_projection(message, turn_state)
        env.app.state.cancels.discard(message.turn_id)
        env.app.state.stream_log.clear(turn_id=message.turn_id)

    plain_events = await _resume(env, plain)
    assert _snapshot_text(plain_events) == [plain.text], plain_events
    voice_events = await _resume(env, voice)
    assert voice.text not in json.dumps(voice_events, ensure_ascii=False), voice_events
    assert [body["state"] for name, body in voice_events if name == "turn.finished"] == [turn_state]
    operator = await env.client.get(f"/v1/speech/messages/{voice.message_id}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == voice.text


async def test_active_voice_resume_hides_partial_while_text_partial_is_visible(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    voice = await _seed_message(env, speech_state="synthesizing", active=True)
    plain = await _seed_message(env, modality="text", active=True)
    for message in (voice, plain):
        assert env.app.state.cancels.partial(message.turn_id) == message.text
        env.app.state.stream_log.clear(turn_id=message.turn_id)

    plain_events = await _resume(env, plain)
    assert _snapshot_text(plain_events) == [plain.text]
    voice_events = await _resume(env, voice)
    assert voice.text not in json.dumps(voice_events, ensure_ascii=False), voice_events
    assert [body["state"] for name, body in voice_events if name == "turn.resumed"] == ["streaming"]


@dataclass(frozen=True)
class _RunningIntent:
    message_id: str
    job_id: str
    canonical: str


async def _seed_running_intent(
    env: TestEnv,
    *,
    created_at: datetime,
    with_turn: bool = True,
) -> _RunningIntent:
    token = uuid.uuid4().hex
    canonical = f"崩溃恢复保留原文-{token}"
    turn_id = f"turn_crash_{token}" if with_turn else f"proactive_opp_crash_{token}"
    async with session_scope() as session:
        conversation = ConversationService(
            session,
            actor_id=env.settings.actor_id,
            persona_id=env.settings.persona_id,
        )
        window = await conversation.create_window(
            subject_id=env.subject_id,
            title="持久语音意图恢复验收",
            channel="web",
        )
        if with_turn:
            session.add(
                Turn(
                    id=turn_id,
                    window_id=window.id,
                    client_turn_id=turn_id,
                    state="streaming",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            await session.flush()
        # Proactive synthesis uses a synthetic turn_id without a Turn row.
        view = await conversation.add_message(
            window.id,
            role="actor",
            text=canonical,
            turn_id=turn_id,
            state=TurnState.completed,
        )
        message = await session.get(Message, view.message_id)
        assert message is not None
        message.output_modality = "voice"
        message.speech_state = "synthesizing"
        message.created_at = created_at
        job_id = f"tts_crash_{token}"
        session.add(
            SpeechSynthesisJob(
                id=job_id,
                message_id=message.id,
                provider_id="voice_tts_fixture",
                model_id="speech-2.8-hd",
                config_revision=1,
                text_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                input_characters=len(canonical),
                state="running",
                usage_source="unknown",
                created_at=created_at,
            )
        )
        return _RunningIntent(message.id, job_id, canonical)


async def _intent_state(intent: _RunningIntent) -> tuple[str, str]:
    async with session_scope() as session:
        job = await session.get(SpeechSynthesisJob, intent.job_id)
        message = await session.get(Message, intent.message_id)
        assert job is not None and message is not None
        assert message.text == intent.canonical
        assert message.output_modality == "voice"
        assert message.audio_asset_id is None
        assert message.speech_delivery_state != "delivered"
        return job.state, message.speech_state


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
async def test_startup_recovers_old_synthesis_intents_without_touching_fresh_work(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    env = logged_in
    model = _VoiceModelEndpoint("恢复过程不应重新生成正文")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    monkeypatch.setattr(worker, "get_settings", lambda: env.settings)
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)
    monkeypatch.setattr(models, "utcnow", lambda: RECOVERY_NOW)
    monkeypatch.setattr(worker, "utcnow", lambda: RECOVERY_NOW)
    assert app_module.DANGLING_TURN_MINUTES == worker.DANGLING_TURN_MINUTES
    cutoff = RECOVERY_NOW - timedelta(minutes=worker.DANGLING_TURN_MINUTES)
    old = await _seed_running_intent(env, created_at=cutoff - timedelta(seconds=1))
    proactive_old = await _seed_running_intent(
        env,
        created_at=cutoff - timedelta(seconds=1),
        with_turn=False,
    )
    boundary = await _seed_running_intent(env, created_at=cutoff)
    fresh = await _seed_running_intent(env, created_at=RECOVERY_NOW)
    for intent in (old, proactive_old, boundary, fresh):
        assert await _intent_state(intent) == ("running", "synthesizing")

    recover = (
        app_module._recover_dangling_turns if entrypoint == "api" else worker.recover_dangling_turns
    )
    await recover()
    for intent in (old, proactive_old):
        assert await _intent_state(intent) == ("uncertain", "cancelled"), (
            "超过既有恢复阈值的持久语音意图仍未结算，或丢失可能已计费的不确定性"
        )
    for intent in (boundary, fresh):
        assert await _intent_state(intent) == ("running", "synthesizing"), (
            "尚未严格早于恢复 cutoff 的任务被启动恢复误杀"
        )
    assert tts.calls == []
    assert model.requests == []

    await recover()
    for intent in (old, proactive_old):
        assert await _intent_state(intent) == ("uncertain", "cancelled")
    for intent in (boundary, fresh):
        assert await _intent_state(intent) == ("running", "synthesizing")
    assert tts.calls == []
