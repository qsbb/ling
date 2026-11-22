"""Store an optional message count limit for each group conversation."""

import sqlalchemy as sa
from alembic import op

revision = "0085_group_reply_max_parts"
down_revision = "0084_merge_plugin_persona"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("windows", sa.Column("group_reply_max_parts", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("windows", "group_reply_max_parts")
