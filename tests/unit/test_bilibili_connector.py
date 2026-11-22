from types import SimpleNamespace

import httpx
from connectors.bilibili.__main__ import ROOM_INFO, Forwarder, event_from_message


def test_bilibili_keeps_account_and_display_name_separate():
    danmaku = SimpleNamespace(
        timestamp=1_795_294_801, msg_id="source-id", room_id=100200300,
        open_id="viewer-id", uname="同名观众", msg="聊配乐",
    )
    event = event_from_message("comment", danmaku)
    assert event["sender"] == {"id": "viewer-id", "name": "同名观众"}
    assert event["event_id"] == "source-id" and event["room_id"] == "100200300"


async def test_room_probe_requires_exact_room_and_keeps_credentials_local():
    returned_room = 100200300

    def platform(req):
        assert req.url == httpx.URL(ROOM_INFO, params={"room_id": "100200300"})
        assert "Authorization" not in req.headers
        return httpx.Response(200, json={"code": 0, "data": {"room_id": returned_room, "live_status": 1}})

    async with (
        httpx.AsyncClient(headers={"Authorization": "Bearer demo-source"}) as ling,
        httpx.AsyncClient(transport=httpx.MockTransport(platform)) as bilibili,
    ):
        forwarder = Forwarder(ling, bilibili, "bili-live")
        observed = await forwarder.room_status("100200300")
        assert observed["kind"] == "live_start"
        returned_room = 100200301
        assert await forwarder.room_status("100200300") is None
