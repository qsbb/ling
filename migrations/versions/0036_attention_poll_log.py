"""注意力轮询记录：她刷到时会怎么判断

Revision ID: 0036_attention_poll_log
Revises: 0035_attention_cursor
Create Date: 2026-10-28

轮询现在是**影子模式**（只决策不发送）。要判断"她的判断像不像人"、
要不要开真实发送，必须有可查的历史——只看日志行是做不了统计的。

记的是**每次轮询**，不是每条消息：

- `unseen_count` / `llm_calls` / `baseline_calls`：成本对照
- `decision` / `reason`：她的结论和她自己给的理由

`baseline_calls` 是"每条消息各调一次"的影子基线，用来算省了多少。
**不存消息正文**：统计不需要正文，存了等于把私聊内容多复制一份到审计表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_attention_poll_log"
down_revision: str | None = "0035_attention_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "attention_poll_log" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "attention_poll_log",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unseen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_attention_poll_log_polled_at", "attention_poll_log", ["polled_at"]
    )
    op.create_index(
        "ix_attention_poll_log_window", "attention_poll_log", ["window_id"]
    )


def downgrade() -> None:
    if "attention_poll_log" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("attention_poll_log")
