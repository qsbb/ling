"""记忆真删的端到端与竞态回归（真实 PostgreSQL）。

覆盖三件在单元测试里测不到的事：

1. **核心竞态**：一条 `memory.extract` 在"用户点删除"之前入队、之后才跑。
   只把 outbox 置成 `cancelled` 抓不住它（它已经不在 pending 里了），必须靠
   代次栅栏让它静默作废。这里分两种：还在 `pending` 的、以及已经 `in_flight`
   （模拟"已被认领、正在 handler 里跑"）的。
2. **端到端**：造一个"什么都有"的主体 → 请求 → 清理 → 断言八张表里该主体
   的行数为 0，墓碑 `state=done`。
3. **`memory_retrieval_logs.query` 存的是用户原话**，必须被删——这一处是整条
   链上最容易漏的，单独断言。

为什么不放在 unit：`session_scope()` / `drain_outbox()` 走的是全局引擎与
连接池，那是真实装配的一部分；用 sqlite 或手搓 session 测出来的"能过"
不代表线上能过。真实库在 `LING_TEST_DATABASE_URL`
（默认 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from ling.domains.memory.purge import has_open_purge, request_purge
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
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
    Window,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

# 被删的正文哨兵。清理之后它不该出现在库里任何一处（除了这里）。
CANARY = "我对花生过敏，千万别给我吃"


async def _seed_everything(env: TestEnv) -> dict[str, str]:
    """造一个"记忆全都有"的主体：原文 + 六类派生数据 + 一条检索日志。"""
    subject_id = env.subject_id
    window_id = f"win_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            Window(
                id=window_id,
                actor_id=env.settings.actor_id,
                persona_id=env.settings.persona_id,
                subject_id=subject_id,
            )
        )
        session.add(
            MemoryRow(
                id=f"mem_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
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
                id=f"ep_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
                window_id=window_id,
                turn_id=f"turn_{uuid.uuid4().hex[:8]}",
                summary=f"用户说过：{CANARY}",
                origin="external_observation",
                visibility="subject_private",
            )
        )
        session.add(
            CoreMemoryRow(
                id=f"core_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
                label="禁忌",
                text=CANARY,
            )
        )
        session.add(
            AwarenessItemRow(
                id=f"aw_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
                kind="subject_fact",
                text=CANARY,
                audience="subject_scoped",
                policy_revision=1,
            )
        )
        session.add(
            EntityRow(
                id=f"ent_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
                name="花生",
                kind="other",
            )
        )
        session.add(
            MemoryRetrievalLog(
                id=f"mlog_{uuid.uuid4().hex[:10]}",
                window_id=window_id,
                query=CANARY,
                hit_ids=[],
            )
        )
        await session.flush()
        # 向量与实体关联靠 memory_id 关联，得先拿到上面那条记忆的 id。
        memory_id = str(
            await session.scalar(
                select(MemoryRow.id).where(MemoryRow.subject_id == subject_id)
            )
        )
        entity_id = str(
            await session.scalar(
                select(EntityRow.id).where(EntityRow.subject_id == subject_id)
            )
        )
        session.add(
            MemoryEmbeddingRow(
                memory_id=memory_id, model_id="emb-test", dim=3, vector=[0.1, 0.2, 0.3]
            )
        )
        session.add(MemoryEntityRow(memory_id=memory_id, entity_id=entity_id, role="about"))
    return {"subject_id": subject_id, "window_id": window_id, "memory_id": memory_id}


async def _count(env: TestEnv, model, column, value: str) -> int:  # noqa: ANN001
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(column == value)
            )
            or 0
        )


# ------------------------------------------------------------------ 核心竞态


async def test_pending_extract_is_fenced_after_purge_request(logged_in: TestEnv) -> None:
    """**核心竞态（pending）**：入队 → 请求删除 → 跑 handler → 没有新记忆。

    这是用户最不能接受的窗口：他刚点了"删除"，而那条早就入队的提取任务
    一跑就把内容又写了回来。代次栅栏就是为这个窗口存在的。
    """
    from ling.domains.memory.purge import bind_claim_fence, release_claim_fence
    from ling.infra.security import new_id
    from ling.worker import handle_memory_extract

    window = await logged_in.client.post("/v1/sessions", json={"title": "竞态"})
    window_id = window.json()["session_id"]
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    event_id = new_id("obx")

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                subject_id=logged_in.subject_id,
                forget_generation=0,
                payload={
                    "subject_id": logged_in.subject_id,
                    "window_id": window_id,
                    "turn_id": turn_id,
                    "user_text": "我住在杭州",
                    "actor_text": "记下了",
                    "source_visibility": "subject_private",
                },
            )
        )

    # 用户点删除（同一事务里写墓碑 + 取消在途事件）。
    async with session_scope() as session:
        request = await request_purge(session, subject_id=logged_in.subject_id)

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.state == "cancelled"

    # 即便有人手工把这条事件捞出来跑（模拟"已经认领、正在 handler 里"），
    # handler 自己也必须读一次代次并作废。
    token = bind_claim_fence(
        topic="memory.extract", subject_id=logged_in.subject_id, generation=0
    )
    try:
        await handle_memory_extract(
            {
                "subject_id": logged_in.subject_id,
                "window_id": window_id,
                "turn_id": turn_id,
                "user_text": "我住在杭州",
                "actor_text": "记下了",
                "source_visibility": "subject_private",
            }
        )
    finally:
        release_claim_fence(token)

    assert (
        await _count(logged_in, MemoryRow, MemoryRow.turn_id, turn_id) == 0
    ), "遗忘请求之后，旧代次的提取任务仍然写入了记忆"
    assert (
        await _count(logged_in, MemoryEpisode, MemoryEpisode.turn_id, turn_id) == 0
    ), "遗忘请求之后，旧代次的提取任务仍然写入了情节"

    # 墓碑仍是 pending（清理阶段由 worker 跑），但它已经封住了写入与读取。
    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "pending"


async def test_in_flight_extract_is_fenced_after_purge_request(logged_in: TestEnv) -> None:
    """**核心竞态（in_flight）**：事件已被认领，删除请求仍必须让它作废。

    `cancelled` 只能拦住还在 `pending` 里的事件。已经认领的（`in_flight`）
    不在 pending 里，改状态抓不住它——只有代次能。
    """
    from ling.domains.memory.purge import bind_claim_fence, release_claim_fence
    from ling.infra.security import new_id
    from ling.worker import handle_memory_extract

    window = await logged_in.client.post("/v1/sessions", json={"title": "在途"})
    window_id = window.json()["session_id"]
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    event_id = new_id("obx")

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                state="in_flight",
                subject_id=logged_in.subject_id,
                forget_generation=0,
                payload={
                    "subject_id": logged_in.subject_id,
                    "window_id": window_id,
                    "turn_id": turn_id,
                    "user_text": "我喜欢喝黑咖啡",
                    "actor_text": "记住了",
                    "source_visibility": "subject_private",
                },
            )
        )

    async with session_scope() as session:
        await request_purge(session, subject_id=logged_in.subject_id)

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        # 在途事件也被取消并脱敏（不是只拦 pending）。
        assert event.state == "cancelled"
        assert event.payload == {"purged": True}

    # 模拟"handler 已经在跑"：认领时记的旧代次必须让它读到"不许写"。
    token = bind_claim_fence(
        topic="memory.extract", subject_id=logged_in.subject_id, generation=0
    )
    try:
        await handle_memory_extract(
            {
                "subject_id": logged_in.subject_id,
                "window_id": window_id,
                "turn_id": turn_id,
                "user_text": "我喜欢喝黑咖啡",
                "actor_text": "记住了",
                "source_visibility": "subject_private",
            }
        )
    finally:
        release_claim_fence(token)

    assert await _count(logged_in, MemoryRow, MemoryRow.turn_id, turn_id) == 0
    assert await _count(logged_in, MemoryEpisode, MemoryEpisode.turn_id, turn_id) == 0


async def test_extract_from_the_current_generation_still_writes(logged_in: TestEnv) -> None:
    """栅栏不能把**正常**写入一起拦掉。

    这条是上两条的反面：只有代次**对不上**才作废。如果只要存在墓碑就一律拒绝，
    用户删完记忆之后就再也记不住任何东西了——那不是"忘记"，那是坏掉。
    """
    from ling.domains.memory.purge import (
        bind_claim_fence,
        current_generation,
        purge_subject,
        release_claim_fence,
    )
    from ling.worker import handle_memory_extract

    window = await logged_in.client.post("/v1/sessions", json={"title": "删除后"})
    window_id = window.json()["session_id"]

    async with session_scope() as session:
        request = await request_purge(session, subject_id=logged_in.subject_id)
    async with session_scope() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)

    async with session_scope() as session:
        generation = await current_generation(session, logged_in.subject_id)
    assert generation == request.generation

    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    token = bind_claim_fence(
        topic="memory.extract",
        subject_id=logged_in.subject_id,
        generation=generation,
    )
    try:
        await handle_memory_extract(
            {
                "subject_id": logged_in.subject_id,
                "window_id": window_id,
                "turn_id": turn_id,
                "user_text": "我住在杭州",
                "actor_text": "记下了",
                "source_visibility": "subject_private",
            }
        )
    finally:
        release_claim_fence(token)

    assert (
        await _count(logged_in, MemoryRow, MemoryRow.turn_id, turn_id) == 1
    ), "当前代次的写入被误拦了——删完之后就再也记不住任何东西"
    assert await _count(logged_in, MemoryEpisode, MemoryEpisode.turn_id, turn_id) == 1


async def test_purge_during_model_extraction_leaves_nothing_behind(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**最真实的窗口**：删除请求正好落在模型提炼的那几十秒里。

    这条用例抓的是一个真实的顺序缺陷：原实现**先写情节、再调模型提炼**，
    于是慢提炼期间的遗忘请求只能在第二次栅栏处 `return`——而情节已经在
    同一个事务里了，它会被一起提交。表现是"删完之后库里又多了一条情节"，
    而且这条情节带着被删原话的摘要。

    现在提炼排在所有写入之前，两道栅栏之间没有任何写入。这里用一个会**在
    提炼过程中发起遗忘请求**的假提炼器来钉住这个顺序：如果以后有人把某次
    写入挪到提炼之前，`MemoryEpisode` 的计数会立刻变成 1。
    """
    from ling.domains.memory.purge import bind_claim_fence, release_claim_fence
    from ling.worker import handle_memory_extract

    window = await logged_in.client.post("/v1/sessions", json={"title": "提炼期间删除"})
    window_id = window.json()["session_id"]
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"

    async def _extract_then_forget(*, user_text: str, actor_text: str, is_group: bool):  # noqa: ANN001, ANN202
        """模拟"模型提炼期间用户点了删除"。"""
        async with session_scope() as inner:
            await request_purge(inner, subject_id=logged_in.subject_id)
        from ling.domains.memory.extract import fallback_candidates

        return fallback_candidates(user_text)

    monkeypatch.setattr("ling.worker._extract_candidates", _extract_then_forget)

    token = bind_claim_fence(
        topic="memory.extract", subject_id=logged_in.subject_id, generation=0
    )
    try:
        await handle_memory_extract(
            {
                "subject_id": logged_in.subject_id,
                "window_id": window_id,
                "turn_id": turn_id,
                "user_text": "我住在杭州",
                "actor_text": "记下了",
                "source_visibility": "subject_private",
            }
        )
    finally:
        release_claim_fence(token)

    assert await _count(logged_in, MemoryEpisode, MemoryEpisode.turn_id, turn_id) == 0, (
        "提炼期间被删除，但情节仍然落了库——写入顺序又回到了'先写后提炼'"
    )
    assert await _count(logged_in, MemoryRow, MemoryRow.turn_id, turn_id) == 0


# ------------------------------------------------------------------ 端到端


async def test_end_to_end_purge_empties_every_table(logged_in: TestEnv) -> None:
    """造全套 → 请求 → 清理 → 八张表里该主体行数为 0，墓碑 done。"""
    from ling.worker import handle_memory_purge

    ids = await _seed_everything(logged_in)
    subject_id = ids["subject_id"]

    # 请求阶段走 HTTP，与后台管理页完全同一条路径（含同事务投递清理任务）。
    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["generation"] == 1
    assert body["tombstone_id"].startswith("purge")

    # 「墓碑存在 ⇒ 清理任务必然存在」：同一事务里两条都落了库。
    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, body["tombstone_id"])
        assert tombstone is not None and tombstone.state == "pending"
        pending = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.topic == "memory.purge",
                    OutboxEvent.state == "pending",
                )
            )
        ).scalars()
        purge_events = [e for e in pending if e.subject_id == subject_id]
        assert len(purge_events) == 1, "墓碑写了，清理任务没写——数据会永远留在库里"
        assert purge_events[0].payload == {"tombstone_id": body["tombstone_id"]}

    # 清理阶段（后台 worker 的入口）。
    await handle_memory_purge({"tombstone_id": body["tombstone_id"]})

    for model, column in (
        (MemoryRow, MemoryRow.subject_id),
        (MemoryEpisode, MemoryEpisode.subject_id),
        (CoreMemoryRow, CoreMemoryRow.subject_id),
        (AwarenessItemRow, AwarenessItemRow.subject_id),
        (EntityRow, EntityRow.subject_id),
    ):
        assert await _count(logged_in, model, column, subject_id) == 0, (
            f"{model.__tablename__} 里还有该主体的行"
        )
    assert await _count(logged_in, MemoryEmbeddingRow, MemoryEmbeddingRow.memory_id, ids["memory_id"]) == 0
    assert await _count(logged_in, MemoryEntityRow, MemoryEntityRow.memory_id, ids["memory_id"]) == 0

    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, body["tombstone_id"])
        assert tombstone is not None
        assert tombstone.state == "done"
        assert tombstone.completed_at is not None
        assert tombstone.memories_deleted == 1
        assert tombstone.episodes_deleted == 1
        assert tombstone.core_deleted == 1
        assert tombstone.awareness_deleted == 1


async def test_retrieval_logs_holding_user_words_are_deleted(logged_in: TestEnv) -> None:
    """`memory_retrieval_logs.query` 存的是**用户原话**，必须被删。

    单独一条用例，因为这是整条链上最容易漏的一处：它不在"记忆"语义里，
    也没有 subject 列（只按 window 关联），漏掉它等于原文还在库里。
    """
    from ling.worker import handle_memory_purge

    ids = await _seed_everything(logged_in)
    window_id = ids["window_id"]

    async with session_scope() as session:
        before = await session.scalar(
            select(func.count())
            .select_from(MemoryRetrievalLog)
            .where(MemoryRetrievalLog.window_id == window_id)
        )
        assert before == 1, "用例前提：检索日志里确实存着用户原话"
        hit = await session.scalar(
            select(MemoryRetrievalLog.query).where(
                MemoryRetrievalLog.window_id == window_id
            )
        )
        assert hit == CANARY

    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text
    await handle_memory_purge({"tombstone_id": response.json()["tombstone_id"]})

    async with session_scope() as session:
        after = await session.scalar(
            select(func.count())
            .select_from(MemoryRetrievalLog)
            .where(MemoryRetrievalLog.window_id == window_id)
        )
        assert after == 0, "检索日志里的用户原话没有被删"

    # 墓碑上必须能证明"删过几条检索日志"，否则这个数字永远是 0，
    # 排查时看不出到底清没清。
    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, response.json()["tombstone_id"])
        assert tombstone is not None
        assert tombstone.retrieval_logs_deleted == 1


async def test_purge_status_endpoint_reports_progress_without_content(
    logged_in: TestEnv,
) -> None:
    """`GET /v1/memory/purge/status` 报告进度，且**不含任何被删内容**。"""
    from ling.worker import handle_memory_purge

    await _seed_everything(logged_in)
    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text
    tombstone_id = response.json()["tombstone_id"]

    pending = (await logged_in.client.get("/v1/memory/purge/status")).json()
    assert pending["items"], pending
    first = pending["items"][0]
    assert first["tombstone_id"] == tombstone_id
    assert first["state"] == "pending"
    assert first["completed_at"] is None

    await handle_memory_purge({"tombstone_id": tombstone_id})

    done = (await logged_in.client.get("/v1/memory/purge/status")).json()
    first = done["items"][0]
    assert first["state"] == "done"
    assert first["completed_at"] is not None
    assert first["memories_deleted"] == 1
    assert first["retrieval_logs_deleted"] == 1
    assert first["error_code"] is None

    # 响应里不能出现被删文本：状态端点不是"删了什么"的查询接口。
    assert CANARY not in str(done)


async def test_list_and_recall_are_empty_between_request_and_purge(
    logged_in: TestEnv,
) -> None:
    """请求到清理之间：行还在库里，但**列表与召回都必须为空**。

    这是"读侧硬门"的接线验证（领域层的判定在
    `tests/unit/test_memory_purge.py::test_read_side_gate_blocks_between_request_and_purge`）。
    没有这道门，用户刚点完"删除"刷新还能看见，而下一轮对话她会当场复述。
    """
    from ling.contracts.v1 import Visibility

    ids = await _seed_everything(logged_in)

    before = (await logged_in.client.get("/v1/memory?include=all")).json()
    assert before["total"] >= 1

    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text

    # 行还在（清理是后台的）——但读侧一律为空。
    assert await _count(logged_in, MemoryRow, MemoryRow.subject_id, ids["subject_id"]) == 1

    during = (await logged_in.client.get("/v1/memory?include=all")).json()
    assert during["memories"] == []
    assert during["total"] == 0

    async with session_scope() as session:
        assert await has_open_purge(session, subject_id=ids["subject_id"]) is True
        recall = await MemoryService(session, embedder=None, reranker=None).recall(
            subject_id=ids["subject_id"],
            query="花生",
            visibility_allow={Visibility.subject_private},
        )
    assert recall.hits == [], "清理进行中仍然召回到了记忆——她下一轮就会把它说出来"
    assert recall.strategy == "purge_pending"

    core = (await logged_in.client.get("/v1/core-memory")).json()
    assert core["items"] == []


async def test_forget_is_idempotent_across_two_requests(logged_in: TestEnv) -> None:
    """连点两次：代次递增，两次都能收敛到 done，且计数不翻倍。"""
    from ling.worker import handle_memory_purge

    await _seed_everything(logged_in)

    first = (await logged_in.client.post("/v1/memory/forget")).json()
    await handle_memory_purge({"tombstone_id": first["tombstone_id"]})
    second = (await logged_in.client.post("/v1/memory/forget")).json()
    await handle_memory_purge({"tombstone_id": second["tombstone_id"]})
    # 再跑一次同一条墓碑（至少一次投递一定会有重复）。
    await handle_memory_purge({"tombstone_id": second["tombstone_id"]})

    assert (first["generation"], second["generation"]) == (1, 2)

    async with session_scope() as session:
        one = await session.get(MemoryPurgeTombstone, first["tombstone_id"])
        two = await session.get(MemoryPurgeTombstone, second["tombstone_id"])
        assert one is not None and two is not None
        assert one.state == "done" and two.state == "done"
        assert one.memories_deleted == 1
        # 第二次请求时已经没有记忆了，所以计数是 0——**不是翻倍**。
        assert two.memories_deleted == 0


async def test_forget_without_a_subject_is_404(logged_in: TestEnv) -> None:
    """主体不存在 = 没有记忆可删。不能编一个 200 让前端以为删过了。"""
    async with session_scope() as session:
        from ling.infra.models import Subject

        subject = await session.get(Subject, logged_in.subject_id)
        assert subject is not None
        # 直接在库里删掉主体（模拟"这个账号已经不存在"），不经过 API。
        await session.delete(subject)

    response = await logged_in.client.post("/v1/memory/forget")
    # 主体没了，鉴权也会失效（登录会话挂在 subject 上）——两种结果都可接受，
    # 不可接受的是 200。
    assert response.status_code in (401, 404), response.text


async def test_purge_sweep_picks_up_an_orphan_tombstone(logged_in: TestEnv) -> None:
    """投递失败的墓碑必须被周期扫描捡起来——否则用户以为删了，数据还在。"""
    from ling.worker import handle_memory_purge_sweep

    ids = await _seed_everything(logged_in)
    # 只写墓碑，**不投** `memory.purge`（模拟"进程在两者之间崩了"）。
    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])

    await handle_memory_purge_sweep({})

    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "done", "孤儿墓碑没被扫描捡起来"
    assert await _count(logged_in, MemoryRow, MemoryRow.subject_id, ids["subject_id"]) == 0


# ------------------------------------------------- 历史事件（subject_id 为 NULL）
#
# `outbox_events.subject_id` 是随真删一起新增的列，**所有历史行都是 NULL**
# （开发库实测：memory.extract 407 条 done + 24 条 pending，无一有该列），
# 而它们 payload 里带着 `user_text` / `actor_text` 原文。
#
# 只按列过滤会一条都选不中：删除后原文仍在库里，pending 的那些一旦被消费
# 还会把内容重新写成记忆。下面两条把"按 payload 兜底"钉死在测试里。


async def test_legacy_pending_event_without_subject_column_is_cancelled(
    logged_in: TestEnv,
) -> None:
    """历史 pending 事件（`subject_id` 列为 NULL）也必须被取消并脱敏。"""
    ids = await _seed_everything(logged_in)
    event_id = f"obx_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                # 关键：列留空，模拟这次迁移之前入队的历史行。
                subject_id=None,
                forget_generation=0,
                payload={
                    "subject_id": ids["subject_id"],
                    "window_id": ids["window_id"],
                    "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
                    "user_text": CANARY,
                    "actor_text": f"她回的：{CANARY}",
                },
            )
        )

    async with session_scope() as session:
        await request_purge(session, subject_id=ids["subject_id"])

    async with session_scope() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.state == "cancelled", "历史 pending 事件没被取消，删除后它还会写回记忆"
        assert CANARY not in str(row.payload), "历史事件的 payload 原文还在库里"


async def test_legacy_done_event_payload_is_scrubbed(logged_in: TestEnv) -> None:
    """已经 done 的历史事件同样要擦正文——它不再被消费，但原文还在库里。"""
    from ling.domains.memory.purge import purge_subject

    ids = await _seed_everything(logged_in)
    event_id = f"obx_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                subject_id=None,
                forget_generation=0,
                state="done",
                payload={
                    "subject_id": ids["subject_id"],
                    "user_text": CANARY,
                    "actor_text": f"她回的：{CANARY}",
                },
            )
        )

    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
    async with session_scope() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)

    async with session_scope() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert CANARY not in str(row.payload), "done 状态的历史事件原文没被擦掉"


# ---------------------------------------------------------- 失败状态必须落库


async def test_purge_failure_is_persisted_despite_rollback(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理失败时墓碑必须**真的**变成 `failed`。

    这里曾经是错的：`purge_subject` 在 except 里写 `failed` 然后抛异常，
    而调用方 `session_scope` 遇到异常会 `rollback()`——写入被一起丢掉，
    墓碑停在 `running`。`memory.purge.sweep` 只扫 `pending/running/failed`，
    所以它还会重试，但前端看到的永远是"一直清理中"，比直接报错更糟：
    它看起来像还在干活。

    做法：让清理过程中的一条 DELETE 抛异常，然后用**新的 session** 重读墓碑。
    """
    from ling.domains.memory.purge import MemoryPurgeError, purge_subject

    ids = await _seed_everything(logged_in)
    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟删除失败")

    # 让删情节那一步炸掉（前面几步成功，确保不是"什么都没做"就失败）。
    monkeypatch.setattr(
        "ling.domains.memory.purge.delete", _boom, raising=True
    )
    with pytest.raises(MemoryPurgeError):
        async with session_scope() as session:
            await purge_subject(session, tombstone_id=request.tombstone_id)

    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "failed", (
            "失败状态被事务回滚吃掉了：墓碑停在 "
            f"{tombstone.state}，用户会一直看到「清理中」"
        )
        assert tombstone.error_code, "失败原因码没落库，无法排查"
