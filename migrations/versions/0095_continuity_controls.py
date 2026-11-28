"""Persist scoped relationship preferences, handoffs, and activity shares."""

import sqlalchemy as sa
from alembic import op

revision = "0095_continuity_controls"
down_revision = "0094_continuity_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("handoff_capsules", sa.Column("source_message_id", sa.String(64)))
    op.add_column("handoff_capsules", sa.Column("target_window_id", sa.String(64)))
    op.add_column("handoff_capsules", sa.Column("publish_consent_revision", sa.Integer()))
    op.create_foreign_key(
        "fk_handoff_source_message", "handoff_capsules", "messages",
        ["source_message_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_handoff_target_window", "handoff_capsules", "windows",
        ["target_window_id"], ["id"], ondelete="CASCADE",
    )
    op.add_column("user_preferences", sa.Column("share_paused", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("user_preferences", sa.Column("share_paused_until", sa.DateTime(timezone=True)))
    op.add_column("activities", sa.Column("simulation_observed_at", sa.DateTime(timezone=True)))
    op.add_column("activities", sa.Column("simulation_action", sa.String(120)))
    op.add_column("memory_consolidations", sa.Column("review_bedtime", sa.DateTime(timezone=True)))
    op.create_table(
        "relationship_preferences",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("text", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_message_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "subject_id", "scope", "kind", "key", name="uq_relationship_preference"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["messages.id"], name="fk_relationship_preference_source", ondelete="CASCADE",
        ),
    )
    op.add_column("proactive_opportunities", sa.Column("related_memory_id", sa.String(64)))
    op.add_column("proactive_opportunities", sa.Column("target_window_id", sa.String(64)))
    op.add_column("proactive_opportunities", sa.Column("source_consent_revision", sa.Integer()))
    op.add_column("proactive_opportunities", sa.Column("source_target_fingerprint", sa.String(64)))
    op.add_column("proactive_opportunities", sa.Column("acknowledged_message_id", sa.String(64)))
    op.add_column("proactive_opportunities", sa.Column("acknowledgement", sa.String(16)))
    op.create_foreign_key(
        "fk_proactive_acknowledged_message", "proactive_opportunities", "messages",
        ["acknowledged_message_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_proactive_acknowledged_message", "proactive_opportunities", type_="foreignkey")
    for column in (
        "acknowledgement", "acknowledged_message_id", "source_consent_revision",
        "source_target_fingerprint", "target_window_id", "related_memory_id",
    ):
        op.drop_column("proactive_opportunities", column)
    op.drop_table("relationship_preferences")
    op.drop_column("memory_consolidations", "review_bedtime")
    op.drop_column("activities", "simulation_action")
    op.drop_column("activities", "simulation_observed_at")
    op.drop_column("user_preferences", "share_paused_until")
    op.drop_column("user_preferences", "share_paused")
    op.drop_constraint("fk_handoff_target_window", "handoff_capsules", type_="foreignkey")
    op.drop_constraint("fk_handoff_source_message", "handoff_capsules", type_="foreignkey")
    op.drop_column("handoff_capsules", "target_window_id")
    op.drop_column("handoff_capsules", "source_message_id")
    op.drop_column("handoff_capsules", "publish_consent_revision")
