"""渠道连接器端口（Connector SDK 的 Python 侧契约）。

所有渠道实现同一组 port（设计文档 §7）：
``verify_inbound / normalize_inbound / fetch_media / render_output / send /
query_receipt / capabilities / health``。

渠道只翻译协议，不拥有第二套人格、记忆或领域数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ling.contracts.v1 import (
    CapabilityDescriptor,
    DeliveryReceipt,
    InteractionEnvelope,
    Modality,
)


class ChannelErrorKind(StrEnum):
    unsupported = "unsupported_capability"
    rate_limited = "rate_limited"
    unauthorized = "unauthorized"
    unreachable = "provider_unavailable"
    rejected = "provider_error"
    # 必须与 rejected 不同的字符串值。两者语义完全不同：
    # rejected = 平台明确拒绝（可以确定没送达）
    # uncertain = 结果未知（可能已送达，必须对账，不能重发）
    # 原先把两个成员设成同一个值 "provider_error"，于是
    # `uncertain == rejected` 为 True，且从值反序列化只会得到 rejected——
    # "结果未知"被降级成"明确失败"，重试就会造成重复消息。
    uncertain = "delivery_uncertain"


class ChannelError(Exception):
    """渠道失败的确定性分类。``uncertain`` 表示结果未知，必须对账而非重发。"""

    def __init__(self, kind: ChannelErrorKind, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable


@dataclass(frozen=True)
class OutboundMessage:
    """要发给平台的一条消息。

    ``operation_id`` 是**本地** operation 关联 ID（``DeliveryService`` 新建的
    ``operations`` 行主键），用来把回执、日志和机会上的
    ``delivery_operation_id`` 对回同一条本地记录。

    它**不是平台侧幂等键**：onebot / qqbot / qq 的 ``connector.send`` 都不把
    它放进出站 payload（qq 只拿它拼本地 ``receipt_id``），平台看不到这个值，
    也就不能据它去重或对账——平台侧唯一可核对的凭据是回执里的
    ``platform_message_id``。
    """

    operation_id: str
    window_ref: str
    text: str
    reply_to: str | None = None
    media_refs: list[str] = field(default_factory=list)
    is_group: bool = False
    # 要 @ 的平台账号（QQ 号）。群里多人说话时用它消歧——
    # "回你"没人知道是对谁说的，@ 一下才说得清。
    mentions: list[str] = field(default_factory=list)
    # 要拍一拍谁（QQ 号）。**不打断的提醒/亲近**，比"在吗"轻。
    poke_targets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReceiptQuery:
    operation_id: str
    platform_message_id: str | None = None


@runtime_checkable
class ChannelConnector(Protocol):
    connector_id: str
    instance_id: str

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """签名与重放窗口校验。任何失败都必须返回 False，不做"宽松通过"。"""
        ...

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:
        """把平台 payload 翻译成统一信封；不得在这里解析身份。"""
        ...

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """取回媒体，返回 (bytes, mime)。受限存储，不直接进领域对象。"""
        ...

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        """按平台能力切分/降级（超长截断、不支持格式降级）。"""
        ...

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送。成功必须带真实平台回执；不确定必须抛 uncertain。"""
        ...

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """对账：查询平台侧真实投递状态。"""
        ...

    def capabilities(self) -> CapabilityDescriptor:
        ...

    async def health(self) -> tuple[bool, str]:
        ...


__all__ = [
    "ChannelConnector",
    "ChannelError",
    "ChannelErrorKind",
    "OutboundMessage",
    "ReceiptQuery",
]
