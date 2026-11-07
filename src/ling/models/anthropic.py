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
    ) -> None:
        self.provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._secret_hint = secret_hint
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

        system_parts: list[str] = []
        turns: list[dict[str, Any]] = []
        # 循环变量不叫 `message`：上面密钥那段已经用同名局部变量存错误文案，
        # 复用同名会让 mypy 把这条消息推断成 str。
        for item in request.messages:
            if item.role == "system":
                system_parts.append(item.content)
                continue
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
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event = parsed.get("type")
                    if event == "content_block_delta":
                        delta = (parsed.get("delta") or {}).get("text")
                        if delta:
                            yield ModelChunk(delta=str(delta))
                    elif event == "message_delta":
                        usage = parsed.get("usage") or {}
                        if usage:
                            prompt = int(usage.get("input_tokens", 0) or 0)
                            completion = int(usage.get("output_tokens", 0) or 0)
                            self.last_usage = Usage(
                                prompt_tokens=prompt,
                                completion_tokens=completion,
                                total_tokens=prompt + completion,
                            )
                        stop = (parsed.get("delta") or {}).get("stop_reason")
                        if stop:
                            yield ModelChunk(done=True, finish_reason=str(stop))
                    elif event == "message_stop":
                        # 某些代理不发 message_delta，靠这个兜底结束。
                        yield ModelChunk(done=True, finish_reason="stop")
                    elif event == "error":
                        detail = (parsed.get("error") or {}).get("message") or "未知错误"
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
