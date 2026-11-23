from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ling.capabilities import tool_plugins
from ling.capabilities.tool_plugins import (
    MANIFEST_NAME,
    PluginManifestError,
    ToolPlugin,
    load_tool_plugins,
    parse_plugin_command,
    register_tool_plugin,
)
from ling.capabilities.tools import ToolOutcome, ToolRegistry
from ling.capabilities.web_search import explicit_search_query, has_explicit_search_command


def test_operator_manifest_is_opt_in_and_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / MANIFEST_NAME
    assert load_tool_plugins(tmp_path) == ()
    entry = {"id": "lookup", "name": "查询", "endpoint": "https://plugin.example.test/run"}
    path.write_text(json.dumps({"schema_version": "v1", "plugins": [entry]}))
    plugin, = load_tool_plugins(tmp_path)
    assert plugin.tool_id == "plugin_lookup"
    assert plugin.enabled is False
    for entries in (
        [entry, entry],
        [entry | {"endpoint": "file:///tmp/secret"}],
        [entry | {"endpoint": "https://credential@plugin.example.test/run"}],
        [entry | {"endpoint": "https://plugin.example.test/run?key=secret"}],
        [entry | {"risk": "write"}],
    ):
        path.write_text(json.dumps({"schema_version": "v1", "plugins": entries}))
        with pytest.raises(PluginManifestError):
            load_tool_plugins(tmp_path)


@pytest.mark.parametrize("text", ["试试 lookup", "/plugin lookup", "/plugin https://example.test arg"])
def test_only_exact_plugin_commands_provide_a_target(text: str) -> None:
    assert parse_plugin_command("/plugin lookup 术语 A") == ("lookup", "术语 A")
    assert parse_plugin_command(text) is None


def test_explicit_search_is_not_a_match_for_discussing_search() -> None:
    assert explicit_search_query("能帮我搜一下博物馆") == "博物馆"
    assert explicit_search_query("帮我查一下公开资料") == "公开资料"
    assert explicit_search_query("搜索：博物馆") == "博物馆"
    assert explicit_search_query("搜索功能现在能用吗？") is None
    assert explicit_search_query("搜集博物馆资料有什么好处？") is None
    assert explicit_search_query("搜一下是什么意思？") is None
    assert explicit_search_query("帮我查一下是什么意思？") is None
    assert has_explicit_search_command("/search：")
    assert has_explicit_search_command("/SEARCH:" + "长" * 241)


class _Chunked(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


@pytest.mark.parametrize("scenario", ["boundary", "oversize", "redirect", "compressed"])
async def test_plugin_bounded_stream_and_fixed_destination(
    monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    limit = 16 * 1024  # public v1 wire limit
    frame = b'{"schema_version":"v1","result":"ok"}'
    exact = frame + b" " * (limit - len(frame))
    chunks = [exact] if scenario == "boundary" else [exact, b" ", b"must-not-be-consumed"]
    stream = _Chunked(chunks)
    requests: list[httpx.Request] = []
    url = "https://plugin.example.test/run"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if scenario == "redirect":
            return httpx.Response(307, headers={"location": "https://other.example.test/run"})
        if scenario == "compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
        return httpx.Response(200, stream=stream)

    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(tool_plugins, "httpx", SimpleNamespace(**(vars(httpx) | {"AsyncClient": client})))
    registry = ToolRegistry()
    register_tool_plugin(registry, ToolPlugin(id="lookup", name="Lookup", endpoint=url, enabled=True), text="query")
    result = await registry.call("plugin_lookup", {})
    assert [str(request.url) for request in requests] == [url]
    assert requests[0].headers["accept-encoding"] == "identity"
    if scenario == "boundary":
        assert result.outcome is ToolOutcome.succeeded
        assert result.output == "ok" and result.untrusted
    else:
        assert result.outcome is ToolOutcome.failed and result.output == ""
        assert stream.consumed == (2 if scenario == "oversize" else 0)
