"""Retain non-content source forget markers for restore and transcript removal."""

import sqlalchemy as sa
from alembic import op

revision = "0093_memory_forget_markers"
down_revision = "0092_shared_arrangements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_source_forget_markers",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        INSERT INTO memory_source_forget_markers (message_id, subject_id, window_id, turn_id, forgotten_at)
        SELECT id, sender_subject_id, window_id, turn_id, memory_hidden_at
        FROM messages
        WHERE role = 'user' AND sender_subject_id IS NOT NULL AND turn_id IS NOT NULL
          AND memory_hidden_at IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_table("memory_source_forget_markers")
