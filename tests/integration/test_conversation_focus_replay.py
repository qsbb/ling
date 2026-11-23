"""Replay idle and continuous private conversations through the OneBot entry."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import Message, Window
from ling.kernel.bridge import WindowSendGate
from tests.integration.conftest import TestEnv
from tests.integration.test_onebot_loop import (
    FakeOneBot,
    _bind,
    _event_body,
    _install,
    _post,
    _prompt_for,
)

pytestmark = pytest.mark.w0_web


@pytest.mark.parametrize("opener", ["今天出门要带伞吗", "？"])
async def test_private_idle_gap_preserves_the_window_but_starts_from_current_topic(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, opener: str,
) -> None:
    async def no_delay(self, *, first):
        return None

    monkeypatch.setattr(WindowSendGate, "pace", no_delay)
    _install(logged_in, FakeOneBot())
    await _bind(logged_in)

    async def prompt(message_id: int, text: str) -> dict:
        return await _prompt_for((await _post(
            logged_in, _event_body(message_id=message_id, message=text),
        )).json())

    yesterday = "昨天的紫铜天线维修计划"
    await prompt(840, yesterday)
    async with session_scope() as session:
        window_id = await session.scalar(select(Window.id).where(Window.channel == "onebot"))
        assert window_id
        for message in (await session.scalars(
            select(Message).where(Message.window_id == window_id),
        )).all():
            message.created_at -= timedelta(days=1)

    fresh = await prompt(841, opener)
    assert fresh["context"]["history_messages"] == 0
    assert fresh["context"]["history_boundary"] == "idle_gap"
    assert all(yesterday not in item["content"] for item in fresh["messages"])
    followup = await prompt(842, "？")
    history = [item["content"] for item in followup["messages"] if item["lane"] == "history"]
    assert len(history) == 2 and opener in history[0]
    assert all(yesterday not in item for item in history)

    referenced = await prompt(843, "[CQ:reply,id=840] 这个呢？")
    quote, = [item for item in referenced["messages"] if item["lane"] == "quote"]
    assert yesterday in quote["content"]
    explicit = await prompt(844, "接着说昨天那个天线计划")
    assert any(
        yesterday in item["content"] for item in explicit["messages"] if item["lane"] == "history"
    )
    async with session_scope() as session:
        assert list(await session.scalars(select(Window.id).where(Window.channel == "onebot"))) == [window_id]
        assert await session.scalar(select(Message.id).where(Message.window_id == window_id, Message.text == yesterday))


async def test_recent_history_cuts_at_gap_inside_a_continuing_conversation(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_delay(self, *, first):
        return None

    monkeypatch.setattr(WindowSendGate, "pace", no_delay)
    _install(logged_in, FakeOneBot())
    await _bind(logged_in)

    older_topic = "早餐的栗子吐司"
    earlier_current = "现在在聊天台上适合看的星星"
    later_current = "刚才说晚点去天台"
    for number, line in enumerate((older_topic, earlier_current, later_current), start=850):
        await _prompt_for((await _post(
            logged_in, _event_body(message_id=number, message=line),
        )).json())
    async with session_scope() as session:
        now = datetime.now(UTC)
        for line, minutes_ago in ((older_topic, 400), (earlier_current, 160), (later_current, 80)):
            turn_id = await session.scalar(select(Message.turn_id).where(Message.text == line))
            assert turn_id
            for message in (await session.scalars(select(Message).where(Message.turn_id == turn_id))).all():
                message.created_at = now - timedelta(minutes=minutes_ago)

    current = await _prompt_for((await _post(
        logged_in, _event_body(message_id=853, message="那里可以看到哪颗星？"),
    )).json())
    history = [item["content"] for item in current["messages"] if item["lane"] == "history"]
    assert current["context"]["history_boundary"] == "idle_gap"
    assert any(earlier_current in line for line in history)
    assert any(later_current in line for line in history)
    assert all(older_topic not in line for line in history)
