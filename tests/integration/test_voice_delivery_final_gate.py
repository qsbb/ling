"""Recheck OneBot voice authorization at the final bridge delivery entry."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from ling.infra.db import session_scope
from ling.infra.models import Message, Turn, Window

pytestmark = pytest.mark.w0_web


class _DeliveryProbe:
    calls: list[tuple[str, str | None, bool]] = []

    def __init__(self, _session, *, settings) -> None:  # noqa: ANN001
        self.settings = settings

    async def deliver(
        self,
        message: Message,
        *,
        connector: object,
        window_ref: str,
        subject_id: str | None,
        is_group: bool,
    ) -> bool:
        self.calls.append((message.id, subject_id, is_group))
        return True


async def _deliver_voice(
    web_env,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn_state: str = "completed",
    archived: bool = False,
    newer_user_message: bool = False,
) -> tuple[tuple[bool, int, int], Message]:
    from ling.capabilities import voice_reply as voice_reply_module

    _DeliveryProbe.calls = []
    monkeypatch.setattr(voice_reply_module, "VoiceReplyService", _DeliveryProbe)

    suffix = uuid.uuid4().hex
    window_id = f"voice_gate_window_{suffix}"
    turn_id = f"voice_gate_turn_{suffix}"
    message_id = f"voice_gate_message_{suffix}"

    async with session_scope() as session:
        window = Window(
            id=window_id,
            actor_id=web_env.settings.actor_id,
            persona_id=web_env.settings.persona_id,
            subject_id=f"voice_gate_subject_{suffix}",
            title="OneBot 语音最终授权验收",
            channel="onebot",
            visibility="subject_private",
            archived=archived,
        )
        turn = Turn(
            id=turn_id,
            window_id=window_id,
            client_turn_id=turn_id,
            state=turn_state,
        )
        voice_message = Message(
            id=message_id,
            window_id=window_id,
            turn_id=turn_id,
            role="actor",
            text="只能通过授权语音通道投递的正文",
            output_modality="voice",
            speech_state="ready",
            speech_delivery_state="pending",
            state="completed",
            sequence=1,
            origin="assistant_reply",
        )
        session.add(window)
        await session.flush()
        session.add(turn)
        await session.flush()
        session.add(voice_message)
        await session.flush()
        if newer_user_message:
            session.add(
                Message(
                    id=f"voice_gate_new_user_{suffix}",
                    window_id=window_id,
                    role="user",
                    text="新的用户输入",
                    state="completed",
                    sequence=2,
                    origin="user_statement",
                )
            )
            await session.flush()

        from ling.kernel.bridge import InboundBridge

        bridge = InboundBridge(
            session,
            coordinator=object(),
            connector=object(),
            secret_box=object(),
            settings=SimpleNamespace(),
        )
        result = await bridge._deliver(  # noqa: SLF001
            envelope=SimpleNamespace(window_ref="onebot:private:100200300"),
            text=voice_message.text,
            subject_id="voice_gate_subject",
            turn_id=turn_id,
        )
        await session.refresh(voice_message)
        return result, voice_message


async def test_completed_current_turn_reaches_voice_delivery(
    web_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, message = await _deliver_voice(web_env, monkeypatch)

    assert result == (True, 1, 1)
    assert _DeliveryProbe.calls == [(message.id, "voice_gate_subject", False)]


@pytest.mark.parametrize(
    "case",
    ["incomplete_turn", "archived_window", "newer_user_message"],
)
async def test_stale_voice_turn_is_cancelled_before_delivery(
    web_env,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    options = {
        "incomplete_turn": {"turn_state": "streaming"},
        "archived_window": {"archived": True},
        "newer_user_message": {"newer_user_message": True},
    }[case]
    result, message = await _deliver_voice(web_env, monkeypatch, **options)

    assert result == (False, 0, 1)
    assert _DeliveryProbe.calls == []
    assert message.speech_delivery_state == "cancelled"
