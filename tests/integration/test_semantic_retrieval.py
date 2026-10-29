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
