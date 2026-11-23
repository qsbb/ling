"""Record scoped owner grants and the source of a desktop task."""

import sqlalchemy as sa
from alembic import op

revision = "0088_desktop_activity_grant"
down_revision = "0087_memory_sleep_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("desktop_devices", sa.Column("activity_grant", sa.JSON(), nullable=True))
    op.add_column("desktop_tasks", sa.Column("source", sa.String(16), nullable=False, server_default="manual"))
    op.add_column("desktop_tasks", sa.Column("source_segment_id", sa.String(64), nullable=True))
    op.add_column("desktop_tasks", sa.Column("authorized_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("desktop_tasks", "authorized_until")
    op.drop_column("desktop_tasks", "source_segment_id")
    op.drop_column("desktop_tasks", "source")
    op.drop_column("desktop_devices", "activity_grant")
