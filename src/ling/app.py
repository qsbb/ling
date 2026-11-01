"""应用装配与生命周期。

依赖装配集中在这里（设计文档 §12 的 ``app.py``）。路由模块只通过
``request.app.state`` 取依赖，不自行构造服务。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.middleware.base import BaseHTTPMiddleware, Response

from ling import __version__
from ling.api.errors import LingError, error_response, ling_error_handler
from ling.config import Settings, get_settings
from ling.contracts.v1 import ErrorCode
from ling.domains.identity.service import IdentityService
from ling.infra.coordination import SpeakLeaseStore, TurnControlStore
from ling.infra.db import create_all_for_dev, dispose_engine, init_engine, session_scope
from ling.infra.security import SecretBox
from ling.kernel.arbiter import SpeakerArbiter
from ling.kernel.decision import GroupBudgetRegistry
from ling.kernel.stream_log import StreamLog
from ling.kernel.turn import CancelRegistry
from ling.logging_setup import setup_logging
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.settings.service import ConfigService

# 轮次多久没更新判定为悬空（与 worker 保持一致）
DANGLING_TURN_MINUTES = 10

# **必须在模块导入时配置**：容器用 `uvicorn ling.app:app` 启动，
# 不走 ling/__main__.py，不在这里配就没有任何 ling 日志。
setup_logging(role="api")

logger = logging.getLogger("ling.app")


async def _recover_dangling_turns() -> None:
    """服务重启恢复：把**长时间无更新**的轮次修正为 failed。

    注意：不能无条件处理所有 pending/streaming。多副本部署时本实例重启
    不该误杀其他实例正在跑的轮次（此前实现的缺陷）。因此只处理
    updated_at 早于阈值的——活跃轮次每次 delta 都会更新它。
    """
    from datetime import timedelta

    from sqlalchemy import update

    from ling.infra.models import Turn, utcnow

    cutoff = utcnow() - timedelta(minutes=DANGLING_TURN_MINUTES)
    async with session_scope() as session:
        result = await session.execute(
            update(Turn)
            .where(
                Turn.state.in_(["pending", "streaming", "stopping"]),
                Turn.updated_at < cutoff,
            )
            .values(
                state="failed",
                error_code="cancelled",
                error_detail="长时间无更新，判定为中断",
            )
        )
        affected = int(getattr(result, "rowcount", 0) or 0)
        if affected:
            logger.warning("重启恢复：%d 个中断轮次已标记为 failed", affected)


async def _bootstrap(settings: Settings) -> None:
    """首次启动引导：默认角色、管理员账号、默认窗口。"""
    from ling.domains.persona.service import PersonaService

    async with session_scope() as session:
        identity = IdentityService(session)
        await PersonaService(session, settings.persona_id).ensure_default()
        if await identity.count_users() > 0:
            return
        # 首次安装：没有配置口令就生成一个随机口令，并**打印到日志**。
        #
        # 为什么不再"没配就报错让人去配环境变量"：安装脚本已经把服务拉起来了，
        # 报错只会让人对着一个起不来的服务猜。随机口令 + 日志打印 + 强制改密，
        # 三件事一起做才既安全又能让人真的登进去。
        generated = not settings.bootstrap_admin_password
        password = settings.bootstrap_admin_password or secrets.token_urlsafe(15)
        await identity.create_user(
            username=settings.bootstrap_admin_user,
            password=password,
            role="owner",
            display_name=settings.bootstrap_admin_user,
            must_change_password=generated,
        )
        if generated:
            logger.warning(
                "\n"
                "%s\n"
                "  首次安装：已创建管理员账号（口令是随机生成的）\n"
                "    用户名  %s\n"
                "    密码    %s\n"
                "  登录后会强制要求修改密码。这条口令也只在此处出现一次。\n"
                "%s",
                "=" * 64,
                settings.bootstrap_admin_user,
                password,
                "=" * 64,
            )
        else:
            logger.info("已创建引导管理员账号 %s", settings.bootstrap_admin_user)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = init_engine(settings)
    if settings.env != "production":
        # 开发/测试：自动建表。生产由 Alembic 迁移负责。
        await create_all_for_dev(engine)
    else:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    box = SecretBox(settings)
    app.state.secret_box = box
    app.state.cancels = CancelRegistry()
    app.state.arbiter = SpeakerArbiter()
    # 跨实例协调（设计文档 §10：不引入 Redis，用已有 PostgreSQL）
    app.state.turn_control = TurnControlStore()
    app.state.speak_lease = SpeakLeaseStore()
    app.state.stream_log = StreamLog()
    app.state.group_budget = GroupBudgetRegistry()
    # 工具注册表：默认只含只读/诊断能力。不可逆与公开发布默认不注册，
    # 需要显式授权（build_default_registry 的 allow_irreversible 参数）。
    from ling.capabilities.tools import build_default_registry

    app.state.tools = build_default_registry()

    def secret_reader(provider_id: str) -> str | None:
        # Provider 构造时需要同步读取；这里用同步缓存 + 启动预热避免阻塞事件循环。
        return app.state.secret_cache.get(provider_id)

    app.state.secret_cache = {}
    # 诊断缓存：provider_id → ok/missing/undecryptable。刷新密钥时一并更新，
    # 供错误提示区分"没存过"与"存过但解不开"。
    app.state.secret_diagnosis = {}
    app.state.registry = ProviderRegistry(
        secret_reader, lambda pid: app.state.secret_diagnosis.get(pid, "missing")
    )
    app.state.router = ModelRouter(app.state.registry)

    # 记录本进程加载到哪一版配置；变了就重载（跨副本传播）。
    from ling.settings.refresh import RevisionWatcher, current_revision

    app.state.revision_watcher = RevisionWatcher()

    # 预热已保存的供应商密钥（只解密到进程内存，不落日志）
    async with session_scope() as session:
        svc = ConfigService(session, box, test_mode_default=settings.test_mode,
                            persona_id=settings.persona_id)
        effective = await svc.effective()
        for pid in effective.providers:
            try:
                app.state.secret_diagnosis[pid] = await svc.secret_diagnosis(pid)
            except Exception:  # noqa: BLE001
                app.state.secret_diagnosis[pid] = "missing"
            secret = await svc.read_secret(pid)
            if secret:
                app.state.secret_cache[pid] = secret
        app.state.revision_watcher.mark_loaded(await current_revision(session))

    # OneBot WS **服务端**（推荐）：NapCat 连进来。见 connectors/onebot/ws_server.py
    ws_server = None
    if settings.onebot_ws_port:
        from connectors.onebot.connector import OneBotV11Connector
        from connectors.onebot.ws_server import OneBotWsServer

        from ling.capabilities.channels import onebot_config

        server_connector = OneBotV11Connector(onebot_config())
        app.state.onebot_connector = server_connector
        ws_server = OneBotWsServer(
            host="0.0.0.0",
            port=settings.onebot_ws_port,
            app=app,
            connector=server_connector,
            token=settings.onebot_ws_token,
        )
        app.state.onebot_ws_server = ws_server
        await ws_server.start()

    # QQ 官方机器人：**WebSocket 网关**（ling 主动连出去）。
    #
    # 为什么默认走网关而不是 Webhook：Webhook 要求腾讯能访问到公网 HTTPS
    # 回调地址，而 ling 常跑在内网；官方文档还明确写了"发送消息接口要求
    # 机器人连接到 WebSocket 保持在线"。所以网关是主路径，Webhook 只是备用。
    #
    # 只在**凭据齐备**时启动：没凭据还去连只会不断失败并刷日志。
    qqbot_gateway = None
    if settings.qqbot_enabled:
        from ling.capabilities.channels import qqbot_config

        try:
            bot_config = qqbot_config(settings)
        except Exception:  # noqa: BLE001 - 配置读不到就不启动
            bot_config = None
        if bot_config is not None and bot_config.configured:
            from connectors.qqbot.connector import QQBotConnector
            from connectors.qqbot.ws_gateway import QQBotGateway

            bot_connector = QQBotConnector(bot_config)
            app.state.qqbot_connector = bot_connector
            qqbot_gateway = QQBotGateway(connector=bot_connector, app=app)
            app.state.qqbot_gateway = qqbot_gateway
            qqbot_gateway.start()
            logger.info(
                "QQ 官方机器人网关已启动（app_id=%s，%s）",
                bot_config.app_id,
                "沙箱" if bot_config.sandbox else "正式",
            )
        else:
            app.state.qqbot_gateway = None
            logger.info(
                "QQ 官方机器人已启用但缺少凭据（LING_QQBOT_APP_ID/CLIENT_SECRET），网关不启动"
            )

    # OneBot 反向 WS 入站：配了地址才启动。
    # 放在 API 进程而不是 worker，因为入站要用的 coordinator/router 都在这里。
    ws_client = None
    if settings.onebot_ws_url:
        from connectors.onebot.connector import OneBotV11Connector
        from connectors.onebot.ws_client import OneBotWsClient

        from ling.capabilities.channels import onebot_config

        connector = OneBotV11Connector(onebot_config())
        app.state.onebot_connector = connector
        ws_client = OneBotWsClient(
            url=settings.onebot_ws_url, app=app, connector=connector
        )
        app.state.onebot_ws = ws_client
        ws_client.start()
        logger.info("OneBot WS 入站已启动: %s", settings.onebot_ws_url)
    else:
        app.state.onebot_ws = None

    await _recover_dangling_turns()
    await _bootstrap(settings)
    logger.info("ling api 就绪 env=%s db=%s", settings.env, _safe_db(settings.database_url))
    try:
        yield
    finally:
        if ws_client is not None:
            await ws_client.stop()
        if ws_server is not None:
            await ws_server.stop()
        if qqbot_gateway is not None:
            await qqbot_gateway.stop()
        await dispose_engine()


def _safe_db(url: str) -> str:
    """日志里不出现口令。"""
    if "@" in url:
        head, tail = url.split("@", 1)
        scheme = head.split("//")[0] if "//" in head else ""
        return f"{scheme}//***@{tail}"
    return url


# 参数校验错误统一成 ErrorResponse 的路径前缀。
# 只覆盖记忆契约（docs/API-WEB-V1.md §10）承诺的端点；其它路径继续用
# FastAPI 默认的 `{"detail":[...]}`。不注册成全局处理器是刻意的：那会把
# "记忆端点的局部统一"扩大成计划外的全局行为变更，其它端点的既有客户端
# （含本仓库前端）已经在按默认形状解析。
VALIDATION_ERROR_PREFIXES = ("/v1/memory", "/v1/core-memory")


def _validation_scope_matches(path: str) -> bool:
    """``path`` 是否落在记忆端点下。

    前缀必须**整段**匹配：``/v1/memory``、``/v1/memory/...``、
    ``/v1/core-memory``、``/v1/core-memory/...`` 命中，``/v1/memoryfoo``
    不算（目前没有这种路由，但匹配规则不该依赖"恰好没有"）。
    """
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in VALIDATION_ERROR_PREFIXES
    )


async def memory_request_validation_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """记忆端点：把 pydantic/FastAPI 的参数校验错误翻译成契约的错误形状。

    参数约束**留在 pydantic 里**（`/openapi.json` 因此写得出
    required/pattern/minLength），这里只负责翻译响应形状；不在记忆前缀内的
    路径原样委托 FastAPI 默认处理器，行为与改动前一致。
    """
    if not _validation_scope_matches(request.url.path):
        return await request_validation_exception_handler(request, exc)

    from ling.api.observability import TRACE_HEADER, trace_id_of

    errors = exc.errors()
    first = errors[0] if errors else {}
    # loc 形如 ("body","kind") / ("query","limit") / ("body","items",0,"x")；
    # 去掉下标，留最后一个字段名。整个 body 缺失时 loc 只有 ("body",)。
    loc = [str(part) for part in first.get("loc", ()) if not isinstance(part, int)]
    field = loc[-1] if loc and loc[-1] != "body" else "请求体"
    detail = str(first.get("msg", "")).strip() or None
    trace_id = trace_id_of(request)
    response = error_response(
        ErrorCode.invalid_request,
        f"{field} 参数不合法",
        status_code=422,
        detail=detail,
        trace_id=trace_id,
    )
    if trace_id:
        response.headers[TRACE_HEADER] = trace_id
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="ling",
        version=__version__,
        description="独立的拟人化 AI 角色系统（会话优先）",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "production" else None,
        openapi_url="/api/openapi.json" if settings.env != "production" else None,
    )
    app.state.settings = settings
    app.add_exception_handler(LingError, ling_error_handler)  # type: ignore[arg-type]
    # 记忆端点（`/v1/memory*`、`/v1/core-memory*`）的 422 统一成
    # ErrorResponse；其它路径仍是 FastAPI 默认的 `{"detail":[...]}`。
    app.add_exception_handler(
        RequestValidationError, memory_request_validation_handler  # type: ignore[arg-type]
    )

    from ling.api.observability import observability_middleware
    from ling.api.routes import actor, auth, chat, health, manage, onebot, ops, sessions
    from ling.api.routes import settings as settings_routes
    from ling.api.routes import version as version_routes

    # 最外层：给每个请求一个 trace_id，并记录访问日志。
    # 必须在路由之前注册，否则 4xx/5xx 走不到这里。
    app.add_middleware(BaseHTTPMiddleware, dispatch=observability_middleware)

    app.include_router(health.router)
    app.include_router(onebot.router)
    app.include_router(ops.router)
    app.include_router(auth.router, prefix="/v1")
    app.include_router(sessions.router, prefix="/v1")
    app.include_router(chat.router, prefix="/v1")
    app.include_router(settings_routes.router, prefix="/v1")
    app.include_router(actor.router, prefix="/v1")
    app.include_router(version_routes.router, prefix="/v1")
    from ling.api.routes import dashboard as dashboard_routes

    app.include_router(dashboard_routes.router, prefix="/v1")
    # W2 管理端点：已实现未测试；前端未开放入口，只供鉴权后的 API 使用
    app.include_router(manage.router, prefix="/v1")
    if settings.qq_enabled:
        from ling.api.routes import qq

        app.include_router(qq.router)
    # QQ 官方机器人 Webhook。默认关闭：没配 app_id/secret 时这个入口只会
    # 对着无效签名返回 403，注册它反而让人以为"已经接上了"。
    if settings.qqbot_enabled:
        from ling.api.routes import qqbot

        app.include_router(qqbot.router)

    _mount_web(app, settings)
    return app


def _mount_web(app: FastAPI, settings: Settings) -> None:
    """托管前端构建产物；没有构建时给可读提示而不是 404。"""
    dist = settings.web_dist
    if dist is None:
        # src/ling/app.py → 仓库根
        candidate = Path(__file__).resolve().parents[2] / "clients" / "web-console" / "dist"
        dist = candidate if candidate.exists() else None
    if dist is not None and dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
        async def spa(full_path: str) -> Response:
            if full_path.startswith(("v1/", "api/", "healthz", "readyz")):
                return JSONResponse({"detail": "not found"}, status_code=404)
            index = dist / "index.html"
            target = dist / full_path
            if full_path and target.is_file():
                return FileResponse(target)
            return FileResponse(index)

    else:

        @app.get("/", include_in_schema=False)
        async def placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "service": "ling",
                    "status": "api-only",
                    "hint": (
                        "前端尚未构建。开发：cd clients/web-console && pnpm install && pnpm dev；"
                        "生产：pnpm build 后设置 LING_WEB_DIST"
                    ),
                    "api_docs": "/api/docs",
                }
            )


app = create_app()


async def ready_check(settings: Settings) -> dict[str, object]:
    """就绪检查：数据库可达 + 迁移版本 + 生效配置修订。"""
    async with session_scope() as session:
        await session.execute(select(1))
        svc = ConfigService(
            session, SecretBox(settings), test_mode_default=settings.test_mode,
            persona_id=settings.persona_id
        )
        effective = await svc.effective()
    return {
        "database": "ok",
        "backend": "postgresql" if settings.is_postgres else "sqlite",
        "config_revision": effective.revision,
        "test_mode": effective.test_mode,
        "profiles": sorted(effective.profiles),
    }
