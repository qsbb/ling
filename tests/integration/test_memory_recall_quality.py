"""Current-fact tools and their scoped retrieval on PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.capabilities.tools import MEMORY_CURRENT_FACT_TOOL_ID
from ling.contracts.v1 import Origin, TurnState, Visibility
from ling.domains.conversation.service import ConversationService
from ling.domains.memory import providers
from ling.domains.memory.service import RECALL_CANDIDATE_LIMIT, MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRetrievalLog, MemoryRow, Message, Window
from ling.kernel.audience import AudienceSnapshot, visibility_set
from ling.kernel.context import ContextCompiler, estimate_tokens
from ling.ports.channel import GroupHistoryAccess
from ling.ports.config import MemoryPolicy
from tests.integration.conftest import TestEnv
from tests.integration.test_tools_turn_wiring import (
    PLAIN_REPLY,
    _bind_model,
    _draft,
    _publish,
    _tool_payload,
    _ToolEndpoint,
    _web_reply,
)
from tests.integration.test_tools_turn_wiring import (
    endpoint as endpoint,
)

pytestmark = pytest.mark.w0_web

WORK_QUERY = "我现在在哪上班？"
WORK_TEXT = "用户在栖木图书馆工作"
HOME_TEXT = "用户住在青岑里七号楼"


@pytest.fixture(autouse=True)
def _without_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LING_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("LING_RERANK_API_KEY", raising=False)


def _fact(
    env: TestEnv, memory_id: str, text: str, *,
    visibility: str = "subject_private", retention_days: int = 3650, **fields: object,
) -> MemoryRow:
    return MemoryRow(
        id=memory_id, subject_id=env.subject_id, kind="assertion", text=text,
        origin=Origin.user_statement.value, visibility=visibility, confidence=0.95,
        source_event_ids=[f"event:{memory_id}"], retention_days=retention_days, **fields,
    )


async def _window(
    env: TestEnv, *, visibility: Visibility = Visibility.subject_private,
) -> str:
    response = await env.client.post("/v1/sessions", json={"title": "记忆召回测试"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    if visibility is not Visibility.subject_private:
        async with session_scope() as session:
            window = await session.get(Window, window_id)
            assert window is not None
            window.visibility = visibility.value
    return window_id


async def _recall_slot(
    session: AsyncSession, env: TestEnv, window_id: str, slot: Literal["works_at", "lives_in"],
    visibility: Visibility = Visibility.subject_private,
    group_history: GroupHistoryAccess | None = None,
) -> list[str]:
    audience = await AudienceSnapshot.freeze(
        session, subject_id=env.subject_id, window_id=window_id, window_visibility=visibility.value,
        whole_group_source_ids=(
            group_history.source_ids
            if group_history is not None and group_history.window_id == window_id
            else frozenset()
        ),
    )
    result = await MemoryService(session).recall(
        subject_id=env.subject_id, query=slot, fact_slot=slot, audience=audience,
        visibility_allow=visibility_set(audience),
    )
    return [hit.memory_id for hit in result.hits]


async def test_model_selects_a_fact_from_beyond_the_recent_candidate_budget(
    logged_in: TestEnv, endpoint: _ToolEndpoint,
) -> None:
    assert providers.build_embedding() is None
    assert providers.build_reranker() is None
    old = datetime.now(UTC) - timedelta(days=400)
    async with session_scope() as session:
        session.add_all([
            _fact(logged_in, f"noise_{i:04d}", f"杂项第 {i} 条，已搬运纸盒")
            for i in range(2 * RECALL_CANDIDATE_LIMIT + 200)
        ])
        session.add_all([
            _fact(logged_in, "work", WORK_TEXT, slot="works_at", created_at=old, updated_at=old),
            _fact(logged_in, "home", HOME_TEXT, slot="lives_in", created_at=old, updated_at=old),
            _fact(
                logged_in, "work_decoy", "栖木图书馆附近有个栖木文化馆，上班路线攻略",
                slot="ongoing_project",
            ),
            _fact(
                logged_in, "home_decoy", "青岑里旁的青岑园定居指南目录",
                slot="ongoing_project",
            ),
            _fact(logged_in, "color", "主人最喜欢的颜色是紫色", created_at=old, updated_at=old),
        ])

    await _bind_model(logged_in, enabled=True)
    utterance = "个人资料里，地址和单位那两栏你还记得什么？"
    endpoint.tool_name = MEMORY_CURRENT_FACT_TOOL_ID
    for slot, memory_id, fact in [("works_at", "work", WORK_TEXT), ("lives_in", "home", HOME_TEXT)]:
        endpoint.requests.clear()
        endpoint.arguments = {"slot": slot}
        assert fact in await _web_reply(logged_in, text=utterance)
        payload = _tool_payload(endpoint)
        assert payload["outcome"] == "succeeded" and payload["untrusted"] is True
        assert "路线攻略" not in payload["data"] and "指南目录" not in payload["data"]
        first = endpoint.requests[0]
        schema = next(item["function"]["parameters"] for item in first["tools"]
                      if item["function"]["name"] == MEMORY_CURRENT_FACT_TOOL_ID)
        assert schema["properties"]["slot"]["enum"] == ["works_at", "lives_in"]
        assert fact not in str(first["messages"])
        async with session_scope() as session:
            traced = await session.scalar(select(MemoryRetrievalLog).where(
                MemoryRetrievalLog.query == slot, MemoryRetrievalLog.strategy == "current_fact",
            ))
            assert traced is not None and traced.hit_ids == [memory_id]

    endpoint.requests.clear()
    endpoint.script = [({"content": PLAIN_REPLY}, "stop")]
    assert await _web_reply(logged_in, text=utterance) == PLAIN_REPLY
    assert len(endpoint.requests) == 1
    async with session_scope() as session:
        direct_lookups = list(await session.scalars(select(MemoryRetrievalLog).where(
            MemoryRetrievalLog.strategy == "current_fact",
        )))
        assert len(direct_lookups) == 2
        window_id = await _window(logged_in)
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text="我最喜欢什么颜色",
        )
        assert compiled.memory_ids == ["color"]


async def test_current_fact_tool_respects_the_published_memory_budget(
    logged_in: TestEnv, endpoint: _ToolEndpoint,
) -> None:
    await _bind_model(logged_in, enabled=True)
    async with session_scope() as session:
        session.add(_fact(logged_in, "work", WORK_TEXT, slot="works_at"))
    await _publish(logged_in, await _draft(logged_in, {"memory": {"max_injected_memories": 0}}))
    endpoint.tool_name = MEMORY_CURRENT_FACT_TOOL_ID
    endpoint.arguments = {"slot": "works_at"}
    assert "tool_not_available" in await _web_reply(logged_in, text=WORK_QUERY)
    tools = [item["function"]["name"] for item in endpoint.requests[0]["tools"]]
    assert MEMORY_CURRENT_FACT_TOOL_ID not in tools and "clock_now" in tools
    assert WORK_TEXT not in str(endpoint.requests)


async def test_current_slot_filters_audience_and_time_before_the_candidate_cap(
    logged_in: TestEnv,
) -> None:
    group_a = await _window(logged_in, visibility=Visibility.group_scope)
    group_b = await _window(logged_in, visibility=Visibility.group_scope)
    group_c = await _window(logged_in, visibility=Visibility.group_scope)
    private = await _window(logged_in)
    group = {"visibility": "group_scope", "source_visibility": "group_scope"}
    group_source_message = "msg_group_work_source"
    other_source_message = "msg_group_c_work_source"
    same_source = {"source_window_id": group_a, "source_message_id": group_source_message}
    old = datetime.now(UTC) - timedelta(days=400)
    past = datetime.now(UTC) - timedelta(days=1)
    future = datetime.now(UTC) + timedelta(days=1)
    async with session_scope() as session:
        session.add_all([
            _fact(logged_in, "private_work", WORK_TEXT, slot="works_at"),
            _fact(
                logged_in, "group_work", "用户在栖木档案馆工作", slot="works_at",
                created_at=old, updated_at=old, source_window_id=group_a,
                source_message_id=group_source_message, **group,
            ),
            _fact(
                logged_in, "group_decoy", "栖木档案馆附近上班路线攻略", slot="ongoing_project",
                source_window_id=group_b, **group,
            ),
        ])
        session.add_all([
            _fact(
                logged_in, f"{key}_{i:02d}", "松影园区的同名资料室",
                slot="works_at", **scope, **group,
            )
            for key, scope in [
                ("other_window", {"source_window_id": group_c, "source_message_id": other_source_message}),
                ("past", {**same_source, "valid_from": past, "valid_until": past}),
                ("future", {**same_source, "valid_from": future}),
                ("expired", {**same_source, "created_at": past, "retention_days": 1}),
            ]
            for i in range(40)
        ])
        session.add(Message(
            id=group_source_message, window_id=group_a, role="user", text="我在栖木档案馆工作",
            sequence=1, state="completed", origin="user_statement",
        ))
        session.add(Message(
            id=other_source_message, window_id=group_c, role="user", text="有人在松影园区上班",
            sequence=1, state="completed", origin="user_statement",
        ))

    async with session_scope() as session:
        for window, visibility, expected, group_history in [
            (
                group_a, Visibility.group_scope, ["group_work"],
                GroupHistoryAccess(group_a, frozenset({group_source_message, other_source_message})),
            ),
            (group_a, Visibility.group_scope, [], None),
            (group_b, Visibility.group_scope, [], None),
            (private, Visibility.subject_private, ["private_work"], None),
        ]:
            assert await _recall_slot(
                session, logged_in, window, "works_at", visibility, group_history,
            ) == expected


async def test_correction_answers_current_fact_and_keeps_the_authorized_original(
    logged_in: TestEnv,
) -> None:
    window_id = await _window(logged_in)
    other_window_id = await _window(logged_in)
    original = "我那时只去墨桥文化馆代班过。"
    corrected = "固定岗位是松影资料室的全职馆员"
    async with session_scope() as session:
        conversation = ConversationService(session)
        turn = await conversation.create_turn(window_id, client_turn_id="before-fix", config_revision=0)
        message = await conversation.add_message(window_id, role="user", text=original, turn_id=turn.id)
        await conversation.set_turn_state(turn.id, TurnState.completed)
        await conversation.save_summary(
            window_id, summary="说过一次代班。", through_sequence=message.sequence, message_count=1,
        )
        source_event_ids = [f"turn:{turn.id}"]
        service = MemoryService(session)
        episode_id = await service.record_episode(
            subject_id=logged_in.subject_id, window_id=window_id, summary="曾到墨桥文化馆代班。",
            origin=Origin.external_observation, source_event_ids=source_event_ids, turn_id=turn.id,
        )
        inference = _fact(
            logged_in, "work_guess", "用户在墨桥文化馆工作", slot="works_at", value="墨桥文化馆",
            source_window_id=window_id, source_account="web:example-account", turn_id=turn.id,
        )
        inference.origin = Origin.model_inference.value
        inference.source_event_ids = source_event_ids
        session.add(inference)

    async with session_scope() as session:
        assert await _recall_slot(session, logged_in, window_id, "works_at") == ["work_guess"]

    response = await logged_in.client.post(
        "/v1/memory/work_guess/supersede", json={"text": corrected, "reason": "合成纠正"},
    )
    assert response.status_code == 200, response.text
    new_id = response.json()["memory_id"]
    async with session_scope() as session:
        service = MemoryService(session)
        new = await session.get(MemoryRow, new_id)
        old = await session.get(MemoryRow, "work_guess")
        assert old is not None and new is not None
        assert old.retracted and old.superseded_by == new_id and new.supersedes == old.id
        assert new.slot == "works_at" and new.value is None
        assert new.source_window_id == window_id and new.source_account == "web:example-account"
        assert new.source_visibility == old.source_visibility
        assert new.origin == Origin.user_statement.value and new.source_event_ids == source_event_ids

        assert await _recall_slot(session, logged_in, window_id, "works_at") == [new_id]

        history_query = "之前在墨桥文化馆代班的原话是什么？"
        history = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text=history_query,
        )
        assert history.memory_ids == [episode_id]
        assert list(history.memory_sources) == ["m1"]
        source = history.memory_sources["m1"]
        for window, expected in [
            (window_id, [{"speaker": "用户", "text": original, "truncated": False}]),
            (other_window_id, []),
        ]:
            audience = await AudienceSnapshot.freeze(
                session, subject_id=logged_in.subject_id, window_id=window,
                window_visibility=Visibility.subject_private.value,
            )
            assert await service.read_source(source, audience=audience) == expected


async def test_legacy_corrections_keep_the_fact_slot_across_two_edits(logged_in: TestEnv) -> None:
    window_id = await _window(logged_in)
    async with session_scope() as session:
        session.add(_fact(logged_in, "initial_work", WORK_TEXT, slot="works_at"))
        await session.flush()
        service = MemoryService(session)
        first_id = await service.supersede("initial_work", "清霁馆的合同工", reason="首次纠正")
        assert first_id is not None
        first = await session.get(MemoryRow, first_id)
        assert first is not None
        first.slot = None  # Persisted shape before supersede copied the slot.
        last_id = await service.supersede(first.id, "云栖阁的全职馆员", reason="再次纠正")
        assert last_id is not None
        last = await session.get(MemoryRow, last_id)
        assert last is not None and last.slot is None

    async with session_scope() as session:
        assert await _recall_slot(session, logged_in, window_id, "works_at") == [last_id]


async def test_injection_budget_does_not_change_the_recall_shortlist(logged_in: TestEnv) -> None:
    window_id = await _window(logged_in)
    query = "纸艺课"
    texts = [
        "纸艺课报名在北楼门厅", "纸艺课作品需要木制画框",
        "纸艺课老师推荐浅色棉浆纸", "纸艺课工具自带白乳胶",
    ]
    async with session_scope() as session:
        session.add_all([_fact(logged_in, f"paper_{i}", text) for i, text in enumerate(texts)])

    async with session_scope() as session:
        for max_items, expected in [(0, 0), (1, 1), (3, 3), (12, 4)]:
            compiler = ContextCompiler(session, memory_policy=MemoryPolicy(max_injected_memories=max_items))
            compiled = await compiler.compile(
                subject_id=logged_in.subject_id, window_id=window_id, user_text=query,
            )
            assert len(compiled.memory_ids) == expected
            trace = await session.scalar(
                select(MemoryRetrievalLog).where(
                    MemoryRetrievalLog.window_id == window_id, MemoryRetrievalLog.query == query,
                ).order_by(MemoryRetrievalLog.created_at.desc()).limit(1)
            )
            if max_items == 0:
                assert compiled.meta["memory_recall_skipped"] == "disabled_by_policy"
                assert trace is None
            else:
                evidence = next(item for item in compiled.contributions if item.lane == "evidence")
                assert sum(line in evidence.text for line in texts) == expected
                assert estimate_tokens(evidence.text) <= evidence.token_budget
                assert trace is not None and set(trace.hit_ids) == {f"paper_{i}" for i in range(4)}
