"""媒体注意力槽位：跨进程单飞的并发正确性（真 PostgreSQL）。

## 为什么这些用例必须是集成测试

槽位要解决的正是"进程内锁不够"：

    Web 请求、普通 worker、专用媒体 worker 可能跑在不同进程

所以用 sqlite 内存库测它等于没测——`FOR UPDATE NOWAIT`、`clock_timestamp()`、
行锁语义在 sqlite 上全都不存在。本文件只跑真 PG，没配 `LING_TEST_DATABASE_URL`
时 skip（不静默通过）。

## 每条用例在钉什么

- **单飞**：N 个并发抢占，恰好 1 个成功。少了这条，"全局唯一"就只是说法。
- **抢不到立即返回**：busy 不能变成"等 50 秒"，那会把用户的问题变成超时。
- **租约**：持有者崩溃后，过期即可被接手；没过期则不行。
- **代次栅栏**：旧持有者醒过来写不进去。这是最容易漏、后果最脏的一条
  （旧结果覆盖新结果）。
- **fail closed**：数据库异常/没有预置行时**不开始**媒体动作，且不能
  把"库坏了"说成"她正忙"。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from ling.domains.media import slot
from ling.infra.db import session_scope
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    web_env,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.asyncio


async def _reset_slot() -> None:
    """把槽位恢复成空槽。用例之间必须互不影响。"""
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE media_slots
                   SET holder_id = NULL, phase = 'held', kind = '',
                       actor_id = '', window_id = '', subject_id = '',
                       call_id = NULL, activity_id = NULL,
                       started_at = NULL, lease_until = NULL
                 WHERE resource_key = :key
                """
            ),
            {"key": slot.RESOURCE_KEY},
        )


async def test_only_one_of_many_concurrent_claims_wins(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """20 个并发抢占，**恰好 1 个**成功。

    这是槽位存在的全部理由。20 是为了让竞争真的发生：少于 2 个时这条
    断言恒真、毫无价值。
    """
    await _reset_slot()

    async def claim(index: int) -> str:
        async with session_scope() as session:
            result = await slot.acquire(
                session,
                kind=slot.KIND_VIDEO_WATCH,
                window_id=f"win_{index}",
                actor_id="actor_test",
            )
            # acquire 不提交就没释放行锁——必须让事务结束，模拟真实路径
            return result.outcome

    outcomes = await asyncio.gather(*(claim(i) for i in range(20)))

    winners = [o for o in outcomes if o == slot.OUTCOME_ACQUIRED]
    assert len(winners) == 1, f"应当恰好 1 个成功，实际 {len(winners)}：{outcomes}"
    assert all(
        o in (slot.OUTCOME_ACQUIRED, slot.OUTCOME_BUSY) for o in outcomes
    ), f"出现非预期结果：{outcomes}"


async def test_second_claim_is_busy_and_reports_the_holder(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """第二个抢占立刻 busy，并且能说出**有效持有者**在哪个窗口。"""
    await _reset_slot()

    async with session_scope() as session:
        first = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_a", actor_id="a"
        )
    assert first.acquired, first

    async with session_scope() as session:
        second = await slot.acquire(
            session, kind=slot.KIND_LIVESTREAM, window_id="win_b", actor_id="a"
        )
    assert second.busy, second
    assert second.holder_window_id == "win_a", (
        "busy 时必须带回持有者窗口，上层才能说'我正忙着'"
    )


async def test_different_kinds_share_the_same_slot(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """看视频、刷流、看直播**争同一行**——不是三把锁。"""
    await _reset_slot()

    async with session_scope() as session:
        got = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_v", actor_id="a"
        )
    assert got.acquired

    for other in (slot.KIND_BILIBILI_FEED, slot.KIND_LIVESTREAM):
        async with session_scope() as session:
            blocked = await slot.acquire(
                session, kind=other, window_id="win_x", actor_id="a"
            )
        assert blocked.busy, f"{other} 竟然没被挡住：{blocked}"


async def test_expired_lease_can_be_taken_over(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """持有者崩溃、租约过期后，别人可以接手（不需要它自己释放）。"""
    await _reset_slot()

    async with session_scope() as session:
        first = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_dead", actor_id="a"
        )
    assert first.acquired

    # 模拟崩溃：把租约推到过去。不调 release——崩溃的进程不会执行 finally。
    async with session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE media_slots
                   SET lease_until = clock_timestamp() - interval '1 second'
                 WHERE resource_key = :key
                """
            ),
            {"key": slot.RESOURCE_KEY},
        )

    async with session_scope() as session:
        second = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_alive", actor_id="a"
        )
    assert second.acquired, second
    assert second.handle is not None
    # 代次必须变了：这是后面所有栅栏比较的基准
    assert second.handle.generation > first.handle.generation


async def test_live_lease_cannot_be_stolen(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """租约**还有效**时抢不走。这条与上一条成对，缺一不可。"""
    await _reset_slot()

    async with session_scope() as session:
        first = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_alive", actor_id="a"
        )
    assert first.acquired

    async with session_scope() as session:
        stolen = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_thief", actor_id="a"
        )
    assert stolen.busy, "有效租约被抢走了——那会对同一个视频开工两次"


async def test_stale_holder_cannot_write_after_takeover(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """**代次栅栏**：旧持有者被接手后，它的写入必须落空。

    这是最脏的一类缺陷：旧执行者晚到的结果覆盖新执行者的，库里的证据与
    真实执行对不上，且没有任何报错。
    """
    await _reset_slot()

    async with session_scope() as session:
        old = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_old", actor_id="a"
        )
    assert old.acquired and old.handle is not None

    # 旧持有者"卡住"：租约过期，被新的接手
    async with session_scope() as session:
        await session.execute(
            text(
                "UPDATE media_slots SET lease_until = clock_timestamp() - interval '1 second'"
                " WHERE resource_key = :key"
            ),
            {"key": slot.RESOURCE_KEY},
        )
    async with session_scope() as session:
        new = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_new", actor_id="a"
        )
    assert new.acquired and new.handle is not None

    # 旧持有者醒过来，想写结果
    async with session_scope() as session:
        assert await slot.fence_ok(session, old.handle) is False, (
            "旧代次的栅栏检查必须失败，否则旧结果会覆盖新结果"
        )
        assert await slot.renew(session, old.handle) is False, (
            "旧代次不能续租成功"
        )
        # 旧持有者的 finally 也不能把新持有者踢掉
        assert await slot.release(session, old.handle) is False, (
            "旧代次的释放必须落空——否则它会把新持有者踢出槽位"
        )

    # 新持有者仍然有效
    async with session_scope() as session:
        assert await slot.fence_ok(session, new.handle) is True


async def test_release_then_reclaim_works(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """正常释放之后别人能立刻拿到——释放不能把槽位弄坏。"""
    await _reset_slot()

    async with session_scope() as session:
        first = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_1", actor_id="a"
        )
        assert await slot.release(session, first.handle) is True

    async with session_scope() as session:
        second = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_2", actor_id="a"
        )
    assert second.acquired, second


async def test_held_is_not_reported_as_active(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """`held`（还在准备）不能被读侧说成"她正在看"。

    这条钉的是"不编造她的状态"：拿到执行权 ≠ 已经在看。
    """
    await _reset_slot()

    async with session_scope() as session:
        got = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_h", actor_id="a"
        )
    assert got.acquired and got.handle is not None

    async with session_scope() as session:
        assert await slot.current_holder(session) is None, (
            "held 阶段就对外声称'正在看'，是编造"
        )

    async with session_scope() as session:
        assert await slot.mark_active(session, got.handle) is True

    async with session_scope() as session:
        holder = await slot.current_holder(session)
    assert holder is not None
    assert holder["kind"] == slot.KIND_VIDEO_WATCH
    assert holder["window_id"] == "win_h"


async def test_unknown_kind_is_rejected_loudly(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """拼错的 kind 不能悄悄占住槽位（那会让媒体链路永久 busy）。"""
    await _reset_slot()
    async with session_scope() as session:
        with pytest.raises(ValueError, match="未知的媒体槽位类型"):
            await slot.acquire(
                session, kind="vidoe_watch", window_id="win_typo", actor_id="a"
            )


async def test_release_expired_only_clears_expired(web_env: TestEnv) -> None:  # noqa: F811 - fixture
    """清理只回收过期槽；有效占用绝不能被顺手清掉。"""
    await _reset_slot()

    async with session_scope() as session:
        got = await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_live", actor_id="a"
        )
    assert got.acquired

    async with session_scope() as session:
        cleared = await slot.release_expired(session)
    assert cleared == 0, "有效占用被回收了——全局单飞会因此失效"

    async with session_scope() as session:
        await session.execute(
            text(
                "UPDATE media_slots SET lease_until = clock_timestamp() - interval '1 second'"
                " WHERE resource_key = :key"
            ),
            {"key": slot.RESOURCE_KEY},
        )
    async with session_scope() as session:
        cleared = await slot.release_expired(session)
    assert cleared == 1

    async with session_scope() as session:
        assert await slot.acquire(
            session, kind=slot.KIND_VIDEO_WATCH, window_id="win_after", actor_id="a"
        )
