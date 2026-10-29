"""语义检索（向量 + 重排）的接线与降级。

用假 provider 做确定性验证：不依赖外部 API，也不会因网络波动误报。
真实 provider 的可用性由单独的手工验证脚本覆盖（不进测试套件）。
"""

from __future__ import annotations

import pytest

from ling.domains.memory.service import RERANK_TOP_N, _cosine
from ling.ports.embedding import EmbeddingError


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


class FakeReranker:
    def __init__(self, order: list[int] | None = None) -> None:
        self.model_id = "fake-rerank"
        self._order = order
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, documents: list[str], *, top_n: int | None = None):
        self.calls.append((query, list(documents)))
        if self._order is None:
            return [(i, 1.0 / (i + 1)) for i in range(len(documents))]
        return [(i, 1.0 - i * 0.1) for i in self._order if i < len(documents)]


class BoomEmbedder:
    model_id = "boom"
    dim = 2

    async def embed(self, texts: list[str]):  # noqa: ANN201
        raise EmbeddingError("上游挂了")


class BoomReranker:
    model_id = "boom"

    async def rerank(self, query: str, documents: list[str], *, top_n: int | None = None):
        raise EmbeddingError("上游挂了")


def test_cosine_basics() -> None:
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([], [1, 0]) == 0.0
    assert _cosine([1, 0], [1, 0, 0]) == 0.0  # 维度不一致直接判 0


def test_rerank_top_n_is_bounded() -> None:
    """精排只作用于 top-N：重排 API 比 embedding 贵且慢。"""
    assert RERANK_TOP_N == 20
