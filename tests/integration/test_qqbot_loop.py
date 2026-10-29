"""QQ 官方机器人（QQ 开放平台）闭环：Webhook 入站 → 统一受理 → 决策 → 被动回复。

**没有连过真实平台**：全部用 ``httpx.MockTransport`` 模拟开放平台，
凭据是假的。这里能证明的是"协议翻译与失败分类按设计工作"，
不能证明"已与 QQ 平台联通"——后者需要真实 app_id/secret 与已开通的权限。

覆盖的关键点：
- openid → ``account_ref``（**不是 QQ 号**）、窗口标识、去重键
- token 提前刷新 + 401 一次性重试
- 成功必须拿到平台真实消息 id；平台报错必须抛 ``ChannelError`` 而不是"假装送达"
- 超长文本分片（不丢字、不静默截断）
- 白名单在写库前拦截（复用 ``ling.kernel.access``）
- 主动推送默认不可声明（被动回复有次数/时效限制）
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from connectors.qqbot.connector import QQBotConnector, ed25519_seed
from connectors.qqbot.message import split_text, target_from_window
from connectors.qqbot.settings import PROD_API_BASE, QQBotConfig
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from ling.contracts.v1 import Modality
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Message, Window
from ling.ports.channel import (
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    ReceiptQuery,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

APP_ID = "102000001"
CLIENT_SECRET = "fake-client-secret"
# Webhook 签名密钥（平台侧的 BotSecret）。假值，只用于本地验签链路。
BOT_SECRET = "fake-bot-secret"
OPENID = "user-openid-AAA"
OTHER_OPENID = "user-openid-BBB"
GROUP_OPENID = "group-openid-GGG"

CFG = QQBotConfig(
    app_id=APP_ID,
    client_secret=CLIENT_SECRET,
    instance_id="qqbot-loop",
    sandbox=False,
    api_base=PROD_API_BASE,
    token_url="https://bots.qq.com/app/getAppAccessToken",
    bot_secret=BOT_SECRET,
)


class FakeQQBot:
    """模拟 QQ 开放平台：换 token + 两个发消息接口。

    ``errors``：按顺序弹出的失败响应，用来验证"平台报错必须如实变成失败"
    与"5xx 只能算 uncertain"。
    """

    def __init__(
        self,
        *,
        expires_in: int | str = 7200,
        errors: list[httpx.Response] | None = None,
        unauthorized_once: bool = False,
        omit_message_id: bool = False,
    ) -> None:
        self.token_requests = 0
        self.sends: list[dict] = []
        self._expires_in = expires_in
        self._errors = list(errors or [])
        self._unauthorized_once = unauthorized_once
        self._omit_message_id = omit_message_id

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/app/getAppAccessToken"):
            self.token_requests += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.token_requests}",
                    "expires_in": self._expires_in,
                },
            )

        body = json.loads(request.content or b"{}")
        self.sends.append(
            {
                "path": path,
                "body": body,
                "authorization": request.headers.get("authorization"),
            }
        )
        if self._unauthorized_once and len(self.sends) == 1:
            return httpx.Response(401, json={"code": 11244, "message": "invalid token"})
        if self._errors:
            return self._errors.pop(0)
        if self._omit_message_id:
            # 200 但没有消息 id：平台说受理了，却拿不到可对账的凭据
            return httpx.Response(200, json={"timestamp": "2026-10-29T04:00:00+08:00"})
        return httpx.Response(
            200,
            json={"id": f"qqmsg-{len(self.sends)}", "timestamp": "2026-10-29T04:00:00+08:00"},
        )


class FakeClock:
    """单调时钟替身：让 token 过期与被动窗口过期能不睡觉地推进。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _install(
    env: TestEnv,
    *,
    cfg: QQBotConfig = CFG,
    fake: FakeQQBot | None = None,
    clock: FakeClock | None = None,
) -> tuple[QQBotConnector, FakeQQBot]:
    fake = fake or FakeQQBot()
    connector = QQBotConnector(
        cfg,
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        now=clock,
    )
    env.app.state.qqbot_connector = connector
    return connector, fake


def _bare_connector(
    cfg: QQBotConfig = CFG,
    *,
    fake: FakeQQBot | None = None,
    clock: FakeClock | None = None,
) -> tuple[QQBotConnector, FakeQQBot]:
    """不挂到 app 上的连接器（纯协议层用例）。"""
    fake = fake or FakeQQBot()
    return (
        QQBotConnector(
            cfg,
            http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            now=clock,
        ),
        fake,
    )


async def _bind(env: TestEnv, *, openid: str = OPENID, proof: str = "proven") -> None:
    """绑定 openid。**身份键是 openid，不是 QQ 号**。"""
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=openid,
            proof=proof,
        )


def _sign_headers(body: bytes, *, ts: int | None = None) -> dict[str, str]:
    """按官方方案签名：``ed25519(secret 派生私钥, timestamp + body)``。

    这里复用连接器导出的 ``ed25519_seed``（BotSecret → 32 字节 seed）。
    补齐规则本身没被独立验证——它本来就是"未核实"项；本用例验证的是
    验签链路（时间戳拼接、hex、重放窗口、失败即拒绝）确实生效。
    """
    stamp = int(time.time()) if ts is None else ts
    key = Ed25519PrivateKey.from_private_bytes(ed25519_seed(BOT_SECRET))
    signature = key.sign(str(stamp).encode() + body).hex()
    return {
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": str(stamp),
        "Content-Type": "application/json",
    }


def _c2c_event(
    content: str = "你好",
    *,
    msg_id: str = "qq-msg-100",
    openid: str = OPENID,
    attachments: list[dict] | None = None,
) -> bytes:
    payload = {
        "id": f"evt-{msg_id}",
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": msg_id,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
            "author": {"user_openid": openid},
            "attachments": attachments or [],
        },
    }
    return json.dumps(payload).encode()


def _group_event(
    content: str = "<@!bot-openid> 大家好",
    *,
    msg_id: str = "qq-msg-200",
    member_openid: str = OPENID,
    group_openid: str = GROUP_OPENID,
) -> bytes:
    payload = {
        "id": f"evt-{msg_id}",
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": msg_id,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
            "group_openid": group_openid,
            "author": {"member_openid": member_openid},
            "attachments": [],
        },
    }
    return json.dumps(payload).encode()


async def _post(env: TestEnv, body: bytes, *, headers: dict | None = None) -> httpx.Response:
    return await env.client.post(
        "/v1/qqbot/events",
        content=body,
        headers=headers if headers is not None else _sign_headers(body),
    )


# ---------------------------------------------------------------- 信封与身份


def test_openid_becomes_account_ref_not_qq_number() -> None:
    """入站信封的身份键必须是 openid，窗口按私聊/群区分。"""
    connector, _ = _bare_connector()
    payload = json.loads(_c2c_event(msg_id="qq-msg-900"))
    envelope = connector.normalize_inbound(payload)

    assert envelope.account_ref == OPENID
    assert envelope.window_ref == f"qqbot-loop:private:{OPENID}"
    # 去重键带 app_id：多机器人共库时 message id 可能撞车
    assert envelope.external_event_id == f"{APP_ID}:qq-msg-900"
    assert envelope.parts[0].text == "你好"
    assert envelope.mentions_actor is False
    assert envelope.connector_instance == CFG.instance_id


def test_group_envelope_uses_group_openid_window() -> None:
    """群窗口按 group_openid 共享，发言人 openid 只作 account_ref。"""
    connector, _ = _bare_connector()
    payload = json.loads(_group_event(member_openid="member-1", msg_id="qq-msg-901"))
    envelope = connector.normalize_inbound(payload)

    assert envelope.account_ref == "member-1"
    assert envelope.window_ref == f"qqbot-loop:group:{GROUP_OPENID}"
    # 群聊事件类型本身就意味着"被 @"，渠道必须如实上报，否则群里永远不接话
    assert envelope.mentions_actor is True
    # @ 标记不该进正文（"@bot 大家好" → "大家好"）
    assert "<@!" not in envelope.parts[0].text
    assert envelope.parts[0].text == "大家好"


async def test_two_members_share_one_group_window(logged_in: TestEnv) -> None:
    """同一个群必须是一个共享会话，否则角色看不到完整群聊语境。"""
    _connector, _fake = _install(logged_in)
    await _bind(logged_in)

    await _post(logged_in, _group_event(member_openid=OPENID, msg_id="g-1"))
    await _post(logged_in, _group_event(member_openid="member-2", msg_id="g-2"))

    async with session_scope() as session:
        windows = list(
            (
                await session.execute(select(Window).where(Window.channel == "qqbot"))
            ).scalars()
        )
    assert len(windows) == 1, f"群里两个人应该共用一个窗口，实际 {len(windows)} 个"
    assert windows[0].title == f"qqbot-loop:group:{GROUP_OPENID}"


# ---------------------------------------------------------------- 被动回复闭环


async def test_private_message_gets_a_passive_reply(logged_in: TestEnv) -> None:
    """核心验收：一条私聊消息最终真的发出了带 msg_id 的被动回复。"""
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    response = await _post(logged_in, _c2c_event("在吗", msg_id="qq-msg-1"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True, body
    assert body["decision"] == "reply", body
    assert body["delivered"] is True, body

    assert fake.sends, "没有任何发送请求 —— 闭环没打通"
    call = fake.sends[0]
    assert call["path"] == f"/v2/users/{OPENID}/messages"
    # 被动回复必须带来源消息的 msg_id 与递增的 msg_seq，否则平台按主动推送处理
    assert call["body"]["msg_id"] == "qq-msg-1"
    assert call["body"]["msg_seq"] == 1
    assert call["body"]["content"].strip()
    assert call["authorization"] == "QQBot tok-1"


async def test_group_reply_uses_group_endpoint(logged_in: TestEnv) -> None:
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    await _post(logged_in, _group_event(msg_id="qq-msg-2"))

    assert fake.sends, "群聊消息没有触发发送"
    assert fake.sends[0]["path"] == f"/v2/groups/{GROUP_OPENID}/messages"
    assert fake.sends[0]["body"]["msg_id"] == "qq-msg-2"


async def test_reply_receipt_is_persisted_with_platform_id(logged_in: TestEnv) -> None:
    """回执必须落库，且带平台真实消息 id（可审计、可对账）。"""
    _connector, fake = _install(logged_in)
    await _bind(logged_in)
    await _post(logged_in, _c2c_event("在吗", msg_id="qq-msg-3"))

    async with session_scope() as session:
        rows = list((await session.execute(select(DeliveryReceiptRow))).scalars())
        actor_msgs = list(
            (await session.execute(select(Message).where(Message.role == "actor"))).scalars()
        )
    assert rows, "没有任何投递回执"
    assert rows[0].state in {"sent", "delivered"}, rows[0].state
    assert rows[0].platform_message_id == "qqmsg-1"
    assert rows[0].channel == "qqbot"
    assert actor_msgs and actor_msgs[-1].text, "回复没落库"
    assert fake.sends


async def test_duplicate_event_processed_once(logged_in: TestEnv) -> None:
    """平台超时会重投同一事件，不能回两遍。"""
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    body = _c2c_event("重复的", msg_id="qq-msg-dup")
    await _post(logged_in, body)
    sent_after_first = len(fake.sends)
    assert sent_after_first >= 1

    second = await _post(logged_in, body)
    assert second.json().get("duplicate") is True, second.json()
    assert len(fake.sends) == sent_after_first, "重投导致重复发送"


# ---------------------------------------------------------------- 鉴权与刷新


async def test_token_refreshes_before_expiry() -> None:
    """token 必须**提前**刷新，而不是等 401。"""
    clock = FakeClock()
    fake = FakeQQBot(expires_in=7200)
    connector, _ = _bare_connector(fake=fake, clock=clock)

    assert await connector.access_token() == "tok-1"
    assert fake.token_requests == 1

    # 还没到提前刷新点：不应该重新换
    clock.value += 100
    assert await connector.access_token() == "tok-1"
    assert fake.token_requests == 1

    # 越过"过期时间 - 提前刷新余量"：必须重新换
    clock.value += 7200 - 300
    assert await connector.access_token() == "tok-2"
    assert fake.token_requests == 2


async def test_401_triggers_one_retry_with_fresh_token() -> None:
    """平台侧作废 token 时（提前刷新也躲不过）必须换新 token 重试一次。"""
    fake = FakeQQBot(unauthorized_once=True)
    connector, _ = _bare_connector(fake=fake)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-401")))

    receipt = await connector.send(
        OutboundMessage(
            operation_id="op_test",
            window_ref=f"qqbot-loop:private:{OPENID}",
            text="在的",
        )
    )

    assert fake.token_requests == 2, "401 之后没有重新换 token"
    assert len(fake.sends) == 2, "401 之后没有重试发送"
    assert receipt.platform_message_id == "qqmsg-2"
    assert fake.sends[-1]["authorization"] == "QQBot tok-2"


async def test_missing_credentials_fail_clearly() -> None:
    connector, fake = _bare_connector(cfg=QQBotConfig())
    with pytest.raises(ChannelError) as excinfo:
        await connector.access_token()
    assert excinfo.value.kind is ChannelErrorKind.unauthorized
    assert "app_id" in excinfo.value.message
    assert fake.token_requests == 0


# ---------------------------------------------------------------- 失败必须如实


def _passive_send(text: str = "在的") -> OutboundMessage:
    return OutboundMessage(
        operation_id="op_send",
        window_ref=f"qqbot-loop:private:{OPENID}",
        text=text,
    )


async def test_platform_rejection_raises_channel_error_not_receipt() -> None:
    """4xx = 平台明确拒绝：必须抛错，不能返回一个"已送达"的回执。"""
    fake = FakeQQBot(
        errors=[httpx.Response(400, json={"code": 40034, "message": "msg_id 已过期"})]
    )
    connector, _ = _bare_connector(fake=fake)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-400")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.rejected
    assert "40034" in excinfo.value.message


async def test_server_error_is_uncertain_not_failed() -> None:
    """5xx = 可能已经发出去：必须标 uncertain（对账），不能当失败重发。"""
    fake = FakeQQBot(errors=[httpx.Response(503, text="upstream")])
    connector, _ = _bare_connector(fake=fake)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-503")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.uncertain


async def test_ok_without_message_id_is_uncertain() -> None:
    """200 但拿不到消息 id → 无法对账 → uncertain，而不是 delivered。"""
    fake = FakeQQBot(omit_message_id=True)
    connector, _ = _bare_connector(fake=fake)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-noid")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.uncertain


async def test_connection_refused_is_unreachable() -> None:
    """连不上平台 = 确定没送到（可重试），与 uncertain 必须区分开。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        raise httpx.ConnectError("connection refused")

    connector = QQBotConnector(CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-down")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.unreachable
    assert excinfo.value.retriable is True


async def test_route_reports_undelivered_when_platform_rejects(logged_in: TestEnv) -> None:
    """平台拒绝时，受理结果必须是 delivered=False——不能"看着成功"。"""
    fake = FakeQQBot(
        errors=[httpx.Response(400, json={"code": 40034, "message": "被动回复次数已用尽"})]
    )
    _install(logged_in, fake=fake)
    await _bind(logged_in)

    body = (await _post(logged_in, _c2c_event("在吗", msg_id="qq-msg-x"))).json()
    assert body["delivered"] is False, body

    async with session_scope() as session:
        rows = list((await session.execute(select(DeliveryReceiptRow))).scalars())
    assert rows, "失败也必须留回执记录"
    assert all(row.state != "delivered" for row in rows), [r.state for r in rows]


async def test_query_receipt_is_uncertain_with_reason() -> None:
    """官方没有逐条投递查询接口：只能如实说"不知道"。"""
    connector, _ = _bare_connector()
    receipt = await connector.query_receipt(
        ReceiptQuery(operation_id="op_x", platform_message_id="qqmsg-1")
    )
    assert receipt.state.value == "uncertain"
    assert receipt.detail and "不提供" in receipt.detail


# ---------------------------------------------------------------- 分片


def test_long_text_is_split_without_loss() -> None:
    text = "今天下午去了一趟图书馆，借了两本书。" * 120
    limit = CFG.max_text_length
    parts = split_text(text, limit)

    assert len(parts) > 1, "超长文本没有分片"
    assert all(len(part) <= limit for part in parts)
    assert "".join(parts) == text, "分片丢字/多字"


async def test_render_output_uses_configured_limit() -> None:
    connector, _ = _bare_connector()
    text = "一" * (CFG.max_text_length * 2 + 7)
    parts = connector.render_output(text, modalities=[Modality.text])
    assert len(parts) == 3
    assert all(len(part) <= CFG.max_text_length for part in parts)
    assert "".join(parts) == text


async def test_overlong_single_send_is_refused_not_truncated() -> None:
    """单条超限必须报错：静默截断会变成"用户只看到半句话但回执说成功"。"""
    connector, fake = _bare_connector()
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-long")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send(text="一" * (CFG.max_text_length + 1)))
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert fake.sends == [], "超限文本仍然发了请求"


# ---------------------------------------------------------------- 主动推送受限


async def test_proactive_push_is_refused_by_default() -> None:
    """没有被动窗口 + 没有主动权限 → 必须拒绝并说明，而不是发出去。"""
    connector, fake = _bare_connector()
    caps = connector.capabilities()
    assert caps.supports_proactive is False, "默认不该声明支持主动推送"

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert "主动推送" in excinfo.value.message
    assert fake.sends == []


async def test_proactive_push_works_only_when_declared() -> None:
    cfg = QQBotConfig(
        app_id=APP_ID,
        client_secret=CLIENT_SECRET,
        instance_id="qqbot-proactive",
        sandbox=False,
        supports_proactive=True,
    )
    connector, fake = _bare_connector(cfg=cfg)
    assert connector.capabilities().supports_proactive is True

    receipt = await connector.send(
        OutboundMessage(
            operation_id="op_proactive",
            window_ref="qqbot-proactive:private:" + OPENID,
            text="主动说一句",
        )
    )
    assert receipt.platform_message_id == "qqmsg-1"
    # 主动推送不带 msg_id（没有可回复的来源消息）
    assert "msg_id" not in fake.sends[0]["body"]
    assert "msg_seq" not in fake.sends[0]["body"]


async def test_passive_reply_quota_is_enforced() -> None:
    """被动回复次数用尽后必须停下来（按主动推送处理），不能一直发到平台报错。"""
    cfg = QQBotConfig(
        app_id=APP_ID,
        client_secret=CLIENT_SECRET,
        instance_id="qqbot-quota",
        sandbox=False,
        # 单聊配额：官方是 60 分钟 / 4 次；这里为了测『用尽』把次数调小
        passive_max_replies_private=2,
    )
    connector, fake = _bare_connector(cfg=cfg)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-quota")))
    message = OutboundMessage(
        operation_id="op_q",
        window_ref="qqbot-quota:private:" + OPENID,
        text="一句",
    )

    await connector.send(message)
    await connector.send(message)
    assert [call["body"]["msg_seq"] for call in fake.sends] == [1, 2]

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(message)
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert len(fake.sends) == 2, "配额用尽后仍然发了请求"


async def test_expired_passive_window_is_not_reused() -> None:
    """窗口过期后不再带 msg_id：否则平台会以"回复超时"报错。"""
    clock = FakeClock()
    connector, fake = _bare_connector(clock=clock)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-old")))

    # 单聊窗口是 60 分钟（官方数值），越过它才算过期
    clock.value += CFG.passive_window_seconds_private + 1
    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert fake.sends == []


# ---------------------------------------------------------------- 能力声明


def test_capabilities_require_credentials() -> None:
    empty = QQBotConnector(QQBotConfig()).capabilities()
    assert empty.enabled is False
    assert empty.supports_proactive is False
    assert empty.supports_media is False

    ready = QQBotConnector(CFG).capabilities()
    assert ready.enabled is True
    assert ready.supports_proactive is False, "默认必须声明为不支持主动推送"
    assert ready.max_text_length == CFG.max_text_length
    assert ready.receipt_reliability == "best_effort"


# ---------------------------------------------------------------- 白名单


async def test_stranger_is_rejected_before_any_write(logged_in: TestEnv) -> None:
    """名单外的 openid：200 + accepted=false，且不发消息、不建窗口。"""
    import dataclasses

    logged_in.app.state.settings = dataclasses.replace(
        logged_in.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({OPENID}),
    )
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    body = (
        await _post(logged_in, _c2c_event("陌生人", msg_id="qq-msg-np", openid=OTHER_OPENID))
    ).json()
    assert body["accepted"] is False, body
    assert "白名单" in body["reason"], body
    assert body["sender"] == OTHER_OPENID
    assert fake.sends == [], "不该给名单外的人发任何东西"

    async with session_scope() as session:
        windows = list(
            (await session.execute(select(Window).where(Window.channel == "qqbot"))).scalars()
        )
    assert windows == [], "名单外的人不该建窗口"


async def test_whitelisted_openid_is_processed(logged_in: TestEnv) -> None:
    import dataclasses

    logged_in.app.state.settings = dataclasses.replace(
        logged_in.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({OPENID}),
    )
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    body = (await _post(logged_in, _c2c_event("我在名单里", msg_id="qq-msg-in"))).json()
    assert body["accepted"] is True, body
    assert fake.sends


# ---------------------------------------------------------------- 验签


async def test_bad_signature_is_rejected_before_any_write(logged_in: TestEnv) -> None:
    _connector, fake = _install(logged_in)
    await _bind(logged_in)

    body = _c2c_event("伪造", msg_id="qq-msg-forged")
    response = await _post(
        logged_in,
        body,
        headers={
            "X-Signature-Ed25519": "00" * 64,
            "X-Signature-Timestamp": str(int(time.time())),
        },
    )
    assert response.status_code == 403
    assert fake.sends == []
    assert fake.token_requests == 0, "验签失败不该已经去换 token"


async def test_stale_timestamp_is_rejected() -> None:
    """重放窗口之外的时间戳一律拒绝。"""
    connector, _ = _bare_connector()
    body = _c2c_event(msg_id="qq-msg-replay")
    headers = _sign_headers(body, ts=int(time.time()) - 3600)
    assert await connector.verify_inbound(headers, body) is False


async def test_missing_bot_secret_rejects_everything() -> None:
    """没有密钥就没有来源可信度：一律 False，不做宽松通过。"""
    connector, _ = _bare_connector(
        cfg=QQBotConfig(app_id=APP_ID, client_secret=CLIENT_SECRET, instance_id="qqbot-nokey")
    )
    body = _c2c_event(msg_id="qq-msg-nokey")
    assert await connector.verify_inbound(_sign_headers(body), body) is False


async def test_url_validation_returns_verifiable_signature(logged_in: TestEnv) -> None:
    """op=13 回调地址校验：返回能被平台公钥验过的签名。"""
    _connector, _fake = _install(logged_in)
    body = json.dumps(
        {"op": 13, "d": {"plain_token": "plain-token-xyz", "event_ts": "1700000000"}}
    ).encode()

    response = await _post(logged_in, body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plain_token"] == "plain-token-xyz"

    public_key = Ed25519PrivateKey.from_private_bytes(ed25519_seed(BOT_SECRET)).public_key()
    # 签名对象是 event_ts + plain_token（不是请求体），签错了平台会拒绝配置
    public_key.verify(bytes.fromhex(data["signature"]), b"1700000000plain-token-xyz")


async def test_unsigned_validation_is_rejected_by_default(logged_in: TestEnv) -> None:
    """默认不允许无签名的回调校验请求——不把验签降级成宽松通过。"""
    _install(logged_in)
    body = json.dumps(
        {"op": 13, "d": {"plain_token": "pt", "event_ts": "1700000000"}}
    ).encode()
    response = await _post(logged_in, body, headers={})
    assert response.status_code == 403


async def test_unsigned_validation_can_be_opted_in(logged_in: TestEnv) -> None:
    """平台不签名时可以显式放行——配置开关打开后才生效。"""
    import dataclasses

    cfg = dataclasses.replace(CFG, allow_unsigned_validation=True)
    _install(logged_in, cfg=cfg)
    body = json.dumps(
        {"op": 13, "d": {"plain_token": "pt", "event_ts": "1700000000"}}
    ).encode()
    response = await _post(logged_in, body, headers={})
    assert response.status_code == 200, response.text
    assert response.json()["plain_token"] == "pt"


# ---------------------------------------------------------------- 其它


def test_window_parsing_rejects_garbage() -> None:
    """窗口解析不出目标时必须报错，不能拿整串当 openid 发出去。"""
    assert target_from_window("qqbot-loop:group:" + GROUP_OPENID) == (True, GROUP_OPENID)
    assert target_from_window("qqbot-loop:private:" + OPENID) == (False, OPENID)
    with pytest.raises(ChannelError):
        target_from_window("qqbot-loop:whatever:123")


async def test_health_reports_auth_only() -> None:
    connector, fake = _bare_connector()
    ok, detail = await connector.health()
    assert ok is True
    assert "未验证" in detail, detail
    assert fake.token_requests == 1

    broken, _ = _bare_connector(cfg=QQBotConfig())
    ok, detail = await broken.health()
    assert ok is False
    assert "app_id" in detail


async def test_health_endpoint_uses_installed_connector(logged_in: TestEnv) -> None:
    _install(logged_in)
    response = await logged_in.client.get("/v1/qqbot/health")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert data["instance"] == CFG.instance_id
    assert data["config"]["app_id"] == APP_ID
    # 明文密钥绝不出现在健康检查里
    assert CLIENT_SECRET not in json.dumps(data)
    assert BOT_SECRET not in json.dumps(data)


async def test_channel_event_type_is_explicitly_unsupported() -> None:
    """频道（guild）事件明确拒绝并给出原因，不猜一套窗口语义。"""
    connector, _ = _bare_connector()
    payload = {
        "op": 0,
        "t": "AT_MESSAGE_CREATE",
        "d": {"id": "g-1", "content": "hi", "channel_id": "ch-1", "guild_id": "gd-1",
              "author": {"id": "u-1"}},
    }
    with pytest.raises(ChannelError) as excinfo:
        connector.normalize_inbound(payload)
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert "频道" in excinfo.value.message


async def test_heartbeat_op_is_ignored_quietly(logged_in: TestEnv) -> None:
    """op=10 心跳不是消息：返回 200 但 accepted=False，避免平台重投。"""
    _install(logged_in)
    body = json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}).encode()
    response = await _post(logged_in, body)
    assert response.status_code == 200
    assert response.json()["accepted"] is False


async def test_media_refs_are_refused_not_silently_dropped() -> None:
    """带媒体的投递必须整条拒绝：只发文字却记成功 = 图被静默丢弃。"""
    connector, fake = _bare_connector()
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-media")))

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(
            OutboundMessage(
                operation_id="op_media",
                window_ref=f"qqbot-loop:private:{OPENID}",
                text="给你看张图",
                media_refs=["https://example.invalid/a.png"],
            )
        )
    assert excinfo.value.kind is ChannelErrorKind.unsupported
    assert "富媒体" in excinfo.value.message
    assert fake.sends == []


async def test_opted_in_unsigned_validation_is_logged_loudly(
    logged_in: TestEnv, caplog
) -> None:  # noqa: ANN001
    """逃生开关被真正用到时必须**打日志**。

    这是唯一一条不验签就能进对话链路的路径。开关打开时如果静默放行，
    运维永远不知道自己正在裸奔——日志是唯一的发现机会。
    """
    import dataclasses
    import logging

    cfg = dataclasses.replace(CFG, allow_unsigned_validation=True)
    _install(logged_in, cfg=cfg)
    body = json.dumps(
        {"op": 13, "d": {"plain_token": "pt", "event_ts": "1700000000"}}
    ).encode()
    with caplog.at_level(logging.WARNING, logger="ling.api.qqbot"):
        response = await _post(logged_in, body, headers={})
    assert response.status_code == 200
    assert any(
        "未验签放行" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


async def test_error_body_with_http_200_is_rejected_not_success() -> None:
    """官方 v2 用 `err_code` 表达业务失败，**HTTP 状态可能仍是 200**。

    不认这个字段的话，一次失败的发送会被记成"成功但没 id"，退化成
    uncertain——分类不准，排查时看不到真正原因（消息审核不通过、
    权限不足等都会被糊成同一句"结果未知"）。
    """
    import httpx

    # 用 FakeQQBot 的 errors 队列：第一次发送就返回这条 200 + err_code
    fake = FakeQQBot(
        errors=[
            httpx.Response(
                200,
                json={"err_code": 304003, "message": "消息审核不通过", "trace_id": "t-1"},
            )
        ]
    )
    connector, _fake = _bare_connector(fake=fake)
    connector.normalize_inbound(json.loads(_c2c_event(msg_id="qq-msg-audit")))
    with pytest.raises(ChannelError) as excinfo:
        await connector.send(_passive_send())
    assert excinfo.value.kind is ChannelErrorKind.rejected
    assert "304003" in excinfo.value.message
