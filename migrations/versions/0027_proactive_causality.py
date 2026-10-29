"""主动机会的因果来源与结算字段

Revision ID: 0027_proactive_causality
Revises: 0026_routine_agenda
Create Date: 2026-10-27

阶段 4「真正产生主动机会」。原来的机会只有 kind/reason/state/expires_at，
无法回答三个关键问题：**为什么是现在**、**同一件事会不会重复**、
**说出去之后怎么结算**。

- `source_event_id`：必须能追到某个事件。没有来源的不是关心，是随机打扰
- `dedupe_key`：同源同类型只生成一条（唯一索引兜底）
- `reason_code`：结构化原因，与给用户看的 `reason` 文案分开
- `not_before` / `best_until`：什么时候说、什么时候已经晚了
- `reply_expected`：生活分享默认 false——不能因为没人回就追问
- `delivery_operation_id` / `delivery_state`：真实投递结算
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_proactive_causality"
down_revision: str | None = "0026_routine_agenda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_COLUMNS = [
    ("source_event_id", sa.String(128), None),
    ("dedupe_key", sa.String(192), None),
    ("reason_code", sa.String(48), None),
    ("not_before", sa.DateTime(timezone=True), None),
    ("best_until", sa.DateTime(timezone=True), None),
    ("channel_hint", sa.String(64), None),
    ("delivery_operation_id", sa.String(64), None),
    ("delivery_state", sa.String(32), None),
]


def upgrade() -> None:
    cols = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")
    }
    for name, type_, default in NEW_COLUMNS:
        if name not in cols:
            op.add_column(
                "proactive_opportunities",
                sa.Column(name, type_, nullable=True, server_default=default),
            )
    if "visibility" not in cols:
        op.add_column(
            "proactive_opportunities",
            sa.Column(
                "visibility",
                sa.String(32),
                nullable=False,
                server_default="subject_private",
            ),
        )
    if "reply_expected" not in cols:
        op.add_column(
            "proactive_opportunities",
            sa.Column(
                "reply_expected", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    # 去重键唯一：同源同类型只允许一条
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_proactive_dedupe "
        "ON proactive_opportunities (dedupe_key) WHERE dedupe_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_proactive_dedupe")
    cols = {
        c["name"]
        for c in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")
    }
    for name, _type, _default in NEW_COLUMNS:
        if name in cols:
            op.drop_column("proactive_opportunities", name)
    for name in ("visibility", "reply_expected"):
        if name in cols:
            op.drop_column("proactive_opportunities", name)
