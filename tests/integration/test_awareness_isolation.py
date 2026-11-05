"""会话隔离模式与跨会话意识（ADR-0004）。

核心不变量：
1. sealed：会话完全隔离，不读长期记忆、不积累意识
2. continuous：跨窗口知道"事实与事件"，但**绝不传对话状态**
3. 群聊窗口在数据层就拿不到私聊事实 → 模型不可能说出来
4. 群 A 的内容不进群 B；群里的"事实"不变成该 subject 的私聊事实
5. 切到 sealed 时已有意识立即失效
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import Origin, Visibility
from ling.domains.awareness.service import (
    Audience,
    AwarenessKind,
    AwarenessService,
)
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import AwarenessItemRow, Window
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv, *, visibility: str = "subject_private", title: str = "w") -> str:
    r = await env.client.post("/v1/sessions", json={"title": title})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility=visibility)
        )
    return session_id


async def _set_mode(env: TestEnv, mode: str) -> None:
    r = await env.client.put("/v1/isolation", json={"mode": mode})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ 模式本身


async def test_default_mode_is_per_window(logged_in: TestEnv) -> None:
    r = await logged_in.client.get("/v1/isolation")
    assert r.json()["mode"] == "per_window"


async def test_mode_switch_is_audited_with_revision(logged_in: TestEnv) -> None:
    first = await logged_in.client.put("/v1/isolation", json={"mode": "continuous"})
    second = await logged_in.client.put("/v1/isolation", json={"mode": "per_window"})
    assert second.json()["revision"] > first.json()["revision"]


async def test_sealed_blocks_long_term_memory_recall(logged_in: TestEnv) -> None:
    """sealed 模式下不召回长期记忆（用户要求严格区隔）。"""
    subject_id = logged_in.subject_id
    async with session_scope() as session:
        await MemoryService(session).propose(
            __import__("ling.contracts.v1", fromlist=["MemoryProposal"]).MemoryProposal(
                proposal_id="mp",
                kind="assertion",
                text="用户住在杭州",
                origin=Origin.user_statement,
                source_event_ids=["evt"],
                visibility=Visibility.subject_private,
            ),
            subject_id=subject_id,
        )

    window_id = await _window(logged_in)
    # per_window：能召回
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=subject_id, window_id=window_id, user_text="杭州"
        )
    assert "杭州" in "\n".join(m.content for m in compiled.messages)

    # sealed：不召回
    await _set_mode(logged_in, "sealed")
    async with session_scope() as session:
        compiled2 = await ContextCompiler(session).compile(
            subject_id=subject_id, window_id=window_id, user_text="杭州"
        )
    joined = "\n".join(m.content for m in compiled2.messages)
    assert "用户住在杭州" not in joined, "sealed 模式不应召回长期记忆"
    assert "你记得的相关事实" not in joined


# ------------------------------------------------------------------ 跨会话意识


async def test_continuous_mode_derives_awareness(logged_in: TestEnv) -> None:
    await _set_mode(logged_in, "continuous")
    window_id = await _window(logged_in)
    async with session_scope() as session:
        created = await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我叫阿澄，我住在杭州",
        )
    assert created, "continuous 模式应派生意识条目"
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AwarenessItemRow).where(
                        AwarenessItemRow.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    assert any("阿澄" in r.text for r in rows)
    # 事实必须被中性化，不保留原话"我叫"
    assert all(not r.text.startswith("我") for r in rows)


async def test_per_window_mode_does_not_derive(logged_in: TestEnv) -> None:
    """默认模式不积累意识：不"偷偷准备"可跨窗口读的东西。"""
    window_id = await _window(logged_in)
    async with session_scope() as session:
        created = await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我叫阿澄",
        )
    assert created == []


async def test_subject_fact_visible_across_private_windows(logged_in: TestEnv) -> None:
    """continuous 模式：私聊 A 的事实能在私聊 B 作为背景出现（这就是"像一个人"）。"""
    await _set_mode(logged_in, "continuous")
    window_a = await _window(logged_in, title="A")
    window_b = await _window(logged_in, title="B")
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_a,
            window_visibility="subject_private",
            user_text="我住在杭州",
        )
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_b, user_text="今天怎么样"
        )
    joined = "\n".join(m.content for m in compiled.messages)
    assert "杭州" in joined, "私聊之间应共享事实"
    assert "仅作背景" in joined, "必须明确标注是背景而非本轮对话"
    assert "不要逐字复述" in joined


async def test_group_window_cannot_see_private_fact(logged_in: TestEnv) -> None:
    """最关键的一条：群聊窗口在数据层就拿不到私聊事实。"""
    await _set_mode(logged_in, "continuous")
    private_window = await _window(logged_in, title="私聊")
    group_window = await _window(logged_in, visibility="group_scope", title="群")

    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=private_window,
            window_visibility="subject_private",
            user_text="我住在杭州",
        )

    # 数据层：群聊窗口看不到
    async with session_scope() as session:
        items = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="group_scope",
            window_id=group_window,
        )
    assert items == [], "群聊不应拿到私聊事实"

    # 提示层：也确实没有
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=group_window,
            user_text="大家好",
            is_group=True,
        )
    assert "杭州" not in "\n".join(m.content for m in compiled.messages)


async def test_group_fact_stays_in_that_group(logged_in: TestEnv) -> None:
    """群里说的事实归 group_scoped：不进该 subject 的私聊，也不进别的群。"""
    await _set_mode(logged_in, "continuous")
    group_a = await _window(logged_in, visibility="group_scope", title="群A")
    group_b = await _window(logged_in, visibility="group_scope", title="群B")
    private = await _window(logged_in, title="私聊")

    async with session_scope() as session:
        service = AwarenessService(session)
        await service.derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=group_a,
            window_visibility="group_scope",
            user_text="我住在杭州",
        )
        # 群里的事实应标为 group_scoped
        rows = list(
            (
                await session.execute(
                    select(AwarenessItemRow).where(
                        AwarenessItemRow.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    assert rows and all(r.audience == Audience.group_scoped.value for r in rows), [
        r.audience for r in rows
    ]

    # 别的群：拿不到
    async with session_scope() as session:
        other = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="group_scope",
            window_id=group_b,
        )
    assert other == [], "群 A 的内容不应进群 B"

    # 私聊：也拿不到（群里说的话不属于私聊事实）
    async with session_scope() as session:
        private_items = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="subject_private",
            window_id=private,
        )
    assert private_items == [], "群里的内容不应进私聊"


async def test_switching_to_sealed_invalidates_awareness(logged_in: TestEnv) -> None:
    """切到 sealed 时已有意识立即失效——否则等于没隔离。"""
    await _set_mode(logged_in, "continuous")
    window_id = await _window(logged_in)
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我叫阿澄",
        )
    await _set_mode(logged_in, "sealed")

    async with session_scope() as session:
        items = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="subject_private",
            window_id=window_id,
        )
    assert items == [], "切到 sealed 后旧意识必须失效"

    # 但保留审计痕迹（不物理删除）
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AwarenessItemRow).where(
                        AwarenessItemRow.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    assert rows and all(r.invalidated_at is not None for r in rows)


async def test_awareness_expires(logged_in: TestEnv) -> None:
    from datetime import UTC, datetime, timedelta

    await _set_mode(logged_in, "continuous")
    window_id = await _window(logged_in)
    async with session_scope() as session:
        item_id = await AwarenessService(session).record(
            subject_id=logged_in.subject_id,
            kind=AwarenessKind.subject_fact,
            text="这个人喜欢猫",
            source_window_id=window_id,
            source_visibility="subject_private",
        )
        assert item_id
        row = await session.get(AwarenessItemRow, item_id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async with session_scope() as session:
        items = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="subject_private",
            window_id=window_id,
        )
    assert all(i.item_id != item_id for i in items)


async def test_awareness_never_carries_conversation_state(logged_in: TestEnv) -> None:
    """派生条目只含事实：不含"上一句""未答问题"等对话状态。"""
    from ling.domains.awareness.service import FORBIDDEN_CROSS_WINDOW

    assert "last_utterance" in FORBIDDEN_CROSS_WINDOW
    assert "open_question" in FORBIDDEN_CROSS_WINDOW
    assert "other_group_content" in FORBIDDEN_CROSS_WINDOW

    await _set_mode(logged_in, "continuous")
    window_id = await _window(logged_in)
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我叫阿澄。你刚才说要去买菜吗？",
        )
        rows = list(
            (
                await session.execute(
                    select(AwarenessItemRow).where(
                        AwarenessItemRow.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    # 只提取事实，不提取问句
    assert all("买菜" not in r.text for r in rows), [r.text for r in rows]
    assert any("阿澄" in r.text for r in rows)


async def test_api_exposes_effects_and_items(logged_in: TestEnv) -> None:
    await _set_mode(logged_in, "continuous")
    window_id = await _window(logged_in)
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我养了一只猫",
        )
    listing = await logged_in.client.get(f"/v1/awareness?window_id={window_id}")
    body = listing.json()
    assert body["mode"] == "continuous"
    assert body["items"]
    assert body["forbidden_cross_window"]
