"""消息记录发言人

Revision ID: 0042_message_sender
Revises: 0041_memory_source_account
Create Date: 2026-10-29

## 为什么需要这一列

群窗口是**多个人的**。消息本身原来不带发言人，于是：

- 她无法回指（"你刚才问的那个"——哪个"你"？）
- 无法按人使用关系/熟悉度（对谁熟、该不该多聊两句）
- 事后追责/调试时也说不清某句话是谁说的

actor 自己说的话留空（role=actor 已经说明是谁）。

## 为什么不从事件反推

一条消息落库之后，原始 OneBot 事件不会一直留着；而群窗口里
"这条是谁发的"没有任何其他字段能恢复。**必须在写入时记。**
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_message_sender"
down_revision: str | None = "0041_memory_source_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "sender_subject_id" not in columns:
        op.add_column(
            "messages", sa.Column("sender_subject_id", sa.String(64), nullable=True)
        )
        op.create_index(
            "ix_messages_sender_subject_id", "messages", ["sender_subject_id"]
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "sender_subject_id" in columns:
        op.drop_index("ix_messages_sender_subject_id", table_name="messages")
        op.drop_column("messages", "sender_subject_id")
