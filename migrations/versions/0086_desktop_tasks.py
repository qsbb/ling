"""Keep Windows companion pairing and one-attempt Notepad tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0086_desktop_tasks"
down_revision = "0085_group_reply_max_parts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "desktop_devices",
        sa.Column("actor_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "desktop_tasks",
        sa.Column(
            "actor_id", sa.String(64), sa.ForeignKey("desktop_devices.actor_id"), primary_key=True,
        ),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("step", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("lease_hash", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_desktop_tasks_history", "desktop_tasks", ["actor_id", "created_at"])
    op.create_index(
        "uq_desktop_tasks_active_actor", "desktop_tasks", ["actor_id"], unique=True,
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("desktop_tasks")
    op.drop_table("desktop_devices")
