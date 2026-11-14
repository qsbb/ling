"""Web 幂等回放必须复用已保存轮次，而不重读当前设置。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import ConversationAttemptSnapshot, Turn
from tests.integration.conftest import TestEnv
from tests.integration.test_window_dialogue_policy import create_window

pytestmark = pytest.mark.w0_web


async def test_completed_turn_replays_without_reading_current_window_config(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ling.settings.window_policy as window_policy
    from ling.api.routes import chat

    window = await create_window(logged_in)
    decision = SimpleNamespace(
        respond=True,
        reason="直接回复",
        followup=None,
        skip_code="private_direct",
        hard=True,
        model_status=SimpleNamespace(value="not_asked"),
        silent_ack=None,
    )
    generation_calls = []

    async def choose(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return decision

    async def generate(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        generation_calls.append(kwargs["turn_id"])
        queue = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    monkeypatch.setattr(chat, "_evaluate_turn_decision", choose)
    monkeypatch.setattr(chat.TurnCoordinator, "run_turn", generate)
    request = {"client_turn_id": "stable-replay-key", "text": "把这条完整回放"}
    first = await logged_in.client.post(f"/v1/sessions/{window}/turns", json=request)
    assert first.status_code == 200, first.text
    assert len(generation_calls) == 1

    async with session_scope() as session:
        turn = await session.scalar(
            select(Turn).where(
                Turn.window_id == window,
                Turn.client_turn_id == request["client_turn_id"],
            )
        )
        assert turn is not None
        turn.state = "completed"
        await ConversationService(session).add_message(
            window,
            role="actor",
            text="原始回复",
            turn_id=turn.id,
            model_label="原始模型",
            attempt=turn.attempt,
        )
        original_turn_id = turn.id
        snapshot = await session.get(
            ConversationAttemptSnapshot, (turn.id, int(turn.attempt or 1))
        )
        assert snapshot is not None
        original_test_mode = snapshot.test_mode

    async def config_must_not_be_read(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("当前配置不可读")

    monkeypatch.setattr(window_policy, "runtime_window_snapshot", config_must_not_be_read)
    replayed = await logged_in.client.post(f"/v1/sessions/{window}/turns", json=request)
    assert replayed.status_code == 200, replayed.text
    assert '"replayed": true' in replayed.text
    assert f'"turn_id": "{original_turn_id}"' in replayed.text
    assert f'"test_mode": {str(original_test_mode).lower()}' in replayed.text
    assert "原始回复" in replayed.text
    assert len(generation_calls) == 1
