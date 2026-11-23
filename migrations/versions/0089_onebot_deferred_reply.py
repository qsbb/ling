"""Store the opt-in and the target frozen by a delayed OneBot reply."""

import sqlalchemy as sa
from alembic import op

revision = "0089_onebot_deferred_reply"
down_revision = "0088_desktop_activity_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("windows", sa.Column("onebot_defer_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("deferred_replies", sa.Column("delivery_target", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("deferred_replies", "delivery_target")
    op.drop_column("windows", "onebot_defer_enabled")
