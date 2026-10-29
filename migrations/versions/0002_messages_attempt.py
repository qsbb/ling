"""messages.attempt: 重试不删除已交付文本，按尝试号编组

Revision ID: 0002_messages_attempt
Revises: 0001_baseline
Create Date: 2026-10-27

背景：重试原先会删掉上一轮的 actor 消息，违反 设计文档 §4.3
「已经送达的句子不能假装撤回」。现在保留旧文本并标 attempt，
前端按 (turn_id, attempt) 编组展示"上一段未完成"与"重新回答"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_messages_attempt"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspect.get_columns("messages")}
    if "attempt" not in cols:
        # 已有数据默认 1（第一轮尝试），服务端非空
        op.add_column(
            "messages",
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspect.get_columns("messages")}
    if "attempt" in cols:
        op.drop_column("messages", "attempt")
