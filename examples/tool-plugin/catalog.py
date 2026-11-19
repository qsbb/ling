"""Runnable read-only tool-plugin example; stdlib only, no Ling secrets needed."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CATALOG = {"茶": "茉莉花茶、白茶", "咖啡": "浅烘拼配、冷萃"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/call":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("invalid length")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or data.get("schema_version") != "v1":
                raise ValueError("invalid payload")
            query = data.get("text")
            if not isinstance(query, str) or not 0 < len(query) <= 400:
                raise ValueError("invalid query")
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        payload = json.dumps(
            {"schema_version": "v1", "result": CATALOG.get(query, "目录中没有这个条目。")},
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
