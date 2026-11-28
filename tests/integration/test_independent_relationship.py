"""独立验证 QQ 入站、Web 审核、关系账本与后续投递。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling import worker
from ling.infra.db import session_scope
from ling.infra.models import Message, RelationshipEvent
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv
from tests.integration.test_onebot_loop import FakeOneBot, _bind
from tests.integration.test_w0_web import _new_session, _send

from .test_independent_onebot import (
    _completed_prompt,
    _consent,
    _HistoryVerifiedConnector,
    _install,
    _prompt_lines,
    _qq,
    _row,
)

pytestmark = pytest.mark.w0_web


async def _settings(env: TestEnv) -> list[dict]:
    response = await env.client.get("/v1/relationship/preferences")
    assert response.status_code == 200, response.text
    return response.json()["preferences"]


async def _set_expression(
    env: TestEnv, preference_id: str, *, state: str, text: str | None = None,
) -> dict:
    body = {"state": state}
    if text is not None:
        body["text"] = text
    response = await env.client.patch(f"/v1/relationship/preferences/{preference_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _allow_group_history(env: TestEnv) -> None:
    draft = await env.client.put("/v1/settings/draft", json={
        "memory": {"shared_events": "enabled"},
    })
    assert draft.status_code == 200, draft.text
    published = await env.client.post("/v1/settings/publish", json={
        "revision": draft.json()["revision"],
    })
    assert published.status_code == 200, published.text


async def test_web_boundary_applies_to_stream_and_stored_reply(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    router = logged_in.app.state.router
    original = router.stream
    topic = "绝版邮戳"

    async def model(config, *, profile, messages, **kwargs):
        if profile == "dialogue.fast":
            yield ModelChunk(delta="刚见到绝版"), StreamOutcome()
            yield ModelChunk(delta="邮戳了。"), StreamOutcome()
            if "模拟中断" in str(messages[-1].content):
                raise RuntimeError("fixture: transport interrupted after deltas")
            yield ModelChunk(done=True), StreamOutcome(text="刚见到绝版邮戳了。")
            return
        async for chunk in original(config, profile=profile, messages=messages, **kwargs):
            yield chunk

    monkeypatch.setattr(router, "stream", model)
    window_id = await _new_session(logged_in.client)
    before = await _send(logged_in.client, window_id, "今天有什么见闻？")
    assert topic in "".join(data["text"] for kind, data in before if kind == "delta")
    applied = await logged_in.client.post("/v1/relationship/preferences/boundary", json={"text": topic})
    assert applied.status_code == 200, applied.text
    after = await _send(logged_in.client, window_id, "今天又有什么新见闻？")
    assert after[-1][0] == "turn.completed"
    assert after[-1][1]["message"]["text"] == "我们可以聊聊别的。"
    assert topic not in "".join(str(data) for _kind, data in after)
    delivered_turn = after[-1][1]["turn_id"]
    assert any(
        record.name == "ling.api.chat" and getattr(record, "event_name", None) == "turn_completed"
        and getattr(record, "attributes", {}).get("turn_id") == delivered_turn
        and getattr(record, "attributes", {}).get("outcome") == "delivered"
        for record in caplog.records
    )
    response = await logged_in.client.get(f"/v1/sessions/{window_id}/messages")
    assert response.status_code == 200, response.text
    assert response.json()["messages"][-1]["text"] == "我们可以聊聊别的。"

    failed = await _send(logged_in.client, window_id, "我们这次模拟中断。")
    assert failed[-1][0] == "turn.failed"
    assert topic not in "".join(str(data) for _kind, data in failed)


@pytest.mark.parametrize("group_access", ["verified_history", "explicit_publish"])
async def test_qq_expression_approval_edit_and_disable_stays_in_its_window_scope(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, group_access: str,
) -> None:
    transport = FakeOneBot()
    if group_access == "verified_history":
        connector = await _install(logged_in, transport, connector_type=_HistoryVerifiedConnector)

        async def group_sources(_ref: str) -> frozenset[str]:
            return frozenset({"689003"})

        monkeypatch.setattr(connector, "whole_group_history", group_sources)
        await _allow_group_history(logged_in)
    else:
        await _install(logged_in, transport)
        await _consent(logged_in, "public_publish")
    await _bind(logged_in)
    private_input = "我们把青桐街叫作「石榴灯」。"
    await _completed_prompt(await _qq(logged_in, 689001, private_input))
    settings = await _settings(logged_in)
    private, = [rule for rule in settings if "石榴灯" in rule["text"]]
    assert private["state"] == "pending" and private["scope"] == "private"
    assert private["source_message_id"] == (await _row(private_input)).id
    before = await _completed_prompt(await _qq(logged_in, 689002, "青桐街活动要怎么简称？"))
    assert "石榴灯" not in _prompt_lines(before, lanes={"relationship"})

    public_topic = "我们把青桐街叫作「青瓷门」。"
    group_in = await _qq(
        logged_in, 689003, "[CQ:at,qq=10001] " + public_topic, group=True,
    )
    await _completed_prompt(group_in)
    settings = await _settings(logged_in)
    public, = [rule for rule in settings if "青瓷门" in rule["text"]]
    assert public["state"] == "pending" and public["scope"] != private["scope"]

    await _set_expression(logged_in, private["id"], state="active")
    await _set_expression(logged_in, public["id"], state="active")
    seen = await _completed_prompt(await _qq(logged_in, 689004, "青桐街的简称是什么？"))
    private_lane = _prompt_lines(seen, lanes={"relationship"})
    assert "石榴灯" in private_lane and "青瓷门" not in private_lane, private_lane

    group_query = "[CQ:at,qq=10001] 群里聊青桐街时用什么简称？"
    group = await _completed_prompt(await _qq(logged_in, 689005, group_query, group=True))
    group_lane = _prompt_lines(group, lanes={"relationship"})
    assert "青瓷门" in group_lane and "石榴灯" not in group_lane, group_lane

    if group_access == "explicit_publish":
        await _consent(logged_in, "public_publish", granted=False)
        revoked = await _completed_prompt(await _qq(logged_in, 689008, group_query, group=True))
        assert "青瓷门" not in _prompt_lines(revoked, lanes={"relationship"})
        await _consent(logged_in, "public_publish")

    await _set_expression(logged_in, private["id"], state="active", text="青桐街称作「山杏灯」")
    changed = await _completed_prompt(await _qq(logged_in, 689006, "青桐街的简称改成什么了？"))
    assert "山杏灯" in _prompt_lines(changed, lanes={"relationship"})
    await _set_expression(logged_in, public["id"], state="disabled")
    disabled = await _completed_prompt(await _qq(logged_in, 689007, group_query, group=True))
    assert "青瓷门" not in _prompt_lines(disabled, lanes={"relationship"})


async def test_relationship_clarification_updates_only_sourced_understanding(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeOneBot()
    await _install(logged_in, transport)
    await _bind(logged_in)
    router = logged_in.app.state.router
    original = router.stream
    impression = "我刚才以为你有点难过。"

    async def generate(config, *, profile, messages, **kwargs):
        newest = str(messages[-1].content) if messages else ""
        if profile == "dialogue.fast" and "打烊没赶上" in newest and "还没聊过" in newest:
            yield ModelChunk(delta=impression), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=impression)
            return
        async for chunk in original(config, profile=profile, messages=messages, **kwargs):
            yield chunk

    monkeypatch.setattr(router, "stream", generate)
    first = await _qq(logged_in, 690001, "这件事还没聊过，昨天我到店时打烊没赶上。")
    await _completed_prompt(first)
    assert any(impression in str(record["body"]) for record in transport.sent)
    before = await _completed_prompt(await _qq(logged_in, 690002, "你上次如何理解我的情绪？"))
    assert "主观理解，仍待确认" in _prompt_lines(before, lanes={"relationship"}), before["context"]

    reply_text = "其实我当时没有难过，我在忙着帮同事。"
    clarified = await _qq(logged_in, 690003, reply_text)
    await _completed_prompt(clarified)
    assert await worker.drain_outbox(limit=10, only_topic="relationship.update") >= 1
    next_turn = await _completed_prompt(await _qq(logged_in, 690004, "后来你怎么理解那一刻？"))
    lane = _prompt_lines(next_turn, lanes={"relationship"})
    assert "用户后来的解释" in lane and "忙着帮同事" in lane, lane
    assert "主观理解，仍待确认" not in lane, lane

    conflicts = (
        (690005, "你错了，我对这次晚到很失望。", "conflict"),
        (690006, "对不起，我上次忘了回你。", "apology"),
        (690007, "我按约定把寄存的钥匙带回来了。", "fulfillment"),
        (690008, "你又忘了我们说过的约定。", "breach"),
    )
    for event_id, text, _ in conflicts:
        await _completed_prompt(await _qq(logged_in, event_id, text))
    assert await worker.drain_outbox(limit=10, only_topic="relationship.update") >= len(conflicts)
    async with session_scope() as session:
        rows = list(await session.scalars(select(RelationshipEvent).where(
            RelationshipEvent.subject_id == logged_in.subject_id,
        )))
        states = {row.kind: row for row in rows}
        assert states["impression"].origin == "model_inference"
        assert states["clarification"].origin == "user_statement"
        for _, text, kind in conflicts:
            event = states[kind]
            reference = event.source_event_ids[0]
            assert reference.startswith("message:")
            source = await session.get(Message, reference.removeprefix("message:"))
            assert source is not None and source.text == text
