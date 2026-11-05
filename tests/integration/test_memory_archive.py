"""记忆归档与遗忘曲线（设计文档 §6.2 / §10）。

三种"遗忘"必须分开，不能混为一谈：
- `valid_until` 到期 → 不再作事实使用（recall 过滤），数据仍在、可审计
- `retention_days` 到期 → 归档（recall 过滤 + 标记 archived_at），**不物理删除**
- 用户撤回 → `retracted=true`（已有，保留证据链）

为什么不物理删除：`superseded_by` 链与 `Episode.source_event_ids` 都引用 id，
删了会断链；而且备份恢复可能让已删内容复活（墓碑检查是另一层防线）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.contracts.v1 import Origin, Visibility
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow
from ling.worker import handle_memory_archive
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _seed(env: TestEnv, *, days_old: int, retention_days: int = 365) -> str:
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryRow(
                id=memory_id,
                subject_id=env.subject_id,
                kind="assertion",
                text=f"旧记忆 {days_old} 天前",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                confidence=0.8,
                source_event_ids=["evt"],
                retracted=False,
                retention_days=retention_days,
                created_at=datetime.now(UTC) - timedelta(days=days_old),
            )
        )
    return memory_id


async def test_expired_retention_is_archived_not_deleted(logged_in: TestEnv) -> None:
    """保留期到期 → 归档（有 archived_at），不是 DELETE。"""
    old_id = await _seed(logged_in, days_old=400, retention_days=365)
    fresh_id = await _seed(logged_in, days_old=10, retention_days=365)

    async with session_scope() as session:
        archived = await MemoryService(session).archive_expired()
    assert archived >= 1

    async with session_scope() as session:
        old_row = await session.get(MemoryRow, old_id)
        fresh_row = await session.get(MemoryRow, fresh_id)
    assert old_row is not None, "记忆被物理删除了——应只归档"
    assert old_row.archived_at is not None
    assert "retention_days" in (old_row.archive_reason or "")
    assert fresh_row.archived_at is None, "未到期的记忆被误归档"


async def test_archived_memory_not_returned_by_recall(logged_in: TestEnv) -> None:
    """归档后不再参与检索。

    注意：保留期硬门已经在 recall 里生效，所以到期记忆在归档**之前**
    也不会被召回。这里验证的是两条防线都在：
    - 未归档但已到期 → 召回排除（硬门）
    - 已归档 → 召回排除（归档标记）
    """
    memory_id = await _seed(logged_in, days_old=400, retention_days=365)

    # 第一条防线：未归档但已到期，召回已排除
    async with session_scope() as session:
        before = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="旧记忆",
            visibility_allow={Visibility.subject_private},
        )
    assert all(h.memory_id != memory_id for h in before.hits)

    async with session_scope() as session:
        await MemoryService(session).archive_expired()
        row = await session.get(MemoryRow, memory_id)
    assert row.archived_at is not None, "归档任务未标记"

    # 第二条防线：归档后同样排除
    async with session_scope() as session:
        after = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="旧记忆",
            visibility_allow={Visibility.subject_private},
        )
    assert all(h.memory_id != memory_id for h in after.hits), "归档后仍被召回"


async def test_valid_until_is_separate_from_retention(logged_in: TestEnv) -> None:
    """valid_until 到期 ≠ 保留期到期：前者不再作事实，但不归档。"""
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryRow(
                id=memory_id,
                subject_id=logged_in.subject_id,
                kind="assertion",
                text="临时状态：正在出差",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                confidence=0.8,
                source_event_ids=["evt"],
                retracted=False,
                retention_days=365,
                valid_until=datetime.now(UTC) - timedelta(days=1),  # 已过期
            )
        )

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="出差",
            visibility_allow={Visibility.subject_private},
        )
    assert all(h.memory_id != memory_id for h in result.hits), "过期状态仍被当作事实"

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row.archived_at is None, "valid_until 到期不该触发归档（保留期内仍可审计）"


async def test_retracted_memory_is_not_archived(logged_in: TestEnv) -> None:
    """已撤回的记忆不需要归档标记——撤回本身就是更强的失效。"""
    memory_id = await _seed(logged_in, days_old=400, retention_days=30)
    async with session_scope() as session:
        await MemoryService(session).retract(memory_id, reason="用户纠正")

    async with session_scope() as session:
        await MemoryService(session).archive_expired()
        row = await session.get(MemoryRow, memory_id)
    assert row.retracted is True
    assert row.archived_at is None, "撤回的记忆不应再被归档（语义重复）"


async def test_old_episodes_are_archived(logged_in: TestEnv) -> None:
    episode_id = f"ep_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=logged_in.subject_id,
                window_id="win_old",
                summary="很久以前的一轮对话",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                source_event_ids=["evt"],
                turn_id=f"turn_{uuid.uuid4().hex[:8]}",
                occurred_at=datetime.now(UTC) - timedelta(days=400),
            )
        )
    async with session_scope() as session:
        count = await MemoryService(session).archive_episodes(older_than_days=180)
    assert count >= 1
    async with session_scope() as session:
        row = await session.get(MemoryEpisode, episode_id)
    assert row is not None, "情节被物理删除了"
    assert row.archived_at is not None


async def test_recall_marks_last_recalled(logged_in: TestEnv) -> None:
    """召回后记录 last_recalled_at，供后续判断"长期不用"。"""
    memory_id = await _seed(logged_in, days_old=5)
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
        assert row.last_recalled_at is None

    async with session_scope() as session:
        await MemoryService(session).mark_recalled([memory_id])

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row.last_recalled_at is not None


async def test_archive_handler_runs(logged_in: TestEnv) -> None:
    """worker handler 可正常执行（运维投递 memory.archive）。"""
    await _seed(logged_in, days_old=400, retention_days=365)
    await handle_memory_archive({})
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    assert any(r.archived_at is not None for r in rows)


async def test_supersede_chain_survives_archive(logged_in: TestEnv) -> None:
    """归档不破坏 supersede 链（这正是不能物理删除的原因）。"""
    old_id = await _seed(logged_in, days_old=400, retention_days=30)
    async with session_scope() as session:
        new_id = await MemoryService(session).supersede(old_id, "用户现在住苏州", reason="纠正")
    assert new_id is not None

    async with session_scope() as session:
        await MemoryService(session).archive_expired()
        old_row = await session.get(MemoryRow, old_id)
        new_row = await session.get(MemoryRow, new_id)
    # supersede 会把旧记忆标为 retracted（更强的失效），归档逻辑因此跳过它。
    # 关键是：两条记录都还在，链没断。
    assert old_row is not None, "旧记忆被物理删除"
    assert old_row.retracted is True
    assert new_row is not None, "新记忆被误删"
    assert new_row.supersedes == old_id, "supersede 链被破坏"


async def test_recall_rejects_expired_retention_without_archive_task(
    logged_in: TestEnv,
) -> None:
    """保留期是硬门：即使归档任务从未运行，到期记忆也不能进模型输入。

    此前实现的关键缺口：归档 handler 是"事后清理"，
    但只要它没跑（停机、积压、忘了投递），到期记忆就会继续被召回。
    recall 必须自己拒绝。
    """
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService

    memory_id = await _seed(logged_in, days_old=400, retention_days=365)
    # 故意**不调用** archive_expired，模拟归档任务停摆
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="旧记忆",
            visibility_allow={Visibility.subject_private},
        )
    assert all(h.memory_id != memory_id for h in result.hits), (
        "保留期已过但未归档的记忆仍被召回（归档任务停摆时就会泄漏）"
    )
    assert result.filtered_out >= 1

    # 数据仍在（硬门只影响检索，不删除）
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None
    assert row.archived_at is None, "recall 不应顺带改库"


async def test_recall_keeps_memory_just_before_retention(logged_in: TestEnv) -> None:
    """边界：保留期最后一天仍可召回。"""
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService

    memory_id = await _seed(logged_in, days_old=364, retention_days=365)
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="旧记忆",
            visibility_allow={Visibility.subject_private},
        )
    assert any(h.memory_id == memory_id for h in result.hits), "未到期却被排除"


async def test_maintenance_is_enqueued_once_per_bucket(logged_in: TestEnv) -> None:
    """维护任务按日期桶幂等投递：同一天只投一次。"""
    from ling.worker import enqueue_maintenance

    # 清掉可能存在的维护任务
    async with session_scope() as session:
        from sqlalchemy import delete

        from ling.infra.models import OutboxEvent

        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.topic == "memory.archive")
        )

    first = await enqueue_maintenance()
    second = await enqueue_maintenance()
    assert first >= 1, "首次应投递"
    assert second == 0, "同一桶重复投递（应幂等）"

    async with session_scope() as session:
        from ling.infra.models import OutboxEvent

        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "memory.archive")
                )
            ).scalars()
        )
    assert len(rows) == 1, f"archive 任务应只有 1 条，实际 {len(rows)}"


async def test_maintenance_interval_gate() -> None:
    """间隔门：1 小时内不重复检查。"""
    from ling.worker import MAINTENANCE_INTERVAL_SECONDS, _should_run_maintenance

    assert _should_run_maintenance(0.0, MAINTENANCE_INTERVAL_SECONDS - 1) is False
    assert _should_run_maintenance(0.0, MAINTENANCE_INTERVAL_SECONDS) is True


async def test_forget_subject_also_archives_episodes(logged_in: TestEnv) -> None:
    """注意：forget_subject 原先只撤回 MemoryRow，Episode 仍可召回。"""
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService

    episode_id = f"ep_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=logged_in.subject_id,
                window_id="win_f",
                summary="用户说了一个秘密",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                source_event_ids=["evt"],
                turn_id=f"turn_{uuid.uuid4().hex[:8]}",
            )
        )
    async with session_scope() as session:
        await MemoryService(session).forget_subject(logged_in.subject_id)
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="秘密",
            visibility_allow={Visibility.subject_private},
        )
    assert all("秘密" not in h.text for h in result.hits), (
        "用户要求清除后，情节仍被召回"
    )


async def test_episode_referenced_by_active_assertion_kept(logged_in: TestEnv) -> None:
    """被仍在使用的断言引用的情节不能归档。

    注释原本写着"没有未撤回断言引用"，但代码里没有这个检查 ——
    文档与实现不符。被引用的情节归档后，"上次那件事"的证据链就断了。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService

    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    episode_id = f"ep_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=logged_in.subject_id,
                window_id="win_ref",
                summary="上次聊到的那件事",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                source_event_ids=[f"turn:{turn_id}"],
                turn_id=turn_id,
                occurred_at=datetime.now(UTC) - timedelta(days=400),
            )
        )
    # 同一轮产出的一条仍有效的断言
    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="ref1", kind="assertion", text="用户喜欢下雨天",
                origin=Origin.user_statement, source_event_ids=[f"turn:{turn_id}"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=logged_in.subject_id, turn_id=turn_id, slot="likes", value="下雨天",
        )

    async with session_scope() as session:
        await MemoryService(session).archive_episodes(older_than_days=180)
    async with session_scope() as session:
        row = await session.get(MemoryEpisode, episode_id)
    assert row is not None
    assert row.archived_at is None, "被有效断言引用的情节被归档了（证据链断裂）"


async def test_episode_not_referenced_is_archived(logged_in: TestEnv) -> None:
    """没人引用的老情节仍要归档（不能因为加了保护就永不清理）。"""
    from ling.domains.memory.service import MemoryService

    episode_id = f"ep_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=logged_in.subject_id,
                window_id="win_noref",
                summary="没人再提的旧事",
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                source_event_ids=["turn:gone"],
                turn_id=f"turn_{uuid.uuid4().hex[:8]}",
                occurred_at=datetime.now(UTC) - timedelta(days=400),
            )
        )
    async with session_scope() as session:
        count = await MemoryService(session).archive_episodes(older_than_days=180)
    assert count >= 1
    async with session_scope() as session:
        row = await session.get(MemoryEpisode, episode_id)
    assert row is not None and row.archived_at is not None
