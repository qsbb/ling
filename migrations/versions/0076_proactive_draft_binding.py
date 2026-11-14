"""Bind newly generated proactive drafts to their target and settings.

Revision ID: 0076_proactive_draft_binding
Revises: 0075_conversation_scope

Existing drafts have no binding and retain their legacy delivery behavior.
Downgrade is refused while a bound candidate or in-flight delivery exists;
otherwise it removes the remaining binding and claim records.
"""

import sqlalchemy as sa
from alembic import op

revision = "0076_proactive_draft_binding"
down_revision = "0075_conversation_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _require_postgresql()
    op.create_table(
        "proactive_draft_bindings",
        sa.Column(
            "opportunity_id", sa.String(64),
            sa.ForeignKey("proactive_opportunities.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("claim_token", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("target_window_id", sa.String(64), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('preparing', 'generating', 'ready', 'uncertain', 'cancelled')",
            name="ck_proactive_draft_binding_state",
        ),
    )
    _install_legacy_draft_guard()


def _require_postgresql() -> None:
    if op.get_context().dialect.name != "postgresql":
        raise RuntimeError("主动草稿绑定需要 PostgreSQL，请先迁移旧数据库")


def _install_legacy_draft_guard() -> None:
    """Prevent an old worker from overwriting a newly bound draft."""
    op.execute("""
            CREATE FUNCTION ling_guard_proactive_draft() RETURNS trigger AS $$
            BEGIN
              IF NEW.draft_text IS NOT NULL AND NEW.draft_text IS DISTINCT FROM OLD.draft_text THEN
                IF OLD.state = 'drafting' AND NEW.state = 'bound_candidate' AND EXISTS (
                  SELECT 1 FROM proactive_draft_bindings b
                  WHERE b.opportunity_id = OLD.id AND b.state = 'generating'
                    AND b.target_window_id IS NOT NULL AND b.snapshot IS NOT NULL
                ) THEN
                  RETURN NEW;
                END IF;
                IF OLD.draft_text IS NOT NULL AND OLD.state = 'candidate' AND NOT EXISTS (
                  SELECT 1 FROM proactive_draft_bindings b WHERE b.opportunity_id = OLD.id
                ) THEN
                  RETURN NEW;
                END IF;
                RAISE EXCEPTION '主动草稿认领已变化，请更新 worker' USING ERRCODE = '23514';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
    """)
    op.execute("""
            CREATE TRIGGER ling_guard_proactive_draft BEFORE UPDATE ON proactive_opportunities
            FOR EACH ROW EXECUTE FUNCTION ling_guard_proactive_draft()
    """)


def downgrade() -> None:
    _require_postgresql()
    if op.get_context().as_sql:
        raise RuntimeError("离线降级无法确认主动草稿状态，请先在线检查")
    op.execute("LOCK TABLE proactive_opportunities IN ACCESS EXCLUSIVE MODE")
    outstanding = op.get_bind().execute(sa.text(
        "SELECT 1 FROM proactive_draft_bindings b "
        "JOIN proactive_opportunities o ON o.id = b.opportunity_id "
        "WHERE o.state IN ('candidate', 'drafting', 'bound_candidate', 'dispatching') LIMIT 1"
    )).first()
    if outstanding is not None:
        raise RuntimeError("仍有绑定目标的主动草稿待处理，请先处理后再降级")
    op.execute("DROP TRIGGER ling_guard_proactive_draft ON proactive_opportunities")
    op.execute("DROP FUNCTION ling_guard_proactive_draft()")
    op.drop_table("proactive_draft_bindings")
