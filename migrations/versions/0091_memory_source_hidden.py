"""Keep forgotten source messages out of model history while retaining the transcript."""

import sqlalchemy as sa
from alembic import op

revision = "0091_memory_source_hidden"
down_revision = "0090_memory_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("memory_hidden_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "memory_hidden_at")
