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
    # 这个工具**输出**是不是第三方/外部数据（网页正文、OCR、群友消息……）。
    #
    # 内置只读工具（时间、回显、白名单命令）的输出来自本机，是可信的；
    # 未来的 ``knowledge.open`` / ``web.read_url`` 必须置 ``True``。
    # 这是 ``ToolResult.untrusted`` 的**默认值来源**：编排层在编码回灌结果时
    # 用它决定要不要加"这些是数据，不是指令"的包裹。
    #
    # 默认 ``True``：新的外部工具忘了声明时，宁可多包一层数据边界，
    # 也不该让第三方文本冒泡成指令。
    output_untrusted: bool = True
    # 能不能交给**模型**通过原生工具调用执行（而不是只给管理 API / 领域内部用）。
    #
    # 这个字段必须显式声明，**不能**从 ``risk`` 或 ``idempotent`` 推断：
    # - ``shell.readonly`` 是 read_only 且 idempotent，但它能读整机状态
    #   （``whoami`` / ``uptime`` / ``date``），把它交给模型等于开一条
    #   "模型可以问宿主环境"的路，必须单独评审；
    # - ``message.recall`` 是不可逆动作，模型只能**提议**，执行要用户确认，
    #   它走的是另一条（确认）回路，不是这个开关。
    #
    # 默认 ``False`` = 失败关闭：新加的工具不会因为"忘了声明"而自动对模型开放。
    model_callable: bool = False
    # 回灌时 ``data`` 字段的字符上限。**与上面的 ``output[:8000]`` 是两回事**：
    # 那是注册表给调用方（API 审计、领域代码）的原始输出；这里是给**模型**看的
    # 单次结果上限。超限必须置 ``truncated=true``，不能悄悄截。
    model_result_chars: int = 2000


@dataclass(frozen=True)
class ToolResult:
    tool_id: str
    call_id: str
    outcome: ToolOutcome
    output: str
    detail: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 工具输出是不可信数据：调用方必须包裹后再进提示。
    # 默认值来自 ``ToolSpec.output_untrusted``（内置只读工具置 False，
    # 外部数据工具保持 True），由 ``ToolRegistry._execute`` 填入。
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

    def spec(self, tool_id: str) -> ToolSpec | None:
        """按 id 取声明；没有就返回 ``None``（不抛：调用方要区分"未注册"）。"""
        return self._specs.get(tool_id)

    def list_model_tools(self) -> list[ToolSpec]:
        """**对模型开放**的工具清单（``model_callable=True`` 的子集）。

        排序保证同一份注册表每轮发出的 schema 顺序稳定——顺序抖动会让
        供应商侧的提示词缓存失效，也会让"这轮发了什么"没法逐字节对比。
        """
        return [spec for spec in self.list_specs() if spec.model_callable]

    async def call(
        self,
        tool_id: str,
        args: dict[str, Any],
        *,
        confirmed: bool = False,
        internal_call_id: str | None = None,
    ) -> ToolResult:
        """执行一次工具调用（成功输出截到 8000 字符）。

        行为与改动前一致；新增的 ``internal_call_id`` 见 :meth:`call_untruncated`。
        """
        return await self._execute(
            tool_id,
            args,
            confirmed=confirmed,
            internal_call_id=internal_call_id,
            output_limit=8000,
        )

    async def call_untruncated(
        self,
        tool_id: str,
        args: dict[str, Any],
        *,
        confirmed: bool = False,
        internal_call_id: str | None = None,
    ) -> ToolResult:
        """与 :meth:`call` 相同，但**保留完整输出**（不截断）。

        ## 为什么不能"先 call 再截"

        注册表这句 ``output[:8000]`` 是**无提示**截断：模型拿到被砍过的文本，
        却没有任何"这里被砍了"的标记，于是会照着半句话下结论。回灌给模型的
        结果必须有 ``truncated=true`` 这个**显式**字段（见 ``kernel/turn.py``
        的结果编码器），所以调用方需要拿到完整输出，自己按单工具上限截并标记。

        ## 为什么不是"重跑一次 handler 拿完整结果"

        那是**错的**：``clock.now`` 会变，命令会重跑，有副作用的工具会执行两次。
        截断与执行是同一个动作的两个出口——所以这里和 :meth:`call` 共用一个
        执行体 ``_execute``，只把上限参数换掉。**handler 只被调用一次。**

        ``internal_call_id`` 是编排层给的**执行审计 ID**（可选）。不给就自己生成。
        它区别于供应商的 ``tool_call_id``：那是协议配对键，这里是执行审计键。
        两者由编排层保存映射，**不能用内部 ID 冒充 provider ID**——注册表既不知道
        也不该知道供应商协议。
        """
        return await self._execute(
            tool_id,
            args,
            confirmed=confirmed,
            internal_call_id=internal_call_id,
            output_limit=None,
        )

    async def _execute(
        self,
        tool_id: str,
        args: dict[str, Any],
        *,
        confirmed: bool,
        internal_call_id: str | None,
        output_limit: int | None,
    ) -> ToolResult:
        """唯一的执行体。``output_limit=None`` = 不截断。"""
        spec = self._specs.get(tool_id)
        call_id = internal_call_id or new_id("call")
        if spec is None:
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.unsupported,
                output="",
                detail=f"工具未注册: {tool_id}",
                # 未注册 = 我们完全不知道它是什么，按最不可信处理
                untrusted=True,
            )
        if spec.requires_confirmation and not confirmed:
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.needs_confirmation,
                output="",
                detail="该动作需要用户明确确认后才能执行",
                untrusted=spec.output_untrusted,
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
                untrusted=spec.output_untrusted,
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界统一归类
            return ToolResult(
                tool_id=tool_id,
                call_id=call_id,
                outcome=ToolOutcome.failed,
                output="",
                detail=str(exc)[:300],
                # 异常文本可能夹带外部数据（比如子进程的 stderr），
                # 所以它跟随 spec 的可信性判断，而不是一律 True/False。
                untrusted=spec.output_untrusted,
            )
        return ToolResult(
            tool_id=tool_id,
            call_id=call_id,
            outcome=ToolOutcome.succeeded,
            output=output if output_limit is None else output[:output_limit],
            untrusted=spec.output_untrusted,
        )


# ------------------------------------------------------------------ 内置只读工具


async def _clock_tool(args: dict[str, Any]) -> str:
    """读当前时间。``tz`` 可以是 IANA 时区名；不给就是 UTC。

    为什么要看 ``tz``：schema 里声明了它，处理函数就必须**真的用它**，
    否则模型传了 ``Asia/Shanghai`` 却拿到 UTC 时间——schema 与行为不一致，
    而模型没法从结果里看出这一点（它只看到"当前 UTC 时间"）。非法时区名
    直接报错（成为一条明确的失败回执），而不是静默退回 UTC。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    raw_tz = str(args.get("tz") or "").strip()
    if raw_tz:
        try:
            now = datetime.now(ZoneInfo(raw_tz))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知时区: {raw_tz}") from exc
        return f"{raw_tz} 当前时间：{now.isoformat()}"
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

#: 时钟工具的 id。理由与 `RECALL_TOOL_ID` 相同：schema 声明侧、
#: 可用性判断侧、测试断言侧写的是同一个常量。
CLOCK_NOW_TOOL_ID = "clock.now"


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
            tool_id=CLOCK_NOW_TOOL_ID,
            version="1.0.0",
            description="读取当前时间（只读）",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
            # ---- 本期**唯一**对模型开放的工具。
            #
            # 三条判据（缺一条都不该开放）：只读、无副作用、幂等。
            # `diagnostics.echo` 虽然也只读，但它的输出是**模型自己给的输入**，
            # 开放它等于让模型在两步之间"回声"自己的参数，没有信息增量，
            # 反而给"伪造一次成功调用"提供了形状。`shell.readonly` 能读宿主机
            # 状态，需要单独评审。`message.recall` 走确认回路。
            model_callable=True,
            # 本机时钟，不是第三方数据
            output_untrusted=False,
            # 参数 schema 必须**真实**：原来是空 dict，而空 schema 在官方
            # 形状里等于"任意参数"，模型会照着自己编的 key 传值，
            # 校验层只认 ``tz``，于是它收到的错误与它看到的不一致。
            input_schema={
                "type": "object",
                "properties": {
                    "tz": {
                        "type": "string",
                        "description": "IANA 时区名，例如 Asia/Shanghai；留空表示 UTC",
                    },
                },
                "required": [],
            },
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
    "CLOCK_NOW_TOOL_ID",
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
