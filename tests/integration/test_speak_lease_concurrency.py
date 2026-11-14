"""Real database races and transaction boundaries for speak leases."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import ling.infra.coordination as coordination
from ling.infra.coordination import LeaseResult, SpeakLeaseStore
from ling.infra.db import session_scope
from ling.infra.models import SpeakLeaseRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _invoke(store: SpeakLeaseStore, operation: str, window: str) -> LeaseResult | bool:
    params = {"actor_id": "race_actor", "window_id": window, "turn_id": f"turn_{window}"}
    if operation == "acquire":
        return await store.acquire(**params)
    return await store.try_promote(**params)


def _acquired(result: LeaseResult | bool) -> bool:
    return result.acquired if isinstance(result, LeaseResult) else result


@pytest.mark.parametrize(
    ("first_operation", "second_operation"),
    [
        ("acquire", "acquire"),
        ("promote", "promote"),
        ("acquire", "promote"),
        ("promote", "acquire"),
    ],
)
async def test_concurrent_first_lease_uses_one_holder(
    web_env: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    first_operation: str,
    second_operation: str,
) -> None:
    """Both independent connections reach their first INSERT before either continues."""
    original_scope = coordination.session_scope
    barrier = asyncio.Barrier(2)
    connection_pids: set[int] = set()

    @asynccontextmanager
    async def racing_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            connection = await session.connection()
            connection_pids.add(await connection.scalar(text("SELECT pg_backend_pid()")))
            original_execute = session.execute
            original_flush = session.flush
            reached_write = False

            async def wait_at_first_insert() -> None:
                nonlocal reached_write
                if not reached_write:
                    reached_write = True
                    await asyncio.wait_for(barrier.wait(), timeout=5)

            async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                if getattr(statement, "is_insert", False) and statement.table.name == "speak_leases":
                    await wait_at_first_insert()
                return await original_execute(statement, *args, **kwargs)

            async def flush(*args: Any, **kwargs: Any) -> None:
                if any(isinstance(item, SpeakLeaseRow) for item in session.new):
                    await wait_at_first_insert()
                await original_flush(*args, **kwargs)

            session.execute = execute  # type: ignore[method-assign]
            session.flush = flush  # type: ignore[method-assign]
            yield session

    stores = [SpeakLeaseStore(lease_seconds=60), SpeakLeaseStore(lease_seconds=60)]
    with monkeypatch.context() as patch:
        patch.setattr(coordination, "session_scope", racing_scope)
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                _invoke(stores[0], first_operation, "one"),
                _invoke(stores[1], second_operation, "two"),
                return_exceptions=True,
            ),
            timeout=10,
        )

    assert len(connection_pids) == 2, connection_pids
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
    results = [outcome for outcome in outcomes if isinstance(outcome, (LeaseResult, bool))]
    assert len(results) == 2
    assert sum(_acquired(result) for result in results) == 1

    holder_index = next(index for index, result in enumerate(results) if _acquired(result))
    loser_index = 1 - holder_index
    holder_window = ("one", "two")[holder_index]
    loser_window = ("one", "two")[loser_index]
    snapshot = await stores[0].snapshot("race_actor")
    assert snapshot["holder_window_id"] == holder_window
    assert snapshot["holder_turn_id"] == f"turn_{holder_window}"
    expected_queue = []
    if (first_operation, second_operation)[loser_index] == "acquire":
        expected_queue = [f"{loser_window}#turn_{loser_window}"]
        loser_result = results[loser_index]
        assert isinstance(loser_result, LeaseResult)
        assert loser_result.holder_window_id == holder_window
        assert loser_result.queue_position == 1
    assert snapshot["queue"] == expected_queue


async def test_queue_holder_and_other_actor_are_independent(web_env: TestEnv) -> None:
    store = SpeakLeaseStore(lease_seconds=60)
    assert (await store.acquire(actor_id="a", window_id="w1", turn_id="t1")).acquired
    assert (await store.acquire(actor_id="b", window_id="other", turn_id="t0")).acquired
    first = await store.acquire(actor_id="a", window_id="w2", turn_id="t2")
    again = await store.acquire(actor_id="a", window_id="w2", turn_id="t2")
    assert not first.acquired and not again.acquired
    assert first.queue_position == again.queue_position == 1
    assert await store.try_promote(actor_id="a", window_id="w2", turn_id="t2") is False
    assert (await store.snapshot("a"))["queue"] == ["w2#t2"]
    assert (await store.snapshot("b"))["holder_window_id"] == "other"
    assert await store.release(actor_id="a", window_id="w1", turn_id="t1") is True
    assert await store.try_promote(actor_id="a", window_id="w2", turn_id="t2") is True
    assert (await store.snapshot("a"))["holder_window_id"] == "w2"
    assert await store.release(actor_id="a", window_id="w2", turn_id="t2") is True
    assert (await store.snapshot("a"))["holder_window_id"] is None


async def test_concurrent_expired_takeover_keeps_one_holder(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpeakLeaseStore(lease_seconds=60)
    assert (await store.acquire(actor_id="expired", window_id="old", turn_id="old")).acquired
    await store.acquire(actor_id="expired", window_id="one", turn_id="t1")
    await store.acquire(actor_id="expired", window_id="two", turn_id="t2")
    async with session_scope() as session:
        await session.execute(
            update(SpeakLeaseRow)
            .where(SpeakLeaseRow.actor_id == "expired")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    original_scope = coordination.session_scope
    barrier = asyncio.Barrier(2)
    pids: set[int] = set()

    @asynccontextmanager
    async def starting_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            connection = await session.connection()
            pids.add(await connection.scalar(text("SELECT pg_backend_pid()")))
            await asyncio.wait_for(barrier.wait(), timeout=5)
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(coordination, "session_scope", starting_scope)
        first, second = await asyncio.wait_for(
            asyncio.gather(
                store.acquire(actor_id="expired", window_id="one", turn_id="t1"),
                store.try_promote(actor_id="expired", window_id="two", turn_id="t2"),
            ),
            timeout=10,
        )
    assert len(pids) == 2
    assert first.acquired != second
    snapshot = await store.snapshot("expired")
    if first.acquired:
        assert snapshot["holder_window_id"] == "one"
        assert snapshot["queue"] == ["one#t1", "two#t2"]
    else:
        assert snapshot["holder_window_id"] == "two"
        assert snapshot["queue"] == ["one#t1"]


@pytest.mark.parametrize("failure", ["exception", "cancel"])
async def test_interrupted_creation_rolls_back_and_can_retry(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original_scope = coordination.session_scope
    ready = asyncio.Event()
    continue_commit = asyncio.Event()

    @asynccontextmanager
    async def interrupted_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            yield session
            ready.set()
            if failure == "exception":
                raise RuntimeError("controlled failure before commit")
            await continue_commit.wait()

    store = SpeakLeaseStore(lease_seconds=60)
    with monkeypatch.context() as patch:
        patch.setattr(coordination, "session_scope", interrupted_scope)
        task = asyncio.create_task(
            store.acquire(actor_id="interrupted", window_id="first", turn_id="t1")
        )
        await asyncio.wait_for(ready.wait(), timeout=5)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="controlled failure"):
                await task

    assert (await store.snapshot("interrupted"))["holder_window_id"] is None
    result = await asyncio.wait_for(
        store.acquire(actor_id="interrupted", window_id="retry", turn_id="t2"), timeout=5
    )
    assert result.acquired
    assert (await store.snapshot("interrupted"))["holder_window_id"] == "retry"
    assert await store.release(actor_id="interrupted", window_id="retry", turn_id="t2")


async def test_store_commit_does_not_commit_another_session(web_env: TestEnv) -> None:
    store = SpeakLeaseStore(lease_seconds=60)
    with pytest.raises(RuntimeError, match="rollback caller"):
        async with session_scope() as caller:
            caller.add(
                SpeakLeaseRow(
                    actor_id="caller",
                    window_id="w1",
                    turn_id="t1",
                    expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    queue=[],
                )
            )
            await caller.flush()
            assert (await store.acquire(actor_id="store", window_id="w2", turn_id="t2")).acquired
            raise RuntimeError("rollback caller")
    async with session_scope() as session:
        assert await session.scalar(select(SpeakLeaseRow).where(SpeakLeaseRow.actor_id == "caller")) is None
        assert await session.scalar(select(SpeakLeaseRow).where(SpeakLeaseRow.actor_id == "store")) is not None


async def test_retries_when_conflicting_lease_is_deleted_before_lock(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpeakLeaseStore(lease_seconds=60)
    assert (await store.acquire(actor_id="deleted", window_id="old", turn_id="t1")).acquired
    original_scope = coordination.session_scope
    conflict_observed = asyncio.Event()
    continue_after_release = asyncio.Event()

    @asynccontextmanager
    async def paused_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            if asyncio.current_task().get_name() == "lease-contender":
                original_execute = session.execute
                paused = False

                async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                    nonlocal paused
                    result = await original_execute(statement, *args, **kwargs)
                    if not paused and getattr(statement, "is_insert", False):
                        paused = True
                        conflict_observed.set()
                        await asyncio.wait_for(continue_after_release.wait(), timeout=5)
                    return result

                session.execute = execute  # type: ignore[method-assign]
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(coordination, "session_scope", paused_scope)
        task = asyncio.create_task(
            store.acquire(actor_id="deleted", window_id="new", turn_id="t2"),
            name="lease-contender",
        )
        try:
            await asyncio.wait_for(conflict_observed.wait(), timeout=5)
            assert await asyncio.wait_for(
                store.release(actor_id="deleted", window_id="old", turn_id="t1"), timeout=5
            )
        finally:
            continue_after_release.set()
        result = await asyncio.wait_for(task, timeout=5)
    assert result.acquired
    assert (await store.snapshot("deleted"))["holder_window_id"] == "new"


async def test_cancelled_waiter_releases_connection_and_other_actor_progresses(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpeakLeaseStore(lease_seconds=60)
    assert (await store.acquire(actor_id="locked", window_id="holder", turn_id="t1")).acquired
    original_scope = coordination.session_scope
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    @asynccontextmanager
    async def observed_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            if asyncio.current_task().get_name() == "blocked-lease-waiter":
                connection = await session.connection()
                waiter_pid.set_result(await connection.scalar(text("SELECT pg_backend_pid()")))
            yield session

    async with session_scope() as blocker:
        await blocker.execute(
            select(SpeakLeaseRow).where(SpeakLeaseRow.actor_id == "locked").with_for_update()
        )
        with monkeypatch.context() as patch:
            patch.setattr(coordination, "session_scope", observed_scope)
            task = asyncio.create_task(
                store.acquire(actor_id="locked", window_id="waiter", turn_id="t2"),
                name="blocked-lease-waiter",
            )
            try:
                pid = await asyncio.wait_for(waiter_pid, timeout=5)
                async with session_scope() as observer:
                    for _ in range(200):
                        wait_type = await observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": pid},
                        )
                        if wait_type == "Lock":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("lease waiter did not reach a PostgreSQL lock wait")
                independent = await asyncio.wait_for(
                    store.acquire(actor_id="different", window_id="other", turn_id="t3"),
                    timeout=3,
                )
                assert independent.acquired
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert (await store.snapshot("locked"))["holder_window_id"] == "holder"
    queued = await asyncio.wait_for(
        store.acquire(actor_id="locked", window_id="waiter", turn_id="t2"), timeout=5
    )
    assert not queued.acquired and queued.queue_position == 1
    assert await store.release(actor_id="locked", window_id="holder", turn_id="t1")
    assert (await store.snapshot("locked"))["holder_window_id"] == "waiter"
