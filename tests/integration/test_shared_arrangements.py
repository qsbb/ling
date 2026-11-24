"""The OneBot intake and the real PostgreSQL worker feed budgeted shared recall."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from ling.domains.awareness.service import IsolationMode
from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import Message, OutboxEvent, SharedArrangement, SharedArrangementEvidence
from ling.kernel.audience import AudienceSnapshot
from ling.kernel.context import ContextCompiler
from ling.kernel.memory_tools import recall_shared_arrangement
from ling.ports.channel import GroupHistoryAccess
from ling.ports.config import MemoryPolicy
from ling.worker import drain_outbox
from tests.integration.conftest import TestEnv
from tests.integration.test_message_recall import (
    GROUP,
    QQ_USER,
    SELF_ID,
    _bind,
    _FakeOneBot,
    _install,
    _post,
    _recall_body,
)

pytestmark = pytest.mark.w0_web


async def _publish(env: TestEnv, mode: str) -> None:
    draft = await env.client.put("/v1/settings/draft", json={"memory": {"shared_events": mode}})
    assert draft.status_code == 200, draft.text
    publish = await env.client.post("/v1/settings/publish", json={"revision": draft.json()["revision"]})
    assert publish.status_code == 200, publish.text


async def _say(env: TestEnv, mid: int, text: str, *, user: int, group: int = int(GROUP)) -> None:
    body = json.dumps({
        "time": int(datetime.now(UTC).timestamp()), "self_id": int(SELF_ID),
        "post_type": "message", "message_type": "group", "group_id": group,
        "message_id": mid, "user_id": user,
        "message": text, "raw_message": text,
        "sender": {"nickname": "同名成员"},
    }).encode()
    response = await _post(env, body)
    assert response.status_code == 200 and response.json()["accepted"], response.text


async def test_shared_arrangement_uses_linked_sources_and_whole_group_access(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    _install(env, _FakeOneBot())
    await _bind(env)
    person_a, person_b = int(QQ_USER), 20004
    old_place, new_place = "星桥图书馆", "云帆公园"
    hidden = "私人信物北冕钥匙"
    original = f"提议周六在{old_place}见面"
    changed = f"改到周日在{new_place}见面"
    await _say(env, 800, "提议周五在白石书店见面", user=person_a)
    await _publish(env, "shadow")
    await _say(env, 801, original, user=person_a)
    await _say(env, 802, "[CQ:reply,id=801]我确认", user=person_b)
    await _say(env, 803, f"我妹妹喜欢{hidden}", user=person_a)
    await _say(env, 804, f"提议周四在{old_place}见面", user=person_b)
    await _say(env, 805, f"[CQ:reply,id=802]{changed}", user=person_a)
    await _say(env, 806, "这是另一个群", user=person_b, group=99998)

    assert await drain_outbox(only_topic="memory.shared_arrangement") == 4
    async with session_scope() as session:
        arrangements = list(await session.scalars(select(SharedArrangement)))
        assert len(arrangements) == 2
        evidence = list(await session.scalars(
            select(SharedArrangementEvidence).order_by(SharedArrangementEvidence.sequence)
        ))
        assert [item.action for item in evidence] == [
            "proposed", "confirmed", "proposed", "rescheduled",
        ]
        proposal, confirmation, unrelated, reschedule = evidence
        assert proposal.subject_id != confirmation.subject_id
        assert confirmation.previous_id == proposal.message_id
        assert reschedule.previous_id == confirmation.message_id
        assert unrelated.arrangement_id != proposal.arrangement_id
        assert unrelated.place == proposal.place
        source = await session.get(Message, proposal.message_id)
        window_id = source.window_id
        other = await session.scalar(select(Message).where(Message.text == "这是另一个群"))
        other_window_id = other.window_id
        conversation = ConversationService(session)
        for index in range(30):
            await conversation.add_message(window_id, role="user", text=f"无关话题{index}")
        await conversation.save_summary(
            window_id, summary="这里只保留无关话题", through_sequence=reschedule.sequence, message_count=6,
        )

    reader = confirmation.subject_id
    source_ids = frozenset((proposal.message_id, confirmation.message_id, reschedule.message_id))

    async def compile(query: str, *, mode="enabled", window=window_id, ids=source_ids, who=reader):
        async with session_scope() as session:
            return await ContextCompiler(
                session, memory_policy=MemoryPolicy(shared_events=mode),
                group_history=GroupHistoryAccess(window, ids),
            ).compile(
                subject_id=who, window_id=window, user_text=query, is_group=True, history_limit=10,
            )

    def evidence_text(compiled):
        return "\n".join(message.content for message in compiled.annotated if message.lane == "evidence")

    for query, expected, forbidden in (
        ("最初约在哪？", old_place, new_place),
        ("现在约在哪？", new_place, old_place),
        ("最后去了没？", "尚无完成证据", old_place),
    ):
        compiled = await compile(query)
        text = evidence_text(compiled)
        assert expected in text and forbidden not in text and hidden not in text
        assert compiled.meta["shared_memory_candidate_ids"] == [proposal.arrangement_id]
        assert proposal.arrangement_id in compiled.memory_ids
        assert all(old_place not in message.content and new_place not in message.content
                   for message in compiled.annotated if message.lane in {"summary", "history"})

    private = (await env.client.post("/v1/sessions", json={"title": "安排询问"})).json()["session_id"]
    for compiled in (
        await compile("现在约在哪？", window=private, who=env.subject_id),
        await compile("现在约在哪？", mode="shadow"),
        await compile("现在约在哪？", window=other_window_id),
        await compile("现在约在哪？", ids=frozenset()),
        await compile("现在约在哪？", ids=source_ids - {confirmation.message_id}),
        await compile("早安"),
    ):
        assert old_place not in evidence_text(compiled) and new_place not in evidence_text(compiled)

    async with session_scope() as session:
        audience = await AudienceSnapshot.freeze(
            session, subject_id=reader, window_id=window_id, window_visibility="group_scope",
        )
    authorized = replace(audience, whole_group_source_ids=source_ids)
    args = {"query": "现在约在哪？"}
    assert new_place not in await recall_shared_arrangement(args, audience=authorized)
    await _publish(env, "enabled")
    assert new_place in await recall_shared_arrangement(args, audience=authorized)
    assert new_place not in await recall_shared_arrangement(args, audience=audience)
    assert new_place not in await recall_shared_arrangement(
        args, audience=replace(authorized, mode=IsolationMode.sealed),
    )

    await _say(env, 807, "[CQ:reply,id=805]我们已经见面了", user=person_b)
    assert await drain_outbox(only_topic="memory.shared_arrangement") == 1
    async with session_scope() as session:
        done = await session.scalar(select(SharedArrangementEvidence).where(
            SharedArrangementEvidence.action == "completed",
        ))
        assert done.subject_id == reader
    completed_ids = source_ids | {done.message_id}
    assert "已经见面" in evidence_text(await compile("最后去了没？", ids=completed_ids))
    assert not evidence_text(await compile("最后去了没？", ids=source_ids))
    await _post(env, _recall_body(message_id=802, user_id=person_b, group_id=int(GROUP)))
    assert not evidence_text(await compile("现在约在哪？", ids=completed_ids))

    await _publish(env, "off")
    await _say(env, 808, "提议周二在白石书店见面", user=person_a)
    async with session_scope() as session:
        events = list(await session.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "memory.shared_arrangement")
        ))
        assert len(events) == 5
