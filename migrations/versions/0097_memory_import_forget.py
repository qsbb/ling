"""Remember which archive records were forgotten without retaining their contents."""

import sqlalchemy as sa
from alembic import op

revision = "0097_memory_import_forget"
down_revision = "0096_memory_object_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("import_key", sa.String(64)))
    op.execute("""
        UPDATE memories
        SET import_key = encode(sha256(convert_to(
            'astrbot:' || (import_source->>'bot_qq') || ':' ||
            (import_source->>'owner_qq') || ':' || (import_source->>'record_id'),
            'UTF8'
        )), 'hex')
        WHERE import_source->>'source' = 'astrbot_memory_companion'
    """)
    op.execute("""
        WITH proven_sources AS (
            SELECT m.id, min(a.connector_instance || ':' || a.external_account_id) AS label
            FROM memories AS m
            JOIN actor_accounts AS a ON a.external_account_id = m.import_source->>'owner_qq'
            JOIN subjects AS s ON s.id = a.subject_id
            LEFT JOIN windows AS w ON w.id = m.source_window_id AND w.channel = 'onebot'
            WHERE m.import_key IS NOT NULL AND m.source_account IS NULL
              AND a.revoked_at IS NULL AND a.proof IN ('proven', 'admin')
              AND (
                  a.connector_instance = 'onebot-default' OR
                  starts_with(w.connector_window_ref, a.connector_instance || ':')
              )
              AND (
                  s.id = m.subject_id OR (
                      s.merged_into = m.subject_id AND NOT EXISTS (
                          SELECT 1 FROM users AS u WHERE u.subject_id = s.id
                      )
                  )
              )
            GROUP BY m.id HAVING count(*) = 1
        )
        UPDATE memories AS m SET source_account = p.label FROM proven_sources AS p
        WHERE m.id = p.id
    """)
    op.create_index("ix_memories_import_key", "memories", ["import_key"])
    op.create_table(
        "memory_import_forget_markers",
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("memory_import_forget_markers")
    op.drop_index("ix_memories_import_key", table_name="memories")
    op.drop_column("memories", "import_key")
