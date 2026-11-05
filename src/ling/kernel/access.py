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


# 准入的三态。原来只有「允许 / 拒绝」两态，那是把两件事混成了一件：
# **能不能进她的世界**（落库、被记住、进上下文）与**能不能让她开口**。
#
# 混在一起的后果是真实发生过的：白名单在入站第一道闸就把群消息杀掉了，
# 于是群聊轮询机制（`domains/attention`，写得挺完整）永远拿不到内容——
# 她**看不见**群里在聊什么，也就谈不上"像人一样偶尔看一眼"。
OBSERVE_ONLY = "observe_only"
"""进上下文、能被记住，但**不触发回复**（除非被明确点名）。"""
MAY_REPLY = "may_reply"
"""既进上下文，也能触发回复。"""
DENIED = "denied"
"""连上下文都不进。只用于私聊陌生人——那是安全边界。"""

VERDICTS = (MAY_REPLY, OBSERVE_ONLY, DENIED)


@dataclass(frozen=True)
class AccessDecision:
    verdict: str
    reason: str
    # 被**明确点名**时是否允许回复。白名单内的人即使在不熟悉的群里，
    # 被 @ 也该能触发回复；陌生人即使在白名单群里被 @ 也不该。
    reply_when_addressed: bool = False

    @property
    def allowed(self) -> bool:
        """还能不能触发回复。**注意**：`observe_only` 这里是 False，
        但它仍然会进上下文——判断"要不要落库"请看 `observable`。"""
        return self.verdict == MAY_REPLY

    @property
    def observable(self) -> bool:
        """能不能进她的上下文（落库、被记住、进注意力轮询）。"""
        return self.verdict in (MAY_REPLY, OBSERVE_ONLY)

    @property
    def code(self) -> str:
        return "allowed" if self.allowed else "denied"


def check_access(
    sender_id: str | None,
    *,
    mode: str,
    allow_list: frozenset[str] | set[str],
    group_id: str | None = None,
    allow_groups: frozenset[str] | set[str] | None = None,
) -> AccessDecision:
    """判定这个发送者能不能跟她说话、以及**能不能进她的上下文**。

    - 名单用**字符串**比较，不做数字转换：QQ 号可能带前导零，
      转 int 会丢掉它，于是"加了白名单却对不上"。
    - 发送者缺失时：whitelist 模式**拒绝**（不知道是谁就不放行），
      blacklist 模式放行（他不在黑名单里）。

    ## 三态而不是两态

    白名单回答的是"**能不能让她开口**"，不是"**能不能进她的世界**"。
    群里绝大多数人不在白名单，但她应该能**看见**群里的对话——否则
    "偶尔看一眼群"这件事根本无从发生（这正是改造前的问题）。

    所以：

    - 白名单里的人：`may_reply`，被 @ / 引用时也确实会回
    - 白名单群里**其他人**：`observe_only`，进上下文但不主动开口；
      被明确 @ 时仍可回（那是"有人跟她说话"，不是"她在插嘴"）
    - 私聊陌生人：`denied`，**连上下文都不进** —— 私聊是安全边界，
      谁都能给她发私信的话，"看一眼手机"就等于把门敞开
    """
    normalized = (sender_id or "").strip()
    listed = normalized in allow_list
    groups = allow_groups if allow_groups is not None else frozenset()
    in_allowed_group = bool(group_id) and str(group_id).strip() in groups

    if mode == "off":
        return AccessDecision(MAY_REPLY, "许可检查已关闭", reply_when_addressed=True)
    if mode not in MODES:
        # 配置写错时**按最严处理**：宁可漏接，不要因为一个拼写错误
        # 把所有陌生人放进来。注意这里连 observe 也不给——配置坏了就该停下，
        # 而不是让陌生人的话先灌进她的记忆。
        return AccessDecision(DENIED, f"未知的许可模式 {mode!r}，按拒绝处理")

    if not normalized:
        if mode == "whitelist":
            return AccessDecision(DENIED, "无法识别发送者，白名单模式不处理")
        return AccessDecision(MAY_REPLY, "无法识别发送者，黑名单模式放行")

    if mode == "blacklist":
        if listed:
            return AccessDecision(DENIED, "发送者在黑名单内，不处理")
        return AccessDecision(MAY_REPLY, "发送者不在黑名单内")

    # ---- whitelist 模式 ----
    if listed:
        return AccessDecision(MAY_REPLY, "发送者在白名单内", reply_when_addressed=True)
    if in_allowed_group:
        # 白名单群里的人：她看得见，但不主动插话；被点名时可以回。
        return AccessDecision(
            OBSERVE_ONLY,
            "这个群在白名单里，消息进上下文但不主动接话",
            reply_when_addressed=True,
        )
    if group_id:
        # 不熟悉的群：也看得见（人本来就会在各种群里潜水），
        # 但**不能**主动开口，被 @ 也不回——她跟这群人没关系。
        return AccessDecision(
            OBSERVE_ONLY, "群不在白名单里，只看不接话", reply_when_addressed=False
        )
    return AccessDecision(DENIED, "发送者不在白名单内，不处理")


__all__ = [
    "DENIED",
    "MAY_REPLY",
    "MODES",
    "OBSERVE_ONLY",
    "VERDICTS",
    "AccessDecision",
    "check_access",
]
