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

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ling.settings.connector_service import SwapOutcome

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
    """按 scope 切换连接器实例，并把结果写回 ``app.state``。"""

    def __init__(self, app: object) -> None:
        self._app = app
        #: scope → 生成 SwapPlan 的工厂（``None`` 表示这个 scope 不重连，
        #: 例如白名单：它只在下一个请求生效，不需要动连接）。
        self._plans: dict[str, Callable[[dict[str, Any]], Awaitable[SwapPlan]]] = {}
        #: scope → 当前实例名 → 对象（供停止与回滚使用）
        self._instances: dict[str, dict[str, Any]] = {}
        #: scope → 构造参数快照（回滚时重建用）
        self._effective: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ 注册

    def register(
        self,
        scope: str,
        factory: Callable[[dict[str, Any]], Awaitable[SwapPlan]],
        *,
        instances: dict[str, Any] | None = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        self._plans[scope] = factory
        if instances is not None:
            self._instances[scope] = instances
        if values is not None:
            self._effective[scope] = values

    def snapshot(self, scope: str) -> dict[str, Any]:
        """当前已生效的构造参数（GET 视图与诊断用；**不含解密后的密钥副本**）。"""
        return dict(self._effective.get(scope) or {})

    # ------------------------------------------------------------ 切换

    async def apply(
        self, scope: str, *, values: dict[str, Any], operation_id: str
    ) -> SwapOutcome:
        """把新配置应用到这个副本。**不抛异常**：失败以 ``ok=False`` 返回。"""
        started = time.monotonic()
        timings: dict[str, float] = {}

        # 白名单等"下一个请求生效"的 scope 没有需要重建的连接。
        # 但**仍然要记录新快照**：入站判断读的是它。
        factory = self._plans.get(scope)
        old_values = dict(self._effective.get(scope) or {})
        old_instances = dict(self._instances.get(scope) or {})

        if factory is None:
            self._effective[scope] = dict(values)
            timings["listener_ready_at"] = round(time.monotonic() - started, 4)
            return SwapOutcome(ok=True, health="ok", timings=timings, stop_calls=0)

        stop_calls = 0
        timings["disconnect_started_at"] = round(time.monotonic() - started, 4)
        try:
            # 1) 停旧的：端口/连接必须释放。
            for name in old_instances:
                instance = old_instances.get(name)
                stopper = getattr(instance, "stop", None)
                if stopper is None:
                    continue
                stop_calls += 1
                result = stopper()
                if hasattr(result, "__await__"):
                    await result
            # 2) 构造并启动新的
            plan = await factory(dict(values))
            new_instances = await plan.start()
        except Exception as exc:  # noqa: BLE001 - 切换失败必须回滚，不能让异常穿出去
            logger.warning(
                "接入器切换失败 scope=%s type=%s（开始回滚）", scope, type(exc).__name__
            )
            rollback_ok, rollback_instances = await self._rollback(scope, old_values)
            timings["listener_ready_at"] = round(time.monotonic() - started, 4)
            if not rollback_ok:
                # 回退也失败：**不能报成功**，如实标 degraded
                return SwapOutcome(
                    ok=False,
                    health="degraded",
                    error_code="rollback_failed",
                    error_safe="恢复上一份配置也失败了，需要人工检查（见 api 日志）",
                    timings=timings,
                    stop_calls=stop_calls,
                )
            self._instances[scope] = rollback_instances
            self._effective[scope] = old_values
            return SwapOutcome(
                ok=False,
                health="degraded",
                error_code=_error_code_for(exc),
                error_safe="新配置没起起来，已经恢复上一份配置和连接",
                timings=timings,
                stop_calls=stop_calls,
            )

        # 3) 成功：替换引用（**在 start 之后**，失败时引用仍指向可用实例）
        self._instances[scope] = new_instances
        self._effective[scope] = dict(values)
        timings["listener_ready_at"] = round(time.monotonic() - started, 4)

        # 4) 远端握手：有界等待，超时只影响 health
        health = "ok"
        if plan.connected is not None:
            connected = await _wait_connected(
                plan.connected, new_instances, timeout=REMOTE_CONNECT_WAIT_SECONDS
            )
            if connected:
                timings["remote_connected_at"] = round(time.monotonic() - started, 4)
            else:
                health = "degraded"
        return SwapOutcome(
            ok=True, health=health, timings=timings, stop_calls=stop_calls
        )

    async def _rollback(
        self, scope: str, old_values: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """用**旧配置**重建实例（旧对象已经 stop，不保证能再 start）。"""
        factory = self._plans.get(scope)
        if factory is None:
            return True, {}
        try:
            plan = await factory(dict(old_values))
            instances = await plan.start()
        except Exception as exc:  # noqa: BLE001 - 回退失败要如实上报
            logger.error(
                "接入器回退失败 scope=%s type=%s —— 需要人工检查", scope, type(exc).__name__
            )
            return False, {}
        return True, instances


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
