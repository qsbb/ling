"""记忆归档字段（保留期到期后归档而非删除）

Revision ID: 0014_memory_archive
Revises: 0013_memory_slot_key
Create Date: 2026-10-27

设计文档 §6.2 要求记忆"可更正、可删除"，§10 要求"恢复前核对撤回墓碑"。
本迁移只加归档能力，不删任何数据：

- memories.archived_at / archive_reason：保留期到期后归档（不参与检索）
- memories.last_recalled_at：最近召回时间（"长期不用"的依据）
- memory_episodes.archived_at：情节同样归档

为什么归档而不是 DELETE：superseded_by 链与 Episode.source_event_ids 都引用 id，
物理删除会断链；且备份恢复可能让已删内容复活（墓碑检查是另一层防线）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_memory_archive"
down_revision: str | None = "0013_memory_slot_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    for name, col in (
        ("archived_at", sa.DateTime(timezone=True)),
        ("archive_reason", sa.String(64)),
        ("last_recalled_at", sa.DateTime(timezone=True)),
    ):
        if name not in cols:
            op.add_column("memories", sa.Column(name, col, nullable=True))
    if "archived_at" not in cols:
        op.create_index("ix_memories_archived_at", "memories", ["archived_at"])

    ep_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memory_episodes")}
    if "archived_at" not in ep_cols:
        op.add_column(
            "memory_episodes", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index("ix_memory_episodes_archived_at", "memory_episodes", ["archived_at"])


def downgrade() -> None:
    if "archived_at" in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("memory_episodes")
    }:
        op.drop_column("memory_episodes", "archived_at")
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    for name in ("last_recalled_at", "archive_reason", "archived_at"):
        if name in cols:
            op.drop_column("memories", name)
