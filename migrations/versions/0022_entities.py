"""实体与记忆-实体关联

Revision ID: 0022_entities
Create Date: 2026-10-27
Revises: 0021_embedding_composite_pk

参照 MaiBot A-Memorix 的图谱关系召回与 Mem0 的 entity linking：
记忆只记录"用户有什么属性"时，用户问"我妈提过的那个人后来怎样"
只能指望文本碰巧命中。有实体与关联后可以沿关系定位。

- entities：同主体下 name 唯一；aliases 存不同叫法（妈/母亲/老妈）
- memory_entities：多对多，role 说明关系语义
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_entities"
down_revision: str | None = "0021_embedding_composite_pk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "entities" not in tables:
        op.create_table(
            "entities",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("subject_id", sa.String(64), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False, server_default="other"),
            sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("subject_id", "name", name="uq_entities_subject_name"),
        )
        op.create_index("ix_entities_subject_id", "entities", ["subject_id"])
    if "memory_entities" not in tables:
        op.create_table(
            "memory_entities",
            sa.Column("memory_id", sa.String(64), primary_key=True),
            sa.Column("entity_id", sa.String(64), primary_key=True),
            sa.Column("role", sa.String(24), nullable=False, server_default="about"),
        )
        op.create_index(
            "ix_memory_entities_entity", "memory_entities", ["entity_id"]
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "memory_entities" in tables:
        op.drop_table("memory_entities")
    if "entities" in tables:
        op.drop_table("entities")
