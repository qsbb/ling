"""Speak lease behavior on supported local SQLite databases."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import ling.infra.coordination as coordination
from ling.infra.coordination import SpeakLeaseStore
from ling.infra.models import SpeakLeaseRow


@pytest_asyncio.fixture(params=["memory", "file"])
async def sqlite_store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[SpeakLeaseStore, str]]:
    mode = str(request.param)
    url = "sqlite+aiosqlite:///:memory:" if mode == "memory" else f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}"
    engine = create_async_engine(url, connect_args={"timeout": 5})
    async with engine.begin() as connection:
        await connection.run_sync(SpeakLeaseRow.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sqlite_scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(coordination, "session_scope", sqlite_scope)
    try:
        yield SpeakLeaseStore(lease_seconds=60), mode
    finally:
        await engine.dispose()


async def test_sqlite_lease_queue_and_release(
    sqlite_store: tuple[SpeakLeaseStore, str]
) -> None:
    store, _ = sqlite_store
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


async def test_sqlite_file_concurrent_first_lease(
    sqlite_store: tuple[SpeakLeaseStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, mode = sqlite_store
    if mode == "memory":
        pytest.skip("SQLite in-memory engine uses one shared connection")
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
