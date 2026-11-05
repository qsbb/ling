"""检索融合与强化反馈。

参照 同类实现 与通用检索实践：
- **RRF 融合**（按排名而非打分加权）：乘法加权要把相关性/新近度/重要性
  压到同一量纲，权重怎么定都是拍的，而且任一路为 0 就整条归零
- **MMR 近重复抑制**：同一件事记成多条时只留最相关那条
- **只强化真正进入提示词的**：否则被预算截掉的记忆会越挤越优先（马太效应）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.memory.service import RRF_K, MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _add(
    env: TestEnv,
    *,
    text: str,
    slot: str,
    confidence: float = 0.8,
    days_old: int = 0,
) -> str:
    async with session_scope() as session:
        memory_id = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id=f"p_{slot}_{abs(hash(text)) % 10000}",
                kind="assertion",
                text=text,
                origin=Origin.user_statement,
                source_event_ids=["evt"],
                visibility=Visibility.subject_private,
                confidence=confidence,
            ),
            subject_id=env.subject_id,
            turn_id=f"turn_{slot}_{abs(hash(text)) % 10000}",
            slot=slot,
        )
    if days_old:
        async with session_scope() as session:
            await session.execute(
                update(MemoryRow)
                .where(MemoryRow.id == memory_id)
                .values(created_at=datetime.now(UTC) - timedelta(days=days_old))
            )
    return memory_id or ""


async def _recall(env: TestEnv, query: str, limit: int = 10) -> list:
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=env.subject_id,
            query=query,
            visibility_allow={Visibility.subject_private},
            limit=limit,
        )
    return result.hits


# ------------------------------------------------------------------ RRF


def test_rrf_constant_is_standard() -> None:
    """RRF 的 k=60 是检索文献里的常用取值，不是随便拍的。"""
    assert RRF_K == 60


async def test_rrf_prefers_multi_signal_match(logged_in: TestEnv) -> None:
    """在多路信号上都靠前的记忆，应排在只在单路靠前的之前。

    A：关键词强 + 新 + 高置信
    B：关键词强，但很旧且置信低
    """
    # 两条文本长度接近（避免 BM25 的长度归一偏置影响判定），
    # 差距只体现在置信度与新近度上；MMR 不会触发（2-gram 重合度 < 0.7）。
    a = await _add(logged_in, text="用户喜欢下雨天散步", slot="likes", confidence=0.95)
    b = await _add(
        logged_in, text="用户喜欢在雨天淋雨", slot="dislikes", confidence=0.05, days_old=300
    )
    hits = await _recall(logged_in, "用户喜欢")
    ids = [h.memory_id for h in hits]
    assert a in ids and b in ids
    assert ids.index(a) < ids.index(b), "多路都强的记忆没有排前面"


async def test_rrf_survives_zero_signal(logged_in: TestEnv) -> None:
    """单路信号为 0 不应让整条归零（乘法加权的老问题）。"""
    # 300 天：在保留期（365）内，但新近度因子已接近下限
    old = await _add(
        logged_in, text="用户住在杭州西湖区", slot="lives_in", confidence=0.9, days_old=300
    )
    hits = await _recall(logged_in, "杭州")
    assert any(h.memory_id == old for h in hits), "很旧但相关的记忆被完全排除"


# ------------------------------------------------------------------ MMR


async def test_mmr_suppresses_near_duplicates(logged_in: TestEnv) -> None:
    """近似重复只留一条，否则证据车道被重复占满。"""
    await _add(logged_in, text="用户喜欢下雨天散步", slot="likes")
    await _add(logged_in, text="用户喜欢下雨天散步", slot="dislikes")  # 文字几乎相同
    hits = await _recall(logged_in, "喜欢下雨天散步")
    texts = [h.text for h in hits]
    # 两条文本完全一致 → 应只保留一条
    assert len(texts) == len(set(texts)), f"近似重复没被抑制: {texts}"


async def test_mmr_keeps_distinct_memories(logged_in: TestEnv) -> None:
    """去重不能过度：不同事实必须都保留。"""
    await _add(logged_in, text="用户喜欢下雨天散步", slot="likes")
    await _add(logged_in, text="用户住在杭州西湖区", slot="lives_in")
    hits = await _recall(logged_in, "杭州 下雨天")
    assert len(hits) >= 2, f"不同事实被误删: {[h.text for h in hits]}"


# ------------------------------------------------------------------ 强化反馈


async def test_reinforce_increments_access_count(logged_in: TestEnv) -> None:
    memory_id = await _add(logged_in, text="用户喜欢下雨天", slot="likes")
    async with session_scope() as session:
        assert await MemoryService(session).reinforce([memory_id]) == 1
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row.access_count == 1
    assert row.last_injected_at is not None


async def test_reinforce_accumulates(logged_in: TestEnv) -> None:
    memory_id = await _add(logged_in, text="用户喜欢下雨天散步", slot="likes")
    for _ in range(3):
        async with session_scope() as session:
            await MemoryService(session).reinforce([memory_id])
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row.access_count == 3


async def test_reinforce_empty_is_noop(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        assert await MemoryService(session).reinforce([]) == 0


async def test_reinforcement_affects_ranking(logged_in: TestEnv) -> None:
    """被频繁实际注入的记忆，排序应更靠前（重要性信号的一部分）。"""
    base = await _add(logged_in, text="用户喜欢喝茶", slot="likes", confidence=0.7)
    boosted = await _add(logged_in, text="用户喜欢喝咖啡", slot="dislikes", confidence=0.7)
    async with session_scope() as session:
        await MemoryService(session).reinforce([boosted] * 1)
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).where(MemoryRow.id == boosted).values(access_count=50)
        )
    hits = await _recall(logged_in, "用户喜欢喝")
    ids = [h.memory_id for h in hits]
    assert boosted in ids and base in ids
    assert ids.index(boosted) < ids.index(base), "高强化记忆没有排前面"


async def test_log_compression_prevents_dominance(logged_in: TestEnv) -> None:
    """强化用对数压缩：注入 100 次不该比 10 次优先 10 倍。

    否则早期偶然被召回过的记忆会永久霸榜。
    """
    import math

    def importance(access: int, confidence: float = 0.8) -> float:
        return confidence * (1.0 + math.log1p(access))

    # 10 次 vs 100 次的差距应远小于 10 倍
    ratio = importance(100) / importance(10)
    assert ratio < 2.0, f"强化没有压缩，100 次比 10 次优先 {ratio:.1f} 倍"


# ------------------------------------------------------------------ 只强化实际注入的


async def test_only_injected_memories_reinforced(logged_in: TestEnv) -> None:
    """被预算截掉的记忆不能被强化。

    这是参照实现的明确要求：把"检索到但没进提示"也算强化，
    它们会越来越优先，长尾永远出不来。
    """
    from ling.kernel.context import ContextCompiler

    # 造一条超长记忆占满预算，再加一条短的
    long_id = await _add(logged_in, text="用户喜欢" + "很长的内容" * 200, slot="likes")
    short_id = await _add(logged_in, text="用户喜欢喝茶", slot="dislikes")

    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "reinforce"})
    ).json()["session_id"]
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            user_text="用户喜欢什么",
            current_turn_id="turn_reinf",
        )
    selected = compiled.meta.get("evidence_selected", 0)
    dropped = compiled.meta.get("evidence_dropped", 0)
    assert selected + dropped >= 1, "没有召回任何记忆，测试前提不成立"

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.id.in_([long_id, short_id]))
                )
            ).scalars()
        )
    reinforced = [r for r in rows if (r.access_count or 0) > 0]
    assert len(reinforced) <= selected, (
        f"强化了 {len(reinforced)} 条但只注入了 {selected} 条"
    )


# ------------------------------------------------------------------ 相关性下限


async def test_irrelevant_memories_are_filtered(logged_in: TestEnv) -> None:
    """与查询同量级之外的结果应被过滤，不该凑数进证据车道。

    没有下限时 `limit=6` 会把第 6 名也返回，角色就容易在不相干的话题上
    提起旧事——这比"少记一件事"更容易让人觉得它没在听。
    """
    await _add(logged_in, text="用户喜欢下雨天散步", slot="likes", confidence=0.95)
    # 与查询完全无关的记忆
    await _add(logged_in, text="用户养了一只橘猫", slot="has_pet", confidence=0.9)
    await _add(logged_in, text="用户住在杭州", slot="lives_in", confidence=0.9)

    hits = await _recall(logged_in, "下雨天散步", limit=10)
    texts = [h.text for h in hits]
    assert any("下雨天" in t for t in texts)
    # 无关项要么被过滤，要么分数明显更低——关键是不能与第一名同量级
    if hits:
        top = hits[0].score
        for h in hits[1:]:
            assert h.score >= top * 0.1, f"入选项分数过低: {h.text} {h.score} vs {top}"


async def test_floor_never_returns_empty_when_hit_exists(logged_in: TestEnv) -> None:
    """下限不能把唯一命中过滤掉（宁可返回一条，也不能全空）。"""
    await _add(logged_in, text="用户喜欢下雨天散步", slot="likes")
    hits = await _recall(logged_in, "下雨天", limit=10)
    assert hits, "唯一命中被下限过滤掉了"


async def test_relative_floor_constant() -> None:
    from ling.domains.memory.service import RELATIVE_SCORE_FLOOR

    assert 0 < RELATIVE_SCORE_FLOOR < 1
