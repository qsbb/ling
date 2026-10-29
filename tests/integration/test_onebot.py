"""OneBot V11 适配：协议解析、验签、发送、以及**完整对话闭环**。

不需要真实 QQ：用 httpx MockTransport 模拟 OneBot 实现，
用合成事件模拟平台上报。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.message import build_text_segments, parse_message, window_ref_for
from connectors.onebot.settings import OneBotConfig

from ling.contracts.v1 import DeliveryState
from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage, ReceiptQuery
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CONFIG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok-abc",
    secret="hmac-secret",
    self_id="10001",
    instance_id="onebot-test",
)


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _event(**overrides) -> dict:
    base = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 555,
        "user_id": 20002,
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {"nickname": "小不点", "sex": "male", "age": 18},
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ 消息解析


def test_parse_cq_string() -> None:
    parsed = parse_message("你好[CQ:at,qq=10001] 在吗", self_id="10001")
    assert parsed.mentions_self is True
    # @机器人 本身不该进正文
    assert "10001" not in parsed.text
    assert "你好" in parsed.text and "在吗" in parsed.text


def test_parse_array_segments() -> None:
    parsed = parse_message(
        [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": " 帮我看看"}},
        ],
        self_id="10001",
    )
    assert parsed.mentions_self is True
    assert "帮我看看" in parsed.text


def test_parse_other_person_at_kept() -> None:
    """@别人要保留（群里可能是在叫别人，不是叫我）。"""
    parsed = parse_message("[CQ:at,qq=30003] 你怎么看", self_id="10001")
    assert parsed.mentions_self is False
    assert "30003" in parsed.text


def test_parse_media_extracted() -> None:
    parsed = parse_message("[CQ:image,file=abc.jpg] 这是什么", self_id="10001")
    assert parsed.media_refs == ["abc.jpg"]
    assert "这是什么" in parsed.text


def test_parse_reply_reference() -> None:
    parsed = parse_message("[CQ:reply,id=777] 同意", self_id="10001")
    assert parsed.reply_to_message_id == "777"


def test_parse_cq_escape() -> None:
    """CQ 码里的 &#91; 等实体要还原，否则正文会出现乱码。"""
    parsed = parse_message("数组是&#91;1,2&#93;", self_id="10001")
    assert parsed.text == "数组是[1,2]"


def test_build_segments_avoids_cq_injection() -> None:
    """发送用数组格式：用户文本里的 [CQ: 不会被当成指令。"""
    segments = build_text_segments("看这个 [CQ:at,qq=all]")
    assert segments == [{"type": "text", "data": {"text": "看这个 [CQ:at,qq=all]"}}]


def test_group_window_shared_across_members() -> None:
    """群窗口只由群号决定，不含发言人。

    否则同一个群的不同成员各拿一个窗口，角色看不到完整群聊，
    也就无法判断该不该接话。
    """
    a = window_ref_for(is_group=True, target_id="88888", instance_id="onebot-test")
    assert a == "onebot-test:group:88888"
    assert "20002" not in a


# ------------------------------------------------------------------ 验签


async def test_signature_valid_accepted() -> None:
    connector = OneBotV11Connector(CONFIG)
    body = json.dumps(_event()).encode()
    sig = hmac.new(b"hmac-secret", body, hashlib.sha1).hexdigest()
    ok = await connector.verify_inbound(
        {"X-Signature": f"sha1={sig}", "X-Self-ID": "10001"}, body
    )
    assert ok is True


async def test_signature_invalid_rejected() -> None:
    connector = OneBotV11Connector(CONFIG)
    body = json.dumps(_event()).encode()
    ok = await connector.verify_inbound(
        {"X-Signature": "sha1=deadbeef", "X-Self-ID": "10001"}, body
    )
    assert ok is False


async def test_signature_missing_rejected() -> None:
    """配了 secret 却没有签名 → 必须拒绝（不能当成未加密上报）。"""
    connector = OneBotV11Connector(CONFIG)
    body = b"{}"
    assert await connector.verify_inbound({"X-Self-ID": "10001"}, body) is False


async def test_wrong_self_id_rejected() -> None:
    """别的机器人误报到同一入口 → 拒绝。"""
    connector = OneBotV11Connector(CONFIG)
    body = b"{}"
    sig = hmac.new(b"hmac-secret", body, hashlib.sha1).hexdigest()
    ok = await connector.verify_inbound(
        {"X-Signature": f"sha1={sig}", "X-Self-ID": "99999"}, body
    )
    assert ok is False


async def test_no_secret_allows_unsigned_but_requires_self_id() -> None:
    """未配 secret 是降级状态（依赖网络隔离），但仍要 X-Self-ID。"""
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", self_id="10001", instance_id="i")
    )
    assert await connector.verify_inbound({"X-Self-ID": "10001"}, b"{}") is True
    assert await connector.verify_inbound({}, b"{}") is False


# ------------------------------------------------------------------ 归一化


async def test_normalize_private(logged_in: TestEnv) -> None:
    connector = OneBotV11Connector(CONFIG)
    envelope = connector.normalize_inbound(_event())
    assert envelope.window_ref == "onebot-test:private:20002"
    assert envelope.account_ref == "20002"
    assert envelope.external_event_id == "10001:555"
    assert envelope.parts and envelope.parts[0].text == "你好"


async def test_normalize_group(logged_in: TestEnv) -> None:
    connector = OneBotV11Connector(CONFIG)
    envelope = connector.normalize_inbound(
        _event(message_type="group", group_id=88888, sub_type="normal")
    )
    assert envelope.window_ref == "onebot-test:group:88888"
    # 发言人仍然是 account_ref，窗口是群
    assert envelope.account_ref == "20002"


async def test_normalize_rejects_non_message_event(logged_in: TestEnv) -> None:
    """心跳/通知暂不进对话链路，但要让调用方知道是"跳过"而非"出错"。"""
    connector = OneBotV11Connector(CONFIG)
    with pytest.raises(ChannelError) as exc:
        connector.normalize_inbound({"post_type": "meta_event"})
    assert exc.value.kind is ChannelErrorKind.unsupported


# ------------------------------------------------------------------ 发送


async def test_send_private_uses_correct_action() -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                        "data": {"message_id": 12345}})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    receipt = await connector.send(
        OutboundMessage(
            operation_id="op_1", window_ref="i:private:20002", text="你好"
        )
    )
    assert seen["url"].endswith("/send_private_msg")
    assert seen["auth"] == "Bearer tok-abc"
    assert seen["body"]["user_id"] == "20002"
    assert receipt.platform_message_id == "12345"
    assert receipt.state is DeliveryState.delivered


async def test_send_group_uses_group_action() -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                        "data": {"message_id": 999}})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    await connector.send(
        OutboundMessage(
            operation_id="op_2", window_ref="i:group:88888", text="大家好", is_group=True
        )
    )
    assert seen["url"].endswith("/send_group_msg")
    assert seen["body"]["group_id"] == "88888"


async def test_auth_failure_maps_to_unauthorized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(operation_id="op", window_ref="i:private:1", text="x")
        )
    assert exc.value.kind is ChannelErrorKind.unauthorized
    assert exc.value.retriable is False


async def test_unknown_action_maps_to_unsupported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(operation_id="op", window_ref="i:private:1", text="x")
        )
    assert exc.value.kind is ChannelErrorKind.unsupported


async def test_async_retcode_is_uncertain_not_failure() -> None:
    """status=async 表示"已提交但结果未知"，不能当失败重发。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "async", "retcode": 1, "data": None})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(operation_id="op", window_ref="i:private:1", text="x")
        )
    assert exc.value.kind is ChannelErrorKind.uncertain


async def test_unreachable_is_retriable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(operation_id="op", window_ref="i:private:1", text="x")
        )
    assert exc.value.kind is ChannelErrorKind.unreachable
    assert exc.value.retriable is True


async def test_query_receipt_unknown_when_not_found() -> None:
    """查不到消息 → unknown，不能当成 failed（否则会重发）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed", "retcode": 1404, "data": None})

    connector = OneBotV11Connector(CONFIG, http=_client(handler))
    receipt = await connector.query_receipt(
        ReceiptQuery(operation_id="op", platform_message_id="999")
    )
    assert receipt.state is DeliveryState.uncertain


# ------------------------------------------------------------------ 能力声明


def test_capabilities_reports_group_support() -> None:
    caps = OneBotV11Connector(CONFIG).capabilities()
    assert caps.supports_group is True
    assert caps.supports_private is True
    # 主动推送取决于实现与账号权限，默认关闭
    assert caps.supports_proactive is False
    assert caps.enabled is True


def test_render_output_splits_over_limit() -> None:
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", max_text_length=10, instance_id="i")
    )
    parts = connector.render_output("一" * 25, modalities=[])
    assert len(parts) == 3
    assert all(len(p) <= 10 for p in parts)
    assert "".join(parts) == "一" * 25
