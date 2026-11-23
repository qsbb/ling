"""记录会话对端在平台上是否还在：``windows.contact_unavailable_*``

Revision ID: 0090_window_contact_unavailable
Revises: 0089_onebot_deferred_reply
Create Date: 2026-11-24

## 为什么需要这两列

用户要求：「把已退出或者已不在的群聊，或者已删除的好友置灰处理」。

这个事实**只有平台知道**，而且对群来说是**一次性的**：
``notice.group_decrease`` 带 ``sub_type=kick_me`` 时说明机器人被踢出了这个群，
之后我们再也收不到它的任何消息。事件只来一次，不落库就永远丢了——
而"机器人还能不能收到这个群的消息"恰恰是一条必须长期记得的事实。

好友那一侧不同：OneBot v11 没有 ``friend_decrease`` 通知（NapCat 也不推），
所以"对方删了我"只能读侧比对 ``get_friend_list``，不需要落库（见
``api/routes/sessions.py::_friend_availability``）。**但两个来源写同一个观察点**，
所以这两列同时服务两种原因。

## 为什么是 ``reason`` + ``at`` 而不是 ``unavailable: bool``

布尔值答不出"为什么"。而灰掉的一行在排障时唯一有用的问题就是
「我自己退的，还是被踢的」——``left`` 和 ``kicked`` 在界面上的处置相同，
但记混了就是错误归属（与撤回路径里 ``self_recall`` 单列同一条理由）。

## 为什么不复用 ``archived``

``archived`` 是**用户**说"这个对话我收起来了"；这两列是**平台**说"这个联系
不在了"。归档的不该显示，不在的必须显示——用户要能翻旧记录。把两者合并
会让"置灰"这个需求根本无法表达。

## 索引

不建索引：``windows.subject_id`` 已有索引，单个用户的会话数是几十的量级，
``contact_unavailable_reason`` 只在读列表时随行带出，不做过滤查询。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090_window_contact_unavailable"
down_revision: str | None = "0089_onebot_deferred_reply"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("windows")}
    if "contact_unavailable_reason" not in columns:
        op.add_column(
            "windows",
            sa.Column("contact_unavailable_reason", sa.String(32), nullable=True),
        )
    if "contact_unavailable_at" not in columns:
        op.add_column(
            "windows",
            sa.Column("contact_unavailable_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("windows")}
    if "contact_unavailable_at" in columns:
        op.drop_column("windows", "contact_unavailable_at")
    if "contact_unavailable_reason" in columns:
        op.drop_column("windows", "contact_unavailable_reason")
