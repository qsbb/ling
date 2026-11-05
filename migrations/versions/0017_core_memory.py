"""核心记忆：每轮常驻，不依赖检索命中

Revision ID: 0017_core_memory
Revises: 0016_knowledge_owner
Create Date: 2026-10-27

参照 Letta 的 core memory blocks 与「我会牢牢记住你」的"核心记忆按 Label
管理、每轮常驻"：边界、禁忌、称呼、重要约定这类内容不能靠 RAG ——
检索没命中就等于角色忘了这些，后果比"少记一件小事"严重得多。

与 memories 表的分工：
- core_memories：少量、人工写入、不衰减、每轮注入
- memories：自动提炼、走检索、有保留期与衰减
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_core_memory"
down_revision: str | None = "0016_knowledge_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "core_memories" in tables:
        return
    op.create_table(
        "core_memories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "origin_kind", sa.String(24), nullable=False, server_default="manual"
        ),
        sa.Column("scope", sa.String(24), nullable=False, server_default="private"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subject_id", "label", name="uq_core_memories_subject_label"),
    )
    op.create_index("ix_core_memories_subject_id", "core_memories", ["subject_id"])
    op.create_index("ix_core_memories_active", "core_memories", ["active"])


def downgrade() -> None:
    if "core_memories" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("core_memories")
