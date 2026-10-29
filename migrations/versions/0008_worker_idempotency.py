"""worker 幂等的数据库级保证

Revision ID: 0008_worker_idempotency
Revises: 0007_cross_instance
Create Date: 2026-10-27

outbox 是"至少一次处理"：崩溃、超时、多副本都会导致同一事件被处理两次。
正确做法不是让 handler 做"先查后写"（那有竞态），而是让数据库拒绝重复：

- memory_episodes(turn_id) 唯一 → 同一轮只一条情节
- relationship_events(turn_id, kind) 唯一 → 同一轮同类互动只记一次
- outbox_events.processed_by → 记录哪个实例处理的，便于多副本诊断

历史数据可能已有重复（本轮之前没有约束），因此先按 turn 去重再建约束。
turn_id 为 NULL 的旧行不参与唯一约束（PostgreSQL 中 NULL 互不相等）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_worker_idempotency"
down_revision: str | None = "0007_cross_instance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # --- memory_episodes ---
    cols = {c["name"] for c in inspector.get_columns("memory_episodes")}
    if "turn_id" not in cols:
        op.add_column("memory_episodes", sa.Column("turn_id", sa.String(64), nullable=True))
        op.create_index("ix_memory_episodes_turn_id", "memory_episodes", ["turn_id"])
    # 先清历史重复：同一 turn 只保留最早一条
    op.execute(
        """
        DELETE FROM memory_episodes a
        USING memory_episodes b
        WHERE a.turn_id IS NOT NULL
          AND a.turn_id = b.turn_id
          AND a.created_at > b.created_at
        """
    )
    existing = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_unique_constraints("memory_episodes")
    }
    if "uq_memory_episodes_turn" not in existing:
        op.create_unique_constraint(
            "uq_memory_episodes_turn", "memory_episodes", ["turn_id"]
        )

    # --- relationship_events ---
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("relationship_events")}
    if "turn_id" not in cols:
        op.add_column("relationship_events", sa.Column("turn_id", sa.String(64), nullable=True))
        op.create_index("ix_relationship_events_turn_id", "relationship_events", ["turn_id"])
    op.execute(
        """
        DELETE FROM relationship_events a
        USING relationship_events b
        WHERE a.turn_id IS NOT NULL
          AND a.turn_id = b.turn_id
          AND a.kind = b.kind
          AND a.occurred_at > b.occurred_at
        """
    )
    existing = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_unique_constraints("relationship_events")
    }
    if "uq_relationship_events_turn_kind" not in existing:
        op.create_unique_constraint(
            "uq_relationship_events_turn_kind", "relationship_events", ["turn_id", "kind"]
        )

    # --- outbox：谁处理的 ---
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("outbox_events")}
    if "processed_by" not in cols:
        op.add_column("outbox_events", sa.Column("processed_by", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_constraint("uq_relationship_events_turn_kind", "relationship_events", type_="unique")
    op.drop_constraint("uq_memory_episodes_turn", "memory_episodes", type_="unique")
    for table, col in (
        ("relationship_events", "turn_id"),
        ("memory_episodes", "turn_id"),
        ("outbox_events", "processed_by"),
    ):
        if col in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.drop_column(table, col)
