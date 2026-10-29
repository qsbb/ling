"""knowledge_items 归属与可见范围

Revision ID: 0016_knowledge_owner
Revises: 0015_commitment_cancel
Create Date: 2026-10-27

此前实现的缺口：知识条目没有 owner/visibility 字段，而 source_kind
允许 ``user_statement``（"用户说他住在杭州"这类个人自述）。
这种条目全局可见 = 把一个人的自述喂给另一个人。

- ``owner_subject_id`` NULL 表示公共知识（任何人都能读）
- 非 NULL 表示仅该 subject 可读

历史数据全部置为 NULL（公共），保持现有行为不变，避免升级后反而查不到。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_knowledge_owner"
down_revision: str | None = "0015_commitment_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("knowledge_items")}
    if "owner_subject_id" not in cols:
        op.add_column(
            "knowledge_items", sa.Column("owner_subject_id", sa.String(64), nullable=True)
        )
        op.create_index(
            "ix_knowledge_items_owner_subject_id", "knowledge_items", ["owner_subject_id"]
        )
    if "visibility" not in cols:
        op.add_column(
            "knowledge_items",
            sa.Column(
                "visibility",
                sa.String(32),
                nullable=False,
                server_default="subject_private",
            ),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("knowledge_items")}
    for name in ("visibility", "owner_subject_id"):
        if name in cols:
            op.drop_column("knowledge_items", name)
