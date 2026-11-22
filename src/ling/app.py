"""应用装配与生命周期。

依赖装配集中在这里（设计文档 §12 的 ``app.py``）。路由模块只通过
``request.app.state`` 取依赖，不自行构造服务。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.middleware.base import BaseHTTPMiddleware, Response

from ling import __version__
from ling.api.error_handlers import register_error_handlers
from ling.config import Settings, get_settings, warn_deprecated_env
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

    from ling.capabilities.voice_reply import recover_synthesis_jobs
    from ling.infra.models import Turn, utcnow

    cutoff = utcnow() - timedelta(minutes=DANGLING_TURN_MINUTES)
    async with session_scope() as session:
        recovered_speech = await recover_synthesis_jobs(session, cutoff=cutoff)
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
        if recovered_speech:
            logger.warning("重启恢复：%d 个中断语音作业已标记为 uncertain", recovered_speech)


async def _bootstrap(settings: Settings) -> None:
    """首次启动引导：默认角色、管理员账号、默认窗口。"""
    from ling.domains.persona.service import PersonaService

    async with session_scope() as session:
        identity = IdentityService(session)
        # **不再建人格行**：新装就是"暂无人设"（用户 2026-11-11 的要求）。
        # 这里调用它只是取一次当前人格并让它出现在日志里——
        # `ensure_default()` 现在读不到就返回 None，不写库。
        persona = await PersonaService(session, settings.persona_id).ensure_default()
        if persona is None:
            logger.info(
                "启动引导：还没有人格（persona_id=%s 指向不存在的行）。"
                "人设页会显示「暂无人设」，新建并设为当前之后立即生效",
                settings.persona_id,
            )
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
    warn_deprecated_env()
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
    # 每日 token 额度：记账表在库里，额度判断在路由里。
    # 时区取用户的本地时区（"今天"是本地日历日，不是 UTC 日）。
    #
    # 走**唯一解析出口**（DB 偏好 > LING_USER_TIMEZONE > Asia/Shanghai）：
    # 只读环境变量的话，用户在设置页填的时区对"今天已用"的日界无效，
    # 额度会在本地凌晨前的某个时刻被清零。
    from ling.infra.db import get_session_factory
    from ling.models.quota import TokenQuotaStore
    from ling.settings.user_preferences import (
        effective_user_timezone,
        owner_subject_id,
    )

    async with session_scope() as session:
        quota_timezone = await effective_user_timezone(
            session,
            subject_id=await owner_subject_id(session),
            env=settings.user_timezone,
        )
    app.state.quota = TokenQuotaStore(
        get_session_factory(), timezone=quota_timezone
    )
    app.state.router = ModelRouter(app.state.registry, app.state.quota)

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

    # ---------------------------------------------------------- 接入器构造
    #
    # 以前的写法是在这里**直接读 env 构造**（`onebot_config()` / `settings.qqbot_*`），
    # 并且每个分支各自 `if` 一遍"配了才起"。现在统一走**有效配置**：
    #
    #     DB 已生效覆盖 > 进程启动时的 env 快照 > 代码默认
    #
    # 这样设置页保存后，同一个构造函数被复用一次就能把新配置真正用起来，
    # 而且不会出现"页面写进去了、进程还读 env"的裂缝（设计 §8）。
    #
    # 切换器按实例保存已发布构造参数，设置保存与路由均从它取运行对象。
    from ling.capabilities.channels import onebot_config as _env_onebot_config
    from ling.capabilities.channels import qqbot_config as _env_qqbot_config
    from ling.kernel.connector_runtime import ConnectorRuntime, SwapPlan
    from ling.settings import connector_fields as _cf
    from ling.settings import connector_overrides as _ov

    async with session_scope() as session:
        _access_effective = await _ov.current_access_snapshot(session, settings=settings)

    app.state.access_snapshot = _access_effective
    app.state.settings = dataclasses.replace(
        settings,
        access_mode=_access_effective.mode,
        access_list=_access_effective.allow_list,
        access_groups=_access_effective.allow_groups,
    )
    connector_runtime = ConnectorRuntime(app)
    app.state.connector_runtime = connector_runtime
    async def _start_onebot(values: dict, instance_id: str) -> dict:
        from connectors.onebot.connector import OneBotV11Connector
        from connectors.onebot.ws_client import OneBotWsClient
        from connectors.onebot.ws_server import OneBotWsServer

        runtime_settings = _ov.ConnectorRuntimeSettings().with_values(_cf.SCOPE_ONEBOT, values)
        config = _ov.build_onebot_config_from(values)
        connector = OneBotV11Connector(config)
        components: dict[str, object] = {"onebot_connector": connector}
        try:
            if runtime_settings.onebot_ws_port:
                server = OneBotWsServer(
                    host=config.ws_host, port=runtime_settings.onebot_ws_port,
                    app=app, connector=connector, token=runtime_settings.onebot_ws_token,
                    source_ips=config.source_ips,
                    trusted_instance_key=("onebot", instance_id),
                )
                components["onebot_ws_server"] = server
                await server.start()
            if runtime_settings.onebot_ws_url:
                client = OneBotWsClient(
                    url=runtime_settings.onebot_ws_url,
                    app=app,
                    connector=connector,
                    trusted_instance_key=("onebot", instance_id),
                )
                components["onebot_ws"] = client
                client.start()
        except BaseException:
            for component in components.values():
                stop = getattr(component, "stop", None)
                if stop is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await stop()
            raise
        return components

    async def _start_qqbot(values: dict, instance_id: str) -> dict:
        from connectors.qqbot.connector import QQBotConnector
        from connectors.qqbot.ws_gateway import QQBotGateway

        runtime_settings = _ov.ConnectorRuntimeSettings().with_values(_cf.SCOPE_QQBOT, values)
        config = _ov.build_qqbot_config_from(values)
        if not runtime_settings.qqbot_enabled or not config.configured:
            return {}
        connector = QQBotConnector(config)
        gateway = QQBotGateway(
            connector=connector,
            app=app,
            trusted_instance_key=("qqbot", instance_id),
        )
        try:
            gateway.start()
        except BaseException:
            await gateway.stop()
            await connector.close()
            raise
        return {"qqbot_gateway": gateway, "qqbot_connector": connector}

    async def _start_access(values: dict) -> dict:
        """白名单生效：**换掉进程里的 Settings 快照**，不重建任何连接。

        `kernel/inbound_intake` 的授权判断读的是 ``app.state.settings``；
        这里用 ``dataclasses.replace`` 造一份新的 frozen Settings，只改
        出入许可的模式和名单，然后**一次赋值**换上去。

        为什么用替换而不是"让入站自己去查 DB"：
        - 入站路径在每条消息上，加一次 DB 查询是白给；
        - 一次属性读取就是一次快照，请求处理期间不会读到一半新一半旧
          （设计 §4："请求处理时捕获一次代际快照"）。

        赋值是同步的（中间没有 await），所以在 asyncio 单线程里对其他协程
        原子可见——不会出现"验签读旧 token、授权读新名单"。
        """
        from ling.settings import connector_fields as _cf2

        old_settings = app.state.settings
        new_settings = dataclasses.replace(
            old_settings,
            access_mode=str(values.get("LING_ACCESS_MODE") or "off"),
            access_list=frozenset(_cf2.normalize_list(values.get("LING_ACCESS_LIST"))),
            access_groups=frozenset(_cf2.normalize_list(values.get("LING_ACCESS_GROUPS"))),
        )
        app.state.settings = new_settings
        logger.info(
            "出入许可已生效 mode=%s 名单=%d 人",
            new_settings.access_mode,
            len(new_settings.access_list),
        )
        return {}

    async def _plan_access(values: dict) -> SwapPlan:
        return SwapPlan(stops=(), start=lambda: _start_access(values))

    async def _plan_onebot(instance_id: str, values: dict) -> SwapPlan:
        return SwapPlan(stops=("onebot_ws_server", "onebot_ws"),
                        start=lambda: _start_onebot(values, instance_id))

    async def _plan_qqbot(instance_id: str, values: dict) -> SwapPlan:
        return SwapPlan(
            stops=("qqbot_gateway",),
            start=lambda: _start_qqbot(values, instance_id),
        )

    connector_runtime.register_factory(_cf.SCOPE_ONEBOT, _plan_onebot)
    connector_runtime.register_factory(_cf.SCOPE_QQBOT, _plan_qqbot)
    connector_runtime.register(
        _cf.SCOPE_ACCESS, _plan_access,
        values={"LING_ACCESS_MODE": _access_effective.mode,
                "LING_ACCESS_LIST": sorted(_access_effective.allow_list),
                "LING_ACCESS_GROUPS": sorted(_access_effective.allow_groups)},
    )

    from ling.infra.models import DEFAULT_INSTANCE_ID, ConnectorConfigHead, ConnectorInstance
    from ling.settings.connector_service import (
        STATE_ROLLED_BACK,
        ConnectorConfigService,
        SwapOutcome,
    )

    async with session_scope() as session:
        interrupted = list((await session.execute(select(ConnectorConfigHead).where(
            ConnectorConfigHead.candidate_revision_id.is_not(None),
        ))).scalars())
        recovery = ConnectorConfigService(settings=settings, box=box, runtime=connector_runtime)
        for head in interrupted:
            await recovery._mark_failed(
                session, scope=head.scope, instance_id=head.instance_id,
                revision_id=cast(str, head.candidate_revision_id),
                outcome=SwapOutcome(ok=False, error_code="activation_interrupted",
                                    error_safe="上次配置切换被中断，已恢复已发布配置"),
                state=STATE_ROLLED_BACK,
            )
        rows = list((await session.execute(select(ConnectorInstance))).scalars())
        enabled_instances = {(row.scope, row.instance_id) for row in rows
                             if row.enabled and row.deleted_at is None}
        present = {(row.scope, row.instance_id) for row in rows}
        for scope in (_cf.SCOPE_ONEBOT, _cf.SCOPE_QQBOT):
            if (scope, DEFAULT_INSTANCE_ID) not in present:
                enabled_instances.add((scope, DEFAULT_INSTANCE_ID))
        startup_values = []
        for scope, instance_id in sorted(enabled_instances):
            if scope == _cf.SCOPE_ACCESS:
                continue
            resolved = await _ov.resolve_field_values(
                session, scope=scope, instance_id=instance_id, settings=settings,
                onebot=_env_onebot_config(), qqbot=_env_qqbot_config(settings),
                box=box, include_secrets=True,
            )
            startup_values.append((scope, instance_id, resolved.fields_by_key()))
    try:
        for scope, instance_id, values in startup_values:
            outcome = await connector_runtime.apply(
                scope, instance_id=instance_id, values=values, operation_id="startup"
            )
            if not outcome.ok:
                logger.error("接入器启动失败 scope=%s instance=%s code=%s",
                             scope, instance_id, outcome.error_code)
    except BaseException:
        await connector_runtime.shutdown()
        raise
    await _recover_dangling_turns()
    await _bootstrap(settings)
    logger.info("ling api 就绪 env=%s db=%s", settings.env, _safe_db(settings.database_url))
    from ling.domains.speech.asr_runtime import stop_worker, worker_loop
    asr_worker = asyncio.create_task(worker_loop(app), name="asr-worker")
    try:
        yield
    finally:
        await stop_worker(asr_worker)
        await connector_runtime.shutdown()
        # 额度记账的后台落库任务要在引擎关闭前收尾：否则最后几轮的用量
        # 只留在内存里，下次启动就没了。
        quota = getattr(app.state, "quota", None)
        if quota is not None:
            await quota.aclose()
        await dispose_engine()


def _safe_db(url: str) -> str:
    """日志里不出现口令。"""
    if "@" in url:
        head, tail = url.split("@", 1)
        scheme = head.split("//")[0] if "//" in head else ""
        return f"{scheme}//***@{tail}"
    return url


def create_app(settings: Settings | None = None) -> FastAPI:
    # 每次建 app 都**确保**日志链路在：`setup_logging` 是幂等的，代价只有几次
    # isinstance 检查。为什么不够"只在模块导入时配一次"——测试或嵌入方可能把
    # root handler 清空（`logging.getLogger().handlers.clear()`、宿主框架重配
    # 日志），那一刻起环形缓冲与 `ContextFilter` 就都不在了，表现是
    # `/v1/logs` 永远为空、日志里的 trace_id 全是 null，而进程看起来一切正常。
    setup_logging(role="api")
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
    app.state.connector_config_baseline = settings
    register_error_handlers(app)

    from ling.api.body_limit import BodySizeLimitMiddleware
    from ling.api.observability import observability_middleware
    from ling.api.routes import actor, auth, chat, health, manage, media, onebot, ops, sessions
    from ling.api.routes import logs as logs_routes
    from ling.api.routes import persona as persona_routes
    from ling.api.routes import recall as recall_routes
    from ling.api.routes import settings as settings_routes
    from ling.api.routes import tool_plugins as tool_plugins_routes
    from ling.api.routes import external_plugins as external_plugins_routes
    from ling.api.routes import version as version_routes
    from ling.api.routes import window_policy as window_policy_routes

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
    app.include_router(window_policy_routes.router, prefix="/v1")
    app.include_router(chat.router, prefix="/v1")
    from ling.api.routes import live, memory_import, transcriptions
    app.include_router(live.router, prefix="/v1")
    app.include_router(memory_import.router, prefix="/v1")
    app.include_router(transcriptions.router, prefix="/v1")
    # 主动撤回自己发的消息（破坏性动作，独立路由与独立授权边界）
    app.include_router(recall_routes.router, prefix="/v1")
    # 生成媒体的读取（SP03）：一条签名 URL（无登录态，`<audio>` 用）+ 三条
    # 登录态端点（会话里的语音列表、单条语音、播放上报）。**无条件注册**：
    # 它只读本项目自己的表与本地媒体目录，没有外部依赖。
    app.include_router(media.router, prefix="/v1")
    app.include_router(settings_routes.router, prefix="/v1")
    app.include_router(tool_plugins_routes.router, prefix="/v1")
    app.include_router(external_plugins_routes.router, prefix="/v1")
    app.include_router(actor.router, prefix="/v1")
    # 多套人格（`/v1/personas`）：列表/新建/详情/编辑/删除/切换。
    # 与 `actor.py` 里的单例 `/v1/persona`（= 当前激活人格的快捷方式）并存。
    app.include_router(persona_routes.router, prefix="/v1")
    app.include_router(version_routes.router, prefix="/v1")
    # 运行日志（本进程的环形缓冲）。观察代价极低，且日志页是运维的常驻入口，
    # 所以**无条件注册**——不像 qq/qqbot 那样按开关注册。
    app.include_router(logs_routes.router, prefix="/v1")
    from ling.api.routes import dashboard as dashboard_routes
    from ling.api.routes import life_timeline as life_timeline_routes

    app.include_router(dashboard_routes.router, prefix="/v1")
    # 只读生活时间轴（设计 §7）：双时区、计划/事实分层、未来意向与梦卡片。
    # 与 dashboard 一样**无条件注册**：它只读本项目自己的表，没有外部依赖。
    app.include_router(life_timeline_routes.router, prefix="/v1")
    # W2 管理端点：已实现未测试；前端未开放入口，只供鉴权后的 API 使用
    app.include_router(manage.router, prefix="/v1")
    if settings.qq_enabled:
        from ling.api.routes import qq

        app.include_router(qq.router)
    # QQ 官方机器人 Webhook。默认关闭：没配 app_id/secret 时这个入口只会
    # 对着无效签名返回 403，注册它反而让人以为"已经接上了"。
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


class _SpaPathConvertor(PathConvertor):
    # A matching SPA GET would take precedence over an API route's partial
    # match, changing a real 405 (+ Allow) into a 404 after the web build.
    regex = r"(?!v1(?:/|$)).*"


register_url_convertor("ling_spa", _SpaPathConvertor())


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

        @app.get("/{full_path:ling_spa}", include_in_schema=False, response_model=None)
        async def spa(full_path: str) -> Response:
            if full_path.startswith(("api/", "healthz", "readyz")):
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
        "backend": "postgresql",
        "config_revision": effective.revision,
        "test_mode": effective.test_mode,
        "profiles": sorted(effective.profiles),
    }
