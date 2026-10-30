"""OneBot V11 连接器：实现 ChannelConnector 端口。

协议要点（依据 OneBot V11 规范）：
- **事件上报**（OneBot → ling）：HTTP POST JSON，头部带 ``X-Self-ID``；
  配了 secret 时带 ``X-Signature: sha1=<hmac_sha1(secret, body)>``
- **API 调用**（ling → OneBot）：``POST http://host:port/<action>``，
  头部 ``Authorization: Bearer <access_token>``
  响应 ``{"status":"ok","retcode":0,"data":{...}}``
  401=未提供令牌，403=令牌不符，404=无此 API，其余一律 200（含业务失败）
- **回执**：``send_private_msg`` / ``send_group_msg`` 的
  ``data.message_id`` 是平台消息 id，可用于对账

与官方 QQ Bot API 的区别：官方 API 用 openid（每个机器人不同），
OneBot 直接用 QQ 号。因此账号绑定的 ``external_account_id`` 是 QQ 号。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import httpx

from ling.contracts.v1 import (
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    InputPart,
    InteractionEnvelope,
    Modality,
    new_id,
)
from ling.ports.channel import (
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    ReceiptQuery,
)

from .message import (
    ParsedMessage,
    build_outbound_segments,
    parse_message,
    window_ref_for,
)
from .settings import OneBotConfig

logger = logging.getLogger("ling.connectors.onebot")

class OneBotV11Connector(ChannelConnector):
    """一个 OneBot V11 实现（NapCat / Lagrange / go-cqhttp 等）的连接器。"""

    connector_id = "onebot"

    def __init__(
        self,
        config: OneBotConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self.instance_id = config.instance_id

    # ------------------------------------------------------------ 出站

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送消息（群聊/私聊按 ``message.is_group`` 选 API）。"""
        if message.is_group:
            action = "send_group_msg"
            params: dict = {"group_id": self._target_id(message.window_ref)}
        else:
            action = "send_private_msg"
            params = {"user_id": self._target_id(message.window_ref)}
        # 出站 payload 只有目标 id + message 段，**不含 operation_id**：
        # ``message.operation_id`` 是本地 operation 行 ID，只用来回填下面
        # 回执里的 ``operation_id``（本地关联）；平台侧收不到这个字段，也不能
        # 按它去重或对账——平台侧凭据是响应里的 ``data.message_id``。
        params["message"] = build_outbound_segments(
            message.text,
            list(message.media_refs),
            list(getattr(message, "mentions", None) or []),
            getattr(message, "reply_to", None),
        )

        data = await self._call(action, params)

        # 拍一拍：**独立动作**，不是消息段。发完再拍。
        #
        # 失败只记日志、不影响已经送达的正文——拍一拍是"轻"的表达，
        # 因为它失败就把整条回复做成失败，是本末倒置。
        for target in list(getattr(message, "poke_targets", None) or []):
            try:
                poke: dict = {"user_id": str(target)}
                if message.is_group:
                    poke["group_id"] = self._target_id(message.window_ref)
                await self._call("send_poke", poke)
            except Exception as exc:  # noqa: BLE001 - 轻动作失败不致命
                logger.warning("拍一拍失败（user=%s）：%s", target, type(exc).__name__)

        platform_message_id = None
        if isinstance(data, dict):
            mid = data.get("message_id")
            if mid is not None:
                platform_message_id = str(mid)
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=(
                DeliveryState.delivered
                if platform_message_id
                else DeliveryState.sent
            ),
            platform_message_id=platform_message_id,
            observed_at=_now(),
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """对账：OneBot 没有独立的"查送达"API。

        只能用 ``get_msg`` 确认消息存在（存在 ≠ 已读，但能区分
        "发出去了" 与 "根本没到平台"）。查不到就是不确定——
        **不能**当成失败，否则会重发造成重复。
        """
        try:
            data = await self._call("get_msg", {"message_id": query.platform_message_id})
        except ChannelError:
            return DeliveryReceipt(
                receipt_id=new_id("rcpt"),
                operation_id=query.operation_id,
                channel=self.connector_id,
                state=DeliveryState.uncertain,
                observed_at=_now(),
                detail="查询失败，结果未知",
            )
        found = isinstance(data, dict) and data.get("message_id") is not None
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=DeliveryState.delivered if found else DeliveryState.uncertain,
            platform_message_id=query.platform_message_id,
            observed_at=_now(),
            detail=None if found else "平台无此消息",
        )

    async def _call(self, action: str, params: dict) -> object:
        """调用 OneBot HTTP API。"""
        url = f"{self._config.api_base.rstrip('/')}/{action}"
        headers = {"Content-Type": "application/json"}
        if self._config.access_token:
            headers["Authorization"] = f"Bearer {self._config.access_token}"
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        try:
            response = await client.post(url, json=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable,
                f"OneBot 不可达: {type(exc).__name__}",
                retriable=True,
            ) from exc
        finally:
            if owns:
                await client.aclose()

        # 状态码语义按规范：401/403 是鉴权问题，404 是 action 不存在，
        # 其余一律 200（业务失败靠 retcode 表达）。
        if response.status_code in {401, 403}:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"OneBot 鉴权失败（{response.status_code}）：检查 access_token",
                retriable=False,
            )
        if response.status_code == 404:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"OneBot 实现不支持该 API: {action}",
                retriable=False,
            )
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 返回 {response.status_code}",
                retriable=False,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelError(
                ChannelErrorKind.uncertain,
                "OneBot 响应不是 JSON（可能已发送但无法确认）",
            ) from exc

        status = str(body.get("status") or "")
        retcode = body.get("retcode")
        # **retcode 是权威字段**：成功必须 retcode == 0 且 status 不是 failed。
        # 原实现写的是 `status == "ok" or retcode == 0`——任一成立就返回成功，
        # 于是 `status="ok" + retcode=100`（平台明确报错）被当成发送成功，
        # 失败被记成"已送达"，既不会重试也不会告警。
        # 两个字段冲突时按失败处理（宁可多一次重投，也不能谎报送达）。
        if retcode == 0 and status != "failed":
            return body.get("data")
        if status == "async" or retcode == 1:
            # 异步提交：结果未知，不能算成功也不能算失败
            raise ChannelError(
                ChannelErrorKind.uncertain, detail_or("异步处理，结果未知", body)
            )
        raise ChannelError(
            ChannelErrorKind.rejected, detail_or(f"OneBot 调用失败 retcode={retcode}", body)
        )

    def _target_id(self, window_ref: str) -> str:
        """从窗口标识取出目标 id（群号或 QQ 号）。"""
        # window_ref 形如 "<instance>:group:<gid>" / "<instance>:private:<uid>"
        parts = window_ref.rsplit(":", 1)
        return parts[-1] if len(parts) == 2 else window_ref

    # ------------------------------------------------------------ 入站

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """校验上报来源。

        配了 secret 就必须验 HMAC-SHA1；配了 self_id 就要求
        ``X-Self-ID`` 与之一致（防止别的实例误配到同一入口）。
        两者都未配置时按规范"未加密上报"处理，但仍要求带 X-Self-ID，
        否则无法区分是哪个机器人。
        """
        normalized = {k.lower(): v for k, v in headers.items()}
        self_id = normalized.get("x-self-id")
        if self._config.self_id:
            if not self_id or str(self_id).strip() != str(self._config.self_id):
                return False
        elif not self_id:
            return False

        secret = self._config.secret
        if not secret:
            # 未配置密钥：规范允许，但这是降级状态（依赖网络隔离）。
            return True

        signature = normalized.get("x-signature") or ""
        if not signature.startswith("sha1="):
            return False
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha1
        ).hexdigest()
        return hmac.compare_digest(signature[len("sha1=") :], expected)

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:
        """把 OneBot 事件翻译成交互信封。

        只翻译，不改写语义：文本原样进 ``parts``，
        @ 与引用作为结构化字段保留，供决策层判断"是不是在跟我说话"。
        """
        post_type = str(payload.get("post_type") or "")
        if post_type != "message":
            # notice/request/meta_event 目前不进入对话链路。
            # 它们仍有价值（入群、撤回、戳一戳），但要先有对应处理策略，
            # 不能当普通消息塞给模型。
            raise ChannelError(
                ChannelErrorKind.unsupported, f"暂不处理 OneBot 事件类型: {post_type}"
            )

        message_type = str(payload.get("message_type") or "")
        is_group = message_type == "group"
        user_id = str(payload.get("user_id") or "")
        group_id = str(payload.get("group_id") or "")
        if not user_id:
            raise ChannelError(ChannelErrorKind.rejected, "事件缺少 user_id")
        if is_group and not group_id:
            raise ChannelError(ChannelErrorKind.rejected, "群消息缺少 group_id")

        parsed: ParsedMessage = parse_message(
            payload.get("message"), self_id=self._config.self_id
        )
        # 字符串格式时 raw_message 更完整（含 CQ 码）；
        # 数组格式时 message 已经是结构化，直接用解析结果。
        if not parsed.text.strip() and isinstance(payload.get("raw_message"), str):
            parsed = parse_message(payload["raw_message"], self_id=self._config.self_id)

        message_id = str(payload.get("message_id") or "")
        self_id = str(payload.get("self_id") or self._config.self_id or "")
        # 去重键必须包含 self_id 与 message_id：同一 QQ 号下 message_id 唯一，
        # 但多实例共库时只靠 message_id 会互相覆盖。
        external_event_id = f"{self_id}:{message_id}" if message_id else new_id("obe")

        occurred = payload.get("time")
        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id=external_event_id,
            account_ref=user_id,
            sender_name=_sender_name(payload),
            window_ref=window_ref_for(
                is_group=is_group,
                target_id=group_id if is_group else user_id,
                instance_id=self.instance_id,
            ),
            occurred_at=_from_ts(occurred),
            # **到达时间**与 `occurred_at`（平台事件时间）是两件事：
            # 前者是我们收到它的时刻，后者是平台说它发生的时刻。
            # 不记录到达时间就没法算"这条消息在队列里等了多久"。
            received_at=datetime.now(UTC),
            modality=parsed.modality(),
            # **永远带一个 text 段，哪怕是空串。**
            # 契约要求 `parts` 至少一个片段；而"只有 @ 没有正文"是真实场景
            # （QQ 里先 @ 再发正文很常见）。原来这里是 `if parsed.text else []`，
            # 于是空正文的 @ 直接被契约校验拒掉，连 bridge 都到不了——
            # 用户看到的是"@ 了她却毫无反应"。
            parts=[InputPart(modality=Modality.text, text=parsed.text)],
            reply_to=parsed.reply_to_message_id,
            media_refs=parsed.media_refs,
            mentions_actor=parsed.mentions_self,
        )

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """下载媒体。OneBot 的图片/语音给的是 url 或本地 file 路径。"""
        if media_ref.startswith(("http://", "https://")):
            client = self._http
            owns = client is None
            if client is None:
                client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
            try:
                response = await client.get(media_ref)
                response.raise_for_status()
                mime = response.headers.get("content-type", "application/octet-stream")
                return response.content, mime
            except httpx.HTTPError as exc:
                raise ChannelError(
                    ChannelErrorKind.unreachable, f"媒体下载失败: {exc}", retriable=True
                ) from exc
            finally:
                if owns:
                    await client.aclose()
        # file:// 或本地路径：规范允许 OneBot 直接给本地文件路径，
        # 但那要求 OneBot 与 ling 同机。这里明确不支持，避免误读别的机器的路径。
        raise ChannelError(
            ChannelErrorKind.unsupported,
            "仅支持 http(s) 媒体地址；本地路径需 OneBot 与本服务同机",
        )

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        """按 QQ 文本上限切分。"""
        limit = self._config.max_text_length
        if len(text) <= limit:
            return [text]
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=self.connector_id,
            version="11",
            modalities=[Modality.text],
            supports_private=True,
            supports_group=self._config.supports_group,
            supports_proactive=self._config.supports_proactive,
            supports_media=self._config.supports_media,
            max_text_length=self._config.max_text_length,
            receipt_reliability="best_effort",
            enabled=bool(self._config.api_base),
        )

    async def health(self) -> tuple[bool, str]:
        """探活：``get_login_info`` 是最轻的可用性探测。"""
        try:
            data = await self._call("get_login_info", {})
        except ChannelError as exc:
            return False, f"{exc.kind.value}: {exc.message}"
        if isinstance(data, dict):
            return True, f"ok self_id={data.get('user_id')}"
        return True, "ok"


def _now():  # noqa: ANN202
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _sender_name(payload: dict) -> str | None:
    """取发送者显示名。拿不到就返回 None——不要编一个假名字。

    注意：这一项**只用于称呼**。身份解析永远走 account_ref，
    因为 QQ 群昵称可以随便改、也可以完全重名。
    """
    sender = payload.get("sender")
    if isinstance(sender, dict):
        for key in ("card", "nickname"):
            value = sender.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:128]
    return None


def _from_ts(value: object):  # noqa: ANN202
    from datetime import UTC, datetime

    try:
        # 平台字段可能是 int/str/float，也可能是 None。
        # 显式走 float 再转 timestamp：int() 对 None 抛 TypeError 是对的，
        # 但类型检查器不接受 object 直接进 int()。
        return datetime.fromtimestamp(float(value), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def detail_or(default: str, body: dict) -> str:
    for key in ("wording", "message", "msg"):
        value = body.get(key)
        if value:
            return f"{default}（{value}）"
    return default


__all__ = ["OneBotV11Connector"]
