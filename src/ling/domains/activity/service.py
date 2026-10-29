"""Activity 域：模拟活动进程、检查点与完成证据。

唯一写入方。状态机（设计文档 §6.3）：
``proposed → running → paused/completed/failed/cancelled``

硬规则：
- 做饭被电话打断可暂停，再从检查点恢复。
- 无食材不能说已做完；完成必须有 ``completion_evidence``。
- 无新事件或到期边界时不持续空转调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ActivityRow, utcnow
from ling.infra.security import new_id

VALID_STATES = {"proposed", "running", "paused", "completed", "failed", "cancelled"}

# 允许的状态迁移；其余一律拒绝（确定性硬门，不由模型决定）
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"running", "cancelled", "failed"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "cancelled", "failed"},
    "completed": set(),
    "failed": {"proposed"},
    "cancelled": {"proposed"},
}


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class ActivityView:
    activity_id: str
    kind: str
    state: str
    place_id: str | None
    checkpoint: dict = field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    completion_evidence: str | None = None
    # 来源：simulated（模拟生活）/ tool_receipt（真实回执）/ user_stated
    origin: str = "simulated"
    plan_id: str | None = None
    visibility: str = "subject_private"

    @property
    def is_active(self) -> bool:
        return self.state in {"running", "paused"}


class ActivityService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def propose(
        self,
        *,
        kind: str,
        place_id: str | None = None,
        plan_id: str | None = None,
        origin: str = "simulated",
        source_event_ids: list[str] | None = None,
        visibility: str = "subject_private",
    ) -> ActivityView:
        """新建一个活动。

        ``origin`` 必须如实：``simulated`` 是模拟生活（不能冒充真实经历），
        ``tool_receipt`` 是有真实回执的动作，``user_stated`` 是用户刚说的。
        """
        row = ActivityRow(
            id=new_id("act"),
            actor_id=self._actor_id,
            kind=kind,
            state="proposed",
            place_id=place_id,
            checkpoint={},
            plan_id=plan_id,
            origin=origin,
            source_event_ids=list(source_event_ids or []),
            visibility=visibility,
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row)

    async def _get(self, activity_id: str) -> ActivityRow:
        row = await self._session.get(ActivityRow, activity_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"活动不存在: {activity_id}")
        return row

    async def transition(
        self,
        activity_id: str,
        to_state: str,
        *,
        completion_evidence: str | None = None,
        checkpoint: dict | None = None,
    ) -> ActivityView:
        if to_state not in VALID_STATES:
            raise InvalidTransition(f"未知状态: {to_state}")
        row = await self._get(activity_id)
        allowed = TRANSITIONS.get(row.state, set())
        if to_state not in allowed:
            raise InvalidTransition(f"不允许 {row.state} → {to_state}")
        if to_state == "completed" and not (completion_evidence or "").strip():
            # 没有完成证据就不能标记完成
            raise InvalidTransition(
                "标记 completed 必须提供完成证据（completion_evidence）："
                "计划不等于完成，不能仅凭自称就标记做完"
            )
        row.state = to_state
        if to_state == "running" and row.started_at is None:
            row.started_at = utcnow()
        if to_state in {"completed", "failed", "cancelled"}:
            row.ended_at = utcnow()
        if completion_evidence:
            row.completion_evidence = completion_evidence.strip()[:1000]
        if checkpoint is not None:
            row.checkpoint = dict(checkpoint)
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def save_checkpoint(self, activity_id: str, checkpoint: dict) -> ActivityView:
        row = await self._get(activity_id)
        if row.state != "running":
            raise InvalidTransition("只有 running 的活动可以保存检查点")
        row.checkpoint = dict(checkpoint)
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def current(self) -> ActivityView | None:
        row = (
            await self._session.execute(
                select(ActivityRow)
                .where(
                    ActivityRow.actor_id == self._actor_id,
                    ActivityRow.state.in_(["running", "paused"]),
                )
                .order_by(ActivityRow.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._view(row) if row else None

    async def history(self, limit: int = 20) -> list[ActivityView]:
        rows = list(
            (
                await self._session.execute(
                    select(ActivityRow)
                    .where(ActivityRow.actor_id == self._actor_id)
                    .order_by(ActivityRow.updated_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    @staticmethod
    def _view(row: ActivityRow) -> ActivityView:
        def _utc(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        return ActivityView(
            activity_id=row.id,
            kind=row.kind,
            state=row.state,
            place_id=row.place_id,
            checkpoint=dict(row.checkpoint or {}),
            started_at=_utc(row.started_at),
            ended_at=_utc(row.ended_at),
            completion_evidence=row.completion_evidence,
            origin=str(row.origin),
            plan_id=row.plan_id,
            visibility=str(row.visibility),
        )
