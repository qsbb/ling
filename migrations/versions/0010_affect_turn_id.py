"""affect_states.last_turn_id：情绪调整的幂等标记

Revision ID: 0010_affect_turn_id
Revises: 0009_memory_turn_id
Create Date: 2026-10-27

此前实现的缺陷：relationship.update 里 record_event 幂等返回后，
handler 仍会继续调整情绪 → 重复处理不断叠加强度。

这里给情绪加 last_turn_id：同一 turn 对同一 (emotion, scope, window)
只调整一次，重试直接返回当前视图。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_affect_turn_id"
down_revision: str | None = "0009_memory_turn_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("affect_states")}
    if "last_turn_id" not in cols:
        op.add_column("affect_states", sa.Column("last_turn_id", sa.String(64), nullable=True))


def downgrade() -> None:
    if "last_turn_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("affect_states")}:
        op.drop_column("affect_states", "last_turn_id")
