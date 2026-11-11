"""FishAudio TTS 适配器：**msgpack 请求体 + 二进制音频流**。

## 这家与其他两家最不一样的三处

1. **请求体不是 JSON，是 MessagePack**（``Content-Type: application/msgpack``）。
   发成 JSON 的表现是供应商那边 400 或反序列化错误——而错误正文往往只有
   ``"failed to decode"`` 这种，看不出是"格式发错了"。这是本模块最核心的一条。

2. **``model`` 是一个 HTTP header**，不是请求体字段。放错地方不会报错，
   只是被忽略、然后跑服务端默认模型——**静默用了错的模型**，计量和音质都
   对不上，而且没有任何错误可查。

3. **成功时返回的是 ``audio/*`` 二进制流，失败时返回 JSON。**
   所以不能只看状态码：``200 + text/html`` 是"请求根本没到 API"
   （Base URL 少 ``/v1``，或 DNS 被劫持到网页），而 ``200 + audio/*`` 才是成功。

## wire 协议

::

    POST {api_base}/v1/tts
    Authorization: Bearer <key>
    Content-Type: application/msgpack
    model: s2.1-pro                     ← 模型在 header 里

    <msgpack: {"text": "...", "format": "mp3", "reference_id": "<32 hex>",
               "normalize": true, "mp3_bitrate": 128, "prosody": {...}}>

    → 200 + audio/mpeg  <二进制音频>

## 音色

**用户自己填 ``reference_id``**（FishAudio 上 32 位十六进制的模型 ID）。
本项目**不**调用 ``/model`` 建音色、也不做参考音频上传（见设计文档 §3.2
"修正范围"）：音色在厂商侧由用户自己准备，我们只校验格式与可用性。
"""

from __future__ import annotations

import re

import httpx

from ling.models.tts_common import (
    SynthesizedAudio,
    TtsError,
    TtsErrorKind,
    classify_http_error,
    redact,
    scrub,
    sniff_container,
)

#: ``reference_id`` 是 32 位十六进制。**在发请求之前就校验**：
#: 格式错的 ID 到了供应商那边只会得到一个含糊的 400，用户不知道该改什么。
REFERENCE_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")

#: 默认接口地址。国内站与国际站域名不同，用户可以在设置里改。
DEFAULT_BASE = "https://api.fish-audio.cn/v1"

#: 支持的容器。``pcm`` 是无头的裸数据，需要额外的采样率才知道怎么播。
SUPPORTED_FORMATS = ("wav", "mp3", "pcm")

#: 官方当前生产模型是 ``s2.1-pro``；``s2.1-pro-free`` 是无 TTFA 保证的开发档。
#: S1 计划 2026-12-31 退役——所以默认值不选 S1 系。
KNOWN_MODELS = ("s2.1-pro", "s2.1-pro-free", "s1")

#: mp3 码率单位是 **kbit/s**（128），不是 bit/s。MiniMax 那边是 bit/s，
#: 混用会得到一个 400 或畸形的采样率。
DEFAULT_MP3_BITRATE = 128


class FishAudioTts:
    """``ProviderKind.fish_audio`` 的实现。无状态、可并发复用。"""

    provider_id = "fish_audio"

    def __init__(
        self,
        *,
        api_key: str,
        reference_id: str = "",
        model: str = "s2.1-pro",
        base_url: str = DEFAULT_BASE,
        output_format: str = "mp3",
        mp3_bitrate: int = DEFAULT_MP3_BITRATE,
        sample_rate: int | None = None,
        speed: float = 1.0,
        volume: float = 0.0,
        timeout_seconds: float = 60.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = (api_key or "").strip()
        self.reference_id = (reference_id or "").strip()
        # ``auto`` 是**上层配置语义**（由编排层解析成具体模型），不是模型名。
        # 发给供应商只会得到 model_not_found。这里把它当"不指定"处理。
        model_value = (model or "").strip()
        self.model = "" if model_value.lower() == "auto" else model_value
        self._base = self._normalize_base(base_url)
        self.output_format = (
            output_format if output_format in SUPPORTED_FORMATS else "mp3"
        )
        self.mp3_bitrate = mp3_bitrate
        self.sample_rate = sample_rate
        self.speed = speed
        # Fish 的 volume 是**分贝偏移**（−10..10，0 = 原音量），不是倍率。
        # 与 MiniMax 的 vol(0..10, 1.0 = 原音量) 语义不同，不能互相照抄。
        self.volume = volume
        self._timeout = timeout_seconds
        self._http = http

    # -------------------------------------------------------------- 配置

    @staticmethod
    def _normalize_base(base_url: str) -> str:
        """补全 ``/v1``。

        用户常把 Base URL 填成站点根（``https://api1.example.org``）。缺 ``/v1``
        时 ``POST /tts`` 会打到网页前端，拿到 **200 的 HTML**——看起来成功、
        实际什么都没合成。这个坑在参考实现里已踩过并修过，这里保持同一口径。
        """
        base = (base_url or DEFAULT_BASE).strip().rstrip("/")
        if not base:
            base = DEFAULT_BASE
        if base.lower().endswith("/v1"):
            return base
        from urllib.parse import urlsplit

        path = urlsplit(base).path.rstrip("/")
        last_segment = path.rsplit("/", 1)[-1] if path else ""
        # 已经是显式 API 前缀（如 /v2、/openai）就不强行改写
        if last_segment and not last_segment.isdigit() and len(last_segment) <= 8:
            return base
        return f"{base}/v1"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/msgpack",
        }
        # **模型在这里，不在请求体里。**空值时不发：自建 fish-speech 服务
        # 没有 per-request model 字段，发了会覆盖服务器加载的模型。
        if self.model:
            headers["model"] = self.model
        return headers

    def build_payload(self, text: str, *, reference_id: str = "") -> dict[str, object]:
        payload: dict[str, object] = {
            "text": text,
            "format": self.output_format,
            # 文本规范化：把数字/日期读成中文口语，否则会逐字读"2026年"。
            "normalize": True,
        }
        ref = (reference_id or self.reference_id).strip()
        if ref:
            payload["reference_id"] = ref
        if self.output_format == "mp3":
            payload["mp3_bitrate"] = self.mp3_bitrate
        if self.sample_rate:
            payload["sample_rate"] = self.sample_rate
        prosody: dict[str, object] = {}
        if self.speed != 1.0:
            prosody["speed"] = self.speed
        if self.volume != 0.0:
            prosody["volume"] = self.volume
        if prosody:
            payload["prosody"] = prosody
        return payload

    # -------------------------------------------------------------- 主流程

    async def synthesize(self, text: str, *, reference_id: str = "") -> SynthesizedAudio:
        spoken = (text or "").strip()
        if not spoken:
            raise TtsError(TtsErrorKind.invalid_request, "合成文本为空", retriable=False)
        if not self._key:
            raise TtsError(
                TtsErrorKind.not_configured,
                "FishAudio API Key 未配置，请在设置里填写后重试",
                retriable=False,
            )
        ref = (reference_id or self.reference_id).strip()
        if not ref:
            raise TtsError(
                TtsErrorKind.not_configured,
                "FishAudio Reference ID 未配置（需填 32 位十六进制音色 ID）",
                retriable=False,
            )
        if not REFERENCE_ID_PATTERN.match(ref):
            # 在**发请求前**拦：显示前 12 位足够定位是填错了哪一个，
            # 又不至于把整串贴进日志。
            raise TtsError(
                TtsErrorKind.invalid_request,
                f"FishAudio Reference ID 格式不对（需 32 位十六进制，当前 '{redact(ref, limit=12)}'）。"
                "可从 https://fish.audio 的音色页取得",
                retriable=False,
            )

        try:
            import msgpack
        except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 声明
            raise TtsError(
                TtsErrorKind.not_configured,
                "缺少 msgpack 依赖，FishAudio 请求体需要 msgpack 编码",
                retriable=False,
            ) from exc

        payload = self.build_payload(spoken, reference_id=ref)
        # ``use_bin_type=True``：Python 3 的标准 str/bin 区分。不带这个参数
        # 时字符串会被编成原始字节，服务端解出来是 bytes 不是 str。
        body = msgpack.packb(payload, use_bin_type=True)
        url = f"{self._base}/tts"

        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.post(
                    url, headers=self._headers(), content=body
                )
            except httpx.TimeoutException as exc:
                raise TtsError(
                    TtsErrorKind.timeout,
                    f"FishAudio 合成超时（{self._timeout:.0f}s）",
                ) from exc
            except httpx.HTTPError as exc:
                raise TtsError(
                    TtsErrorKind.unavailable,
                    f"FishAudio 不可达：{type(exc).__name__}",
                ) from exc
        finally:
            if owns:
                await client.aclose()

        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> SynthesizedAudio:
        content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        data = response.content

        if response.status_code == 200 and content_type.startswith("audio/"):
            if not data:
                raise TtsError(TtsErrorKind.protocol, "FishAudio 返回了空的音频流")
            container = sniff_container(data)
            mime_container = _container_from_mime(content_type)
            if container is None:
                # 声称是 audio/* 但字节不像任何已知容器——把**声明的**类型
                # 当作可用信息（有些新容器我们还没认），但记下不一致。
                container = mime_container or self.output_format
            return SynthesizedAudio(
                audio=data,
                container=container,
                duration_ms=None,  # 流式响应没有时长字段，**不估算**
                sample_rate=self.sample_rate,
                provider_request_id=str(response.headers.get("x-request-id") or "") or None,
            )

        # ---- 以下都是失败。200 + HTML 是"请求根本没到 API"，单独给话术。
        text_head = data.decode("utf-8", errors="replace")
        lowered = text_head[:512].lstrip().lower()
        if response.status_code == 200 and (
            lowered.startswith("<!doctype html") or lowered.startswith("<html")
        ):
            raise TtsError(
                TtsErrorKind.invalid_request,
                "FishAudio 返回的是网页而不是音频——Base URL 可能缺少 /v1 路径，"
                "或请求被网关拦截。请把接口地址改成以 /v1 结尾后重试",
                retriable=False,
                status_code=200,
            )
        if response.status_code == 200:
            # 200 但不是 audio/* 也不是 HTML：多半是一段 JSON 错误。
            raise TtsError(
                TtsErrorKind.protocol,
                f"FishAudio 返回了非音频响应（content-type={redact(content_type, limit=40)}）："
                f"{scrub(redact(text_head, limit=200), self._key)}",
            )
        raise classify_http_error(
            response.status_code,
            text_head,
            secret=self._key,
            provider="FishAudio",
            hint="请检查 API Key 与 Reference ID 是否属于当前接口站点"
            if response.status_code in (401, 403)
            else "",
        )


def _container_from_mime(mime: str) -> str | None:
    return {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/pcm": "pcm",
    }.get(mime)


__all__ = [
    "DEFAULT_BASE",
    "DEFAULT_MP3_BITRATE",
    "FishAudioTts",
    "KNOWN_MODELS",
    "REFERENCE_ID_PATTERN",
    "SUPPORTED_FORMATS",
]
