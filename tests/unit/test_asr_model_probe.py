from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ling.api.routes import settings as settings_routes
from ling.ports.model import ProviderKind
from ling.ports.speech import AsrResult, SpeechUsage


@pytest.mark.asyncio
async def test_asr_catalog_probe_uses_compatible_models_endpoint_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fixture-secret-never-return-this"
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "fun-asr"}]})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        settings_routes.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    async def read_secret(provider_id: str) -> str:
        assert provider_id == "dash"
        return secret

    provider = SimpleNamespace(
        provider_id="dash",
        kind=ProviderKind.dashscope_asr.value,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    result = await settings_routes._probe_asr_catalog(
        provider, SimpleNamespace(read_secret=read_secret)
    )

    assert result.ok is True
    assert result.models == ["fun-asr"]
    assert len(seen) == 1
    assert str(seen[0].url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {secret}"
    assert secret not in result.detail


@pytest.mark.asyncio
async def test_realtime_asr_model_is_reported_as_unsupported_for_file_probe() -> None:
    async def read_secret(_provider_id: str) -> str:
        raise AssertionError("realtime models do not use the file ASR adapter")

    provider = SimpleNamespace(provider_id="dash", kind=ProviderKind.dashscope_asr.value)
    result = await settings_routes._probe_asr_model(
        provider,
        "qwen-audio-3.0-realtime-flash",
        SimpleNamespace(read_secret=read_secret),
    )

    assert result.ok is False
    assert "实时语音模型" in result.detail
    assert "文件转写" in result.detail


@pytest.mark.asyncio
async def test_file_asr_probe_sends_the_requested_model_and_synthetic_wav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, bytes]] = []

    class Adapter:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["api_key"] == "fixture-secret"
            assert kwargs["base_url"] == "https://dashscope.aliyuncs.com/api/v1"

        async def transcribe(self, request, *, call) -> AsrResult:
            seen.append((request.model_id, request.audio.content))
            return AsrResult(
                text="probe",
                language=None,
                confidence=None,
                provider_request_id="request-fixture",
                usage=SpeechUsage(),
            )

    monkeypatch.setattr("ling.models.dashscope_asr.DashScopeAsr", Adapter)

    async def read_secret(_provider_id: str) -> str:
        return "fixture-secret"

    provider = SimpleNamespace(
        provider_id="dash",
        kind=ProviderKind.dashscope_asr.value,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout_seconds=10,
    )
    result = await settings_routes._probe_asr_model(
        provider, "fun-asr", SimpleNamespace(read_secret=read_secret)
    )

    assert result.ok is True
    assert "完成识别请求" in result.detail
    assert seen[0][0] == "fun-asr"
    assert seen[0][1].startswith(b"RIFF")
    assert len(seen[0][1]) > 44
