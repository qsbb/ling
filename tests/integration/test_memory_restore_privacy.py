"""An offline PostgreSQL restore reapplies both targeted and subject-wide forget state."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.domains.memory.restore_privacy import ForgetState, ForgottenSource, replay_forgetting
from ling.infra.db import session_scope
from ling.infra.models import (
    MemoryRow,
    MemorySourceForgetMarker,
    Message,
    OutboxEvent,
    Subject,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_restore_replays_newer_forgetting_before_reading_old_messages(logged_in: TestEnv) -> None:
    window = (await logged_in.client.post("/v1/sessions", json={"title": "隔离恢复"})).json()["session_id"]
    owner = logged_in.subject_id
    async with session_scope() as session:
        conversation = ConversationService(session)
        target = await conversation.add_message(
            window, role="user", text="我住在蓝湾巷", turn_id="audit-old-turn", sender_subject_id=owner,
        )
        kept = await conversation.add_message(
            window, role="user", text="我喜欢陶艺", turn_id="audit-independent-turn", sender_subject_id=owner,
        )
        session.add(Subject(id="second-subject"))
        session.add_all([
            MemoryRow(
                id=memory_id, subject_id=subject, kind="assertion", text=sentence,
                origin="user_statement", visibility="subject_private",
                source_event_ids=[f"message:{message_id}"], source_message_id=message_id,
                source_window_id=window,
            )
            for memory_id, subject, sentence, message_id in (
                ("address", owner, target.text, target.message_id),
                ("interest", owner, kept.text, kept.message_id),
                ("second-old", "second-subject", "我喜欢雪景", None),
            )
        ])
        session.add(OutboxEvent(
            id="pending-from-backup", subject_id=owner, topic="memory.extract",
            payload={"turn_id": target.turn_id, "user_text": target.text},
        ))

    state = ForgetState(
        sources=[ForgottenSource(
            message_id=target.message_id, subject_id=owner, window_id=window,
            turn_id=target.turn_id, forgotten_at=datetime.now(UTC),
        )],
        generations={"second-subject": 3},
    )
    for _ in range(2):
        async with session_scope() as session:
            await replay_forgetting(session, state)
    async with session_scope() as session:
        assert await session.get(MemorySourceForgetMarker, target.message_id)
        assert (await session.get(Message, target.message_id)).text == target.text
        assert [msg.message_id for msg in await ConversationService(session).messages_for_prompt(window)] == [
            kept.message_id,
        ]
        assert list(await session.scalars(select(MemoryRow.id))) == ["interest"]
        assert (await session.get(Subject, "second-subject")).forget_generation == 3
        pending = await session.get(OutboxEvent, "pending-from-backup")
        assert pending.state == "cancelled" and pending.payload == {"purged": True}
