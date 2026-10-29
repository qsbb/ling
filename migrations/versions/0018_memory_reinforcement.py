"""记忆强化统计：access_count / last_injected_at

Revision ID: 0018_memory_reinforcement
Revises: 0017_core_memory
Create Date: 2026-10-27

原则：「只有最后真正进入提示词的记录才更新
注入统计与强化分」。这条约束很重要——如果"检索到但被预算截掉"也计分，
被强化的记忆会越来越优先，形成马太效应，长尾记忆永远出不来。

- `access_count`：实际注入次数，参与重要性排序（对数压缩，避免霸榜）
- `last_injected_at`：最后一次实际注入时间

与已有的 `last_recalled_at` 分工：
- last_recalled_at：被**检索到**（含被截断的）
- last_injected_at：真正**进了提示词**
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_memory_reinforcement"
down_revision: str | None = "0017_core_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    if "access_count" not in cols:
        op.add_column(
            "memories",
            sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "last_injected_at" not in cols:
        op.add_column(
            "memories",
            sa.Column("last_injected_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    for name in ("last_injected_at", "access_count"):
        if name in cols:
            op.drop_column("memories", name)
