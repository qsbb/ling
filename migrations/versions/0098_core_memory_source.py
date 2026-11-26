"""Keep a fixed memory tied to the lifetime of its original record."""

import sqlalchemy as sa
from alembic import op

revision = "0098_core_memory_source"
down_revision = "0097_memory_import_forget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("core_memories", sa.Column("source_memory_id", sa.String(64)))
    op.create_foreign_key(
        "fk_core_memories_source_memory", "core_memories", "memories",
        ["source_memory_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "core_memories_source_memory_id_key", "core_memories", ["source_memory_id"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM core_memories WHERE source_memory_id IS NOT NULL")
    op.drop_constraint("core_memories_source_memory_id_key", "core_memories", type_="unique")
    op.drop_constraint("fk_core_memories_source_memory", "core_memories", type_="foreignkey")
    op.drop_column("core_memories", "source_memory_id")
