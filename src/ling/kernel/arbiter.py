"""SpeakerArbiter：同一角色的对外发言仲裁（设计文档 §4.3 / §5）。

问题：多端（Web / QQ / 手机）可以同时发言，每个窗口一个 TurnCoordinator。
如果不仲裁，同一时刻角色可能"同时在两个窗口说话"，这不像一个人。

设计意图：**串行提交与对外发言**，但**不串行模型生成**。
- 到达顺序：入站时分配序号，先到先提交
- 权威提交：ActorSequencer 已有 revision 栅栏
- 对外发言：同时只允许一个窗口在"说"，其他窗口排队但可以先准备候选

实现取舍：本模块只做**发言占用**（短时租约），不阻塞入站。
排队窗口立即得到"已受理、排队中"的状态，而不是干等。

多实例部署时，``SpeakLease`` 需换成数据库行锁/Redis；这里保持单进程语义并在
文档标注，避免假装已经是分布式的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DEFAULT_LEASE_SECONDS = 180


@dataclass
class SpeakLease:
    """一次发言占用。超时自动失效，避免进程崩溃后永久锁死。"""

    window_id: str
    turn_id: str
    acquired_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    )
    queue_position: int = 0

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= now


@dataclass
class AcquireResult:
    acquired: bool
    holder_window_id: str | None = None
    queue_position: int = 0
    reason: str | None = None
    lease: SpeakLease | None = None


class SpeakerArbiter:
    """单进程发言仲裁器。

    语义：
    - 同一 actor 同一时刻只有一个窗口"正在说"
    - 其他窗口**不被拒绝**，而是拿到排队位置（前端显示"已受理，前面还有人"）
    - 占用有租约，超时自动释放，避免崩溃后锁死
    """

    def __init__(self, *, default_lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._lease: SpeakLease | None = None
        self._queue: list[str] = []
        self._lease_seconds = max(10, default_lease_seconds)

    def _current(self) -> SpeakLease | None:
        if self._lease is not None and self._lease.expired():
            self._lease = None
        return self._lease

    def acquire(self, *, window_id: str, turn_id: str) -> AcquireResult:
        """请求发言权。"""
        current = self._current()
        if current is None:
            lease = SpeakLease(
                window_id=window_id,
                turn_id=turn_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            )
            self._lease = lease
            if window_id in self._queue:
                self._queue.remove(window_id)
            return AcquireResult(acquired=True, lease=lease)

        if current.window_id == window_id:
            # 同一窗口的**不同轮次**不能直接替换占用（此前实现的问题）：
            # 上一轮还在说话时，新一轮必须排队，否则两轮的 delta 会交错。
            if current.turn_id == turn_id:
                # 同一轮：续约
                current.expires_at = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
                return AcquireResult(acquired=True, lease=current)
            key = f"{window_id}#{turn_id}"
            if key not in self._queue:
                self._queue.append(key)
            return AcquireResult(
                acquired=False,
                holder_window_id=current.window_id,
                queue_position=self._queue.index(key) + 1,
                reason="同一窗口上一轮仍在输出，新一轮排队（避免两个轮次的文字交错）",
            )

        # 队列 key 一律带 turn_id，提升时才能恢复正确的轮次
        key = f"{window_id}#{turn_id}"
        if key not in self._queue:
            self._queue.append(key)
        return AcquireResult(
            acquired=False,
            holder_window_id=current.window_id,
            queue_position=self._queue.index(key) + 1,
            reason="角色此刻正在另一个窗口说话，已受理并排队",
        )

    def release(self, *, window_id: str, turn_id: str | None = None) -> bool:
        """释放发言权并**把队列里下一个等待者提升为持有者**。

        注意：原先只清空占用，排队者永远拿不到（没有提升逻辑）。
        返回 True 表示确实释放了自己持有的租约。
        """
        current = self._lease
        released = (
            current is not None
            and current.window_id == window_id
            and (turn_id is None or current.turn_id == turn_id)
        )
        if released:
            self._lease = None
        # 清掉自己可能残留的排队项（两种 key 形式）
        self._queue = [
            k for k in self._queue if k != window_id and k != f"{window_id}#{turn_id}"
        ]
        if released:
            self._promote_next()
        return released

    def _promote_next(self) -> None:
        """把队首提升为持有者。"""
        while self._queue and self._lease is None:
            head = self._queue.pop(0)
            next_window, next_turn = head.split("#", 1) if "#" in head else (head, "")
            self._lease = SpeakLease(
                window_id=next_window,
                turn_id=next_turn,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            )
            return

    def try_promote(self, *, window_id: str, turn_id: str) -> bool:
        """排队者主动申请提升：队首且当前空闲时才成功。

        轮次在开始输出前循环调用它，配合短等待实现"排队后仍能说"。
        """
        if self._lease is not None:
            return False
        key = f"{window_id}#{turn_id}"
        if not self._queue:
            # 队列为空（例如前一个持有者崩溃后租约过期）：直接拿
            self._lease = SpeakLease(
                window_id=window_id,
                turn_id=turn_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            )
            return True
        if self._queue[0] != key:
            return False
        self._queue.pop(0)
        self._lease = SpeakLease(
            window_id=window_id,
            turn_id=turn_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
        )
        return True

    def extend(self, *, window_id: str, turn_id: str) -> bool:
        """续约（流式输出期间定期调用，避免长回复被误判超时）。"""
        current = self._current()
        if current is None or current.window_id != window_id or current.turn_id != turn_id:
            return False
        current.expires_at = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
        return True

    def holder(self) -> str | None:
        current = self._current()
        return current.window_id if current else None

    def queue(self) -> list[str]:
        """排队中的 key 列表：``window`` 或 ``window#turn``。"""
        return list(self._queue)

    def snapshot(self) -> dict[str, object]:
        current = self._current()
        return {
            "holder_window_id": current.window_id if current else None,
            "holder_turn_id": current.turn_id if current else None,
            "queue": list(self._queue),
        }


__all__ = ["AcquireResult", "SpeakLease", "SpeakerArbiter"]
