"""Native adapter helpers. Tool policy and execution stay in the kernel."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from ling.models.openai_compatible import _TOOL_IDS_BY_WIRE_NAME, _TOOL_WIRE_NAMES
from ling.ports.model import (
    ChatMessageIn,
    ModelError,
    ModelErrorKind,
    ProviderKind,
    ProviderToolReplay,
    ToolCall,
    ToolDefinition,
)


def wire_tool_name(tool_id: str) -> str:
    return _TOOL_WIRE_NAMES.get(tool_id, tool_id)


def internal_tool_name(wire_name: str) -> str:
    return _TOOL_IDS_BY_WIRE_NAME.get(wire_name, wire_name)


def tool_schema(tool: ToolDefinition) -> dict[str, Any]:
    return {"properties": {}, **tool.parameters, "type": "object"}


def tool_protocol_error(detail: str) -> ModelError:
    return ModelError(ModelErrorKind.error, detail, retriable=False)


def tool_message_groups(
    messages: Sequence[ChatMessageIn], *, enabled: bool
) -> Iterator[tuple[ChatMessageIn, list[ChatMessageIn]]]:
    """Group each assistant proposal with the immediately following results.

    A bare tool result is not user text, and a missing/foreign result must not
    be manufactured or forwarded. Results are left in their supplied order.
    """
    i = 0
    while i < len(messages):
        message = messages[i]
        i += 1
        if message.is_tool_result():
            raise tool_protocol_error("工具结果缺少对应的助手调用")
        if not message.tool_calls:
            yield message, []
            continue
        if not enabled:
            raise ModelError(
                ModelErrorKind.unsupported, "该端点未启用原生工具回灌", retriable=False
            )
        if message.role != "assistant":
            raise tool_protocol_error("工具调用必须来自助手消息")
        results: list[ChatMessageIn] = []
        while i < len(messages) and messages[i].is_tool_result():
            results.append(messages[i])
            i += 1
        ids = {call.provider_call_id for call in message.tool_calls}
        if (
            len(ids) != len(message.tool_calls)
            or not all(call_id.strip() for call_id in ids)
            or len(results) != len(ids)
            or {result.provider_call_id for result in results} != ids
        ):
            raise tool_protocol_error("工具回灌 ID 必须与同批助手调用一一对应")
        yield message, results


def scoped_tool_replay(
    calls: tuple[ToolCall, ...], *, kind: ProviderKind, provider_id: str, model_id: str
) -> ProviderToolReplay | None:
    """Replay opaque provider context only to the original model target."""
    replay = next((call.provider_replay for call in calls if call.provider_replay), None)
    if replay is not None and (replay.kind, replay.provider_id, replay.model_id) != (
        kind,
        provider_id,
        model_id,
    ):
        raise ModelError(
            ModelErrorKind.unsupported, "不能跨模型目标回灌原生工具上下文", retriable=False
        )
    return replay
