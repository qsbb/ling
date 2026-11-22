"""Independent migration for Ling external plugin system v1.

Baseline: 0082_live_sessions (at bd5ac914388abce6b7ae2be721dd58d64d7b7ce7).
This revision uses an independent ID so the integration owner can graft it into
a single final migration chain without colliding with parallel branches.
"""

import sqlalchemy as sa
from alembic import op

revision = "0083_external_plugins_v1"
down_revision = "0082_live_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_plugins",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("api_version", sa.String(32), nullable=False),
        sa.Column("min_host_version", sa.String(32), nullable=False),
        sa.Column("max_host_version", sa.String(32), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("commands", sa.JSON(), nullable=False),
        sa.Column("granted_capabilities", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(24), nullable=False, server_default="registered"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_plugin_state",
        sa.Column("plugin_id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("external_plugin_state")
    op.drop_table("external_plugins")
