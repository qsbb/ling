"""睡眠期记忆整理：``memory_consolidations``

Revision ID: 0062_memory_consolidate
Revises: 0061_media_slot
Create Date: 2026-11-10

## 这张表记什么

一晚一行：**这一晚的整理做了什么、做到哪一步**。

- 为什么不是只写日志：整理是**会改记忆**的动作（归档一批低价值记忆、
  把同槽位的并成摘要）。改完只在日志里留一行，用户既看不到、也无法判断
  "我昨晚丢了什么"。有了这张表，记忆页才能如实说"她在 11-09 夜里整理过，
  归档了 3 条"。
- 为什么要 `sleep_date` 唯一：整理必须**一晚一次**。同时起两个作业去整理
  同一晚，会把同一批记忆并两遍；更糟的是两条摘要互相覆盖来源链。

## 为什么状态要有 `skipped`

`docs/SLEEP-MEMORY-CONSOLIDATION.md` §3 第 13 条明确：调度点是 `life.schedule`
算出的**预计**睡眠窗口，**不宣称角色真的睡着**。所以常见情况是"到了预计
入睡时间，但这一晚没有任何可整理的东西"。

那不是失败，也不该记成 ok。`skipped` + `reason` 才是诚实的：它说明作业
跑了、判断过、确实没有可做的事。把它记成 `ok` 会让"整理了几条"这个数字
失去意义。

## 为什么不存模型摘要

第一版**只做确定性整理**（去重、按受众边界分组、归档低价值），不调模型。
所以这张表里没有摘要正文——没有正文就没有"把模型编的话存成事实"的通道。
将来要加日摘要时，必须新增独立的、明确标注为"非事实"的列，而不是复用这里。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062_memory_consolidate"
down_revision: str | None = "0061_media_slot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("memory_consolidations"):
        op.create_table(
            "memory_consolidations",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("actor_id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("sleep_date", sa.Date(), nullable=False),
            # ok = 真的做了事；skipped = 判断过但没有可整理的；failed = 出错
            sa.Column("state", sa.String(length=16), nullable=False),
            # 为什么是这个状态（人话，给记忆页展示）
            sa.Column("reason", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("groups_merged", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rows_archived", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "state IN ('ok', 'skipped', 'failed')",
                name="ck_memory_consolidations_state",
            ),
            # 一晚一行：两个作业同时整理同一晚会把同一批记忆并两遍，
            # 更糟的是两条摘要互相覆盖来源链。
            sa.UniqueConstraint(
                "actor_id", "subject_id", "sleep_date",
                name="uq_memory_consolidations_night",
            ),
        )
        op.create_index(
            "ix_memory_consolidations_actor_day",
            "memory_consolidations",
            ["actor_id", "sleep_date"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("memory_consolidations"):
        indexes = {idx["name"] for idx in inspector.get_indexes("memory_consolidations")}
        if "ix_memory_consolidations_actor_day" in indexes:
            op.drop_index(
                "ix_memory_consolidations_actor_day",
                table_name="memory_consolidations",
            )
        op.drop_table("memory_consolidations")
