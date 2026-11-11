"""Web 播放事件表：``media_play_events``

Revision ID: 0066_media_play_events
Revises: 0065_media_assets
Create Date: 2026-11-11

## 为什么需要这张表

SP01 的 `media_assets` 明确**不记录是否已发送**（那属于投递域），也**不记录
是否被播放过**。而用户要求能回答「主人到底听没听」，这就需要播放有自己的
事实表，且**不能**与送达共用字段：

- 送达 = 渠道回了回执（`delivery_receipts`，投递层写）；
- 播放 = 主人在 Web 上点了播放（本表，API 写）。

两者混成一个字段后，"发出去但没人听"和"没发出去但主人自己听了"都无法表达。

## 设计要点

- **主键 `id` 由 `(message_id, subject_id)` 派生**：一行 = 一人一消息。
  拖动进度条会发多个 Range 请求，按请求计数会让"听过几次"等于"点了多少下"。
- **`play_count` 累加 + `last_played_at`**：统计量，不是凭据。
- **`asset_digest` 与 `message_id` 都存**：前者钉住"当时听到的那一份音频"
  （重新合成会换 digest），后者回答"这条消息被听过没有"。
- **不设过期**：音频文件被清理之后，"她那天发过语音、主人听过"仍然是事实。
- **`completed` 只在收到 `ended` 时为真**：把"播到 99%"当听完，会让
  「他根本没听完」这个信号消失。

## 为什么先判 `has_table`

与 0065 同样的理由：开发库可能已由 `Base.metadata.create_all` 建好过，
`create_table` 本身不幂等。先查再建，迁移在两种库上都能跑通。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_media_play_events"
down_revision: str | None = "0065_media_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("media_play_events"):
        op.create_table(
            "media_play_events",
            sa.Column("id", sa.String(length=96), primary_key=True),
            sa.Column("message_id", sa.String(length=64), nullable=False),
            sa.Column("asset_digest", sa.String(length=64), nullable=False),
            sa.Column("window_id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("play_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "first_played_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "last_played_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("position_ms", sa.Integer(), nullable=True),
            sa.Column(
                "completed", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        # 会话页问"这个窗口里哪些听过"。
        op.create_index(
            "ix_media_play_window", "media_play_events", ["window_id", "last_played_at"]
        )

    existing = {index["name"] for index in sa.inspect(bind).get_indexes("media_play_events")}
    for column in ("message_id", "window_id", "subject_id"):
        name = f"ix_media_play_events_{column}"
        if name not in existing:
            op.create_index(name, "media_play_events", [column])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("media_play_events"):
        op.drop_table("media_play_events")
