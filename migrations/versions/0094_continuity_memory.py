"""Link conversation evidence and record confirmed reply delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0094_continuity_memory"
down_revision = "0093_memory_forget_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("delivery_confirmed_at", sa.DateTime(timezone=True)))
    op.add_column("memories", sa.Column("fact_operation", sa.String(16), nullable=False, server_default="assert"))
    op.add_column("memory_episodes", sa.Column("previous_message_id", sa.String(64)))
    op.create_foreign_key(
        "fk_memory_episodes_previous_message", "memory_episodes", "messages",
        ["previous_message_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_memory_episodes_previous_message_id", "memory_episodes", ["previous_message_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_episodes_previous_message_id", table_name="memory_episodes")
    op.drop_constraint("fk_memory_episodes_previous_message", "memory_episodes", type_="foreignkey")
    op.drop_column("memory_episodes", "previous_message_id")
    op.drop_column("memories", "fact_operation")
    op.drop_column("messages", "delivery_confirmed_at")
