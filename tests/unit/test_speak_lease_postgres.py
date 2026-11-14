"""Speak lease queue and first-claim behavior on PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ling.infra.coordination as coordination
from ling.infra.coordination import SpeakLeaseStore


@pytest.fixture
def postgres_store(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> SpeakLeaseStore:
    @asynccontextmanager
    async def postgres_scope() -> AsyncIterator[AsyncSession]:
        async with postgres_session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    monkeypatch.setattr(coordination, "session_scope", postgres_scope)
    return SpeakLeaseStore(lease_seconds=60)


async def test_postgres_lease_queue_and_release(postgres_store: SpeakLeaseStore) -> None:
    store = postgres_store
    assert (await store.acquire(actor_id="a", window_id="one", turn_id="t1")).acquired
    queued = await store.acquire(actor_id="a", window_id="two", turn_id="t2")
    assert not queued.acquired and queued.queue_position == 1
    assert await store.try_promote(actor_id="a", window_id="two", turn_id="t2") is False
    assert (await store.snapshot("a"))["holder_window_id"] == "one"
    assert await store.release(actor_id="a", window_id="one", turn_id="t1")
    assert await store.try_promote(actor_id="a", window_id="two", turn_id="t2")
    assert (await store.snapshot("a"))["holder_window_id"] == "two"
    assert await store.release(actor_id="a", window_id="two", turn_id="t2")
    assert (await store.snapshot("a"))["holder_window_id"] is None


async def test_postgres_concurrent_first_lease(
    postgres_store: SpeakLeaseStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_store
    original_scope = coordination.session_scope
    barrier = asyncio.Barrier(2)
    connections: set[int] = set()

    @asynccontextmanager
    async def synchronized_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            connection = await session.connection()
            connections.add(id(connection.sync_connection.connection.dbapi_connection))
            original_execute = session.execute
            started = False

            async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                nonlocal started
                if not started and getattr(statement, "is_insert", False):
                    started = True
                    await asyncio.wait_for(barrier.wait(), timeout=5)
                return await original_execute(statement, *args, **kwargs)

            session.execute = execute  # type: ignore[method-assign]
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(coordination, "session_scope", synchronized_scope)
        results = await asyncio.wait_for(
            asyncio.gather(
                store.acquire(actor_id="a", window_id="one", turn_id="t1"),
                store.acquire(actor_id="a", window_id="two", turn_id="t2"),
            ),
            timeout=10,
        )
    assert len(connections) == 2
    assert sum(result.acquired for result in results) == 1
    holder = "one" if results[0].acquired else "two"
    waiter = "two#t2" if results[0].acquired else "one#t1"
    snapshot = await store.snapshot("a")
    assert snapshot["holder_window_id"] == holder
    assert snapshot["queue"] == [waiter]
