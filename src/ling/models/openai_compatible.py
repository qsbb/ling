"""OpenAI 兼容接口适配器（真实供应商路径）。

支持任何暴露 ``/chat/completions`` 的服务：OpenAI、DeepSeek、Moonshot、vLLM、Ollama
的 OpenAI 兼容端点等。**能力必须逐型号探测**：这里只声明协议层的流式与用量解析，
不保证对方支持工具、结构化输出、视觉或音频。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ling.ports.model import (
    ModelChunk,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ProbeResult,
    ProviderKind,
    Usage,
)


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
    ) -> None:
        self.provider_id = provider_id
        self.kind = kind
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
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.profile.startswith("dialogue"):
            # 让兼容端点尽可能返回增量 usage（支持与否由对方决定）
            payload["stream_options"] = {"include_usage": True}

        url = f"{self._base_url}/chat/completions"
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
                            delta = (choice.get("delta") or {}).get("content")
                            if delta:
                                yield ModelChunk(delta=delta)
                            finish = choice.get("finish_reason")
                            if finish:
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


def _encode_message(message: object) -> dict[str, Any]:
    """把一条消息编码成 OpenAI 兼容格式。

    纯文本仍然是 ``{"role", "content": "..."}``——**不要**改成永远是数组：
    有些兼容端点（尤其本地部署）对数组形式的纯文本支持不好。

    带图时才切成多模态数组：text 段在前，image_url 段在后。
    """
    role = getattr(message, "role", "user")
    content = getattr(message, "content", "")
    images = tuple(getattr(message, "images", ()) or ())
    if not images:
        return {"role": role, "content": content}
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": image.url}})
    return {"role": role, "content": parts}
