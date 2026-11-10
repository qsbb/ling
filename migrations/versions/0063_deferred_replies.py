"""延后回复：``deferred_replies``

Revision ID: 0063_deferred_replies
Revises: 0062_memory_consolidate
Create Date: 2026-11-10

## 为什么需要这张表

用户裁决（2026-11-10）：「做，但是只在她主动选择时才延后，且延后多久让她自由
发挥，上限 5 分钟」。

她可以选择"过一会儿再回"。这句话必须是一个**有主键、有截止、能查询、能取消、
能在崩溃后对账**的持久记录——而不是进程里的一个 `asyncio.sleep`：

- 进程重启后，"她答应了 45 秒后回"不能凭空消失（用户会一直等）；
- 两个 worker 同时到点，只能有一个真的去生成（否则用户收到两条）；
- 期间来了新输入，旧的那句"等会儿"必须能被**确定地**撤销资格（否则就是
  插进新话题的迟到补话，`docs/ASYNC-TOOL-DECISION.md` 明令禁止）。

## 为什么不是 `operations` / `media_read_jobs` 加个 type

`operations` 的状态词表围绕"发没发出去"；`media_read_jobs` 围绕"读没读到、
能不能再花钱"。延后的状态围绕"她答应的那句话还算不算数"，三条状态机的合法
转移完全不同。硬塞进任何一张表，两边的状态机就只能有一个活下来。

## 字段分四组

1. **身份与因果**：`actor_id/window_id/subject_id/source_turn_id/source_message_id`
   —— 谁在哪一轮要求的。`source_turn_id` **唯一**，这是"重复扫描只产生一条
   记录"的数据库保证（不是进程内变量）。
2. **时长（分三个数）**：`requested_delay_ms` = 她真正要求的；`applied_delay_ms`
   = 代码实际用的（≤300000）；`clamped` = 两者是否不等。
   三个都要存：只存钳后值，就等于把"系统上限"说成"她选择等这么久"，而那是
   本项目明令禁止的一类事实伪造（与"把槽位忙包装成人格选择"同一条底线）。
   用户界面上要能说"她本来想等更久"。
3. **截止**：`chosen_due_at` = 登记时刻 + `applied_delay_ms`；
   `hard_deadline_at` = `chosen_due_at + 60s`（生成宽限，**有硬上界**）。
   没有第二个字段，"她选了 300 秒"就会变成"等 300 秒 + 生成超时 + 再等"的
   无上界体验。两个时刻都用数据库时间写入，不用应用时钟。
4. **状态与栅栏**：`state` + `reason` + `input_sequence`（抢占栅栏）+
   `lease_owner/lease_until/claim_generation`（认领栅栏）+ `model_started_at`
   （区分"还能重排"与"可能已收费"）+ 各时间戳。

## 为什么 `input_sequence` 是派生栅栏而不是"取消代次"列

新输入的入口分散在 web 路由、连接器入站桥、注意力轮询三处，任何"新输入时
顺手 +1"的钩子都必然漏掉一处——而漏掉的表现是"她插进新话题补了一句"，极难
复现。改成派生：`SELECT max(messages.sequence) WHERE window_id=? AND role='user'`
大于 `input_sequence` 就是被抢占了。**没有写入点，就没有漏写的可能。**
所以这一列存的是"被延后那条输入的 `messages.sequence`"，不是计数器。

## 索引

- `UNIQUE(source_turn_id)`：同一轮最多一条延后记录（幂等的库级保证）。
- `(state, chosen_due_at)`：认领扫描的唯一查询形状（到期 + 未终结）。
- `(state, hard_deadline_at)`：硬截止兜底扫描。
- `(lease_owner, lease_until)`：过期租约回收。
- `(window_id, state)`：抢占与"这个窗口还有没有在等的回复"。

## 为什么不预置行

`media_slots` 预置一行是因为"全局只有一个槽"必须由数据保证。这里没有这类
全局唯一性——每一轮都可以有自己的延后记录，所以不需要预置。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_deferred_replies"
down_revision: str | None = "0062_memory_consolidate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 状态取值必须与 `domains/deferred/state.py::DeferredReplyState` 一致。
#: 迁移里写成 CHECK 是**兜底**：状态机在 Python 里，但"库里存了拼错的状态"
#: 是排查时最费时的一种坏数据，让它插不进去。
_STATES = ("pending", "claimed", "delivered", "cancelled", "failed")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("deferred_replies"):
        return

    op.create_table(
        "deferred_replies",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("window_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        # 因果：谁在哪一轮要求的。**唯一**——同一轮最多一条延后记录。
        sa.Column("source_turn_id", sa.String(length=64), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=True),
        # 抢占栅栏：被延后那条用户消息的 messages.sequence（见模块注释）。
        sa.Column("input_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        # 时长三个数：原值 / 实际值 / 是否被钳（见模块注释第 2 组）。
        sa.Column("requested_delay_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("applied_delay_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("clamped", sa.Boolean(), nullable=False, server_default=sa.false()),
        # 截止：到点认领 + 生成宽限后的硬截止。
        sa.Column("chosen_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_deadline_at", sa.DateTime(timezone=True), nullable=False),
        # 状态机。
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        # 交付结果：指向真正说出那句话的新轮次。
        sa.Column("delivered_turn_id", sa.String(length=64), nullable=True),
        sa.Column("delivered_message_id", sa.String(length=64), nullable=True),
        # 认领栅栏（与 media_read_jobs 同一套写法）。
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0"),
        # 模型**可能已经调用**的时间。`claimed -> pending` 的重排只允许它为空时。
        sa.Column("model_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'delivered', 'cancelled', 'failed')",
            name="ck_deferred_replies_state",
        ),
        # 钳后时长不得越界。放在库里而不是只放 Python：绕过 service 直接写库的
        # 路径（手工修数据、将来的批处理）也必须受这条约束。
        sa.CheckConstraint(
            "applied_delay_ms >= 0 AND applied_delay_ms <= 300000",
            name="ck_deferred_replies_delay_cap",
        ),
        sa.CheckConstraint(
            "hard_deadline_at > chosen_due_at",
            name="ck_deferred_replies_deadline_order",
        ),
    )
    op.create_index(
        "uq_deferred_replies_source_turn",
        "deferred_replies",
        ["source_turn_id"],
        unique=True,
    )
    op.create_index(
        "ix_deferred_replies_state_due",
        "deferred_replies",
        ["state", "chosen_due_at"],
    )
    op.create_index(
        "ix_deferred_replies_state_hard_deadline",
        "deferred_replies",
        ["state", "hard_deadline_at"],
    )
    op.create_index(
        "ix_deferred_replies_lease",
        "deferred_replies",
        ["lease_owner", "lease_until"],
    )
    op.create_index(
        "ix_deferred_replies_window_state",
        "deferred_replies",
        ["window_id", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("deferred_replies"):
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("deferred_replies")}
    for name in (
        "ix_deferred_replies_window_state",
        "ix_deferred_replies_lease",
        "ix_deferred_replies_state_hard_deadline",
        "ix_deferred_replies_state_due",
        "uq_deferred_replies_source_turn",
    ):
        if name in indexes:
            op.drop_index(name, table_name="deferred_replies")
    op.drop_table("deferred_replies")


# 显式引用：让"状态集合改了要同步迁移"这件事在文件里可见。
assert set(_STATES) == {"pending", "claimed", "delivered", "cancelled", "failed"}
