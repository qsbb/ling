"""REQ-09 acceptance through lifespan, management HTTP and protocol HTTP.

The oracle predates implementation and lives in the local acceptance criteria.
Only socket startup and external HTTP transport are simulated. Configuration
activation, runtime selection, signatures, deduplication, turns and delivery use
production entry points. Real platform connectivity requires separate evidence.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import struct
import time
import uuid
import wave
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from connectors.onebot import connector as onebot_module
from connectors.onebot.ws_client import OneBotWsClient
from connectors.onebot.ws_server import OneBotWsServer
from connectors.qqbot import connector as qqbot_module
from connectors.qqbot.connector import ed25519_seed
from connectors.qqbot.ws_gateway import QQBotGateway
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import null, select, text

from ling.app import create_app
from ling.capabilities.delivery import DeliveryService
from ling.domains.identity.service import IdentityService
from ling.domains.speech import asr_runtime
from ling.infra.asr_jobs import AsrJob
from ling.infra.db import init_engine, session_scope
from ling.infra.models import (
    ConnectorConfigHead,
    ConnectorConfigRevision,
    ConnectorOverrideValue,
    Message,
    Operation,
    Turn,
    Window,
)
from ling.models import openai_compatible
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.channel import OutboundMessage
from ling.ports.speech import AsrResult, SpeechUsage
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

BASE = "/v1/settings/connectors"
ORIGIN = {"Origin": "http://testserver"}
SELF_ID = "100200300"
USER_ID = "100200301"
OPENID = "acceptance-openid"


def _wave_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(
            b"".join(struct.pack("<h", round(500 * math.sin(index / 9))) for index in range(1600))
        )
    return buffer.getvalue()


class ProtocolWire:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.lifecycle: list[tuple[str, object]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append(
            {
                "host": request.url.host,
                "path": request.url.path,
                "authorization": request.headers.get("authorization"),
                "body": body,
            }
        )
        if request.url.path.endswith(".wav"):
            return httpx.Response(200, content=_wave_bytes(), headers={"content-type": "audio/wav"})
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(
                200, json={"access_token": f"token-{request.url.host}", "expires_in": 7200}
            )
        if ".onebot." in request.url.host:
            data = {"user_id": SELF_ID, "message_id": 800000 + len(self.calls)}
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})
        return httpx.Response(200, json={"id": f"platform-{len(self.calls)}"})

    def stopped(self, component: object) -> int:
        return sum(action == "stop" and obj is component for action, obj in self.lifecycle)

    def sends(self) -> list[dict[str, Any]]:
        return [
            call
            for call in self.calls
            if call["path"] == "/send_private_msg" or call["path"].startswith("/v2/users/")
        ]


@pytest.fixture(autouse=True)
def protocol_wire(monkeypatch: pytest.MonkeyPatch) -> ProtocolWire:
    wire = ProtocolWire()
    # onebot_config reads env independently from Settings. Pin every credential
    # used by the default instance before the real web_env lifespan starts.
    for name, value in {
        "LING_ONEBOT_API_BASE": "https://default.onebot.example.test",
        "LING_ONEBOT_ACCESS_TOKEN": "fake-default-access",
        "LING_ONEBOT_SECRET": "fake-default-signature",
        "LING_ONEBOT_SELF_ID": SELF_ID,
        "LING_ONEBOT_INSTANCE_ID": "legacy-onebot",
        "LING_ONEBOT_WS_HOST": "127.0.0.1",
        "LING_ONEBOT_WS_PORT": "",
        "LING_ONEBOT_WS_URL": "",
        "LING_ONEBOT_WS_TOKEN": "",
        "LING_ONEBOT_SUPPORTS_GROUP": "1",
        "LING_ONEBOT_PROACTIVE": "0",
        "LING_ONEBOT_MEDIA_HOSTS": "audio.example.test",
    }.items():
        monkeypatch.setenv(name, value)
    client_factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(wire.handle))
    facade = SimpleNamespace(**(vars(httpx) | {"AsyncClient": client_factory}))
    monkeypatch.setattr(onebot_module, "httpx", facade)
    monkeypatch.setattr(qqbot_module, "httpx", facade)

    def start(component: object) -> None:
        wire.lifecycle.append(("start", component))
        if isinstance(component, OneBotWsClient):
            endpoint = component.url
        else:
            endpoint = component._connector.config.base_url
        if "reject-start" in endpoint:
            raise RuntimeError("controlled startup failure")
        component.connected = True

    async def stop(component: object) -> None:
        wire.lifecycle.append(("stop", component))
        component.connected = False

    async def start_server(component: object) -> None:
        wire.lifecycle.append(("start", component))
        assert component.token, "test listener must have a configured token"

    async def idle_asr(app: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(OneBotWsClient, "start", start)
    monkeypatch.setattr(OneBotWsClient, "stop", stop)
    monkeypatch.setattr(OneBotWsServer, "start", start_server)
    monkeypatch.setattr(OneBotWsServer, "stop", stop)
    monkeypatch.setattr(QQBotGateway, "start", start)
    monkeypatch.setattr(QQBotGateway, "stop", stop)
    monkeypatch.setattr(asr_runtime, "worker_loop", idle_asr)
    return wire


def _values(scope: str, instance_id: str) -> dict[str, Any]:
    if scope == "onebot":
        return {
            "LING_ONEBOT_API_BASE": f"https://{instance_id}.onebot.example.test",
            "LING_ONEBOT_ACCESS_TOKEN": f"fake-access-{instance_id}",
            "LING_ONEBOT_SECRET": f"fake-signature-{instance_id}",
            "LING_ONEBOT_SELF_ID": SELF_ID,
            "LING_ONEBOT_WS_URL": f"ws://{instance_id}.onebot.example.test",
            "LING_ONEBOT_WS_TOKEN": f"fake-ws-{instance_id}",
            "LING_ONEBOT_MEDIA_HOSTS": ["audio.example.test"],
            "LING_ONEBOT_SUPPORTS_GROUP": instance_id == "bravo",
            "LING_ONEBOT_PROACTIVE": True,
        }
    return {
        "LING_QQBOT_ENABLED": True,
        "LING_QQBOT_APP_ID": f"fake-app-{instance_id}",
        "LING_QQBOT_CLIENT_SECRET": f"fake-client-{instance_id}",
        "LING_QQBOT_BOT_SECRET": f"fake-signature-{instance_id}",
        "LING_QQBOT_API_BASE": f"https://{instance_id}.qqbot.example.test",
        "LING_QQBOT_TOKEN_URL": f"https://{instance_id}.qqbot.example.test/app/getAppAccessToken",
        "LING_QQBOT_SUPPORTS_GROUP": instance_id == "bravo",
        "LING_QQBOT_PROACTIVE": True,
    }


async def _create(env: TestEnv, scope: str, instance_id: str) -> None:
    response = await env.client.post(
        f"{BASE}/{scope}/instances",
        headers=ORIGIN,
        json={
            "instance_id": instance_id,
            "display_name": f"验收-{scope}-{instance_id}",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["item"]["instance_id"] == instance_id


async def _save(
    env: TestEnv,
    scope: str,
    instance_id: str,
    values: dict[str, Any],
    *,
    generation: int | None = None,
) -> httpx.Response:
    headers = dict(ORIGIN)
    if generation is not None:
        headers["If-Match"] = str(generation)
    return await env.client.put(
        f"{BASE}/{scope}",
        params={"instance_id": instance_id},
        headers=headers,
        json={"set": values},
    )


async def _pair(env: TestEnv, scope: str) -> Any:
    for instance_id in ("alpha", "bravo"):
        await _create(env, scope, instance_id)
        response = await _save(env, scope, instance_id, _values(scope, instance_id))
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "active", response.text
        assert response.json()["generation"] == 1, response.text
    return env.app.state.connector_runtime


def _transport(runtime: Any, scope: str, instance_id: str) -> object:
    name = "onebot_ws" if scope == "onebot" else "qqbot_gateway"
    return runtime.components(scope, instance_id)[name]


async def _view(env: TestEnv, scope: str, instance_id: str) -> dict[str, Any]:
    response = await env.client.get(BASE, params={"instance_id": instance_id})
    assert response.status_code == 200, response.text
    assert response.json()["instances"][scope] == instance_id
    return next(item for item in response.json()["connectors"] if item["id"] == scope)


async def _health(env: TestEnv, scope: str, instance_id: str) -> dict[str, Any]:
    response = await env.client.get(f"/v1/{scope}/instances/{instance_id}/health")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True, response.text
    assert response.json()["instance"] == f"{scope}-{instance_id}"
    return response.json()


class ChatWire:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        event = {"choices": [{"delta": {"content": "收到这条消息。"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    def inputs(self) -> list[str]:
        return [
            str(message.get("content", ""))
            for request in self.requests
            for message in request["messages"]
            if message["role"] == "user"
        ]


async def _chat(env: TestEnv, monkeypatch: pytest.MonkeyPatch, *, asr: bool = False) -> ChatWire:
    providers = [
        {
            "provider_id": "accept-chat",
            "kind": "openai_compatible",
            "enabled": True,
            "base_url": "https://chat.example.test/v1",
            "models": [{"model_id": "chat"}],
        }
    ]
    profiles = [{"profile": "dialogue.fast", "provider_id": "accept-chat", "model_id": "chat"}]
    if asr:
        providers.append(
            {
                "provider_id": "accept-asr",
                "kind": "siliconflow_asr",
                "enabled": True,
                "base_url": "https://asr.example.test/v1",
                "models": [{"model_id": "recognizer"}],
            }
        )
        profiles.append(
            {"profile": "speech.asr", "provider_id": "accept-asr", "model_id": "recognizer"}
        )
    response = await env.client.put(
        "/v1/settings/draft",
        json={
            "providers": providers,
            "profiles": profiles,
            "dialogue": {"private_may_stay_silent": False},
        },
    )
    assert response.status_code == 200, response.text
    publish = await env.client.post(
        "/v1/settings/publish", json={"revision": response.json()["revision"]}
    )
    assert publish.status_code == 200, publish.text
    wire = ChatWire()
    factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(wire.handle))
    monkeypatch.setattr(
        openai_compatible, "httpx", SimpleNamespace(**(vars(httpx) | {"AsyncClient": factory}))
    )
    env.app.state.router = ModelRouter(ProviderRegistry(lambda _: "fake-chat-key"))
    return wire


async def _bind(env: TestEnv, scope: str) -> None:
    async with session_scope() as session:
        for instance_id in ("alpha", "bravo"):
            await IdentityService(session).bind_account(
                subject_id=env.subject_id,
                connector_instance=f"{scope}-{instance_id}",
                external_account_id=USER_ID if scope == "onebot" else OPENID,
                proof="proven",
            )


def _event(
    scope: str, text: str, *, audio_ref: str | None = None, message_id: int = 445566
) -> bytes:
    if scope == "onebot":
        message: str | list[dict[str, Any]] = text
        if audio_ref:
            message = [{"type": "record", "data": {"file": audio_ref, "url": audio_ref}}]
        payload = {
            "time": int(time.time()),
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": message_id,
            "user_id": int(USER_ID),
            "message": message,
            "raw_message": text,
            "sender": {"nickname": "验收用户"},
        }
    else:
        payload = {
            "id": f"event-{message_id}",
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": f"message-{message_id}",
                "content": text,
                "author": {"user_openid": OPENID},
                "timestamp": datetime.now(UTC).isoformat(),
                "attachments": (
                    [{"url": audio_ref, "content_type": "audio/wav", "filename": "voice.wav"}]
                    if audio_ref
                    else []
                ),
            },
        }
    return json.dumps(payload, ensure_ascii=False).encode()


def _headers(scope: str, instance_id: str, body: bytes) -> dict[str, str]:
    secret = f"fake-signature-{instance_id}"
    if scope == "onebot":
        return {
            "Content-Type": "application/json",
            "X-Self-ID": SELF_ID,
            "X-Signature": "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest(),
        }
    stamp = str(int(time.time()))
    private = Ed25519PrivateKey.from_private_bytes(ed25519_seed(secret))
    return {
        "Content-Type": "application/json",
        "X-Signature-Timestamp": stamp,
        "X-Signature-Ed25519": private.sign(stamp.encode() + body).hex(),
    }


async def _post(env: TestEnv, scope: str, instance_id: str, body: bytes) -> httpx.Response:
    return await env.client.post(
        f"/v1/{scope}/instances/{instance_id}/events",
        content=body,
        headers=_headers(scope, instance_id, body),
    )


async def _window_for_turn(turn_id: str) -> str:
    async with session_scope() as session:
        turn = await session.get(Turn, turn_id)
        assert turn is not None, turn_id
        return turn.window_id


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_two_instances_activate_and_health_uses_each_credential(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
) -> None:
    env = logged_in
    default = getattr(env.app.state, f"{scope}_connector", None)
    runtime = await _pair(env, scope)
    alpha = runtime.connector(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    assert alpha is not None and bravo is not None and alpha is not bravo
    assert getattr(env.app.state, f"{scope}_connector", None) is default
    assert alpha.instance_id == f"{scope}-alpha"
    assert bravo.instance_id == f"{scope}-bravo"
    assert runtime.for_namespace(alpha.instance_id, scope) is alpha
    assert runtime.for_namespace(bravo.instance_id, scope) is bravo
    assert alpha.capabilities().supports_group is False
    assert bravo.capabilities().supports_group is True
    for instance_id in ("alpha", "bravo"):
        view = await _view(env, scope, instance_id)
        assert view["generation"] == 1
        fields = {item["key"]: item for item in view["fields"]}
        assert (
            fields[f"LING_{scope.upper()}_API_BASE"]["value"]
            == f"https://{instance_id}.{scope}.example.test"
        )
        await _health(env, scope, instance_id)
    if scope == "onebot":
        calls = [call for call in protocol_wire.calls if call["path"] == "/get_login_info"]
        assert [(call["host"], call["authorization"]) for call in calls] == [
            ("alpha.onebot.example.test", "Bearer fake-access-alpha"),
            ("bravo.onebot.example.test", "Bearer fake-access-bravo"),
        ]
    else:
        calls = [
            call for call in protocol_wire.calls if call["path"].endswith("/app/getAppAccessToken")
        ]
        assert [
            (call["host"], call["body"]["appId"], call["body"]["clientSecret"]) for call in calls
        ] == [
            ("alpha.qqbot.example.test", "fake-app-alpha", "fake-client-alpha"),
            ("bravo.qqbot.example.test", "fake-app-bravo", "fake-client-bravo"),
        ]


async def test_new_onebot_namespace_does_not_inherit_default_credentials(
    logged_in: TestEnv,
) -> None:
    env = logged_in
    legacy = env.app.state.onebot_connector
    assert legacy.instance_id == "legacy-onebot"
    assert legacy._config.access_token == "fake-default-access"
    await _create(env, "onebot", "blank")
    response = await _save(
        env, "onebot", "blank", {"LING_ONEBOT_API_BASE": "https://blank.onebot.example.test"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "active", response.text
    connector = env.app.state.connector_runtime.connector("onebot", "blank")
    assert connector.instance_id == "onebot-blank"
    assert not connector._config.access_token
    assert not connector._config.secret
    assert not connector._config.self_id
    assert env.app.state.onebot_connector is legacy
    response = await env.client.get("/v1/onebot/health")
    assert response.status_code == 200, response.text
    assert response.json()["instance"] == "legacy-onebot"


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_hot_update_and_generation_compare_only_touch_target(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    before = {key: runtime.connector(scope, key) for key in ("alpha", "bravo")}
    transports = {key: _transport(runtime, scope, key) for key in ("alpha", "bravo")}
    bravo_values = runtime.snapshot(scope, "bravo")
    response = await _save(
        env,
        scope,
        "alpha",
        {
            f"LING_{scope.upper()}_API_BASE": f"https://updated.{scope}.example.test",
        },
        generation=1,
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "active", response.text
    assert response.json()["generation"] == 2
    assert runtime.connector(scope, "alpha") is not before["alpha"]
    assert runtime.connector(scope, "bravo") is before["bravo"]
    assert protocol_wire.stopped(transports["alpha"]) == 1
    assert protocol_wire.stopped(transports["bravo"]) == 0
    assert runtime.snapshot(scope, "bravo") == bravo_values
    assert (await _view(env, scope, "bravo"))["generation"] == 1
    stale = await _save(
        env, scope, "alpha", {f"LING_{scope.upper()}_SUPPORTS_GROUP": True}, generation=1
    )
    assert stale.status_code == 409, stale.text
    bravo = await _save(
        env, scope, "bravo", {f"LING_{scope.upper()}_SUPPORTS_GROUP": False}, generation=1
    )
    assert bravo.status_code == 202, bravo.text
    assert bravo.json()["state"] == "active", bravo.text
    assert bravo.json()["generation"] == 2
    async with session_scope() as session:
        heads = list(
            (
                await session.scalars(
                    select(ConnectorConfigHead).where(
                        ConnectorConfigHead.scope == scope,
                        ConnectorConfigHead.instance_id.in_(["alpha", "bravo"]),
                    )
                )
            ).all()
        )
    assert {row.instance_id: row.generation for row in heads} == {"alpha": 2, "bravo": 2}


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_failed_activation_restores_only_target(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    bravo = runtime.connector(scope, "bravo")
    bravo_transport = _transport(runtime, scope, "bravo")
    bravo_values = runtime.snapshot(scope, "bravo")
    alpha_values = runtime.snapshot(scope, "alpha")
    key = "LING_ONEBOT_WS_URL" if scope == "onebot" else "LING_QQBOT_API_BASE"
    value = (
        "ws://reject-start.example.test"
        if scope == "onebot"
        else "https://reject-start.qqbot.example.test"
    )
    response = await _save(env, scope, "alpha", {key: value}, generation=1)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "rolled_back", response.text
    assert runtime.snapshot(scope, "alpha") == alpha_values
    assert runtime.snapshot(scope, "bravo") == bravo_values
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(bravo_transport) == 0
    assert (await _view(env, scope, "bravo"))["generation"] == 1
    await _health(env, scope, "alpha")
    await _health(env, scope, "bravo")


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_same_platform_id_isolated_and_replies_use_original_instance(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    env = logged_in
    await _pair(env, scope)
    wire = await _chat(env, monkeypatch)
    await _bind(env, scope)
    phrases = {key: f"{key}独立短语-{uuid.uuid4().hex}" for key in ("alpha", "bravo")}
    alpha_body = _event(scope, phrases["alpha"])
    denied = await env.client.post(
        f"/v1/{scope}/instances/alpha/events",
        content=alpha_body,
        headers=_headers(scope, "bravo", alpha_body),
    )
    assert denied.status_code == 403, denied.text
    assert wire.requests == []
    assert protocol_wire.sends() == []
    windows: dict[str, str] = {}
    for instance_id in ("alpha", "bravo"):
        response = await _post(env, scope, instance_id, _event(scope, phrases[instance_id]))
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True, response.text
        assert response.json()["delivered"] is True, response.text
        windows[instance_id] = await _window_for_turn(response.json()["turn_id"])
    assert windows["alpha"] != windows["bravo"]
    before = len(protocol_wire.sends())
    duplicate = await _post(env, scope, "alpha", alpha_body)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["duplicate"] is True, duplicate.text
    assert len(protocol_wire.sends()) == before
    async with session_scope() as session:
        for instance_id, window_id in windows.items():
            window = await session.get(Window, window_id)
            rows = list(
                (
                    await session.scalars(
                        select(Message).where(
                            Message.window_id == window_id,
                            Message.role == "user",
                        )
                    )
                ).all()
            )
            assert window.title.startswith(f"{scope}-{instance_id}:")
            assert [row.text for row in rows] == [phrases[instance_id]]
    assert any(phrases["alpha"] in text for text in wire.inputs()), wire.requests
    assert any(phrases["bravo"] in text for text in wire.inputs()), wire.requests
    sends = protocol_wire.sends()
    assert {call["host"] for call in sends} == {
        f"alpha.{scope}.example.test",
        f"bravo.{scope}.example.test",
    }
    for call in sends:
        instance_id = call["host"].split(".", 1)[0]
        if scope == "onebot":
            assert call["authorization"] == f"Bearer fake-access-{instance_id}"
            assert str(call["body"]["user_id"]) == USER_ID
        else:
            assert call["authorization"] == f"QQBot token-{instance_id}.qqbot.example.test"
            assert call["path"] == f"/v2/users/{OPENID}/messages"
            assert call["body"]["msg_id"] == "message-445566"

    if scope == "onebot":
        # Recall normal replies through the owner HTTP entry, including the
        # stored delivery receipt. A last-singleton lookup sends these elsewhere.
        for instance_id, window_id in windows.items():
            async with session_scope() as session:
                message = await session.scalar(
                    select(Message)
                    .where(
                        Message.window_id == window_id,
                        Message.role == "actor",
                    )
                    .order_by(Message.sequence.desc())
                )
                message_id = message.id
            response = await env.client.post(
                f"/v1/sessions/{window_id}/messages/{message_id}/recall", json={}
            )
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "recalled", response.text
            recall = [call for call in protocol_wire.calls if call["path"] == "/delete_msg"][-1]
            assert recall["host"] == f"{instance_id}.onebot.example.test"
            assert recall["authorization"] == f"Bearer fake-access-{instance_id}"


@pytest.mark.parametrize("field", ["LING_ONEBOT_WS_PORT", "LING_ONEBOT_INSTANCE_ID"])
async def test_conflicting_target_is_rejected_before_either_transport_stops(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    field: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, "onebot")
    for instance_id, port in (("alpha", "27181"), ("bravo", "27182")):
        response = await _save(env, "onebot", instance_id, {"LING_ONEBOT_WS_PORT": port})
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "active", response.text
    before = {key: runtime.components("onebot", key) for key in ("alpha", "bravo")}
    lifecycle = list(protocol_wire.lifecycle)
    conflict = "27181" if field == "LING_ONEBOT_WS_PORT" else "onebot-alpha"
    response = await _save(env, "onebot", "bravo", {field: conflict})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["field"] == field, response.text
    assert protocol_wire.lifecycle == lifecycle
    for instance_id in ("alpha", "bravo"):
        after = runtime.components("onebot", instance_id)
        assert after.keys() == before[instance_id].keys()
        assert all(after[key] is obj for key, obj in before[instance_id].items())
        assert (await _view(env, "onebot", instance_id))["generation"] == 2
        await _health(env, "onebot", instance_id)


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_disable_enable_and_delete_preserve_other_instance(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    alpha = runtime.connector(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    alpha_transport = _transport(runtime, scope, "alpha")
    bravo_transport = _transport(runtime, scope, "bravo")
    target = f"{BASE}/{scope}/instances/alpha"
    disabled = await env.client.put(f"{target}/enabled", headers=ORIGIN, json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["item"]["enabled"] is False
    assert runtime.connector(scope, "alpha") is None
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(alpha_transport) == 1
    assert protocol_wire.stopped(bravo_transport) == 0
    unavailable = await env.client.get(f"/v1/{scope}/instances/alpha/health")
    assert unavailable.status_code == 404, unavailable.text
    unavailable = await _post(env, scope, "alpha", _event(scope, "停用之后"))
    assert unavailable.status_code == 404, unavailable.text
    from ling.kernel.inbound_intake import process_inbound_payload

    stale = await process_inbound_payload(env.app, alpha, json.loads(_event(scope, "旧连接迟到")))
    assert stale["accepted"] is False, stale
    assert stale["reason"] == "instance_unavailable", stale
    await _health(env, scope, "bravo")
    enabled = await env.client.put(f"{target}/enabled", headers=ORIGIN, json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert runtime.connector(scope, "alpha") is not None
    assert runtime.connector(scope, "alpha") is not alpha
    await _health(env, scope, "alpha")
    deleted = await env.client.delete(
        target, headers={**ORIGIN, "Content-Type": "application/json"}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["item"]["deleted_at"] is not None
    assert runtime.connector(scope, "alpha") is None
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(bravo_transport) == 0
    unavailable = await env.client.get(f"/v1/{scope}/instances/alpha/health")
    assert unavailable.status_code == 404, unavailable.text
    listing = await env.client.get(f"{BASE}/{scope}/instances")
    assert listing.status_code == 200, listing.text
    assert "alpha" not in {item["instance_id"] for item in listing.json()["items"]}
    assert "bravo" in {item["instance_id"] for item in listing.json()["items"]}


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_asr_download_and_completion_use_audio_origin_instance(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    chat = await _chat(env, monkeypatch, asr=True)
    await _bind(env, scope)
    granted = await env.client.put("/v1/consents", json={"scope": "media.inbound", "granted": True})
    assert granted.status_code == 200, granted.text
    downloads: list[str] = []
    phrases = {key: f"{key}语音短语-{uuid.uuid4().hex}" for key in ("alpha", "bravo")}
    recognition_calls: list[bytes] = []

    class Recognizer:
        async def transcribe(self, request, *, call) -> AsrResult:
            recognition_calls.append(request.audio.content)
            phrase = phrases[downloads[-1].removeprefix(f"{scope}-")]
            return AsrResult(phrase, "zh", None, None, SpeechUsage())

    async def build(config, *, secret_reader):
        return SimpleNamespace(
            ok=True,
            reason="",
            provider=Recognizer(),
            provider_id="accept-asr",
            model_id="recognizer",
        )

    connector_type = (
        onebot_module.OneBotV11Connector if scope == "onebot" else qqbot_module.QQBotConnector
    )
    original_fetch = connector_type.fetch_media

    async def fetch(connector, ref):
        downloads.append(connector.instance_id)
        return await original_fetch(connector, ref)

    monkeypatch.setattr("ling.domains.speech.asr_providers.build_asr_provider", build)
    monkeypatch.setattr(connector_type, "fetch_media", fetch)
    window_ids: dict[str, str] = {}
    for instance_id in ("alpha", "bravo"):
        response = await _post(
            env,
            scope,
            instance_id,
            _event(
                scope,
                "",
                audio_ref=f"https://audio.example.test/{instance_id}.wav",
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True, response.text
    async with session_scope() as session:
        jobs = list((await session.scalars(select(AsrJob).order_by(AsrJob.created_at))).all())
    assert len(jobs) == 2
    for job in jobs:
        namespace = job.payload["envelope"]["connector_instance"]
        assert runtime.for_namespace(namespace, scope) is not None
        instance_id = namespace.removeprefix(f"{scope}-")
        assert instance_id in {"alpha", "bravo"}
        window_ids[instance_id] = job.window_id
        assert await asr_runtime.process_job(env.app, job.id)
        async with session_scope() as session:
            completed = await session.get(AsrJob, job.id)
            assert completed.state == "succeeded", completed.error_message
            assert completed.submission == "committed"
    assert window_ids["alpha"] != window_ids["bravo"]
    assert downloads == [f"{scope}-alpha", f"{scope}-bravo"]
    assert len(recognition_calls) == 2
    with wave.open(io.BytesIO(_wave_bytes()), "rb") as original:
        expected_pcm = original.readframes(original.getnframes())
    for content in recognition_calls:
        with wave.open(io.BytesIO(content), "rb") as normalized:
            assert normalized.getnchannels() == 1
            assert normalized.getframerate() == 16000
            assert normalized.getsampwidth() == 2
            assert normalized.readframes(normalized.getnframes()) == expected_pcm
    sends = protocol_wire.sends()
    assert {call["host"] for call in sends} == {
        f"alpha.{scope}.example.test",
        f"bravo.{scope}.example.test",
    }
    for call in sends:
        instance_id = call["host"].split(".", 1)[0]
        if scope == "onebot":
            assert call["authorization"] == f"Bearer fake-access-{instance_id}"
            assert str(call["body"]["user_id"]) == USER_ID
        else:
            assert call["authorization"] == f"QQBot token-{instance_id}.qqbot.example.test"
            assert call["path"] == f"/v2/users/{OPENID}/messages"
            assert call["body"]["msg_id"] == "message-445566"
    for instance_id, window_id in window_ids.items():
        assert any(phrases[instance_id] in text for text in chat.inputs()), chat.requests
        async with session_scope() as session:
            users = list(
                (
                    await session.scalars(
                        select(Message).where(
                            Message.window_id == window_id,
                            Message.role == "user",
                        )
                    )
                ).all()
            )
        assert len(users) == 1
        assert phrases[instance_id] in users[0].text


async def _pending_delivery(env: TestEnv, window_id: str, text: str) -> tuple[str, str]:
    """Persist a finished turn and its interrupted delivery for the real worker."""
    turn_id = f"turn_accept_{uuid.uuid4().hex}"
    async with session_scope() as session:
        window = await session.get(Window, window_id)
        assert window is not None
        previous = await session.scalar(
            select(Message.sequence)
            .where(Message.window_id == window_id)
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        session.add(
            Turn(
                id=turn_id,
                window_id=window_id,
                client_turn_id=turn_id,
                state="completed",
            )
        )
        session.add(
            Message(
                id=f"msg_accept_{uuid.uuid4().hex}",
                window_id=window_id,
                turn_id=turn_id,
                role="actor",
                text=text,
                state="completed",
                sequence=(previous or 0) + 1,
                origin="model_inference",
            )
        )
        pending = await DeliveryService(session).record_pending(
            connector_id=window.channel,
            actor_id=env.settings.actor_id,
            subject_id=env.subject_id,
            window_ref=window.title,
            is_group=False,
            plan_id=f"plan_{turn_id}",
            turn_id=turn_id,
            reason="generation_timeout",
            detail="独立验收持久续发任务",
        )
        pending.created_at = datetime.now(UTC) - timedelta(minutes=1)
        return pending.id, turn_id


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
@pytest.mark.parametrize("stop_alpha", [None, "disabled", "deleted"])
async def test_worker_resumes_delivery_using_each_persisted_window_instance(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    stop_alpha: str | None,
) -> None:
    from ling.domains.goals import delivery_reconcile
    from ling.worker import handle_delivery_reconcile

    env = logged_in
    await _pair(env, scope)
    await _chat(env, monkeypatch)
    await _bind(env, scope)
    # A worker process resolves its settings from get_settings rather than the
    # API's state. Give it the same isolated database and encryption key.
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)

    async def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_reconcile, "_pause", no_pause)
    phrases = {key: f"{key}后台续发-{uuid.uuid4().hex}" for key in ("alpha", "bravo")}
    pending: dict[str, tuple[str, str]] = {}
    for instance_id in ("alpha", "bravo"):
        response = await _post(env, scope, instance_id, _event(scope, f"建立{instance_id}窗口"))
        assert response.status_code == 200, response.text
        assert response.json()["delivered"] is True, response.text
        window_id = await _window_for_turn(response.json()["turn_id"])
        pending[instance_id] = await _pending_delivery(env, window_id, phrases[instance_id])

    target = f"{BASE}/{scope}/instances/alpha"
    if stop_alpha == "disabled":
        response = await env.client.put(
            f"{target}/enabled", headers=ORIGIN, json={"enabled": False}
        )
        assert response.status_code == 200, response.text
    elif stop_alpha == "deleted":
        response = await env.client.delete(
            target, headers={**ORIGIN, "Content-Type": "application/json"}
        )
        assert response.status_code == 200, response.text

    protocol_wire.calls.clear()
    await handle_delivery_reconcile({})
    sends = protocol_wire.sends()
    expected = {"bravo"} if stop_alpha else {"alpha", "bravo"}
    assert len(sends) == len(expected), sends
    assert {call["host"] for call in sends} == {
        f"{instance_id}.{scope}.example.test" for instance_id in expected
    }
    for call in sends:
        instance_id = call["host"].split(".", 1)[0]
        assert phrases[instance_id] in json.dumps(call["body"], ensure_ascii=False)
        if scope == "onebot":
            assert call["authorization"] == f"Bearer fake-access-{instance_id}"
            assert str(call["body"]["user_id"]) == USER_ID
        else:
            assert call["authorization"] == f"QQBot token-{instance_id}.qqbot.example.test"
            assert call["path"] == f"/v2/users/{OPENID}/messages"

    async with session_scope() as session:
        for instance_id, (pending_id, turn_id) in pending.items():
            operation = await session.get(Operation, pending_id)
            assert operation is not None
            assert operation.state == ("succeeded" if instance_id in expected else "proposed")
            deliveries = list(
                (await session.scalars(select(Operation).where(Operation.kind == "delivery"))).all()
            )
            resumed = [
                row for row in deliveries if row.payload.get("reply_plan") == f"plan_{turn_id}"
            ]
            assert len(resumed) == (1 if instance_id in expected else 0)
            for row in resumed:
                assert row.state == "succeeded", row.detail
                assert row.payload["instance"] == f"{scope}-{instance_id}"
                assert row.payload["window_ref"].startswith(f"{scope}-{instance_id}:")
                assert row.subject_id == env.subject_id

    # Settled deliveries are idempotent. An unavailable A remains pending while
    # B's positive delivery has already demonstrated that this worker ran.
    before = list(protocol_wire.sends())
    await handle_delivery_reconcile({})
    assert protocol_wire.sends() == before


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
@pytest.mark.parametrize("interrupted_candidate", [False, True])
async def test_fresh_lifespan_recovers_enabled_instances_and_stops_each_component(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
    interrupted_candidate: bool,
) -> None:
    env = logged_in
    original = await _pair(env, scope)
    for instance_id in ("charlie", "delta"):
        await _create(env, scope, instance_id)
        saved = await _save(env, scope, instance_id, _values(scope, instance_id))
        assert saved.status_code == 202, saved.text
        assert saved.json()["state"] == "active", saved.text
    disabled = await env.client.put(
        f"{BASE}/{scope}/instances/charlie/enabled",
        headers=ORIGIN,
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    deleted = await env.client.delete(
        f"{BASE}/{scope}/instances/delta",
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 200, deleted.text
    candidate_id = f"crev_interrupted_{uuid.uuid4().hex}"
    previous_active_id = None
    if interrupted_candidate:
        async with session_scope() as session:
            head = await session.get(ConnectorConfigHead, (scope, "alpha"))
            assert head is not None and head.active_revision_id
            previous_active_id = head.active_revision_id
            session.add(
                ConnectorConfigRevision(
                    id=candidate_id,
                    scope=scope,
                    instance_id="alpha",
                    parent_id=previous_active_id,
                    state="pending",
                    created_by=env.subject_id,
                )
            )
            rows = list(
                (
                    await session.scalars(
                        select(ConnectorOverrideValue).where(
                            ConnectorOverrideValue.revision_id == previous_active_id
                        )
                    )
                ).all()
            )
            assert rows
            for row in rows:
                value = row.plain_value
                if row.field_key == f"LING_{scope.upper()}_API_BASE":
                    value = f"https://unactivated.{scope}.example.test"
                session.add(
                    ConnectorOverrideValue(
                        id=f"covr_interrupted_{uuid.uuid4().hex}",
                        revision_id=candidate_id,
                        field_key=row.field_key,
                        value_kind=row.value_kind,
                        plain_value=null() if row.value_kind == "secret" else value,
                        ciphertext=row.ciphertext,
                        preview=row.preview,
                        audit_fingerprint=row.audit_fingerprint,
                    )
                )
            head.candidate_revision_id = candidate_id
    old_connectors = {key: original.connector(scope, key) for key in ("alpha", "bravo")}
    old_components = {key: _transport(original, scope, key) for key in ("alpha", "bravo")}
    await original.shutdown()
    for component in old_components.values():
        assert protocol_wire.stopped(component) == 1

    startup_index = len(protocol_wire.lifecycle)
    restarted = create_app(env.settings)
    recovered_components: list[object] = []
    try:
        async with (
            restarted.router.lifespan_context(restarted),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
            ) as client,
        ):
            login = await client.post(
                "/v1/auth/login",
                json={"username": "tester", "password": "tester-password"},
            )
            assert login.status_code == 200, login.text
            restarted_env = TestEnv(
                client=client,
                settings=env.settings,
                schema=env.schema,
                app=restarted,
                subject_id=env.subject_id,
            )
            runtime = restarted.state.connector_runtime
            protocol_wire.calls.clear()
            for instance_id in ("alpha", "bravo"):
                connector = runtime.connector(scope, instance_id)
                assert connector is not None and connector is not old_connectors[instance_id]
                assert connector.instance_id == f"{scope}-{instance_id}"
                component = _transport(runtime, scope, instance_id)
                recovered_components.append(component)
                assert protocol_wire.stopped(component) == 0
                assert (
                    sum(
                        action == "start" and obj is component
                        for action, obj in protocol_wire.lifecycle
                    )
                    == 1
                )
                await _health(restarted_env, scope, instance_id)
                assert (await _view(restarted_env, scope, instance_id))["generation"] == 1

            if scope == "onebot":
                calls = [call for call in protocol_wire.calls if call["path"] == "/get_login_info"]
                assert {(call["host"], call["authorization"]) for call in calls} == {
                    (f"{key}.onebot.example.test", f"Bearer fake-access-{key}")
                    for key in ("alpha", "bravo")
                }
            else:
                calls = [
                    call
                    for call in protocol_wire.calls
                    if call["path"].endswith("/app/getAppAccessToken")
                ]
                assert {
                    (call["host"], call["body"]["appId"], call["body"]["clientSecret"])
                    for call in calls
                } == {
                    (f"{key}.qqbot.example.test", f"fake-app-{key}", f"fake-client-{key}")
                    for key in ("alpha", "bravo")
                }
            for instance_id in ("charlie", "delta"):
                assert runtime.connector(scope, instance_id) is None
                assert runtime.components(scope, instance_id) == {}
                response = await client.get(f"/v1/{scope}/instances/{instance_id}/health")
                assert response.status_code == 404, response.text
                response = await _post(
                    restarted_env, scope, instance_id, _event(scope, "重启后停用实例")
                )
                assert response.status_code == 404, response.text
            started_namespaces = [
                component._connector.instance_id
                for action, component in protocol_wire.lifecycle[startup_index:]
                if action == "start" and hasattr(component, "_connector")
            ]
            assert f"{scope}-charlie" not in started_namespaces
            assert f"{scope}-delta" not in started_namespaces
            assert started_namespaces.count(f"{scope}-alpha") == 1
            assert started_namespaces.count(f"{scope}-bravo") == 1
            legacy = restarted.state.onebot_connector
            assert legacy.instance_id == "legacy-onebot"
            assert legacy._config.access_token == "fake-default-access"
            response = await client.get("/v1/onebot/health")
            assert response.status_code == 200, response.text
            assert response.json()["instance"] == "legacy-onebot"
            if interrupted_candidate:
                async with session_scope() as session:
                    head = await session.get(ConnectorConfigHead, (scope, "alpha"))
                    candidate = await session.get(ConnectorConfigRevision, candidate_id)
                    assert head is not None and candidate is not None
                    assert head.active_revision_id == previous_active_id
                    assert head.candidate_revision_id is None
                    assert head.generation == 1
                    assert candidate.state == "rolled_back"
                changed = await _save(
                    restarted_env,
                    scope,
                    "alpha",
                    {f"LING_{scope.upper()}_API_BASE": f"https://recovered.{scope}.example.test"},
                    generation=1,
                )
                assert changed.status_code == 202, changed.text
                assert changed.json()["state"] == "active", changed.text
                assert changed.json()["generation"] == 2
                recovered_components.append(_transport(runtime, scope, "alpha"))
                await _health(restarted_env, scope, "alpha")
                assert runtime.connector(scope, "bravo") is not None
    finally:
        # The nested app disposes the process-local engine. Restore the fixture's
        # factory so its outer lifespan can close normally even on an assertion.
        init_engine(env.settings)
    assert len(recovered_components) == 2 + int(interrupted_candidate)
    for component in recovered_components:
        assert protocol_wire.stopped(component) == 1


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
@pytest.mark.parametrize("action", ["disabled", "deleted", "updated"])
async def test_acquired_worker_connector_rechecks_committed_instance_change(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    action: str,
) -> None:
    from ling import worker
    from ling.domains.goals import delivery_reconcile

    env = logged_in
    await _pair(env, scope)
    await _chat(env, monkeypatch)
    await _bind(env, scope)
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)
    phrases = {key: f"{key}并发续发-{uuid.uuid4().hex}" for key in ("alpha", "bravo")}
    pending_ids: dict[str, str] = {}
    for instance_id in ("alpha", "bravo"):
        response = await _post(env, scope, instance_id, _event(scope, f"建立{instance_id}窗口"))
        assert response.status_code == 200, response.text
        assert response.json()["delivered"] is True, response.text
        window_id = await _window_for_turn(response.json()["turn_id"])
        pending_ids[instance_id], _ = await _pending_delivery(env, window_id, phrases[instance_id])

    acquired = asyncio.Event()
    release = asyncio.Event()
    old_objects: list[object] = []
    original_loader = worker._group_reply_connector

    async def paused_loader(channel: str, window_ref: str):
        connector = await original_loader(channel, window_ref)
        if window_ref.startswith(f"{scope}-alpha:"):
            assert connector is not None
            old_objects.append(connector)
            acquired.set()
            await release.wait()
        return connector

    async def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(worker, "_group_reply_connector", paused_loader)
    monkeypatch.setattr(delivery_reconcile, "_pause", no_pause)
    protocol_wire.calls.clear()
    work = asyncio.create_task(worker.handle_delivery_reconcile({}))
    try:
        await asyncio.wait_for(acquired.wait(), timeout=10)
        assert len(old_objects) == 1
        target = f"{BASE}/{scope}/instances/alpha"
        if action == "disabled":
            changed = await env.client.put(
                f"{target}/enabled", headers=ORIGIN, json={"enabled": False}
            )
            assert changed.status_code == 200, changed.text
        elif action == "deleted":
            changed = await env.client.delete(
                target, headers={**ORIGIN, "Content-Type": "application/json"}
            )
            assert changed.status_code == 200, changed.text
        else:
            changed = await _save(
                env,
                scope,
                "alpha",
                {f"LING_{scope.upper()}_API_BASE": f"https://updated.{scope}.example.test"},
                generation=1,
            )
            assert changed.status_code == 202, changed.text
            assert changed.json()["state"] == "active", changed.text
            assert changed.json()["generation"] == 2
    finally:
        release.set()
        await asyncio.wait_for(work, timeout=10)

    sends = protocol_wire.sends()
    assert len(sends) == 1, sends
    assert sends[0]["host"] == f"bravo.{scope}.example.test"
    assert phrases["bravo"] in json.dumps(sends[0]["body"], ensure_ascii=False)
    assert phrases["alpha"] not in json.dumps(sends, ensure_ascii=False)
    expected_authorization = (
        "Bearer fake-access-bravo" if scope == "onebot" else "QQBot token-bravo.qqbot.example.test"
    )
    assert sends[0]["authorization"] == expected_authorization
    async with session_scope() as session:
        alpha = await session.get(Operation, pending_ids["alpha"])
        bravo = await session.get(Operation, pending_ids["bravo"])
        assert alpha is not None and alpha.state == "proposed", alpha.detail if alpha else None
        assert bravo is not None and bravo.state == "succeeded", bravo.detail if bravo else None


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_failed_stop_restores_target_and_leaves_other_instance_running(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    alpha = runtime.connector(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    old_component = _transport(runtime, scope, "alpha")
    bravo_component = _transport(runtime, scope, "bravo")
    original_stop = old_component.stop

    async def stop_then_fail() -> None:
        await original_stop()
        raise RuntimeError("controlled stop failure")

    monkeypatch.setattr(old_component, "stop", stop_then_fail)
    target = f"{BASE}/{scope}/instances/alpha"
    failed = await env.client.put(f"{target}/enabled", headers=ORIGIN, json={"enabled": False})
    assert failed.status_code == 503, failed.text
    assert runtime.connector(scope, "alpha") is not None
    assert runtime.connector(scope, "alpha") is not alpha
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(old_component) == 1
    assert protocol_wire.stopped(bravo_component) == 0
    listing = await env.client.get(f"{BASE}/{scope}/instances")
    assert listing.status_code == 200, listing.text
    directory = {item["instance_id"]: item for item in listing.json()["items"]}
    assert directory["alpha"]["enabled"] is True
    assert directory["bravo"]["enabled"] is True
    assert (await _view(env, scope, "alpha"))["generation"] == 1
    assert (await _view(env, scope, "bravo"))["generation"] == 1
    protocol_wire.calls.clear()
    await _health(env, scope, "alpha")
    assert any(call["host"] == f"alpha.{scope}.example.test" for call in protocol_wire.calls)
    again = await env.client.put(f"{target}/enabled", headers=ORIGIN, json={"enabled": False})
    assert again.status_code == 200, again.text
    assert runtime.connector(scope, "alpha") is None
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(bravo_component) == 0


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_cancelled_activation_clears_candidate_and_preserves_previous_configuration(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    previous = runtime.snapshot(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    bravo_component = _transport(runtime, scope, "bravo")
    transport_type = OneBotWsClient if scope == "onebot" else QQBotGateway
    original_start = transport_type.start

    def start_or_cancel(component) -> None:
        endpoint = component.url if scope == "onebot" else component._connector.config.base_url
        if "cancel-start" in endpoint:
            raise asyncio.CancelledError("controlled activation cancellation")
        original_start(component)

    monkeypatch.setattr(transport_type, "start", start_or_cancel)
    key = "LING_ONEBOT_WS_URL" if scope == "onebot" else "LING_QQBOT_API_BASE"
    endpoint = (
        "ws://cancel-start.onebot.example.test"
        if scope == "onebot"
        else "https://cancel-start.qqbot.example.test"
    )
    response = await _save(env, scope, "alpha", {key: endpoint}, generation=1)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "rolled_back", response.text
    assert runtime.snapshot(scope, "alpha") == previous
    assert runtime.connector(scope, "bravo") is bravo
    assert protocol_wire.stopped(bravo_component) == 0
    async with session_scope() as session:
        head = await session.get(ConnectorConfigHead, (scope, "alpha"))
        assert head is not None and head.candidate_revision_id is None
        assert head.generation == 1
    await _health(env, scope, "alpha")
    await _health(env, scope, "bravo")
    changed = await _save(
        env,
        scope,
        "alpha",
        {f"LING_{scope.upper()}_API_BASE": f"https://after-cancel.{scope}.example.test"},
        generation=1,
    )
    assert changed.status_code == 202, changed.text
    assert changed.json()["state"] == "active", changed.text
    assert changed.json()["generation"] == 2


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_management_and_dashboard_reflect_runtime_instance_capabilities(
    logged_in: TestEnv,
    scope: str,
) -> None:
    env = logged_in
    await _pair(env, scope)
    # The legacy configuration path creates the default directory row on a
    # fresh deployment. GET keeps its existing read-only contract.
    implicit_default = await env.client.put(
        f"{BASE}/{scope}",
        headers=ORIGIN,
        json={"set": {f"LING_{scope.upper()}_PROACTIVE": False}},
    )
    assert implicit_default.status_code in {200, 202}, implicit_default.text
    assert implicit_default.json()["state"] in {"active", "unchanged"}, implicit_default.text
    default = await env.client.put(
        f"{BASE}/{scope}/instances/default/enabled",
        headers=ORIGIN,
        json={"enabled": False},
    )
    assert default.status_code == 200, default.text
    draft = await env.client.put(
        "/v1/settings/draft",
        json={"dialogue": {"quiet_start_minute": 0, "quiet_end_minute": 0}},
    )
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text

    async def assert_display(expected: bool) -> None:
        management = await env.client.get("/v1/proactive")
        assert management.status_code == 200, management.text
        assert management.json()["gate"] is expected, management.text
        if expected:
            assert management.json()["gate_reasons"] == [], management.text
        else:
            assert management.json()["gate_reasons"], management.text
        dashboard = await env.client.get("/v1/dashboard")
        assert dashboard.status_code == 200, dashboard.text
        sections = dashboard.json()["sections"]
        proactive = sections["proactive"]
        assert proactive["status"] == "empty", proactive
        assert proactive["data"]["delivery_enabled"] is expected, proactive
        if expected:
            assert proactive["data"]["gate_reason"] == "", proactive
        else:
            assert proactive["data"]["gate_reason"], proactive
        connectivity = sections["connectivity"]
        assert connectivity["status"] == "ok", connectivity
        channel = next(
            item for item in connectivity["data"]["channels"] if item["channel"] == scope
        )
        assert channel["configured"] is True, channel
        assert channel["proactive_ready"] is expected, channel

    await assert_display(True)
    for instance_id, expected in (("alpha", True), ("bravo", False)):
        disabled = await env.client.put(
            f"{BASE}/{scope}/instances/{instance_id}/enabled",
            headers=ORIGIN,
            json={"enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        await assert_display(expected)
    enabled = await env.client.put(
        f"{BASE}/{scope}/instances/bravo/enabled",
        headers=ORIGIN,
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    await assert_display(True)


async def _deliver_direct(session, env: TestEnv, connector, phrase: str):
    peer = USER_ID if connector.connector_id == "onebot" else OPENID
    return await DeliveryService(session).deliver(
        connector=connector,
        message=OutboundMessage(
            operation_id="unused-before-delivery",
            window_ref=f"{connector.instance_id}:private:{peer}",
            text=phrase,
        ),
        actor_id=env.settings.actor_id,
        subject_id=env.subject_id,
    )


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_lifecycle_lock_refuses_target_without_blocking_other_instance(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    alpha = runtime.connector(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    assert alpha is not None and bravo is not None
    protocol_wire.calls.clear()

    async def send_alpha():
        async with session_scope() as session:
            return await _deliver_direct(session, env, alpha, "锁争用期间的 A 投递")

    async with session_scope() as lifecycle:
        await lifecycle.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"connector:{scope}:alpha"},
        )
        # B's positive result proves delivery is running while A's lifecycle
        # transaction remains open. The timeout only bounds a blocking defect.
        async with session_scope() as session:
            other = await _deliver_direct(session, env, bravo, "锁争用期间的 B 投递")
            assert other.delivered, other.detail
        refused = await asyncio.wait_for(send_alpha(), timeout=3)
        assert refused.delivered is False, refused.detail
        sends = protocol_wire.sends()
        assert len(sends) == 1, sends
        assert sends[0]["host"] == f"bravo.{scope}.example.test", sends

    async with session_scope() as session:
        restored = await _deliver_direct(session, env, alpha, "解锁后的 A 投递")
        assert restored.delivered, restored.detail
    assert [call["host"] for call in protocol_wire.sends()] == [
        f"bravo.{scope}.example.test",
        f"alpha.{scope}.example.test",
    ]


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
@pytest.mark.parametrize("preload_head", [False, True])
async def test_candidate_configuration_pauses_target_and_refreshes_cached_head(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    scope: str,
    preload_head: bool,
) -> None:
    env = logged_in
    runtime = await _pair(env, scope)
    alpha = runtime.connector(scope, "alpha")
    bravo = runtime.connector(scope, "bravo")
    assert alpha is not None and bravo is not None
    assert not hasattr(alpha, "_ling_config_generation")
    for connector in (alpha, bravo):
        async with session_scope() as session:
            baseline = await _deliver_direct(session, env, connector, "候选切换前的正向投递")
            assert baseline.delivered, baseline.detail
    protocol_wire.calls.clear()
    candidate_id = f"crev_paused_{uuid.uuid4().hex}"
    async with session_scope() as sending:
        cached_head = None
        if preload_head:
            cached_head = await sending.get(ConnectorConfigHead, (scope, "alpha"))
            assert cached_head is not None and cached_head.candidate_revision_id is None
        async with session_scope() as changing:
            head = await changing.get(ConnectorConfigHead, (scope, "alpha"))
            assert head is not None
            changing.add(
                ConnectorConfigRevision(
                    id=candidate_id,
                    scope=scope,
                    instance_id="alpha",
                    parent_id=head.active_revision_id,
                    state="activating",
                )
            )
            head.candidate_revision_id = candidate_id
        if cached_head is not None:
            assert cached_head.candidate_revision_id is None
        refused = await _deliver_direct(sending, env, alpha, "候选切换期间禁止的 A 投递")
        assert refused.delivered is False, refused.detail
        other = await _deliver_direct(sending, env, bravo, "候选切换期间允许的 B 投递")
        assert other.delivered, other.detail
    sends = protocol_wire.sends()
    assert len(sends) == 1, sends
    assert sends[0]["host"] == f"bravo.{scope}.example.test", sends
    async with session_scope() as changing:
        head = await changing.get(ConnectorConfigHead, (scope, "alpha"))
        revision = await changing.get(ConnectorConfigRevision, candidate_id)
        assert head is not None and revision is not None
        head.candidate_revision_id = None
        revision.state = "rolled_back"
    async with session_scope() as session:
        restored = await _deliver_direct(session, env, alpha, "候选结束后允许的 A 投递")
        assert restored.delivered, restored.detail
    assert [call["host"] for call in protocol_wire.sends()] == [
        f"bravo.{scope}.example.test",
        f"alpha.{scope}.example.test",
    ]


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_default_video_callback_downloads_from_original_window_instance(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    from ling.domains.consent.service import ConsentService
    from ling.domains.conversation.service import ConversationService
    from ling.domains.media.service import INBOUND_SCOPE, MediaEvidenceService
    from ling.domains.media.video import short_ref_for
    from ling.domains.media.watch import default_watch_callback
    from ling.ports.model import ModelChunk

    env = logged_in
    await _pair(env, scope)
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)
    downloads: list[tuple[str, str]] = []
    model_inputs: list[bytes] = []
    connector_type = (
        onebot_module.OneBotV11Connector if scope == "onebot" else qqbot_module.QQBotConnector
    )

    async def fetch(connector, ref: str) -> tuple[bytes, str]:
        downloads.append((connector.instance_id, ref))
        return connector.instance_id.encode(), "video/mp4"

    async def native_mode(session, *, subject_id: str | None) -> str:
        return "native"

    async def stream(config, *, profile, messages, allow_fallback):
        assert profile == "perception.video"
        assert allow_fallback is False
        assert len(messages) == 1 and len(messages[0].videos) == 1
        video = base64.b64decode(messages[0].videos[0].url.split(",", 1)[1])
        model_inputs.append(video)
        yield ModelChunk(delta=f"视频来自 {video.decode()}"), SimpleNamespace(error=None)

    monkeypatch.setattr(connector_type, "fetch_media", fetch)
    monkeypatch.setattr("ling.settings.user_preferences.load_video_mode", native_mode)
    callback = default_watch_callback(
        router=SimpleNamespace(stream=stream), actor_id=env.settings.actor_id,
    )
    evidence: dict[tuple[str, int], tuple[str, str]] = {}
    shared_ref = "opaque-same-video-reference"
    async with session_scope() as session:
        await ConsentService(session).grant(subject_id=env.subject_id, scope=INBOUND_SCOPE)
        conversation = ConversationService(session)
        media = MediaEvidenceService(session, actor_id=env.settings.actor_id)
        for instance_id in ("alpha", "bravo"):
            peer = USER_ID if scope == "onebot" else OPENID
            window = await conversation.create_window(
                subject_id=env.subject_id,
                title=f"{scope}-{instance_id}:private:{peer}",
                channel=scope,
            )
            for ordinal in (1, 2):
                item = await media.record(
                    subject_id=env.subject_id,
                    window_id=window.id,
                    media_ref=shared_ref,
                    kind="video",
                    source_event_id=f"video-{instance_id}-{ordinal}",
                )
                evidence[(instance_id, ordinal)] = (window.id, short_ref_for(item.evidence_id))

    for instance_id in ("alpha", "bravo"):
        window_id, ref = evidence[(instance_id, 1)]
        result = await callback(window_id, [ref])
        assert result["reads"] == 1, result
        assert f"视频来自 {scope}-{instance_id}" in result["observed"], result
        assert result["denied_kind"] is None, result
    assert downloads == [(f"{scope}-alpha", shared_ref), (f"{scope}-bravo", shared_ref)]
    assert model_inputs == [f"{scope}-alpha".encode(), f"{scope}-bravo".encode()]
    disabled = await env.client.put(
        f"{BASE}/{scope}/instances/alpha/enabled", headers=ORIGIN, json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    window_id, ref = evidence[("alpha", 2)]
    refused = await callback(window_id, [ref])
    assert refused["observed"] == "", refused
    assert refused["reads"] == 0 and refused["denied_kind"] == "error", refused
    assert len(downloads) == 2 and len(model_inputs) == 2
    window_id, ref = evidence[("bravo", 2)]
    other = await callback(window_id, [ref])
    assert other["reads"] == 1 and f"视频来自 {scope}-bravo" in other["observed"], other
    assert downloads == [
        (f"{scope}-alpha", shared_ref),
        (f"{scope}-bravo", shared_ref),
        (f"{scope}-bravo", shared_ref),
    ]
    assert model_inputs == [
        f"{scope}-alpha".encode(), f"{scope}-bravo".encode(), f"{scope}-bravo".encode(),
    ]


async def test_proactive_unavailable_instance_falls_back_to_web_and_other_instance_sends(
    logged_in: TestEnv,
    protocol_wire: ProtocolWire,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ling import worker
    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import ProactiveOpportunity, Subject

    env = logged_in
    for instance_id in ("alpha", "bravo"):
        await _create(env, "onebot", instance_id)
        values = _values("onebot", instance_id)
        if instance_id == "alpha":
            values.pop("LING_ONEBOT_API_BASE")
        changed = await _save(env, "onebot", instance_id, values)
        assert changed.status_code == 202, changed.text
    runtime = env.app.state.connector_runtime
    alpha = runtime.connector("onebot", "alpha")
    bravo = runtime.connector("onebot", "bravo")
    assert alpha is not None and bravo is not None
    assert alpha.capabilities().supports_proactive is True
    assert alpha.capabilities().enabled is False
    assert bravo.capabilities().supports_proactive is True
    assert bravo.capabilities().enabled is True
    draft = await env.client.put(
        "/v1/settings/draft",
        json={"dialogue": {
            "proactive_delivery_enabled": True,
            "proactive_daily_limit": 5,
            "quiet_start_minute": 0,
            "quiet_end_minute": 0,
        }},
    )
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]},
    )
    assert published.status_code == 200, published.text
    monkeypatch.setattr(worker, "get_settings", lambda: env.settings)
    monkeypatch.setattr("ling.config.get_settings", lambda: env.settings)
    subjects = {"alpha": env.subject_id, "bravo": f"subj_bravo_{uuid.uuid4().hex}"}
    phrases = {key: f"{key}主动投递-{uuid.uuid4().hex}" for key in subjects}
    opportunities: dict[str, str] = {}
    web_windows: dict[str, str] = {}
    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add(Subject(id=subjects["bravo"], display_name="验收 B 用户"))
        await session.flush()
        conversation = ConversationService(session)
        # B is first so the counterexample also executes its positive delivery.
        for ordinal, instance_id in enumerate(("bravo", "alpha")):
            await conversation.create_window(
                subject_id=subjects[instance_id],
                title=f"onebot-{instance_id}:private:{USER_ID}",
                channel="onebot",
            )
            web = await conversation.create_window(
                subject_id=subjects[instance_id], title=f"{instance_id} Web 回退", channel="web",
            )
            web_windows[instance_id] = web.id
            opportunity_id = f"opp_fallback_{uuid.uuid4().hex}"
            opportunities[instance_id] = opportunity_id
            session.add(ProactiveOpportunity(
                id=opportunity_id,
                actor_id=env.settings.actor_id,
                subject_id=subjects[instance_id],
                kind="check_in",
                reason="多实例主动回退验收",
                state="candidate",
                expires_at=now + timedelta(hours=1),
                created_at=now + timedelta(milliseconds=ordinal),
                draft_text=phrases[instance_id],
            ))
    protocol_wire.calls.clear()
    with caplog.at_level(logging.INFO):
        await worker.handle_proactive_dispatch({})
    sends = protocol_wire.sends()
    assert len(sends) == 1, sends
    assert sends[0]["host"] == "bravo.onebot.example.test", sends
    assert phrases["bravo"] in json.dumps(sends[0]["body"], ensure_ascii=False)
    assert phrases["alpha"] not in json.dumps(sends, ensure_ascii=False)
    async with session_scope() as session:
        alpha_opportunity = await session.get(ProactiveOpportunity, opportunities["alpha"])
        bravo_opportunity = await session.get(ProactiveOpportunity, opportunities["bravo"])
        messages = list((await session.scalars(select(Message).where(
            Message.window_id.in_(web_windows.values()), Message.role == "actor",
        ))).all())
    assert bravo_opportunity is not None and bravo_opportunity.state == "sent"
    assert not [message for message in messages if message.window_id == web_windows["bravo"]]
    assert alpha_opportunity is not None and alpha_opportunity.state == "sent", (
        alpha_opportunity.reason if alpha_opportunity is not None else "missing alpha opportunity"
    )
    assert [(message.window_id, message.text) for message in messages] == [
        (web_windows["alpha"], phrases["alpha"]),
    ]
    skipped = [getattr(record, "attributes", {}) for record in caplog.records
               if getattr(record, "event_name", "") == "proactive_dispatch_skipped"
               and getattr(record, "attributes", {}).get("reason_code") == "no_proactive_channel"]
    assert [attributes["opportunity_id"] for attributes in skipped] == [opportunities["alpha"]]
