"""并发更新同一关系/情绪投影：不能丢失更新（此前实现的风险）。

"读出 → 计算 → 写回"在并发下会丢失更新：
两个 worker 同时读到 familiarity=0.10，各自 +0.02 写回 0.12，
应该是 0.14。行锁（SELECT FOR UPDATE）保证串行提交。

这些测试用真实并发事务验证，不是模拟。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from ling.contracts.v1 import Origin
from ling.domains.affect.service import AffectService
from ling.domains.relationship.service import RelationshipService
from ling.infra.db import session_scope
from ling.infra.models import RelationshipState
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_concurrent_warmth_events_accumulate(logged_in: TestEnv) -> None:
    """N 个并发善意事件：熟悉度增量必须全部生效。"""
    subject_id = logged_in.subject_id
    rounds = 6

    async def one_round(i: int) -> None:
        async with session_scope() as session:
            await RelationshipService(session).record_event(
                subject_id=subject_id,
                kind="warmth",
                origin=Origin.user_statement,
                source_event_ids=[f"evt_{i}"],
                turn_id=f"turn_conc_{i}",
            )

    await asyncio.gather(*(one_round(i) for i in range(rounds)))

    async with session_scope() as session:
        row = (
            await session.execute(
                select(RelationshipState).where(
                    RelationshipState.actor_id == "actor_ling",
                    RelationshipState.subject_id == subject_id,
                )
            )
        ).scalar_one()
    # warmth 每轮 +0.02；6 轮应为 0.12
    expected = round(0.02 * rounds, 4)
    assert abs(row.familiarity - expected) < 1e-6, (
        f"丢失更新：期望 {expected}，实际 {row.familiarity}"
    )


async def test_concurrent_affect_nudges_accumulate(logged_in: TestEnv) -> None:
    """N 个并发情绪注入：强度增量必须全部生效。"""
    rounds = 6

    async def one_round(i: int) -> None:
        async with session_scope() as session:
            await AffectService(session).nudge("curious", 0.05, scope="global")

    # 清掉既有状态，保证基线确定
    async with session_scope() as session:
        from ling.infra.models import AffectState

        rows = list(
            (
                await session.execute(
                    select(AffectState).where(AffectState.actor_id == "actor_ling")
                )
            ).scalars()
        )
        for row in rows:
            await session.delete(row)

    await asyncio.gather(*(one_round(i) for i in range(rounds)))

    async with session_scope() as session:
        view = await AffectService(session).current()
    expected = round(0.05 * rounds, 3)
    assert abs(view.intensity - expected) < 0.02, (
        f"情绪更新丢失：期望约 {expected}，实际 {view.intensity}"
    )


async def test_repeated_event_does_not_double_apply(logged_in: TestEnv) -> None:
    """同一 turn 重复提交（幂等）与并发（不同 turn）要能区分开。"""
    subject_id = logged_in.subject_id
    # 同一 turn 提交 3 次：只应生效一次
    for _ in range(3):
        async with session_scope() as session:
            await RelationshipService(session).record_event(
                subject_id=subject_id,
                kind="warmth",
                origin=Origin.user_statement,
                source_event_ids=["evt_same"],
                turn_id="turn_same",
            )
    async with session_scope() as session:
        row = (
            await session.execute(
                select(RelationshipState).where(
                    RelationshipState.actor_id == "actor_ling",
                    RelationshipState.subject_id == subject_id,
                )
            )
        ).scalar_one()
    assert abs(row.familiarity - 0.02) < 1e-6, (
        f"同一 turn 被重复计入：{row.familiarity}"
    )
