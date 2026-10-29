"""自我时间线：角色自己「做过什么」。

**唯一写入方**。一条硬规则：

> **只有已完成且有完成证据的活动才能派生经历。**

计划、打算、想象、模型自述都不能写成经历——那是编造人生。
这也是它跟"计划"分开成两张表的原因：`agenda_plans` 存"打算"，
这里只存"确实发生过的"。

隐私：自我经历默认 ``actor_private``。群聊里不能拿它讲
"我昨天做了什么"——那对群里其他人没有意义，还可能带上私聊的因果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ActivityRow, SelfExperienceRow, utcnow
from ling.infra.security import new_id

# 自我经历的可见范围默认 = 角色私有。
# 想让它进私聊上下文，由 SelfView 在**同一受众**里决定是否投影。
DEFAULT_VISIBILITY = "actor_private"

# 单个活动派生一条经历：用活动 id 做去重键，
# 重复完成/重试都只会产生一条。
def dedupe_key_for(activity_id: str) -> str:
    return f"activity:{activity_id}"


@dataclass(frozen=True)
class ExperienceView:
    experience_id: str
    kind: str
    summary: str
    origin: str
    occurred_at: datetime
    source_event_ids: list[str]


class SelfTimelineService:
    """角色自我经历的唯一写入方。"""

    def __init__(self, session: AsyncSession, *, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    async def record_completed_activity(self, activity_id: str) -> ExperienceView | None:
        """从一个**已完成且有证据**的活动派生经历。

        不满足条件时返回 None（不写半条记录）：
        - 活动不存在或不属于本角色
        - 状态不是 completed
        - 没有完成证据
        """
        row = await self._session.get(ActivityRow, activity_id)
        if row is None or row.actor_id != self._actor_id:
            return None
        if row.state != "completed":
            return None
        if not (row.completion_evidence or "").strip():
            # 没有证据就不算做过——宁可少记一条，也不能编
            return None

        key = dedupe_key_for(activity_id)
        existing = (
            await self._session.execute(
                select(SelfExperienceRow).where(SelfExperienceRow.dedupe_key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._view(existing)

        summary = self._summarize(row)
        created = SelfExperienceRow(
            id=new_id("selfexp"),
            actor_id=self._actor_id,
            kind="activity",
            summary=summary,
            source_event_ids=[f"activity:{activity_id}"],
            dedupe_key=key,
            origin=str(row.origin),
            visibility=DEFAULT_VISIBILITY,
            occurred_at=row.ended_at or utcnow(),
        )
        self._session.add(created)
        await self._session.flush()
        return self._view(created)

    async def backfill(self, *, limit: int = 50) -> int:
        """补齐历史：把已完成但还没有经历的活动补上。

        幂等——已经派生过的会因为去重键跳过。
        """
        rows = list(
            (
                await self._session.execute(
                    select(ActivityRow)
                    .where(
                        ActivityRow.actor_id == self._actor_id,
                        ActivityRow.state == "completed",
                    )
                    .order_by(ActivityRow.ended_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        created = 0
        for row in rows:
            # 先查去重键：`record_completed_activity` 对已存在的行也会返回
            # view（便于调用方拿到结果），直接用它计数会把旧行算成新建。
            key = dedupe_key_for(row.id)
            exists = (
                await self._session.execute(
                    select(SelfExperienceRow.id).where(
                        SelfExperienceRow.dedupe_key == key
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            if await self.record_completed_activity(row.id) is not None:
                created += 1
        return created

    async def today(self, *, now: datetime | None = None) -> list[ExperienceView]:
        """今天的经历（按角色本地日）。供"今天做了什么"用。"""
        from ling.domains.agenda.service import AgendaService

        now = now or utcnow()
        routine = await AgendaService(self._session, actor_id=self._actor_id).routine()
        try:
            from zoneinfo import ZoneInfo

            local_now = now.astimezone(ZoneInfo(routine.timezone))
        except Exception:  # noqa: BLE001
            local_now = now
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = list(
            (
                await self._session.execute(
                    select(SelfExperienceRow)
                    .where(
                        SelfExperienceRow.actor_id == self._actor_id,
                        SelfExperienceRow.archived_at.is_(None),
                        SelfExperienceRow.occurred_at >= start_local,
                    )
                    .order_by(SelfExperienceRow.occurred_at)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    async def recent(self, *, days: int = 3, limit: int = 5) -> list[ExperienceView]:
        since = utcnow() - timedelta(days=max(1, days))
        rows = list(
            (
                await self._session.execute(
                    select(SelfExperienceRow)
                    .where(
                        SelfExperienceRow.actor_id == self._actor_id,
                        SelfExperienceRow.archived_at.is_(None),
                        SelfExperienceRow.occurred_at >= since,
                    )
                    .order_by(SelfExperienceRow.occurred_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    # ---------------------------------------------------------- 内部

    @staticmethod
    def _summarize(row: ActivityRow) -> str:
        """把活动写成一句经历。

        **不添加活动没有的信息**：只写 kind 与证据摘要，
        不编造细节（"读完了三章"这种只有证据里才有的东西不能凭空补）。
        """
        evidence = (row.completion_evidence or "").strip().replace("\n", " ")
        kind_label = {
            "reading": "读书",
            "writing": "写东西",
            "resting": "休息",
            "cooking": "做饭",
        }.get(str(row.kind), str(row.kind))
        if evidence:
            return f"{kind_label}：{evidence[:120]}"
        return kind_label

    @staticmethod
    def _view(row: SelfExperienceRow) -> ExperienceView:
        occurred = row.occurred_at
        if occurred is not None and occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        return ExperienceView(
            experience_id=row.id,
            kind=str(row.kind),
            summary=str(row.summary),
            origin=str(row.origin),
            occurred_at=occurred,
            source_event_ids=list(row.source_event_ids or []),
        )


__all__ = [
    "DEFAULT_VISIBILITY",
    "ExperienceView",
    "SelfTimelineService",
    "dedupe_key_for",
]
