"""Independent ASR acceptance through authenticated HTTP and real model requests.

The recognition provider supplies a unique phrase. The chat provider uses its
normal HTTP adapter, so an unconnected transcript or a placeholder cannot pass.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import uuid
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from ling.domains.speech import asr_runtime
from ling.infra.asr_jobs import AsrCallReservation, AsrJob
from ling.infra.db import session_scope
from ling.infra.models import Message, Turn
from ling.models import openai_compatible
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.speech import AsrResult, SpeechError, SpeechErrorKind, SpeechUsage
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web

_REAL_WORKER_LOOP = asr_runtime.worker_loop


def _audio(*, silent: bool = False) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", 0 if silent else round(500 * math.sin(index / 9)))
            for index in range(1600)
        ))
    return stream.getvalue()


async def _idle_worker(app: object) -> None:
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def deterministic_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTP creates durable work. Tests explicitly tick the production handler,
    # controlling cancellation boundaries without racing the background poller.
    monkeypatch.setattr(asr_runtime, "worker_loop", _idle_worker)


@dataclass
class Recognition:
    phrase: str = field(default_factory=lambda: f"接受短语-{uuid.uuid4().hex}")
    calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None
    error: SpeechError | None = None

    async def transcribe(self, request, *, call) -> AsrResult:
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return AsrResult(self.phrase, "zh", None, None, SpeechUsage())


class ChatWire:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        event = {"choices": [{"delta": {"content": "收到你的内容。"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    def inputs(self) -> list[str]:
        return [
            str(message.get("content", ""))
            for request in self.requests
            for message in request["messages"]
            if message["role"] == "user"
        ]


async def _setup(
    env: TestEnv, monkeypatch: pytest.MonkeyPatch, *, daily_seconds: float = 0,
) -> tuple[str, str, Recognition, ChatWire]:
    granted = await env.client.put("/v1/consents", json={"scope": "media.inbound", "granted": True})
    assert granted.status_code == 200, granted.text
    draft = await env.client.put("/v1/settings/draft", json={
        "providers": [
            {"provider_id": "accept-asr", "kind": "siliconflow_asr",
             "base_url": "https://asr.example.test/v1", "enabled": True,
             "extra": {"asr_daily_seconds": daily_seconds}, "models": [{"model_id": "recognizer"}]},
            {"provider_id": "accept-chat", "kind": "openai_compatible",
             "base_url": "https://chat.example.test/v1", "enabled": True,
             "models": [{"model_id": "chat"}]},
        ],
        "profiles": [
            {"profile": "speech.asr", "provider_id": "accept-asr", "model_id": "recognizer"},
            {"profile": "dialogue.fast", "provider_id": "accept-chat", "model_id": "chat"},
        ],
        "dialogue": {"private_may_stay_silent": False},
    })
    assert draft.status_code == 200, draft.text
    published = await env.client.post("/v1/settings/publish", json={"revision": draft.json()["revision"]})
    assert published.status_code == 200, published.text
    window = await env.client.post("/v1/sessions", json={})
    assert window.status_code == 201, window.text
    window_id = window.json()["session_id"]
    upload = await env.client.post(
        f"/v1/sessions/{window_id}/audio-inputs",
        files={"file": ("voice.wav", _audio(), "audio/wav")},
    )
    assert upload.status_code == 201, upload.text
    recognizer = Recognition()

    async def build(config, *, secret_reader):
        return SimpleNamespace(ok=True, reason="", provider=recognizer,
                               provider_id="accept-asr", model_id="recognizer")

    monkeypatch.setattr("ling.domains.speech.asr_providers.build_asr_provider", build)
    wire = ChatWire()
    client_factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(wire.handle))
    monkeypatch.setattr(openai_compatible, "httpx", SimpleNamespace(**(vars(httpx) | {"AsyncClient": client_factory})))
    env.app.state.router = ModelRouter(ProviderRegistry(lambda _: "fixture-chat-key"))
    return window_id, upload.json()["asset_id"], recognizer, wire


async def _create(env: TestEnv, window: str, asset: str, **changes) -> dict:
    response = await env.client.post(f"/v1/sessions/{window}/transcriptions", json={
        "asset_id": asset, "client_request_id": "acceptance-request", "mode": "send", **changes,
    })
    assert response.status_code == 202, response.text
    return response.json()


async def _state(env: TestEnv, window: str, job_id: str) -> dict:
    response = await env.client.get(f"/v1/sessions/{window}/transcriptions/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _messages(window: str) -> list[Message]:
    async with session_scope() as session:
        return list((await session.scalars(select(Message).where(Message.window_id == window).order_by(Message.sequence))).all())


async def test_send_uses_recognized_phrase_in_chat_http_and_is_idempotent(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset)
    duplicate = await _create(logged_in, window, asset)
    assert duplicate["job_id"] == created["job_id"]
    assert await asr_runtime.process_job(logged_in.app, created["job_id"])
    state = await _state(logged_in, window, created["job_id"])
    assert state["state"] == "succeeded", state
    assert state["submission"] == "committed", state
    assert any(recognition.phrase in text for text in wire.inputs()), wire.requests
    rows = await _messages(window)
    user_rows = [row for row in rows if row.role == "user"]
    assert len(user_rows) == 1
    assert user_rows[0].text == recognition.phrase
    assert any(row.role == "actor" for row in rows)
    await asr_runtime.submit_job(logged_in.app, created["job_id"])
    async with session_scope() as session:
        turns = list((await session.scalars(select(Turn).where(Turn.window_id == window))).all())
    assert len(turns) == 1
    assert recognition.calls == 1
    assert state["result"]["audio"][0]["confidence"] is None


async def test_draft_waits_for_http_commit_and_uses_user_edit(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset, mode="draft")
    await asr_runtime.process_job(logged_in.app, created["job_id"])
    state = await _state(logged_in, window, created["job_id"])
    assert state["text"] == recognition.phrase
    assert wire.requests == []
    assert await _messages(window) == []
    edited = f"用户校正-{uuid.uuid4().hex}"
    stale = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{created['job_id']}/commit",
        json={"result_revision": state["revision"] - 1, "text": edited},
    )
    assert stale.status_code == 409, stale.text
    committed = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{created['job_id']}/commit",
        json={"result_revision": state["revision"], "text": edited},
    )
    assert committed.status_code == 200, committed.text
    await asr_runtime.submit_job(logged_in.app, created["job_id"], automatic=True)
    assert any(edited in text for text in wire.inputs()), wire.requests
    assert not any(recognition.phrase in text for text in wire.inputs())
    assert [row.text for row in await _messages(window) if row.role == "user"] == [edited]


@pytest.mark.parametrize("boundary", ["revoke", "cancel"])
async def test_inflight_result_cannot_reply_after_revoke_or_cancel(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    recognition.release = asyncio.Event()
    created = await _create(logged_in, window, asset)
    task = asyncio.create_task(asr_runtime.process_job(logged_in.app, created["job_id"]))
    try:
        await asyncio.wait_for(recognition.started.wait(), timeout=10)
        if boundary == "revoke":
            response = await logged_in.client.put("/v1/consents", json={"scope": "media.inbound", "granted": False})
            assert response.status_code == 200, response.text
        else:
            response = await logged_in.client.delete(f"/v1/sessions/{window}/transcriptions/{created['job_id']}")
            assert response.status_code == 200, response.text
    finally:
        recognition.release.set()
        await asyncio.wait_for(task, timeout=15)
    assert wire.requests == []
    rows = await _messages(window)
    assert not any(row.role == "actor" for row in rows)
    assert not any(recognition.phrase in row.text for row in rows)
    async with session_scope() as session:
        job = await session.get(AsrJob, created["job_id"])
        assert job.submission != "committed"
        assert job.text == ""


async def test_late_result_updates_original_without_starting_another_reply(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    recognition.release = asyncio.Event()
    created = await _create(logged_in, window, asset)
    task = asyncio.create_task(asr_runtime.process_job(logged_in.app, created["job_id"]))
    try:
        await asyncio.wait_for(recognition.started.wait(), timeout=10)
        later_text = f"后来消息-{uuid.uuid4().hex}"
        later = await logged_in.client.post(f"/v1/sessions/{window}/turns", json={
            "client_turn_id": f"later-{uuid.uuid4().hex}", "text": later_text,
        })
        assert later.status_code == 200, later.text
        events = await read_sse(later)
        assert any(event == "turn.completed" for event, _ in events), events
        calls_before_completion = len(wire.requests)
    finally:
        recognition.release.set()
        await asyncio.wait_for(task, timeout=15)
    assert len(wire.requests) == calls_before_completion
    users = [row for row in await _messages(window) if row.role == "user"]
    assert [row.text for row in users] == [recognition.phrase, later_text]


async def test_unknown_call_keeps_budget_reservation_for_retry(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch, daily_seconds=0.15)
    recognition.error = SpeechError(SpeechErrorKind.timeout, "fixture timeout", retriable=True)
    created = await _create(logged_in, window, asset)
    await asr_runtime.process_job(logged_in.app, created["job_id"])
    first = await _state(logged_in, window, created["job_id"])
    assert first["state"] == "unknown", first
    assert first["charged_seconds"] > 0
    retried = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions/{created['job_id']}/retry")
    assert retried.status_code == 202, retried.text
    recognition.error = None
    await asr_runtime.process_job(logged_in.app, created["job_id"])
    second = await _state(logged_in, window, created["job_id"])
    assert second["error"]["code"] == "quota", second
    assert recognition.calls == 1
    assert second["charged_seconds"] == first["charged_seconds"]
    assert wire.requests == []


async def test_silent_attachment_preserves_mixed_text_as_one_input(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _, recognition, wire = await _setup(logged_in, monkeypatch)
    silent = await logged_in.client.post(f"/v1/sessions/{window}/audio-inputs", files={
        "file": ("silence.wav", _audio(silent=True), "audio/wav"),
    })
    assert silent.status_code == 201, silent.text
    text = f"正文仍须进入对话-{uuid.uuid4().hex}"
    created = await _create(logged_in, window, silent.json()["asset_id"], text=text)
    await asr_runtime.process_job(logged_in.app, created["job_id"])
    assert recognition.calls == 0
    assert any(text in content for content in wire.inputs()), wire.requests
    assert len([row for row in await _messages(window) if row.role == "user"]) == 1


async def test_recognition_failure_still_submits_mixed_text(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    recognition.error = SpeechError(SpeechErrorKind.invalid_audio, "fixture decode failure")
    text = f"保留可读文字-{uuid.uuid4().hex}"
    created = await _create(logged_in, window, asset, text=text)
    await asr_runtime.process_job(logged_in.app, created["job_id"])

    state = await _state(logged_in, window, created["job_id"])
    assert state["state"] == "failed", state
    assert state["submission"] == "committed", state
    assert any(text in content for content in wire.inputs()), wire.requests
    assert not any(recognition.phrase in content for content in wire.inputs())
    assert [row.text for row in await _messages(window) if row.role == "user"] == [text]


async def test_completed_draft_is_not_readable_after_media_revocation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, _ = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset, mode="draft")
    await asr_runtime.process_job(logged_in.app, created["job_id"])
    before = await _state(logged_in, window, created["job_id"])
    assert before["text"] == recognition.phrase
    revoke = await logged_in.client.put("/v1/consents", json={"scope": "media.inbound", "granted": False})
    assert revoke.status_code == 200, revoke.text
    after = await logged_in.client.get(f"/v1/sessions/{window}/transcriptions/{created['job_id']}")
    assert recognition.phrase not in after.text
    assert after.status_code in {200, 403, 404}, after.text
    if after.status_code == 200:
        assert after.json()["text"] == ""
        assert after.json()["result"] == {}


async def _run_worker_polls(app: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe two completed production recovery passes before stopping the loop."""
    finished = asyncio.Event()
    process_job = asr_runtime.process_job
    polls = 0

    async def observed_process(app, job_id=None):
        nonlocal polls
        result = await process_job(app, job_id)
        polls += 1
        if polls >= 2:
            finished.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(asr_runtime, "process_job", observed_process)
        task = asyncio.create_task(_REAL_WORKER_LOOP(app))
        try:
            await asyncio.wait_for(finished.wait(), timeout=20)
        finally:
            await asr_runtime.stop_worker(task)
    assert polls >= 2


@pytest.mark.parametrize("boundary", ["revoke", "cancel", "expire"])
async def test_edited_draft_is_cleared_after_authorization_or_cancellation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset, mode="draft")
    job_id = created["job_id"]
    await asr_runtime.process_job(logged_in.app, job_id)
    state = await _state(logged_in, window, job_id)
    assert state["text"] == recognition.phrase
    edited = f"需要清除的用户编辑-{uuid.uuid4().hex}"
    committed = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{job_id}/commit",
        json={"result_revision": state["revision"], "text": edited},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["text"] == edited
    async with session_scope() as session:
        assert (await session.get(AsrJob, job_id)).edited_text == edited
    assert wire.requests == []

    if boundary == "cancel":
        response = await logged_in.client.delete(f"/v1/sessions/{window}/transcriptions/{job_id}")
        assert response.status_code == 200, response.text
        assert response.json()["text"] == ""
        assert edited not in response.text
    else:
        consent = {"scope": "media.inbound", "granted": boundary == "expire"}
        if boundary == "expire":
            consent["valid_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        response = await logged_in.client.put("/v1/consents", json=consent)
        assert response.status_code == 200, response.text
        denied = await logged_in.client.post(
            f"/v1/sessions/{window}/transcriptions/{job_id}/commit",
            json={"result_revision": state["revision"]},
        )
        assert denied.status_code == 403, denied.text
        assert edited not in denied.text

    after = await _state(logged_in, window, job_id)
    assert after["state"] == "cancelled", after
    assert after["submission"] == "suppressed", after
    assert after["text"] == ""
    assert after["result"] == {}
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        assert job.text == ""
        assert job.edited_text is None
        assert job.result == {}
    await asr_runtime.submit_job(logged_in.app, job_id, automatic=True)
    assert wire.requests == []
    assert await _messages(window) == []


async def test_worker_suppression_clears_edited_draft(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset, mode="draft")
    job_id = created["job_id"]
    await asr_runtime.process_job(logged_in.app, job_id)
    state = await _state(logged_in, window, job_id)
    edited = f"恢复时也须清除-{uuid.uuid4().hex}"
    committed = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{job_id}/commit",
        json={"result_revision": state["revision"], "text": edited},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["text"] == edited
    revoked = await logged_in.client.put("/v1/consents", json={"scope": "media.inbound", "granted": False})
    assert revoked.status_code == 200, revoked.text

    # Let recovery perform its authorization cleanup before any GET can do so.
    await _run_worker_polls(logged_in.app, monkeypatch)
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        assert job.state == "cancelled"
        assert job.submission == "suppressed"
        assert job.text == ""
        assert job.edited_text is None
        assert job.result == {}
    assert wire.requests == []
    assert recognition.calls == 1
    assert await _messages(window) == []


@pytest.mark.parametrize("error_kind, expected_state", [
    (SpeechErrorKind.invalid_audio, "failed"),
    (SpeechErrorKind.timeout, "unknown"),
])
@pytest.mark.parametrize("with_text", [True, False], ids=["mixed-text", "audio-only"])
async def test_failed_input_recovers_submission_without_recognizing_again(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
    error_kind: SpeechErrorKind, expected_state: str, with_text: bool,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    recognition.error = SpeechError(error_kind, "fixture recognition failure")
    text = f"中断后恢复原文字-{uuid.uuid4().hex}" if with_text else ""
    created = await _create(logged_in, window, asset, text=text)
    job_id = created["job_id"]
    submit_attempts = 0

    async def interrupted_submit(*args, **kwargs):
        nonlocal submit_attempts
        submit_attempts += 1
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(asr_runtime, "submit_job", interrupted_submit)
        if with_text:
            with pytest.raises(asyncio.CancelledError):
                await asr_runtime.process_job(logged_in.app, job_id)
        else:
            assert await asr_runtime.process_job(logged_in.app, job_id)
    before = await _state(logged_in, window, job_id)
    assert before["state"] == expected_state, before
    assert before["submission"] == "pending", before
    assert submit_attempts == int(with_text)
    assert recognition.calls == 1
    assert wire.requests == []

    await _run_worker_polls(logged_in.app, monkeypatch)
    after = await _state(logged_in, window, job_id)
    assert after["state"] == expected_state, after
    assert recognition.calls == 1
    assert after["charged_seconds"] == before["charged_seconds"]
    rows = await _messages(window)
    async with session_scope() as session:
        turns = list((await session.scalars(select(Turn).where(Turn.window_id == window))).all())
    if with_text:
        assert after["submission"] == "committed", after
        assert [row.text for row in rows if row.role == "user"] == [text]
        assert len([row for row in rows if row.role == "actor"]) == 1
        assert len(turns) == 1
        assert sum(text in content for content in wire.inputs()) == 1, wire.requests
    else:
        assert after["submission"] == "pending", after
        assert wire.requests == []
        assert turns == []
        assert not any(row.role == "actor" for row in rows)
        assert all(row.text == "" for row in rows)


async def test_expired_queue_state_survives_get_and_can_be_retried(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset)
    job_id = created["job_id"]
    queued_before_retry = datetime.now(UTC) - timedelta(minutes=2)
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        original_created_at = job.created_at
        job.queued_at = queued_before_retry
    assert not await asr_runtime.process_job(logged_in.app, job_id)
    expired = await _state(logged_in, window, job_id)
    assert expired["state"] == "expired", expired
    assert expired["submission"] == "pending", expired
    assert expired["error"]["code"] == "expired", expired
    assert recognition.calls == 0
    assert wire.requests == []

    retried = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions/{job_id}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["job_id"] == job_id
    assert retried.json()["state"] == "queued"
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        assert job.created_at == original_created_at
        assert job.queued_at > queued_before_retry
    assert await asr_runtime.process_job(logged_in.app, job_id)
    after = await _state(logged_in, window, job_id)
    assert after["state"] == "succeeded", after
    assert after["submission"] == "committed", after
    assert recognition.calls == 1
    assert any(recognition.phrase in content for content in wire.inputs()), wire.requests
    assert [row.text for row in await _messages(window) if row.role == "user"] == [recognition.phrase]


async def _reservations(job_id: str) -> list[AsrCallReservation]:
    async with session_scope() as session:
        return list((await session.scalars(select(AsrCallReservation).where(
            AsrCallReservation.job_id == job_id,
        ).order_by(AsrCallReservation.attempt, AsrCallReservation.audio_index))).all())


async def test_unknown_retry_on_new_day_preserves_both_attempt_reservations(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch, daily_seconds=0.15)
    first_day = logged_in.app.state.quota.today()
    current_day = first_day
    monkeypatch.setattr(logged_in.app.state.quota, "today", lambda: current_day)
    recognition.error = SpeechError(SpeechErrorKind.timeout, "fixture first-day timeout", retriable=True)
    created = await _create(logged_in, window, asset)
    job_id = created["job_id"]
    await asr_runtime.process_job(logged_in.app, job_id)
    first = await _state(logged_in, window, job_id)
    assert first["state"] == "unknown", first
    assert first["charged_seconds"] == pytest.approx(0.1)
    assert recognition.calls == 1
    assert wire.requests == []
    first_reservations = await _reservations(job_id)
    assert [(row.attempt, row.audio_index, row.day) for row in first_reservations] == [(1, 0, first_day)]
    assert first_reservations[0].seconds == pytest.approx(0.1)

    current_day = first_day + timedelta(days=1)
    retried = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions/{job_id}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["created_at"] == first["created_at"]
    recognition.error = None
    await asr_runtime.process_job(logged_in.app, job_id)
    second = await _state(logged_in, window, job_id)
    assert second["state"] == "succeeded", second
    assert second["submission"] == "committed", second
    assert second["charged_seconds"] == pytest.approx(0.2)
    assert recognition.calls == 2
    assert any(recognition.phrase in content for content in wire.inputs()), wire.requests
    reservations = await _reservations(job_id)
    assert [(row.attempt, row.audio_index, row.day) for row in reservations] == [
        (1, 0, first_day), (2, 0, current_day),
    ]
    assert all(row.provider_id == "accept-asr" for row in reservations)
    assert [row.seconds for row in reservations] == pytest.approx([0.1, 0.1])
    assert [row.text for row in await _messages(window) if row.role == "user"] == [recognition.phrase]


async def test_new_day_quota_counts_other_jobs_before_calling_provider(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch, daily_seconds=0.15)
    current_day = logged_in.app.state.quota.today() + timedelta(days=1)
    monkeypatch.setattr(logged_in.app.state.quota, "today", lambda: current_day)
    first = await _create(logged_in, window, asset, client_request_id="first-new-day-job")
    await asr_runtime.process_job(logged_in.app, first["job_id"])
    first_state = await _state(logged_in, window, first["job_id"])
    assert first_state["state"] == "succeeded", first_state
    assert first_state["charged_seconds"] == pytest.approx(0.1)
    assert recognition.calls == 1
    assert any(recognition.phrase in content for content in wire.inputs()), wire.requests

    second = await _create(logged_in, window, asset, client_request_id="second-new-day-job")
    await asr_runtime.process_job(logged_in.app, second["job_id"])
    second_state = await _state(logged_in, window, second["job_id"])
    assert second_state["state"] == "failed", second_state
    assert second_state["error"]["code"] == "quota", second_state
    assert second_state["submission"] != "committed", second_state
    assert second_state["charged_seconds"] == 0
    assert recognition.calls == 1
    assert len([row for row in await _messages(window) if row.role == "actor"]) == 1
    assert await _reservations(second["job_id"]) == []


async def test_queued_audio_reserves_on_call_day_after_midnight(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch, daily_seconds=0.15)
    queued_day = logged_in.app.state.quota.today()
    current_day = queued_day
    monkeypatch.setattr(logged_in.app.state.quota, "today", lambda: current_day)
    created = await _create(logged_in, window, asset)
    job_id = created["job_id"]
    assert await _reservations(job_id) == []
    assert recognition.calls == 0

    # Advancing the account's calendar day leaves this short queue wait intact.
    current_day = queued_day + timedelta(days=1)
    await asr_runtime.process_job(logged_in.app, job_id)
    state = await _state(logged_in, window, job_id)
    assert state["state"] == "succeeded", state
    assert state["created_at"] == created["created_at"]
    assert recognition.calls == 1
    reservations = await _reservations(job_id)
    assert len(reservations) == 1
    assert reservations[0].day == current_day
    assert reservations[0].seconds == pytest.approx(0.1)
    assert any(recognition.phrase in content for content in wire.inputs()), wire.requests


async def test_old_creation_time_does_not_expire_a_fresh_queue_attempt(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, asset, recognition, wire = await _setup(logged_in, monkeypatch)
    created = await _create(logged_in, window, asset)
    job_id = created["job_id"]
    original_created_at = datetime.now(UTC) - timedelta(days=2)
    async with session_scope() as session:
        job = await session.get(AsrJob, job_id)
        job.created_at = original_created_at
        assert job.queued_at > original_created_at
    assert await asr_runtime.process_job(logged_in.app, job_id)
    after = await _state(logged_in, window, job_id)
    assert after["state"] == "succeeded", after
    assert after["submission"] == "committed", after
    async with session_scope() as session:
        assert (await session.get(AsrJob, job_id)).created_at == original_created_at
    assert recognition.calls == 1
    assert any(recognition.phrase in content for content in wire.inputs()), wire.requests
