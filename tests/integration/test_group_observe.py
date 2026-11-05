"""群里陌生人的消息：她**看得见**，但不主动开口。

改造前：白名单在入站第一道闸就把群消息杀了，消息根本不落库，
群聊轮询（`domains/attention`）永远拿不到内容——"偶尔看一眼群"
这件事无从发生。

这组测试钉住的正是那个边界：能进上下文 ≠ 能触发回复。
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import Message
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture
from tests.integration.test_onebot_loop import (  # noqa: PLC2701
    QQ_USER,
    FakeOneBot,
    _event_body,
    _install,
    _post,
)

pytestmark = pytest.mark.w0_web

STRANGER = 888888888
KNOWN_GROUP = 700000001
OTHER_GROUP = 777777777


def _group_env(env: TestEnv, *, groups: frozenset[str]) -> None:
    """白名单模式 + 指定白名单群。"""
    env.app.state.settings = dataclasses.replace(
        env.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({QQ_USER}),
        access_groups=groups,
    )


def _group_event(group_id: int, *, user_id: int = STRANGER) -> bytes:
    return _event_body(
        message_type="group",
        sub_type="normal",
        group_id=group_id,
        user_id=user_id,
        message="今晚吃啥",
        raw_message="今晚吃啥",
    )


async def _messages(env: TestEnv) -> list[Message]:
    async with session_scope() as session:
        return list((await session.execute(select(Message))).scalars())


async def test_stranger_in_known_group_is_persisted(logged_in: TestEnv) -> None:  # noqa: F811
    """白名单群里的陌生人：消息必须落库（她看得见），但不发回复。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    _group_env(logged_in, groups=frozenset({str(KNOWN_GROUP)}))

    response = await _post(logged_in, _group_event(KNOWN_GROUP))
    assert response.status_code == 200, response.text

    rows = await _messages(logged_in)
    texts = [r.text for r in rows if r.role == "user"]
    assert "今晚吃啥" in texts, "群消息必须落库，否则群聊轮询拿不到内容"
    assert not any(r.role == "actor" for r in rows), "不该主动回复陌生人"
    assert fake.sent == [], "不该调用发送接口"


async def test_stranger_in_unknown_group_is_persisted_too(logged_in: TestEnv) -> None:  # noqa: F811
    """不熟悉的群也看得见——人本来就会在各种群里潜水。但同样不开口。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    _group_env(logged_in, groups=frozenset({str(KNOWN_GROUP)}))

    await _post(logged_in, _group_event(OTHER_GROUP))

    rows = await _messages(logged_in)
    assert any(r.role == "user" and r.text == "今晚吃啥" for r in rows)
    assert not any(r.role == "actor" for r in rows)
    assert fake.sent == []


async def test_private_stranger_still_fully_denied(logged_in: TestEnv) -> None:  # noqa: F811
    """私聊仍是安全边界：陌生人连上下文都不进。

    这条最关键——"看得见群"不能顺带把私信的门也打开。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    _group_env(logged_in, groups=frozenset({str(KNOWN_GROUP)}))

    await _post(logged_in, _event_body(user_id=STRANGER, message="在吗"))

    rows = await _messages(logged_in)
    assert not any(r.text == "在吗" for r in rows), "私聊陌生人不能落库"
    assert fake.sent == []


async def test_owner_in_group_can_still_be_answered(logged_in: TestEnv) -> None:  # noqa: F811
    """白名单用户在群里被 @ 时照常能回——改造没有把主人也一起静音。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    _group_env(logged_in, groups=frozenset({str(OTHER_GROUP)}))

    # 主人（QQ_USER）在陌生群里 @ 她
    body = _event_body(
        message_type="group",
        sub_type="normal",
        group_id=OTHER_GROUP,
        user_id=int(QQ_USER),
        message=[{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 在吗"}}],
        raw_message="[CQ:at,qq=10001] 在吗",
    )
    response = await _post(logged_in, body)
    assert response.status_code == 200, response.text
    # 至少证明这条没被当成 observe_only 静默掉
    assert response.json()["accepted"] is True, response.text


# ------------------------------------------------ 拒绝日志开关（三态改造后）

@pytest.mark.asyncio
@pytest.mark.parametrize("log_denials", [True, False])
async def test_denied_private_still_rejected_and_counted(
    logged_in: TestEnv,  # noqa: F811
    log_denials: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**关日志不能连拒绝一起关掉**，且计数在两种情况下都要涨。

    三态改造后只有**私聊陌生人**会走到 `denied`，所以这条用私聊来钉。
    """
    from ling.kernel import inbound_intake

    events: list[str] = []
    monkeypatch.setattr("ling.audit.security_event", lambda a, **k: events.append(a))

    fake = FakeOneBot()
    _install(logged_in, fake)
    logged_in.app.state.settings = dataclasses.replace(
        logged_in.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({QQ_USER}),
        access_groups=frozenset(),
        log_access_denials=log_denials,
    )

    before = inbound_intake.denied_total()
    await _post(logged_in, _event_body(user_id=STRANGER, message="在吗"))

    assert inbound_intake.denied_total() == before + 1, "计数必须涨"
    assert bool([e for e in events if e == "inbound_denied"]) is log_denials
    # 无论开关如何，消息都不能落库
    rows = await _messages(logged_in)
    assert not any(r.text == "在吗" for r in rows)
    assert fake.sent == []
