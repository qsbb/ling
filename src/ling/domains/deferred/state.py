"""延后回复（`DeferredReply`）的**状态机**：纯函数，不碰数据库。

## 这是什么

用户裁决（2026-11-10）原话：「做，但是只在她主动选择时才延后，且延后多久让她
自由发挥，上限 5 分钟」。

一句话语义：**她在生成正文之前主动说"过一会儿再回"**，代码把这句话变成一个
有截止时刻的持久记录，到点由 worker 另开一个**新的逻辑轮次**去回。这不是
"排队"、不是"重试"、不是"工具结果回来后补话"——后三者在
`docs/ASYNC-TOOL-DECISION.md` 与 `docs/MEDIA-EXCLUSIVE-SLOT.md` §8 里都是
明确否决的。

## 三条硬约束（写在这里是因为它们约束的是状态机本身）

1. **默认立即回复**。只有模型明确给出延后意图才有这张表的行；代码任何情况下
   都不许自己决定"延后一下比较好"。所以 `pending` 的**唯一**入口是
   `service.schedule()`，而它只由 `kernel/turn.py` 在**解析到合法标记**时调用。
2. **时长由她给**，不是从档位里挑。见 `markers.py`。
3. **上限 300 秒，代码强制**。模型不能延长；超了就钳到 300，并且**如实记下
   被钳过**（`clamped`）。把"系统上限"说成"她选择等这么久"是本项目明令禁止的
   一类事实伪造，与"把槽位忙包装成人格选择"同一条底线。

## 状态图

    pending ──> claimed ──> delivered          （正常：到点 → 新轮 → 投递成功）
       │           ├──────> failed             （生成失败/回执不明）
       │           ├──────> cancelled          （抢占/授权失效/窗口归档）
       │           └──────> pending            （租约过期且**还没调模型**：可重排）
       ├──────────> cancelled                  （新输入抢占、用户停止、窗口归档）
       └──────────> failed                     （到硬截止仍无可交付正文且兜底也发不出去）

**终态吸收**：`delivered` / `cancelled` / `failed` 出边为空，任何离开它们的
转移抛 :class:`InvalidDeferredTransition`。这是本状态机唯一真正重要的性质——
一条已经 `delivered` 的记录被改成 `cancelled`，会让"她回了"和"她没回"同时成立，
而读侧（消息接口、行为日志对账）会按其中任意一个行动。

## 为什么 `claimed -> pending` 存在

租约只解决"别永久占住"，不解决"占住的人死了"。worker 崩在认领之后、调模型
之前时，那条记录必须能被下一个 worker 捡起来，否则她答应的那句话永远不来。
但如果崩溃发生在**模型已经开始调用之后**（`model_started_at` 非空），重排就
意味着可能第二次收费、且用户可能收到两条。所以这条边**有前提**：只允许
`model_started_at IS NULL` 时走；否则走 `claimed -> failed`（`reason` 记
`lease_expired_after_model_start`），不自动再烧。这条判据在 `service.reclaim()`。

## 与 `TurnState` 的关系（**刻意不合并**）

`TurnState` 是"这一轮执行到哪了"，`DeferredReplyState` 是"她答应的那句话现在
怎么样了"。原轮会正常终结为 `completed`（她确实说完了这一轮），延后是**她的
意愿**，不是轮次没跑完。把 `deferred` 塞进 `TurnState` 会让"轮次已完成但用户
还没收到东西"变成一个无法表达的矛盾。所以对外用
`handled_as="deferred"` + SSE `turn.deferred` 表达，不动 `TurnState`。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ACTIVE_STATES",
    "HARD_DEADLINE_SECONDS",
    "MAX_DELAY_SECONDS",
    "TERMINAL_STATES",
    "DeferredReplyState",
    "InvalidDeferredTransition",
    "is_terminal",
    "transition",
]


#: 她可以要求的**最大等待时长**（秒）。用户原话「上限 5 分钟」。
#:
#: 这个数管的是"她选择的等待时长"，不含生成耗时——"延后多久"字面就是等待
#: 时长。生成另给 :data:`HARD_DEADLINE_SECONDS` 的宽限（见那里）。
MAX_DELAY_SECONDS = 300

#: 到点之后允许生成多久。到 `chosen_due_at + 这个数` 还没交付正文就发兜底短句。
#:
#: 为什么必须有这个上界：没有它，"她选了 300 秒"会变成"用户等 300 秒 + 生成
#: 超时（不知道多久）+ 再等"——用户面对的是**无上界**的等待，而这正是延后
#: 回复最容易变成"消息石沉大海"的地方。
#:
#: 为什么是 60 秒而不是更长：一轮正常生成在 P95 上远小于这个数（见
#: `kernel/turn.py` 的流式超时），60 秒足够覆盖"上下文重编译 + 一次生成"；
#: 再长就变成"她说了 5 分钟却 7 分钟才回"，与她承诺的时长不符。
HARD_DEADLINE_SECONDS = 60


class DeferredReplyState(StrEnum):
    """延后回复的状态。字符串值与 `deferred_replies.state` 一一对应。"""

    #: 已登记，等着到点。**只有**解析到合法延后标记才会进这个状态。
    pending = "pending"
    #: 已被某个 worker 认领（带租约与代次），正在生成。
    claimed = "claimed"
    #: 正文已确认投递出去（含"用了兜底短句"这一种，由 `fallback_used` 区分）。
    delivered = "delivered"
    #: 明确不投递了：被新输入抢占、授权失效、窗口归档、用户停止。
    cancelled = "cancelled"
    #: 想投但没成：生成失败、回执不明、租约在模型调用后过期。
    failed = "failed"


#: 终态：吸收态，不可再变。
TERMINAL_STATES: frozenset[DeferredReplyState] = frozenset(
    {
        DeferredReplyState.delivered,
        DeferredReplyState.cancelled,
        DeferredReplyState.failed,
    }
)

#: 非终态（"在途"）。worker 的认领扫描只对 `pending` 生效。
ACTIVE_STATES: frozenset[DeferredReplyState] = frozenset(
    {DeferredReplyState.pending, DeferredReplyState.claimed}
)


def is_terminal(state: DeferredReplyState) -> bool:
    return state in TERMINAL_STATES


#: 合法转移表。**唯一**的事实来源——`transition()` 与
#: `service` 里的条件更新（`WHERE state=:current`）都按它写。
#:
#: 终态出边为空。**不要**为了"重试"给终态加边：重试应当新建一条记录
#: （新 `source_turn_id`），否则同一行会同时承载两次执行的历史，而
#: "她到底回过没有"就再也查不清了。
ALLOWED_TRANSITIONS: dict[DeferredReplyState, frozenset[DeferredReplyState]] = {
    DeferredReplyState.pending: frozenset(
        {
            DeferredReplyState.claimed,
            DeferredReplyState.cancelled,
            # 到硬截止仍没有 worker 认领过（例如进程长期没跑），也走 failed
            # 而不是留在 pending 里假装"还在等"。用户看到的必须是确定的结论。
            DeferredReplyState.failed,
        }
    ),
    DeferredReplyState.claimed: frozenset(
        {
            DeferredReplyState.delivered,
            DeferredReplyState.cancelled,
            DeferredReplyState.failed,
            # 见模块注释"为什么 claimed -> pending 存在"：**只**在还没调模型时。
            DeferredReplyState.pending,
        }
    ),
    DeferredReplyState.delivered: frozenset(),
    DeferredReplyState.cancelled: frozenset(),
    DeferredReplyState.failed: frozenset(),
}


class InvalidDeferredTransition(Exception):
    """非法状态转移（**不改任何行**）。

    ``code`` 是稳定码（给测试/对账），字符串本身是人话。
    """

    def __init__(
        self,
        current: DeferredReplyState,
        target: DeferredReplyState,
        note: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        self.code = f"illegal_transition:{current.value}->{target.value}"
        super().__init__(
            note
            or f"延后回复状态不能从 {current.value} 变到 {target.value}"
            f"（{'终态不可再变' if is_terminal(current) else '不在允许的转移表里'}）"
        )


def transition(
    current: DeferredReplyState, target: DeferredReplyState
) -> DeferredReplyState:
    """校验一次状态转移，合法返回 ``target``，非法抛异常。

    纯函数：不碰数据库、不看时间、不产生副作用。同状态转移（``pending ->
    pending``）**非法**——那不是转移，是一次空写；允许它会让"重复扫描"看起来
    像成功，从而掩盖真正的状态冲突（例如两个 worker 同时认领）。
    """
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidDeferredTransition(current, target)
    return target
