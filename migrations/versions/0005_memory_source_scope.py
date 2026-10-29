"""记忆与意识加来源作用域（修跨受众泄漏）

Revision ID: 0005_memory_source_scope
Revises: 0004_session_policy_awareness
Create Date: 2026-10-27

此前实现的真实缺陷：
- worker 提取记忆时固定写 subject_private，群聊内容会变成"该用户的私聊事实"
- recall 对 group_scope 只按可见性过滤，不核对"是否本群"
- MemoryRow 没有来源窗口/群键，因此"群 A 不进群 B"无法实现
- AwarenessItemRow 没有来源事件引用，原事实撤回时无法级联失效

本迁移只加列（nullable 或带默认），不重写既有数据。
已有数据的 source_visibility 保留默认值 subject_private——历史上
`per_window` 模式下的记忆本就是 subject 级，语义一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_memory_source_scope"
down_revision: str | None = "0004_session_policy_awareness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # memories：来源窗口 + 来源可见范围
    cols = _cols("memories")
    if "source_window_id" not in cols:
        op.add_column("memories", sa.Column("source_window_id", sa.String(64), nullable=True))
        op.create_index("ix_memories_source_window_id", "memories", ["source_window_id"])
    if "source_visibility" not in cols:
        op.add_column(
            "memories",
            sa.Column(
                "source_visibility", sa.String(32), nullable=False, server_default="subject_private"
            ),
        )
        op.create_index("ix_memories_source_visibility", "memories", ["source_visibility"])

    # memory_episodes：来源可见范围
    cols = _cols("memory_episodes")
    if "source_visibility" not in cols:
        op.add_column(
            "memory_episodes",
            sa.Column(
                "source_visibility", sa.String(32), nullable=False, server_default="subject_private"
            ),
        )
        op.create_index(
            "ix_memory_episodes_source_visibility", "memory_episodes", ["source_visibility"]
        )

    # awareness_items：来源事件引用 + 同意修订
    cols = _cols("awareness_items")
    if "source_event_ids" not in cols:
        op.add_column(
            "awareness_items",
            sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "consent_revision" not in cols:
        op.add_column(
            "awareness_items",
            sa.Column("consent_revision", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for table, cols in (
        ("awareness_items", ("consent_revision", "source_event_ids")),
        ("memory_episodes", ("source_visibility",)),
        ("memories", ("source_visibility", "source_window_id")),
    ):
        existing = _cols(table)
        for name in cols:
            if name in existing:
                op.drop_column(table, name)
