"""群聊接话：沉默 / 回答 / 澄清的端到端行为。

设计文档 §6.1 候选含"沉默"；§2「02 自然会话」要求判断"该不该接话"。
关键不变量：沉默不是丢弃——消息仍落库，前端收到明确 decision 而不是空等。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from ling.infra.db import session_scope
from ling.infra.models import Window
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


async def _group_window(env: TestEnv) -> str:
    r = await env.client.post("/v1/sessions", json={"title": "群聊"})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility="group_scope")
        )
    return session_id


async def _send(client, session_id: str, text: str):
    body = {"client_turn_id": str(uuid.uuid4()), "text": text}
    response = await client.post(f"/v1/sessions/{session_id}/turns", json=body)
    assert response.status_code == 200, response.text
    return await read_sse(response)


async def test_group_plain_chat_gets_silence_not_empty_wait(logged_in: TestEnv) -> None:
    """群聊普通聊天：不回复，且必须明确告知"选择了沉默"。"""
    session_id = await _group_window(logged_in)
    events = await _send(logged_in.client, session_id, "今天中午吃什么好呢")

    names = [n for n, _ in events]
    assert events[0][0] == "turn.started"
    completed = events[-1][1]
    assert names[-1] == "turn.completed"
    assert completed["decision"] == "silence"
    assert completed["message"] is None
    assert completed["decision_reason"], "沉默必须给出可解释原因"

    # 消息仍然落库（不是丢弃）
    listing = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    texts = [m["text"] for m in listing.json()["messages"]]
    assert "今天中午吃什么好呢" in texts
    # 没有 actor 回复
    assert not [m for m in listing.json()["messages"] if m["role"] == "actor"]


async def test_group_mention_forces_reply(logged_in: TestEnv) -> None:
    session_id = await _group_window(logged_in)
    events = await _send(logged_in.client, session_id, "ling 你觉得呢？")
    completed = events[-1][1]
    assert completed["decision"] == "reply"
    assert completed["message"] is not None
    assert "测试模式" in completed["message"]["text"]


async def test_private_chat_never_silent(logged_in: TestEnv) -> None:
    """私聊对角色直接说话，不能沉默（会被当成坏了）。"""
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "私聊"})
    ).json()["session_id"]
    events = await _send(logged_in.client, session_id, "在吗")
    assert events[-1][1]["decision"] == "reply"
    assert events[-1][1]["message"] is not None


async def test_group_flood_is_throttled_but_mention_still_answers(
    logged_in: TestEnv,
) -> None:
    """群内连发会触发静默，但被点名仍然必须答（硬规则）。"""
    session_id = await _group_window(logged_in)
    # 连续三次点名，耗尽预算
    for i in range(3):
        await _send(logged_in.client, session_id, f"ling 第{i}次点名")

    # 第四次普通发言：触发静默
    events = await _send(logged_in.client, session_id, "随便聊聊")
    assert events[-1][1]["decision"] == "silence"

    # 再次点名：硬规则仍然回答（豁免频率限制）
    mentioned = await _send(logged_in.client, session_id, "ling 这个你必须回答")
    assert mentioned[-1][1]["decision"] == "reply"


async def test_group_silence_has_silent_ack_hint(logged_in: TestEnv) -> None:
    """平台支持时应给无声存在感（表情回应），而不是完全消失。"""
    session_id = await _group_window(logged_in)
    events = await _send(logged_in.client, session_id, "随便说点啥")
    assert events[-1][1]["silent_ack"] == "react"


async def test_decision_reason_present_on_reply(logged_in: TestEnv) -> None:
    """回答也要说明为什么回答（可审计"为什么说/没说"）。

    注意 ``decision_hard`` 的语义变了：私聊的"回"现在是**选择**而非硬规则
    （`private_may_stay_silent` 默认开启，用户可以要求她真的能沉默）。
    所以这里断言的是"能解释为什么"，而不是"一定是硬规则"——
    后者等于把"私聊必回"写死回测试里。
    """
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "可解释"})
    ).json()["session_id"]
    events = await _send(logged_in.client, session_id, "你好")
    started = events[0][1]
    assert started["decision"] == "reply"
    assert started["decision_reason"]
    # 决策来源必须可见：模型参与了没有、还是走的硬规则
    assert started["model_status"] in {
        "ok",
        "not_asked",
        "unbound",
        "error",
        "overridden",
    }, started["model_status"]


async def test_silence_records_how_the_message_was_handled(logged_in: TestEnv) -> None:
    """沉默时消息要落库，并记下"看见了但没回"。

    这是"她选择不回"和"她根本没收到"能分开的唯一依据。
    """
    from sqlalchemy import select

    from ling.infra.models import Message

    session_id = await _group_window(logged_in)
    text = "今天中午吃什么好呢"
    await _send(logged_in.client, session_id, text)

    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.text == text))
        ).scalars().first()
    assert row is not None, "沉默时消息丢了"
    assert row.handled_as == "ignored", row.handled_as
    assert row.handled_reason, "沉默要给得出原因"


async def test_reply_records_handled_as_replied(logged_in: TestEnv) -> None:
    from sqlalchemy import select

    from ling.infra.models import Message

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "私聊"})
    ).json()["session_id"]
    text = "你好呀"
    await _send(logged_in.client, session_id, text)

    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.text == text))
        ).scalars().first()
    assert row is not None
    assert row.handled_as == "replied", row.handled_as
