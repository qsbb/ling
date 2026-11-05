"""健康检查：存活、就绪（数据库 + 配置修订）、模型健康摘要。

## 为什么 `/readyz` 不加鉴权，而是按"是否已登录"给不同详细度

容器的 healthcheck 用 ``python -c`` + ``urllib`` 探 ``/readyz``，且**只看 HTTP
状态码**（见 ``deploy/compose.yaml`` 的 api healthcheck）。一旦给它加
``CurrentAuth``，容器会永远 unhealthy、部署直接失败（安装器也要等它）。

所以这里保持"未鉴权也能拿到状态"，但**未鉴权只给状态**：
``backend``（用的什么数据库）、``config_revision``、``test_mode``、
``profiles``（绑了哪些职责）与异常原文一律不外泄——这些是部署指纹，
探针不需要它们。已鉴权（且不是首次安装未改密）才返回完整详情。

``/healthz`` 是存活探针，**永远**只回 ``status``/``service`` 两个固定字段。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ling.api.deps import CurrentAuth
from ling.config import Settings

logger = logging.getLogger("ling.api.health")

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针：进程能应答即活。**不含任何部署细节**（它不鉴权、最常被打）。"""
    return {"status": "alive", "service": "ling-api"}


async def _has_valid_session(request: Request) -> bool:
    """就绪检查用的**可选**鉴权：拿不到 / 过期 / 未改密一律按未登录处理。

    为什么不直接用 ``CurrentAuth``：它抛 401，而 ``/readyz`` 必须对容器
    healthcheck 保持可用。也不把校验包在依赖里——数据库不可用时连"这是不是
    已登录用户"都判断不了，而就绪检查恰恰要处理那种情况，所以整个校验包在
    ``try`` 里，失败就按未登录降级（就绪检查自己会另报原因）。
    """
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.cookie_name) or ""
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        return False
    try:
        from ling.domains.identity.service import IdentityService
        from ling.infra.db import session_scope

        async with session_scope() as session:
            user = await IdentityService(session).resolve_token(token)
    except Exception:  # noqa: BLE001 - 库不可用 ⇒ 按未登录；503 会说明原因
        return False
    if user is None:
        return False
    # 首次安装的随机口令还没改：初始化未完成，同样不给部署细节。
    # 判据与 `deps.current_auth` 的那道闸一致，避免两条路径各自漂移。
    return not user.must_change_password


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    from ling.app import ready_check

    authenticated = await _has_valid_session(request)
    try:
        detail = await ready_check(settings)
    except Exception as exc:  # noqa: BLE001 - 就绪检查必须给出原因
        # 原因写进**服务端日志**（运维要能查），但未鉴权调用方只拿到状态：
        # 异常里常有 DSN、主机名、驱动细节，甚至口令片段。
        logger.warning("readyz 未就绪：%s", exc)
        body: dict[str, object] = {"status": "not-ready"}
        if authenticated:
            body["detail"] = str(exc)[:300]
        return JSONResponse(status_code=503, content=body)
    if not authenticated:
        return JSONResponse(status_code=200, content={"status": "ready"})
    return JSONResponse(status_code=200, content={"status": "ready", **detail})


@router.get("/v1/health/model")
async def model_health(request: Request, auth: CurrentAuth) -> dict[str, object]:
    """各职责 profile 的熔断摘要。含 ``env`` 与职责绑定，必须鉴权。"""
    settings: Settings = request.app.state.settings
    from ling.api.deps import config_service
    from ling.infra.db import session_scope

    async with session_scope() as session:
        svc = config_service(request, session)
        effective = await svc.effective()
    router_ = request.app.state.router
    degraded = [
        name
        for name, binding in effective.profiles.items()
        if router_.health_snapshot().get(f"{binding.provider_id}/{binding.model_id}", {}).get("open")
    ]
    return {
        "revision": effective.revision,
        "test_mode": effective.test_mode,
        "profiles": sorted(effective.profiles),
        "degraded": degraded,
        "status": "degraded" if degraded else "ok",
        "env": settings.env,
    }
