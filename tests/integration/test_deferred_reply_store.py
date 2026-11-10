"""延后回复的持久化层：真 PostgreSQL 上的并发、栅栏、二分。

## 为什么这些必须是集成测试

三件事只有在真库上才有意义：

1. **两个独立 session 并发认领同一行**——条件更新在顺序执行时**必然**通过，
   那样的测试什么都证明不了。必须真的让两个事务同时跑（照
   `test_media_slot.py` 的写法）。
2. **`UNIQUE(source_turn_id)` 的并发插入**——进程内去重在多副本时等于没有。
3. **`claimed → pending` 的二分**（`model_started_at` 为空/非空）——搞反了就是
   "崩溃后重复烧钱"。

## 每条用例在钉什么

- 同一轮只登记一条（重复登记返回 `None`，库里仍只有一行）；
- 到期前认领不到、到期后认领得到；
- **20 个并发认领恰好 1 个赢**；
- 抢占是派生的：窗口里出现更晚的 user 消息即被抢占，`actor` 消息不算；
- 租约过期二分：没调模型回 `pending`、调过模型走 `failed`；
- 终态吸收：已 `delivered` 的记录不能再被改成 `cancelled`；
- 硬截止到期 → `failed`，**不假装已送达**。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.deferred import service as deferred
from ling.domains.deferred.state import DeferredReplyState
from ling.infra.db import session_scope
from ling.infra.models import DeferredReply, Message, Turn, Window
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    web_env,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


async def _make_window(window_id: str = "win_def") -> None:
    async with session_scope() as session:
        session.add(
            Window(
                id=window_id,
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_a",
                title="测试窗口",
                channel="web",
            )
        )


async def _make_turn(window_id: str, turn_id: str) -> None:
    async with session_scope() as session:
        session.add(
            Turn(
                id=turn_id,
                window_id=window_id,
                client_turn_id=turn_id,
                state="completed",
                config_revision=1,
            )
        )


async def _add_user_message(window_id: str, *, sequence: int, text: str = "在吗") -> None:
    async with session_scope() as session:
        session.add(
            Message(
                id=f"msg_{window_id}_{sequence}",
                window_id=window_id,
                role="user",
                text=text,
                sequence=sequence,
            )
        )


async def _schedule(
    *,
    turn_id: str = "turn_a",
    window_id: str = "win_def",
    delay_ms: int = 45_000,
    now: datetime = NOW,
    input_sequence: int = 1,
    requested_ms: int | None = None,
) -> DeferredReply | None:
    async with session_scope() as session:
        return await deferred.schedule(
            session,
            actor_id="actor_ling",
            window_id=window_id,
            subject_id="subj_a",
            source_turn_id=turn_id,
            source_message_id=f"msg_{window_id}_{input_sequence}",
            input_sequence=input_sequence,
            applied_delay_ms=delay_ms,
            requested_delay_ms=requested_ms if requested_ms is not None else delay_ms,
            clamped=requested_ms is not None and requested_ms > delay_ms,
            now=now,
        )


# ------------------------------------------------------------------ 登记


async def test_schedule_persists_deadlines_from_now(web_env: TestEnv) -> None:  # noqa: F811
    """`chosen_due_at` 与 `hard_deadline_at` 都按**登记时刻**算定并写死。

    写死而不是读时再算，是"重启后仍按原截止"（断言 8）的前提。
    """
    await _make_window()
    await _make_turn("win_def", "turn_a")
    row = await _schedule(delay_ms=45_000)
    assert row is not None
    assert row.state == DeferredReplyState.pending.value
    assert row.chosen_due_at == NOW + timedelta(seconds=45)
    # 生成宽限 60 秒，且**短于**"用户再等一轮"的直觉
    assert row.hard_deadline_at == NOW + timedelta(seconds=45 + 60)
    assert row.applied_delay_ms == 45_000


async def test_clamped_delay_keeps_requested_value(web_env: TestEnv) -> None:  # noqa: F811
    """钳过的记录必须同时留下"她要的"与"实际用的"。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    row = await _schedule(delay_ms=300_000, requested_ms=999_000)
    assert row is not None
    assert row.applied_delay_ms == 300_000
    assert row.requested_delay_ms == 999_000
    assert row.clamped is True


async def test_duplicate_schedule_returns_none_and_keeps_one_row(web_env: TestEnv) -> None:  # noqa: F811
    """同一轮重复登记：返回 `None`，库里仍只有一行。

    这是"重复任务只产生一个轮次与一个投递计划"的第一道闸（第二道是
    `turns.uq_turn_client`）。
    """
    await _make_window()
    await _make_turn("win_def", "turn_a")
    first = await _schedule()
    assert first is not None
    second = await _schedule()
    assert second is None
    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(DeferredReply))
    assert count == 1


# ------------------------------------------------------------------ 认领


async def test_not_claimable_before_due(web_env: TestEnv) -> None:  # noqa: F811
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        claimed = await deferred.claim_due(
            session, lease_owner="w1", now=NOW + timedelta(seconds=44)
        )
    assert claimed is None, "44 秒就认领到 = 她会提前回"


async def test_claimable_at_due(web_env: TestEnv) -> None:  # noqa: F811
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        claimed = await deferred.claim_due(
            session, lease_owner="w1", now=NOW + timedelta(seconds=45)
        )
    assert claimed is not None
    assert claimed.state == DeferredReplyState.claimed.value
    assert claimed.claim_generation == 1
    assert claimed.lease_owner == "w1"


async def test_only_one_of_many_concurrent_claims_wins(web_env: TestEnv) -> None:  # noqa: F811
    """**20 个并发认领恰好 1 个赢**（真 PG、真并发、独立 session）。

    顺序执行两次只有一个赢家是条件更新的必然结果，证明不了并发安全。这里让
    20 个协程各开自己的 `session_scope()` 同时抢同一行——少了这条，"用户只
    收到一条回复"就只是说法。
    """
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    moment = NOW + timedelta(seconds=60)

    async def attempt(index: int) -> bool:
        async with session_scope() as session:
            got = await deferred.claim_due(
                session, lease_owner=f"w{index}", now=moment
            )
            return got is not None

    results = await asyncio.gather(*(attempt(i) for i in range(20)))
    winners = [r for r in results if r]
    assert len(winners) == 1, f"应当恰好 1 个认领成功，实际 {len(winners)}"

    async with session_scope() as session:
        row = await session.scalar(select(DeferredReply))
    assert row is not None
    assert row.claim_generation == 1, "每次成功认领只该 +1，20 次竞争不能加 20"


# ------------------------------------------------------------------ 抢占


async def test_no_preemption_without_new_user_input(web_env: TestEnv) -> None:  # noqa: F811
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _add_user_message("win_def", sequence=1)
    await _schedule(input_sequence=1)
    async with session_scope() as session:
        assert await deferred.input_preempted(
            session, window_id="win_def", input_sequence=1
        ) is False


async def test_new_user_input_preempts(web_env: TestEnv) -> None:  # noqa: F811
    """窗口里出现更晚的**用户**消息 → 抢占。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _add_user_message("win_def", sequence=1)
    await _schedule(input_sequence=1)
    await _add_user_message("win_def", sequence=2, text="我又说话了")
    async with session_scope() as session:
        assert await deferred.input_preempted(
            session, window_id="win_def", input_sequence=1
        ) is True


async def test_actor_message_does_not_preempt(web_env: TestEnv) -> None:  # noqa: F811
    """她自己的回复不算新输入——否则一条延后回复会把自己抢占掉。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _add_user_message("win_def", sequence=1)
    await _schedule(input_sequence=1)
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_actor_2",
                window_id="win_def",
                role="actor",
                text="我先说一句",
                sequence=2,
            )
        )
    async with session_scope() as session:
        assert await deferred.input_preempted(
            session, window_id="win_def", input_sequence=1
        ) is False


async def test_preemption_is_scoped_to_window(web_env: TestEnv) -> None:  # noqa: F811
    """别的窗口来了新输入，不该抢占本窗口的延后回复。"""
    await _make_window("win_def")
    await _make_window("win_other")
    await _make_turn("win_def", "turn_a")
    await _add_user_message("win_def", sequence=1)
    await _schedule(input_sequence=1)
    await _add_user_message("win_other", sequence=5, text="另一个窗口的话")
    async with session_scope() as session:
        assert await deferred.input_preempted(
            session, window_id="win_def", input_sequence=1
        ) is False


# ------------------------------------------------------------------ 租约二分


async def test_expired_lease_before_model_start_requeues(web_env: TestEnv) -> None:  # noqa: F811
    """租约过期、**还没调模型** → 回 `pending`（没花钱，可以重新执行）。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        await deferred.claim_due(session, lease_owner="w1", now=NOW + timedelta(seconds=45))

    async with session_scope() as session:
        counts = await deferred.reclaim_expired_leases(
            session, now=NOW + timedelta(seconds=45 + deferred.CLAIM_LEASE_SECONDS + 1)
        )
    assert counts == {"requeued": 1, "failed": 0}

    async with session_scope() as session:
        row = await session.scalar(select(DeferredReply))
    assert row is not None
    assert row.state == DeferredReplyState.pending.value
    assert row.lease_owner is None
    assert row.reason == "lease_expired_before_model_start"


async def test_expired_lease_after_model_start_fails_without_retry(web_env: TestEnv) -> None:  # noqa: F811
    """租约过期、**已经调过模型** → `failed`，不自动重跑。

    这条与上一条搞反了就是"崩溃后重复烧钱"：模型可能已经收过费，用户也可能
    已经看到过内容。
    """
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        claimed = await deferred.claim_due(
            session, lease_owner="w1", now=NOW + timedelta(seconds=45)
        )
    assert claimed is not None
    async with session_scope() as session:
        ok = await deferred.mark_model_started(
            session,
            reply_id=claimed.id,
            lease_owner="w1",
            claim_generation=claimed.claim_generation,
            now=NOW + timedelta(seconds=46),
        )
    assert ok

    async with session_scope() as session:
        counts = await deferred.reclaim_expired_leases(
            session, now=NOW + timedelta(seconds=45 + deferred.CLAIM_LEASE_SECONDS + 1)
        )
    assert counts == {"requeued": 0, "failed": 1}

    async with session_scope() as session:
        row = await session.scalar(select(DeferredReply))
    assert row is not None
    assert row.state == DeferredReplyState.failed.value
    assert row.reason == "lease_expired_after_model_start"


# ------------------------------------------------------------------ 终结与吸收


async def test_delivered_is_terminal(web_env: TestEnv) -> None:  # noqa: F811
    """已 `delivered` 的记录不能再被改成 `cancelled`——否则"她回了"和"她没回"
    会同时成立。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        claimed = await deferred.claim_due(
            session, lease_owner="w1", now=NOW + timedelta(seconds=45)
        )
    assert claimed is not None
    async with session_scope() as session:
        assert await deferred.finish(
            session,
            reply_id=claimed.id,
            target=DeferredReplyState.delivered,
            delivered_turn_id="turn_new",
            now=NOW + timedelta(seconds=46),
        )

    from ling.domains.deferred.state import InvalidDeferredTransition

    async with session_scope() as session:
        with pytest.raises(InvalidDeferredTransition):
            await deferred.finish(
                session,
                reply_id=claimed.id,
                target=DeferredReplyState.cancelled,
                reason="late_cancel",
            )

    async with session_scope() as session:
        row = await session.scalar(select(DeferredReply))
    assert row is not None
    assert row.state == DeferredReplyState.delivered.value
    assert row.delivered_turn_id == "turn_new"
    assert row.finished_at is not None


async def test_stale_owner_cannot_finish(web_env: TestEnv) -> None:  # noqa: F811
    """租约已经被别人接走时，旧持有者的 `finish` 写不进去（代次栅栏）。"""
    await _make_window()
    await _make_turn("win_def", "turn_a")
    await _schedule(delay_ms=45_000)
    async with session_scope() as session:
        first = await deferred.claim_due(
            session, lease_owner="w1", now=NOW + timedelta(seconds=45)
        )
    assert first is not None
    # 模拟租约过期后被 w2 接手：直接把 owner/generation 改掉
    async with session_scope() as session:
        row = await session.get(DeferredReply, first.id)
        assert row is not None
        row.lease_owner = "w2"
        row.claim_generation = first.claim_generation + 1

    async with session_scope() as session:
        wrote = await deferred.finish(
            session,
            reply_id=first.id,
            target=DeferredReplyState.delivered,
            lease_owner="w1",
            claim_generation=first.claim_generation,
        )
    assert wrote is False, "旧持有者竟然写成功了：会覆盖新持有者的结果"

    async with session_scope() as session:
        row = await session.scalar(select(DeferredReply))
    assert row is not None
    assert row.state == DeferredReplyState.claimed.value, "状态不该被旧持有者改掉"


async def test_hard_deadline_fails_without_claiming_delivery(web_env: TestEnv) -> None:  # noqa: F811
    """到硬截止仍没终结 → `failed`，且**不写** `delivered_turn_id`。

    绝不假装已送达：用户没收到就是没收到。
    """
    await _make_window()
    await _make_turn("win_def", "turn_a")
    row = await _schedule(delay_ms=45_000)
    assert row is not None
    async with session_scope() as session:
        count = await deferred.expire_past_hard_deadline(
            session, now=row.hard_deadline_at + timedelta(seconds=1)
        )
    assert count == 1
    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.failed.value
    assert stored.reason == "hard_deadline_exceeded"
    assert stored.delivered_turn_id is None
    assert stored.fallback_used is False
