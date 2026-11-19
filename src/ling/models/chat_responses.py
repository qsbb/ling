"""OpenAI Responses API 适配器（``POST {base}/responses``）。

## 与 ``/chat/completions`` 的差别（都是静默错，不会报错）

1. **输入结构**：``input`` 是一个 item 数组，每条是
   ``{"role", "content": ...}``，
   不是 ``messages`` + 纯字符串 content。
2. **SSE 事件**：文本增量在 ``response.output_text.delta`` 的 ``delta``；
   结束在 ``response.completed``；错误在 ``response.failed`` 或 ``error``。
3. **图片**：``{"type": "input_image", "image_url": "<url 或 data URL>"}``。
4. **用量**：``response.usage.input_tokens`` / ``output_tokens``，
   在 ``response.completed`` 事件里。

所以它必须是独立适配器。把"事件名不同"当成"小差异"塞进兼容适配器，
结果是流式永远拿不到文本、只有最后一帧空回复——而日志里一切正常。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ling.models.native_tools import (
    internal_tool_name,
    scoped_tool_replay,
    tool_message_groups,
    tool_protocol_error,
    tool_schema,
    wire_tool_name,
)
from ling.ports.model import (
    ChatMessageIn,
    ModelChunk,
    ModelChunkKind,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ProbeResult,
    ProviderKind,
    ProviderToolReplay,
    ToolCall,
    Usage,
)


class ChatResponsesProvider:
    provider_id: str
    kind: ProviderKind = ProviderKind.chat_responses

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = 60.0,
        secret_hint: Callable[[str], str] | None = None,
        native_tools: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._secret_hint = secret_hint
        self.supports_native_tools = native_tools is True
        self.last_usage: Usage | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if not self._api_key:
            hint = self._secret_hint(self.provider_id) if self._secret_hint else ""
            if hint == "undecryptable":
                message = (
                    f"供应商 {self.provider_id} 的密钥无法解密："
                    "根密钥不匹配（数据目录是否变过？检查 root.key），或需要重新填写密钥"
                )
            else:
                message = f"供应商 {self.provider_id} 还没有保存密钥"
            raise ModelError(ModelErrorKind.missing_secret, message, retriable=False)

        self.last_usage = None
        use_tools = self.supports_native_tools and bool(request.tools)
        has_tool_history = False
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for item, results in tool_message_groups(
            request.messages, enabled=self.supports_native_tools
        ):
            if item.role == "system":
                instructions.append(item.content)
            elif item.tool_calls:
                replay = scoped_tool_replay(
                    item.tool_calls,
                    kind=self.kind,
                    provider_id=self.provider_id,
                    model_id=request.model_id,
                )
                items.extend(_encode_call_batch(item, replay))
                items.extend(
                    {
                        "type": "function_call_output",
                        "call_id": result.provider_call_id,
                        "output": result.content,
                    }
                    for result in results
                )
                has_tool_history = True
            else:
                items.append(_encode_item(item))

        payload: dict[str, Any] = {
            "model": request.model_id,
            "input": items,
            "stream": True,
        }
        if instructions:
            # Responses API 用顶层 ``instructions`` 装系统提示，
            # 不放进 input 里（放进去会被当成用户内容，人设就"降级成聊天对象"）。
            payload["instructions"] = "\n\n".join(instructions)
        if request.max_tokens:
            payload["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        if use_tools or has_tool_history:
            # 无状态回灌，不在供应商保存会话；reasoning 模型需要原始加密上下文。
            payload["store"] = False
            payload["include"] = ["reasoning.encrypted_content"]
        if use_tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": wire_tool_name(tool.name),
                    "description": tool.description,
                    "parameters": tool_schema(tool),
                    # Responses 省略 strict 时会收紧 schema，改变可选参数的语义。
                    "strict": tool.strict,
                }
                for tool in request.tools
            ]
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
            if request.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = request.parallel_tool_calls

        url = f"{self._base_url}/responses"
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream("POST", url, headers=self._headers(), json=payload) as response,
            ):
                if response.status_code == 401:
                    raise ModelError(
                        ModelErrorKind.auth,
                        f"供应商 {self.provider_id} 鉴权失败（密钥无效或权限不足）",
                        retriable=False,
                    )
                if response.status_code == 429:
                    raise ModelError(
                        ModelErrorKind.unavailable,
                        f"供应商 {self.provider_id} 限流，请稍后重试",
                        retriable=True,
                    )
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise ModelError(
                        ModelErrorKind.error,
                        f"供应商返回 HTTP {response.status_code}: {body}",
                        retriable=response.status_code >= 500,
                    )
                # 官方 completed 带最终 output。兼容只在 output_item.done 给出
                # 完整 item 的端点，但仍然要求整步结束；绝不执行未完成的参数分片。
                finished_items: dict[int, dict[str, Any]] = {}
                started_calls: set[int] = set()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    event = parsed.get("type")
                    if event == "response.output_text.delta":
                        delta = parsed.get("delta")
                        if delta:
                            yield ModelChunk(delta=str(delta))
                    elif use_tools and event in {
                        "response.output_item.added",
                        "response.output_item.done",
                    }:
                        index = parsed.get("output_index")
                        event_item = parsed.get("item") or {}
                        if isinstance(index, int) and isinstance(event_item, dict):
                            if event == "response.output_item.done":
                                finished_items[index] = event_item
                            if event_item.get("type") == "function_call":
                                started_calls.add(index)
                    elif event == "response.completed":
                        completed = parsed.get("response") or {}
                        usage = completed.get("usage") or {}
                        if usage:
                            prompt = int(usage.get("input_tokens", 0) or 0)
                            completion = int(usage.get("output_tokens", 0) or 0)
                            self.last_usage = Usage(
                                prompt_tokens=prompt,
                                completion_tokens=completion,
                                total_tokens=prompt + completion,
                            )
                        calls: list[ToolCall] = []
                        if use_tools:
                            output = completed.get("output")
                            indexed = (
                                dict(enumerate(output))
                                if isinstance(output, list)
                                else finished_items
                            )
                            calls = _completed_calls(
                                indexed,
                                started_calls,
                                provider_id=self.provider_id,
                                model_id=request.model_id,
                            )
                            if calls and request.tool_choice == "none":
                                raise tool_protocol_error("Responses 在禁止工具调用时返回了调用")
                            for call in calls:
                                yield ModelChunk(
                                    kind=ModelChunkKind.tool_call,
                                    tool_call=call,
                                    provider_call_id=call.provider_call_id,
                                    tool_call_complete=True,
                                )
                        yield ModelChunk(done=True, finish_reason="tool_calls" if calls else "stop")
                        return
                    elif event == "response.incomplete":
                        if started_calls:
                            raise tool_protocol_error("Responses 工具步骤不完整，未提交调用")
                        reason = (parsed.get("response") or {}).get("incomplete_details") or {}
                        yield ModelChunk(
                            done=True, finish_reason=str(reason.get("reason") or "incomplete")
                        )
                        return
                    elif event in {"response.failed", "error"}:
                        detail = (
                            ((parsed.get("response") or {}).get("error") or {}).get("message")
                            or parsed.get("message")
                            or parsed.get("code")
                            or (parsed.get("error") or {}).get("message")
                            or (parsed.get("error") or {}).get("code")
                            or "未知错误"
                        )
                        raise ModelError(
                            ModelErrorKind.error,
                            f"供应商 {self.provider_id} 返回错误: {detail}",
                            retriable=False,
                        )
                if use_tools:
                    raise tool_protocol_error("Responses 流在工具步骤结束前断开")
        except httpx.TimeoutException as exc:
            raise ModelError(
                ModelErrorKind.timeout, f"供应商 {self.provider_id} 响应超时", retriable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                ModelErrorKind.unavailable,
                f"无法连接供应商 {self.provider_id}: {exc}",
                retriable=True,
            ) from exc

    async def probe(self) -> ProbeResult:
        """连接检查：Responses API 没有 /models，用一次极小请求验证鉴权。

        比"拉模型清单"更可靠：它验证的正是对话会走的那条路径，而且花的是
        几个 token，不是整轮对话的钱。
        """
        if not self._api_key:
            return ProbeResult(ok=False, detail=f"供应商 {self.provider_id} 还没有保存密钥")
        url = f"{self._base_url}/responses"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json={
                        "model": "probe",
                        "input": "ping",
                        "max_output_tokens": 1,
                        "stream": False,
                    },
                )
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, detail=f"连接失败: {exc}")
        if response.status_code == 401:
            return ProbeResult(ok=False, detail="鉴权失败：密钥无效或权限不足")
        if response.status_code in {400, 404}:
            # 400 常见于"probe 不是这个账号可用的模型 ID"——那说明鉴权
            # 已经过了（未鉴权会先 401），端点也在。
            return ProbeResult(
                ok=True,
                detail="端点可达且鉴权通过；模型 ID 请手动填写",
                models=[],
            )
        if response.status_code >= 400:
            return ProbeResult(
                ok=False,
                detail=f"端点返回 HTTP {response.status_code}: {response.text[:200]}",
            )
        return ProbeResult(ok=True, detail="端点可达且鉴权通过", models=[])


def _completed_calls(
    indexed: dict[int, Any], started_calls: set[int], *, provider_id: str, model_id: str
) -> list[ToolCall]:
    replay_items: list[dict[str, Any]] = []
    call_items: dict[int, dict[str, Any]] = {}
    for index, item in sorted(indexed.items()):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"reasoning", "message", "function_call"}:
            replay_items.append(item)
        if item_type == "function_call":
            call_items[index] = item
    if not started_calls.issubset(call_items):
        raise tool_protocol_error("Responses 缺少已提出的工具调用结束项")
    replay = ProviderToolReplay(
        ProviderKind.chat_responses, provider_id, model_id, tuple(replay_items)
    )
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for index, item in call_items.items():
        call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
        if (
            item.get("status") not in {None, "completed"}
            or not isinstance(call_id, str)
            or not call_id.strip()
            or call_id in seen
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
            or not arguments.strip()
        ):
            raise tool_protocol_error("Responses 返回了无法回灌的工具调用")
        seen.add(call_id)
        calls.append(ToolCall(call_id, internal_tool_name(name), arguments, index, replay))
    return calls


def _encode_call_batch(
    message: ChatMessageIn,
    replay: ProviderToolReplay | None,
) -> list[dict[str, Any]]:
    calls = {
        call.provider_call_id: {
            "type": "function_call",
            "call_id": call.provider_call_id,
            "name": wire_tool_name(call.name),
            "arguments": call.arguments,
        }
        for call in message.tool_calls
    }
    if replay is None:
        text = (
            [_encode_item(message)] if message.content or message.images or message.videos else []
        )
        return [*text, *calls.values()]
    items = []
    for item in replay.items:
        if item.get("type") == "function_call":
            # 不因回放原始上下文而重新带上未被编排层接纳的调用。
            call = calls.pop(item["call_id"], None)
            if call is not None:
                items.append({**item, **call})
        else:
            items.append(item)
    return [*items, *calls.values()]


def _encode_item(message: ChatMessageIn) -> dict[str, Any]:
    if getattr(message, "videos", ()):
        raise ModelError(
            ModelErrorKind.unsupported,
            "Responses 接口不支持原生视频输入",
            retriable=False,
        )
    blocks: list[dict[str, Any]] = []
    if message.role == "assistant" and not message.images:
        return {"role": "assistant", "content": message.content}
    if message.content:
        blocks.append({"type": "input_text", "text": message.content})
    for image in message.images:
        blocks.append({"type": "input_image", "image_url": image.url, "detail": "auto"})
    if not blocks:
        blocks.append({"type": "input_text", "text": ""})
    return {"role": message.role, "content": blocks}


__all__ = ["ChatResponsesProvider"]
