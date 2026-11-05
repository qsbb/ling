"""QQ 官方机器人 **WebSocket 网关**的测试。

没有真实凭据、也不连真实平台：`websockets.connect` 与 HTTP 都是假的。
这里能证明的是"协议帧收发与状态机正确"，**不能**证明"已连通真实 QQ"。
"""

from __future__ import annotations

import contextlib
import json

import pytest
from connectors.qqbot.ws_gateway import (
    ACTION_IDENTIFY,
    ACTION_RESUME,
    ACTION_STOP,
    INTENTS_GROUP_AND_C2C,
    GatewayState,
    QQBotGateway,
    action_for_close,
    build_heartbeat,
    build_identify,
    build_resume,
)

pytestmark = pytest.mark.w0_web


# ---------------------------------------------------------------- 纯构造


def test_identify_carries_bot_token_prefix_and_intents() -> None:
    """Identify 的 token 必须是 ``QQBot <access_token>`` 前缀，intents 用群+单聊位。"""
    frame = build_identify("tk", intents=INTENTS_GROUP_AND_C2C, shard=(0, 1))
    assert frame["op"] == 2
    assert frame["d"]["token"] == "QQBot tk"
    assert frame["d"]["intents"] == 1 << 25
    assert frame["d"]["shard"] == [0, 1]


def test_resume_carries_session_and_seq() -> None:
    frame = build_resume("tk", GatewayState(session_id="s-1", last_seq=42))
    assert frame["op"] == 6
    assert frame["d"]["session_id"] == "s-1"
    assert frame["d"]["seq"] == 42


def test_heartbeat_first_uses_null() -> None:
    """首帧心跳 d 必须为 null（官方文档明确写的）。"""
    assert build_heartbeat(None)["d"] is None
    assert build_heartbeat(7)["d"] == 7


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (4008, ACTION_RESUME),
        (4009, ACTION_RESUME),
        (4007, ACTION_IDENTIFY),
        (4006, ACTION_IDENTIFY),
        (4900, ACTION_IDENTIFY),
        (4913, ACTION_IDENTIFY),
        (4914, ACTION_STOP),
        (4915, ACTION_STOP),
        (None, ACTION_IDENTIFY),
        (1006, ACTION_IDENTIFY),
    ],
)
def test_close_code_policy(code: int | None, expected: str) -> None:
    """关闭码决定 Resume / Identify / 停止。

    未知码按 Identify：Resume 依赖 session 仍有效，session 已失效时会立刻再断，
    形成紧密重连循环。Identify 更安全。
    """
    assert action_for_close(code) == expected


# ---------------------------------------------------------------- 帧状态机


async def _gateway() -> QQBotGateway:
    return QQBotGateway(connector=object(), app=object())


async def test_hello_interval_is_stored_not_just_logged() -> None:
    """Hello 给的心跳周期必须**存下来**。

    只打日志不存的话，心跳会一直用兜底值；平台要求的周期更短时会被判超时
    断连，而且日志里看不出任何异常（这是个真实的接线缺口）。
    """
    gw = await _gateway()
    assert gw._hello_interval_ms == 0.0
    await gw.handle_frame(json.dumps({"op": 10, "d": {"heartbeat_interval": 12000}}))
    assert gw._hello_interval_ms == 12000.0


async def test_ready_sets_session_and_seq() -> None:
    gw = await _gateway()
    await gw.handle_frame(
        json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sess-abc"}})
    )
    assert gw.state.session_id == "sess-abc"
    assert gw.state.last_seq == 1
    assert gw.state.can_resume is True


async def test_seq_advances_on_every_frame() -> None:
    """每帧的 s 都要记——重连时靠它补事件，断档会重复或漏发。"""
    gw = await _gateway()
    for seq in (5, 9, 12):
        await gw.handle_frame(json.dumps({"op": 0, "s": seq, "t": "RESUMED", "d": ""}))
    assert gw.state.last_seq == 12


async def test_bad_frame_does_not_raise() -> None:
    """坏帧只丢一条，不能断连接。"""
    gw = await _gateway()
    assert await gw.handle_frame("不是 json") is None
    assert await gw.handle_frame(json.dumps([1, 2])) is None


# ---------------------------------------------------------------- 连接循环


class _FakeWS:
    """假连接：记录发出去的帧，然后立刻按预置关闭码结束。"""

    def __init__(self, sent: list[dict], close_code: int | None) -> None:
        self._sent = sent
        self._close_code = close_code

    async def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))

    def __aiter__(self):  # noqa: ANN204
        return self

    async def __anext__(self):  # noqa: ANN204
        import websockets

        if self._close_code is None:
            raise StopAsyncIteration
        raise websockets.exceptions.ConnectionClosed(None, None)

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_exc):  # noqa: ANN002
        return False


async def test_run_sends_identify_when_no_session(monkeypatch) -> None:  # noqa: ANN001
    """没有会话时发 Identify；且网关地址**来自 /gateway**，不是拼出来的。"""
    import websockets

    sent: list[dict] = []
    urls: list[str] = []

    class _Connector:
        async def api_get(self, path: str):  # noqa: ANN202
            assert path == "/gateway"
            return {"url": "wss://example.invalid/websocket"}

        async def access_token(self) -> str:
            return "tk"

    def _connect(url, **_kw):  # noqa: ANN001, ANN003
        urls.append(url)
        return _FakeWS(sent, None)

    monkeypatch.setattr(websockets, "connect", _connect)
    gw = QQBotGateway(connector=_Connector(), app=object(), reconnect_min=0.01)

    import asyncio

    task = asyncio.create_task(gw.run())
    await asyncio.sleep(0.2)
    gw._stopped = True
    task.cancel()
    with contextlib.suppress(BaseException):
        await task

    assert urls and urls[0] == "wss://example.invalid/websocket", urls
    assert sent and sent[0]["op"] == 2, sent[:1]


async def test_run_resumes_when_session_exists(monkeypatch) -> None:  # noqa: ANN001
    """有会话时先发 Resume（补发漏掉的事件），不是重新 Identify。"""
    import asyncio

    import websockets

    sent: list[dict] = []

    class _Connector:
        async def api_get(self, _path: str):  # noqa: ANN202
            return {"url": "wss://example.invalid/websocket"}

        async def access_token(self) -> str:
            return "tk"

    monkeypatch.setattr(
        websockets, "connect", lambda url, **kw: _FakeWS(sent, None)  # noqa: ANN001, ANN003
    )
    gw = QQBotGateway(connector=_Connector(), app=object(), reconnect_min=0.01)
    gw.state = GatewayState(session_id="sess-x", last_seq=99)

    task = asyncio.create_task(gw.run())
    await asyncio.sleep(0.2)
    gw._stopped = True
    task.cancel()
    with contextlib.suppress(BaseException):
        await task

    assert sent and sent[0]["op"] == 6, sent[:1]
    assert sent[0]["d"]["seq"] == 99


def test_api_base_is_the_documented_unified_host() -> None:
    """OpenAPI 基址必须是官方文档的**统一请求地址**。

    文档「API 调用指南 → 调用方式 → 统一请求地址」写的是
    ``https://api.bot.qq.com``，没有 sandbox 专用域名。
    旧的 ``api.sgroup.qq.com`` 是上一代写法——用错等于全链路连不上，
    而报错只会是 DNS/404，看不出是地址写错了。
    """
    from connectors.qqbot.settings import PROD_API_BASE, QQBotConfig

    assert PROD_API_BASE == "https://api.bot.qq.com"
    # 沙箱/正式**不换域名**：区别在平台侧的机器人状态
    assert QQBotConfig(sandbox=True).base_url == PROD_API_BASE
    assert QQBotConfig(sandbox=False).base_url == PROD_API_BASE
    # 显式覆盖仍然生效（自建代理/平台改域名）
    assert (
        QQBotConfig(api_base="https://proxy.example/").base_url
        == "https://proxy.example"
    )


def test_token_endpoint_is_the_documented_one() -> None:
    """换 token 的地址也要钉住：错了就拿不到凭证，后面全是 401。"""
    from connectors.qqbot.settings import DEFAULT_TOKEN_URL

    assert DEFAULT_TOKEN_URL == "https://api.bot.qq.com/app/getAppAccessToken"
