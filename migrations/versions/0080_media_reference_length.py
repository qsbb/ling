"""Keep the full inbound media reference accepted by the v1 contract."""

import sqlalchemy as sa
from alembic import op

revision = "0080_media_reference_length"
down_revision = "0079_manual_names_and_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("media_evidence", "media_ref", existing_type=sa.String(512), type_=sa.String(2048))


def downgrade() -> None:
    # PostgreSQL refuses the narrowing when long values exist; don't cast/truncate them.
    op.alter_column("media_evidence", "media_ref", existing_type=sa.String(2048), type_=sa.String(512))
