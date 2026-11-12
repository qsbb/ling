"""SiliconFlow 文件语音识别，使用 multipart 音频转写协议。

协议依据 https://docs.siliconflow.cn/docs/api/audio-transcriptions-post 。
模型由已发布绑定指定，目录示例不作为线上可用性保证。
"""

from __future__ import annotations

import httpx

from ling.models.asr_common import check_request, response_body, result_text
from ling.ports.speech import (
    AsrCapabilities,
    AsrRequest,
    AsrResult,
    SpeechCall,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
)

DEFAULT_BASE = "https://api.siliconflow.cn/v1"


class SiliconFlowAsr:
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
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._http = http

    def capabilities(self, model_id: str) -> AsrCapabilities:
        return AsrCapabilities(
            formats=frozenset({"wav", "mp3"}),
            max_file_bytes=50 * 1024 * 1024,
            max_audio_seconds=3600.0,
        )

    async def transcribe(self, request: AsrRequest, *, call: SpeechCall) -> AsrResult:
        check_request(request, self.capabilities(request.model_id))
        if not self._key:
            raise SpeechError(SpeechErrorKind.not_configured, "请先配置语音识别密钥")
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.post(
                    f"{self._base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    data={"model": request.model_id},
                    files={
                        "file": (
                            f"audio.{request.audio.spec.container}",
                            request.audio.content,
                            request.audio.spec.mime_type,
                        )
                    },
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise SpeechError(SpeechErrorKind.timeout, "语音识别请求超时") from exc
            except httpx.HTTPError as exc:
                raise SpeechError(SpeechErrorKind.unavailable, "语音识别连接中断") from exc
            body = response_body(response)
            return AsrResult(
                text=result_text(body.get("text")),
                language=None,
                confidence=None,
                provider_request_id=response.headers.get("x-siliconcloud-trace-id"),
                usage=SpeechUsage(),
            )
        finally:
            if self._http is None:
                await client.aclose()


__all__ = ["DEFAULT_BASE", "SiliconFlowAsr"]
