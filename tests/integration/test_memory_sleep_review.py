"""Exercise the night worker and explicit API read against PostgreSQL."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling import worker
from ling.contracts.v1 import TurnState
from ling.domains.conversation.service import ConversationService
from ling.domains.memory import sleep_review
from ling.domains.memory.consolidate import consolidate_night
from ling.infra.db import session_scope
from ling.infra.models import MemoryConsolidationRow, MemoryRow, Message, Window, utcnow
from tests.integration.conftest import TestEnv
from tests.integration.test_memory_consolidate import _add_low_value_memories

pytestmark = pytest.mark.w0_web


async def _statement(
    env: TestEnv, memory_id: str, phrase: str, at: datetime, *, visibility: str = "subject_private",
) -> tuple[str, str]:
    response = await env.client.post("/v1/sessions", json={"title": "回顾证据"})
    assert response.status_code == 201
    window_id = response.json()["session_id"]
    async with session_scope() as session:
        window = await session.get(Window, window_id)
        assert window is not None
        window.visibility = visibility
        conversation = ConversationService(session)
        turn = await conversation.create_turn(window_id, client_turn_id=f"turn:{memory_id}", config_revision=0)
        turn.created_at = at
        message = await conversation.add_message(
            window_id, role="user", text=f"原话是{phrase}", turn_id=turn.id, sender_subject_id=env.subject_id,
        )
        await conversation.set_turn_state(turn.id, TurnState.completed)
        session.add(MemoryRow(
            id=memory_id, subject_id=env.subject_id, kind="assertion", text=phrase,
            origin="user_statement", visibility=visibility, source_visibility=visibility,
            source_window_id=window_id, turn_id=turn.id, source_event_ids=[f"turn:{turn.id}"], confidence=0.8,
        ))
    return window_id, message.message_id


async def _night(env: TestEnv) -> dict:
    response = await env.client.get("/v1/memory/sleep-review")
    assert response.status_code == 200, response.text
    night, = response.json()["nights"]
    return night


async def test_sleep_review_rechecks_originals_and_current_audience(logged_in: TestEnv) -> None:
    bedtime = utcnow() - timedelta(minutes=35)
    await _statement(logged_in, "mem_garden", "我给山茶浇水", bedtime - timedelta(minutes=5))
    window_id, message_id = await _statement(
        logged_in, "mem_bridge", "我参观了石桥", bedtime - timedelta(hours=1),
    )
    other, _ = await _statement(logged_in, "mem_window", "我计划读旧地图", bedtime - timedelta(hours=3))
    await _statement(
        logged_in, "mem_group", "群里的活动", bedtime - timedelta(minutes=1), visibility="group_scope",
    )
    await _statement(logged_in, "mem_old", "上周留下的旧事实", bedtime - timedelta(days=2))
    await _statement(logged_in, "mem_next_day", "入睡时段之后的新事实", bedtime + timedelta(minutes=1))
    payload = {
        "actor_id": logged_in.settings.actor_id, "subject_id": logged_in.subject_id,
        "sleep_date": bedtime.date().isoformat(), "bed_at": bedtime.isoformat(),
    }
    await worker.handle_memory_consolidate(payload)
    night = await _night(logged_in)
    assert night["consolidation_state"] == "skipped" and night["review_status"] == "ready"
    assert [item["memory_id"] for item in night["items"]] == ["mem_garden", "mem_bridge", "mem_window"]
    bridge = night["items"][1]
    assert bridge["window_id"] == window_id
    assert bridge["original"] == [{"speaker": "用户", "text": "原话是我参观了石桥", "truncated": False}]

    sealed = await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    assert sealed.status_code == 200
    assert (await _night(logged_in))["items"] == []
    reopened = await logged_in.client.put("/v1/isolation", json={"mode": "per_window"})
    assert reopened.status_code == 200
    assert (await _night(logged_in))["items"] == night["items"]

    corrected = await logged_in.client.post(
        "/v1/memory/mem_garden/supersede", json={"text": "我给杜鹃浇水", "reason": "更正"},
    )
    assert corrected.status_code == 200
    async with session_scope() as session:
        original = await session.get(Message, message_id)
        assert original is not None
        original.retracted_at = utcnow()
        window = await session.get(Window, other)
        assert window is not None
        window.visibility = "group_scope"

    await worker.handle_memory_consolidate(payload)
    changed = await _night(logged_in)
    assert (changed["sleep_date"], changed["review_status"], changed["items"]) == (
        payload["sleep_date"], "empty", [],
    )


async def test_review_retry_does_not_leave_partially_archived_sources(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id, subject_id = logged_in.settings.actor_id, logged_in.subject_id
    await _add_low_value_memories(actor_id=actor_id, subject_id=subject_id, count=3)
    bedtime = utcnow() - timedelta(minutes=35)

    async def failed(*args, **kwargs):
        raise RuntimeError("fixture review interrupted")

    async def work():
        async with session_scope() as session:
            return await consolidate_night(
                session, actor_id=actor_id, subject_id=subject_id, sleep_date=bedtime.date(),
                review_bedtime=bedtime,
            )

    with monkeypatch.context() as patch:
        patch.setattr(sleep_review, "capture_review_ids", failed)
        with pytest.raises(RuntimeError, match="fixture review interrupted"):
            await work()
    async with session_scope() as session:
        assert await session.scalar(select(func.count(MemoryConsolidationRow.id))) == 0
        assert await session.scalar(select(func.count(MemoryRow.id)).where(MemoryRow.archived_at.is_not(None))) == 0
    outcomes = await asyncio.gather(work(), work())
    assert sorted(outcome.already_done for outcome in outcomes) == [False, True]
    assert [outcome.rows_archived for outcome in outcomes] == [3, 3]
    assert (await _night(logged_in))["review_status"] == "empty"
