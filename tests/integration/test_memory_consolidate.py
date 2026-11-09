"""睡眠期记忆整理：一晚一次、确定性、零模型。

## 用户原话

「睡眠的时候应该要对记忆进行压缩或者什么样的」

## 这些用例在钉什么

- **一晚一次**：重复投递不会把同一批记忆并两遍（归档虽幂等，但 `rows_archived`
  这个计数会失真——那是用户唯一能看到的"昨晚发生了什么"）。
- **`skipped` 不是成功**：最常见的情况是"到了预计入睡时间，这晚没有可整理的
  东西"。记成 `ok` 会让"整理了几条"这个数字失去意义。
- **零模型**：第一版刻意不调模型。模型写的摘要一旦入库，就会取代原始记忆
  参与召回——那等于让模型编的一句话变成她记得的事。
- **失败不拖垮调度**：整理出错要落 `failed` 记录（用户有权知道"昨晚没整理
  完"），而不是抛出去把睡眠调度整条链路卡住。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from ling.domains.memory.consolidate import (
    STATE_FAILED,
    STATE_OK,
    STATE_SKIPPED,
    consolidate_night,
    recent_consolidations,
)
from ling.infra.db import session_scope
from ling.infra.models import MemoryConsolidationRow, MemoryRow, utcnow
from tests.integration.conftest import (  # noqa: F401 - fixtures
    TestEnv,
    logged_in,
    web_env,
)

pytestmark = pytest.mark.asyncio

SLEEP_DATE = date(2026, 11, 9)


async def _clean(actor_id: str, subject_id: str) -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(MemoryConsolidationRow).where(
                MemoryConsolidationRow.actor_id == actor_id,
                MemoryConsolidationRow.subject_id == subject_id,
            )
        )


async def _add_low_value_memories(
    *, actor_id: str, subject_id: str, count: int, age_days: int = 240
) -> None:
    """造一批"够老、低价值、同槽位、同受众边界"的记忆。

    必须让受众边界的每一维都相同（主体/可见范围/来源窗口/origin），
    否则它们不会被分到一组——那正是压缩的分组键，也是隐私边界。
    """
    from ling.infra.security import new_id

    old = utcnow() - timedelta(days=age_days)
    async with session_scope() as session:
        for i in range(count):
            session.add(
                MemoryRow(
                    id=new_id("mem"),
                    subject_id=subject_id,
                    kind="assertion",
                    text=f"用户喜欢第 {i} 种茶",
                    slot="preference",
                    value=f"第 {i} 种茶",
                    origin="user_statement",
                    visibility="subject_private",
                    confidence=0.3,
                    # 压缩的分组键含来源窗口：同组必须这一列相同，
                    # 否则会被拆成不同的组（那正是隐私边界）。
                    source_window_id="win_seed",
                    created_at=old,
                    updated_at=old,
                )
            )


async def test_night_with_nothing_to_merge_is_skipped_not_ok(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """没有可整理的东西时记 `skipped`，不记 `ok`。

    这条钉的是"数字要有意义"：如果空跑也记 ok，那"她整理过 N 晚"就变成了
    "调度器还活着"，用户读不出任何关于记忆的信息。
    """
    actor_id = logged_in.settings.actor_id
    subject_id = logged_in.subject_id
    await _clean(actor_id, subject_id)

    async with session_scope() as session:
        outcome = await consolidate_night(
            session,
            actor_id=actor_id,
            subject_id=subject_id,
            sleep_date=SLEEP_DATE,
        )
    assert outcome.state == STATE_SKIPPED, outcome
    assert "预计入睡" in outcome.reason, "reason 必须说清是预计入睡，不能暗示她真的睡了"
    assert outcome.groups_merged == 0 and outcome.rows_archived == 0


async def test_merging_low_value_memories_is_recorded(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """真的合并了 → `ok` + 计数，并且计数与库里实际归档数一致。"""
    actor_id = logged_in.settings.actor_id
    subject_id = logged_in.subject_id
    await _clean(actor_id, subject_id)
    await _add_low_value_memories(actor_id=actor_id, subject_id=subject_id, count=3)

    async with session_scope() as session:
        outcome = await consolidate_night(
            session,
            actor_id=actor_id,
            subject_id=subject_id,
            sleep_date=SLEEP_DATE,
        )
    assert outcome.state == STATE_OK, outcome
    assert outcome.groups_merged >= 1
    assert outcome.rows_archived >= 3
    assert "合并" in outcome.reason and "归档" in outcome.reason

    # 库里确实有归档（不是只写了个好看的计数）
    async with session_scope() as session:
        archived = (
            await session.execute(
                select(MemoryRow.id).where(
                    MemoryRow.subject_id == subject_id,
                    MemoryRow.archived_at.is_not(None),
                )
            )
        ).all()
    assert len(archived) >= 3, "记录说归档了，但库里没有归档行"


async def test_second_call_same_night_is_idempotent(logged_in: TestEnv) -> None:  # noqa: F811
    """同一晚第二次调用：不重复整理，返回已有结果。

    没有这条，两个 worker 同时投递就会把同一批记忆并两遍，且两次计数相加
    会给出一个从未发生过的"归档了 6 条"。
    """
    actor_id = logged_in.settings.actor_id
    subject_id = logged_in.subject_id
    await _clean(actor_id, subject_id)
    await _add_low_value_memories(actor_id=actor_id, subject_id=subject_id, count=3)

    async with session_scope() as session:
        first = await consolidate_night(
            session, actor_id=actor_id, subject_id=subject_id, sleep_date=SLEEP_DATE
        )
    async with session_scope() as session:
        second = await consolidate_night(
            session, actor_id=actor_id, subject_id=subject_id, sleep_date=SLEEP_DATE
        )

    assert second.already_done is True
    assert second.state == first.state
    assert second.rows_archived == first.rows_archived, (
        "第二次调用改变了归档计数——说明它又跑了一遍"
    )

    # 一晚只有一行记录
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MemoryConsolidationRow).where(
                    MemoryConsolidationRow.actor_id == actor_id,
                    MemoryConsolidationRow.subject_id == subject_id,
                    MemoryConsolidationRow.sleep_date == SLEEP_DATE,
                )
            )
        ).scalars()
    assert len(list(rows)) == 1


async def test_failure_is_recorded_not_raised(
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整理出错时落 `failed` 记录并照常返回，不把异常抛给睡眠调度。

    为什么重要：睡眠调度每 60 秒扫描一次，一次整理异常如果抛出去，
    会把同一轮里的梦、跨夜结算一起带崩——那三件事互不相干。
    """
    from ling.domains.memory import service as memory_service

    actor_id = logged_in.settings.actor_id
    subject_id = logged_in.subject_id
    await _clean(actor_id, subject_id)

    async def _boom(self, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("模拟整理失败")

    monkeypatch.setattr(memory_service.MemoryService, "compact_low_value", _boom)

    async with session_scope() as session:
        outcome = await consolidate_night(
            session, actor_id=actor_id, subject_id=subject_id, sleep_date=SLEEP_DATE
        )
    assert outcome.state == STATE_FAILED
    assert "整理失败" in outcome.reason

    # 失败也要留痕：用户看记忆页时要知道"昨晚没整理完"
    async with session_scope() as session:
        rows = await recent_consolidations(
            session, actor_id=actor_id, subject_id=subject_id
        )
    assert any(r.sleep_date == SLEEP_DATE and r.state == STATE_FAILED for r in rows)


async def test_other_subjects_memories_are_not_touched(logged_in: TestEnv) -> None:  # noqa: F811
    """整理只动本主体的记忆。

    压缩会归档记忆。按主体隔离是记忆模块的基本边界——整理作业不该成为
    绕过它的后门。
    """
    actor_id = logged_in.settings.actor_id
    mine = logged_in.subject_id
    other = "subj_consolidate_other"
    await _clean(actor_id, mine)
    await _clean(actor_id, other)
    await _add_low_value_memories(actor_id=actor_id, subject_id=mine, count=3)
    await _add_low_value_memories(actor_id=actor_id, subject_id=other, count=3)

    async with session_scope() as session:
        await consolidate_night(
            session, actor_id=actor_id, subject_id=mine, sleep_date=SLEEP_DATE
        )

    async with session_scope() as session:
        untouched = (
            await session.execute(
                select(MemoryRow.id).where(
                    MemoryRow.subject_id == other,
                    MemoryRow.archived_at.is_not(None),
                )
            )
        ).all()
    assert untouched == [], "整理动了别人主体的记忆"
