"""Independent PostgreSQL checks for concurrent initial presence creation."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, text

from ling.domains.presence.service import PresenceService
from ling.infra.db import get_session_factory
from ling.infra.models import PresenceState


async def _blocked_by(factory, blocked_pid: int, blocker_pid: int) -> None:
    async def poll() -> None:
        async with factory() as observer:
            while True:
                blockers = await observer.scalar(
                    text('SELECT pg_blocking_pids(:pid)'), {'pid': blocked_pid}
                )
                if blocker_pid in blockers:
                    return
                await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=5)


async def test_contending_initializer_reads_committed_winner(web_env):
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        first_pid = await first.scalar(text('SELECT pg_backend_pid()'))
        second_pid = await second.scalar(text('SELECT pg_backend_pid()'))
        assert first_pid != second_pid
        winner = await PresenceService(first, actor_id='independent-contended')._row()
        winner.energy, winner.social_load, winner.satiety, winner.revision = 19, 54, 33, 9
        winner.settled_at = datetime(2026, 1, 2, tzinfo=UTC)
        await first.flush()
        waiter = asyncio.create_task(
            PresenceService(second, actor_id='independent-contended')._row()
        )
        try:
            await _blocked_by(factory, second_pid, first_pid)
            assert not waiter.done()
            await first.commit()
            actual = await asyncio.wait_for(waiter, timeout=5)
            assert (actual.energy, actual.social_load, actual.satiety, actual.revision) == (19, 54, 33, 9)
            assert actual.settled_at == datetime(2026, 1, 2, tzinfo=UTC)
            assert await second.scalar(select(1)) == 1
            await second.rollback()
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    async with factory() as observer:
        rows = (await observer.scalars(select(PresenceState).where(
            PresenceState.actor_id == 'independent-contended'
        ))).all()
        assert len(rows) == 1 and (rows[0].energy, rows[0].revision) == (19, 9)


async def test_other_actor_can_initialize_before_first_transaction_finishes(web_env):
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        await PresenceService(first, actor_id='independent-first')._row()
        other = await asyncio.wait_for(
            PresenceService(second, actor_id='independent-other')._row(), timeout=5
        )
        assert other.actor_id == 'independent-other'
        await second.commit()
        await first.rollback()
    async with factory() as observer:
        assert await observer.get(PresenceState, 'independent-first') is None
        assert await observer.get(PresenceState, 'independent-other') is not None


async def test_cancelled_waiter_does_not_prevent_later_read(web_env):
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        first_pid = await first.scalar(text('SELECT pg_backend_pid()'))
        second_pid = await second.scalar(text('SELECT pg_backend_pid()'))
        assert first_pid != second_pid
        await PresenceService(first, actor_id='independent-cancel')._row()
        waiter = asyncio.create_task(PresenceService(second, actor_id='independent-cancel')._row())
        try:
            await _blocked_by(factory, second_pid, first_pid)
            waiter.cancel()
            result = await asyncio.gather(waiter, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            await second.rollback()
            await first.commit()
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    async with factory() as observer:
        row = await PresenceService(observer, actor_id='independent-cancel')._row()
        assert (row.energy, row.social_load, row.revision) == (70, 20, 0)
        assert await observer.scalar(select(1)) == 1
