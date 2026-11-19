"""Anthropic Messages API 适配器（``POST {base}/messages``）。

## 为什么不能复用 OpenAI 兼容适配器

看起来都是"发 messages、流式收文本"，但协议层有四处不同，任何一处错了都
不是报错而是**静默错**：

1. **system 不在 messages 里**：Anthropic 把它放在顶层的 ``system`` 字段。
   混进 messages 会被判非法请求（或某些代理直接丢弃，于是人设失效）。
2. **SSE 事件名不同**：文本增量在 ``content_block_delta`` 的
   ``delta.text``，结束在 ``message_stop``，错误是 ``error`` 事件——
   不是 ``data: {"choices": ...}``。
3. **图片是 base64 块**：``{"type": "image", "source": {"type": "base64",
   "media_type", "data"}}``，不接受 URL。
4. **用量字段名**：``usage.input_tokens`` / ``output_tokens``。

把这几处差异塞进兼容适配器里，会让那个类变成一个"看 base_url 猜协议"的
分叉器——那正是最难维护的形状。所以单独一份。
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


class AnthropicProvider:
    provider_id: str
    kind: ProviderKind = ProviderKind.anthropic

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
        headers = {
            "Content-Type": "application/json",
            # 这个头是**必需的**：缺了它会返回 400 而不是 401，
            # 用户看到的是"请求格式错误"，方向完全错。
            "anthropic-version": "2023-06-01",
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
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
        system_parts: list[str] = []
        turns: list[dict[str, Any]] = []
        for item, results in tool_message_groups(
            request.messages, enabled=self.supports_native_tools
        ):
            if item.role == "system":
                system_parts.append(item.content)
            elif item.tool_calls:
                replay = scoped_tool_replay(
                    item.tool_calls,
                    kind=self.kind,
                    provider_id=self.provider_id,
                    model_id=request.model_id,
                )
                turns.extend(
                    (
                        {"role": "assistant", "content": _encode_call_batch(item, replay)},
                        # 所有并行结果必须在紧随其后的同一条 user 消息里。
                        {
                            "role": "user",
                            "content": [_encode_tool_result(result) for result in results],
                        },
                    )
                )
            else:
                turns.append(_encode_turn(item))

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": turns,
            # Anthropic **要求** max_tokens 必填。不传会 400，而不是"不限制"。
            # 所以这里必须给一个值：用户设了上限就用它，没设就用一个足够大
            # 但仍是显式数字的值（4096），并在注释里说明这不是"不限制"。
            "max_tokens": request.max_tokens or 4096,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        if use_tools:
            payload["tools"] = [
                {
                    "name": wire_tool_name(tool.name),
                    "description": tool.description,
                    "input_schema": tool_schema(tool),
                    **({"strict": True} if tool.strict else {}),
                }
                for tool in request.tools
            ]
            if request.tool_choice is not None or request.parallel_tool_calls is False:
                choice = request.tool_choice or "auto"
                payload["tool_choice"] = {"type": "any" if choice == "required" else choice}
                if choice != "none" and request.parallel_tool_calls is False:
                    payload["tool_choice"]["disable_parallel_tool_use"] = True

        url = f"{self._base_url}/messages"
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
                blocks = _ToolBlocks() if use_tools else None
                stop: str | None = None
                prompt = completion = 0
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        parsed = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    event = parsed.get("type")
                    if blocks is not None:
                        blocks.feed(event, parsed)
                    if event == "content_block_start":
                        block = parsed.get("content_block") or {}
                        if block.get("type") == "text" and block.get("text"):
                            yield ModelChunk(delta=block["text"])
                    elif event == "content_block_delta":
                        delta = parsed.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield ModelChunk(delta=str(delta["text"]))
                    elif event in {"message_start", "message_delta"}:
                        envelope = (
                            parsed.get("message") or {} if event == "message_start" else parsed
                        )
                        usage = envelope.get("usage") or {}
                        if usage:
                            prompt = int(usage.get("input_tokens", prompt) or 0)
                            completion = int(usage.get("output_tokens", completion) or 0)
                            self.last_usage = Usage(
                                prompt_tokens=prompt,
                                completion_tokens=completion,
                                total_tokens=prompt + completion,
                            )
                        if event == "message_delta":
                            stop = (parsed.get("delta") or {}).get("stop_reason") or stop
                    elif event == "message_stop":
                        if blocks is not None and (stop == "tool_use" or blocks.has_tool_calls):
                            if stop != "tool_use" or request.tool_choice == "none":
                                raise tool_protocol_error(
                                    "Anthropic 工具步骤未正常结束或调用未获许可"
                                )
                            for call in blocks.complete(self.provider_id, request.model_id):
                                yield ModelChunk(
                                    kind=ModelChunkKind.tool_call,
                                    tool_call=call,
                                    provider_call_id=call.provider_call_id,
                                    tool_call_complete=True,
                                )
                            stop = "tool_calls"
                        yield ModelChunk(done=True, finish_reason=stop or "stop")
                        return
                    elif event == "error":
                        detail = (parsed.get("error") or {}).get("message") or "未知错误"
                        raise ModelError(
                            ModelErrorKind.error,
                            f"供应商 {self.provider_id} 返回错误: {detail}",
                            retriable=False,
                        )
                if use_tools:
                    raise tool_protocol_error("Anthropic 流在工具步骤结束前断开")
                if stop:
                    yield ModelChunk(done=True, finish_reason=stop)
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
        """连接检查：拉 ``/models``（Anthropic 兼容端点提供）。

        Anthropic 官方 API **没有** ``/models`` 端点，会返回 404。那种情况下
        退化成"端点可达 + 鉴权头已发出"，并在 detail 里明说这一点——不要
        假装拉到了模型清单，也不要因此判成失败。
        """
        if not self._api_key:
            return ProbeResult(ok=False, detail=f"供应商 {self.provider_id} 还没有保存密钥")
        url = f"{self._base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, detail=f"连接失败: {exc}")
        if response.status_code == 401:
            return ProbeResult(ok=False, detail="鉴权失败：密钥无效或权限不足")
        if response.status_code == 404:
            return ProbeResult(
                ok=True,
                detail="端点可达；该接口没有 /models，模型 ID 请手动填写",
                models=[],
            )
        if response.status_code >= 400:
            return ProbeResult(
                ok=False,
                detail=f"端点返回 HTTP {response.status_code}: {response.text[:200]}",
            )
        models: list[str] = []
        try:
            data = response.json()
            for item in data.get("data", []):
                mid = item.get("id")
                if mid:
                    models.append(str(mid))
        except (json.JSONDecodeError, AttributeError):
            pass
        return ProbeResult(ok=True, detail="端点可达且鉴权通过", models=models[:50])


class _ToolBlocks:
    """Collect official indexed content blocks until the whole step commits."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.arguments: dict[int, list[str]] = {}
        self.closed: set[int] = set()

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.arguments)

    def feed(self, event: str | None, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        if not isinstance(index, int):
            return
        if event == "content_block_start":
            block = payload.get("content_block") or {}
            block_type = block.get("type")
            if block_type in {"tool_use", "thinking", "redacted_thinking", "text"}:
                self.blocks[index] = dict(block)
            if block_type == "tool_use":
                self.arguments[index] = []
        elif event == "content_block_delta" and index in self.blocks:
            block = self.blocks[index]
            delta = payload.get("delta") or {}
            delta_type = str(delta.get("type") or "")
            if block["type"] == "tool_use" and delta_type == "input_json_delta":
                self.arguments[index].append(delta.get("partial_json") or "")
            else:
                field = {
                    ("text", "text_delta"): "text",
                    ("thinking", "thinking_delta"): "thinking",
                    ("thinking", "signature_delta"): "signature",
                }.get((block["type"], delta_type))
                if field is not None:
                    block[field] = (block.get(field) or "") + (delta.get(field) or "")
        elif event == "content_block_stop":
            self.closed.add(index)

    def complete(self, provider_id: str, model_id: str) -> list[ToolCall]:
        if not self.arguments or not self.closed.issuperset(self.blocks):
            raise tool_protocol_error("Anthropic 缺少完整的工具调用内容块")
        items = sorted(self.blocks.items())
        replay = ProviderToolReplay(
            ProviderKind.anthropic, provider_id, model_id, tuple(block for _, block in items)
        )
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for index, block in items:
            if block["type"] != "tool_use":
                continue
            call_id, name = block.get("id"), block.get("name")
            arguments = "".join(self.arguments[index])
            if not arguments and isinstance(block.get("input"), dict):
                arguments = json.dumps(block["input"], ensure_ascii=False)
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or call_id in seen
                or not isinstance(name, str)
                or not name
            ):
                raise tool_protocol_error("Anthropic 返回了无法回灌的工具调用")
            # Anthropic 的 tool_use.input 必须是对象，不能原样回灌破损的 JSON。
            _parse_tool_input(arguments)
            seen.add(call_id)
            calls.append(ToolCall(call_id, internal_tool_name(name), arguments, index, replay))
        return calls


def _parse_tool_input(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise tool_protocol_error("Anthropic 工具调用参数不是有效的 JSON 对象") from exc
    if not isinstance(value, dict):
        raise tool_protocol_error("Anthropic 工具调用参数不是有效的 JSON 对象")
    return value


def _encode_call_batch(
    message: ChatMessageIn,
    replay: ProviderToolReplay | None,
) -> list[dict[str, Any]]:
    calls = {
        call.provider_call_id: {
            "type": "tool_use",
            "id": call.provider_call_id,
            "name": wire_tool_name(call.name),
            "input": _parse_tool_input(call.arguments or "{}"),
        }
        for call in message.tool_calls
    }
    if replay is None:
        encoded = _encode_turn(message)["content"]
        blocks = (
            ([{"type": "text", "text": encoded}] if encoded else [])
            if isinstance(encoded, str)
            else encoded
        )
        return [*blocks, *calls.values()]
    blocks = []
    for block in replay.items:
        if block["type"] == "tool_use":
            call = calls.pop(block["id"], None)
            if call is not None:
                blocks.append(call)
        else:
            blocks.append(block)
    return [*blocks, *calls.values()]


def _encode_tool_result(result: ChatMessageIn) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": result.provider_call_id,
        "content": result.content,
    }
    try:
        payload = json.loads(result.content)
    except json.JSONDecodeError:
        return block
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        block["is_error"] = True
    return block


def _encode_turn(message: ChatMessageIn) -> dict[str, Any]:
    """一条 user/assistant 消息 → Anthropic 的 content 块数组。

    纯文本用字符串形式（Anthropic 两种都收，字符串更短）；带图才切成块数组。
    """
    if getattr(message, "videos", ()):
        # Anthropic 的 Messages API 收 image / document，不收视频。
        # 明确报错让调用方退回抽帧；静默丢弃会让模型"什么都没看到"却照常作答。
        raise ModelError(
            ModelErrorKind.unsupported,
            "Anthropic 接口不支持原生视频输入",
            retriable=False,
        )
    if not message.images:
        return {"role": message.role, "content": message.content}
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for image in message.images:
        parsed = _split_data_url(image.url)
        if parsed is None:
            # Anthropic 不收 URL 图片。明确说清，而不是丢图让她"看不见"
            # 却照常回答——那会让她凭空描述一张自己没看到的图。
            raise ModelError(
                ModelErrorKind.unsupported,
                "Anthropic 接口只接受 base64 图片；这条图片是 URL，无法发送",
                retriable=False,
            )
        media_type, data = parsed
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        )
    return {"role": message.role, "content": blocks}


def _split_data_url(url: str) -> tuple[str, str] | None:
    """``data:image/png;base64,xxxx`` → (``image/png``, ``xxxx``)。"""
    if not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    if ";base64" not in header:
        return None
    media_type = header[5:].split(";")[0] or "image/png"
    return media_type, data


__all__ = ["AnthropicProvider"]
