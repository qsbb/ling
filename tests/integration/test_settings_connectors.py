"""接入器设置页的只读诊断端点（``GET /v1/settings/connectors``）。

三条硬要求，逐条锁在这里：

1. **三态分开**：未配置 / 已配置未连接 / 已连接。两个渠道各自都要能分辨——
   混成一句"不可用"，用户就分不清是漏填变量还是连接坏了。
2. **脱敏**：设了 ``LING_QQBOT_CLIENT_SECRET`` 等密钥后，响应里**任何位置**
   都不出现那个值。不是"某个字段是 null 就行"——是遍历整份 JSON 逐个字符串比。
3. **不冒充**：只读进程里已经存在的对象，不发网络探测；``configured`` 与总览
   同源（QQ 官方机器人复用 ``QQBotConfig.configured``）。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from ling.settings.connectors import CONNECTOR_REASON_TEXT

pytestmark = pytest.mark.w0_web

ENDPOINT = "/v1/settings/connectors"

# 故意用一眼能认出来的值：出现在响应里就一定能被抓到。
QQBOT_APP_ID = "102000001"
QQBOT_CLIENT_SECRET = "qqbot-secret-SHOULD-NEVER-APPEAR-8f3a"
QQBOT_BOT_SECRET = "qqbot-botsecret-SHOULD-NEVER-APPEAR-9b71"
ONEBOT_WS_TOKEN = "onebot-ws-token-SHOULD-NEVER-APPEAR-c204"
ONEBOT_WS_URL = "ws://192.0.2.10:3001?access_token=onebot-url-token-SHOULD-NEVER-APPEAR"
ONEBOT_SECRET = "onebot-hmac-SHOULD-NEVER-APPEAR-71d5"

SECRETS = (
    QQBOT_CLIENT_SECRET,
    QQBOT_BOT_SECRET,
    ONEBOT_WS_TOKEN,
    "onebot-url-token-SHOULD-NEVER-APPEAR",
    ONEBOT_SECRET,
)


class FakeOneBotWsServer:
    """只保留端点会读的两个计数（见 ``OneBotWsServer.status()``）。"""

    def __init__(self, *, clients: int = 0, received: int = 0) -> None:
        self.clients = clients
        self.received = received


class FakeOneBotWsClient:
    def __init__(self, *, connected: bool, received: int = 0) -> None:
        self.connected = connected
        self.received = received


class FakeQQBotGateway:
    def __init__(self, *, connected: bool, received: int = 0) -> None:
        self.connected = connected
        self.received = received


@pytest.fixture(autouse=True)
def _clean_onebot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``onebot_config()`` 直接读进程环境变量（那是它的单一来源）。

    开发机 shell 里可能有 ``LING_ONEBOT_*``（dev_env.sh 会导出），漏进测试会让
    "未配置"用例静默变成"已配置"。这里显式清空，不依赖运行者的环境。
    """
    for name in (
        "LING_ONEBOT_API_BASE",
        "LING_ONEBOT_ACCESS_TOKEN",
        "LING_ONEBOT_SECRET",
        "LING_ONEBOT_SELF_ID",
        "LING_ONEBOT_WS_HOST",
        "LING_ONEBOT_MEDIA_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ 工具


def _use_settings(env: Any, **changes: Any) -> None:
    """换掉这条请求会读到的 Settings。

    ``Settings`` 是 frozen dataclass，用 ``replace`` 造一份新的挂回 ``app.state``——
    路由与 ``qqbot_config()`` 读的都是 ``app.state.settings``，所以改这里就够。
    """
    env.app.state.settings = dataclasses.replace(env.app.state.settings, **changes)


async def _fetch(env: Any) -> dict:
    response = await env.client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    return response.json()


def _channel(body: dict, channel_id: str) -> dict:
    for item in body["connectors"]:
        if item["id"] == channel_id:
            return item
    raise AssertionError(f"响应里没有渠道 {channel_id}：{[c['id'] for c in body['connectors']]}")


def _field(channel: dict, name: str) -> dict:
    for item in channel["fields"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"{channel['id']} 没有字段 {name}")


def _strings(value: Any) -> list[str]:
    """把整份 JSON 里所有字符串（含键名）摊平，供逐条比对。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


# ------------------------------------------------------------------ 形状


async def test_shape_and_reason_codes(logged_in) -> None:
    """两个渠道都在，字段齐全，状态码都能在码表里查到中文。"""
    body = await _fetch(logged_in)

    # v2：新增 generation / activation / 可编辑字段与白名单组（设计 §6）。
    assert body["schema_version"] == "v2"
    assert body["as_of"]
    assert [c["id"] for c in body["connectors"]] == ["onebot", "qqbot"]
    assert body["access"]["id"] == "access"
    assert body["access"]["generation"] == 0
    assert body["can_edit"] is True, "owner 应当可以编辑"

    for channel in body["connectors"]:
        assert set(channel) >= {
            "id",
            "label",
            "configured",
            "enabled",
            "status",
            "status_reason",
            "status_message",
            "guidance",
            "editable",
            "storage",
            "fields",
            "runtime",
        }
        # 可配置之后 editable 对 owner 为 True；非 owner 见 test_member_*。
        assert channel["editable"] is True
        # 值落 DB（容器只读根目录，写不了 .env）
        assert channel["storage"] == "db"
        assert channel["generation"] == 0
        assert set(channel["activation"]) >= {
            "state",
            "active_generation",
            "candidate_generation",
            "ack_count",
            "expected_count",
        }
        # 状态码必须在码表里，文案来自码表（前端不另维护一份翻译）。
        assert channel["status_reason"] in CONNECTOR_REASON_TEXT
        assert channel["status_message"] == CONNECTOR_REASON_TEXT[channel["status_reason"]]
        # 每个渠道都要有一句"怎么改"，且点名真实的环境变量。
        assert channel["guidance"]
        assert "LING_" in channel["guidance"]
        for field in channel["fields"]:
            # `key` 是 v2 契约名；`name`/`set` 是兼容别名（旧前端读它们）。
            assert set(field) >= {
                "key",
                "name",
                "label",
                "kind",
                "set",
                "value",
                "source",
                "configured",
                "preview",
                "editable",
            }
            assert field["key"] == field["name"]
            assert field["key"].startswith("LING_")
            assert field["source"] in {"db", "env", "default"}
            assert isinstance(field["set"], bool)
            assert field["editable"] is True


async def test_requires_auth(web_env) -> None:
    """部署指纹（开了哪个渠道、监听哪个端口）不该匿名可读。"""
    response = await web_env.client.get(ENDPOINT)
    assert response.status_code == 401


# ------------------------------------------------------------------ OneBot 三态


async def test_onebot_not_configured(logged_in) -> None:
    """什么都没配：未配置，而不是"连接失败"。"""
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is False
    assert channel["enabled"] is False
    assert channel["status"] == "not_configured"
    assert channel["status_reason"] == "onebot_no_inbound_path"
    assert "没有任何入站通道" in channel["status_message"]
    # 未配置时不许出现"失败/不通"这类词：它会把人引去查一条从未建立的连接。
    assert "失败" not in channel["status_message"]
    for name in (
        "LING_ONEBOT_WS_PORT",
        "LING_ONEBOT_WS_TOKEN",
        "LING_ONEBOT_WS_URL",
        "LING_ONEBOT_SECRET",
    ):
        assert _field(channel, name)["set"] is False, name
    assert "LING_ONEBOT_WS_PORT" in channel["guidance"]


async def test_onebot_port_without_token_is_its_own_reason(logged_in) -> None:
    """只配了端口没配 token：和服务端"拒绝启动"对应，不能说成同一件事。"""
    _use_settings(logged_in, onebot_ws_port=6799, onebot_ws_token=None)
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is False
    # 端口配了（想启用），但起不来：enabled 与 configured 在这里必须分开。
    assert channel["enabled"] is True
    assert channel["status"] == "not_configured"
    assert channel["status_reason"] == "onebot_ws_token_missing"
    assert _field(channel, "LING_ONEBOT_WS_PORT")["set"] is True
    assert _field(channel, "LING_ONEBOT_WS_TOKEN")["set"] is False
    assert "LING_ONEBOT_WS_TOKEN" in channel["guidance"]


async def test_onebot_configured_but_server_waiting(logged_in) -> None:
    """已配置、服务端在监听、还没有客户端：已配置未连接。"""
    _use_settings(logged_in, onebot_ws_port=6799, onebot_ws_token=ONEBOT_WS_TOKEN)
    logged_in.app.state.onebot_ws_server = FakeOneBotWsServer(clients=0, received=0)
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is True
    assert channel["enabled"] is True
    assert channel["status"] == "configured_disconnected"
    assert channel["status_reason"] == "onebot_ws_server_waiting"
    assert channel["runtime"] == {"connections": 0, "received": 0}


async def test_onebot_configured_but_not_started(logged_in) -> None:
    """配置齐了、进程里却没有服务端：这就是"改完没重启"，必须说得出来。"""
    _use_settings(logged_in, onebot_ws_port=6799, onebot_ws_token=ONEBOT_WS_TOKEN)
    # 不挂 onebot_ws_server：模拟启动时还没有这段配置
    logged_in.app.state.onebot_ws_server = None
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is True
    assert channel["status"] == "configured_disconnected"
    assert channel["status_reason"] == "onebot_ws_server_not_started"
    assert "没重启" in channel["guidance"]
    assert channel["runtime"] == {"connections": None, "received": None}


async def test_onebot_connected_via_server(logged_in) -> None:
    """NapCat 连进来了：已连接。"""
    _use_settings(logged_in, onebot_ws_port=6799, onebot_ws_token=ONEBOT_WS_TOKEN)
    logged_in.app.state.onebot_ws_server = FakeOneBotWsServer(clients=1, received=132)
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["status"] == "connected"
    assert channel["status_reason"] == "onebot_ws_server_client"
    assert channel["runtime"] == {"connections": 1, "received": 132}
    assert "没有需要改的" in channel["guidance"]


async def test_onebot_connected_via_client(logged_in) -> None:
    """另一个方向（ling 连出去）同样要能报"已连接"。"""
    _use_settings(logged_in, onebot_ws_url=ONEBOT_WS_URL)
    logged_in.app.state.onebot_ws = FakeOneBotWsClient(connected=True, received=7)
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is True
    assert channel["status"] == "connected"
    assert channel["status_reason"] == "onebot_ws_client_connected"
    assert channel["runtime"] == {"connections": 1, "received": 7}


async def test_onebot_http_only_is_not_reported_as_connected(logged_in, monkeypatch) -> None:
    """只配 HTTP 上报时没有持久连接可观测，不能说"已连接"。"""
    monkeypatch.setenv("LING_ONEBOT_SECRET", ONEBOT_SECRET)
    channel = _channel(await _fetch(logged_in), "onebot")

    assert channel["configured"] is True
    assert channel["status"] == "configured_disconnected"
    assert channel["status_reason"] == "onebot_http_only"
    assert channel["runtime"] == {"connections": None, "received": None}


# ------------------------------------------------------------------ QQBot 三态


async def test_qqbot_not_configured(logged_in) -> None:
    channel = _channel(await _fetch(logged_in), "qqbot")

    assert channel["configured"] is False
    assert channel["status"] == "not_configured"
    assert channel["status_reason"] == "qqbot_credentials_missing"
    assert _field(channel, "LING_QQBOT_APP_ID")["set"] is False
    assert _field(channel, "LING_QQBOT_CLIENT_SECRET")["set"] is False
    # 指引必须点出真实变量名，不是"去配置一下"。
    assert "LING_QQBOT_APP_ID" in channel["guidance"]
    assert "LING_QQBOT_CLIENT_SECRET" in channel["guidance"]


async def test_qqbot_configured_but_channel_disabled(logged_in) -> None:
    """凭据齐了但开关关着：这是"关了"，不是"未连接"。"""
    _use_settings(
        logged_in,
        qqbot_enabled=False,
        qqbot_app_id=QQBOT_APP_ID,
        qqbot_client_secret=QQBOT_CLIENT_SECRET,
    )
    channel = _channel(await _fetch(logged_in), "qqbot")

    assert channel["configured"] is True
    assert channel["enabled"] is False
    assert channel["status"] == "disabled"
    assert channel["status_reason"] == "qqbot_channel_disabled"
    # 开关的 `value` 是规范化后的布尔（v2）；人话状态在 status_message。
    assert _field(channel, "LING_QQBOT_ENABLED")["value"] is False
    assert "LING_QQBOT_ENABLED" in channel["guidance"]


async def test_qqbot_enabled_but_gateway_missing(logged_in) -> None:
    _use_settings(
        logged_in,
        qqbot_enabled=True,
        qqbot_app_id=QQBOT_APP_ID,
        qqbot_client_secret=QQBOT_CLIENT_SECRET,
    )
    logged_in.app.state.qqbot_gateway = None
    channel = _channel(await _fetch(logged_in), "qqbot")

    assert channel["status"] == "configured_disconnected"
    assert channel["status_reason"] == "qqbot_gateway_not_started"


async def test_qqbot_configured_but_disconnected(logged_in) -> None:
    _use_settings(
        logged_in,
        qqbot_enabled=True,
        qqbot_app_id=QQBOT_APP_ID,
        qqbot_client_secret=QQBOT_CLIENT_SECRET,
    )
    logged_in.app.state.qqbot_gateway = FakeQQBotGateway(connected=False)
    channel = _channel(await _fetch(logged_in), "qqbot")

    assert channel["configured"] is True
    assert channel["status"] == "configured_disconnected"
    assert channel["status_reason"] == "qqbot_gateway_disconnected"
    assert channel["runtime"] == {"connections": 0, "received": 0}


async def test_qqbot_connected(logged_in) -> None:
    _use_settings(
        logged_in,
        qqbot_enabled=True,
        qqbot_app_id=QQBOT_APP_ID,
        qqbot_client_secret=QQBOT_CLIENT_SECRET,
    )
    logged_in.app.state.qqbot_gateway = FakeQQBotGateway(connected=True, received=18)
    channel = _channel(await _fetch(logged_in), "qqbot")

    assert channel["status"] == "connected"
    assert channel["status_reason"] == "qqbot_gateway_connected"
    assert channel["runtime"] == {"connections": 1, "received": 18}


# ------------------------------------------------------------------ 脱敏


async def test_secrets_never_appear_anywhere(logged_in, monkeypatch) -> None:
    """设置所有密钥后，响应里**任何位置**都不能出现它们的值。

    比"某字段是 null"强的地方：这里把整份 JSON 摊平成字符串列表逐个比对，
    所以新增一个字段、日志串、错误串、把 secret 拼进 guidance 的人都会红。
    """
    _use_settings(
        logged_in,
        qqbot_enabled=True,
        qqbot_app_id=QQBOT_APP_ID,
        qqbot_client_secret=QQBOT_CLIENT_SECRET,
        qqbot_bot_secret=QQBOT_BOT_SECRET,
        onebot_ws_port=6799,
        onebot_ws_token=ONEBOT_WS_TOKEN,
        onebot_ws_url=ONEBOT_WS_URL,
    )
    monkeypatch.setenv("LING_ONEBOT_SECRET", ONEBOT_SECRET)
    logged_in.app.state.onebot_ws_server = FakeOneBotWsServer(clients=1, received=1)
    logged_in.app.state.qqbot_gateway = FakeQQBotGateway(connected=True)

    response = await logged_in.client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    body = response.json()
    haystack = _strings(body)

    for secret in SECRETS:
        assert not any(secret in text for text in haystack), f"响应里出现了密钥：{secret}"

    # 值不留，但"设了没有"必须如实。
    qqbot = _channel(body, "qqbot")
    for name in ("LING_QQBOT_APP_ID", "LING_QQBOT_CLIENT_SECRET", "LING_QQBOT_BOT_SECRET"):
        field = _field(qqbot, name)
        assert field["set"] is True, name
        assert field["value"] is None, f"{name} 回了值（只该回 set）"
        assert field["kind"] == "secret"
    onebot = _channel(body, "onebot")
    for name in ("LING_ONEBOT_WS_TOKEN", "LING_ONEBOT_WS_URL", "LING_ONEBOT_SECRET"):
        field = _field(onebot, name)
        assert field["set"] is True, name
        assert field["value"] is None, name


async def test_reason_text_covers_every_status_used(logged_in) -> None:
    """凡是端点会吐出来的状态码，码表里都得有中文（不能退化成"状态未知"）。"""
    seen: set[str] = set()

    cases: list[dict[str, Any]] = [
        {},
        {"onebot_ws_port": 6799},
        {"onebot_ws_port": 6799, "onebot_ws_token": ONEBOT_WS_TOKEN},
        {"onebot_ws_url": ONEBOT_WS_URL},
        {"qqbot_enabled": True, "qqbot_app_id": QQBOT_APP_ID,
         "qqbot_client_secret": QQBOT_CLIENT_SECRET},
        {"qqbot_enabled": False, "qqbot_app_id": QQBOT_APP_ID,
         "qqbot_client_secret": QQBOT_CLIENT_SECRET},
    ]
    for changes in cases:
        _use_settings(logged_in, **changes)
        body = await _fetch(logged_in)
        for channel in body["connectors"]:
            seen.add(channel["status_reason"])
            assert channel["status_message"] != CONNECTOR_REASON_TEXT["unknown"]

    # 兜底码本身不该被任何分支用到；能到这里说明判定覆盖完整。
    assert "unknown" not in seen
