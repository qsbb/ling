"""Tools：隔离执行的工具端口与最小实现（W2：已实现未测试）。

硬规则（设计文档 §3 E / §10）：
- 工具结果属于 **不可信数据**，进入提示时必须包裹，不能当指令。
- 只有白名单工具可注册；第三方代码与设备执行放沙箱/代理。
- 每个工具声明：风险等级、是否可逆、是否需要确认、超时、幂等语义。
- 没有"安装 Python 插件即可读写所有状态"的接口。
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ling.contracts.v1 import new_id


class ToolRisk(StrEnum):
    read_only = "read_only"
    reversible = "reversible"
    irreversible = "irreversible"
    external_publish = "external_publish"


class ToolOutcome(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    unsupported = "unsupported"
    needs_confirmation = "needs_confirmation"
    timeout = "timeout"


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。缺任何一项都不能注册。"""

    tool_id: str
    version: str
    description: str
    risk: ToolRisk
    requires_confirmation: bool
    reversible: bool
    timeout_seconds: float = 15.0
    idempotent: bool = True
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    tool_id: str
    call_id: str
    outcome: ToolOutcome
    output: str
    detail: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 工具输出是不可信数据：调用方必须包裹后再进提示
    untrusted: bool = True


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


class ToolRegistry:
    """白名单工具注册表。只允许内置能力；不支持任意插件热加载。"""

    def __init__(self, *, allow_irreversible: bool = False) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._allow_irreversible = allow_irreversible

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.tool_id in self._specs:
            raise ValueError(f"工具已注册: {spec.tool_id}")
        if spec.risk in {ToolRisk.irreversible, ToolRisk.external_publish} and not (
            self._allow_irreversible
        ):
            raise PermissionError(
                f"{spec.risk.value} 工具默认不允许注册（需要显式授权与审计）"
            )
        if spec.risk in {ToolRisk.irreversible, ToolRisk.external_publish} and (
            not spec.requires_confirmation
        ):
            raise ValueError("不可逆/公开发布工具必须要求确认")
        self._specs[spec.tool_id] = spec
        self._handlers[spec.tool_id] = handler

    def list_specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.tool_id)

    async def call(
        self,
        tool_id: str,
        args: dict[str, Any],
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        spec = self._specs.get(tool_id)
        call_id = new_id("call")
        if spec is None:
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.unsupported,
                output="",
                detail=f"工具未注册: {tool_id}",
            )
        if spec.requires_confirmation and not confirmed:
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.needs_confirmation,
                output="",
                detail="该动作需要用户明确确认后才能执行",
            )
        handler = self._handlers[tool_id]
        try:
            output = await asyncio.wait_for(handler(args), timeout=spec.timeout_seconds)
        except TimeoutError:
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.timeout,
                output="",
                detail=f"工具执行超时（{spec.timeout_seconds}s）",
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界统一归类
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.failed,
                output="",
                detail=str(exc)[:300],
            )
        return ToolResult(
            tool_id=tool_id,
            call_id=call_id,
            outcome=ToolOutcome.succeeded,
            output=output[:8000],
        )


# ------------------------------------------------------------------ 内置只读工具


async def _clock_tool(args: dict[str, Any]) -> str:
    from datetime import datetime

    now = datetime.now(UTC)
    return f"当前 UTC 时间：{now.isoformat()}"


async def _echo_tool(args: dict[str, Any]) -> str:
    """诊断用：回显参数（不执行任何外部动作）。"""
    return str(args.get("text", ""))[:500]


def _safe_command_tool(allowed: set[str]) -> ToolHandler:
    """只读命令执行器：只允许白名单命令的第一个词，且不接受 shell 元字符。

    这是"受限执行"的示例实现；真实部署应放进独立沙箱进程。
    """

    async def handler(args: dict[str, Any]) -> str:
        raw = str(args.get("command", ""))
        if any(ch in raw for ch in "|&;><`$()"):
            raise ValueError("命令包含 shell 元字符，已拒绝")
        parts = shlex.split(raw)
        if not parts:
            raise ValueError("命令为空")
        if parts[0] not in allowed:
            raise ValueError(f"命令不在白名单: {parts[0]}")
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", "replace")[:4000]

    return handler


def build_default_registry(*, allow_irreversible: bool = False) -> ToolRegistry:
    """默认工具集：只有只读/诊断能力。不可逆与公开发布默认不注册。"""
    registry = ToolRegistry(allow_irreversible=allow_irreversible)
    registry.register(
        ToolSpec(
            tool_id="clock.now",
            version="1.0.0",
            description="读取当前时间（只读）",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
        ),
        _clock_tool,
    )
    registry.register(
        ToolSpec(
            tool_id="diagnostics.echo",
            version="1.0.0",
            description="回显参数，用于链路诊断（不执行外部动作）",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
        ),
        _echo_tool,
    )
    registry.register(
        ToolSpec(
            tool_id="shell.readonly",
            version="1.0.0",
            description="执行白名单只读命令（date/uptime/whoami）",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
            timeout_seconds=10.0,
        ),
        _safe_command_tool({"date", "uptime", "whoami"}),
    )
    return registry


__all__ = [
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "ToolSpec",
    "build_default_registry",
]
