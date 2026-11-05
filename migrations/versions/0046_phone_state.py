"""看手机状态：上次/下次/此刻在看什么

Revision ID: 0046_phone_state
Revises: 0045_turn_control_attempt
Create Date: 2026-11-06

## 为什么需要这张表

要让 ling 像人一样"隔一会儿拿起手机看一眼"，需要三件**跨活动**的事实：

1. 上次看是什么时候（界面上显示「上次看手机 16:50」）
2. 下次什么时候看（显示「下次看手机 17:05」）
3. 此刻在不在看、在看什么

前两条不能用「上次 + 固定间隔」现算：规则是**这次看完了才开始计时**，
而且在忙、在睡、刚聊完都会改间隔。现算的话界面上那个数字每次刷新都不同，
人会看出来是假的。

不塞进 `activities.checkpoint`：那是某个活动自己的进度快照，活动结束就
冻结；看手机跨活动，写日记时也在看，不属于任何一个活动。

## 为什么 next_look_at 带索引

worker 每隔几秒要问一次「有没有到点该看了」，那张表很小（一个 actor 一行），
但索引让这个查询保持 O(1)，将来多角色也不用改。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0046_phone_state"
down_revision: str | None = "0045_turn_control_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phone_states",
        sa.Column("actor_id", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("watching_app", sa.String(48), nullable=True),
        sa.Column("watching_ref", sa.String(64), nullable=True),
        sa.Column("watching_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_looked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_look_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(64), nullable=True),
        sa.Column("last_context", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "ix_phone_states_next_look_at", "phone_states", ["next_look_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_phone_states_next_look_at", table_name="phone_states")
    op.drop_table("phone_states")
