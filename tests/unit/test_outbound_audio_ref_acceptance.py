"""Acceptance tests for the internal audio reference and OneBot wire boundary.

The tests use an authorized local-file export and an in-memory OneBot HTTP fake.
They do not contact a real OneBot server or validate production SILK quality.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from pydantic import ValidationError

from ling.capabilities.delivery import DeliveryOutcome
from ling.capabilities.voice_reply import VoiceReplyService
from ling.contracts.v1 import (
    AttachmentKind,
    OperationState,
    OutboundAttachment,
)
from ling.domains.media.exports import ExportTarget, ExportTransport
from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage

ACTOR_ID = "actor-audio-acceptance"
WINDOW_ID = "onebot-voice:private:100200300"
ASSET_ID = "asset-audio-acceptance"
CANONICAL_TEXT = "canonical text must remain private"


def _silk_bytes(size_bytes: int = 64) -> bytes:
    assert size_bytes >= 10
    return b"\x02#!SILK_V3" + b"x" * (size_bytes - 10)


class _OneBotCapture:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 123}},
        )


class _AuthorizedMedia:
    def __init__(self, path: str, *, size_bytes: int = 64) -> None:
        self.path = path
        self.size_bytes = size_bytes
        self.view_requests: list[tuple[str, str]] = []
        self.export_requests: list[tuple[str, str, str, str]] = []

    async def view_for_window(
        self,
        asset_id: str,
        *,
        requester_actor: str,
        requester_window: str,
    ) -> SimpleNamespace:
        self.view_requests.append((requester_actor, requester_window))
        assert asset_id == ASSET_ID
        assert requester_actor == ACTOR_ID
        assert requester_window == WINDOW_ID
        return SimpleNamespace(
            asset_id=ASSET_ID,
            digest="sha256:audio-acceptance",
            mime_type="audio/silk",
            format="silk",
            size_bytes=self.size_bytes,
            duration_ms=1000,
            sample_rate=24000,
        )

    async def export_for_window(
        self,
        asset_ref: str,
        *,
        transport: ExportTransport,
        requester_actor: str,
        requester_window: str,
    ) -> ExportTarget:
        self.export_requests.append((asset_ref, transport.value, requester_actor, requester_window))
        assert asset_ref == ASSET_ID
        assert requester_actor == ACTOR_ID
        assert requester_window == WINDOW_ID
        return ExportTarget(
            transport=transport,
            url=None,
            path=self.path if transport is ExportTransport.local_path else None,
            data_uri=None,
            size_bytes=self.size_bytes,
            mime_type="audio/silk",
        )


class _Session:
    async def scalar(self, _statement: object) -> None:
        return None

    async def flush(self) -> None:
        return None


def _connector(capture: _OneBotCapture, **config_overrides: int):
    config = OneBotConfig(
        api_base="http://onebot.test:5700",
        access_token="fixture-token",
        self_id="10001",
        instance_id="audio-ref-acceptance",
        **config_overrides,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(capture.handle))
    return OneBotV11Connector(config, http=client), client


def _voice_message() -> SimpleNamespace:
    return SimpleNamespace(
        id="message-audio-acceptance",
        turn_id="turn-audio-acceptance",
        window_id=WINDOW_ID,
        audio_asset_id=ASSET_ID,
        output_modality="voice",
        speech_state="ready",
        speech_delivery_state=None,
        text=CANONICAL_TEXT,
    )


def _voice_service(media: _AuthorizedMedia) -> VoiceReplyService:
    service = object.__new__(VoiceReplyService)
    service.session = _Session()
    service.settings = SimpleNamespace(actor_id=ACTOR_ID)
    service.media = media
    return service


async def _send_through_connector(
    self,
    *,
    connector: OneBotV11Connector,
    message: OutboundMessage,
    **_kwargs: object,
) -> DeliveryOutcome:
    assert message.text == ""
    assert len(message.attachments) == 1
    assert message.attachments[0].kind is AttachmentKind.audio
    assert Path(message.attachments[0].ref).is_absolute()
    assert not message.attachments[0].ref.startswith("base64://")
    # The real DeliveryService assigns its operation id before invoking a connector.
    outbound = replace(message, operation_id="op_audio_ref_acceptance")
    receipt = await connector.send(outbound)
    return DeliveryOutcome(
        operation_id=receipt.operation_id,
        state=OperationState.succeeded,
        receipt=receipt,
        detail=receipt.detail,
    )


@pytest.mark.asyncio
async def test_voice_reply_exports_local_file_and_sends_one_base64_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_bytes = _silk_bytes(2048)
    audio_path = tmp_path / "reply.silk"
    audio_path.write_bytes(audio_bytes)
    media = _AuthorizedMedia(str(audio_path))
    message = _voice_message()
    capture = _OneBotCapture()
    connector, client = _connector(capture)
    monkeypatch.delenv("LING_SPEECH_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "ling.capabilities.delivery.DeliveryService.deliver",
        _send_through_connector,
    )

    try:
        delivered = await _voice_service(media).deliver(
            message,
            connector=connector,
            window_ref=WINDOW_ID,
            subject_id=None,
            is_group=False,
        )
    finally:
        await client.aclose()

    assert delivered is True
    assert message.speech_delivery_state == "delivered"
    assert media.view_requests == [(ACTOR_ID, WINDOW_ID)]
    assert media.export_requests == [
        (ASSET_ID, ExportTransport.local_path.value, ACTOR_ID, WINDOW_ID)
    ]
    assert len(str(audio_path)) <= 2048
    assert len(capture.requests) == 1
    segments = capture.requests[0]["message"]
    assert len(segments) == 1
    assert segments[0]["type"] == "record"
    file_ref = segments[0]["data"]["file"]
    assert file_ref.startswith("base64://")
    assert len(file_ref) > 2048
    assert base64.b64decode(file_ref.removeprefix("base64://")) == audio_bytes
    assert CANONICAL_TEXT not in json.dumps(capture.requests[0])


@pytest.mark.asyncio
async def test_local_path_over_reference_limit_is_rejected_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_path = "/" + "p" * 2048
    media = _AuthorizedMedia(long_path)
    message = _voice_message()
    capture = _OneBotCapture()
    connector, client = _connector(capture)
    monkeypatch.delenv("LING_SPEECH_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "ling.capabilities.delivery.DeliveryService.deliver",
        _send_through_connector,
    )

    try:
        with pytest.raises(ValidationError):
            await _voice_service(media).deliver(
                message,
                connector=connector,
                window_ref=WINDOW_ID,
                subject_id=None,
                is_group=False,
            )
    finally:
        await client.aclose()

    assert media.export_requests == [
        (ASSET_ID, ExportTransport.local_path.value, ACTOR_ID, WINDOW_ID)
    ]
    assert capture.requests == []


def test_long_ordinary_audio_url_is_rejected() -> None:
    long_url = "https://media.test/" + "a" * 2050

    with pytest.raises(ValidationError):
        OutboundAttachment(kind=AttachmentKind.audio, ref=long_url)


@pytest.mark.asyncio
async def test_non_audio_base64_reference_does_not_become_record() -> None:
    capture = _OneBotCapture()
    connector, client = _connector(capture)
    attachment = OutboundAttachment(
        kind=AttachmentKind.image,
        ref="base64://c2hvcnQtZml4dHVyZQ==",
    )
    message = OutboundMessage(
        operation_id="op_audio_ref_acceptance",
        window_ref=WINDOW_ID,
        text="",
        attachments=[attachment],
    )

    try:
        await connector.send(message)
    finally:
        await client.aclose()

    assert len(capture.requests) == 1
    segments = capture.requests[0]["message"]
    assert all(segment["type"] != "record" for segment in segments)


@pytest.mark.parametrize(
    ("base64_limit", "voice_limit"),
    [(32, 4096), (4096, 32)],
    ids=["inline-base64-limit", "audio-byte-limit"],
)
@pytest.mark.asyncio
async def test_configured_audio_size_limits_reject_before_platform_send(
    tmp_path: Path,
    base64_limit: int,
    voice_limit: int,
) -> None:
    audio_path = tmp_path / "large-reply.silk"
    audio_path.write_bytes(_silk_bytes(128))
    capture = _OneBotCapture()
    connector, client = _connector(
        capture,
        voice_base64_max_bytes=base64_limit,
        voice_max_bytes=voice_limit,
    )
    message = OutboundMessage(
        operation_id="op_audio_ref_acceptance",
        window_ref=WINDOW_ID,
        text="",
        attachments=[
            OutboundAttachment(
                kind=AttachmentKind.audio,
                ref=str(audio_path),
                mime_type="audio/silk",
            )
        ],
    )

    try:
        with pytest.raises(ChannelError) as error:
            await connector.send(message)
    finally:
        await client.aclose()

    assert error.value.kind is ChannelErrorKind.rejected
    assert capture.requests == []
