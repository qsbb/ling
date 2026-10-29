"""commitments.cancel_reason：承诺取消原因

Revision ID: 0015_commitment_cancel
Revises: 0014_memory_archive
Create Date: 2026-10-27

`COMMITMENT_STATES` 与 `reschedule` 都假定存在 cancelled 终态，
但没有任何代码路径能设置它——用户无法撤回承诺。
补上 cancel() 与原因字段；撤回本身是需要可解释的事实。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_commitment_cancel"
down_revision: str | None = "0014_memory_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("commitments")}
    if "cancel_reason" not in cols:
        op.add_column("commitments", sa.Column("cancel_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    if "cancel_reason" in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("commitments")
    }:
        op.drop_column("commitments", "cancel_reason")
