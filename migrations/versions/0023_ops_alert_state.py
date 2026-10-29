"""告警去重状态

Revision ID: 0023_ops_alert_state
Revises: 0022_entities
Create Date: 2026-10-27

`/v1/health/ops` 解决"能查"，但不解决"会知道"——个人服务器没人天天看仪表盘。
主动推送需要去重：不做的话 worker 挂一小时会推几百条，
很快让人开始忽略通知，真正要处理的那条就淹在噪声里。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_ops_alert_state"
down_revision: str | None = "0022_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "ops_alert_state" in tables:
        return
    op.create_table(
        "ops_alert_state",
        sa.Column("fingerprint", sa.String(128), primary_key=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if "ops_alert_state" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("ops_alert_state")
