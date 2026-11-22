"""Merge the external plugin and persona dialogue migration branches."""

revision = "0084_merge_plugin_persona"
down_revision = ("0083_external_plugins_v1", "0083_persona_dialogue_options")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
