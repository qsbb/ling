"""Keep source IDs for each nightly memory review."""

import sqlalchemy as sa
from alembic import op

revision = "0087_memory_sleep_review"
down_revision = "0086_desktop_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_consolidations", sa.Column("review_memory_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_consolidations", "review_memory_ids")
