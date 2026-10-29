"""第三方客户端 SDK（Python 侧参考实现）+ 协议文档生成。

任何客户端（手机、VR、桌面形象、第三方 App）都通过同一套契约接入：
认证 → 提交 InteractionEnvelope → 读取 SSE/轮询结果 → 回报回执。

本 SDK 只依赖 httpx，不依赖 ling 的领域代码，因此第三方可以单独使用。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class LingClientConfig:
    base_url: str
    token: str | None = None
    timeout_seconds: float = 60.0


class LingClient:
    """最小客户端。所有方法都返回平台无关的 dict 或事件流。"""

    def __init__(self, config: LingClientConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http = http
        self._owns_http = http is None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._config.base_url, timeout=self._config.timeout_seconds
            )
        return self._http

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        return headers

    # ---------------------------------------------------------- 认证

    async def login(self, username: str, password: str) -> dict[str, Any]:
        client = await self._client()
        response = await client.post(
            "/v1/auth/login", json={"username": username, "password": password}
        )
        response.raise_for_status()
        data = response.json()
        # 只保留 cookie 里的会话；不把口令存在客户端
        cookie = response.cookies.get("ling_sid")
        if cookie:
            self._config.token = cookie
            client.cookies.set("ling_sid", cookie)
        return data

    async def me(self) -> dict[str, Any]:
        client = await self._client()
        response = await client.get("/v1/auth/me", headers=self._headers())
        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------------- 会话

    async def list_sessions(self) -> list[dict[str, Any]]:
        client = await self._client()
        response = await client.get("/v1/sessions", headers=self._headers())
        response.raise_for_status()
        return response.json()["sessions"]

    async def create_session(self, title: str | None = None) -> dict[str, Any]:
        client = await self._client()
        response = await client.post(
            "/v1/sessions", json={"title": title}, headers=self._headers()
        )
        response.raise_for_status()
        return response.json()

    async def messages(self, session_id: str, *, limit: int = 100) -> dict[str, Any]:
        client = await self._client()
        response = await client.get(
            f"/v1/sessions/{session_id}/messages",
            params={"limit": limit},
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    # ---------------------------------------------------------- 聊天

    async def send(self, session_id: str, text: str, *, client_turn_id: str) -> AsyncIterator[dict]:
        """发送并流式读取事件。产出 ``{"event": ..., "data": {...}}``。"""
        client = await self._client()
        async with client.stream(
            "POST",
            f"/v1/sessions/{session_id}/turns",
            json={"client_turn_id": client_turn_id, "text": text},
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
            event_name = ""
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    yield {"event": event_name, "data": json.loads(line[6:])}

    async def stop(self, turn_id: str) -> dict[str, Any]:
        client = await self._client()
        response = await client.post(f"/v1/turns/{turn_id}/stop", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def retry(self, turn_id: str) -> AsyncIterator[dict]:
        client = await self._client()
        async with client.stream(
            "POST", f"/v1/turns/{turn_id}/retry", headers=self._headers()
        ) as response:
            response.raise_for_status()
            event_name = ""
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    yield {"event": event_name, "data": json.loads(line[6:])}

    # ---------------------------------------------------------- 角色与设置

    async def persona(self) -> dict[str, Any]:
        client = await self._client()
        response = await client.get("/v1/persona", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def config(self) -> dict[str, Any]:
        client = await self._client()
        response = await client.get("/v1/settings/config", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def health(self) -> dict[str, Any]:
        """模型健康摘要（provider 是否可用）。"""
        client = await self._client()
        response = await client.get("/v1/health/model")
        response.raise_for_status()
        return response.json()

    async def liveness(self) -> dict[str, Any]:
        """存活探针（不需要登录）。

        与 ``health()`` 不是一回事：探针回答"进程还活着吗"，
        模型健康回答"能不能思考"。第三方做重连判断要用前者——
        拿模型健康当探针会在模型故障时误判成"服务挂了"。
        """
        client = await self._client()
        response = await client.get("/healthz")
        response.raise_for_status()
        return response.json()


def describe_protocol() -> dict[str, Any]:
    """机器可读的协议描述，供第三方生成客户端。"""
    return {
        "schema_version": "v1",
        "auth": {"type": "cookie", "name": "ling_sid", "login": "POST /v1/auth/login"},
        "chat": {
            "send": "POST /v1/sessions/{id}/turns",
            "stream_events": ["turn.started", "delta", "turn.completed", "turn.stopped",
                              "turn.failed"],
            "stop": "POST /v1/turns/{id}/stop",
            "retry": "POST /v1/turns/{id}/retry",
            "idempotency": "client_turn_id 唯一标识一轮",
        },
        "history": "GET /v1/sessions/{id}/messages（浏览器不保存权威历史）",
        "embodiment": {
            "note": "具身客户端使用 ling 原生 Session/Interaction/AvatarIntent 协议",
            "actions": [
                "nod",
                "shake_head",
                "tilt_head",
                "smile",
                "blink",
                "look_at_user",
                "wave",
                "idle_breath",
            ],
            "perception_default": "全部关闭，需逐项授权且有时效",
        },
        "errors": "ErrorResponse{code,message,retriable,detail,trace_id}",
    }


__all__ = ["LingClient", "LingClientConfig", "describe_protocol"]
