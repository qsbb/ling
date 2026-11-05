"""OneBot 反向 WebSocket 的**服务端**（NapCat 作为客户端连过来）。

## 为什么要有这一种模式

ling 先做的是「客户端」模式（ling 连 NapCat 的 wsServer）。实测在这个环境里
**收不到消息事件**——只收到心跳：

    OneBot WS 已连接（headers=['X-Client-Role','X-Self-ID']）
    忽略 OneBot 事件: 暂不处理 OneBot 事件类型: meta_event   ← 只有这个

用完全相同的连接参数（含 `X-Client-Role: Universal`）单独探针验证过：
握手成功、心跳正常、但群消息事件一条都不推。换连接参数、重启 NapCat、
清掉残留连接都试过，现象不变。

反方向（插件当服务端、NapCat 当客户端连过来）则工作正常：

    # NapCat 侧配置示意
    "httpClients": []            # 不用 HTTP 上报
    # 反向 WS 客户端指向 ling 的 ws://<ling-host>:6799

也就是「ling 当服务端，NapCat 当客户端连过来」。这个方向是实现中**验证过**
的方向，所以默认走它。

## 与客户端模式的关系

两种模式共用同一套受理（`ling.kernel.inbound_intake`），协议差异只在这层：
谁发起连接、谁验 token。

客户端模式保留（`LING_ONEBOT_WS_URL`），但**默认不用**——原因如上，
不是设计偏好，是实测不通。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

logger = logging.getLogger("ling.connectors.onebot.ws_server")


class OneBotWsAuthError(RuntimeError):
    """WS 服务端缺 token 等鉴权前置条件不满足，**拒绝启动**。

    单独一个异常类型，是为了让 ``ling/app.py`` 能把它和"端口被占用"
    这类启动失败区分开：前者是配置错误（必须改配置），后者可能只是
    另一个进程占着端口（重试或换端口即可）。
    """


class OneBotWsServer:
    """监听一个 WS 端口，把 NapCat 推来的事件交给统一受理入口。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        app: object,
        connector: object,
        token: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self._app = app
        self._connector = connector
        self.clients = 0
        self.received = 0
        self.last_error: str | None = None
        self._server: Any = None

    # ------------------------------------------------------------ 鉴权

    def _authorized(self, path: str, headers: Any) -> bool:
        """**没有 token 就一律拒绝**。

        NapCat 的 wsClient 把 token 放在 `Authorization: Bearer <token>`
        或 URL 的 `?access_token=` 里（两种实现都有）。两种都认。

        原实现在没配 token 时直接返回 True（只打一条 warning），等于
        "任何能连上这个端口的人都能往对话链路里推事件"。这里改成
        fail-closed：没 token 时连启动都不允许（见 ``start()``），
        即使有人绕过启动直接调这个方法，也一律拒绝。
        """
        if not self.token:
            return False
        auth = ""
        try:
            auth = str(headers.get("Authorization") or "")
        except Exception:  # noqa: BLE001 - 头拿不到就当没有
            auth = ""
        if auth.lower().startswith("bearer ") and auth[7:].strip() == self.token:
            return True
        if "access_token=" in (path or ""):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(path).query)
            if (query.get("access_token") or [""])[0] == self.token:
                return True
        return False

    # ------------------------------------------------------------ 单条消息

    async def handle_raw(self, raw: str | bytes) -> dict | None:
        """解析一条原始帧并处理。坏帧只丢一条，不断连接。"""
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
        post_type = payload.get("post_type")
        try:
            result = await process_inbound_payload(self._app, self._connector, payload)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能断连接
            logger.exception("WS 入站处理失败（连接保持）")
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"accepted": False, "reason": "处理失败"}

        # **成功路径也要留一行**。
        #
        # 这一行不是可有可无：之前"入站到底通没通"排查了很久，原因就是
        # 消息处理成功时**什么日志都没有**——我一直在 grep 一个不存在的行，
        # 于是把"已经通了"误判成"帧根本没到"。心跳（meta_event）刷屏的时候
        # 尤其容易看不出来。
        if post_type == "message":
            logger.info(
                "WS 入站消息：user=%s group=%s → %s（%s）",
                payload.get("user_id"),
                payload.get("group_id"),
                result.get("accepted"),
                str(result.get("reason") or result.get("decision") or "")[:60],
            )
        return result

    # ------------------------------------------------------------ 连接处理

    async def _handler(self, ws: Any) -> None:
        path = getattr(getattr(ws, "request", None), "path", "") or ""
        headers = getattr(getattr(ws, "request", None), "headers", {}) or {}
        if not self._authorized(path, headers):
            logger.warning("OneBot WS 服务端拒绝连接：token 不匹配")
            await ws.close(code=1008, reason="unauthorized")
            return

        self.clients += 1
        logger.info("NapCat 已连入 OneBot WS 服务端（当前 %d 个连接）", self.clients)
        try:
            async for raw in ws:
                await self.handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个连接出错不影响其它
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("OneBot WS 连接异常结束：%s", self.last_error)
        finally:
            self.clients = max(0, self.clients - 1)
            logger.info("NapCat 断开 OneBot WS 服务端（剩余 %d 个连接）", self.clients)

    # ------------------------------------------------------------ 启停

    async def start(self) -> None:
        """启动监听。**没有 token 直接抛错，不启动。**

        为什么是抛错而不是 warning 后放行：这个端口一旦开起来，推来的
        事件会直接进对话链路（白名单、身份、决策、投递都在同一套受理里）。
        没有 token 就等于把「以某个白名单 QQ 号说话」的能力交给任何
        能连上端口的人。warning 是会被忽略的——它是日志，不是门。
        """
        import websockets

        if not self.token:
            raise OneBotWsAuthError(
                f"OneBot WS 服务端缺少 token（{self.host}:{self.port}）："
                "拒绝启动。请在 OneBot 侧与 LING_ONEBOT_WS_TOKEN 配同一个值"
            )
        self._server = await websockets.serve(self._handler, self.host, self.port)
        logger.info("OneBot WS 服务端已监听 ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
            logger.info("OneBot WS 服务端已停止")

    def status(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "clients": self.clients,
            "received": self.received,
            "token_configured": bool(self.token),
            "last_error": self.last_error,
        }


__all__ = ["OneBotWsAuthError", "OneBotWsServer"]
