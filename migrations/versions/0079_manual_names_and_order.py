"""Persist manual person names and conversation presentation preferences."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_manual_names_and_order"
down_revision = "0078_proactive_daily_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column("display_name_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("windows", sa.Column("display_name_override", sa.String(60), nullable=True))
    op.add_column("windows", sa.Column("manual_order", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("windows", "manual_order")
    op.drop_column("windows", "display_name_override")
    op.drop_column("subjects", "display_name_manual")
