"""渠道构造：连接器与配置的**单一来源**。

为什么单独抽出来：API 路由与 worker 都要构造连接器。
两处各写一遍环境变量解析，迟早会漂（一边读了新变量、另一边没读），
表现为"网页里能收到消息，主动提醒却发不出去"这类难查的问题。

现在有两条 QQ 接入路径：

- ``onebot``：NapCat 等 OneBot 实现，直接按 QQ 号发
- ``qqbot``：QQ 官方机器人（开放平台），openid 身份 + ``msg_id`` 被动回复

两者的配置项、能力声明与"能不能主动推送"的判定完全不同，但都必须经过
这里，不能让调用方自己去读环境变量。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ling.ports.channel import ChannelConnector


def onebot_config():  # noqa: ANN201
    """从环境变量读 OneBot 配置。"""
    from connectors.onebot.settings import OneBotConfig

    return OneBotConfig(
        api_base=os.environ.get("LING_ONEBOT_API_BASE", "http://127.0.0.1:5700"),
        access_token=os.environ.get("LING_ONEBOT_ACCESS_TOKEN") or None,
        secret=os.environ.get("LING_ONEBOT_SECRET") or None,
        self_id=os.environ.get("LING_ONEBOT_SELF_ID") or None,
        instance_id=os.environ.get("LING_ONEBOT_INSTANCE_ID", "onebot-default"),
        supports_group=os.environ.get("LING_ONEBOT_SUPPORTS_GROUP", "1") == "1",
        supports_proactive=os.environ.get("LING_ONEBOT_PROACTIVE", "0") == "1",
    )


def build_onebot_connector() -> ChannelConnector:
    from connectors.onebot.connector import OneBotV11Connector

    return OneBotV11Connector(onebot_config())


def qqbot_config(settings: object | None = None):  # noqa: ANN201
    """从 Settings 读 QQ 官方机器人配置。

    与其他渠道不同，这里**从 Settings 读而不是直接读 os.environ**：
    qqbot 有 10 多个相关变量（沙箱/基址/token 端点/签名密钥/配额），
    散在两处解析必然漂；Settings 已经是环境变量的唯一解析点。
    ``settings`` 参数让测试/多实例场景能显式传入，而不是依赖进程全局。
    """
    from connectors.qqbot.settings import DEFAULT_TOKEN_URL, QQBotConfig

    if settings is None:
        from ling.config import get_settings

        settings = get_settings()
    get = lambda name: getattr(settings, name)  # noqa: E731 - 局部取值器，读起来更短

    return QQBotConfig(
        app_id=get("qqbot_app_id") or "",
        client_secret=get("qqbot_client_secret") or "",
        instance_id=get("qqbot_instance_id") or "qqbot-default",
        sandbox=bool(get("qqbot_sandbox")),
        api_base=get("qqbot_api_base") or None,
        token_url=get("qqbot_token_url") or DEFAULT_TOKEN_URL,
        bot_secret=get("qqbot_bot_secret") or None,
        webhook_public_key_hex=get("qqbot_webhook_public_key") or None,
        allow_unsigned_validation=bool(get("qqbot_allow_unsigned_validation")),
        supports_proactive=bool(get("qqbot_proactive")),
        supports_group=bool(get("qqbot_supports_group")),
    )


def build_qqbot_connector(settings: object | None = None) -> ChannelConnector:
    from connectors.qqbot.connector import QQBotConnector

    return QQBotConnector(qqbot_config(settings))


def qqbot_enabled(settings: object | None = None) -> bool:
    """渠道开关。未启用时不注册 Webhook 路由：**入口不存在**比"存在但拒绝"更难误配。"""
    if settings is None:
        from ling.config import get_settings

        settings = get_settings()
    return bool(getattr(settings, "qqbot_enabled", False))


def proactive_connector() -> ChannelConnector | None:
    """返回**能主动推送**的连接器；没有就返回 None。

    判定看能力自检而不是"配了没配"：
    `supports_proactive` 是平台权限探测的结果，配了变量但平台不给权限
    同样发不出去——那种情况下必须如实标记不可达，不能假装已发送。

    OneBot 优先（现有部署走它），其次 QQ 官方机器人——后者默认不允许
    主动推送，只有显式配置 ``LING_QQBOT_PROACTIVE=1`` 才会被选中。
    """
    for build in (build_onebot_connector, build_qqbot_connector):
        try:
            connector = build()
        except Exception:  # noqa: BLE001 - 单个渠道构造失败不该让整条判定失败
            continue
        caps = connector.capabilities()
        if caps.enabled and caps.supports_proactive:
            return connector
    return None


def proactive_connector_status() -> tuple[ChannelConnector | None, str | None]:
    """返回 (连接器, 不可用的**精确原因**)。

    ## 为什么需要原因

    `proactive_connector()` 只回答"能不能"，门控就只能说一句
    「需账号权限探测通过」——用户会去找一个**根本不存在的平台权限**。
    实际原因往往是环境变量没打开（`LING_ONEBOT_PROACTIVE=0`）。

    两种失败必须分开说：
    - 连接器压根没配好 → 去配 OneBot
    - 配好了但没声明支持主动推送 → 去打开那个开关
    """
    # **两个渠道都要如实报**。
    #
    # 原来只查 OneBot：部署实例上 qqbot 明明配好了（只是平台没给主动权限），
    # 界面却只说「OneBot 连接器已禁用」。用户会去配 OneBot，然后照样发不出去，
    # 而真正的原因（官方机器人需要单独申请主动推送权限）一个字都没提到。
    reasons: list[str] = []

    try:
        onebot = build_onebot_connector()
    except Exception as exc:  # noqa: BLE001 - 构造失败按"没配好"处理
        onebot = None
        reasons.append(f"OneBot 连接器不可用（{type(exc).__name__}），先检查连接配置")
    if onebot is not None:
        caps = onebot.capabilities()
        if caps.enabled and caps.supports_proactive:
            return onebot, None
        if not caps.enabled:
            reasons.append("OneBot 连接器已禁用（LING_QQ_ENABLED=0）")
        else:
            reasons.append(
                "OneBot 没有声明支持主动推送（LING_ONEBOT_PROACTIVE=0）："
                "确认该 QQ 号能主动发消息后设为 1"
            )

    qqconn, qqreason = qqbot_connector_status()
    if qqconn is not None:
        return qqconn, None
    if qqreason:
        reasons.append(qqreason)

    return None, "；".join(reasons) if reasons else "没有可用的主动推送渠道"


def qqbot_connector_status() -> tuple[ChannelConnector | None, str | None]:
    """QQ 官方机器人的 (连接器, 不可用原因)。

    与 OneBot 的分开说，因为失败原因完全不同：官方机器人的主动推送
    **本来就默认没有**（要单独申请），把它混进 OneBot 的提示里会让人
    去翻 NapCat 配置。
    """
    try:
        connector = build_qqbot_connector()
    except Exception as exc:  # noqa: BLE001 - 构造失败按"没配好"处理
        return None, f"QQ 官方机器人连接器不可用（{type(exc).__name__}）"
    caps = connector.capabilities()
    if not caps.enabled:
        return (
            None,
            "QQ 官方机器人凭据不全（LING_QQBOT_APP_ID / LING_QQBOT_CLIENT_SECRET）",
        )
    if not caps.supports_proactive:
        return (
            None,
            "QQ 官方机器人默认只支持被动回复：主动推送需在开放平台单独申请，"
            "获得权限后再设 LING_QQBOT_PROACTIVE=1",
        )
    return connector, None


__all__ = [
    "build_onebot_connector",
    "build_qqbot_connector",
    "onebot_config",
    "proactive_connector",
    "proactive_connector_status",
    "qqbot_config",
    "qqbot_connector_status",
    "qqbot_enabled",
]
