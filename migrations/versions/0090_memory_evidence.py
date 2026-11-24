"""Link new conversational memories to their original user message."""

import sqlalchemy as sa
from alembic import op

revision = "0090_memory_evidence"
down_revision = "0089_onebot_deferred_reply"
branch_labels = None
depends_on = None

TABLES = ("memories", "memory_episodes")


def upgrade() -> None:
    op.add_column(
        "memories", sa.Column("fact_phase", sa.String(16), nullable=False, server_default="current")
    )
    op.drop_constraint("uq_memories_turn_slot", "memories", type_="unique")
    op.create_unique_constraint(
        "uq_memories_turn_slot_phase", "memories", ["turn_id", "slot", "fact_phase"]
    )
    for table in TABLES:
        op.add_column(table, sa.Column("source_message_id", sa.String(64), nullable=True))
        op.create_index(f"ix_{table}_source_message_id", table, ["source_message_id"])


def downgrade() -> None:
    # Historical rows introduced by this revision prevent a destructive schema rollback.
    op.create_unique_constraint("uq_memories_turn_slot", "memories", ["turn_id", "slot"])
    op.drop_constraint("uq_memories_turn_slot_phase", "memories", type_="unique")
    op.drop_column("memories", "fact_phase")
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_source_message_id", table_name=table)
        op.drop_column(table, "source_message_id")
