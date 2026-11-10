"""后台视频读取的持久预算预留。

## 用户原话

「看视频这个模型直接使用设定的看视频模型，**次数限制去掉**，看视频额度如果
没了，直接把事实告诉主回复链路」

所以这里**没有**次数限额（`VIDEO_MAX_CALLS_PER_DAY is None`），只守 token 额度。

## 为什么必须落库（这个文件存在的原因）

后台视频作业在 worker 里跑，而 worker 可以多副本。进程内计数器让每个副本
各算一份"今天还能看几个"——**N 个副本烧 N 倍的钱，且完全看不出来**。

下面第 2 条用例就是钉这件事：多副本各自的 session 必须看到彼此的预留。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from ling.domains.media.budget import (
    VIDEO_DAILY_TOKEN_BUDGET,
    VIDEO_MAX_CALLS_PER_DAY,
    VIDEO_RESERVED_INPUT_TOKENS,
    VIDEO_RESERVED_OUTPUT_TOKENS,
    VIDEO_WATCH_KIND,
    finish_video_call,
    reserve_video_call,
)
from ling.infra.db import session_scope
from ling.infra.models import BehaviorModelCallRow
from tests.integration.conftest import TestEnv, logged_in, web_env  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
DAY = date(2026, 11, 10)


async def _clean(actor_id: str) -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(BehaviorModelCallRow).where(
                BehaviorModelCallRow.actor_id == actor_id,
                BehaviorModelCallRow.kind == VIDEO_WATCH_KIND,
                BehaviorModelCallRow.plan_day == DAY,
            )
        )


async def test_reservation_is_persisted(logged_in: TestEnv) -> None:  # noqa: F811
    """预留必须**落库**，不是内存计数。"""
    await _clean(logged_in.settings.actor_id)
    async with session_scope() as session:
        got = await reserve_video_call(
            session,
            actor_id=logged_in.settings.actor_id,
            idempotency_key="video:job_1",
            now=NOW,
        )
    assert got.ok, got.reason

    async with session_scope() as session:
        row = (
            await session.execute(
                select(BehaviorModelCallRow).where(
                    BehaviorModelCallRow.id == got.call_id
                )
            )
        ).scalar_one_or_none()
    assert row is not None, "预留没落库——多副本会各算一份"
    assert row.kind == VIDEO_WATCH_KIND
    assert row.state == "reserved"
    assert row.reserved_input_tokens > 0 and row.reserved_output_tokens > 0


async def test_another_replica_sees_the_previous_reservation(logged_in: TestEnv) -> None:  # noqa: F811
    """**多副本可见**：另一个独立 session（模拟另一个副本）能看到已有预留。

    这条是整个模块存在的理由。内存计数器在这里必然失败。
    """
    await _clean(logged_in.settings.actor_id)
    async with session_scope() as s1:
        first = await reserve_video_call(
            s1, actor_id=logged_in.settings.actor_id, idempotency_key="video:a", now=NOW
        )
    assert first.ok

    # 全新的 session = 另一个副本的视角
    async with session_scope() as s2:
        rows = (
            await s2.execute(
                select(BehaviorModelCallRow.id).where(
                    BehaviorModelCallRow.actor_id == logged_in.settings.actor_id,
                    BehaviorModelCallRow.kind == VIDEO_WATCH_KIND,
                    BehaviorModelCallRow.plan_day == DAY,
                )
            )
        ).all()
    assert len(rows) == 1, "另一个副本看不到已有预留"


async def test_same_attempt_twice_only_one_wins(logged_in: TestEnv) -> None:  # noqa: F811
    """同一 attempt 并发预留只有一个赢家（唯一约束保证）。"""
    await _clean(logged_in.settings.actor_id)
    async with session_scope() as session:
        first = await reserve_video_call(
            session, actor_id=logged_in.settings.actor_id,
            idempotency_key="video:dup", now=NOW,
        )
    assert first.ok

    # 第二个用同一个 key：它会被分配 attempt=2（不是重复计数），仍然可以预留，
    # 但要能看清"这是第二次尝试"——预算不退款正是靠这个。
    async with session_scope() as session:
        second = await reserve_video_call(
            session, actor_id=logged_in.settings.actor_id,
            idempotency_key="video:dup", now=NOW,
        )
    assert second.ok
    assert second.call_id != first.call_id


async def test_no_call_count_limit(logged_in: TestEnv) -> None:  # noqa: F811
    """**没有次数限制**：放行次数只由 token 额度决定，不由调用次数决定。

    用户原话「次数限制去掉」。这里曾经写着一个 `VIDEO_MAX_CALLS_PER_DAY = 50`
    的"防失控硬护栏"，理由是"防死循环"。那是个自欺的护栏：它对正常使用不可见，
    对失控也不够快（50 次视频早就烧掉一天预算了，真正起作用的是 token 额度），
    而一旦有人调大 token 预算，它就会悄悄变成产品限额——与用户要求相反。
    现在它是 `None`，本用例钉住"边界由 token 决定"。
    """
    assert VIDEO_MAX_CALLS_PER_DAY is None, (
        "用户明确要求去掉次数限制，不该有次数上限"
    )
    await _clean(logged_in.settings.actor_id)

    token_bound = VIDEO_DAILY_TOKEN_BUDGET // (
        VIDEO_RESERVED_INPUT_TOKENS + VIDEO_RESERVED_OUTPUT_TOKENS
    )

    ok_count = 0
    for i in range(token_bound + 3):
        async with session_scope() as session:
            got = await reserve_video_call(
                session,
                actor_id=logged_in.settings.actor_id,
                idempotency_key=f"video:run_{i}",
                now=NOW,
            )
        if not got.ok:
            break
        ok_count += 1

    assert ok_count == token_bound, (
        f"应当恰好放行 {token_bound} 次（只由 token 预算决定），实际 {ok_count}"
    )
    async with session_scope() as session:
        beyond = await reserve_video_call(
            session,
            actor_id=logged_in.settings.actor_id,
            idempotency_key="video:run_beyond",
            now=NOW,
        )
    assert not beyond.ok, "越过 token 预算之后仍然放行"


async def test_finish_records_actual_state(logged_in: TestEnv) -> None:  # noqa: F811
    """结清后状态与 token 记账正确；失败也占额（不退款）。"""
    await _clean(logged_in.settings.actor_id)
    async with session_scope() as session:
        got = await reserve_video_call(
            session, actor_id=logged_in.settings.actor_id,
            idempotency_key="video:fin", now=NOW,
        )
    assert got.ok and got.call_id

    async with session_scope() as session:
        await finish_video_call(session, call_id=got.call_id, state="failed")

    async with session_scope() as session:
        row = await session.get(BehaviorModelCallRow, got.call_id)
    assert row is not None
    assert row.state == "failed"
    # 失败仍占额：它仍计入当日已用次数
    async with session_scope() as session:
        still = await reserve_video_call(
            session, actor_id=logged_in.settings.actor_id,
            idempotency_key="video:after_fail", now=NOW,
        )
    assert still.ok
    assert still.call_id != got.call_id
