"""Exercise client-facing routes on fresh PostgreSQL schemas, grouped by workflow."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import pytest_asyncio

from ling.api.routes import version as version_routes
from ling.domains.conversation.service import ConversationService
from ling.domains.goals.service import GoalsService
from ling.domains.speech.asr_runtime import process_job
from ling.infra.asr_jobs import AsrJob
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow, Operation, RelationshipEvent, Turn
from ling.kernel.turn import RECALL_PROPOSAL_KIND
from ling.logging_setup import emit_event
from tests.integration.conftest import TestEnv, read_sse
from tests.integration.test_asr_input import audio
from tests.integration.test_asr_input import setup as setup_asr
from tests.integration.test_connector_instances_api import FakeRuntime
from tests.integration.test_life_timeline import DAY, _seed_day
from tests.integration.test_manage_subjects_paging import _seed as seed_subjects
from tests.integration.test_proactive_history import _candidate as proactive_candidate
from tests.integration.test_recall_tool import _FakeOneBot, _seed_recallable
from tests.integration.test_recall_tool import _install as install_onebot
from tests.integration.test_version_image_update import (
    CURRENT_DIGEST,
)
from tests.integration.test_version_image_update import _body as image_update_body
from tests.integration.test_version_image_update import _configure as configure_image_channel
from tests.integration.test_version_image_update import (
    _write_status as write_image_status,
)
from tests.integration.test_version_update_api import (
    CURRENT_COMMIT,
    CURRENT_VERSION,
)
from tests.integration.test_version_update_api import _body as source_update_body
from tests.integration.test_version_update_api import _configure as configure_source_channel
from tests.integration.test_version_update_api import (
    _heartbeat as write_source_heartbeat,
)
from tests.integration.test_version_update_api import (
    _latest as sample_release,
)
from tests.integration.test_voice_reply_acceptance import _seed_message as seed_voice
from tests.integration.test_web_media_upload import _tiny_png
from tests.regression.chat_fixture import new_window
from tests.regression.client_api import CLIENTS, STREAMED, WEB, Capture
from tests.regression.snapshots import GOLDEN, shape

pytestmark = pytest.mark.w0_web


@pytest_asyncio.fixture
async def capture(logged_in: TestEnv):
    async with Capture(logged_in).open() as recorder:
        yield recorder


def test_client_inventory_matches_committed_samples() -> None:
    recorded = {}
    for path in GOLDEN.glob("api-*.json"):
        for case, sample in json.loads(path.read_text(encoding="utf-8")).items():
            name = case.split("/", 1)[0]
            assert sample["operation"] == WEB[name]
            recorded[name] = sample["operation"]
    # The three streaming workflow operations have live HTTP/SSE regressions,
    # not synthetic JSON response samples.
    assert set(recorded) == set(WEB) - STREAMED
    assert set(CLIENTS["mobile"].values()) <= set(WEB.values())


def test_response_shapes_do_not_serialize_fixture_values_or_map_keys() -> None:
    probe = "opaque-wp00-sensitive-sample"
    payload = {
        "list": [{"payload": probe}],
        "lookup": {probe: {"label": probe}},
        "untyped": {probe: [{probe: {probe: probe}}]},
    }
    assert probe in json.dumps(payload)
    paths = {"maps": frozenset({".lookup"}), "opaque_maps": frozenset({".untyped"})}
    result = shape(payload, **paths)
    assert result == {
        "list": [{"payload": "string"}],
        "lookup": {"$map": [{"label": "string"}]},
        "untyped": {"$map": [[{"$map": [{"$map": ["string"]}]}]]},
    }
    assert probe not in json.dumps(result)
    assert result == shape(
        {
            "list": [{"payload": "different"}],
            "lookup": {"different": {"label": "different"}},
            "untyped": {"different": [{"another": {"third": "different"}}]},
        },
        **paths,
    )
    assert shape({"value": None}) == {"value": "null"}
    assert shape({}) == {}
    assert shape({"items": []}) == {"items": []}
    assert shape({"items": [{"value": None}]}) == {"items": [{"value": "null"}]}


async def test_api_account(capture: Capture) -> None:
    await capture.raw("login", json={"username": "tester", "password": "tester-password"})
    await capture.raw("me")
    await capture.raw("dashboard")
    await capture.raw("userPreferences")
    await capture.raw("setTimezone", json={"timezone": "UTC"})
    await capture.raw("setVideoMode", json={"video_mode": "native"})
    await capture.raw("consents", variant="empty")
    await capture.raw("setConsent", json={"scope": "media.inbound", "granted": True})
    await capture.raw("consents", variant="populated")
    await capture.raw(
        "changeAccount", json={"current": "tester-password", "username": "sample-user"}
    )
    await capture.raw(
        "changePassword",
        status=204,
        media_type=None,
        json={"current": "tester-password", "new": "sample-new-password"},
    )
    await capture.raw("logout")
    await capture.raw("me", variant="logged-out", status=401)
    capture.check("account")


async def test_api_conversation(capture: Capture) -> None:
    await capture.raw("listSessions", variant="empty")
    first = (await capture.json("createSession", status=201, json={"title": "Sample"}))[
        "session_id"
    ]
    other = (await capture.json("createSession", status=201, variant="unnamed", json={}))[
        "session_id"
    ]
    params = {"session_id": first}
    await capture.raw("renameSession", path_args=params, json={"title": "Renamed sample"})
    await capture.raw(
        "moveSession",
        status=204,
        path_args=params,
        json={"anchor_id": other, "before": True},
    )
    await capture.raw("pinSession", path_args=params, json={"pinned": True})
    await capture.raw("listSessions", variant="populated")
    await capture.raw("messages", variant="empty", path_args=params)

    async with session_scope() as session:
        # A preexisting message without a turn has no saved prompt.
        conv = ConversationService(session, actor_id=capture.env.settings.actor_id)
        earlier = await conv.add_message(first, role="user", text="Earlier sample message.")
    reply = await capture.client.post(
        f"/v1/sessions/{first}/turns", json={"client_turn_id": "wp00", "text": "请回答样本问题。"}
    )
    assert reply.status_code == 200, reply.text
    event, final = (await read_sse(reply))[-1]
    assert event == "turn.completed" and final["message"]
    await capture.raw("messages", variant="populated", path_args=params)
    prompt = await capture.json(
        "messagePrompt", path_args={"message_id": final["message"]["message_id"]}
    )
    assert prompt["state"] == "available" and any(
        section["items"] for section in prompt["sections"]
    )
    unavailable = await capture.json(
        "messagePrompt", variant="unavailable", path_args={"message_id": earlier.message_id}
    )
    assert unavailable["state"] == "unavailable"
    await capture.raw(
        "messagePrompt", variant="missing", status=404, path_args={"message_id": "msg_missing"}
    )
    capture.check("conversation")


async def test_api_recall(capture: Capture) -> None:
    fake = _FakeOneBot()
    install_onebot(capture.env, fake)
    window_id, message_id = await _seed_recallable(capture.env)
    async with session_scope() as session:
        for proposal in ("sample-confirm", "sample-decline"):
            session.add(
                Operation(
                    id=proposal,
                    kind=RECALL_PROPOSAL_KIND,
                    actor_id=capture.env.settings.actor_id,
                    state="proposed",
                    payload={"window_id": window_id, "message_id": message_id},
                )
            )
    params = {"conversation_id": window_id}
    proposals = await capture.json("recallProposals", variant="populated", path_args=params)
    assert len(proposals) == 2
    await capture.raw(
        "declineRecall",
        status=204,
        path_args={**params, "proposal_id": "sample-decline"},
    )
    result = await capture.json(
        "confirmRecall", path_args={**params, "proposal_id": "sample-confirm"}
    )
    assert result["state"] == "recalled"
    assert fake.delete_calls() == 1
    await capture.raw("recallProposals", variant="empty", path_args=params)
    capture.check("recall")


async def test_api_memory_and_subjects(capture: Capture) -> None:
    env = capture.env
    source, target = await seed_subjects("wp00", 2)
    window = await new_window(env)
    turn_id = "turn_wp00_interaction"
    async with session_scope() as session:
        for key in ("sample-memory-a", "sample-memory-b"):
            session.add(
                MemoryRow(
                    id=key,
                    subject_id=env.subject_id,
                    kind="assertion",
                    text=f"Test data for {key}",
                    origin="user_statement",
                    visibility="subject_private",
                    confidence=0.9,
                    source_event_ids=["sample-event"],
                    source_window_id=window,
                )
            )
        session.add(Turn(id=turn_id, window_id=window, client_turn_id="wp00-interaction"))
        await session.flush()
        await ConversationService(session).add_message(
            window,
            role="user",
            text="Sample interaction",
            turn_id=turn_id,
            sender_subject_id=env.subject_id,
        )
        session.add(
            RelationshipEvent(
                id="event_wp00_interaction",
                actor_id=env.settings.actor_id,
                subject_id=env.subject_id,
                turn_id=turn_id,
                kind="warmth",
                origin="user_statement",
            )
        )

    await capture.raw("subjects", params={"limit": 20})
    await capture.raw(
        "renameSubject", path_args={"subject_id": source}, json={"display_name": "Sample person"}
    )
    for section, subject in (
        ("accounts", source),
        ("memories", env.subject_id),
        ("interactions", env.subject_id),
    ):
        detail = await capture.json(
            "subjectDetails",
            variant=section,
            path_args={"subject_id": subject},
            params={"section": section},
        )
        assert detail["items"], section
    await capture.raw("mergeSubjects", json={"source_id": source, "target_id": target})
    await capture.raw("unmergeSubject", path_args={"source_id": source})

    await capture.raw("listMemories", variant="current")
    await capture.raw(
        "supersedeMemory",
        path_args={"memory_id": "sample-memory-a"},
        json={"text": "Sample correction", "reason": "Sample reason"},
    )
    await capture.raw(
        "retractMemory",
        path_args={"memory_id": "sample-memory-b"},
        json={"reason": "Sample reason"},
    )
    await capture.raw("listMemories", variant="retracted", params={"include": "retracted"})
    core = await capture.json(
        "upsertCoreMemory", json={"label": "sample", "text": "Sample core memory"}
    )
    await capture.raw("listCoreMemory")
    await capture.raw("deleteCoreMemory", path_args={"core_id": core["core_id"]})
    await capture.raw("forgetAllMemories")
    await capture.raw("purgeStatus")
    capture.check("memory-and-subjects")


async def test_api_persona_and_life(capture: Capture) -> None:
    await capture.raw("listPersonas", variant="empty")
    first = await capture.json(
        "createPersona",
        status=201,
        json={"display_name": "Sample", "system_prompt": "Synthetic personality."},
    )
    second = await capture.json(
        "createPersona", status=201, variant="inactive", json={"display_name": "Other"}
    )
    params = {"persona_id": first["persona_id"]}
    await capture.raw("activatePersona", path_args=params, json={})
    await capture.raw("persona")
    await capture.raw("getPersona", path_args=params)
    await capture.raw(
        "updatePersonaById", path_args=params, json={"display_name": "Renamed sample"}
    )
    await capture.raw("listPersonas", variant="populated")
    await capture.raw("deletePersona", status=204, path_args={"persona_id": second["persona_id"]})
    await capture.raw("timeline", opaque_maps=frozenset({".events[].payload"}))

    timeline = await capture.json("lifeTimeline", variant="empty")
    job = await capture.json(
        "createOutlineJob",
        status=202,
        json={"plan_day": timeline["plan_day"], "force": False, "request_id": "wp00-outline"},
    )
    await capture.raw("outlineJob", path_args={"job_id": job["job_id"]})
    await _seed_day()
    timeline = await capture.json(
        "lifeTimeline", variant="planned", params={"date": DAY.isoformat()}
    )
    assert timeline["segments"] and timeline["facts"] and timeline["intents"] and timeline["dream"]

    activity = await capture.json(
        "activity", opaque_maps=frozenset({".current.checkpoint", ".history[].checkpoint"})
    )
    assert activity["current"] and activity["history"]
    env = capture.env
    async with session_scope() as session:
        await GoalsService(session, actor_id=env.settings.actor_id).create_goal(title="Sample goal")
        session.add(proactive_candidate("opp_wp00", env.subject_id, reason="Sample reason"))
    assert (await capture.json("goals"))["goals"]
    proactive = await capture.json("proactive")
    assert proactive["pending"] and proactive["recent"]

    attribute = "wp00_dynamic_tag"
    nested = "wp00_nested_dynamic_tag"
    content = "wp00_sample_log_value"
    emit_event(
        logging.getLogger("ling.wp00"),
        logging.INFO,
        event_name="wp00_sample",
        message="WP00 sample log",
        attributes={attribute: [{nested: content}]},
    )
    logs = await capture.json(
        "getLogs", params={"logger": "ling.wp00"}, opaque_maps=frozenset({".items[].attributes"})
    )
    (attributes,) = [
        item["attributes"] for item in logs["items"] if attribute in item["attributes"]
    ]
    assert attributes[attribute][0][nested] == content
    assert all(
        probe not in json.dumps(capture.samples["getLogs"])
        for probe in (attribute, nested, content)
    )
    capture.check("persona-and-life")


async def test_api_connector_and_window_settings(capture: Capture) -> None:
    env = capture.env
    env.app.state.connector_runtime = FakeRuntime()
    scope = {"scope": "onebot"}
    await capture.raw("connectors", variant="default")
    await capture.raw("connectorInstances", variant="empty", path_args=scope)
    created = await capture.json(
        "createConnectorInstance",
        status=201,
        path_args=scope,
        json={"display_name": "Sample"},
    )
    instance = {**scope, "instance_id": created["item"]["instance_id"]}
    await capture.raw("connectorInstances", variant="populated", path_args=scope)
    await capture.raw(
        "renameConnectorInstance", path_args=instance, json={"display_name": "Renamed sample"}
    )
    await capture.raw(
        "saveConnectorConfig",
        path_args=scope,
        status=202,
        params={"instance_id": instance["instance_id"]},
        json={"set": {"LING_ONEBOT_INSTANCE_ID": "sample-ns"}},
        headers={"If-Match": "0"},
    )
    await capture.raw(
        "connectors", variant="instance", params={"instance_id": instance["instance_id"]}
    )
    await capture.raw("instanceDialoguePolicy", path_args=instance)
    change = {"set": {"private_may_stay_silent": False}, "clear": []}
    preview = await capture.json("previewInstanceDialoguePolicy", path_args=instance, json=change)
    await capture.raw(
        "saveInstanceDialoguePolicy",
        path_args=instance,
        json={**change, "preview_token": preview["preview_token"]},
    )

    async with session_scope() as session:
        window = await ConversationService(session, actor_id=env.settings.actor_id).create_window(
            subject_id=env.subject_id,
            title="sample-ns:private:100200300",
            channel="onebot",
        )
    params = {"session_id": window.id}
    options = await capture.json("windowOriginBinding", path_args=params)
    assert options["candidates"]
    preview = await capture.json("previewWindowOriginBinding", path_args=params, json=instance)
    await capture.raw(
        "saveWindowOriginBinding",
        path_args=params,
        json={**instance, "preview_token": preview["preview_token"]},
    )
    await capture.raw("windowDialoguePolicy", path_args=params)
    change = {"set": {"group_turn_budget": 3}, "clear": []}
    preview = await capture.json("previewWindowPolicy", path_args=params, json=change)
    await capture.raw(
        "saveWindowPolicy",
        path_args=params,
        json={**change, "preview_token": preview["preview_token"]},
    )

    await capture.raw("setConnectorInstanceEnabled", path_args=instance, json={"enabled": False})
    # The current Web calls omit Content-Type for these bodyless writes.
    await capture.raw(
        "deleteConnectorInstance", variant="web-request", status=422, path_args=instance
    )
    await capture.raw("resetConnectorConfig", variant="web-request", status=422)
    await capture.raw(
        "deleteConnectorInstance", path_args=instance, headers={"Content-Type": "application/json"}
    )
    await capture.raw("resetConnectorConfig", headers={"Content-Type": "application/json"})
    capture.check("connectors")


async def test_api_model_settings(capture: Capture) -> None:
    await capture.raw("taxonomy")
    await capture.raw("ToolStatus.load")
    edit = {
        "providers": [{"provider_id": "fake", "models": [{"model_id": "sample-chat"}]}],
        "dialogue": {"private_may_stay_silent": False},
    }
    first = await capture.json("saveDraft", json=edit)
    await capture.raw("publish", json={"revision": first["revision"]})
    second = await capture.json(
        "saveDraft", variant="second", json={"dialogue": {"group_turn_budget": 8}}
    )
    await capture.raw("publish", variant="second", json={"revision": second["revision"]})
    await capture.raw("rollback", json={"to_revision": first["revision"]})
    await capture.raw("settingsResolve", json={})
    await capture.raw("quickConfigPreview", json={})

    # A synthetic credential with no recognizable provider prefix. Its raw
    # value and any preview are both excluded from the shape snapshot.
    probe = "wp00-opaque-provider-fixture-credential"
    provider = {"provider_id": "fake"}
    await capture.raw("setSecret", path_args=provider, json={"secret": probe})
    await capture.raw("probeProvider", path_args=provider)
    config = await capture.raw("settingsConfig", opaque_maps=frozenset({".providers[].extra"}))
    assert probe not in config.text
    assert any(p["models"] for p in config.json()["providers"])
    await capture.env.app.state.quota.record("fake", 17)
    await capture.raw("settingsHealth", maps=frozenset({".health", ".profiles"}))
    usage = await capture.json("settingsUsage", maps=frozenset({".usage", ".limits"}))
    assert "fake" in usage["usage"]
    await capture.raw("deleteSecret", path_args=provider)
    capture.check("settings")


async def test_api_media_and_transcription(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = capture.env
    session_id, _ = await setup_asr(env, monkeypatch)
    params = {"session_id": session_id}
    await capture.raw(
        "uploadMedia", path_args=params, files={"upload": ("sample.png", _tiny_png(), "image/png")}
    )
    await capture.raw(
        "uploadVoiceReference", files={"upload": ("sample.wav", audio(), "audio/wav")}
    )
    asset = await capture.json(
        "uploadAudio",
        status=201,
        path_args=params,
        files={"file": ("sample.wav", audio(), "audio/wav")},
    )
    job = await capture.json(
        "createTranscription",
        status=202,
        path_args=params,
        json={"asset_id": asset["asset_id"], "client_request_id": "wp00-asr", "mode": "draft"},
    )
    job_id = job["job_id"]
    params["job_id"] = job_id
    await capture.raw("transcription", path_args=params, variant="queued")
    # Inject a persisted failure to exercise the retry transition without
    # depending on a real speech service or on elapsed time.
    async with session_scope() as session:
        row = await session.get(AsrJob, job_id)
        row.state = "failed"
        row.error_code = "provider_error"
        row.error_message = "synthetic upstream failure"
    await capture.raw("transcription", path_args=params, variant="failed")
    await capture.raw("retryTranscription", status=202, path_args=params)
    assert await process_job(env.app, job_id)
    finished = await capture.json("transcription", path_args=params, variant="finished")
    assert finished["state"] == "succeeded"
    await capture.raw(
        "commitTranscription",
        path_args=params,
        json={"result_revision": finished["revision"], "text": "Sample transcript"},
    )

    cancelled = await capture.json(
        "createTranscription",
        status=202,
        variant="second",
        path_args=params,
        json={
            "asset_id": asset["asset_id"],
            "client_request_id": "wp00-asr-cancel",
            "mode": "draft",
        },
    )
    await capture.raw("cancelTranscription", path_args={**params, "job_id": cancelled["job_id"]})

    empty = await new_window(env)
    await capture.raw(
        "voiceMessages.fetchVoiceMessages", variant="empty", path_args={"session_id": empty}
    )
    voice = await seed_voice(env)
    message = {"message_id": voice.message_id}
    await capture.raw(
        "voiceMessages.fetchVoiceMessages",
        variant="ready",
        path_args={"session_id": voice.window_id},
    )
    result = await capture.json("voiceMessages.refetchVoiceUrl", path_args=message)
    await capture.raw("VoicePlayer.audio", url=result["url"], media_type="audio/wav")
    await capture.raw(
        "VoicePlayer.audio",
        variant="range",
        url=result["url"],
        status=206,
        media_type="audio/wav",
        headers={"Range": "bytes=0-15"},
    )
    await capture.raw(
        "voiceMessages.fetchOperatorSpeechText", path_args=message, params={"view": "operator"}
    )
    await capture.raw(
        "voiceMessages.reportPlayback",
        path_args=message,
        json={"position_ms": 30, "completed": False},
    )
    capture.check("media")


async def test_api_updates(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LING_COMMIT", CURRENT_COMMIT)
    monkeypatch.setattr(version_routes, "current_version", lambda: CURRENT_VERSION)

    async def upstream_release():
        return sample_release()

    monkeypatch.setattr(version_routes, "_fetch_update_release", upstream_release)
    await capture.raw("version")
    await capture.raw("imageUpdate", variant="source-mode")
    await capture.raw("updateStatus", variant="disabled")

    source = tmp_path / "source"
    source.mkdir()
    write_source_heartbeat(configure_source_channel(capture.env, source))
    release = await capture.json("checkRelease")
    assert release["update_eligible"] is True
    update_headers = {"X-Ling-Update": "1"}
    await capture.raw(
        "requestUpdate", status=201, headers=update_headers, json=source_update_body()
    )
    await capture.raw("updateStatus", variant="queued")

    monkeypatch.setenv("LING_APP_IMAGE", CURRENT_DIGEST)
    image = configure_image_channel(capture.env, tmp_path)
    write_image_status(image)
    await capture.raw("imageUpdate", variant="available", maps=frozenset({".release_notes.notes"}))
    request = "a" * 32
    await capture.raw("imageUpdateCheck", status=202, json={"request_id": request})
    await capture.raw("imageUpdateCheck", variant="deduped", json={"request_id": request})
    await capture.raw(
        "requestImageUpdate", status=201, headers=update_headers, json=image_update_body()
    )
    capture.check("updates")
