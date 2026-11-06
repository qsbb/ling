"""供应商每日 token 用量表

Revision ID: 0048_provider_token_usage
Revises: 0047_persona_scenarios
Create Date: 2026-11-06

## 为什么需要这张表

供应商页要新增「日限额（tokens）」，而在这之前**额度只存不判**：
`ProfileBinding.daily_cost_limit` 存进了修订、也在界面上显示，但全仓
没有任何一处代码读它做判断。也就是说那个输入框当时只是个装饰。

要让"超了就不再用这一家"真的成立，必须先有**运行事实**：这家今天已经用了
多少 token。那不是配置，是每次调用后都会变的计数，所以单独一张表。

## 为什么主键是 (provider_id, day)

额度是"这家一天能用多少"，同一家下面五个职责共用一份。按职责分行的话，
判断超限前必须先跨行求和，而这是每轮调用都要做的事。

## 为什么 day 是 DATE

额度按**本地日历日**清零。存日期而不是时间戳，跨天就只是一次主键失配，
不需要任何定时清理任务。

## 旧数据

不回填。迁移前没有记账，任何回填出来的数字都是编的——显示"今天已用 0"
比显示一个凭空的数字诚实。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048_provider_token_usage"
down_revision: str | None = "0047_persona_scenarios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_token_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("provider_id", "day", name="uq_provider_token_day"),
    )
    op.create_index(
        "ix_provider_token_usage_provider_id", "provider_token_usage", ["provider_id"]
    )
    op.create_index("ix_provider_token_usage_day", "provider_token_usage", ["day"])


def downgrade() -> None:
    op.drop_index("ix_provider_token_usage_day", table_name="provider_token_usage")
    op.drop_index(
        "ix_provider_token_usage_provider_id", table_name="provider_token_usage"
    )
    op.drop_table("provider_token_usage")
