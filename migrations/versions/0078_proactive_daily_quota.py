"""Persist per-subject, local-day proactive quota reservations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_proactive_daily_quota"
down_revision = "0077_window_origin_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        raise RuntimeError("主动联系日额度预留需要 PostgreSQL")
    op.create_table(
        "proactive_quota_reservations",
        sa.Column(
            "opportunity_id",
            sa.String(64),
            sa.ForeignKey("proactive_opportunities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'sent', 'uncertain', 'released')",
            name="ck_proactive_quota_reservation_state",
        ),
    )
    op.create_index(
        "ix_proactive_quota_subject_day_state",
        "proactive_quota_reservations",
        ["actor_id", "subject_id", "local_day", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proactive_quota_subject_day_state",
        table_name="proactive_quota_reservations",
    )
    op.drop_table("proactive_quota_reservations")
