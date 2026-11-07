"""群名片与昵称：连接器必须**分开带**，而且入站零外呼。

为什么分开：``sender_name`` 是"这个群里此刻该叫他什么"（群名片优先），
``sender_nickname`` 是"这个 QQ 号自己的名字"（人级、跨群稳定）。落库更新
``subjects.display_name`` 只能用后者——用群名片写人级列会在群之间抖动，
还会把别人的私聊标题一起改掉（见 docs/GROUP-CARD-RESEARCH.md §C.1）。

还要钉住两件"照抄 AstrBot 会踩"的事：**不许编 ``"N/A"`` 占位符**；
**入站路径不许补拉 ``get_group_member_info``**（零外呼）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig


def _connector(handler=None) -> OneBotV11Connector:  # noqa: ANN001
    config = OneBotConfig(
        api_base="http://onebot.test:5700",
        access_token="",
        self_id="10001",
        instance_id="onebot-unit",
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return OneBotV11Connector(config, http=http)


def _group_event(**sender) -> dict:  # noqa: ANN003
    return {
        "time": 0,
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1,
        "user_id": 20002,
        "group_id": 700000001,
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "raw_message": "在吗",
        "sender": {"user_id": 20002, **sender},
    }


def test_card_and_nickname_are_carried_separately() -> None:
    envelope = _connector().normalize_inbound(
        _group_event(card="阿懒", nickname="阿岚")
    )
    # 本次称呼用群名片（群里该这么叫）
    assert envelope.sender_name == "阿懒"
    # 人级字段用昵称（落库只认它）
    assert envelope.sender_nickname == "阿岚"


def test_empty_card_falls_back_to_nickname() -> None:
    envelope = _connector().normalize_inbound(_group_event(card="", nickname="阿岚"))
    assert envelope.sender_name == "阿岚"
    assert envelope.sender_nickname == "阿岚"


def test_blank_fields_become_none_not_a_placeholder() -> None:
    """名片和昵称都空 → 都是 None。**绝不**写 "N/A"（AstrBot 的坑）。"""
    envelope = _connector().normalize_inbound(_group_event(card="  ", nickname=""))
    assert envelope.sender_name is None
    assert envelope.sender_nickname is None


def test_missing_sender_object_does_not_break() -> None:
    payload = _group_event()
    payload.pop("sender")
    envelope = _connector().normalize_inbound(payload)
    assert envelope.sender_name is None
    assert envelope.sender_nickname is None


def test_missing_sender_still_accepts_the_message() -> None:
    """拿不到名字**不是**拒收的理由：账号与窗口照常带上，只缺名字。"""
    payload = _group_event()
    payload.pop("sender")
    envelope = _connector().normalize_inbound(payload)
    assert envelope.account_ref == "20002"
    assert envelope.window_ref == "onebot-unit:group:700000001"
    assert envelope.sender_name is None


def test_private_message_has_no_card() -> None:
    """私聊事件规范里没有 ``card``：本次称呼自然退回昵称。"""
    event = _group_event(nickname="阿岚")
    event.update({"message_type": "private", "sub_type": "friend"})
    event.pop("group_id")
    envelope = _connector().normalize_inbound(event)
    assert envelope.sender_name == "阿岚"
    assert envelope.sender_nickname == "阿岚"


def test_names_are_trimmed_and_capped() -> None:
    envelope = _connector().normalize_inbound(
        _group_event(card="  阿懒  ", nickname="n" * 300)
    )
    assert envelope.sender_name == "阿懒"
    assert envelope.sender_nickname is not None
    assert len(envelope.sender_nickname) == 128


@pytest.mark.asyncio
async def test_normalize_inbound_makes_no_outbound_calls() -> None:
    """**入站零外呼**：不补拉 ``get_group_member_info``。

    这条是副作用断言：名字已经从事件里白拿到了，任何外呼都只是拖慢入站
    并增加风控暴露面（AstrBot 也只在 @ 提人时才拉）。
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    connector = _connector(handler)
    envelope = connector.normalize_inbound(_group_event(card="阿懒", nickname="阿岚"))

    assert envelope.sender_name == "阿懒"
    assert calls == [], f"入站不该有任何 API 调用：{calls}"


def test_envelope_json_keeps_both_names() -> None:
    """契约序列化：两个字段都在（旧连接器不发也不会报错）。"""
    envelope = _connector().normalize_inbound(_group_event(card="阿懒", nickname="阿岚"))
    dumped = json.loads(envelope.model_dump_json())
    assert dumped["sender_name"] == "阿懒"
    assert dumped["sender_nickname"] == "阿岚"


def test_old_envelope_without_nickname_still_validates() -> None:
    """向后兼容：老连接器（或旧测试桩）只给 ``sender_name`` 也能构造信封。"""
    from ling.contracts.v1 import InteractionEnvelope

    envelope = InteractionEnvelope(
        connector_instance="onebot-unit",
        external_event_id="evt-1",
        window_ref="onebot-unit:group:700000001",
        account_ref="20002",
        sender_name="阿岚",
    )
    assert envelope.sender_nickname is None
