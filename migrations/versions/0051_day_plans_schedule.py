"""她的一天：日计划、时间轴段、预算/变更/梦/结算/情绪信号

Revision ID: 0051_day_plans_schedule
Revises: 0050_video_mode
Create Date: 2026-11-07

## 这一版做了什么（阶段 1：日界与大纲 + 全部新表）

按 `docs/DREAM-SCHEDULE-DESIGN.md` §6 一次落地七张新表与三处加列。**只加表/
可空列/带默认值的新列**，不重写旧数据：旧 `agenda_plans` 保持原值，历史活动
不猜关联（`activities.schedule_segment_id` 全为 NULL）。回滚应用代码时新表可以
闲置，旧"小事"仍然工作。

## 为什么重叠防护先放服务层

`btree_gist` 的 `EXCLUDE` 只是**额外保险**：它防的是"同一个 actor 的 current
叶段时间重叠"，防不了父子包含、跨表锚点（`commitments` / 睡眠）与历史版本。
所以迁移里先探测扩展能不能装（`CREATE EXTENSION` 可能没有权限），装得上就加上，
装不上就把原因写在日志里并依赖 `agenda/timeline.py` 的事务校验——两条路都不会
让重叠静默入库。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051_day_plans_schedule"
down_revision: str | None = "0050_video_mode"
branch_labels = None
depends_on = None

EXCLUSION_NAME = "ex_schedule_segments_current_leaf"


def _day_plans() -> None:
    op.create_table(
        "day_plans",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("plan_day", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("timezone_source", sa.String(16), nullable=False, server_default="default"),
        sa.Column("timezone_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("routine_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("generation_source", sa.String(16), nullable=False, server_default="rule"),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "plan_day", name="uq_day_plans_actor_day"),
        # 复合 FK 目标：让 schedule_segments 能保证"段与计划同 actor"。
        sa.UniqueConstraint("id", "actor_id", name="uq_day_plans_id_actor"),
        sa.CheckConstraint("revision >= 0", name="ck_day_plans_revision"),
        sa.CheckConstraint("window_end_at > window_start_at", name="ck_day_plans_window"),
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'degraded')", name="ck_day_plans_state"
        ),
    )
    op.create_index("ix_day_plans_actor_id", "day_plans", ["actor_id"])
    op.create_index("ix_day_plans_state", "day_plans", ["state"])
    op.create_index(
        "ix_day_plans_actor_window", "day_plans", ["actor_id", "window_start_at"]
    )


def _schedule_segments() -> None:
    op.create_table(
        "schedule_segments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.String(64), nullable=True),
        # 根段用空串而不是 NULL：PostgreSQL 的唯一键对 NULL 互不相等，
        # 写成 NULL 会让"同一版本同层同起点"的去重对根段失效。
        sa.Column("parent_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("layer", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("place_id", sa.String(64), nullable=True),
        sa.Column("commitment_level", sa.String(16), nullable=False, server_default="tentative"),
        sa.Column("status", sa.String(16), nullable=False, server_default="current"),
        sa.Column("is_leaf", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("anchor_kind", sa.String(16), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="model"),
        sa.Column("revision_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision_ended", sa.Integer(), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "actor_id"],
            ["day_plans.id", "day_plans.actor_id"],
            name="fk_schedule_segments_plan_actor",
        ),
        sa.UniqueConstraint("id", "plan_id", name="uq_schedule_segments_id_plan"),
        sa.ForeignKeyConstraint(
            ["parent_id", "plan_id"],
            ["schedule_segments.id", "schedule_segments.plan_id"],
            name="fk_schedule_segments_parent_plan",
        ),
        sa.ForeignKeyConstraint(
            ["place_id"], ["world_places.id"], name="fk_schedule_segments_place"
        ),
        sa.UniqueConstraint(
            "plan_id",
            "revision_created",
            "layer",
            "start_at",
            "parent_key",
            name="uq_schedule_segments_rev",
        ),
        sa.CheckConstraint("end_at > start_at", name="ck_schedule_segments_order"),
        # 根段 parent_id IS NULL、parent_key = ''（空串是防 NULL 绕过唯一键的手段）；
        # 子段两者都非空。写成 length(parent_key) > 0 会拒收全部根段。
        sa.CheckConstraint(
            "(parent_id IS NULL AND parent_key = '')"
            " OR (parent_id IS NOT NULL AND length(parent_key) > 0)",
            name="ck_schedule_segments_parent_key",
        ),
        sa.CheckConstraint(
            "layer IN ('block', 'hour', 'minute')", name="ck_schedule_segments_layer"
        ),
        sa.CheckConstraint(
            "status IN ('current', 'superseded', 'cancelled')",
            name="ck_schedule_segments_status",
        ),
        sa.CheckConstraint(
            "anchor_kind IN ('', 'sleep', 'commitment')",
            name="ck_schedule_segments_anchor",
        ),
        sa.CheckConstraint(
            "source IN ('model', 'rule', 'baseline', 'user')",
            name="ck_schedule_segments_source",
        ),
        sa.CheckConstraint(
            "commitment_level IN ('tentative', 'confirmed')",
            name="ck_schedule_segments_commitment",
        ),
    )
    op.create_index("ix_schedule_segments_plan_id", "schedule_segments", ["plan_id"])
    op.create_index("ix_schedule_segments_actor_id", "schedule_segments", ["actor_id"])
    op.create_index("ix_schedule_segments_status", "schedule_segments", ["status"])
    op.create_index(
        "ix_schedule_segments_actor_window",
        "schedule_segments",
        ["actor_id", "start_at", "end_at"],
    )
    op.create_index(
        "ix_schedule_segments_plan_rev", "schedule_segments", ["plan_id", "revision_created"]
    )


def _schedule_change_requests() -> None:
    op.create_table(
        "schedule_change_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("source_turn_id", sa.String(64), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=True),
        sa.Column("plan_id", sa.String(64), nullable=True),
        sa.Column("base_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=True),
        sa.Column("range_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("range_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["day_plans.id"], name="fk_schedule_change_plan"
        ),
        sa.UniqueConstraint(
            "actor_id", "source_turn_id", name="uq_schedule_change_actor_turn"
        ),
        sa.CheckConstraint(
            "decision IN ('accept', 'decline', 'defer')",
            name="ck_schedule_change_decision",
        ),
    )
    op.create_index(
        "ix_schedule_change_requests_actor_id", "schedule_change_requests", ["actor_id"]
    )
    op.create_index(
        "ix_schedule_change_actor_created",
        "schedule_change_requests",
        ["actor_id", "created_at"],
    )


def _behavior_model_calls() -> None:
    op.create_table(
        "behavior_model_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("plan_day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column(
            "reserved_input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "reserved_output_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("actual_tokens", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "actor_id",
            "kind",
            "idempotency_key",
            "attempt",
            name="uq_behavior_model_calls_attempt",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'committed', 'failed', 'expired')",
            name="ck_behavior_model_calls_state",
        ),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens >= 0 "
            "AND (actual_tokens IS NULL OR actual_tokens >= 0)",
            name="ck_behavior_model_calls_tokens",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_behavior_model_calls_attempt"),
    )
    op.create_index(
        "ix_behavior_model_calls_day",
        "behavior_model_calls",
        ["actor_id", "plan_day", "kind", "state"],
    )
    op.create_index(
        "ix_behavior_model_calls_window",
        "behavior_model_calls",
        ["actor_id", "window_id", "created_at"],
    )


def _dream_reports() -> None:
    op.create_table(
        "dream_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("sleep_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="no_dream"),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("mood", sa.String(32), nullable=True),
        sa.Column("cue_ids", sa.JSON(), nullable=False),
        sa.Column("transform_ids", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False, server_default="simulated"),
        sa.Column(
            "visibility", sa.String(24), nullable=False, server_default="actor_private"
        ),
        sa.Column("plan_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["day_plans.id"], name="fk_dream_reports_plan"),
        sa.UniqueConstraint("actor_id", "sleep_date", name="uq_dream_reports_actor_date"),
        sa.CheckConstraint(
            "status IN ('ready', 'failed', 'missed', 'no_dream')",
            name="ck_dream_reports_status",
        ),
        sa.CheckConstraint("origin = 'simulated'", name="ck_dream_reports_origin"),
        sa.CheckConstraint(
            "visibility = 'actor_private'", name="ck_dream_reports_visibility"
        ),
    )
    op.create_index("ix_dream_reports_actor_id", "dream_reports", ["actor_id"])


def _sleep_settlements() -> None:
    op.create_table(
        "sleep_settlements",
        sa.Column("actor_id", sa.String(64), primary_key=True),
        sa.Column("sleep_date", sa.Date(), primary_key=True),
        sa.Column("bed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("global_affect_before", sa.JSON(), nullable=False),
        sa.Column("global_affect_after", sa.JSON(), nullable=False),
        sa.Column(
            "source_routine_revision", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _affect_signals() -> None:
    op.create_table(
        "affect_signals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        sa.Column("window_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("emotion", sa.String(32), nullable=False),
        sa.Column("intensity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("valence", sa.Float(), nullable=True),
        sa.Column("arousal", sa.Float(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "actor_id",
            "source_event_id",
            "scope",
            "window_id",
            "emotion",
            name="uq_affect_signals_event",
        ),
        sa.CheckConstraint("intensity >= 0", name="ck_affect_signals_intensity"),
        sa.CheckConstraint(
            "valence IS NULL OR valence BETWEEN -1 AND 1",
            name="ck_affect_signals_valence",
        ),
        sa.CheckConstraint(
            "arousal IS NULL OR arousal BETWEEN -1 AND 1",
            name="ck_affect_signals_arousal",
        ),
        sa.CheckConstraint(
            "scope IN ('global', 'window')", name="ck_affect_signals_scope"
        ),
    )
    op.create_index("ix_affect_signals_actor_id", "affect_signals", ["actor_id"])
    op.create_index(
        "ix_affect_signals_actor_time", "affect_signals", ["actor_id", "occurred_at"]
    )


def _column_additions() -> None:
    # activities：可空 FK，历史活动不猜关联（阶段 1 只加列，写入在阶段 2 接）。
    op.add_column("activities", sa.Column("schedule_segment_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_activities_schedule_segment_id", "activities", ["schedule_segment_id"]
    )
    op.create_foreign_key(
        "fk_activities_schedule_segment",
        "activities",
        "schedule_segments",
        ["schedule_segment_id"],
        ["id"],
    )

    # presence_states：饱腹，既有行回填 70（模拟初值，不伪造过去的餐食）。
    op.add_column(
        "presence_states",
        sa.Column("satiety", sa.Integer(), nullable=False, server_default=sa.text("70")),
    )
    op.create_check_constraint(
        "ck_presence_states_satiety", "presence_states", "satiety BETWEEN 0 AND 100"
    )

    # affect_states：Russell 二维连续状态，旧行保留 NULL。
    op.add_column("affect_states", sa.Column("valence", sa.Float(), nullable=True))
    op.add_column("affect_states", sa.Column("arousal", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_affect_states_valence", "affect_states", "valence IS NULL OR valence BETWEEN -1 AND 1"
    )
    op.create_check_constraint(
        "ck_affect_states_arousal", "affect_states", "arousal IS NULL OR arousal BETWEEN -1 AND 1"
    )


def _overlap_exclusion() -> None:
    """尽力加 `btree_gist` 的 EXCLUDE；装不上就跳过并写明原因。

    `CREATE EXTENSION` 可能需要超级用户。直接执行失败会让整个迁移事务中止，
    所以放在 ``DO`` 块的异常处理里（只回滚子事务），再查一次是否真的装上了。
    它只是额外保险：服务层 `agenda/timeline.py` 仍有事务校验。
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        print("[0051] 非 PostgreSQL：跳过 schedule_segments 的 EXCLUDE 约束")
        return
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                CREATE EXTENSION IF NOT EXISTS btree_gist;
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'btree_gist 不可用（%），跳过 EXCLUDE', SQLERRM;
            END $$;
            """
        )
    )
    installed = bind.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
    ).first()
    if installed is None:
        available = bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist'")
        ).first()
        if available is None:
            reason = "本机 PostgreSQL 没有 btree_gist（contrib 扩展包未安装）"
        else:
            reason = "当前数据库角色没有 CREATE EXTENSION 权限"
        print(
            f"[0051] {reason}：schedule_segments 的重叠防护只由服务层事务校验保证"
            "（见 domains/agenda/timeline.py），不影响建表"
        )
        return
    op.execute(
        f"""
        ALTER TABLE schedule_segments
        ADD CONSTRAINT {EXCLUSION_NAME}
        EXCLUDE USING gist (
            actor_id WITH =,
            tstzrange(start_at, end_at, '[)') WITH &&
        )
        WHERE (status = 'current' AND is_leaf)
        """
    )


def upgrade() -> None:
    # 依赖顺序：计划头 → 段（自引用/复合 FK）→ 引用计划的审计表 → 独立表。
    _day_plans()
    _schedule_segments()
    _schedule_change_requests()
    _behavior_model_calls()
    _dream_reports()
    _sleep_settlements()
    _affect_signals()
    _column_additions()
    _overlap_exclusion()


def downgrade() -> None:
    op.execute(f"ALTER TABLE schedule_segments DROP CONSTRAINT IF EXISTS {EXCLUSION_NAME}")

    op.drop_constraint("ck_affect_states_arousal", "affect_states", type_="check")
    op.drop_constraint("ck_affect_states_valence", "affect_states", type_="check")
    op.drop_column("affect_states", "arousal")
    op.drop_column("affect_states", "valence")

    op.drop_constraint("ck_presence_states_satiety", "presence_states", type_="check")
    op.drop_column("presence_states", "satiety")

    op.drop_constraint("fk_activities_schedule_segment", "activities", type_="foreignkey")
    op.drop_index("ix_activities_schedule_segment_id", table_name="activities")
    op.drop_column("activities", "schedule_segment_id")

    op.drop_table("affect_signals")
    op.drop_table("sleep_settlements")
    op.drop_table("dream_reports")
    op.drop_table("behavior_model_calls")
    op.drop_table("schedule_change_requests")
    op.drop_table("schedule_segments")
    op.drop_table("day_plans")
