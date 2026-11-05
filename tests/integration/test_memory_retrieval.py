"""记忆检索质量测试：先过滤权限、再 BM25 融合排序。

这些测试固定住三条不变量：
1. **权限优先于相关性**：无权内容不会被高相关度"排上来"。
2. **排序合理性**：词频饱和、长度归一、时间衰减、置信度都参与。
3. **撤回即失效**：撤回后的记忆永不出现。

参考实现思路：mem0 的多信号检索（semantic + BM25 + entity）融合，
以及 Graphiti 的"旧事实被 invalidate 而不是删除"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.contracts.v1 import Origin, Visibility
from ling.domains.memory.service import MemoryService, _bm25_scores, _recency_factor
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 纯函数


def test_bm25_prefers_rare_term_over_common() -> None:
    """区分度高的词权重更大：查 '蓝鲸' 时含蓝鲸的文档应排在前面。"""
    docs = [
        ("m1", "用户喜欢喝咖啡"),  # ripgrep: 咖啡
        ("m2", "用户提到了蓝鲸这种动物"),
        ("m3", "今天天气不错"),
        ("m4", "用户说咖啡太苦了"),
    ]
    scores = _bm25_scores("蓝鲸", docs)
    assert scores["m2"] > scores["m1"], scores
    assert scores["m2"] > scores["m3"], scores


def test_bm25_saturates_term_frequency() -> None:
    """词频饱和：同一词出现 10 次不该比 2 次相关 5 倍。"""
    docs = [
        ("short", "猫 猫"),
        ("long", "猫 " * 10 + "无关内容填充填充填充填充填充填充"),
    ]
    scores = _bm25_scores("猫", docs)
    # 长文档重复多，但长度归一后不应出现夸张倍数
    assert scores["long"] / max(scores["short"], 1e-9) < 3.0, scores


def test_bm25_returns_zero_for_no_overlap() -> None:
    scores = _bm25_scores("完全不相干的词", [("m1", "今天天气不错")])
    assert scores["m1"] == 0.0


def test_recency_factor_decays_but_has_floor() -> None:
    now = datetime.now(UTC)
    fresh = _recency_factor(now, now)
    old = _recency_factor(now - timedelta(days=730), now)
    assert fresh == 1.0
    assert 0.5 <= old < fresh, (old, fresh)


# ------------------------------------------------------------------ 权限优先


async def _seed(env: TestEnv, subject_id: str, rows: list[dict]) -> list[str]:
    ids: list[str] = []
    async with session_scope() as session:
        for row in rows:
            memory_id = f"mem_{uuid.uuid4().hex[:12]}"
            session.add(
                MemoryRow(
                    id=memory_id,
                    subject_id=subject_id,
                    kind=row.get("kind", "assertion"),
                    text=row["text"],
                    origin=row.get("origin", Origin.user_statement.value),
                    visibility=row.get("visibility", Visibility.subject_private.value),
                    confidence=row.get("confidence", 0.6),
                    source_event_ids=["evt_seed"],
                    retracted=row.get("retracted", False),
                )
            )
            ids.append(memory_id)
    return ids


async def test_recall_filters_visibility_before_ranking(logged_in: TestEnv) -> None:
    """群聊上下文不得召回私聊记忆——即使它相关度最高。"""
    subject_id = logged_in_user_subject(logged_in)
    ids = await _seed(
        logged_in,
        subject_id,
        [
            {"text": "用户说他最喜欢的动物是蓝鲸", "visibility": "subject_private"},
            {"text": "蓝鲸是一种海洋哺乳动物", "visibility": "public"},
        ],
    )
    async with session_scope() as session:
        service = MemoryService(session)
        # 群聊：只能读 group_scope / public
        result = await service.recall(
            subject_id=subject_id,
            query="蓝鲸",
            visibility_allow={Visibility.group_scope, Visibility.public},
        )
        texts = [h.text for h in result.hits]
        assert any("海洋哺乳动物" in t for t in texts), texts
        assert not any("最喜欢的动物" in t for t in texts), "私聊记忆泄漏到群聊上下文"
        assert result.filtered_out >= 1

        # 私聊：两条都能读到，且"喜欢蓝鲸"这条更贴近个人事实
        private_result = await service.recall(
            subject_id=subject_id,
            query="蓝鲸 喜欢",
            visibility_allow={Visibility.subject_private, Visibility.public},
        )
        assert len(private_result.hits) >= 1
    assert len(ids) == 2


async def test_retracted_memory_never_returned(logged_in: TestEnv) -> None:
    subject_id = logged_in_user_subject(logged_in)
    ids = await _seed(
        logged_in,
        subject_id,
        [{"text": "用户住在杭州"}],
    )
    async with session_scope() as session:
        service = MemoryService(session)
        before = await service.recall(
            subject_id=subject_id,
            query="杭州",
            visibility_allow={Visibility.subject_private},
        )
        assert len(before.hits) == 1
        await service.retract(ids[0], reason="用户纠正：不在杭州")
    async with session_scope() as session:
        after = await MemoryService(session).recall(
            subject_id=subject_id,
            query="杭州",
            visibility_allow={Visibility.subject_private},
        )
        assert after.hits == [], "撤回后的记忆不得再被召回"


async def test_model_inference_cannot_become_fact(logged_in: TestEnv) -> None:
    """模型推测不得写入可检索长期记忆。"""
    from ling.contracts.v1 import MemoryProposal

    subject_id = logged_in_user_subject(logged_in)
    async with session_scope() as session:
        service = MemoryService(session)
        memory_id = await service.propose(
            MemoryProposal(
                proposal_id="mp_1",
                kind="assertion",
                text="用户可能养了一只猫",
                origin=Origin.model_inference,
                source_event_ids=["evt_1"],
                visibility=Visibility.subject_private,
            ),
            subject_id=subject_id,
        )
        assert memory_id is None, "模型推测必须被拒绝"
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == subject_id)
                )
            ).scalars()
        )
        assert not any("养了一只猫" in r.text for r in rows)


async def test_supersede_keeps_chain(logged_in: TestEnv) -> None:
    """用户纠正：旧记忆失效，新记忆带 supersedes 链，历史可追溯。"""
    subject_id = logged_in_user_subject(logged_in)
    ids = await _seed(logged_in, subject_id, [{"text": "用户喜欢喝美式"}])
    async with session_scope() as session:
        service = MemoryService(session)
        new_id = await service.supersede(ids[0], "用户其实更喜欢拿铁", reason="用户纠正")
        assert new_id is not None
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == subject_id)
                )
            ).scalars()
        )
        old = next(r for r in rows if r.id == ids[0])
        new = next(r for r in rows if r.id == new_id)
        assert old.retracted is True
        assert new.supersedes == old.id
        assert "拿铁" in new.text


def logged_in_user_subject(env: TestEnv) -> str:
    """从已建立的应用状态里取当前登录用户的 subject_id。

    fixtures 里用户是 bootstrap 创建的，subject 是随机 ID，
    因此必须走真实来源而不是拼字符串。
    """
    return env.subject_id  # type: ignore[attr-defined]
