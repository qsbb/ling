"""用户偏好表加 ``video_mode``（看视频：抽帧 / 原生视频输入）

Revision ID: 0050_video_mode
Revises: 0049_user_preferences
Create Date: 2026-11-07

## 为什么进 user_preferences 而不是 config_revisions

"我怎么看视频"是**用户的事实**，不是"我们怎么跑模型"：换个对话模型、
回滚一次配置修订，都不该把用户选的读取方式一起改掉。反过来，改它也不该
触发一次模型配置发布。

## 为什么允许 NULL

NULL = **没设过**，读侧回退 ``frames``（任何供应商都能用的那一种）。
不写默认值进库：那会让"从没选过"和"选过又选回默认"变成同一件事，
以后想改默认值就改不动了（老行还钉着旧默认）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0050_video_mode"
down_revision: str | None = "0049_user_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("video_mode", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "video_mode")
