"""出入许可：谁能跟她说话。

## 为什么在边界上拦

一个陌生 QQ 号发来消息，如果不在这里挡住，下游会**建窗口、建会话记录、
写消息、跑决策，甚至调模型**——全流程走完才可能被发现不该理。
那是浪费，也是一份不该存在的记录。

所以在 `normalize_inbound` 之后、**任何写库之前**就判定。

## 三种模式

- `off`：谁都能说话（默认之外的宽松选择）
- `whitelist`：**只有名单里的人**能说话——名单外一律不处理
- `blacklist`：名单外的人都能说话

## 被拒时返回 200 + accepted=false

OneBot 在非 200 时会反复重投同一条事件。拒绝不是"服务出错"，
是"这条我们不处理"——用 200 表明"已收到，不必再投"，
理由写在响应体里便于排查。

## 一个刻意的选择：拒绝时**不**建窗口

名单外的人不该在库里留下会话痕迹。只在日志里留一行（含 QQ 号），
便于你自己核对是不是漏加了谁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("ling.kernel.access")

MODES = ("off", "whitelist", "blacklist")


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str

    @property
    def code(self) -> str:
        return "allowed" if self.allowed else "denied"


def check_access(
    sender_id: str | None,
    *,
    mode: str,
    allow_list: frozenset[str] | set[str],
) -> AccessDecision:
    """判定这个发送者能不能跟她说话。

    - 名单用**字符串**比较，不做数字转换：QQ 号可能带前导零，
      转 int 会丢掉它，于是"加了白名单却对不上"。
    - 发送者缺失时：whitelist 模式**拒绝**（不知道是谁就不放行），
      blacklist 模式放行（他不在黑名单里）。
    """
    normalized = (sender_id or "").strip()
    listed = normalized in allow_list

    if mode == "off":
        return AccessDecision(True, "许可检查已关闭")
    if mode not in MODES:
        # 配置写错时**按最严处理**：宁可漏接，不要因为一个拼写错误
        # 把所有陌生人放进来。
        return AccessDecision(False, f"未知的许可模式 {mode!r}，按拒绝处理")
    if not normalized:
        if mode == "whitelist":
            return AccessDecision(False, "无法识别发送者，白名单模式不处理")
        return AccessDecision(True, "无法识别发送者，黑名单模式放行")

    if mode == "whitelist":
        return (
            AccessDecision(True, "发送者在白名单内")
            if listed
            else AccessDecision(False, "发送者不在白名单内，不处理")
        )
    return (
        AccessDecision(False, "发送者在黑名单内，不处理")
        if listed
        else AccessDecision(True, "发送者不在黑名单内")
    )


__all__ = ["MODES", "AccessDecision", "check_access"]
