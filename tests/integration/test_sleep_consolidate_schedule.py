"""睡眠期整理**何时**被投递：早于到点不投、到点投一次、一晚只投一次。

## 为什么单独测调度

`consolidate_night()` 只管"这一晚怎么整理"，不管"什么时候该整理"。真正容易
出错的是调度：

- 投早了 → 睡前那几句话还没落库，整理漏掉它们；
- 投晚了（比如窗口结束后才补） → 跟她醒来后的第一轮抢写；
- 一晚投多次 → 同一批记忆被并两遍，`rows_archived` 计数失真。

这三件事都不会在 `consolidate_night()` 的用例里被发现，所以单独测。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.dream.service import NightWindow
from ling.infra.db import session_scope
from ling.infra.models import MemoryConsolidationRow
from ling.worker import (
    CONSOLIDATION_CATCHUP_HOURS,
    CONSOLIDATION_DELAY_MINUTES,
    _schedule_consolidation,
)
from tests.integration.conftest import TestEnv, logged_in, web_env  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

SLEEP_DATE = date(2026, 11, 9)
BED_AT = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)  # 本地 23:00（+08）
WAKE_AT = BED_AT + timedelta(hours=8)


def _window() -> NightWindow:
    return NightWindow(
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
        wake_at=WAKE_AT,
        timezone="Asia/Shanghai",
        routine_revision=1,
    )


async def _clean(actor_id: str, subject_id: str) -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(MemoryConsolidationRow).where(
                MemoryConsolidationRow.actor_id == actor_id,
                MemoryConsolidationRow.subject_id == subject_id,
            )
        )


async def test_not_enqueued_before_the_delay(logged_in: TestEnv) -> None:  # noqa: F811
    """入睡后还不满 30 分钟 → 不投。

    留这 30 分钟是给"睡前那几句话"落库。投早了整理就会漏掉它们。
    """
    await _clean(logged_in.settings.actor_id, logged_in.subject_id)
    too_early = BED_AT + timedelta(minutes=CONSOLIDATION_DELAY_MINUTES - 1)
    async with session_scope() as session:
        enqueued = await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=too_early,
        )
    assert enqueued is False


async def test_enqueued_once_after_the_delay(logged_in: TestEnv) -> None:  # noqa: F811
    """入睡后满 30 分钟 → 投一次；再扫一次不重复投。"""
    await _clean(logged_in.settings.actor_id, logged_in.subject_id)
    due = BED_AT + timedelta(minutes=CONSOLIDATION_DELAY_MINUTES + 1)

    async with session_scope() as session:
        first = await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=due,
        )
    assert first is True

    async with session_scope() as session:
        second = await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=due + timedelta(minutes=5),
        )
    assert second is False, "同一晚投了两次——记忆会被并两遍"


async def test_not_enqueued_when_already_consolidated(logged_in: TestEnv) -> None:  # noqa: F811
    """这一晚已经有整理记录 → 不再投（含 skipped/failed）。"""
    await _clean(logged_in.settings.actor_id, logged_in.subject_id)
    async with session_scope() as session:
        session.add(
            MemoryConsolidationRow(
                id="cons_probe",
                actor_id=logged_in.settings.actor_id,
                subject_id=logged_in.subject_id,
                sleep_date=SLEEP_DATE,
                state="skipped",
                reason="已整理过",
                created_at=datetime.now(UTC),
            )
        )
    async with session_scope() as session:
        enqueued = await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=BED_AT + timedelta(minutes=CONSOLIDATION_DELAY_MINUTES + 1),
        )
    assert enqueued is False


async def test_no_catchup_long_after_the_window(logged_in: TestEnv) -> None:  # noqa: F811
    """窗口过去太久 → 不补投。

    停机一整天后恢复时，对同一批记忆做一次"补课式"归档没有收益，
    用户看起来还莫名其妙。
    """
    await _clean(logged_in.settings.actor_id, logged_in.subject_id)
    late = WAKE_AT + timedelta(hours=CONSOLIDATION_CATCHUP_HOURS + 1)
    async with session_scope() as session:
        enqueued = await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=late,
        )
    assert enqueued is False


async def test_outbox_carries_the_topic(logged_in: TestEnv) -> None:  # noqa: F811
    """投出去的事件 topic 必须是 `memory.consolidate`，且带主体与 sleep_date。

    拼错 topic 不会报错——它会安静地躺在 outbox 里没人消费，而"每晚整理"
    这个功能等于不存在。
    """
    from ling.infra.models import OutboxEvent

    await _clean(logged_in.settings.actor_id, logged_in.subject_id)
    async with session_scope() as session:
        await _schedule_consolidation(
            session,
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window=_window(),
            now=BED_AT + timedelta(minutes=CONSOLIDATION_DELAY_MINUTES + 1),
        )
    async with session_scope() as session:
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "memory.consolidate")
            )
        ).scalar_one_or_none()
    assert row is not None, "没有投出 memory.consolidate 事件"
    assert row.payload.get("subject_id") == logged_in.subject_id
    assert row.payload.get("sleep_date") == SLEEP_DATE.isoformat()
