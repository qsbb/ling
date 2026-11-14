"""Add platform-instance overrides and stable conversation provenance.

Downgrade drops instance overrides, stable window provenance, and attempt
configuration snapshots. These values are intentionally not folded into the
global configuration because doing so would change the meaning of existing
windows and lose their original source information.

Revision ID: 0075_conversation_scope
Revises: 0074_six_field_window_overrides
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0075_conversation_scope"
down_revision = "0074_six_field_window_overrides"
branch_labels = None
depends_on = None


_OVERRIDE_FIELDS = (
    "persona_id",
    "private_may_stay_silent",
    "group_mention_requires_reply",
    "group_attention_deferred",
    "group_turn_budget",
    "rest_may_skip",
)


def upgrade() -> None:
    op.add_column("windows", sa.Column("connector_window_ref", sa.String(256), nullable=True))
    op.add_column("windows", sa.Column("origin_scope", sa.String(16), nullable=True))
    op.add_column("windows", sa.Column("origin_instance_id", sa.String(16), nullable=True))
    op.add_column(
        "windows",
        sa.Column(
            "origin_status",
            sa.String(24),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE windows SET origin_status = 'not_applicable' WHERE channel = 'web'"
        )
    )
    # Historical platform windows retain a null independent reference. Runtime
    # lookup may fall back to title, which remains a compatibility path rather
    # than evidence of a stable connector target.
    op.create_check_constraint(
        "ck_window_origin_identity",
        "windows",
        sa.text(
            "(origin_status = 'known' AND origin_scope IS NOT NULL "
            "AND origin_scope IN ('onebot', 'qqbot') "
            "AND origin_instance_id IS NOT NULL) OR "
            "(origin_status IN ('unknown', 'not_applicable') "
            "AND origin_scope IS NULL AND origin_instance_id IS NULL)"
        ),
    )
    op.create_foreign_key(
        "fk_windows_origin_connector_instance",
        "windows",
        "connector_instances",
        ["origin_scope", "origin_instance_id"],
        ["scope", "instance_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_window_origin_web_only",
        "windows",
        sa.text(
            "(channel = 'web' AND origin_status = 'not_applicable') OR "
            "(channel <> 'web' AND origin_status <> 'not_applicable')"
        ),
    )
    op.create_index(
        "ix_windows_connector_ref",
        "windows",
        ["actor_id", "channel", "connector_window_ref"],
    )
    op.create_index(
        "ix_windows_origin_instance",
        "windows",
        ["origin_scope", "origin_instance_id", "origin_status"],
    )

    allowed_fields = ", ".join(f"'{field}'" for field in _OVERRIDE_FIELDS)
    op.create_table(
        "instance_dialogue_overrides",
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("instance_id", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "instance_id"],
            ["connector_instances.scope", "connector_instances.instance_id"],
            name="fk_instance_dialogue_overrides_connector_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope", "instance_id"),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_instance_dialogue_overrides_revision",
        ),
        sa.CheckConstraint(
            "scope IN ('onebot', 'qqbot')",
            name="ck_instance_dialogue_overrides_scope",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(overrides) = 'object' AND "
            f"overrides - ARRAY[{allowed_fields}] = '{{}}'::jsonb",
            name="ck_instance_dialogue_overrides_supported_keys",
        ),
    )

    op.create_table(
        "conversation_attempt_snapshots",
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("global_revision", sa.Integer(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("instance_scope", sa.String(16), nullable=True),
        sa.Column("instance_id", sa.String(16), nullable=True),
        sa.Column("instance_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("impact_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("origin_status", sa.String(24), nullable=False),
        sa.Column("persona_id", sa.String(64), nullable=True),
        sa.Column("persona_revision", sa.Integer(), nullable=True),
        sa.Column(
            "effective_values",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "field_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["instance_scope", "instance_id"],
            ["connector_instances.scope", "connector_instances.instance_id"],
            name="fk_conversation_attempt_connector_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("turn_id", "attempt"),
        sa.CheckConstraint(
            "(origin_status = 'known' AND instance_scope IS NOT NULL "
            "AND instance_scope IN ('onebot', 'qqbot') AND instance_id IS NOT NULL) OR "
            "(origin_status IN ('unknown', 'not_applicable') "
            "AND instance_scope IS NULL AND instance_id IS NULL)",
            name="ck_conversation_attempt_instance_identity",
        ),
        sa.CheckConstraint(
            "origin_status IN ('known', 'unknown', 'not_applicable')",
            name="ck_conversation_attempt_origin_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(effective_values) = 'object' AND "
            "effective_values - ARRAY['private_may_stay_silent', "
            "'group_mention_requires_reply', 'group_attention_deferred', "
            "'group_turn_budget', 'rest_may_skip'] = '{}'::jsonb AND "
            "effective_values ?& ARRAY['private_may_stay_silent', "
            "'group_mention_requires_reply', 'group_attention_deferred', "
            "'group_turn_budget', 'rest_may_skip']",
            name="ck_conversation_attempt_effective_values",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(field_sources) = 'object' AND "
            "field_sources - ARRAY['persona_id', 'private_may_stay_silent', "
            "'group_mention_requires_reply', 'group_attention_deferred', "
            "'group_turn_budget', 'rest_may_skip'] = '{}'::jsonb AND "
            "field_sources ?& ARRAY['persona_id', 'private_may_stay_silent', "
            "'group_mention_requires_reply', 'group_attention_deferred', "
            "'group_turn_budget', 'rest_may_skip']",
            name="ck_conversation_attempt_field_sources",
        ),
        sa.CheckConstraint("attempt > 0", name="ck_conversation_attempt_number"),
        sa.CheckConstraint(
            "global_revision >= 0 AND instance_revision >= 0 "
            "AND session_revision >= 0 AND impact_epoch >= 0 "
            "AND (persona_revision IS NULL OR persona_revision >= 0)",
            name="ck_conversation_attempt_revisions",
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO dialogue_config_guard (id, generation) VALUES (1, 0) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION bump_dialogue_config_generation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                UPDATE dialogue_config_guard
                SET generation = generation + 1
                WHERE id = 1;
                RETURN NULL;
            END;
            $$
            """
        )
    )
    triggers = (
        ("windows_insert_delete", "INSERT OR DELETE", "windows"),
        (
            "windows_policy_columns",
            "UPDATE OF actor_id, subject_id, channel, visibility, archived, "
            "origin_scope, origin_instance_id, origin_status",
            "windows",
        ),
        ("connector_instances_insert_delete", "INSERT OR DELETE", "connector_instances"),
        (
            "connector_instances_policy_columns",
            "UPDATE OF enabled, deleted_at",
            "connector_instances",
        ),
        ("connector_config_heads_insert_delete", "INSERT OR DELETE", "connector_config_heads"),
        (
            "connector_config_heads_policy_columns",
            "UPDATE OF active_revision_id, generation",
            "connector_config_heads",
        ),
        (
            "window_dialogue_overrides_insert_delete",
            "INSERT OR DELETE",
            "window_dialogue_overrides",
        ),
        (
            "window_dialogue_overrides_policy_columns",
            "UPDATE OF revision, overrides",
            "window_dialogue_overrides",
        ),
        (
            "instance_dialogue_overrides_insert_delete",
            "INSERT OR DELETE",
            "instance_dialogue_overrides",
        ),
        (
            "instance_dialogue_overrides_policy_columns",
            "UPDATE OF revision, overrides",
            "instance_dialogue_overrides",
        ),
        ("personas_insert_delete", "INSERT OR DELETE", "personas"),
        ("personas_update", "UPDATE", "personas"),
    )
    for name, events, table in triggers:
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{name}_dialogue_epoch "
                f"BEFORE {events} ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION bump_dialogue_config_generation()"
            )
        )


def downgrade() -> None:
    print(
        "warning: dropping instance overrides, window provenance, and "
        "conversation attempt snapshots"
    )
    for name, table in (
        ("windows_insert_delete", "windows"),
        ("windows_policy_columns", "windows"),
        ("connector_instances_insert_delete", "connector_instances"),
        ("connector_instances_policy_columns", "connector_instances"),
        ("connector_config_heads_insert_delete", "connector_config_heads"),
        ("connector_config_heads_policy_columns", "connector_config_heads"),
        ("window_dialogue_overrides_insert_delete", "window_dialogue_overrides"),
        ("window_dialogue_overrides_policy_columns", "window_dialogue_overrides"),
        ("instance_dialogue_overrides_insert_delete", "instance_dialogue_overrides"),
        ("instance_dialogue_overrides_policy_columns", "instance_dialogue_overrides"),
        ("personas_insert_delete", "personas"),
        ("personas_update", "personas"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{name}_dialogue_epoch ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS bump_dialogue_config_generation()"))
    op.drop_table("conversation_attempt_snapshots")
    op.drop_table("instance_dialogue_overrides")
    op.drop_index("ix_windows_origin_instance", table_name="windows")
    op.drop_index("ix_windows_connector_ref", table_name="windows")
    op.drop_constraint(
        "fk_windows_origin_connector_instance", "windows", type_="foreignkey"
    )
    op.drop_constraint("ck_window_origin_web_only", "windows", type_="check")
    op.drop_constraint("ck_window_origin_identity", "windows", type_="check")
    op.drop_column("windows", "origin_status")
    op.drop_column("windows", "origin_instance_id")
    op.drop_column("windows", "origin_scope")
    op.drop_column("windows", "connector_window_ref")
