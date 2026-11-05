"""QQ 官方机器人（QQ 开放平台 / QQ Bot）连接配置。

与 OneBot 的差别决定了这里的字段：官方接口靠 **AppID + ClientSecret 换
access_token**（约 7200 秒），消息用 ``msg_id`` 做被动回复，凭据与窗口
语义都不在客户端手里——所以「能发什么」必须由配置如实声明，而不是假设。
"""

from __future__ import annotations

from dataclasses import dataclass

# OpenAPI 统一请求地址。
#
# **已核实**（官方 api-v2「API 调用指南 → 调用方式 → 统一请求地址」）：
#   https://api.bot.qq.com
# 文档明确说"统一请求地址"，**没有** sandbox 专用域名；旧的
# ``api.sgroup.qq.com`` / ``sandbox.api.sgroup.qq.com`` 是上一代文档的写法，
# 用它会连不上。沙箱/正式的区别由**平台侧机器人状态**决定，不是换域名。
PROD_API_BASE = "https://api.bot.qq.com"
# 保留这个名字只为兼容既有配置读取；v2 下与正式同址。
SANDBOX_API_BASE = PROD_API_BASE
# 换取 access_token 的端点。官方新旧文档给过两个地址：
#   - https://bots.qq.com/app/getAppAccessToken（当前文档）
#   - https://api.sgroup.qq.com/app/getAppAccessToken（旧文档）
# 默认用前者；若平台侧只在后者生效，用 LING_QQBOT_TOKEN_URL 覆盖。
# **未核实**：本仓库没有真实凭据，无法确认哪一个是当前生效的域名。
# 以官方文档为准（api-v2「获取访问凭证」，2026-09 更新）：
#   POST https://api.bot.qq.com/app/getAppAccessToken
#   body: {"appId": ..., "clientSecret": ...}（camelCase）
# 文档同时说明：失败时 HTTP 仍是 200，要按响应体的 code 判定。
DEFAULT_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"


@dataclass(frozen=True)
class QQBotConfig:
    """一个 QQ 官方机器人实例的连接参数。

    一个实例对应开放平台上的一个机器人（``app_id``）。多个机器人就配多个
    ``instance_id``，账号绑定按 instance 隔离——openid 是**每个机器人下
    独立**的，同一个真人在两个机器人下有两个不同 openid。
    """

    app_id: str = ""
    # AppSecret（开放平台「机器人 → 开发设置」里的 ClientSecret）。
    # 只在服务端内存里用，绝不落日志。
    client_secret: str = ""
    # 实例标识，进入 ActorAccount.connector_instance
    instance_id: str = "qqbot-default"
    # 沙箱环境。默认 True：没有真实资质时先打沙箱，避免把测试消息发到正式用户。
    sandbox: bool = True
    # 显式覆盖 API 基址（自建代理 / 平台改域名时用）。为空则按 sandbox 选。
    api_base: str | None = None
    token_url: str = DEFAULT_TOKEN_URL
    # Webhook 签名密钥（开放平台里的 BotSecret）。官方用 Ed25519：
    # 私钥由 BotSecret 派生，验签时用同一个 secret 推出公钥。
    # 为空则**拒绝一切入站事件**（没有密钥就无法确认来源）。
    bot_secret: str | None = None
    # 直接给 Ed25519 公钥（hex）。给了就优先用它验签，不再从 bot_secret 派生。
    webhook_public_key_hex: str | None = None
    # 回调地址校验（op=13）是否允许不带签名头。
    # 官方是否对 URL 校验请求签名**未核实**；默认 False（要求签名），
    # 万一平台不签，显式打开这个开关，而不是把验签降级成"宽松通过"。
    allow_unsigned_validation: bool = False
    # 单次 HTTP 调用超时（秒）
    timeout_seconds: float = 15.0
    # token 提前刷新的余量（秒）。**不等 401 才换**：401 时代价是一次失败的
    # 发送（可能是用户看得见的丢消息），提前换只是多一次无副作用的请求。
    token_refresh_margin_seconds: float = 300.0
    # 渠道能力声明。QQ 官方默认只允许**被动回复**：
    # 主动推送需要单独申请权限（群主动消息额度尤甚），拿不到就发不出去，
    # 所以默认 False，必须显式配置后才声明支持。
    supports_proactive: bool = False
    supports_group: bool = True
    # 富媒体发送要先上传文件拿 file_info，再带 media 字段发消息（三步流程）。
    # 未实现完整流程前声明 False：宁可如实拒绝，也不要"发出文本、图静默丢失
    # 却照样拿到回执"。
    supports_media: bool = False
    # 单条文本长度上限。**未核实**：官方文档没有给出稳定数字（群/C2C 与
    # 频道各不相同），这里取保守值，部署时按实测调整。
    max_text_length: int = 1500
    # 被动回复窗口：一条用户消息允许几次回复、多久内有效。
    #
    # **已核实**（官方文档「消息收发概述 → 频率与时效规则」）：
    #   单聊：有效期 60 分钟，每条消息可回复 4 次
    #   群聊：有效期  5 分钟，每条消息可回复 5 次
    # 两个场景数值不同，所以做成两组而不是一个。
    passive_window_seconds_private: float = 3600.0
    passive_max_replies_private: int = 4
    passive_window_seconds_group: float = 300.0
    passive_max_replies_group: int = 5

    def passive_quota(self, *, is_group: bool) -> tuple[float, int]:
        """(窗口秒数, 可回复次数)。单聊与群聊的官方数值不同。"""
        if is_group:
            return self.passive_window_seconds_group, self.passive_max_replies_group
        return self.passive_window_seconds_private, self.passive_max_replies_private
    # 发送消息体类型：0=纯文本，2=markdown。markdown 需要在开放平台上
    # 为机器人申请对应模板/权限，默认纯文本。
    msg_type: int = 0

    @property
    def base_url(self) -> str:
        """OpenAPI 基址。v2 只有一个统一地址，沙箱不换域名。"""
        if self.api_base:
            return self.api_base.rstrip("/")
        return PROD_API_BASE

    @property
    def configured(self) -> bool:
        """凭据是否齐备。**不验证有效性**——有效与否只能靠 health() 探。"""
        return bool(self.app_id and self.client_secret)


__all__ = [
    "DEFAULT_TOKEN_URL",
    "PROD_API_BASE",
    "SANDBOX_API_BASE",
    "QQBotConfig",
]
