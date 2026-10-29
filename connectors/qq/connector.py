"""QQ 官方机器人连接器（W1：只开发，未测试）。

依据（2026-10-26 核对官方文档）：
- QQ 机器人开放平台 v2 API：`https://api.sgroup.qq.com`
- 鉴权：`Authorization: Bot {appid}.{token}`；当前推荐用 AppID + AppSecret 换
  `access_token`（`POST /app/getAppAccessToken`），有效期 7200s。
- 事件：Webhook 回调（Ed25519 签名）或 WebSocket 网关。
- 发送文档注明**主动推送能力自 2025-04-21 起不再提供**，同页仍保留旧配额说明：
  因此本连接器**默认禁止主动投递**，必须以账号实际权限探测为准。

本模块不拥有第二套人格、记忆或领域库；它只做协议翻译、签名校验、去重与回执。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

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

QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_TOKEN_PATH = "/app/getAppAccessToken"

# 事件类型（官方 intents / 回调 op）
EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"  # 私聊
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"  # 群聊 @机器人
EVENT_AT_MESSAGE_CREATE = "AT_MESSAGE_CREATE"  # 频道 @（不同产品线）
EVENT_DIRECT_MESSAGE_CREATE = "DIRECT_MESSAGE_CREATE"  # 频道私信

SUPPORTED_EVENTS = {
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    EVENT_AT_MESSAGE_CREATE,
    EVENT_DIRECT_MESSAGE_CREATE,
}


@dataclass
class QQConfig:
    """一个 QQ 机器人实例的配置。凭据只在服务端。"""

    app_id: str
    app_secret: str
    instance_id: str = "qq-default"
    # 主动推送：默认关闭，必须由账号权限探测结果显式打开
    proactive_enabled: bool = False
    # 允许的群/私聊白名单；空表示不额外限制（仍受平台权限约束）
    allowed_groups: set[str] = field(default_factory=set)
    allowed_users: set[str] = field(default_factory=set)
    api_base: str = QQ_API_BASE
    # Webhook Ed25519 公钥（hex 或 base64）。未配置时拒绝一切入站事件。
    webhook_public_key: str | None = None


class QQTokenCache:
    """access_token 缓存。提前 60 秒过期，避免临界失败。"""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self) -> str | None:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        return None

    def set(self, token: str, expires_in: int) -> None:
        self._token = token
        self._expires_at = time.monotonic() + max(60, expires_in)

    def clear(self) -> None:
        self._token = None
        self._expires_at = 0.0


class QQConnector:
    """QQ 官方接口连接器。

    W1 状态：**已实现未测试**。没有 appid/secret 与已开通的私聊/群聊权限，
    因此从未与真实平台联调；所有网络路径都以 httpx 调用与错误分类实现。
    """

    connector_id = "qq"

    def __init__(self, config: QQConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self.instance_id = config.instance_id
        self._tokens = QQTokenCache()
        self._http = http
        self._owns_http = http is None

    # ---------------------------------------------------------- 鉴权

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def access_token(self) -> str:
        cached = self._tokens.get()
        if cached:
            return cached
        client = await self._client()
        try:
            response = await client.post(
                f"{self._config.api_base}{QQ_TOKEN_PATH}",
                json={"appId": self._config.app_id, "clientSecret": self._config.app_secret},
            )
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable, f"QQ 鉴权端点不可达: {exc}", retriable=True
            ) from exc
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"QQ 鉴权失败 HTTP {response.status_code}: {response.text[:200]}",
            )
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise ChannelError(ChannelErrorKind.unauthorized, "QQ 鉴权响应缺少 access_token")
        self._tokens.set(str(token), int(data.get("expires_in", 7200)))
        return str(token)

    def _auth_header(self, token: str) -> dict[str, str]:
        # 官方文档：Authorization: Bot {appid}.{token}
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self._config.app_id,
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------- 入站

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """Webhook 签名校验。

        官方回调使用 Ed25519：``X-Signature-Ed25519`` + ``X-Signature-Timestamp``，
        签名内容为 ``timestamp + body``，公钥由 ``X-Signature-Ed25519`` 对应的
        bot secret 派生（``/app/getAppAccessToken`` 前需先在开放平台配置）。

        W1 未持有真实公钥，因此这里实现**可验证的严格模式**：
        配置了 ``webhook_public_key`` 时用 Ed25519 校验；未配置时拒绝一切请求
        （默认关闭，绝不"宽松通过"）。
        """
        signature = headers.get("x-signature-ed25519") or headers.get("X-Signature-Ed25519")
        timestamp = headers.get("x-signature-timestamp") or headers.get("X-Signature-Timestamp")
        public_key = self._config.webhook_public_key
        if not public_key:
            # 没有公钥就不能验证签名 → 一律拒绝，避免伪造事件进入
            return False
        if not signature or not timestamp:
            return False
        try:
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:  # pragma: no cover
            return False
        # 重放窗口：5 分钟
        try:
            if abs(time.time() - int(timestamp)) > 300:
                return False
        except ValueError:
            return False
        try:
            key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        except (ValueError, TypeError):
            try:
                key = Ed25519PublicKey.from_public_bytes(
                    base64.b64decode(public_key)
                )
            except Exception:  # noqa: BLE001
                return False
        message = timestamp.encode() + body
        try:
            key.verify(bytes.fromhex(signature), message)
            return True
        except Exception:  # noqa: BLE001 - 签名不匹配
            try:
                key.verify(base64.b64decode(signature), message)
                return True
            except Exception:  # noqa: BLE001
                return False

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:
        """把 QQ 回调 payload 翻译成统一信封。

        身份解析**不在这里**：``account_ref`` 只是渠道侧 ID，服务器再查绑定表。
        """
        event_type = payload.get("t") or payload.get("event_type") or ""
        data = payload.get("d") or payload
        if event_type not in SUPPORTED_EVENTS and event_type:
            raise ChannelError(
                ChannelErrorKind.unsupported, f"暂不处理该 QQ 事件类型: {event_type}"
            )

        message_id = str(data.get("id") or data.get("message_id") or "")
        if not message_id:
            raise ChannelError(ChannelErrorKind.rejected, "QQ 事件缺少消息 ID，无法去重")

        author = data.get("author") or {}
        author_id = str(author.get("user_openid") or author.get("id") or author.get("union_openid") or "")
        group_openid = data.get("group_openid")
        channel_id = data.get("channel_id")
        guild_id = data.get("guild_id")

        # 窗口：私聊按用户，群聊按群；两者绝不合并
        if group_openid:
            window_ref = f"qq:group:{group_openid}"
        elif channel_id:
            window_ref = f"qq:channel:{channel_id}"
        elif guild_id:
            window_ref = f"qq:guild:{guild_id}:{author_id}"
        else:
            window_ref = f"qq:c2c:{author_id}"

        text = str(data.get("content") or "").strip()
        timestamp = data.get("timestamp")
        occurred_at: datetime | None = None
        if isinstance(timestamp, str):
            try:
                occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                occurred_at = None

        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id=message_id,
            occurred_at=occurred_at,
            account_ref=author_id or None,
            window_ref=window_ref,
            modality=Modality.text,
            parts=[InputPart(modality=Modality.text, text=text)],
            origin=Origin.user_statement,
        )

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """取回 QQ 附件。返回 (bytes, mime)。

        需要 ``access_token`` 与附件 URL 白名单（只允许 QQ 官方域名），
        避免把任意 URL 变成 SSRF 跳板。
        """
        allowed_hosts = {"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "qq.com"}
        from urllib.parse import urlparse

        parsed = urlparse(media_ref)
        if parsed.scheme not in {"https"}:
            raise ChannelError(ChannelErrorKind.rejected, "QQ 媒体只接受 https URL")
        host = (parsed.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            raise ChannelError(
                ChannelErrorKind.rejected, f"QQ 媒体域名不在白名单内: {host}"
            )
        client = await self._client()
        try:
            response = await client.get(media_ref)
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable, f"拉取 QQ 媒体失败: {exc}", retriable=True
            ) from exc
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.rejected, f"拉取 QQ 媒体 HTTP {response.status_code}"
            )
        mime = response.headers.get("content-type", "application/octet-stream")
        return response.content, mime

    # ---------------------------------------------------------- 出站

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        """QQ 文本上限按官方文档取 2000 字符（不同接口可能更小，需实测）。

        超出时按句子边界切分，绝不静默截断。
        """
        limit = 2000
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current = ""
        for sentence in _split_sentences(text):
            if len(current) + len(sentence) <= limit:
                current += sentence
            else:
                if current:
                    chunks.append(current)
                while len(sentence) > limit:
                    chunks.append(sentence[:limit])
                    sentence = sentence[limit:]
                current = sentence
        if current:
            chunks.append(current)
        return chunks

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送消息。

        - 私聊：``POST /v2/users/{openid}/messages``
        - 群聊：``POST /v2/groups/{group_openid}/messages``
        群聊必须带 ``msg_id``（被动回复）或 ``event_id``；主动群发默认不可用。
        """
        token = await self.access_token()
        client = await self._client()

        if message.is_group:
            group_openid = _group_from_window(message.window_ref)
            if not group_openid:
                raise ChannelError(
                    ChannelErrorKind.rejected, f"无法从窗口标识解析群 openid: {message.window_ref}"
                )
            url = f"{self._config.api_base}/v2/groups/{group_openid}/messages"
            payload: dict[str, Any] = {"content": message.text, "msg_type": 0}
            if message.reply_to:
                payload["msg_id"] = message.reply_to
        else:
            user_openid = _user_from_window(message.window_ref)
            if not user_openid:
                raise ChannelError(
                    ChannelErrorKind.rejected, f"无法从窗口标识解析用户 openid: {message.window_ref}"
                )
            url = f"{self._config.api_base}/v2/users/{user_openid}/messages"
            payload = {"content": message.text, "msg_type": 0}
            if message.reply_to:
                payload["msg_id"] = message.reply_to

        try:
            response = await client.post(url, headers=self._auth_header(token), json=payload)
        except httpx.HTTPError as exc:
            # 网络层失败：结果未知，必须对账而不是重发
            raise ChannelError(
                ChannelErrorKind.uncertain, f"QQ 发送网络异常，结果未知: {exc}", retriable=False
            ) from exc

        if response.status_code == 401:
            self._tokens.clear()
            raise ChannelError(ChannelErrorKind.unauthorized, "QQ 鉴权失效，需重新获取 token")
        if response.status_code == 429:
            raise ChannelError(ChannelErrorKind.rate_limited, "QQ 限流", retriable=True)
        if response.status_code >= 500:
            raise ChannelError(
                ChannelErrorKind.uncertain,
                f"QQ 服务端 {response.status_code}，投递结果未知",
                retriable=False,
            )
        if response.status_code >= 400:
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"QQ 拒绝发送 HTTP {response.status_code}: {response.text[:200]}",
            )
        data = response.json() if response.content else {}
        platform_id = data.get("id")
        if not platform_id:
            # 200 但没有 message id：不能算已送达
            return DeliveryReceipt(
                receipt_id=f"qq-{message.operation_id}",
                operation_id=message.operation_id,
                channel="qq",
                state=DeliveryState.uncertain,
                observed_at=datetime.now(UTC),
                detail="QQ 返回 200 但缺少消息 ID，需对账",
            )
        return DeliveryReceipt(
            receipt_id=f"qq-{message.operation_id}",
            operation_id=message.operation_id,
            channel="qq",
            state=DeliveryState.sent,
            platform_message_id=str(platform_id),
            observed_at=datetime.now(UTC),
            detail="QQ 已受理（被动回复窗口内）",
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """QQ 没有独立的投递状态查询接口；只能确认"已受理"。

        因此这里返回 ``uncertain`` 并说明：不能把"接口 200"当成"用户已读"。
        """
        return DeliveryReceipt(
            receipt_id=f"qq-recon-{query.operation_id}",
            operation_id=query.operation_id,
            channel="qq",
            state=DeliveryState.uncertain,
            platform_message_id=query.platform_message_id,
            observed_at=datetime.now(UTC),
            detail="QQ 官方接口不提供逐条投递状态查询，只能确认受理",
        )

    def capabilities(self) -> CapabilityDescriptor:
        """能力矩阵。未做账号权限探测前，主动推送一律声明为不支持。"""
        return CapabilityDescriptor(
            capability_id="qq.message",
            version="1.0.0",
            modalities=[Modality.text, Modality.image],
            supports_private=True,
            supports_group=True,
            supports_proactive=bool(self._config.proactive_enabled),
            supports_edit=False,  # QQ 官方接口不支持编辑已发消息
            supports_media=True,
            max_text_length=2000,
            receipt_reliability="best_effort",
            enabled=bool(self._config.app_id and self._config.app_secret),
        )

    async def health(self) -> tuple[bool, str]:
        if not self._config.app_id or not self._config.app_secret:
            return False, "缺少 QQ appid/appsecret（未配置凭据）"
        try:
            await self.access_token()
        except ChannelError as exc:
            return False, exc.message
        return True, "鉴权通过（未验证消息收发权限）"


def _split_sentences(text: str) -> list[str]:
    """按中文/英文句末标点切分，保留标点。"""
    out: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in "。！？!?；;\n":
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


def _group_from_window(window_ref: str) -> str | None:
    parts = window_ref.split(":")
    if len(parts) == 3 and parts[0] == "qq" and parts[1] == "group":
        return parts[2]
    return None


def _user_from_window(window_ref: str) -> str | None:
    parts = window_ref.split(":")
    if len(parts) == 3 and parts[0] == "qq" and parts[1] == "c2c":
        return parts[2]
    return None


def sign_payload_for_debug(app_secret: str, body: bytes) -> str:
    """仅用于本地自测的 HMAC 占位；真实回调用 Ed25519，不能拿这个冒充签名。"""
    return hashlib.sha256(app_secret.encode() + body).hexdigest()


def parse_callback_op(body: bytes) -> tuple[int, dict]:
    """解析回调 op。op=13 是平台校验回调地址（需回 op=12 + plain_token 签名）。"""
    payload = json.loads(body.decode("utf-8"))
    return int(payload.get("op", 0)), payload


__all__ = [
    "EVENT_C2C_MESSAGE_CREATE",
    "EVENT_GROUP_AT_MESSAGE_CREATE",
    "QQ_API_BASE",
    "QQConfig",
    "QQConnector",
    "QQTokenCache",
    "SUPPORTED_EVENTS",
    "parse_callback_op",
    "sign_payload_for_debug",
]
