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

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.media.budget import ReservationResult
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


# ================================================================== 执行侧
#
# 下面是裁决 §3.3（执行与恢复）。**不做预算**——那是第 4 步，本轮一次模型调用
# 也不预留额度，也**不**补 `behavior_model_calls`。这个顺序是有意的：先把
# "作业怎么死、怎么活过来"做对，再谈"能花多少钱"。
#
# ## 一次执行的生命周期
#
#     claim（pending→running，拿租约）
#       → 取件（下载 + 抽帧）        ← 只有这一段的失败**抛异常**让 outbox 重排
#       → 模型开始（写 model_started_at，提交）
#       → 调模型
#       → 写证据（MediaEvidenceService.describe）
#       → 提交终态（条件更新，带租约代次栅栏）
#
# ## 为什么"只有取件类故障抛异常"
#
# `outbox` 只对**异常**退避重排（`worker.settle_outbox_event`）。裁决说得很
# 清楚：ToolRegistry 的 `failed`/`timeout` 返回值不会触发重试，所以：
#
# - **取件类故障**（网络抖动、文件临时读不到）：此刻还没花钱，重排是安全的，
#   而且重排是**唯一**能让它自动再试的机制 → 抛异常；
# - **模型已经开跑之后**的一切结局：必须**就地落终态**（`failed`/`timeout`/
#   `unknown`），**返回**而不是抛。抛出去会让 outbox 重排，然后模型第二次被
#   调用——那是第二次收费，而第一次可能已经成功过。
#
# ## 四类崩溃各自怎么收敛
#
# | 死在哪 | 库里的样子 | 谁来收 | 结果 |
# | --- | --- | --- | --- |
# | 入队后、认领前 | `pending`，事件 `pending`/`in_flight` 租约到期 | outbox 租约回收 + 清理 | 重新排队 |
# | 认领后、模型前 | `running`，`model_started_at IS NULL`，租约过期 | 清理任务 | 置 `pending` **重新执行** |
# | 模型开始后 | `running`，`model_started_at` 非空，租约过期 | 清理任务 | 置 `unknown`，**不自动再烧** |
# | 结果提交后 | 终态 + `result_ref` | 重放（事件重投） | 只读已有结果，**零模型调用** |
#
# 第三行与第二行的区别就是 `model_started_at`。它必须**在调模型之前**单独提交
# ——和模型调用放在同一个事务里的话，崩溃时这一行会跟着回滚，"模型可能已经
# 收过费"这件事就查不到了，重排后就会第二次收费。

UNKNOWN_JOB_CODE = "worker_died_after_model_start"


class ReadJobError(Exception):
    """**取件类**故障：还没有花任何模型调用，重排是安全的。

    只由 :func:`run_read_job` 在"模型调用之前"抛出。outbox 看到异常会按
    指数退避重排（最多 5 次，到上限转 dead 并同步终结业务作业）。

    ``reason`` 会写进 outbox 的 ``last_error`` 与日志，**不是**人话文案。
    """


@dataclass(frozen=True)
class ReadOutcome:
    """一次执行的**结构化**结果。

    ## 为什么需要它

    `watch.py` / `describe.py` 的回调只回一个观察字符串或 ``None``，故障类型
    全部糊在一起：下载失败、模型超时、撤权、内容为空在调用方看来都是"没拿到
    字符串"。而作业的终态需要区分它们（`failed` vs `timeout` vs `cancelled`
    vs `unknown`）。所以在 jobs 里包一层，**不改**现有回调签名——那是第 4 步
    与读侧的接口，动它会把影响面扩到视频同步路径。

    ``observed`` 是模型看到的描述（成功时），只在内存里用于日志；落库走
    `MediaEvidenceService.describe`，不复制第二份。
    """

    state: JobState
    error_code: str | None = None
    #: 成功时指向 evidence_id（描述就写在那条证据上，不另存副本）
    evidence_ref: str | None = None
    observed: str | None = None
    #: 是否需要 outbox 重排（只有取件类故障为 True，且那种情况会抛异常，
    #: 这个字段是给"将来要在返回值里表达"留的位置，当前恒为 False）
    retriable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.succeeded


#: 租约时长（秒）。600 秒 = 10 分钟：一次视频读取的硬上限是 180 秒（第 4 步
#: 才落地执行超时），租约留 3 倍余量，避免"读得慢一点就被别人抢走"。
#:
#: 为什么必须比执行时长大而不是相等：抢走之后原执行者的结果会被栅栏丢弃，
#: 新的执行者从头再读一次——如果租约刚好等于执行时长，慢一点的视频会**永远**
#: 被抢、永远读不完。
MEDIA_READ_LEASE_SECONDS = 600

#: 排队截止的兜底：`deadline` 可空（第 2 步的迁移为了可回滚没写非空），
#: 清理任务对 NULL 用 `requested_at + MEDIA_READ_QUEUE_DEADLINE` 兜。
MEDIA_READ_CLEANUP_INTERVAL_SECONDS = 60


async def claim_read_job(
    session: AsyncSession,
    *,
    call_id: str,
    lease_owner: str,
    now: datetime | None = None,
) -> bool:
    """把作业从 `pending` 抢成 `running`，`lease_generation += 1`。

    条件更新（`WHERE call_id=? AND state='pending'`）而不是"先读后写"：
    两个媒体 worker 同时被叫醒时只有一个人能拿到执行权，另一个 ``rowcount=0``。

    **不加行锁**：裁决要求"下载、抽帧、模型运行不占数据库行锁"。这里是一个
    短事务，更新完立刻结束，执行的几百秒里不持有任何锁。
    """
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    moment = now or _utcnow()
    result = await session.execute(
        update(MediaReadJob)
        .where(
            MediaReadJob.call_id == call_id,
            MediaReadJob.state == JobState.pending.value,
        )
        .values(
            state=JobState.running.value,
            started_at=moment,
            lease_owner=lease_owner,
            lease_until=moment + timedelta(seconds=MEDIA_READ_LEASE_SECONDS),
            lease_generation=MediaReadJob.lease_generation + 1,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def _lease_generation_of(session: AsyncSession, call_id: str) -> int:
    from ling.infra.models import MediaReadJob

    value = await session.scalar(
        select(MediaReadJob.lease_generation).where(MediaReadJob.call_id == call_id)
    )
    return int(value or 0)


async def mark_model_started(
    call_id: str, *, lease_owner: str, lease_generation: int
) -> bool:
    """**在调模型之前**单独提交 `model_started_at`（崩溃语义的分水岭）。

    为什么必须是独立事务、必须在模型之前：

    - 崩溃后清理任务靠"`running` 且 `model_started_at` 非空"判断
      **模型可能已经收过费**，从而记 `unknown` 而**不**自动重跑；
    - 如果它和结果在同一个事务里，崩溃时一起回滚，重排后就会第二次调用模型。

    条件更新带 `lease_generation`：租约已经被别人接走的作业不该再被写。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    moment = _utcnow()
    async with session_scope() as session:
        result = await session.execute(
            update(MediaReadJob)
            .where(
                MediaReadJob.call_id == call_id,
                MediaReadJob.state == JobState.running.value,
                MediaReadJob.lease_generation == lease_generation,
            )
            .values(
                model_started_at=moment,
                # 续租：模型这一次调用最多 180 秒，加上取件也远小于 600 秒，
                # 但从"现在"重新算一次更稳（取件可能已经花掉不少时间）。
                lease_until=moment + timedelta(seconds=MEDIA_READ_LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        await session.flush()
        return int(getattr(result, "rowcount", 0) or 0) == 1


async def commit_read_outcome(
    session: AsyncSession,
    *,
    call_id: str,
    lease_generation: int,
    outcome: ReadOutcome,
    now: datetime | None = None,
) -> bool:
    """**带栅栏**地提交终态：``WHERE call_id=? AND lease_generation=? AND state='running'``。

    ``rowcount != 1`` 说明租约已经被别人抢走（或作业已被清理任务终结）——本次
    结果**丢弃**，不覆盖别人的。返回 ``False`` 让调用方记一条日志。

    为什么不在 catch 里"再试一次"：租约被抢走意味着另一个执行者正在跑同一段
    视频。谁先提交谁算数，后到者的结果没有意义（内容可能不同，证据行只有一个）。

    终态还必须是 :func:`transition` 允许的（`running` 出发的边），非法转移直接
    抛 :class:`InvalidJobTransition` ——那是 bug，不是运行期状态。
    """
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    transition(JobState.running, outcome.state)
    moment = now or _utcnow()
    result = await session.execute(
        update(MediaReadJob)
        .where(
            MediaReadJob.call_id == call_id,
            MediaReadJob.lease_generation == lease_generation,
            MediaReadJob.state == JobState.running.value,
        )
        .values(
            state=outcome.state.value,
            error_code=outcome.error_code,
            result_ref=outcome.evidence_ref if outcome.succeeded else None,
            completed_at=moment,
            lease_owner=None,
            lease_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def _cancel_requested(session: AsyncSession, call_id: str) -> bool:
    """这个作业是否已经被要求取消（读一次，不做锁）。"""
    from ling.infra.models import MediaReadJob

    value = await session.scalar(
        select(MediaReadJob.cancel_requested_at).where(MediaReadJob.call_id == call_id)
    )
    return value is not None


async def _job_window(session: AsyncSession, call_id: str) -> tuple[str, str, str] | None:
    """(window_id, evidence_id, subject_id)；作业不存在返回 None。"""
    from ling.infra.models import MediaReadJob

    row = (
        await session.execute(
            select(
                MediaReadJob.window_id,
                MediaReadJob.evidence_id,
                MediaReadJob.subject_id,
            ).where(MediaReadJob.call_id == call_id)
        )
    ).first()
    if row is None:
        return None
    return str(row[0]), str(row[1]), str(row[2] or "")


def classify_describe_error(exc: BaseException) -> ReadOutcome:
    """把媒体域的错误翻译成终态（**不抛**）。

    对应关系（`domains/media/errors.py` 的三个字段各回答一个问题）：

    | 异常 | 终态 | 理由 |
    | --- | --- | --- |
    | `MediaDescribeError(model_kind=timeout)` | `timeout` | 模型超时，确定没拿到 |
    | `MediaDescribeError(source=model)` 其它 | `failed` | 模型/配置侧失败 |
    | `MediaDescribeError(retriable=False)` | `failed` | 重试也不会好 |
    | `MediaDescribeError` 其它 | `failed` | 已经花过模型钱，**不重排** |
    | `MediaReadError`（太大/为空） | `failed` | 这条视频读不了 |
    | `MediaEvidenceError`（撤权/过期） | `failed` | 业务拒绝 → `error_code=authorization_revoked` |
    | 其它 | `failed` | 兜底，仍然落终态 |

    ## 为什么"可重试的模型失败"也记 `failed` 而不是抛出去重排

    因为模型**已经被调用过**（`model_started_at` 已写）。抛出去让 outbox 重排
    = 再调一次模型 = 第二次收费，而第一次可能已经成功、只是我们没接住。裁决
    的结论是"新 handler 必须持久化业务终态"。所以这里一律返回终态。

    真正可重试的是**取件**（还没写 `model_started_at`），那段由
    :func:`run_read_job` 直接抛 :class:`ReadJobError`。
    """
    from ling.domains.media.describe import MediaReadError
    from ling.domains.media.errors import (
        SOURCE_BUSINESS,
        SOURCE_REF,
        MediaDescribeError,
        MediaRefError,
        MediaRefPermanentError,
    )
    from ling.domains.media.service import MediaEvidenceError
    from ling.ports.model import ModelErrorKind

    # **顺序要紧**：`MediaRefPermanentError` 是 `MediaDescribeError` 的**子类**
    # （见 errors.py），先判父类会把"引用越界"吞进模型失败分支，error_code 变成
    # 一个只有内部人看得懂的 `media_ref_permanent_error`，而"引用坏了（重试
    # 永远没用）"和"模型这次不行"是两件要分开说的事。从最具体到最宽泛。
    code = type(exc).__name__

    if isinstance(exc, MediaRefPermanentError):
        return ReadOutcome(state=JobState.failed, error_code="media_ref_unavailable")
    if isinstance(exc, MediaReadError):
        # 视频太大/内容为空：这条引用读不了，不是模型故障。
        return ReadOutcome(state=JobState.failed, error_code="video_unreadable")
    if isinstance(exc, MediaRefError):
        return ReadOutcome(state=JobState.failed, error_code="media_ref_unavailable")
    if isinstance(exc, MediaEvidenceError):
        # 撤权/过期/窗口不匹配：业务拒绝 → 永远不该是成功。
        return ReadOutcome(state=JobState.failed, error_code="authorization_revoked")
    if isinstance(exc, MediaDescribeError):
        kind = exc.model_kind
        if kind in (ModelErrorKind.timeout, getattr(ModelErrorKind.timeout, "value", None)):
            return ReadOutcome(state=JobState.timeout, error_code="model_timeout")
        if exc.source == SOURCE_BUSINESS:
            return ReadOutcome(state=JobState.failed, error_code="authorization_revoked")
        if exc.source == SOURCE_REF:
            return ReadOutcome(state=JobState.failed, error_code="media_ref_unavailable")
        return ReadOutcome(state=JobState.failed, error_code=_short_code(code))
    return ReadOutcome(state=JobState.failed, error_code=_short_code(code))


def _short_code(raw: str) -> str:
    """异常类名 → 稳定错误码（蛇形、截断到 64）。"""
    import re as _re

    snake = _re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()
    return snake[:64]


async def run_read_job(
    *,
    call_id: str,
    lease_owner: str,
    describer: object | None = None,
    service_factory: Callable[[AsyncSession], object] | None = None,
    now: datetime | None = None,
) -> ReadOutcome:
    """执行一次后台视频读取：`pending → running → 终态`。

    ## 参数为什么是这个形状

    ``describer`` 是可选的**已构造**的 `MediaDescriber`（worker 侧注入，复用
    它的 `fetch_media` / router / config_reader）。不传就由 ``service_factory``
    造一个——但生产路径**总是**注入，因为 worker 有渠道连接器与密钥缓存，
    测试里也能塞一个假的。这样可以单测整条执行链而不用起 worker。

    ``service_factory``：``(session) -> MediaEvidenceService`` 的工厂。传它而
    不是直接传 service，是因为执行跨多个事务（认领 / 写 model_started_at /
    提交终态），每个事务有自己的 session。

    ## 返回值 vs 异常（这是本函数最重要的约定）

    - **返回** :class:`ReadOutcome`：业务已经有结论了，**不要再跑**。调用方
      必须把 outbox 事件正常确认（done）。**失败也返回**。
    - **抛** :class:`ReadJobError`：取件类故障，**还没花钱**，请重排。

    没有第三种情况。"模型调用之后"的任何异常都会被翻译成终态返回，绝不外抛。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import utcnow as _utcnow

    moment = now or _utcnow()

    # 描述器必须在**花钱和改状态之前**就确认存在。
    #
    # 这条检查原来在预算预留之后（第 3 步），于是 `describer is None` 这条
    # 正常路径（worker 的模型路由还没就绪，`_build_video_describer` 返回 None）
    # 每重排一次就漏一条 `reserved` 预留行：`_day_call_totals` 把 reserved
    # 计入当日额度，而 `_reserve_call` 不看租约是否过期，于是这些行**当天
    # 永久占额**。路由没就绪的那几个小时足以把 120k 额度吃光，之后所有视频
    # 读取一起变成 budget_denied。检查挪到最前面，泄漏的源头就没有了。
    if describer is None:
        raise ReadJobError("没有可用的描述器（worker 未注入 MediaDescriber）")

    # ---------------------------------------------------------- 0. 预算闸
    #
    # 模型调用前先**持久**预留额度（设计 §0 第 4 步）。为什么必须落库：
    # 后台视频作业在 worker 里跑，而 worker 可以多副本。进程内计数器让
    # 每个副本各算一份"今天还能看几个"——N 个副本烧 N 倍的钱，且看不出来。
    #
    # 用户明确要求「次数限制去掉」，所以这里**只有 token 额度**，没有次数上限。
    #
    # ## 为什么闸必须在认领之前
    #
    # 状态机的裁决是「没额度就永远不该进 `running`」，所以
    # `ALLOWED_TRANSITIONS` 里**故意没有** `running -> budget_denied` 这条边，
    # 只有 `pending -> budget_denied`。而认领会把行改成 running，于是
    # "认领之后才发现没额度"这条路根本没有地方落终态。
    #
    # 曾经写成"先认领、再判预算、拿不到就落 budget_denied"，结果只要额度
    # 真的用尽，`commit_read_outcome` 的第一行 `transition(running,
    # budget_denied)` 就必抛 `InvalidJobTransition`。那不是 `ReadJobError`，
    # worker 抓不到，一路抛到 outbox → 退避重试 5 次（每次同一结果）→ dead →
    # 被对账落成 `failed/outbox_dead`。用户永远看不到 `budget_denied`，
    # `chat.py` 里那句「这次额度不够，没有读」也永远不会出现，还白烧 5 次重试。
    #
    # 所以闸放在认领之前：在 `pending` 上判，`pending -> budget_denied` 是合法边。
    reservation = await _reserve_budget(call_id=call_id, lease_owner=lease_owner)
    if not reservation.ok:
        # 拿不到预留：落 `budget_denied` 终态并正常确认事件——**不重排**。
        # 预算不会因为重试而变多，重试只会让同一天的额度被反复探测。
        denied = ReadOutcome(state=JobState.budget_denied, error_code="budget_denied")
        if not await _deny_before_claim(call_id=call_id, outcome=denied):
            # 认领前状态已经变了（别人抢先/已被取消）：如实读现状返回。
            async with session_scope() as session:
                return await _already_settled_outcome(session, call_id)
        return denied
    budget_call_id = reservation.call_id

    # ---------------------------------------------------------- 1. 认领
    async with session_scope() as session:
        claimed = await claim_read_job(
            session, call_id=call_id, lease_owner=lease_owner, now=moment
        )
        if not claimed:
            # 抢不到：作业已经是终态、或者被别的 worker 拿走了。两种都不是
            # 错误——outbox 至少一次投递，重复事件是常态。
            #
            # 但预算**已经预留了**，必须结清：认领失败说明这次根本不该执行，
            # `reserved` 留在当天账上就是永久占额（`_reserve_call` 不看租约）。
            await _settle_budget(call_id=call_id, budget_call_id=budget_call_id, ran=False)
            return await _already_settled_outcome(session, call_id)
        generation = await _lease_generation_of(session, call_id)
        located = await _job_window(session, call_id)
        if located is None:  # pragma: no cover - 刚认领过
            return ReadOutcome(state=JobState.failed, error_code="job_missing")
        window_id, evidence_id, _subject = located

    factory = service_factory
    if factory is None:
        def factory(session):  # noqa: ANN001, ANN202
            from ling.domains.media.service import MediaEvidenceService

            settings = _settings()
            return MediaEvidenceService(session, actor_id=settings.actor_id)

    # ---------------------------------------------------------- 2. 执行前重检
    #
    # **取件之前**要重查三件事：作业还在不在这个窗口、授权还在不在、有没有
    # 被取消。裁决："执行前及提交结果前重新检查窗口、授权、有效期和取消标记"。
    #
    # 这一段查库失败属于"取件类"（还没花钱）：抛出去让 outbox 退避重试。
    try:
        pre = await _pre_execution_check(
            call_id=call_id,
            window_id=window_id,
            evidence_id=evidence_id,
            factory=factory,
        )
    except ReadJobError:
        raise
    except Exception as exc:  # noqa: BLE001 - 查库抖动，还没花钱
        raise ReadJobError(f"执行前检查失败: {type(exc).__name__}") from exc

    if pre is not None:
        # 取消 / 撤权 / 证据消失：**已经有结论**，落终态并正常确认事件。
        await _settle(call_id=call_id, generation=generation, outcome=pre)
        return pre

    # ---------------------------------------------------------- 3. 模型之前
    #
    # `model_started_at` 必须**先提交**再调模型（见 `mark_model_started`）。
    # 写不进去说明租约已被抢走：直接放弃，不要执行。
    if not await mark_model_started(
        call_id, lease_owner=lease_owner, lease_generation=generation
    ):
        # 租约已被抢走，这次不执行了。预留**必须**结清——否则那条 `reserved`
        # 会留在当天账上永久占额（`_reserve_call` 不看租约是否过期）。
        await _settle_budget(call_id=call_id, budget_call_id=budget_call_id, ran=False)
        async with session_scope() as session:
            return await _already_settled_outcome(session, call_id)

    # ---------------------------------------------------------- 4. 调模型 + 写证据
    #
    # `describe_video` 内部会：取件（可能再抛取件类错误——但此时
    # `model_started_at` 已写，**不能再重排**，否则可能第二次收费）→ 调模型 →
    # 走 `MediaEvidenceService.describe` 写回证据。
    #
    # 所以下面的 except 一律翻译成终态，**不**抛。
    try:
        video_reader = describer  # MediaDescriber（worker 注入）
        read = await video_reader.describe_video(  # type: ignore[attr-defined]
            await _evidence_view(factory, evidence_id, window_id)
        )
    except asyncio.CancelledError:
        # **优雅退出**（SIGTERM）会取消这个任务。模型可能已经调用过，
        # 所以记 `unknown`——这正是"模型开始后崩溃"的等价情形。
        #
        # 然后**必须重新抛出**：吞掉 CancelledError 会让关停卡在这里，
        # 而 asyncio 期望它一路传到取消方。落终态是"顺手把账记清"，
        # 不是"把取消变成正常返回"。
        await _settle(
            call_id=call_id,
            generation=generation,
            outcome=ReadOutcome(state=JobState.unknown, error_code="worker_shutdown"),
        )
        # 关停时模型可能已经调用过 → 占额，不退款。
        await _settle_budget(call_id=call_id, budget_call_id=budget_call_id, ran=False)
        raise
    except BaseException as exc:  # noqa: BLE001 - 模型之后的一切都必须落终态
        outcome = classify_describe_error(exc)
        await _settle(call_id=call_id, generation=generation, outcome=outcome)
        # 失败也占额：无法证明模型没被调用过。
        await _settle_budget(call_id=call_id, budget_call_id=budget_call_id, ran=False)
        return outcome

    # ---------------------------------------------------------- 5. 提交结果前再查取消
    #
    # 裁决："晚到的内容禁止成为可消费证据"。`describe_video` 已经把描述写进
    # 证据行了，所以这里要**撤回证据的可读性**——做法是把证据标成终态失败
    # （`mark_describe_failed`），读侧就不会把它的描述当成有效内容。
    try:
        cancelled = await _cancel_requested_after_model(call_id=call_id, factory=factory, evidence_id=evidence_id, window_id=window_id)
    except Exception:  # noqa: BLE001 - 查库失败不改变结论：结果已经拿到
        cancelled = False

    if cancelled:
        outcome = ReadOutcome(
            state=JobState.cancelled, error_code="cancel_requested_during_run"
        )
    else:
        outcome = ReadOutcome(
            state=JobState.succeeded,
            evidence_ref=evidence_id,
            observed=(getattr(read, "text", "") or "")[:200],
        )
    await _settle(call_id=call_id, generation=generation, outcome=outcome)
    # 走到了这里说明模型真的产出过内容（无论结果最终是被采用还是被丢弃）。
    await _settle_budget(call_id=call_id, budget_call_id=budget_call_id, ran=True)
    return outcome


async def _reserve_budget(*, call_id: str, lease_owner: str) -> ReservationResult:
    """在**认领之前**为这次后台读取预留预算。

    独立成函数是因为它必须早于认领调用，而认领在 `run_read_job` 的开头；
    内联会让"先花钱还是先改状态"这个顺序在读代码时看不出来。
    """
    from ling.config import get_settings
    from ling.domains.media.budget import reserve_video_call
    from ling.infra.db import session_scope

    async with session_scope() as budget_session:
        return await reserve_video_call(
            budget_session,
            # actor 是**全局唯一**的角色身份（不随请求变），从设置取即可；
            # `run_read_job` 的入参里没有它，加一个只为传它会把调用点全改一遍。
            actor_id=str(get_settings().actor_id),
            idempotency_key=f"video:{call_id}",
            lease_owner=lease_owner,
        )


async def _deny_before_claim(*, call_id: str, outcome: ReadOutcome) -> bool:
    """在 `pending` 上直接落 `budget_denied`（**不经过** `commit_read_outcome`）。

    为什么不能用 `commit_read_outcome`：那个函数把 `WHERE ... AND
    state='running'` 和 `transition(running, ...)` 都写死了——那是"执行完
    才提交"的栅栏。预算拒绝发生在**认领之前**，行还是 `pending`，走那条路
    只会抛 `InvalidJobTransition`（这正是之前的 P0）。

    这里用条件更新把闸做在同一处：`WHERE state='pending'`，并清掉租约字段
    （此时本来也没有租约，但保持与其它终态一致）。返回 False 表示行已经不是
    `pending` 了——别人抢先执行、或已被取消/清理，此时**不改任何行**，
    让调用方读现状返回。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    transition(JobState.pending, outcome.state)
    moment = _utcnow()
    async with session_scope() as session:
        result = await session.execute(
            update(MediaReadJob)
            .where(
                MediaReadJob.call_id == call_id,
                MediaReadJob.state == JobState.pending.value,
            )
            .values(
                state=outcome.state.value,
                error_code=outcome.error_code,
                result_ref=None,
                completed_at=moment,
                lease_owner=None,
                lease_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        denied = int(getattr(result, "rowcount", 0) or 0) == 1
    if denied:
        emit_job_log(
            "media_read_budget_denied",
            "额度不足，这次没有读（作业未开工即终结）",
            call_id=call_id,
        )
    return denied


async def _settle_budget(*, call_id: str, budget_call_id: str | None, ran: bool) -> None:
    """结清这次预留。**失败与 unknown 都占额**。

    `ran` 只表示"模型真的产出过内容"，不表示成功：被取消、结果被丢弃
    都属于"模型已经调用过"，一样花钱。缺 usage 时 `actual_tokens=None`
    而不虚填 0（设计：缺 usage 保留预留）。

    模块级而不是 `run_read_job` 内嵌：所有早退路径（认领失败、
    `mark_model_started` 失败、取消、异常、成功）都必须走到这里，内嵌时
    定义在预算闸之后，前面两条早退就够不着它——那正是 `reserved` 泄漏的成因。
    """
    if not budget_call_id:
        return
    from ling.domains.media.budget import finish_video_call
    from ling.infra.db import session_scope

    try:
        async with session_scope() as budget_session:
            await finish_video_call(
                budget_session,
                call_id=budget_call_id,
                state="committed" if ran else "failed",
                actual_tokens=None,
            )
    except Exception as exc:  # noqa: BLE001 - 结账失败不能吞掉业务结果
        emit_job_log(
            "media_budget_settle_failed",
            "后台视频预算结清失败",
            call_id=call_id,
            error=type(exc).__name__,
        )


async def _settle(*, call_id: str, generation: int, outcome: ReadOutcome) -> bool:
    """提交终态（独立事务）。返回 False = 租约被抢走，结果已丢弃。"""
    from ling.infra.db import session_scope

    async with session_scope() as session:
        committed = await commit_read_outcome(
            session, call_id=call_id, lease_generation=generation, outcome=outcome
        )
    if not committed:
        emit_job_log(
            "media_read_outcome_discarded",
            "这次读取的结果被丢弃：租约已被别的执行者接走",
            call_id=call_id,
            state=outcome.state.value,
        )
    return committed


async def _already_settled_outcome(session: AsyncSession, call_id: str) -> ReadOutcome:
    """认领失败时读一眼现状（用于返回值，**不改任何行**）。

    这里有个容易写错的地方：**已经 `succeeded` 的作业被重放时，绝不能再调
    一次模型**。所以返回的是"库里的状态"，而不是"去重新执行一遍"。
    """
    from ling.infra.models import MediaReadJob

    row = await session.get(MediaReadJob, call_id)
    if row is None:
        return ReadOutcome(state=JobState.failed, error_code="job_missing")
    state = JobState(row.state)
    if state is JobState.succeeded:
        # 重放：只读已有结果。**零模型调用**。
        return ReadOutcome(
            state=JobState.succeeded,
            evidence_ref=row.result_ref,
            observed=None,
        )
    if state in ACTIVE_STATES:
        # 别人正在跑（或还在排队）：什么都不做。
        return ReadOutcome(state=state, error_code="in_progress_elsewhere")
    return ReadOutcome(state=state, error_code=row.error_code)


async def _pre_execution_check(
    *,
    call_id: str,
    window_id: str,
    evidence_id: str,
    factory: object,
) -> ReadOutcome | None:
    """取件前的三查：取消 / 证据可用 / 授权。命中返回终态，否则 None。

    ## 为什么"授权"不在这里重新判一遍

    撤权后"不许读"这条判据**本来就**在 `MediaEvidenceService.describe` 里
    （描述前重新查 `media.inbound`），所以真正的写入点自带这道门——在这里再
    实现一次两个可能的主体（上传者 / 窗口主人）只会让两处规则走散，而"哪一处
    松了"是最难发现的错。

    这里只判两件**执行特有**的事：

    - 取消：作业自己的状态，证据服务不知道；
    - 证据还在不在这个窗口、有没有过期：`get()` 是既有判据，直接复用。

    撤权的情况会在第 4 步写证据时由服务拒绝，并被
    :func:`classify_describe_error` 翻成 `failed/authorization_revoked`——
    结论一样（不写描述、不留成功），只是判得更晚一点。**晚一点的代价是零**：
    撤权后调模型本身就只是浪费一次调用，不会把内容写进证据。

    查库异常**不**在这里吞：调用方把它当取件类故障抛出去（还没花钱，重排安全）。
    """
    from ling.infra.db import session_scope

    async with session_scope() as session:
        if await _cancel_requested(session, call_id):
            return ReadOutcome(state=JobState.cancelled, error_code="cancel_requested")
        view = await factory(session).get(evidence_id, window_id=window_id)  # type: ignore[operator]
        if view is None:
            return ReadOutcome(state=JobState.expired, error_code="evidence_unavailable")
        return None


async def _evidence_view(factory: object, evidence_id: str, window_id: str) -> object:
    """取 `EvidenceView`（`describe_video` 的入参形状）。"""
    from ling.infra.db import session_scope

    async with session_scope() as session:
        view = await factory(session).get(evidence_id, window_id=window_id)  # type: ignore[operator]
    if view is None:
        from ling.domains.media.errors import MediaRefPermanentError

        raise MediaRefPermanentError("证据在读取过程中消失了")
    return view


async def _cancel_requested_after_model(
    *, call_id: str, factory: object, evidence_id: str, window_id: str
) -> bool:
    """模型之后的取消重检；命中时把证据标成不可消费。

    裁决原文："晚到的内容禁止成为可消费证据"。`describe_video` 已经调过
    `MediaEvidenceService.describe` 把描述写进去了，所以这里用
    `mark_describe_failed` 把它挪出"可读"集合——读侧（第 4 步）只认同窗口、
    已授权、未过期的**成功**证据，标了 `describe_failed_at` 的不会被当成有效内容。

    这是**尽力而为**：撤权/过期时 `mark_describe_failed` 自己也可能失败，
    那时登记一条 WARNING，不改变"作业是 cancelled"的结论。
    """
    from ling.infra.db import session_scope

    async with session_scope() as session:
        if not await _cancel_requested(session, call_id):
            return False
        try:
            await factory(session).mark_describe_failed(  # type: ignore[operator]
                evidence_id, window_id=window_id, reason="读取被取消，结果不得成为证据"
            )
        except Exception as exc:  # noqa: BLE001 - 标记失败不改变结论
            emit_job_log(
                "media_read_cancel_mark_failed",
                "取消之后的证据标记失败（结果仍按取消处理）",
                call_id=call_id,
                error_type=type(exc).__name__,
            )
        return True


def _settings() -> object:
    """延迟取 settings（jobs 是领域模块，不在导入期碰全局配置）。"""
    from ling.config import get_settings

    return get_settings()


def emit_job_log(event: str, message: str, **attributes: object) -> None:
    """一条运行日志（**不带**视频内容/描述正文）。

    为什么另开一个薄函数而不是直接 `logger.warning`：作业的几个关键事件
    （结果被丢弃、取消后标记失败）必须能被日志规则单独盯住，字段名也要稳定。
    """
    import logging

    logging.getLogger("ling.domains.media.jobs").warning(
        "%s | event=%s %s",
        message,
        event,
        " ".join(f"{key}={value}" for key, value in attributes.items()),
    )


# ================================================================== 清理（零模型）


async def expire_overdue_jobs(*, now: datetime | None = None) -> dict[str, int]:
    """清理任务：把"排太久还没开始"的置 `expired`、把"租约过期"的收尾。

    ## 两件事，都是**零模型**

    1. `pending` 且超过 `deadline`（**可空**：用 `requested_at + 15 分钟` 兜底）
       → `expired`。用户不该对着一个永远排不上的作业等。
    2. `running` 且 `lease_until` 已过（执行者崩溃了）：
       - `model_started_at IS NULL` → 回 `pending`（**重新排队**，还没花钱，
         重跑是安全的）；
       - `model_started_at IS NOT NULL` → `unknown`（模型可能收过费，
         **不许**自动再烧一次）。

    第 2 条的两个分支就是裁决"四类崩溃"里第二、三类的收敛器。判据只有
    `model_started_at` 一个——它就是为此存在的那一列。

    ## 为什么不用 `transition()` 之外的路径

    清理走的是 `pending→expired`、`running→unknown`、`running→pending` 里前两条
    合法边（第三条是"回到 pending"，`transition()` 明确不允许）。所以回
    `pending` 不是状态转移，而是**认领失败的回滚**：执行者没留下任何痕迹
    （`model_started_at` 为空），等价于"这次认领没发生"。用条件更新把租约清掉、
    状态改回 `pending`，语义上是一次撤销而不是一次转移——这条注释就是为了让
    下一个人不要"顺手"把 `pending -> pending` 加进转移表。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    moment = now or _utcnow()
    counts = {"expired": 0, "requeued": 0, "unknown": 0}

    async with session_scope() as session:
        # 1) 排队超时。deadline 可空 → COALESCE 到 requested_at + 15 分钟。
        effective_deadline = func.coalesce(
            MediaReadJob.deadline,
            MediaReadJob.requested_at
            + text("interval '15 minutes'"),
        )
        expired = await session.execute(
            update(MediaReadJob)
            .where(
                MediaReadJob.state == JobState.pending.value,
                effective_deadline <= moment,
            )
            .values(
                state=JobState.expired.value,
                error_code="queue_deadline_exceeded",
                completed_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        counts["expired"] = int(getattr(expired, "rowcount", 0) or 0)

        # 2a) 租约过期且**还没开始模型** → 退回 pending（重新执行）
        requeued = await session.execute(
            update(MediaReadJob)
            .where(
                MediaReadJob.state == JobState.running.value,
                MediaReadJob.model_started_at.is_(None),
                MediaReadJob.lease_until.is_not(None),
                MediaReadJob.lease_until < moment,
            )
            .values(
                state=JobState.pending.value,
                started_at=None,
                lease_owner=None,
                lease_until=None,
                # 注意：**不动** lease_generation。它是"第几次被认领"的计数，
                # 回退会让旧执行者的结果提交"重新变有效"——那正是栅栏要防的。
            )
            .execution_options(synchronize_session=False)
        )
        counts["requeued"] = int(getattr(requeued, "rowcount", 0) or 0)

        # 2b) 租约过期且**模型已经开始** → unknown（不自动再烧）
        unknown = await session.execute(
            update(MediaReadJob)
            .where(
                MediaReadJob.state == JobState.running.value,
                MediaReadJob.model_started_at.is_not(None),
                MediaReadJob.lease_until.is_not(None),
                MediaReadJob.lease_until < moment,
            )
            .values(
                state=JobState.unknown.value,
                error_code=UNKNOWN_JOB_CODE,
                completed_at=moment,
                lease_owner=None,
                lease_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        counts["unknown"] = int(getattr(unknown, "rowcount", 0) or 0)

    return counts


async def settle_dead_jobs(*, call_id: str | None = None) -> int:
    """outbox 到 dead 之后，把**还悬空**的业务作业同步终结。

    裁决："沿用最多 5 次失败、指数退避上限 300 秒，到 dead 同步终结业务作业。"
    不终结的话业务作业会永远停在 `pending`——用户看到"已安排，尚未读到结果"，
    而队列里什么都没有了。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import MediaReadJob
    from ling.infra.models import utcnow as _utcnow

    moment = _utcnow()
    async with session_scope() as session:
        stmt = (
            update(MediaReadJob)
            .where(MediaReadJob.state.in_(tuple(s.value for s in ACTIVE_STATES)))
            .values(
                state=JobState.failed.value,
                error_code="outbox_dead",
                completed_at=moment,
                lease_owner=None,
                lease_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if call_id is not None:
            stmt = stmt.where(MediaReadJob.call_id == call_id)
        result = await session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
