"""健康检查：存活、就绪（数据库 + 配置修订）、模型健康摘要。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ling.config import Settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "alive", "service": "ling-api"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    from ling.app import ready_check

    try:
        detail = await ready_check(settings)
    except Exception as exc:  # noqa: BLE001 - 就绪检查必须给出原因
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "detail": str(exc)[:300]},
        )
    return JSONResponse(status_code=200, content={"status": "ready", **detail})


@router.get("/v1/health/model")
async def model_health(request: Request) -> dict[str, object]:
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
