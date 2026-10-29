"""注意力调度游标

Revision ID: 0035_attention_cursor
Revises: 0034_unseen_partial_index
Create Date: 2026-10-28

与 `messages.attention_state` 分开：

  messages.attention_state   **消息状态**：她看没看见这一条（逐条，可审计）
  attention_cursor           **调度状态**：下次什么时候看这个窗口

混在一起会有两个坏结果：要么每次轮询扫全部未读来推算时间，
要么把"下次何时看"写进消息行——两者都把简单查询变成全表更新。

`lease_until` 用 `SKIP LOCKED` 防多 worker 重复处理同一窗口。
`focused_until` 是"正在看这个群"的租约（带 TTL 的状态，不是事件）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_attention_cursor"
down_revision: str | None = "0034_unseen_partial_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "attention_cursor" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "attention_cursor",
        sa.Column("window_id", sa.String(64), primary_key=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("focused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mention_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attention_cursor_due", "attention_cursor", ["next_due_at"])


def downgrade() -> None:
    if "attention_cursor" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("attention_cursor")
