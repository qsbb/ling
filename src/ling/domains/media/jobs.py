"""视频后台取证作业（`media.video.read`）的**状态机**与**入队投递**。

本模块目前只落裁决 `docs/ASYNC-TOOL-DECISION.md` §3 的第 1、2 步：入口、持久
状态、状态机的**纯函数**、以及"作业 + outbox 同事务入队"。执行（第 3 步）与
预算/读侧（第 4 步）**不在本轮**，所以这里没有任何模型调用、没有 worker 注册。

## 状态机

    pending ──> running ──> succeeded
       │           ├──────> failed
       │           ├──────> timeout
       │           ├──────> unknown
       │           ├──────> cancelled
       │           └──────> expired
       ├──────────> cancelled
       ├──────────> expired
       └──────────> budget_denied

**终态只有七个里的六个**（`succeeded/failed/timeout/unknown/cancelled/expired/
budget_denied`），`pending` 与 `running` 是非终态。终态**不可再变**——这一条是
状态机唯一真正重要的性质：一个已经 `succeeded` 的作业被改成 `failed`，就会让
"结果已经落库"和"作业说失败了"同时成立，而下游（读侧、对账）会按其中任意一个
行动。所以终态是吸收态，任何离开它的转移都抛错。

## 为什么是纯函数

裁决要求"非法转移抛错"，而非法转移的触发条件大多在**并发**里（两个 worker 抢
同一行、晚到的结果想覆盖已终结的作业）。把判据写成纯函数，才能在单测里穷举
每一条边，而不是靠"跑一遍集成看有没有炸"。真正的落地还要靠 SQL 的
`WHERE state IN (...)` 条件更新（第 3 步），两者判据必须一致。

## `unknown` 为什么不能叫 `failed`

`unknown` 专指"模型**可能已经调用了**，但我们没有持久结果"（进程在模型调用后
被杀、或结果事务没提交）。它和 `failed`（确定没成功）的区别是**能不能再烧一次
钱**：`failed` 可以按重试策略再试，`unknown` 不许自动重跑——那可能第二次收费。
把它并进 `failed` 就等于用一个更确定的词掩盖一个不确定的事实。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import MediaReadJob

__all__ = [
    "ACTIVE_STATES",
    "JOB_SCHEMA_VERSION",
    "MEDIA_READ_QUEUE_DEADLINE",
    "MEDIA_READ_TOOL_VERSION",
    "MEDIA_READ_TOPIC",
    "JobState",
    "InvalidJobTransition",
    "enqueue_read_job",
    "is_terminal",
    "outbox_event_id_for",
    "transition",
]


JOB_SCHEMA_VERSION = "v1"

#: 工具版本。同一证据**同一版本**最多一个在途作业（部分唯一索引按它分组）。
#:
#: 版本升级的语义：新版本允许对同一证据再排一个作业（比如换了更便宜的前端
#: 抽帧），旧版本的在途作业不阻塞它。写死在代码里而不是配置项——它是**行为的
#: 身份**，不是可调参数；改它必须同时改读侧对结果格式的假设。
MEDIA_READ_TOOL_VERSION = "media.video.read@1"

#: outbox topic。第 3 步的 worker 会订阅它（本轮不注册）。
MEDIA_READ_TOPIC = "media.video.read"

#: 排队截止：登记后 15 分钟还没开始就由清理任务终结（裁决 §3 第 3 步）。
#:
#: 为什么是"排队"而不是"总时长"：用户等的是"它什么时候开始读"，而一次读取
#: 本身有独立的 180 秒执行超时。两者混成一个字段会让"排了很久但刚好开始"的
#: 作业被误杀。
MEDIA_READ_QUEUE_DEADLINE = timedelta(minutes=15)


class JobState(StrEnum):
    """作业状态。字符串值与数据库列一一对应（迁移 0058 的 CHECK 约束）。"""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    #: 模型**可能已经调用**但没有持久结果。**不等于失败**，不许自动重跑。
    unknown = "unknown"
    timeout = "timeout"
    cancelled = "cancelled"
    expired = "expired"
    budget_denied = "budget_denied"


#: 终态：吸收态，不可再变。
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.succeeded,
        JobState.failed,
        JobState.timeout,
        JobState.unknown,
        JobState.cancelled,
        JobState.expired,
        JobState.budget_denied,
    }
)

#: 非终态（"在途"）。入队的互斥索引只对这几个状态生效。
ACTIVE_STATES: frozenset[JobState] = frozenset({JobState.pending, JobState.running})


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_STATES


#: 合法转移表。**唯一**的事实来源——`transition()` 与迁移里的 CHECK 都按它写。
#:
#: 为什么 `running` 能到 `cancelled`：用户点取消时作业已经在跑，我们置
#: `cancel_requested_at` 并尽力停；真的停下之后才落 `cancelled`。这条边是"停下
#: 来了"，不是"从没开始过"。
#:
#: 为什么 `pending` 能到 `budget_denied`：预算检查在**开工前**做（裁决 §3 第 4
#: 步），没额度就永远不该进 `running`。`running` 之后才发现没额度是 bug，所以
#: 没有 `running -> budget_denied` 这条边。
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.pending: frozenset(
        {
            JobState.running,
            JobState.cancelled,
            JobState.expired,
            JobState.budget_denied,
        }
    ),
    JobState.running: frozenset(
        {
            JobState.succeeded,
            JobState.failed,
            JobState.timeout,
            JobState.unknown,
            JobState.cancelled,
            JobState.expired,
        }
    ),
    # 终态：出边为空。**不要**为了"重试"给终态加边——重试应当新建一个作业
    # （新 call_id），否则同一行会同时承载两次执行的历史。
    JobState.succeeded: frozenset(),
    JobState.failed: frozenset(),
    JobState.timeout: frozenset(),
    JobState.unknown: frozenset(),
    JobState.cancelled: frozenset(),
    JobState.expired: frozenset(),
    JobState.budget_denied: frozenset(),
}


class InvalidJobTransition(Exception):
    """非法状态转移（**不改任何行**）。

    ``note`` 是人话（给日志/接口），``code`` 是稳定码（给测试/对账）。
    """

    def __init__(self, current: JobState, target: JobState, note: str | None = None) -> None:
        self.current = current
        self.target = target
        self.code = f"illegal_transition:{current.value}->{target.value}"
        super().__init__(
            note
            or f"作业状态不能从 {current.value} 变到 {target.value}"
            f"（{'终态不可再变' if is_terminal(current) else '不在允许的转移表里'}）"
        )


def transition(current: JobState, target: JobState) -> JobState:
    """校验一次状态转移，合法则返回 ``target``，非法抛 :class:`InvalidJobTransition`。

    纯函数：不碰数据库、不看当前时间、不产生副作用。调用方拿到返回值后自己
    决定怎么落库（第 3 步用 `UPDATE … WHERE state=:current` 条件更新，两者判据
    必须一致）。

    同状态转移（``pending -> pending``）**非法**：那不是转移，是一次空写。允许它
    会让"重复投递"看起来像成功，从而把真正的状态冲突掩盖掉。
    """
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidJobTransition(current, target)
    return target


def outbox_event_id_for(call_id: str) -> str:
    """由 call_id **稳定派生** outbox 事件主键。

    ## 为什么不能把幂等键只放 payload 里

    `outbox_events` 的幂等靠的是**主键**（`worker.py` 的 `_enqueue_event`
    先 `session.get` 再插，主键冲突兜住并发）。如果只是 payload 里放一个
    ``call_id``，主键仍是随机 id，同一作业被投两次就是**两条事件、两次执行**。

    派生规则写成函数而不是在调用点拼字符串：拼错了不会报错，只会变成两条
    互不相识的事件——最难发现的那种错。
    """
    return f"outbox_{MEDIA_READ_TOPIC.replace('.', '_')}_{call_id}"


async def enqueue_read_job(
    session: AsyncSession,
    *,
    job: MediaReadJob,
    now: datetime | None = None,
) -> bool:
    """把作业与它的 outbox 事件**同事务**插入；返回"是否真的新建了"。

    ## 顺序与原子性

    1. 先 ``session.add(job)`` + ``flush()``：作业行必须先存在，否则 outbox
       事件被 worker 抢到时会指向一条还不存在的作业（worker 那边就会记
       "unknown job" 然后丢弃，用户等一个永远不会开始的读取）。
    2. 再按主键插 outbox 事件。**同一个事务**，所以"作业存在但事件没了"
       （永远不执行）和"事件存在但作业没了"（执行时找不到）都不可能。
    3. 调用方负责 ``commit()``。本函数**不提交**——提交点必须由调用方掌握，
       否则"202 已返回但事务其实没提交"这种顺序问题会出现在路由里。

    ## 重复入队

    作业行本身有 ``UNIQUE(actor_id, window_id, request_key)``，重复插入会先
    在 ``flush()`` 抛 :class:`IntegrityError`；调用方应catch它并返回**同一个**
    作业（幂等 202）。outbox 事件这层用主键去重再兜一次：即使作业行因为某种
    原因被复用，事件也不会重复。
    """
    moment = now or datetime.now(UTC)

    from ling.infra.models import OutboxEvent

    session.add(job)
    # 作业行的唯一约束在**这里**暴露（重复 request_key / 在途互斥）。
    # 让它在 flush 当场炸，而不是等到 commit 时才发现——那时路由已经在拼
    # 202 的响应了。
    await session.flush()

    call_id = job.call_id
    event_id = outbox_event_id_for(call_id)
    if await session.get(OutboxEvent, event_id) is not None:
        # 已经有事件（重复投递）：作业行刚插进去说明是新作业，事件却已存在
        # ——只可能是"上一次插了事件但作业回滚过"。补一条新事件会更糟（两次
        # 执行），所以这里**不插**、交给对账；返回 False 让调用方知道。
        return False
    try:
        async with session.begin_nested():
            session.add(
                OutboxEvent(
                    id=event_id,
                    topic=MEDIA_READ_TOPIC,
                    # payload **只放** call_id 与 schema 版本。作业的一切事实
                    # 都读库：放进 payload 等于让"消息里的状态"和"行里的状态"
                    # 各说各话，而 payload 是发出去就改不了的。
                    payload={"call_id": call_id, "schema_version": JOB_SCHEMA_VERSION},
                    available_at=moment,
                    created_at=moment,
                )
            )
            await session.flush()
    except IntegrityError:
        # 两个并发请求同时插：主键兜底，另一个赢了。不是错误。
        return False
    return True
