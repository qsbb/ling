"""Goals 域：目标、承诺与履约状态。

唯一写入方。硬规则（设计文档 §1 第 6 条 / §5）：
- 承诺是**持久任务**，直到完成/取消/失败。
- 到期投递必须记录真实回执；平台不支持主动投递时标"无法投递"，不能记成已发送。
- 改期会让旧定时任务失效（由 due_at + state 派生，不需要额外取消）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import CommitmentRow, GoalRow, utcnow
from ling.infra.security import new_id

COMMITMENT_STATES = {"active", "delivered", "missed", "cancelled", "undeliverable"}


@dataclass(frozen=True)
class GoalView:
    goal_id: str
    title: str
    horizon: str
    state: str
    progress: float
    note: str | None


@dataclass(frozen=True)
class CommitmentView:
    commitment_id: str
    subject_id: str
    text: str
    due_at: datetime
    timezone: str
    state: str
    delivery_operation_id: str | None
    delivery_state: str | None
    # 取消原因（撤回承诺时要能解释"为什么没做到"）
    cancel_reason: str | None = None


class GoalsService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    # ---------------------------------------------------------- 目标

    async def create_goal(
        self, *, title: str, horizon: str = "short", note: str | None = None
    ) -> GoalView:
        row = GoalRow(
            id=new_id("goal"),
            actor_id=self._actor_id,
            title=title.strip()[:200],
            horizon=horizon if horizon in {"short", "long"} else "short",
            state="active",
            progress=0.0,
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return self._goal_view(row)

    async def update_goal(
        self, goal_id: str, *, progress: float | None = None, state: str | None = None
    ) -> GoalView:
        row = await self._session.get(GoalRow, goal_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"目标不存在: {goal_id}")
        if progress is not None:
            row.progress = max(0.0, min(1.0, progress))
        if state is not None and state in {"active", "done", "dropped"}:
            row.state = state
        row.updated_at = utcnow()
        await self._session.flush()
        return self._goal_view(row)

    async def list_goals(self, *, state: str | None = None) -> list[GoalView]:
        stmt = select(GoalRow).where(GoalRow.actor_id == self._actor_id)
        if state:
            stmt = stmt.where(GoalRow.state == state)
        rows = list((await self._session.execute(stmt)).scalars())
        return [self._goal_view(r) for r in rows]

    # ---------------------------------------------------------- 承诺

    async def create_commitment(
        self,
        *,
        subject_id: str,
        text: str,
        due_at: datetime,
        timezone: str = "Asia/Shanghai",
        window_id: str | None = None,
        source_event_ids: list[str] | None = None,
    ) -> CommitmentView:
        if due_at.tzinfo is None:
            raise ValueError("承诺到期时间必须带时区")
        row = CommitmentRow(
            id=new_id("cmt"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            window_id=window_id,
            text=text.strip()[:1000],
            due_at=due_at,
            timezone=timezone,
            state="active",
            source_event_ids=list(source_event_ids or []),
        )
        self._session.add(row)
        await self._session.flush()
        return self._commitment_view(row)

    async def reschedule(self, commitment_id: str, new_due: datetime) -> CommitmentView:
        """改期：旧时间失效，仍是同一条承诺（保留证据链）。"""
        if new_due.tzinfo is None:
            raise ValueError("改期时间必须带时区")
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        if row.state in {"cancelled"}:
            raise ValueError("已取消的承诺不能改期")
        row.due_at = new_due
        row.state = "active"
        row.delivery_operation_id = None
        row.delivery_state = None
        row.updated_at = utcnow()
        await self._session.flush()
        return self._commitment_view(row)

    async def cancel(self, commitment_id: str, *, reason: str | None = None) -> CommitmentView:
        """取消承诺（终态）。

        ``COMMITMENT_STATES`` 里声明了 ``cancelled``，``reschedule`` 也检查
        "已取消不能改期"，但**原先没有任何方法能把它设成 cancelled**——
        用户无法撤回一个自己许下的承诺，取消是条死状态。

        取消保留记录（不删除）：承诺是"我说过要做的事"，
        撤回本身也是事实，需要可解释。
        """
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        if row.state == "delivered":
            raise ValueError("已履行的承诺不能取消")
        if row.state == "cancelled":
            return self._commitment_view(row)  # 幂等
        row.state = "cancelled"
        row.delivery_state = "cancelled"
        row.updated_at = utcnow()
        if reason:
            row.cancel_reason = reason[:500]
        await self._session.flush()
        return self._commitment_view(row)

    async def mark_delivery(
        self,
        commitment_id: str,
        *,
        operation_id: str | None,
        delivery_state: str,
    ) -> CommitmentView:
        """记录投递结果。只有 ``sent``/``delivered`` 才算已履行。"""
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        row.delivery_operation_id = operation_id
        row.delivery_state = delivery_state
        if delivery_state in {"sent", "delivered"}:
            row.state = "delivered"
        elif delivery_state == "unsupported":
            row.state = "undeliverable"
        elif delivery_state == "failed":
            row.state = "missed"
        row.updated_at = utcnow()
        await self._session.flush()
        return self._commitment_view(row)

    async def due_commitments(self, *, within: timedelta = timedelta(0)) -> list[CommitmentView]:
        """到期且仍 active 的承诺（供 worker 调度）。"""
        cutoff = datetime.now(UTC) + within
        rows = list(
            (
                await self._session.execute(
                    select(CommitmentRow)
                    .where(
                        CommitmentRow.actor_id == self._actor_id,
                        CommitmentRow.state == "active",
                        CommitmentRow.due_at <= cutoff,
                    )
                    .order_by(CommitmentRow.due_at)
                )
            ).scalars()
        )
        return [self._commitment_view(r) for r in rows]

    async def current_commitment(self, commitment_id: str) -> CommitmentView:
        """读单条承诺当前状态（用于验证投递是否真的推进了）。"""
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        return self._commitment_view(row)

    async def list_commitments(self, subject_id: str | None = None) -> list[CommitmentView]:
        stmt = select(CommitmentRow).where(CommitmentRow.actor_id == self._actor_id)
        if subject_id:
            stmt = stmt.where(CommitmentRow.subject_id == subject_id)
        stmt = stmt.order_by(CommitmentRow.due_at)
        rows = list((await self._session.execute(stmt)).scalars())
        return [self._commitment_view(r) for r in rows]

    @staticmethod
    def _goal_view(row: GoalRow) -> GoalView:
        return GoalView(
            goal_id=row.id,
            title=row.title,
            horizon=row.horizon,
            state=row.state,
            progress=float(row.progress),
            note=row.note,
        )

    @staticmethod
    def _commitment_view(row: CommitmentRow) -> CommitmentView:
        due = row.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return CommitmentView(
            commitment_id=row.id,
            subject_id=row.subject_id,
            text=row.text,
            due_at=due,
            timezone=row.timezone,
            state=row.state,
            delivery_operation_id=row.delivery_operation_id,
            delivery_state=row.delivery_state,
            cancel_reason=row.cancel_reason,
        )
