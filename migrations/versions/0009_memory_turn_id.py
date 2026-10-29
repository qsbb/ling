"""memories 幂等键：(turn_id, text) 唯一

Revision ID: 0009_memory_turn_id
Revises: 0008_worker_idempotency
Create Date: 2026-10-27

此前实现的漏洞：Episode 幂等后 Assertion 仍会重复写入。
同一轮重试会不断长出同一条"用户的名字是 X"。

历史数据先去重（同 turn 同文本保留最早一条），再建唯一约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_memory_turn_id"
down_revision: str | None = "0008_worker_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    if "turn_id" not in cols:
        op.add_column("memories", sa.Column("turn_id", sa.String(64), nullable=True))
        op.create_index("ix_memories_turn_id", "memories", ["turn_id"])
    op.execute(
        """
        DELETE FROM memories a
        USING memories b
        WHERE a.turn_id IS NOT NULL
          AND a.turn_id = b.turn_id
          AND a.text = b.text
          AND a.created_at > b.created_at
        """
    )
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("memories")
    }
    if "uq_memories_turn_text" not in existing:
        op.create_unique_constraint("uq_memories_turn_text", "memories", ["turn_id", "text"])


def downgrade() -> None:
    op.drop_constraint("uq_memories_turn_text", "memories", type_="unique")
    if "turn_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}:
        op.drop_column("memories", "turn_id")
