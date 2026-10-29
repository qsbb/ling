"""SiliconFlow 的 BGE reranker。

重排只作用于候选集（top-N），比 embedding 贵，用于精排。
实测对"我有什么忌口"这类查询区分度很高（相关条目 0.22 vs 无关 0.00002）。

注意响应里 ``document`` 字段为 null，只能靠 ``index`` 对回原顺序——
不能假设提供方会回显文本。
"""

from __future__ import annotations

import httpx

from ling.ports.embedding import EmbeddingError


class SiliconFlowRerank:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout_seconds: float = 20.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model
        self._key = api_key
        self._base = base_url
        self._timeout = timeout_seconds
        self._http = http

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.post(
                    f"{self._base.rstrip('/')}/rerank",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self.model_id,
                        "query": query,
                        "documents": documents,
                        "top_n": top_n or len(documents),
                    },
                )
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"重排不可达: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise EmbeddingError(
                    f"重排失败 {response.status_code}: {response.text[:160]}"
                )
            try:
                body = response.json()
                results = body.get("results") or []
                pairs = [
                    (int(r["index"]), float(r["relevance_score"])) for r in results
                ]
            except (ValueError, KeyError, TypeError) as exc:
                raise EmbeddingError("重排响应格式不可解析") from exc
        finally:
            if owns:
                await client.aclose()
        pairs.sort(key=lambda p: -p[1])
        return pairs


__all__ = ["SiliconFlowRerank"]
