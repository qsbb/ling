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

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        allow_irreversible: bool | None = None,
    ) -> None:
        """注册一个工具。

        ``allow_irreversible`` 是**逐次**的显式授权，默认 ``None`` = 用注册表
        级的开关（``ToolRegistry(allow_irreversible=...)``）。

        为什么需要它：注册表级的开关一开，**之后任何**不可逆工具都能注册进来；
        而真正要表达的是"这一个工具已经过评审、允许存在"。逐次的写法让授权
        落在注册那一行上——审计时一眼能看出"是谁批准了哪个不可逆工具"，
        而不是"某个地方开过总开关"。

        它**不是**绕过确认：下面那条"必须 requires_confirmation"仍然生效，
        权限硬门（`manage.call_tool` 的 PolicyGate）也照旧。
        """
        if spec.tool_id in self._specs:
            raise ValueError(f"工具已注册: {spec.tool_id}")
        authorized = self._allow_irreversible if allow_irreversible is None else allow_irreversible
        if spec.risk in {ToolRisk.irreversible, ToolRisk.external_publish} and not authorized:
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


# ------------------------------------------------------------------ 撤回（不可逆）


class RecallToolError(RuntimeError):
    """撤回工具拿不到执行条件（没会话、没连接器）。

    **不是**撤回失败：这类错误发生在发 action 之前，语义是"这次撤不了"。
    单独一个类型，是为了让 handler 能把它翻译成人话而不是抛栈。
    """


async def _recall_tool(args: dict[str, Any]) -> str:
    """``message.recall`` 的处理器：撤回**她自己**的一条消息。

    ## 这个 handler 只有一条执行路径

    它**不自己判**归属/已送达/时间窗/频次/对象，而是直接调
    :func:`ling.domains.conversation.recall.resolve_recall_target` +
    :func:`perform_recall` —— 也就是 ``POST /v1/sessions/{id}/messages/{mid}/recall``
    背后那套五道闸。判据只有一份，手端点与模型提议点走同一条。抄一份过来
    迟早会走散（而"模型那条路判松了"正是最难发现的那种错）。

    ## 为什么它默认根本不会被调用

    ``ToolSpec.requires_confirmation=True``：``ToolRegistry.call(confirmed=False)``
    在**进这个函数之前**就返回 ``needs_confirmation`` 了。模型只能提议，
    执行要用户确认。这条防线在注册表里，不靠 handler 自觉。

    ## 参数

    ``message_id``（必需）：本地消息 id，由调用方把标记短号解析好再传进来。
    ``window_id``（必需）：会话 id —— 撤回必须限定在某个窗口内，
    不接受"随便哪条她说的"（那会跨会话撤）。
    ``connector``（必需，非 JSON）：连接器实例。由调用方注入，**不从参数里读**：
    参数来自模型/请求体，让它们指定连接器等于让调用方决定"发到哪个平台"。
    """
    from ling.domains.conversation.recall import (
        RecallRejected,
        perform_recall,
        resolve_recall_target,
    )

    window_id = str(args.get("window_id") or "").strip()
    message_id = str(args.get("message_id") or "").strip()
    connector = args.get("connector")
    if not window_id:
        raise RecallToolError("不知道在哪个会话里撤回")
    if not message_id:
        raise RecallToolError("没有说要撤回哪一条")
    if connector is None:
        # 纯 Web 会话没有连接器：平台上没有可撤的东西。
        # 这条**不算失败**——如实说"这个接入方式不支持撤回"。
        return "这个接入方式不支持撤回（平台上没有这条消息）"

    session = args.get("session")
    if session is None:
        raise RecallToolError("撤回需要数据库会话")

    try:
        target = await resolve_recall_target(
            session, window_id=window_id, message_id=message_id
        )
    except RecallRejected as rejected:
        # 五道闸拒了：**平台一次 action 都没收到**。人话直出，不带原因码。
        raise RecallToolError(rejected.note) from rejected

    outcome = await perform_recall(session, connector=connector, target=target)
    # ``outcome.note`` 已经是人话（`RECALL_RESULT_NOTES`），
    # 且 ``uncertain`` 说的是"没拿到回执，不确定是否撤回成功"——**不是成功**。
    return outcome.note


#: 撤回工具的 id。常量而不是字面量：解析标记那一侧（`kernel/turn.py`）与
#: 注册这一侧必须写同一个字符串，两处各打一遍字迟早会有一个打错，而"打错了"
#: 的表现是"模型提议了但什么都没发生"——最难查的那种。
RECALL_TOOL_ID = "message.recall"


def build_default_registry(
    *, allow_irreversible: bool = False, include_recall: bool = True
) -> ToolRegistry:
    """默认工具集：只读/诊断 + 撤回（不可逆，需确认）。

    ## 撤回为什么要进默认集

    用户拍板「撤回消息只做注册工具开放给 llm」。它**必须**进默认集，否则
    ``/v1/tools`` 里看不到它、模型侧也就没有可提议的动作。

    但它和只读工具不是一类东西，所以：
    - ``risk=irreversible``：撤回在平台上不可撤销（撤了就是撤了）；
    - ``requires_confirmation=True``：**这是模型不能直接执行的唯一原因**，
      见 `ToolRegistry.call` —— ``confirmed=False`` 时它连 handler 都不进；
    - ``reversible=False`` / ``idempotent=True``：重复撤同一条是幂等的
      （第二次会因为 ``recalled_at`` 非空被拒），但撤了不能反悔。

    ``include_recall=False`` 是给"明确不要这个能力"的部署留的开关；默认开。
    """
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
    if include_recall:
        # 撤回是**不可逆**工具，注册表默认拒绝它（见 `ToolRegistry.register`）。
        # 这里把 allow_irreversible 显式打开一次——不是绕过闸门，而是"这个具体
        # 工具已经过评审、确认可以存在"：它仍然要求确认、仍然过权限硬门
        # （`manage.call_tool` 的 PolicyGate）、仍然走完整五道闸。
        registry.register(
            ToolSpec(
                tool_id=RECALL_TOOL_ID,
                version="1.0.0",
                description=(
                    "撤回她自己发出去的一条消息。不可逆，必须由用户确认后才执行；"
                    "只在她自己的发言、有真实送达回执、且在平台撤回时间窗内时可用"
                ),
                risk=ToolRisk.irreversible,
                requires_confirmation=True,
                reversible=False,
                # 撤回是"发一条平台 action"，比只读命令慢，但远快于 15 秒。
                timeout_seconds=20.0,
                idempotent=True,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "string",
                            "description": "要撤回的那条消息的 id（上下文里 [recall:…] 标记给的那个）",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选：为什么要撤回（只用于审计，不发给平台）",
                            "maxLength": 200,
                        },
                    },
                    "required": ["message_id"],
                },
            ),
            _recall_tool,
            allow_irreversible=True,
        )
    return registry


__all__ = [
    "RECALL_TOOL_ID",
    "RecallToolError",
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "ToolSpec",
    "build_default_registry",
]
