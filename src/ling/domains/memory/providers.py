"""记忆检索的向量/重排提供方装配（按环境变量，未配置则降级）。

**为什么不把密钥写进代码或配置快照**：provider 凭据泄漏一次就要全部轮换。
这里只从环境变量读，且不落库、不写日志。``env.example`` 里只留空占位。

未配置时的行为：
- 没有 embedding → 检索退回 BM25 + RRF（字面路径），功能不残缺
- 没有 rerank → 跳过精排，RRF 结果直接用

这就是参照实现强调的"语义路径默认关闭、需要明确配置后才启用"。
"""

from __future__ import annotations

import os


def build_embedding():  # noqa: ANN201
    """按环境变量构造向量提供方；未配置返回 None。"""
    key = os.environ.get("LING_EMBEDDING_API_KEY")
    account = os.environ.get("LING_EMBEDDING_ACCOUNT_ID")
    model = os.environ.get("LING_EMBEDDING_MODEL", "@cf/baai/bge-m3")
    base = os.environ.get("LING_EMBEDDING_API_BASE")
    if not key:
        return None
    try:
        from ling.models.cloudflare_embedding import CloudflareEmbedding

        if not account and not base:
            return None
        return CloudflareEmbedding(
            account_id=account or "", api_key=key, model=model, base_url=base
        )
    except Exception:  # noqa: BLE001 - 装配失败按未配置处理
        return None


def build_reranker():  # noqa: ANN201
    """按环境变量构造重排提供方；未配置返回 None。"""
    key = os.environ.get("LING_RERANK_API_KEY")
    if not key:
        return None
    try:
        from ling.models.siliconflow_rerank import SiliconFlowRerank

        return SiliconFlowRerank(
            api_key=key,
            model=os.environ.get("LING_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
            base_url=os.environ.get(
                "LING_RERANK_API_BASE", "https://api.siliconflow.cn/v1"
            ),
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = ["build_embedding", "build_reranker"]
