"""Web decision and generation share one persona snapshot per turn."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ling.kernel.autonomy import Situation
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv
from tests.integration.test_persona_injection import _write_persona
from tests.integration.test_window_dialogue_policy import create_window, publish

pytestmark = pytest.mark.w0_web


@pytest.mark.parametrize("initial_state", ["edit", "create", "read_error"])
async def test_web_persona_edit_between_decision_and_generation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    initial_state: str,
) -> None:
    old_prompt = "SNAPSHOT_OLD_PERSONA"
    new_prompt = "SNAPSHOT_NEW_PERSONA"
    if initial_state != "create":
        await _write_persona(logged_in, system_prompt=old_prompt, traits={}, boundaries=[])
    await publish(logged_in, True)
    window = await create_window(logged_in)
    inner_router = logged_in.app.state.router

    class ObservingRouter:
        def __init__(self) -> None:
            self.decisions: list[str] = []
            self.generations: list[str] = []

        def __getattr__(self, name: str):
            return getattr(inner_router, name)

        async def stream(self, config, *, profile, messages, **kwargs) -> AsyncIterator:
            prompt = "\n".join(str(message.content or "") for message in messages)
            if "只输出一个 JSON 对象" in prompt:
                self.decisions.append(prompt)
                if len(self.decisions) == 1:
                    response = await logged_in.client.put(
                        "/v1/persona", json={"system_prompt": new_prompt}
                    )
                    assert response.status_code == 200, response.text
                text = '{"respond":true,"reason":"test","length":"normal"}'
            else:
                self.generations.append(prompt)
                text = "快照测试回复"
            yield ModelChunk(delta=text), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=text)

    async def available() -> Situation:
        return Situation(availability="available")

    router = ObservingRouter()
    logged_in.app.state.router = router
    monkeypatch.setattr("ling.kernel.autonomy.make_situation_reader", lambda *args, **kwargs: available)
    if initial_state == "read_error":
        from ling.domains.persona.service import PersonaService

        original_read = PersonaService._row_or_none
        failed = False

        async def fail_once(self, persona_id):  # noqa: ANN001, ANN202
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic persona read failure")
            return await original_read(self, persona_id)

        monkeypatch.setattr(PersonaService, "_row_or_none", fail_once)
    for turn_id in ("snapshot-first", "snapshot-next"):
        response = await logged_in.client.post(
            f"/v1/sessions/{window}/turns", json={"client_turn_id": turn_id, "text": "你好"}
        )
        assert response.status_code == 200 and "快照测试回复" in response.text, response.text

    assert len(router.decisions) == len(router.generations) == 2
    assert (old_prompt in router.decisions[0]) is (initial_state == "edit")
    assert (old_prompt in router.generations[0]) is (initial_state == "edit")
    assert new_prompt not in router.decisions[0]
    assert new_prompt not in router.generations[0]
    assert new_prompt in router.decisions[1] and old_prompt not in router.decisions[1]
    assert new_prompt in router.generations[1] and old_prompt not in router.generations[1]
    if initial_state == "read_error":
        assert "读取会话人格失败" in caplog.text
        assert "这不是注入链路故障" not in caplog.text
    elif initial_state == "create":
        assert "读取会话人格失败" not in caplog.text
        assert "本轮未注入人设" in caplog.text
