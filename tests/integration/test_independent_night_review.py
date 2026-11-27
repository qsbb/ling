"""Independent PostgreSQL checks for the continuity entries in the nightly review."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ling import worker
from ling.contracts.v1 import Origin, TurnState
from ling.domains.activity.service import ActivityService
from ling.domains.conversation.service import ConversationService
from ling.domains.relationship.service import RelationshipService
from ling.infra.db import session_scope
from ling.infra.models import (
    DayPlanRow,
    DreamReportRow,
    IntentRow,
    Message,
    ScheduleSegmentRow,
    utcnow,
)
from tests.integration.conftest import TestEnv
from tests.integration.test_memory_sleep_review import _night, _statement

pytestmark = pytest.mark.w0_web


async def _consolidate(env: TestEnv, bedtime: datetime) -> dict:
    await worker.handle_memory_consolidate({
        "actor_id": env.settings.actor_id,
        "subject_id": env.subject_id,
        "sleep_date": bedtime.date().isoformat(),
        "bed_at": bedtime.isoformat(),
    })
    return await _night(env)


async def _complete(
    session: AsyncSession, actor_id: str, at: datetime, description: str, *,
    origin: str = "simulated", evidence: str | None = "simulated_outcome",
) -> str:
    service = ActivityService(session, actor_id=actor_id)
    activity = await service.propose(kind="walking", visibility="actor_private", origin=origin)
    await service.transition(activity.activity_id, "running", now=at - timedelta(minutes=3))
    await service.transition(
        activity.activity_id, "completed", now=at, completion_evidence=description, evidence_kind=evidence,
    )
    return activity.activity_id


async def _relationship_message(
    session: AsyncSession, env: TestEnv, window_id: str, *, at: datetime,
    kind: str, role: str, origin: Origin, text: str,
) -> str:
    dialogue = ConversationService(session)
    turn = await dialogue.create_turn(window_id, client_turn_id=f"night:{kind}", config_revision=0)
    turn.created_at = at
    message = await dialogue.add_message(
        window_id, role=role, text=text, turn_id=turn.id, origin=origin,
        sender_subject_id=env.subject_id if role == "user" else None,
    )
    row = await session.get(Message, message.message_id)
    assert row is not None
    row.created_at = at
    await dialogue.set_turn_state(turn.id, TurnState.completed)
    await RelationshipService(session, actor_id=env.settings.actor_id).record_event(
        subject_id=env.subject_id, kind=kind, origin=origin, note=text,
        source_event_ids=[f"message:{row.id}"], turn_id=turn.id, occurred_at=at,
        apply_to_projection=False,
    )
    return row.id


async def _seed_review(env: TestEnv, bedtime: datetime) -> tuple[dict[str, tuple[str, str, str]], str]:
    actor_id = env.settings.actor_id
    at = bedtime - timedelta(hours=1)
    window_id, _ = await _statement(env, "night-statement", "我给山茶浇水", at)
    async with session_scope() as session:
        simulated = await _complete(session, actor_id, at, "我在模拟街道散步")
        observed = await _complete(
            session, actor_id, at, "提醒设备回执已到", origin="tool_receipt", evidence=None,
        )
        await _complete(session, actor_id, at, "只经过了时钟节点", evidence=None)
        await _complete(session, "other-actor", at, "另一角色的独立活动")
        session.add(DayPlanRow(
            id="night-plan", actor_id=actor_id, plan_day=at.date(),
            window_start_at=at - timedelta(hours=5), window_end_at=at + timedelta(hours=6),
        ))
        await session.flush()
        session.add(ScheduleSegmentRow(
            id="night-segment", actor_id=actor_id, plan_id="night-plan", kind="leisure",
            title="取消了旧书店行程", start_at=at - timedelta(minutes=15), end_at=at,
            status="cancelled", visibility="actor_private",
        ))
        await session.flush()
        service = ActivityService(session, actor_id=actor_id)
        cancelled = await service.propose(
            kind="leisure", visibility="actor_private", schedule_segment_id="night-segment",
        )
        await service.transition(cancelled.activity_id, "cancelled", now=at)
        session.add(DreamReportRow(
            id="night-dream", actor_id=actor_id, sleep_date=bedtime.date(),
            status="ready", narrative="梦里我登上了纸灯岛",
        ))
        session.add(IntentRow(
            id="night-intention", actor_id=actor_id, subject_id=None, kind="note", state="open",
            goal="我想找一支新的画笔", source_kind="self", source_ref=f"activity:{simulated}",
            visibility="actor_private", dedupe_key="night-intention", created_at=at,
        ))
        impression = await _relationship_message(
            session, env, window_id, at=at, kind="impression", role="actor",
            origin=Origin.model_inference, text="我刚才以为你有点难过。",
        )
        clarification = await _relationship_message(
            session, env, window_id, at=at + timedelta(minutes=1), kind="clarification", role="user",
            origin=Origin.user_statement, text="其实我没有难过，我在忙着帮同事。",
        )
    return {
        simulated: ("activity", "simulated_outcome", "我在模拟街道散步"),
        observed: ("activity", "observed", "提醒设备回执已到"),
        "night-segment": ("plan", "cancelled", "取消了旧书店行程"),
        "night-dream": ("dream", "simulated", "梦里我登上了纸灯岛"),
        impression: ("impression", "model_inference", "我刚才以为你有点难过。"),
        clarification: ("clarification", "user_statement", "其实我没有难过，我在忙着帮同事。"),
        "night-intention": ("intention", "open", "我想找一支新的画笔"),
    }, clarification


def _entries(night: dict) -> dict[str, tuple[str, str, str]]:
    return {row["source_id"]: (row["kind"], row["status"], row["text"]) for row in night["life"]}


async def test_night_review_keeps_simulated_real_and_personal_sources_distinct(logged_in: TestEnv) -> None:
    bedtime = utcnow() - timedelta(minutes=40)
    expected, clarification = await _seed_review(logged_in, bedtime)
    reviewed = await _consolidate(logged_in, bedtime)
    assert _entries(reviewed) == expected
    statement, = reviewed["items"]
    assert statement["original"] == [{"speaker": "用户", "text": "原话是我给山茶浇水", "truncated": False}]

    async with session_scope() as session:
        source = await session.get(Message, clarification)
        assert source is not None
        source.memory_hidden_at = utcnow()
    current = await _night(logged_in)
    assert _entries(current) == {key: value for key, value in expected.items() if key != clarification}
    assert current["items"] == reviewed["items"]


async def test_night_review_uses_bedtime_after_a_late_worker_run(logged_in: TestEnv) -> None:
    bedtime = utcnow() - timedelta(hours=3)
    before_at = bedtime - timedelta(hours=23)
    after_at = bedtime + timedelta(hours=1)
    await _statement(logged_in, "night-before", "睡前整理过画册", before_at)
    await _statement(logged_in, "night-after", "下一日选好了画笔", after_at)
    async with session_scope() as session:
        before = await _complete(session, logged_in.settings.actor_id, before_at, "入睡前检查邮票")
        await _complete(session, logged_in.settings.actor_id, after_at, "之后才整理抽屉")
    reviewed = await _consolidate(logged_in, bedtime)
    assert [item["memory_id"] for item in reviewed["items"]] == ["night-before"]
    assert _entries(reviewed) == {before: ("activity", "simulated_outcome", "入睡前检查邮票")}


async def test_sealed_night_review_hides_relationship_sources_but_keeps_autonomous_life(logged_in: TestEnv) -> None:
    bedtime = utcnow() - timedelta(minutes=40)
    expected, _ = await _seed_review(logged_in, bedtime)
    assert _entries(await _consolidate(logged_in, bedtime)) == expected
    sealed = await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    assert sealed.status_code == 200, sealed.text
    reviewed = await _night(logged_in)
    assert reviewed["items"] == []
    assert _entries(reviewed) == {
        key: value for key, value in expected.items() if value[0] not in {"impression", "clarification"}
    }
