"""Record manual window bindings and transactional policy audit events.

Revision ID: 0077_window_origin_binding
Revises: 0076_proactive_draft_binding

Downgrade discards manual-binding markers and conversation-policy audit rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0077_window_origin_binding"
down_revision = "0076_proactive_draft_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "windows",
        sa.Column("origin_manually_bound", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "conversation_policy_audit",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("set_fields", JSONB(), nullable=False),
        sa.Column("clear_fields", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_policy_audit")
    op.drop_column("windows", "origin_manually_bound")
