"""Add independent persona dialogue switches.

Revision ID: 0083_persona_dialogue_options
Baseline: bd5ac914388abce6b7ae2be721dd58d64d7b7ce7
Previous head: 0082_live_sessions
Integration note: keep this revision ID and merge the chain explicitly if
another branch adds a migration from the same parent.
"""

import sqlalchemy as sa
from alembic import op

revision = "0083_persona_dialogue_options"
down_revision = "0082_live_sessions"
branch_labels = None
depends_on = None


_COLUMNS = (
    "anti_prompt_injection",
    "anti_prompt_leak",
    "deny_ai_assistant",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "personas",
            sa.Column(
                name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("personas", name)
