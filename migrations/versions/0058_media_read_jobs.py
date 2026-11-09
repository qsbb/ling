"""视频后台取证作业：``media_read_jobs``

Revision ID: 0058_media_read_jobs
Revises: 0057_outbound_recall
Create Date: 2026-11-09

裁决：`docs/ASYNC-TOOL-DECISION.md` §3 第 2 步。本轮只落**入口 + 持久状态**，
执行在第 3 步（worker 注册、租约续期、结果提交），预算与读侧在第 4 步。

## 为什么是独立一张表

`operations` 记的是**投递**（一个 part 发到平台、拿到回执），它的 `state`
词表围绕"发没发出去"；这个作业的 `state` 围绕"读没读到、能不能再花钱"。
两者合法转移完全不同，合表之后只能有一套状态机活下来——要么投递被塞进
"读"的语义，要么反过来。所以分开。

## 三条约束怎么建的

1. **`UNIQUE(actor_id, window_id, request_key)`**（普通唯一索引）
   同一个人在同一窗口重发同一个 `request_key` 必须命中同一个作业。
   **不带 evidence_id**：`request_key` 是调用方的幂等键，同一个键配不同参数
   要能被识别成冲突（409），而不是悄悄排第二个作业。跨窗口不合并——两个窗口
   的授权与可见性不同。
2. **部分唯一索引 `(evidence_id, tool_version) WHERE state IN
   ('pending','running')`**
   同一证据同一版本最多一个**在途**作业。终态之后允许再排（用户想再读一遍、
   或上次失败要重试），只是不许同时有两个在途——那会两次视觉调用、两次收费，
   而结果写的是同一行证据。用部分索引而不是普通唯一约束，正是因为"终态不算"。
3. **`CHECK (state IN (...))`**
   状态词表放在数据库里兜底。`domains/media/jobs.py::JobState` 是事实来源，
   这里只是防止有人手写 SQL 插进一个应用层不认识的状态——那种行会让清理任务
   永远捞不到、读侧永远判不出终态。

    CHECK **不**表达转移合法性（那需要跨行历史），转移由 `transition()` 纯函数
   + 第 3 步的条件更新 `WHERE state = :expected` 保证。这里只保证"取值在集合内"。

## 为什么 `deadline` 可空

裁决的词是"排队截止时间定为**登记后** 15 分钟"，所以它本该非空。留可空是为了
**迁移可回滚 + 历史行**：本迁移之前不存在作业行，但若将来有人先建行后算
deadline（例如从别的系统导入），非空约束会让整批插入失败。应用层在创建时
**总是**写它（`jobs.enqueue_read_job` 的调用方），清理任务对 NULL 单独处理
（按 `requested_at + 15min` 兜底）——不把"数据完整性"押在一个 DB 默认值上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_media_read_jobs"
down_revision: str | None = "0057_outbound_recall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 与 `domains/media/jobs.py::JobState` 逐字对应。**不要**在这里加应用层不认识
#: 的值——两处不一致时，应用会写入一个 CHECK 立刻拒绝的状态，表现为 500。
_STATES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "timeout",
    "unknown",
    "cancelled",
    "expired",
    "budget_denied",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "media_read_jobs" in inspector.get_table_names():
        return

    op.create_table(
        "media_read_jobs",
        sa.Column("call_id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("evidence_id", sa.String(64), nullable=False),
        # --- 因果（只用于审计，不作准入判据）---
        sa.Column("source_message_id", sa.String(64), nullable=True),
        sa.Column("source_turn_id", sa.String(64), nullable=True),
        sa.Column("source_attempt", sa.Integer(), nullable=True),
        # --- 协议 ---
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False, server_default="video"),
        sa.Column(
            "feedback_mode",
            sa.String(32),
            nullable=False,
            server_default="evidence_only",
        ),
        sa.Column("authorization_ref", sa.String(128), nullable=True),
        # --- 状态机 ---
        sa.Column(
            "state", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("result_ref", sa.String(128), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_started_at", sa.DateTime(timezone=True), nullable=True),
        # --- 租约（第 3 步）---
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lease_generation", sa.Integer(), nullable=False, server_default="0"
        ),
        # 1) 幂等键：同一人同一窗口同一 request_key 只一个作业
        sa.UniqueConstraint(
            "actor_id", "window_id", "request_key", name="uq_media_read_job_request"
        ),
        # 3) 状态取值兜底（转移合法性不在这里，见模块注释）
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in _STATES) + ")",
            name="ck_media_read_jobs_state",
        ),
    )

    # 普通索引
    op.create_index("ix_media_read_jobs_actor_id", "media_read_jobs", ["actor_id"])
    op.create_index("ix_media_read_jobs_subject_id", "media_read_jobs", ["subject_id"])
    op.create_index("ix_media_read_jobs_window_id", "media_read_jobs", ["window_id"])
    op.create_index("ix_media_read_jobs_evidence_id", "media_read_jobs", ["evidence_id"])
    op.create_index("ix_media_read_jobs_state", "media_read_jobs", ["state"])
    op.create_index(
        "ix_media_read_jobs_actor_window", "media_read_jobs", ["actor_id", "window_id"]
    )
    op.create_index(
        "ix_media_read_jobs_state_deadline", "media_read_jobs", ["state", "deadline"]
    )
    op.create_index(
        "ix_media_read_jobs_lease", "media_read_jobs", ["lease_owner", "lease_until"]
    )

    # 2) 在途互斥（部分唯一索引）
    #
    # `postgresql_where` 而不是 `sqlite_where` 之外再写一份：SQLAlchemy 的
    # `op.create_index` 支持方言专属的 WHERE，两个都给了才能让 SQLite 派的
    # 测试库也建得出这条索引（本项目的测试库是 PG，但迁移的可移植性不该
    # 因为"恰好只有 PG"而丢掉）。
    op.create_index(
        "uq_media_read_job_active_evidence",
        "media_read_jobs",
        ["evidence_id", "tool_version"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'running')"),
        sqlite_where=sa.text("state IN ('pending', 'running')"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "media_read_jobs" not in inspector.get_table_names():
        return
    op.drop_index(
        "uq_media_read_job_active_evidence", table_name="media_read_jobs"
    )
    for name in (
        "ix_media_read_jobs_lease",
        "ix_media_read_jobs_state_deadline",
        "ix_media_read_jobs_actor_window",
        "ix_media_read_jobs_state",
        "ix_media_read_jobs_evidence_id",
        "ix_media_read_jobs_window_id",
        "ix_media_read_jobs_subject_id",
        "ix_media_read_jobs_actor_id",
    ):
        op.drop_index(name, table_name="media_read_jobs")
    op.drop_table("media_read_jobs")
