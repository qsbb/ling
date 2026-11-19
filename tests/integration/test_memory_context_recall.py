"""本轮文本决定是否自动召回，普通检索仍保留显式查询能力。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from ling.contracts.v1 import Visibility
from ling.domains.conversation.service import ConversationService
from ling.domains.memory import providers
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRetrievalLog, MemoryRow
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv) -> str:
    response = await env.client.post("/v1/sessions", json={"title": "短消息召回"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _memory(env: TestEnv, memory_id: str, text: str, **fields: object) -> MemoryRow:
    return MemoryRow(
        id=memory_id,
        subject_id=env.subject_id,
        kind="assertion",
        text=text,
        origin="user_statement",
        visibility="subject_private",
        confidence=0.95,
        source_event_ids=["evt_example"],
        **fields,
    )


@pytest.mark.parametrize(
    "message",
    ["　嗯。 ", "你在吗？", "ＯＫ！", "...？", ""],
    ids=["acknowledgment", "presence", "full-width-english", "punctuation", "empty"],
)
async def test_low_information_turn_skips_semantic_recall(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    # 查询和候选的余弦相似度为零，融合仍可能用其他排名选中候选。
    embed = AsyncMock(
        side_effect=lambda texts: [[1.0, 0.0] if text == message else [0.0, 1.0] for text in texts]
    )
    embedder = SimpleNamespace(model_id="test-embedding", embed=embed)
    monkeypatch.setattr(providers, "build_embedding", lambda *_a, **_kw: embedder)
    monkeypatch.setattr(providers, "build_reranker", lambda *_a, **_kw: None)

    window_id = await _window(logged_in)
    earlier = "周末的火车票已经订好了"
    async with session_scope() as session:
        session.add(
            _memory(
                logged_in, "mem_blue_whale", "上个月在海边看到了蓝鲸",
                created_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        await ConversationService(session).add_message(window_id, role="user", text=earlier)

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text=message
        )
        assert compiled.memory_ids == []
        assert not any(item.lane == "evidence" for item in compiled.contributions)
        assert any(item.role == "user" and earlier in item.content for item in compiled.messages)
        assert compiled.meta["memory_recall_skipped"] == "low_information"
        embed.assert_not_awaited()
        assert (await session.scalars(select(MemoryRetrievalLog))).all() == []
        row = await session.get(MemoryRow, "mem_blue_whale")
        assert row is not None and row.last_recalled_at is None and row.access_count == 0

        # 相同输入直接调用检索仍能找到候选，证明跳过发生在对话编译入口。
        direct = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query=message,
            visibility_allow={Visibility.subject_private},
        )
        assert [hit.memory_id for hit in direct.hits] == ["mem_blue_whale"]
        assert [message] in [call.args[0] for call in embed.await_args_list]


async def test_short_topic_and_explicit_history_still_recall(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "build_embedding", lambda *_a, **_kw: None)
    monkeypatch.setattr(providers, "build_reranker", lambda *_a, **_kw: None)
    window_id = await _window(logged_in)
    async with session_scope() as session:
        session.add_all([
            _memory(logged_in, "mem_current_home", "用户住在苏州"),
            _memory(
                logged_in, "mem_old_home", "用户住在杭州",
                valid_until=datetime.now(UTC) - timedelta(days=1),
            ),
        ])
        await ConversationService(session).add_message(window_id, role="user", text="杭州")

    async with session_scope() as session:
        compiler = ContextCompiler(session)
        for query, expected_id, evidence in [
            ("苏州？", "mem_current_home", "用户住在苏州"),
            ("嗯，你还记得我以前住在杭州吗？", "mem_old_home", "用户住在杭州"),
        ]:
            compiled = await compiler.compile(
                subject_id=logged_in.subject_id, window_id=window_id, user_text=query
            )
            assert expected_id in compiled.memory_ids
            assert any(
                item.lane == "evidence" and evidence in item.text
                for item in compiled.contributions
            )
            log = await session.scalar(
                select(MemoryRetrievalLog).where(
                    MemoryRetrievalLog.window_id == window_id, MemoryRetrievalLog.query == query
                )
            )
            assert log is not None and expected_id in log.hit_ids
