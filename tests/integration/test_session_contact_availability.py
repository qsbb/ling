"""OneBot 群成员通知和好友表在会话列表中的显示。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.contracts.v1 import ContactAvailability, ContactStatusEvent
from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import Window
from ling.kernel import inbound_intake
from ling.ports.channel import ChannelError, ChannelErrorKind
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-contact",
)
GROUP = "99999"
PEER = "20003"
SELF_ID = "10001"


def _decrease(*, sub_type: str, user_id: str, group_id: str = GROUP) -> dict:
    """一条 `notice.group_decrease`（字段按 OneBot v11 notice.md）。"""
    return {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": sub_type,
        "group_id": int(group_id),
        "user_id": int(user_id),
        "operator_id": 0,
        "self_id": int(SELF_ID),
        "time": 1_700_000_000,
    }


# ------------------------------------------------------------------ 1. 判定


def test_connector_maps_kick_me_to_contact_status_event() -> None:
    """机器人被踢（`kick_me`）→ `ContactStatusEvent`，原因记 `kicked`。"""
    connector = OneBotV11Connector(CFG)
    event = connector.normalize_inbound(_decrease(sub_type="kick_me", user_id=SELF_ID))

    assert isinstance(event, ContactStatusEvent), type(event)
    assert event.availability is ContactAvailability.kicked
    assert event.window_ref == f"{CFG.instance_id}:group:{GROUP}"
    assert event.target_ref == GROUP
    assert event.external_event_id == (
        f"{SELF_ID}:group_decrease:{GROUP}:kick_me:1700000000.0"
    )


def test_connector_maps_self_leave_to_contact_status_event() -> None:
    """机器人**自己退群**（`leave` + `user_id == self_id`）→ 原因记 `left`。

    这条与上一条必须分开：`kicked` 与 `left` 在界面上处置相同，但把
    "被踢"记成"自己退的"是错误归属（与撤回路径 `self_recall` 单列同理）。
    """
    connector = OneBotV11Connector(CFG)
    event = connector.normalize_inbound(_decrease(sub_type="leave", user_id=SELF_ID))

    assert isinstance(event, ContactStatusEvent), type(event)
    assert event.availability is ContactAvailability.left


def test_connector_ignores_decrease_of_other_members() -> None:
    """**别人**退群/被踢：仍然按"已知且不重要"静默，不得产生状态事件。

    这是防误伤的那一半：活跃群里每天有人退群，把那些也记成"我们不在了"
    会让整个群列表变灰。
    """
    connector = OneBotV11Connector(CFG)
    for sub_type in ("leave", "kick"):
        with pytest.raises(ChannelError) as exc:
            connector.normalize_inbound(_decrease(sub_type=sub_type, user_id="30004"))
        assert exc.value.kind is ChannelErrorKind.ignored, sub_type


def test_connector_ignores_unknown_decrease_subtype() -> None:
    """规范外的 `sub_type`：不动状态（宁可漏一次，也不要灰掉一个正常群）。"""
    connector = OneBotV11Connector(CFG)
    with pytest.raises(ChannelError) as exc:
        connector.normalize_inbound(_decrease(sub_type="mystery", user_id=SELF_ID))
    assert exc.value.kind is ChannelErrorKind.ignored


# ------------------------------------------------------------------ 2. 持久化


async def _make_group_window(env, *, title: str | None = None) -> str:
    reference = title or f"{CFG.instance_id}:group:{GROUP}"
    async with session_scope() as session:
        window = await ConversationService(session).create_window(
            subject_id=env.subject_id, title=reference, channel="onebot"
        )
        window.updated_at = datetime.now(UTC) - timedelta(days=3)
        await session.flush()
        return str(window.id)


def _kick_event(reference: str) -> ContactStatusEvent:
    return ContactStatusEvent(
        connector_instance=CFG.instance_id,
        external_event_id=f"{CFG.instance_id}:group_decrease:{GROUP}:kick_me",
        window_ref=reference,
        target_ref=GROUP,
        availability=ContactAvailability.kicked,
        user_ref=SELF_ID,
        occurred_at=datetime.now(UTC),
    )


async def test_contact_status_is_persisted_without_touching_activity(logged_in) -> None:  # noqa: F811
    """成员变动保留原有的会话更新时间。"""
    window_id = await _make_group_window(logged_in)
    async with session_scope() as session:
        before = (await session.get(Window, window_id)).updated_at

    reply = await inbound_intake._process_contact_status(
        _kick_event(f"{CFG.instance_id}:group:{GROUP}")
    )
    assert reply["accepted"] is True and reply["window_matched"] is True

    async with session_scope() as session:
        window = await session.get(Window, window_id)
        assert window is not None
        assert window.contact_unavailable_reason == ContactAvailability.kicked.value
        assert window.contact_unavailable_at is not None
        assert window.updated_at == before, "标记不可用不该改动最近活跃时间"


async def test_contact_status_is_idempotent(logged_in) -> None:  # noqa: F811
    """平台重投同一条事件：第二次返回 duplicate，数据只变一次。"""
    window_id = await _make_group_window(logged_in)
    reference = f"{CFG.instance_id}:group:{GROUP}"

    event = _kick_event(reference)
    first = await inbound_intake._process_contact_status(event)
    second = await inbound_intake._process_contact_status(event)

    assert first["accepted"] is True and not first.get("duplicate")
    assert second.get("duplicate") is True
    async with session_scope() as session:
        assert (await session.get(Window, window_id)).contact_unavailable_reason is not None


async def test_contact_status_for_unknown_window_is_not_an_error(logged_in) -> None:  # noqa: F811
    """没有对应窗口时：接受、不建窗口、不报错。

    机器人可能在建窗口之前就被踢（入站全被白名单挡掉）。那时没有东西要
    标灰，但事件本身是合法的——把它判成失败会让平台反复重投。
    """
    event = _kick_event(f"{CFG.instance_id}:group:123456")
    reply = await inbound_intake._process_contact_status(event)

    assert reply["accepted"] is True
    assert reply["window_matched"] is False
    async with session_scope() as session:
        rows = list(
            (await session.execute(select(Window).where(Window.title == event.window_ref))).scalars()
        )
    assert rows == [], "不该为一条联系通知凭空建窗口"


# ------------------------------------------------------------------ 3. 呈现


async def test_list_exposes_contact_unavailable(logged_in) -> None:  # noqa: F811
    """/v1/sessions 带出 `contact_unavailable`，正常会话为 `None`。

    两侧都要断言：只有"灰的那条带原因"还不够——如果实现把每条都写成
    `kicked`，这条负向断言（正常的为 None）会红。反过来如果只断言正常
    会话是 None，去掉写入后本用例照样绿，所以两条必须同时存在。
    """
    grey_id = await _make_group_window(logged_in)
    async with session_scope() as session:
        web = await ConversationService(session).create_window(
            subject_id=logged_in.subject_id, title="网页会话"
        )
        web_id = str(web.id)

    await inbound_intake._process_contact_status(
        _kick_event(f"{CFG.instance_id}:group:{GROUP}")
    )

    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200, response.text
    by_id = {row["session_id"]: row for row in response.json()["sessions"]}

    assert by_id[grey_id]["contact_unavailable"] == ContactAvailability.kicked.value
    assert by_id[web_id]["contact_unavailable"] is None
    result = await logged_in.client.get(f"/v1/sessions/{grey_id}")
    assert result.status_code == 200, result.text
    assert result.json()["contact_unavailable"] == "kicked"


async def test_rejoin_clears_old_status_and_next_kick_is_applied(logged_in: TestEnv) -> None:  # noqa: F811
    connector = OneBotV11Connector(CFG)
    window_id = await _make_group_window(logged_in)
    kick = _decrease(sub_type="kick_me", user_id=SELF_ID)
    join = {**kick, "notice_type": "group_increase", "sub_type": "invite", "time": kick["time"] + 2}
    stale_kick = {**kick, "time": kick["time"] + 1}
    second_kick = {**kick, "time": kick["time"] + 3}

    async def notice(payload: dict) -> dict:
        return await inbound_intake.process_inbound_payload(
            logged_in.app, connector, payload, trusted_instance_key=("onebot", "default")
        )

    async def status() -> str | None:
        result = await logged_in.client.get(f"/v1/sessions/{window_id}")
        assert result.status_code == 200, result.text
        return result.json()["contact_unavailable"]

    assert (await notice(kick))["window_matched"] is True
    assert await status() == "kicked"
    assert (await notice({**join, "user_id": 30004}))["accepted"] is False
    assert await status() == "kicked"
    assert (await notice(join))["window_matched"] is True
    assert await status() is None
    assert (await notice(stale_kick))["window_matched"] is False
    assert await status() is None
    assert (await notice(second_kick))["window_matched"] is True
    assert await status() == "kicked"


async def test_friend_status_uses_one_snapshot_per_connector(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    connector = OneBotV11Connector(CFG)
    logged_in.app.state.onebot_connector = connector
    connector.friend_names_ttl = 0
    connector.friend_names_miss_ttl = 0
    response: list[dict] | None = [{"user_id": int(PEER), "nickname": "", "remark": ""}]
    requests = 0

    async def peer_list(action: str, params: dict) -> list[dict]:
        nonlocal requests
        assert action == "get_friend_list"
        requests += 1
        if response is None:
            raise ChannelError(ChannelErrorKind.unreachable, "连接失败")
        return response

    monkeypatch.setattr(connector, "_call", peer_list)
    async with session_scope() as session:
        service = ConversationService(session)
        friend = await service.create_window(
            subject_id=logged_in.subject_id,
            title=f"{CFG.instance_id}:private:{PEER}",
            channel="onebot",
        )
        friend.display_name_override = "好友别名"
        temporary = await service.create_window(
            subject_id=logged_in.subject_id,
            title=f"{CFG.instance_id}:private:20004",
            channel="onebot",
        )
        friend_id, temporary_id = friend.id, temporary.id

    async def unavailable() -> tuple[str | None, str | None]:
        result = await logged_in.client.get("/v1/sessions")
        assert result.status_code == 200, result.text
        rows = {row["session_id"]: row for row in result.json()["sessions"]}
        assert rows[friend_id]["display_name"] == "好友别名"
        return rows[friend_id]["contact_unavailable"], rows[temporary_id]["contact_unavailable"]

    assert await unavailable() == (None, None)
    assert connector.was_friend(PEER)
    assert requests == 1
    response = None
    assert await unavailable() == (None, None)
    response = []
    assert await unavailable() == ("friend_deleted", None)
    response = [{"user_id": int(PEER), "nickname": "阿岚"}]
    assert await unavailable() == (None, None)


async def test_contact_status_event_is_not_a_message(logged_in) -> None:  # noqa: F811
    """它是"不是消息"的类型，不能进对话链路。

    只断言类型还不够：真正的风险是有人把空信封塞进 bridge，于是群里凭空
    多一条空发言。所以这里断言窗口一条消息都没多。
    """
    from ling.infra.models import Message

    window_id = await _make_group_window(logged_in)
    reply = await inbound_intake._process_contact_status(
        _kick_event(f"{CFG.instance_id}:group:{GROUP}")
    )
    assert reply["accepted"] is True

    async with session_scope() as session:
        messages = list(
            (await session.execute(select(Message).where(Message.window_id == window_id))).scalars()
        )
    assert messages == [], "联系状态通知不该产生消息行"


def test_contract_round_trips_through_json() -> None:
    """契约过得了 JSON 边界（前端读到的是这个形状）。"""
    event = ContactStatusEvent(
        connector_instance=CFG.instance_id,
        external_event_id="x:1",
        window_ref=f"{CFG.instance_id}:group:{GROUP}",
        availability=ContactAvailability.left,
    )
    payload = json.loads(event.model_dump_json())
    assert payload["availability"] == "left"
    assert ContactStatusEvent.model_validate(payload).availability is ContactAvailability.left
