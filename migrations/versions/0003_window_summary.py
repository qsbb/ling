"""window_contexts: 滚动摘要字段（分层上下文）

Revision ID: 0003_window_summary
Revises: 0002_messages_attempt
Create Date: 2026-10-27

设计文档 §6.2：WindowContext 负责"引用、指代、话题、未答问题"，默认闲置 2 小时过期；
更早的对话不能无限塞进提示，也不能直接丢掉导致角色失忆。
这里加滚动摘要：把已滚出原文窗口的早期对话压成一段摘要，
仍属"短时窗口上下文"，不是长期记忆（长期走 Memory/Episode）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_window_summary"
down_revision: str | None = "0002_messages_attempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspect.get_columns("window_contexts")}
    if "summary" not in cols:
        op.add_column("window_contexts", sa.Column("summary", sa.Text(), nullable=True))
    if "summary_through_sequence" not in cols:
        op.add_column(
            "window_contexts",
            sa.Column("summary_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        )
    if "summary_message_count" not in cols:
        op.add_column(
            "window_contexts",
            sa.Column("summary_message_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspect.get_columns("window_contexts")}
    for name in ("summary_message_count", "summary_through_sequence", "summary"):
        if name in cols:
            op.drop_column("window_contexts", name)
