"""Store source-linked group arrangements and their immutable contributions."""

import sqlalchemy as sa
from alembic import op

revision = "0092_shared_arrangements"
down_revision = "0091_memory_source_hidden"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shared_arrangements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("window_id", sa.String(64), sa.ForeignKey("windows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_shared_arrangements_window_id", "shared_arrangements", ["window_id"])
    op.create_table(
        "shared_arrangement_evidence",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("arrangement_id", sa.String(64), sa.ForeignKey(
            "shared_arrangements.id", ondelete="CASCADE",
        ), nullable=False),
        sa.Column("previous_id", sa.String(64), nullable=True),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("connector_instance", sa.String(64), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("scheduled_time", sa.String(32), nullable=True),
        sa.Column("place", sa.String(48), nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(action IN ('proposed', 'rescheduled') AND scheduled_time IS NOT NULL AND place IS NOT NULL)"
            " OR (action IN ('confirmed', 'cancelled', 'completed') AND scheduled_time IS NULL AND place IS NULL)",
            name="ck_shared_arrangement_contribution",
        ),
    )
    for column in ("arrangement_id", "subject_id"):
        op.create_index(f"ix_shared_arrangement_evidence_{column}", "shared_arrangement_evidence", [column])


def downgrade() -> None:
    op.drop_table("shared_arrangement_evidence")
    op.drop_table("shared_arrangements")
