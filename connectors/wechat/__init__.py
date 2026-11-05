"""微信连接器：按已确定且获准的账号类型分别实现（W2：已实现未测试）。"""

from connectors.wechat.connector import (
    PROFILES,
    AccountTypeProfile,
    WeChatAccountType,
    WeChatConfig,
    WeChatConnector,
    describe_account_types,
)

__all__ = [
    "PROFILES",
    "AccountTypeProfile",
    "WeChatAccountType",
    "WeChatConfig",
    "WeChatConnector",
    "describe_account_types",
]
