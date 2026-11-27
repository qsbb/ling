"""记忆自动写入链路：轮次完成 → outbox → worker 提取 → 可检索。

这是  指出的缺口：`record_episode` 从未被调用、`recall` 不查 Episode，
导致"三天后接上上次那个事"实际不可用。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, Message, OutboxEvent
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


async def test_library_shows_episode_from_completed_turn_without_assertions(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling import worker

    async def library() -> dict:
        response = await logged_in.client.get("/v1/memory", params={"view": "library"})
        assert response.status_code == 200, response.text
        return response.json()

    assert (await library())["extraction"] is None
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "记忆库经历"})
    ).json()["session_id"]
    text = "周六和朋友整理了书架"
    events = await _send(logged_in.client, session_id, text)
    assert events[-1][0] == "turn.completed"
    before = await library()
    assert before["total"] == 0 and before["extraction"]["state"] == "pending"

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    assert await worker.drain_outbox(limit=1, only_topic="memory.extract") == 1
    after = await library()
    assert after["extraction"]["state"] == "done" and after["total"] == 1
    assert after["memories"][0]["record_type"] == "experience"
    assert text in after["memories"][0]["text"]
    assert (await logged_in.client.get("/v1/memory")).json()["total"] == 0


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


async def test_linked_fact_keeps_two_periods_and_retry_keeps_newer_state(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling import worker
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.models import Message

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "时间依据"})
    ).json()["session_id"]
    first = "以前住杭州，现在住苏州"
    second = "我搬到宁波"
    for utterance in (first, second):
        await _send(logged_in.client, session_id, utterance)
    async with session_scope() as session:
        events = list(await session.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
        ))
        payloads = {row.payload["user_text"]: row.payload for row in events}
    for utterance in (first, second, first):
        await worker.handle_memory_extract(payloads[utterance])

    async with session_scope() as session:
        rows = list(await session.scalars(select(MemoryRow).where(MemoryRow.slot == "lives_in")))
        by_value = {row.value: row for row in rows}
        assert len(rows) == 3 and set(by_value) == {"杭州", "苏州", "宁波"}
        assert by_value["杭州"].fact_phase == "historical"
        assert by_value["苏州"].valid_until == by_value["宁波"].valid_from
        assert by_value["宁波"].valid_until is None
        for row in rows:
            message = await session.get(Message, row.source_message_id)
            assert message is not None and message.sender_subject_id == logged_in.subject_id
            assert f"message:{message.id}" in row.source_event_ids
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="lives_in", fact_slot="lives_in",
            visibility_allow={Visibility.subject_private},
        )
        assert [hit.memory_id for hit in result.hits] == [by_value["宁波"].id]
        message = await session.get(Message, by_value["宁波"].source_message_id)
        message.retracted_at = message.created_at

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="lives_in", fact_slot="lives_in",
            visibility_allow={Visibility.subject_private},
        )
        assert result.hits == []


async def test_forget_source_removes_old_prompt_material_and_allows_new_evidence(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling import worker
    from ling.domains.conversation.service import ConversationService
    from ling.kernel.context import ContextCompiler

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    client = logged_in.client
    window = (await client.post("/v1/sessions", json={"title": "定向遗忘"})).json()["session_id"]
    address = "青竹小巷"
    interest = "银河积木"

    async def learn(utterance: str) -> dict:
        await _send(client, window, utterance)
        async with session_scope() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
                .order_by(OutboxEvent.created_at.desc()).limit(1)
            )
            payload = dict(event.payload)
        await worker.handle_memory_extract(payload)
        return payload

    original = await learn(f"我住在{address}")
    await learn(f"我喜欢{interest}")
    async with session_scope() as session:
        before = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window, user_text="我现在住在哪里？",
        )
        before_text = "\n".join(message.content for message in before.messages)
        assert address in before_text and interest in before_text
        row = await session.scalar(select(MemoryRow).where(MemoryRow.value == address))
        await ConversationService(session).save_summary(
            window, summary=f"旧摘要包含{address}", through_sequence=2, message_count=2,
        )
        target_id = row.id

    evidence = await client.get(f"/v1/memory/{target_id}/source")
    assert evidence.status_code == 200 and evidence.json()["text"] == f"我住在{address}"

    response = await client.post(f"/v1/memory/{target_id}/forget")
    assert response.status_code == 200, response.text
    assert (await client.get(f"/v1/memory/{target_id}/source")).status_code == 404
    await worker.handle_memory_extract(original)
    async with session_scope() as session:
        after = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window, user_text="我现在住在哪里？",
        )
        after_text = "\n".join(message.content for message in after.messages)
        assert address not in after_text and interest in after_text
        assert not await session.scalar(select(MemoryRow.id).where(MemoryRow.value == address))
        assert await session.scalar(select(MemoryRow.id).where(MemoryRow.value == interest))
    found = (await client.get("/v1/memory", params={"view": "library", "q": address})).json()
    assert address not in str(found)

    await learn(f"我住在{address}")
    async with session_scope() as session:
        relearned = await session.scalar(select(MemoryRow).where(MemoryRow.value == address))
        assert relearned and relearned.id != target_id and relearned.turn_id != original["turn_id"]


async def test_private_forget_command_requires_one_source_and_is_not_remembered(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling import worker
    from ling.kernel.context import ContextCompiler

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    env = logged_in
    first, second = [
        (await env.client.post("/v1/sessions", json={"title": title})).json()["session_id"]
        for title in ("住址记录", "另一个私聊")
    ]
    target, control = "我住在浅水湾石桥", "我喜欢纸飞机"

    async def send(window: str, text: str) -> tuple[str, str]:
        events = await _send(env.client, window, text)
        event, last = events[-1]
        assert event == "turn.completed"
        return last["turn_id"], last["message"]["text"]

    target_turn, _ = await send(first, target)
    await send(first, control)
    other_turn, _ = await send(second, target)
    await worker.drain_outbox(only_topic="memory.extract")

    async def exists(turn_id: str) -> bool:
        async with session_scope() as session:
            return bool(await session.scalar(select(MemoryEpisode.id).where(MemoryEpisode.turn_id == turn_id)))

    assert await exists(target_turn) and await exists(other_turn)
    _, clarify = await send(first, "忘掉那个。")
    assert "请引用" in clarify
    assert await exists(target_turn)
    command, confirmation = await send(first, f"请忘掉我在这段对话里说过的『{target}』这条记忆。")
    assert "已遗忘" in confirmation
    assert not await exists(target_turn)
    assert await exists(other_turn)
    async with session_scope() as session:
        assert not await session.scalar(select(OutboxEvent.id).where(
            OutboxEvent.payload["turn_id"].as_string() == command,
        ))
        command_message = await session.scalar(select(Message).where(Message.turn_id == command, Message.role == "user"))
        assert command_message is not None and command_message.memory_hidden_at is not None
        prompt = await ContextCompiler(session).compile(
            subject_id=env.subject_id, window_id=first, user_text="刚才聊了什么？",
        )
        assert not any(target in item.content for item in prompt.messages)
        assert any(control in item.content for item in prompt.messages)
