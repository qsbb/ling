"""Web 会话覆盖必须经权限、预览和真实接话决策生效。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import (
    ConversationAttemptSnapshot,
    Message,
    Turn,
    WindowDialogueOverride,
)
from ling.kernel.autonomy import Situation
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
ORIGIN = {"Origin": "http://testserver"}
FIELD = "private_may_stay_silent"


async def create_window(env: TestEnv) -> str:
    response = await env.client.post("/v1/sessions", json={"title": "策略测试"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def change(value: bool | None) -> dict:
    return {"set": {} if value is None else {FIELD: value}, "clear": [FIELD] if value is None else []}


async def preview(env: TestEnv, window: str, value: bool | None) -> dict:
    response = await env.client.post(
        f"/v1/sessions/{window}/dialogue-policy/preview", json=change(value), headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    return response.json()


async def save(env: TestEnv, window: str, value: bool | None, token: str):  # noqa: ANN202
    return await env.client.put(
        f"/v1/sessions/{window}/dialogue-policy",
        json={**change(value), "preview_token": token},
        headers=ORIGIN,
    )


async def preview_operation(env: TestEnv, window: str, body: dict):
    return await env.client.post(
        f"/v1/sessions/{window}/dialogue-policy/preview", json=body, headers=ORIGIN
    )


async def save_operation(env: TestEnv, window: str, body: dict, token: str):
    return await env.client.put(
        f"/v1/sessions/{window}/dialogue-policy",
        json={**body, "preview_token": token},
        headers=ORIGIN,
    )


async def set_policy(env: TestEnv, window: str, value: bool | None) -> dict:
    current = await preview(env, window, value)
    response = await save(env, window, value, current["preview_token"])
    assert response.status_code == 200, response.text
    return response.json()


async def publish(env: TestEnv, value: bool) -> None:
    draft = await env.client.put("/v1/settings/draft", json={"dialogue": {FIELD: value}})
    assert draft.status_code == 200, draft.text
    result = await env.client.post("/v1/settings/publish", json={"revision": draft.json()["revision"]})
    assert result.status_code == 200, result.text


async def test_override_false_and_clear_survive_reads_and_other_windows(logged_in: TestEnv) -> None:
    first, second = await create_window(logged_in), await create_window(logged_in)
    initial = (await logged_in.client.get(f"/v1/sessions/{first}/dialogue-policy")).json()
    assert initial["global_value"] is True
    assert initial["override"] is None and initial["effective"] is True
    saved = await set_policy(logged_in, first, False)
    assert saved["source"] == "session" and saved["effective"] is False
    assert saved["session_revision"] == 1
    other = (await logged_in.client.get(f"/v1/sessions/{second}/dialogue-policy")).json()
    assert other["source"] == "global" and other["effective"] is True
    inherited = await set_policy(logged_in, first, None)
    assert inherited["source"] == "global" and inherited["effective"] is True
    assert inherited["session_revision"] == 2
    await publish(logged_in, False)
    assert (await logged_in.client.get(f"/v1/sessions/{first}/dialogue-policy")).json()["effective"] is False
    explicit_true = await set_policy(logged_in, first, True)
    assert explicit_true["source"] == "session" and explicit_true["effective"] is True
    assert (await logged_in.client.get(f"/v1/sessions/{second}/dialogue-policy")).json()["effective"] is False
    await set_policy(logged_in, first, None)
    await publish(logged_in, True)
    assert (await logged_in.client.get(f"/v1/sessions/{first}/dialogue-policy")).json()["effective"] is True


async def test_six_field_partial_updates_and_persona_tristate_persist(
    logged_in: TestEnv, caplog: pytest.LogCaptureFixture
) -> None:
    window = await create_window(logged_in)
    created = await logged_in.client.post(
        "/v1/personas", json={"display_name": "会话覆盖", "system_prompt": "独立人格"}
    )
    assert created.status_code == 201, created.text
    persona_id = created.json()["persona_id"]

    initial = (
        await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")
    ).json()
    global_fields = initial["fields"]
    budget = 20 if global_fields["group_turn_budget"]["global_value"] != 20 else 19
    updates = {
        "persona_id": persona_id,
        "private_may_stay_silent": not global_fields["private_may_stay_silent"]["global_value"],
        "group_mention_requires_reply": not global_fields["group_mention_requires_reply"]["global_value"],
        "group_attention_deferred": not global_fields["group_attention_deferred"]["global_value"],
        "group_turn_budget": budget,
        "rest_may_skip": not global_fields["rest_may_skip"]["global_value"],
    }
    body = {"set": updates, "clear": []}
    proposed_response = await preview_operation(logged_in, window, body)
    assert proposed_response.status_code == 200, proposed_response.text
    proposed = proposed_response.json()["proposed"]["fields"]
    for field, value in updates.items():
        assert proposed[field] == {
            "global_value": global_fields[field]["global_value"],
            "instance_override": None,
            "has_instance_override": False,
            "session_override": value,
            "has_session_override": True,
            "effective": value,
            "source": "session",
        }

    with caplog.at_level(logging.INFO, logger="ling.audit"):
        saved_response = await save_operation(
            logged_in, window, body, proposed_response.json()["preview_token"]
        )
    assert saved_response.status_code == 200, saved_response.text
    audit_records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "audit_window_dialogue_policy_changed"
    ]
    assert len(audit_records) == 1
    audit = audit_records[0].attributes
    assert audit["attributes"]["set_fields"] == sorted(updates)
    assert audit["attributes"]["revision"] == 1
    assert persona_id not in str(audit)
    assert "独立人格" not in str(audit)
    saved = saved_response.json()
    assert saved["session_revision"] == 1
    for field, value in updates.items():
        assert saved["fields"][field]["session_override"] == value
        assert saved["fields"][field]["has_session_override"] is True
        assert saved["fields"][field]["effective"] == value
        assert saved["fields"][field]["source"] == "session"

    next_budget = 1 if budget != 1 else 2
    partial_body = {"set": {"group_turn_budget": next_budget}, "clear": []}
    partial_preview = await preview_operation(logged_in, window, partial_body)
    assert partial_preview.status_code == 200, partial_preview.text
    partial_save = await save_operation(
        logged_in, window, partial_body, partial_preview.json()["preview_token"]
    )
    assert partial_save.status_code == 200, partial_save.text
    partial = partial_save.json()
    assert partial["session_revision"] == 2
    for field, value in updates.items():
        expected = next_budget if field == "group_turn_budget" else value
        assert partial["fields"][field]["effective"] == expected

    null_body = {"set": {"persona_id": None}, "clear": []}
    null_preview = await preview_operation(logged_in, window, null_body)
    assert null_preview.status_code == 200, null_preview.text
    null_save = await save_operation(
        logged_in, window, null_body, null_preview.json()["preview_token"]
    )
    assert null_save.status_code == 200, null_save.text
    explicit_null = null_save.json()["fields"]["persona_id"]
    assert explicit_null == {
        "global_value": global_fields["persona_id"]["global_value"],
        "instance_override": None,
        "has_instance_override": False,
        "session_override": None,
        "has_session_override": True,
        "effective": None,
        "source": "session",
    }
    assert null_save.json()["session_revision"] == 3

    clear_some = {"set": {}, "clear": ["persona_id", "rest_may_skip"]}
    clear_preview = await preview_operation(logged_in, window, clear_some)
    assert clear_preview.status_code == 200, clear_preview.text
    clear_save = await save_operation(
        logged_in, window, clear_some, clear_preview.json()["preview_token"]
    )
    assert clear_save.status_code == 200, clear_save.text
    cleared = clear_save.json()
    assert cleared["session_revision"] == 4
    for field in ("persona_id", "rest_may_skip"):
        value = cleared["fields"][field]
        assert value["has_session_override"] is False
        assert value["session_override"] is None
        assert value["effective"] == value["global_value"]
        assert value["source"] == "global"
    assert cleared["fields"]["private_may_stay_silent"]["source"] == "session"
    assert cleared["fields"]["group_turn_budget"]["effective"] == next_budget

    clear_rest = {
        "set": {},
        "clear": [
            "private_may_stay_silent",
            "group_mention_requires_reply",
            "group_attention_deferred",
            "group_turn_budget",
        ],
    }
    final_preview = await preview_operation(logged_in, window, clear_rest)
    assert final_preview.status_code == 200, final_preview.text
    final_save = await save_operation(
        logged_in, window, clear_rest, final_preview.json()["preview_token"]
    )
    assert final_save.status_code == 200, final_save.text
    final = final_save.json()
    assert final["session_revision"] == 5
    for value in final["fields"].values():
        assert value["has_session_override"] is False
        assert value["session_override"] is None
        assert value["effective"] == value["global_value"]
        assert value["source"] == "global"

    async with session_scope() as session:
        persisted = await session.get(WindowDialogueOverride, window)
        assert persisted is not None
        assert persisted.overrides == {}
        assert persisted.revision == 5


@pytest.mark.parametrize(
    "body",
    [
        {"set": {"unexpected": True}, "clear": []},
        {"set": {"private_may_stay_silent": 1}, "clear": []},
        {"set": {"private_may_stay_silent": None}, "clear": []},
        {"set": {"group_mention_requires_reply": "true"}, "clear": []},
        {"set": {"group_attention_deferred": 0}, "clear": []},
        {"set": {"rest_may_skip": None}, "clear": []},
        {"set": {"group_turn_budget": True}, "clear": []},
        {"set": {"group_turn_budget": 0}, "clear": []},
        {"set": {"group_turn_budget": 21}, "clear": []},
        {"set": {"group_turn_budget": "7"}, "clear": []},
        {"set": {"persona_id": ""}, "clear": []},
        {"set": {"persona_id": "missing-persona"}, "clear": []},
        {"set": {}, "clear": ["unknown-field"]},
        {"set": {"private_may_stay_silent": False}, "clear": ["private_may_stay_silent"]},
        {"set": {}, "clear": ["rest_may_skip", "rest_may_skip"]},
        {"set": {}, "clear": []},
    ],
)
async def test_invalid_six_field_changes_leave_override_untouched(
    logged_in: TestEnv, body: dict
) -> None:
    window = await create_window(logged_in)
    response = await preview_operation(logged_in, window, body)
    assert response.status_code == 422, response.text
    current = (
        await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")
    ).json()
    assert current["session_revision"] == 0
    assert all(not field["has_session_override"] for field in current["fields"].values())


async def test_persona_revision_change_invalidates_preview(logged_in: TestEnv) -> None:
    created = await logged_in.client.post(
        "/v1/personas", json={"display_name": "预览人格", "system_prompt": "初始"}
    )
    assert created.status_code == 201, created.text
    persona_id = created.json()["persona_id"]
    window = await create_window(logged_in)
    body = {"set": {"persona_id": persona_id}, "clear": []}
    preview_response = await preview_operation(logged_in, window, body)
    assert preview_response.status_code == 200, preview_response.text

    update = await logged_in.client.put(
        f"/v1/personas/{persona_id}", json={"system_prompt": "修订后"}
    )
    assert update.status_code == 200, update.text
    saved = await save_operation(
        logged_in, window, body, preview_response.json()["preview_token"]
    )
    assert saved.status_code == 409, saved.text
    current = (
        await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")
    ).json()
    assert current["session_revision"] == 0
    assert current["fields"]["persona_id"]["has_session_override"] is False


async def test_persona_delete_is_blocked_while_session_override_references_it(
    logged_in: TestEnv,
) -> None:
    created = await logged_in.client.post(
        "/v1/personas", json={"display_name": "被会话引用", "system_prompt": "测试"}
    )
    assert created.status_code == 201, created.text
    persona_id = created.json()["persona_id"]
    window = await create_window(logged_in)
    body = {"set": {"persona_id": persona_id}, "clear": []}
    preview_response = await preview_operation(logged_in, window, body)
    assert preview_response.status_code == 200, preview_response.text
    saved = await save_operation(
        logged_in, window, body, preview_response.json()["preview_token"]
    )
    assert saved.status_code == 200, saved.text

    blocked = await logged_in.client.delete(f"/v1/personas/{persona_id}")
    assert blocked.status_code == 409, blocked.text
    assert "persona_session_override:1" in blocked.text

    clear = {"set": {}, "clear": ["persona_id"]}
    clear_preview = await preview_operation(logged_in, window, clear)
    assert clear_preview.status_code == 200, clear_preview.text
    clear_saved = await save_operation(
        logged_in, window, clear, clear_preview.json()["preview_token"]
    )
    assert clear_saved.status_code == 200, clear_saved.text
    deleted = await logged_in.client.delete(f"/v1/personas/{persona_id}")
    assert deleted.status_code == 204, deleted.text


async def test_previews_are_bound_to_operation_window_time_and_both_revisions(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await create_window(logged_in), await create_window(logged_in)
    token = (await preview(logged_in, first, False))["preview_token"]
    assert (await save(logged_in, first, True, token)).status_code == 409
    assert (await save(logged_in, second, False, token)).status_code == 409
    assert (await save(logged_in, first, False, token + "x")).status_code == 409
    await publish(logged_in, False)
    await publish(logged_in, True)
    assert (await save(logged_in, first, False, token)).status_code == 409
    old = (await preview(logged_in, first, False))["preview_token"]
    await set_policy(logged_in, first, True)
    await set_policy(logged_in, first, None)
    assert (await save(logged_in, first, False, old)).status_code == 409
    before_archive = (await preview(logged_in, first, False))["preview_token"]
    assert (await logged_in.client.patch(f"/v1/sessions/{first}", json={"archived": True})).status_code == 200
    assert (await save(logged_in, first, False, before_archive)).status_code == 409
    assert (await logged_in.client.patch(f"/v1/sessions/{first}", json={"archived": False})).status_code == 200
    assert (await save(logged_in, first, False, before_archive)).status_code == 409
    monkeypatch.setattr("ling.api.routes.window_policy._PREVIEW_SECONDS", -1)
    expired = (await preview(logged_in, first, False))["preview_token"]
    assert (await save(logged_in, first, False, expired)).status_code == 409


async def test_concurrent_save_uses_one_preview_at_most_once(logged_in: TestEnv) -> None:
    window = await create_window(logged_in)
    token = (await preview(logged_in, window, False))["preview_token"]
    responses = await asyncio.gather(
        save(logged_in, window, False, token), save(logged_in, window, False, token)
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    view = (await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")).json()
    assert view["override"] is False and view["session_revision"] == 1


@pytest.mark.parametrize("body", [
    {"set": {FIELD: "false"}, "clear": []},
    {"set": {FIELD: None}, "clear": []},
    {"set": {"unknown": True}, "clear": []},
    {"set": {FIELD: False}, "clear": [FIELD]},
    {"set": {}, "clear": [FIELD, FIELD]},
    {"set": {}, "clear": []},
    {"set": {FIELD: False}, "clear": [], "extra": "unexpected"},
])
async def test_invalid_policy_operations_are_rejected(logged_in: TestEnv, body: dict) -> None:
    window = await create_window(logged_in)
    result = await logged_in.client.post(
        f"/v1/sessions/{window}/dialogue-policy/preview", json=body, headers=ORIGIN
    )
    assert result.status_code == 422, result.text


async def test_scope_and_write_guards(logged_in: TestEnv) -> None:
    window = await create_window(logged_in)
    async with session_scope() as session:
        other = await IdentityService(session).create_user(username="member", password="member-pass")
        service = ConversationService(session, actor_id=logged_in.settings.actor_id)
        foreign = await service.create_window(subject_id=other.subject_id)
        channel = await service.create_window(subject_id=logged_in.subject_id, channel="onebot")
        group = await service.create_window(subject_id=logged_in.subject_id, channel="onebot")
        channel.origin_status = "unknown"
        group.origin_status = "unknown"
        group.visibility = "group_scope"
        foreign_id, channel_id, group_id = foreign.id, channel.id, group.id
    for inaccessible in (foreign_id, "missing-window"):
        result = await logged_in.client.get(f"/v1/sessions/{inaccessible}/dialogue-policy")
        assert result.status_code == 404, result.text
    for manageable in (channel_id, group_id):
        result = await logged_in.client.get(f"/v1/sessions/{manageable}/dialogue-policy")
        assert result.status_code == 200, result.text
    url = f"/v1/sessions/{window}/dialogue-policy/preview"
    no_origin = logged_in.client.build_request("POST", url, json=change(False))
    del no_origin.headers["Origin"]
    assert (await logged_in.client.send(no_origin)).status_code == 403
    assert (await logged_in.client.post(url, json=change(False), headers={"Origin": "https://elsewhere.test"})).status_code == 403
    assert (await logged_in.client.post(url, content="{}", headers={**ORIGIN, "Content-Type": "text/plain"})).status_code == 422
    async with AsyncClient(transport=ASGITransport(app=logged_in.app), base_url="http://testserver") as member:
        login = await member.post("/v1/auth/login", json={"username": "member", "password": "member-pass"})
        assert login.status_code == 200, login.text
        assert (await member.get(f"/v1/sessions/{window}/dialogue-policy")).status_code == 404
        assert (await member.get(f"/v1/sessions/{channel_id}/dialogue-policy")).status_code == 404
        assert (await member.get(f"/v1/sessions/{group_id}/dialogue-policy")).status_code == 404
        assert (await member.get(f"/v1/sessions/{foreign_id}/dialogue-policy")).status_code == 200
        assert (await member.post(f"/v1/sessions/{foreign_id}/dialogue-policy/preview", json=change(False), headers=ORIGIN)).status_code == 403


class SilentRouter:
    def __init__(self) -> None:
        self.calls = []

    async def stream(self, config, **kwargs) -> AsyncIterator:  # noqa: ANN001, ANN003
        self.calls.append(config)
        text = '{"respond": false, "reason": "这次不接话", "length": "normal"}'
        yield ModelChunk(delta=text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=text)


async def test_http_decision_uses_override_and_restores_global(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.api.routes import chat

    window = await create_window(logged_in)
    router = SilentRouter()
    logged_in.app.state.router = router
    generation_configs = []

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generation_configs.append(kwargs["config"])
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    async def available() -> Situation:
        return Situation(availability="available")

    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    await set_policy(logged_in, window, False)
    first = await logged_in.client.post(
        f"/v1/sessions/{window}/turns", json={"client_turn_id": "first", "text": "你好"}
    )
    assert first.status_code == 200 and '"decision": "reply"' in first.text
    assert router.calls == []
    assert generation_configs[0].dialogue.private_may_stay_silent is False
    await set_policy(logged_in, window, None)
    second = await logged_in.client.post(
        f"/v1/sessions/{window}/turns", json={"client_turn_id": "second", "text": "你好"}
    )
    assert second.status_code == 200 and '"decision": "silence"' in second.text
    assert len(router.calls) == 1 and router.calls[0].dialogue.private_may_stay_silent is True
    async with session_scope() as session:
        messages = (await session.scalars(select(Message).where(Message.window_id == window))).all()
        turns = (
            await session.scalars(select(Turn).where(Turn.window_id == window))
        ).all()
        snapshots = (
            await session.scalars(
                select(ConversationAttemptSnapshot).where(
                    ConversationAttemptSnapshot.turn_id.in_([turn.id for turn in turns])
                )
            )
        ).all()
    assert len([msg for msg in messages if msg.role == "user"]) == 2
    assert any(msg.handled_as == "ignored" for msg in messages)
    turn_ids = {turn.client_turn_id: turn.id for turn in turns}
    assert {snapshot.turn_id for snapshot in snapshots} == {turn_ids["first"]}
    assert [(snapshot.turn_id, snapshot.attempt) for snapshot in snapshots] == [
        (turn_ids["first"], 1)
    ]


async def test_config_is_fixed_between_decision_and_generation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.api.routes import chat

    window = await create_window(logged_in)
    await set_policy(logged_in, window, False)
    generation_configs = []
    original = chat._evaluate_turn_decision

    async def evaluate(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = await original(*args, **kwargs)
        await set_policy(logged_in, window, True)
        return result

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generation_configs.append(kwargs["config"])
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    monkeypatch.setattr(chat, "_evaluate_turn_decision", evaluate)
    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    result = await logged_in.client.post(
        f"/v1/sessions/{window}/turns", json={"client_turn_id": "snapshot", "text": "你好"}
    )
    assert result.status_code == 200, result.text
    assert generation_configs[0].dialogue.private_may_stay_silent is False
    assert (await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")).json()["effective"] is True


async def test_asr_submission_uses_policy_at_submission_time(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.domains.speech.asr_runtime import process_job, submit_job
    from tests.integration.test_asr_input import setup

    window, asset = await setup(logged_in, monkeypatch)
    router = SilentRouter()
    logged_in.app.state.router = router

    async def available() -> Situation:
        return Situation(availability="available")

    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    created = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions",
        json={"asset_id": asset, "client_request_id": "policy-asr", "mode": "draft"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    assert await process_job(logged_in.app, job_id)
    state = (await logged_in.client.get(f"/v1/sessions/{window}/transcriptions/{job_id}")).json()
    assert state["state"] == "succeeded"
    await set_policy(logged_in, window, True)
    committed = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{job_id}/commit",
        json={"result_revision": state["revision"]},
    )
    assert committed.status_code == 200, committed.text
    await submit_job(logged_in.app, job_id)
    assert len(router.calls) == 1
    assert router.calls[0].dialogue.private_may_stay_silent is True
    async with session_scope() as session:
        messages = (await session.scalars(select(Message).where(Message.window_id == window))).all()
    assert any(msg.text == "蓝色风筝序号七十三" and msg.handled_as == "ignored" for msg in messages)


async def test_database_snapshot_never_mixes_old_global_and_new_override(
    logged_in: TestEnv,
) -> None:
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import runtime_window_config

    window = await create_window(logged_in)
    await set_policy(logged_in, window, False)
    global_read = asyncio.Event()
    continue_read = asyncio.Event()

    class PausedConfig:
        def __init__(self, config: ConfigService) -> None:
            self.config = config

        def with_session(self, session):  # noqa: ANN001, ANN202
            return PausedConfig(self.config.with_session(session))

        async def effective(self):  # noqa: ANN202
            current = await self.config.effective()
            global_read.set()
            await continue_read.wait()
            return current

    async with session_scope() as session:
        config = PausedConfig(ConfigService(session, logged_in.app.state.secret_box))
        task = asyncio.create_task(runtime_window_config(config, window))
        try:
            await asyncio.wait_for(global_read.wait(), timeout=5)
            await publish(logged_in, False)
            await set_policy(logged_in, window, None)
        finally:
            continue_read.set()
        result = await asyncio.wait_for(task, timeout=5)
    # 旧状态为全局 True、覆盖 False，新状态为全局 False、无覆盖。
    # 只有旧全局与新覆盖混合才会错误地得到 True。
    assert result.dialogue.private_may_stay_silent is False
    current = (await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")).json()
    assert current["global_value"] is False and current["override"] is None


async def test_retry_uses_current_policy(logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from ling.api.routes import chat
    from ling.infra.models import Turn

    window = await create_window(logged_in)
    await set_policy(logged_in, window, False)
    generation_configs = []

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generation_configs.append(kwargs["config"])
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    response = await logged_in.client.post(
        f"/v1/sessions/{window}/turns", json={"client_turn_id": "retry-input", "text": "你好"}
    )
    assert response.status_code == 200, response.text
    async with session_scope() as session:
        turn = await session.scalar(select(Turn).where(Turn.window_id == window))
        assert turn is not None
        turn.state = "failed"
        turn_id = turn.id
    await set_policy(logged_in, window, True)
    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200 and '"retried": true' in retry.text
    assert [item.dialogue.private_may_stay_silent for item in generation_configs] == [False, True]


async def test_two_windows_use_independent_decisions(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.api.routes import chat

    first, second = await create_window(logged_in), await create_window(logged_in)
    await set_policy(logged_in, first, False)
    await set_policy(logged_in, second, True)
    router = SilentRouter()
    logged_in.app.state.router = router
    generated = []

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generated.append((kwargs["window_id"], kwargs["config"].dialogue.private_may_stay_silent))
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    async def available() -> Situation:
        return Situation(availability="available")

    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    for window, key, expected in [(first, "a", "reply"), (second, "b", "silence")]:
        result = await logged_in.client.post(
            f"/v1/sessions/{window}/turns", json={"client_turn_id": key, "text": "你好"}
        )
        assert result.status_code == 200 and f'"decision": "{expected}"' in result.text
    assert generated == [(first, False)]
    assert [call.dialogue.private_may_stay_silent for call in router.calls] == [True]
    await set_policy(logged_in, first, None)
    await publish(logged_in, False)
    first_policy = (await logged_in.client.get(f"/v1/sessions/{first}/dialogue-policy")).json()
    second_policy = (await logged_in.client.get(f"/v1/sessions/{second}/dialogue-policy")).json()
    assert first_policy["effective"] is False
    assert second_policy["effective"] is True
    next_turn = await logged_in.client.post(
        f"/v1/sessions/{second}/turns", json={"client_turn_id": "c", "text": "你好"}
    )
    assert next_turn.status_code == 200 and '"decision": "silence"' in next_turn.text
    assert [call.dialogue.private_may_stay_silent for call in router.calls] == [True, True]


async def test_asr_explicit_false_overrides_true_global(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """转写提交使用显式 false，而不是因缺少覆盖而沿用 true。"""
    from ling.api.routes import chat
    from ling.domains.speech.asr_runtime import process_job, submit_job
    from tests.integration.test_asr_input import setup

    window, asset = await setup(logged_in, monkeypatch)
    await publish(logged_in, True)
    saved = await set_policy(logged_in, window, False)
    assert saved["global_value"] is True and saved["effective"] is False
    router = SilentRouter()
    logged_in.app.state.router = router
    decision_values = []
    generation_values = []
    original = chat._evaluate_turn_decision

    async def evaluate(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        decision_values.append((await args[4].effective()).dialogue.private_may_stay_silent)
        return await original(*args, **kwargs)

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generation_values.append(kwargs["config"].dialogue.private_may_stay_silent)
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    async def available() -> Situation:
        return Situation(availability="available")

    monkeypatch.setattr(chat, "_evaluate_turn_decision", evaluate)
    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    created = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions",
        json={"asset_id": asset, "client_request_id": "independent-asr", "mode": "draft"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    assert await process_job(logged_in.app, job_id)
    state = (await logged_in.client.get(f"/v1/sessions/{window}/transcriptions/{job_id}")).json()
    committed = await logged_in.client.post(
        f"/v1/sessions/{window}/transcriptions/{job_id}/commit",
        json={"result_revision": state["revision"]},
    )
    assert committed.status_code == 200, committed.text
    await submit_job(logged_in.app, job_id)
    assert decision_values == [False]
    assert generation_values == [False]
    assert router.calls == []
    async with session_scope() as session:
        messages = (await session.scalars(select(Message).where(Message.window_id == window))).all()
    assert any(msg.text == "蓝色风筝序号七十三" and msg.handled_as == "replied" for msg in messages)


async def test_policy_preview_token_is_bound_to_the_user(logged_in: TestEnv) -> None:
    """同一主体的另一位 owner 也不能使用本人创建的预览。"""
    window = await create_window(logged_in)
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="second-owner", password="owner-password", role="owner",
            subject_id=logged_in.subject_id,
        )
    token = (await preview(logged_in, window, False))["preview_token"]
    async with AsyncClient(transport=ASGITransport(app=logged_in.app), base_url="http://testserver") as other:
        login = await other.post("/v1/auth/login", json={"username": "second-owner", "password": "owner-password"})
        assert login.status_code == 200
        assert (await other.get(f"/v1/sessions/{window}/dialogue-policy")).status_code == 200
        response = await other.put(
            f"/v1/sessions/{window}/dialogue-policy",
            json={**change(False), "preview_token": token}, headers=ORIGIN,
        )
        assert response.status_code == 409, response.text
    current = (await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")).json()
    assert current["override"] is None and current["session_revision"] == 0
    assert (await save(logged_in, window, False, token)).status_code == 200


async def test_policy_write_is_confined_to_owned_subject(logged_in: TestEnv) -> None:
    """其他主体的 owner 不能读取、预览或保存该会话的设置。"""
    from starlette.requests import Request

    from ling.api.routes.window_policy import _read_token, _token

    window = await create_window(logged_in)
    token = (await preview(logged_in, window, False))["preview_token"]
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="foreign-owner", password="owner-password", role="owner"
        )
        await IdentityService(session).create_user(
            username="same-subject-member", password="member-password", role="member",
            subject_id=logged_in.subject_id,
        )
    async with AsyncClient(transport=ASGITransport(app=logged_in.app), base_url="http://testserver") as member:
        login = await member.post(
            "/v1/auth/login", json={"username": "same-subject-member", "password": "member-password"}
        )
        assert login.status_code == 200
        assert (await member.get(f"/v1/sessions/{window}/dialogue-policy")).status_code == 200
        assert (await member.post(
            f"/v1/sessions/{window}/dialogue-policy/preview", json=change(False), headers=ORIGIN
        )).status_code == 403
        assert (await member.put(
            f"/v1/sessions/{window}/dialogue-policy",
            json={**change(False), "preview_token": token}, headers=ORIGIN,
        )).status_code == 403
    async with AsyncClient(transport=ASGITransport(app=logged_in.app), base_url="http://testserver") as foreign:
        login = await foreign.post("/v1/auth/login", json={"username": "foreign-owner", "password": "owner-password"})
        assert login.status_code == 200
        own_window = await foreign.post("/v1/sessions", json={"title": "另一主体"})
        assert own_window.status_code == 201
        own_id = own_window.json()["session_id"]
        own_preview = await foreign.post(
            f"/v1/sessions/{own_id}/dialogue-policy/preview", json=change(False), headers=ORIGIN
        )
        assert own_preview.status_code == 200
        own_token = own_preview.json()["preview_token"]
        assert (await foreign.get(f"/v1/sessions/{window}/dialogue-policy")).status_code == 404
        assert (await foreign.post(
            f"/v1/sessions/{window}/dialogue-policy/preview", json=change(False), headers=ORIGIN
        )).status_code == 404
        denied = await foreign.put(
            f"/v1/sessions/{window}/dialogue-policy",
            json={**change(False), "preview_token": token}, headers=ORIGIN,
        )
        assert denied.status_code == 409, denied.text
        # 模拟有效令牌已绑定到另一个主体的目标，确保归属校验本身仍然拒绝。
        request = Request({"type": "http", "app": logged_in.app, "headers": []})
        payload = _read_token(request, own_token)
        payload["window_id"] = window
        forged_token = _token(request, payload)
        denied = await foreign.put(
            f"/v1/sessions/{window}/dialogue-policy",
            json={**change(False), "preview_token": forged_token}, headers=ORIGIN,
        )
        assert denied.status_code == 404, denied.text
        own_save = await foreign.put(
            f"/v1/sessions/{own_id}/dialogue-policy",
            json={**change(False), "preview_token": own_token}, headers=ORIGIN,
        )
        assert own_save.status_code == 200, own_save.text
    current = (await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")).json()
    assert current["override"] is None and current["session_revision"] == 0
    assert (await save(logged_in, window, False, token)).status_code == 200
