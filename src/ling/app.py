"""应用装配与生命周期。

依赖装配集中在这里（设计文档 §12 的 ``app.py``）。路由模块只通过
``request.app.state`` 取依赖，不自行构造服务。
"""

from __future__ import annotations

import logging
import os
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
from ling.domains.identity.service import IdentityService, LoginRateLimiter
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
                # **这条不进内存缓冲**：缓冲会由 `/v1/logs` 交给浏览器，
                # 而这里的明文口令是真实的登录凭据。stdout（`docker logs`）
                # 是它仅有的、安装时人手抄的通道，必须保留；但多一条 HTTP
                # 读路径就多一次泄漏机会，而它没有任何排查价值。
                extra={"buffer_exclude": True},
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
    # 登录失败限速计数器。挂在 app.state 上而不是模块全局：
    # 一个进程一份，**进程内**共享（多副本部署时各副本独立计数，
    # 语义见 LoginRateLimiter 的类注释，不假装是全局的）。
    app.state.login_limiter = LoginRateLimiter()
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
                            persona_id=settings.persona_id,
                            log_denials_default=settings.log_access_denials)
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

    # OneBot **HTTP 上报入口**的密钥自检。
    #
    # `/v1/onebot/events` 这条路由**没有 auth 依赖**，端口 6520 默认对外发布，
    # 所以 `LING_ONEBOT_SECRET` 是它唯一的来源判据；为空时 `verify_inbound`
    # 一律返回 False（fail-closed，见 connectors/onebot/connector.py）。
    #
    # 这里**只打日志、不拒绝启动**：这条入口是"可不用"的（很多部署走
    # WS 客户端/服务端模式，根本不配 HTTP 上报）。为了让一个没在用的入口
    # 而让整个服务起不来，是把安全改成了可用性事故。所以：入口自己拒绝，
    # 启动时把"没配 secret，上报会全部 403"喊出来。
    #
    # 级别分两档：**配了 OneBot 才 ERROR**（那时这条入口本该工作，却全 403），
    # 完全没配 OneBot 时只 WARNING——否则每次启动都有一条"可以忽略的 ERROR"，
    # 会把真正的 ERROR 淹掉。
    from ling.capabilities.channels import onebot_config as _onebot_config_for_check

    _onebot_cfg = _onebot_config_for_check()
    if not _onebot_cfg.secret:
        _onebot_in_use = bool(
            os.environ.get("LING_ONEBOT_API_BASE")
            or os.environ.get("LING_ONEBOT_ACCESS_TOKEN")
            or os.environ.get("LING_ONEBOT_SELF_ID")
            or os.environ.get("LING_ONEBOT_WS_PORT")
            or os.environ.get("LING_ONEBOT_WS_URL")
        )
        _msg = (
            "OneBot 未配置 LING_ONEBOT_SECRET：/v1/onebot/events 会拒绝**所有**"
            "上报（403）。这条路由没有鉴权依赖，未配密钥时放行等于允许任何人"
            "伪造 X-Self-ID + 白名单 QQ 号冒充用户。"
            "走 WS 模式（不用 HTTP 上报）时这条入口本来就用不到；"
            "要用 HTTP 上报，请在 OneBot 侧与 .env 里配同一个 secret。"
        )
        if _onebot_in_use:
            logger.error("%s", _msg)
        else:
            logger.warning("%s", _msg)

    # OneBot WS **服务端**（推荐）：NapCat 连进来。见 connectors/onebot/ws_server.py
    #
    # host 由 `LING_ONEBOT_WS_HOST` 决定，**默认 127.0.0.1**。原来这里硬编码
    # 0.0.0.0，等于"配了端口就自动对外"，而 token 为空时还放行——两者叠加
    # 就是任何能访问该端口的人都能往对话链路里推事件。现在没有 token 时
    # `start()` 直接抛错（fail-closed），对外也必须显式配置。
    ws_server = None
    if settings.onebot_ws_port:
        from connectors.onebot.connector import OneBotV11Connector
        from connectors.onebot.ws_server import OneBotWsAuthError, OneBotWsServer

        from ling.capabilities.channels import onebot_config

        server_config = onebot_config()
        server_connector = OneBotV11Connector(server_config)
        app.state.onebot_connector = server_connector
        ws_server = OneBotWsServer(
            # 单一来源：`OneBotConfig.ws_host`（`onebot_config()` 从
            # `LING_ONEBOT_WS_HOST` 读，默认 127.0.0.1）。不在 Settings 里
            # 再定义一份——同一个开关两个默认值迟早漂。
            host=server_config.ws_host,
            port=settings.onebot_ws_port,
            app=app,
            connector=server_connector,
            token=settings.onebot_ws_token,
        )
        try:
            await ws_server.start()
        except OneBotWsAuthError as exc:
            # **不让进程带着一个"谁都能连"的端口起来**。配置错就启动失败，
            # 让它在部署时就暴露出来，而不是安静地跑到被冒充之后才发现。
            logger.error("%s", exc)
            raise
        app.state.onebot_ws_server = ws_server

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

    from ling.api.body_limit import BodySizeLimitMiddleware
    from ling.api.observability import observability_middleware
    from ling.api.routes import actor, auth, chat, health, manage, onebot, ops, sessions
    from ling.api.routes import logs as logs_routes
    from ling.api.routes import settings as settings_routes
    from ling.api.routes import version as version_routes

    # 请求体上限（**先**注册 = 在**内层**）。原来是 nginx 的
    # `client_max_body_size 32m`，删掉反代后由应用自己承担；它只读
    # Content-Length、绝不消费 body 流（否则 SSE 流式回复会被破坏），
    # 所以可以安全地待在 observability 里面。
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_body_bytes)

    # 最外层：给每个请求一个 trace_id，并记录访问日志。
    #
    # **后**注册 = 更外层。`add_middleware` 内部是 `user_middleware.insert(0, …)`，
    # 即后注册的排在列表最前、在 `build_middleware_stack` 里最后被包 → 最外层。
    # 所以这里必须是最后注册的那一个（`user_middleware` 会是
    # `['BaseHTTPMiddleware', 'BodySizeLimitMiddleware']`）。
    #
    # 顺序别调过来。BodySizeLimit 必须在**内层**，这样它返回的 413 才经过这里：
    # 响应带上 `X-Trace-Id`、错误体里的 `trace_id` 不再是 null，访问日志里也有
    # 一条 413。超大请求恰恰是最需要排查的那类——反过来的话 413 在日志之前
    # 就返回了，用户报"上传失败"时只能看到一个 trace_id 为 null 的错误。
    #
    # 多包一层的代价可以忽略：超限判定只看 `Content-Length` 头，body 一个字节
    # 都不会被读进内存，拒绝路径不会因此变贵。
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
    # 运行日志（本进程的环形缓冲）。观察代价极低，且日志页是运维的常驻入口，
    # 所以**无条件注册**——不像 qq/qqbot 那样按开关注册。
    app.include_router(logs_routes.router, prefix="/v1")
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


def _resolve_dist_file(root: Path, full_path: str) -> Path | None:
    """把 SPA 兜底路由的请求路径解析成 **root 之内** 的真实文件。

    越界、非法、不存在、不是普通文件，一律返回 ``None``（调用方回
    ``index.html``）。这是**未认证可达**的路径，三道检查缺一不可：

    1. candidate 也要 ``resolve()``：跟随符号链接，dist 内部指到外面的链接
       因此会落到 root 之外被拒；
    2. 用 ``Path.is_relative_to`` 判包含，**不用字符串前缀**——
       ``/app/web-evil`` 以 ``/app/web`` 开头，字符串前缀比较会误通过；
    3. 返回给 ``FileResponse`` 的必须是解析后的 candidate，不能再拿原始
       ``full_path`` 拼一次。

    ``full_path`` 是 Starlette 捕获的**已解码**路径，``../``（``%2e%2e``、
    ``..%2f`` 解码后同样是 ``../``）能逃出 dist，读到 ``/data/root.key``
    这类文件——根密钥可解密库里全部供应商密钥。所以只判 ``is_file()`` 不够。
    """
    if not full_path:
        return None
    try:
        candidate = (root / full_path).resolve()
    except (OSError, ValueError):
        # 畸形路径（例如百分号编码出来的 NUL）按"不存在"处理。
        # ``Path.is_file()`` 本来就吞掉这些异常，改动不要让它变成 500。
        return None
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None


def _mount_web(app: FastAPI, settings: Settings) -> None:
    """托管前端构建产物；没有构建时给可读提示而不是 404。"""
    dist = settings.web_dist
    if dist is None:
        # src/ling/app.py → 仓库根
        candidate = Path(__file__).resolve().parents[2] / "clients" / "web-console" / "dist"
        dist = candidate if candidate.exists() else None
    if dist is not None and dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        # dist 是 bind mount，自身可能就是符号链接：包含性判断必须对着
        # 解析后的真实目录做（``resolve()`` 缺省非 strict，路径不存在也不抛）。
        root = dist.resolve()
        index = root / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
        async def spa(full_path: str) -> Response:
            if full_path.startswith(("v1/", "api/", "healthz", "readyz")):
                return JSONResponse({"detail": "not found"}, status_code=404)
            target = _resolve_dist_file(root, full_path)
            if target is not None:
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
