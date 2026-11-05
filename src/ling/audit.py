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
behavior_logger = logging.getLogger("ling.behavior")

AUDIT = "audit"
SECURITY = "security"
# 运行日志：api、连接、调度这类**系统内部**的事实。
RUNTIME = "runtime"
# 行为日志：她**作为一个人的经历**——收到了谁的话、看了哪个群、
# 决定接不接、为什么、在忙什么。读起来应该像日记，不像 trace。
BEHAVIOR = "behavior"


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
    "BEHAVIOR",
    "RUNTIME",
    "SECURITY",
    "audit_event",
    "behavior_event",
    "hash_account_ref",
    "security_event",
]


def behavior_event(
    action: str,
    message: str,
    *,
    outcome: str = "ok",
    attributes: dict[str, Any] | None = None,
) -> None:
    """记一条**行为**日志：她经历了什么、怎么想的、做了什么。

    ## 与 audit / security 的区别

    - ``audit``  到底谁改了什么（可追责的事实）
    - ``security`` 谁被拒绝了、为什么（安全边界）
    - ``behavior`` 她收到了谁的话、看了哪个群、接不接、为什么——**读起来
      像日记**，用来回答"她刚才在忙什么""她有没有看群"这类问题

    ``message`` 必须是**人话**，不是事件名：写「看了下 XX 群，他们在聊游戏，
    插不上嘴，没说话」，不要写「poll_window result=silent」。

    ## 为什么正文可以进这里

    行为日志和审计日志的约束**刻意不同**：审计只放计数/枚举/ID，因为要长期
    留存；行为日志是"她这一生经历了什么"的叙述，没有说话人就无法还原。
    但它仍然只进内存环形缓冲（与运行日志同一条管道），**不落库、不外发**，
    保留时长受 500 条上限约束。
    """
    payload: dict[str, Any] = {
        "log_kind": BEHAVIOR,
        "action": action,
        "outcome": outcome,
    }
    if attributes:
        payload["attributes"] = attributes
    emit_event(
        behavior_logger,
        logging.INFO,
        event_name=action,
        message=message,
        attributes=payload,
    )
