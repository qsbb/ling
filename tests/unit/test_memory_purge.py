"""记忆真删：墓碑、代次栅栏与物理清理的单元回归。

用户原话三句：

1. 「用户说忘了……这种东西不会也不应该存在」
2. 「只能后台管理页删，对话里不能删」
3. 「真删：原文和派生数据都从库里清掉」

这个文件钉的是**领域层**的判定（`domains/memory/purge.py`），不经过 HTTP、
不经过 worker。跑在 sqlite 内存库上：这些用例要验证的是"哪些行被删了、
哪些字段被换了"，与方言无关；真实 PostgreSQL 上的竞态与端到端在
`tests/integration/test_memory_purge_flow.py`。

为什么用 sqlite 而不是测试库：单元回归必须能在没有 127.0.0.1:55432 的机器
上跑。把"领域逻辑对不对"和"这个环境有没有 PG"绑在一起，等于让前者永远
没法单独跑。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ling.domains.memory.purge import (
    MemoryPurgeError,
    claim_still_valid,
    has_open_purge,
    list_tombstones,
    open_purge_subjects,
    pending_tombstones,
    purge_subject,
    request_purge,
)
from ling.infra.db import Base
from ling.infra.models import (
    AwarenessItemRow,
    CoreMemoryRow,
    EntityRow,
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryEpisode,
    MemoryPurgeTombstone,
    MemoryRetrievalLog,
    MemoryRow,
    OutboxEvent,
    Subject,
    Window,
)

# 被删的正文。用一望即知的哨兵串：一旦它出现在任何**保留下来**的东西里
# （墓碑、事件 payload、审计字段），用例就会直接指出来。
CANARY = "他对花生过敏这件事不要提"


@pytest.fixture()
async def session_factory():
    """一个建好全部表的 sqlite 内存库 + 会话工厂。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory) -> dict[str, str]:  # noqa: ANN001 - 测试夹具，类型从简
    """造一个"什么都有"的主体：记忆 + 全部派生数据 + 在途事件。"""
    async with factory() as session:
        subject = Subject(id="subj_canary", display_name="测试用户", forget_generation=0)
        session.add(subject)
        session.add(
            Window(
                id="win_1",
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id=subject.id,
            )
        )
        session.add(
            MemoryRow(
                id="mem_1",
                subject_id=subject.id,
                kind="assertion",
                text=CANARY,
                origin="user_statement",
                visibility="subject_private",
                confidence=0.9,
                slot="health",
                value="花生过敏",
            )
        )
        session.add(
            MemoryEpisode(
                id="ep_1",
                subject_id=subject.id,
                window_id="win_1",
                turn_id="turn_1",
                summary=f"用户提到{CANARY}",
                origin="external_observation",
                visibility="subject_private",
            )
        )
        session.add(
            CoreMemoryRow(
                id="core_1",
                subject_id=subject.id,
                label="边界",
                text=CANARY,
            )
        )
        session.add(
            AwarenessItemRow(
                id="aw_1",
                subject_id=subject.id,
                kind="subject_fact",
                text=CANARY,
                audience="subject_scoped",
                policy_revision=1,
            )
        )
        session.add(
            MemoryEmbeddingRow(
                memory_id="mem_1", model_id="emb-test", dim=3, vector=[0.1, 0.2, 0.3]
            )
        )
        session.add(EntityRow(id="ent_1", subject_id=subject.id, name="花生", kind="other"))
        session.add(MemoryEntityRow(memory_id="mem_1", entity_id="ent_1", role="about"))
        # 检索日志存的是**用户原话**，是最容易漏掉的一处。
        session.add(
            MemoryRetrievalLog(
                id="mlog_1",
                window_id="win_1",
                query=CANARY,
                hit_ids=["mem_1"],
            )
        )
        # 在途事件：payload 里带着原文。真库里就有 24 条这样的 pending
        # `memory.extract`——只软清除的话，它们一执行就把内容写回去。
        for index, state in enumerate(("pending", "in_flight", "done")):
            session.add(
                OutboxEvent(
                    id=f"obx_{index}",
                    topic="memory.extract",
                    state=state,
                    subject_id=subject.id,
                    forget_generation=0,
                    payload={"subject_id": subject.id, "user_text": CANARY},
                )
            )
        await session.commit()
    return {"subject_id": subject.id}


async def _counts(factory, subject_id: str) -> dict[str, int]:
    """该主体在各张表里的行数（清理后应当全为 0）。"""
    from sqlalchemy import func, select

    async with factory() as session:
        out: dict[str, int] = {}
        for name, column in (
            ("memories", MemoryRow.subject_id),
            ("episodes", MemoryEpisode.subject_id),
            ("core", CoreMemoryRow.subject_id),
            ("awareness", AwarenessItemRow.subject_id),
            ("entities", EntityRow.subject_id),
        ):
            out[name] = int(
                await session.scalar(
                    select(func.count()).select_from(column.table).where(column == subject_id)
                )
                or 0
            )
        out["embeddings"] = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEmbeddingRow)
                .where(MemoryEmbeddingRow.memory_id == "mem_1")
            )
            or 0
        )
        out["entity_links"] = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEntityRow)
                .where(MemoryEntityRow.memory_id == "mem_1")
            )
            or 0
        )
        out["retrieval_logs"] = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryRetrievalLog)
                .where(MemoryRetrievalLog.window_id == "win_1")
            )
            or 0
        )
        return out


# ------------------------------------------------------------------ 请求阶段


async def test_request_purge_bumps_generation_and_writes_pending_tombstone(
    session_factory,
) -> None:
    """代次 +1 且墓碑是 `pending`：两者是"已接受请求"的唯一证据。"""
    ids = await _seed(session_factory)
    async with session_factory() as session:
        before = await session.get(Subject, ids["subject_id"])
        assert before is not None
        assert before.forget_generation == 0

        request = await request_purge(
            session, subject_id=ids["subject_id"], requested_by="user_1"
        )
        await session.commit()

    assert request.generation == 1
    assert request.tombstone_id.startswith("purge")
    # 3 条事件里 2 条是 pending/in_flight，`done` 的那条不该被改状态。
    assert request.events_cancelled == 2

    async with session_factory() as session:
        subject = await session.get(Subject, ids["subject_id"])
        assert subject is not None
        assert subject.forget_generation == 1, "代次必须 +1，否则旧写入仍然有效"
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "pending"
        assert tombstone.generation == 1
        assert tombstone.requested_by == "user_1"
        assert tombstone.completed_at is None


async def test_request_purge_cancels_and_scrubs_in_flight_events(session_factory) -> None:
    """在途事件被置 cancelled，且 **payload 换成 `{"purged": True}`**。

    只改状态不清 payload 等于没删：原文还躺在 `outbox_events.payload` 里，
    而且是最容易被忽略的一处副本（没人会去翻 outbox 表）。
    """
    ids = await _seed(session_factory)
    async with session_factory() as session:
        await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        for event_id in ("obx_0", "obx_1"):
            event = await session.get(OutboxEvent, event_id)
            assert event is not None
            assert event.state == "cancelled"
            assert event.payload == {"purged": True}, "payload 里的原文必须被擦掉"
            assert CANARY not in str(event.payload)
            assert event.last_error == "memory_purged"

        # 已经 done 的事件**状态不动**：它已经发生过了，改成 cancelled 是伪造历史。
        # 但它的 payload 由清理阶段负责脱敏（见下一个用例）。
        done = await session.get(OutboxEvent, "obx_2")
        assert done is not None
        assert done.state == "done"


async def test_request_purge_rejects_unknown_subject(session_factory) -> None:
    """主体不存在 = 没有记忆可删。不建墓碑，也不静默返回成功。"""
    async with session_factory() as session:
        with pytest.raises(MemoryPurgeError) as excinfo:
            await request_purge(session, subject_id="subj_不存在")
    assert excinfo.value.code == "subject_not_found"


async def test_request_purge_is_idempotent_in_generation_monotonicity(session_factory) -> None:
    """连点两次删除：代次是 1→2，不是 1→1。

    代次必须**严格递增**：第二次请求也要让第一次请求之后、它之前的写入作废。
    """
    ids = await _seed(session_factory)
    async with session_factory() as session:
        first = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        second = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    assert (first.generation, second.generation) == (1, 2)
    async with session_factory() as session:
        subject = await session.get(Subject, ids["subject_id"])
        assert subject is not None
        assert subject.forget_generation == 2


# ------------------------------------------------------------------ 清理阶段


async def test_purge_subject_physically_deletes_every_derived_table(session_factory) -> None:
    """真删：八张表里该主体的行全部为 0，且墓碑置 done 并记下计数。"""
    ids = await _seed(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        result = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    assert result.state == "done"
    assert result.memories_deleted == 1
    assert result.episodes_deleted == 1
    assert result.core_deleted == 1
    assert result.awareness_deleted == 1
    assert result.embeddings_deleted == 1
    assert result.entities_deleted == 1  # 记忆↔实体的关联行
    assert result.retrieval_logs_deleted == 1, "检索日志存着用户原话，必须被删"

    assert await _counts(session_factory, ids["subject_id"]) == {
        "memories": 0,
        "episodes": 0,
        "core": 0,
        "awareness": 0,
        "entities": 0,
        "embeddings": 0,
        "entity_links": 0,
        "retrieval_logs": 0,
    }

    async with session_factory() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "done"
        assert tombstone.completed_at is not None
        assert tombstone.retrieval_logs_deleted == 1
        assert tombstone.error_code is None


async def test_purge_subject_scrubs_historical_event_payloads(session_factory) -> None:
    """连已经 `done` 的历史事件也要脱敏：它们不再被消费，但原文还在库里。"""
    ids = await _seed(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    async with session_factory() as session:
        done = await session.get(OutboxEvent, "obx_2")
        assert done is not None
        assert done.state == "done"
        assert done.payload == {"purged": True}
        assert CANARY not in str(done.payload)


async def test_purge_subject_is_idempotent(session_factory) -> None:
    """重复跑同一个墓碑：不报错，计数**不翻倍**。

    幂等不是"可选的优化"：清理必须能重试（删到一半崩了要能续），而
    至少一次投递意味着它一定会被跑两次以上。
    """
    ids = await _seed(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        first = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()
    async with session_factory() as session:
        second = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    assert second.state == "done"
    assert second.memories_deleted == first.memories_deleted == 1
    assert second.retrieval_logs_deleted == first.retrieval_logs_deleted == 1
    assert second.episodes_deleted == first.episodes_deleted == 1

    async with session_factory() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.memories_deleted == 1, "重复清理把计数又加了一遍"


async def test_purge_subject_rejects_unknown_tombstone(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(MemoryPurgeError) as excinfo:
            await purge_subject(session, tombstone_id="purge_不存在")
    assert excinfo.value.code == "tombstone_not_found"


# ------------------------------------------------------------------ 墓碑不含正文


async def test_tombstone_and_events_never_carry_the_deleted_text(session_factory) -> None:
    """墓碑是**长期保留**的（用于备份恢复后防复活），所以它不能存正文。

    它一旦存了被删的文本，就变成了一个新的泄漏点——用户要求的是"不存在"，
    不是"换个地方存"。这条断言把"以后有人顺手加一列 original_text"钉死。
    """
    ids = await _seed(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    async with session_factory() as session:
        rows = await list_tombstones(session, subject_id=ids["subject_id"])
        assert rows
        blob = repr(
            [
                {k: v for k, v in row.__dict__.items() if not k.startswith("_")}
                for row in rows
            ]
        )
        assert CANARY not in blob, "墓碑里出现了被删正文"
        assert "花生过敏" not in blob, "墓碑里出现了从原文派生的值"

        # 墓碑列集合也钉一下：新增列必须重新审视"它会不会装正文"。
        columns = {col.name for col in MemoryPurgeTombstone.__table__.columns}
        assert columns == {
            "id",
            "subject_id",
            "requested_by",
            "generation",
            "state",
            "requested_at",
            "completed_at",
            "memories_deleted",
            "episodes_deleted",
            "core_deleted",
            "awareness_deleted",
            "events_cancelled",
            "embeddings_deleted",
            "entities_deleted",
            "retrieval_logs_deleted",
            "events_scrubbed",
            "error_code",
        }


# ------------------------------------------------------------------ 读侧硬门


async def test_read_side_gate_blocks_between_request_and_purge(session_factory) -> None:
    """请求到清理之间，行还在库里，但**不能再被读到**。

    代次栅栏管的是写入，管不到读取。这段时间里如果照常列出/召回，用户刚点完
    "删除"就会看到她把他刚要求忘掉的事再说一遍——比"删得慢"糟得多。
    """
    from sqlalchemy import select

    from ling.domains.memory.service import MemoryService

    ids = await _seed(session_factory)
    async with session_factory() as session:
        service = MemoryService(session, embedder=None, reranker=None)
        # 清理前：读得到。
        assert await has_open_purge(session, subject_id=ids["subject_id"]) is False
        assert [r.id for r in await service.list_memories(ids["subject_id"], include="all")] == [
            "mem_1"
        ]
        assert await service.count_memories(ids["subject_id"], include="all") == 1

        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        service = MemoryService(session, embedder=None, reranker=None)
        assert await has_open_purge(session, subject_id=ids["subject_id"]) is True
        assert await open_purge_subjects(
            session, subject_ids=[ids["subject_id"], "subj_别的"]
        ) == {ids["subject_id"]}
        # 行还在（物理删除是后台的），但读侧一律返回空。
        still_there = (
            await session.execute(
                select(MemoryRow).where(MemoryRow.subject_id == ids["subject_id"])
            )
        ).scalars()
        assert list(still_there), "这个用例的前提是：此刻行还在库里"
        assert await service.list_memories(ids["subject_id"], include="all") == []
        assert await service.count_memories(ids["subject_id"], include="all") == 0

    # 清理完成后，门自然打开（此时也没行可读了）。
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()
    async with session_factory() as session:
        assert await has_open_purge(session, subject_id=ids["subject_id"]) is False


async def test_failed_tombstone_also_blocks_reads(session_factory) -> None:
    """`failed` 也算"未完成"：清理失败时放行读取等于用一次故障换一次泄漏。"""
    ids = await _seed(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        tombstone.state = "failed"
        tombstone.error_code = "purge_failed"
        await session.commit()

    async with session_factory() as session:
        assert await has_open_purge(session, subject_id=ids["subject_id"]) is True
        assert await pending_tombstones(session) == [request.tombstone_id]


# ------------------------------------------------------------------ 代次栅栏


async def test_claim_fence_rejects_writes_from_an_older_generation(session_factory) -> None:
    """绑了旧代次的在途任务，在请求提交后必须读到"不许写"。

    这是"边删边写"唯一的堵法：`cancelled` 抓不住已经认领的事件。
    """
    from ling.domains.memory.purge import bind_claim_fence, release_claim_fence

    ids = await _seed(session_factory)
    token = bind_claim_fence(topic="memory.extract", subject_id=ids["subject_id"], generation=0)
    try:
        async with session_factory() as session:
            assert await claim_still_valid(session) is True, "没删之前当然允许写"

        async with session_factory() as session:
            await request_purge(session, subject_id=ids["subject_id"])
            await session.commit()

        async with session_factory() as session:
            assert await claim_still_valid(session) is False, "旧代次的在途任务必须作废"
    finally:
        release_claim_fence(token)


async def test_claim_fence_allows_writes_without_a_bound_claim(session_factory) -> None:
    """没有绑定（直接调用 handler 的脚本/测试）时视为有效，不改变旧行为。"""
    await _seed(session_factory)
    async with session_factory() as session:
        assert await claim_still_valid(session) is True
