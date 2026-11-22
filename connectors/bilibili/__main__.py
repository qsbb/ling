from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

logger = logging.getLogger(__name__)
PLATFORM_EVENTS = {"live_start", "live_end"}
ROOM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info"


def event_from_message(kind: str, message: Any) -> dict:
    timestamp = int(message.timestamp)
    event: dict[str, Any] = {
        "kind": kind,
        "event_id": str(message.msg_id or f"{kind}:{message.room_id}:{timestamp}:{message.open_id}"),
        "room_id": str(message.room_id),
        "occurred_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
    }
    if kind == "comment":
        event["text"] = message.msg[:500]
        event["sender"] = {"id": message.open_id or None, "name": message.uname or None}
    return event


class Forwarder:
    def __init__(
        self, http: httpx.AsyncClient, room_http: httpx.AsyncClient, instance: str,
        *, signing_secret: str = "",
    ) -> None:
        self.http = http
        self.room_http = room_http
        self.path = f"/v1/custom/{instance}"
        self.signing_secret = signing_secret.encode()
        self.queue: asyncio.Queue[tuple[dict, str | None]] = asyncio.Queue(maxsize=64)
        self.session_id: str | None = None
        self.room_id: str | None = None
        self.platform: tuple[dict, str] | None = None
        self.next_probe = 0.0

    def headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.signing_secret:
            headers["X-Ling-Signature"] = "sha256=" + hmac.digest(
                self.signing_secret, body, "sha256",
            ).hex()
        return headers

    def submit(self, kind: str, message: Any) -> None:
        if kind == "comment" and getattr(message, "is_mirror", False):
            return
        event = event_from_message(kind, message)
        if kind in PLATFORM_EVENTS:
            if self.session_id is not None:
                self.platform = (event, self.session_id)
        elif self.session_id is not None:
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait((event, self.session_id))

    async def _post(self, event: dict, session_id: str) -> str:
        body = json.dumps({**event, "session_id": session_id}, ensure_ascii=False).encode()
        response = await self.http.post(f"{self.path}/events", content=body, headers=self.headers(body))
        response.raise_for_status()
        return response.json()["status"]

    async def room_status(self, room_id: str) -> dict | None:
        now = datetime.now(UTC)
        response = await self.room_http.get(ROOM_INFO, params={"room_id": room_id})
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        live = data.get("live_status")
        if body.get("code") != 0 or str(data.get("room_id")) != room_id or live not in {0, 1, 2}:
            return None
        return {
            "room_id": room_id, "kind": "live_start" if live == 1 else "live_end",
            "occurred_at": now.isoformat(), "event_id": f"room-status:{now.timestamp()}",
        }

    async def run(self) -> None:
        sent_platform: tuple[str, str] | None = None
        while True:
            try:
                response = await self.http.get(f"{self.path}/session", headers=self.headers(b""))
                response.raise_for_status()
                source = response.json()
                if source["session_id"] != self.session_id:
                    self.next_probe = 0
                self.session_id = source["session_id"]
                self.room_id = source["room_id"]
                if self.platform:
                    event, session_id = self.platform
                    key = (session_id, event["event_id"])
                    if session_id == self.session_id and event["room_id"] == self.room_id and key != sent_platform:
                        await self._post(event, session_id)
                        sent_platform = key
                if self.session_id is not None and self.room_id is not None and monotonic() >= self.next_probe:
                    self.next_probe = monotonic() + 10
                    try:
                        state = await self.room_status(self.room_id)
                    except (httpx.HTTPError, ValueError):
                        logger.info("暂时无法核对 B 站房间状态")
                    else:
                        if state:
                            await self._post(state, self.session_id)
                # Drain a bounded batch, then refresh the session assignment.
                for _ in range(16):
                    if self.queue.empty():
                        break
                    item, queued_session_id = self.queue.get_nowait()
                    if queued_session_id and queued_session_id == self.session_id and item["room_id"] == self.room_id:
                        await self._post(item, queued_session_id)
            except httpx.HTTPError:
                self.session_id = None
                logger.warning("暂时无法向 Ling 上报 B 站事件，请检查接入器状态和凭据")
            await asyncio.sleep(0.5)


async def _run(args: argparse.Namespace) -> None:
    try:
        import blivedm
    except ImportError as exc:
        raise SystemExit("请先按直播接入文档安装 blivedm") from exc
    credentials = [
        os.environ.get("BILIBILI_ACCESS_KEY_ID", ""), os.environ.get("BILIBILI_ACCESS_KEY_SECRET", ""),
        os.environ.get("BILIBILI_APP_ID", ""), os.environ.get("BILIBILI_ANCHOR_CODE", ""),
    ]
    if not all(credentials):
        raise SystemExit("请设置 B 站开放平台应用凭据和主播身份码")
    platform_client = blivedm.OpenLiveClient(
        credentials[0], credentials[1], int(credentials[2]), credentials[3],
    )
    source_token = os.environ.get("LING_INGRESS_TOKEN")
    headers = {"Authorization": f"Bearer {source_token}"} if source_token else {}
    signing_secret = os.environ.get("LING_INGRESS_SIGNING_SECRET", "")
    async with (
        httpx.AsyncClient(base_url=args.server, headers=headers, timeout=5) as http,
        httpx.AsyncClient(timeout=3) as room_http,
    ):
        forwarder = Forwarder(http, room_http, args.instance, signing_secret=signing_secret)

        class Handler(blivedm.BaseHandler):
            def _on_open_live_danmaku(self, _client, message):
                forwarder.submit("comment", message)

            def _on_open_live_gift(self, _client, message):
                forwarder.submit("gift", message)

            def _on_open_live_like(self, _client, message):
                forwarder.submit("like", message)

            def _on_open_live_enter_room(self, _client, message):
                forwarder.submit("entrance", message)

            def _on_open_live_start_live(self, _client, message):
                forwarder.submit("live_start", message)

            def _on_open_live_end_live(self, _client, message):
                forwarder.submit("live_end", message)

        platform_client.set_handler(Handler())
        platform_client.start()
        try:
            async with asyncio.TaskGroup() as tasks:
                sender = tasks.create_task(forwarder.run())
                await platform_client.join()
                sender.cancel()
        finally:
            await platform_client.stop_and_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="B 站开放平台弹幕采集")
    parser.add_argument("--server", required=True, help="Ling API 地址")
    parser.add_argument("--instance", required=True, help="已启用的 B 站接入器 ID")
    args = parser.parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,15}", args.instance) is None:
        parser.error("接入器 ID 格式有误")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
