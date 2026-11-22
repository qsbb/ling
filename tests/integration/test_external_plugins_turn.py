"""Turn-level wiring tests for external plugin commands and model tools."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ling.capabilities import external_plugins_client as plugin_client_mod
from ling.infra.db import session_scope
from ling.infra.models import ExternalPluginStateRow
from ling.models import openai_compatible
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from tests.integration import test_tools_turn_wiring as wiring
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ENDPOINT = "http://plugin.example.test"
PLUGIN_ID = "example.notes"


def _manifest() -> dict[str, Any]:
    from ling import __version__ as HOST_VERSION
    from ling.capabilities.external_plugin_contract import API_VERSION

    return {
        "manifest_version": "v1",
        "id": PLUGIN_ID,
        "version": "1.0.0",
        "name": "示例笔记插件",
        "description": "",
        "api_version": API_VERSION,
        "min_host_version": HOST_VERSION,
        "max_host_version": HOST_VERSION,
        "capabilities": ["tools", "commands", "host.state"],
        "events": [],
        "tools": [{
            "name": "lookup",
            "description": "查询目录",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "additionalProperties": False,
            },
        }],
        "commands": [{"name": "lookup", "pattern": "^/plugincmd example\\.notes lookup (.+)$"}],
    }


class _Server:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.manifest_id = PLUGIN_ID

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/manifest":
            return httpx.Response(200, json={**_manifest(), "id": self.manifest_id})
        if request.url.path == "/v1/invoke":
            body = json.loads(request.content)
            self.calls.append(body)
            from ling.capabilities.external_plugin_contract import API_VERSION

            if body["method"] == "tools.call":
                if body["host_results"]:
                    value = body["host_results"][0]["value"]
                    text = "state:" + (value if value is not None else "empty")
                    return httpx.Response(200, json={
                        "api_version": API_VERSION,
                        "request_id": body["request_id"],
                        "result": {"text": text},
                    })
                query = body["params"]["arguments"].get("query")
                if query != "茶":
                    call = {
                        "call_id": "note",
                        "capability": "host.state",
                        "op": "get" if query is None else "set",
                        "key": "note",
                    }
                    if query is not None:
                        call["value"] = query
                    return httpx.Response(200, json={
                        "api_version": API_VERSION,
                        "request_id": body["request_id"],
                        "result": {},
                        "host_calls": [call],
                    })
            return httpx.Response(200, json={
                "api_version": API_VERSION,
                "request_id": body["request_id"],
                "result": {"text": "插件返回的真实结果"},
                "host_calls": [],
            })
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def endpoint(logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch) -> wiring._ToolEndpoint:
    model = wiring._ToolEndpoint()
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(model.handle))
    monkeypatch.setattr(
        openai_compatible, "httpx", SimpleNamespace(**(vars(httpx) | {"AsyncClient": client}))
    )
    router = ModelRouter(ProviderRegistry(lambda _: "fixture-model-key"))
    logged_in.app.state.router = router
    return model


@pytest.fixture
def plugin_server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    server = _Server()
    monkeypatch.setattr(
        plugin_client_mod.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(server.handler)),
    )
    return server


async def _register(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/plugins/register",
        json={
            "endpoint": ENDPOINT,
            "expected_id": PLUGIN_ID,
            "granted_capabilities": ["tools", "commands", "host.state"],
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    response = await logged_in.client.post(
        f"/v1/plugins/{PLUGIN_ID}/enable", json={"enabled": True}
    )
    assert response.status_code == 200, response.text


async def test_explicit_command_runs_through_turn(
    logged_in: TestEnv,
    endpoint: wiring._ToolEndpoint,
    plugin_server: _Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await wiring._bind_model(logged_in, enabled=True)
    await _register(logged_in)
    reply = await wiring._web_reply(logged_in, text="/plugincmd example.notes lookup 茶")
    assert reply == "插件返回的真实结果"
    commands = [call for call in plugin_server.calls if call.get("method") == "commands.call"]
    assert len(commands) == 1
    assert commands[0]["params"] == {"command": "lookup", "arguments": {"text": "茶"}}


async def test_external_tool_is_exposed_to_model(
    logged_in: TestEnv,
    endpoint: wiring._ToolEndpoint,
    plugin_server: _Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await wiring._bind_model(logged_in, enabled=True)
    await _register(logged_in)
    endpoint.tool_name = "plugin_example_notes_lookup"
    endpoint.arguments = {"query": "茶"}
    reply = await wiring._web_reply(logged_in, text="帮我查一下茶")
    assert "插件返回的真实结果" in reply
    calls = [call for call in plugin_server.calls if call.get("method") == "tools.call"]
    assert len(calls) == 1
    assert calls[0]["params"] == {"tool": "lookup", "arguments": {"query": "茶"}}
    tools = {item["function"]["name"] for item in endpoint.requests[0]["tools"]}
    assert "plugin_example_notes_lookup" in tools


async def test_model_tool_persists_state_across_turns(
    logged_in: TestEnv, endpoint: wiring._ToolEndpoint, plugin_server: _Server,
) -> None:
    await wiring._bind_model(logged_in, enabled=True)
    await _register(logged_in)
    endpoint.tool_name = "plugin_example_notes_lookup"
    endpoint.arguments = {"query": "persisted fixture note"}
    assert "state:empty" in await wiring._web_reply(logged_in)
    assert plugin_server.calls[-1]["host_results"][0]["ok"] is True

    async with session_scope() as session:
        row = await session.get(ExternalPluginStateRow, (PLUGIN_ID, "note"))
        assert row is not None and row.value == "persisted fixture note"
    endpoint.arguments = {}
    assert "state:persisted fixture note" in await wiring._web_reply(logged_in)
    assert plugin_server.calls[-1]["host_results"][0]["value"] == "persisted fixture note"


@pytest.mark.parametrize("change", ["disable", "seal"])
async def test_tool_rechecks_access_after_model_proposal_starts(
    logged_in: TestEnv, endpoint: wiring._ToolEndpoint, plugin_server: _Server, change: str,
) -> None:
    await wiring._bind_model(logged_in, enabled=True)
    await _register(logged_in)
    endpoint.tool_name = "plugin_example_notes_lookup"
    endpoint.arguments = {"query": "茶"}
    assert "插件返回的真实结果" in await wiring._web_reply(logged_in)
    endpoint.requests.clear()
    started, release = asyncio.Event(), asyncio.Event()
    endpoint.pause_proposal = started, release
    turn = asyncio.create_task(wiring._web_reply(logged_in))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if change == "disable":
            response = await logged_in.client.post(f"/v1/plugins/{PLUGIN_ID}/disable")
        else:
            response = await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
        assert response.status_code == 200, response.text
    finally:
        release.set()
        await asyncio.wait_for(turn, timeout=5)
    assert len(plugin_server.calls) == 1


async def test_changed_manifest_identity_deactivates_registration(
    logged_in: TestEnv, endpoint: wiring._ToolEndpoint, plugin_server: _Server,
) -> None:
    await wiring._bind_model(logged_in, enabled=True)
    await _register(logged_in)
    text = f"/plugincmd {PLUGIN_ID} lookup 茶"
    assert await wiring._web_reply(logged_in, text=text) == "插件返回的真实结果"

    plugin_server.manifest_id = "other.notes"
    synced = await logged_in.client.post(f"/v1/plugins/{PLUGIN_ID}/sync")
    assert synced.status_code == 422, synced.text
    enabled = await logged_in.client.post(f"/v1/plugins/{PLUGIN_ID}/enable", json={"enabled": True})
    assert enabled.status_code == 422, enabled.text
    status = await logged_in.client.get(f"/v1/plugins/{PLUGIN_ID}/status")
    assert status.json()["enabled"] is False and status.json()["reachable"] is False
    assert await wiring._web_reply(logged_in, text=text) != "插件返回的真实结果"
    assert len(plugin_server.calls) == 1
