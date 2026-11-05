"""消息的注意力状态：看没看见

Revision ID: 0033_message_attention
Revises: 0032_interaction_load
Create Date: 2026-10-28

与 `handled_as` 严格分开：

  attention_state=unseen  还没看到（消息在库里，但没进过她的决策）
  attention_state=seen    看过了（进过决策上下文）

  handled_as=replied      看见了并回了
  handled_as=ignored      看见了但选择不回

混用这两个概念会让"没看见"和"看见但没回"再次无法区分——
这正是本项目反复要防的那类静默错误。

历史记录留空（`NULL`），**不回填**：无法判断当时她到底看没看见，
武断填成任何一边都是编造。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_message_attention"
down_revision: str | None = "0032_interaction_load"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")
    }
    if "attention_state" not in columns:
        op.add_column(
            "messages", sa.Column("attention_state", sa.String(24), nullable=True)
        )
    if "seen_at" not in columns:
        op.add_column(
            "messages", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    columns = {
        col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")
    }
    if "seen_at" in columns:
        op.drop_column("messages", "seen_at")
    if "attention_state" in columns:
        op.drop_column("messages", "attention_state")
