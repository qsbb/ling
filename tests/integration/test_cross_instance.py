"""跨实例协调：停止信号与发言租约（用两个独立 store 实例模拟两个进程）。

关键：每个 store 都是**独立对象**，只通过数据库通信——这正是多实例的形态。
如果测试里复用同一个对象，内存状态会掩盖真实问题。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ling.infra.coordination import SpeakLeaseStore, TurnControlStore
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 停止信号


async def test_stop_propagates_between_instances(logged_in: TestEnv) -> None:
    """实例 A 请求停止，实例 B 的轮次能发现。"""
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    instance_a = TurnControlStore(poll_seconds=0.05)
    instance_b = TurnControlStore(poll_seconds=0.05)

    assert await instance_b.is_stop_requested(turn_id) is False
    await instance_a.request_stop(turn_id)
    assert await instance_b.is_stop_requested(turn_id) is True


async def test_stop_request_is_idempotent(logged_in: TestEnv) -> None:
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    control = TurnControlStore()
    await control.request_stop(turn_id)
    await control.request_stop(turn_id)  # 第二次不报错
    assert await control.is_stop_requested(turn_id) is True


async def test_watch_sets_local_token_on_stop(logged_in: TestEnv) -> None:
    """跨实例停止最终体现在本地停止令牌上。"""
    from ling.kernel.turn import StopToken

    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    requester = TurnControlStore(poll_seconds=0.05)
    watcher_store = TurnControlStore(poll_seconds=0.05)
    token = StopToken()

    task = asyncio.create_task(watcher_store.watch(turn_id, token))
    await asyncio.sleep(0.1)
    assert token.stopped is False

    await requester.request_stop(turn_id)
    for _ in range(40):
        await asyncio.sleep(0.05)
        if token.stopped:
            break
    task.cancel()
    assert token.stopped is True, "跨实例停止信号没有到达轮次"


async def test_clear_removes_signal(logged_in: TestEnv) -> None:
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    control = TurnControlStore()
    await control.request_stop(turn_id)
    await control.clear(turn_id)
    assert await control.is_stop_requested(turn_id) is False


# ------------------------------------------------------------------ 发言租约


async def test_only_one_instance_holds_lease(logged_in: TestEnv) -> None:
    """两个实例抢同一 actor：只有一个拿到。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=30)
    b = SpeakLeaseStore(lease_seconds=30)

    first = await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")
    assert first.acquired is True

    second = await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")
    assert second.acquired is False
    assert second.holder_window_id == "win_a"
    assert "排队" in (second.reason or "")


async def test_release_promotes_next_across_instances(logged_in: TestEnv) -> None:
    """实例 A 释放后，实例 B 的排队者被提升。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=30)
    b = SpeakLeaseStore(lease_seconds=30)

    await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")
    await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")

    assert await a.release(actor_id=actor, window_id="win_a", turn_id="t1") is True
    snapshot = await b.snapshot(actor)
    assert snapshot["holder_window_id"] == "win_b"
    assert snapshot["holder_turn_id"] == "t2"


async def test_lease_expires_so_crash_does_not_lock(logged_in: TestEnv) -> None:
    """实例崩溃不释放时，租约到期可被抢占。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=10)
    b = SpeakLeaseStore(lease_seconds=10)

    assert (await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")).acquired
    # 手工把租约改成已过期，模拟崩溃
    from datetime import UTC, datetime, timedelta

    from ling.infra.models import SpeakLeaseRow

    async with session_scope() as session:
        row = await session.get(SpeakLeaseRow, actor)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    got = await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")
    assert got.acquired is True, "租约过期后仍被锁死——实例崩溃会导致永久不可用"


async def test_try_promote_only_for_queue_head(logged_in: TestEnv) -> None:
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=30)
    b = SpeakLeaseStore(lease_seconds=30)
    c = SpeakLeaseStore(lease_seconds=30)

    await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")
    await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")
    await c.acquire(actor_id=actor, window_id="win_c", turn_id="t3")

    # 非队首不能提升
    assert await c.try_promote(actor_id=actor, window_id="win_c", turn_id="t3") is False
    # 释放后 B 成为队首
    await a.release(actor_id=actor, window_id="win_a", turn_id="t1")
    # 此时 B 已被 release 自动提升，C 仍不能抢
    snap = await b.snapshot(actor)
    assert snap["holder_window_id"] == "win_b"


async def test_same_turn_renews(logged_in: TestEnv) -> None:
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w", turn_id="t1")
    again = await store.acquire(actor_id=actor, window_id="w", turn_id="t1")
    assert again.acquired is True


async def test_extend_requires_matching_lease(logged_in: TestEnv) -> None:
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w", turn_id="t1")
    assert await store.extend(actor_id=actor, window_id="w", turn_id="t1") is True
    assert await store.extend(actor_id=actor, window_id="other", turn_id="t1") is False


# ------------------------------------------------------------------ 死锁回归


async def test_leave_removes_from_queue(logged_in: TestEnv) -> None:
    """等超时的轮次必须能离队。

    真实事故：队列堆到 29 条后角色开始不回话。根因是 `release()` 把队列头
    提升为新持有者，但那条轮次早已放弃等待并退出，没人再释放它 ——
    队列只增不减，后面所有轮次被永久压住。
    """
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w1", turn_id="holder")
    queued = await store.acquire(actor_id=actor, window_id="w2", turn_id="waiter")
    assert queued.acquired is False
    assert queued.queue_position == 1

    assert await store.leave(actor_id=actor, window_id="w2", turn_id="waiter") is True
    snap = await store.snapshot(actor)
    assert snap["queue"] == [], f"离队后仍在队列里: {snap}"


async def test_leave_unknown_is_false(logged_in: TestEnv) -> None:
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w1", turn_id="t1")
    assert await store.leave(actor_id=actor, window_id="nope", turn_id="nope") is False


async def test_release_does_not_promote_departed_waiter(logged_in: TestEnv) -> None:
    """核心回归：放弃等待的轮次不该在 release 时被提升为持有者。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w1", turn_id="holder")
    await store.acquire(actor_id=actor, window_id="w2", turn_id="waiter")
    await store.leave(actor_id=actor, window_id="w2", turn_id="waiter")

    await store.release(actor_id=actor, window_id="w1", turn_id="holder")
    snap = await store.snapshot(actor)
    assert snap["holder_window_id"] is None, (
        f"已放弃的轮次被提升为持有者，会造成死锁: {snap}"
    )


async def test_queue_is_capped(logged_in: TestEnv) -> None:
    """队列不能无限增长（无上限时一次死锁就会长期拖垮角色）。"""
    from ling.infra.coordination import MAX_QUEUE_LENGTH

    actor = f"actor_{uuid.uuid4().hex[:8]}"
    store = SpeakLeaseStore(lease_seconds=30)
    await store.acquire(actor_id=actor, window_id="w0", turn_id="holder")
    for i in range(MAX_QUEUE_LENGTH + 10):
        await store.acquire(actor_id=actor, window_id=f"w{i}", turn_id=f"t{i}")
    snap = await store.snapshot(actor)
    assert len(snap["queue"]) <= MAX_QUEUE_LENGTH, (
        f"队列长度 {len(snap['queue'])} 超过上限 {MAX_QUEUE_LENGTH}"
    )
