"""QQ 官方机器人（QQ 开放平台 / QQ Bot）渠道连接器。

与 OneBot/NapCat 是两条独立的接入路径：
- OneBot：QQ 号身份、CQ/消息段、可自由发送（取决于实现与账号）
- QQ 官方：openid 身份、``msg_id`` 被动回复（有次数与时效）、
  主动推送需单独申请权限、Webhook Ed25519 签名

两者共用 ``ling.kernel.inbound_intake`` 的入站受理与 ``ling.kernel.bridge``
的决策/投递链路，渠道层只翻译协议。
"""

from connectors.qqbot.connector import QQBotConnector
from connectors.qqbot.settings import QQBotConfig

__all__ = ["QQBotConfig", "QQBotConnector"]
