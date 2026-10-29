"""Affect 域：短时情绪、动机与衰减。

唯一写入方。硬规则：
- 情绪是**短时衰减状态**，不是人格；不能放宽权限。
- 强度随时间线性衰减；无事件不空转调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import AffectState, utcnow
from ling.infra.security import new_id

EMOTIONS = ("calm", "warm", "tired", "curious", "uneasy", "content")


@dataclass(frozen=True)
class AffectView:
    emotion: str
    intensity: float
    updated_at: datetime
    cause: str | None

    def tone_hint(self) -> str:
        """给 ContextCompiler 的**表达提示**，不是权限。"""
        if self.intensity < 0.15:
            return ""
        mapping = {
            "calm": "语气平稳",
            "warm": "语气偏暖",
            "tired": "有点疲倦，回复更短",
            "curious": "好奇，愿意多问一句",
            "uneasy": "有点不安，措辞更谨慎",
            "content": "心情不错",
        }
        return mapping.get(self.emotion, "")


class AffectService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def _rows(self) -> list[AffectState]:
        return list(
            (
                await self._session.execute(
                    select(AffectState).where(AffectState.actor_id == self._actor_id)
                )
            ).scalars()
        )

    async def current(self) -> AffectView:
        """读取**全局**语气趋势（跨窗口可用）。

        window 作用域的情绪带具体原因，绝不跨窗口，因此不在此返回。
        """
        now = datetime.now(UTC)
        rows = [r for r in await self._rows() if (r.scope or "global") == "global"]
        best: AffectView | None = None
        for row in rows:
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            decayed = max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours)
            view = AffectView(
                emotion=row.emotion,
                intensity=round(decayed, 3),
                updated_at=updated,
                cause=row.cause,
            )
            if best is None or view.intensity > best.intensity:
                best = view
        if best is None:
            return AffectView(emotion="calm", intensity=0.0, updated_at=now, cause=None)
        return best

    async def nudge(
        self,
        emotion: str,
        delta: float,
        *,
        cause: str | None = None,
        decay_per_hour: float = 0.15,
        scope: str = "global",
        window_id: str | None = None,
        turn_id: str | None = None,
    ) -> AffectView:
        """改变某种情绪强度（0..1）。同一时刻允许多种情绪共存，读取时取主导。"""
        if emotion not in EMOTIONS:
            raise ValueError(f"未知情绪: {emotion}")
        if scope not in {"global", "window"}:
            raise ValueError(f"未知情绪作用域: {scope}")
        if scope == "window" and not window_id:
            raise ValueError("window 作用域必须提供 window_id")
        query = select(AffectState).where(
            AffectState.actor_id == self._actor_id, AffectState.emotion == emotion
        )
        if scope == "window":
            query = query.where(
                AffectState.scope == "window", AffectState.window_id == (window_id or "")
            )
        else:
            # global 用空串存储（见下方 upsert 注释）
            query = query.where(
                AffectState.scope == "global",
                sa_func.coalesce(AffectState.window_id, "") == "",
            )
        # 行锁：nudge 是"读出 → 按时间衰减 → 加增量 → 写回"，
        # 并发下会丢失更新（此前实现的问题）。加锁保证同一情绪的调整串行。
        row = (await self._session.execute(query.with_for_update())).scalar_one_or_none()
        if row is None:
            # 并发下多个事务同时发现"没有这条情绪"→ 都 INSERT → 唯一约束冲突。
            # 行锁对不存在的行无效，用 ON CONFLICT DO NOTHING + 重读加锁。
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await self._session.execute(
                pg_insert(AffectState)
                .values(
                    id=new_id("aff"),
                    actor_id=self._actor_id,
                    emotion=emotion,
                    intensity=0.0,
                    decay_per_hour=decay_per_hour,
                    scope=scope,
                    # global 用空串而不是 NULL：PostgreSQL 中 NULL 互不相等，
                    # 唯一约束会被绕过（并发实测暴露）。空串让约束真正生效。
                    window_id=(window_id or "") if scope == "window" else "",
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        AffectState.actor_id,
                        AffectState.emotion,
                        AffectState.scope,
                        AffectState.window_id,
                    ]
                )
            )
            await self._session.flush()
            row = (await self._session.execute(query.with_for_update())).scalar_one()

        now = datetime.now(UTC)
        if turn_id and (row.last_turn_id or "") == turn_id:
            # 同一轮重复处理：不再叠加强度，直接返回当前视图（幂等）
            return await self.current()

        # 用**原子 UPDATE** 计算，而不是"读旧值 → Python 加 → 写回"。
        # 此前实现的丢失更新问题：并发事务各读各的基线，后写的覆盖先写的。
        # SQL 里做衰减 + 增量，数据库保证读-改-写是单条语句的原子操作。
        decay_expr = AffectState.intensity - (
            AffectState.decay_per_hour
            * sa_func.greatest(
                0.0,
                sa_func.extract("epoch", sa.literal(now) - AffectState.updated_at) / 3600.0,
            )
        )
        new_intensity = sa_func.least(1.0, sa_func.greatest(0.0, decay_expr + delta))
        values: dict = {
            "intensity": new_intensity,
            "decay_per_hour": decay_per_hour,
            "updated_at": utcnow(),
        }
        if turn_id:
            values["last_turn_id"] = turn_id
        if cause:
            values["cause"] = cause
        await self._session.execute(
            sa_update(AffectState).where(AffectState.id == row.id).values(**values)
        )
        await self._session.flush()
        return await self.current()
        await self._session.flush()
        return await self.current()

    async def for_window(self, window_id: str) -> AffectView | None:
        """读取某窗口的局部情绪（不跨窗口）。"""
        now = datetime.now(UTC)
        rows = [
            r
            for r in await self._rows()
            if (r.scope or "global") == "window" and r.window_id == window_id
        ]
        best: AffectView | None = None
        for row in rows:
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            decayed = max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours)
            if best is None or decayed > best.intensity:
                best = AffectView(
                    emotion=row.emotion,
                    intensity=round(decayed, 3),
                    updated_at=updated,
                    cause=row.cause,
                )
        return best

    async def snapshot(self) -> list[AffectView]:
        now = datetime.now(UTC)
        out: list[AffectView] = []
        for row in await self._rows():
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            out.append(
                AffectView(
                    emotion=row.emotion,
                    intensity=round(max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours), 3),
                    updated_at=updated,
                    cause=row.cause,
                )
            )
        out.sort(key=lambda v: -v.intensity)
        return out
