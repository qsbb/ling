"""会话置灰：群被退/被踢、好友已删时，那一行要"还在、但看得出来不在了"。

## 这一层要守住什么

用户原话：「把已退出或者已不在的群聊，或者已删除的好友置灰处理」。拆开是
三条互不替代的断言：

1. **判定**：`notice.group_decrease` 里只有"我们不在这个群了"才产生状态事件；
   别人退群、被踢与我们的窗口无关，不能把正常群灰掉（`test_..._only_for_self`）。
2. **持久化**：事件只来一次，所以必须落库；且**绝不动 `updated_at`**——
   它是列表排序键，改它会让刚被踢的群跳到最前面。
3. **呈现**：`GET /v1/sessions` 要带出 `contact_unavailable`，且**仍然可点**
   （前端只降透明度，条目本身不隐藏）。

## 为什么"好友已删"不在这条链路里

OneBot v11 没有 `friend_decrease` 通知（NapCat 也不推），所以好友那一侧
只能读侧比对 `get_friend_list`。那条路径的关键风险是**采集失败被当成
"全都删了"**，本文件用"空表不判"与"有一条已删"两条用例分别钉住两侧。
"""

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
from tests.integration.conftest import logged_in  # noqa: F401 - fixture

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
    # 去重键带群号与子类型：平台重投同一条不能产生第二条日志。
    assert event.external_event_id == (
        f"{CFG.instance_id}:group_decrease:{GROUP}:kick_me"
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
    """写入 `contact_unavailable_*`，**且 `updated_at` 一动不动**。

    后半句是本用例真正的价值：`updated_at` 是列表排序键，若被写成"现在"，
    这个刚被踢的群会跳到列表最前面——用户最不需要看到的那条。
    """
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

    first = await inbound_intake._process_contact_status(_kick_event(reference))
    second = await inbound_intake._process_contact_status(_kick_event(reference))

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


async def test_friend_list_missing_peer_marks_friend_deleted() -> None:
    """好友表里查不到对端 → `friend_deleted`；空表（采集失败）不判。

    这两半缺一不可：只测"查不到就标"，去掉"空表不判"的守卫后本用例仍绿；
    只测"空表不判"，实现完全不做判定也绿。所以两条必须一起跑。
    """
    from ling.api.routes.sessions import _friend_availability

    class _Connector:
        def __init__(self, table: dict[str, tuple[str | None, str | None]]) -> None:
            self.table = table

        async def fetch_friend_names(self):
            return self.table

    class _App:
        def __init__(self, connector: _Connector) -> None:
            self.state = type("S", (), {})()
        # 连接器查找走 `connector_for_namespace(app, ...)`，由下面 patch 掉。

    window = Window(
        id="win_private",
        actor_id="actor",
        subject_id="subj_peer",
        title=f"{CFG.instance_id}:private:{PEER}",
        channel="onebot",
        visibility="subject_private",
        connector_window_ref=f"{CFG.instance_id}:private:{PEER}",
        origin_status="known",
        origin_scope="onebot",
        origin_instance_id=CFG.instance_id,
    )

    import ling.capabilities.channels as channels

    original = channels.connector_for_namespace
    try:
        class _R:
            def __init__(self) -> None:
                self.app = object()

        async def run(table: dict[str, tuple[str | None, str | None]]) -> dict[str, str]:
            channels.connector_for_namespace = lambda *a, **k: _Connector(table)  # type: ignore[assignment]
            return await _friend_availability(_R(), [window])

        # 1) 表里没有这个人 → 已删
        assert await run({"100200300": (None, "别人")}) == {
            "win_private": ContactAvailability.friend_deleted.value
        }
        # 2) 空表 = 采集失败，**不判**（否则一次超时会把整列私聊灰掉）
        assert await run({}) == {}
        # 3) 表里有这个人 → 正常
        assert await run({PEER: (None, "阿岚")}) == {}
    finally:
        channels.connector_for_namespace = original  # type: ignore[assignment]


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
