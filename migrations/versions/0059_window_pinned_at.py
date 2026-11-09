"""会话置顶：``windows.pinned_at``

Revision ID: 0059_window_pinned_at
Revises: 0058_media_read_jobs
Create Date: 2026-11-09

## 为什么需要这一列

对话页要能置顶会话，且置顶的排在最前（用户原话：「对话页面新增可置顶，
其他按照好感度」）。没有这一列时，置顶只能存在浏览器里——换设备/清缓存就
丢了，而置顶表达的是"这个人/这个群对我重要"，那是**服务端事实**。

## 为什么是 `timestamptz` 而不是 `boolean`

多个置顶之间也有先后（先置顶的更靠上）。布尔值只能回答"是不是置顶"，
排不出置顶内部的顺序，于是"刚置顶的那个"会按 `updated_at` 漂到别处。
时间戳同时表达"是否置顶"（非空即置顶）与"何时置顶"（排序键）。

`NULL` = 未置顶。不设默认值：默认值会让"未置顶"和"置顶于某个时刻"在
数据上不再可区分（`DEFAULT now()` 会把每一行都变成置顶）。

## 为什么不复用 `updated_at`

`updated_at` 是"最近活跃"的时间轴：置顶/取消置顶都写它，会让会话在列表里
因为一次点击而跳到"最近活跃"的位置，而它根本没有新消息。两件事用两列。

## 索引

不建索引：`windows.subject_id` 已有索引，单个用户的会话数是几十的量级，
排序在内存里做即可。加索引反而要为一次列表查询多维护一棵 B 树。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_window_pinned_at"
down_revision: str | None = "0058_media_read_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("windows")}
    if "pinned_at" not in columns:
        op.add_column(
            "windows",
            sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("windows")}
    if "pinned_at" in columns:
        op.drop_column("windows", "pinned_at")
