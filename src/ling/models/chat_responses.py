"""OpenAI Responses API 适配器（``POST {base}/responses``）。

## 与 ``/chat/completions`` 的差别（都是静默错，不会报错）

1. **输入结构**：``input`` 是一个 item 数组，每条是
   ``{"role", "content": [{"type": "input_text"|"output_text"|"input_image", ...}]}``，
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

from ling.ports.model import (
    ChatMessageIn,
    ModelChunk,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ProbeResult,
    ProviderKind,
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
    ) -> None:
        self.provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._secret_hint = secret_hint
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

        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        # 循环变量不叫 `message`：上面密钥那段已用同名局部变量存错误文案。
        for item in request.messages:
            if item.role == "system":
                instructions.append(item.content)
                continue
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
                    event = parsed.get("type")
                    if event == "response.output_text.delta":
                        delta = parsed.get("delta")
                        if delta:
                            yield ModelChunk(delta=str(delta))
                    elif event == "response.completed":
                        usage = (parsed.get("response") or {}).get("usage") or {}
                        if usage:
                            prompt = int(usage.get("input_tokens", 0) or 0)
                            completion = int(usage.get("output_tokens", 0) or 0)
                            self.last_usage = Usage(
                                prompt_tokens=prompt,
                                completion_tokens=completion,
                                total_tokens=prompt + completion,
                            )
                        yield ModelChunk(done=True, finish_reason="stop")
                    elif event in {"response.failed", "error"}:
                        detail = (
                            (parsed.get("response") or {}).get("error", {}).get("message")
                            or (parsed.get("error") or {}).get("message")
                            or (parsed.get("error") or {}).get("code")
                            or "未知错误"
                        )
                        raise ModelError(
                            ModelErrorKind.error,
                            f"供应商 {self.provider_id} 返回错误: {detail}",
                            retriable=False,
                        )
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


def _encode_item(message: ChatMessageIn) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    text_type = "output_text" if message.role == "assistant" else "input_text"
    if message.content:
        blocks.append({"type": text_type, "text": message.content})
    for image in message.images:
        blocks.append({"type": "input_image", "image_url": image.url})
    if not blocks:
        blocks.append({"type": text_type, "text": ""})
    return {"role": message.role, "content": blocks}


__all__ = ["ChatResponsesProvider"]
