"""PostgreSQL cross-connection tests for proactive daily quota reservations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.proactive.dispatch import (
    _release_daily_quota,
    _reserve_daily_quota,
    _settle_daily_quota,
)
from ling.infra.db import get_session_factory, session_scope
from ling.infra.models import ProactiveOpportunity
from ling.infra.proactive_quota import ProactiveQuotaReservation

pytestmark = pytest.mark.w0_web


async def _seed_opportunities(env, opportunity_ids: list[str]) -> None:
    async with session_scope() as session:
        for opportunity_id in opportunity_ids:
            session.add(
                ProactiveOpportunity(
                    id=opportunity_id,
                    actor_id=env.settings.actor_id,
                    subject_id=env.subject_id,
                    kind="quota-test",
                    reason="synthetic test reason",
                    state="candidate",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )


async def test_two_postgresql_connections_admit_at_most_the_daily_limit(logged_in):
    """Concurrent reservations for distinct opportunities share one subject-day cap."""
    engine = get_session_factory().kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL and independent connections")

    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    opportunity_ids = ["quota-race-a", "quota-race-b"]
    await _seed_opportunities(logged_in, opportunity_ids)
    barrier = asyncio.Barrier(2)
    backend_pids: list[int] = []

    async def reserve(opportunity_id: str) -> bool:
        async with engine.connect() as connection:
            async with connection.begin():
                backend_pids.append(await connection.scalar(text("SELECT pg_backend_pid()")))
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                await barrier.wait()
                return await _reserve_daily_quota(
                    session,
                    actor_id=logged_in.settings.actor_id,
                    subject_id=logged_in.subject_id,
                    opportunity_id=opportunity_id,
                    daily_limit=1,
                    timezone_name="America/Los_Angeles",
                    now=now,
                )

    results = await asyncio.wait_for(
        asyncio.gather(*(reserve(item) for item in opportunity_ids)), timeout=15
    )
    assert len(set(backend_pids)) == 2
    assert sum(results) == 1
    async with session_scope() as session:
        reservations = list(
            (
                await session.scalars(
                    select(ProactiveQuotaReservation).where(
                        ProactiveQuotaReservation.opportunity_id.in_(opportunity_ids)
                    )
                )
            ).all()
        )
    assert len(reservations) == 1
    assert reservations[0].state == "reserved"


@pytest.mark.parametrize("final_state", ["sent", "uncertain"])
async def test_sent_and_uncertain_reservations_continue_to_count(logged_in, final_state):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    first_id, second_id = f"quota-{final_state}-a", f"quota-{final_state}-b"
    await _seed_opportunities(logged_in, [first_id, second_id])

    async with session_scope() as session:
        assert await _reserve_daily_quota(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            opportunity_id=first_id,
            daily_limit=1,
            timezone_name="America/Los_Angeles",
            now=now,
        )
        await _settle_daily_quota(
            session, opportunity_id=first_id, delivery_state=final_state
        )

    async with session_scope() as session:
        assert not await _reserve_daily_quota(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            opportunity_id=second_id,
            daily_limit=1,
            timezone_name="America/Los_Angeles",
            now=now,
        )


async def test_explicit_pre_send_release_frees_the_daily_slot(logged_in):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    first_id, second_id = "quota-release-a", "quota-release-b"
    await _seed_opportunities(logged_in, [first_id, second_id])

    async with session_scope() as session:
        assert await _reserve_daily_quota(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            opportunity_id=first_id,
            daily_limit=1,
            timezone_name="America/Los_Angeles",
            now=now,
        )
        await _release_daily_quota(session, opportunity_id=first_id)

    async with session_scope() as session:
        assert await _reserve_daily_quota(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            opportunity_id=second_id,
            daily_limit=1,
            timezone_name="America/Los_Angeles",
            now=now,
        )


async def test_quota_day_uses_the_subject_timezone(logged_in):
    opportunity_ids = ["quota-day-before", "quota-day-after"]
    await _seed_opportunities(logged_in, opportunity_ids)
    moments = [
        datetime(2026, 11, 15, 6, 30, tzinfo=UTC),
        datetime(2026, 11, 15, 8, 30, tzinfo=UTC),
    ]

    for opportunity_id, now in zip(opportunity_ids, moments, strict=True):
        async with session_scope() as session:
            assert await _reserve_daily_quota(
                session,
                actor_id=logged_in.settings.actor_id,
                subject_id=logged_in.subject_id,
                opportunity_id=opportunity_id,
                daily_limit=1,
                timezone_name="America/Los_Angeles",
                now=now,
            )


async def test_bound_policy_guard_is_released_before_send(logged_in, monkeypatch):
    from ling.domains.proactive import dispatch
    from ling.settings.window_policy import lock_dialogue_config

    async def guarded_check(session, **_kwargs):
        await lock_dialogue_config(session)
        return True

    monkeypatch.setattr(
        dispatch, "_bound_delivery_target_allowed_in_session", guarded_check
    )
    async with session_scope() as session:
        assert await dispatch.bound_delivery_target_allowed(
            session,
            opportunity_id="quota-guard-check",
            window_id="quota-guard-window",
            config_reader=object(),
            channel="web",
            expected_target="synthetic-target",
        )

    writer_acquired = False
    async with session_scope() as writer:
        await asyncio.wait_for(lock_dialogue_config(writer), timeout=3)
        writer_acquired = True
    sends: list[str] = []
    if writer_acquired:
        sends.append("after-policy-check")
    assert sends == ["after-policy-check"]
