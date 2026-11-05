"""QQ 官方机器人的 WebSocket 网关（出站连接到腾讯，**不需要公网地址**）。

## 为什么必须有这个模式

官方机器人有两种收事件的方式：Webhook 与 WebSocket。

- **Webhook** 要求腾讯能访问到一个**公网 HTTPS** 回调地址。ling 通常跑在
  家里/NAS 的内网（没有公网 IP、域名、证书），这条路根本走不通。
- **WebSocket** 是 ling 主动连出去（``wss://api.bot.qq.com/websocket/``），
  不需要任何入站端口、不需要公网、更不需要扫码。

而且官方文档明确写着"发送消息接口要求机器人接口需要连接到 WebSocket 上
保持在线状态"——也就是说**只做 Webhook 连消息都发不出去**。

## 协议（照官方文档 api-v2「WebSocket 方式」实现）

1. ``GET /gateway`` 拿网关地址（返回 ``{"url": "wss://..."}``）
2. 连接后先收到 ``op=10 Hello``，里面是 ``heartbeat_interval``（毫秒）
3. 发 ``op=2 Identify``：``{token: "QQBot <access_token>", intents, shard}``
4. 收到 ``op=0 t=READY``，带 ``session_id``；此后每帧的 ``s`` 是序号
5. 按周期发 ``op=1`` 心跳（``d`` 为最近一次的 ``s``，首次为 null），
   平台回 ``op=11``
6. 断线重连带 ``op=6 Resume``（``session_id`` + ``seq``）补发漏掉的事件；
   补完收到 ``t=RESUMED``

## 关闭码策略（官方表格）

- ``4007`` seq 错误 / ``4006`` session 无效 → 重新 Identify
- ``4008`` 发太快 / ``4009`` 连接过期 → 可以 Resume
- ``4914`` 只允许沙箱 / ``4915`` 已封禁 → **停止**，重连没有意义
- ``4900~4913`` 内部错误 → 重新 Identify

## 未核实

- 网关地址里的路径（``/websocket/`` 末尾斜杠）以 ``/gateway`` 返回值为准，
  这里不拼路径，直接用平台给的 URL。
- ``heartbeat_interval`` 的具体值（文档示例 45000ms），代码按收到的用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("ling.connectors.qqbot.gateway")

from ling.logging_setup import emit_event  # noqa: E402 - 避免与上面 logger 定义顺序冲突

# 群与单聊消息事件都在这一位里：C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE
# （官方文档「事件订阅 Intents」：GROUP_AND_C2C_EVENT (1 << 25)）
INTENTS_GROUP_AND_C2C = 1 << 25

# 重连退避
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0

# 关闭码 → 下一步动作
ACTION_IDENTIFY = "identify"
ACTION_RESUME = "resume"
ACTION_STOP = "stop"


def action_for_close(code: int | None) -> str:
    """关闭码对应的恢复动作。**未知码按 Identify 处理**。

    为什么不按 Resume：Resume 依赖 session 仍然有效，session 已失效时
    Resume 会立刻再断，形成紧密的重连循环。Identify 是更安全的默认。
    """
    if code in (4008, 4009):
        return ACTION_RESUME
    if code in (4914, 4915):
        # 只允许沙箱 / 已封禁：重连没有意义，只会刷屏
        return ACTION_STOP
    return ACTION_IDENTIFY


@dataclass(frozen=True)
class GatewayState:
    """跨连接保留的状态：断线重连要靠它补事件。"""

    session_id: str = ""
    last_seq: int | None = None

    @property
    def can_resume(self) -> bool:
        return bool(self.session_id) and self.last_seq is not None


def build_identify(token: str, *, intents: int, shard: tuple[int, int]) -> dict:
    return {
        "op": 2,
        "d": {
            "token": f"QQBot {token}",
            "intents": intents,
            "shard": [shard[0], shard[1]],
            "properties": {"$os": "linux", "$browser": "ling", "$device": "ling"},
        },
    }


def build_resume(token: str, state: GatewayState) -> dict:
    return {
        "op": 6,
        "d": {
            "token": f"QQBot {token}",
            "session_id": state.session_id,
            "seq": state.last_seq,
        },
    }


def build_heartbeat(seq: int | None) -> dict:
    # 首帧心跳 d 传 null（文档明确写了）
    return {"op": 1, "d": seq}


class QQBotGateway:
    """维持到 QQ 网关的长连接，把事件交给统一受理入口。"""

    def __init__(
        self,
        *,
        connector: Any,
        app: Any,
        intents: int = INTENTS_GROUP_AND_C2C,
        shard: tuple[int, int] = (0, 1),
        reconnect_min: float = RECONNECT_MIN,
        reconnect_max: float = RECONNECT_MAX,
    ) -> None:
        self._connector = connector
        self._app = app
        self.intents = intents
        self.shard = shard
        self._min = reconnect_min
        self._max = reconnect_max
        self.state = GatewayState()
        self.connected = False
        self.received = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        # Hello 帧给的心跳周期（毫秒）。读不到时才用文档示例值兜底。
        self._hello_interval_ms: float = 0.0

    # ------------------------------------------------------------ 单帧处理

    async def handle_frame(self, raw: str | bytes) -> dict | None:
        """处理一帧。返回事件受理结果（非事件帧返回 None）。

        心跳/Hello/READY/Resumed 这些**协议帧不进对话链路**，但必须在这里
        消费掉并更新状态——否则序号断档，重连时会重复补发或漏事件。
        """
        try:
            frame = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("QQ 网关帧不是合法 JSON，已丢弃: %s", type(exc).__name__)
            return None
        if not isinstance(frame, dict):
            logger.warning("QQ 网关帧不是对象，已丢弃")
            return None

        op = frame.get("op")
        seq = frame.get("s")
        if isinstance(seq, int):
            self.state = GatewayState(session_id=self.state.session_id, last_seq=seq)

        if op == 0:
            event = str(frame.get("t") or "")
            if event == "READY":
                data = frame.get("d") or {}
                self.state = GatewayState(
                    session_id=str(data.get("session_id") or ""), last_seq=seq
                )
                emit_event(
                    logger,
                    logging.INFO,
                    event_name="connector_connected",
                    message="QQ 网关已就绪",
                    attributes={
                        "channel": "qqbot",
                        "connection_state": "connected",
                        # session 短哈希就够定位，不落完整值
                        "session_prefix": (self.state.session_id or "")[:8],
                    },
                )
                return None
            if event == "RESUMED":
                emit_event(
                    logger,
                    logging.INFO,
                    event_name="connector_resumed",
                    message="QQ 网关会话已恢复（补发完成）",
                    attributes={"channel": "qqbot", "last_seq": self.state.last_seq},
                )
                return None

            # 真正的事件：交给统一受理（与 Webhook 完全同一条路径）
            from ling.kernel.inbound_intake import process_inbound_payload

            self.received += 1
            try:
                result = await process_inbound_payload(self._app, self._connector, frame)
            except Exception as exc:  # noqa: BLE001 - 单条失败不能断连接
                logger.exception("QQ 网关事件处理失败（连接保持）")
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                return {"accepted": False, "reason": "处理失败"}
            logger.info(
                "QQ 网关入站：%s → %s（%s）",
                event,
                result.get("accepted"),
                str(result.get("reason") or result.get("decision") or "")[:60],
            )
            return result

        if op == 10:
            # Hello：心跳周期由平台给。**必须存下来**，否则心跳会一直用兜底值，
            # 平台要求更短的周期时会被判超时断连。
            data = frame.get("d") or {}
            interval = float(data.get("heartbeat_interval") or 0)
            if interval > 0:
                self._hello_interval_ms = interval
            emit_event(
                logger,
                logging.INFO,
                event_name="connector_hello",
                message="QQ 网关握手完成",
                attributes={
                    "channel": "qqbot",
                    "heartbeat_interval_ms": interval,
                },
            )
            return None
        if op in (11, 7, 9):
            # 心跳 ACK / 服务器要求重连（7）/ 会话失效（9）
            return None

        logger.info("QQ 网关忽略未知 op=%s", op)
        return None

    # ------------------------------------------------------------ 连接循环

    async def _gateway_url(self) -> str:
        """向平台要网关地址。

        不自己拼 ``/websocket``：路径由平台返回，拼错了就是连不上还查不出原因。
        """
        data = await self._connector.api_get("/gateway")
        if not isinstance(data, dict) or not data.get("url"):
            raise RuntimeError(f"网关地址缺失: {data!r}"[:200])
        return str(data["url"])

    async def run(self) -> None:
        """永久运行：连上就收，断了按关闭码决定 Resume 还是 Identify。"""
        import websockets

        delay = self._min
        attempt = 0
        while not self._stopped:
            attempt += 1
            # 每次连接前先决定"这次是 Identify 还是 Resume"，连上就照做
            action = ACTION_RESUME if self.state.can_resume else ACTION_IDENTIFY
            try:
                url = await self._gateway_url()
                # **显式不走代理**（proxy=None）。
                #
                # websockets 默认会读系统代理配置（macOS 上是系统设置里的
                # SOCKS）。QQ 是境内服务，把它绕到境外代理上既慢又容易断；
                # 而且本项目没有装 python-socks，会直接
                # ImportError: connecting through a SOCKS proxy requires python-socks
                # ——报错长得像依赖缺失，实际是"被系统代理带走了"。
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, proxy=None
                ) as ws:
                    self.connected = True
                    delay = self._min
                    token = await self._connector.access_token()
                    if action == ACTION_RESUME:
                        await ws.send(json.dumps(build_resume(token, self.state)))
                        logger.info("QQ 网关重连中，尝试恢复会话 seq=%s", self.state.last_seq)
                    else:
                        # 重新 Identify = 开新会话，旧序号不再适用
                        self.state = GatewayState()
                        await ws.send(
                            json.dumps(
                                build_identify(
                                    token, intents=self.intents, shard=self.shard
                                )
                            )
                        )
                    heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        async for raw in ws:
                            await self.handle_frame(raw)
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await heartbeat
            except asyncio.CancelledError:
                self.connected = False
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                # 关闭码决定还能不能继续：见 action_for_close 的说明
                self.connected = False
                decision = action_for_close(getattr(exc, "code", None))
                self.last_error = f"closed code={getattr(exc, 'code', None)}"
                code = getattr(exc, "code", None)
                # 状态机事件：连接关闭 / 计划重连 / 永久停止，三种分开记
                emit_event(
                    logger,
                    logging.ERROR if decision == ACTION_STOP else logging.WARNING,
                    event_name=(
                        "connector_retry_exhausted"
                        if decision == ACTION_STOP
                        else "connector_disconnected"
                    ),
                    message="QQ 网关连接关闭",
                    attributes={
                        "channel": "qqbot",
                        "close_code": code,
                        "next_action": decision,
                        "connection_state": "disconnected",
                    },
                )
                if decision == ACTION_STOP:
                    self._stopped = True
                elif decision == ACTION_IDENTIFY:
                    # 会新开会话，旧 session 作废
                    self.state = GatewayState()
            except Exception as exc:  # noqa: BLE001 - 任何异常都重连
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                emit_event(
                    logger,
                    logging.WARNING,
                    event_name="connector_reconnect_failed",
                    message="QQ 网关连接失败，将退避重试",
                    attributes={
                        "channel": "qqbot",
                        "error_type": type(exc).__name__,
                        "attempt": attempt,
                        "backoff_s": delay,
                        "retriable": True,
                    },
                )
            finally:
                self.connected = False

            if self._stopped:
                if self.last_error:
                    logger.error("QQ 网关已停止：%s（重连无意义，请检查机器人状态）", self.last_error)
                return
            await asyncio.sleep(delay)
            delay = min(self._max, delay * 2)

    async def _heartbeat_loop(self, ws: Any) -> None:
        """按平台给的周期发心跳。

        周期从 Hello 帧里读；读不到就按 45 秒（文档示例值）兜底，
        而不是"不发心跳"——不发的话连接会被平台判死。
        """
        interval_ms = float(getattr(self, "_hello_interval_ms", 0) or 45000)
        while True:
            await asyncio.sleep(max(interval_ms / 1000.0, 1.0))
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(build_heartbeat(self.state.last_seq)))

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self.run(), name="qqbot-gateway")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "session": bool(self.state.session_id),
            "last_seq": self.state.last_seq,
            "received": self.received,
            "intents": self.intents,
            "shard": list(self.shard),
            "last_error": self.last_error,
        }


__all__ = [
    "ACTION_IDENTIFY",
    "ACTION_RESUME",
    "ACTION_STOP",
    "INTENTS_GROUP_AND_C2C",
    "QQBotGateway",
    "GatewayState",
    "action_for_close",
    "build_heartbeat",
    "build_identify",
    "build_resume",
]
