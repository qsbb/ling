"""接入器（渠道连接器）的只读视图：**凭据齐不齐、连没连上、改哪里**。

## 为什么需要这一页

渠道配置全部来自环境变量（OneBot 的 ``LING_ONEBOT_*``、QQ 官方机器人的
``LING_QQBOT_*``），密钥只存在于宿主部署目录的 ``.env``（0600）。没有这一页时，
"她收不到 QQ 消息"只能靠登宿主机翻文件、或者猜——而猜错的方向代价很高
（去改 NapCat，实际是 api 容器没重启）。

总览的 ``connectivity`` 段只回答"配没配"（一句话、一个布尔），不回答
"缺哪一项、改完要做什么"。两处**不各写一套判定**：``configured`` 一律复用
``capabilities.channels`` 里那份（``qqbot_config().configured`` /
OneBot 的入站路径判定），这里只补"缺哪个变量"和"现在通不通"。

## 为什么只读

做成网页可编辑要同时回答三个问题：值存哪（DB 配置修订？宿主的 ``.env``？）、
密钥怎么加密、改完怎么让进程重读。三个都还没有答案，所以这一轮先让现状
**看得见**；``editable`` 恒为 ``False``，前端不得把它画成表单。

## 三态（加一个"关了"）

``not_configured`` / ``configured_disconnected`` / ``connected`` 必须分开说：
"没配"要去填变量，"配了没通"要去看连接，"通了"什么都不用做。混成一句
"不可用"会让人不知道是漏填还是填错。

``disabled`` 是第四种，只在 QQ 官方机器人上出现：凭据齐了但
``LING_QQBOT_ENABLED=0``。把它说成"未连接"是错的——连接从来没被尝试过，
去查连接只会白费时间。

## 密钥

``fields`` 里只回 ``set: bool``，**绝不含明文**。``ConnectorField`` 对
``kind="secret"`` 的字段连值都不保存（构造时就只传布尔），``to_dict`` 再兜一层，
两道保险都在这里而不是在路由里——路由以后被人改，也不会突然开始回显。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------------ 状态码

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_DISABLED = "disabled"
STATUS_DISCONNECTED = "configured_disconnected"
STATUS_CONNECTED = "connected"

# 「这个状态为什么是这样」的码表。码给程序判断，文案给人读；前端只显示文案，
# 不自己再维护一份翻译（否则后端的判定改了，前端还在说旧理由）。
CONNECTOR_REASON_TEXT: dict[str, str] = {
    # --- OneBot ---
    "onebot_no_inbound_path": (
        "没有任何入站通道：WS 端口、WS 地址、HTTP 上报密钥三项都没配"
    ),
    "onebot_ws_token_missing": (
        "配了 WS 端口但没有 token：服务端会拒绝启动（端口一开，推来的事件"
        "直接进对话链路，没 token 等于谁都能冒充）"
    ),
    "onebot_ws_server_client": "NapCat 已连入 WS 服务端",
    "onebot_ws_client_connected": "ling 已连上 OneBot 的 WS 服务端",
    "onebot_ws_server_not_started": (
        "端口已配置，但进程里没有 WS 服务端（改完 .env 没重启，或启动时失败）"
    ),
    "onebot_ws_client_not_started": (
        "WS 地址已配置，但进程里没有出站连接（改完 .env 没重启？）"
    ),
    "onebot_ws_server_waiting": "WS 服务端在监听，但还没有客户端连入",
    "onebot_ws_client_disconnected": "出站 WS 连接已启动，但当前是断的",
    "onebot_http_only": (
        "只配了 HTTP 上报：这条路径没有持久连接，「收到才算通」在这里看不到"
    ),
    # --- QQ 官方机器人 ---
    "qqbot_credentials_missing": "凭据不全（需要 APP_ID 与 CLIENT_SECRET 两项）",
    "qqbot_channel_disabled": "凭据已配好，但渠道开关是关的（LING_QQBOT_ENABLED=0）",
    "qqbot_gateway_not_started": (
        "已启用且有凭据，但进程里没有网关（改完 .env 没重启？）"
    ),
    "qqbot_gateway_connected": "网关已连上 QQ 开放平台",
    "qqbot_gateway_disconnected": "网关在运行，但当前没连上（看 api 日志里的关闭码）",
    # 兜底：判定分支不应该走到这里，走到了也不能编一个理由。
    "unknown": "状态未知",
}

# 宿主机上"改哪里"的固定说法。生产端的运行期数据在部署目录，不在仓库里
# （见仓库约定：凭据只在宿主 `.env`，0600）。
_ENV_HINT = "宿主部署目录的 `.env`"

# 两个渠道的**必需**凭据（只有这些参与 configured 判定）。
_ONEBOT_REQUIRED = ("LING_ONEBOT_WS_TOKEN",)
_QQBOT_REQUIRED = ("LING_QQBOT_APP_ID", "LING_QQBOT_CLIENT_SECRET")


# ------------------------------------------------------------------ 字段


@dataclass(frozen=True)
class ConnectorField:
    """一项配置在界面上的样子。

    ``kind``：
    - ``secret``：只报"设没设"，**值不进这个对象**（构造参数只有布尔）；
    - ``text``：非密钥，有值时直接展示（端口、地址、公钥、实例标识）；
    - ``switch``：开关，``value`` 是"开"/"关"。

    ``set`` 的含义统一为"这一项有生效值"：secretext 为非空、switch 恒真
    （开关总有状态，值看 ``value``）。前端按 ``kind`` 决定怎么画，
    不要把 ``set`` 当成"用户显式设过"——有默认值的字段（监听地址、
    实例标识）默认就是有值的。
    """

    name: str
    label: str
    kind: str
    set: bool
    value: str | None = None
    source: str = "env"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            # 第二道保险：secret 无论调用方传了什么，输出一律没有值。
            "set": bool(self.set),
            "value": None if self.kind == "secret" else self.value,
            "source": self.source,
        }


def _text(name: str, label: str, value: str | None) -> ConnectorField:
    return ConnectorField(name=name, label=label, kind="text", set=bool(value), value=value)


def _secret(name: str, label: str, configured: bool) -> ConnectorField:
    # 只传布尔：连原始值都不进对象，repr/日志里也就不会漏。
    return ConnectorField(name=name, label=label, kind="secret", set=bool(configured))


def _switch(name: str, label: str, on: bool) -> ConnectorField:
    return ConnectorField(
        name=name, label=label, kind="switch", set=True, value="开" if on else "关"
    )


def _onebot_fields(settings: Any, onebot: Any) -> tuple[ConnectorField, ...]:
    return (
        _text(
            "LING_ONEBOT_WS_PORT",
            "WS 服务端端口（NapCat 连进来；留空=不启用）",
            str(settings.onebot_ws_port) if settings.onebot_ws_port else None,
        ),
        _secret(
            "LING_ONEBOT_WS_TOKEN",
            "WS 服务端令牌（与 NapCat 侧 wsClient 的 token 逐字符一致）",
            bool(settings.onebot_ws_token),
        ),
        # 反向 WS 地址可能内嵌 `?access_token=...`，所以按密钥处理、不回显。
        _secret(
            "LING_ONEBOT_WS_URL",
            "出站 WS 地址（ling 主动连 NapCat；可能内嵌令牌，不显示）",
            bool(settings.onebot_ws_url),
        ),
        _secret(
            "LING_ONEBOT_SECRET",
            "HTTP 上报签名密钥（不用 HTTP 上报可留空）",
            bool(onebot.secret),
        ),
        _text(
            "LING_ONEBOT_WS_HOST",
            "WS 服务端监听地址（默认 127.0.0.1，容器里要对外须改 0.0.0.0）",
            onebot.ws_host,
        ),
        _text(
            "LING_ONEBOT_API_BASE",
            "OneBot HTTP API 地址（发消息用；NapCat 不在本机就必须改）",
            onebot.api_base,
        ),
    )


def _qqbot_fields(settings: Any) -> tuple[ConnectorField, ...]:
    return (
        _switch(
            "LING_QQBOT_ENABLED",
            "渠道开关（关着就不启动网关）",
            bool(settings.qqbot_enabled),
        ),
        # AppID 不是密钥，但它是机器人身份：连接器连日志里都只打指纹
        # （见 connectors/qqbot/connector.py::health），这里同样不回显。
        _secret("LING_QQBOT_APP_ID", "AppID（开放平台「开发设置」）", bool(settings.qqbot_app_id)),
        _secret(
            "LING_QQBOT_CLIENT_SECRET",
            "AppSecret（换 access_token 用）",
            bool(settings.qqbot_client_secret),
        ),
        _secret(
            "LING_QQBOT_BOT_SECRET",
            "Webhook 签名密钥 BotSecret（只用网关时可留空）",
            bool(settings.qqbot_bot_secret),
        ),
        _text(
            "LING_QQBOT_WEBHOOK_PUBLIC_KEY",
            "Ed25519 公钥 hex（直接给公钥时优先用它验签）",
            settings.qqbot_webhook_public_key,
        ),
        _switch(
            "LING_QQBOT_SANDBOX",
            "沙箱环境（开着时只能和沙箱名单里的账号对话）",
            bool(settings.qqbot_sandbox),
        ),
        _text(
            "LING_QQBOT_INSTANCE_ID",
            "实例标识（账号绑定按它隔离）",
            getattr(settings, "qqbot_instance_id", None) or None,
        ),
        _text(
            "LING_QQBOT_API_BASE",
            "API 基址（留空按沙箱/正式自动选）",
            settings.qqbot_api_base,
        ),
    )


# ------------------------------------------------------------------ 指引


def _missing(fields: tuple[ConnectorField, ...], names: tuple[str, ...]) -> list[str]:
    return [f.name for f in fields if f.name in names and not f.set]


def _join_names(names: list[str]) -> str:
    return " 与 ".join(f"`{name}`" for name in names)


def _onebot_guidance(
    status: str, reason: str, fields: tuple[ConnectorField, ...]
) -> str:
    if status == STATUS_CONNECTED:
        return "已连接，没有需要改的。"
    if reason == "onebot_ws_token_missing":
        return (
            f"在{_ENV_HINT} 里补上 {_join_names(_missing(fields, _ONEBOT_REQUIRED))}"
            "（与 NapCat 侧 wsClient 的 token 逐字符一致）：没有 token 时 WS 服务端"
            "拒绝启动。改完重启 api 服务。"
        )
    if reason == "onebot_no_inbound_path":
        return (
            f"在{_ENV_HINT} 里任选一条入站路径：`LING_ONEBOT_WS_PORT` + "
            "`LING_ONEBOT_WS_TOKEN`（NapCat 连进来，推荐）、`LING_ONEBOT_WS_URL`"
            "（ling 连出去）、或 `LING_ONEBOT_SECRET`（HTTP 上报）。"
            "改完重启 api 服务。"
        )
    if reason == "onebot_ws_server_not_started":
        return (
            "端口已配但进程里没有 WS 服务端：多半是改完 `.env` 没重启 api 服务；"
            "重启后仍然没有，就看启动日志——端口被占用、缺 token 都会让它起不来。"
        )
    if reason == "onebot_ws_server_waiting":
        return (
            "服务端已在监听，等 NapCat 连入：在 NapCat 侧把反向 WS 客户端指向 "
            "`ws://<ling 地址>:<LING_ONEBOT_WS_PORT>` 并填同一个 token。"
            "容器部署还要发布该端口（见 `deploy/compose.ws.example.yaml`）。"
        )
    if reason == "onebot_ws_client_not_started":
        return (
            "WS 地址已配但进程里没有出站连接：多半是改完 `.env` 没重启 api 服务。"
        )
    if reason == "onebot_ws_client_disconnected":
        return (
            "出站连接已启动但当前是断的：看 api 日志里的 OneBot WS 重连记录——"
            "地址写错、网络不通、NapCat 没在跑都会停在这里。"
        )
    if reason == "onebot_http_only":
        return (
            "只配了 HTTP 上报：它没有持久连接，收不到上报就是断的。在 NapCat 侧"
            "确认 httpClients 指向 `/v1/onebot/events`，且 token 与 "
            "`LING_ONEBOT_SECRET` 逐字符一致。"
        )
    return f"在{_ENV_HINT} 里检查 `LING_ONEBOT_*`，改完重启 api 服务。"


def _qqbot_guidance(status: str, reason: str, fields: tuple[ConnectorField, ...]) -> str:
    if status == STATUS_CONNECTED:
        return "已连接，没有需要改的。"
    if reason == "qqbot_credentials_missing":
        missing = _missing(fields, _QQBOT_REQUIRED)
        names = _join_names(missing) if missing else _join_names(list(_QQBOT_REQUIRED))
        return (
            f"在{_ENV_HINT} 里设置 {names}（开放平台「开发设置」里的 AppID / AppSecret），"
            "并把 `LING_QQBOT_ENABLED` 设为 `1`。改完重启 api 服务。"
        )
    if reason == "qqbot_channel_disabled":
        return (
            f"凭据已就位，只差开关：在{_ENV_HINT} 里把 `LING_QQBOT_ENABLED` 设为 `1`，"
            "然后重启 api 服务。"
        )
    if reason == "qqbot_gateway_not_started":
        return (
            "已启用且有凭据，但进程里没有网关：多半是改完 `.env` 没重启 api 服务。"
        )
    if reason == "qqbot_gateway_disconnected":
        return (
            "网关在运行但当前没连上：看 api 日志里的网关关闭码——凭据失效、"
            "沙箱/正式环境不匹配都会断在这里。"
        )
    return f"在{_ENV_HINT} 里检查 `LING_QQBOT_*`，改完重启 api 服务。"


# ------------------------------------------------------------------ 运行时


def _runtime(connections: int | None, received: int | None) -> dict[str, int | None]:
    """进程里能直接看到的事实，不探测、不外呼。

    ``received`` 是**帧/事件计数**，不是"消息条数"：OneBot 的心跳也计在内。
    "已连接但计数一直是 0"本身就是有用的结论（连上了但对面没在推）。
    """
    return {"connections": connections, "received": received}


def _onebot_runtime(ws_server: Any, ws_client: Any) -> dict[str, int | None]:
    # WS 服务端优先：它是推荐方向，且连接数就是 NapCat 的个数。
    if ws_server is not None:
        return _runtime(
            int(getattr(ws_server, "clients", 0) or 0),
            int(getattr(ws_server, "received", 0) or 0),
        )
    if ws_client is not None:
        return _runtime(
            1 if getattr(ws_client, "connected", False) else 0,
            int(getattr(ws_client, "received", 0) or 0),
        )
    # 只配了 HTTP 上报：没有可观察的连接对象。
    return _runtime(None, None)


def _qqbot_runtime(gateway: Any) -> dict[str, int | None]:
    if gateway is None:
        return _runtime(None, None)
    return _runtime(
        1 if getattr(gateway, "connected", False) else 0,
        int(getattr(gateway, "received", 0) or 0),
    )


# ------------------------------------------------------------------ 视图


def _onebot_view(settings: Any, onebot: Any, app_state: Any) -> dict[str, Any]:
    """OneBot 的只读视图。

    OneBot **没有独立的渠道开关**（``/v1/onebot/events`` 常驻注册，配了 WS 端口/
    地址就在启动时起对应组件），所以 ``enabled`` 报的是"有没有配入站路径"——
    这是代码里唯一存在的"打开/关闭"，不另外发明一个。
    """
    ws_port = settings.onebot_ws_port
    ws_token = bool(settings.onebot_ws_token)
    ws_url = settings.onebot_ws_url
    http_secret = bool(onebot.secret)

    # 三种入站模式各自"能不能起来"：WS 服务端要端口 + token（fail-closed），
    # WS 客户端只要有地址，HTTP 上报只要密钥。
    ws_server_ready = bool(ws_port and ws_token)
    ws_client_ready = bool(ws_url)
    http_ready = http_secret
    configured = ws_server_ready or ws_client_ready or http_ready
    enabled = bool(ws_port or ws_url or http_secret)

    ws_server = getattr(app_state, "onebot_ws_server", None) if app_state is not None else None
    ws_client = getattr(app_state, "onebot_ws", None) if app_state is not None else None

    if not configured:
        # 只配了端口没配 token 与"完全没配"要分开：前者是差一项，后者是没开始。
        reason = (
            "onebot_ws_token_missing" if (ws_port and not ws_token) else "onebot_no_inbound_path"
        )
        status = STATUS_NOT_CONFIGURED
    elif int(getattr(ws_server, "clients", 0) or 0) > 0:
        status, reason = STATUS_CONNECTED, "onebot_ws_server_client"
    elif bool(getattr(ws_client, "connected", False)):
        status, reason = STATUS_CONNECTED, "onebot_ws_client_connected"
    elif ws_server_ready and ws_server is None:
        status, reason = STATUS_DISCONNECTED, "onebot_ws_server_not_started"
    elif ws_client_ready and ws_client is None:
        status, reason = STATUS_DISCONNECTED, "onebot_ws_client_not_started"
    elif ws_server_ready:
        status, reason = STATUS_DISCONNECTED, "onebot_ws_server_waiting"
    elif ws_client_ready:
        status, reason = STATUS_DISCONNECTED, "onebot_ws_client_disconnected"
    else:
        status, reason = STATUS_DISCONNECTED, "onebot_http_only"

    fields = _onebot_fields(settings, onebot)
    return {
        "id": "onebot",
        "label": "OneBot（NapCat / Lagrange / go-cqhttp）",
        "configured": configured,
        "enabled": enabled,
        "status": status,
        "status_reason": reason,
        "status_message": CONNECTOR_REASON_TEXT.get(reason, CONNECTOR_REASON_TEXT["unknown"]),
        "guidance": _onebot_guidance(status, reason, fields),
        # 只读：值在宿主 `.env` 里，本页不给编辑入口。
        "editable": False,
        "storage": "env",
        "fields": [f.to_dict() for f in fields],
        "runtime": _onebot_runtime(ws_server, ws_client),
    }


def _qqbot_view(settings: Any, cfg: Any, app_state: Any) -> dict[str, Any]:
    """QQ 官方机器人的只读视图。

    ``configured`` **直接复用** ``QQBotConfig.configured``（凭据齐不齐），不在这里
    再判一次：两处判定迟早漂，而"配好了却显示未配置"正是总览那次踩过的坑。
    """
    configured = bool(cfg.configured)
    enabled = bool(settings.qqbot_enabled)
    gateway = getattr(app_state, "qqbot_gateway", None) if app_state is not None else None

    if not configured:
        status, reason = STATUS_NOT_CONFIGURED, "qqbot_credentials_missing"
    elif not enabled:
        # 开关关着时连接根本没被尝试过：不能报"未连接"（那会让人去查连接）。
        status, reason = STATUS_DISABLED, "qqbot_channel_disabled"
    elif gateway is None:
        status, reason = STATUS_DISCONNECTED, "qqbot_gateway_not_started"
    elif bool(getattr(gateway, "connected", False)):
        status, reason = STATUS_CONNECTED, "qqbot_gateway_connected"
    else:
        status, reason = STATUS_DISCONNECTED, "qqbot_gateway_disconnected"

    fields = _qqbot_fields(settings)
    return {
        "id": "qqbot",
        "label": "QQ 官方机器人（QQ 开放平台）",
        "configured": configured,
        "enabled": enabled,
        "status": status,
        "status_reason": reason,
        "status_message": CONNECTOR_REASON_TEXT.get(reason, CONNECTOR_REASON_TEXT["unknown"]),
        "guidance": _qqbot_guidance(status, reason, fields),
        "editable": False,
        "storage": "env",
        "fields": [f.to_dict() for f in fields],
        "runtime": _qqbot_runtime(gateway),
    }


def connector_views(settings: Any, app_state: Any = None) -> list[dict[str, Any]]:
    """两个渠道的只读视图。

    ``app_state`` 是 FastAPI 的 ``app.state``：连接状态只能从里面已经构造好的
    运行时对象读（``onebot_ws_server`` / ``onebot_ws`` / ``qqbot_gateway``），
    这里**不发任何网络探测**——设置页会被反复打开，不能拿它当探活工具。
    对象不存在就是不存在（说明没启动），不猜。
    """
    from ling.capabilities.channels import onebot_config, qqbot_config

    onebot = onebot_config()
    cfg = qqbot_config(settings)
    return [_onebot_view(settings, onebot, app_state), _qqbot_view(settings, cfg, app_state)]


__all__ = [
    "CONNECTOR_REASON_TEXT",
    "ConnectorField",
    "STATUS_CONNECTED",
    "STATUS_DISABLED",
    "STATUS_DISCONNECTED",
    "STATUS_NOT_CONFIGURED",
    "connector_views",
]
