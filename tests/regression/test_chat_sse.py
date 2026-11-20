"""HTTP/SSE characterization; real socket streaming, scripted model boundary."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
import uvicorn
from httpx import AsyncClient, Response
from sqlalchemy import select

from ling import worker
from ling.domains.deferred import service as deferred
from ling.infra.db import session_scope
from ling.infra.models import DeferredReply
from ling.ports.model import ModelChunk, ModelRequest
from tests.integration.conftest import TestEnv
from tests.regression.chat_fixture import (
    FIRST,
    SECOND,
    ScriptedProvider,
    install_provider,
    new_window,
    publish,
)
from tests.regression.snapshots import shape, snapshot

pytestmark = pytest.mark.w0_web


@pytest_asyncio.fixture
async def http_client(logged_in: TestEnv) -> AsyncIterator[AsyncClient]:
    # The decision and reply calls share a profile. Select the existing
    # always-reply policy so only generation uses the gated provider.
    await publish(logged_in, {"dialogue": {"private_may_stay_silent": False}})
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        host, port = sock.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(logged_in.app, lifespan="off", log_config=None, access_log=False)
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with AsyncClient(
                base_url=f"http://{host}:{port}",
                cookies=dict(logged_in.client.cookies),
                trust_env=False,
                timeout=15,
            ) as client:
                yield client
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=20)


def send_body() -> dict[str, str]:
    return {"client_turn_id": "wp00-sse", "text": "请回复一条测试消息。"}


async def sse_events(response: Response) -> AsyncIterator[tuple[str, dict]]:
    assert response.status_code == 200, await response.aread()
    assert response.headers["content-type"].split(";")[0] == "text/event-stream"
    event = ""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").lstrip(" ")
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").lstrip(" "))
        elif not line and data:
            assert event
            yield event, json.loads("\n".join(data))
            event = ""
            data = []
    assert not data, "SSE frame did not end with a blank line"


def assert_stream_snapshot(name: str, events: list[tuple[str, dict]]) -> None:
    turn_id = events[0][1]["turn_id"]
    for _, data in events:
        if "turn_id" in data:
            assert data["turn_id"] == turn_id
        if data.get("message"):
            assert data["message"]["turn_id"] == turn_id
    snapshot(f"sse-{name}", [{"event": event, "data": shape(data)} for event, data in events])


def gated_provider() -> tuple[ScriptedProvider, asyncio.Event]:
    release = asyncio.Event()

    async def stream(_request: ModelRequest) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(delta=FIRST)
        await release.wait()
        yield ModelChunk(delta=SECOND)
        yield ModelChunk(done=True, finish_reason="stop")

    return ScriptedProvider(stream), release


async def test_send_is_incremental_and_replays_one_turn(
    logged_in: TestEnv, http_client: AsyncClient
) -> None:
    session_id = await new_window(logged_in)
    provider, release = gated_provider()
    install_provider(logged_in, provider)
    path = f"/v1/sessions/{session_id}/turns"
    try:
        async with http_client.stream("POST", path, json=send_body()) as response:
            stream = sse_events(response)
            started, first = await anext(stream), await anext(stream)
            assert started[0] == "turn.started"
            assert first[0] == "delta" and first[1]["text"] == FIRST
            release.set()
            events = [started, first, *[event async for event in stream]]
    finally:
        release.set()
    assert events[-1][0] == "turn.completed"
    assert events[-1][1]["message"]["text"] == FIRST + SECOND
    assert_stream_snapshot("send", events)

    replay = [event async for event in sse_events(await http_client.post(path, json=send_body()))]
    assert len(provider.requests) == 1
    assert replay[0][1]["turn_id"] == started[1]["turn_id"]
    assert replay[-1][1]["message"]["message_id"] == events[-1][1]["message"]["message_id"]
    assert_stream_snapshot("replay", replay)


async def test_stop_during_generation_then_retry_same_turn(
    logged_in: TestEnv, http_client: AsyncClient
) -> None:
    session_id = await new_window(logged_in)
    provider, release = gated_provider()
    install_provider(logged_in, provider)
    try:
        async with http_client.stream(
            "POST", f"/v1/sessions/{session_id}/turns", json=send_body()
        ) as response:
            stream = sse_events(response)
            started, first = await anext(stream), await anext(stream)
            assert first[0] == "delta" and first[1]["text"] == FIRST
            turn_id = started[1]["turn_id"]
            stopped = await http_client.post(f"/v1/turns/{turn_id}/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["turn_id"] == turn_id
            assert stopped.json()["state"] in {"stopping", "stopped"}
            assert stopped.json()["was_running"] is True
            release.set()
            events = [started, first, *[event async for event in stream]]
    finally:
        release.set()
    assert events[-1][0] == "turn.stopped"
    assert events[-1][1]["message"]["text"] == FIRST
    assert [data["text"] for event, data in events if event == "delta"] == [FIRST]
    snapshot("stop-http", {"status": stopped.status_code, "data": shape(stopped.json())})
    assert_stream_snapshot("stop", events)

    response = await http_client.post(f"/v1/turns/{turn_id}/retry")
    retry = [event async for event in sse_events(response)]
    assert len(provider.requests) == 2
    assert retry[0][1]["turn_id"] == turn_id
    assert retry[0][1]["attempt"] == 2
    assert retry[0][1]["retried"] is True
    assert retry[-1][0] == "turn.completed"
    assert retry[-1][1]["message"]["attempt"] == 2
    assert retry[-1][1]["message"]["text"] == FIRST + SECOND
    assert_stream_snapshot("retry", retry)


async def test_deferred_sse_then_worker_delivery(
    logged_in: TestEnv, http_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await new_window(logged_in)
    answer = "这是到期后发送的测试回复。"
    provider: ScriptedProvider

    async def stream(_request: ModelRequest) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(delta="<<等会回 999>>" if len(provider.requests) == 1 else answer)
        yield ModelChunk(done=True, finish_reason="stop")

    provider = ScriptedProvider(stream)
    router = install_provider(logged_in, provider)
    response = await http_client.post(f"/v1/sessions/{session_id}/turns", json=send_body())
    events = [event async for event in sse_events(response)]
    assert [event for event, _ in events] == ["turn.started", "turn.deferred", "turn.completed"]
    assert events[-1][1]["message"] is None
    assert events[1][1]["clamped"] is True
    assert events[1][1]["requested_seconds"] == 999
    assert_stream_snapshot("defer", events)

    async with session_scope() as session:
        row = (await session.scalars(select(DeferredReply))).one()
        assert row.source_turn_id == events[0][1]["turn_id"]
        due = row.chosen_due_at
        reply_id = row.id

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "get_settings", lambda: logged_in.settings)
    moment = due - timedelta(seconds=1)
    monkeypatch.setattr(deferred, "utcnow", lambda: moment)
    history = f"/v1/sessions/{session_id}/messages"
    await worker.handle_deferred_reply_sweep({})
    assert len(provider.requests) == 1
    assert all(
        msg["role"] != "actor" for msg in (await http_client.get(history)).json()["messages"]
    )

    moment = due
    await worker.handle_deferred_reply_sweep({})
    assert len(provider.requests) == 2
    async with session_scope() as session:
        row = await session.get(DeferredReply, reply_id)
        assert row.state == "delivered"
        assert row.delivered_turn_id and row.delivered_turn_id != row.source_turn_id
    messages = (await http_client.get(history)).json()["messages"]
    (actor,) = [msg for msg in messages if msg["role"] == "actor"]
    assert actor["turn_id"] == row.delivered_turn_id
    assert actor["text"] == answer
