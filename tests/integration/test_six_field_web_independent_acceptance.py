"""Observe decision and real generation through the existing Web HTTP entry."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import Message
from ling.kernel.autonomy import Situation
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv
from tests.integration.test_window_dialogue_policy import create_window, publish, set_policy

pytestmark = pytest.mark.w0_web


async def test_http_false_override_snapshot_survives_policy_change_before_real_generation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ling.api.routes import chat

    window = await create_window(logged_in)
    await publish(logged_in, True)
    saved = await set_policy(logged_in, window, False)
    assert saved["global_value"] is True and saved["effective"] is False
    observed_decision: list[bool] = []
    observed_generation: list[bool] = []
    original_decision = chat._evaluate_turn_decision
    inner_router = logged_in.app.state.router
    marker = "independent-real-generated-answer"

    class ObservingRouter:
        def __getattr__(self, name: str):
            return getattr(inner_router, name)

        async def stream(self, config, *, profile, messages, **kwargs):
            is_choice = bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")
            if is_choice:
                payload = '{"respond":false,"reason":"test","length":"normal"}'
                yield ModelChunk(delta=payload), StreamOutcome()
                yield ModelChunk(done=True), StreamOutcome(text=payload)
                return
            observed_generation.append(config.dialogue.private_may_stay_silent)
            yield ModelChunk(delta=marker), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=marker)

    async def evaluate(*args, **kwargs):
        observed_decision.append((await args[4].effective()).dialogue.private_may_stay_silent)
        decision = await original_decision(*args, **kwargs)
        if len(observed_decision) == 1:
            current = await set_policy(logged_in, window, True)
            assert current["effective"] is True
        return decision

    async def available() -> Situation:
        return Situation(availability="available")

    logged_in.app.state.router = ObservingRouter()
    monkeypatch.setattr(chat, "_evaluate_turn_decision", evaluate)
    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    first = await logged_in.client.post(
        f"/v1/sessions/{window}/turns",
        json={"client_turn_id": "independent-first", "text": "你好"},
    )
    assert first.status_code == 200 and '"decision": "reply"' in first.text, first.text
    assert marker in first.text
    assert observed_decision == [False]
    assert observed_generation == [False]
    async with session_scope() as session:
        persisted = (await session.scalars(select(Message).where(Message.window_id == window))).all()
    assert any(message.role == "actor" and marker in (message.text or "") for message in persisted)
    current = await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")
    assert current.status_code == 200 and current.json()["effective"] is True
    second = await logged_in.client.post(
        f"/v1/sessions/{window}/turns",
        json={"client_turn_id": "independent-second", "text": "你好"},
    )
    assert second.status_code == 200 and '"decision": "silence"' in second.text, second.text
    assert observed_decision == [False, True]
    assert observed_generation == [False]
