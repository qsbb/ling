"""百炼 Qwen 文件识别，使用原生 JSON 与音频 Data URI。

协议依据 https://help.aliyun.com/en/model-studio/qwen-audio-asr-http-api 。
base_url 可填工作空间域名、api/v1 根路径或完整 generation 端点。
"""

from __future__ import annotations

import base64

import httpx

from ling.models.asr_common import (
    check_request,
    optional_number,
    optional_tokens,
    response_body,
    result_text,
)
from ling.ports.speech import (
    AsrCapabilities,
    AsrRequest,
    AsrResult,
    SpeechCall,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
)

DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
_GENERATION_PATH = "/services/aigc/multimodal-generation/generation"


class DashScopeAsr:
    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str,
        base_url: str = DEFAULT_BASE,
        timeout_seconds: float = 90.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._key = api_key
        base = base_url.rstrip("/")
        if base.endswith(_GENERATION_PATH):
            self._endpoint = base
        elif base.endswith("/api/v1"):
            self._endpoint = f"{base}{_GENERATION_PATH}"
        else:
            self._endpoint = f"{base}/api/v1{_GENERATION_PATH}"
        self._timeout = timeout_seconds
        self._http = http

    def capabilities(self, model_id: str) -> AsrCapabilities:
        return AsrCapabilities(
            formats=frozenset({"wav", "mp3", "opus"}),
            language_hints=True,
            vocabulary=True,
        )

    async def transcribe(self, request: AsrRequest, *, call: SpeechCall) -> AsrResult:
        check_request(request, self.capabilities(request.model_id))
        if not self._key:
            raise SpeechError(SpeechErrorKind.not_configured, "请先配置语音识别密钥")
        parameters: dict[str, object] = {"format": request.audio.spec.container}
        if request.audio.spec.sample_rate_hz is not None:
            parameters["sample_rate"] = str(request.audio.spec.sample_rate_hz)
        if request.language:
            parameters["language_hints"] = [request.language]
        if request.vocabulary:
            parameters["vocabulary"] = dict.fromkeys(request.vocabulary, 3)
        encoded = base64.b64encode(request.audio.content).decode("ascii")
        payload = {
            "model": request.model_id,
            "input": {
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:{request.audio.spec.mime_type};base64,{encoded}"
                        },
                    }],
                }],
            },
            "parameters": parameters,
        }
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "X-DashScope-SSE": "disable",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise SpeechError(SpeechErrorKind.timeout, "语音识别请求超时") from exc
            except httpx.HTTPError as exc:
                raise SpeechError(SpeechErrorKind.unavailable, "语音识别连接中断") from exc
            body = response_body(response)
            output = body.get("output")
            if not isinstance(output, dict):
                raise SpeechError(SpeechErrorKind.protocol, "语音识别响应缺少 output")
            usage = body.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise SpeechError(SpeechErrorKind.protocol, "语音识别用量结构不符合协议")
            seconds = optional_number(usage.get("duration"))
            input_tokens = optional_tokens(usage.get("input_tokens"))
            output_tokens = optional_tokens(usage.get("output_tokens"))
            known_usage = any(x is not None for x in (seconds, input_tokens, output_tokens))
            request_id = body.get("request_id")
            return AsrResult(
                text=result_text(output.get("text")),
                language=None,
                confidence=None,
                provider_request_id=request_id if isinstance(request_id, str) else None,
                usage=SpeechUsage(
                    audio_seconds=seconds,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage_source="provider" if known_usage else "unknown",
                ),
            )
        finally:
            if self._http is None:
                await client.aclose()


__all__ = ["DEFAULT_BASE", "DashScopeAsr"]
