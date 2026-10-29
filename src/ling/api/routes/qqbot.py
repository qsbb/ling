"""QQ 官方机器人（QQ 开放平台 / QQ Bot）入站 Webhook 路由。

在开放平台「机器人 → 开发设置 → 事件订阅」里把回调地址填成
``POST /v1/qqbot/events``，本路由负责：验签 → （回调地址校验）→
交给统一入站受理（``ling.kernel.inbound_intake``）。

## 为什么走 Webhook 而不是 WebSocket 网关

网关要自己维护心跳、分片、重连与 session 续传，而这些与"能不能收到消息"
无关；Webhook 只需要一个 HTTPS 入口 + Ed25519 验签。先用简单的一条路把
闭环跑通，网关模式以后再加，不必现在把两套连接状态都实现一遍。

## 为什么业务失败也返回 200

QQ 官方对非 200 会重投（与 OneBot 同理）。"这条我们不处理"不是服务故障，
用 200 + ``accepted:false`` 表达，理由写在响应体里便于排查。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode

logger = logging.getLogger("ling.api.qqbot")

router = APIRouter(prefix="/v1/qqbot", tags=["qqbot"])


def _connector(request: Request):  # noqa: ANN202
    """按需构造连接器（缓存在 app.state，避免每次请求重建）。

    **必须缓存**：被动回复窗口（哪条消息还能回、回了几次）在连接器实例上，
    每次请求新建一个实例等于把窗口全部忘掉，所有回复都会退化成主动推送
    而被拒绝。
    """
    existing = getattr(request.app.state, "qqbot_connector", None)
    if existing is not None:
        return existing
    from ling.capabilities.channels import build_qqbot_connector

    connector = build_qqbot_connector(getattr(request.app.state, "settings", None))
    request.app.state.qqbot_connector = connector
    return connector


@router.post("/events")
async def qqbot_events(request: Request) -> JSONResponse:
    """接收 QQ 官方机器人事件回调。

    - 验签失败 → 403（来源不可信，不该被当成正常事件吞掉）
    - ``op=13`` 回调地址校验 → 回签名（这是平台确认"你是你"的握手）
    - 其余 → 统一受理入口
    """
    body = await request.body()
    connector = _connector(request)
    headers = {k: v for k, v in request.headers.items()}

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LingError(
            ErrorCode.invalid_request, "回调正文不是合法 JSON", status_code=400
        ) from exc
    if not isinstance(payload, dict):
        raise LingError(ErrorCode.invalid_request, "回调正文必须是对象", status_code=400)

    verified = await connector.verify_inbound(headers, body)
    op = payload.get("op")
    is_validation = str(op) == "13"
    # 唯一允许的例外：平台在配置回调地址时可能不带签名头（**未核实**），
    # 且这个例外只对 op=13 生效、且必须显式配置才打开。
    if not verified and is_validation and connector.config.allow_unsigned_validation:
        # 逃生开关被真正用到了。**必须留痕**：这是唯一一条不验签就能进
        # 对话链路的路径，运维如果忘了关掉它，从日志里要能看出来。
        logger.warning(
            "QQ 机器人回调**未验签放行**（op=13，LING_QQBOT_ALLOW_UNSIGNED_VALIDATION=1）："
            "确认回调地址后应尽量关掉"
        )
    if not verified and not (is_validation and connector.config.allow_unsigned_validation):
        logger.warning("QQ 机器人回调验签失败（op=%s）", op)
        raise LingError(ErrorCode.forbidden, "QQ 机器人回调验签失败", status_code=403)

    if is_validation:
        raw_data = payload.get("d")
        data: dict = raw_data if isinstance(raw_data, dict) else {}
        plain_token = str(data.get("plain_token") or "")
        event_ts = str(data.get("event_ts") or "")
        if not plain_token or not event_ts:
            raise LingError(
                ErrorCode.invalid_request, "回调校验请求缺少 plain_token/event_ts", status_code=400
            )
        try:
            return JSONResponse(connector.sign_url_validation(plain_token, event_ts))
        except Exception as exc:  # noqa: BLE001 - 缺密钥时明确报错，不静默
            logger.warning("回调地址校验失败：%s", type(exc).__name__)
            raise LingError(
                ErrorCode.forbidden,
                "无法完成回调地址校验：未配置 bot_secret",
                status_code=403,
            ) from exc

    from ling.kernel.inbound_intake import process_inbound_payload

    return JSONResponse(await process_inbound_payload(request.app, connector, payload))


@router.get("/health")
async def qqbot_health(request: Request) -> dict:
    """探测凭据是否可用（换取 access_token）。

    只说**鉴权**通过：消息能否真的发出去取决于被动回复窗口与主动推送权限，
    这一点在 ``detail`` 里如实写明。
    """
    connector = _connector(request)
    ok, detail = await connector.health()
    from connectors.qqbot.connector import describe_config

    return {
        "ok": ok,
        "detail": detail,
        "instance": connector.instance_id,
        "config": describe_config(connector.config),
    }
