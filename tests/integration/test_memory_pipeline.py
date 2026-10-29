"""记忆自动写入链路：轮次完成 → outbox → worker 提取 → 可检索。

这是  指出的缺口：`record_episode` 从未被调用、`recall` 不查 Episode，
导致"三天后接上上次那个事"实际不可用。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, OutboxEvent
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


async def _send(client, session_id: str, text: str):
    body = {"client_turn_id": str(uuid.uuid4()), "text": text}
    response = await client.post(f"/v1/sessions/{session_id}/turns", json=body)
    assert response.status_code == 200, response.text
    return await read_sse(response)


async def test_completed_turn_enqueues_memory_extract(logged_in: TestEnv) -> None:
    """轮次完成必须在同一事务里登记 outbox，而不是丢掉这次经历。"""
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "记忆链路"})
    ).json()["session_id"]
    await _send(logged_in.client, session_id, "我叫阿澄")

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
                )
            ).scalars()
        )
    assert rows, "完成的轮次应登记 memory.extract 任务"
    payload = dict(rows[-1].payload or {})
    assert payload.get("user_text") == "我叫阿澄"
    assert payload.get("actor_text")
    assert payload.get("subject_id")


async def test_failed_turn_does_not_enqueue_memory(logged_in: TestEnv) -> None:
    """失败的轮次不应提取记忆：只有真正发生的对话才值得记住。"""
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {"provider_id": "broken", "kind": "fake", "enabled": True,
                 "extra": {"mode": "failing"}},
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "broken", "model_id": "deterministic"}
            ],
            "test_mode": True,
        },
    )
    await logged_in.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "失败不记忆"})
    ).json()["session_id"]
    events = await _send(logged_in.client, session_id, "这条会失败")
    assert events[-1][0] == "turn.failed"

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
                )
            ).scalars()
        )
    assert not rows, "失败轮次不应产生记忆任务"


async def test_worker_extracts_episode_and_assertion(logged_in: TestEnv) -> None:
    """worker 处理 outbox 后：Episode 落库、明确自述变成可检索断言。"""
    from ling.worker import handle_memory_extract

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "提取"})
    ).json()["session_id"]
    await _send(logged_in.client, session_id, "我叫阿澄，我喜欢下雨天")

    # 取出待处理任务并交给 worker handler
    async with session_scope() as session:
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
            )
        ).scalars().first()
        assert row is not None
        payload = dict(row.payload or {})

    await handle_memory_extract(payload)

    async with session_scope() as session:
        episodes = list(
            (
                await session.execute(
                    select(MemoryEpisode).where(MemoryEpisode.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
        memories = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    assert episodes, "应写入情节记忆"
    assert any("阿澄" in e.summary for e in episodes)
    texts = [m.text for m in memories]
    assert any("阿澄" in t for t in texts), f"应提取名字断言，实际 {texts}"
    assert any("下雨天" in t for t in texts), f"应提取偏好断言，实际 {texts}"
    # 每条记忆都必须有证据
    assert all(m.source_event_ids for m in memories)


async def test_episode_is_recallable_later(logged_in: TestEnv) -> None:
    """Episode 必须能被后续轮次检索到（"三天后接上上次那个事"的基础）。"""
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService
    from ling.worker import handle_memory_extract

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "长期承接"})
    ).json()["session_id"]
    await _send(logged_in.client, session_id, "我养了一只叫团子的猫")

    async with session_scope() as session:
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
            )
        ).scalars().first()
    await handle_memory_extract(dict(row.payload or {}))

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="团子 猫",
            visibility_allow={Visibility.subject_private},
        )
    texts = [h.text for h in result.hits]
    assert any("团子" in t for t in texts), f"Episode 未被检索到，实际 {texts}"


async def test_episode_respects_audience(logged_in: TestEnv) -> None:
    """群聊上下文不得召回私聊 Episode。"""
    from ling.contracts.v1 import Origin, Visibility
    from ling.domains.memory.service import MemoryService

    async with session_scope() as session:
        await MemoryService(session).record_episode(
            subject_id=logged_in.subject_id,
            window_id="win_secret",
            summary="用户私下说了一个秘密",
            source_event_ids=["evt_1"],
            origin=Origin.user_statement,
            visibility=Visibility.subject_private,
        )

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="秘密",
            visibility_allow={Visibility.group_scope, Visibility.public},
        )
    assert all("秘密" not in h.text for h in result.hits), "群聊上下文召回了私聊情节"


async def test_worker_handles_missing_fields_gracefully() -> None:
    """缺字段的任务不应抛异常（否则 worker 会卡住整批）。"""
    from ling.worker import handle_memory_extract

    await handle_memory_extract({"subject_id": "s"})
    await handle_memory_extract({})
