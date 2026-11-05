"""会话隔离模式与跨会话意识（ADR-0004）

Revision ID: 0004_session_policy_awareness
Revises: 0003_window_summary
Create Date: 2026-10-27

用户需求：隔离模式下会话互不可见；连续模式下角色像一个人，
知道"刚才聊过什么/别的群发生了什么"，但不能被其他会话上下文干扰。

关键设计：跨窗口只传**事实与事件**，绝不传对话状态。
awareness 条目按 audience 在数据层过滤，群聊窗口根本看不到私聊事实。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_session_policy_awareness"
down_revision: str | None = "0003_window_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    tables = set(inspect.get_table_names())
    if "session_policies" not in tables:
        op.create_table(
            "session_policies",
            sa.Column("subject_id", sa.String(64), primary_key=True),
            sa.Column("mode", sa.String(24), nullable=False, server_default="per_window"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "awareness_items" not in tables:
        op.create_table(
            "awareness_items",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("subject_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("audience", sa.String(32), nullable=False),
            sa.Column("source_window_id", sa.String(64), nullable=True),
            sa.Column(
                "source_visibility", sa.String(32), nullable=False, server_default="window_private"
            ),
            sa.Column(
                "origin", sa.String(32), nullable=False, server_default="user_statement"
            ),
            sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_awareness_items_subject_id", "awareness_items", ["subject_id"])
        op.create_index("ix_awareness_items_audience", "awareness_items", ["audience"])
        op.create_index(
            "ix_awareness_subject_kind", "awareness_items", ["subject_id", "kind"]
        )


def downgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    tables = set(inspect.get_table_names())
    if "awareness_items" in tables:
        op.drop_table("awareness_items")
    if "session_policies" in tables:
        op.drop_table("session_policies")
