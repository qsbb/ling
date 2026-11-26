"""merge memory evidence and window contact heads

Revision ID: e55c1cf92a74
Revises: 0090_window_contact_unavailable, 0093_memory_forget_markers
Create Date: 2026-11-27 00:51:35.816612
"""

from __future__ import annotations

from collections.abc import Sequence


revision: str = 'e55c1cf92a74'
down_revision: tuple[str, str] | None = ('0090_window_contact_unavailable', '0093_memory_forget_markers')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
