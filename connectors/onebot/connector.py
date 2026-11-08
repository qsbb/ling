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
from collections import deque
from datetime import UTC, datetime, timedelta

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
    delivery_text_digest,
)

from .message import (
    ParsedMessage,
    build_outbound_segments,
    parse_message,
    window_ref_for,
)
from .settings import OneBotConfig

logger = logging.getLogger("ling.connectors.onebot")

# ---------------------------------------------------------------- 对账常量
#
# **全部是工程初值，未经真实 NapCat 长跑验证。** 依据见
# `docs/OUTBOUND-DELIVERY-RESEARCH.md` §4.2/§4.3/§4.5。

# 回显环形缓冲：`message_sent`（我们自己发出的消息的回显）只用来给
# "没有拿到 message_id 的那一段"补 id。**不是必需依赖**——NapCat 的
# `reportSelfMessage` 默认是 false，大多数部署里这条事件根本不会来。
ECHO_MAX_ENTRIES = 64

# 见过的入站 message_id 上限。用于出站引用段的**本地白名单**：
# 引用一个从没见过的 id，在 NapCat 上是"静默丢掉引用段、正文照发"（§4.4），
# 本地能先挡住就没必要让平台去丢。
KNOWN_MESSAGE_ID_LIMIT = 512

# `get_group_msg_history` / `get_friend_msg_history` 一次取多少条。
# 取太多会把 NapCat 打满（这是外呼），20 条覆盖几十秒的窗口足够。
HISTORY_LOOKBACK = 20

# 内容比对的时间窗：`[sent_at - 30s, sent_at + 180s]`。
# 下限留 30 秒是给"本地时间比平台快"的时钟偏差留余量。
HISTORY_MATCH_BEFORE_SECONDS = 30.0
HISTORY_MATCH_AFTER_SECONDS = 180.0

# "查不到 ≠ 没发出"（§4.2）的例外门槛：只有消息**足够旧**时，
# "平台查无此 id + 历史里也没有同文消息"这两个否证才足以判 failed。
# 太新的消息还可能在投递队列里，早判就会误记失败。
EXPLICIT_MISSING_GRACE_SECONDS = 180.0

# 平台明确说"消息不存在"的字样（NapCat 的 GetMsg 抛 `Error('消息不存在')`）。
_MISSING_HINTS = ("消息不存在", "not found", "消息不存在或已撤回")


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

        # ---- 对账/引用用到的进程内证据（都不落库） ----
        #
        # 1) 出站回显：`message_sent` 带上我们自己发出的消息的 `message_id`，
        #    与 `send_*_msg` 回执同一个 id 空间（NapCat 的
        #    `MessageUnique.createUniqueMsgId`），所以可以给"没拿到回执 id"
        #    的那一段补 id。
        self._echoes: deque[tuple[datetime, str, str, str]] = deque(
            maxlen=ECHO_MAX_ENTRIES
        )
        # 2) 发出但平台没回 message_id 的那几条：`operation_id -> (window_ref,
        #    正文, 发出时间)`。只在**同一进程**里能把回显对回 operation；
        #    worker 是另一个进程，所以对账不能只依赖它（见 `query_receipt`）。
        self._unidentified: dict[str, tuple[str, str, datetime]] = {}
        # 3) 见过的入站 message_id（引用白名单）。有界：只留最近的，
        #    因为 NapCat 的短 id 表本身也是 5000 条 FIFO（§4.2）。
        self._known_message_ids: deque[str] = deque(maxlen=KNOWN_MESSAGE_ID_LIMIT)
        self._known_message_set: set[str] = set()
        # 4) 外部事件 id → 平台 message_id。上下文要给模型**真实 id**（§5 P0-4），
        #    而 id 只有连接器知道（`f"{self_id}:{message_id}"` 是 OneBot 的拼法）。
        self._event_message_ids: dict[str, str] = {}

    # ------------------------------------------------------------ 出站

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送消息（群聊/私聊按 ``message.is_group`` 选 API）。

        ## 三件必须分开的事

        1. **引用白名单**：`reply_to` 必须是**我们真的见过**的入站 message_id，
           否则本地就去掉引用段（降级为普通消息），不让平台去静默丢它
           （NapCat 对无效 reply 是"丢弃该段、正文照发"，§4.4）。
        2. **装饰段兜底**：平台明确拒收（``rejected``）时去掉引用/@ 重发一次，
           正文不能跟着丢。**注意**：调研（§4.5）里 NapCat 全仓没有
           ``retcode=100``，无效引用走的是"静默丢段"，所以这条路基本不会触发；
           真正治本的是给模型真实 id（上下文里的 `[msg_id:…]`）。
        3. **回执语义**：`retcode=0` 但 `data` 为空 / 没有 ``message_id`` 时，
           平台只是"没说失败"，**不是**"确认送达"。这种情况记
           ``uncertain``（进对账），绝不记 ``sent``——`sent` 在上层被算成
           "已送达"，于是"平台没给 id"被当成"发完了"。
        """
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
        mentions = list(getattr(message, "mentions", None) or [])
        reply_to = getattr(message, "reply_to", None)
        dropped_reply: str | None = None
        if reply_to and self._known_message_set and str(reply_to) not in self._known_message_set:
            # 只丢**引用段**，不丢正文、也不丢 @：引用是装饰，正文才是话本身。
            dropped_reply = str(reply_to)
            logger.info(
                "引用目标不在见过的入站消息里，按普通消息发送（不发送引用段）：%s",
                dropped_reply,
            )
            reply_to = None
        params["message"] = build_outbound_segments(
            message.text,
            list(message.media_refs),
            mentions,
            reply_to,
        )

        # 装饰段（引用 / @）被平台明确拒收时，**不能连正文一起丢掉**。
        #
        # 只在 ``rejected`` 上重试：``uncertain``（异步提交/响应不是 JSON/
        # NapCat 的 1200、200）可能**已经发出去了**，重发就是重复消息；
        # ``unreachable`` / ``unauthorized`` 重发也不会有不同结果。
        #
        # 按调研（§4.4/§4.5）：NapCat 对**无效 reply 是静默丢弃该段、正文照发**，
        # 只有"所有段都被丢掉"才会变成错误。所以这条分支在 NapCat 上几乎不会
        # 触发——**别把它当成引用失效的主要修法**，治本是把真实 id 放进上下文。
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
        notes = []
        if degraded:
            # 降级重发过就如实写进回执：事后对账时能看出"这条为什么少了引用"。
            notes.append(f"已去掉引用/@ 重发（平台拒收装饰段：{degraded}）")
        if dropped_reply:
            notes.append(f"引用目标不在可见范围（{dropped_reply}），已降级为普通消息")
        if platform_message_id is None:
            # 平台回了成功但没有 id：**不能算已送达**，只能算"提交了、未确认"。
            self._remember_unidentified(message)
            notes.append("平台未返回 message_id（提交结果无法确认，进对账而不是记已送达）")
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=(
                DeliveryState.delivered
                if platform_message_id
                else DeliveryState.uncertain
            ),
            platform_message_id=platform_message_id,
            observed_at=_now(),
            detail="；".join(notes) if notes else None,
        )

    def _remember_unidentified(self, message: OutboundMessage) -> None:
        """记住"发出去了但平台没给 id"的那一条，供同进程的对账补 id 用。

        有界：只留最近 128 条（一次长回复最多 5 段，够覆盖正常会话）。
        """
        if len(self._unidentified) >= 128:
            oldest = sorted(self._unidentified.items(), key=lambda kv: kv[1][2])[:32]
            for key, _ in oldest:
                self._unidentified.pop(key, None)
        self._unidentified[str(message.operation_id)] = (
            message.window_ref,
            message.text,
            _now(),
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """对账：OneBot 没有独立的"查送达"API。

        三级证据，**只能单向确认**（§4.2）：

        1. 有 ``platform_message_id`` → ``get_msg``。查到 → ``delivered``；
           平台明确说"消息不存在" → **一个否证**，还要第二个才敢判 failed。
        2. 没有 id → 先看同进程的 ``message_sent`` 回显（回显带真实
           ``message_id``，与发送回执同一 id 空间）；再看
           ``get_group_msg_history`` / ``get_friend_msg_history`` 的**内容比对**
           （自己发的 + 同文 + 时间窗内，且必须**唯一命中**）。
        3. 两个否证（平台查无此 id + 近期历史里没有同文消息）**且**消息已过
           宽限期 → ``failed``；否则一律 ``uncertain``。

        **查不到 ≠ 没发出去**（§4.2）：NapCat 的短 id 表是 5000 条 FIFO，
        进程重启/超量/撤回都会"消息不存在"。所以不允许因为一次查不到就记失败。
        这里的 ``failed`` 只表示"两个独立证据都说没到平台"，而且仍然**不重发**。
        """
        if query.platform_message_id:
            try:
                data = await self._call(
                    "get_msg", {"message_id": query.platform_message_id}
                )
            except ChannelError as exc:
                if _looks_missing(exc.message):
                    return await self._negative_evidence(
                        query, reason=f"get_msg: {exc.message}"
                    )
                return self._receipt(
                    query,
                    DeliveryState.uncertain,
                    detail=f"查询失败，结果未知（{exc.message}）",
                )
            found = isinstance(data, dict) and data.get("message_id") is not None
            if found:
                return self._receipt(
                    query,
                    DeliveryState.delivered,
                    platform_message_id=query.platform_message_id,
                )
            return await self._negative_evidence(query, reason="get_msg: 空 data")

        # 没有平台 id：先看回显（同进程内最快、最准的证据）
        echo_id = self._match_echo(query)
        if echo_id:
            return self._receipt(
                query,
                DeliveryState.delivered,
                platform_message_id=echo_id,
                detail="按 message_sent 回显确认（回显里的 message_id 与发送回执同 id 空间）",
            )
        return await self._negative_evidence(query, reason="无平台 id")

    async def _negative_evidence(
        self, query: ReceiptQuery, *, reason: str
    ) -> DeliveryReceipt:
        """一个否证之后：能再拿到第二个否证、且消息已过宽限期，才判 failed。"""
        scanned = await self._scan_history(query)
        if scanned.matched_id:
            return self._receipt(
                query,
                DeliveryState.delivered,
                platform_message_id=scanned.matched_id,
                detail=f"按历史内容比对确认（{reason}）",
            )
        if scanned.ambiguous:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史里有不止一条同文消息，无法唯一命中",
            )
        if not scanned.ok:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史查询不可用，无法取得第二个否证",
            )
        if not scanned.saw_self:
            # **历史里一条自己的消息都没有** = 还不能证明"这份历史会包含我发的
            # 消息"。调研 §7.6 正是未证实项（`get_group_msg_history` 是否含
            # 机器人自己发的消息，没有实机验证）。在这种情况下"没找到同文消息"
            # 不构成否证——否则整个 NapCat 的行为差异会被我们误判成"没送达"。
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=(
                    f"{reason}；历史里看不到任何自己发出的消息，"
                    "无法据此判定（平台历史可能不含自身消息）"
                ),
            )
        # 两个否证都拿到了：还要过了宽限期才敢说"没到平台"
        sent_at = _aware(query.sent_at)
        if sent_at is not None and (
            _now() - sent_at
        ).total_seconds() < EXPLICIT_MISSING_GRACE_SECONDS:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史里也没有同文消息，但消息太新（可能仍在投递）",
            )
        return self._receipt(
            query,
            DeliveryState.failed,
            detail=(
                f"{reason}；近期历史里也没有同文消息（两个独立否证）——"
                "按未到达平台记录。注意：平台已发出但被撤回/超出保留窗口也会长这样"
            ),
        )

    async def _scan_history(self, query: ReceiptQuery) -> _HistoryScan:
        """在窗口历史里找"自己发的、同文的、时间窗内"的消息。"""
        window_ref = query.window_ref or ""
        digest = query.text_digest or ""
        if not window_ref or not digest:
            # 拿不到会话/正文就没法比对：当作"没有证据"，不当作否证。
            return _HistoryScan(ok=False)
        sent_at = _aware(query.sent_at) or _now()
        since = sent_at - timedelta(seconds=HISTORY_MATCH_BEFORE_SECONDS)
        until = sent_at + timedelta(seconds=HISTORY_MATCH_AFTER_SECONDS)
        is_group = ":group:" in window_ref
        action = "get_group_msg_history" if is_group else "get_friend_msg_history"
        target_key = "group_id" if is_group else "user_id"
        try:
            data = await self._call(
                action,
                {target_key: self._target_id(window_ref), "count": HISTORY_LOOKBACK},
            )
        except ChannelError as exc:
            logger.debug("历史查询失败（对账保持 uncertain）：%s", exc.message)
            return _HistoryScan(ok=False)
        if isinstance(data, dict):
            rows = data.get("messages") or data.get("message") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list):
            return _HistoryScan(ok=False)
        self_id = str(self._config.self_id or "")
        hits: list[str] = []
        saw_self = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender = row.get("sender") or {}
            sender_id = str((sender or {}).get("user_id") or "")
            if not self_id or sender_id != self_id:
                continue
            saw_self = True
            parsed = parse_message(row.get("message"), self_id=self_id)
            if delivery_text_digest(parsed.text) != digest:
                continue
            at = _from_ts(row.get("time"))
            if at is not None and not (since <= at <= until):
                continue
            mid = row.get("message_id")
            if mid is not None:
                hits.append(str(mid))
        if len(hits) == 1:
            return _HistoryScan(ok=True, matched_id=hits[0], saw_self=True)
        if len(hits) > 1:
            return _HistoryScan(ok=True, ambiguous=True, saw_self=True)
        return _HistoryScan(ok=True, saw_self=saw_self)

    def _match_echo(self, query: ReceiptQuery) -> str | None:
        """用同进程收到的 ``message_sent`` 回显给这一段补 id。

        只在这个连接器实例**自己发过**那一段时才用（`_unidentified`），
        所以不会把别人发的消息当成自己的。
        """
        seen = self._unidentified.get(str(query.operation_id))
        if seen is None:
            return None
        window_ref, text, at = seen
        wanted = delivery_text_digest(text)
        since = at - timedelta(seconds=HISTORY_MATCH_BEFORE_SECONDS)
        until = at + timedelta(seconds=HISTORY_MATCH_AFTER_SECONDS)
        target = self._target_id(window_ref)
        hits = [
            message_id
            for observed, echo_target, message_id, echo_text in self._echoes
            if echo_target == target
            and delivery_text_digest(echo_text) == wanted
            and since <= observed <= until
        ]
        # 唯一命中才算证据（§5 P0-2 风险：同一句话连发两遍会误命中）
        return hits[0] if len(hits) == 1 else None

    def _receipt(
        self,
        query: ReceiptQuery,
        state: DeliveryState,
        *,
        platform_message_id: str | None = None,
        detail: str | None = None,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=state,
            platform_message_id=platform_message_id or query.platform_message_id,
            observed_at=_now(),
            detail=detail,
        )

    def platform_message_id_for_event(self, external_event_id: str) -> str | None:
        """外部事件 id → 平台 message_id（给上下文注入 `[msg_id:…]` 用）。

        只有连接器知道这个映射：OneBot 的 `external_event_id` 是
        ``f"{self_id}:{message_id}"``，别的渠道不是这个拼法。所以内核不该自己
        拆字符串，而是问连接器——不支持就返回 ``None``，上下文里就不带 id。
        """
        return self._event_message_ids.get(str(external_event_id or ""))

    def _remember_message_id(self, message_id: str, external_event_id: str) -> None:
        """记下一条见过的入站 message_id（引用白名单 + 上下文 id）。"""
        if message_id not in self._known_message_set:
            self._known_message_ids.append(message_id)
            # 白名单直接由环形缓冲重建：`deque(maxlen=…)` 自动淘汰最旧的，
            # 重建一次 512 个元素的集合比维护"谁被淘汰了"的账更不容易错。
            self._known_message_set = set(self._known_message_ids)
        if len(self._event_message_ids) >= KNOWN_MESSAGE_ID_LIMIT * 4:
            # 事件 id 表只服务"当前这一轮"的上下文注入，可以有界地丢最旧的。
            for key in list(self._event_message_ids)[: KNOWN_MESSAGE_ID_LIMIT]:
                self._event_message_ids.pop(key, None)
        self._event_message_ids[str(external_event_id)] = message_id

    def _record_echo(self, payload: dict) -> None:
        """把一条 ``message_sent`` 回显记进有界环形缓冲。

        只记**目标 + 正文 + message_id + 观察时间**：对账靠这四项做
        "自己发的、同文的、时间窗内的唯一命中"。
        """
        message_id = payload.get("message_id")
        if message_id is None:
            return
        # 记账失败**不能**让这条回显变成错误：`_record_echo` 在
        # `normalize_inbound` 里、被抛 ``ignored`` 之前调用，异常一穿出去，
        # ``process_inbound_payload`` 只会把 ChannelError/ValueError 收成
        # ``accepted=False``，其它异常会一路冒到上报路由 → 平台按"投递失败"
        # 重投同一条回显。可以吞的理由：这只是一个对账用的**加速证据**
        # （`reportSelfMessage` 默认关闭），丢了它并不影响送达判定——送达仍由
        # `send()` 回执与 `get_msg` 决定。
        try:
            target = str(
                payload.get("target_id")
                or payload.get("group_id")
                or payload.get("user_id")
                or ""
            )
            parsed = parse_message(payload.get("message"), self_id=self._config.self_id)
            self._echoes.append((_now(), target, str(message_id), parsed.text))
        except Exception as exc:  # noqa: BLE001 - 辅助记账失败不改变事件语义
            logger.debug("回显记账失败（不影响对账，只少一条加速证据）：%s", type(exc).__name__)


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
        # ---- NapCat 的实际错误码（`OneBotAction.ts`，见调研 §4.5）----
        #
        # | retcode | 含义                          | 分类      |
        # |---------|-------------------------------|-----------|
        # | 400/1400| 参数 schema 校验失败（没发）    | rejected  |
        # | 1404    | 不支持的 API                   | unsupported |
        # | 1200/200| **动作内部抛异常**（可能是发送超时、也可能"消息体无法解析"）| uncertain |
        #
        # 关键：**1200/200 把"发送前失败"和"发送中超时"混成同一个码**，
        # 只能保守按"可能已发出"处理——记 uncertain 进对账，**绝不重发**
        # （重发就是重复消息）。原实现把它们都归成 ``rejected``，
        # 而 ``rejected`` 会触发"去掉装饰重发"，等于把超时当"肯定没发出"。
        #
        # 另：全仓检索 NapCat 的 ``OB11Response.error(...)`` 只出
        # 200/400/1200/1400/1404，**没有 100**。所以线上若真见到
        # ``retcode=100``，那一定来自别的实现（如 Lagrange），按兜底的
        # ``rejected`` 处理是安全的；但别拿它当"引用失效"的证据——NapCat 对
        # 无效 reply 是**静默丢掉该段、正文照发**（§4.4），根本不会报错。
        if retcode in {1200, 200}:
            raise ChannelError(
                ChannelErrorKind.uncertain,
                detail_or(f"OneBot 动作内部异常 retcode={retcode}（可能已发送）", body),
            )
        if retcode == 1404:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                detail_or("OneBot 实现不支持该 API retcode=1404", body),
                retriable=False,
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
            #
            # 但**要把 id 记下来**（调研 §4.3 / §5 P0-2）：回显里的
            # ``message_id`` 与 ``send_*_msg`` 回执的 ``message_id`` 是
            # 同一个 id 空间，所以它能给"发出去了但平台没给 id"的那一段补 id。
            # 只在同进程有效（worker 是另一个进程），所以它只是对账的
            # **加速证据**，不是必需依赖——`reportSelfMessage` 默认是关的。
            self._record_echo(payload)
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
        # 这条入站消息的**真实平台 id**有两个用处：
        # 1. 出站引用白名单（引用一个没见过的 id，NapCat 只会静默丢段，§4.4）；
        # 2. 上下文里给模型看 `[msg_id:…]`（§5 P0-4）。
        if message_id:
            self._remember_message_id(message_id, external_event_id)

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


def _aware(value):  # noqa: ANN001, ANN202
    """把可能 naive 的时间统一成带时区的 UTC。拿不到就返回 None。"""
    from datetime import UTC, datetime

    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _looks_missing(message: str) -> bool:
    """平台的话里有没有"这条消息不存在"。"""
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _MISSING_HINTS)


class _HistoryScan:
    """一次历史内容比对的结果。

    ``ok`` 只是"查询成功"，**不等于**"没有命中"：
    - ``matched_id`` 唯一命中 → 可以补 id、记 delivered
    - ``ambiguous`` 命中多条 → 只能保持 uncertain（同一句话连发两遍）
    - ``ok=False`` → 查询不可用，**不能**当成否证
    - ``saw_self=False`` → 这份历史里**一条自己的消息都没有**。调研 §7.6 没
      证实"平台历史是否包含机器人自己发的消息"，所以"没找到同文消息"在这种
      情况下不构成否证（否则平台行为差异会被误判成"没送达"）。
    """

    __slots__ = ("ambiguous", "matched_id", "ok", "saw_self")

    def __init__(
        self,
        *,
        ok: bool,
        matched_id: str | None = None,
        ambiguous: bool = False,
        saw_self: bool = False,
    ) -> None:
        self.ok = ok
        self.matched_id = matched_id
        self.ambiguous = ambiguous
        self.saw_self = saw_self


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
