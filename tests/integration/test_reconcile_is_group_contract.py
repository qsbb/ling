"""对账时把**已知的**群/私聊语义传给连接器，不让它去推断。

## 这一步原来缺什么

`DeliveryService.reconcile` 手上已经有权威数据——发送时把
`OutboundMessage.is_group` 写进了 `operations.payload["is_group"]`
（`:217` 正常投递、`:498` 超时续发两条路径都写）。但对账构造 `ReceiptQuery`
时**没把它传下去**，于是连接器只能从 `window_ref` 字符串里找 `":group:"` 反推。

后果不是"多查一次"：查历史要选对接口（群聊 `get_group_msg_history`、
私聊 `get_friend_msg_history`），**选错就查不到**，那条消息永远停在
`uncertain`。用户看到的是"发出去了，但系统一直不敢确认"。

## 为什么用"形态相反"的值测

任务卡要求的构造是：`payload.is_group=True`，但 `window_ref` 是**私聊形态**。

只有形态相反，才能证明连接器收到的是**传入值**而不是"推断碰巧也对"。
如果两者形态一致，那么传与不传、推断与不推断，结果都一样——测试通过
但什么也没证明。（这类"假通过"今天已经付过代价。）

## 断言在契约层

断言的是**连接器收到的 `ReceiptQuery.is_group`**，不是"最后调了哪个
action"。前者是被测代码与连接器之间的契约，后者是 OneBot 实现内部的细节。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.capabilities.delivery import DeliveryService
from ling.contracts.v1 import DeliveryState, OperationState
from ling.infra.db import session_scope
from ling.infra.models import Operation
from ling.infra.security import new_id
from ling.ports.channel import (
    CapabilityDescriptor,
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    DeliveryReceipt,
    OutboundMessage,
    ReceiptQuery,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_owner"
CHANNEL = "onebot"
INSTANCE = "onebot-is-group"

# **私聊形态**的窗口标识。注意它**不含** `:group:`——这正是让"推断"给出
# 与"传入值"相反结论的关键。
PRIVATE_SHAPED_REF = "onebot:private:100200300"


class _SpyConnector(ChannelConnector):
    """记下 `query_receipt` 收到的每一次查询，回答"已送达"。"""

    connector_id = CHANNEL
    instance_id = INSTANCE

    def __init__(self) -> None:
        self.queried: list[ReceiptQuery] = []

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=CHANNEL,
            version="11",
            supports_private=True,
            supports_group=True,
            max_text_length=4000,
            receipt_reliability="best_effort",
            enabled=True,
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:  # pragma: no cover
        raise AssertionError("对账不该发送")

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        self.queried.append(query)
        return DeliveryReceipt(
            receipt_id="rcpt_spy",
            operation_id=query.operation_id,
            channel=CHANNEL,
            state=DeliveryState.delivered,
            platform_message_id="pm_spy",
            observed_at=datetime.now(UTC),
            detail="对账结果",
        )

    async def verify_inbound(self, headers, body) -> bool:  # noqa: ANN001
        return True

    def normalize_inbound(self, payload: dict):  # noqa: ANN001
        raise NotImplementedError

    async def fetch_media(self, media_ref: str):  # pragma: no cover
        return b"", ""

    def render_output(self, text: str, *, modalities) -> list[str]:  # noqa: ANN001
        return [text]

    async def health(self) -> tuple[bool, str]:
        return True, "ok"


class _AlwaysUncertain(ChannelConnector):
    """发送永远"不知道成没成"——用来造出一条 `uncertain` 的投递。"""

    connector_id = CHANNEL
    instance_id = INSTANCE

    def capabilities(self) -> CapabilityDescriptor:
        return _SpyConnector().capabilities()

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        raise ChannelError(ChannelErrorKind.uncertain, "发送超时，未知是否到达")

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:  # pragma: no cover
        raise AssertionError("这一步不该查询")

    async def verify_inbound(self, headers, body) -> bool:  # noqa: ANN001
        return True

    def normalize_inbound(self, payload: dict):  # noqa: ANN001
        raise NotImplementedError

    async def fetch_media(self, media_ref: str):  # pragma: no cover
        return b"", ""

    def render_output(self, text: str, *, modalities) -> list[str]:  # noqa: ANN001
        return [text]

    async def health(self) -> tuple[bool, str]:
        return True, "ok"


async def _uncertain_then_reconcile(
    env: TestEnv, *, is_group: bool | None, window_ref: str
) -> ReceiptQuery:
    """造一条 uncertain 投递，再对账一次，返回连接器收到的查询。

    `is_group` 直接覆盖 payload 里的值，用来构造"payload 说 A、
    `window_ref` 看起来是 B"的相反形态。
    """
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=_AlwaysUncertain(),
            message=OutboundMessage(
                operation_id=new_id("op"),
                window_ref=window_ref,
                text="她要说的话",
                # 发送时的业务语义。对它来说群/私聊是**已知**的，
                # 不该在事后靠字符串反推。
                is_group=bool(is_group) if is_group is not None else False,
            ),
            actor_id=ACTOR,
            subject_id=env.subject_id,
        )
    assert outcome.state is OperationState.uncertain, outcome.detail

    operation_id = outcome.operation_id
    # 把 payload 里的 is_group 改成我们要的三态值（含"这个字段上线前
    # 发出的旧数据"那种缺 key 的情形）。
    async with session_scope() as session:
        row = await session.get(Operation, operation_id)
        payload = dict(row.payload or {})
        if is_group is None:
            payload.pop("is_group", None)
        else:
            payload["is_group"] = is_group
        row.payload = payload
        await session.commit()

    spy = _SpyConnector()
    async with session_scope() as session:
        await DeliveryService(session).reconcile(spy, operation_id)

    assert spy.queried, "对账根本没问连接器"
    return spy.queried[-1]


async def test_reconcile_passes_the_stored_is_group_not_an_inference(
    logged_in: TestEnv,
) -> None:
    """核心：payload 说"群聊"，但 `window_ref` 是私聊形态 → 传下去的是 **True**。

    `window_ref` 里的 `private` 会让**推断**得出 `False`。两者相反，
    所以连接器收到 `True` 只能是"用了传入值"。旧实现（不传）会得到 `None`，
    由连接器自己推断成 `False` —— 这条就会红。
    """
    query = await _uncertain_then_reconcile(
        logged_in, is_group=True, window_ref=PRIVATE_SHAPED_REF
    )

    assert query.is_group is True, (
        f"连接器收到的 is_group={query.is_group!r}，"
        f"说明调用方没把 payload 里的已知语义传下去（window_ref={query.window_ref!r}）"
    )


async def test_reconcile_passes_false_for_a_private_delivery(logged_in: TestEnv) -> None:
    """反向配平：payload 说"私聊" → 传下去的是 **False**（不是 None）。

    与上一条配对，证明传的是"实际值"而不是"恒为 True"。
    """
    query = await _uncertain_then_reconcile(
        logged_in, is_group=False, window_ref=PRIVATE_SHAPED_REF
    )

    assert query.is_group is False


async def test_reconcile_keeps_none_for_operations_without_the_field(
    logged_in: TestEnv,
) -> None:
    """旧 operation（payload 里没有 `is_group`）→ 传 **None**，不是 False。

    这是三态字段的意义所在：`None` = "调用方不知道，请连接器退回自己的推断"。
    如果写成 `bool(payload.get("is_group"))`，旧的私聊/群聊投递会被**一律**
    当成"确认私聊"，反而把误判固定死——那比不传更糟，因为它让
    "不知道"和"确定是私聊"变得无法区分。
    """
    query = await _uncertain_then_reconcile(
        logged_in, is_group=None, window_ref=PRIVATE_SHAPED_REF
    )

    assert query.is_group is None, (
        f"缺字段时传了 {query.is_group!r}，应当保留 None 让连接器自己推断"
    )
