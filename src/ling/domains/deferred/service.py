"""延后回复的持久化、认领与终结。

本模块只做**状态**：登记、抢占判定、认领（短事务 + 租约 + 代次）、终结。
生成与投递在 `worker.py` 里另开一个新逻辑轮次完成——这里一次模型都不调，
也一次网络都不发。

## 三条不变式（每条都有对应的库级或代码级保证）

1. **同一轮最多一条记录**：`uq_deferred_replies_source_turn` 唯一索引。
   重复登记靠 `IntegrityError` 兜住，不靠进程内去重（多副本时那种去重等于没有）。
2. **到点只有一个 worker 能认领**：条件更新
   `WHERE id=? AND state='pending'` + `claim_generation += 1`。
   `rowcount != 1` 就是没抢到，调用方必须**放弃**而不是继续生成。
3. **抢占是派生的，没有写入点**：见 :func:`input_preempted`。

## 为什么时间都用数据库时间

多机时钟偏差会让"谁先到期"判断错乱。写入 `chosen_due_at` 用的是**调用方传入
的 now**（默认 `utcnow()`），而扫描比较用的是同一个时钟源；跨进程竞争靠的是
条件更新而不是时钟精度，所以这里不需要 `clock_timestamp()`。需要的是**单调
一致**：`chosen_due_at` 一旦写定就不再变（重启后仍按原截止，这是断言 8 的
要求）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.deferred.state import (
    HARD_DEADLINE_SECONDS,
    DeferredReplyState,
    transition,
)
from ling.infra.models import DeferredReply, Message, utcnow

__all__ = [
    "CLAIM_LEASE_SECONDS",
    "PREEMPT_REASON_NEW_INPUT",
    "SUPPORTED_CHANNELS",
    "claim_due",
    "defer_reply_id_for",
    "expire_past_hard_deadline",
    "finish",
    "input_preempted",
    "mark_model_started",
    "pending_for_window",
    "reclaim_expired_leases",
    "schedule",
]

logger = logging.getLogger("ling.domains.deferred")

#: 认领租约。一次生成（上下文重编译 + 模型 + 投递）必须在它之内完成，否则
#: 另一个 worker 会接手同一条记录并**再生成一次**——用户收到两条。
#:
#: 取 240 秒 = 硬截止 60 秒 + 投递与重试余量，且明显大于一轮正常生成。
#: 与 `media_read_jobs` 的 240 秒租约同一量级（那里是"下载 + 抽帧 + 视觉"）。
CLAIM_LEASE_SECONDS = 240

#: 被新输入抢占的原因码（稳定字符串，会落库与进行为日志）。
PREEMPT_REASON_NEW_INPUT = "new_input"

#: **当前支持延后的渠道**。不在这个集合里的渠道 → 到点直接
#: `cancelled(reason=channel_unsupported)`，**不发任何东西**。
#:
#: ## 为什么要一个显式白名单，而不是"反正现在只有 web 用"
#:
#: 延后回复的外发闸（`TurnCoordinator.deliver_gate`）拦的是"消息落库"这一步，
#: 因为 Web 路径上落库就等于送达。但外部渠道（QQ/OneBot）的投递发生在
#: `kernel/bridge.py::_deliver`，那道闸**管不到它**。
#:
#: 也就是说：如果哪天有人让群聊也能延后，那条回复会**绕过**抢占闸直接发到群里
#: ——正是断言 10 禁止的"迟到补话"，而且不会有任何测试变红（当时没有覆盖那条
#: 路径的用例）。注释拦不住人，常量能：不在白名单的渠道根本走不到生成。
#:
#: 选"显式拒绝"而不是"加一条断言"，是因为前者把"当前不支持"变成一个**用户和
#: 日志都能看到的事实**（`reason=channel_unsupported`），而不是一个隐含假设。
#: 将来真要支持群聊时，做的是"把渠道加进来 + 补一条 `_deliver` 前的抢占检查 +
#: 一组群聊用例"，而不是删掉一个碍事的断言。
SUPPORTED_CHANNELS: frozenset[str] = frozenset({"web", ""})


def defer_reply_id_for(source_turn_id: str) -> str:
    """由 `source_turn_id` **稳定派生**主键。

    与 `media/jobs.py::outbox_event_id_for` 同一个理由：随机主键 + 唯一索引只能
    保证"插不进第二条"，而**稳定主键**还能让"重复登记"变成一次无害的主键命中，
    不必依赖异常控制流。两者都有（主键稳定 + 唯一索引），是最省事的一层保险。
    """
    return f"dfr_{source_turn_id}"


async def schedule(
    session: AsyncSession,
    *,
    actor_id: str,
    window_id: str,
    subject_id: str | None,
    source_turn_id: str,
    source_message_id: str | None,
    input_sequence: int,
    applied_delay_ms: int,
    requested_delay_ms: int,
    clamped: bool,
    now: datetime | None = None,
) -> DeferredReply | None:
    """登记一条延后回复。返回**新建**的行；重复登记返回 ``None``。

    调用方（`kernel/turn.py`）只在**解析到合法标记**时调它——代码任何情况下都
    不许自己决定延后。`applied_delay_ms <= 0` 时调用方应当按立即回复处理、根本
    不调这里（"延后 0 秒"不是延后）。

    `chosen_due_at` 用 `now + applied_delay_ms` 算定并**写死**：重启后必须仍按
    原截止（断言 8），所以不能读的时候再算。
    """
    moment = now or utcnow()
    due_at = moment + timedelta(milliseconds=max(0, applied_delay_ms))
    hard_deadline = due_at + timedelta(seconds=HARD_DEADLINE_SECONDS)
    row = DeferredReply(
        id=defer_reply_id_for(source_turn_id),
        actor_id=actor_id,
        window_id=window_id,
        subject_id=subject_id,
        source_turn_id=source_turn_id,
        source_message_id=source_message_id,
        input_sequence=int(input_sequence),
        requested_delay_ms=int(requested_delay_ms),
        applied_delay_ms=int(applied_delay_ms),
        clamped=bool(clamped),
        chosen_due_at=due_at,
        hard_deadline_at=hard_deadline,
        state=DeferredReplyState.pending.value,
        created_at=moment,
        updated_at=moment,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # 同一轮重复登记（重试、或两个入口都解析到标记）。这是**预期**行为，
        # 不是错误：那一轮已经有一条在等了，再排一条会让用户收到两条回复。
        logger.info(
            "延后回复重复登记，忽略 turn=%s window=%s", source_turn_id, window_id
        )
        return None
    return row


async def input_preempted(
    session: AsyncSession, *, window_id: str, input_sequence: int
) -> bool:
    """这个窗口里是否出现了比 `input_sequence` 更晚的**用户输入**。

    ## 为什么是派生而不是"取消代次"列

    新输入的入口分散在 web 路由、入站桥、注意力轮询三处。任何"新输入时顺手
    把某个计数器 +1"的钩子都必然漏掉一处，而漏掉的表现是"她插进新话题补了
    一句"——用户看到的是话题错位，极难复现、极难归因。

    派生没有写入点，也就没有漏写的可能：直接问"还有没有更晚的用户消息"。

    只数 `role='user'`：她自己的回复（`role='actor'`）不算新输入，否则一条
    延后回复自己就会把自己抢占掉。
    """
    latest = await session.scalar(
        select(func.max(Message.sequence)).where(
            Message.window_id == window_id,
            Message.role == "user",
        )
    )
    return int(latest or 0) > int(input_sequence)


async def pending_for_window(
    session: AsyncSession, *, window_id: str
) -> list[DeferredReply]:
    """这个窗口里还没终结的延后记录（诊断/接口用）。"""
    rows = (
        await session.execute(
            select(DeferredReply)
            .where(
                DeferredReply.window_id == window_id,
                DeferredReply.state.in_(
                    [
                        DeferredReplyState.pending.value,
                        DeferredReplyState.claimed.value,
                    ]
                ),
            )
            .order_by(DeferredReply.chosen_due_at)
        )
    ).scalars()
    return list(rows)


async def claim_due(
    session: AsyncSession,
    *,
    lease_owner: str,
    now: datetime | None = None,
) -> DeferredReply | None:
    """认领一条**已到期且未终结**的记录；抢不到返回 ``None``。

    ## 为什么用条件更新而不是先读后写

    两个 worker 同时被叫醒时会读到同一行。条件更新
    （`WHERE id=? AND state='pending'`）让只有一个的 `rowcount == 1`——这是
    "用户只收到一条回复"的**唯一**保证。先读后写在这里必然出错，而且出错时
    两个 worker 都认为自己抢到了。

    ## ⚠️ 真正的栅栏是 `WHERE state='pending'` 这个条件，**不是** `rowcount == 1`

    这条值得单独写下来，因为**很多人会误判**：以为"检查了 rowcount == 1 就
    等于上了乐观锁"。实际上 `UPDATE` 只要匹配到行就返回 `rowcount=1`——条件
    写松了，20 个并发全都会"成功"，而每个都以为自己是唯一的赢家。

    本仓库实测过这一点：把 `WHERE state='pending'` 删掉、只保留
    `rowcount < 1` 的容错判断，`test_only_one_of_many_concurrent_claims_wins`
    立刻变红（20 个认领全部"成功"、`claim_generation` 被加了 20 次）。
    所以下一个人改这段时，**不要**因为"反正有 rowcount 兜底"就放松那个条件。

    ## 为什么认领即提交、生成在事务外

    生成要调模型（几十秒）。把行锁或事务开在那儿，等于让整张表的扫描停摆，
    而且崩溃时那条记录会带着未提交的 `claimed` 一起回滚——看起来像"从没被
    认领过"，于是下一个 worker 又生成一次。

    ## 为什么**不**在这里判抢占

    抢占要在三个时点各比一次（认领时、生成前、外发前），其中后两次在 worker
    里。这里只判"到期"与"没被抢"，是三个时点里的第一个。
    """
    moment = now or utcnow()
    candidate = await session.scalar(
        select(DeferredReply.id)
        .where(
            DeferredReply.state == DeferredReplyState.pending.value,
            DeferredReply.chosen_due_at <= moment,
        )
        .order_by(DeferredReply.chosen_due_at, DeferredReply.id)
        .limit(1)
    )
    if candidate is None:
        return None

    result = await session.execute(
        update(DeferredReply)
        .where(
            DeferredReply.id == candidate,
            DeferredReply.state == DeferredReplyState.pending.value,
            DeferredReply.chosen_due_at <= moment,
        )
        .values(
            state=DeferredReplyState.claimed.value,
            lease_owner=lease_owner,
            lease_until=moment + timedelta(seconds=CLAIM_LEASE_SECONDS),
            claim_generation=DeferredReply.claim_generation + 1,
            claimed_at=moment,
            updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        # 抢输了：另一个 worker 在我读和写之间拿走了它。**放弃**，不要重试
        # 同一行——那会变成两个 worker 抢同一行的活锁。
        logger.info("延后回复认领竞争失败 id=%s owner=%s", candidate, lease_owner)
        return None

    row = await session.get(DeferredReply, candidate)
    if row is not None:
        # 让内存对象与刚写的库状态一致（上面用了 synchronize_session=False）。
        await session.refresh(row)
    return row


async def mark_model_started(
    session: AsyncSession,
    *,
    reply_id: str,
    lease_owner: str,
    claim_generation: int,
    now: datetime | None = None,
) -> bool:
    """**在调模型之前**单独提交 `model_started_at`（崩溃语义的分水岭）。

    与 `media/jobs.py::mark_model_started` 同一理由：崩溃后清理靠这一列判断
    "模型可能已经收过费"，从而**不自动重跑**。如果它和结果在同一个事务里，
    崩溃时一起回滚，重排后就会第二次调用模型、第二次收费。

    调用方必须**先提交这个事务**再调模型（本函数不提交，见 `worker.py` 的
    调用点：它在自己的 `session_scope()` 里，出块即提交）。
    """
    moment = now or utcnow()
    result = await session.execute(
        update(DeferredReply)
        .where(
            DeferredReply.id == reply_id,
            DeferredReply.state == DeferredReplyState.claimed.value,
            DeferredReply.lease_owner == lease_owner,
            DeferredReply.claim_generation == claim_generation,
        )
        .values(model_started_at=moment, updated_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def finish(
    session: AsyncSession,
    *,
    reply_id: str,
    target: DeferredReplyState,
    reason: str | None = None,
    lease_owner: str | None = None,
    claim_generation: int | None = None,
    delivered_turn_id: str | None = None,
    delivered_message_id: str | None = None,
    fallback_used: bool = False,
    now: datetime | None = None,
) -> bool:
    """把一条记录推进到终态（或 `claimed -> pending` 重排）。

    条件更新带 `state` 与（可选）`lease_owner` + `claim_generation`：租约已经被
    别人接走的记录不该再被旧持有者写。返回 ``False`` 表示"没写进去"——那不是
    错误，是"我已经不是这条记录的主人了"（或者它已经终结）。

    `transition()` 在**写之前**调用：非法转移抛异常，一行都不改。这与
    `media/jobs.py` 的分工一致（纯函数定判据，SQL 落库）。

    ## ⚠️ 调用方：一条记录从 `claimed` 到终态只应经过**一个**写入点

    `worker._run_deferred_turn` 只判定、只返回，`worker._settle_deferred` 才
    调本函数写终态。第一版把终态写在各个栅栏里，结果是"抢占"那条路径被写了
    两次（栅栏写 `cancelled`、结算又写 `cancelled`），而同态转移在状态机里是
    **非法**的 → 真跑起来直接抛 `InvalidDeferredTransition`，整条链断掉。

    要新增一条"提前终结"的路径时，改的是 `_settle_deferred` 的入参（让它知道
    为什么终结），不是在这里之外再调一次 `finish`。
    """
    moment = now or utcnow()
    row = await session.get(DeferredReply, reply_id)
    if row is None:
        return False
    current = DeferredReplyState(str(row.state))
    # **先校验、再写**。反过来（先写状态、再判条件）正是上一轮 P0 的成因：
    # 条件不满足时已经改了一半，回滚不掉。
    transition(current, target)

    conditions = [
        DeferredReply.id == reply_id,
        DeferredReply.state == current.value,
    ]
    if lease_owner is not None:
        conditions.append(DeferredReply.lease_owner == lease_owner)
    if claim_generation is not None:
        conditions.append(DeferredReply.claim_generation == claim_generation)

    values: dict[str, object] = {
        "state": target.value,
        "reason": (reason or "")[:64] or None,
        "updated_at": moment,
    }
    if target in {
        DeferredReplyState.delivered,
        DeferredReplyState.cancelled,
        DeferredReplyState.failed,
    }:
        values["finished_at"] = moment
        # 终结即放租约：留着它会让清理任务以为还有人在干活。
        values["lease_owner"] = None
        values["lease_until"] = None
    if target is DeferredReplyState.pending:
        # 重排：清租约、清 model_started_at 以外的东西（model_started_at 必须
        # 已经为空才允许走到这里，由 `reclaim_expired_leases` 保证）。
        values["lease_owner"] = None
        values["lease_until"] = None
    if delivered_turn_id is not None:
        values["delivered_turn_id"] = delivered_turn_id
    if delivered_message_id is not None:
        values["delivered_message_id"] = delivered_message_id
    if fallback_used:
        values["fallback_used"] = True

    result = await session.execute(
        update(DeferredReply)
        .where(*conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def reclaim_expired_leases(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """回收过期租约。两条路径，判据是 `model_started_at`。

    - **为空** → 回 `pending`（还没花钱，可以重新执行）；
    - **非空** → `failed(reason=lease_expired_after_model_start)`（模型可能已经
      收过费，也可能已经产出过内容；自动重跑会二次收费、且用户可能收到两条）。

    这个二分搞反了就是"崩溃后重复烧钱"，所以两条路径各有一条断言。
    """
    moment = now or utcnow()
    expired = (
        await session.execute(
            select(DeferredReply).where(
                DeferredReply.state == DeferredReplyState.claimed.value,
                DeferredReply.lease_until.is_not(None),
                DeferredReply.lease_until <= moment,
            )
        )
    ).scalars()
    counts = {"requeued": 0, "failed": 0}
    for row in list(expired):
        if row.model_started_at is None:
            if await finish(
                session,
                reply_id=row.id,
                target=DeferredReplyState.pending,
                reason="lease_expired_before_model_start",
                now=moment,
            ):
                counts["requeued"] += 1
        elif await finish(
            session,
            reply_id=row.id,
            target=DeferredReplyState.failed,
            reason="lease_expired_after_model_start",
            now=moment,
        ):
            counts["failed"] += 1
    return counts


async def expire_past_hard_deadline(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """到硬截止还没终结的记录 → `failed`（**不发任何东西**）。

    为什么不能留在 `pending`/`claimed` 里：用户看到的是"她答应了要回"，
    而实际上不会有任何回复。留下一条永远不动的记录，等于让界面一直显示
    "正在等她回"，那是最糟的一种假象。

    发兜底短句的那条路径在 `worker.py`（那里才能投递）；本函数只处理
    "连兜底都没机会发"的情况（worker 长期没跑、或兜底本身也过期了）。
    """
    moment = now or utcnow()
    rows = (
        await session.execute(
            select(DeferredReply).where(
                DeferredReply.state.in_(
                    [
                        DeferredReplyState.pending.value,
                        DeferredReplyState.claimed.value,
                    ]
                ),
                DeferredReply.hard_deadline_at <= moment,
            )
        )
    ).scalars()
    count = 0
    for row in list(rows):
        if await finish(
            session,
            reply_id=row.id,
            target=DeferredReplyState.failed,
            reason="hard_deadline_exceeded",
            now=moment,
        ):
            count += 1
    return count
