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

import asyncio
import hashlib
import hmac
import logging
import time
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
        # 探活结果的短缓存。``health()`` 会**外呼** NapCat（``get_login_info``），
        # 之前端点没有鉴权也没缓存：任何能访问 6520 的人都能用它当放大器，
        # 一秒几十次地打 NapCat。缓存只是限流，不是能力——鉴权在路由层。
        self._health_cache: tuple[float, tuple[bool, str]] | None = None
        # 缓存有效期（秒）。探活是运维动作，10 秒的陈旧度完全够用。
        self.health_cache_seconds = 10.0
        self._health_clock = time.monotonic
        # 群名缓存：``group_id -> (monotonic 时间戳, 群名或 None)``。
        #
        # 为什么不落库：群名是**平台侧的展示信息**，不是业务事实。为它加一个
        # 数据库列要迁移、要回填、还要处理"群改名了什么时候失效"——而它
        # 丢了的代价只是界面显示「群 100200400」。进程内缓存 + 前端回退
        # 已经够用，并且重启后第一次读会自动重新采集。
        self._group_name_cache: dict[str, tuple[float, str | None]] = {}
        self.group_name_ttl = 6 * 3600.0  # 采集成功后的复用时长
        self.group_name_miss_ttl = 300.0  # 采集失败后的静默时长（别一直打 NapCat）
        # 单次采集超时。**故意比 `config.timeout_seconds` 短**：这条外呼挂在
        # 读接口上，等 15 秒的群名不如先显示「群 123」。
        self.group_name_timeout = 2.0
        self.group_name_cache_max = 512

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

        # 装饰段（引用 / @）被平台拒收时，**不能连正文一起丢掉**。
        #
        # 为什么会发生：她可以自己选表达方式（`capabilities/expression_choice`），
        # 而提示里写的是 ``[quote:消息序号]`` / ``[at:QQ号]``——上下文里
        # **从来没有给过她真实的消息 id 或 QQ 号**（`kernel/context.py` 只给
        # 名字）。于是她写出来的引用 id 常常对不上任何平台消息，NapCat 的
        # `send_*_msg` 会整条拒收（``retcode=100``）。
        #
        # 结果是"她决定了回、正文也生成了，对方一个字都没收到"，而本地只知道
        # "这段没送达"。引用是**装饰**，正文才是话本身：平台明确拒收
        # （``rejected``）说明这一条根本没发出去，此时去掉装饰重发一次，
        # 宁可少一个 @ 也不能让她的话消失。
        #
        # 只在 ``rejected`` 上重试：``uncertain``（异步提交/响应不是 JSON）
        # 可能**已经发出去了**，重发就是重复消息；``unreachable`` /
        # ``unauthorized`` 重发也不会有不同结果。
        mentions = list(getattr(message, "mentions", None) or [])
        reply_to = getattr(message, "reply_to", None)
        degraded: str | None = None
        try:
            data = await self._call(action, params)
        except ChannelError as exc:
            if not (mentions or reply_to) or exc.kind is not ChannelErrorKind.rejected:
                raise
            degraded = exc.message
            logger.warning(
                "装饰段被平台拒收，去掉引用/@ 重发一次（正文不丢）：%s", exc.message
            )
            params["message"] = build_outbound_segments(
                message.text, list(message.media_refs)
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
            # 降级重发过就如实写进回执：事后对账时能看出"这条为什么少了引用"。
            detail=(
                f"已去掉引用/@ 重发（平台拒收装饰段：{degraded}）"
                if degraded
                else None
            ),
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
        """校验上报来源。**没配 secret 就一律拒绝**（fail-closed）。

        配了 secret 就必须验 HMAC-SHA1；配了 self_id 就要求
        ``X-Self-ID`` 与之一致（防止别的实例误配到同一入口）。

        ## 为什么没配 secret 时不再放行

        原实现按 OneBot 规范允许"未加密上报"（只要求带 ``X-Self-ID``），
        理由是"依赖网络隔离"。这个前提在默认部署下**不成立**：

        - ``POST /v1/onebot/events`` 这条路由**没有任何 auth 依赖**
          （见 ``src/ling/api/routes/onebot.py``），端口 6520 默认对外发布；
        - ``deploy/env.example`` 里 ``LING_ONEBOT_SECRET=`` 默认为空；
        - 正常使用必须配 ``LING_ACCESS_LIST``，而名单里就是 QQ 号。

        于是任何能访问 6520 的人只要伪造 ``X-Self-ID`` 和
        ``user_id=<白名单里的 QQ 号>``，就能以该用户身份对话、写记忆、
        让她向真实好友发言。这是完整的账号接管，不是理论风险。

        因此：secret 是这条入口的**必要条件**。没配 = 全部 403，
        启动时另打一条 ERROR 日志说明怎么配（见 ``ling/app.py``）。
        """
        secret = self._config.secret
        if not secret:
            # 没有密钥就没有任何可信来源的判据。X-Self-ID 是请求方自己填的，
            # 不构成凭据。
            return False

        normalized = {k.lower(): v for k, v in headers.items()}
        self_id = normalized.get("x-self-id")
        if self._config.self_id:
            if not self_id or str(self_id).strip() != str(self._config.self_id):
                return False
        elif not self_id:
            return False

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
        if post_type == "meta_event":
            # 心跳和生命周期**静默丢弃**，不记日志。
            #
            # 心跳每 30 秒一条，两天就能把 500 条内存缓冲里的有效日志全挤出去；
            # 而它证明的是"连接还活着"，这件事连接层已经在做（WS 断开、异常
            # 结束、鉴权失败都有 WARNING）。用 ``ignored`` 而不是 ``unsupported``：
            # 后者会让 inbound_intake 记一条"忽略事件"，那正是刷屏的来源。
            raise ChannelError(
                ChannelErrorKind.ignored,
                f"meta_event:{payload.get('meta_event_type') or ''}",
            )
        if post_type == "message_sent":
            # **机器人自己发出去的消息的回显**（NapCat/LLOneBot 的
            # ``reportSelfMessage``）。按设计没有要处理的东西：
            #
            # 正文、收件人、平台消息 id 都在 ``send()`` 的返回值里，
            # 再把它当成入站消息送进对话链路，等于让她对着自己说话。
            # 但它**每发一条就跟着来一条**，按"未处理事件"记一条 INFO
            # （浏览器行为日志里显示成「忽略事件: 暂不处理 OneBot 事件类型:
            # message_sent」）会把真正有用的日志挤出去。
            #
            # 所以归到 ``ignored``：inbound_intake 对它**不记行为日志**。
            # 想确认"这条到底发出去没有"时看下面这行 DEBUG——里面的
            # ``message_id`` 就是 ``send()`` 回执里的 ``platform_message_id``，
            # 两条日志对得上就说明平台确实收下了。
            #
            # 这里**不补写回执**：``DeliveryReceiptRow`` 必须挂在
            # ``operation_id`` 上，而回显事件里没有这个值；拿它凑一条
            # "已送达"只能证明平台存在这条消息，属于伪造送达证据。
            message_id = payload.get("message_id")
            logger.debug(
                "出站回显（自己发出的消息，按设计忽略）：message_id=%s target=%s",
                message_id,
                payload.get("target_id"),
            )
            raise ChannelError(
                ChannelErrorKind.ignored, f"message_sent:{message_id or ''}"
            )
        if post_type != "message":
            # notice/request 目前不进入对话链路。
            # 它们仍有价值（入群、撤回、戳一戳、加好友请求），但要先有对应
            # 处理策略，不能当普通消息塞给模型。
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
        # 本次称呼（card 优先）与昵称（人级）分开带走：显示用前者，
        # 落库更新 `subjects.display_name` 只用后者。
        sender_name, sender_nickname = _sender_fields(payload)
        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id=external_event_id,
            account_ref=user_id,
            sender_name=sender_name,
            sender_nickname=sender_nickname,
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
        """下载媒体。OneBot 的图片/语音给的是 http(s) url 或本地 file 路径。

        ## 为什么要有域名白名单与大小上限

        原实现是「任意 http(s) URL + ``response.content`` 整体入内存」：
        既能被拿来探测内网（SSRF），也能被一个超大响应把进程内存吃光。
        媒体 URL 来自**平台事件正文**，也就是外部可控输入，不能当可信。

        白名单匹配用 ``host == h or host.endswith("." + h)``，**不是裸
        ``endswith``**——``evilqpic.cn`` 以 ``qpic.cn`` 结尾，裸匹配会放行。

        **端口也要校验**（``connectors/media_url.py``）：白名单里全是图床域名，
        腾讯图床不走非标准端口。只校验 host 时 ``gchat.qpic.cn:8443`` 会被放行，
        一台把该域名反代到别处的机器就能借非标准端口把请求引到内网。
        """
        from urllib.parse import urlparse

        from connectors.media_url import host_allowed, port_is_standard

        parsed = urlparse(media_ref)
        if parsed.scheme not in {"http", "https"}:
            # file:// 或本地路径：规范允许 OneBot 直接给本地文件路径，
            # 但那要求 OneBot 与 ling 同机。这里明确不支持，避免误读别的机器的路径。
            raise ChannelError(
                ChannelErrorKind.unsupported,
                "仅支持 http(s) 媒体地址；本地路径需 OneBot 与本服务同机",
            )
        host = (parsed.hostname or "").lower()
        if not host_allowed(host, self._config.media_allowed_hosts):
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 媒体域名不在白名单内: {host or '(空)'}",
            )
        if not port_is_standard(parsed):
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 媒体端口不是该协议的默认端口: {parsed.netloc}",
            )

        limit = self._config.media_max_bytes
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        try:
            # **流式读**，边读边计数：``response.content`` 会先把整个响应体
            # 读进内存，等发现超限时内存已经吃掉了。
            #
            # `follow_redirects=False`：白名单只校验了**第一跳**。允许跟随
            # 重定向的话，一个白名单域名只要 302 到 127.0.0.1 或内网地址，
            # 白名单就形同虚设（SSRF 的常见绕过）。httpx 默认不跟随，
            # 这里显式写出来，避免调用方注入的 client 打开了跟随。
            async with client.stream("GET", media_ref, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ChannelError(
                        ChannelErrorKind.rejected,
                        f"拉取 OneBot 媒体 HTTP {response.status_code}",
                    )
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > limit:
                            raise ChannelError(
                                ChannelErrorKind.rejected,
                                f"媒体声明大小 {declared} 字节，超过上限 {limit}",
                            )
                    except ValueError:
                        # 头部不是数字：不因此放行，交给下面的流式计数兜底
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise ChannelError(
                            ChannelErrorKind.rejected,
                            f"媒体超过上限 {limit} 字节，已中断下载",
                        )
                    chunks.append(chunk)
                mime = response.headers.get(
                    "content-type", "application/octet-stream"
                )
                return b"".join(chunks), mime
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable, f"媒体下载失败: {exc}", retriable=True
            ) from exc
        finally:
            if owns:
                await client.aclose()

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
        """探活：``get_login_info`` 是最轻的可用性探测。

        **结果短缓存（默认 10 秒）**：这条探活是**外呼** NapCat 的。之前端点
        既没鉴权也没缓存，任何能访问 API 端口的人都能拿它当放大器打 NapCat
        （一台个人机器上的 NapCat 撑不住这种"健康检查"）。缓存把上限压到
        "每 10 秒一次"，鉴权在路由层另算。

        失败结果同样缓存：NapCat 挂掉时反复重试只会更糟。代价是恢复最多晚
        10 秒被发现，对运维探活可以接受。
        """
        now = self._health_clock()
        cached = self._health_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        result = await self._probe_login()
        self._health_cache = (now + self.health_cache_seconds, result)
        return result

    async def fetch_group_name(self, group_id: str) -> str | None:
        """采集群名（``get_group_info``）。**拿不到就返回 ``None``，绝不抛**。

        ## 为什么采集放在连接器里

        群名是平台侧的事实，只有连接器知道怎么问（OneBot 的
        ``get_group_info``；换成 QQ 官方机器人就是另一个动作）。上层的
        对话/接口层只该问"这个群叫什么"，不该认识 ``group_id`` 以外的协议细节。

        ## 为什么带缓存、还给失败也缓存

        调用点是**读侧**（会话列表），没有缓存的话每开一次页面就打一遍
        NapCat；群多的时候就是一阵外呼风暴。成功缓存久一点（群名很少改），
        失败缓存短一点（NapCat 刚恢复时别让界面长时间空着）。

        ## 为什么失败要吞掉

        群名只是标题，不是对话必需信息。``get_group_info`` 在不少 OneBot
        实现里根本没实现（404 → ``unsupported``），也可能因为断连超时。
        为了一个标题让会话列表 500，是拿"次要信息"换"主功能"。
        调用方拿到 ``None`` 时前端回退成「群 {id}」。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return None
        now = time.monotonic()
        cached = self._group_name_cache.get(gid)
        if cached is not None:
            stamped, cached_name = cached
            ttl = self.group_name_ttl if cached_name else self.group_name_miss_ttl
            if now - stamped < ttl:
                return cached_name

        name: str | None = None
        try:
            data = await asyncio.wait_for(
                self._call("get_group_info", {"group_id": gid}),
                timeout=self.group_name_timeout,
            )
            if isinstance(data, dict):
                raw = data.get("group_name")
                if isinstance(raw, str) and raw.strip():
                    # 截到与 windows.title / subjects.display_name 同级的长度：
                    # 契约里这不是长文本字段，超长只会在界面上被裁一次。
                    name = raw.strip()[:128]
        except (ChannelError, TimeoutError) as exc:
            # 超时也走这里：``asyncio.wait_for`` 抛的是内建 TimeoutError。
            logger.debug("采集群名失败（回退为无名）：group=%s %s", gid, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - 采集是尽力而为，不能影响读侧
            logger.debug("采集群名异常（回退为无名）：group=%s %s", gid, type(exc).__name__)

        self._group_name_cache[gid] = (now, name)
        self._trim_group_name_cache()
        return name

    def _trim_group_name_cache(self) -> None:
        """缓存只保留最近的若干群，防止长跑进程里字典无限长。"""
        if len(self._group_name_cache) <= self.group_name_cache_max:
            return
        # 按时间戳丢最旧的：群名的价值随"最近是否活跃"衰减，不需要 LRU。
        for gid, _ in sorted(self._group_name_cache.items(), key=lambda kv: kv[1][0])[
            : len(self._group_name_cache) - self.group_name_cache_max
        ]:
            self._group_name_cache.pop(gid, None)

    async def _probe_login(self) -> tuple[bool, str]:
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


def _sender_fields(payload: dict) -> tuple[str | None, str | None]:
    """返回 (本次称呼, 昵称)。

    - 本次称呼 = ``card or nickname``：**这个群里此刻该叫她/他什么**。
      群名片是群级字段，私聊事件里根本不存在，所以私聊退回昵称是对的。
    - 昵称 = 只有 ``nickname``：**这个 QQ 号自己的名字**（跨群）。
      它是"人级"字段，落库更新 ``subjects.display_name`` 只用它——用群名片
      写人级列会被"这个人今天在 A 群改了个名片"带着抖（见 docs/GROUP-CARD-RESEARCH.md）。

    两者**分开传**，因为用途不同：显示用前者（改名下一句就生效），
    落库用后者（跨群稳定）。合成一个值就没法在落库时选择"只用昵称"。

    拿不到就是 ``None``——不要编 "N/A" 之类的占位符（AstrBot 有个 ``"N/A"``
    字面量的坑，照抄会把占位符写进库）。
    """
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None, None
    card = _clean_name(sender.get("card"))
    nickname = _clean_name(sender.get("nickname"))
    return (card or nickname), nickname


def _clean_name(value: object) -> str | None:
    """名字清洗：空白即"没有"，长度对齐 ``subjects.display_name`` 的 128。"""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text[:128]
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
