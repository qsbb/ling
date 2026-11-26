"""Objects added by the memory worker, and labels confirmed against a retained record."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from ling import worker
from ling.infra.db import session_scope
from ling.infra.models import (
    EntityRow,
    MemoryEntityRow,
    MemoryEpisode,
    Message,
    OutboxEvent,
    utcnow,
)
from ling.kernel.context import ContextCompiler
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


class _ObjectRouter:
    def __init__(self, mentions: dict[str, str]) -> None:
        self.mentions = mentions

    async def stream(self, config, *, profile, messages, **kwargs):
        assert profile == "memory.extract"
        assert "逐字摘名称" in messages[0].content
        output = json.dumps(
            [{"object": name, "kind": kind} for name, kind in self.mentions.items()],
            ensure_ascii=False,
        )
        yield ModelChunk(delta=output), StreamOutcome()


async def _window(env: TestEnv) -> str:
    response = await env.client.post("/v1/sessions", json={"title": "对象来源测试"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _send_and_extract(env: TestEnv, window_id: str, text: str) -> dict:
    response = await env.client.post(
        f"/v1/sessions/{window_id}/turns", json={"client_turn_id": uuid4().hex, "text": text},
    )
    assert response.status_code == 200, response.text
    assert (await read_sse(response))[-1][0] == "turn.completed"
    assert await worker.drain_outbox(limit=1, only_topic="memory.extract") == 1
    async with session_scope() as session:
        event = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.topic == "memory.extract",
            OutboxEvent.payload["user_text"].as_string() == text,
        ).order_by(OutboxEvent.created_at.desc()).limit(1))
        assert event is not None and event.state == "done"
        return dict(event.payload)


async def _objects(env: TestEnv) -> list[dict]:
    response = await env.client.get("/v1/memory/objects")
    assert response.status_code == 200, response.text
    return [item for item in response.json()["objects"] if item["kind"] != "self"]


async def _records(env: TestEnv, **filters) -> list[dict]:
    response = await env.client.get("/v1/memory", params={"view": "library", **filters})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == len(payload["memories"])
    return payload["memories"]


async def _prompt(env: TestEnv, window_id: str, text: str) -> tuple[list[str], str]:
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=env.subject_id, window_id=window_id, user_text=text,
        )
    system = "\n".join(item.content for item in compiled.messages if item.role == "system")
    assert [item.content for item in compiled.messages if item.role == "user"] == [text]
    return compiled.memory_ids, system


async def test_live_objects_share_an_episode_and_cannot_return_after_forgetting(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    first_window, query_window = await _window(env), await _window(env)
    monkeypatch.setattr(worker, "refresh_memory_router_if_stale", AsyncMock())
    mentions = {"阿禾": "person", "读书会": "group", "河西书店": "place", "蓝封画册": "item", "水岸之歌": "work"}
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", _ObjectRouter({**mentions, "杜撰画廊": "place"}))
    first_text = "阿禾说，读书会昨天在河西书店找到了蓝封画册，还听了水岸之歌。"
    old_payload = await _send_and_extract(env, first_window, first_text)

    initial = {item["name"]: item for item in await _objects(env)}
    assert {name: item["kind"] for name, item in initial.items()} == mentions
    event_ids = set()
    for name, item in initial.items():
        records = await _records(env, object_id=item["entity_id"], q="蓝封画册")
        assert item["count"] == len(records) == 1
        assert name in records[0]["text"]
        assert records[0]["record_type"] == "experience"
        assert records[0]["source_role"] == "conversation"
        event_ids.add(records[0]["memory_id"])
    assert len(event_ids) == 1
    first_id = event_ids.pop()
    source = await env.client.get(f"/v1/memory/{first_id}/source")
    assert source.status_code == 200 and source.json()["text"] == first_text
    await worker.handle_memory_extract(old_payload)
    assert await _objects(env) == list(initial.values())

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", _ObjectRouter({
        "松果猫": "pet", "木工课程": "topic", "蓝封画册": "item",
    }))
    second_text = "另一册也叫蓝封画册，木工课程散场时遇见了松果猫。"
    await _send_and_extract(env, first_window, second_text)
    objects = await _objects(env)
    items = [item for item in objects if item["name"] == "蓝封画册"]
    assert len(items) == 2 and len({item["entity_id"] for item in items}) == 2
    matches = [(await _records(env, object_id=item["entity_id"]))[0] for item in items]
    assert {item["text"] for item in matches} == {f"用户：{first_text}", f"用户：{second_text}"}
    other_id = next(item["memory_id"] for item in matches if item["memory_id"] != first_id)

    ids, prompt = await _prompt(env, query_window, "河西书店那次听的是什么？")
    assert first_id in ids and "水岸之歌" in prompt
    forgot = await env.client.post(f"/v1/memory/{first_id}/forget")
    assert forgot.status_code == 200, forgot.text
    with monkeypatch.context() as scoped:
        scoped.setattr(worker, "_MEMORY_ROUTER", _ObjectRouter(mentions))
        await worker.handle_memory_extract(old_payload)
    surviving = await _objects(env)
    assert {item["name"] for item in surviving} == {"蓝封画册", "木工课程", "松果猫"}
    assert {item["memory_id"] for item in await _records(env)} == {other_id}
    assert not await _records(env, object_id=initial["河西书店"]["entity_id"])
    assert (await env.client.get(f"/v1/memory/{first_id}/source")).status_code == 404
    after_ids, after_prompt = await _prompt(env, query_window, "河西书店那次听的是什么？")
    assert first_id not in after_ids and "水岸之歌" not in after_prompt
    async with session_scope() as session:
        assert await session.get(EntityRow, initial["河西书店"]["entity_id"]) is None
        assert not list(await session.scalars(select(MemoryEntityRow).where(
            MemoryEntityRow.memory_id == first_id,
        )))

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", _ObjectRouter({"蓝封画册": "item", "河西书店": "place"}))
    await _send_and_extract(env, first_window, "我重新告诉你，河西书店有一册新的蓝封画册。")
    relearned = next(item for item in await _objects(env) if item["name"] == "河西书店")
    assert relearned["entity_id"] != initial["河西书店"]["entity_id"]
    assert (await _records(env, object_id=relearned["entity_id"]))[0]["memory_id"] != first_id


async def test_manual_object_link_is_literal_and_revalidates_each_source(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = logged_in
    window_id = await _window(env)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    await _send_and_extract(env, window_id, "她说在鹭溪站拍过站牌。")
    await _send_and_extract(env, window_id, "鹭溪站后来换了站牌。")
    first, second = reversed(await _records(env))
    body = {"kind": "place", "name": "鹭溪站"}
    def label_url(row: dict) -> str:
        return f"/v1/memory/{row['memory_id']}/objects"

    invalid = await env.client.post(label_url(first), json={**body, "name": "鹿栖站"})
    assert invalid.status_code == 422
    marked = await env.client.post(label_url(first), json=body)
    assert marked.status_code == 200, marked.text
    object_id = marked.json()["entity_id"]
    assert (await env.client.post(label_url(first), json=body)).json()["entity_id"] == object_id
    linked = await env.client.post(label_url(second), json={**body, "object_id": object_id})
    assert linked.status_code == 200 and linked.json()["entity_id"] == object_id
    assert {item["memory_id"] for item in await _records(env, object_id=object_id)} == {
        first["memory_id"], second["memory_id"],
    }

    async with session_scope() as session:
        episode = await session.get_one(MemoryEpisode, first["memory_id"])
        source = await session.get_one(Message, episode.source_message_id)
        source.retracted_at = utcnow()
    active = await _records(env, object_id=object_id)
    assert [item["memory_id"] for item in active] == [second["memory_id"]]
    assert (await _objects(env))[0]["count"] == 1
    assert (await env.client.post(label_url(first), json=body)).status_code == 404
