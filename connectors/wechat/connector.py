"""微信连接器：按**已确定且获准**的账号类型分别实现（W2：已实现通用端口，未测试）。

设计文档 §7 明确：不能把个人微信、公众号、企微当成同一个协议，也不能承诺
同样的主动能力。因此本模块：

1. 只实现 ling 侧的**通用渠道端口**（签名、归一化、发送、能力、健康）。
2. 每种账号类型（公众号 / 小程序客服 / 微信客服 / 企微）各有独立的
   ``AccountTypeProfile``，能力矩阵必须逐类型声明。
3. **不猜测个人微信接口**。个人微信没有合规直连协议，因此不提供实现。

当前状态：**已实现未测试**。没有确定账号类型与资质，从未联调。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ling.contracts.v1 import (
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    InputPart,
    InteractionEnvelope,
    Modality,
    Origin,
)
from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage, ReceiptQuery


class WeChatAccountType(StrEnum):
    """已获准的账号类型。新增类型必须先在这里登记。"""

    official_account = "official_account"  # 公众号（服务号/订阅号）
    miniprogram_customer_service = "miniprogram_customer_service"  # 小程序客服消息
    wecom_customer_service = "wecom_customer_service"  # 微信客服（企业微信）
    wecom_internal_bot = "wecom_internal_bot"  # 企微内部机器人
    # 个人微信没有合规直连协议：故意不登记。


@dataclass(frozen=True)
class AccountTypeProfile:
    """一个账号类型的能力与限制。数值来自官方文档，必须逐账号探测确认。"""

    account_type: WeChatAccountType
    supports_private: bool
    supports_group: bool
    supports_proactive: bool
    max_text_length: int
    receipt_reliability: str
    notes: str

    @property
    def capability_id(self) -> str:
        return f"wechat.{self.account_type.value}"


PROFILES: dict[WeChatAccountType, AccountTypeProfile] = {
    WeChatAccountType.official_account: AccountTypeProfile(
        account_type=WeChatAccountType.official_account,
        supports_private=True,
        supports_group=False,
        # 公众号被动回复有 48 小时窗口；客服消息也受限，不能当无限主动推送
        supports_proactive=False,
        max_text_length=2048,
        receipt_reliability="best_effort",
        notes="被动回复窗口约 48 小时；主动群发需模板消息且受平台审核",
    ),
    WeChatAccountType.miniprogram_customer_service: AccountTypeProfile(
        account_type=WeChatAccountType.miniprogram_customer_service,
        supports_private=True,
        supports_group=False,
        supports_proactive=False,
        max_text_length=2048,
        receipt_reliability="best_effort",
        notes="仅在小程序会话内可发客服消息，用户需先进入会话",
    ),
    WeChatAccountType.wecom_customer_service: AccountTypeProfile(
        account_type=WeChatAccountType.wecom_customer_service,
        supports_private=True,
        supports_group=False,
        supports_proactive=False,
        max_text_length=2048,
        receipt_reliability="best_effort",
        notes="微信客服有会话状态与接待人员限制，主动触达受平台规则约束",
    ),
    WeChatAccountType.wecom_internal_bot: AccountTypeProfile(
        account_type=WeChatAccountType.wecom_internal_bot,
        supports_private=True,
        supports_group=True,
        supports_proactive=True,
        max_text_length=2048,
        receipt_reliability="best_effort",
        notes="企微内部机器人可在企业内部主动推送；仅限企业内部使用",
    ),
}


@dataclass
class WeChatConfig:
    account_type: WeChatAccountType
    instance_id: str = "wechat-default"
    app_id: str = ""
    app_secret: str = ""
    token: str = ""  # 回调签名 token
    encoding_aes_key: str | None = None  # 安全模式下的消息加密
    api_base: str = "https://api.weixin.qq.com"
    allowed_users: set[str] = field(default_factory=set)


class WeChatConnector:
    """微信连接器（按账号类型）。

    W2 边界：实现协议端口与能力矩阵；**未联调**。
    没有确定账号类型时，调用方应先读 ``PROFILES`` 决定能力，而不是假设。
    """

    connector_id = "wechat"

    def __init__(self, config: WeChatConfig, *, http: Any = None) -> None:
        if config.account_type not in PROFILES:
            raise ValueError(f"未登记的微信账号类型: {config.account_type}")
        self._config = config
        self.instance_id = config.instance_id
        self._http = http
        self._token_cache: tuple[str, datetime] | None = None

    @property
    def profile(self) -> AccountTypeProfile:
        return PROFILES[self._config.account_type]

    # ---------------------------------------------------------- 入站

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """签名校验（``msg_signature`` = sha1(sort(token, timestamp, nonce, encrypt))）。

        未配置 token 时拒绝一切请求（默认关闭）。
        """
        if not self._config.token:
            return False
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-wechat-signature") or lowered.get("msg_signature")
        timestamp = lowered.get("timestamp")
        nonce = lowered.get("nonce")
        if not signature or not timestamp or not nonce:
            return False
        # 重放窗口：5 分钟
        try:
            if abs(datetime.now(UTC).timestamp() - int(timestamp)) > 300:
                return False
        except ValueError:
            return False
        encrypt = ""
        if self._config.encoding_aes_key:
            import json

            try:
                payload = json.loads(body.decode("utf-8"))
                encrypt = str(payload.get("Encrypt", ""))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
        parts = sorted([self._config.token, timestamp, nonce, encrypt])
        expected = hashlib.sha1("".join(parts).encode()).hexdigest()
        return hmac.compare_digest(expected, signature)

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:
        """把微信回调翻译成统一信封。身份解析不在这里。"""
        msg_type = payload.get("MsgType") or payload.get("msg_type")
        if msg_type not in {"text", "event", "image", "voice"}:
            raise ChannelError(
                ChannelErrorKind.unsupported, f"暂不处理该微信消息类型: {msg_type}"
            )
        # 微信不提供稳定消息 ID；用 (FromUserName, CreateTime, MsgId) 组合
        external_id = str(
            payload.get("MsgId")
            or f"{payload.get('FromUserName', '')}:{payload.get('CreateTime', '')}"
        )
        if not external_id or external_id == ":":
            raise ChannelError(ChannelErrorKind.rejected, "微信回调缺少可去重的消息标识")

        account_ref = str(payload.get("FromUserName") or "")
        text = str(payload.get("Content") or "").strip()
        is_group = self.profile.supports_group and str(payload.get("ChatId") or "") != ""
        window_ref = (
            f"wechat:group:{payload.get('ChatId')}"
            if is_group
            else f"wechat:user:{account_ref}"
        )
        parts = [InputPart(modality=Modality.text, text=text)] if text else []
        if not parts:
            raise ChannelError(ChannelErrorKind.unsupported, "该微信消息没有可用文本内容")

        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id=external_id,
            account_ref=account_ref or None,
            window_ref=window_ref,
            modality=Modality.text,
            parts=parts,
            origin=Origin.user_statement,
        )

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """取回微信媒体。只接受微信官方域名，避免 SSRF。"""
        from urllib.parse import urlparse

        parsed = urlparse(media_ref)
        if parsed.scheme != "https":
            raise ChannelError(ChannelErrorKind.rejected, "微信媒体只接受 https URL")
        host = (parsed.hostname or "").lower()
        if not host.endswith("qpic.cn") and not host.endswith("weixin.qq.com"):
            raise ChannelError(ChannelErrorKind.rejected, f"微信媒体域名不在白名单: {host}")
        if self._http is None:
            raise ChannelError(ChannelErrorKind.unreachable, "未配置 HTTP 客户端")
        response = await self._http.get(media_ref)
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.rejected, f"拉取微信媒体 HTTP {response.status_code}"
            )
        return response.content, response.headers.get("content-type", "application/octet-stream")

    # ---------------------------------------------------------- 出站

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        limit = self.profile.max_text_length
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        for start in range(0, len(text), limit):
            chunks.append(text[start : start + limit])
        return chunks

    def capabilities(self) -> CapabilityDescriptor:
        profile = self.profile
        return CapabilityDescriptor(
            capability_id=profile.capability_id,
            version="1.0.0",
            modalities=[Modality.text, Modality.image],
            supports_private=profile.supports_private,
            supports_group=profile.supports_group,
            supports_proactive=profile.supports_proactive,
            supports_edit=False,  # 微信不支持编辑已发消息
            supports_media=True,
            max_text_length=profile.max_text_length,
            receipt_reliability=profile.receipt_reliability,  # type: ignore[arg-type]
            enabled=bool(self._config.app_id and self._config.app_secret),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送。各账号类型的端点不同，必须分别实现；未实现的类型明确报 unsupported。"""
        profile = self.profile
        if message.is_group and not profile.supports_group:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"{profile.account_type.value} 不支持群聊发送",
            )
        if self._http is None:
            raise ChannelError(ChannelErrorKind.unreachable, "未配置 HTTP 客户端")
        if not (self._config.app_id and self._config.app_secret):
            raise ChannelError(
                ChannelErrorKind.unauthorized, "缺少微信 appid/secret（未配置凭据）"
            )
        # 具体端点按账号类型分发；这里保留结构，未联调前不臆造 URL
        raise ChannelError(
            ChannelErrorKind.unsupported,
            f"{profile.account_type.value} 的发送端点尚未联调（W3 需按账号类型补全）",
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=f"wechat-recon-{query.operation_id}",
            operation_id=query.operation_id,
            channel="wechat",
            state=DeliveryState.uncertain,
            platform_message_id=query.platform_message_id,
            observed_at=datetime.now(UTC),
            detail="微信各账号类型均不提供逐条投递状态查询，只能确认受理",
        )

    async def health(self) -> tuple[bool, str]:
        if not (self._config.app_id and self._config.app_secret):
            return False, "缺少微信 appid/secret（未配置凭据）"
        return False, f"{self.profile.account_type.value} 未联调（W3 待测）"


def describe_account_types() -> list[dict[str, object]]:
    """给管理台展示的账号类型能力对照。"""
    return [
        {
            "account_type": profile.account_type.value,
            "capability_id": profile.capability_id,
            "supports_private": profile.supports_private,
            "supports_group": profile.supports_group,
            "supports_proactive": profile.supports_proactive,
            "max_text_length": profile.max_text_length,
            "receipt_reliability": profile.receipt_reliability,
            "notes": profile.notes,
            "implemented": False,
            "blocked_by": "需要确定账号类型并取得对应资质",
        }
        for profile in PROFILES.values()
    ]


__all__ = [
    "PROFILES",
    "AccountTypeProfile",
    "WeChatAccountType",
    "WeChatConfig",
    "WeChatConnector",
    "describe_account_types",
]
