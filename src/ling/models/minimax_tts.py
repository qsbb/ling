"""MiniMax TTS 适配器：JSON 进，**hex 字符串**音频出。

## 这家最容易踩的三个坑

1. **HTTP 200 不代表成功。** MiniMax 的错误放在响应体里：
   ``{"base_resp": {"status_code": 1004, "status_msg": "..."}}``，
   而 HTTP 状态码仍是 200。只看 ``response.status_code`` 会把一次鉴权失败
   当成成功，然后把空音频交给下游——下游报的是"解码失败"，离真正的原因
   隔了整整一层。这是本模块最核心的一条（见 :func:`_check_business_status`）。

2. **音频可能是 ``hex`` 字符串，也可能是 ``url``。** 默认 ``output_format="hex"``
   时音频在 ``data.audio`` 里是一长串十六进制；设成 ``"url"`` 时给的是一个
   24 小时有效的链接。**只做 hex**，理由是 URL 模式要额外发一次 HTTP 去取，
   而那个链接是**未鉴权**的——它一旦进日志就等于音频泄漏。想省事反而更危险。

3. **``data.status`` 与 ``base_resp.status_code`` 是两回事。** 前者描述音频
   生成进度（2 = 完成），后者是业务错误码。两个都要看：只看后者时，
   一个"成功但没有音频"的响应会静默产出空字节。

## wire 协议

::

    POST https://api.minimax.io/v1/t2a_v2      （国际站）
    POST https://api.minimax.cn/v1/t2a_v2      （中文站）
    Authorization: Bearer <key>
    Content-Type: application/json

    {
      "model": "speech-2.8-hd",
      "text": "要读的话",
      "stream": false,
      "voice_setting": {"voice_id": "...", "speed": 1.0, "vol": 1.0, "pitch": 0},
      "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000},
      "output_format": "hex"
    }

    → {"data": {"audio": "<hex>", "status": 2},
       "extra_info": {"audio_length": 1234, "audio_sample_rate": 32000},
       "base_resp": {"status_code": 0, "status_msg": "success"}}

## 音色

**用户自己填 ``voice_id``**（MiniMax 的预注册音色 ID，如 ``male-qn-qingse``）。
与 MiMo 的"每次传参考音频"完全不同——所以这是两个独立的适配器，不是一个
带开关的适配器。
"""

from __future__ import annotations

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

#: 国际站与中文站是两个**独立**的账号体系，密钥不通用。用户选错站点的表现
#: 是 401，而 401 的提示是"检查密钥"——所以两个地址都要能填。
INTERNATIONAL_BASE = "https://api.minimax.io"
CHINA_BASE = "https://api.minimax.cn"

#: 文档里列出的模型。**不做白名单硬拦**：MiniMax 上新模型比我们发版快，
#: 硬拦会让用户"配置成功但调不了"。这里只用于设置页的默认值与下拉提示。
KNOWN_MODELS = (
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "speech-2.6-hd",
    "speech-2.6-turbo",
    "speech-02-hd",
    "speech-02-turbo",
    "speech-01-hd",
    "speech-01-turbo",
)

#: 支持的情绪（``voice_setting.emotion``）。传空字符串 = 不加情绪控制。
#: 这是 2.x 才有的能力：老模型收到会报 400，所以默认不发。
KNOWN_EMOTIONS = (
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
    "neutral",
    "calm",
    "fluent",
)

#: 音频格式 → 默认采样率。MiniMax 对每种容器有各自允许的采样率集合，
#: 填错的组合会 400。这里给的是文档里的常用值。
DEFAULT_SAMPLE_RATE = {
    "mp3": 32000,
    "wav": 32000,
    "pcm": 24000,
    "flac": 32000,
    "opus": 32000,
}

#: 业务错误码 → 我们的分类。**只收能确定的**，其余落到 invalid_request。
#:
#: 1004 = 鉴权失败；1008 = 余额不足；1013 = 内部错误；1039 = 文本过长；
#: 2013 = 参数不合法。余额不足刻意**不**归到 rate_limited：它是"去充值"，
#: 不是"等一下再来"。
_BUSINESS_CODES: dict[int, TtsErrorKind] = {
    1004: TtsErrorKind.authentication,
    1008: TtsErrorKind.rate_limited,  # 余额/配额：不可立刻重试，但也不是我们的错
    1013: TtsErrorKind.unavailable,
    1039: TtsErrorKind.invalid_request,
    2013: TtsErrorKind.invalid_request,
}

#: 余额不足说的不是"稍后重试"，而是"去充值"。单列出来才能给出对的话。
_BALANCE_CODES = frozenset({1008})


class MiniMaxTts:
    """``ProviderKind.minimax_tts`` 的实现。

    无状态、可并发复用：每次 :meth:`synthesize` 自己做一次请求，
    不缓存任何与用户相关的数据。
    """

    provider_id = "minimax_tts"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "speech-2.8-hd",
        voice_id: str = "",
        base_url: str = INTERNATIONAL_BASE,
        output_format: str = "mp3",
        sample_rate: int | None = None,
        speed: float = 1.0,
        volume: float = 1.0,
        pitch: int = 0,
        emotion: str = "",
        timeout_seconds: float = 60.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = (api_key or "").strip()
        self.model = model or "speech-2.8-hd"
        self.voice_id = (voice_id or "").strip()
        self._base = (base_url or INTERNATIONAL_BASE).rstrip("/")
        self.output_format = (output_format or "mp3").lower()
        self.sample_rate = sample_rate or DEFAULT_SAMPLE_RATE.get(self.output_format, 32000)
        self.speed = speed
        self.volume = volume
        self.pitch = pitch
        self.emotion = (emotion or "").strip()
        self._timeout = timeout_seconds
        self._http = http

    # -------------------------------------------------------------- 请求构造

    def build_payload(self, text: str, *, voice_id: str = "") -> dict[str, object]:
        """拼请求体。**独立成方法**，这样测试可以只验 wire 形状不碰网络。"""
        voice = (voice_id or self.voice_id).strip()
        voice_setting: dict[str, object] = {
            "voice_id": voice,
            "speed": self.speed,
            "vol": self.volume,
            "pitch": self.pitch,
        }
        # **只在配了情绪时才发**：老模型（speech-01/02）收到 emotion 会 400，
        # 而用户不一定知道自己选的是哪一代。
        if self.emotion:
            voice_setting["emotion"] = self.emotion
        return {
            "model": self.model,
            "text": text,
            # 非流式：一次拿到完整音频。流式那个是 WebSocket，另一套协议，
            # 本适配器不做（见模块注释）。
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "format": self.output_format,
                "sample_rate": self.sample_rate,
                # 单位是 **bit/s**（128000），不是 kbit/s。Fish 那边是 kbit/s，
                # 两家的单位不一样，混用会得到一个 400 或者一段 1000 倍的码率。
                "bitrate": 128000,
                "channel": 1,
            },
            # 只要 hex：url 模式会给一个未鉴权链接，日志里出现即泄漏。
            "output_format": "hex",
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------- 主流程

    async def synthesize(
        self, text: str, *, voice_id: str = "", language_boost: str = ""
    ) -> SynthesizedAudio:
        """把 ``text`` 合成成音频。

        ``language_boost`` 是 MiniMax 的语种增强（``"zh"`` / ``"auto"`` 等），
        中文角色建议传 ``"zh"``：不传时它按文本自行判断，中英混排容易读错音。
        """
        spoken = (text or "").strip()
        if not spoken:
            raise TtsError(TtsErrorKind.invalid_request, "合成文本为空", retriable=False)
        if not self._key:
            raise TtsError(
                TtsErrorKind.not_configured,
                "MiniMax API Key 未配置，请在设置里填写后重试",
                retriable=False,
            )
        voice = (voice_id or self.voice_id).strip()
        if not voice:
            raise TtsError(
                TtsErrorKind.not_configured,
                "MiniMax Voice ID 未配置（需填 MiniMax 控制台里的音色 ID，如 male-qn-qingse）",
                retriable=False,
            )

        payload = self.build_payload(spoken, voice_id=voice)
        if language_boost:
            payload["language_boost"] = language_boost

        url = f"{self._base}/v1/t2a_v2"
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
                    f"MiniMax 合成超时（{self._timeout:.0f}s）",
                ) from exc
            except httpx.HTTPError as exc:
                raise TtsError(
                    TtsErrorKind.unavailable,
                    f"MiniMax 不可达：{type(exc).__name__}",
                ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code != 200:
            raise classify_http_error(
                response.status_code,
                response.text,
                secret=self._key,
                provider="MiniMax",
                hint="请检查 API Key 与站点（国际站 api.minimax.io / 中文站 api.minimax.cn 的密钥不通用）"
                if response.status_code in (401, 403)
                else "",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TtsError(
                TtsErrorKind.protocol,
                f"MiniMax 响应不是 JSON：{scrub(redact(response.text, limit=120), self._key)}",
            ) from exc
        if not isinstance(body, dict):
            raise TtsError(TtsErrorKind.protocol, "MiniMax 响应顶层不是对象")

        # **先看业务状态码**——HTTP 200 + base_resp.status_code=1004 是鉴权失败。
        self._check_business_status(body)

        audio = self._extract_audio(body)
        container = sniff_container(audio)
        if container is None:
            raise TtsError(
                TtsErrorKind.protocol,
                "MiniMax 返回的音频无法识别容器格式（可能不是音频）",
            )
        extra = body.get("extra_info") or {}
        return SynthesizedAudio(
            audio=audio,
            container=container,
            duration_ms=_int_or_none(extra.get("audio_length")),
            sample_rate=_int_or_none(extra.get("audio_sample_rate")),
            provider_request_id=str(extra.get("trace_id") or "") or None,
        )

    # -------------------------------------------------------------- 响应解析

    def _check_business_status(self, body: dict) -> None:
        """``base_resp.status_code`` 非 0 就是失败——**哪怕 HTTP 是 200**。"""
        base_resp = body.get("base_resp")
        if not isinstance(base_resp, dict):
            # 没有 base_resp 的响应本身就不符合协议。**不当作成功**：
            # 缺字段时"乐观放行"的结果是把空音频当音频用。
            raise TtsError(
                TtsErrorKind.protocol, "MiniMax 响应缺少 base_resp 字段"
            )
        raw_code = base_resp.get("status_code", 0)
        try:
            code = int(raw_code)
        except (TypeError, ValueError) as exc:
            raise TtsError(
                TtsErrorKind.protocol,
                f"MiniMax base_resp.status_code 不是整数：{redact(raw_code, limit=40)}",
            ) from exc
        if code == 0:
            return
        # status_msg 可能回显请求内容，也可能带密钥——一律过脱敏。
        detail = scrub(redact(base_resp.get("status_msg", ""), limit=200), self._key)
        kind = _BUSINESS_CODES.get(code, TtsErrorKind.invalid_request)
        message = f"MiniMax 合成失败（业务码 {code}）：{detail or '（无说明）'}"
        retriable: bool | None = None
        if code in _BALANCE_CODES:
            message += "。账户余额或额度不足，请到 MiniMax 控制台充值后重试"
            # **余额不足不可重试**，虽然它映射的 kind（rate_limited）默认可重试。
            # 这是"同一 kind、不同可重试性"的真实例子：限流是"等一会儿再来"，
            # 欠费是"去充值"——重试策略若按 kind 一刀切，欠费会被反复重打。
            retriable = False
        elif kind is TtsErrorKind.authentication:
            message += "。请检查 API Key 与所选站点"
        raise TtsError(kind, message, status_code=200, retriable=retriable)

    def _extract_audio(self, body: dict) -> bytes:
        """从 ``data.audio`` 取 hex 并解码成字节。"""
        data = body.get("data")
        if not isinstance(data, dict):
            raise TtsError(TtsErrorKind.protocol, "MiniMax 响应缺少 data 字段")
        # data.status 描述生成进度：2 = 完成。**不看这个字段**的话，
        # 一个"成功但还没生成完"的响应会得到空 audio。
        status = data.get("status")
        if status is not None and _int_or_none(status) != 2:
            raise TtsError(
                TtsErrorKind.protocol,
                f"MiniMax 音频未生成完成（data.status={redact(status, limit=20)}）",
            )
        raw = data.get("audio")
        if not isinstance(raw, str) or not raw:
            raise TtsError(TtsErrorKind.protocol, "MiniMax 响应里没有音频数据（data.audio 为空）")
        try:
            audio = bytes.fromhex(raw.strip())
        except ValueError as exc:
            # **不要**把 raw 拼进消息：它可能有几十万字符。
            raise TtsError(
                TtsErrorKind.protocol,
                f"MiniMax 返回的音频不是合法的 hex（长度 {len(raw)}）",
            ) from exc
        if not audio:
            raise TtsError(TtsErrorKind.protocol, "MiniMax 解码后的音频为空")
        return audio


def _int_or_none(value: object) -> int | None:
    """把上游的"数字"字段转成 int，转不了返回 None。

    **不吞 bool**：``int(True) == 1`` 会让 ``data.status=true`` 被当成 1
    （未完成），而 JSON 里出现 bool 说明对方没按约定发数字——那种情况该
    落到"格式不对"，不是静默按 1 处理。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    if isinstance(value, float):
        return int(value)
    return None


__all__ = [
    "CHINA_BASE",
    "DEFAULT_SAMPLE_RATE",
    "INTERNATIONAL_BASE",
    "KNOWN_EMOTIONS",
    "KNOWN_MODELS",
    "MiniMaxTts",
]
