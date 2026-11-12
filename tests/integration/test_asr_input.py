"""Audio input must deliver actual recognition text through the normal dialogue entry."""
from __future__ import annotations

import io
import math
import struct
import wave
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ling.domains.speech.asr_runtime import process_job, submit_job
from ling.infra.asr_jobs import AsrJob
from ling.infra.db import session_scope
from ling.infra.models import Message
from ling.ports.speech import AsrResult, SpeechUsage
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def audio() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(struct.pack("<h", int(1000 * math.sin(i / 10))) for i in range(1600)))
    return stream.getvalue()


async def setup(env: TestEnv, monkeypatch) -> tuple[str, str]:
    # Keep the test worker deterministic, then invoke the production job handler explicitly.
    response = await env.client.put("/v1/consents", json={"scope": "media.inbound", "granted": True})
    assert response.status_code == 200, response.text
    draft = await env.client.put("/v1/settings/draft", json={
        "providers": [{"provider_id": "asrfixture", "kind": "siliconflow_asr",
                       "base_url": "https://example.test/v1", "models": [{"model_id": "fixture"}]}],
        "profiles": [{"profile": "speech.asr", "provider_id": "asrfixture", "model_id": "fixture"}],
        "dialogue": {"private_may_stay_silent": False},
    })
    assert draft.status_code == 200, draft.text
    published = await env.client.post("/v1/settings/publish", json={"revision": draft.json()["revision"]})
    assert published.status_code == 200, published.text
    window = (await env.client.post("/v1/sessions", json={})).json()["session_id"]
    uploaded = await env.client.post(f"/v1/sessions/{window}/audio-inputs", files={"file": ("a.wav", audio(), "audio/wav")})
    assert uploaded.status_code == 201, uploaded.text

    class Provider:
        async def transcribe(self, request, *, call):
            return AsrResult("蓝色风筝序号七十三", "zh", None, None, SpeechUsage())
    async def build(config, *, secret_reader):
        return SimpleNamespace(ok=True, reason="", provider=Provider(), provider_id="asrfixture", model_id="fixture")
    monkeypatch.setattr("ling.domains.speech.asr_providers.build_asr_provider", build)
    return window, uploaded.json()["asset_id"]


async def test_draft_then_commit_is_one_real_input(logged_in: TestEnv, monkeypatch) -> None:
    window, asset = await setup(logged_in, monkeypatch)
    created = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions", json={
        "asset_id": asset, "client_request_id": "draft-1", "mode": "draft"})
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    await process_job(logged_in.app, job_id)
    state = (await logged_in.client.get(f"/v1/sessions/{window}/transcriptions/{job_id}")).json()
    assert state["state"] == "succeeded", state
    assert state["text"] == "蓝色风筝序号七十三"
    async with session_scope() as session:
        assert not (await session.scalars(select(Message).where(Message.window_id == window))).all()
    commit = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions/{job_id}/commit", json={"result_revision": state["revision"]})
    assert commit.status_code == 200, commit.text
    await submit_job(logged_in.app, job_id)
    first = (await logged_in.client.get(f"/v1/sessions/{window}/transcriptions/{job_id}")).json()
    again = await logged_in.client.post(f"/v1/sessions/{window}/transcriptions/{job_id}/commit", json={"result_revision": state["revision"]})
    assert again.json()["message_id"] == first["message_id"]
    async with session_scope() as session:
        rows = (await session.scalars(select(Message).where(Message.window_id == window, Message.role == "user"))).all()
        assert len(rows) == 1
        assert rows[0].text == "蓝色风筝序号七十三"
        assert (await session.get(AsrJob, job_id)).result["audio"][0]["confidence"] is None
