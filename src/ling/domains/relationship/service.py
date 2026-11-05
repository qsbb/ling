"""Relationship 域：人际事件、边界与关系投影。

唯一写入方。规则（设计文档 §6.3）：
- 关系由**有归因**的互动事件驱动；讽刺、转述、身份不明先留暂存解释。
- 情绪可以影响用词，不能放宽权限。
- 关系数值只对 actor 与本人可见，群聊只看到允许外显的姿态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin
from ling.infra.models import RelationshipEvent, RelationshipState, utcnow
from ling.infra.security import new_id

# 事件类型 → (熟悉度增量, 信任增量)。权重只是初值，可被评估调整。
EVENT_EFFECTS: dict[str, tuple[float, float]] = {
    "warmth": (0.02, 0.01),
    "shared_experience": (0.03, 0.02),
    "conflict": (0.0, -0.04),
    "repair": (0.01, 0.03),
    "boundary": (0.0, 0.0),
    "disclosure": (0.02, 0.03),
}

AMBIGUOUS_ORIGINS = {Origin.model_inference}


@dataclass(frozen=True)
class RelationshipView:
    subject_id: str
    familiarity: float
    trust: float
    boundaries: list[str]
    last_interaction_at: datetime | None


class RelationshipService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def state(self, subject_id: str, *, for_update: bool = False) -> RelationshipState:
        """读取关系投影。

        ``for_update=True`` 时加行锁：投影更新是"读出 → 计算 → 写回"，
        多副本并发下两个 worker 同时读到旧值会丢失一次更新
        （真实的并发风险）。加锁保证串行提交。
        """
        stmt = select(RelationshipState).where(
            RelationshipState.actor_id == self._actor_id,
            RelationshipState.subject_id == subject_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            # 并发下两个事务可能同时发现"不存在"，都去 INSERT → 唯一约束冲突。
            # 行锁对不存在的行无效，因此用 ON CONFLICT DO NOTHING + 重读。
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await self._session.execute(
                pg_insert(RelationshipState)
                .values(
                    id=new_id("rel"),
                    actor_id=self._actor_id,
                    subject_id=subject_id,
                    familiarity=0.0,
                    trust=0.0,
                    boundaries=[],
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing(index_elements=[RelationshipState.subject_id])
            )
            await self._session.flush()
            # 重读（此时一定存在；带锁保证后续更新串行）
            stmt2 = select(RelationshipState).where(
                RelationshipState.actor_id == self._actor_id,
                RelationshipState.subject_id == subject_id,
            )
            if for_update:
                stmt2 = stmt2.with_for_update()
            row = (await self._session.execute(stmt2)).scalar_one()
        return row

    async def record_event(
        self,
        *,
        subject_id: str,
        kind: str,
        origin: Origin,
        source_event_ids: list[str],
        note: str | None = None,
        weight: float = 1.0,
        apply_to_projection: bool = True,
        turn_id: str | None = None,
    ) -> RelationshipEvent:
        """记录互动事件。

        ``origin=model_inference`` 或缺少证据的事件**不改变投影**：
        模型推测不能改写关系。这是确定性规则，不由模型决定。
        """
        if kind not in EVENT_EFFECTS:
            raise ValueError(f"未知关系事件类型: {kind}")
        if not source_event_ids:
            raise ValueError("关系事件必须有 source_event_ids")
        if turn_id:
            # 幂等：同一轮同一类互动只记一次，重复处理直接返回既有事件，
            # 不重复放大熟悉度/信任（worker 至少一次处理的必然后果）。
            existing = (
                await self._session.execute(
                    select(RelationshipEvent).where(
                        RelationshipEvent.turn_id == turn_id,
                        RelationshipEvent.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        trusted = origin not in AMBIGUOUS_ORIGINS
        row = RelationshipEvent(
            id=new_id("relev"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            kind=kind,
            weight=weight,
            origin=origin.value,
            source_event_ids=list(source_event_ids),
            note=note,
            occurred_at=utcnow(),
            turn_id=turn_id,
        )
        self._session.add(row)
        if apply_to_projection and trusted:
            # 加行锁：并发 worker 处理同一人的不同轮次时，
            # "读旧值 → 加增量 → 写回" 会丢失更新。
            state = await self.state(subject_id, for_update=True)
            d_fam, d_trust = EVENT_EFFECTS[kind]
            state.familiarity = round(
                min(1.0, max(0.0, float(state.familiarity) + d_fam * weight)), 4
            )
            state.trust = round(min(1.0, max(-1.0, float(state.trust) + d_trust * weight)), 4)
            state.last_interaction_at = utcnow()
        await self._session.flush()
        return row

    async def add_boundary(self, subject_id: str, boundary: str) -> RelationshipView:
        state = await self.state(subject_id)
        boundaries = list(state.boundaries or [])
        if boundary not in boundaries:
            boundaries.append(boundary)
        state.boundaries = boundaries
        await self._session.flush()
        return self.view(state)

    async def remove_boundary(self, subject_id: str, boundary: str) -> RelationshipView:
        state = await self.state(subject_id)
        state.boundaries = [b for b in (state.boundaries or []) if b != boundary]
        await self._session.flush()
        return self.view(state)

    async def recent_events(self, subject_id: str, limit: int = 20) -> list[RelationshipEvent]:
        return list(
            (
                await self._session.execute(
                    select(RelationshipEvent)
                    .where(RelationshipEvent.subject_id == subject_id)
                    .order_by(RelationshipEvent.occurred_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    def view(state: RelationshipState) -> RelationshipView:
        last = state.last_interaction_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return RelationshipView(
            subject_id=state.subject_id,
            familiarity=float(state.familiarity),
            trust=float(state.trust),
            boundaries=list(state.boundaries or []),
            last_interaction_at=last,
        )

    def external_hint(self, view: RelationshipView) -> str:
        """群聊里只允许外显的粗粒度姿态，不暴露数值。"""
        if view.familiarity >= 0.6:
            return "对这个人比较熟悉"
        if view.familiarity >= 0.2:
            return "认识这个人"
        return "还不太熟"
