"""OneBot 反向 WebSocket 入站客户端。

## 它解决什么问题

HTTP 上报（`httpClients`）要求 **NapCat 能访问到 ling**：ling 必须绑
`0.0.0.0`，把整个 API 暴露在局域网上，而且换网络环境就断。

WebSocket 反过来：**ling 主动连 NapCat** 的 wsServer。于是

- 本机不需要开放任何入站端口（只连出去）
- 断线能自动重连，不需要人管
- 换机器只改一个 URL

## 与 HTTP 上报共用同一套受理

收到 payload 之后一律交给 `ling.kernel.inbound_intake.process_inbound_payload`，
白名单、去重、身份解析、决策、投递只有一份实现。

## 为什么不在这里验签

`verify_inbound` 校验 `X-Self-ID` 与 HMAC，为的是确认"这条 HTTP 请求确实
来自我们配的实例"。WS 是**我们主动连出去**的：对端由连接目标确定，没有
"别人往我端口乱发"的风险面。所以不重复做头部校验——但白名单照样生效。

## 一个已知限制

发送仍然走 HTTP API（`LING_ONEBOT_API_BASE`）。也就是说 WS 只承载入站。
OneBot 也允许在 WS 上发 action 并收 echo 回执，但那需要重做投递层的
调用方式；当前 HTTP API 已经能拿到真实回执（`message_id`），没必要动。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

logger = logging.getLogger("ling.connectors.onebot.ws")

# 断线重连的退避：从 2 秒起翻倍，封顶 60 秒。
# 不固定 5 秒的原因是 NapCat 重启时会在几十秒内反复拒绝连接，
# 固定间隔会把日志刷满。
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0


class OneBotWsClient:
    """连到 NapCat 的 wsServer，把收到的事件交给统一受理入口。"""

    def __init__(
        self,
        *,
        url: str,
        app: object,
        connector: object,
        reconnect_min: float = RECONNECT_MIN,
        reconnect_max: float = RECONNECT_MAX,
    ) -> None:
        self.url = url
        self._app = app
        self._connector = connector
        # 机器人自己的 QQ 号。OneBot 反向 WS 的握手头，缺了它 NapCat 会把这个
        # 连接当成"只调 API、不收事件"的客户端——**消息事件一条都不会推过来**，
        # 只有心跳。实测：不带 X-Client-Role 时只收到 meta_event。
        cfg = getattr(connector, "_config", None)
        self.self_id = str(getattr(cfg, "self_id", "") or "")
        self._min = reconnect_min
        self._max = reconnect_max
        self.connected = False
        self.received = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ 单条消息

    async def handle_raw(self, raw: str | bytes) -> dict | None:
        """解析一条原始帧并处理。返回受理结果（非事件帧返回 None）。

        单独抽出来是为了能测：不需要真开 socket 就能验证"坏 JSON 不炸、
        事件走统一入口"。
        """
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("WS 帧不是合法 JSON，已丢弃: %s", type(exc).__name__)
            return None
        if not isinstance(payload, dict):
            logger.warning("WS 帧不是对象，已丢弃")
            return None

        from ling.kernel.inbound_intake import process_inbound_payload

        self.received += 1
        try:
            return await process_inbound_payload(self._app, self._connector, payload)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能断连接
            # 一条消息处理失败就断线重连的话，同一条坏消息会把连接打成死循环。
            logger.exception("WS 入站处理失败（连接保持）")
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"accepted": False, "reason": "处理失败"}

    # ------------------------------------------------------------ 连接循环

    async def run(self) -> None:
        """永久运行：连上就收，断了就退避重连。取消时干净退出。"""
        import websockets

        delay = self._min
        while True:
            try:
                headers = {"X-Client-Role": "Universal"}
                if self.self_id:
                    headers["X-Self-ID"] = self.self_id
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    additional_headers=headers,
                ) as ws:
                    self.connected = True
                    delay = self._min  # 连上就重置退避
                    logger.info(
                        "OneBot WS 已连接: %s（headers=%s）", self.url, sorted(headers)
                    )
                    async for raw in ws:
                        await self.handle_raw(raw)
            except asyncio.CancelledError:
                self.connected = False
                logger.info("OneBot WS 客户端已停止")
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都重连
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                logger.warning(
                    "OneBot WS 断开（%s），%.0f 秒后重连", self.last_error, delay
                )
            await asyncio.sleep(delay)
            delay = min(self._max, delay * 2)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="onebot-ws")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            # 停机路径不抛：任务可能已因连接错误结束，也可能刚好在处理消息。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def status(self) -> dict:
        return {
            "url": self.url,
            "connected": self.connected,
            "received": self.received,
            "last_error": self.last_error,
        }


__all__ = ["RECONNECT_MAX", "RECONNECT_MIN", "OneBotWsClient"]
