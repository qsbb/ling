"""Cloudflare Workers AI 的 BGE-M3 向量化。

端点是 OpenAI 兼容的 ``/ai/v1/embeddings``，所以请求形状与 openai 一致。
BGE-M3 是**多语言**模型（中英混排的中文对话场景比纯英文模型稳），
1024 维，实测区分度可用。
"""

from __future__ import annotations

import httpx

from ling.ports.embedding import EmbeddingError

# 一次请求最多嵌多少条：太少浪费往返，太多单次失败面变大
MAX_BATCH = 32


class CloudflareEmbedding:
    """``@cf/baai/bge-m3`` 向量化。"""

    def __init__(
        self,
        *,
        account_id: str,
        api_key: str,
        model: str = "@cf/baai/bge-m3",
        base_url: str | None = None,
        timeout_seconds: float = 20.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model
        self.dim = 1024
        self._account = account_id
        self._key = api_key
        self._timeout = timeout_seconds
        self._http = http
        self._base = (
            base_url
            or f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            for start in range(0, len(texts), MAX_BATCH):
                batch = texts[start : start + MAX_BATCH]
                try:
                    response = await client.post(
                        f"{self._base.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {self._key}"},
                        json={"model": self.model_id, "input": batch},
                    )
                except httpx.HTTPError as exc:
                    raise EmbeddingError(
                        f"向量化不可达: {type(exc).__name__}"
                    ) from exc
                if response.status_code != 200:
                    raise EmbeddingError(
                        f"向量化失败 {response.status_code}: {response.text[:160]}"
                    )
                try:
                    body = response.json()
                    data = body.get("data") or []
                    # 按 index 排序，保证与输入顺序一致（不同提供方顺序不保证）
                    data = sorted(data, key=lambda d: d.get("index", 0))
                    out.extend([list(d["embedding"]) for d in data])
                except (ValueError, KeyError, TypeError) as exc:
                    raise EmbeddingError("向量化响应格式不可解析") from exc
        finally:
            if owns:
                await client.aclose()
        if len(out) != len(texts):
            raise EmbeddingError(f"向量条数不匹配: 期望 {len(texts)}，得到 {len(out)}")
        return out


__all__ = ["CloudflareEmbedding"]
