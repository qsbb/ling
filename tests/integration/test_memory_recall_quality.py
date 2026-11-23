"""Current-fact recall and the prompt that is compiled from it, on PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.contracts.v1 import Origin, TurnState, Visibility
from ling.domains.conversation.service import ConversationService
from ling.domains.memory import providers
from ling.domains.memory.service import RECALL_CANDIDATE_LIMIT, MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRetrievalLog, MemoryRow, Window
from ling.kernel.audience import AudienceSnapshot
from ling.kernel.context import ContextCompiler, estimate_tokens
from ling.ports.config import MemoryPolicy
from tests.integration.conftest import TestEnv

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


async def test_direct_paraphrases_find_old_facts_without_model_configuration(
    logged_in: TestEnv,
) -> None:
    assert providers.build_embedding() is None
    assert providers.build_reranker() is None
    window_id = await _window(logged_in)
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

    async with session_scope() as session:
        compiler = ContextCompiler(session)
        for query, memory_id, fact in [
            (WORK_QUERY, "work", WORK_TEXT),
            ("我目前在哪里任职？", "work", WORK_TEXT),
            ("我现在定居何处？", "home", HOME_TEXT),
            ("我最喜欢什么颜色", "color", "主人最喜欢的颜色是紫色"),
        ]:
            compiled = await compiler.compile(
                subject_id=logged_in.subject_id, window_id=window_id, user_text=query,
            )
            assert compiled.memory_ids == [memory_id], query
            evidence = next(item for item in compiled.contributions if item.lane == "evidence")
            assert fact in evidence.text
            assert "路线攻略" not in evidence.text and "指南目录" not in evidence.text
            assert evidence.source_event_ids == [f"event:{memory_id}"]
            assert estimate_tokens(evidence.text) <= evidence.token_budget

        traced = await session.scalar(select(MemoryRetrievalLog).where(
            MemoryRetrievalLog.window_id == window_id, MemoryRetrievalLog.query == WORK_QUERY,
        ))
        assert traced is not None and traced.hit_ids == ["work"]


async def test_current_slot_filters_audience_and_time_before_the_candidate_cap(
    logged_in: TestEnv,
) -> None:
    group_a = await _window(logged_in, visibility=Visibility.group_scope)
    group_b = await _window(logged_in, visibility=Visibility.group_scope)
    group_c = await _window(logged_in, visibility=Visibility.group_scope)
    private = await _window(logged_in)
    group = {"visibility": "group_scope", "source_visibility": "group_scope"}
    old = datetime.now(UTC) - timedelta(days=400)
    past = datetime.now(UTC) - timedelta(days=1)
    future = datetime.now(UTC) + timedelta(days=1)
    async with session_scope() as session:
        session.add_all([
            _fact(logged_in, "private_work", WORK_TEXT, slot="works_at"),
            _fact(
                logged_in, "group_work", "用户在栖木档案馆工作", slot="works_at",
                created_at=old, updated_at=old, source_window_id=group_a, **group,
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
                ("other_window", {"source_window_id": group_c}),
                ("past", {"source_window_id": group_a, "valid_from": past, "valid_until": past}),
                ("future", {"source_window_id": group_a, "valid_from": future}),
                ("expired", {"source_window_id": group_a, "created_at": past, "retention_days": 1}),
            ]
            for i in range(40)
        ])

    async with session_scope() as session:
        compiler = ContextCompiler(session)
        for window, is_group, query, expected in [
            (group_a, True, WORK_QUERY, ["group_work"]),
            (group_b, True, WORK_QUERY, []),
            (private, False, WORK_QUERY, ["private_work"]),
            (group_a, True, "栖木档案馆先不说了，数据库怎么建表？", []),
            (group_a, True, "晚上好！", []),
        ]:
            compiled = await compiler.compile(
                subject_id=logged_in.subject_id, window_id=window,
                is_group=is_group, user_text=query,
            )
            assert compiled.memory_ids == expected
            assert any(item.lane == "evidence" for item in compiled.contributions) is bool(expected)


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
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text=WORK_QUERY,
        )
        assert compiled.memory_ids == ["work_guess"]
        assert any("模糊的印象" in item.text for item in compiled.contributions if item.lane == "evidence")

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

        compiler = ContextCompiler(session)
        current = await compiler.compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text=WORK_QUERY,
        )
        assert current.memory_ids == [new_id] and current.memory_sources == {}
        evidence = next(item.text for item in current.contributions if item.lane == "evidence")
        assert corrected in evidence and "墨桥文化馆" not in evidence

        history_query = "之前在墨桥文化馆代班的原话是什么？"
        history = await compiler.compile(
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
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text=WORK_QUERY,
        )
        assert compiled.memory_ids == [last_id]


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
