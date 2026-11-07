"""装饰段（引用 / @）被平台拒收时，**正文不能跟着一起丢**。

## 为什么这条测试存在

她可以自己选表达方式（`capabilities/expression_choice`），而给她的提示是
``[quote:消息序号]`` / ``[at:QQ号]``——上下文里**从来没有**真实的消息 id 或
QQ 号（`kernel/context.py` 只给名字）。于是她写出来的引用 id 常常对不上任何
平台消息，NapCat 的 ``send_*_msg`` 会整条拒收。

实测（2026-11-08，复现脚本 /tmp/diag_outbound.py 场景⑥）：
``[quote:3]`` → ``{"type":"reply","data":{"id":"3"}}`` → ``retcode=100`` →
**整条消息没发出去**，而正文完全正常。用户看到的是"她决定回了，但一个字
都没到"，本地只看到一条"这段没送达"。

参考语义（`ports/channel.py`）：
- ``rejected``：平台明确拒绝，**这条没发出去** → 去掉装饰重发是安全的；
- ``uncertain``：可能已经发出去了 → 绝不重发（会变成重复消息）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig

from ling.contracts.v1 import DeliveryState
from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage


def _connector(handler) -> OneBotV11Connector:  # noqa: ANN001
    config = OneBotConfig(
        api_base="http://onebot.test:5700",
        access_token="",
        self_id="10001",
        instance_id="onebot-send",
    )
    return OneBotV11Connector(
        config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _message(**overrides) -> OutboundMessage:  # noqa: ANN003
    payload = {
        "operation_id": "op_test",
        "window_ref": "onebot-send:private:100200300",
        "text": "那件事我记着呢，别急。",
        "is_group": False,
        "reply_to": "3",
    }
    payload.update(overrides)
    return OutboundMessage(**payload)


@pytest.mark.asyncio
async def test_rejected_decoration_is_dropped_and_body_still_sent() -> None:
    """引用 id 不存在 → 平台拒收 → 去掉引用重发，正文必须到。"""
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        sent.append(body)
        has_reply = any(seg.get("type") == "reply" for seg in body.get("message") or [])
        if has_reply:
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "retcode": 100,
                    "data": None,
                    "wording": "消息发送失败：引用的消息不存在",
                },
            )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )

    receipt = await _connector(handler).send(_message())

    assert len(sent) == 2, "应当在去掉装饰后重发一次"
    assert any(seg.get("type") == "reply" for seg in sent[0]["message"])
    assert not any(seg.get("type") == "reply" for seg in sent[1]["message"]), (
        "重发时不能还带着被拒的引用段"
    )
    texts = [
        seg["data"].get("text", "")
        for seg in sent[1]["message"]
        if seg.get("type") == "text"
    ]
    assert "那件事我记着呢" in "".join(texts), "正文必须原样发出去"

    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "9001"
    # 降级必须留痕：事后能看出"这条为什么少了引用"
    assert "去掉引用" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_rejected_without_decoration_is_not_retried() -> None:
    """没有装饰段可去：失败就是失败，不许无限重试。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"status": "failed", "retcode": 100, "data": None}
        )

    with pytest.raises(ChannelError) as excinfo:
        await _connector(handler).send(_message(reply_to=None))
    assert excinfo.value.kind is ChannelErrorKind.rejected
    assert calls == 1, "没有装饰段时不该重发"


@pytest.mark.asyncio
async def test_uncertain_result_is_never_resent() -> None:
    """「结果未知」可能已经发出去了——重发就是重复消息，宁可报不确定。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "async", "retcode": 1, "data": None})

    with pytest.raises(ChannelError) as excinfo:
        await _connector(handler).send(_message())
    assert excinfo.value.kind is ChannelErrorKind.uncertain
    assert calls == 1, "不确定结果绝不允许重发"


@pytest.mark.asyncio
async def test_normal_send_keeps_decoration() -> None:
    """平台接受时，引用要照原样发出去（不能因为有了兜底就先去掉装饰）。"""
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9002}}
        )

    receipt = await _connector(handler).send(_message())

    assert len(sent) == 1
    assert {"type": "reply", "data": {"id": "3"}} in sent[0]["message"]
    assert receipt.state is DeliveryState.delivered
    assert receipt.detail is None
