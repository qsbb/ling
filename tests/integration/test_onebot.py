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


async def test_no_secret_rejects_everything() -> None:
    """未配 secret → **一律拒绝**（fail-closed）。

    ## 为什么改掉了旧断言

    旧断言是 `verify_inbound({"X-Self-ID": "10001"}, b"{}") is True`，
    理由是"未配 secret 是降级状态（依赖网络隔离）"。这个前提在默认部署下
    不成立：`/v1/onebot/events` 没有任何 auth 依赖，6520 默认对外发布，
    `deploy/env.example` 里 `LING_ONEBOT_SECRET=` 默认就是空。于是任何能
    访问该端口的人伪造 `X-Self-ID` + `user_id=<白名单 QQ 号>` 就能以该用户
    身份对话、写记忆、让她向真实好友发言——完整的账号接管。

    `X-Self-ID` 是请求方自己填的，不构成凭据，所以"没有 secret"就等于
    "没有可信来源判据"，只能拒绝。旧断言保护的是一个错误行为，
    不能为了跑通而保留它。
    """
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", self_id="10001", instance_id="i")
    )
    # 带上"看起来完整"的头也照样拒绝：X-Self-ID 不是凭据
    assert await connector.verify_inbound({"X-Self-ID": "10001"}, b"{}") is False
    assert await connector.verify_inbound({}, b"{}") is False
    # 自己伪造一个签名也不行（没有密钥就没有正确的签名）
    assert (
        await connector.verify_inbound(
            {"X-Self-ID": "10001", "X-Signature": "sha1=deadbeef"}, b"{}"
        )
        is False
    )


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


# ------------------------------------------------------------------ 媒体下载约束


def _media_client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_media_rejects_host_not_in_whitelist() -> None:
    """白名单外的域名一律拒绝——**包括"以白名单域名结尾"的仿冒域名**。

    裸 `endswith("qpic.cn")` 会放行 `evilqpic.cn`：它同样以 `qpic.cn` 结尾。
    正确判定是 `host == h or host.endswith("." + h)`，即只有"本身等于"或
    "以 `.` 开头的子域名"才算命中。
    """
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://x",
            secret="s",
            self_id="10001",
            instance_id="i",
        ),
        http=_media_client(handler),
    )
    for url in (
        "https://evilqpic.cn/a.png",  # 裸 endswith 的经典绕过
        "https://qpic.cn.evil.com/a.png",  # 子域名位置被反过来占
        "http://127.0.0.1:8080/a.png",  # 内网地址
        "http://[::1]/a.png",  # 内网 IPv6
        "https://evil.example/a.png",
    ):
        with pytest.raises(ChannelError) as exc:
            await connector.fetch_media(url)
        assert exc.value.kind is ChannelErrorKind.rejected, f"{url} 没有被拒绝"
    assert called is False, "白名单外的地址竟然发出了真实请求"


async def test_fetch_media_allows_whitelisted_subdomain() -> None:
    """白名单域名的正常子域名要放行（QQ 图床都是子域名形态）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"png-bytes", headers={"content-type": "image/png"}
        )

    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", secret="s", self_id="10001", instance_id="i"),
        http=_media_client(handler),
    )
    data, mime = await connector.fetch_media("https://gchat.qpic.cn/a.png")
    assert data == b"png-bytes"
    assert mime == "image/png"


async def test_fetch_media_rejects_oversize_stream() -> None:
    """超过上限必须中断并报错，不能把整个响应读进内存。

    这里声明一个巨大的 content-length，断言在**读之前**就被拒——
    说明上限不是事后才检查的。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-length": str(64 * 1024 * 1024),
            },
            content=b"x",
        )

    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://x",
            secret="s",
            self_id="10001",
            instance_id="i",
            media_max_bytes=1024,
        ),
        http=_media_client(handler),
    )
    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://gchat.qpic.cn/big.png")
    assert exc.value.kind is ChannelErrorKind.rejected
    assert "上限" in exc.value.message


async def test_fetch_media_rejects_oversize_without_content_length() -> None:
    """没有 content-length 时也要靠流式计数拦住（头部不可信）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"y" * 4096, headers={"content-type": "image/png"}
        )

    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://x",
            secret="s",
            self_id="10001",
            instance_id="i",
            media_max_bytes=1024,
        ),
        http=_media_client(handler),
    )
    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://gchat.qpic.cn/big2.png")
    assert exc.value.kind is ChannelErrorKind.rejected
    assert "上限" in exc.value.message


async def test_fetch_media_rejects_non_http_scheme() -> None:
    """`file:` / `ftp:` 等非 http(s) 一律按不支持拒绝（本地路径不走这里）。"""
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", secret="s", self_id="10001", instance_id="i")
    )
    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("file:/etc/passwd")
    assert exc.value.kind is ChannelErrorKind.unsupported


async def test_fetch_media_rejects_non_default_port() -> None:
    """白名单域 + 非标准端口 → 必须拒（只校验 host 时 `:8443` 会被放行）。

    图床只有 443。端口不设约束时，一台把 `gchat.qpic.cn` 反代到内网的机器
    就能用 8443 把请求引到别处——白名单校验的是"域名是谁"，不是"连到哪"。
    """
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", secret="s", self_id="10001", instance_id="i"),
        http=_media_client(handler),
    )
    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://gchat.qpic.cn:8443/a.png")
    assert exc.value.kind is ChannelErrorKind.rejected
    assert called is False, "非标准端口竟然发出了真实请求"


async def test_fetch_media_allows_explicit_default_port() -> None:
    """显式写 `:443` 是默认端口，要放行——不是"带冒号就拒"。"""
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", secret="s", self_id="10001", instance_id="i"),
        http=_media_client(
            lambda request: httpx.Response(
                200, content=b"ok", headers={"content-type": "image/png"}
            )
        ),
    )
    data, _mime = await connector.fetch_media("https://gchat.qpic.cn:443/a.png")
    assert data == b"ok"


async def test_fetch_media_does_not_follow_redirects() -> None:
    """白名单域名 302 到内网 → **不能被跟随**，否则白名单形同虚设。

    `follow_redirects=False` 是这条的落地方式：httpx 默认不跟随，但调用方
    可能注入一个打开跟随的 client，所以显式写出来。
    """
    seen_redirect = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_redirect
        if str(request.url).startswith("https://gchat.qpic.cn/"):
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1:8080/secret"}
            )
        seen_redirect = True
        return httpx.Response(200, content=b"secret")

    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://x", secret="s", self_id="10001", instance_id="i"),
        http=_media_client(handler),
    )
    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://gchat.qpic.cn/redir.png")
    assert exc.value.kind is ChannelErrorKind.rejected
    assert seen_redirect is False, "重定向被跟随了：白名单可以被 302 绕过"
