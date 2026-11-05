"""向量与重排端口。

参照实现（MaiBot A-Memorix 双路检索、某同类实现的
Embedding 候选 + Rerank 二阶段）都把"语义候选 + 重排"作为质量关键。
这里把两者抽象成端口，具体提供方按配置注入，未配置时自动降级。

设计取舍：
- **embedding 用批量接口**：一次请求嵌多条，避免逐条往返
- **rerank 只对候选集做**：它比 embedding 贵且慢，用于精排 top-N
- 两者都可缺省：缺失时检索退回 BM25 + RRF 纯字面路径
"""

from __future__ import annotations

from typing import Protocol


class EmbeddingError(Exception):
    """向量化失败。调用方应降级而不是让检索整体失败。"""


class EmbeddingProvider(Protocol):
    """把文本嵌成稠密向量。"""

    model_id: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回顺序与输入一致。"""
        ...


class RerankProvider(Protocol):
    """对候选集重排。"""

    model_id: str

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """返回 (原下标, 相关性分数)，按分数降序。"""
        ...


__all__ = ["EmbeddingError", "EmbeddingProvider", "RerankProvider"]
