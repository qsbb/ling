"""Run approved Notepad activities from the real life worker to its fact view."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from ling import worker
from ling.api.routes import desktop
from ling.infra.db import session_scope
from ling.infra.models import ActivityRow, Persona, ScheduleSegmentRow
from ling.kernel.desktop_activity import AUTONOMY_TOPIC
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.test_activity_boundaries import DAY, _at, _freeze, _seed_day, _seed_routine
from tests.integration.test_desktop_tasks import PATH, POLL, _claim, _report
from tests.integration.test_desktop_tasks import linked as linked

pytestmark = pytest.mark.w0_web


@pytest.mark.parametrize("outcome", [
    "verified", "rest", "chose_rest", "invalid", "revoked", "revoked_during_choice",
    "changed_during_choice", "expired", "locked", "mismatch",
])
async def test_life_worker_spends_one_grant_and_records_only_verified_activity(
    linked, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    owner, companion = linked
    clock = _at(10 * 60)
    _freeze(monkeypatch, clock)
    monkeypatch.setattr(desktop, "utcnow", lambda: clock)
    ids = await _seed_day()
    await _seed_routine()
    persona_text = "你喜欢在上午写短文，也会按自己的状态安排休息。"
    async with session_scope() as session:
        segment = await session.get(ScheduleSegmentRow, ids["work"])
        assert segment is not None
        segment.kind = "rest" if outcome == "rest" else "hobby"
        persona_id = worker.get_settings().persona_id
        persona = await session.get(Persona, persona_id)
        if persona is None:
            session.add(Persona(id=persona_id, system_prompt=persona_text))
        else:
            persona.system_prompt = persona_text

    request_id = str(uuid4())
    note = "今夜记下三颗星\n春天要去天台。"
    expires = clock + timedelta(seconds=8 if outcome == "expired" else 120)
    monkeypatch.setattr(worker, "refresh_memory_router_if_stale", AsyncMock())
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[list] = []
    paused_choice = outcome in {"revoked_during_choice", "changed_during_choice"}

    async def stream(config, *, profile, messages, **kwargs):
        assert profile == "dialogue.fast" and kwargs["allow_fallback"] is False
        assert note not in str(messages)
        assert persona_text in messages[1].content
        calls.append(messages)
        if paused_choice:
            started.set()
            await release.wait()
        output = {
            "chose_rest": '{"action":"rest"}',
            "invalid": "我去写一段吧",
        }.get(outcome, '{"action":"write"}')
        yield ModelChunk(delta=output), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=output)

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", SimpleNamespace(stream=stream))
    body = {
        "request_id": request_id,
        "start_at": (clock - timedelta(seconds=10)).isoformat(),
        "end_at": expires.isoformat(),
        "text": note,
    }
    stored = await owner.put(f"{PATH}/autonomy", json=body)
    assert stored.status_code == 200, stored.text
    assert stored.json()["grant"]["decision"] == "pending"
    assert (await companion.post(POLL, json={"ready": True})).json() == {"task": None}
    await worker.handle_life_tick({})
    await worker.handle_life_tick({})
    pending = (await owner.get(f"{PATH}/autonomy")).json()["grant"]
    assert pending["decision"] == ("rest" if outcome == "rest" else "deciding")
    assert (await owner.get(PATH)).json()["tasks"] == [] and calls == []
    drain = asyncio.create_task(worker.drain_outbox(only_topic=AUTONOMY_TOPIC))
    if paused_choice:
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if outcome == "revoked_during_choice":
                revoked = await asyncio.wait_for(owner.delete(f"{PATH}/autonomy"), timeout=3)
                assert revoked.json()["grant"]["decision"] == "revoked"
            else:
                async with session_scope() as session:
                    leaf = await session.get(ScheduleSegmentRow, ids["work"])
                    assert leaf is not None
                    leaf.kind = "work"
        finally:
            release.set()
    assert await asyncio.wait_for(drain, timeout=8) == int(outcome != "rest")
    assert len(calls) == int(outcome != "rest")
    state = (await owner.get(f"{PATH}/autonomy")).json()["grant"]
    decision = {
        "rest": "rest", "chose_rest": "rest", "invalid": "unavailable",
        "revoked_during_choice": "revoked", "changed_during_choice": "unavailable",
    }.get(outcome, "queued")
    assert state["decision"] == decision

    if decision != "queued":
        assert await _claim(companion) is None
        assert (await owner.get(PATH)).json()["tasks"] == []
    else:
        task = await _claim(companion)
        assert task["id"] == request_id and task["text"] == note
        assert await _report(companion, task, "opened")
        assert await _report(companion, task, "written")
        if outcome == "expired":
            assert task["lease_seconds"] == 8
            clock += timedelta(seconds=9)
        elif outcome == "revoked":
            removed = await owner.delete(f"{PATH}/autonomy")
            assert removed.status_code == 200 and removed.json()["grant"]["decision"] == "revoked"
        elif outcome == "locked":
            await companion.post(POLL, json={"ready": False})
        elif outcome == "verified":
            clock += timedelta(seconds=1)
        assert await _report(
            companion, task, "verified", readback="不同的内容" if outcome == "mismatch" else note,
        ) is (outcome in {"verified", "mismatch"})
        assert not await _report(companion, task, "verified", readback=note)
        all_tasks = (await owner.get(PATH)).json()["tasks"]
        recorded, = all_tasks
        assert recorded["source"] == "life" and recorded["source_segment_id"] == ids["work"]
        expected = {
            "verified": ("succeeded", None),
            "revoked": ("interrupted", "authorization_revoked"),
            "expired": ("interrupted", "authorization_expired"),
            "locked": ("interrupted", "desktop_unavailable"),
            "mismatch": ("failed", "readback_mismatch"),
        }
        assert (recorded["state"], recorded["reason"]) == expected[outcome]
        await worker.handle_life_tick({})
        assert (await owner.get(PATH)).json()["tasks"] == all_tasks

    async with session_scope() as session:
        facts = list(await session.scalars(select(ActivityRow).where(ActivityRow.origin == "tool_receipt")))
        assert len(facts) == (1 if outcome == "verified" else 0)
        if facts:
            fact, = facts
            assert fact.state == "completed" and fact.kind == "writing"
            assert fact.completion_evidence.startswith("kind=observed;")
            assert fact.checkpoint == {"desktop_task_id": request_id}
    if outcome == "verified":
        response = await owner.get("/v1/life/timeline", params={"date": DAY.isoformat()})
        assert response.status_code == 200, response.text
        matching = [fact for fact in response.json()["facts"] if fact["activity_id"] == facts[0].id]
        assert len(matching) == 1 and matching[0]["kind"] == "writing"
