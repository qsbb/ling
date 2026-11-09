"""记忆真删：墓碑表 + 遗忘代次

Revision ID: 0060_memory_purge
Revises: 0059_window_pinned_at
Create Date: 2026-11-09

## 为什么需要这张表

用户原话：「用户说忘了……这种东西不会也不应该存在」「只能后台管理页删，
对话里不能删」「真删：原文和派生数据都从库里清掉」。

现状是**软清除**：`forget_subject()` 只把行标成 `retracted` / `archived`，
原文还躺在库里；而且它**完全不碰 outbox**——真库里就有 24 条 pending 的
`memory.extract`，payload 里带 `user_text` / `actor_text` 原文，清完之后
那些事件一执行就把内容重新写回记忆。这不是"可能"，是必然。

所以需要两样东西：

1. **墓碑**：证明"这个主体在某个时刻被删过"，且**不含任何正文**。
   保留它是为了两件事：
   - 备份恢复后重放墓碑，避免已删内容复活；
   - 删除是异步的，前端要能查到"删到哪一步了"。
2. **代次（generation）**：删除请求会与"已经认领、正在跑"的提取任务竞态。
   光把 outbox 置为 cancelled 抓不住在途任务（它已经不在 pending 里了）。
   入队时记下代次、写库前比对代次，才能让旧任务**静默作废**。

## 为什么代次在 `subjects` 上而不在新表

代次是"这个主体当前是第几代"，与主体一一对应，查询点又全在写入热路径上
（每条记忆写入前都要读一次）。放 `subjects` 上可以跟已有的 subject 查询
合并，不需要多一次 JOIN；放新表则每次写入都要多一跳。

墓碑表仍要独立：一次遗忘一行，是**事件**不是属性，且要保留历史（谁在何时
删过几次）。

## 为什么墓碑表不存 subject 外键

删除后 subject 行本身可能被清理（未来），而墓碑必须留着才能防复活。
外键会让"删主体"变成"必须先删墓碑"，正好把防护顺序搞反。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_memory_purge"
down_revision: str | None = "0059_window_pinned_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) 主体遗忘代次：默认 0 = 从未遗忘过。
    subject_columns = {col["name"] for col in inspector.get_columns("subjects")}
    if "forget_generation" not in subject_columns:
        op.add_column(
            "subjects",
            sa.Column(
                "forget_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )

    # 2) 墓碑表：一次遗忘一行，**不含正文**。
    if not inspector.has_table("memory_purge_tombstones"):
        op.create_table(
            "memory_purge_tombstones",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("subject_id", sa.String(length=64), nullable=False),
            sa.Column("requested_by", sa.String(length=64), nullable=True),
            # 请求时该主体的代次；删除完成后与 subjects.forget_generation 相等
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            # 计数只留数字，不留任何内容
            sa.Column("memories_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("episodes_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("core_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("awareness_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("events_cancelled", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("embeddings_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("entities_deleted", sa.Integer(), nullable=False, server_default="0"),
            # 检索日志与历史事件脱敏的计数。`memory_retrieval_logs.query` 存的是
            # 用户原话，这一项必须能单独回答"删了几条"，不能并进别的数字。
            sa.Column(
                "retrieval_logs_deleted", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("events_scrubbed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(length=48), nullable=True),
            sa.CheckConstraint(
                "state IN ('pending', 'running', 'done', 'failed')",
                name="ck_memory_purge_tombstones_state",
            ),
            sa.CheckConstraint(
                "generation >= 1", name="ck_memory_purge_tombstones_generation"
            ),
        )
        op.create_index(
            "ix_memory_purge_tombstones_subject",
            "memory_purge_tombstones",
            ["subject_id", "requested_at"],
        )
    else:
        # 已经建过表的库（本地开发库、先跑过一版迁移的测试实例）：只补缺的列。
        # 迁移必须能在"表已存在"的库上再跑一次，否则 upgrade 会直接炸在
        # `create_table` 上，而这正是本地反复调试时最常见的形态。
        existing = {
            col["name"] for col in inspector.get_columns("memory_purge_tombstones")
        }
        for name in ("retrieval_logs_deleted", "events_scrubbed"):
            if name not in existing:
                op.add_column(
                    "memory_purge_tombstones",
                    sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
                )

    # 3) outbox 加代次与来源主体：让"已认领的旧任务"能被认出来并作废。
    #
    #    `subject_id` 冗余到 outbox 上，是因为取消时要按主体过滤——
    #    payload 是 JSON，各 topic 的键名不同（memory.extract 用 subject_id，
    #    其它 topic 未必有），在 JSON 上过滤既慢又不可靠。
    outbox_columns = {col["name"] for col in inspector.get_columns("outbox_events")}
    if "subject_id" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("subject_id", sa.String(length=64), nullable=True),
        )
        op.create_index(
            "ix_outbox_events_subject_state",
            "outbox_events",
            ["subject_id", "state"],
        )
    if "forget_generation" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column(
                "forget_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    outbox_columns = {col["name"] for col in inspector.get_columns("outbox_events")}
    if "forget_generation" in outbox_columns:
        op.drop_column("outbox_events", "forget_generation")
    if "subject_id" in outbox_columns:
        indexes = {idx["name"] for idx in inspector.get_indexes("outbox_events")}
        if "ix_outbox_events_subject_state" in indexes:
            op.drop_index("ix_outbox_events_subject_state", table_name="outbox_events")
        op.drop_column("outbox_events", "subject_id")

    if inspector.has_table("memory_purge_tombstones"):
        indexes = {idx["name"] for idx in inspector.get_indexes("memory_purge_tombstones")}
        if "ix_memory_purge_tombstones_subject" in indexes:
            op.drop_index(
                "ix_memory_purge_tombstones_subject",
                table_name="memory_purge_tombstones",
            )
        op.drop_table("memory_purge_tombstones")

    subject_columns = {col["name"] for col in inspector.get_columns("subjects")}
    if "forget_generation" in subject_columns:
        op.drop_column("subjects", "forget_generation")
