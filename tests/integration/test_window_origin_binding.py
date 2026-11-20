"""平台来源绑定、影响范围与策略审计的 PostgreSQL 回归。"""
from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.conversation_policy_audit import ConversationPolicyAudit
from ling.infra.db import session_scope
from ling.infra.models import ConnectorInstance, Message, Window, WindowDialogueOverride
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
ORIGIN = {"Origin": "http://testserver"}
INSTANCE_URL = "/v1/settings/connectors/onebot/instances/alpha/dialogue-policy"


async def _instance(env: TestEnv, instance_id: str = "alpha") -> None:
    async with session_scope() as session:
        session.add(ConnectorInstance(
            scope="onebot", instance_id=instance_id,
            display_name=f"测试-{instance_id}", enabled=True,
        ))


async def _window(env: TestEnv, *, title: str = "onebot-alpha:private:100200301") -> str:
    async with session_scope() as session:
        service = ConversationService(session, actor_id=env.settings.actor_id)
        window = await service.create_window(
            subject_id=env.subject_id, title=title, channel="onebot"
        )
        await service.add_message(
            window.id, role="user", text="先前消息", sender_subject_id=env.subject_id
        )
        return window.id


async def _save_instance(env: TestEnv, url: str, change: dict) -> dict:
    preview = await env.client.post(f"{url}/preview", json=change, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    result = await env.client.put(
        url, json={**change, "preview_token": preview.json()["preview_token"]}, headers=ORIGIN
    )
    assert result.status_code == 200, result.text
    return result.json()


async def test_preview_binding_and_unbinding_preserve_history(logged_in: TestEnv) -> None:
    env = logged_in
    await _instance(env)
    window_id = await _window(env)
    current = (await env.client.get(INSTANCE_URL)).json()
    chosen = not current["fields"]["private_may_stay_silent"]["global_value"]
    await _save_instance(env, INSTANCE_URL, {"set": {"private_may_stay_silent": chosen}, "clear": []})
    url = f"/v1/sessions/{window_id}/origin-binding"
    options = await env.client.get(url)
    assert options.status_code == 200, options.text
    assert options.json()["candidates"] == [
        {"scope": "onebot", "instance_id": "alpha", "display_name": "测试-alpha", "enabled": True}
    ]
    body = {"scope": "onebot", "instance_id": "alpha"}
    preview = await env.client.post(f"{url}/preview", json=body, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    document = preview.json()
    assert document["current"]["origin_status"] == "unknown"
    assert document["current"]["fields"]["private_may_stay_silent"]["effective"] is not chosen
    assert document["proposed"]["fields"]["private_may_stay_silent"]["effective"] is chosen
    assert document["proposed"]["fields"]["private_may_stay_silent"]["source"] == "instance"
    async with session_scope() as session:
        before = await session.get(Window, window_id)
        historical_persona = before.persona_id
        original_messages = [(m.id, m.text) for m in (await session.scalars(
            select(Message).where(Message.window_id == window_id)
        )).all()]
        assert before.origin_status == "unknown" and not before.origin_manually_bound
    saved = await env.client.put(
        url, json={**body, "preview_token": document["preview_token"]}, headers=ORIGIN
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["origin_status"] == "known"
    assert saved.json()["instance_id"] == "alpha"
    assert saved.json()["fields"]["private_may_stay_silent"]["effective"] is chosen
    async with session_scope() as session:
        row = await session.get(Window, window_id)
        assert row.origin_manually_bound is True
        assert row.persona_id == historical_persona
        messages = [(m.id, m.text) for m in (await session.scalars(
            select(Message).where(Message.window_id == window_id)
        )).all()]
        assert messages == original_messages
    assert (await env.client.post(f"{url}/preview", json=body, headers=ORIGIN)).status_code == 409
    cleared = {"scope": None, "instance_id": None}
    unbind = await env.client.post(f"{url}/preview", json=cleared, headers=ORIGIN)
    assert unbind.status_code == 200, unbind.text
    assert unbind.json()["proposed"]["fields"]["private_may_stay_silent"]["source"] == "global"
    result = await env.client.put(
        url, json={**cleared, "preview_token": unbind.json()["preview_token"]}, headers=ORIGIN
    )
    assert result.status_code == 200, result.text
    assert result.json()["origin_status"] == "unknown"
    async with session_scope() as session:
        row = await session.get(Window, window_id)
        assert row.origin_scope is None and row.origin_instance_id is None
        assert row.origin_manually_bound is False
        records = list((await session.scalars(select(ConversationPolicyAudit).where(
            ConversationPolicyAudit.target_id == window_id
        ).order_by(ConversationPolicyAudit.created_at))).all())
        assert [record.action for record in records] == [
            "window_origin_binding_changed", "window_origin_binding_changed"
        ]


async def test_binding_rejects_invalid_targets_stale_preview_and_unauthorized(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    await _instance(env)
    await _instance(env, "bravo")
    window_id = await _window(env)
    url = f"/v1/sessions/{window_id}/origin-binding"
    body = {"scope": "onebot", "instance_id": "alpha"}
    no_origin = env.client.build_request("POST", f"{url}/preview", json=body)
    del no_origin.headers["Origin"]
    assert (await env.client.send(no_origin)).status_code == 403
    assert (await env.client.post(f"{url}/preview", json={"scope": "qqbot", "instance_id": "alpha"}, headers=ORIGIN)).status_code == 422
    assert (await env.client.post(f"{url}/preview", json={"scope": "onebot", "instance_id": "bravo"}, headers=ORIGIN)).status_code == 409
    assert (await env.client.post(f"{url}/preview", json={"scope": "onebot", "instance_id": "missing"}, headers=ORIGIN)).status_code == 404
    preview = await env.client.post(f"{url}/preview", json=body, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    assert (await env.client.put(url, json={**body, "preview_token": token + "bad"}, headers=ORIGIN)).status_code == 409
    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", "alpha"))
        row.enabled = False
    stale = await env.client.put(url, json={**body, "preview_token": token}, headers=ORIGIN)
    assert stale.status_code == 409, stale.text
    async with session_scope() as session:
        row = await session.get(Window, window_id)
        assert row.origin_status == "unknown"
        await IdentityService(session).create_user(
            username="member", password="member-password", role="member", subject_id=env.subject_id
        )
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://testserver") as member:
        login = await member.post("/v1/auth/login", json={"username": "member", "password": "member-password"})
        assert login.status_code == 200, login.text
        assert (await member.get(url)).status_code == 403
        assert (await member.post(f"{url}/preview", json=body, headers=ORIGIN)).status_code == 403


async def test_binding_rejects_existing_exact_window_and_deleted_target(logged_in: TestEnv) -> None:
    env = logged_in
    await _instance(env)
    window_id = await _window(env)
    url = f"/v1/sessions/{window_id}/origin-binding/preview"
    body = {"scope": "onebot", "instance_id": "alpha"}
    async with session_scope() as session:
        row = await session.get(Window, window_id)
        session.add(Window(
            id="other-window", actor_id=row.actor_id, subject_id=row.subject_id,
            persona_id="old-persona", channel="onebot", visibility="subject_private",
            title=row.title, connector_window_ref=row.title, origin_status="known",
            origin_scope="onebot", origin_instance_id="alpha",
        ))
    conflict = await env.client.post(url, json=body, headers=ORIGIN)
    assert conflict.status_code == 409 and "已有相同" in conflict.text
    async with session_scope() as session:
        duplicate = await session.get(Window, "other-window")
        await session.delete(duplicate)
        connector = await session.get(ConnectorInstance, ("onebot", "alpha"))
        from ling.infra.models import utcnow
        connector.deleted_at = utcnow()
    assert (await env.client.post(url, json=body, headers=ORIGIN)).status_code == 404


async def test_instance_preview_is_read_only_and_each_real_change_is_audited(
    logged_in: TestEnv, caplog: pytest.LogCaptureFixture
) -> None:
    env = logged_in
    url = "/v1/settings/connectors/onebot/instances/default/dialogue-policy"
    async with session_scope() as session:
        assert await session.get(ConnectorInstance, ("onebot", "default")) is None
    first = {"set": {"private_may_stay_silent": False}, "clear": []}
    preview = await env.client.post(f"{url}/preview", json=first, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    async with session_scope() as session:
        assert await session.get(ConnectorInstance, ("onebot", "default")) is None
        assert not (await session.scalars(select(ConversationPolicyAudit))).all()
    with caplog.at_level(logging.INFO, logger="ling.audit"):
        saved = await env.client.put(url, json={**first, "preview_token": preview.json()["preview_token"]}, headers=ORIGIN)
        assert saved.status_code == 200, saved.text
        assert saved.json()["instance_revision"] == 1
        second = {"set": {"private_may_stay_silent": True}, "clear": []}
        updated = await _save_instance(env, url, second)
        assert updated["instance_revision"] == 2
        no_op = await _save_instance(env, url, second)
        assert no_op["instance_revision"] == 2
    events = [r for r in caplog.records if getattr(r, "event_name", "") == "audit_instance_dialogue_policy_changed"]
    assert len(events) == 2
    async with session_scope() as session:
        rows = list((await session.scalars(select(ConversationPolicyAudit).where(
            ConversationPolicyAudit.action == "instance_dialogue_policy_changed"
        ).order_by(ConversationPolicyAudit.revision))).all())
    assert [(row.revision, row.set_fields, row.clear_fields) for row in rows] == [
        (1, ["private_may_stay_silent"], []), (2, ["private_may_stay_silent"], [])
    ]


async def test_instance_impact_excludes_other_private_subjects_and_unknown_sources(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    await _instance(env)
    async with session_scope() as session:
        for name, subject, visibility, archived, status, actor in [
            ("active", env.subject_id, "subject_private", False, "known", env.settings.actor_id),
            ("archive", env.subject_id, "subject_private", True, "known", env.settings.actor_id),
            ("shadow", env.subject_id, "subject_private", False, "known", env.settings.actor_id),
            ("group", "other", "group_scope", False, "known", env.settings.actor_id),
            ("foreign", "other", "subject_private", False, "known", env.settings.actor_id),
            ("foreign-actor", env.subject_id, "subject_private", False, "known", "other-actor"),
            ("unknown", env.subject_id, "subject_private", False, "unknown", env.settings.actor_id),
        ]:
            session.add(Window(
                id=name, actor_id=actor, subject_id=subject, persona_id="p", title=name,
                channel="onebot", visibility=visibility, archived=archived, origin_status=status,
                origin_scope="onebot" if status == "known" else None,
                origin_instance_id="alpha" if status == "known" else None,
            ))
        await session.flush()
        session.add(WindowDialogueOverride(
            window_id="shadow", revision=1,
            overrides={"private_may_stay_silent": False}, updated_by="fixture",
        ))
    view = await env.client.get(INSTANCE_URL)
    global_value = view.json()["fields"]["private_may_stay_silent"]["global_value"]
    change = {"set": {"private_may_stay_silent": not global_value}, "clear": []}
    preview = await env.client.post(f"{INSTANCE_URL}/preview", json=change, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    impact = preview.json()["proposed"]
    assert impact["affected_active_windows"] == 2
    assert impact["affected_archived_windows"] == 1
    assert impact["session_shadowed_windows"] == 1


async def test_audit_failure_rolls_back_policy_and_default_instance(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.api.routes import window_policy

    url = "/v1/settings/connectors/onebot/instances/default/dialogue-policy"
    change = {"set": {"group_turn_budget": 7}, "clear": []}
    preview = await logged_in.client.post(f"{url}/preview", json=change, headers=ORIGIN)
    assert preview.status_code == 200, preview.text

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(window_policy, "_record_policy_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await logged_in.client.put(
            url, json={**change, "preview_token": preview.json()["preview_token"]}, headers=ORIGIN
        )
    async with session_scope() as session:
        assert await session.get(ConnectorInstance, ("onebot", "default")) is None
        assert not (await session.scalars(select(ConversationPolicyAudit))).all()
