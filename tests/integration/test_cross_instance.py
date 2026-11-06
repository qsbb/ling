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


# ------------------------------------------------- 停止信号按 attempt 隔离
#
# 缺陷（实测复现）：信号只绑 turn_id，而 retry 复用同一 turn_id 只把 attempt
# 加一 → 上一轮的标记把**每一次**重试出来的新一轮立刻停掉（0.5s 轮询间隔下
# 新一轮首次检查就命中）。下面几条用两个独立 store 实例（=两个进程）验证
# 修复在跨实例语义下同样成立。


async def test_stop_signal_is_scoped_to_attempt(logged_in: TestEnv) -> None:
    """同一个 turn_id 的旧 attempt 标记，不得命中新 attempt。"""
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    stopper = TurnControlStore(poll_seconds=0.05)
    runner = TurnControlStore(poll_seconds=0.05)

    await stopper.request_stop(turn_id, 1)
    assert await runner.is_stop_requested(turn_id, 1) is True
    assert await runner.is_stop_requested(turn_id, 2) is False, (
        "旧 attempt 的停止标记泄漏到了新一轮（重试会被立刻停掉）"
    )


async def test_watch_scoped_to_attempt_does_not_fire_on_old_marker(
    logged_in: TestEnv,
) -> None:
    """跨实例 watcher：新一轮（attempt=2）不会被 attempt=1 的旧标记停掉。"""
    from ling.kernel.turn import StopToken

    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    stopper = TurnControlStore(poll_seconds=0.05)
    runner = TurnControlStore(poll_seconds=0.05)

    await stopper.request_stop(turn_id, 1)  # 上一轮留下的标记
    token = StopToken()
    task = asyncio.create_task(runner.watch(turn_id, token, 2))
    try:
        # 远多于一个轮询周期：旧实现下这里必然变成 True
        for _ in range(12):
            await asyncio.sleep(0.05)
            assert token.stopped is False, "新一轮被旧 attempt 的标记停掉了"
    finally:
        task.cancel()


async def test_old_attempt_still_stoppable_while_new_attempt_runs(
    logged_in: TestEnv,
) -> None:
    """「stop → 立即 retry（旧轮次未终结）」：新旧 attempt 互不干扰。

    旧轮次仍在轮询自己的 attempt，必须**照样停得下来**（所以不能靠
    "retry 前清标记"来修：那会让旧轮次永远停不下来）。
    """
    from ling.kernel.turn import StopToken

    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    stopper = TurnControlStore(poll_seconds=0.05)
    old_runner = TurnControlStore(poll_seconds=0.05)
    new_runner = TurnControlStore(poll_seconds=0.05)

    old_token = StopToken()
    new_token = StopToken()
    old_task = asyncio.create_task(old_runner.watch(turn_id, old_token, 1))
    new_task = asyncio.create_task(new_runner.watch(turn_id, new_token, 2))

    await stopper.request_stop(turn_id, 1)  # 用户点了停止

    for _ in range(40):
        await asyncio.sleep(0.05)
        if old_token.stopped:
            break
    old_task.cancel()
    assert old_token.stopped is True, "旧轮次停不下来"

    # 新一轮全程不受影响
    await asyncio.sleep(0.2)
    new_task.cancel()
    assert new_token.stopped is False, "旧轮次的停止信号污染了新一轮"


async def test_clear_one_attempt_keeps_others(logged_in: TestEnv) -> None:
    """终结清理必须只清自己那一行，否则并发中的旧轮次会失去停止信号。"""
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    control = TurnControlStore()
    await control.request_stop(turn_id, 1)
    await control.request_stop(turn_id, 2)

    await control.clear(turn_id, 1)
    assert await control.is_stop_requested(turn_id, 1) is False
    assert await control.is_stop_requested(turn_id, 2) is True, (
        "清一个 attempt 把别的 attempt 也清掉了"
    )


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


# ------------------------------------------- B1：队首不能抢走**仍然有效**的租约
#
# 缺陷（实测复现）：`try_promote()` 只要 `queue[0] == key` 就无条件改写
# `window_id/turn_id/expires_at`，完全不看现持有者的租约是否还有效。
# 而 `release()` 本来就会把队首提升为持有者，所以「我是队首 + 别人仍有效
# 持有」这个状态**必然出现**：
#
#   A 持有并流式输出中 → B 首次 delta 到 → `_wait_for_floor` 第一次迭代
#   就抢成功（0.25s 内）→ 两个窗口同时外发。
#
# 此后 A 的 `extend()` rowcount=0，`release()` 也不匹配 —— A 的租约没有任何
# 回收路径，只能等 180s 过期兜底。


async def test_queue_head_cannot_steal_live_lease(logged_in: TestEnv) -> None:
    """A 持有 → B 排队 → **B 抢不走**，且 A 仍能续约。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=180)
    b = SpeakLeaseStore(lease_seconds=180)

    assert (await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")).acquired
    queued = await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")
    assert queued.acquired is False
    assert queued.queue_position == 1, "B 应该是队首"

    # B 是队首，但 A 的租约仍然有效 —— 必须抢不走
    assert await b.try_promote(actor_id=actor, window_id="win_b", turn_id="t2") is False, (
        "队首抢走了仍然有效的租约：两个窗口会同时外发"
    )
    snap = await b.snapshot(actor)
    assert snap["holder_window_id"] == "win_a"
    assert snap["holder_turn_id"] == "t1"

    # A 的租约没被改写：续约必须成功（原实现里这一步 rowcount=0）
    assert await a.extend(actor_id=actor, window_id="win_a", turn_id="t1") is True, (
        "A 的租约被队首改写后无法续约，等于没有任何回收路径"
    )


async def test_expired_lease_is_still_takeable_by_queue_head(logged_in: TestEnv) -> None:
    """A 的租约**过期**后，队首 B 仍能正常接手（崩溃恢复路径不能坏）。"""
    from datetime import UTC, datetime, timedelta

    from ling.infra.models import SpeakLeaseRow

    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=180)
    b = SpeakLeaseStore(lease_seconds=180)

    await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")
    await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")

    # 模拟 A 崩溃：租约到期
    async with session_scope() as session:
        row = await session.get(SpeakLeaseRow, actor)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await b.try_promote(actor_id=actor, window_id="win_b", turn_id="t2") is True, (
        "租约过期后队首抢不到，崩溃恢复路径被破坏"
    )
    snap = await b.snapshot(actor)
    assert snap["holder_window_id"] == "win_b"
    assert snap["holder_turn_id"] == "t2"
    assert snap["queue"] == [], f"接手后应把自己移出队列: {snap}"


async def test_promoted_waiter_can_confirm_its_floor(logged_in: TestEnv) -> None:
    """`release()` 把队首提升为持有者后，**它自己必须能确认这个租约**。

    这条是上面那条守卫的配套：守卫按「已经是持有者」放行，若先判队首就会
    在提升后一直返回 False —— 排队者永远说不了话。
    """
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    a = SpeakLeaseStore(lease_seconds=180)
    b = SpeakLeaseStore(lease_seconds=180)

    await a.acquire(actor_id=actor, window_id="win_a", turn_id="t1")
    await b.acquire(actor_id=actor, window_id="win_b", turn_id="t2")
    assert await a.release(actor_id=actor, window_id="win_a", turn_id="t1") is True

    snap = await b.snapshot(actor)
    assert snap["holder_window_id"] == "win_b"
    assert await b.try_promote(actor_id=actor, window_id="win_b", turn_id="t2") is True, (
        "被 release() 提升为持有者后确认失败：排队者永远等不到发言权"
    )


# ------------------------------- B1（续）：TurnCoordinator 层不得"抢到就外发"
#
# 存储层的守卫只管住"队首能不能抢"；还有两件事必须一起成立，否则缺陷照样显形：
#   1. A 持有期间，B 的轮次**一个 delta 都不能外发**（否则两窗口同时外发）；
#   2. A 的 `extend()` 返回 False 时，A **不能再继续外发**（原实现忽略返回值，
#      于是 A 一边"失去"租约、一边继续吐 delta，且 `release()` 也失败 ——
#      这条租约没有任何回收路径）。


def _fast_config():
    from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig

    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(
                provider_id="fake", kind="fake", enabled=True,
                extra={"chunk_delay": 0.01},
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="fake", model_id="deterministic",
            )
        },
        test_mode=True,
    )


async def _new_window(client, title: str) -> str:
    return (await client.post("/v1/sessions", json={"title": title})).json()["session_id"]


async def _make_turn(window_id: str, text: str) -> str:
    from ling.domains.conversation.service import ConversationService

    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=text, turn_id=turn.id)
        return turn.id


async def _drain(queue) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            return events
        events.append((item.event, item.data))


def _coordinator(env, lease_store):
    from ling.kernel.turn import TurnCoordinator
    from ling.models.registry import ProviderRegistry
    from ling.models.router import ModelRouter

    return TurnCoordinator(
        router=ModelRouter(ProviderRegistry(lambda _pid: None)),
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        speak_lease=lease_store,
    )


async def test_holder_keeps_floor_while_waiter_streams(logged_in: TestEnv) -> None:
    """A 持有租约、B 的轮次全程跑完：B 不能抢走，也不能外发任何 delta。"""
    actor = f"actor_{uuid.uuid4().hex[:8]}"
    holder = SpeakLeaseStore(lease_seconds=180)
    waiter = SpeakLeaseStore(lease_seconds=180)
    win_a = await _new_window(logged_in.client, "A-hold")
    win_b = await _new_window(logged_in.client, "B-wait")

    assert (
        await holder.acquire(actor_id=actor, window_id=win_a, turn_id="manual")
    ).acquired

    coordinator = _coordinator(logged_in, waiter)
    coordinator._actor_id = actor  # noqa: SLF001 - 让两边用同一个 actor
    turn_id = await _make_turn(win_b, "B 说话")
    queue = await coordinator.run_turn(
        window_id=win_b,
        subject_id=logged_in.subject_id,
        turn_id=turn_id,
        user_text="B 说话",
        config=_fast_config(),
    )
    events = await _drain(queue)
    names = [n for n, _ in events]

    assert "delta" not in names, "A 仍持有有效租约，B 却对外发了 delta（两个窗口同时说话）"
    completed = [d for n, d in events if n == "turn.completed"]
    assert completed
    assert completed[0]["decision"] == "deferred_by_floor"
    assert completed[0]["message"] is None

    # A 的租约仍然完好：还能续约，也还能正常释放
    assert await holder.extend(actor_id=actor, window_id=win_a, turn_id="manual") is True
    assert await holder.release(actor_id=actor, window_id=win_a, turn_id="manual") is True
    # B 已经离队（没等满 8 秒也不能把队列项留成死锁）
    snap = await waiter.snapshot(actor)
    assert snap["holder_window_id"] is None, f"释放后仍有人持有: {snap}"
    assert snap["queue"] == [], f"放弃等待的轮次仍留在队列里（死锁）: {snap}"


class _ExtendFailsAfter:
    """租约替身：前 ``ok_times`` 次续约成功，之后一律失败。

    模拟"租约被另一个窗口合法接管/过期被接手"：`extend()` 的 WHERE 不再命中。
    """

    def __init__(self, *, ok_times: int) -> None:
        self._ok = ok_times
        self.extends = 0
        self.releases = 0
        self.leaves = 0

    async def acquire(self, *, actor_id: str, window_id: str, turn_id: str):
        from ling.infra.coordination import LeaseResult

        return LeaseResult(acquired=True, holder_window_id=window_id, holder_turn_id=turn_id)

    async def try_promote(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        return True

    async def extend(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        self.extends += 1
        return self.extends <= self._ok

    async def release(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        self.releases += 1
        return False

    async def leave(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        self.leaves += 1
        return True


async def test_turn_stops_emitting_after_extend_fails(
    logged_in: TestEnv, caplog: pytest.LogCaptureFixture
) -> None:
    """续约失败后必须**立刻停止对外输出**，而不是继续装成还持有。

    原实现忽略 `extend()` 的返回值：租约已经不属于自己了，delta 照发、
    `held_by_me` 仍为 True，前端看到两个窗口同时在说话；而 `release()` 又因为
    window/turn 不匹配而失败 —— 这条租约没有任何回收路径，只能等 180s 过期。
    """
    lease = _ExtendFailsAfter(ok_times=1)
    win = await _new_window(logged_in.client, "extend-fails")
    coordinator = _coordinator(logged_in, lease)
    turn_id = await _make_turn(win, "续约会失败")
    queue = await coordinator.run_turn(
        window_id=win,
        subject_id=logged_in.subject_id,
        turn_id=turn_id,
        user_text="续约会失败",
        config=_fast_config(),
    )
    events = await _drain(queue)

    deltas = [d for n, d in events if n == "delta"]
    assert len(deltas) <= 1, (
        f"续约失败后仍继续外发（{len(deltas)} 个 delta）：租约已不属于自己却还在说"
    )
    assert lease.extends >= 2, "续约根本没被调用（这个测试没有覆盖到缺陷点）"
    completed = [d for n, d in events if n == "turn.completed"]
    assert completed
    assert completed[0]["decision"] == "deferred_by_floor", (
        "中途失去发言权却按正常回复交付"
    )
    assert completed[0]["suppressed_reason"] and "失去发言权" in completed[0]["suppressed_reason"]
    # 失去租约必须**留下可查的痕迹**（不能静默地"少说一半"）
    assert any(
        "续约失败" in r.getMessage() for r in caplog.records
    ), "续约失败没有任何日志：排查'两个窗口同时说话'时看不到线索"
    # 收尾必须把自己从队列里摘掉，否则 release() 会把已放弃的轮次提升为持有者
    assert lease.leaves >= 1, "失去租约后没有清理自己的排队项（死锁风险）"
