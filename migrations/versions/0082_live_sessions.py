"""Add custom ingress instances, bounded live sessions and public viewer notes."""

import sqlalchemy as sa
from alembic import op

revision = "0082_live_sessions"
down_revision = "0081_memory_import_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_connector_instance_scope", "connector_instances", type_="check")
    op.create_check_constraint(
        "ck_connector_instance_scope", "connector_instances",
        "scope IN ('onebot', 'qqbot', 'access', 'custom_http')",
    )
    op.add_column("connector_instances", sa.Column("custom_config", sa.JSON(none_as_null=True)))
    op.create_table(
        "live_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("connector_id", sa.String(16), nullable=False),
        sa.Column("room_id", sa.String(128), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runtime", sa.JSON(), nullable=False),
    )
    op.create_index(
        "uq_live_session_active_actor", "live_sessions", ["actor_id"], unique=True,
        postgresql_where=sa.text("state IN ('preview', 'starting', 'live', 'stopping')"),
    )
    op.create_table(
        "live_viewers",
        sa.Column("connector_id", sa.String(16), primary_key=True),
        sa.Column("room_id", sa.String(128), primary_key=True),
        sa.Column("account_ref", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("live_viewers")
    op.drop_table("live_sessions")
    op.execute("DELETE FROM connector_instances WHERE scope = 'custom_http'")
    op.drop_column("connector_instances", "custom_config")
    op.drop_constraint("ck_connector_instance_scope", "connector_instances", type_="check")
    op.create_check_constraint(
        "ck_connector_instance_scope", "connector_instances", "scope IN ('onebot', 'qqbot', 'access')",
    )
