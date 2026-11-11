"""渠道连接器端口（Connector SDK 的 Python 侧契约）。

所有渠道实现同一组 port（设计文档 §7）：
``verify_inbound / normalize_inbound / fetch_media / render_output / send /
query_receipt / capabilities / health``。

渠道只翻译协议，不拥有第二套人格、记忆或领域数据库。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ling.contracts.v1 import (
    AttachmentKind,
    CapabilityDescriptor,
    DeliveryReceipt,
    InteractionEnvelope,
    Modality,
    OutboundAttachment,
    RecallEvent,
    RecallReceipt,
)


def normalize_delivery_text(text: str) -> str:
    """投递内容比对用的归一化：折叠所有空白。

    只用于"平台侧那条消息是不是我们发的这一段"的判断（内容比对），
    不参与任何发送。折叠空白是因为正文经过平台往返后换行/空格可能变。
    """
    return re.sub(r"\s+", "", text or "")


def delivery_text_digest(text: str) -> str:
    """投递内容摘要（**不落正文**，只落这个摘要）。

    对账时要用"平台历史里有没有同文消息"作证据，但把回复正文抄进
    `operations.payload` 等于给内容开第二份副本。所以只存摘要：
    摘要能比对、不能还原。
    """
    normalized = normalize_delivery_text(text).encode("utf-8")
    return "sha256:" + hashlib.sha256(normalized).hexdigest()[:32]


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
    # 明确「收到但不处理」的事件（心跳、已读回执之类）。
    #
    # 与 ``unsupported`` 的区别只在**日志**上，但那个区别很重要：``unsupported``
    # 说明"这类事件我们还没实现"，值得记一条让人看见；``ignored`` 说明
    # "这类事件按设计就该静默丢掉"，记下来只会把日志缓冲刷满——OneBot 的
    # 心跳每 30 秒一条，两天就能把 500 条缓冲里的有效日志全挤出去。
    ignored = "ignored_event"


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
    # **出站**媒体引用，故意不套 `contracts.v1.MediaRef`：入站那个类型只允许
    # http/https 或不透明 id（防 SSRF），而出站要交给平台自己解析——OneBot 的
    # `file` 还接受本地路径与 base64，收紧只会把能发的东西变少。方向不同，
    # 信任也不同：这里的值由我们自己产生，不是外部输入。
    #
    # **legacy**：不带类型，无法区分图片/语音/视频。保留只为兼容既有调用方
    # 与已落库数据；新代码请用 `attachments`。判定能力时这里一律当作**图片**
    # （见 `CapabilityDescriptor.media_supported`）。
    #
    # 刻意**不做**"从引用内容猜类型"的隐式转换：一个 `asset:abc` 猜错了，
    # 结果就是"语音被当图片发出去"——用户只会看到一张图，不会有任何报错。
    media_refs: list[str] = field(default_factory=list)
    # **类型化**的出站附件。新代码用这个。
    #
    # 与 `media_refs` 并存而不是替换：替换会让所有既有构造点（三个渠道、
    # 投递层、大量测试）一次性全改，而这个契约是后面 12 个任务的依赖面。
    # 并存 + 唯一的判定规则（`iter_outbound_attachments()`）代价更小，
    # 语义也不含糊。
    attachments: list[OutboundAttachment] = field(default_factory=list)
    is_group: bool = False
    # 要 @ 的平台账号（QQ 号）。群里多人说话时用它消歧——
    # "回你"没人知道是对谁说的，@ 一下才说得清。
    mentions: list[str] = field(default_factory=list)
    # 要拍一拍谁（QQ 号）。**不打断的提醒/亲近**，比"在吗"轻。
    poke_targets: list[str] = field(default_factory=list)


def iter_outbound_attachments(message: OutboundMessage) -> list[OutboundAttachment]:
    """把一条消息的**全部**媒体统一成类型化附件列表。

    `media_refs`（legacy）在这里被当作 **image**，与它过去的行为等价——
    这正是兼容的关键：旧渠道声明 `supports_media=True` 且实际只发图片，
    转换后仍然是"图片 + 能发图片 → 放行"，行为一分不变。

    **顺序**：先 `media_refs` 后 `attachments`。调用方若要挑"第一条语音发
    哪个"，应按 `kind` 过滤而不是依赖位置。

    永远返回新 list，不改动入参（`OutboundMessage` 是 frozen 的）。
    """
    legacy = [
        OutboundAttachment(kind=AttachmentKind.image, ref=ref)
        for ref in message.media_refs
    ]
    return [*legacy, *message.attachments]


@dataclass(frozen=True)
class ReceiptQuery:
    """一次对账查询。

    ``platform_message_id`` 是**最直接**的证据，但很多平台并不保证给
    （OneBot 的 ``send_*_msg`` 在 ``status=ok`` + 空 data 时就没有 id）。
    没有 id 时，连接器只能用"会话 + 正文 + 大致时间"去平台历史里做内容比对
    （见 docs/OUTBOUND-DELIVERY-RESEARCH.md §5 P0-2），所以这里额外带上三项。
    三项都是**可选**的：拿不到就退回"不确定"，绝不当成"没发出去"。
    """

    operation_id: str
    platform_message_id: str | None = None
    # 目标会话（形如 "<instance>:private:<uid>" / "<instance>:group:<gid>"）。
    window_ref: str | None = None
    # 这一段发出去的正文摘要（`delivery_text_digest`），用于内容比对。
    # **不是正文**：库里存的是摘要，见 `operations.payload["text_digest"]`。
    text_digest: str | None = None
    # 发出时间（对账时算时间窗与宽限期用）。
    sent_at: datetime | None = None
    # 这条消息当时发往群聊还是私聊。**三态**：
    #
    # - ``None``（默认）= 调用方不知道。连接器应退回自己的推断（OneBot 是
    #   按 ``window_ref`` 反推），而不是当成私聊。
    # - ``True`` / ``False`` = 调用方明确知道，连接器直接采用。
    #
    # ## 为什么需要这个字段
    #
    # 对账时平台常常没给 ``platform_message_id``（OneBot 的 ``send_*_msg``
    # 在 ``status=ok`` + 空 data 时就没有），连接器只能去平台历史里做内容
    # 比对。**而查历史要选对接口**：群聊是 ``get_group_msg_history``、私聊是
    # ``get_friend_msg_history``，选错就查不到，于是那条消息永远停在
    # ``uncertain``。
    #
    # 在那之前，连接器只能从 ``window_ref`` 字符串反推（``":group:" in ref``）。
    # 字符串是拼接出来的约定而不是受约束的类型，这会误判——而"什么算群聊"
    # 本来就是业务语义，不该由传输层推断。
    #
    # ## 为什么不是 ``bool = False``
    #
    # ``False`` 会被读成"调用方确认这是私聊"，与"调用方不知道"是两件事。
    # 用 ``False`` 当默认值等于**替调用方做了一个它没做的判断**，而且这个
    # 判断还是错的（真实是群聊时会去查私聊历史）。同一个道理见
    # ``CapabilityDescriptor.supported_media`` 的三态设计。
    #
    # ## 为什么 `OutboundMessage` 和 `RecallRequest` 都已经有它
    #
    # 那两处早就有 ``is_group``，唯独这里缺——**能力具备而字段缺失就是遗漏**：
    # 发起对账的调用方本来就知道这条 operation 发往哪里
    # （``operations.payload["window_ref"]`` 与它同在一条记录上）。
    is_group: bool | None = None


@dataclass(frozen=True)
class RecallRequest:
    """一次**主动撤回**的请求（撤回我们自己发出去的那条）。

    ``platform_message_id`` 是**唯一**判据：只有当初发送时拿到的真实回执 id
    才能指向"平台上那一条"。**不接受**用正文、时间或本地 message_id 去猜——
    猜错就是撤掉别人的消息，那是不可撤销的破坏。

    ``window_ref`` 与 ``is_group`` 只给实现方拼平台参数用（部分实现要求
    群号），**不参与"能不能撤"的判定**。
    """

    platform_message_id: str
    window_ref: str = ""
    is_group: bool = False


@runtime_checkable
class ChannelConnector(Protocol):
    connector_id: str
    instance_id: str

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """签名与重放窗口校验。任何失败都必须返回 False，不做"宽松通过"。"""
        ...

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope | RecallEvent:
        """把平台 payload 翻译成统一信封；不得在这里解析身份。

        **除了信封，还可能返回 :class:`RecallEvent`**（对方撤回了某条消息）。
        撤回不是一条消息：没有正文、不该触发回复、不该产生 ``messages`` 行，
        所以它有自己的类型，由 ``inbound_intake`` 用 ``isinstance`` 分流。
        把撤回硬塞进信封只有两种写法（造一条空发言 / 给信封加"这不是消息"的
        开关），两种都是在伪造输入。
        """
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

    async def recall(self, request: RecallRequest) -> RecallReceipt:
        """**主动撤回**我们自己发出去的一条消息（可选能力）。

        什么时候会被调用：用户在界面上对**她自己的**一条消息点"撤回"。
        实现方必须：

        - 只用 ``platform_message_id`` 定位，**不许**按内容/时间猜；
        - 拿不到确认就返回 ``uncertain``（**不许**返回 ``recalled``）；
        - 平台明确不支持时返回 ``unsupported``（**不许**假装成功）；
        - 时间窗/权限由上层先判（见 `domains/conversation/recall.py`），
          实现方仍然可以在平台报错时如实映射。

        ``supports_recall=False`` 的渠道不实现这个方法也行——上层会先看
        ``capabilities().supports_recall``，不会调用它。
        """
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
    "RecallRequest",
    "ReceiptQuery",
    "delivery_text_digest",
    "iter_outbound_attachments",
    "normalize_delivery_text",
]
