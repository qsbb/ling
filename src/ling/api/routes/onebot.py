"""OneBot V11 入站路由。

OneBot 实现（NapCat / Lagrange / go-cqhttp）在配置里把
「事件上报地址（HTTP POST）」指到 ``POST /v1/onebot/events``，
本路由负责：验签 → 去重 → 归一化 → **跑完整一轮对话并回复**。

为什么这个路由是必要的：之前 QQ 连接器只做到"收下事件"，没有接进
轮次流水线，所以连接器能收消息但用户在 QQ 里聊不了。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode

logger = logging.getLogger("ling.api.onebot")

router = APIRouter(prefix="/v1/onebot", tags=["onebot"])


def _config():  # noqa: ANN202
    # 与 worker 共用同一份解析，避免两边漂移
    from ling.capabilities.channels import onebot_config

    return onebot_config()


def _connector(request: Request):  # noqa: ANN202
    """按需构造连接器（实例缓存在 app.state，避免每次请求重建）。"""
    existing = getattr(request.app.state, "onebot_connector", None)
    if existing is not None:
        return existing
    from connectors.onebot.connector import OneBotV11Connector

    connector = OneBotV11Connector(_config())
    request.app.state.onebot_connector = connector
    return connector


@router.post("/events")
async def onebot_events(request: Request) -> JSONResponse:
    """接收 OneBot 事件上报。

    返回 200 即表示"已受理"。OneBot 规范允许在响应体里放「快速操作」
    （如 ``{"reply": "..."}``）直接让平台回复；这里不用那条路径，
    因为回复要走投递层留 Operation 与回执，否则"发了没有"无法对账。

    注意：业务失败也返回 200（规范里只有鉴权/格式错误才用非 200），
    否则 OneBot 会反复重投同一条事件。
    """
    body = await request.body()
    connector = _connector(request)
    headers = {k: v for k, v in request.headers.items()}

    if not await connector.verify_inbound(headers, body):
        # 验签失败用 403：这是"来源不可信"，不该被当成正常事件吞掉
        logger.warning("OneBot 上报验签失败")
        raise LingError(ErrorCode.forbidden, "OneBot 上报验签失败", status_code=403)

    import json

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LingError(
            ErrorCode.invalid_request, "上报正文不是合法 JSON", status_code=400
        ) from exc
    if not isinstance(payload, dict):
        raise LingError(ErrorCode.invalid_request, "上报正文必须是对象", status_code=400)

    # 收到 payload 之后的一切都在 kernel/inbound_intake —— HTTP、WebSocket 与
    # QQ 官方机器人 Webhook 三条入站必须共用同一套（白名单、去重、身份、
    # 决策、投递），分成几份实现迟早有一条忘了某个检查，而且是静默的。
    from ling.kernel.inbound_intake import process_inbound_payload

    return JSONResponse(await process_inbound_payload(request.app, connector, payload))


@router.get("/health")
async def onebot_health(request: Request) -> dict:
    """探测 OneBot 实现是否可用（get_login_info）。"""
    connector = _connector(request)
    ok, detail = await connector.health()
    return {"ok": ok, "detail": detail, "instance": connector.instance_id}
