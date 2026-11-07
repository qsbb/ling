"""每个 subject 的用户偏好表（时区）

Revision ID: 0049_user_preferences
Revises: 0048_provider_token_usage
Create Date: 2026-11-07

## 为什么需要这张表

时区原来只有一个来源：环境变量 ``LING_USER_TIMEZONE``。没设时 `proactive`
的门控直接判定"不知道你的时区"，于是**整条主动联系停掉**；总览的「今天」
边界退回 UTC；而用户在设置页里找不到任何地方可以填。

## 为什么不进 config_revisions

``config_revisions.payload`` 的语义是"我们怎么跑"，它跟着发布/回滚走。
时区是**用户的事实**：回滚到旧修订不该把"我在东八区"一起回滚掉；反过来，
改一次时区也不该触发一次模型配置的发布（发布失败会让勿扰时段整段算错）。

所以照 `SessionPolicyRow`（会话隔离模式）的做法：按 subject 主键独立成表。

## 为什么 timezone 允许 NULL

NULL = **没设过**，解析时回退到环境变量、再回退到 ``Asia/Shanghai``。
空字符串也按"没设过"处理（见 ``ling/settings/user_preferences.py``），
但库里不主动写空串：保留 NULL 才能区分"从没填过"和"填过又清掉"。

## 旧数据

不回填。环境变量里配过的时区**不复制**进这张表：那会让"改环境变量"从此
静默失效，而用户以为它还有效。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0049_user_preferences"
down_revision: str | None = "0048_provider_token_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
