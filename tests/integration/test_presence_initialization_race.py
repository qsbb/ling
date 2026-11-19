"""Presence initialization must tolerate races without taking over caller transactions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.presence.service import PresenceService
from ling.infra.db import get_session_factory, session_scope
from ling.infra.models import Message, PresenceState
from tests.integration.test_connector_runtime_multi_acceptance import (
    _bind,
    _chat,
    _event,
    _pair,
    _post,
)
from tests.integration.test_connector_runtime_multi_acceptance import (
    protocol_wire as protocol_wire,
)

pytestmark = pytest.mark.w0_web


def _synchronize_missing_reads(monkeypatch: pytest.MonkeyPatch, actor_id: str) -> list[int]:
    """Make two separate sessions both observe a missing row before either proceeds."""
    original_get = AsyncSession.get
    arrived = asyncio.Event()
    session_ids: set[int] = set()
    backend_pids: list[int] = []

    async def synchronized_get(session, entity, ident, *args, **kwargs):
        row = await original_get(session, entity, ident, *args, **kwargs)
        if (
            entity is PresenceState
            and ident == actor_id
            and row is None
            and id(session) not in session_ids
        ):
            session_ids.add(id(session))
            backend_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            if len(session_ids) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=15)
        return row

    monkeypatch.setattr(AsyncSession, "get", synchronized_get)
    return backend_pids


def _values(row: PresenceState) -> tuple[int, int, int, int]:
    return row.energy, row.social_load, row.satiety, row.revision


async def test_concurrent_postgres_first_reads_are_safe(web_env, monkeypatch):
    actor_id = "race-actor"
    backend_pids = _synchronize_missing_reads(monkeypatch, actor_id)
    factory = get_session_factory()

    async def worker():
        async with factory() as session:
            row = await PresenceService(session, actor_id=actor_id)._row()
            values = _values(row)
            assert await session.scalar(select(1)) == 1
            await session.commit()
            return values

    results = await asyncio.wait_for(
        asyncio.gather(worker(), worker(), return_exceptions=True), timeout=30
    )
    assert len(backend_pids) == 2 and len(set(backend_pids)) == 2
    assert results == [(70, 20, 70, 0), (70, 20, 70, 0)]
    async with session_scope() as session:
        rows = list(
            (
                await session.scalars(
                    select(PresenceState).where(PresenceState.actor_id == actor_id)
                )
            ).all()
        )
        assert len(rows) == 1
        assert _values(rows[0]) == (70, 20, 70, 0)


async def test_existing_values_and_other_actors_remain_independent(web_env):
    timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id="existing-actor",
                energy=23,
                social_load=61,
                satiety=41,
                revision=7,
                settled_at=timestamp,
            )
        )
    async with session_scope() as session:
        existing = await PresenceService(session, actor_id="existing-actor")._row()
        new = await PresenceService(session, actor_id="different-actor")._row()
        assert _values(existing) == (23, 61, 41, 7)
        assert existing.settled_at == timestamp
        assert _values(new) == (70, 20, 70, 0)
        assert existing.actor_id != new.actor_id
    async with session_scope() as session:
        assert _values(await session.get(PresenceState, "existing-actor")) == (23, 61, 41, 7)
        assert _values(await session.get(PresenceState, "different-actor")) == (70, 20, 70, 0)


async def test_concurrent_different_actors_keep_separate_states(web_env):
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id="parallel-existing", energy=24, social_load=52, satiety=39, revision=4
            )
        )
    factory = get_session_factory()
    ready = asyncio.Event()
    backend_pids: list[int] = []

    async def read(actor_id: str):
        async with factory() as session:
            backend_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            if len(backend_pids) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
            row = await PresenceService(session, actor_id=actor_id)._row()
            result = (row.actor_id, _values(row))
            await session.commit()
            return result

    results = await asyncio.wait_for(
        asyncio.gather(read("parallel-existing"), read("parallel-new")), timeout=20
    )
    assert len(backend_pids) == 2 and len(set(backend_pids)) == 2
    assert results == [
        ("parallel-existing", (24, 52, 39, 4)),
        ("parallel-new", (70, 20, 70, 0)),
    ]


async def test_postgres_initialization_does_not_commit_caller_work(web_env):
    factory = get_session_factory()
    async with factory() as writer, factory() as observer:
        writer.add(PresenceState(actor_id="unrelated-pending", energy=11))
        await writer.flush()
        await PresenceService(writer, actor_id="rollback-actor")._row()
        assert writer.in_transaction()
        assert await observer.get(PresenceState, "unrelated-pending") is None
        assert await observer.get(PresenceState, "rollback-actor") is None
        await observer.rollback()
        await writer.rollback()
    async with factory() as observer:
        assert await observer.get(PresenceState, "unrelated-pending") is None
        assert await observer.get(PresenceState, "rollback-actor") is None


async def test_second_postgres_transaction_can_initialize_after_first_rolls_back(
    web_env, monkeypatch
):
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        row = await PresenceService(first, actor_id="rollback-race")._row()
        assert _values(row) == (70, 20, 70, 0)
        missing_seen = asyncio.Event()
        original_get = AsyncSession.get

        async def observe_second(session, entity, ident, *args, **kwargs):
            result = await original_get(session, entity, ident, *args, **kwargs)
            if (
                session is second
                and entity is PresenceState
                and ident == "rollback-race"
                and not missing_seen.is_set()
            ):
                assert result is None
                missing_seen.set()
            return result

        monkeypatch.setattr(AsyncSession, "get", observe_second)
        task = asyncio.create_task(PresenceService(second, actor_id="rollback-race")._row())
        try:
            await asyncio.wait_for(missing_seen.wait(), timeout=10)
            await first.rollback()
            surviving = await asyncio.wait_for(task, timeout=10)
            assert _values(surviving) == (70, 20, 70, 0)
            assert await second.scalar(select(1)) == 1
            await second.commit()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as observer:
        assert _values(await observer.get(PresenceState, "rollback-race")) == (70, 20, 70, 0)


async def test_concurrent_committed_nondefault_values_are_preserved(web_env, monkeypatch):
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        first.add(
            PresenceState(
                actor_id="race-nondefault", energy=19, social_load=54, satiety=33, revision=9
            )
        )
        await first.flush()
        missing_seen = asyncio.Event()
        original_get = AsyncSession.get

        async def observe_second(session, entity, ident, *args, **kwargs):
            result = await original_get(session, entity, ident, *args, **kwargs)
            if (
                session is second
                and entity is PresenceState
                and ident == "race-nondefault"
                and not missing_seen.is_set()
            ):
                assert result is None
                missing_seen.set()
            return result

        monkeypatch.setattr(AsyncSession, "get", observe_second)
        task = asyncio.create_task(PresenceService(second, actor_id="race-nondefault")._row())
        try:
            await asyncio.wait_for(missing_seen.wait(), timeout=10)
            await first.commit()
            row = await asyncio.wait_for(task, timeout=10)
            assert _values(row) == (19, 54, 33, 9)
            assert await second.scalar(select(1)) == 1
            await second.commit()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as observer:
        assert _values(await observer.get(PresenceState, "race-nondefault")) == (19, 54, 33, 9)


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_simultaneous_real_inbound_initializes_presence_once(
    logged_in,
    protocol_wire,
    monkeypatch,
    scope,
):
    await _pair(logged_in, scope)
    await _chat(logged_in, monkeypatch)
    await _bind(logged_in, scope)
    # Both turns must reach prompt compilation concurrently to race on the first
    # presence read. Do not also require either turn to finish within the other
    # turn's floor wait budget; speaker arbitration has its own integration tests.
    monkeypatch.setattr(logged_in.app.state, "speak_lease", None)
    monkeypatch.setattr(logged_in.app.state, "arbiter", None)
    async with session_scope() as session:
        assert await session.get(PresenceState, "actor_ling") is None
    backend_pids = _synchronize_missing_reads(monkeypatch, "actor_ling")
    responses = await asyncio.wait_for(
        asyncio.gather(
            *[
                _post(
                    logged_in,
                    scope,
                    "alpha",
                    _event(scope, f"presence-race-{number}", message_id=10100 + number),
                )
                for number in (1, 2)
            ],
            return_exceptions=True,
        ),
        timeout=45,
    )
    assert len(backend_pids) == 2 and len(set(backend_pids)) == 2
    assert all(not isinstance(response, BaseException) for response in responses), responses
    assert all(
        response.status_code == 200 and response.json().get("accepted") is True
        for response in responses
    ), [response.text for response in responses]
    async with session_scope() as session:
        messages = list(
            (
                await session.scalars(
                    select(Message).where(
                        Message.role == "user",
                        Message.text.in_(["presence-race-1", "presence-race-2"]),
                    )
                )
            ).all()
        )
        assert sorted(message.text for message in messages) == [
            "presence-race-1",
            "presence-race-2",
        ]
        rows = list(
            (
                await session.scalars(
                    select(PresenceState).where(PresenceState.actor_id == "actor_ling")
                )
            ).all()
        )
        assert len(rows) == 1
    assert len(protocol_wire.sends()) == 2


async def test_postgres_initialization_preserves_values_and_caller_rollback(
    postgres_session_factory,
):
    factory = postgres_session_factory
    async with factory() as writer, factory() as observer:
        writer.add(PresenceState(actor_id="pg-unrelated", energy=13))
        await writer.flush()
        created = await PresenceService(writer, actor_id="pg-new")._row()
        assert _values(created) == (70, 20, 70, 0)
        assert writer.in_transaction()
        assert await observer.get(PresenceState, "pg-new") is None
        assert await observer.get(PresenceState, "pg-unrelated") is None
        await observer.rollback()
        await writer.rollback()
    async with factory() as session:
        assert await session.get(PresenceState, "pg-new") is None
        session.add(
            PresenceState(
                actor_id="pg-existing", energy=22, social_load=44, satiety=66, revision=8
            )
        )
        await session.commit()
    async with factory() as session:
        existing = await PresenceService(session, actor_id="pg-existing")._row()
        assert _values(existing) == (22, 44, 66, 8)
        created = await PresenceService(session, actor_id="pg-other")._row()
        assert _values(created) == (70, 20, 70, 0)
        await session.commit()
