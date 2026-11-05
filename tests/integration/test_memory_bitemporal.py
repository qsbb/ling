"""双时间轴：改口后按新值回答，问过去仍能答出。

参照 Zep/Graphiti 的双时间轴——**事实有效期**与**系统记录时间**分开。
不做的后果（排第 3 的缺口）：
- 用户说「我住杭州」后来「我住苏州了」，两条同时有效 → 模型可能挑旧的答
- 又问「我以前住哪」，旧值已被覆盖 → 答不出

实现要点：
- 单值槽位（name/lives_in/works_at/birthday/health）新值生效即关闭旧值
- 旧行**不删**，只设 `valid_until`，供"问过去"使用
- 只有当提问含「以前/之前/原来」等标记时才把历史带进上下文
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.memory.service import (
    SINGLE_VALUE_SLOTS,
    MemoryService,
    is_single_value,
    render_evidence,
    wants_history,
)
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _assert_fact(
    env: TestEnv, *, slot: str, value: str, text: str | None = None, turn: str
) -> str:
    async with session_scope() as session:
        return (
            await MemoryService(session).propose(
                MemoryProposal(
                    proposal_id=f"bt_{turn}",
                    kind="assertion",
                    text=text or f"用户住在{value}",
                    origin=Origin.user_statement,
                    source_event_ids=["e"],
                    visibility=Visibility.subject_private,
                    confidence=0.9,
                ),
                subject_id=env.subject_id,
                turn_id=turn,
                slot=slot,
                value=value,
            )
        ) or ""


# ------------------------------------------------------------------ 单值 vs 多值


def test_single_value_slots() -> None:
    assert is_single_value("lives_in")
    assert is_single_value("name")
    assert not is_single_value("likes")
    assert not is_single_value("habit")
    assert "lives_in" in SINGLE_VALUE_SLOTS


# ------------------------------------------------------------------ 改口


async def test_new_value_closes_old(logged_in: TestEnv) -> None:
    """改口后旧值必须被关闭（否则两条同时有效，模型可能挑旧的）。"""
    old = await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt1")
    await _assert_fact(logged_in, slot="lives_in", value="苏州", turn="bt2")

    async with session_scope() as session:
        row = await session.get(MemoryRow, old)
    assert row is not None
    assert row.valid_until is not None, "旧值没有被关闭"
    assert row.valid_until <= datetime.now(UTC)


async def test_old_value_kept_for_history(logged_in: TestEnv) -> None:
    """旧行不能删——否则"你以前住哪"永远答不出。"""
    old = await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt3")
    await _assert_fact(logged_in, slot="lives_in", value="苏州", turn="bt4")

    async with session_scope() as session:
        row = await session.get(MemoryRow, old)
    assert row is not None, "旧行被物理删除"
    assert row.text and "杭州" in row.text


async def test_current_recall_excludes_old(logged_in: TestEnv) -> None:
    """默认召回只给现状，不带旧值。"""
    await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt5")
    await _assert_fact(logged_in, slot="lives_in", value="苏州", turn="bt6")

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="用户住在",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    texts = [h.text for h in result.hits]
    assert any("苏州" in t for t in texts)
    assert not any("杭州" in t for t in texts), f"旧住址出现在现状召回里: {texts}"


async def test_history_recall_includes_old(logged_in: TestEnv) -> None:
    """问过去时旧值要能召回。"""
    await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt7")
    await _assert_fact(logged_in, slot="lives_in", value="苏州", turn="bt8")

    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="用户住在",
            visibility_allow={Visibility.subject_private},
            limit=5,
            include_history=True,
        )
    texts = [h.text for h in result.hits]
    assert any("杭州" in t for t in texts), "问过去时旧值没被召回"
    assert any("苏州" in t for t in texts)


async def test_multi_value_slots_not_closed(logged_in: TestEnv) -> None:
    """多值槽位绝不互相取代（喜欢的东西可以有多个）。"""
    a = await _assert_fact(logged_in, slot="likes", value="喝茶", text="用户喜欢喝茶", turn="bt9")
    await _assert_fact(logged_in, slot="likes", value="咖啡", text="用户喜欢咖啡", turn="bt10")

    async with session_scope() as session:
        row = await session.get(MemoryRow, a)
    assert row is not None and row.valid_until is None, "多值槽位被错误关闭"


async def test_same_value_not_closed(logged_in: TestEnv) -> None:
    """重复说同一件事不算改口，不该关闭。"""
    a = await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt11")
    await _assert_fact(logged_in, slot="lives_in", value="杭州", turn="bt12")
    async with session_scope() as session:
        row = await session.get(MemoryRow, a)
    assert row is not None and row.valid_until is None


# ------------------------------------------------------------------ 意图判定


@pytest.mark.parametrize("text", ["你以前住哪", "我之前说过什么", "原来的名字", "过去的事"])
def test_wants_history_true(text: str) -> None:
    assert wants_history(text)


@pytest.mark.parametrize("text", ["你好", "今天吃什么", "我住在哪"])
def test_wants_history_false(text: str) -> None:
    assert not wants_history(text)


def test_history_rendered_as_past(logged_in: object) -> None:
    """历史事实必须单独成组，不能混进"可以直接说"里。"""
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryHit

    hit = MemoryHit(
        memory_id="m1", kind="assertion", text="用户住在杭州",
        origin=Origin.user_statement, visibility=Visibility.subject_private,
        confidence=0.9, source_event_ids=[], score=0.1,
        valid_until=datetime(2020, 1, 1, tzinfo=UTC),  # 已过期
    )
    text = render_evidence([hit])
    assert "以前" in text
    assert "现在已经不是这样" in text
    assert "可以直接说出来" not in text, "历史事实被混进了可直接表述的组"
