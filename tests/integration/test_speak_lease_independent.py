"""Independent PostgreSQL lease timing and lock-wait checks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

import ling.infra.coordination as coordination
from ling.infra.coordination import SpeakLeaseStore
from ling.infra.db import get_session_factory, session_scope
from ling.infra.models import SpeakLeaseRow

pytestmark = pytest.mark.w0_web


async def test_lease_remains_valid_after_waiting_for_row_lock(web_env, monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(coordination, "datetime", Clock)
    async with session_scope() as session:
        session.add(SpeakLeaseRow(
            actor_id="independent-wait", window_id="old", turn_id="old-turn",
            expires_at=start - timedelta(seconds=1), queue=[],
        ))
    factory = get_session_factory()
    original_scope = coordination.session_scope
    waiter_pid = asyncio.get_running_loop().create_future()

    @asynccontextmanager
    async def observed_scope():
        async with original_scope() as session:
            if asyncio.current_task().get_name() == "controlled-lease-waiter":
                waiter_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            yield session

    monkeypatch.setattr(coordination, "session_scope", observed_scope)
    store = SpeakLeaseStore(lease_seconds=10)
    async with factory() as blocker:
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(select(SpeakLeaseRow).where(
            SpeakLeaseRow.actor_id == "independent-wait"
        ).with_for_update())
        task = asyncio.create_task(
            store.acquire(actor_id="independent-wait", window_id="first", turn_id="first-turn"),
            name="controlled-lease-waiter",
        )
        try:
            pid = await asyncio.wait_for(waiter_pid, 5)
            async def blocked_by_holder():
                async with factory() as observer:
                    while True:
                        blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"),
                                                         {"pid": pid})
                        if blocker_pid in blockers:
                            return
                        await asyncio.sleep(0.02)
            await asyncio.wait_for(blocked_by_holder(), 5)
            assert not task.done()
            Clock.current = start + timedelta(seconds=20)
            await blocker.commit()
            first = await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as observer:
        lease = await observer.get(SpeakLeaseRow, "independent-wait")
        assert lease.window_id == "first"
        expiry = lease.expires_at
    second = await store.acquire(
        actor_id="independent-wait", window_id="second", turn_id="second-turn"
    )
    assert first.acquired and not second.acquired, (first, second, expiry, Clock.current)
    assert expiry > Clock.current


async def test_two_first_acquires_use_one_holder(web_env, monkeypatch):
    original = coordination._lock_or_create_lease
    arrived = asyncio.Event()
    release = asyncio.Event()
    pids = []

    async def synchronized(session, **kwargs):
        pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        if len(pids) == 2:
            arrived.set()
        await release.wait()
        return await original(session, **kwargs)

    monkeypatch.setattr(coordination, "_lock_or_create_lease", synchronized)
    store = SpeakLeaseStore(lease_seconds=60)
    tasks = [asyncio.create_task(store.acquire(
        actor_id="independent-cold", window_id=window, turn_id=f"{window}-turn"
    )) for window in ("first", "second")]
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        assert len(set(pids)) == 2
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
        assert sum(outcome.acquired for outcome in outcomes) == 1
        holder = next(outcome for outcome in outcomes if outcome.acquired)
        snapshot = await store.snapshot("independent-cold")
        assert snapshot["holder_window_id"] == holder.holder_window_id
        assert len(snapshot["queue"]) == 1
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_nonholder_cannot_release_the_active_lease(web_env):
    store = SpeakLeaseStore(lease_seconds=60)
    holder = await store.acquire(actor_id="independent-owner", window_id="owner", turn_id="t1")
    assert holder.acquired
    assert not await store.release(actor_id="independent-owner", window_id="other", turn_id="t2")
    snapshot = await store.snapshot("independent-owner")
    assert (snapshot["holder_window_id"], snapshot["holder_turn_id"]) == ("owner", "t1")
    assert await store.release(actor_id="independent-owner", window_id="owner", turn_id="t1")
    assert (await store.snapshot("independent-owner"))["holder_window_id"] is None


async def test_concurrent_first_web_turns_finish_without_lease_conflict(logged_in, monkeypatch):
    from collections.abc import AsyncIterator

    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk
    from tests.integration.test_window_dialogue_policy import create_window, publish

    await publish(logged_in, False)
    windows = [await create_window(logged_in), await create_window(logged_in)]
    inner_router = logged_in.app.state.router

    class Router:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            return getattr(inner_router, name)

        async def stream(self, config, **kwargs) -> AsyncIterator:
            self.calls += 1
            yield ModelChunk(delta="independent lease response"), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text="independent lease response")

    router = Router()
    logged_in.app.state.router = router
    original = coordination._lock_or_create_lease
    arrived = asyncio.Event()
    release = asyncio.Event()
    pids = []

    async def synchronized(session, **kwargs):
        pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        if len(pids) == 2:
            arrived.set()
        if len(pids) <= 2:
            await release.wait()
        return await original(session, **kwargs)

    monkeypatch.setattr(coordination, "_lock_or_create_lease", synchronized)
    tasks = [asyncio.create_task(logged_in.client.post(
        f"/v1/sessions/{window}/turns",
        json={"client_turn_id": f"lease-http-{index}", "text": "你好"},
    )) for index, window in enumerate(windows)]
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        assert len(set(pids)) == 2
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert all(response.status_code == 200 for response in responses)
        assert all("event: turn.completed" in response.text for response in responses), [
            response.text for response in responses
        ]
        assert all("event: turn.failed" not in response.text for response in responses)
        assert router.calls == 2
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
