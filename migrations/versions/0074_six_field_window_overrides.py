"""Store session conversation overrides as a PostgreSQL JSONB object.

Revision ID: 0074_six_field_window_overrides
Revises: 0073_window_dialogue_overrides
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0074_six_field_window_overrides"
down_revision = "0073_window_dialogue_overrides"
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
    op.alter_column(
        "window_dialogue_overrides",
        "revision",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="1",
    )
    op.add_column(
        "window_dialogue_overrides",
        sa.Column(
            "overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE window_dialogue_overrides
            SET overrides = jsonb_build_object(
                'private_may_stay_silent', private_may_stay_silent
            )
            WHERE private_may_stay_silent IS NOT NULL
            """
        )
    )
    op.drop_column("window_dialogue_overrides", "private_may_stay_silent")
    allowed_fields = ", ".join(f"'{field}'" for field in _OVERRIDE_FIELDS)
    op.create_check_constraint(
        "ck_window_dialogue_overrides_supported_keys",
        "window_dialogue_overrides",
        sa.text(
            "jsonb_typeof(overrides) = 'object' AND "
            f"overrides - ARRAY[{allowed_fields}] = '{{}}'::jsonb"
        ),
    )
    op.create_check_constraint(
        "ck_window_dialogue_overrides_revision",
        "window_dialogue_overrides",
        sa.text("revision > 0"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    extra_overrides = connection.execute(
        sa.text(
            """
            SELECT count(*)
            FROM window_dialogue_overrides
            WHERE (overrides - 'private_may_stay_silent') <> '{}'::jsonb
            """
        )
    ).scalar_one()
    if extra_overrides:
        raise RuntimeError(
            "无法降级：会话覆盖包含旧版本无法保存的字段，请先清除这些覆盖"
        )

    op.add_column(
        "window_dialogue_overrides",
        sa.Column("private_may_stay_silent", sa.Boolean(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE window_dialogue_overrides
            SET private_may_stay_silent =
                (overrides ->> 'private_may_stay_silent')::boolean
            WHERE overrides ? 'private_may_stay_silent'
            """
        )
    )
    op.drop_constraint(
        "ck_window_dialogue_overrides_supported_keys",
        "window_dialogue_overrides",
        type_="check",
    )
    op.drop_constraint(
        "ck_window_dialogue_overrides_revision",
        "window_dialogue_overrides",
        type_="check",
    )
    op.drop_column("window_dialogue_overrides", "overrides")
    op.alter_column(
        "window_dialogue_overrides",
        "revision",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=None,
    )
