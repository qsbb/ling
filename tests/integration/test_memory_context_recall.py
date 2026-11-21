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
from ling.infra.models import MemoryEpisode, MemoryRetrievalLog, MemoryRow
from ling.kernel.context import ContextCompiler, estimate_tokens
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
    # Make the semantic path eligible, so the automatic skip is what prevents recall.
    embed = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts])
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


async def test_recall_requires_relevance_before_ranking(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "用户把雨具放在入户柜下层"
    embed = AsyncMock(side_effect=lambda texts: [
        [1.0, 0.0] if item in {text, "遮雨用的东西呢？"} else [0.0, 1.0] for item in texts
    ])
    embedder = SimpleNamespace(model_id="test-relevance", embed=embed)
    monkeypatch.setattr(providers, "build_embedding", lambda *_a, **_kw: embedder)
    monkeypatch.setattr(providers, "build_reranker", lambda *_a, **_kw: None)
    window_id = await _window(logged_in)
    async with session_scope() as session:
        session.add(_memory(logged_in, "mem_umbrella", text))

    async with session_scope() as session:
        compiler = ContextCompiler(session)
        cases = [
            ("今天用什么数据库？", []),
            ("入户柜的铰链松了怎么修？", []),
            ("遮雨用的东西呢？", ["mem_umbrella"]),
        ]
        for query, expected in cases:
            result = await compiler.compile(
                subject_id=logged_in.subject_id, window_id=window_id, user_text=query,
            )
            assert result.memory_ids == expected
            assert any(text in message.content for message in result.messages) is bool(expected)
        assert ["今天用什么数据库？"] in [call.args[0] for call in embed.await_args_list]


async def test_prompt_keeps_three_distinct_memories_and_counts_only_injection(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "build_embedding", lambda *_a, **_kw: None)
    monkeypatch.setattr(providers, "build_reranker", lambda *_a, **_kw: None)
    window_id = await _window(logged_in)
    texts = [
        "面试改期到下周一，地点在科技馆", "面试要带好作品集，老师会先检查",
        "面试同行的是表姐，之后一起吃饭", "面试服装放在储藏柜，领带另外装",
        "面试线路经过体育中心，出门要转公交",
    ]
    async with session_scope() as session:
        for index, text in enumerate(texts):
            session.add(_memory(logged_in, f"mem_interview_{index}", text))
        session.add(MemoryEpisode(
            id="ep_duplicate", subject_id=logged_in.subject_id, window_id=window_id,
            summary=f"用户：{texts[0]}",
            origin="external_observation", visibility="subject_private",
            source_event_ids=["evt_example"],
        ))

    async with session_scope() as session:
        result = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text="面试",
        )
        evidence = next(message.content for message in result.annotated if message.lane == "evidence")
        assert sum(evidence.count(text) for text in texts) == 3
        assert "ep_duplicate" not in result.memory_ids
        assert "曾经聊过的话" not in evidence
        assert estimate_tokens(evidence) <= 420
        assert len(result.memory_ids) == 3 and result.meta["evidence_dropped"] == 3
        rows = list(await session.scalars(select(MemoryRow)))
        assert {row.id for row in rows if row.access_count} == set(result.memory_ids)


async def test_short_followup_uses_recent_topic_without_reinjecting_the_conversation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "build_embedding", lambda *_a, **_kw: None)
    monkeypatch.setattr(providers, "build_reranker", lambda *_a, **_kw: None)
    window_id = await _window(logged_in)
    earlier = "还记得我说的面试吗"
    reply = "你说上周准备去复试，过得怎么样？"
    async with session_scope() as session:
        conversation = ConversationService(session)
        await conversation.add_message(window_id, role="user", text=earlier, turn_id="test-recent")
        await conversation.add_message(window_id, role="actor", text=reply, turn_id="test-recent")
        session.add(MemoryEpisode(
            id="ep_recent", subject_id=logged_in.subject_id, window_id=window_id,
            turn_id="test-recent", summary=f"用户：{earlier}\nling：{reply}",
            origin="external_observation", visibility="subject_private",
            source_event_ids=["turn:test-recent"],
        ))
        session.add(MemoryEpisode(
            id="ep_older", subject_id=logged_in.subject_id, window_id=window_id,
            turn_id="test-older", summary="用户：我下周有个面试",
            origin="external_observation", visibility="subject_private",
            source_event_ids=["turn:test-older"],
        ))
        session.add(MemoryEpisode(
            id="ep_older_paraphrase", subject_id=logged_in.subject_id, window_id=window_id,
            summary="用户：下礼拜那场面试要准备一下。",
            origin="external_observation", visibility="subject_private",
            source_event_ids=["turn:test-older"],
        ))

    async with session_scope() as session:
        result = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text="哪家公司？",
        )
    assert len(result.memory_ids) == 1
    assert result.memory_ids[0] in {"ep_older", "ep_older_paraphrase"}
    assert sum(reply in message.content for message in result.messages) == 1
    evidence = next(message.content for message in result.annotated if message.lane == "evidence")
    assert "曾经聊过的话" in evidence and "面试" in evidence
