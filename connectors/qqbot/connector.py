"""QQ 官方机器人连接器：实现 ChannelConnector 端口。

## 依据

QQ 开放平台（QQ Bot）HTTP API v2（**未核实**：本仓库没有真实凭据，
下面每条都只按官方文档实现，没有与真实平台联调过）：

- 基址：正式 ``https://api.sgroup.qq.com``，沙箱 ``https://sandbox.api.sgroup.qq.com``
- 鉴权：``app_id`` + ``client_secret`` 换 ``access_token``（约 7200 秒），
  之后请求带 ``Authorization: QQBot <access_token>``
- 发消息：``POST /v2/users/{openid}/messages``、``POST /v2/groups/{group_openid}/messages``
  带 ``msg_id``（被动回复）+ ``msg_seq``；返回 ``{"id": "...", "timestamp": "..."}``
- 入站：Webhook 回调，Ed25519 签名（``X-Signature-Ed25519`` + ``X-Signature-Timestamp``，
  签名内容 ``timestamp + body``）

## 三条不可越过的限制（必须体现在代码里，不能假装没有）

1. **被动回复有次数与时效**：一条用户消息只允许有限次回复、且只在一段时间内有效。
   超出后平台要么报错要么不投递。所以本连接器按消息记窗口与配额，
   超了就当成主动推送处理。
2. **主动推送需要单独申请权限**。默认 ``supports_proactive=False``：
   没有窗口时**直接拒绝并说明原因**，绝不敢发出去再"看起来成功了"。
3. **openid 不是 QQ 号**。它是每个机器人下独立的用户标识，也拿不到 QQ 号。
   身份键用 openid，不去猜、不去反查。

## 与 OneBot 的关系

协议完全不同（openid vs QQ 号、msg_id 被动回复 vs 直接发、签名 vs token），
所以是**独立连接器**；但收到 payload 之后的一切（白名单、去重、身份、决策、
投递）都走 ``ling.kernel.inbound_intake``，与 OneBot 共用同一套。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    build_message_body,
    media_marker,
    parse_inbound_event,
    send_path,
    split_text,
    target_from_window,
    window_ref_for,
)
from .settings import QQBotConfig

logger = logging.getLogger("ling.connectors.qqbot")

# Webhook 签名的重放窗口（秒）。平台会带 event_ts，超过这个窗口的请求一律拒绝，
# 避免有人录下一条合法请求反复重放。
_REPLAY_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class _PassiveWindow:
    """一条用户消息开启的被动回复窗口。"""

    msg_id: str
    opened_at: float
    # 已经用这条 msg_id 回过几次。平台按次数记账，我们必须自己记，
    # 否则会一直发到平台报错为止（用户侧看到的是"突然不回了"）。
    used: int = 0
    # 群聊还是单聊：官方的窗口时长与可回复次数**两者不同**
    # （单聊 60 分钟 / 4 次，群聊 5 分钟 / 5 次），所以窗口必须记场景。
    is_group: bool = False


class QQBotConnector(ChannelConnector):
    """QQ 官方机器人连接器。"""

    connector_id = "qqbot"

    def __init__(
        self,
        config: QQBotConfig,
        *,
        http: httpx.AsyncClient | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self.instance_id = config.instance_id
        self._http = http
        self._owns_http = http is None
        # 注入时钟：token 过期与被动窗口都是时间相关逻辑，
        # 测试必须能不睡觉就把时间推过去（否则只能靠 sleep 或改私有字段）。
        self._now = now or time.monotonic
        self._token: str | None = None
        self._token_expires_at = 0.0
        # 观测用：换过几次 token。排查"是不是每次都重新鉴权"靠它。
        self.token_refreshes = 0
        self.last_error: str | None = None
        # 窗口 → 被动回复窗口。**只在进程内存里**：多副本部署时，
        # 若回复由另一个副本发出，本副本没有窗口，会退化成"需要主动推送权限"
        # 并如实拒绝（见 settings 与 docs 的限制说明）。
        self._passive: dict[str, _PassiveWindow] = {}

    @property
    def config(self) -> QQBotConfig:
        """只读访问配置。路由层要判断"回调校验是否允许不签名"等策略，
        从连接器问比再从环境变量读一遍可靠。"""
        return self._config

    # ------------------------------------------------------------ 鉴权

    async def access_token(self, *, force: bool = False) -> str:
        """取 access_token，**提前**刷新而不是等 401。

        401 的代价是一次失败的发送（用户看得见的丢消息）；提前刷新只是
        多一次无副作用的请求。所以缓存到期前 ``token_refresh_margin_seconds``
        就换新的。
        """
        margin = self._config.token_refresh_margin_seconds
        if (
            not force
            and self._token
            and self._now() < self._token_expires_at - margin
        ):
            return self._token
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        if not self._config.app_id or not self._config.client_secret:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                "缺少 app_id / client_secret（LING_QQBOT_APP_ID、LING_QQBOT_CLIENT_SECRET）",
            )
        client, owns = self._client()
        try:
            response = await client.post(
                self._config.token_url,
                json={
                    "appId": self._config.app_id,
                    "clientSecret": self._config.client_secret,
                },
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable,
                f"QQ 鉴权端点不可达: {type(exc).__name__}",
                retriable=True,
            ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"QQ 鉴权失败 HTTP {response.status_code}: {_body_snippet(response)}",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ChannelError(
                ChannelErrorKind.unauthorized, "QQ 鉴权响应不是 JSON"
            ) from exc
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"QQ 鉴权响应缺少 access_token: {_body_snippet(response)}",
            )
        expires_in = _as_int(data.get("expires_in"), default=7200)
        self._token = str(token)
        # 至少给 60 秒有效期，避免平台返回极小值导致每次调用都重新换 token
        self._token_expires_at = self._now() + max(60, expires_in)
        self.token_refreshes += 1
        return self._token

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        """返回 (客户端, 是否本次新建)。新建的调用方负责关闭。"""
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self._config.timeout_seconds), True

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------ 出站

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送一条消息。**没有窗口又没主动权限时必须失败，不能假装送达。**"""
        is_group, target_id = target_from_window(message.window_ref)
        if not message.text.strip():
            raise ChannelError(ChannelErrorKind.rejected, "消息正文为空，未发送")

        if len(message.text) > self._config.max_text_length:
            # 单条超限必须由调用方先分片（render_output / plan_parts）。
            # 这里静默截断会让用户只看半句话、本地却记成已送达。
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"文本超出单条上限（{len(message.text)} > {self._config.max_text_length}），"
                "应先分片再发送",
            )

        if message.media_refs:
            # 富媒体是"先上传拿 file_info 再带 media 发送"的三步流程，当前未实现。
            # 明确拒绝而不是只发文字：只发文字却拿到回执 = 图被静默丢弃，
            # 而本地记录写的是"已送达"。
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"QQ 官方富媒体发送流程未实现（{len(message.media_refs)} 个媒体未发送）",
            )

        # 配额在**发送前**记账：宁可多发失败时白耗一次额度，
        # 也不让两条并发回复拿到同一个 msg_seq（平台会按重复回复拒绝）。
        passive = self._take_passive(message.window_ref, message.reply_to)
        if passive is None and not self._config.supports_proactive:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                "没有可用的被动回复窗口（未回复过该消息、窗口已过期或次数已用尽），"
                "而主动推送需要单独申请权限（LING_QQBOT_PROACTIVE=1 且平台已开通）",
            )

        msg_id, msg_seq = passive if passive else (None, None)
        body = build_message_body(
            message.text,
            msg_id=msg_id,
            msg_seq=msg_seq,
            msg_type=self._config.msg_type,
        )

        path = send_path(is_group=is_group, target_id=target_id)
        data = await self._post(path, body)

        platform_message_id = None
        if isinstance(data, dict):
            mid = data.get("id")
            if mid is not None:
                platform_message_id = str(mid)
        if not platform_message_id:
            # 200 但没有消息 id：平台说受理了但我们拿不到可对账的凭据。
            # 不能记成 delivered——这正是"假装送达"的入口。
            raise ChannelError(
                ChannelErrorKind.uncertain,
                f"平台返回 200 但缺少消息 id，投递结果未知: {data!r}"[:300],
            )
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=message.operation_id,
            channel=self.connector_id,
            # 与 onebot 保持一致：拿到平台消息 id 就记 delivered。
            # 两条渠道对"同一个事实"用同一个状态名，否则以后对账时
            # 要记两套规则。
            state=DeliveryState.delivered,
            platform_message_id=platform_message_id,
            observed_at=datetime.now(UTC),
            detail=(
                f"QQ 平台已受理（被动回复，msg_id={msg_id} seq={msg_seq}）"
                if msg_id
                else "QQ 平台已受理（主动推送）"
            ),
        )

    async def _post(self, path: str, body: dict) -> object:
        """带 token 的 POST，含一次 401 重试。"""
        return await self._request_with_retry("POST", path, body, allow_retry=True)

    async def api_get(self, path: str) -> object:
        """带 token 的 GET（网关地址等只读接口）。

        与发送共用同一套鉴权、401 刷新重试与失败分类：读接口失败也要
        给出可判定的种类，不能只在发送路径上做分类。
        """
        return await self._request_with_retry("GET", path, None, allow_retry=True)

    async def _request_with_retry(
        self, method: str, path: str, body: dict | None, *, allow_retry: bool
    ) -> object:
        token = await self.access_token()
        url = f"{self._config.base_url}{path}"
        client, owns = self._client()
        try:
            headers = {
                "Authorization": f"QQBot {token}",
                "X-Union-Appid": self._config.app_id,
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            response = await client.request(method, url, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 连接都没建立 → 确定没送到，可以重试
            raise ChannelError(
                ChannelErrorKind.unreachable,
                f"QQ API 不可达: {type(exc).__name__}",
                retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            # 读超时这类：请求可能已经到达平台并被执行 → 结果未知，不能重发
            raise ChannelError(
                ChannelErrorKind.uncertain,
                f"QQ API 请求结果未知（{type(exc).__name__}），需对账而非重发",
            ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code == 401 and allow_retry:
            # 提前刷新也会撞上平台侧作废（改密钥、切沙箱/正式）。
            # 401 = 未被受理，所以重试是安全的。
            logger.warning("QQ API 返回 401，强制刷新 token 后重试一次: %s", path)
            self._token = None
            self._token_expires_at = 0.0
            return await self._request_with_retry(method, path, body, allow_retry=False)
        if response.status_code == 401:
            self.last_error = "unauthorized"
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"QQ 鉴权失败（401）：{_body_snippet(response)}",
            )
        if response.status_code == 403:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"QQ 拒绝访问（403），通常是机器人缺少对应权限: {_body_snippet(response)}",
            )
        if response.status_code == 429:
            raise ChannelError(
                ChannelErrorKind.rate_limited,
                f"QQ 限流: {_body_snippet(response)}",
                retriable=True,
            )
        if response.status_code >= 500:
            # 5xx：平台可能已经发出去也可能没有 → 结果未知，**不能重发**
            raise ChannelError(
                ChannelErrorKind.uncertain,
                f"QQ 服务端 {response.status_code}，投递结果未知: {_body_snippet(response)}",
            )
        if response.status_code >= 400:
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"QQ 拒绝发送 HTTP {response.status_code}: {_body_snippet(response)}",
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ChannelError(
                ChannelErrorKind.uncertain, "QQ 响应不是 JSON，投递结果未知"
            ) from exc

        # 官方 v2 用 `err_code` 表达业务失败，**HTTP 状态可能仍是 200**。
        # 不认这个字段的话，一次失败的发送会被当成成功（而它没有 id，
        # 只会退化成 uncertain——分类不准，运维排查时看不到真正原因）。
        if isinstance(data, dict):
            err_code = data.get("err_code")
            if err_code not in (None, 0, "0"):
                raise ChannelError(
                    ChannelErrorKind.rejected,
                    f"QQ 接口返回错误 err_code={err_code}: "
                    f"{str(data.get('message') or '')[:200]}",
                )
        return data

    # ---------------------------------------------------- 被动回复窗口

    def _take_passive(
        self, window_ref: str, reply_to: str | None
    ) -> tuple[str, int] | None:
        """取这次发送可用的 (msg_id, msg_seq)；没有可用窗口就返回 None。

        - 显式给了 ``reply_to``：以它为准（可能是更早的消息）
        - 否则用该窗口最近一条入站消息开启的窗口
        - 过期 / 次数用尽 → None（上层据此判定为主动推送）
        """
        msg_id = reply_to or None
        if msg_id is None:
            window = self._passive.get(window_ref)
            if window is None:
                return None
            msg_id = window.msg_id
        window = self._passive.get(window_ref)
        if window is not None and window.msg_id == msg_id:
            ttl, max_replies = self._config.passive_quota(is_group=window.is_group)
            age = self._now() - window.opened_at
            if age > ttl:
                logger.info(
                    "被动回复窗口已过期（%.0fs > %.0fs，%s），按主动推送处理",
                    age,
                    ttl,
                    "群聊" if window.is_group else "单聊",
                )
                return None
            if window.used >= max_replies:
                logger.info(
                    "被动回复次数用尽（%d/%d，%s），按主动推送处理",
                    window.used,
                    max_replies,
                    "群聊" if window.is_group else "单聊",
                )
                return None
            seq = window.used + 1
            self._passive[window_ref] = _PassiveWindow(
                msg_id=msg_id,
                opened_at=window.opened_at,
                used=seq,
                is_group=window.is_group,
            )
            return msg_id, seq
        # 窗口不是这条消息开的（显式 reply_to 指到别处）：仍然允许一次回复，
        # seq=1；平台才是最终裁判，我们不替它编造配额。
        return msg_id, 1

    def _remember_passive(
        self, window_ref: str, msg_id: str, *, is_group: bool
    ) -> None:
        """记下一条入站消息开启的被动回复窗口。"""
        self._prune_passive()
        self._passive[window_ref] = _PassiveWindow(
            msg_id=msg_id, opened_at=self._now(), used=0, is_group=is_group
        )

    def _prune_passive(self) -> None:
        """清掉过期窗口，避免长期运行时无限增长。"""
        now = self._now()
        # 用两种场景里更长的那个做清理阈值，短的由取用时按窗口自己的场景判断
        ttl = max(
            self._config.passive_window_seconds_private,
            self._config.passive_window_seconds_group,
            1.0,
        )
        expired = [key for key, win in self._passive.items() if now - win.opened_at > ttl]
        for key in expired:
            self._passive.pop(key, None)

    # ------------------------------------------------------------ 入站

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """校验 Webhook 签名（Ed25519，签名内容 ``timestamp + body``）。

        **没有密钥就一律返回 False**：无法验签等于无法确认来源，
        宽松通过会把"谁都能伪造用户消息"变成默认行为。
        """
        normalized = {k.lower(): v for k, v in headers.items()}
        signature = normalized.get("x-signature-ed25519")
        timestamp = normalized.get("x-signature-timestamp")
        public_key = self._public_key()
        if public_key is None:
            logger.warning(
                "QQ 机器人没有配置 bot_secret / webhook 公钥，拒绝一切入站事件"
            )
            return False
        if not signature or not timestamp:
            return False
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
            return False
        message = str(timestamp).encode() + body
        try:
            public_key.verify(bytes.fromhex(signature), message)
            return True
        except Exception:  # noqa: BLE001 - 签名不匹配/格式错都是拒绝
            return False

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:
        """把事件体翻译成交互信封，并记下被动回复窗口。

        **身份不在这里解析**：``account_ref`` 只是渠道侧 openid，
        服务器再按 (instance, openid) 查绑定表。
        """
        parsed = parse_inbound_event(payload)
        # parse_inbound_event 已经保证群消息带 group_openid、私聊带 openid，
        # 这里的 or "" 只是让类型收敛；真为空会在下面 envelope 校验里暴露。
        target_id = (parsed.group_openid or "") if parsed.is_group else parsed.account_ref
        window_ref = window_ref_for(
            instance_id=self.instance_id,
            is_group=parsed.is_group,
            target_id=target_id,
        )
        # 这条消息就是后续被动回复能用的"凭据"。不记下来的话，
        # 回复时就没法带 msg_id，只能当成主动推送（默认被拒）。
        self._remember_passive(
            window_ref, parsed.message_id, is_group=parsed.is_group
        )

        parts = [InputPart(modality=Modality.text, text=parsed.text)]
        media_refs = list(parsed.media_refs)
        if media_refs:
            # 与 OneBot 侧一致的占位标记：让模型知道"这里有个附件"，
            # 同时 bridge 会剥掉标记判断"有没有真话"。
            markers = "".join(media_marker(_guess_content_type(ref)) for ref in media_refs)
            parts[0] = InputPart(modality=Modality.text, text=f"{parsed.text}{markers}")

        return InteractionEnvelope(
            connector_instance=self.instance_id,
            # 去重键：消息 id 在同一机器人下唯一；带上 app_id 让多机器人共库时
            # 也不会互相覆盖。
            external_event_id=f"{self._config.app_id}:{parsed.message_id}",
            account_ref=parsed.account_ref,
            sender_name=parsed.display_name,
            window_ref=window_ref,
            occurred_at=parsed.occurred_at,
            received_at=datetime.now(UTC),
            modality=parsed.modality(),
            parts=parts,
            media_refs=media_refs,
            mentions_actor=parsed.mentions_actor,
        )

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """下载附件。

        官方给的是带时效的 https 地址；只接受 https，不做域名白名单猜测——
        **未核实**附件实际域名（会随产品线变化），因此把"来源可信"这件事
        交给调用方的存储侧约束，而不是在这里硬编码一个可能过时的列表。
        """
        if not media_ref.startswith("https://"):
            raise ChannelError(
                ChannelErrorKind.unsupported, "QQ 官方附件只接受 https 地址"
            )
        client, owns = self._client()
        try:
            response = await client.get(media_ref)
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable,
                f"附件下载失败: {type(exc).__name__}",
                retriable=True,
            ) from exc
        finally:
            if owns:
                await client.aclose()
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"附件下载 HTTP {response.status_code}",
            )
        return response.content, response.headers.get("content-type", "application/octet-stream")

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        """按单条上限分片（中文句末标点优先），不截断、不丢字。"""
        return split_text(text, self._config.max_text_length)

    # ------------------------------------------------------------ 对账 / 能力

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """对账：QQ 官方**没有**按消息 id 查投递状态的公开接口。

        所以这里只能返回 ``uncertain`` 并说明原因——把它报成 delivered
        等于凭空造一个"送达"结论；报成 failed 又会导致重复发送。
        """
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=DeliveryState.uncertain,
            platform_message_id=query.platform_message_id,
            observed_at=datetime.now(UTC),
            detail=(
                "QQ 官方不提供逐条投递状态查询；只能确认平台已受理（未核实是否存在"
                "其它查询途径），需人工核对"
            ),
        )

    def capabilities(self) -> CapabilityDescriptor:
        """能力声明。

        - ``enabled``：凭据齐备且开关打开（**不代表**凭据有效，有效性只能探）
        - ``supports_proactive``：默认 False。主动推送要平台单独授权，
          这里如实反映配置，不把"想发"当成"能发"。
        """
        return CapabilityDescriptor(
            capability_id="qqbot.message",
            version="v2",
            modalities=[Modality.text],
            supports_private=True,
            supports_group=self._config.supports_group,
            supports_proactive=self._config.supports_proactive,
            # 官方接口不支持编辑已发消息
            supports_edit=False,
            supports_media=self._config.supports_media,
            max_text_length=self._config.max_text_length,
            receipt_reliability="best_effort",
            enabled=self._config.configured,
        )

    async def health(self) -> tuple[bool, str]:
        """探活：能不能换到 token。

        只能证明**鉴权**通过；消息能否真的发出去取决于被动窗口/主动权限，
        这一点在返回里如实写出来，不冒充"已连通"。
        """
        if not self._config.configured:
            return False, "缺少 app_id / client_secret（未配置凭据）"
        try:
            await self.access_token()
        except ChannelError as exc:
            return False, f"{exc.kind.value}: {exc.message}"
        remaining = max(0.0, self._token_expires_at - self._now())
        return True, (
            f"鉴权通过（app_id={self._config.app_id}，token 剩余 {remaining:.0f}s，"
            f"环境={'沙箱' if self._config.sandbox else '正式'}）；"
            "消息收发权限未验证"
        )

    # ------------------------------------------------------------ 签名工具

    def _public_key(self):  # noqa: ANN202
        """取用于验签的 Ed25519 公钥；没有可用的密钥就返回 None。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if self._config.webhook_public_key_hex:
            try:
                return Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(self._config.webhook_public_key_hex)
                )
            except (ValueError, TypeError):
                logger.warning("LING_QQBOT_WEBHOOK_PUBLIC_KEY 不是合法 hex 公钥，忽略")
        if self._config.bot_secret:
            return _private_key_from_secret(self._config.bot_secret).public_key()
        return None

    def sign_url_validation(self, plain_token: str, event_ts: str) -> dict[str, str]:
        """回调地址校验（op=13）要回的东西。

        官方要求返回 ``{"plain_token": ..., "signature": hex(ed25519(event_ts + plain_token))}``，
        私钥由 BotSecret 派生（**未核实**派生的补齐规则）。
        """
        if not self._config.bot_secret:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                "没有配置 bot_secret，无法完成回调地址校验",
            )
        signature = _private_key_from_secret(self._config.bot_secret).sign(
            (event_ts + plain_token).encode()
        )
        return {"plain_token": plain_token, "signature": signature.hex()}


# ------------------------------------------------------------------ 工具


def ed25519_seed(bot_secret: str) -> bytes:
    """BotSecret → Ed25519 seed（32 字节）。

    **未核实**：官方文档没有明说短 secret 的补齐方式。此处用"循环重复后截断
    到 32 字节"——这是同类实现里常见的做法。若平台侧计算方式不同，
    表现为"所有回调验签都失败"，此时直接配 ``webhook_public_key_hex`` 绕过。
    """
    raw = (bot_secret or "").encode("utf-8")
    if not raw:
        return b""
    repeats = 32 // len(raw) + 1
    return (raw * repeats)[:32]


def _private_key_from_secret(bot_secret: str):  # noqa: ANN202
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = ed25519_seed(bot_secret)
    if not seed:
        raise ChannelError(ChannelErrorKind.unauthorized, "bot_secret 为空，无法派生签名密钥")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _guess_content_type(url: str) -> str:
    """从附件 url 猜类型，仅用于正文占位标记。猜不出按 file。"""
    lowered = url.lower().split("?", 1)[0]
    for kind in ("image", "voice", "video"):
        if kind in lowered:
            return kind
    for ext, kind in (
        (".png", "image"),
        (".jpg", "image"),
        (".jpeg", "image"),
        (".gif", "image"),
        (".mp3", "voice"),
        (".silk", "voice"),
        (".mp4", "video"),
    ):
        if lowered.endswith(ext):
            return kind
    return "file"


def _as_int(value: object, *, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _body_snippet(response: httpx.Response, limit: int = 200) -> str:
    """错误体摘要。用摘要而不是全量：错误体可能很长，也可能含不该进日志的字段。"""
    try:
        return response.text[:limit]
    except Exception:  # noqa: BLE001 - 读不到正文不该掩盖原始错误
        return f"<{response.status_code}>"


def _hash_for_log(value: str) -> str:
    """配置自检时打印指纹而不是明文（用于排查"用错凭据"）。"""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def describe_config(config: QQBotConfig) -> dict[str, Any]:
    """配置摘要（不含明文密钥），给 /health 与管理端展示用。"""
    return {
        "instance_id": config.instance_id,
        "app_id": config.app_id,
        "app_id_fingerprint": _hash_for_log(config.app_id) if config.app_id else "",
        "sandbox": config.sandbox,
        "base_url": config.base_url,
        "bot_secret_configured": bool(config.bot_secret),
        "supports_proactive": config.supports_proactive,
        "supports_media": config.supports_media,
        "max_text_length": config.max_text_length,
    }


__all__ = ["QQBotConnector", "describe_config", "ed25519_seed"]
