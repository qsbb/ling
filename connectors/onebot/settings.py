"""OneBot V11 连接配置。

对应 OneBot 实现的配置项：HTTP API 服务地址与访问令牌，
以及反向上报（HTTP POST）使用的签名密钥。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OneBotConfig:
    """一个 OneBot V11 实现实例的连接参数。

    一个实例对应一个 QQ 号（``self_id``）。多开多个 QQ 就配多个实例，
    各自的 ``instance_id`` 不同，账号绑定按 ``instance_id`` 隔离
    （避免两个机器人共用 QQ 号时身份混淆）。
    """

    # OneBot 实现的 HTTP API 基址，如 http://127.0.0.1:5700
    # 对应 OneBot 配置里的 http.host / http.port
    api_base: str = "http://127.0.0.1:5700"
    # 访问令牌。OneBot 侧配了 access token 时这里必须一致，
    # 否则 API 调用返回 401（未提供）或 403（不匹配）。
    access_token: str | None = None
    # 反向上报签名密钥。配了 secret 后 OneBot 会在每次上报带
    # X-Signature: sha1=<hmac_sha1(secret, body)>，必须校验。
    secret: str | None = None
    # 机器人自己的 QQ 号。用于校验上报的 X-Self-ID 与事件 self_id 一致，
    # 不一致说明上报来自别的实例，应拒绝。
    self_id: str | None = None
    # 实例标识，进入 ActorAccount.connector_instance，做账号命名空间隔离
    instance_id: str = "onebot-default"
    # 单次 API 调用超时（秒）。QQ 平台偶发慢响应，不宜设太短。
    timeout_seconds: float = 15.0
    # 渠道能力声明。OneBot V11 的群聊是常态能力；
    # 主动推送取决于所用实现与账号权限，默认关闭，需探测后打开。
    supports_group: bool = True
    supports_proactive: bool = False
    supports_media: bool = True
    # QQ 单条消息文本上限（实践中约 4500，留余量）
    max_text_length: int = 4000


DEFAULT_CONFIG = OneBotConfig()

__all__ = ["DEFAULT_CONFIG", "OneBotConfig"]
