"""A model's delayed reply uses the original, still authorized OneBot target."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from ling import worker
from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import DeferredReply, Message, Operation, Window
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv
from tests.integration.test_onebot_loop import (
    CFG,
    QQ_USER,
    FakeOneBot,
    _bind,
    _event_body,
    _install,
    _post,
)

pytestmark = pytest.mark.w0_web


class _Delivery(FakeOneBot):
    lose_ack = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        response = await super().handler(request)
        if self.lose_ack and request.url.path.endswith("/send_private_msg"):
            raise httpx.ReadTimeout("fixture reply interrupted after platform receipt", request=request)
        return response


@pytest.mark.parametrize("outcome", ["sent", "disabled", "recalled", "preempted", "rebound", "ack_lost"])
async def test_onebot_deferred_reply_rechecks_current_input_and_real_receipt(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    fake = _Delivery()
    connector = _install(logged_in, fake)
    await _bind(logged_in)
    router = logged_in.app.state.router
    original_stream = router.stream
    output = "好的，前一条我看到了。"
    generating = asyncio.Event()
    release = asyncio.Event()

    async def stream(config, *, profile, **kwargs):
        if profile != "dialogue.fast":
            async for event in original_stream(config, profile=profile, **kwargs):
                yield event
            return
        if outcome == "preempted" and output == "当时的原问题已经想清楚了。":
            generating.set()
            await release.wait()
        yield ModelChunk(delta=output), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=output)

    resolved: list[tuple[str, str]] = []

    async def load(channel, window_ref, *, instance_key):
        assert channel == "onebot" and window_ref == f"{CFG.instance_id}:private:{QQ_USER}"
        resolved.append(instance_key)
        return connector

    monkeypatch.setattr(router, "stream", stream)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_group_reply_connector", load)
    first = (await _post(logged_in, _event_body(message_id=900, message="在吗？"))).json()
    assert first["delivered"], first
    async with session_scope() as session:
        window = await session.scalar(select(Window).where(Window.channel == "onebot"))
        assert window is not None and window.trusted_instance_key is not None
        session_id, instance_key = window.id, window.trusted_instance_key
    path = f"/v1/sessions/{session_id}/onebot-deferred"
    assert (await logged_in.client.get(path)).json() == {"enabled": False}
    enabled = await logged_in.client.put(path, json={"enabled": True})
    assert enabled.status_code == 200, enabled.text

    text = "我有点拿不定主意。"
    immediate = (await _post(logged_in, _event_body(message_id=901, message=text))).json()
    assert immediate["delivered"], immediate
    async with session_scope() as session:
        assert await session.scalar(select(DeferredReply)) is None
    output = "<<等会回 1>>"
    before = len(fake.sent)
    queued = (await _post(logged_in, _event_body(message_id=902, message=text))).json()
    assert queued["accepted"] and queued["handled_as"] == "deferred" and not queued["delivered"], queued
    assert len(fake.sent) == before
    async with session_scope() as session:
        job = await session.scalar(select(DeferredReply))
        assert job is not None and job.window_id == session_id
        assert job.delivery_target == {
            "instance_id": instance_key[1], "window_ref": f"{CFG.instance_id}:private:{QQ_USER}",
        }
        source = await session.get(Message, job.source_message_id)
        assert source is not None and source.text == "我有点拿不定主意。"
        job.chosen_due_at = datetime.now(UTC) - timedelta(seconds=1)
        job.hard_deadline_at = datetime.now(UTC) + timedelta(seconds=50)
        job_id = job.id

    if outcome == "disabled":
        assert (await logged_in.client.put(path, json={"enabled": False})).status_code == 200
    elif outcome == "recalled":
        recalled = (await _post(logged_in, _event_body(
            post_type="notice", notice_type="friend_recall", message_id=902,
        ))).json()
        assert recalled["accepted"], recalled
    elif outcome == "rebound":
        async with session_scope() as session:
            changed = await session.get(Window, session_id)
            assert changed is not None
            changed.origin_status, changed.origin_scope, changed.origin_instance_id = "unknown", None, None
    elif outcome == "ack_lost":
        fake.lose_ack = True

    output = "当时的原问题已经想清楚了。"
    if outcome == "preempted":
        processing = asyncio.create_task(worker.handle_deferred_reply_sweep({}))
        try:
            await asyncio.wait_for(generating.wait(), timeout=8)
            async with session_scope() as session:
                await ConversationService(session).add_message(session_id, role="user", text="我已有新话题了。")
        finally:
            release.set()
            await asyncio.wait_for(processing, timeout=10)
    else:
        await worker.handle_deferred_reply_sweep({})
    await worker.handle_deferred_reply_sweep({})

    external_calls = fake.sent[before:]
    expected = {
        "sent": ("delivered", None),
        "disabled": ("cancelled", "target_changed"),
        "recalled": ("cancelled", "source_unavailable"),
        "preempted": ("cancelled", "new_input"),
        "rebound": ("cancelled", "target_changed"),
        "ack_lost": ("failed", "delivery_uncertain"),
    }
    async with session_scope() as session:
        settled = await session.get(DeferredReply, job_id)
        assert settled is not None and (settled.state, settled.reason) == expected[outcome]
        operations = list(await session.scalars(select(Operation).where(
            Operation.payload["deferred_reply_id"].as_string() == job_id,
        )))
        if outcome in {"sent", "ack_lost"}:
            sent, = external_calls
            assert sent["action"] == "send_private_msg" and sent["body"]["user_id"] == QQ_USER
            assert output in str(sent["body"])
            operation, = operations
            assert operation.state == ("succeeded" if outcome == "sent" else "uncertain")
            assert resolved == [instance_key]
        else:
            assert external_calls == [] and operations == []
        assert bool(settled.delivered_turn_id) is (outcome == "sent")
