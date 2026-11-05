"""主动机会的实际送达时间

Revision ID: 0024_proactive_sent_at
Revises: 0023_ops_alert_state
Create Date: 2026-10-27

每日主动限额原先按 `created_at` 计日。跨日场景会算错：
23:50 创建、00:10 发送的机会，会占掉**前一天**的额度。

改为按实际送达时间 `sent_at` 计日。历史数据回填为 `created_at`
（没有更好信息，回填既不夸大也不缩小当天用量）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_proactive_sent_at"
down_revision: str | None = "0023_ops_alert_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")}
    if "sent_at" not in cols:
        op.add_column(
            "proactive_opportunities",
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        )
    # 已发送但没有 sent_at 的历史行：用 created_at 回填（保守，不夸大用量）
    op.execute(
        "UPDATE proactive_opportunities SET sent_at = created_at "
        "WHERE state = 'sent' AND sent_at IS NULL"
    )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")}
    if "sent_at" in cols:
        op.drop_column("proactive_opportunities", "sent_at")
