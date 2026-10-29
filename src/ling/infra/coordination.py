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
    """停止信号的持久化存储。"""

    def __init__(self, *, poll_seconds: float = STOP_POLL_SECONDS) -> None:
        self._poll = poll_seconds

    async def request_stop(self, turn_id: str) -> None:
        """记录"这一轮请停止"。同一轮重复请求是一次幂等写入。"""
        async with session_scope() as session:
            await _upsert_stop(session, turn_id)

    async def is_stop_requested(self, turn_id: str) -> bool:
        async with session_scope() as session:
            row = await session.get(TurnControlRow, turn_id)
            return row is not None and row.stop_requested_at is not None

    async def clear(self, turn_id: str) -> None:
        async with session_scope() as session:
            await session.execute(delete(TurnControlRow).where(TurnControlRow.turn_id == turn_id))

    async def watch(self, turn_id: str, token: object) -> None:
        """后台轮询：发现停止请求就设置本地令牌。

        轮次结束时由调用方取消这个 task；这里不自行退出，
        避免"刚好在结束前 0.4 秒收到停止"被漏掉。
        """
        setter = getattr(token, "stop", None)
        while True:
            await asyncio.sleep(self._poll)
            try:
                if await self.is_stop_requested(turn_id):
                    if callable(setter):
                        setter()
                    return
            except Exception:  # noqa: BLE001 - 轮询失败不应中断轮次
                continue


async def _upsert_stop(session: AsyncSession, turn_id: str) -> None:
    stmt = (
        pg_insert(TurnControlRow)
        .values(turn_id=turn_id, stop_requested_at=utcnow(), updated_at=utcnow())
        .on_conflict_do_update(
            index_elements=[TurnControlRow.turn_id],
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
        """排队者申请提升：队首且当前空闲/已过期时才成功。"""
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

            if queue and queue[0] == key:
                row.window_id = window_id
                row.turn_id = turn_id
                row.expires_at = expires
                row.queue = queue[1:]
                await session.flush()
                return True
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
