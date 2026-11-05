"""④ 记忆与学习：保留期裁决与"没配重排就不生效"的纯单元回归。

这些不需要数据库：它们钉的是**判定函数**本身，跑得快，失败信息直接指向
"谁赢了"（策略上限 vs 行内值）。集成层面的"发布后真的变"在
`tests/integration/test_memory_policy.py`。
"""

from __future__ import annotations

import logging

from ling.domains.memory import service as memory_service
from ling.domains.memory.service import MemoryService
from ling.ports.config import MemoryPolicy


class _Row:
    """`effective_retention_days` 只需要一个带 retention_days 的对象。"""

    def __init__(self, retention_days: int | None) -> None:
        self.retention_days = retention_days


def _service(**kwargs: object) -> MemoryService:
    return MemoryService(None, embedder=None, reranker=None, **kwargs)  # type: ignore[arg-type]


def test_default_policy_matches_contract_defaults() -> None:
    """默认值就是 `MemoryPolicy()`——不配置时行为等于抽常量之前。"""
    policy = _service().policy
    assert policy == MemoryPolicy()
    assert policy.relative_score_floor == 0.35
    assert policy.rerank_top_n == 20
    assert policy.recency_half_life_days == 120.0
    assert policy.max_retention_days == 3650


def test_write_stamps_the_policy_value_not_the_column_default() -> None:
    """写入时按当时的策略盖章。

    列默认是 365、策略默认是 3650——旧实现让行永远盖 365，
    于是"设置里填 3650"永远不生效。盖章必须取策略值。
    """
    assert _service().write_retention_days() == 3650
    assert _service(policy=MemoryPolicy(max_retention_days=30)).write_retention_days() == 30


def test_policy_is_a_hard_cap_over_row_value() -> None:
    """策略上限压过行内值：行内 3650 + 策略 30 → 30（调小立即生效）。"""
    service = _service(policy=MemoryPolicy(max_retention_days=30))
    assert service.effective_retention_days(_Row(3650)) == 30
    assert service.effective_retention_days(_Row(30)) == 30


def test_row_value_may_be_shorter_than_the_cap() -> None:
    """行内值可以更短：行内 10 + 策略 3650 → 10（显式的更严请求被尊重）。"""
    service = _service(policy=MemoryPolicy(max_retention_days=3650))
    assert service.effective_retention_days(_Row(10)) == 10


def test_row_without_value_falls_back_to_the_policy() -> None:
    """旧行/NULL 不能被当成 365：兜底用策略值，再被上限压一次。"""
    assert (
        _service(policy=MemoryPolicy(max_retention_days=1000)).effective_retention_days(_Row(None))
        == 1000
    )


def test_rerank_inactive_is_logged_once_when_no_reranker(caplog, monkeypatch) -> None:  # noqa: ANN001
    """没配重排模型时，必须明说 `rerank_top_n` 不生效（每进程只喊一次）。"""
    monkeypatch.setattr(memory_service, "_RERANK_INACTIVE_WARNED", False)
    with caplog.at_level(logging.INFO, logger="ling.domains.memory"):
        _service(policy=MemoryPolicy(rerank_top_n=7))
        _service(policy=MemoryPolicy(rerank_top_n=7))
    messages = [r.message for r in caplog.records if "rerank_top_n" in r.message]
    assert len(messages) == 1, f"应当只喊一次，实际 {len(messages)} 次：{messages}"
    assert "不生效" in messages[0]
    assert "7" in messages[0]


def test_rerank_warning_is_skipped_when_reranker_present(caplog, monkeypatch) -> None:  # noqa: ANN001
    """配了重排模型就不该喊"不生效"（否则真配置了的人也被误导）。"""
    monkeypatch.setattr(memory_service, "_RERANK_INACTIVE_WARNED", False)

    class _Reranker:
        async def rerank(self, query: str, texts: list[str], *, top_n: int):  # noqa: ANN201
            return []

    with caplog.at_level(logging.INFO, logger="ling.domains.memory"):
        MemoryService(None, embedder=object(), reranker=_Reranker())  # type: ignore[arg-type]
    assert not [r for r in caplog.records if "不生效" in r.message]
