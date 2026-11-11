"""小米 MiMo TTS 适配器：走 ``chat/completions``，音频在 ``message.audio.data``。

## 这家是三家里面最"不像 TTS"的一个

它**没有专用 TTS 端点**。合成就是把话当作 assistant 消息发出去，让模型"说"
出来，音频放在响应的 ``choices[0].message.audio.data`` 里（**base64**）。
所以：

1. **我们直接发 HTTP，不用 ``openai`` SDK。** 参考实现用了 ``AsyncOpenAI``，
   但那会引入一个只为三家之一的、几十 MB 的依赖，且它的 ``chat.completions.create``
   类型签名不认识 ``audio`` 这个扩展字段（要 ``**payload`` 绕过类型检查）。
   这里用 httpx 发同一个 JSON——wire 上完全等价，而且离线可测。

2. **音色不是 ID，是每次随请求上传的参考音频。** ``audio.voice`` 是一整个
   ``data:{mime};base64,{...}`` 的 data URL。**每次合成都要重新编码一遍**
   （没有"先注册再只传 ID"的步骤）。这意味着：
   - 文本再短，请求体也有几 MB（参考音频的大小）；
   - **data URL 绝不能进日志**（它就是一段完整音频），也不能入库；
   - 并发时要算进有界预算，否则几条并发就把内存顶满。

3. **``base_url`` 是 OpenAI 兼容的**（``https://api.xiaomimimo.com/v1``），
   请求路径是 ``/chat/completions``。

## wire 协议

::

    POST https://api.xiaomimimo.com/v1/chat/completions
    Authorization: Bearer <key>
    Content-Type: application/json

    {
      "model": "mimo-v2.5-tts-voiceclone",
      "messages": [
        {"role": "user", "content": "<风格/情境描述，可空>"},
        {"role": "assistant", "content": "<要读的文本>"}
      ],
      "audio": {"format": "wav", "voice": "data:audio/wav;base64,<...>"}
    }

    → {"choices": [{"message": {"audio": {"data": "<base64>"}}}], ...}

## 为什么 ``mimo_tts`` 是独立 kind

同一个厂商、同一个 base_url，但 MiMo 既能聊天也能合成。**不能用一个 kind
声明两种能力**——那会让"把 TTS 供应商塞进聊天槽位"这类错配通过校验。
所以是两条 provider 配置，见 `ports/model.py::ProviderKind.mimo_chat/mimo_tts`。
"""

from __future__ import annotations

import base64

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

DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5-tts-voiceclone"

#: 支持的输出容器。
SUPPORTED_FORMATS = ("wav", "mp3", "pcm", "flac")

#: 参考音频允许的 MIME。**必须嗅探实际字节**，不能信文件名后缀：
#: 一个 .wav 后缀的 mp3 会让 data URL 的 mime 说谎，服务端解码失败的表现
#: 是"合成出来的声音是噪音"，而不是报错。
_MIME_BY_CONTAINER = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "pcm": "audio/pcm",
}

#: 参考音频大小上限（字节）。data URL 会膨胀 4/3，所以 10 MB 音频
#: 进请求体是 ~13.4 MB。超过这个量级的"音色参考"通常是误传了整个录音文件。
MAX_VOICE_BYTES = 10 * 1024 * 1024


class MimoTts:
    """``ProviderKind.mimo_tts`` 的实现。无状态、可并发复用。"""

    provider_id = "mimo_tts"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE,
        output_format: str = "wav",
        style_context: str = "",
        timeout_seconds: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip()
        # 保留用户填的完整 base（含 /v1）。只去尾部斜杠，**不擅自补 /v1**：
        # 小米的兼容端点本身就是 .../v1，而用户也可能自建反代。
        self._base = (base_url or DEFAULT_BASE).strip().rstrip("/")
        self.output_format = (
            output_format if output_format in SUPPORTED_FORMATS else "wav"
        )
        self.style_context = (style_context or "").strip()
        self._timeout = timeout_seconds
        self._http = http

    # -------------------------------------------------------------- 参考音频

    @staticmethod
    def encode_voice_data_url(audio: bytes, *, declared_mime: str = "") -> str:
        """把参考音频编成 ``data:{mime};base64,{...}``。

        MIME **以嗅探为准**，``declared_mime`` 只在嗅探失败时兜底：

        - 嗅探成功 → 用嗅探出的容器对应的标准 MIME。这是最可靠的：
          用户给的 .wav 其实是 mp3 时，说 ``audio/wav`` 会让服务端按 WAV 解码，
          结果是一段噪音而**不是错误**——这种"成功但听不出是话"的故障最难查。
        - 嗅探失败 → 用 ``declared_mime``（用户/资产层声明）。
        - 都没有 → 拒绝，不猜。
        """
        if not audio:
            raise TtsError(
                TtsErrorKind.invalid_request, "参考音频为空，无法合成", retriable=False
            )
        if len(audio) > MAX_VOICE_BYTES:
            raise TtsError(
                TtsErrorKind.invalid_request,
                f"参考音频过大（{len(audio) / 1024 / 1024:.1f} MB，上限 "
                f"{MAX_VOICE_BYTES / 1024 / 1024:.0f} MB）",
                retriable=False,
            )
        container = sniff_container(audio)
        mime = _MIME_BY_CONTAINER.get(container or "", "") or (declared_mime or "").strip()
        if not mime.startswith("audio/"):
            raise TtsError(
                TtsErrorKind.invalid_request,
                "参考音频格式无法识别（需 wav / mp3 / flac 等常见格式）",
                retriable=False,
            )
        encoded = base64.b64encode(audio).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    # -------------------------------------------------------------- 请求构造

    def build_payload(self, text: str, *, voice_data_url: str) -> dict[str, object]:
        """拼 ``chat/completions`` 请求体。

        ``messages`` 的构造方式**照参考实现**：

        - 有 ``style_context`` 时先放一条 ``user``（这是"情境/风格"提示，
          比如"用温柔的语气，语速稍慢"）；
        - 要读的文本放 ``assistant``——也就是让模型"说出这句话"，
          而不是"回复这句话"。**顺序反了效果完全不同**：文本进 user 位置时，
          模型会当成问题去回答，而不是朗读。
        """
        messages: list[dict[str, str]] = []
        if self.style_context:
            messages.append({"role": "user", "content": self.style_context})
        messages.append({"role": "assistant", "content": text})
        return {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.output_format, "voice": voice_data_url},
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------- 主流程

    async def synthesize(
        self,
        text: str,
        *,
        voice_audio: bytes = b"",
        voice_mime: str = "",
        style_context: str = "",
    ) -> SynthesizedAudio:
        """合成 ``text``。

        ``voice_audio`` 是**参考音频的原始字节**（不是 data URL、不是路径）：
        适配器不读文件、不查数据库（设计文档 §3.2："adapter 不查数据库、
        不读客户端路径"），由编排层加载好受限资产再传进来。
        """
        spoken = (text or "").strip()
        if not spoken:
            raise TtsError(TtsErrorKind.invalid_request, "合成文本为空", retriable=False)
        if not self._key:
            raise TtsError(
                TtsErrorKind.not_configured,
                "MiMo API Key 未配置，请在设置里填写后重试",
                retriable=False,
            )
        if not voice_audio:
            raise TtsError(
                TtsErrorKind.not_configured,
                "MiMo 需要参考音频才能合成（它的音色来自每次随请求上传的参考音频，"
                "不是预注册的音色 ID）",
                retriable=False,
            )

        voice_data_url = self.encode_voice_data_url(
            voice_audio, declared_mime=voice_mime
        )
        if style_context:
            # 单次调用的 style_context 覆盖构造时的默认值。
            original = self.style_context
            self.style_context = style_context.strip()
            try:
                payload = self.build_payload(spoken, voice_data_url=voice_data_url)
            finally:
                self.style_context = original
        else:
            payload = self.build_payload(spoken, voice_data_url=voice_data_url)

        url = f"{self._base}/chat/completions"
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                response = await client.post(url, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                raise TtsError(
                    TtsErrorKind.timeout,
                    f"MiMo 合成超时（{self._timeout:.0f}s）。参考音频较大时"
                    "首次请求会更慢，可适当调大超时",
                ) from exc
            except httpx.HTTPError as exc:
                # **异常文本里可能带 URL 和已编码的 body**——过一遍 scrub，
                # 参考音频（也就是用户的声音）绝不能因为一个报错进日志。
                raise TtsError(
                    TtsErrorKind.unavailable,
                    f"MiMo 不可达：{redact(type(exc).__name__)}",
                ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code != 200:
            raise classify_http_error(
                response.status_code,
                # 失败响应里**不会**有参考音频（那是请求体），但保险起见仍过脱敏。
                response.text,
                secret=self._key,
                provider="MiMo",
                hint="请检查 API Key 是否属于小米 MiMo 平台"
                if response.status_code in (401, 403)
                else "",
            )

        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> SynthesizedAudio:
        try:
            body = response.json()
        except ValueError as exc:
            raise TtsError(
                TtsErrorKind.protocol,
                f"MiMo 响应不是 JSON：{scrub(redact(response.text, limit=120), self._key)}",
            ) from exc
        if not isinstance(body, dict):
            raise TtsError(TtsErrorKind.protocol, "MiMo 响应顶层不是对象")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise TtsError(TtsErrorKind.protocol, "MiMo 响应里没有 choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise TtsError(TtsErrorKind.protocol, "MiMo 响应的 choices[0] 不是对象")

        # ``finish_reason`` 能区分"模型拒答/截断"与"正常生成"。因为这条路径
        # 本质是 chat，模型完全可能**不生成音频而生成一段文本**（例如拒绝）。
        finish = first.get("finish_reason")
        message = first.get("message")
        if not isinstance(message, dict):
            raise TtsError(TtsErrorKind.protocol, "MiMo 响应的 message 缺失")

        audio_obj = message.get("audio")
        audio_data = audio_obj.get("data") if isinstance(audio_obj, dict) else None
        if not audio_data:
            # 把模型说了什么带出来——最常见的原因是它**回了一段文本而不是音频**
            # （要求模型做到它不支持的事、或参考音频不合规）。
            said = message.get("content")
            extra = f"模型输出：{redact(said, limit=160)}" if said else ""
            if finish and finish != "stop":
                extra = f"{extra}（finish_reason={redact(finish, limit=40)}）".strip()
            raise TtsError(
                TtsErrorKind.protocol,
                "MiMo 响应里没有音频数据"
                + (f"。{extra}" if extra else "。请检查参考音频是否合规"),
            )

        try:
            audio = base64.b64decode(audio_data, validate=True)
        except (ValueError, TypeError) as exc:
            # 消息里**绝不**带 audio_data 本身：它就是整段音频。
            raise TtsError(
                TtsErrorKind.protocol, "MiMo 返回的音频不是合法的 base64"
            ) from exc
        if not audio:
            raise TtsError(TtsErrorKind.protocol, "MiMo 解码后的音频为空")

        container = sniff_container(audio) or self.output_format
        raw_usage = body.get("usage")
        usage: dict[str, object] = raw_usage if isinstance(raw_usage, dict) else {}
        return SynthesizedAudio(
            audio=audio,
            container=container,
            # chat 路径没有时长字段。**不估算**：估出来的时长会让播放回执
            # 与"说完了没"的判断建立在编造的数字上。
            duration_ms=None,
            sample_rate=None,
            provider_request_id=str(body.get("id") or usage.get("id") or "") or None,
        )


__all__ = [
    "DEFAULT_BASE",
    "DEFAULT_MODEL",
    "MAX_VOICE_BYTES",
    "MimoTts",
    "SUPPORTED_FORMATS",
]
