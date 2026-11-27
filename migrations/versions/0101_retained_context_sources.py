"""Track recall sources used in retained replies and rolling summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0101_retained_context_sources"
down_revision = "0100_media_consent_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("context_sources", ARRAY(sa.String())))
    op.add_column("window_contexts", sa.Column("context_sources", ARRAY(sa.String())))


def downgrade() -> None:
    op.drop_column("window_contexts", "context_sources")
    op.drop_column("messages", "context_sources")
