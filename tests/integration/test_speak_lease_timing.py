"""Lease deadlines are calculated after database lock waits."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import ling.infra.coordination as coordination
from ling.infra.coordination import LeaseResult, SpeakLeaseStore
from ling.infra.db import get_session_factory, session_scope
from ling.infra.models import SpeakLeaseRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[datetime]:
    class Clock(datetime):
        current = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return cls.current

    monkeypatch.setattr(coordination, "datetime", Clock)
    return Clock


async def _seed_lease(clock: type[datetime], *, expires_in: int, holder: str = "old") -> None:
    async with session_scope() as session:
        session.add(
            SpeakLeaseRow(
                actor_id="timed",
                window_id=holder,
                turn_id=f"{holder}_turn",
                expires_at=clock.now(UTC) + timedelta(seconds=expires_in),
                queue=["candidate#candidate_turn", "other#other_turn"],
            )
        )


async def _wait_for_locked_lease(
    monkeypatch: pytest.MonkeyPatch,
    clock: type[datetime],
    operation: Callable[[], Awaitable[Any]],
    *,
    uncommitted_insert: bool = False,
    transfer_holder: bool = False,
) -> Any:
    factory = get_session_factory()
    original_scope = coordination.session_scope
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    @asynccontextmanager
    async def observed_scope() -> AsyncIterator[AsyncSession]:
        async with original_scope() as session:
            if asyncio.current_task().get_name() == "timed-lease-waiter":
                connection = await session.connection()
                waiter_pid.set_result(await connection.scalar(text("SELECT pg_backend_pid()")))
            yield session

    async with factory() as blocker:
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        if uncommitted_insert:
            blocker.add(
                SpeakLeaseRow(
                    actor_id="timed",
                    window_id="uncommitted",
                    turn_id="uncommitted_turn",
                    expires_at=clock.now(UTC) + timedelta(seconds=10),
                    queue=[],
                )
            )
            await blocker.flush()
        else:
            row = (
                await blocker.execute(
                    select(SpeakLeaseRow)
                    .where(SpeakLeaseRow.actor_id == "timed")
                    .with_for_update()
                )
            ).scalar_one()
            if transfer_holder:
                row.window_id = "new_holder"
                row.turn_id = "new_holder_turn"
                row.expires_at = clock.now(UTC) + timedelta(seconds=40)
                await blocker.flush()

        with monkeypatch.context() as patch:
            patch.setattr(coordination, "session_scope", observed_scope)
            task = asyncio.create_task(operation(), name="timed-lease-waiter")
            try:
                pid = await asyncio.wait_for(waiter_pid, timeout=5)

                async def wait_for_blocker() -> None:
                    async with factory() as observer:
                        while True:
                            assert not task.done(), "lease request finished without waiting"
                            blockers = await observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                            )
                            if blocker_pid in blockers:
                                return
                            await asyncio.sleep(0.02)

                await asyncio.wait_for(wait_for_blocker(), timeout=5)
                assert not task.done()
                clock.current += timedelta(seconds=20)  # type: ignore[attr-defined]
                if uncommitted_insert:
                    await blocker.rollback()
                else:
                    await blocker.commit()
                return await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def _request(store: SpeakLeaseStore, operation: str) -> LeaseResult | bool:
    params = {"actor_id": "timed", "window_id": "candidate", "turn_id": "candidate_turn"}
    if operation == "acquire":
        return await store.acquire(**params)
    return await store.try_promote(**params)


def _succeeded(result: LeaseResult | bool) -> bool:
    return result.acquired if isinstance(result, LeaseResult) else result


async def _assert_current_lease(store: SpeakLeaseStore, clock: type[datetime]) -> None:
    async with session_scope() as session:
        row = await session.get(SpeakLeaseRow, "timed")
        assert row is not None
        assert row.window_id == "candidate"
        assert row.turn_id == "candidate_turn"
        assert row.expires_at == clock.now(UTC) + timedelta(seconds=10)
    contender = await store.acquire(actor_id="timed", window_id="intruder", turn_id="intruder_turn")
    assert not contender.acquired
    assert contender.holder_window_id == "candidate"


@pytest.mark.parametrize("operation", ["acquire", "promote"])
@pytest.mark.parametrize("expires_in", [-1, 10])
async def test_acquisition_rechecks_expiry_after_lock_wait(
    web_env: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    clock: type[datetime],
    operation: str,
    expires_in: int,
) -> None:
    await _seed_lease(clock, expires_in=expires_in)
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(monkeypatch, clock, lambda: _request(store, operation))
    assert _succeeded(result)
    await _assert_current_lease(store, clock)
    snapshot = await store.snapshot("timed")
    expected = ["candidate#candidate_turn", "other#other_turn", "intruder#intruder_turn"]
    if operation == "promote":
        expected.remove("candidate#candidate_turn")
    assert snapshot["queue"] == expected


@pytest.mark.parametrize("operation", ["acquire", "promote"])
async def test_unexpired_holder_survives_lock_wait(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime], operation: str
) -> None:
    await _seed_lease(clock, expires_in=40)
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(monkeypatch, clock, lambda: _request(store, operation))
    assert not _succeeded(result)
    assert (await store.snapshot("timed"))["holder_window_id"] == "old"


@pytest.mark.parametrize("operation", ["acquire", "promote"])
async def test_same_holder_renewal_after_lock_wait(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime], operation: str
) -> None:
    await _seed_lease(clock, expires_in=40, holder="candidate")
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(monkeypatch, clock, lambda: _request(store, operation))
    assert _succeeded(result)
    await _assert_current_lease(store, clock)


@pytest.mark.parametrize("operation", ["acquire", "promote"])
async def test_first_insert_after_other_transaction_rolls_back(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime], operation: str
) -> None:
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(
        monkeypatch, clock, lambda: _request(store, operation), uncommitted_insert=True
    )
    assert _succeeded(result)
    await _assert_current_lease(store, clock)


async def test_release_sets_promoted_deadline_after_lock_wait(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime]
) -> None:
    await _seed_lease(clock, expires_in=40)
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(
        monkeypatch,
        clock,
        lambda: store.release(actor_id="timed", window_id="old", turn_id="old_turn"),
    )
    assert result is True
    await _assert_current_lease(store, clock)
    assert (await store.snapshot("timed"))["queue"] == ["other#other_turn", "intruder#intruder_turn"]


async def test_extend_sets_deadline_after_lock_wait(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime]
) -> None:
    await _seed_lease(clock, expires_in=40, holder="candidate")
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(
        monkeypatch,
        clock,
        lambda: store.extend(actor_id="timed", window_id="candidate", turn_id="candidate_turn"),
    )
    assert result is True
    await _assert_current_lease(store, clock)


async def test_extend_rechecks_holder_after_lock_wait(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, clock: type[datetime]
) -> None:
    await _seed_lease(clock, expires_in=40, holder="candidate")
    original_deadline = clock.now(UTC) + timedelta(seconds=40)
    store = SpeakLeaseStore(lease_seconds=10)
    result = await _wait_for_locked_lease(
        monkeypatch,
        clock,
        lambda: store.extend(actor_id="timed", window_id="candidate", turn_id="candidate_turn"),
        transfer_holder=True,
    )
    assert result is False
    async with session_scope() as session:
        row = await session.get(SpeakLeaseRow, "timed")
        assert row is not None
        assert row.window_id == "new_holder"
        assert row.expires_at == original_deadline
