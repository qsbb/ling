"""提示词快照：每条回复"这次看到的上下文"（只读「查看本次提示词」）

Revision ID: 0053_prompt_snapshots
Revises: 0052_schedule_intentions
Create Date: 2026-11-08

## 这一版做了什么

新表 ``prompt_snapshots``：一轮对话**真正发给模型的 messages** 存一份。

- ``UNIQUE(turn_id)``：同一轮只存一份。重试复用同一个 ``turn_id``，
  捕获点是"有则覆盖"——**不按 attempt 追加**。按 attempt 存会让这张表
  随重试次数膨胀，而且同一轮的旧提示词既没人看、又多一份泄漏面。
- 索引 ``(actor_id, created_at)``：只读 API 与清理任务都按这两列筛。
- ``message_id`` 可空：快照在回复落库**之前**写入，回复落库后回填。
  因此**不加外键**——快照的写入与消息生命周期解耦，重试删旧消息时
  也不该被外键挡住。
- ``expires_at``：保留期 7 天，由 worker 的小时级维护任务（``session.purge``
  处理器）清理：先清正文（行留作"曾经有过快照"的墓碑），墓碑再放
  ``PROMPT_SNAPSHOT_TOMBSTONE_DAYS`` 天真删。

## 这份迁移**不做**什么

- 不回填任何历史轮次：旧对话没有快照，读接口就如实说 ``unavailable``
  （"没存下来"），而不是拿现在的上下文假装当时的样子。
- 不改 ``messages`` / ``turns``：快照是旁路观测，不是对话事实的一部分。

## 数据形状（``payload``）

```json
{
  "schema_version": 1,
  "captured_at": "2026-11-08T03:00:00+00:00",
  "model": {"provider_id": "p", "model_id": "m", "profile": "dialogue.fast"},
  "token_total": 12345,
  "truncated": false,
  "dropped_items": 0,
  "messages": [
    {"index": 0, "role": "system", "key": "persona", "lane": "persona",
     "tokens": 1234, "content": "…", "sender": null, "at": null}
  ]
}
```

``key`` 是**编译器直接标注**的语义块（system / persona / memory / summary /
history / input / media / plan / other），不是事后按正文猜的。

## 回滚

``downgrade()`` 只删本表与新索引，不碰任何对话数据。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0053_prompt_snapshots"
down_revision: str | None = "0052_schedule_intentions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=False),
        # 回复落库后回填；写入时为空。**不加外键**（见模块注释）。
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # payload 的**结构**版本，不是契约 v1。
        sa.Column(
            "schema_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "truncated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        # 到期后置 NULL（正文清空）——读接口据此报 ``expired``。
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.UniqueConstraint("turn_id", name="uq_prompt_snapshots_turn"),
        sa.CheckConstraint(
            "size_bytes >= 0", name="ck_prompt_snapshots_size_bytes"
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_prompt_snapshots_schema_version"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_prompt_snapshots_expiry"
        ),
    )
    op.create_index(
        "ix_prompt_snapshots_actor_created",
        "prompt_snapshots",
        ["actor_id", "created_at"],
    )
    # 读接口从消息反查快照（message_id 回填后才命中），清理任务按过期时间扫。
    op.create_index(
        "ix_prompt_snapshots_message", "prompt_snapshots", ["message_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_snapshots_message", table_name="prompt_snapshots")
    op.drop_index(
        "ix_prompt_snapshots_actor_created", table_name="prompt_snapshots"
    )
    op.drop_table("prompt_snapshots")
