"""A7 端到端：超长回复在**真实连接器**上必须段段合规、段段送达。

单元测试（`tests/unit/test_chunk_part_limits.py`）证明 `plan_parts` 会按渠道上限
切分。这里证明另外一半：**桥真的把渠道上限传下去了**，且真实连接器一条都
没拒。

覆盖两种真实取值：
- qqbot：1500（官方机器人，最容易触发；旧实现在这里丢尾段）
- onebot：4000（NapCat / go-cqhttp 等）

为什么必须走真实连接器：`plan_parts` 默认有保守兜底（1500），所以"没传上限"
在 qqbot 上**看起来是对的**、在 onebot 上却会把 3000 字的回复拆成两条本可以
一条发完的消息。只有拿真实 `capabilities().max_text_length` 比对，才能证明
"传的是渠道自己的值"，而不是"碰巧兜底值够用"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from connectors.qqbot.connector import QQBotConnector
from connectors.qqbot.settings import PROD_API_BASE, QQBotConfig

from ling.capabilities.chunking import MAX_PARTS_PRIVATE
from ling.infra.db import session_scope
from ling.kernel.bridge import InboundBridge
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

QQBOT_CFG = QQBotConfig(
    app_id="102000001",
    client_secret="fake-client-secret",
    instance_id="qqbot-a7",
    sandbox=False,
    api_base=PROD_API_BASE,
    token_url="https://bots.qq.com/app/getAppAccessToken",
    bot_secret="fake-bot-secret",
)
ONE_BOT_CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-a7",
)

QQBOT_PRIVATE_OPENID = "openid-a7"


def _long_reply(chars: int) -> str:
    """带句子边界的长回复（`split_semantic` 只在句子边界收段）。"""
    sentence = "今天下午去了一趟图书馆，借了两本书，回来路上还买了点菜。"
    return (sentence * (chars // len(sentence) + 1))[:chars]


def _squash(value: str) -> str:
    import re

    return re.sub(r"\s+", "", value)


class _RecordingHandler:
    """假的平台 HTTP 端点：记录出站消息正文，一律回成功 + 消息 id。

    两个渠道的**成功判据不同**，必须分别回：
    - qqbot：200 + ``{"id": ...}``（拿不到 id 只能算 uncertain）
    - onebot：``{"status": "ok", "retcode": 0, "data": {"message_id": ...}}``
      （retcode 是权威字段，缺了会被判成"调用失败"）

    混用格式会把"假成功"变成"真失败"，那测的就不是分段了。
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self._msg_seq = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "tok-a7", "expires_in": 7200})
        body = json.loads(request.content or b"{}")
        # qqbot 的纯文本消息体是 {"content": "..."}；onebot 是 message 数组
        if isinstance(body.get("content"), str):
            self.texts.append(body["content"])
            self._msg_seq += 1
            return httpx.Response(
                200, json={"id": f"pm-a7-{self._msg_seq}", "timestamp": "2026-11-03T00:00:00Z"}
            )
        segments = body.get("message") or []
        self.texts.append(
            "".join(
                str(seg.get("data", {}).get("text", ""))
                for seg in segments
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
        )
        self._msg_seq += 1
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 9000 + self._msg_seq}},
        )


def _qqbot_connector(handler: _RecordingHandler) -> QQBotConnector:
    connector = QQBotConnector(
        QQBOT_CFG,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler.handler)),
    )
    # 打开被动回复窗口：qqbot 没有窗口就只能"主动推送"（默认被拒）。
    # 这里直接喂一条入站事件——与真实链路走同一个方法。
    connector.normalize_inbound(
        {
            "id": "evt-a7",
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "qq-msg-a7",
                "content": "在吗",
                "timestamp": datetime.now(UTC).isoformat(),
                "author": {"user_openid": QQBOT_PRIVATE_OPENID},
                "attachments": [],
            },
        }
    )
    return connector


def _onebot_connector(handler: _RecordingHandler) -> OneBotV11Connector:
    return OneBotV11Connector(
        ONE_BOT_CFG,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler.handler)),
    )


async def _deliver_via_bridge(env: TestEnv, connector: object, *, text: str, window_ref: str):
    """借真实 `InboundBridge._deliver` 走完整分段 → 投递链路。"""
    bridge = InboundBridge(
        None,  # type: ignore[arg-type]
        coordinator=object(),
        connector=connector,
        secret_box=object(),
    )
    envelope = type("Env", (), {"window_ref": window_ref, "connector_instance": "x"})()
    import asyncio

    from ling.capabilities.chunking import PART_DELAY_SECONDS

    # 段间停顿是给人看的节奏，测试里不需要等
    original_delay = PART_DELAY_SECONDS
    try:
        import ling.capabilities.chunking as chunking

        chunking.PART_DELAY_SECONDS = 0.0
        async with session_scope() as session:
            bridge._session = session  # noqa: SLF001 - 单测里替换协作者
            return await asyncio.wait_for(
                bridge._deliver(  # noqa: SLF001
                    envelope=envelope, text=text, subject_id="subject_a7"
                ),
                timeout=60,
            )
    finally:
        import ling.capabilities.chunking as chunking

        chunking.PART_DELAY_SECONDS = original_delay


async def test_qqbot_long_reply_every_part_fits_and_all_are_sent(web_env: TestEnv) -> None:
    """qqbot（1500）：**每一段都 <= 1500，且没有任何一段被丢弃。**

    旧实现在这里最惨：3340 字产出 `[105, 105, 110, 110, 2910]`，
    前四段发出去、2910 字的尾段被 `DeliveryService` 整条拒收，
    `_deliver` 一遇拒绝就 `break`——「部分送达 + 尾段永久丢失」。
    """
    handler = _RecordingHandler()
    connector = _qqbot_connector(handler)
    text = _long_reply(3340)

    delivered, sent, total = await _deliver_via_bridge(
        web_env,
        connector,
        text=text,
        window_ref=f"qqbot-a7:private:{QQBOT_PRIVATE_OPENID}",
    )

    assert delivered is True, f"没有全部送达：{sent}/{total}"
    assert sent == total >= 1
    # 送出的每一条都必须在渠道上限之内
    assert all(len(t) <= 1500 for t in handler.texts), (
        f"有段超过 qqbot 上限：{[len(t) for t in handler.texts]}"
    )
    # 而且**一条都没少**：拼回去等于原稿
    assert _squash("".join(handler.texts)) == _squash(text), "分段丢字了"
    assert len(handler.texts) == total


async def test_onebot_long_reply_every_part_fits_and_all_are_sent(web_env: TestEnv) -> None:
    """onebot（4000）：同样段段合规、段段送达。

    上限更大不等于不会踩：旧实现在 7400 字时产出
    `[105, 105, 110, 110, 6910]`，尾段 6910 > 4000 → 整条拒收。
    """
    handler = _RecordingHandler()
    connector = _onebot_connector(handler)
    text = _long_reply(7400)

    delivered, sent, total = await _deliver_via_bridge(
        web_env,
        connector,
        text=text,
        window_ref="onebot-a7:private:20002",
    )

    assert delivered is True, f"没有全部送达：{sent}/{total}"
    assert sent == total >= 2
    assert all(len(t) <= 4000 for t in handler.texts), (
        f"有段超过 onebot 上限：{[len(t) for t in handler.texts]}"
    )
    assert _squash("".join(handler.texts)) == _squash(text), "分段丢字了"


async def test_bridge_passes_channel_limit_not_just_fallback(web_env: TestEnv) -> None:
    """桥必须把**渠道自己的**上限传给规划器，而不是靠 1500 兜底。

    判据：5000 字在 onebot（4000）上物理上要 2 段；若桥没传上限、
    用了 1500 兜底，就会切成 4 段。段数是最直接的证据——
    它既证明上限被传下去了，也证明"没多造气泡"。
    """
    handler = _RecordingHandler()
    connector = _onebot_connector(handler)
    text = _long_reply(5000)
    assert len(text) > 4000  # 必须真的超过渠道上限，否则测不到这条路径

    delivered, sent, total = await _deliver_via_bridge(
        web_env,
        connector,
        text=text,
        window_ref="onebot-a7:private:20002",
    )

    assert delivered is True
    assert total == 2, f"没按渠道上限（4000）切，而是切成 {total} 段（兜底 1500 会是 4 段）"
    assert all(len(t) <= 4000 for t in handler.texts)
    assert _squash("".join(handler.texts)) == _squash(text)


async def test_normal_length_reply_keeps_human_rhythm(web_env: TestEnv) -> None:
    """修复不能让**正常长度**的回复失去"连发几条"的人味。

    这是防刷屏约束的另一面：段数上限是 `MAX_PARTS_PRIVATE`，
    且短回复不该因为"按渠道上限切"而被并成一条。
    """
    handler = _RecordingHandler()
    connector = _onebot_connector(handler)
    # 6 行、明显短于 4000：按模型换行分条（但不超过防刷屏上限）
    text = "\n".join(f"第{index}行，随便聊两句今天的事。" for index in range(6))

    delivered, sent, total = await _deliver_via_bridge(
        web_env,
        connector,
        text=text,
        window_ref="onebot-a7:private:20002",
    )

    assert delivered is True
    assert 2 <= total <= MAX_PARTS_PRIVATE, f"正常长度被并成 {total} 段（或拆过头）"
    assert _squash("".join(handler.texts)) == _squash(text)
