"""三家 TTS 适配器的单元测试。**全部 mock，不调任何付费 API。**

## 为什么测试要写得这么细

三家的失败方式恰好都很"安静"：

- MiniMax：HTTP 200 里带 ``base_resp.status_code=1004``——只看状态码会把
  鉴权失败当成功；
- FishAudio：Base URL 少了 ``/v1`` 会拿到 **200 的 HTML 网页**——看起来成功；
- MiMo：这个路径本质是 chat，模型完全可能**不返回音频而回一段文本**。

这三种都会让"合成成功"的假象一路走到下游，然后在别处炸掉。所以断言的重点
不是"正常路径能跑通"，而是**这些静默失败被拦住**。

用 ``httpx.MockTransport`` 而不是 ``unittest.mock`` 替换 ``post``：它连
**请求体、header、URL** 都一起断言，而那正是各家最容易搞错的地方
（msgpack vs JSON、model 在 header 里）。
"""

from __future__ import annotations

import base64
import json

import httpx
import msgpack
import pytest

from ling.models.fish_audio import DEFAULT_BASE as FISH_DEFAULT_BASE
from ling.models.fish_audio import FishAudioTts
from ling.models.mimo_tts import MimoTts
from ling.models.minimax_tts import INTERNATIONAL_BASE, MiniMaxTts
from ling.models.tts_common import (
    SynthesizedAudio,
    TtsError,
    TtsErrorKind,
    classify_http_error,
    redact,
    scrub,
    sniff_container,
)

# 假密钥：刻意长得像真的（sk- 前缀 + 长串），用来验证它不会进日志/异常消息。
FAKE_KEY = "sk-fake-not-a-real-key-0123456789"

# 最小的合法 WAV 头 + 一点数据（够 sniff_container 认出 wav）
FAKE_WAV = (
    b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)
# 最小的 ID3 头，足够识别成 mp3
FAKE_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00" + b"\x00" * 32


# ============================================================ 共享部件


class TestRedact:
    """密钥脱敏。这一组是"不泄露密钥"要求的直接证据。"""

    def test_sk_dash_prefix_is_redacted(self) -> None:
        """``sk-`` 连字符前缀——**这正是 `logging_setup.redact_value` 漏掉的那个**。

        它只认 ``sk_``（下划线）。而 MiniMax/OpenAI 系用的全是连字符。
        所以适配器层必须自己抹，不能指望日志层那道闸。
        """
        out = redact("api key is sk-abcdef123456 here")
        assert "sk-abcdef123456" not in out
        assert "[REDACTED]" in out

    def test_bearer_token_is_redacted(self) -> None:
        assert "supersecretvalue" not in redact("Authorization: Bearer supersecretvalue")

    def test_jwt_is_redacted(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLWhlcmU"
        assert "eyJhbGciOiJIUzI1NiJ9" not in redact(f"token={jwt}")

    def test_audio_data_url_is_redacted(self) -> None:
        """MiMo 的 data URL **就是一段完整音频**，进日志等于泄漏用户声音。"""
        url = "data:audio/wav;base64," + base64.b64encode(FAKE_WAV).decode()
        out = redact(f"voice={url}")
        assert "base64," not in out.replace("[REDACTED]", "")
        assert "UklGRiQ" not in out

    def test_multiline_is_collapsed(self) -> None:
        """上游 JSON 错误常带换行——一条日志变多行会破坏 JSON 行解析。"""
        assert "\n" not in redact('{"a":1}\n{"b":2}')

    def test_long_text_is_truncated(self) -> None:
        out = redact("x" * 5000, limit=100)
        assert len(out) <= 100
        assert out.endswith("…")

    def test_scrub_removes_exact_secret(self) -> None:
        """形态猜不出时用已知原文兜底。"""
        assert "MY-ODD-SECRET-KEY" not in scrub("failed with MY-ODD-SECRET-KEY", "MY-ODD-SECRET-KEY")

    def test_scrub_ignores_too_short_secret(self) -> None:
        """太短的"密钥"不替换——否则整段文本被毁，排障信息全丢。"""
        assert scrub("abc def", "abc") == "abc def"


class TestClassifyHttpError:
    def test_401_is_authentication_not_generic(self) -> None:
        """401 必须归到 authentication。归到 error 会让用户去查网络。"""
        err = classify_http_error(401, "unauthorized")
        assert err.kind is TtsErrorKind.authentication
        assert err.retriable is False

    def test_429_is_retriable_rate_limit(self) -> None:
        err = classify_http_error(429, "slow down")
        assert err.kind is TtsErrorKind.rate_limited
        assert err.retriable is True

    def test_5xx_is_retriable_unavailable(self) -> None:
        err = classify_http_error(503, "upstream down")
        assert err.kind is TtsErrorKind.unavailable
        assert err.retriable is True

    def test_400_is_not_retriable(self) -> None:
        """重发同一个坏请求只会得到同一个 400。"""
        err = classify_http_error(400, "bad voice id")
        assert err.kind is TtsErrorKind.invalid_request
        assert err.retriable is False

    def test_error_body_does_not_leak_secret(self) -> None:
        """**上游把密钥回显在错误正文里**——这是要防的真实场景。"""
        err = classify_http_error(401, f"invalid key: {FAKE_KEY}", secret=FAKE_KEY)
        assert FAKE_KEY not in err.message
        assert "[REDACTED]" in err.message


class TestSniffContainer:
    def test_recognises_wav_mp3(self) -> None:
        assert sniff_container(FAKE_WAV) == "wav"
        assert sniff_container(FAKE_MP3) == "mp3"

    def test_html_is_not_audio(self) -> None:
        """200 + HTML = 请求没到 API。绝不能当成音频。"""
        assert sniff_container(b"<!DOCTYPE html><html><body>hi") is None

    def test_json_is_not_audio(self) -> None:
        assert sniff_container(b'{"error": "invalid request"}') is None

    def test_empty_is_none(self) -> None:
        assert sniff_container(b"") is None


class TestSynthesizedAudio:
    def test_repr_does_not_include_audio_bytes(self) -> None:
        """有人顺手 ``logger.info("%s", result)`` 时不能把整段音频打印出来。"""
        audio = SynthesizedAudio(audio=b"\x00\x01secretbytes", container="wav")
        assert "secretbytes" not in repr(audio)
        assert f"bytes={len(audio.audio)}" in repr(audio)


# ============================================================ MiniMax


def _minimax_response(
    *,
    audio: bytes = FAKE_MP3,
    status_code_business: int = 0,
    status_msg: str = "success",
    data_status: int = 2,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {"audio": audio.hex(), "status": data_status},
            "extra_info": {"audio_length": 1234, "audio_sample_rate": 32000},
            "base_resp": {"status_code": status_code_business, "status_msg": status_msg},
        },
    )


def _minimax(handler, **kwargs) -> MiniMaxTts:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return MiniMaxTts(
        api_key=FAKE_KEY, voice_id="male-qn-qingse", http=client, **kwargs
    )


class TestMiniMax:
    async def test_成功路径_发出正确的wire形状(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return _minimax_response()

        result = await _minimax(handler).synthesize("你好呀")

        assert captured["url"].endswith("/v1/t2a_v2")
        assert captured["url"].startswith(INTERNATIONAL_BASE)
        assert captured["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
        assert captured["headers"]["content-type"] == "application/json"

        body = captured["body"]
        assert body["model"] == "speech-2.8-hd"
        assert body["text"] == "你好呀"
        assert body["stream"] is False
        # **只要 hex**：url 模式给的未鉴权链接进日志即泄漏
        assert body["output_format"] == "hex"
        assert body["voice_setting"]["voice_id"] == "male-qn-qingse"
        # bitrate 单位是 bit/s，不是 kbit/s
        assert body["audio_setting"]["bitrate"] == 128000

        assert result.container == "mp3"
        assert result.audio == FAKE_MP3
        assert result.duration_ms == 1234
        assert result.sample_rate == 32000

    async def test_HTTP200但业务码非0必须失败(self) -> None:
        """**本模块最核心的一条**：MiniMax 的错误藏在 base_resp 里。"""
        err = await _expect_error(
            _minimax(
                lambda r: _minimax_response(
                    status_code_business=1004, status_msg="invalid api key"
                )
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.authentication
        assert "1004" in err.message
        assert err.retriable is False

    async def test_余额不足给出充值提示而不是稍后重试(self) -> None:
        err = await _expect_error(
            _minimax(
                lambda r: _minimax_response(
                    status_code_business=1008, status_msg="insufficient balance"
                )
            ).synthesize("你好")
        )
        assert "充值" in err.message
        assert err.retriable is False, "余额不足不是'等一下再来'"

    async def test_业务错误正文里的密钥被脱敏(self) -> None:
        err = await _expect_error(
            _minimax(
                lambda r: _minimax_response(
                    status_code_business=1004, status_msg=f"key {FAKE_KEY} rejected"
                )
            ).synthesize("你好")
        )
        assert FAKE_KEY not in err.message
        assert "[REDACTED]" in err.message

    async def test_音频未完成_data_status非2_失败(self) -> None:
        err = await _expect_error(
            _minimax(lambda r: _minimax_response(data_status=1)).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.protocol
        assert "未生成完成" in err.message

    async def test_缺少base_resp不算成功(self) -> None:
        """缺字段时"乐观放行"的结果是把空音频当音频用。"""
        err = await _expect_error(
            _minimax(
                lambda r: httpx.Response(200, json={"data": {"audio": "", "status": 2}})
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.protocol
        assert "base_resp" in err.message

    async def test_hex解不开归类protocol不是error(self) -> None:
        err = await _expect_error(
            _minimax(
                lambda r: httpx.Response(
                    200,
                    json={
                        "data": {"audio": "not-hex-at-all", "status": 2},
                        "base_resp": {"status_code": 0},
                    },
                )
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.protocol
        # 不要回显那串原始 hex（可能几十万字符）
        assert "not-hex-at-allx" not in err.message

    async def test_401归鉴权并提示站点(self) -> None:
        err = await _expect_error(
            _minimax(lambda r: httpx.Response(401, text="unauthorized")).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.authentication
        assert "站点" in err.message, "国际站/中文站密钥不通用，提示要带上"

    async def test_超时分类正确(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        err = await _expect_error(_minimax(handler).synthesize("你好"))
        assert err.kind is TtsErrorKind.timeout
        assert err.retriable is True

    async def test_未配密钥不发请求(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return _minimax_response()

        adapter = _minimax(handler)
        adapter._key = ""  # noqa: SLF001
        err = await _expect_error(adapter.synthesize("你好"))
        assert err.kind is TtsErrorKind.not_configured
        assert called is False, "没密钥就不该发请求"

    async def test_未配音色不发请求(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return _minimax_response()

        adapter = _minimax(handler)
        adapter.voice_id = ""
        err = await _expect_error(adapter.synthesize("你好"))
        assert err.kind is TtsErrorKind.not_configured
        assert "Voice ID" in err.message
        assert called is False

    async def test_空文本拒绝(self) -> None:
        err = await _expect_error(_minimax(lambda r: _minimax_response()).synthesize("   "))
        assert err.kind is TtsErrorKind.invalid_request

    async def test_emotion只在配置时发出(self) -> None:
        """老模型收到 emotion 会 400，所以默认不发。"""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _minimax_response()

        plain = _minimax(handler)
        await plain.synthesize("你好")
        assert "emotion" not in bodies[-1]["voice_setting"]

        with_emotion = _minimax(handler, emotion="happy")
        await with_emotion.synthesize("你好")
        assert bodies[-1]["voice_setting"]["emotion"] == "happy"

    async def test_language_boost可传(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _minimax_response()

        await _minimax(handler).synthesize("你好", language_boost="zh")
        assert bodies[-1]["language_boost"] == "zh"


# ============================================================ FishAudio

VALID_REF = "0123456789abcdef0123456789abcdef"


def _fish(handler, **kwargs) -> FishAudioTts:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return FishAudioTts(
        api_key=FAKE_KEY, reference_id=VALID_REF, http=client, **kwargs
    )


class TestFishAudio:
    async def test_成功路径_请求体是msgpack且模型在header(self) -> None:
        """两处最容易错的地方：**msgpack 不是 JSON**、**model 在 header**。"""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["raw"] = request.content
            return httpx.Response(
                200, headers={"content-type": "audio/mpeg"}, content=FAKE_MP3
            )

        result = await _fish(handler).synthesize("你好呀")

        assert captured["url"].endswith("/v1/tts")
        assert captured["headers"]["content-type"] == "application/msgpack"
        assert captured["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
        # **模型是 header**，不是请求体字段
        assert captured["headers"]["model"] == "s2.1-pro"

        # 请求体必须能被 msgpack 解开，且解出来不是 JSON 文本
        payload = msgpack.unpackb(captured["raw"], raw=False)
        assert payload["text"] == "你好呀"
        assert payload["reference_id"] == VALID_REF
        assert payload["format"] == "mp3"
        assert payload["normalize"] is True
        assert "model" not in payload, "模型不该出现在请求体里"
        # mp3 码率单位是 kbit/s
        assert payload["mp3_bitrate"] == 128

        assert result.container == "mp3"
        assert result.audio == FAKE_MP3

    async def test_reference_id格式错在发请求前就拦下(self) -> None:
        """格式错的 ID 到供应商那边只会得到一个含糊的 400。"""
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=FAKE_MP3)

        adapter = _fish(handler)
        adapter.reference_id = "too-short"
        err = await _expect_error(adapter.synthesize("你好"))
        assert err.kind is TtsErrorKind.invalid_request
        assert "32 位十六进制" in err.message
        assert called is False, "格式错就不该发出去"

    async def test_200加HTML网页报Base_URL缺v1(self) -> None:
        """DNS 劫持/少写 /v1 的典型症状——绝不能当成音频。"""
        err = await _expect_error(
            _fish(
                lambda r: httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b"<!DOCTYPE html><html><body>not api</body></html>",
                )
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.invalid_request
        assert "/v1" in err.message
        assert "<!DOCTYPE" not in err.message, "不要把整页 HTML 回显"

    async def test_200但不是音频也不是HTML报protocol(self) -> None:
        err = await _expect_error(
            _fish(
                lambda r: httpx.Response(
                    200, headers={"content-type": "application/json"}, json={"err": "x"}
                )
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.protocol
        assert "非音频响应" in err.message

    async def test_401归鉴权(self) -> None:
        err = await _expect_error(
            _fish(lambda r: httpx.Response(401, json={"message": "bad key"})).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.authentication

    async def test_错误正文里的密钥被脱敏(self) -> None:
        err = await _expect_error(
            _fish(lambda r: httpx.Response(401, text=f"invalid {FAKE_KEY}")).synthesize("你好")
        )
        assert FAKE_KEY not in err.message

    async def test_空音频流失败(self) -> None:
        err = await _expect_error(
            _fish(
                lambda r: httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"")
            ).synthesize("你好")
        )
        assert err.kind is TtsErrorKind.protocol
        assert "空" in err.message

    async def test_超时分类(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("nope", request=request)

        err = await _expect_error(_fish(handler).synthesize("你好"))
        assert err.kind is TtsErrorKind.timeout

    async def test_base_url自动补v1(self) -> None:
        assert FishAudioTts._normalize_base("https://api.fish.audio") == "https://api.fish.audio/v1"
        assert FishAudioTts._normalize_base("https://x.org/v1") == "https://x.org/v1"
        assert FishAudioTts._normalize_base("https://x.org/api/v2") == "https://x.org/api/v2"
        assert FishAudioTts._normalize_base("") == FISH_DEFAULT_BASE

    async def test_model为auto时不发送(self) -> None:
        """``auto`` 是上层语义，发给供应商只会 model_not_found。"""
        headers_seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            headers_seen.append(dict(request.headers))
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=FAKE_MP3)

        adapter = _fish(handler)
        adapter.model = ""
        await adapter.synthesize("你好")
        assert "model" not in headers_seen[-1]

    async def test_未配密钥不发请求(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=FAKE_MP3)

        adapter = _fish(handler)
        adapter._key = ""  # noqa: SLF001
        err = await _expect_error(adapter.synthesize("你好"))
        assert err.kind is TtsErrorKind.not_configured
        assert called is False


# ============================================================ MiMo


def _mimo_response(audio: bytes = FAKE_WAV, **message_extra) -> httpx.Response:  # noqa: ANN003
    message: dict = {"role": "assistant", "content": ""}
    message.update(message_extra)
    if "audio" not in message:
        message["audio"] = {"data": base64.b64encode(audio).decode("ascii")}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-fake-123",
            "model": "mimo-v2.5-tts-voiceclone",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        },
    )


def _mimo(handler, **kwargs) -> MimoTts:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return MimoTts(api_key=FAKE_KEY, http=client, **kwargs)


class TestMimo:
    async def test_成功路径_参考音频进data_url且文本在assistant位置(self) -> None:
        """三处关键：**走 chat/completions**、**音色是 data URL**、
        **要读的文本放 assistant**（放 user 位置会被当成问题去回答）。"""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return _mimo_response()

        result = await _mimo(handler, style_context="温柔一点").synthesize(
            "你好呀", voice_audio=FAKE_WAV
        )

        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
        body = captured["body"]
        assert body["model"] == "mimo-v2.5-tts-voiceclone"
        # 音色是每次随请求上传的 data URL，不是预注册 ID
        voice = body["audio"]["voice"]
        assert voice.startswith("data:audio/wav;base64,")
        assert base64.b64decode(voice.split(",", 1)[1]) == FAKE_WAV

        messages = body["messages"]
        assert messages[0] == {"role": "user", "content": "温柔一点"}
        assert messages[-1] == {"role": "assistant", "content": "你好呀"}

        assert result.container == "wav"
        assert result.audio == FAKE_WAV
        assert result.provider_request_id == "chatcmpl-fake-123"

    async def test_没有style_context时不发user消息(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _mimo_response()

        await _mimo(handler).synthesize("你好", voice_audio=FAKE_WAV)
        assert [m["role"] for m in bodies[-1]["messages"]] == ["assistant"]

    async def test_模型回文本而不是音频时必须失败(self) -> None:
        """这条路径本质是 chat：模型完全可能拒绝并回一段话。

        静默当成"没有音频"是最糟的——用户只看到没声音，不知道是被拒了。
        """
        err = await _expect_error(
            _mimo(
                lambda r: _mimo_response(audio=b"", content="我无法生成音频")
            ).synthesize("你好", voice_audio=FAKE_WAV)
        )
        assert err.kind is TtsErrorKind.protocol
        assert "我无法生成音频" in err.message, "要把模型说了什么带出来"

    async def test_非stop的finish_reason被带出(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "content_filter",
                        }
                    ]
                },
            )

        err = await _expect_error(_mimo(handler).synthesize("你好", voice_audio=FAKE_WAV))
        assert err.kind is TtsErrorKind.protocol
        assert "content_filter" in err.message

    async def test_参考音频mime以嗅探为准(self) -> None:
        """**关键**：.wav 后缀的 mp3 若声明成 audio/wav，服务端会按 WAV 解码，
        结果是噪音而不是错误——这种"成功但听不出是话"最难查。"""

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["audio"]["voice"].startswith("data:audio/mpeg;base64,"), (
                "应该按嗅探出的 mp3 声明 mime，而不是用户给的 audio/wav"
            )
            return _mimo_response()

        await _mimo(handler).synthesize(
            "你好", voice_audio=FAKE_MP3, voice_mime="audio/wav"
        )

    async def test_参考音频无法嗅探时用声明mime(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["audio"]["voice"].startswith("data:audio/ogg;base64,")
            return _mimo_response()

        # 一段"嗅探不出容器但有声明 mime"的裸数据。刻意用**不能**被
        # 裸 PCM 启发式认出的形状（高字节均匀分布），否则会走 pcm 分支、
        # 测不到这里真正要验的"用声明 mime 兜底"。
        await _mimo(handler).synthesize(
            "你好", voice_audio=b"\x99\x88\x77\x66" * 128, voice_mime="audio/ogg"
        )

    async def test_参考音频无法识别且无声明mime时拒绝(self) -> None:
        err = await _expect_error(
            _mimo(lambda r: _mimo_response()).synthesize(
                "你好", voice_audio=b"\x99\x88\x77\x66" * 128
            )
        )
        assert err.kind is TtsErrorKind.invalid_request
        assert "无法识别" in err.message

    async def test_没有参考音频时明确报错(self) -> None:
        """MiMo 的音色来自参考音频——没有它就不能合成，不能静默用别人的声音。"""
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return _mimo_response()

        err = await _expect_error(_mimo(handler).synthesize("你好"))
        assert err.kind is TtsErrorKind.not_configured
        assert "参考音频" in err.message
        assert called is False

    async def test_参考音频过大被拒(self) -> None:
        err = await _expect_error(
            _mimo(lambda r: _mimo_response()).synthesize(
                "你好", voice_audio=b"RIFF" + b"\x00" * (11 * 1024 * 1024)
            )
        )
        assert err.kind is TtsErrorKind.invalid_request
        assert "过大" in err.message

    async def test_base64不合法时失败且不回显音频(self) -> None:
        err = await _expect_error(
            _mimo(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"audio": {"data": "!!!not-base64!!!"}}}
                        ]
                    },
                )
            ).synthesize("你好", voice_audio=FAKE_WAV)
        )
        assert err.kind is TtsErrorKind.protocol
        assert "!!!not-base64!!!" not in err.message

    async def test_401归鉴权(self) -> None:
        err = await _expect_error(
            _mimo(lambda r: httpx.Response(401, text="unauthorized")).synthesize(
                "你好", voice_audio=FAKE_WAV
            )
        )
        assert err.kind is TtsErrorKind.authentication

    async def test_异常消息里不含参考音频(self) -> None:
        """一个报错不能把用户的声音带进日志。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        err = await _expect_error(_mimo(handler).synthesize("你好", voice_audio=FAKE_WAV))
        assert err.kind is TtsErrorKind.unavailable
        assert "UklGRiQ" not in err.message

    async def test_未配密钥不发请求(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return _mimo_response()

        adapter = _mimo(handler)
        adapter._key = ""  # noqa: SLF001
        err = await _expect_error(adapter.synthesize("你好", voice_audio=FAKE_WAV))
        assert err.kind is TtsErrorKind.not_configured
        assert called is False

    async def test_单次style_context覆盖默认值且不残留(self) -> None:
        """覆盖必须是单次的：一次带风格的调用不能污染后续所有调用。"""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _mimo_response()

        adapter = _mimo(handler, style_context="默认风格")
        await adapter.synthesize("一", voice_audio=FAKE_WAV, style_context="临时风格")
        await adapter.synthesize("二", voice_audio=FAKE_WAV)

        assert bodies[0]["messages"][0]["content"] == "临时风格"
        assert bodies[1]["messages"][0]["content"] == "默认风格", "单次覆盖泄漏到了下一次"


# ============================================================ 三家共性


class TestCrossAdapterInvariants:
    """三家各自的实现，但契约必须一致。"""

    @pytest.mark.parametrize(
        ("adapter_factory", "call"),
        [
            (lambda h: _minimax(h), lambda a: a.synthesize("你好")),
            (lambda h: _fish(h), lambda a: a.synthesize("你好")),
            (
                lambda h: _mimo(h),
                lambda a: a.synthesize("你好", voice_audio=FAKE_WAV),
            ),
        ],
        ids=["minimax", "fish_audio", "mimo"],
    )
    async def test_三家都返回统一的SynthesizedAudio(self, adapter_factory, call) -> None:  # noqa: ANN001
        """调用方不该关心某家返回的是 hex、二进制流还是 base64。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/v1/t2a_v2"):
                return _minimax_response(audio=FAKE_MP3)
            if str(request.url).endswith("/v1/tts"):
                return httpx.Response(
                    200, headers={"content-type": "audio/mpeg"}, content=FAKE_MP3
                )
            return _mimo_response(audio=FAKE_WAV)

        adapter = adapter_factory(handler)
        result = await call(adapter)
        assert isinstance(result, SynthesizedAudio)
        assert result.audio
        assert result.container in {"wav", "mp3", "pcm", "flac", "ogg", "m4a"}
        # 容器是按**实际字节**判的，不是照抄请求参数
        assert sniff_container(result.audio) in {None, result.container}

    @pytest.mark.parametrize(
        ("adapter_factory", "call"),
        [
            (lambda h: _minimax(h), lambda a: a.synthesize("你好")),
            (lambda h: _fish(h), lambda a: a.synthesize("你好")),
            (
                lambda h: _mimo(h),
                lambda a: a.synthesize("你好", voice_audio=FAKE_WAV),
            ),
        ],
        ids=["minimax", "fish_audio", "mimo"],
    )
    async def test_三家在401时都归到authentication(self, adapter_factory, call) -> None:
        """同一个 401 在三家必须是同一个语义，否则用户提示会各处不同。"""
        adapter = adapter_factory(lambda r: httpx.Response(401, text="unauthorized"))
        err = await _expect_error(call(adapter))
        assert err.kind is TtsErrorKind.authentication
        assert err.retriable is False


async def _expect_error(coro) -> TtsError:  # noqa: ANN001
    """断言抛出 :class:`TtsError` 并返回它。"""
    with pytest.raises(TtsError) as info:
        await coro
    return info.value
