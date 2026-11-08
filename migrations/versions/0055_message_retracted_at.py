"""消息撤回：``messages.retracted_at``

Revision ID: 0055_message_retracted_at
Revises: 0054_connector_config
Create Date: 2026-11-09

## 为什么需要这一列

她在群里看过一条消息、可能已经记进情节记忆、也可能正在上下文里引用它。
对方随后**撤回了**那条消息：此时"我们继续把原文喂给模型"是错的——对方
明确表达了"这条不算数"。AstrBot 与 MaiBot 都只做到"收到撤回通知"，没有任何
一层让模型忘掉它；这是我们要补的那一层。

## 为什么是加列，而不是删行

- **可追溯**：为什么某一轮她没接那句话、为什么某条记忆缺一块，要能回答。
  删行之后"这条消息从来不存在"和"它被撤回了"在库里长得一样。
- **原文仍属于证据链**：`memories.source_event_ids`、轮的 `turn_id`、审阅
  页面都会指向它；行没了就只剩悬空引用。
- **幂等**：重复的撤回通知要有地方"已经处理过"——`retracted_at` 非空就是
  那个标记（条件更新 `WHERE retracted_at IS NULL`，不靠内存）。

## 为什么不回改已生成的记忆/摘要

撤回是"以后别再引用它"，不是"过去发生过的事没有发生"。已写入的记忆带着
自己的 `source_event_ids`；把它们批量改写等于让历史随一条撤回抖动，而且
一条撤回能牵连到多少派生内容是不可预知的。所以口径是：

    **读取时按 `retracted_at` 过滤，写入时不再产生新的引用。**

原文一旦被撤，就不再进上下文/摘要素材/提示词；已经沉淀的记忆不主动回改
（用户要清除记忆有专门的 `retract`/`forget` 入口，那是另一件事）。

## 旧数据

不回填：NULL = 没被撤回。唯一索引只建在 `window_id` 上（已有），
本列不需要索引——所有查询都是"按 window 取消息再过滤"，不会有人
`WHERE retracted_at IS NULL` 全表扫。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_message_retracted_at"
down_revision: str | None = "0054_connector_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "retracted_at" not in columns:
        op.add_column(
            "messages",
            sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "retracted_at" in columns:
        op.drop_column("messages", "retracted_at")
