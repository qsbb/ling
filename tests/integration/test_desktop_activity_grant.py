"""Run approved Notepad activities from the real life worker to its fact view."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from ling import worker
from ling.api.routes import desktop
from ling.infra.db import session_scope
from ling.infra.models import ActivityRow, ScheduleSegmentRow
from tests.integration.test_activity_boundaries import DAY, _at, _freeze, _seed_day, _seed_routine
from tests.integration.test_desktop_tasks import PATH, POLL, _claim, _report
from tests.integration.test_desktop_tasks import linked as linked

pytestmark = pytest.mark.w0_web


@pytest.mark.parametrize("outcome", ["verified", "rest", "revoked", "expired", "locked", "mismatch"])
async def test_life_worker_spends_one_grant_and_records_only_verified_activity(
    linked, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    owner, companion = linked
    clock = _at(10 * 60)
    _freeze(monkeypatch, clock)
    monkeypatch.setattr(desktop, "utcnow", lambda: clock)
    ids = await _seed_day()
    await _seed_routine()
    async with session_scope() as session:
        segment = await session.get(ScheduleSegmentRow, ids["work"])
        assert segment is not None
        segment.kind = "rest" if outcome == "rest" else "hobby"

    request_id = str(uuid4())
    note = "今夜记下三颗星\n春天要去天台。"
    expires = clock + timedelta(seconds=8 if outcome == "expired" else 120)
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
    state = (await owner.get(f"{PATH}/autonomy")).json()["grant"]
    assert state["decision"] == ("rest" if outcome == "rest" else "queued")

    if outcome == "rest":
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
