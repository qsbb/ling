"""Independent voice projections and normal reply entry points.

The acceptance criteria were written before the current entry-point wiring was
implemented. Platform, model, and TTS boundaries use local deterministic fakes.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import struct
import uuid
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ling.contracts.v1 import TurnState
from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.domains.media.assets import GeneratedMediaService
from ling.domains.speech.orchestration import SYNTHESIS_SCOPE, SynthesisState, decode_state
from ling.infra.db import session_scope
from ling.infra.models import (
    DeferredReply,
    IdempotencyKey,
    Message,
    SpeechSynthesisJob,
    Turn,
    Window,
)
from ling.infra.security import load_signing_root_key
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


@dataclass(frozen=True)
class ProjectionMessage:
    window_id: str
    message_id: str
    text: str
    audio_asset_id: str | None
    turn_id: str | None


def _wav_bytes(scale: int = 20) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(
            b"".join(struct.pack("<h", (index % 80) * scale) for index in range(1600))
        )
    return buffer.getvalue()


async def _seed_message(
    env: TestEnv,
    *,
    modality: str = "voice",
    speech_state: str = "ready",
    owner_subject_id: str | None = None,
    actor_id: str | None = None,
    active: bool = False,
) -> ProjectionMessage:
    actor = actor_id or env.settings.actor_id
    owner = owner_subject_id or env.subject_id
    canonical = f"验收语音原文-{uuid.uuid4().hex}"
    async with session_scope() as session:
        conversation = ConversationService(
            session, actor_id=actor, persona_id=env.settings.persona_id
        )
        window = await conversation.create_window(
            subject_id=owner, title="语音投影验收", channel="web"
        )
        turn_id = None
        if active:
            turn_id = f"turn_projection_{uuid.uuid4().hex}"
            session.add(
                Turn(id=turn_id, window_id=window.id, client_turn_id=turn_id, state="streaming")
            )
            await session.flush()
        view = await conversation.add_message(
            window.id,
            role="actor",
            text=canonical,
            turn_id=turn_id,
            state=TurnState.streaming if active else TurnState.completed,
        )
        message = await session.get(Message, view.message_id)
        assert message is not None
        message.output_modality = modality
        message.speech_state = speech_state if modality == "voice" else "none"
        message.speech_delivery_state = "uncertain" if modality == "voice" else None
        asset_id = None
        if modality == "voice" and speech_state == "ready":
            media = GeneratedMediaService(
                session,
                data_dir=env.settings.data_dir,
                root_key=load_signing_root_key(env.settings),
                actor_id=actor,
            )
            asset = await media.save_bytes(
                _wav_bytes(),
                actor_id=actor,
                window_id=window.id,
                subject_id=owner,
                message_id=message.id,
                turn_id=turn_id,
                fmt="wav",
                mime_type="audio/wav",
                sample_rate=16000,
                duration_ms=100,
                purpose="tts_output",
                producer="acceptance_fixture",
            )
            asset_id = asset.asset_id
            message.audio_asset_id = asset_id
        result = ProjectionMessage(window.id, message.id, canonical, asset_id, turn_id)
    if active:
        assert result.turn_id is not None
        env.app.state.cancels.set_partial(result.turn_id, canonical)
    return result


async def _create_and_login(env: TestEnv, *, role: str) -> str:
    username = f"voice_{role}_{uuid.uuid4().hex[:8]}"
    async with session_scope() as session:
        user = await IdentityService(session).create_user(
            username=username,
            password="fixture-member-password",
            role=role,
        )
    response = await env.client.post(
        "/v1/auth/login",
        json={"username": username, "password": "fixture-member-password"},
    )
    assert response.status_code == 200, response.text
    return user.subject_id


async def test_owner_ordinary_views_hide_voice_and_operator_returns_same_audio(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    voice = await _seed_message(env)
    plain = await _seed_message(env, modality="text")
    ordinary = await env.client.get(f"/v1/sessions/{voice.window_id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    message = next(
        item for item in ordinary.json()["messages"] if item["message_id"] == voice.message_id
    )
    assert message["text"] == "" and voice.text not in ordinary.text
    assert (
        message["output_modality"] == "voice" and message["audio_asset_id"] == voice.audio_asset_id
    )
    assert message["speech_state"] == "ready"
    assert message["speech_delivery_state"] == "uncertain"
    plain_response = await env.client.get(f"/v1/sessions/{plain.window_id}/messages")
    assert plain_response.status_code == 200, plain_response.text
    assert plain_response.json()["messages"][0]["text"] == plain.text
    assert plain_response.json()["messages"][0]["output_modality"] == "text"
    for path in (
        f"/v1/sessions/{voice.window_id}/voice-messages",
        f"/v1/messages/{voice.message_id}/voice",
    ):
        ordinary = await env.client.get(path)
        assert ordinary.status_code == 200, ordinary.text
        assert voice.text not in ordinary.text
        item = ordinary.json()
        if "voice_messages" in item:
            assert len(item["voice_messages"]) == 1
            item = item["voice_messages"][0]
        assert item["message_id"] == voice.message_id and item["text"] == ""
        assert item["delivered"] is False and item["played"] is False
        audio = await env.client.get(item["url"])
        assert audio.status_code == 200 and audio.content == _wav_bytes()
        with wave.open(io.BytesIO(audio.content), "rb") as decoded:
            assert decoded.getnframes() > 0
        operator = await env.client.get(path, params={"view": "operator"})
        assert operator.status_code == 200, operator.text
        operator_item = operator.json()
        if "voice_messages" in operator_item:
            operator_item = operator_item["voice_messages"][0]
        assert operator_item["text"] == voice.text
    operator = await env.client.get(
        f"/v1/sessions/{voice.window_id}/messages", params={"view": "operator"}
    )
    assert operator.status_code == 200, operator.text
    assert operator.json()["messages"][0]["text"] == voice.text
    backstage = await env.client.get(f"/v1/speech/messages/{voice.message_id}")
    assert backstage.status_code == 200, backstage.text
    assert backstage.json()["view"] == "operator"
    assert backstage.json()["message"]["text"] == voice.text
    assert backstage.json()["message"]["message_id"] == voice.message_id
    assert backstage.json()["voice"]["text"] == voice.text
    async with session_scope() as session:
        row = await session.get(Message, voice.message_id)
        assert row is not None and row.text == voice.text


@pytest.mark.parametrize(
    "speech_state", ["pending", "synthesizing", "failed", "cancelled", "expired"]
)
async def test_incomplete_voice_states_keep_canonical_only_in_explicit_operator_view(
    logged_in: TestEnv,
    speech_state: str,
) -> None:
    env = logged_in
    voice = await _seed_message(env, speech_state=speech_state)
    ordinary = await env.client.get(f"/v1/sessions/{voice.window_id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    item = ordinary.json()["messages"][0]
    assert item["text"] == "" and voice.text not in ordinary.text
    assert item["output_modality"] == "voice" and item["speech_state"] == speech_state
    operator = await env.client.get(f"/v1/speech/messages/{voice.message_id}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == voice.text
    assert operator.json()["message"]["speech_state"] == speech_state
    assert operator.json()["voice"] is None


async def test_member_owns_participant_audio_but_all_operator_forms_return_403(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    owner_subject_id = await _create_and_login(env, role="member")
    voice = await _seed_message(env, owner_subject_id=owner_subject_id)
    for path in (
        f"/v1/sessions/{voice.window_id}/messages",
        f"/v1/sessions/{voice.window_id}/voice-messages",
        f"/v1/messages/{voice.message_id}/voice",
    ):
        ordinary = await env.client.get(path)
        assert ordinary.status_code == 200, ordinary.text
        assert voice.text not in ordinary.text
        operator = await env.client.get(path, params={"view": "operator"})
        assert operator.status_code == 403, operator.text
        assert voice.text not in operator.text
    backstage = await env.client.get(f"/v1/speech/messages/{voice.message_id}")
    assert backstage.status_code == 403, backstage.text
    assert voice.text not in backstage.text


async def test_cross_subject_window_access_stays_closed_and_actor_backend_scope_is_checked(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    owner_voice = await _seed_message(env)
    other_subject = await _create_and_login(env, role="member")
    own_voice = await _seed_message(env, owner_subject_id=other_subject)
    for path, expected in (
        (f"/v1/sessions/{owner_voice.window_id}/messages", 404),
        (f"/v1/sessions/{owner_voice.window_id}/voice-messages", 200),
        (f"/v1/messages/{owner_voice.message_id}/voice", 404),
    ):
        refused = await env.client.get(path)
        assert refused.status_code == expected, refused.text
        assert owner_voice.text not in refused.text
        if "voice-messages" in path:
            assert refused.json()["voice_messages"] == []
    positive = await env.client.get(f"/v1/messages/{own_voice.message_id}/voice")
    assert positive.status_code == 200, positive.text
    admin_subject = await _create_and_login(env, role="admin")
    own_admin_voice = await _seed_message(env, owner_subject_id=admin_subject)
    other_actor_voice = await _seed_message(
        env, owner_subject_id=admin_subject, actor_id="actor_other_fixture"
    )
    admin_positive = await env.client.get(f"/v1/speech/messages/{owner_voice.message_id}")
    assert admin_positive.status_code == 200, admin_positive.text
    assert admin_positive.json()["message"]["text"] == owner_voice.text
    ordinary = await env.client.get(f"/v1/sessions/{own_admin_voice.window_id}/messages")
    assert ordinary.status_code == 200 and own_admin_voice.text not in ordinary.text
    refused = await env.client.get(f"/v1/speech/messages/{other_actor_voice.message_id}")
    assert refused.status_code == 404 and other_actor_voice.text not in refused.text


@pytest.mark.parametrize("path_kind", ["messages", "voice-messages", "voice", "backstage"])
async def test_invalid_projection_parameter_is_rejected(logged_in: TestEnv, path_kind: str) -> None:
    env = logged_in
    voice = await _seed_message(env)
    paths = {
        "messages": f"/v1/sessions/{voice.window_id}/messages",
        "voice-messages": f"/v1/sessions/{voice.window_id}/voice-messages",
        "voice": f"/v1/messages/{voice.message_id}/voice",
        "backstage": f"/v1/speech/messages/{voice.message_id}",
    }
    response = await env.client.get(paths[path_kind], params={"view": "invalid-fixture-view"})
    assert response.status_code == 422, response.text
    assert voice.text not in response.text


@pytest.mark.parametrize("before_sequence", [None, 1])
async def test_active_voice_partial_stays_hidden_while_active_text_is_visible(
    logged_in: TestEnv,
    before_sequence: int | None,
) -> None:
    env = logged_in
    voice = await _seed_message(env, speech_state="synthesizing", active=True)
    plain = await _seed_message(env, modality="text", active=True)
    params = {"before_sequence": before_sequence, "limit": 1} if before_sequence is not None else {}
    text_response = await env.client.get(f"/v1/sessions/{plain.window_id}/messages", params=params)
    assert text_response.status_code == 200, text_response.text
    assert text_response.json()["active_turn"]["partial_text"] == plain.text
    voice_response = await env.client.get(f"/v1/sessions/{voice.window_id}/messages", params=params)
    assert voice_response.status_code == 200, voice_response.text
    assert voice_response.json()["active_turn"]["turn_id"] == voice.turn_id
    assert voice.text not in voice_response.text


async def test_voice_playback_uses_the_asset_explicitly_selected_by_the_message(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    voice = await _seed_message(env)
    selected_audio = _wav_bytes(scale=30)
    async with session_scope() as session:
        media = GeneratedMediaService(
            session,
            data_dir=env.settings.data_dir,
            root_key=load_signing_root_key(env.settings),
            actor_id=env.settings.actor_id,
        )
        selected = await media.save_bytes(
            selected_audio,
            actor_id=env.settings.actor_id,
            window_id=voice.window_id,
            subject_id=env.subject_id,
            message_id=voice.message_id,
            fmt="wav",
            mime_type="audio/wav",
            sample_rate=16000,
            duration_ms=100,
            purpose="tts_output",
            producer="replacement_fixture",
        )
        row = await session.get(Message, voice.message_id)
        assert row is not None and selected.asset_id != voice.audio_asset_id
        row.audio_asset_id = selected.asset_id
    messages = await env.client.get(f"/v1/sessions/{voice.window_id}/messages")
    assert messages.status_code == 200, messages.text
    assert messages.json()["messages"][0]["audio_asset_id"] == selected.asset_id
    assert voice.text not in messages.text
    playback = await env.client.get(f"/v1/messages/{voice.message_id}/voice")
    assert playback.status_code == 200, playback.text
    audio = await env.client.get(playback.json()["url"])
    assert audio.status_code == 200, audio.text
    assert audio.content == selected_audio


VOICE_PROVIDER = {
    "provider_id": "voice_tts_fixture",
    "kind": "minimax_tts",
    "base_url": "https://tts.example.test",
    "enabled": True,
    "models": [{"model_id": "speech-2.8-hd"}],
}
MODEL_PROVIDER = {
    "provider_id": "voice_model_fixture",
    "kind": "openai_compatible",
    "base_url": "https://model.example.test/v1",
    "enabled": True,
    "extra": {"native_tools": True},
    "models": [{"model_id": "fixture-chat"}],
}


def _model_sse(deltas: list[dict] | dict, finish_reason: str) -> str:
    parts = deltas if isinstance(deltas, list) else [deltas]
    chunks = []
    for index, delta in enumerate(parts):
        finish = finish_reason if index == len(parts) - 1 else None
        chunks.append(
            "data: "
            + json.dumps(
                {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                ensure_ascii=False,
            )
            + "\n\n"
        )
    return "".join(chunks) + "data: [DONE]\n\n"


class _VoiceModelEndpoint:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[dict] = []
        self.script: list[tuple[list[dict] | dict, str]] = []

    async def handle(self, request) -> object:  # noqa: ANN001,ANN201
        import httpx

        body = json.loads(request.content)
        self.requests.append(body)
        if self.script:
            deltas, finish = self.script.pop(0)
        else:
            deltas, finish = {"content": self.reply}, "stop"
        return httpx.Response(
            200,
            text=_model_sse(deltas, finish),
            headers={"Content-Type": "text/event-stream"},
        )


class _VoiceTtsEndpoint:
    def __init__(
        self, *, fail: bool = False, pause: bool = False, cancel_after_start: bool = False
    ) -> None:
        self.calls: list[dict] = []
        self.on_request: Callable[[dict], Awaitable[None]] | None = None
        self.fail = fail
        self.cancel_after_start = cancel_after_start
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not pause:
            self.release.set()

    async def handle(self, request) -> object:  # noqa: ANN001,ANN201
        import httpx

        body = json.loads(request.content)
        self.calls.append(body)
        self.started.set()
        if self.on_request is not None:
            await self.on_request(body)
        await self.release.wait()
        if self.cancel_after_start:
            raise asyncio.CancelledError
        if self.fail:
            return httpx.Response(503, text="fixture TTS unavailable")
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0, "status_msg": ""},
                "data": {"audio": _wav_bytes().hex(), "status": 2},
                "extra_info": {"audio_length": 800, "audio_sample_rate": 16000},
            },
        )


async def _publish_voice_settings(
    env: TestEnv,
    *,
    with_tts: bool = True,
    with_voice: bool = True,
    tools_enabled: bool = False,
) -> None:
    providers = [MODEL_PROVIDER]
    profiles = [
        {
            "profile": "dialogue.fast",
            "provider_id": "voice_model_fixture",
            "model_id": "fixture-chat",
        }
    ]
    if with_tts:
        providers.append(VOICE_PROVIDER)
        profiles.append(
            {
                "profile": "speech.tts",
                "provider_id": "voice_tts_fixture",
                "model_id": "speech-2.8-hd",
            }
        )
    dialogue: dict = {
        "tools_enabled": tools_enabled,
        # Keep autonomy's model call out of the scripted tool-turn response order.
        "private_may_stay_silent": False,
    }
    if with_voice:
        dialogue["voice"] = {"kind": "minimax_tts", "voice_id": "fixture-voice"}
    draft = await env.client.put(
        "/v1/settings/draft",
        json={"providers": providers, "profiles": profiles, "dialogue": dialogue},
    )
    assert draft.status_code == 200, draft.text
    errors = [item for item in draft.json()["validation_errors"] if item["severity"] == "error"]
    assert not errors, errors
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text
    if with_tts:
        secret = await env.client.put(
            "/v1/settings/providers/voice_tts_fixture/secret",
            json={"secret": "fixture-tts-secret"},
        )
        assert secret.status_code == 200, secret.text


def _install_voice_runtime(
    env: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    model: _VoiceModelEndpoint,
    tts: _VoiceTtsEndpoint,
) -> None:
    import httpx

    import ling.worker as worker_module
    from ling.models import minimax_tts, openai_compatible
    from ling.models.registry import ProviderRegistry
    from ling.models.router import ModelRouter

    model_client = partial(httpx.AsyncClient, transport=httpx.MockTransport(model.handle))
    model_http = SimpleNamespace(**(vars(httpx) | {"AsyncClient": model_client}))
    monkeypatch.setattr(openai_compatible, "httpx", model_http)
    tts_client = partial(httpx.AsyncClient, transport=httpx.MockTransport(tts.handle))
    tts_http = SimpleNamespace(**(vars(httpx) | {"AsyncClient": tts_client}))
    monkeypatch.setattr(minimax_tts, "httpx", tts_http)

    router = ModelRouter(ProviderRegistry(lambda _provider_id: "fixture-model-secret"))
    env.app.state.router = router
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)


async def _start_web_turn(
    env: TestEnv,
    *,
    text: str = "请回答",
    client_turn_id: str | None = None,
) -> tuple[str, list[tuple[str, dict]]]:
    created = await env.client.post("/v1/sessions", json={"title": "语音入口验收"})
    assert created.status_code == 201, created.text
    window_id = created.json()["session_id"]
    response = await env.client.post(
        f"/v1/sessions/{window_id}/turns",
        json={"client_turn_id": client_turn_id or f"voice-{uuid.uuid4().hex}", "text": text},
    )
    assert response.status_code == 200, response.text
    return window_id, await read_sse(response)


def _event_text(events: list[tuple[str, dict]]) -> str:
    return json.dumps(events, ensure_ascii=False, sort_keys=True)


async def _actor_messages(window_id: str) -> list[Message]:
    async with session_scope() as session:
        rows = await session.execute(
            select(Message).where(Message.window_id == window_id, Message.role == "actor")
        )
        return list(rows.scalars())


def _completed_message(events: list[tuple[str, dict]]) -> dict:
    completed = [payload for name, payload in events if name == "turn.completed"]
    assert completed, events
    assert completed[-1].get("message"), completed[-1]
    return completed[-1]["message"]


async def _assert_audio_playback(env: TestEnv, message_id: str, expected: bytes) -> None:
    playback = await env.client.get(f"/v1/messages/{message_id}/voice")
    assert playback.status_code == 200, playback.text
    response = await env.client.get(playback.json()["url"])
    assert response.status_code == 200, response.text
    assert response.content == expected
    with wave.open(io.BytesIO(response.content), "rb") as audio:
        assert audio.getframerate() == 16000


async def test_web_final_speak_is_one_hidden_voice_message_with_playback(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "这是最终语音正文 web-final-voice。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)

    window_id, events = await _start_web_turn(env)
    participant_output = _event_text(events)
    message = _completed_message(events)

    assert len(tts.calls) == 1, f"预期一次 TTS，实际请求 {len(tts.calls)} 次"
    assert tts.calls[0]["voice_setting"]["voice_id"] == "fixture-voice"
    assert tts.calls[0]["text"] == canonical
    assert canonical not in participant_output, participant_output
    assert "[speak]" not in participant_output
    assert message["text"] == ""
    assert message["output_modality"] == "voice"
    assert message["speech_state"] == "ready"
    assert message["audio_asset_id"]

    stored = await _actor_messages(window_id)
    assert len(stored) == 1, stored
    assert stored[0].id == message["message_id"]
    assert stored[0].text == canonical
    assert stored[0].audio_asset_id == message["audio_asset_id"]
    ordinary = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    assert canonical not in ordinary.text
    assert ordinary.json()["messages"][-1]["message_id"] == message["message_id"]
    operator = await env.client.get(f"/v1/speech/messages/{message['message_id']}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == canonical
    assert operator.json()["message"]["message_id"] == message["message_id"]
    await _assert_audio_playback(env, message["message_id"], _wav_bytes())


async def test_published_voice_capability_does_not_convert_plain_text_reply(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "普通文字仍然逐字可见。"
    model = _VoiceModelEndpoint(canonical)
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)

    window_id, events = await _start_web_turn(env)
    message = _completed_message(events)
    assert not tts.calls, tts.calls
    assert canonical in _event_text(events)
    assert message["text"] == canonical
    assert message["output_modality"] == "text"
    stored = await _actor_messages(window_id)
    assert len(stored) == 1 and stored[0].text == canonical


@pytest.mark.parametrize("missing_part", ["binding", "voice"])
async def test_speak_without_usable_tts_is_rejected_without_text_leak(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    missing_part: str,
) -> None:
    env = logged_in
    canonical = f"拒绝语音后也不补发原文-{missing_part}"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(
        env,
        with_tts=missing_part != "binding",
        with_voice=missing_part != "voice",
    )

    window_id, events = await _start_web_turn(env)
    message = _completed_message(events)
    assert not tts.calls, tts.calls
    assert canonical not in _event_text(events)
    assert message["output_modality"] == "voice"
    assert message["text"] == ""
    assert message["audio_asset_id"] is None
    assert message["speech_state"] == "failed"
    stored = await _actor_messages(window_id)
    assert len(stored) == 1 and stored[0].text == canonical
    assert stored[0].output_modality == "voice"
    assert stored[0].speech_state == "failed"
    history = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert history.status_code == 200, history.text
    assert canonical not in history.text
    assert history.json()["messages"][-1]["text"] == ""
    assert history.json()["messages"][-1]["speech_state"] == "failed"
    operator = await env.client.get(f"/v1/speech/messages/{message['message_id']}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == canonical


async def test_tool_candidate_speak_is_not_synthesized_or_exposed(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    candidate = "TOOL_CANDIDATE_PRIVATE [speak] must stay internal"
    canonical = "最终工具结果后的语音正文 tool-final-voice。"
    model = _VoiceModelEndpoint("")
    model.script = [
        (
            [
                {"content": candidate},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_voice_acceptance",
                            "type": "function",
                            "function": {
                                "name": "clock_now",
                                "arguments": '{"tz": "Asia/Shanghai"}',
                            },
                        }
                    ],
                },
                {"content": "候选正文不能越过工具结果直接发布。"},
            ],
            "tool_calls",
        ),
        ({"content": f"[speak] {canonical}"}, "stop"),
    ]
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env, tools_enabled=True)

    window_id, events = await _start_web_turn(env, text="帮我查一下时间再回答")
    message = _completed_message(events)
    assert len(model.requests) == 2, model.requests
    continuation_messages = model.requests[1]["messages"]
    assistant_calls = [
        call
        for item in continuation_messages
        if item.get("role") == "assistant"
        for call in item.get("tool_calls", [])
        if call.get("id") == "call_voice_acceptance"
    ]
    tool_results = [item for item in continuation_messages if item.get("role") == "tool"]
    assert len(assistant_calls) == 1, continuation_messages
    assert len(tool_results) == 1, continuation_messages
    assert tool_results[0]["tool_call_id"] == "call_voice_acceptance"
    assert str(tool_results[0].get("content") or "").strip(), continuation_messages
    assistant_position = next(
        index
        for index, item in enumerate(continuation_messages)
        if item.get("role") == "assistant"
        and any(call.get("id") == "call_voice_acceptance" for call in item.get("tool_calls", []))
    )
    tool_result_position = continuation_messages.index(tool_results[0])
    assert assistant_position < tool_result_position, continuation_messages
    assert len(tts.calls) == 1, tts.calls
    assert tts.calls[0]["text"] == canonical
    assert candidate not in _event_text(events)
    assert canonical not in _event_text(events)
    assert message["output_modality"] == "voice" and message["text"] == ""
    stored = await _actor_messages(window_id)
    assert len(stored) == 1 and stored[0].text == canonical


async def test_tts_failure_keeps_voice_state_and_does_not_restore_canonical_text(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "供应商失败时只显示语音失败状态。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint(fail=True)
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)

    window_id, events = await _start_web_turn(env)
    message = _completed_message(events)
    assert len(tts.calls) == 1
    assert message["output_modality"] == "voice"
    assert message["speech_state"] == "failed"
    assert message["audio_asset_id"] is None
    assert message["text"] == ""
    assert canonical not in _event_text(events)
    ordinary = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert ordinary.status_code == 200 and canonical not in ordinary.text
    operator = await env.client.get(f"/v1/speech/messages/{message['message_id']}")
    assert operator.status_code == 200
    assert operator.json()["message"]["text"] == canonical


@pytest.mark.parametrize("speech_state", ["ready", "failed", "cancelled"])
async def test_web_voice_reply_behavior_log_matches_state_and_is_restricted(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    speech_state: str,
) -> None:
    from ling.logging_setup import clear_log_buffer

    env = logged_in
    canonical = f"行为日志保留原话-{speech_state}-{uuid.uuid4().hex[:8]}"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint(
        fail=speech_state == "failed",
        cancel_after_start=speech_state == "cancelled",
    )
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    clear_log_buffer()

    with caplog.at_level(logging.INFO, logger="ling"):
        window_id, events = await _start_web_turn(env)

    assert len(tts.calls) == 1, tts.calls
    stored = await _actor_messages(window_id)
    assert len(stored) == 1
    message = stored[0]
    assert message.text == canonical
    assert message.output_modality == "voice"
    assert message.speech_state == speech_state
    assert canonical not in _event_text(events)
    history = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert history.status_code == 200, history.text
    assert canonical not in history.text
    assert history.json()["messages"][-1]["text"] == ""

    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "speech_reply"
        and getattr(record, "attributes", {}).get("message_id") == message.id
    ]
    assert len(records) == 1, [record.getMessage() for record in caplog.records]
    record = records[0]
    assert f"[语音] {canonical}" in record.getMessage()
    attributes = record.attributes
    assert attributes["log_kind"] == "behavior"
    assert attributes["provider_id"] == "voice_tts_fixture"
    assert attributes["speech_state"] == speech_state
    assert "speech_delivery_state" in attributes
    assert attributes["speech_delivery_state"] == message.speech_delivery_state
    if speech_state == "ready":
        assert attributes["speech_delivery_state"] == "delivered"
    else:
        assert attributes["speech_delivery_state"] is None

    def attribute_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return {str(key).lower() for key in value} | {
                key for child in value.values() for key in attribute_keys(child)
            }
        if isinstance(value, list):
            return {key for child in value for key in attribute_keys(child)}
        return set()

    forbidden_fields = {
        "secret", "api_key", "authorization", "access_token", "request",
        "request_body", "request_payload", "raw_payload", "payload", "audio",
        "audio_bytes", "audio_data", "base64", "data_url", "voice_setting",
    }
    assert not forbidden_fields.intersection(attribute_keys(attributes))
    assert canonical not in json.dumps(attributes, ensure_ascii=False)
    sensitive_values = (
        "fixture-tts-secret",
        "fixture-model-secret",
        "base64://",
        "data:audio/",
        _wav_bytes().hex(),
        base64.b64encode(_wav_bytes()).decode(),
    )
    captured = record.getMessage() + json.dumps(attributes, ensure_ascii=False)
    for value in sensitive_values:
        assert value not in captured

    for role in ("owner", "admin"):
        if role == "admin":
            await _create_and_login(env, role=role)
        response = await env.client.get(
            "/v1/logs", params={"kind": "behavior", "q": canonical, "limit": 1000}
        )
        assert response.status_code == 200, response.text
        log_items = [
            item for item in response.json()["items"]
            if item.get("event_name") == "speech_reply"
            and item.get("attributes", {}).get("message_id") == message.id
        ]
        assert len(log_items) == 1, response.text
        assert log_items[0]["kind"] == "behavior"
        assert f"[语音] {canonical}" in log_items[0]["message"]
        assert log_items[0]["attributes"]["provider_id"] == "voice_tts_fixture"
        assert log_items[0]["attributes"]["speech_state"] == speech_state
        assert log_items[0]["attributes"]["speech_delivery_state"] == message.speech_delivery_state
        for value in sensitive_values:
            assert value not in response.text

    await _create_and_login(env, role="member")
    refused = await env.client.get("/v1/logs", params={"kind": "behavior", "q": canonical})
    assert refused.status_code == 403, refused.text
    assert canonical not in refused.text


async def test_same_web_client_turn_retry_reuses_message_and_tts_result(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "幂等重试不能重复合成。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    client_turn_id = f"voice-retry-{uuid.uuid4().hex}"

    window_id, first_events = await _start_web_turn(env, client_turn_id=client_turn_id)
    second = await env.client.post(
        f"/v1/sessions/{window_id}/turns",
        json={"client_turn_id": client_turn_id, "text": "请回答"},
    )
    assert second.status_code == 200, second.text
    second_events = await read_sse(second)
    first_message = _completed_message(first_events)
    second_message = _completed_message(second_events)
    assert first_message["message_id"] == second_message["message_id"]
    assert len(tts.calls) == 1, f"同轮重试触发了 {len(tts.calls)} 次供应商调用"
    assert len(await _actor_messages(window_id)) == 1


async def test_voice_text_budget_is_checked_before_calling_tts(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "预算边界" * 251
    assert len(canonical) > 1000
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)

    window_id, events = await _start_web_turn(env)
    message = _completed_message(events)
    assert not tts.calls, "超出单次语音文本预算仍然调用了供应商"
    assert message["output_modality"] == "voice"
    assert message["speech_state"] == "failed"
    assert message["text"] == ""
    assert canonical not in _event_text(events)
    stored = await _actor_messages(window_id)
    assert len(stored) == 1 and stored[0].text == canonical


async def test_stop_during_tts_discards_late_audio_without_text_fallback(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "停止期间返回的迟到音频不能发布。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint(pause=True)
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    created = await env.client.post("/v1/sessions", json={"title": "语音取消验收"})
    assert created.status_code == 201, created.text
    window_id = created.json()["session_id"]
    response_task = asyncio.create_task(
        env.client.post(
            f"/v1/sessions/{window_id}/turns",
            json={"client_turn_id": f"voice-stop-{uuid.uuid4().hex}", "text": "请回答"},
        )
    )
    await asyncio.wait_for(tts.started.wait(), timeout=10)
    async with session_scope() as session:
        turn = await session.scalar(select(Turn).where(Turn.window_id == window_id))
        assert turn is not None
        turn_id = turn.id
    stopped = await env.client.post(f"/v1/turns/{turn_id}/stop")
    assert stopped.status_code == 200, stopped.text
    tts.release.set()
    response = await asyncio.wait_for(response_task, timeout=20)
    events = await read_sse(response)
    message = _completed_message(events)
    assert len(tts.calls) == 1
    assert canonical not in _event_text(events)
    assert message["output_modality"] == "voice"
    assert message["speech_state"] == "cancelled"
    assert message["audio_asset_id"] is None
    assert message["text"] == ""
    assert (
        not await _actor_messages(window_id)
        or (await _actor_messages(window_id))[0].speech_state == "cancelled"
    )


async def test_cancelled_persisted_synthesis_is_uncertain_and_not_retried(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "供应商调用中断后的同一消息不能重复计费。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint(cancel_after_start=True)
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)
    created = await env.client.post("/v1/sessions", json={"title": "合成取消作业验收"})
    assert created.status_code == 201, created.text
    window_id = created.json()["session_id"]
    client_turn_id = f"voice-cancel-job-{uuid.uuid4().hex}"
    observed_jobs: list[tuple[str | None, str | None, str | None]] = []

    async def observe_committed_job(body: dict) -> None:
        async with session_scope() as session:
            message = await session.scalar(
                select(Message).where(
                    Message.text == body["text"],
                    Message.output_modality == "voice",
                )
            )
            job = None
            if message is not None:
                job = await session.scalar(
                    select(SpeechSynthesisJob).where(
                        SpeechSynthesisJob.message_id == message.id
                    )
                )
            observed_jobs.append(
                (
                    message.id if message is not None else None,
                    job.id if job is not None else None,
                    job.state if job is not None else None,
                )
            )

    tts.on_request = observe_committed_job

    response = await env.client.post(
        f"/v1/sessions/{window_id}/turns",
        json={"client_turn_id": client_turn_id, "text": "请回答"},
    )
    events = await read_sse(response)
    terminal = next(
        payload
        for name, payload in events
        if name in {"turn.completed", "turn.stopped"} and payload.get("message")
    )
    message = terminal["message"]
    assert len(tts.calls) == 1, tts.calls
    assert len(observed_jobs) == 1, observed_jobs
    assert observed_jobs[0][0] == message["message_id"]
    assert observed_jobs[0][1] is not None, (
        "供应商请求开始前未观察到已提交的合成任务"
    )
    assert observed_jobs[0][2] not in {None, "succeeded", "failed", "uncertain", "cancelled"}
    assert message["output_modality"] == "voice"
    assert message["speech_state"] == "cancelled"
    assert message["text"] == ""
    assert canonical not in _event_text(events)

    async with session_scope() as session:
        job = await session.scalar(
            select(SpeechSynthesisJob).where(SpeechSynthesisJob.message_id == message["message_id"])
        )
        assert job is not None
        assert job.state == "uncertain"
        assert job.usage_source == "unknown"
        synthesis_key = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == SYNTHESIS_SCOPE,
                IdempotencyKey.key.like(f"%:{message['message_id']}"),
            )
        )
        assert synthesis_key is not None
        assert decode_state(synthesis_key.result_ref)[0] is SynthesisState.uncertain

    replay = await env.client.post(
        f"/v1/sessions/{window_id}/turns",
        json={"client_turn_id": client_turn_id, "text": "请回答"},
    )
    assert replay.status_code == 200, replay.text
    replay_events = await read_sse(replay)
    assert len(tts.calls) == 1, f"同一消息重试再次调用 TTS: {tts.calls}"
    assert replay_events
    replay_messages = await _actor_messages(window_id)
    assert len(replay_messages) == 1
    assert replay_messages[0].id == message["message_id"]
    async with session_scope() as session:
        synthesis_key = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == SYNTHESIS_SCOPE,
                IdempotencyKey.key.like(f"%:{message['message_id']}"),
            )
        )
        assert synthesis_key is not None
        assert decode_state(synthesis_key.result_ref)[0] is SynthesisState.uncertain
    ordinary = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    assert canonical not in ordinary.text


async def test_onebot_http_reply_delivers_one_record_without_canonical_text(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    canonical = "OneBot 直回只发送一条语音。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint()
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env)

    from connectors.onebot import voice as onebot_voice
    from connectors.onebot.silk import SilkPayload

    from tests.integration.test_onebot_loop import (
        FakeOneBot,
        _bind,
        _event_body,
        _install,
        _post,
    )

    class _FixtureSilkCodec:
        def to_silk(self, _source):  # noqa: ANN001,ANN201
            return SilkPayload(
                data=b"\x02#!SILK_V3" + b"fixture",
                duration_seconds=0.1,
                source_duration_seconds=0.1,
            )

    monkeypatch.setattr(onebot_voice, "default_codec", _FixtureSilkCodec)
    platform = FakeOneBot()
    _install(env, platform)
    await _bind(env)

    response = await _post(env, _event_body())
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True, response.text
    assert response.json()["delivered"] is True, response.text
    assert len(tts.calls) == 1
    assert len(platform.sent) == 1, platform.sent
    delivery = platform.sent[0]
    assert delivery["action"] == "send_private_msg"
    segments = delivery["body"]["message"]
    assert len(segments) == 1
    assert segments[0]["type"] == "record"
    assert segments[0]["data"]["file"].startswith("base64://")
    assert canonical not in json.dumps(platform.sent, ensure_ascii=False)

    async with session_scope() as session:
        window = await session.scalar(
            select(Window).where(
                Window.channel == "onebot",
                Window.subject_id == env.subject_id,
            )
        )
        assert window is not None
        message = await session.scalar(
            select(Message).where(
                Message.window_id == window.id,
                Message.role == "actor",
            )
        )
        assert message is not None
        message_id = message.id
        assert message.text == canonical
        assert message.output_modality == "voice"
        assert message.speech_state == "ready"
        assert message.audio_asset_id

    ordinary = await env.client.get(f"/v1/sessions/{window.id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    assert canonical not in ordinary.text
    operator = await env.client.get(f"/v1/speech/messages/{message_id}")
    assert operator.status_code == 200, operator.text
    assert operator.json()["message"]["text"] == canonical


@pytest.mark.parametrize("tts_configured", [True, False], ids=["provider-failure", "missing-tts"])
async def test_deferred_web_voice_failure_does_not_send_text_fallback(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    tts_configured: bool,
) -> None:
    env = logged_in
    canonical = "延后回复的语音合成失败后也不补发正文。"
    model = _VoiceModelEndpoint(f"[speak] {canonical}")
    tts = _VoiceTtsEndpoint(fail=True)
    _install_voice_runtime(env, monkeypatch, model, tts)
    await _publish_voice_settings(env, with_tts=tts_configured)

    import ling.worker as worker_module
    from ling.domains.deferred import service as deferred

    monkeypatch.setattr(worker_module, "get_settings", lambda: env.settings)

    moment = datetime.now(UTC) - timedelta(seconds=2)
    source_turn_id = f"voice-deferred-{uuid.uuid4().hex}"
    async with session_scope() as session:
        conversation = ConversationService(
            session,
            actor_id=env.settings.actor_id,
            persona_id=env.settings.persona_id,
        )
        window = await conversation.create_window(
            subject_id=env.subject_id,
            title="延后语音验收",
            channel="web",
        )
        user_message = await conversation.add_message(
            window.id,
            role="user",
            text="请稍后回答这个问题。",
        )
        row = await deferred.schedule(
            session,
            actor_id=env.settings.actor_id,
            window_id=window.id,
            subject_id=env.subject_id,
            source_turn_id=source_turn_id,
            source_message_id=user_message.message_id,
            input_sequence=user_message.sequence,
            applied_delay_ms=1000,
            requested_delay_ms=1000,
            clamped=False,
            now=moment,
        )
        assert row is not None
        reply_id = row.id
        window_id = window.id

    await worker_module.handle_deferred_reply_sweep({})

    actors = await _actor_messages(window_id)
    async with session_scope() as session:
        stored = await session.get(DeferredReply, reply_id)
        turn = await session.scalar(
            select(Turn).where(Turn.client_turn_id == f"deferred:{reply_id}")
        )
    diagnostic = {
        "model_request_count": len(model.requests),
        "tts_call_count": len(tts.calls),
        "turn": None
        if turn is None
        else {
            "state": turn.state,
            "error_code": turn.error_code,
            "error_detail": turn.error_detail,
        },
        "deferred": None
        if stored is None
        else {
            "state": stored.state,
            "reason": stored.reason,
            "fallback_used": stored.fallback_used,
            "delivered_turn_id": stored.delivered_turn_id,
            "delivered_message_id": stored.delivered_message_id,
        },
        "actor_messages": [
            {
                "text": item.text,
                "output_modality": item.output_modality,
                "speech_state": item.speech_state,
            }
            for item in actors
        ],
    }
    assert len(model.requests) == 1, diagnostic
    assert len(tts.calls) == int(tts_configured), json.dumps(diagnostic, ensure_ascii=False)
    assert turn is not None and turn.state == "completed", diagnostic
    assert len(actors) == 1
    assert actors[0].text == canonical
    assert actors[0].output_modality == "voice"
    assert actors[0].speech_state == "failed"
    assert actors[0].speech_delivery_state is None
    assert actors[0].audio_asset_id is None
    ordinary = await env.client.get(f"/v1/sessions/{window_id}/messages")
    assert ordinary.status_code == 200, ordinary.text
    assert canonical not in ordinary.text
    assert stored is not None, diagnostic
    assert stored.fallback_used is False
    assert stored.state == "failed", diagnostic
