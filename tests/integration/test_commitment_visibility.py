"""承诺的默认可见集：默认只含待履行（active），历史状态需显式查询。

真实缺陷（FINDINGS F4）：`list_commitments()` 不按 state 过滤，于是
`cancelled` / `undeliverable` 被当成待履行返回。修复只收窄**默认可见集**，
不删除任何记录——历史必须可查。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.goals.service import COMMITMENT_STATES, GoalsService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _active(env: TestEnv, text: str, *, due_in: timedelta = timedelta(hours=1)) -> str:
    async with session_scope() as session:
        view = await GoalsService(session).create_commitment(
            subject_id=env.subject_id,
            text=text,
            due_at=datetime.now(UTC) + due_in,
        )
    return view.commitment_id


async def _mk_states(env: TestEnv) -> dict[str, str]:
    """造齐五种状态各一条，返回 state -> id。"""
    active = await _active(env, "待履行的事")

    cancelled = await _active(env, "已取消的事")
    async with session_scope() as session:
        await GoalsService(session).cancel(cancelled, reason="计划变了")

    undeliverable = await _active(env, "送不出去的事")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            undeliverable, operation_id=None, delivery_state="unsupported"
        )

    delivered = await _active(env, "已送到的事")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            delivered, operation_id="op_1", delivery_state="delivered"
        )

    missed = await _active(env, "没送到的事")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            missed, operation_id="op_2", delivery_state="failed"
        )

    return {
        "active": active,
        "cancelled": cancelled,
        "undeliverable": undeliverable,
        "delivered": delivered,
        "missed": missed,
    }


async def test_default_only_returns_active(logged_in: TestEnv) -> None:
    ids = await _mk_states(logged_in)
    async with session_scope() as session:
        got = await GoalsService(session).list_commitments(logged_in.subject_id)
    assert [c.commitment_id for c in got] == [ids["active"]]
    assert all(c.state == "active" for c in got)


async def test_explicit_state_returns_history(logged_in: TestEnv) -> None:
    ids = await _mk_states(logged_in)
    for state in ("cancelled", "undeliverable", "delivered", "missed"):
        async with session_scope() as session:
            got = await GoalsService(session).list_commitments(logged_in.subject_id, state=state)
        assert [c.commitment_id for c in got] == [ids[state]], state
        assert all(c.state == state for c in got), state


async def test_all_state_keeps_every_record(logged_in: TestEnv) -> None:
    """默认集合收窄不等于删数据：`state="all"` 必须能看到全部五条。"""
    ids = await _mk_states(logged_in)
    async with session_scope() as session:
        got = await GoalsService(session).list_commitments(logged_in.subject_id, state="all")
    assert {c.commitment_id for c in got} == set(ids.values())
    assert {c.state for c in got} == set(COMMITMENT_STATES)


async def test_unknown_state_is_rejected_not_silently_empty(logged_in: TestEnv) -> None:
    """拼错状态若静默返回空，会被误读成"真没有"；必须报错。"""
    with pytest.raises(ValueError, match="未知承诺状态"):
        async with session_scope() as session:
            await GoalsService(session).list_commitments(
                logged_in.subject_id,
                state="canceled",  # 少一个 l
            )
