"""审计日志与安全日志。

## 为什么要和运行日志分开

运行日志回答"系统在干什么"，审计日志回答"**谁**在什么时候**改了什么**"，
安全日志回答"**谁**被拒绝了、为什么"。三者混在一起时：

- 审计条目会被心跳、轮询、`httpx` 噪音淹掉，长期留存成本也高
- 安全事件夹在 INFO 里，告警规则没法只盯一个命名空间
- 出事时"最近谁撤回过记忆""谁改过配置"要靠翻流水账

所以用独立的命名空间 `ling.audit` / `ling.security`，字段也是固定的。

## 为什么先落日志不落库

审计"落库"要解决保留策略、清理、备份一致性；而这些在单用户个人部署里
收益有限、成本不低。**先把字段和调用点做对**，将来要接数据库只需在
`audit_event` / `security_event` 里加一个 sink，调用点一行都不用改。

## 硬约束

- 审计字段只放**计数、枚举、ID、版本号**，绝不放消息正文或记忆内容。
- `target_id` 用内部 ID，外部账号标识必须哈希后再写（见 `hash_account_ref`）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from ling.logging_setup import emit_event

audit_logger = logging.getLogger("ling.audit")
security_logger = logging.getLogger("ling.security")

AUDIT = "audit"
SECURITY = "security"


def hash_account_ref(raw: str | None) -> str:
    """外部账号标识的哈希，用于日志里指代"同一个人"而不暴露原值。

    用 HMAC 而不是裸 SHA-256：QQ 号、openid 这类标识熵很低，
    裸哈希可以被字典反查。密钥取 `LING_LOG_IDENTITY_KEY`，没配就用进程级
    随机值（同一次运行内可比，重启后不可比——这比泄漏原值好）。
    """
    if not raw:
        return ""
    key = os.environ.get("LING_LOG_IDENTITY_KEY") or _PROCESS_KEY
    return hmac.new(key.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]


_PROCESS_KEY = os.urandom(16).hex()


def audit_event(
    action: str,
    *,
    outcome: str = "ok",
    actor: str = "owner",
    target_type: str = "",
    target_id: str = "",
    reason_code: str = "",
    attributes: dict[str, Any] | None = None,
) -> None:
    """记一条审计。`action` 是受控枚举式命名，如 `config_published`。"""
    payload: dict[str, Any] = {
        "log_kind": AUDIT,
        "action": action,
        "outcome": outcome,
        "actor": actor,
    }
    if target_type:
        payload["target_type"] = target_type
    if target_id:
        payload["target_id"] = target_id
    if reason_code:
        payload["reason_code"] = reason_code
    if attributes:
        payload["attributes"] = attributes

    emit_event(
        audit_logger,
        logging.INFO if outcome == "ok" else logging.WARNING,
        event_name=f"audit_{action}",
        message=_AUDIT_MESSAGES.get(action, action),
        attributes=payload,
    )


def security_event(
    action: str,
    *,
    outcome: str = "denied",
    channel: str = "",
    account_ref: str | None = None,
    reason_code: str = "",
    remote: str = "",
    attributes: dict[str, Any] | None = None,
) -> None:
    """记一条安全事件（拒绝、验签失败、限流等）。"""
    payload: dict[str, Any] = {
        "log_kind": SECURITY,
        "action": action,
        "outcome": outcome,
    }
    if channel:
        payload["channel"] = channel
    if account_ref:
        # 只写哈希：安全日志长期留存，不该成为账号标识的泄漏源
        payload["account_ref_hash"] = hash_account_ref(account_ref)
    if reason_code:
        payload["reason_code"] = reason_code
    if remote:
        payload["remote"] = remote
    if attributes:
        payload["attributes"] = attributes

    emit_event(
        security_logger,
        logging.WARNING if outcome == "denied" else logging.ERROR,
        event_name=f"security_{action}",
        message=_SECURITY_MESSAGES.get(action, action),
        attributes=payload,
    )


# 审计动作 → 中文说明（保持事件名稳定、措辞可改）
_AUDIT_MESSAGES: dict[str, str] = {
    "config_published": "配置已发布",
    "config_rolled_back": "配置已回滚",
    "provider_secret_set": "供应商密钥已更新",
    "memory_retracted": "记忆已撤回",
    "memory_forgotten": "记忆已清除",
    "memory_superseded": "记忆已更正",
    "consent_changed": "授权已变更",
    "subjects_merged": "账号身份已合并",
    "subjects_unmerged": "账号身份已拆分",
    "account_bound": "账号已绑定",
    "password_changed": "登录口令已修改",
    "session_revoked": "登录会话已吊销",
}

_SECURITY_MESSAGES: dict[str, str] = {
    "inbound_denied": "入站被拒",
    "auth_failed": "登录失败",
    "signature_invalid": "回调验签失败",
    "unsigned_validation_allowed": "未验签放行",
    "must_change_password_blocked": "未改密请求被拦",
    "rate_limited": "请求被限流",
}

__all__ = [
    "AUDIT",
    "SECURITY",
    "audit_event",
    "hash_account_ref",
    "security_event",
]
