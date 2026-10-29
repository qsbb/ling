"""共处会话持久化

Revision ID: 0037_together_persist
Revises: 0036_attention_poll_log
Create Date: 2026-10-28

`TogetherService` 原来是纯内存字典：**每次进程重启，所有共读/共看/一起听
会话全部丢失**；多 worker 之间也各有一份，互相看不见。

共处会话是"正在一起做一件事"的状态，丢了用户看到的是"刚才那个共读没了"——
而且没有任何错误提示。这是数据丢失，不是性能问题。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_together_persist"
down_revision: str | None = "0036_attention_poll_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "together_sessions" not in tables:
        op.create_table(
            "together_sessions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("host_subject_id", sa.String(64), nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="active"),
            # 每个参与者各自的上报位置。服务端不猜"看到哪了"
            sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_together_sessions_host", "together_sessions", ["host_subject_id"]
        )

    if "together_participants" not in tables:
        op.create_table(
            "together_participants",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(64),
                sa.ForeignKey("together_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("subject_id", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(128), nullable=False),
            sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "session_id", "subject_id", name="uq_together_participant"
            ),
        )
        op.create_index(
            "ix_together_participants_session", "together_participants", ["session_id"]
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "together_participants" in tables:
        op.drop_table("together_participants")
    if "together_sessions" in tables:
        op.drop_table("together_sessions")
