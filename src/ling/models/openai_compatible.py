"""OpenAI 兼容接口适配器（真实供应商路径）。

支持任何暴露 ``/chat/completions`` 的服务：OpenAI、DeepSeek、Moonshot、vLLM、Ollama
的 OpenAI 兼容端点等。**能力必须逐型号探测**：这里只声明协议层的流式与用量解析，
不保证对方支持工具、结构化输出、视觉或音频。

## 工具回路（本期只做 Chat Completions 这一种 wire）

请求侧按官方形状发 ``tools`` / ``tool_choice`` / ``parallel_tool_calls``；
回灌时先回放 assistant 的 ``tool_calls``，再逐条发
``{"role":"tool","tool_call_id":<provider_call_id>,"content":...}``。

响应侧的流式形状是：``choices[0].delta.tool_calls[i]`` 是**对象数组**，
每个分片只带部分字段——``index`` 首先出现（它是装配键），``id``/``type``
通常只在第一片，``function.name`` 可能被切成多片，``function.arguments``
是**字符串分片**、必须按 index 拼接后才能 ``json.loads``。

**未实现的供应商**（本期明确不做，不允许用文本标签退化顶替）：

- Responses API（``chat_responses.py``）：回灌形状是
  ``{"type":"function_call_output","call_id":...}``，不是 ``role:"tool"``。
- Anthropic Messages（``anthropic.py``）：回灌是 user 消息里的
  ``{"type":"tool_result","tool_use_id":...}`` content block。

这两个适配器目前**既不发 ``tools``，也不解析工具事件**。当主对话绑到它们时，
编排层会给这一轮做 ``tool_set_unavailable`` 标记（见 ``kernel/turn.py``）：
让模型知道"这一轮没有可调用的工具"，而不是让它把查询结果写成文本标签。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ling.capabilities.tools import (
    CLOCK_NOW_TOOL_ID,
    KNOWLEDGE_SEARCH_TOOL_ID,
    MEDIA_WATCH_VIDEO_TOOL_ID,
)
from ling.ports.model import (
    ModelChunk,
    ModelChunkKind,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ProbeResult,
    ProviderKind,
    ToolCall,
    Usage,
)

# Chat Completions 的函数名不接受点号。领域工具 ID 仍用于权限与审计。
_TOOL_WIRE_NAMES = {
    CLOCK_NOW_TOOL_ID: "clock_now",
    KNOWLEDGE_SEARCH_TOOL_ID: "knowledge_search",
    MEDIA_WATCH_VIDEO_TOOL_ID: "media_watch_video",
}
_TOOL_IDS_BY_WIRE_NAME = {name: tool_id for tool_id, name in _TOOL_WIRE_NAMES.items()}


class OpenAICompatibleProvider:
    provider_id: str
    kind: ProviderKind

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str | None,
        kind: ProviderKind = ProviderKind.openai_compatible,
        timeout_seconds: float = 60.0,
        secret_hint: Callable[[str], str] | None = None,
        # 本适配器**做到**了什么。由装配方（`ProviderRegistry`）显式声明，
        # 而不是由 kind 推断：`openai_compatible` 只说明端点说
        # ``/chat/completions``，不说明它认 ``tools``。
        #
        # 编排层用 HTTP 打实时端点前也读它（`NativeToolsSupport`）：
        # 声明为假时，这一轮的工具就该标成"不可用"，而不是发出 ``tools``
        # 然后让模型从来不调（那是最难查的那种静默降级）。
        native_tools: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.kind = kind
        self.supports_native_tools = bool(native_tools)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        # 可选：告诉调用方"为什么没有密钥"（没存过 / 解不开）
        self._secret_hint = secret_hint
        self.last_usage: Usage | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if not self._api_key:
            # 区分两种原因：从来没存过 vs 存过但解不开（根密钥变了）。
            # 统一说"还没有保存密钥"会让后一种用户反复重填也修不好。
            hint = ""
            if self._secret_hint:
                hint = self._secret_hint(self.provider_id)
            if hint == "undecryptable":
                message = (
                    f"供应商 {self.provider_id} 的密钥无法解密："
                    "根密钥不匹配（数据目录是否变过？检查 root.key），"
                    "或需要重新填写密钥"
                )
            else:
                message = f"供应商 {self.provider_id} 还没有保存密钥"
            raise ModelError(ModelErrorKind.missing_secret, message, retriable=False)
        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": [_encode_message(m) for m in request.messages],
            "stream": True,
        }
        # 温度只在调用方明确给了才发：默认不发，让上游用自己的默认值。
        # 各家为对话调好的默认值通常比我们统一填一个 0.8 更合适。
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.profile.startswith("dialogue"):
            # 让兼容端点尽可能返回增量 usage（支持与否由对方决定）
            payload["stream_options"] = {"include_usage": True}
        # ---- 工具：**空元组 = 不发这个字段**。
        #
        # 不要改成"总是发一个空数组"：有些兼容端点把 ``tools: []`` 当作
        # "已启用工具模式"，于是开始校验 ``tool_choice`` 甚至直接 400，
        # 而普通对话本来根本不需要这些字段。
        if request.tools:
            payload["tools"] = [_encode_tool(tool) for tool in request.tools]
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice

        parallel = _encode_parallel_tool_calls(request)
        if parallel is not None:
            payload["parallel_tool_calls"] = parallel

        url = f"{self._base_url}/chat/completions"
        # 流式工具调用的装配器**跨 SSE 行存活**：分片按 ``index`` 累积，
        # 参数到 ``finish_reason`` 才拼完。放在 try 里、每行过一遍，
        # 是因为同一个响应可能先给几个 index 的开头再交错给参数。
        assembler = _ToolCallAssembler()
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
                        usage = parsed.get("usage")
                        if usage:
                            self.last_usage = Usage(
                                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                                total_tokens=int(usage.get("total_tokens", 0) or 0),
                            )
                        choices = parsed.get("choices") or []
                        for choice in choices:
                            delta = choice.get("delta") or {}
                            # **顺序不能反**：文本分片与工具分片可能出现在同一
                            # 个 delta 里（混合输出）。先发工具事件，再发文本，
                            # 与供应商给出的字段顺序一致；反过来会让"调用前的
                            # 那句话"排到调用之后。
                            for event in assembler.feed(delta.get("tool_calls")):
                                yield event
                            content = delta.get("content")
                            if content:
                                yield ModelChunk(delta=content)
                            finish = choice.get("finish_reason")
                            if finish:
                                # 收尾：把每个 index 的完整调用一次性交出去
                                # （OpenAI 在 ``finish_reason`` 之后不会再发
                                # 这个 index 的分片）。``tool_calls`` 这个结束
                                # 原因本身不携带结果，所以不在这里变成文本。
                                for event in assembler.finalize():
                                    yield event
                                yield ModelChunk(done=True, finish_reason=str(finish))
        except httpx.TimeoutException as exc:
            raise ModelError(
                ModelErrorKind.timeout, f"供应商 {self.provider_id} 响应超时", retriable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                ModelErrorKind.unavailable, f"无法连接供应商 {self.provider_id}: {exc}", retriable=True
            ) from exc

    async def probe(self) -> ProbeResult:
        """连接检查：拉取模型列表，验证端点与鉴权；不消耗生成额度。"""
        if not self._api_key:
            return ProbeResult(
                ok=False, detail=f"供应商 {self.provider_id} 还没有保存密钥"
            )
        url = f"{self._base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, detail=f"连接失败: {exc}")
        if response.status_code == 401:
            return ProbeResult(ok=False, detail="鉴权失败：密钥无效或权限不足")
        if response.status_code >= 400:
            return ProbeResult(
                ok=False, detail=f"端点返回 HTTP {response.status_code}: {response.text[:200]}"
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


__all__ = ["OpenAICompatibleProvider"]


class _ToolCallAssembler:
    """把流式 ``delta.tool_calls`` 分片拼成完整的 :class:`ToolCall` 事件。

    官方的分片契约（每一片都可能只带其中几个字段）：

    - ``index``：装配键。**必须按它归组**，不能按到达顺序——并行时
      index 1 的第一片可能先于 index 0 的第二片到达。
    - ``id`` / ``type``：通常只在第一片出现；缺 ``id`` 时**不能编一个**。
    - ``function.name``：可能被切成多片（例如 ``"clock"`` + ``".now"``）。
    - ``function.arguments``：字符串分片，必须**原样拼接**后再解析。
      在分片中途去 ``json.loads`` 必然失败，所以这里只累积。

    产出的事件序列（对每个 index）：

    1. 第一片带出 ``id`` 或 ``name`` 时：一个 ``kind=tool_call`` 的
       :class:`ModelChunk`，携带 ``ToolCall``（此时 ``arguments`` 可能为空）。
    2. 每个 ``arguments`` 分片：一个 ``tool_arguments_delta``。
    3. ``finalize()``：每 index 一个 ``tool_call_complete``，带最终 ``name``
       与完整 ``arguments``——**这是编排层唯一应当信任的"参数已齐全"信号**。

    「缺 ``id`` 的调用」是真实存在的兼容层行为。这里保留空 ``provider_call_id``
    并在 ``finalize`` 里也产出它：编排层必须能为它生成一条失败回执（"这个调用
    没法配对"），而不是静默丢弃——静默丢弃的后果是下一轮 ``role:"tool"`` 的
    结果数量与 assistant 里的调用对不上，供应商 400，而日志里什么都没有。
    """

    def __init__(self) -> None:
        self._order: list[int] = []
        self._entries: dict[int, dict[str, Any]] = {}

    def _entry(self, index: int) -> dict[str, Any]:
        if index not in self._entries:
            self._entries[index] = {"id": "", "name": "", "arguments": ""}
            self._order.append(index)
        return self._entries[index]

    def feed(self, deltas: object) -> list[ModelChunk]:
        """吃一个 ``delta.tool_calls``（可能是 None / 列表 / 脏数据）。"""
        if not isinstance(deltas, list):
            return []
        out: list[ModelChunk] = []
        for raw in deltas:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index", len(self._order)))
            except (TypeError, ValueError):
                index = len(self._order)
            is_new_index = index not in self._entries
            entry = self._entry(index)
            call_id = raw.get("id")
            function = raw.get("function")
            name_part = ""
            args_part: str | None = None
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    name_part = function["name"]
                if isinstance(function.get("arguments"), str):
                    args_part = function["arguments"]
            if isinstance(call_id, str) and call_id and not entry["id"]:
                entry["id"] = call_id
            if name_part:
                entry["name"] += name_part
            # "开始"只在该 index **第一次**出现时发一次。判据是"新 index"，
            # 不是"这一片带了 id 或 name"：官方后续分片段也可能重复带
            # ``function.name``，按内容判会为同一次调用发出第二次 start，
            # 编排层看到两条 start 就会以为模型提了两次调用。
            if is_new_index:
                out.append(
                    ModelChunk(
                        kind=ModelChunkKind.tool_call,
                        tool_call=ToolCall(
                            provider_call_id=entry["id"],
                            name=_TOOL_IDS_BY_WIRE_NAME.get(entry["name"], entry["name"]),
                            arguments="",
                            raw_index=index,
                        ),
                        provider_call_id=entry["id"] or None,
                    )
                )
            if args_part is not None:
                entry["arguments"] += args_part
                if args_part:
                    out.append(
                        ModelChunk(
                            kind=ModelChunkKind.tool_call,
                            tool_arguments_delta=args_part,
                            provider_call_id=entry["id"] or None,
                        )
                    )
        return out

    def finalize(self) -> list[ModelChunk]:
        """流结束时为每个 index 产出一次"参数已齐全"（幂等，可重复调用）。"""
        out: list[ModelChunk] = []
        for index in self._order:
            entry = self._entries[index]
            out.append(
                ModelChunk(
                    kind=ModelChunkKind.tool_call,
                    tool_call=ToolCall(
                        provider_call_id=entry["id"],
                        name=_TOOL_IDS_BY_WIRE_NAME.get(entry["name"], entry["name"]),
                        arguments=entry["arguments"],
                        raw_index=index,
                    ),
                    provider_call_id=entry["id"] or None,
                    tool_call_complete=True,
                )
            )
        self._order.clear()
        self._entries.clear()
        return out


def _encode_tool(tool: object) -> dict[str, Any]:
    """``ToolDefinition`` → 官方 ``tools[i]`` 形状。"""
    parameters = getattr(tool, "parameters", None)
    name = getattr(tool, "name", "")
    function: dict[str, Any] = {
        "name": _TOOL_WIRE_NAMES.get(name, name),
        "description": getattr(tool, "description", ""),
    }
    # 空参数 schema 也照发 ``{"type":"object","properties":{}}``：
    # 缺 ``parameters`` 字段时部分兼容端点会拒绝，而"无参数工具"是合法形状。
    schema: dict[str, Any] = dict(parameters or {})
    encoded: dict[str, Any] = {
        "type": "object",
        "properties": dict(schema.get("properties") or {}),
    }
    required = schema.get("required")
    if required:
        encoded["required"] = list(required)
    function["parameters"] = encoded
    if getattr(tool, "strict", False):
        function["strict"] = True
    return {"type": "function", "function": function}


def _encode_parallel_tool_calls(request: ModelRequest) -> bool | None:
    """``parallel_tool_calls`` 只在这次请求**真的带工具**时才发。

    不带工具时发这个字段没有意义，而且有的兼容端点会因为"设了并行开关却没有
    工具"而报 400。``None`` 表示由供应商默认；``True`` 也只是"允许"，
    不是"一定会并行"。
    """
    if not request.tools:
        return None
    return request.parallel_tool_calls


def _encode_message(message: object) -> dict[str, Any]:
    """把一条消息编码成 OpenAI 兼容格式。

    纯文本仍然是 ``{"role", "content": "..."}``——**不要**改成永远是数组：
    有些兼容端点（尤其本地部署）对数组形式的纯文本支持不好。

    带图时才切成多模态数组：text 段在前，image_url 段在后。

    ## 工具回路的两条消息形状

    - assistant 提出了调用：必须带上 ``tool_calls``（原样回放）。缺了它，
      后面的 ``role:"tool"`` 就是"无主结果"，OpenAI 直接 400。
      ``content`` 为 **空字符串**而不是 ``None``：两者语义相近，但有的兼容
      端点只收字符串，而 "先想一句再调工具" 的情形下正文本来是空的。
    - ``role="tool"``：只发 ``tool_call_id`` + ``content``（+ ``name``，
      官方已不要求，发了也无害，便于兼容层路由）。**不**把结果拼进
      system/user 正文——那是"把第三方数据当指令"的路子，见
      ``kernel/turn.py::_encode_tool_results``。
    """
    role = getattr(message, "role", "user")
    content = getattr(message, "content", "")
    images = tuple(getattr(message, "images", ()) or ())
    videos = tuple(getattr(message, "videos", ()) or ())
    tool_calls = tuple(getattr(message, "tool_calls", ()) or ())
    if role == "tool":
        probe_id = getattr(message, "provider_call_id", None)
        if not probe_id:
            # 没有配对键的工具结果**不能**发出去：它会被端点当成无主数据，
            # 要么 400，要么被静默丢弃。这里是编程错误，不是运行时状况。
            raise ValueError("role=tool 的消息缺少 provider_call_id，无法配对")
        return {"role": "tool", "tool_call_id": probe_id, "content": content}
    encoded: dict[str, Any] = {"role": role}
    if not images and not videos:
        encoded["content"] = content
    else:
        parts: list[dict[str, Any]] = []
        if content:
            parts.append({"type": "text", "text": content})
        for image in images:
            parts.append({"type": "image_url", "image_url": {"url": image.url}})
        # 原生视频输入：OpenAI 兼容世界里的事实标准是 ``video_url`` 段
        # （Gemini / Qwen-VL / 豆包 / 智谱 / Kimi 的兼容层都收这个形状）。
        # 不支持的端点会明确报错，由调用方退回抽帧——这里不做能力猜测。
        for video in videos:
            parts.append({"type": "video_url", "video_url": {"url": video.url}})
        encoded["content"] = parts
    if tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.provider_call_id,
                "type": "function",
                "function": {
                    "name": _TOOL_WIRE_NAMES.get(call.name, call.name),
                    "arguments": call.arguments,
                },
            }
            for call in tool_calls
        ]
    return encoded
