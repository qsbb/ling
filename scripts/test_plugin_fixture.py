"""隔离的外部插件测试服务（仅标准库）：给 Web 页的端到端演练用。

与 ``examples/external-plugin/server.py`` 的区别：那份是文档示例，版本区间被
钉在写它的那个 Ling 版本上；这份可以按需要改 manifest（版本区间、id、能力），
用来在真实 API 上触发「登记后标识变化」「版本不兼容」这类失败分支。

运行::

    python scripts/test_plugin_fixture.py --port 8197 --id example.notes

``POST /__control`` 运行中改写 manifest（页面演练"同步后标识变化"要靠它）：:

    {"id": "example.renamed"}      # 换 id
    {"version_range": ["0.0.15", "0.0.15"]}
    {"capabilities": ["tools"]}    # 只声明工具
    {"mode": "bad_json"}           # manifest 返回非 JSON
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API_VERSION = "ling-plugin/1"

_state: dict[str, Any] = {
    "id": "example.notes",
    "name": "隔离测试插件",
    "version": "1.2.0",
    "version_range": ["0.0.15", "0.0.15"],
    "capabilities": ["tools", "commands", "events", "host.state"],
    "mode": "normal",
    "invocations": 0,
    "events": [],
}
_lock = threading.Lock()


def manifest() -> dict[str, Any]:
    caps = list(_state["capabilities"])
    body: dict[str, Any] = {
        "manifest_version": "v1",
        "id": _state["id"],
        "version": _state["version"],
        "name": _state["name"],
        "description": "页面验收用的隔离插件",
        "api_version": API_VERSION,
        "min_host_version": _state["version_range"][0],
        "max_host_version": _state["version_range"][1],
        "capabilities": caps,
        "events": ["turn.completed"] if "events" in caps else [],
        "tools": [],
        "commands": [],
    }
    if "tools" in caps:
        body["tools"] = [
            {
                "name": "lookup",
                "description": "查询隔离目录",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 200}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]
    if "commands" in caps:
        body["commands"] = [
            {"name": "lookup", "pattern": r"^/plugincmd example\.notes lookup (.+)$"}
        ]
    return body


def _send(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = payload if isinstance(payload, bytes) else json.dumps(
        payload, ensure_ascii=False
    ).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 64 * 1024:
            return None
        payload = json.loads(handler.rfile.read(length))
        return payload if isinstance(payload, dict) else None
    except (ValueError, RecursionError):
        return None


def _invoke(payload: dict[str, Any]) -> dict[str, Any]:
    rid = str(payload.get("request_id") or "")
    with _lock:
        _state["invocations"] += 1
    params = payload.get("params") or {}
    host_results = payload.get("host_results") or []
    if host_results:
        return {
            "api_version": API_VERSION,
            "request_id": rid,
            "result": {"text": str(host_results[0].get("value") or "已保存")},
            "host_calls": [],
        }
    if payload.get("method") == "commands.call":
        text = (params.get("arguments") or {}).get("text", "")
        return {
            "api_version": API_VERSION,
            "request_id": rid,
            "result": {"text": f"隔离目录命中：{text}"},
            "host_calls": [],
        }
    if payload.get("method") == "tools.call":
        query = (params.get("arguments") or {}).get("query", "")
        if query == "__state":
            return {
                "api_version": API_VERSION,
                "request_id": rid,
                "result": {"text": ""},
                "host_calls": [
                    {
                        "call_id": "call_get",
                        "capability": "host.state",
                        "op": "get",
                        "key": "note",
                    }
                ],
            }
        return {
            "api_version": API_VERSION,
            "request_id": rid,
            "result": {"text": f"隔离目录：{query or '（空）'}"},
            "host_calls": [],
        }
    return {
        "api_version": API_VERSION,
        "request_id": rid,
        "error": {"code": "method_not_allowed", "message": "不支持的方法"},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/manifest":
            if _state["mode"] == "bad_json":
                _send(self, 200, b"this is not json")
                return
            _send(self, 200, manifest())
            return
        if self.path == "/__state":
            with _lock:
                _send(self, 200, dict(_state))
            return
        _send(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        payload = _read(self)
        if payload is None:
            _send(self, 400, {"error": "invalid_request"})
            return
        if self.path == "/__control":
            with _lock:
                for key in ("id", "name", "version", "version_range", "capabilities", "mode"):
                    if key in payload:
                        _state[key] = payload[key]
                snapshot = dict(_state)
            _send(self, 200, snapshot)
            return
        if self.path == "/v1/invoke":
            _send(self, 200, _invoke(payload))
            return
        if self.path == "/v1/events":
            with _lock:
                _state["events"].append(payload)
            _send(self, 200, {"ok": True, "seen": len(_state["events"])})
            return
        _send(self, 404, {"error": "not_found"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8197)
    parser.add_argument("--id", default="example.notes")
    args = parser.parse_args()
    _state["id"] = args.id
    print(f"test plugin on {args.host}:{args.port} id={args.id}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
