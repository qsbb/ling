from __future__ import annotations

import json
from functools import partial

import httpx
import pytest
from pydantic import ValidationError

from ling.capabilities import external_plugins_client as client_mod
from ling.capabilities.external_plugin_contract import (
    API_VERSION,
    PluginManifest,
    version_in_range,
)
from ling.capabilities.external_plugins_client import (
    PluginClient,
    PluginRejected,
    PluginUnavailable,
)
from ling.capabilities.external_plugins_wiring import parse_external_command


def _manifest(**overrides: object) -> dict[str, object]:
    base = {
        "manifest_version": "v1",
        "id": "example.notes",
        "version": "1.0.0",
        "name": "示例",
        "description": "",
        "api_version": API_VERSION,
        "min_host_version": "0.0.14",
        "max_host_version": "0.0.14",
        "capabilities": ["tools"],
        "events": [],
        "tools": [{
            "name": "lookup",
            "description": "查询",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }],
        "commands": [],
    }
    base.update(overrides)
    return base


def test_manifest_closed_version_range_and_capability_shapes() -> None:
    assert version_in_range("0.0.14", minimum="0.0.14", maximum="0.0.14") is True
    assert version_in_range("0.0.13", minimum="0.0.14", maximum="0.0.14") is False
    assert version_in_range("0.0.15", minimum="0.0.14", maximum="0.0.14") is False
    PluginManifest.model_validate(_manifest())
    for bad in (
        _manifest(capabilities=["tools", "shell.exec"]),
        _manifest(events=["unknown.event"], capabilities=["tools", "events"]),
        _manifest(tools=[], capabilities=["tools"]),
        _manifest(min_host_version="bad"),
    ):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/plugincmd example.notes lookup 茶", ("example.notes", "lookup", "茶")),
        ("/plugincmd example.notes", None),
        ("/plugincmd https://bad.example lookup x", None),
        ("随便说说", None),
    ],
)
def test_external_command_is_exact(text: str, expected: object) -> None:
    assert parse_external_command(text) == expected


class _Chunked(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


@pytest.mark.parametrize("scenario", ["redirect", "compressed", "oversize", "bad_json"])
async def test_manifest_read_is_bounded_and_fails_closed(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    frame = json.dumps(_manifest()).encode()
    chunk = _Chunked([frame] if scenario != "oversize" else [frame, b" " * 20000])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if scenario == "redirect":
            return httpx.Response(307, headers={"location": "http://other.example/v1/manifest"})
        if scenario == "compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=chunk)
        if scenario == "oversize":
            return httpx.Response(200, stream=chunk)
        return httpx.Response(200, text="not json")

    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", client)
    with pytest.raises(PluginUnavailable):
        await PluginClient().fetch_manifest("http://plugin.example.test")
    assert len(seen) == 1
    assert "accept-encoding" in seen[0].headers and seen[0].headers["accept-encoding"] == "identity"


async def test_invoke_host_calls_are_round_tripped(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("host_results"):
            return httpx.Response(200, json={
                "api_version": API_VERSION,
                "request_id": body["request_id"],
                "result": {"text": ""},
                "host_calls": [{
                    "call_id": "call_set",
                    "capability": "host.state",
                    "op": "set",
                    "key": "note",
                    "value": "记住",
                }],
            })
        return httpx.Response(200, json={
            "api_version": API_VERSION,
            "request_id": body["request_id"],
            "result": {"text": "已保存"},
            "host_calls": [],
        })

    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", client)

    async def executor(calls: list[object], request_id: str) -> list[object]:
        return [{"call_id": "call_set", "ok": True}]

    text = await PluginClient().invoke(
        endpoint="http://plugin.example.test",
        plugin_id="example.notes",
        method="tools.call",
        params={"tool": "remember", "arguments": {}},
        request_id="plgreq_1",
        host_executor=executor,
    )
    assert text == "已保存"
    assert len(bodies) == 2
    assert bodies[0]["host_results"] == []
    assert bodies[1]["host_results"][0]["call_id"] == "call_set"
    assert bodies[1]["host_results"][0]["ok"] is True


async def test_invoke_error_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "api_version": API_VERSION,
            "request_id": "plgreq_2",
            "error": {"code": "invalid_arguments", "message": "参数错误"},
        })

    monkeypatch.setattr(
        client_mod.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PluginRejected):
        await PluginClient().invoke(
            endpoint="http://plugin.example.test",
            plugin_id="example.notes",
            method="tools.call",
            params={},
            request_id="plgreq_2",
        )
