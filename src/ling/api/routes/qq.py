"""QQ webhook 接收端点（W1：已开发，未测试，默认不挂载）。

挂载条件：``LING_QQ_ENABLED=1`` 且配置了 webhook 公钥。未满足时端点不存在，
避免半成品入口被误当成已接通。

**未验证**：没有真实 QQ 账号权限、没有回调域名，因此从未与平台联调。
"""

from __future__ import annotations

import json

from connectors.qq.connector import QQConnector
from connectors.qq.ingress import QQIngress
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ling.config import Settings
from ling.infra.db import session_scope

router = APIRouter(tags=["qq"], include_in_schema=False)


def _build_connector(request: Request) -> QQConnector | None:
    settings: Settings = request.app.state.settings
    qq = getattr(settings, "qq", None)
    if qq is None:
        return None
    return QQConnector(qq)


@router.post("/webhooks/qq")
async def qq_webhook(request: Request) -> Response:
    """QQ 回调入口。

    - op=13（回调地址校验）：按官方要求回 op=12 + plain_token + 签名。
    - 其它 op：交给 QQIngress（签名→解析→去重→身份）。
    """
    settings: Settings = request.app.state.settings
    if not getattr(settings, "qq_enabled", False):
        return JSONResponse({"detail": "qq connector disabled"}, status_code=404)

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    connector = _build_connector(request)
    if connector is None:
        return JSONResponse({"detail": "qq connector not configured"}, status_code=503)

    if not await connector.verify_inbound(headers, body):
        return JSONResponse({"detail": "signature verification failed"}, status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"detail": "invalid json"}, status_code=400)

    if int(payload.get("op", 0)) == 13:
        # 回调地址校验：必须用 Ed25519 私钥对 plain_token 签名
        plain_token = str((payload.get("d") or {}).get("plain_token", ""))
        event_ts = str((payload.get("d") or {}).get("event_ts", ""))
        signature = _sign_validation(settings, event_ts, plain_token)
        if signature is None:
            return JSONResponse({"detail": "validation key missing"}, status_code=503)
        return JSONResponse({"plain_token": plain_token, "signature": signature})

    async with session_scope() as session:
        ingress = QQIngress(session, connector)
        result = await ingress.handle(headers, body)

    if not result.accepted:
        return JSONResponse({"detail": result.reason or "rejected"}, status_code=400)
    if result.duplicate:
        # 平台会重试；返回 200 让它停止重试
        return JSONResponse({"ok": True, "duplicate": True})
    return JSONResponse(
        {
            "ok": True,
            "event_id": result.envelope.external_event_id if result.envelope else None,
            "subject_bound": result.subject_id is not None,
        }
    )


def _sign_validation(settings: Settings, event_ts: str, plain_token: str) -> str | None:
    """回显签名。需要 webhook 私钥（由开放平台下发），未配置时返回 None。"""
    qq = getattr(settings, "qq", None)
    if qq is None or not getattr(qq, "webhook_private_key", None):
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(qq.webhook_private_key))
    except (ValueError, TypeError):
        return None
    return key.sign(f"{event_ts}{plain_token}".encode()).hex()


__all__ = ["router"]
