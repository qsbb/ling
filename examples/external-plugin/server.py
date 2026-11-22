"""Standalone Ling external plugin example: stdlib only, no Ling secrets needed.

Run:
    python examples/external-plugin/server.py --host 127.0.0.1 --port 8096

It implements the v1 contract: manifest, invoke (tools/commands + host.state),
and idempotent turn.completed event handling.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API_VERSION = "ling-plugin/1"
PLUGIN_ID = "example.notes"
SUMMARY = re.compile(r"^(.*)$", re.DOTALL)

CATALOG = {
    "茶": "茉莉花茶、白茶",
    "咖啡": "浅烘拼配、冷萃",
}

_state: dict[str, str] = {}
_seen_events: set[str] = set()
_lock = threading.Lock()


def manifest() -> dict[str, Any]:
    return {
        "manifest_version": "v1",
        "id": PLUGIN_ID,
        "version": "1.0.0",
        "name": "示例笔记插件",
        "description": "演示工具、命令、事件和宿主状态能力",
        "api_version": API_VERSION,
        "min_host_version": "0.0.14",
        "max_host_version": "0.0.14",
        "capabilities": ["tools", "commands", "events", "host.state"],
        "events": ["turn.completed"],
        "tools": [
            {
                "name": "lookup",
                "description": "查询示例目录中的条目，返回一句话结果",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "remember",
                "description": "把一句话写入插件自己的持久状态（演示 host.state）",
                "input_schema": {
                    "type": "object",
                    "properties": {"note": {"type": "string", "minLength": 1, "maxLength": 500}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "recall",
                "description": "读取插件此前记住的一句话",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ],
        "commands": [
            {"name": "lookup", "pattern": r"^/plugincmd example\.notes lookup (.+)$"},
        ],
    }


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 16 * 1024:
            return None
        payload = json.loads(handler.rfile.read(length))
        return payload if isinstance(payload, dict) else None
    except (ValueError, RecursionError):
        return None


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handle_invoke(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "")
    method = payload.get("method")
    params = payload.get("params") or {}
    host_results = {
        str(item.get("call_id")): item for item in (payload.get("host_results") or [])
        if isinstance(item, dict)
    }

    if host_results:
        # Second round: read the result of state.set / state.get.
        note = host_results.get("call_set")
        got = host_results.get("call_get")
        if isinstance(got, dict) and got.get("ok"):
            value = got.get("value") or "（还没有记住任何内容）"
        else:
            value = str((note or {}).get("value") or "已保存")
        return {
            "api_version": API_VERSION,
            "request_id": request_id,
            "result": {"text": value},
            "host_calls": [],
        }

    if method == "commands.call":
        command = str(params.get("command") or "")
        text = str((params.get("arguments") or {}).get("text") or "")
        if command == "lookup":
            value = CATALOG.get(text, "目录中没有这个条目。")
            return {
                "api_version": API_VERSION,
                "request_id": request_id,
                "result": {"text": f"示例目录：{value}"},
                "host_calls": [],
            }
        return {
            "api_version": API_VERSION,
            "request_id": request_id,
            "error": {"code": "unknown_command", "message": "未知命令"},
        }

    if method == "tools.call":
        tool = str(params.get("tool") or "")
        arguments = params.get("arguments") or {}
        if tool == "lookup":
            query = str(arguments.get("query") or "")
            if not query:
                return {
                    "api_version": API_VERSION,
                    "request_id": request_id,
                    "error": {"code": "invalid_arguments", "message": "query 不能为空"},
                }
            value = CATALOG.get(query, "目录中没有这个条目。")
            return {
                "api_version": API_VERSION,
                "request_id": request_id,
                "result": {"text": f"示例目录：{value}"},
                "host_calls": [],
            }
        if tool == "remember":
            note = str(arguments.get("note") or "")
            if not note:
                return {
                    "api_version": API_VERSION,
                    "request_id": request_id,
                    "error": {"code": "invalid_arguments", "message": "note 不能为空"},
                }
            return {
                "api_version": API_VERSION,
                "request_id": request_id,
                "result": {"text": ""},
                "host_calls": [
                    {
                        "call_id": "call_set",
                        "capability": "host.state",
                        "op": "set",
                        "key": "note",
                        "value": note,
                    }
                ],
            }
        if tool == "recall":
            return {
                "api_version": API_VERSION,
                "request_id": request_id,
                "result": {"text": ""},
                "host_calls": [
                    {"call_id": "call_get", "capability": "host.state", "op": "get", "key": "note"}
                ],
            }
        return {
            "api_version": API_VERSION,
            "request_id": request_id,
            "error": {"code": "unknown_tool", "message": "未知工具"},
        }

    return {
        "api_version": API_VERSION,
        "request_id": request_id,
        "error": {"code": "method_not_allowed", "message": "不支持的方法"},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print("[plugin] " + format % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/manifest":
            _send(self, 200, manifest())
        elif self.path == "/healthz":
            _send(self, 200, {"ok": True})
        elif self.path == "/v1/events":
            with _lock:
                _send(self, 200, {"ok": True, "seen": sorted(_seen_events)})
        else:
            _send(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        payload = _read_body(self)
        if payload is None:
            _send(self, 400, {"error": "invalid_request"})
            return
        if self.path == "/v1/invoke":
            _send(self, 200, _handle_invoke(payload))
        elif self.path == "/v1/events":
            event_id = str(payload.get("event_id") or "")
            if not event_id or payload.get("api_version") != API_VERSION:
                _send(self, 400, {"error": "invalid_event"})
                return
            with _lock:
                is_new = event_id not in _seen_events
                _seen_events.add(event_id)
                data = payload.get("data") or {}
                if is_new and isinstance(data, dict):
                    _state["last_turn"] = str(data.get("turn_id") or "")
            _send(self, 200, {"ok": True, "duplicate": not is_new, "seen": len(_seen_events)})
        else:
            _send(self, 404, {"error": "not_found"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ling external plugin example")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args()
    print(f"plugin example listening on {args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
