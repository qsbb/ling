"""出站语义：retcode 分类、回执"未确认"、引用白名单、对账证据。

依据 `docs/OUTBOUND-DELIVERY-RESEARCH.md`：

- §4.5 NapCat 的实际 retcode（400/1400 校验失败、1200/200 动作异常、
  1404 不支持的 API，**没有 100**）；
- §4.4 无效 reply 是"静默丢掉该段、正文照发"，不是整条拒收；
- §4.2 `get_msg` 只能**单向确认**，"查不到 ≠ 没发出去"。

这些结论直接决定投递状态机的边界，所以逐条钉在这里（不连数据库）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig

from ling.contracts.v1 import DeliveryState
from ling.ports.channel import (
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    ReceiptQuery,
    delivery_text_digest,
)

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="",
    self_id="10001",
    instance_id="onebot-semantics",
)


def _connector(handler) -> OneBotV11Connector:  # noqa: ANN001
    return OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _message(**overrides) -> OutboundMessage:  # noqa: ANN003
    payload = {
        "operation_id": "op_semantics",
        "window_ref": "onebot-semantics:private:100200300",
        "text": "这条要发出去",
        "is_group": False,
    }
    payload.update(overrides)
    return OutboundMessage(**payload)


def _failed(retcode: int, *, wording: str | None = None) -> httpx.Response:
    body: dict = {"status": "failed", "retcode": retcode, "data": None}
    if wording:
        body["wording"] = wording
    return httpx.Response(200, json=body)


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})


def _inbound(connector: OneBotV11Connector, message_id: int, text: str = "在吗") -> None:
    """喂一条入站事件：顺手建立"见过的 message_id"白名单。"""
    connector.normalize_inbound(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 10001,
            "message_id": message_id,
            "user_id": 100200300,
            "message": text,
            "raw_message": text,
            "sender": {"nickname": "Co"},
        }
    )


# ---------------------------------------------------------------- retcode 语义


@pytest.mark.parametrize(
    ("retcode", "expect"),
    [
        # 发送前就失败（schema / json 校验）：明确没发出 → rejected
        (400, ChannelErrorKind.rejected),
        (1400, ChannelErrorKind.rejected),
        # 动作内部抛异常：可能是"消息体无法解析"（没发出），也可能是
        # **发送超时**（已经发出去了）。两者同一个码，只能保守按"可能已发出"。
        (1200, ChannelErrorKind.uncertain),
        (200, ChannelErrorKind.uncertain),
        # 「不支持的 API」：重发没有意义，也不该被当成"平台拒收"去降级重发
        (1404, ChannelErrorKind.unsupported),
        # 规范里的 async 码
        (1, ChannelErrorKind.uncertain),
        # 兜底：别的实现（如 Lagrange）可能用 100 表示参数错误，
        # **NapCat 全仓没有 100**（§4.5），所以它不是"引用失效"的证据。
        (100, ChannelErrorKind.rejected),
    ],
)
@pytest.mark.asyncio
async def test_retcode_classification(retcode: int, expect: ChannelErrorKind) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return _failed(retcode)

    with pytest.raises(ChannelError) as excinfo:
        await _connector(handler).send(_message())
    assert excinfo.value.kind is expect
    # **uncertain 绝不重发**：这条是红线（重发 = 重复消息）
    expected_calls = 1
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_wording_is_kept_in_detail() -> None:
    """线上判断"1200 到底是超时还是消息体无法解析"全靠那行字。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return _failed(1200, wording="发送超时")

    with pytest.raises(ChannelError) as excinfo:
        await _connector(handler).send(_message())
    assert "发送超时" in excinfo.value.message


# ---------------------------------------------------------------- 回执未确认


@pytest.mark.asyncio
async def test_ok_without_message_id_is_uncertain_not_sent() -> None:
    """`retcode=0` + 空 data ≠ 已送达：只能算"提交了、未确认"。

    原实现把这种情况记成 ``sent``，而上层把 ``sent`` 算成"已送达"——
    于是"平台没给 id"被当成"发完了"，对账永远对不上。
    """
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return _ok(None)

    receipt = await _connector(handler).send(_message())
    assert receipt.state is DeliveryState.uncertain
    assert receipt.platform_message_id is None
    assert "message_id" in (receipt.detail or "")
    assert calls == 1, "没有回执 id 时也不许重发"


@pytest.mark.asyncio
async def test_ok_with_message_id_is_delivered() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return _ok({"message_id": 4321})

    receipt = await _connector(handler).send(_message())
    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "4321"
    assert receipt.detail is None


# ---------------------------------------------------------------- 引用白名单


@pytest.mark.asyncio
async def test_quote_of_known_message_keeps_reply_segment() -> None:
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b""))
        return _ok({"message_id": 11})

    connector = _connector(handler)
    _inbound(connector, 777)
    receipt = await connector.send(_message(reply_to="777"))

    assert {"type": "reply", "data": {"id": "777"}} in sent[0]["message"]
    assert receipt.state is DeliveryState.delivered


@pytest.mark.asyncio
async def test_quote_of_unknown_id_is_dropped_locally() -> None:
    """编出来的 id 在本地就剥掉：NapCat 只会静默丢该段（§4.4），别指望它报错。"""
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b""))
        return _ok({"message_id": 12})

    connector = _connector(handler)
    _inbound(connector, 777)
    receipt = await connector.send(_message(reply_to="3"))

    assert len(sent) == 1, "降级不该多打一次平台"
    assert not any(seg["type"] == "reply" for seg in sent[0]["message"])
    texts = "".join(
        seg["data"].get("text", "")
        for seg in sent[0]["message"]
        if seg["type"] == "text"
    )
    assert "这条要发出去" in texts, "正文不能跟着引用一起丢"
    assert receipt.state is DeliveryState.delivered
    assert "引用目标不在可见范围" in (receipt.detail or "")


def test_platform_message_id_for_event_maps_back() -> None:
    """上下文要的是**真实平台 id**，而只有连接器知道事件 id 的拼法。"""
    connector = _connector(lambda request: _ok({"message_id": 1}))  # noqa: ARG005
    _inbound(connector, 888)
    assert connector.platform_message_id_for_event("10001:888") == "888"
    assert connector.platform_message_id_for_event("不认识的 id") is None


# ---------------------------------------------------------------- 对账证据


@pytest.mark.asyncio
async def test_query_receipt_found_means_delivered() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/get_msg")
        return _ok({"message_id": 555, "sender": {"user_id": 10001}})

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(operation_id="op1", platform_message_id="555")
    )
    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "555"


@pytest.mark.asyncio
async def test_query_receipt_missing_but_recent_stays_uncertain() -> None:
    """查不到 ≠ 没发出去：消息还很新时只能保持不确定（§4.2）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        if action == "get_msg":
            return _failed(200, wording="消息不存在")
        return _ok({"messages": []})

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op1",
            platform_message_id="555",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("这一段"),
            sent_at=datetime.now(UTC),
        )
    )
    assert receipt.state is DeliveryState.uncertain


@pytest.mark.asyncio
async def test_query_receipt_missing_and_old_means_failed() -> None:
    """两个独立否证 + 过了宽限期，才敢说"没到平台"（仍然不重发）。

    第二个否证要成立，必须先看到"这份历史确实包含我自己发的消息"——
    否则平台行为差异会被当成"没送达"（见下一条）。
    """
    other_self = {
        "message_id": 999,
        "time": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        "sender": {"user_id": 10001},
        "message": [{"type": "text", "data": {"text": "另一句我已经发出去的话"}}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        if action == "get_msg":
            return _failed(200, wording="消息不存在")
        return _ok({"messages": [other_self]})

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op1",
            platform_message_id="555",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("这一段"),
            sent_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    assert receipt.state is DeliveryState.failed
    assert "两个独立否证" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_query_receipt_history_without_self_messages_is_not_evidence() -> None:
    """历史里一条自己的消息都没有 → "没找到同文"**不构成否证**。

    调研 §7.6 没有实机证实"`get_*_msg_history` 是否包含机器人自己发的消息"。
    如果某个实现不含自身消息，我们就会把"其实发成功了"判成 failed——
    接着重投会重发（重复消息）。所以这条路只能停在 uncertain。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        if action == "get_msg":
            return _failed(200, wording="消息不存在")
        return _ok({"messages": []})

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op1",
            platform_message_id="555",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("这一段"),
            sent_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    assert receipt.state is DeliveryState.uncertain
    assert "平台历史可能不含自身消息" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_query_receipt_without_id_uses_message_sent_echo() -> None:
    """回显里的 message_id 与发送回执同 id 空间，可以给"没拿到 id"的那段补 id。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url).rsplit("/", 1)[-1])
        return _ok(None)  # 发送成功但没给 id

    connector = _connector(handler)
    message = _message(text="我到家了")
    await connector.send(message)

    # 平台随后推来"自己发出的消息"回显（NapCat 的 reportSelfMessage）
    with pytest.raises(ChannelError):
        connector.normalize_inbound(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "self_id": 10001,
                "message_id": 9090,
                "target_id": 100200300,
                "user_id": 100200300,
                "message": "我到家了",
                "raw_message": "我到家了",
                "sender": {"nickname": "ling"},
            }
        )

    receipt = await connector.query_receipt(
        ReceiptQuery(operation_id=message.operation_id)
    )
    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "9090"
    assert "回显" in (receipt.detail or "")
    assert calls == ["send_private_msg"], "有回显时不该再去打 get_msg"


@pytest.mark.asyncio
async def test_query_receipt_without_id_uses_history_content_match() -> None:
    """没有回显时，用 `get_friend_msg_history` 做内容比对（唯一命中才算）。"""
    sent_at = datetime.now(UTC) - timedelta(seconds=20)

    async def handler(request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        if action == "get_friend_msg_history":
            return _ok(
                {
                    "messages": [
                        {
                            "message_id": 6100,
                            "time": int(sent_at.timestamp()),
                            "sender": {"user_id": 10001},
                            "message": [{"type": "text", "data": {"text": "我到家了"}}],
                        },
                        {
                            "message_id": 6101,
                            "time": int(sent_at.timestamp()),
                            "sender": {"user_id": 20002},
                            "message": [{"type": "text", "data": {"text": "好"}}],
                        },
                    ]
                }
            )
        return _failed(200, wording="不存在")

    connector = _connector(handler)
    with pytest.raises(ChannelError):
        connector.normalize_inbound(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "self_id": 10001,
                "message_id": 6100,
                "target_id": 100200300,
                "message": "我到家了",
                "raw_message": "我到家了",
                "sender": {"nickname": "ling"},
            }
        )
    receipt = await connector.query_receipt(
        ReceiptQuery(
            operation_id="op_history",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("我到家了"),
            sent_at=sent_at,
        )
    )
    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "6100"
    assert "历史内容比对" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_query_receipt_ambiguous_history_stays_uncertain() -> None:
    """同一句话连发两遍 → 无法唯一命中 → 保持不确定（绝不猜）。"""
    sent_at = datetime.now(UTC) - timedelta(seconds=20)
    hit = {
        "time": int(sent_at.timestamp()),
        "sender": {"user_id": 10001},
        "message": [{"type": "text", "data": {"text": "好"}}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"messages": [{**hit, "message_id": 1}, {**hit, "message_id": 2}]})

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_ambig",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("好"),
            sent_at=sent_at,
        )
    )
    assert receipt.state is DeliveryState.uncertain
    assert "无法唯一命中" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_query_receipt_channel_error_stays_uncertain() -> None:
    """对账本身失败（外呼挂了）绝不能变成 failed。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectError("不可达", request=request)

    receipt = await _connector(handler).query_receipt(
        ReceiptQuery(operation_id="op1", platform_message_id="555")
    )
    assert receipt.state is DeliveryState.uncertain


# ------------------------------------------------------ 对账：群聊判定的事实来源
#
# `ReceiptQuery.is_group` 是三态的（`None` = 调用方不知道）。连接器选历史接口时
# 必须**先看调用方给的事实，没有才反推**。这三条钉住三态的三个分支。


@pytest.mark.asyncio
async def test_receipt_uses_caller_supplied_is_group_true() -> None:
    """调用方给 `is_group=True` → 用**群聊**历史接口。

    关键在窗口标识是**私聊形态**（`:private:`）：如果连接器偷偷自己反推，
    就会走私聊分支。断言它走了群聊分支，才证明"调用方的事实优先"。
    """
    actions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        actions.append(str(request.url).rsplit("/", 1)[-1])
        return _ok({"messages": []})

    await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_caller_group",
            window_ref="onebot-semantics:private:100200300",
            text_digest=delivery_text_digest("随便"),
            sent_at=datetime.now(UTC) - timedelta(seconds=20),
            is_group=True,
        )
    )
    assert actions == ["get_group_msg_history"], (
        f"调用方说群聊，却走了 {actions}——连接器没有采用调用方给的事实"
    )


@pytest.mark.asyncio
async def test_receipt_uses_caller_supplied_is_group_false() -> None:
    """调用方给 `is_group=False` → 用**私聊**历史接口。

    ## 这条防的是 `or` 的坑

    如果实现写成 ``is_group = query.is_group or _infer(...)``，那么明确的
    ``False`` 会被当成"没给"，于是去反推——而窗口标识是群聊形态，反推结果是
    ``True``，走了群聊接口。这条会红。

    必须用 ``is not None`` 判断。
    """
    actions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        actions.append(str(request.url).rsplit("/", 1)[-1])
        return _ok({"messages": []})

    await _connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_caller_private",
            window_ref="onebot-semantics:group:123456",
            text_digest=delivery_text_digest("随便"),
            sent_at=datetime.now(UTC) - timedelta(seconds=20),
            is_group=False,
        )
    )
    assert actions == ["get_friend_msg_history"], (
        f"调用方说私聊，却走了 {actions}——明确的 False 被当成'没给'了"
    )


@pytest.mark.asyncio
async def test_receipt_falls_back_to_inference_when_is_group_absent() -> None:
    """调用方没给（`None`）→ 退回反推，且**不误判**含 ``group`` 字样的私聊。

    这是渐进收窄的关键：在调用方补上 `is_group` 之前，行为必须与改动前一致。
    """
    actions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        actions.append(str(request.url).rsplit("/", 1)[-1])
        return _ok({"messages": []})

    connector = _connector(handler)
    sent_at = datetime.now(UTC) - timedelta(seconds=20)

    # 真群聊：反推为群聊
    await connector.query_receipt(
        ReceiptQuery(
            operation_id="op_infer_group",
            window_ref="onebot-semantics:group:123456",
            text_digest=delivery_text_digest("随便"),
            sent_at=sent_at,
        )
    )
    assert actions[-1] == "get_group_msg_history"

    # **带 group 字样的私聊**：不能误判成群聊
    await connector.query_receipt(
        ReceiptQuery(
            operation_id="op_infer_private",
            window_ref="onebot-semantics:private:group:123",
            text_digest=delivery_text_digest("随便"),
            sent_at=sent_at,
        )
    )
    assert actions[-1] == "get_friend_msg_history", (
        "私聊标识被误判成群聊——子串匹配的老问题又回来了"
    )
