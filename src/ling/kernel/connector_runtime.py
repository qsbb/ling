"""接入器**实例切换器**：在进程里完成 stop(旧) → construct(新) → start(新)。

## 为什么切换要在 api 进程里，且必须允许"构造后注入"

连接器实例挂在 ``app.state`` 上（``onebot_ws_server`` / ``onebot_ws`` /
``qqbot_gateway`` / ``onebot_connector`` / ``qqbot_connector``）。"改配置"
真正要做的是**换掉这些对象**：

- 旧对象先 ``stop()``（端口必须释放，否则新实例 bind 失败）；
- 用新配置构造新对象；
- ``start()``；成功后才把 ``app.state`` 上的引用替换成新的。

失败时必须用**旧配置**重新构造一份（旧对象 stop 之后不保证能再 start，
见设计 §4），并让引用继续指向可用的那份配置。

## 为什么用"工厂注册表"而不是在这里 import 具体连接器

切换器要能被单测驱动（不需要真的绑端口）：测试注册一个假工厂，断言
"校验失败时 0 次 stop""start 抛错时旧配置被重建"。生产在 ``app.py`` 里注册
真实工厂。

## 时序记录（设计 §4 要求可量化中断窗口）

每次切换记录三个时刻：``disconnect_started_at``（开始停旧）、
``listener_ready_at``（新实例 start 返回）、``remote_connected_at``（网关/WS
在等待窗口内报告已连上）。**"任务已创建"不等于"已连上"**，所以远端那一步
有独立的有界等待，超时只影响 ``health``（degraded），不影响"本地已切换"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ling.infra.models import DEFAULT_INSTANCE_ID
from ling.settings.connector_service import ConnectorServiceError, SwapOutcome

logger = logging.getLogger("ling.kernel.connector_runtime")

#: 远端握手的有界等待（秒）。设计 §4：远端连不上不该无限卡在"生效中"。
REMOTE_CONNECT_WAIT_SECONDS = 3.0

#: 启动后检查"是否已连上"的间隔
REMOTE_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class SwapPlan:
    """一次切换的动作清单（由工厂按 scope 生成）。"""

    #: 要停掉的对象（按顺序）；可能为空
    stops: tuple[str, ...]
    #: 构造+启动，返回"新实例的名字 → 对象"映射，失败抛异常
    start: Callable[[], Awaitable[dict[str, Any]]]
    #: 判断远端是否已连上（拿新实例映射；返回 True=已连）
    connected: Callable[[dict[str, Any]], bool] | None = None


class ConnectorRuntime:
    """按 (scope, instance_id) 切换连接器实例，并把结果写回 ``app.state``。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        #: 实例身份 → 生成 SwapPlan 的工厂（``None`` 表示这个 scope 不重连，
        #: 例如白名单：它只在下一个请求生效，不需要动连接）。
        self._plans: dict[tuple[str, str], Callable[[dict[str, Any]], Awaitable[SwapPlan]]] = {}
        #: 实例身份 → 组件名 → 对象（供停止与回滚使用）
        self._instances: dict[tuple[str, str], dict[str, Any]] = {}
        #: 实例身份 → 构造参数快照（回滚时重建用）
        self._effective: dict[tuple[str, str], dict[str, Any]] = {}
        self._factories: dict[str, Callable[[str, dict[str, Any]], Awaitable[SwapPlan]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 注册

    def register(
        self,
        scope: str,
        factory: Callable[[dict[str, Any]], Awaitable[SwapPlan]],
        *,
        instance_id: str = DEFAULT_INSTANCE_ID,
        instances: dict[str, Any] | None = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        key = (scope, instance_id)
        self._plans[key] = factory
        if instances is not None:
            self._instances[key] = instances
            self._publish(scope, instance_id, instances)
        if values is not None:
            self._effective[key] = values

    def snapshot(self, scope: str, instance_id: str = DEFAULT_INSTANCE_ID) -> dict[str, Any]:
        """当前构造参数的进程内副本，仅供重建与状态解析，禁止输出或序列化。"""
        return dict(self._effective.get((scope, instance_id)) or {})

    def register_factory(
        self, scope: str, factory: Callable[[str, dict[str, Any]], Awaitable[SwapPlan]]
    ) -> None:
        self._factories[scope] = factory

    def components(self, scope: str, instance_id: str = DEFAULT_INSTANCE_ID) -> dict[str, Any]:
        return dict(self._instances.get((scope, instance_id)) or {})

    def connector(self, scope: str, instance_id: str = DEFAULT_INSTANCE_ID) -> Any:
        return self.components(scope, instance_id).get(f"{scope}_connector")

    def connectors(self, scope: str | None = None) -> list[Any]:
        return [
            connector
            for (protocol, _instance_id), components in self._instances.items()
            if scope is None or protocol == scope
            if (connector := components.get(f"{protocol}_connector")) is not None
            and getattr(connector, "_ling_runtime_active", True) is not False
        ]

    def for_namespace(self, namespace: str, scope: str | None = None) -> Any:
        for (protocol, _instance_id), components in self._instances.items():
            if scope is not None and protocol != scope:
                continue
            connector = components.get(f"{protocol}_connector")
            if (connector is not None and connector.instance_id == namespace
                    and getattr(connector, "_ling_runtime_active", True) is not False):
                return connector
        return None

    def _publish(self, scope: str, instance_id: str, components: dict[str, Any]) -> None:
        connector = components.get(f"{scope}_connector")
        if connector is not None:
            connector._ling_runtime_active = True
            connector._ling_instance_key = (scope, instance_id)
        # 旧调用方通过这些属性访问默认实例。
        if instance_id == DEFAULT_INSTANCE_ID:
            names = {
                "onebot": ("onebot_connector", "onebot_ws", "onebot_ws_server"),
                "qqbot": ("qqbot_connector", "qqbot_gateway"),
            }.get(scope, ())
            for name in names:
                setattr(self._app.state, name, components.get(name))

    async def stop(self, scope: str, instance_id: str = DEFAULT_INSTANCE_ID) -> None:
        async with self._lock:
            key = (scope, instance_id)
            components = self.components(scope, instance_id)
            values = self.snapshot(scope, instance_id)
            try:
                await _stop_components(components, scope)
            except (Exception, asyncio.CancelledError) as exc:
                restored, instances = await self._rollback(scope, values, instance_id=instance_id)
                self._instances[key] = instances
                self._publish(scope, instance_id, instances)
                if not restored:
                    self._effective.pop(key, None)
                raise ConnectorServiceError(
                    "connector_stop_failed" if restored else "rollback_failed",
                    "实例未能停用，已恢复原连接" if restored else "实例停止与恢复失败，请检查运行日志",
                    status_code=503,
                ) from exc
            self._instances.pop(key, None)
            self._effective.pop(key, None)
            self._publish(scope, instance_id, {})

    async def shutdown(self) -> None:
        async with self._lock:
            for (scope, instance_id), components in list(self._instances.items()):
                try:
                    await _stop_components(components, scope)
                except (Exception, asyncio.CancelledError) as exc:
                    logger.error("接入器关闭失败 scope=%s instance=%s type=%s",
                                 scope, instance_id, type(exc).__name__)
                self._publish(scope, instance_id, {})
            self._instances.clear()
            self._effective.clear()

    def validate(self, scope: str, instance_id: str, values: dict[str, Any]) -> None:
        namespace_key = f"LING_{scope.upper()}_INSTANCE_ID"
        for (other_scope, other_id), other in self._effective.items():
            if (other_scope, other_id) == (scope, instance_id):
                continue
            other_namespace = other.get(f"LING_{other_scope.upper()}_INSTANCE_ID")
            if values.get(namespace_key) and values[namespace_key] == other_namespace:
                raise ConnectorServiceError(
                    "invalid_config", "账号命名空间已由另一个实例使用", field_name=namespace_key
                )
            if scope == other_scope == "onebot":
                port = values.get("LING_ONEBOT_WS_PORT")
                if port and str(port) == str(other.get("LING_ONEBOT_WS_PORT") or ""):
                    raise ConnectorServiceError(
                        "invalid_config", "监听端口已由另一个实例使用",
                        field_name="LING_ONEBOT_WS_PORT",
                    )

    async def apply(
        self, scope: str, *, values: dict[str, Any], operation_id: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> SwapOutcome:
        async with self._lock:
            self.validate(scope, instance_id, values)
            return await self._apply(scope, values=values, operation_id=operation_id,
                                     instance_id=instance_id)

    async def _apply(
        self, scope: str, *, values: dict[str, Any], operation_id: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> SwapOutcome:
        """把新配置应用到这个副本。**不抛异常**：失败以 ``ok=False`` 返回。"""
        started = time.monotonic()
        timings: dict[str, float] = {}

        # 白名单等"下一个请求生效"的 scope 没有需要重建的连接。
        # 但**仍然要记录新快照**：入站判断读的是它。
        key = (scope, instance_id)
        factory = self._plans.get(key)
        if factory is None and scope in self._factories:
            async def factory(candidate: dict[str, Any]) -> SwapPlan:
                return await self._factories[scope](instance_id, candidate)
            self._plans[key] = factory
        old_values = dict(self._effective.get((scope, instance_id)) or {})
        old_instances = dict(self._instances.get((scope, instance_id)) or {})

        if factory is None:
            self._effective[key] = dict(values)
            timings["listener_ready_at"] = round(time.monotonic() - started, 4)
            return SwapOutcome(ok=True, health="ok", timings=timings, stop_calls=0)

        stop_calls = 0
        timings["disconnect_started_at"] = round(time.monotonic() - started, 4)
        try:
            stop_calls = sum(
                callable(getattr(component, "stop", None) or getattr(component, "close", None))
                for component in old_instances.values()
            )
            await _stop_components(old_instances, scope)
            # 2) 构造并启动新的
            plan = await factory(dict(values))
            new_instances = await plan.start()
        except (Exception, asyncio.CancelledError) as exc:  # 切换取消也必须恢复并结清修订
            logger.warning(
                "接入器切换失败 scope=%s type=%s（开始回滚）", scope, type(exc).__name__
            )
            if old_values:
                rollback_ok, rollback_instances = await self._rollback(
                    scope, old_values, instance_id=instance_id
                )
            else:
                rollback_ok, rollback_instances = True, {}
            timings["listener_ready_at"] = round(time.monotonic() - started, 4)
            if not rollback_ok:
                self._instances.pop(key, None)
                self._effective.pop(key, None)
                self._publish(scope, instance_id, {})
                # 回退也失败：**不能报成功**，如实标 degraded
                return SwapOutcome(
                    ok=False,
                    health="degraded",
                    error_code="rollback_failed",
                    error_safe="恢复上一份配置也失败了，需要人工检查（见 api 日志）",
                    timings=timings,
                    stop_calls=stop_calls,
                )
            self._instances[key] = rollback_instances
            self._effective[key] = old_values
            self._publish(scope, instance_id, rollback_instances)
            return SwapOutcome(
                ok=False,
                health="degraded",
                error_code=_error_code_for(exc),
                error_safe="新配置没起起来，已经恢复上一份配置和连接",
                timings=timings,
                stop_calls=stop_calls,
            )

        # 3) 成功：替换引用（**在 start 之后**，失败时引用仍指向可用实例）
        self._instances[key] = new_instances
        self._publish(scope, instance_id, new_instances)
        self._effective[key] = dict(values)
        timings["listener_ready_at"] = round(time.monotonic() - started, 4)

        # 4) 远端握手：有界等待，超时只影响 health
        health = "ok"
        if plan.connected is not None:
            try:
                connected = await _wait_connected(
                    plan.connected, new_instances, timeout=REMOTE_CONNECT_WAIT_SECONDS
                )
            except asyncio.CancelledError:
                connected = False
            if connected:
                timings["remote_connected_at"] = round(time.monotonic() - started, 4)
            else:
                health = "degraded"
        return SwapOutcome(
            ok=True, health=health, timings=timings, stop_calls=stop_calls
        )

    async def _rollback(
        self, scope: str, old_values: dict[str, Any], *, instance_id: str
    ) -> tuple[bool, dict[str, Any]]:
        """用**旧配置**重建实例（旧对象已经 stop，不保证能再 start）。"""
        key = (scope, instance_id)
        factory = self._plans.get(key)
        if factory is None and scope in self._factories:
            async def factory(candidate: dict[str, Any]) -> SwapPlan:
                return await self._factories[scope](instance_id, candidate)
            self._plans[key] = factory
        if factory is None:
            return True, {}
        try:
            plan = await factory(dict(old_values))
            instances = await plan.start()
        except (Exception, asyncio.CancelledError) as exc:  # 回退失败要如实上报
            logger.error(
                "接入器回退失败 scope=%s type=%s —— 需要人工检查", scope, type(exc).__name__
            )
            return False, {}
        return True, instances


async def _stop_components(components: dict[str, Any], scope: str) -> None:
    """先拒绝新的收发，再逐个关闭组件，任一失败仍继续释放其它资源。"""
    connector = components.get(f"{scope}_connector")
    if connector is not None:
        connector._ling_runtime_active = False
    error: BaseException | None = None
    for component in components.values():
        stopper = getattr(component, "stop", None) or getattr(component, "close", None)
        if stopper is None:
            continue
        try:
            result = stopper()
            if hasattr(result, "__await__"):
                await result
        except (Exception, asyncio.CancelledError) as exc:
            error = error or exc
    if error is not None:
        raise error


def _error_code_for(exc: BaseException) -> str:
    """异常 → 稳定错误码（**不带异常原文**：它可能含 URL / Authorization）。"""
    name = type(exc).__name__.lower()
    if "auth" in name or "token" in name:
        return "connector_start_failed"
    if "bind" in name or "address" in name or "port" in name:
        return "bind_failed"
    return "connector_start_failed"


async def _wait_connected(
    probe: Callable[[dict[str, Any]], bool],
    instances: dict[str, Any],
    *,
    timeout: float,
) -> bool:
    """有界等待"远端已连"。返回 False 表示"还没连上"（不是失败）。"""
    import asyncio

    deadline = time.monotonic() + timeout
    while True:
        try:
            if probe(instances):
                return True
        except Exception:  # noqa: BLE001 - 探活不该影响切换结果
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(REMOTE_POLL_INTERVAL_SECONDS)


__all__ = [
    "REMOTE_CONNECT_WAIT_SECONDS",
    "ConnectorRuntime",
    "SwapPlan",
]
