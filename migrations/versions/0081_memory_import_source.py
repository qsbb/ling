"""Keep external memory provenance alongside imported records."""

import sqlalchemy as sa
from alembic import op

revision = "0081_memory_import_source"
down_revision = "0080_media_reference_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("import_source", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "import_source")
