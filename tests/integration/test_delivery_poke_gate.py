"""投递闸：渠道没有拍一拍能力时，**整条拒绝**而不是"发正文、拍一拍静默丢掉"。

## 这个文件要证明什么

`OutboundMessage.poke_targets` 在 OneBot 连接器里真的会发一个 `send_poke`
action（`connectors/onebot/connector.py`）。QQ 官方 / 微信没有这个能力。所以
"她写了 `[poke:…]`、投递层照发"的后果是：正文送达、回执 `delivered`、
**用户没被拍到**，而本地没有任何一行说这件事没发生。

## 为什么必须有一条"正向"用例

只断言"没有 poke 能力时不发"是不够的：一个**永远拒绝所有消息**的实现也能让
那条绿。所以这里必须有配对的一条——有能力的渠道**照常送达**，而且同一个
`OutboundMessage` 的正文确实进了 `sent`。两条必须同一次运行里同时成立，
且打同一个注入点（同一条 `deliver()` 的能力校验段）。

## 为什么 @ 与引用**没有**同类闸

`CapabilityDescriptor` 目前**没有** `supports_mention` / `supports_quote`
（只有 `supports_poke` / `supports_recall`）。所以那两项无法在此校验——不是
"不需要"，是"字段还不存在"。补它需要先做独立的契约变更（加字段 + 四个连接器
声明 + 重导 schema）。`src/ling/capabilities/delivery.py` 的注释写了同一件事，
这里放一条测试把那个缺口**显式钉住**：一旦有人加了字段，这条会红，提醒他
把闸一起补上。

代价来源：设计文档 `CAPABILITY-TAG-SYSTEM.md` 第 3 节缺陷表最后一行
（「投递闸也未校验 @/拍一拍是否为本渠道能力」）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.capabilities.delivery import DeliveryService
from ling.contracts.v1 import CapabilityDescriptor, DeliveryState, OperationState
from ling.infra.db import session_scope
from ling.ports.channel import (
    ChannelConnector,
    DeliveryReceipt,
    OutboundMessage,
    ReceiptQuery,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class _PokeConnector(ChannelConnector):
    """只区分 `supports_poke` 的假连接器。其余能力全开，便于隔离变量。"""

    connector_id = "pokefake"
    instance_id = "pokefake:test"

    def __init__(self, *, supports_poke: bool) -> None:
        self._caps = CapabilityDescriptor(
            capability_id="pokefake",
            version="1",
            supports_private=True,
            supports_group=True,
            supports_proactive=True,
            supports_poke=supports_poke,
            max_text_length=4096,
            enabled=True,
        )
        self.sent: list[OutboundMessage] = []

    def capabilities(self) -> CapabilityDescriptor:
        return self._caps

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=DeliveryState.delivered,
            platform_message_id="pm-1",
            observed_at=datetime.now(UTC),
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id="rcpt_q",
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=DeliveryState.uncertain,
            observed_at=datetime.now(UTC),
        )


def _message(*, poke: list[str] | None = None) -> OutboundMessage:
    return OutboundMessage(
        operation_id="op_test",
        window_ref="onebot:private:100200300",
        text="在呢",
        poke_targets=list(poke or []),
    )


@pytest.mark.asyncio
async def test_poke_is_rejected_when_channel_lacks_the_capability(logged_in: TestEnv) -> None:
    """渠道**不支持**拍一拍 → 整条拒绝，连接器一个字节都没收到。

    反面：一个"只丢 poke、正文照发"的实现会让 `sent` 非空而这条红。
    """
    connector = _PokeConnector(supports_poke=False)
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=_message(poke=["100200300"]),
            actor_id="actor_1",
            subject_id=None,
        )

    assert outcome.state is OperationState.failed, (
        f"不支持拍一拍的渠道居然投递成功：state={outcome.state}"
    )
    assert outcome.receipt is not None
    assert outcome.receipt.state == DeliveryState.unsupported.value, (
        f"回执状态应是 unsupported（明确「没做」），实际 {outcome.receipt.state}"
    )
    assert outcome.delivered is False, "没有回执却声称送达了"
    assert connector.sent == [], f"正文被发出去了，而拍一拍会被静默丢弃：{connector.sent}"
    assert "拍一拍" in outcome.detail, f"拒绝理由没说清是哪一项：{outcome.detail!r}"


@pytest.mark.asyncio
async def test_poke_passes_when_channel_declares_it(logged_in: TestEnv) -> None:
    """**配对的正向用例**：渠道声明了 `supports_poke` → 照常送达。

    这条与上一条打同一个注入点（同一条 `deliver()`）。没有它，上一若被一个
    "永远拒绝"的实现满足就会变绿而无人发现。
    """
    connector = _PokeConnector(supports_poke=True)
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=_message(poke=["100200300"]),
            actor_id="actor_1",
            subject_id=None,
        )

    assert outcome.state is OperationState.succeeded, (
        f"支持拍一拍的渠道被拦下了：state={outcome.state} detail={outcome.detail}"
    )
    assert len(connector.sent) == 1, f"应当发出一条：{connector.sent}"
    assert connector.sent[0].poke_targets == ["100200300"], (
        "拍一拍目标在投递过程中丢了——提示词说能拍、这里却没传下去"
    )


@pytest.mark.asyncio
async def test_message_without_poke_is_unaffected(logged_in: TestEnv) -> None:
    """没有 `poke_targets` 的普通消息**不受新闸影响**。

    这条防的是"闸写成 `if not caps.supports_poke: reject`"——那样所有纯文字
    消息在 QQ/微信上都会失败，而上面两条用例都抓不到它（它们都带 poke）。
    """
    connector = _PokeConnector(supports_poke=False)
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=_message(),
            actor_id="actor_1",
            subject_id=None,
        )

    assert outcome.state is OperationState.succeeded, (
        f"不含拍一拍的普通消息被误拦：{outcome.detail}"
    )
    assert len(connector.sent) == 1


def test_mention_and_quote_have_no_capability_field_yet() -> None:
    """**缺口显式钉住**：@ 与引用的能力位尚不存在，所以它们**无法**被闸校验。

    这不是"不需要校验"，是"契约里还没有这个数据"。这条用例的作用是：

    - 现在：把缺口写成可执行的断言，而不是注释里的一句话（AGENTS.md：
      「注释里声称的每一条语义不同，都应该能在代码或测试里找到区分方式」）；
    - 将来：有人加了 `supports_mention` / `supports_quote` 时，**这条会红**——
      那正是提醒他把 `delivery.py` 的闸一起补上，而不是只加字段。

    改这条测试的正确方式不是删掉它，而是：加字段 → 加闸 → 把本用例改成
    "字段存在，且闸会拦"。
    """
    fields = set(CapabilityDescriptor.model_fields)
    assert "supports_poke" in fields, "supports_poke 不见了——拍一拍闸失去了依据"
    missing = {"supports_mention", "supports_quote"} - fields
    assert missing == {"supports_mention", "supports_quote"}, (
        "能力位 `supports_mention` / `supports_quote` 已经出现——"
        "请到 `capabilities/delivery.py` 把对应的投递闸补上，"
        "并把本测试改成「闸会拦」的用例（删除本测试会留下一个无人知晓的缺口）"
    )
