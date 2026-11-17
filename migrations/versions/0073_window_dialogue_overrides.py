"""Persist Web private-session dialogue overrides.

Revision ID: 0073_window_dialogue_overrides
Revises: 0072_proactive_gate_snapshot
"""

import sqlalchemy as sa
from alembic import op

revision = "0073_window_dialogue_overrides"
down_revision = "0072_proactive_gate_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dialogue_config_guard",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "window_dialogue_overrides",
        sa.Column("window_id", sa.String(64), sa.ForeignKey("windows.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("private_may_stay_silent", sa.Boolean(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("window_dialogue_overrides")
    op.drop_table("dialogue_config_guard")
