"""Record verified successful model outline commits.

Revision ID: 0071_outline_success_time
Revises: 0070_voice_reply_projection
"""

import sqlalchemy as sa
from alembic import op

revision = "0071_outline_success_time"
down_revision = "0070_voice_reply_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "day_plans",
        sa.Column("last_model_outline_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("day_plans", "last_model_outline_at")
