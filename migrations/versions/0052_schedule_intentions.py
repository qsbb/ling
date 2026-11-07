"""长程意向：有日期的目标（不是时段）、周期规则、准备段的关联列

Revision ID: 0052_schedule_intentions
Revises: 0051_day_plans_schedule
Create Date: 2026-11-08

## 这一版做了什么

按 `docs/DREAM-SCHEDULE-DESIGN.md` §3A/§6 落"阶段 1.75a：长程存储与滚动"的
**存储部分**：

1. 新表 `schedule_intentions`：指向未来日期区间的有来源目标，是"意向"不是
   "时段"。`confirmed` 只表示用户对这个**目标/日期**作过明确确认，仍是高优先
   软偏好；硬锚点只能来自带精确时段的确认来源（`commitments` / confirmed
   `agenda_plans` / 周期 occurrence）。
2. `schedule_segments` 加三列（**全部可空**）：`intention_id`、`preparation_for_day`、
   `visibility`。旧段一律 NULL：不猜来源、不回填，也就不必重写历史。
3. 新表 `schedule_recurrence_rules`：用户确认 + 精确本地时段 + 有效期 + actor。
   **本批只建表，不做展开**：没有任何代码会读它生成 occurrence 或硬锚点；
   展开在夜间输入装配那一批接线（设计 §6/§9 阶段 1.75b）。

## 这份迁移**不做**什么

- 不写清理/保留期任务。设计 §6 的约定写在这里供后续实现：
  * `schedule_intentions`：终态正文 90 天后归档为"ID/状态/来源哈希/时间"的
    审计摘要，审计摘要至少保留 1 年；被计划段或决定引用的行不提前删。
  * `schedule_segments`：`superseded` 正文 90 天后可归档成摘要，但被活动/决定
    引用的行不删，至少留 1 年；`intention_id` 引用的意向摘要随段保留期保留。
  * `schedule_recurrence_rules`：撤销后旧来源审计保留 1 年。
  * 以上都**不是**自动淘汰：容量满时不挤掉已确认来源，过期只转
    `expired_unknown`（"过期 = 不知道"）。
- 不碰 `intents` / `agenda_plans` / `commitments`：旧 `intents` 是跨轮的
  `reply/note/act/reach_out` 念头，语义不同，不重释、不批量迁移。
- 不回填任何历史数据：旧 `schedule_segments` 的 `intention_id` 保持 NULL。

## 回滚

`downgrade()` 只删本迁移新增的表/列/索引/约束，阶段 1 的 `day_plans` /
`schedule_segments` 行原样保留。回滚应用代码时先关新写入与周期展开（本批
本来就未启用），旧"小事"与承诺仍由原来源表保护——关闭意向功能**不会**取消
任何已确认承诺。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0052_schedule_intentions"
down_revision: str | None = "0051_day_plans_schedule"
branch_labels = None
depends_on = None

INTENTION_KINDS = (
    "meal",
    "work",
    "study",
    "reading",
    "exercise",
    "walk",
    "chore",
    "errand",
    "leisure",
    "social",
    "hobby",
    "rest",
    "commute",
    "travel",
    "free",
    "other",
)


def _schedule_intentions() -> None:
    op.create_table(
        "schedule_intentions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        # 目标区间是 `target_timezone` 的**本地日期**（与 plan_day 同为午夜日）。
        sa.Column("target_start_day", sa.Date(), nullable=False),
        sa.Column("target_end_day", sa.Date(), nullable=False),
        sa.Column(
            "target_timezone",
            sa.String(64),
            nullable=False,
            server_default="Asia/Shanghai",
        ),
        sa.Column("kind", sa.String(32), nullable=False, server_default="other"),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="proposed"),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("resolution_evidence_ref", sa.String(160), nullable=True),
        sa.Column("confirmation_source_ref", sa.String(160), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prep_source_ref", sa.String(160), nullable=True),
        sa.Column("prep_due_day", sa.Date(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("horizon_rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "visibility", sa.String(32), nullable=False, server_default="actor_private"
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # 同一来源重放的幂等凭据。**不用**标题/JSON 做唯一性：标题会改，
        # JSON 在 PostgreSQL 里没有稳定相等语义。
        sa.UniqueConstraint(
            "actor_id",
            "source_kind",
            "source_ref",
            "target_start_day",
            "target_end_day",
            name="uq_schedule_intentions_source",
        ),
        sa.CheckConstraint(
            "target_end_day >= target_start_day",
            name="ck_schedule_intentions_window",
        ),
        # ≤ 90 天跨度（设计 §6）。用日期相减而不是 `date + 90`：后者在
        # PostgreSQL 是"加天数"，但在其它方言没有同义语义。
        sa.CheckConstraint(
            "target_end_day - target_start_day <= 90",
            name="ck_schedule_intentions_span",
        ),
        sa.CheckConstraint(
            "(prep_due_day IS NULL) = (prep_source_ref IS NULL)",
            name="ck_schedule_intentions_prep_pair",
        ),
        sa.CheckConstraint(
            "prep_due_day IS NULL OR prep_due_day < target_start_day",
            name="ck_schedule_intentions_prep_due",
        ),
        sa.CheckConstraint(
            "state != 'confirmed' OR confirmation_source_ref IS NOT NULL",
            name="ck_schedule_intentions_confirmed_source",
        ),
        sa.CheckConstraint(
            "priority BETWEEN 0 AND 100", name="ck_schedule_intentions_priority"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_schedule_intentions_revision"),
        sa.CheckConstraint(
            "horizon_rev >= 1", name="ck_schedule_intentions_horizon_rev"
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'planned', 'confirmed', 'completed',"
            " 'missed', 'abandoned', 'expired_unknown')",
            name="ck_schedule_intentions_state",
        ),
        sa.CheckConstraint(
            "source_kind IN ('model', 'user', 'actor', 'recurrence')",
            name="ck_schedule_intentions_source_kind",
        ),
        sa.CheckConstraint(
            "kind IN ("
            + ", ".join(f"'{value}'" for value in INTENTION_KINDS)
            + ")",
            name="ck_schedule_intentions_kind",
        ),
    )
    # 展望/容量/过期的读路径：按 actor + 目标起点日 + 状态。
    op.create_index(
        "ix_schedule_intentions_actor_horizon",
        "schedule_intentions",
        ["actor_id", "target_start_day", "state"],
    )


def _schedule_recurrence_rules() -> None:
    """周期规则：**只建表，本批不做展开**（见模块注释）。"""
    op.create_table(
        "schedule_recurrence_rules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        # 0=周一 … 6=周日
        sa.Column("weekday", sa.Integer(), nullable=False),
        # 本地分钟；end 允许 1440（本地 24:00 = 到当天结束）
        sa.Column("start_local_minute", sa.Integer(), nullable=False),
        sa.Column("end_local_minute", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("effective_start_day", sa.Date(), nullable=False),
        sa.Column("effective_end_day", sa.Date(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        # 用户确认的凭据（不是模型输出）：模型无权建 active 规则。
        sa.Column("confirmation_source_ref", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "visibility", sa.String(32), nullable=False, server_default="subject_private"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "actor_id",
            "confirmation_source_ref",
            name="uq_schedule_recurrence_rule_source",
        ),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6", name="ck_schedule_recurrence_weekday"
        ),
        sa.CheckConstraint(
            "start_local_minute BETWEEN 0 AND 1439 AND "
            "end_local_minute BETWEEN 1 AND 1440",
            name="ck_schedule_recurrence_minutes",
        ),
        sa.CheckConstraint(
            "end_local_minute > start_local_minute",
            name="ck_schedule_recurrence_range",
        ),
        sa.CheckConstraint(
            "effective_end_day >= effective_start_day",
            name="ck_schedule_recurrence_effective",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'cancelled')",
            name="ck_schedule_recurrence_state",
        ),
    )


def _segment_columns() -> None:
    """`schedule_segments` 的三个**可空**长程扩展列（旧段不回填）。"""
    op.add_column(
        "schedule_segments", sa.Column("intention_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "schedule_segments", sa.Column("preparation_for_day", sa.Date(), nullable=True)
    )
    op.add_column(
        "schedule_segments", sa.Column("visibility", sa.String(32), nullable=True)
    )
    op.create_foreign_key(
        "fk_schedule_segments_intention",
        "schedule_segments",
        "schedule_intentions",
        ["intention_id"],
        ["id"],
    )
    op.create_index(
        "ix_schedule_segments_intention_status",
        "schedule_segments",
        ["intention_id", "status"],
    )
    # 关联列成对出现：普通段不能只挂一半的"为某目标日准备"。
    op.create_check_constraint(
        "ck_schedule_segments_preparation_pair",
        "schedule_segments",
        "(intention_id IS NULL) = (preparation_for_day IS NULL)",
    )


def upgrade() -> None:
    # 顺序：先建意向表（segment 的 FK 指向它），再加段上的可空列，
    # 最后建与两者都无关的周期规则。
    _schedule_intentions()
    _segment_columns()
    _schedule_recurrence_rules()


def downgrade() -> None:
    op.drop_table("schedule_recurrence_rules")

    op.drop_constraint(
        "ck_schedule_segments_preparation_pair",
        "schedule_segments",
        type_="check",
    )
    op.drop_index(
        "ix_schedule_segments_intention_status", table_name="schedule_segments"
    )
    op.drop_constraint(
        "fk_schedule_segments_intention", "schedule_segments", type_="foreignkey"
    )
    op.drop_column("schedule_segments", "visibility")
    op.drop_column("schedule_segments", "preparation_for_day")
    op.drop_column("schedule_segments", "intention_id")

    op.drop_index(
        "ix_schedule_intentions_actor_horizon", table_name="schedule_intentions"
    )
    op.drop_table("schedule_intentions")
