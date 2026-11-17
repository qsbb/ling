"""Keep the latest actual proactive gate evaluation.

Revision ID: 0072_proactive_gate_snapshot
Revises: 0071_outline_success_time
"""

import sqlalchemy as sa
from alembic import op

revision = "0072_proactive_gate_snapshot"
down_revision = "0071_outline_success_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("proactive_opportunities", sa.Column("last_gate_allowed", sa.Boolean()))
    op.add_column("proactive_opportunities", sa.Column("last_gate_reasons", sa.JSON()))
    op.add_column(
        "proactive_opportunities", sa.Column("last_gate_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_proactive_actor_subject_created",
        "proactive_opportunities",
        ["actor_id", "subject_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_proactive_actor_subject_created", table_name="proactive_opportunities")
    op.drop_column("proactive_opportunities", "last_gate_at")
    op.drop_column("proactive_opportunities", "last_gate_reasons")
    op.drop_column("proactive_opportunities", "last_gate_allowed")
