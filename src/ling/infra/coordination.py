"""跨实例协调：停止信号与发言租约（不引入 Redis）。

设计文档 §10 明确首版不把 Redis 作为依赖。所以多实例必需的两件事
用已有 PostgreSQL 实现：

1. **停止信号**：在实例 A 点停止，轮次可能跑在实例 B。
   用 ``turn_controls`` 表 + 令牌侧定期轮询（间隔 0.5s，可接受）。

2. **发言租约**：同一角色同时只在一个窗口说话。
   用 ``INSERT ... ON CONFLICT DO UPDATE ... WHERE`` 做原子抢占，
   不需要分布式锁服务；租约到期自动可被抢占，实例崩溃不会永久锁死。

明确的能力降级（不假装跨实例等价）：
- **流式片段续传**：仍限单进程。多实例下 resume 退化为"权威快照"，
  前端仍能看到完整文本，只是没有逐段回放。
- **群发言频率**：仍限单进程。多实例下实际限额约为 单实例限额 × 实例数。
  这是宽松方向的降级（不会更打扰用户），已在 DecisionEngine 注释标注。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.db import session_scope
from ling.infra.models import SpeakLeaseRow, TurnControlRow, utcnow

DEFAULT_LEASE_SECONDS = 180
# 排队上限：超过说明有轮次放弃后没调 leave()。
# 丢最旧而不是拒绝新的——新的还在等，旧的早走了。
MAX_QUEUE_LENGTH = 32
# 停止信号轮询间隔：越短响应越快，但查询越频繁。0.5s 是对话场景的合理折中。
STOP_POLL_SECONDS = 0.5


# ------------------------------------------------------------------ 停止信号


class TurnControlStore:
    """停止信号的持久化存储。

    **信号的作用域是 (turn_id, attempt)，不是 turn_id。**

    retry 复用同一个 ``turn_id``，只把 ``attempt`` 加一。信号若只按 turn_id
    绑定，就变成"与轮次永久绑定"：用户「停止 → 重试」时，新一轮首次轮询
    （``watch`` 先睡 0.5s 再查）就会命中上一轮留下的标记，直接收敛成
    stopped —— 重试多少次都停，唯一逃生口是新一轮 0.5s 内跑完（真实模型
    场景不可能）。

    按 attempt 隔离后两种时序都正确：

    - 「stop → 等旧轮次终结 → retry」：旧标记是 (tid, 1)，新一轮查 (tid, 2)，
      不命中；``_finalize`` 顺手把 (tid, 1) 清掉，库里也不留垃圾。
    - 「stop → 立即 retry（旧轮次还没终结）」：旧标记仍然是 (tid, 1)，旧轮次
      继续轮询 (tid, 1) 因而**照样停得下来**；新一轮查 (tid, 2) 立刻就是
      False。两者互不干扰——这正是方案 2/3 做不到的：只靠终态清理会让
      新一轮继续命中旧标记，retry 前清标记又会让旧轮次永远停不下来。
    """

    def __init__(self, *, poll_seconds: float = STOP_POLL_SECONDS) -> None:
        self._poll = poll_seconds

    async def request_stop(self, turn_id: str, attempt: int = 1) -> None:
        """记录"这一轮（这个 attempt）请停止"。重复请求是一次幂等写入。"""
        async with session_scope() as session:
            await _upsert_stop(session, turn_id, attempt)

    async def is_stop_requested(self, turn_id: str, attempt: int = 1) -> bool:
        async with session_scope() as session:
            row = await session.get(TurnControlRow, (turn_id, int(attempt)))
            return row is not None and row.stop_requested_at is not None

    async def clear(self, turn_id: str, attempt: int | None = None) -> None:
        """清理停止标记。

        ``attempt=None`` 清掉该 turn 的**所有** attempt（运维/调试用）；
        给具体 attempt 时只清那一行（``_finalize`` 走这条路径）。
        """
        async with session_scope() as session:
            stmt = delete(TurnControlRow).where(TurnControlRow.turn_id == turn_id)
            if attempt is not None:
                stmt = stmt.where(TurnControlRow.attempt == int(attempt))
            await session.execute(stmt)

    async def watch(self, turn_id: str, token: object, attempt: int = 1) -> None:
        """后台轮询：发现停止请求就设置本地令牌。

        轮次结束时由调用方取消这个 task；这里不自行退出，
        避免"刚好在结束前 0.4 秒收到停止"被漏掉。
        """
        setter = getattr(token, "stop", None)
        while True:
            await asyncio.sleep(self._poll)
            try:
                if await self.is_stop_requested(turn_id, attempt):
                    if callable(setter):
                        setter()
                    return
            except Exception:  # noqa: BLE001 - 轮询失败不应中断轮次
                continue


async def _upsert_stop(session: AsyncSession, turn_id: str, attempt: int = 1) -> None:
    stmt = (
        pg_insert(TurnControlRow)
        .values(
            turn_id=turn_id,
            attempt=int(attempt),
            stop_requested_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=[TurnControlRow.turn_id, TurnControlRow.attempt],
            set_={"stop_requested_at": utcnow(), "updated_at": utcnow()},
        )
    )
    await session.execute(stmt)


# ------------------------------------------------------------------ 发言租约


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    holder_window_id: str | None = None
    holder_turn_id: str | None = None
    queue_position: int = 0
    reason: str | None = None


class SpeakLeaseStore:
    """发言租约的持久化实现（跨实例语义）。"""

    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._lease_seconds = max(10, lease_seconds)

    async def acquire(self, *, actor_id: str, window_id: str, turn_id: str) -> LeaseResult:
        """原子获取或排队。

        用一个事务完成"读当前 → 判断 → 写"，依赖行级锁保证多实例下不双写。
        """
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._lease_seconds)
        async with session_scope() as session:
            # 锁住这一行（如果存在），避免两个实例同时判定"空闲"
            row = (
                await session.execute(
                    select(SpeakLeaseRow)
                    .where(SpeakLeaseRow.actor_id == actor_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None or _expired(row.expires_at, now):
                # 空闲或已过期：直接占用
                new_row = SpeakLeaseRow(
                    actor_id=actor_id,
                    window_id=window_id,
                    turn_id=turn_id,
                    expires_at=expires,
                    queue=[],
                )
                if row is None:
                    session.add(new_row)
                else:
                    row.window_id = window_id
                    row.turn_id = turn_id
                    row.expires_at = expires
                    row.queue = list(row.queue or [])
                await session.flush()
                return LeaseResult(acquired=True, holder_window_id=window_id,
                                   holder_turn_id=turn_id)

            # 同一轮：续约
            if row.window_id == window_id and row.turn_id == turn_id:
                row.expires_at = expires
                await session.flush()
                return LeaseResult(acquired=True, holder_window_id=window_id,
                                   holder_turn_id=turn_id)

            # 别人持有：入队
            key = f"{window_id}#{turn_id}"
            queue = list(row.queue or [])
            if key not in queue:
                queue.append(key)
                # 上限保护：队列无限增长说明有轮次放弃后没离队。
                # 丢弃最旧的，至少保证新轮次有机会被提升。
                if len(queue) > MAX_QUEUE_LENGTH:
                    queue = queue[-MAX_QUEUE_LENGTH:]
                row.queue = queue
                await session.flush()
            return LeaseResult(
                acquired=False,
                holder_window_id=row.window_id,
                holder_turn_id=row.turn_id,
                queue_position=queue.index(key) + 1,
                reason=(
                    "角色此刻正在另一个窗口说话，已受理并排队"
                    if row.window_id != window_id
                    else "同一窗口上一轮仍在输出，新一轮排队（避免文字交错）"
                ),
            )

    async def try_promote(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        """排队者申请提升：**只有当前无人有效持有**时才成功。

        成功只有两种情形：

        1. 租约已过期（持有者崩溃/卡死）——任何等待者都可以接手，这是崩溃
           恢复路径；
        2. 自己**已经是**持有者——``release()`` 已把队首提升为我，或者就是
           同一轮的续约。

        **「队首」不等于「可以抢」。**

        原实现只要 ``queue[0] == key`` 就无条件改写 ``window_id/turn_id/
        expires_at``，完全不看现持有者的租约是否还有效。而 ``release()``
        本来就会把队首提升为持有者，所以「我是队首 + 别人仍有效持有」这个
        状态**必然出现**：A 正在流式输出，排队者 B 的第一次轮询（0.25s 内）
        就把 A 的租约改写成了自己的 —— 两个窗口同时外发。此后 A 的
        ``extend()`` rowcount=0、``release()`` 也不匹配，A 的租约没有任何
        回收路径，只能等 180s 过期兜底。

        修法：把"队首认领"降级为"只有已经持有才认领"。租约过期时上面的分支
        已经处理，所以走到队首分支时持有者**一定还有效**——此时只能返回
        False，让本轮继续排队（等对方 ``release()`` 提升，或等它过期）。
        """
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._lease_seconds)
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SpeakLeaseRow)
                    .where(SpeakLeaseRow.actor_id == actor_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    SpeakLeaseRow(
                        actor_id=actor_id,
                        window_id=window_id,
                        turn_id=turn_id,
                        expires_at=expires,
                        queue=[],
                    )
                )
                await session.flush()
                return True

            key = f"{window_id}#{turn_id}"
            queue = list(row.queue or [])

            if _expired(row.expires_at, now):
                row.window_id = window_id
                row.turn_id = turn_id
                row.expires_at = expires
                row.queue = [k for k in queue if k != key]
                await session.flush()
                return True

            # 已经是持有者：release() 已把队首提升为我（我的 key 同时被弹出），
            # 或同一轮续约。这里续约并顺手清掉可能残留的队列项。
            #
            # **这一条必须在队首判断之前**：被提升之后我的 key 已不在队列里，
            # 若先判队首就会一直返回 False —— 排队者永远说不了话。
            if row.window_id == window_id and row.turn_id == turn_id:
                row.expires_at = expires
                row.queue = [k for k in queue if k != key]
                await session.flush()
                return True

            # 到这里：持有者是**别人**，且它的租约仍然有效（过期已在上面处理）。
            # 哪怕我是队首，也不许抢 —— 抢走会让两个窗口同时外发。
            return False

    async def leave(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        """放弃排队（等超时的轮次必须调用）。

        不调用会造成**死锁**：`release()` 把队列头提升为持有者，
        但如果那个轮次早已放弃等待并退出，就没有人会再释放它 ——
        队列只增不减，后面所有轮次都被"排队"压住。
        真实数据上堆到 29 条，角色开始不回话。
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SpeakLeaseRow)
                    .where(SpeakLeaseRow.actor_id == actor_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            key = f"{window_id}#{turn_id}"
            queue = [k for k in (row.queue or []) if k != key]
            if len(queue) == len(row.queue or []):
                return False
            row.queue = queue
            await session.flush()
            return True

    async def release(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        """释放并提升队首。"""
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._lease_seconds)
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SpeakLeaseRow)
                    .where(SpeakLeaseRow.actor_id == actor_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.window_id != window_id or row.turn_id != turn_id:
                return False

            key = f"{window_id}#{turn_id}"
            queue = [k for k in (row.queue or []) if k != key]
            if queue:
                head = queue.pop(0)
                next_window, next_turn = head.split("#", 1) if "#" in head else (head, "")
                row.window_id = next_window
                row.turn_id = next_turn
                row.expires_at = expires
                row.queue = queue
            else:
                await session.execute(
                    delete(SpeakLeaseRow).where(SpeakLeaseRow.actor_id == actor_id)
                )
            await session.flush()
            return True

    async def extend(self, *, actor_id: str, window_id: str, turn_id: str) -> bool:
        expires = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
        async with session_scope() as session:
            result = await session.execute(
                update(SpeakLeaseRow)
                .where(
                    SpeakLeaseRow.actor_id == actor_id,
                    SpeakLeaseRow.window_id == window_id,
                    SpeakLeaseRow.turn_id == turn_id,
                )
                .values(expires_at=expires, updated_at=utcnow())
            )
            return bool(getattr(result, "rowcount", 0))

    async def snapshot(self, actor_id: str) -> dict[str, object]:
        async with session_scope() as session:
            row = await session.get(SpeakLeaseRow, actor_id)
            if row is None:
                return {"holder_window_id": None, "holder_turn_id": None, "queue": []}
            if _expired(row.expires_at, datetime.now(UTC)):
                return {"holder_window_id": None, "holder_turn_id": None, "queue": []}
            return {
                "holder_window_id": row.window_id,
                "holder_turn_id": row.turn_id,
                "queue": list(row.queue or []),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }


def _expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return True
    return (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)) <= now


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MAX_QUEUE_LENGTH",
    "STOP_POLL_SECONDS",
    "LeaseResult",
    "SpeakLeaseStore",
    "TurnControlStore",
]
