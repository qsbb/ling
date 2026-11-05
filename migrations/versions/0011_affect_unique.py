"""affect_states 唯一键（并发 upsert 的前提）

Revision ID: 0011_affect_unique
Revises: 0010_affect_turn_id
Create Date: 2026-10-27

此前实现的并发丢失更新风险：nudge 是"读出 → 衰减 → 加增量 → 写回"。
只加行锁不够——行不存在时锁无效，两个事务会同时 INSERT。

用 ON CONFLICT DO NOTHING + 重读需要唯一约束。
键为 (actor_id, emotion, scope, window_id)：同一角色同一情绪在同一作用域
（global 或某个 window）只有一条记录。

历史数据先去重再建约束。注意 window_id 可为 NULL，
PostgreSQL 中 NULL 互不相等，因此 global 行的唯一性由 (actor, emotion, scope)
+ NULL 共同保证（实际上仍可能出现多条 global，先清理）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_affect_unique"
down_revision: str | None = "0010_affect_turn_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 去重：同一 (actor, emotion, scope, window) 保留 intensity 最高的一条
    op.execute(
        """
        DELETE FROM affect_states a
        USING affect_states b
        WHERE a.actor_id = b.actor_id
          AND a.emotion = b.emotion
          AND a.scope = b.scope
          AND COALESCE(a.window_id, '') = COALESCE(b.window_id, '')
          AND a.intensity < b.intensity
        """
    )
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("affect_states")
    }
    if "uq_affect_actor_emotion_scope" not in existing:
        op.create_unique_constraint(
            "uq_affect_actor_emotion_scope",
            "affect_states",
            ["actor_id", "emotion", "scope", "window_id"],
        )


def downgrade() -> None:
    op.drop_constraint("uq_affect_actor_emotion_scope", "affect_states", type_="unique")
