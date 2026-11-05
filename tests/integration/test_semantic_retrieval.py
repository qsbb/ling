"""语义检索（向量 + 重排）在真实库上的行为。

用假 provider 做确定性验证：不依赖外部 API，也不会因网络波动误报。
真实 provider 的可用性由手工验证脚本覆盖（不进测试套件）。
"""

from __future__ import annotations

import pytest

from ling.ports.embedding import EmbeddingError
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class FakeEmbedder:
    """按预置表返回向量；未登记的文本返回正交向量（相似度 0）。"""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.model_id = "fake-embed"
        self.dim = 2
        self._table = table
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._table.get(t, [0.0, 1.0]) for t in texts]


class BoomEmbedder:
    model_id = "boom"
    dim = 2

    async def embed(self, texts: list[str]):  # noqa: ANN201
        raise EmbeddingError("上游挂了")


class BoomReranker:
    model_id = "boom"

    async def rerank(self, query: str, documents: list[str], *, top_n: int | None = None):
        raise EmbeddingError("上游挂了")


async def test_vector_path_ranks_semantic_match(logged_in: object) -> None:
    """字面无交集但语义相关的记忆，应通过向量路被召回。

    "我有什么忌口" 与 "用户不吃香菜" 没有共同 2-gram（BM25 命中不了），
    但语义相近——这正是双路检索要解决的情况。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    subject_id = logged_in.subject_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        service = MemoryService(session)
        await service.propose(
            MemoryProposal(
                proposal_id="p1", kind="assertion", text="用户不吃香菜",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=subject_id, turn_id="t1", slot="dislikes",
        )
        await service.propose(
            MemoryProposal(
                proposal_id="p2", kind="assertion", text="用户养了一只橘猫",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=subject_id, turn_id="t2", slot="has_pet",
        )

    # 让查询与"忌口"在向量空间相近、与"猫"正交
    embedder = FakeEmbedder({
        "我有什么忌口": [1.0, 0.0],
        "用户不吃香菜": [1.0, 0.0],
        "用户养了一只橘猫": [0.0, 1.0],
    })
    async with session_scope() as session:
        result = await MemoryService(session, embedder=embedder, reranker=None).recall(
            subject_id=subject_id,
            query="我有什么忌口",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    texts = [h.text for h in result.hits]
    assert "用户不吃香菜" in texts, f"语义相关但字面无交集的记忆没被召回: {texts}"
    assert texts.index("用户不吃香菜") == 0, "语义最相关的没排第一"


async def test_no_embedder_keeps_bm25_behaviour(logged_in: object) -> None:
    """未配置 embedding 时行为与以前完全等价（只是少了向量那一路）。"""
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    subject_id = logged_in.subject_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p1", kind="assertion", text="用户喜欢下雨天",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=subject_id, turn_id="t1", slot="likes",
        )
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=subject_id,
            query="下雨天",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    assert any("下雨天" in h.text for h in result.hits)


async def test_embedder_failure_falls_back(logged_in: object) -> None:
    """向量服务挂了不能让召回整体失败，应退回字面路径。"""
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    subject_id = logged_in.subject_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p1", kind="assertion", text="用户喜欢下雨天",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=subject_id, turn_id="t1", slot="likes",
        )
    async with session_scope() as session:
        result = await MemoryService(
            session, embedder=BoomEmbedder(), reranker=None
        ).recall(
            subject_id=subject_id,
            query="下雨天",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    assert any("下雨天" in h.text for h in result.hits), "向量故障导致召回全空"


async def test_reranker_failure_falls_back(logged_in: object) -> None:
    """重排挂了应原样返回 RRF 结果，而不是丢掉候选。"""
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    subject_id = logged_in.subject_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p1", kind="assertion", text="用户喜欢下雨天散步",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=subject_id, turn_id="t1", slot="likes",
        )
    async with session_scope() as session:
        result = await MemoryService(
            session, embedder=None, reranker=BoomReranker()
        ).recall(
            subject_id=subject_id,
            query="下雨天",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    assert any("下雨天" in h.text for h in result.hits)


class ReverseReranker:
    """把候选顺序颠倒（用于验证精排真的生效）。"""

    model_id = "reverse"

    async def rerank(self, query: str, documents: list[str], *, top_n: int | None = None):
        return [(i, float(i)) for i in reversed(range(len(documents)))]


async def test_rerank_actually_reorders(logged_in: TestEnv) -> None:
    """精排必须影响**最终顺序**。

    真实缺陷：精排先改了顺序，随后 `scored.sort` 又按旧 RRF 分重排，
    精排等于完全无效（实测：让精排器明确颠倒前两名，
    返回顺序仍是原 RRF 顺序）。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        service = MemoryService(session)
        for i, text in enumerate(["用户喜欢下雨天散步", "用户喜欢喝茶", "用户喜欢看纪录片"]):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"rr{i}", kind="assertion", text=text,
                    origin=Origin.user_statement, source_event_ids=["e"],
                    visibility=Visibility.subject_private, confidence=0.9,
                ),
                subject_id=logged_in.subject_id, turn_id=f"rrT{i}",
                slot=f"likes{i}", value=text,
            )

    async with session_scope() as session:
        base = await MemoryService(session, embedder=None, reranker=None).recall(
            subject_id=logged_in.subject_id, query="用户喜欢",
            visibility_allow={Visibility.subject_private}, limit=5,
        )
    async with session_scope() as session:
        reranked = await MemoryService(
            session, embedder=None, reranker=ReverseReranker()
        ).recall(
            subject_id=logged_in.subject_id, query="用户喜欢",
            visibility_allow={Visibility.subject_private}, limit=5,
        )
    base_order = [h.memory_id for h in base.hits]
    new_order = [h.memory_id for h in reranked.hits]
    assert base_order != new_order, (
        f"精排没有影响最终顺序（仍是 {base_order}）"
    )


class SwitchableEmbedder:
    """模型可切换，用于验证换模型不会撞主键。"""

    def __init__(self, model_id: str, table: dict[str, list[float]]) -> None:
        self.model_id = model_id
        self.dim = 2
        self._table = table

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._table.get(t, [0.0, 1.0]) for t in texts]


async def test_embedding_model_switch_does_not_crash(logged_in: TestEnv) -> None:
    """换 embedding 模型时必须能并存两套向量。

    原主键只有 memory_id，但代码按 (memory_id, model_id) 判断是否已有向量
    → 新模型写向量撞主键冲突，异常被吞后事务失效，
    后续召回报 PendingRollbackError（实测）。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="sw1", kind="assertion", text="用户喜欢下雨天",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=logged_in.subject_id, turn_id="swT1", slot="likes", value="下雨天",
        )

    table = {"我喜欢什么": [1.0, 0.0], "用户喜欢下雨天": [1.0, 0.0]}
    # 模型 v1
    async with session_scope() as session:
        first = await MemoryService(
            session, embedder=SwitchableEmbedder("v1", table), reranker=None
        ).recall(
            subject_id=logged_in.subject_id, query="我喜欢什么",
            visibility_allow={Visibility.subject_private}, limit=5,
        )
    assert any("下雨天" in h.text for h in first.hits)

    # 换成模型 v2 —— 同一批记忆要能重新向量化，不能崩
    async with session_scope() as session:
        second = await MemoryService(
            session, embedder=SwitchableEmbedder("v2", table), reranker=None
        ).recall(
            subject_id=logged_in.subject_id, query="我喜欢什么",
            visibility_allow={Visibility.subject_private}, limit=5,
        )
    assert any("下雨天" in h.text for h in second.hits), "换模型后召回失败"

    # 两套向量并存
    from sqlalchemy import select

    from ling.infra.models import MemoryEmbeddingRow

    async with session_scope() as session:
        rows = list((await session.execute(select(MemoryEmbeddingRow))).scalars())
    assert len({r.model_id for r in rows}) == 2, f"模型向量没有并存: {[r.model_id for r in rows]}"


class _ReversedCandidateResult:
    """Only the candidate-pool query is reversed; every other query is untouched."""

    def __init__(self, result, rows) -> None:  # noqa: ANN001
        self._result = result
        self._rows = rows

    def scalars(self):  # noqa: ANN201
        return list(reversed(self._rows))

    def __getattr__(self, name: str):  # noqa: ANN401
        return getattr(self._result, name)


def _selects_memory_rows(statement) -> bool:  # noqa: ANN001
    try:
        descriptions = statement.column_descriptions
    except Exception:  # noqa: BLE001 - 非 Select 语句没有这个属性
        return False
    from ling.infra.models import MemoryRow

    return any(description.get("entity") is MemoryRow for description in descriptions)


class _ReverseCandidateRowsSession:
    """Proxy session that returns the same MemoryRow result set in reverse order.

    The real database is still queried and every other statement is delegated
    unchanged.  ``captured_row_orders`` records what the service actually
    received, so the test can prove it reversed the real row set rather than
    feeding a hand-built list.
    """

    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session
        self.captured_row_orders: list[list[str]] = []

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        result = await self._session.execute(statement, *args, **kwargs)
        if _selects_memory_rows(statement):
            rows = list(result.scalars())
            # Record the order the service actually receives (reversed).
            self.captured_row_orders.append(
                [row.id for row in reversed(rows)]
            )
            return _ReversedCandidateResult(result, rows)
        return result

    def __getattr__(self, name: str):  # noqa: ANN401
        return getattr(self._session, name)


async def test_recall_order_is_stable_when_sql_rows_are_reversed(
    logged_in: TestEnv,
) -> None:
    """真实 ``recall`` 的最终顺序不能取决于候选池 SQL 返回的行序。

    原实现里 RRF 各路的 ``_rank_map`` 对"零分/同分"也强制给出 1..n 的
    连续名次，名次又按传入列表顺序分配。于是同一批记忆只要数据库返回行序
    不同，BM25/新近度/重要性三路的同分项就会互换名次，RRF 总分随之变化——
    语义最相关的记忆可能被挤到后面。这个用例不测 helper，而是：
    1. 用真实库、真实 ``recall`` 跑一遍；
    2. 用代理 session 只把候选池同一个 SQL 结果集反转，再跑一遍；
    3. 断言两次的最终命中 ID/文本顺序完全一致，且语义命中排第一。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope
    from ling.infra.models import MemoryRow

    query = "我有什么忌口"
    texts = [
        "用户不吃香菜",
        "用户养了一只橘猫",
        "用户喜欢喝手冲咖啡",
        "用户住在杭州西湖区",
        "用户在做体检预约",
        "用户喜欢看纪录片",
    ]
    semantic_text = texts[0]
    fixed_created = datetime(2026, 1, 1, tzinfo=UTC)
    fixed_now = datetime(2026, 6, 1, tzinfo=UTC)

    async with session_scope() as session:
        service = MemoryService(session)
        ids: dict[str, str] = {}
        for index, text in enumerate(texts):
            memory_id = await service.propose(
                MemoryProposal(
                    proposal_id=f"row-order-{index}",
                    kind="assertion",
                    text=text,
                    origin=Origin.user_statement,
                    source_event_ids=["evt-row-order"],
                    visibility=Visibility.subject_private,
                    confidence=0.9,
                ),
                subject_id=logged_in.subject_id,
                turn_id=f"rowOrderT{index}",
                slot=f"likes_row_order_{index}",
            )
            assert memory_id is not None
            ids[text] = memory_id
        # Equal created_at/confidence/access_count => BM25, recency and
        # importance all tie.  Make the semantic row newest so the SQL pool
        # (updated_at DESC) puts it first; reversing the pool moves it last.
        await session.execute(
            update(MemoryRow)
            .where(MemoryRow.id.in_(list(ids.values())))
            .values(created_at=fixed_created, updated_at=fixed_created)
        )
        await session.execute(
            update(MemoryRow)
            .where(MemoryRow.id == ids[semantic_text])
            .values(updated_at=fixed_created + timedelta(seconds=1))
        )

    # Only the query and the semantic row have a vector along [1, 0].  All
    # other texts are orthogonal (the fake's default is [0, 1]).
    table = {query: [1.0, 0.0], semantic_text: [1.0, 0.0]}

    async with session_scope() as session:
        normal = await MemoryService(
            session, embedder=FakeEmbedder(table), reranker=None
        ).recall(
            subject_id=logged_in.subject_id,
            query=query,
            visibility_allow={Visibility.subject_private},
            limit=10,
            now=fixed_now,
        )

    async with session_scope() as session:
        reversing = _ReverseCandidateRowsSession(session)
        reversed_result = await MemoryService(
            reversing, embedder=FakeEmbedder(table), reranker=None
        ).recall(
            subject_id=logged_in.subject_id,
            query=query,
            visibility_allow={Visibility.subject_private},
            limit=10,
            now=fixed_now,
        )

    assert reversing.captured_row_orders, "没有拦截到候选池查询"
    received = reversing.captured_row_orders[0]
    assert set(received) == set(ids.values()), "反转的不是同一批候选行"
    assert received[-1] == ids[semantic_text], (
        f"语义命中没有落在反转后的末尾: {received} semantic={ids[semantic_text]}"
    )

    normal_ids = [hit.memory_id for hit in normal.hits]
    reversed_ids = [hit.memory_id for hit in reversed_result.hits]
    assert normal_ids == reversed_ids, (
        "候选池 SQL 行序改变了 recall 的最终顺序：\n"
        f"  正序: {[hit.text for hit in normal.hits]}\n"
        f"  反序: {[hit.text for hit in reversed_result.hits]}"
    )
    assert [hit.text for hit in normal.hits] == [
        hit.text for hit in reversed_result.hits
    ]
    assert normal.hits[0].text == semantic_text, (
        f"语义命中最相关的记忆没排第一: {[hit.text for hit in normal.hits]}"
    )
    assert reversed_result.hits[0].text == semantic_text, (
        f"反转行序后语义命中不再排第一: {[hit.text for hit in reversed_result.hits]}"
    )
