"""向量预计算：把「检索时同步补」的积压挪到后台。

## 为什么

召回路径上的补向量是**一次外部 HTTP 调用一条**，而它发生在用户等回复的时候。
积压 500 条时，一次召回就是 500 次串行请求——这是本项目离得最近的性能悬崖。

两道防线：

1. **止血**：`_ensure_embeddings_for` 有一次最多补 8 条的硬上限，
   超出部分本次语义路不完整，但不拖死回复
2. **治本**：`embedding.backfill` 在后台预计算，让积压根本不存在

本文件盯住这两道都不退化成假的。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling.domains.memory.service import MAX_LAZY_EMBEDS
from ling.infra.db import session_scope
from ling.infra.models import MemoryEmbeddingRow, MemoryRow
from ling.worker import handle_embedding_backfill
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
MODEL_ID = "stub-embed-v1"


class _StubEmbedder:
    """固定维度的假 embedder，记录被要求嵌入多少条。"""

    model_id = MODEL_ID

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


async def _clean() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(MemoryEmbeddingRow).where(MemoryEmbeddingRow.model_id == MODEL_ID)
        )
        await session.execute(
            delete(MemoryRow).where(MemoryRow.id.like("mem_bf_%"))
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _seed_memories(n: int, *, prefix: str = "mem_bf") -> list[str]:
    ids: list[str] = []
    async with session_scope() as session:
        for i in range(n):
            mid = f"{prefix}_{i:03d}"
            session.add(
                MemoryRow(
                    id=mid,
                    subject_id="subj_embed_test",
                    kind="assertion",
                    text=f"第 {i} 条需要向量化的记忆",
                    origin="user_statement",
                    visibility="subject_private",
                )
            )
            ids.append(mid)
    return ids


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    import ling.domains.memory.providers as providers

    stub = _StubEmbedder()
    monkeypatch.setattr(providers, "build_embedding", lambda: stub)
    return stub


# ---------------------------------------------------------------- 治本


async def test_backfill_embeds_missing(logged_in: TestEnv, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """后台把缺向量的记忆补上。"""
    ids = await _seed_memories(5)
    stub = _install_stub(monkeypatch)

    await handle_embedding_backfill({})

    assert stub.batches, "没有调用 embedder"
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MemoryEmbeddingRow).where(
                    MemoryEmbeddingRow.model_id == MODEL_ID
                )
            )
        ).scalars().all()
    stored = {r.memory_id for r in rows}
    for mid in ids:
        assert mid in stored, f"{mid} 没有补上向量"


async def test_backfill_is_idempotent(logged_in: TestEnv, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """重复跑不会重复写——已存在 (memory_id, model_id) 直接跳过。"""
    await _seed_memories(3)
    stub = _install_stub(monkeypatch)

    await handle_embedding_backfill({})
    first = len(stub.batches)
    assert first >= 1

    await handle_embedding_backfill({})
    # 第二次没有任何缺口，不该再调 embedder
    assert len(stub.batches) == first, "重复补算了"

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MemoryEmbeddingRow).where(
                    MemoryEmbeddingRow.model_id == MODEL_ID
                )
            )
        ).scalars().all()
    assert len(rows) == len({r.memory_id for r in rows}), "写重了"


async def test_backfill_skips_without_provider(logged_in: TestEnv, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """没配 embedding 提供方时安静跳过，不报错、不写空向量。"""
    import ling.domains.memory.providers as providers

    monkeypatch.setattr(providers, "build_embedding", lambda: None)
    await _seed_memories(2)
    await handle_embedding_backfill({})

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MemoryEmbeddingRow).where(
                    MemoryEmbeddingRow.model_id == MODEL_ID
                )
            )
        ).scalars().all()
    assert rows == []


async def test_backfill_respects_batch_size(logged_in: TestEnv, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """一次只补 batch 条，不能一口气打几百次 HTTP。"""
    await _seed_memories(20)
    stub = _install_stub(monkeypatch)

    await handle_embedding_backfill({"batch": 5})

    assert stub.batches
    assert len(stub.batches[0]) <= 5, f"一次嵌入 {len(stub.batches[0])} 条，超过 batch=5"


# ---------------------------------------------------------------- 止血


def test_lazy_embedding_has_a_hard_cap() -> None:
    """懒补齐必须有硬上限。

    没有上限时，积压多少就在召回路径上串行打多少次外部 HTTP——
    而用户正在等这一条回复。这个常量是那道止血阀。
    """
    assert MAX_LAZY_EMBEDS > 0
    assert MAX_LAZY_EMBEDS <= 20, "上限太松，挡不住积压"


async def test_recall_only_embeds_up_to_cap(logged_in: TestEnv, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """积压超过上限时，一次召回最多同步补 MAX_LAZY_EMBEDS 条。"""
    from ling.domains.memory.service import MemoryService

    await _seed_memories(MAX_LAZY_EMBEDS + 12)
    stub = _install_stub(monkeypatch)

    async with session_scope() as session:
        service = MemoryService(session, embedder=stub, reranker=None)
        rows = (
            await session.execute(
                select(MemoryRow).where(MemoryRow.id.like("mem_bf_%"))
            )
        ).scalars().all()
        await service._ensure_embeddings_for(rows)  # noqa: SLF001

    assert stub.batches, "没有触发补向量"
    embedded = sum(len(b) for b in stub.batches)
    assert embedded <= MAX_LAZY_EMBEDS, f"同步补了 {embedded} 条，超过上限"
