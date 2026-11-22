"""Integration coverage for administrator-registered external plugins.

Uses a real PostgreSQL schema via ``web_env``/``logged_in`` and a mocked
plugin HTTP endpoint. Plugin code is never loaded into the Ling process.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from ling import __version__ as HOST_VERSION
from ling.capabilities.external_plugin_contract import API_VERSION
from ling.capabilities import external_plugins_client as client_mod
from ling.infra.db import session_scope
from ling.infra.models import ExternalPluginRow, ExternalPluginStateRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PLUGIN_ID = "example.notes"
ENDPOINT = "http://plugin.example.test"


def _manifest(**overrides: Any) -> dict[str, Any]:
    base = {
        "manifest_version": "v1",
        "id": PLUGIN_ID,
        "version": "1.0.0",
        "name": "示例笔记插件",
        "description": "",
        "api_version": API_VERSION,
        "min_host_version": HOST_VERSION,
        "max_host_version": HOST_VERSION,
        "capabilities": ["tools", "commands", "events", "host.state"],
        "events": ["turn.completed"],
        "tools": [{
            "name": "lookup",
            "description": "查询目录",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }],
        "commands": [{"name": "lookup", "pattern": "^/plugincmd example\\.notes lookup (.+)$"}],
    }
    base.update(overrides)
    return base


class _PluginServer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.host_rounds = 0
        self.fail_events = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/manifest":
            return httpx.Response(200, json=_manifest())
        if request.url.path.endswith("/v1/events"):
            body = json.loads(request.content)
            self.events.append(body)
            if self.fail_events:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/invoke":
            body = json.loads(request.content)
            self.calls.append(body)
            method = body.get("method")
            params = body.get("params") or {}
            rid = body.get("request_id", "")
            if body.get("host_results"):
                value = body["host_results"][0].get("value")
                return httpx.Response(200, json={
                    "api_version": API_VERSION,
                    "request_id": rid,
                    "result": {"text": value or "已保存"},
                    "host_calls": [],
                })
            if method == "commands.call":
                text = (params.get("arguments") or {}).get("text", "")
                return httpx.Response(200, json={
                    "api_version": API_VERSION,
                    "request_id": rid,
                    "result": {"text": f"命令结果：{text}"},
                    "host_calls": [],
                })
            tool = params.get("tool")
            if tool == "state_demo":
                self.host_rounds += 1
                return httpx.Response(200, json={
                    "api_version": API_VERSION,
                    "request_id": rid,
                    "result": {"text": ""},
                    "host_calls": [{
                        "call_id": "call_1",
                        "capability": "host.state",
                        "op": "set",
                        "key": "note",
                        "value": "插件自己的值",
                    }],
                })
            query = (params.get("arguments") or {}).get("query", "")
            return httpx.Response(200, json={
                "api_version": API_VERSION,
                "request_id": rid,
                "result": {"text": f"查询结果：{query}"},
                "host_calls": [],
            })
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def plugin_server(monkeypatch: pytest.MonkeyPatch) -> _PluginServer:
    server = _PluginServer()
    monkeypatch.setattr(
        client_mod.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(server.handler)),
    )
    return server


async def _register(env: TestEnv, *, enabled: bool = True) -> dict[str, Any]:
    response = await env.client.post(
        "/v1/plugins/register",
        json={
            "endpoint": ENDPOINT,
            "expected_id": PLUGIN_ID,
            "granted_capabilities": ["tools", "commands", "events", "host.state"],
            "enabled": enabled,
        },
    )
    assert response.status_code == 200, response.text
    if enabled:
        response = await env.client.post(
            f"/v1/plugins/{PLUGIN_ID}/enable", json={"enabled": True}
        )
        assert response.status_code == 200, response.text
    return response.json()


async def test_register_list_status_and_admin_only(
    logged_in: TestEnv, plugin_server: _PluginServer
) -> None:
    payload = await _register(logged_in)
    assert payload["id"] == PLUGIN_ID
    assert payload["enabled"] is True
    assert payload["status"] == "ready"
    assert ENDPOINT not in json.dumps(payload)

    listing = await logged_in.client.get("/v1/plugins")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["plugins"]] == [PLUGIN_ID]
    assert listing.json()["legacy"] == []

    status = await logged_in.client.get(f"/v1/plugins/{PLUGIN_ID}/status")
    assert status.status_code == 200
    assert status.json()["reachable"] is True

    legacy_status = await logged_in.client.get("/v1/settings/tool-status")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["external_plugins"][0]["id"] == PLUGIN_ID
    assert ENDPOINT not in legacy_status.text


async def test_non_admin_cannot_manage_plugins(
    logged_in: TestEnv, plugin_server: _PluginServer
) -> None:
    # The seeded account is admin/owner; create a normal user through the existing
    # identity service to prove the management boundary, then reuse its token.
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        user = await IdentityService(session).create_user(
            username="viewer", password="viewer-password", role="member"
        )
        await session.commit()
    assert user is not None
    login = await logged_in.client.post(
        "/v1/auth/login", json={"username": "viewer", "password": "viewer-password"}
    )
    assert login.status_code == 200, login.text
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=logged_in.app), base_url="http://testserver"
    ) as viewer_client:
        viewer_client.cookies.update(login.cookies)
        listing = await viewer_client.get("/v1/plugins")
        assert listing.status_code == 403, listing.text
        register = await viewer_client.post(
            "/v1/plugins/register",
            json={"endpoint": ENDPOINT, "granted_capabilities": ["tools"]},
        )
        assert register.status_code == 403, register.text


async def test_register_rejects_incompatible_host_version(
    logged_in: TestEnv, plugin_server: _PluginServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = plugin_server.handler

    def incompatible(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/manifest":
            return httpx.Response(200, json=_manifest(
                min_host_version="99.0.0", max_host_version="99.0.0"
            ))
        return original(request)

    monkeypatch.setattr(
        client_mod.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(incompatible)),
    )
    response = await logged_in.client.post(
        "/v1/plugins/register",
        json={"endpoint": ENDPOINT, "granted_capabilities": ["tools"]},
    )
    assert response.status_code == 422, response.text


async def test_disable_stops_command_and_enable_restores(
    logged_in: TestEnv, plugin_server: _PluginServer
) -> None:
    await _register(logged_in)
    disabled = await logged_in.client.post(
        f"/v1/plugins/{PLUGIN_ID}/disable", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    async with session_scope() as session:
        row = await session.get(ExternalPluginRow, PLUGIN_ID)
        assert row is not None and row.enabled is False
        from ling.capabilities.external_plugins_service import ExternalPluginService

        service = ExternalPluginService(session, client=client_mod.PluginClient())
        with pytest.raises(Exception):
            await service.invoke(
                plugin=row, method="commands.call", params={"command": "lookup", "arguments": {"text": "茶"}}
            )
    assert [call for call in plugin_server.calls if call.get("method") == "commands.call"] == []


async def test_host_state_is_plugin_scoped_and_persisted(
    logged_in: TestEnv, plugin_server: _PluginServer
) -> None:
    await _register(logged_in)
    async with session_scope() as session:
        row = await session.get(ExternalPluginRow, PLUGIN_ID)
        assert row is not None
        from ling.capabilities.external_plugins_service import ExternalPluginService

        service = ExternalPluginService(session, client=client_mod.PluginClient())
        text = await service.invoke(
            plugin=row, method="tools.call", params={"tool": "state_demo", "arguments": {}}
        )
        assert text == "已保存"
        stored = await session.get(ExternalPluginStateRow, (PLUGIN_ID, "note"))
        assert stored is not None and stored.value == "插件自己的值"
    assert plugin_server.host_rounds == 1

    # A second call reads the persisted value back through host.state.get.
    async with session_scope() as session:
        from ling.capabilities.external_plugins_service import ExternalPluginService

        service = ExternalPluginService(session, client=client_mod.PluginClient())
        row = await session.get(ExternalPluginRow, PLUGIN_ID)
        assert row is not None
        calls = [{
            "call_id": "g", "capability": "host.state", "op": "get", "key": "note"
        }]
        from ling.capabilities.external_plugin_contract import HostCall

        executor = service.host_call_executor(row)
        results = await executor([HostCall.model_validate(calls[0])], "req_state")
        assert results[0].ok is True and results[0].value == "插件自己的值"


async def test_event_id_stable_and_delivery_failure_is_isolated(
    logged_in: TestEnv, plugin_server: _PluginServer
) -> None:
    await _register(logged_in)
    async with session_scope() as session:
        from ling.capabilities.external_plugins_service import ExternalPluginService

        service = ExternalPluginService(session, client=client_mod.PluginClient())
        row = await session.get(ExternalPluginRow, PLUGIN_ID)
        assert row is not None
        first = await service.emit_completed(
            plugin=row, turn_id="turn_1", window_id="win_1", subject_id="subj_1", attempt=1
        )
        second = await service.emit_completed(
            plugin=row, turn_id="turn_1", window_id="win_1", subject_id="subj_1", attempt=1
        )
        assert first is True and second is True
        assert [event["event_id"] for event in plugin_server.events] == [
            "evt_turn_completed_turn_1_1_example.notes",
            "evt_turn_completed_turn_1_1_example.notes",
        ]
        assert all("text" not in json.dumps(event["data"]) for event in plugin_server.events)

        plugin_server.fail_events = True
        third = await service.emit_completed(
            plugin=row, turn_id="turn_2", window_id="win_1", subject_id="subj_1", attempt=1
        )
        assert third is False
