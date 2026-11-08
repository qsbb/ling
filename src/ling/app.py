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
    # 两个东西要分开：
    # * `_connector_effective`：本次**实际生效**的解密后配置（只在本进程内存）；
    # * `ConnectorRuntime`：负责 stop(旧) → construct(新) → start(新) 的切换器，
    #   设置页保存时由 `settings/connector_service.py` 调它。
    from ling.capabilities.channels import onebot_config as _env_onebot_config
    from ling.capabilities.channels import qqbot_config as _env_qqbot_config
    from ling.kernel.connector_runtime import ConnectorRuntime, SwapPlan
    from ling.settings import connector_fields as _cf
    from ling.settings import connector_overrides as _ov

    async with session_scope() as session:
        _onebot_effective = await _ov.resolve_field_values(
            session,
            scope=_cf.SCOPE_ONEBOT,
            settings=settings,
            onebot=_env_onebot_config(),
            qqbot=_env_qqbot_config(settings),
            box=box,
            include_secrets=True,
        )
        _qqbot_effective = await _ov.resolve_field_values(
            session,
            scope=_cf.SCOPE_QQBOT,
            settings=settings,
            onebot=_env_onebot_config(),
            qqbot=_env_qqbot_config(settings),
            box=box,
            include_secrets=True,
        )
        _access_effective = await _ov.current_access_snapshot(session, settings=settings)

    app.state.connector_effective = {
        _cf.SCOPE_ONEBOT: dict(_onebot_effective.fields_by_key()),
        _cf.SCOPE_QQBOT: dict(_qqbot_effective.fields_by_key()),
    }
    app.state.access_snapshot = _access_effective
    connector_runtime = ConnectorRuntime(app)
    app.state.connector_runtime = connector_runtime
    # 一次性快照：运行期参数（WS 端口/令牌/地址、渠道开关）。它们不属于
    # `OneBotConfig` / `QQBotConfig`，但同样是"有效配置"的一部分。
    app.state.connector_runtime_settings = _ov.ConnectorRuntimeSettings(
        onebot_ws_port=settings.onebot_ws_port,
        onebot_ws_token=settings.onebot_ws_token,
        onebot_ws_url=settings.onebot_ws_url,
        onebot_secret=_onebot_effective.text("LING_ONEBOT_SECRET"),
        qqbot_enabled=_qqbot_effective.flag("LING_QQBOT_ENABLED"),
    )

    async def _start_onebot(values: dict) -> dict:
        """按一份有效配置起 OneBot 的组件，返回实例映射。

        WS 服务端 / 出站 WS 客户端 / HTTP 懒构造连接器三者互不排斥，所以
        一次构造里可能起两个组件。**失败时由调用方回滚**，这里只管起。
        """
        import dataclasses

        from connectors.onebot.connector import OneBotV11Connector

        runtime_settings = dataclasses.replace(
            app.state.connector_runtime_settings,  # type: ignore[arg-type]
        ).with_values(_cf.SCOPE_ONEBOT, values)
        app.state.connector_runtime_settings = runtime_settings
        config = _ov.build_onebot_config_from(values)
        instances: dict[str, object] = {}

        port = runtime_settings.onebot_ws_port
        if port:
            from connectors.onebot.ws_server import OneBotWsAuthError, OneBotWsServer

            server_connector = OneBotV11Connector(config)
            ws_server = OneBotWsServer(
                host=config.ws_host,
                port=port,
                app=app,
                connector=server_connector,
                token=runtime_settings.onebot_ws_token,
            )
            try:
                await ws_server.start()
            except OneBotWsAuthError as exc:
                # **不让进程带着一个"谁都能连"的端口起来**（fail-closed）。
                raise RuntimeError("OneBot WS 服务端拒绝启动：缺少令牌") from exc
            instances["onebot_ws_server"] = ws_server
            instances["onebot_connector"] = server_connector
            app.state.onebot_ws_server = ws_server
            app.state.onebot_connector = server_connector
            logger.info("OneBot WS 服务端已启动 %s:%s", config.ws_host, port)
        else:
            app.state.onebot_ws_server = None

        if runtime_settings.onebot_ws_url:
            from connectors.onebot.ws_client import OneBotWsClient

            connector = OneBotV11Connector(config)
            ws_client = OneBotWsClient(
                url=runtime_settings.onebot_ws_url, app=app, connector=connector
            )
            app.state.onebot_connector = connector
            app.state.onebot_ws = ws_client
            ws_client.start()
            instances["onebot_ws"] = ws_client
            instances["onebot_connector"] = connector
            logger.info("OneBot WS 入站已启动: %s", runtime_settings.onebot_ws_url)
        else:
            app.state.onebot_ws = None

        if not instances:
            # 只走 HTTP 上报：仍然要有可用的连接器对象（发消息要用）。
            connector = OneBotV11Connector(config)
            app.state.onebot_connector = connector
            instances["onebot_connector"] = connector
        return instances

    async def _start_qqbot(values: dict) -> dict:
        from connectors.qqbot.connector import QQBotConnector
        from connectors.qqbot.ws_gateway import QQBotGateway

        runtime_settings = app.state.connector_runtime_settings
        runtime_settings = runtime_settings.with_values(_cf.SCOPE_QQBOT, values)
        app.state.connector_runtime_settings = runtime_settings
        bot_config = _ov.build_qqbot_config_from(values)
        if not runtime_settings.qqbot_enabled or not bot_config.configured:
            # 关着或凭据不全：不启动网关。**如实记下原因**，不假装在连。
            app.state.qqbot_gateway = None
            logger.info(
                "QQ 官方机器人不启动（enabled=%s configured=%s）",
                runtime_settings.qqbot_enabled,
                bot_config.configured,
            )
            return {}
        bot_connector = QQBotConnector(bot_config)
        gateway = QQBotGateway(connector=bot_connector, app=app)
        app.state.qqbot_connector = bot_connector
        app.state.qqbot_gateway = gateway
        gateway.start()
        logger.info(
            "QQ 官方机器人网关已启动（app_id 指纹=%s，%s）",
            _fingerprint(bot_config.app_id),
            "沙箱" if bot_config.sandbox else "正式",
        )
        return {"qqbot_gateway": gateway, "qqbot_connector": bot_connector}

    async def _start_access(values: dict) -> dict:
        """白名单生效：**换掉进程里的 Settings 快照**，不重建任何连接。

        `kernel/inbound_intake` 的授权判断读的是 ``app.state.settings``；
        这里用 ``dataclasses.replace`` 造一份新的 frozen Settings，只改
        ``access_mode`` / ``access_list``，然后**一次赋值**换上去。

        为什么用替换而不是"让入站自己去查 DB"：
        - 入站路径在每条消息上，加一次 DB 查询是白给；
        - 一次属性读取就是一次快照，请求处理期间不会读到一半新一半旧
          （设计 §4："请求处理时捕获一次代际快照"）。

        赋值是同步的（中间没有 await），所以在 asyncio 单线程里对其他协程
        原子可见——不会出现"验签读旧 token、授权读新名单"。
        """
        import dataclasses

        from ling.settings import connector_fields as _cf2

        old_settings = app.state.settings
        new_settings = dataclasses.replace(
            old_settings,
            access_mode=str(values.get("LING_ACCESS_MODE") or "off"),
            access_list=frozenset(_cf2.normalize_list(values.get("LING_ACCESS_LIST"))),
        )
        app.state.settings = new_settings
        async with session_scope() as session:
            app.state.access_snapshot = await _ov.current_access_snapshot(
                session, settings=new_settings
            )
        logger.info(
            "出入许可已生效 mode=%s 名单=%d 人",
            new_settings.access_mode,
            len(new_settings.access_list),
        )
        return {}

    async def _plan_access(values: dict) -> SwapPlan:
        return SwapPlan(stops=(), start=lambda: _start_access(values))

    async def _plan_onebot(values: dict) -> SwapPlan:
        return SwapPlan(stops=("onebot_ws_server", "onebot_ws"), start=lambda: _start_onebot(values))

    async def _plan_qqbot(values: dict) -> SwapPlan:
        return SwapPlan(stops=("qqbot_gateway",), start=lambda: _start_qqbot(values))

    connector_runtime.register(
        _cf.SCOPE_ONEBOT,
        _plan_onebot,
        values=app.state.connector_effective[_cf.SCOPE_ONEBOT],
    )
    connector_runtime.register(
        _cf.SCOPE_QQBOT,
        _plan_qqbot,
        values=app.state.connector_effective[_cf.SCOPE_QQBOT],
    )
    connector_runtime.register(
        _cf.SCOPE_ACCESS,
        _plan_access,
        values={
            "LING_ACCESS_MODE": _access_effective.mode,
            "LING_ACCESS_LIST": sorted(_access_effective.allow_list),
        },
    )

    # 启动时按有效配置起组件（与升级前行为一致：env 就是"有效配置"）。
    if settings.onebot_ws_port or settings.onebot_ws_url:
        _onebot_instances = await _start_onebot(
            app.state.connector_effective[_cf.SCOPE_ONEBOT]
        )
        connector_runtime.register(
            _cf.SCOPE_ONEBOT,
            _plan_onebot,
            instances=_onebot_instances,
            values=app.state.connector_effective[_cf.SCOPE_ONEBOT],
        )
    else:
        # 只走 HTTP：仍需一个连接器对象（懒构造也会覆盖它）。
        from connectors.onebot.connector import OneBotV11Connector as _LazyOneBot

        app.state.onebot_connector = _LazyOneBot(
            _ov.build_onebot_config_from(app.state.connector_effective[_cf.SCOPE_ONEBOT])
        )
    _qqbot_instances = await _start_qqbot(app.state.connector_effective[_cf.SCOPE_QQBOT])
    connector_runtime.register(
        _cf.SCOPE_QQBOT,
        _plan_qqbot,
        instances=_qqbot_instances,
        values=app.state.connector_effective[_cf.SCOPE_QQBOT],
    )
    await _recover_dangling_turns()
    await _bootstrap(settings)
    logger.info("ling api 就绪 env=%s db=%s", settings.env, _safe_db(settings.database_url))
    try:
        yield
    finally:
        for _scope in (_cf.SCOPE_ONEBOT, _cf.SCOPE_QQBOT):
            for _name, _instance in (connector_runtime._instances.get(_scope) or {}).items():
                _stopper = getattr(_instance, "stop", None)
                if _stopper is not None:
                    try:
                        _result = _stopper()
                        if hasattr(_result, "__await__"):
                            await _result
                    except Exception:  # noqa: BLE001 - 关闭路径不掩盖原始错误
                        logger.warning("关闭接入器组件失败：%s", _name)
        # 额度记账的后台落库任务要在引擎关闭前收尾：否则最后几轮的用量
        # 只留在内存里，下次启动就没了。
        quota = getattr(app.state, "quota", None)
        if quota is not None:
            await quota.aclose()
        await dispose_engine()


def _fingerprint(value: str) -> str:
    """身份类字段（AppID）的短指纹：日志里只用它，不出现原值。"""
    import hashlib

    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8] if value else ""


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
