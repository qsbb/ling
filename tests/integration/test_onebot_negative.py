"""QQ 路径的负例验收：隐私、投递失败语义、重投幂等。

运维交付清单第 2 项要求"失败/重投/隐私负例验收"。
正向路径（能聊）已有覆盖，这里补**出错时会怎样**——
个人服务器上，出错时的行为比顺利时的行为更决定信不信得过。

要点：
- 私聊内容不得出现在群聊的上下文里
- 发送失败要记为失败（不是"已发送"）
- 不确定的失败（超时/async）不能重投成两条
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import Message, Window
from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-neg",
)
QQ_USER = "20002"
GROUP = "88888"


class RecordingOneBot:
    def __init__(self, *, retcode: int = 0, status: str = "ok") -> None:
        self.sent: list[dict] = []
        self._retcode = retcode
        self._status = status

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        action = str(request.url).rsplit("/", 1)[-1]
        self.sent.append({"action": action, "body": body})
        if self._retcode != 0 or self._status != "ok":
            return httpx.Response(
                200, json={"status": self._status, "retcode": self._retcode, "data": None}
            )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )


async def _setup(env: TestEnv, fake: RecordingOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    env.app.state.onebot_connector = connector
    async with session_scope() as session:
        view = await IdentityService(session).create_user(
            username=f"qqneg_{env.subject_id[-6:]}", password="pw-not-used"
        )
        await IdentityService(session).bind_account(
            subject_id=view.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof="verified",
        )
    return connector


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    mac = hmac.new(CFG.secret.encode(), body, hashlib.sha1).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Self-ID": CFG.self_id,
        "X-Signature": f"sha1={mac}",
    }


def _private_event(text: str, *, msg_id: int = 1) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": int(CFG.self_id),
        "user_id": int(QQ_USER),
        "message_id": msg_id,
        "time": 0,
        "raw_message": text,
        "message": text,
        "sender": {"user_id": int(QQ_USER), "nickname": "阿澄"},
    }


def _group_event(text: str, *, msg_id: int = 2, user_id: int = 30003) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": int(CFG.self_id),
        "user_id": user_id,
        "group_id": int(GROUP),
        "message_id": msg_id,
        "time": 0,
        "raw_message": text,
        "message": text,
        "sender": {"user_id": user_id, "nickname": "路⼈"},
    }


async def _post(env: TestEnv, payload: dict) -> httpx.Response:
    body, headers = _signed(payload)
    return await env.client.post("/v1/onebot/events", content=body, headers=headers)


# ------------------------------------------------------------------ 隐私负例


async def test_private_content_not_in_group_context(logged_in: TestEnv) -> None:
    """私聊里说的内容不得出现在群聊的上下文里。

    这是最直接的隐私事故形态：在同一个人面前把私聊的话在群里说出来。
    """
    fake = RecordingOneBot()
    await _setup(logged_in, fake)

    # 私聊里说一个只有对方知道的事
    secret = "我在考虑换工作的事"
    await _post(logged_in, _private_event(secret, msg_id=11))

    # 等后台把这一轮跑完
    for _ in range(40):
        async with session_scope() as session:
            rows = list(
                (await session.execute(select(Message).where(Message.role == "actor"))).scalars()
            )
        if rows:
            break
        await _sleep()

    # 之后在群里 @ 机器人
    await _post(logged_in, _group_event("@ling 大家好", msg_id=12))
    await _sleep()

    # 检查群窗口的输入消息里有没有带入私聊内容
    async with session_scope() as session:
        group_windows = list(
            (
                await session.execute(
                    # 群窗口的 title 就是 window_ref（形如 "<instance>:group:<gid>"）
                    select(Window).where(Window.title.like("%:group:%"))
                )
            ).scalars()
        )
        group_msgs = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.window_id.in_([w.id for w in group_windows] or [""])
                    )
                )
            ).scalars()
        )
    for m in group_msgs:
        assert secret not in (m.text or ""), (
            f"私聊内容出现在群聊窗口的消息里（role={m.role}）"
        )


# ------------------------------------------------------------------ 投递失败语义


async def test_send_failure_marks_message_failed(logged_in: TestEnv) -> None:
    """发送失败必须记为失败，不能记成"已发送"。

    记错比失败更糟：用户看不到回复，而系统认为已经送达，不会重试。
    """
    fake = RecordingOneBot(retcode=100)  # 平台返回错误
    connector = await _setup(logged_in, fake)

    # 契约：失败**抛出** ChannelError，只有成功才返回回执。
    # 这样调用方不会"忘了检查 ok"就把失败当成功。
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(
                operation_id="op-neg-1",
                window_ref=f"{CFG.instance_id}:private:{QQ_USER}",
                text="这条发不出去",
            )
        )
    assert exc.value.kind is not ChannelErrorKind.uncertain
    assert exc.value.kind.value != "delivered"


async def test_async_retcode_is_uncertain(logged_in: TestEnv) -> None:
    """retcode=1 / status=async 是"不确定"，不能当成功也不能当失败。"""
    fake = RecordingOneBot(retcode=1, status="async")
    connector = await _setup(logged_in, fake)
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(
                operation_id="op-neg-2",
                window_ref=f"{CFG.instance_id}:private:{QQ_USER}",
                text="不确定",
            )
        )
    # 必须与"平台明确拒收"区分开：不确定不能盲目重投（可能发出两条）
    assert exc.value.kind is ChannelErrorKind.uncertain, (
        f"异步回执被归类为 {exc.value.kind}，重投逻辑会出错"
    )
    assert exc.value.retriable is False, "不确定的结果不该被标记为可重试"


async def test_unreachable_is_retriable(logged_in: TestEnv) -> None:
    """连不上应是可重试，不是永久失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("不可达", request=request)

    connector = OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    # 不可达是**抛出**而不是返回失败回执：连接器层的故障不该被当成
    # "平台拒收"，否则重试策略会走错分支。
    with pytest.raises(ChannelError) as exc:
        await connector.send(
            OutboundMessage(
                operation_id="op-neg-3",
                window_ref=f"{CFG.instance_id}:private:{QQ_USER}",
                text="连不上",
            )
        )
    assert exc.value.kind is ChannelErrorKind.unreachable
    assert exc.value.retriable is True


# ------------------------------------------------------------------ 重投幂等


async def test_redelivery_after_success_is_blocked(logged_in: TestEnv) -> None:
    """成功后重复投递同一事件不应再产生一条回复。"""
    fake = RecordingOneBot()
    await _setup(logged_in, fake)
    payload = _private_event("你好", msg_id=21)

    await _post(logged_in, payload)
    await _sleep()
    first = len(fake.sent)
    await _post(logged_in, payload)
    await _sleep()
    assert len(fake.sent) == first, "重复投递又发了一次"


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.35)
