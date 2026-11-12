"""文件识别协议与装配回归，所有 HTTP 请求由受控 transport 处理。"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from ling.domains.speech.asr_providers import build_asr_provider, resolve_asr
from ling.models.dashscope_asr import DashScopeAsr
from ling.models.siliconflow_asr import SiliconFlowAsr
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig, ProviderModel
from ling.ports.speech import (
    AsrRequest,
    AudioInput,
    AudioSpec,
    SpeechCall,
    SpeechError,
    SpeechErrorKind,
)
from ling.settings.service import _provider_shape_issues

KEY = "credential-for-asr-fixture"
AUDIO = AudioInput(
    content=b"RIFF-file-recognition-fixture",
    filename="../../private.wav",
    spec=AudioSpec(
        mime_type="audio/wav", container="wav", codec="pcm_s16le",
        sample_rate_hz=16000, channels=1,
    ),
)
CALL = SpeechCall("op-fixture", "attempt-fixture", "window-fixture", None, 9)


def _adapter(kind: str, client: httpx.AsyncClient):
    cls = SiliconFlowAsr if kind == "siliconflow_asr" else DashScopeAsr
    return cls(provider_id="fixture", api_key=KEY, http=client)


def _response(kind: str, text: str = "识别输出应该进入正式输入") -> dict:
    return {"text": text} if kind == "siliconflow_asr" else {"output": {"text": text}}


async def test_siliconflow_uploads_binary_multipart_with_bound_model() -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == "https://api.siliconflow.cn/v1/audio/transcriptions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        mime = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
            + await request.aread()
        )
        fields = {
            part.get_param("name", header="content-disposition"): part
            for part in mime.iter_parts()
        }
        assert set(fields) == {"file", "model"}
        assert fields["model"].get_payload(decode=True) == b"fixture-recognizer-42"
        assert fields["file"].get_payload(decode=True) == AUDIO.content
        assert fields["file"].get_filename() == "audio.wav"
        return httpx.Response(
            200, json={"text": "听见了测试短语甲"},
            headers={"x-siliconcloud-trace-id": "provider-trace-fixture"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _adapter("siliconflow_asr", client).transcribe(
            AsrRequest("fixture-recognizer-42", AUDIO), call=CALL
        )
    assert len(seen) == 1
    assert result.text == "听见了测试短语甲"
    assert result.provider_request_id == "provider-trace-fixture"
    assert result.confidence is None and result.language is None
    assert result.usage.audio_seconds is None
    assert result.usage.input_tokens is None
    assert result.usage.usage_source == "unknown"


@pytest.mark.parametrize("base", [
    "https://workspace.example.test",
    "https://workspace.example.test/api/v1/",
    "https://workspace.example.test/api/v1/services/aigc/multimodal-generation/generation",
])
async def test_dashscope_uses_json_data_uri_and_native_result(base: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://workspace.example.test/api/v1/services/aigc/multimodal-generation/generation"
        )
        assert request.headers["content-type"] == "application/json"
        assert request.headers["x-dashscope-sse"] == "disable"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        assert body["model"] == "qwen-audio-3.0-asr-flash"
        audio = body["input"]["messages"][0]["content"][0]
        assert audio["type"] == "input_audio"
        assert audio["input_audio"]["data"] == (
            "data:audio/wav;base64," + base64.b64encode(AUDIO.content).decode()
        )
        assert body["parameters"] == {
            "format": "wav", "sample_rate": "16000", "language_hints": ["zh"],
            "vocabulary": {"测试专名": 3},
        }
        return httpx.Response(200, json={
            "output": {"text": "听见了测试短语乙"},
            "usage": {"duration": 2.5, "input_tokens": 8, "output_tokens": 4},
            "request_id": "dashscope-request-fixture",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DashScopeAsr(
            provider_id="dash", api_key=KEY, base_url=base, http=client
        ).transcribe(
            AsrRequest("qwen-audio-3.0-asr-flash", AUDIO, language="zh", vocabulary=("测试专名",)),
            call=CALL,
        )
    assert result.text == "听见了测试短语乙"
    assert result.provider_request_id == "dashscope-request-fixture"
    assert result.language is None and result.confidence is None
    assert result.usage.audio_seconds == 2.5
    assert result.usage.input_tokens == 8
    assert result.usage.output_tokens == 4
    assert result.usage.usage_source == "provider"


@pytest.mark.parametrize("kind", ["siliconflow_asr", "dashscope_asr"])
@pytest.mark.parametrize(("status", "error", "retriable"), [
    (401, SpeechErrorKind.authentication, False),
    (403, SpeechErrorKind.authentication, False),
    (404, SpeechErrorKind.invalid_request, False),
    (413, SpeechErrorKind.invalid_request, False),
    (429, SpeechErrorKind.rate_limited, True),
    (503, SpeechErrorKind.unavailable, True),
])
async def test_http_errors_have_typed_safe_messages(kind, status, error, retriable) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text=f"echo {KEY} data:audio/wav;base64,secret-audio")
    )) as client:
        with pytest.raises(SpeechError) as caught:
            await _adapter(kind, client).transcribe(AsrRequest("fixture-asr", AUDIO), call=CALL)
    assert caught.value.kind == error
    assert caught.value.retriable is retriable
    assert KEY not in str(caught.value)
    assert "secret-audio" not in str(caught.value)


@pytest.mark.parametrize("kind", ["siliconflow_asr", "dashscope_asr"])
async def test_blank_result_is_unintelligible_without_claiming_silence(kind: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=_response(kind, " \n "))
    )) as client:
        with pytest.raises(SpeechError) as caught:
            await _adapter(kind, client).transcribe(AsrRequest("fixture-asr", AUDIO), call=CALL)
    assert caught.value.kind == SpeechErrorKind.unintelligible


@pytest.mark.parametrize("kind", ["siliconflow_asr", "dashscope_asr"])
@pytest.mark.parametrize("body", [[], {}, {"code": "InvalidApiKey", "message": KEY}])
async def test_malformed_or_business_error_cannot_be_a_success(kind: str, body) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body)
    )) as client:
        with pytest.raises(SpeechError) as caught:
            await _adapter(kind, client).transcribe(AsrRequest("fixture-asr", AUDIO), call=CALL)
    assert caught.value.kind == SpeechErrorKind.protocol
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("kind", ["siliconflow_asr", "dashscope_asr"])
async def test_timeout_does_not_authorize_automatic_retry(kind: str) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("possible upstream charge", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SpeechError) as caught:
            await _adapter(kind, client).transcribe(AsrRequest("fixture-asr", AUDIO), call=CALL)
    assert caught.value.kind == SpeechErrorKind.timeout
    assert caught.value.retriable is False


@pytest.mark.parametrize(("language", "vocabulary"), [("zh", ()), (None, ("测试",))])
async def test_siliconflow_rejects_unsupported_options_before_http(language, vocabulary) -> None:
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: requests.append(req) or httpx.Response(200, json={"text": "unused"})
    )) as client:
        with pytest.raises(SpeechError) as caught:
            await _adapter("siliconflow_asr", client).transcribe(
                AsrRequest("fixture-asr", AUDIO, language=language, vocabulary=vocabulary), call=CALL
            )
    assert caught.value.kind == SpeechErrorKind.unsupported
    assert requests == []


@pytest.mark.parametrize("kind", ["siliconflow_asr", "dashscope_asr"])
async def test_empty_audio_and_translation_are_rejected_without_http(kind: str) -> None:
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: requests.append(req) or httpx.Response(200, json=_response(kind))
    )) as client:
        for request, expected in [
            (AsrRequest("fixture-asr", replace(AUDIO, content=b"")), SpeechErrorKind.invalid_audio),
            (AsrRequest("fixture-asr", AUDIO, task="translate_en"), SpeechErrorKind.unsupported),
        ]:
            with pytest.raises(SpeechError) as caught:
                await _adapter(kind, client).transcribe(request, call=CALL)
            assert caught.value.kind == expected
    assert requests == []


def _config(kind: str = "siliconflow_asr") -> EffectiveConfig:
    provider = ProviderConfig(
        provider_id="recognition", kind=kind, has_secret=True,
        timeout_seconds=42, models=(ProviderModel(model_id="bound-model"),),
    )
    return EffectiveConfig(revision=9, providers={"recognition": provider}, profiles={
        "speech.asr": ProfileBinding("speech.asr", "recognition", "bound-model")
    })


@pytest.mark.parametrize(("kind", "expected_class"), [
    ("siliconflow_asr", SiliconFlowAsr), ("dashscope_asr", DashScopeAsr),
])
async def test_factory_uses_binding_and_secret_without_network(kind, expected_class) -> None:
    read = []

    async def secret(provider_id):
        read.append(provider_id)
        return KEY

    view = await build_asr_provider(_config(kind), secret_reader=secret)
    assert view.ok and view.configured
    assert type(view.provider) is expected_class
    assert view.provider_id == "recognition" and view.model_id == "bound-model"
    assert view.provider._timeout == 42
    assert view.capabilities.file_transcription is True
    assert view.capabilities.realtime_input is False
    assert read == ["recognition"]
    assert KEY not in repr(view)
    assert KEY not in repr(view.provider)


async def test_factory_fails_closed_for_missing_binding_secret_disabled_and_chat_kind() -> None:
    no_binding = _config()
    no_binding.profiles.clear()
    disabled = _config()
    disabled.profiles["speech.asr"] = replace(disabled.profiles["speech.asr"], enabled=False)
    for config in (None, no_binding, disabled, _config("openai_compatible"), _config()):
        view = await build_asr_provider(config)
        assert not view.ok and view.reason
        assert view.capabilities.file_transcription is False
        with pytest.raises(SpeechError) as caught:
            await view.provider.transcribe(AsrRequest("fixture-asr", AUDIO), call=CALL)
        assert caught.value.kind == SpeechErrorKind.not_configured


def test_resolve_asr_reads_no_secret_and_reports_bound_target() -> None:
    resolution = resolve_asr(_config())
    assert resolution.usable
    assert resolution.provider_id == "recognition"
    assert resolution.model_id == "bound-model"


def test_settings_rejects_invalid_asr_url_but_allows_default_endpoint() -> None:
    for kind in ("siliconflow_asr", "dashscope_asr"):
        invalid = _provider_shape_issues("asr", {"kind": kind, "base_url": "file:///local", "has_secret": True})
        assert any(issue.field.endswith("base_url") for issue in invalid)
        assert _provider_shape_issues("asr", {"kind": kind, "has_secret": True}) == []
