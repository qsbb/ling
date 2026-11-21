import io
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select

from ling.capabilities.voice_reply import PreparedVoice, VoiceReplyService
from ling.domains.identity.service import IdentityService
from ling.domains.live.autonomy import consider_start
from ling.domains.live.speech import build_speech
from ling.infra.db import session_scope
from ling.infra.models import ActivityRow, ActorAccount, MemoryRow, OutboxEvent
from ling.ports.model import ModelChunk
from ling.ports.speech import (
    AudioSpec,
    ExpressionIntent,
    MiniMaxVoice,
    SpeechUsage,
    TtsChunk,
    TtsEnd,
    TtsStart,
)

TOKEN = "acceptance-stage-00000001"
SOURCE_TOKEN = "acceptance-source-000001"
ROOM_CONFIG = {"display_name": "直播验收", "room_id": "100200300", "stage_token": TOKEN}
CUSTOM = "/v1/settings/custom-connectors"
SOURCE = "/v1/custom/bili-live"
RUNS = "/v1/live/sessions"


def stage_body(run_id: str, **changes):
    return {"device_id": "device_acceptance", "session_id": run_id, "obs_active": False, **changes}


def comment(run_id: str, **changes):
    return {
        "session_id": run_id, "room_id": ROOM_CONFIG["room_id"],
        "event_id": "test-dm", "kind": "comment",
        "occurred_at": datetime.now(UTC).isoformat(),
        "sender": {"id": "first-id", "name": "同名观众"}, "text": "可以聊聊今天的配乐吗",
        **changes,
    }


def peer_client(env, *, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app, client=("127.0.0.1", 50999)),
        base_url="http://testserver", headers=headers,
    )


async def test_feature_enable_is_idempotent_and_ingress_is_bounded(logged_in):
    env = logged_in
    feature = "/v1/settings/live-features/bilibili/bili-live"
    for _ in range(2):
        response = await env.client.put(feature, json=ROOM_CONFIG)
        assert response.status_code == 200, response.text
        assert response.json()["account_prefix"] == "custom-http.bili-live"
    listed = await env.client.get(CUSTOM)
    assert len(listed.json()["connectors"]) == 1
    assert TOKEN not in listed.text
    opened = await env.client.post(RUNS, json={"connector_id": "bili-live", "mode": "preview"})
    assert opened.status_code == 201, opened.text
    run_id = opened.json()["id"]
    async with peer_client(env) as source:
        info = await source.get(f"{SOURCE}/session")
        assert info.json()["session_id"] == run_id
        first = await source.post(f"{SOURCE}/events", json=comment(run_id))
        assert first.json() == {"status": "accepted"}
        duplicate = await source.post(f"{SOURCE}/events", json=comment(run_id))
        assert duplicate.json() == {"status": "duplicate"}
        other_run = await source.post(f"{SOURCE}/events", json=comment("old_session"))
        assert other_run.json() == {"status": "other_session"}
        other_room = await source.post(f"{SOURCE}/events", json=comment(run_id, room_id="other"))
        assert other_room.status_code == 403
        expired = await source.post(f"{SOURCE}/events", json=comment(
            run_id, event_id="old", occurred_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        ))
        assert expired.json() == {"status": "expired"}
        for ix in range(12):
            body = comment(run_id, event_id=f"spam-{ix}", text=f"直播测试 {ix}")
            assert (await source.post(f"{SOURCE}/events", json=body)).json()["status"] == "accepted"
        await source.post(f"{SOURCE}/events", json=comment(
            run_id, event_id="second", sender={"id": "second-id", "name": "同名观众"},
        ))
        await source.post(f"{SOURCE}/events", json=comment(run_id, event_id="join", kind="entrance"))
        current = (await env.client.get(f"{RUNS}/current")).json()["session"]
        assert current["pending_comments"] == 2
        assert current["counts"]["entrance"] == 1
        assert current["viewers"] == []
        async with session_scope() as db:
            assert await db.scalar(select(func.count()).select_from(ActorAccount)) == 0
        rejected = await source.post(f"{SOURCE}/events", json=comment(run_id), headers={"Via": "proxy"})
        assert rejected.status_code == 403
        configured = await env.client.put(feature, json={**ROOM_CONFIG, "enabled": False})
        assert configured.status_code == 200, configured.text
        disabled = await source.post(f"{SOURCE}/events", json=comment(run_id))
        assert disabled.status_code == 409


async def test_broadcast_waits_for_observed_platform_and_owned_obs(logged_in):
    env = logged_in
    configured = await env.client.put(
        f"{CUSTOM}/bili-live", json={**ROOM_CONFIG, "obs_control": True, "token": SOURCE_TOKEN},
    )
    assert configured.status_code == 200, configured.text
    opened = await env.client.post(RUNS, json={"connector_id": "bili-live", "mode": "broadcast"})
    assert opened.status_code == 201, opened.text
    run_id = opened.json()["id"]
    async with (
        peer_client(env, headers={"Authorization": f"Bearer {TOKEN}"}) as stage,
        peer_client(env, headers={"Authorization": f"Bearer {SOURCE_TOKEN}"}) as source,
    ):
        async def status(**changes):
            response = await stage.post(f"{SOURCE}/stage", json=stage_body(run_id, **changes))
            assert response.status_code == 200, response.text
            return response.json()["action"]

        async def public_state():
            return (await env.client.get(f"{RUNS}/current")).json()["session"]["state"]

        assert await status(obs_active=True) == "hold"
        assert await public_state() == "starting"
        assert await status() == "start"
        assert await status() == "hold"
        assert await status(obs_active=True, control="started") == "hold"
        assert await public_state() == "starting"
        async with session_scope() as db:
            assert await db.scalar(select(func.count()).select_from(ActivityRow)) == 0
        invalid_source = await stage.post(
            f"{SOURCE}/events", json=comment(run_id, kind="live_start"),
        )
        assert invalid_source.status_code == 403
        event = await source.post(
            f"{SOURCE}/events", json=comment(run_id, event_id="live-confirmed", kind="live_start"),
        )
        assert event.json() == {"status": "accepted"}
        assert await status(obs_active=False) == "hold"
        assert await public_state() == "starting"
        assert await status(obs_active=True) == "hold"
        assert await status(obs_active=True, control="started") == "ready"
        assert await public_state() == "live"
        event = await source.post(f"{SOURCE}/events", json=comment(run_id))
        assert event.json()["status"] == "accepted"
        assert await status(obs_active=True) == "ready"
        async with session_scope() as db:
            assert await db.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.topic == "livestream.speech"
            )) == 1
        assert (await env.client.post(f"{RUNS}/{run_id}/stop")).status_code == 200
        assert await status(obs_active=True) == "stop"
        assert await status(control="stopped") == "hold"
        assert await public_state() == "stopping"
        event = await source.post(
            f"{SOURCE}/events", json=comment(run_id, event_id="live-ended", kind="live_end"),
        )
        assert event.json() == {"status": "accepted"}
        assert await status() == "hold"
        assert await public_state() == "ended"


async def test_only_played_viewers_belong_to_room_memory(logged_in, monkeypatch):
    env = logged_in
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 3200)
    wav = buffer.getvalue()
    spec = AudioSpec("audio/wav", "wav", "pcm_s16le", 16000, 1)
    prompts = []
    spoken = []

    class Provider:
        async def synthesize(self, request, *, call):
            spoken.append(request.text)
            yield TtsStart(spec)
            yield TtsChunk(0, wav)
            yield TtsEnd(spec, None, SpeechUsage(audio_seconds=0.2))

    async def prepare(_self, *_args, **_kwargs):
        return PreparedVoice(
            provider=Provider(), voice=MiniMaxVoice(voice_id="demo-voice"),
            intent=ExpressionIntent("voice", "model", 1),
            provider_id="demo", model_id="speech-demo",
        )

    class Router:
        async def stream(self, _effective, *, profile, messages):
            assert profile == "dialogue.fast"
            prompts.append(messages[-1].content)
            yield ModelChunk(delta="谢谢你问起今天的配乐。", done=True), SimpleNamespace(error=None)

    monkeypatch.setattr(VoiceReplyService, "prepare", prepare)
    async with session_scope() as db:
        db.add(MemoryRow(
            id="private-sentinel", kind="episode", text="不得进直播模型的私聊哨兵",
            subject_id=env.subject_id, visibility="subject_private", origin="user_statement",
        ))
        await IdentityService(db).bind_account(
            subject_id=env.subject_id, connector_instance="onebot-demo",
            external_account_id="100200300", proof="admin",
        )

    configured = await env.client.put(f"{CUSTOM}/bili-live", json=ROOM_CONFIG)
    assert configured.status_code == 200

    async with (
        peer_client(env, headers={"Authorization": f"Bearer {TOKEN}"}) as stage,
        peer_client(env) as source,
    ):
        async def open_preview():
            out = await env.client.post(RUNS, json={"connector_id": "bili-live", "mode": "preview"})
            assert out.status_code == 201, out.text
            return out.json()["id"]

        async def tick(run_id: str, **changes):
            out = await stage.post(f"{SOURCE}/stage", json=stage_body(run_id, **changes))
            assert out.status_code == 200, out.text
            return out.json()

        async def reply(run_id: str, event: dict):
            received = await source.post(f"{SOURCE}/events", json=event)
            assert received.json()["status"] == "accepted"
            assert (await tick(run_id))["action"] == "ready"
            async with session_scope() as db:
                outbox = await db.scalar(
                    select(OutboxEvent).where(OutboxEvent.topic == "livestream.speech")
                    .order_by(OutboxEvent.created_at.desc()).limit(1)
                )
                work = outbox.payload
            await build_speech(env.settings, Router(), **work)
            order = await tick(run_id)
            assert order["action"] == "speak", order
            assert order["text"] == spoken[-1]
            audio = await stage.get(f"{SOURCE}/stage/audio/{run_id}/{order['speech_id']}")
            assert audio.content == wav
            assert order["text"] == "谢谢你问起今天的配乐。"
            return order["speech_id"]

        run_id = await open_preview()
        speech_id = await reply(run_id, comment(run_id))
        remember = f"{RUNS}/{run_id}/viewers"
        body = {"account_ref": "first-id", "note": "常问电影配乐", "merge_into": env.subject_id}
        assert (await env.client.put(remember, json=body)).status_code == 404
        await tick(run_id, playback={"speech_id": speech_id, "state": "completed"})
        saved = await env.client.put(remember, json=body)
        assert saved.status_code == 200, saved.text
        assert (await env.client.post(f"{RUNS}/{run_id}/stop")).status_code == 200
        await tick(run_id)
        second_run = await open_preview()
        assert second_run != run_id
        await reply(second_run, comment(second_run, event_id="back", sender={
            "id": "first-id", "name": "改名后的观众",
        }))
        assert "改名后的观众" in prompts[-1]
        assert "常问电影配乐" in prompts[-1]
        assert "不得进直播模型的私聊哨兵" not in prompts[-1]
        async with session_scope() as db:
            account = await db.scalar(select(ActorAccount).where(
                ActorAccount.connector_instance == "custom-http.bili-live",
            ))
            assert account.external_account_id == "first-id"
            assert await IdentityService(db).canonical_subject(account.subject_id) == env.subject_id


async def test_autonomous_start_respects_decision_and_authorization(logged_in):
    env = logged_in
    now = datetime.now(UTC)

    def request_window(before, after):
        return {
            **ROOM_CONFIG, "obs_control": True, "public_topic": "今晚聊配乐",
            "autonomous_window": {"starts_at": before.isoformat(), "ends_at": after.isoformat()},
        }

    class Router:
        answer = "等待"

        async def stream(self, _effective, *, profile, messages):
            assert messages[-1].content == "今晚聊配乐"
            yield ModelChunk(delta=self.answer, done=True), SimpleNamespace(error=None)

    router = Router()
    async with peer_client(env, headers={"Authorization": f"Bearer {TOKEN}"}) as stage:
        async def status(**changes):
            response = await stage.post(f"{SOURCE}/stage", json=stage_body(None, **changes))
            assert response.status_code == 200, response.text
            return response.json()

        async def decide():
            async with session_scope() as db:
                row = await db.scalar(
                    select(OutboxEvent).where(OutboxEvent.topic == "livestream.decide")
                    .order_by(OutboxEvent.created_at.desc()).limit(1)
                )
                args = row.payload
            await consider_start(env.settings, router, **args)

        assert (await env.client.put(f"{CUSTOM}/bili-live", json=request_window(
            now + timedelta(minutes=2), now + timedelta(minutes=5),
        ))).status_code == 200
        assert (await status(broadcast_ready=True))["action"] == "hold"
        async with session_scope() as db:
            assert await db.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.topic == "livestream.decide"
            )) == 0
        assert (await env.client.put(f"{CUSTOM}/bili-live", json=request_window(
            now - timedelta(minutes=1), now + timedelta(minutes=1),
        ))).status_code == 200
        assert (await status())["action"] == "prepare"
        assert (await status(broadcast_ready=True))["action"] == "hold"
        await decide()
        assert (await env.client.get(f"{RUNS}/current")).json()["session"] is None
        assert (await env.client.get(CUSTOM)).json()["connectors"][0]["autonomous_state"] == "wait"
        router.answer = "开播"
        assert (await status(broadcast_ready=True))["session_id"] is None
        assert (await env.client.put(f"{CUSTOM}/bili-live", json=request_window(
            now - timedelta(minutes=1), now + timedelta(minutes=2),
        ))).status_code == 200
        await status(broadcast_ready=True)
        await decide()
        state = (await env.client.get(f"{RUNS}/current")).json()["session"]
        assert state["state"] == "starting"
        assert datetime.fromisoformat(state["deadline_at"]) <= now + timedelta(minutes=2)
        assert (await status(session_id=state["id"], device_id="other_stage_device"))["action"] == "hold"
        assert (await status(session_id=state["id"]))["action"] == "start"
