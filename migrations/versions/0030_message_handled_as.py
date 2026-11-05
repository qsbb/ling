"""消息的处置结果：看见了之后做了什么

Revision ID: 0030_message_handled_as
Revises: 0029_media_evidence
Create Date: 2026-10-28

在这之前，QQ 路径判定"沉默"时是在**落库之前**就 return 的——
消息根本没有进数据库。于是"她选择不回"和"她根本没收到"在外面
完全一样，她也不记得有人跟她说过话。

`handled_as` 记录的是「看见之后的处置」，与「回了什么」是两件事：

  replied  已经回应
  ignored  看到了，选择不回（消息仍然在库里，仍然算她见过）

`noted` / `deferred` / `acted` 留给意图层，**不预先编造**。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_message_handled_as"
down_revision: str | None = "0029_media_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")
    }
    if "handled_as" not in columns:
        op.add_column("messages", sa.Column("handled_as", sa.String(24), nullable=True))
    if "handled_reason" not in columns:
        op.add_column("messages", sa.Column("handled_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {
        col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")
    }
    if "handled_reason" in columns:
        op.drop_column("messages", "handled_reason")
    if "handled_as" in columns:
        op.drop_column("messages", "handled_as")
