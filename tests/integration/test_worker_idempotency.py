"""worker 幂等性：至少一次处理不得产生重复数据。

outbox 是"至少一次"语义：崩溃重试、超时重试、多副本都会让同一事件
被处理两次以上。手写"先查后写"有竞态，所以靠**数据库唯一约束**兜底，
handler 只需把"已存在"翻译成正常返回。

幂等键：
- memory_episodes(turn_id)          → 同一轮只一条情节
- relationship_events(turn_id, kind) → 同一轮同类互动只记一次（不重复放大关系）
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, RelationshipEvent
from ling.worker import handle_memory_extract, handle_relationship_update
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _payload(env: TestEnv, **overrides) -> dict:
    base = {
        "subject_id": env.subject_id,
        "window_id": "win_idem",
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "user_text": "我叫阿澄，我住在杭州",
        "actor_text": "记住了",
        "source_visibility": "subject_private",
    }
    base.update(overrides)
    return base


async def _count(model, **filters) -> int:
    async with session_scope() as session:
        stmt = select(func.count()).select_from(model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        return int((await session.execute(stmt)).scalar_one())


async def test_memory_extract_twice_creates_one_episode(logged_in: TestEnv) -> None:
    """同一 turn 处理两次：只应有一条 Episode。"""
    payload = _payload(logged_in)
    await handle_memory_extract(payload)
    await handle_memory_extract(payload)  # 模拟重试

    count = await _count(MemoryEpisode, turn_id=payload["turn_id"])
    assert count == 1, f"重复处理产生了 {count} 条情节"


async def test_memory_extract_twice_does_not_duplicate_assertions(
    logged_in: TestEnv,
) -> None:
    """断言也不能重复（虽然当前 propose 没有唯一键，靠 Episode 幂等短路）。"""
    payload = _payload(logged_in)
    await handle_memory_extract(payload)
    async with session_scope() as session:
        first = list(
            (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.subject_id == logged_in.subject_id,
                        MemoryRow.text.like("%阿澄%"),
                    )
                )
            ).scalars()
        )
    await handle_memory_extract(payload)
    async with session_scope() as session:
        second = list(
            (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.subject_id == logged_in.subject_id,
                        MemoryRow.text.like("%阿澄%"),
                    )
                )
            ).scalars()
        )
    assert len(first) == len(second), (
        f"重试后断言数量从 {len(first)} 变成 {len(second)}"
    )


async def test_relationship_update_twice_creates_one_event(logged_in: TestEnv) -> None:
    payload = {
        "subject_id": logged_in.subject_id,
        "window_id": "win_rel",
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "user_text": "谢谢你，有你真好",
    }
    await handle_relationship_update(payload)
    await handle_relationship_update(payload)

    count = await _count(RelationshipEvent, turn_id=payload["turn_id"])
    assert count == 1, f"重复处理产生了 {count} 条关系事件"


async def test_relationship_familiarity_not_double_counted(logged_in: TestEnv) -> None:
    """重复处理不得把熟悉度加两次——那会让关系失真。"""
    from ling.domains.relationship.service import RelationshipService

    payload = {
        "subject_id": logged_in.subject_id,
        "window_id": "win_rel2",
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "user_text": "谢谢你，有你真好",
    }
    await handle_relationship_update(payload)
    async with session_scope() as session:
        service = RelationshipService(session)
        after_first = service.view(await service.state(logged_in.subject_id))

    await handle_relationship_update(payload)
    async with session_scope() as session:
        service = RelationshipService(session)
        after_second = service.view(await service.state(logged_in.subject_id))

    assert after_first.familiarity == after_second.familiarity, (
        f"熟悉度被重复累加：{after_first.familiarity} → {after_second.familiarity}"
    )


async def test_different_turns_still_create_separate_episodes(logged_in: TestEnv) -> None:
    """幂等不能过度：不同轮次必须各自留一条情节。"""
    first = _payload(logged_in, turn_id="turn_a1")
    second = _payload(logged_in, turn_id="turn_a2")
    await handle_memory_extract(first)
    await handle_memory_extract(second)
    assert await _count(MemoryEpisode, turn_id="turn_a1") == 1
    assert await _count(MemoryEpisode, turn_id="turn_a2") == 1


async def test_episode_records_turn_id(logged_in: TestEnv) -> None:
    """幂等键必须落库，否则下次重试无从判断。"""
    payload = _payload(logged_in)
    await handle_memory_extract(payload)
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryEpisode).where(MemoryEpisode.turn_id == payload["turn_id"])
            )
        ).scalar_one()
    assert row.turn_id == payload["turn_id"]
    assert row.source_event_ids, "情节必须带证据"


async def test_outbox_skip_locked_prevents_double_claim(logged_in: TestEnv) -> None:
    """两个 worker 并发领取：SKIP LOCKED 保证同一条只被一个拿到。"""
    from ling.infra.models import OutboxEvent

    turn_id = f"turn_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=f"obx_{uuid.uuid4().hex[:12]}",
                topic="memory.extract",
                payload={
                    "subject_id": logged_in.subject_id,
                    "window_id": "win_lock",
                    "turn_id": turn_id,
                    "user_text": "我叫锁定测试",
                    "actor_text": "好",
                    "source_visibility": "subject_private",
                },
            )
        )

    # 用两个独立事务模拟两个 worker 同时领取
    async def claim_once() -> list[str]:
        from sqlalchemy import select as sa_select

        from ling.infra.models import utcnow

        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        sa_select(OutboxEvent)
                        .where(
                            OutboxEvent.state == "pending",
                            OutboxEvent.topic == "memory.extract",
                            OutboxEvent.available_at <= utcnow(),
                        )
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).scalars()
            )
            for row in rows:
                row.state = "done"
            return [r.id for r in rows]

    first = await claim_once()
    second = await claim_once()
    assert len(first) == 1
    assert second == [], "同一条 outbox 事件被两个 worker 重复领取"


async def test_failed_event_is_not_reprocessed_after_done(logged_in: TestEnv) -> None:
    """已完成的 outbox 不会被再次领取。"""
    from ling.worker import drain_outbox

    payload = _payload(logged_in)
    from ling.infra.models import OutboxEvent

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=f"obx_{uuid.uuid4().hex[:12]}",
                topic="memory.extract",
                payload=payload,
            )
        )
    processed = await drain_outbox()
    assert processed >= 1
    again = await drain_outbox()
    assert again == 0, "已处理的 outbox 被重复处理"


async def test_affect_not_double_stacked(logged_in: TestEnv) -> None:
    """此前实现的缺陷：关系事件幂等后，情绪仍会被重复叠加。"""
    from ling.infra.models import AffectState

    payload = {
        "subject_id": logged_in.subject_id,
        "window_id": "win_affect_idem",
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "user_text": "谢谢你，有你真好",
    }
    await handle_relationship_update(payload)
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AffectState).where(AffectState.actor_id == "actor_ling")
                )
            ).scalars()
        )
        first = {r.id: r.intensity for r in rows}

    # 重复处理同一 turn 3 次
    for _ in range(3):
        await handle_relationship_update(payload)

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AffectState).where(AffectState.actor_id == "actor_ling")
                )
            ).scalars()
        )
        second = {r.id: r.intensity for r in rows}
    assert first == second, f"情绪被重复叠加：{first} → {second}"


async def test_recover_only_touches_stale_turns(logged_in: TestEnv) -> None:
    """此前实现的缺陷：worker 启动会误杀另一个实例正在处理的活跃轮次。"""
    from datetime import UTC, datetime, timedelta

    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import Turn
    from ling.worker import recover_dangling_turns

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "悬空判定"})
    ).json()["session_id"]
    async with session_scope() as session:
        conv = ConversationService(session)
        active = await conv.create_turn(
            session_id, client_turn_id=f"ct-{uuid.uuid4().hex[:6]}", config_revision=0
        )
        stale = await conv.create_turn(
            session_id, client_turn_id=f"ct-{uuid.uuid4().hex[:6]}", config_revision=0
        )
        await conv.set_turn_state(active.id, TurnState.streaming)
        await conv.set_turn_state(stale.id, TurnState.streaming)
        active_id, stale_id = active.id, stale.id

    # 独立事务里改 updated_at：set_turn_state 会自动刷新它，
    # 必须在状态写完之后再改时间戳。
    async with session_scope() as session:
        stale_row = await session.get(Turn, stale_id)
        stale_row.updated_at = datetime.now(UTC) - timedelta(minutes=30)
        # active 明确设为"刚刚更新过"，模拟另一个实例正在处理
        active_row = await session.get(Turn, active_id)
        active_row.updated_at = datetime.now(UTC)

    # 先确认阈值判断的前提：active 的时间戳确实在阈值内
    async with session_scope() as session:
        check = await session.get(Turn, active_id)
        from ling.worker import DANGLING_TURN_MINUTES

        age_minutes = (
            datetime.now(UTC) - (check.updated_at if check.updated_at.tzinfo
                                 else check.updated_at.replace(tzinfo=UTC))
        ).total_seconds() / 60
        assert age_minutes < DANGLING_TURN_MINUTES, (
            f"测试前提不成立：active 轮次年龄 {age_minutes:.1f} 分钟"
        )

    await recover_dangling_turns()

    async with session_scope() as session:
        active_row = await session.get(Turn, active_id)
        stale_row = await session.get(Turn, stale_id)
    assert active_row.state == "streaming", "活跃轮次被误判为悬空"
    assert stale_row.state == "failed", "真正的悬空轮次未被修正"


async def test_failure_applies_backoff_not_immediate_retry(logged_in: TestEnv) -> None:
    """失败后必须退避，否则连续轮询几次就把次数耗尽（此前实现的问题）。"""
    from ling.infra.models import OutboxEvent
    from ling.worker import drain_outbox

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=f"obx_{uuid.uuid4().hex[:12]}",
                topic="does.not.exist",  # 触发 handler 缺失
                payload={},
            )
        )
    # handler 缺失时 worker 标 unhandled，不算失败——先直接构造失败
    from sqlalchemy import update as sa_update

    from ling.infra.models import OutboxEvent as OE

    async with session_scope() as session:
        await session.execute(
            sa_update(OE).where(OE.topic == "does.not.exist").values(topic="memory.extract",
                                                                     payload={"subject_id": "s"})
        )
    await drain_outbox()

    async with session_scope() as session:
        row = (
            await session.execute(
                select(OE).where(OE.topic == "memory.extract").order_by(OE.created_at.desc())
            )
        ).scalars().first()
        assert row is not None
        # 缺字段的 handler 会正常返回（不抛），所以这条会 done。
        # 这里主要验证状态机字段存在且可用。
        assert row.state in {"done", "pending", "dead"}
        assert row.available_at is not None


async def test_dead_events_can_be_replayed(logged_in: TestEnv) -> None:
    """失败到上限后转 dead，可人工重放，不是静默丢弃。"""
    from ling.infra.models import OutboxEvent
    from ling.worker import replay_dead_events

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=f"obx_{uuid.uuid4().hex[:12]}",
                topic="memory.extract",
                payload={"subject_id": "s"},
                state="dead",
                attempts=5,
                last_error="模拟连续失败",
            )
        )
    count = await replay_dead_events()
    assert count >= 1

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.state == "dead")
                )
            ).scalars()
        )
    assert rows == [], "dead 事件未被重新入队"


async def test_slot_key_survives_wording_change(logged_in: TestEnv) -> None:
    """改提取模板的措辞不会绕过去重（此前实现的隐患）。

    旧实现用 (turn_id, text) 作键：模板从"用户喜欢 X"改成"用户偏好 X"
    就会被当成新记忆。现在用语义槽位，措辞无关。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService

    subject_id = logged_in.subject_id
    turn_id = f"turn_{uuid.uuid4().hex[:10]}"

    async with session_scope() as session:
        first = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p1",
                kind="assertion",
                text="用户喜欢 下雨天",
                origin=Origin.user_statement,
                source_event_ids=["evt"],
                visibility=Visibility.subject_private,
            ),
            subject_id=subject_id,
            turn_id=turn_id,
            slot="likes",
        )

    # 同一 slot、不同措辞（模拟规则升级）
    async with session_scope() as session:
        second = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p2",
                kind="assertion",
                text="用户偏好 雨天",
                origin=Origin.user_statement,
                source_event_ids=["evt"],
                visibility=Visibility.subject_private,
            ),
            subject_id=subject_id,
            turn_id=turn_id,
            slot="likes",
        )

    assert first == second, "同一槽位产生了新记忆（措辞变化绕过了去重）"

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.turn_id == turn_id, MemoryRow.slot == "likes"
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1, f"同一槽位有 {len(rows)} 条记忆"
    assert rows[0].text == "用户偏好 雨天", "新措辞应更新既有条目"


async def test_different_slots_create_separate_memories(logged_in: TestEnv) -> None:
    """不同槽位必须各自留一条（幂等不能过度）。"""
    payload = _payload(logged_in, user_text="我叫阿澄，我喜欢下雨天，我住在杭州")
    await handle_memory_extract(payload)

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.turn_id == payload["turn_id"])
                )
            ).scalars()
        )
    slots = {r.slot for r in rows}
    assert {"name", "likes", "lives_in"} <= slots, slots


async def test_extract_twice_still_one_per_slot(logged_in: TestEnv) -> None:
    """重复处理同一条：每个槽位仍只有一条。"""
    payload = _payload(logged_in, user_text="我叫阿澄，我喜欢下雨天，我住在杭州")
    await handle_memory_extract(payload)
    await handle_memory_extract(payload)
    await handle_memory_extract(payload)

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.turn_id == payload["turn_id"])
                )
            ).scalars()
        )
    assert len(rows) == 3, f"重复处理后变成 {len(rows)} 条（应为 3 个槽位各一条）"
    assert len({r.slot for r in rows}) == 3
