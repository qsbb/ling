"""PostgreSQL migration preserves pre-existing drafts without guessing their origin."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.w0_web


async def test_legacy_draft_survives_binding_migration_roundtrip(logged_in):
    path = Path(__file__).parents[2] / 'migrations/versions/0076_proactive_draft_binding.py'
    spec = importlib.util.spec_from_file_location('ling_test_proactive_binding_migration', path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = 'draft_binding_' + uuid.uuid4().hex[:12]
    engine = create_async_engine(logged_in.settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text('CREATE TABLE proactive_opportunities (id VARCHAR(64) PRIMARY KEY, draft_text TEXT, state VARCHAR(24))'))
            await connection.execute(text("INSERT INTO proactive_opportunities VALUES ('legacy', 'synthetic old draft', 'candidate')"))

            def migrate(sync_connection, operation):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    getattr(migration, operation)()

            await connection.run_sync(migrate, 'upgrade')
            assert await connection.scalar(text('SELECT count(*) FROM proactive_draft_bindings')) == 0
            # Old pending drafts remain editable; an old worker may not create
            # a new unbound draft after the migration.
            await connection.execute(text(
                "UPDATE proactive_opportunities SET draft_text = 'legacy edit' WHERE id = 'legacy'"
            ))
            await connection.execute(text(
                "INSERT INTO proactive_opportunities VALUES ('new', NULL, 'candidate')"
            ))
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text(
                        "UPDATE proactive_opportunities SET draft_text = 'unbound' WHERE id = 'new'"
                    ))
            await connection.execute(text(
                "UPDATE proactive_opportunities SET state = 'drafting' WHERE id = 'new'"
            ))
            await connection.execute(text(
                "INSERT INTO proactive_draft_bindings "
                "(opportunity_id, claim_token, state, target_window_id, snapshot, updated_at) "
                "VALUES ('new', 'synthetic', 'generating', 'window', CAST('{}' AS JSON), NOW())"
            ))
            await connection.execute(text(
                "UPDATE proactive_opportunities SET draft_text = 'bound draft', "
                "state = 'bound_candidate' WHERE id = 'new'"
            ))
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text(
                        "UPDATE proactive_opportunities SET draft_text = 'old late result' WHERE id = 'new'"
                    ))
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text(
                        "INSERT INTO proactive_draft_bindings (opportunity_id, claim_token, state, updated_at) "
                        "VALUES ('legacy', 'synthetic', 'invalid', NOW())"
                    ))
            await connection.execute(text(
                "INSERT INTO proactive_draft_bindings (opportunity_id, claim_token, state, updated_at) "
                "VALUES ('legacy', 'synthetic', 'ready', NOW())"
            ))
            with pytest.raises(RuntimeError, match='主动草稿待处理'):
                await connection.run_sync(migrate, 'downgrade')
            assert await connection.scalar(text('SELECT count(*) FROM proactive_draft_bindings')) == 2
            await connection.execute(text("UPDATE proactive_opportunities SET state = 'sent'"))
            await connection.run_sync(migrate, 'downgrade')
            assert await connection.scalar(text("SELECT draft_text FROM proactive_opportunities WHERE id = 'legacy'")) == 'legacy edit'
            await connection.run_sync(migrate, 'upgrade')
            assert await connection.scalar(text('SELECT count(*) FROM proactive_draft_bindings')) == 0
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest.mark.parametrize('operation', ['upgrade', 'downgrade'])
def test_non_postgres_migration_fails_before_changing_schema(monkeypatch, operation):
    path = Path(__file__).parents[2] / 'migrations/versions/0076_proactive_draft_binding.py'
    spec = importlib.util.spec_from_file_location('ling_test_unsupported_draft_binding_migration', path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration.op, 'get_context', lambda: SimpleNamespace(
        dialect=SimpleNamespace(name='sqlite'), as_sql=False,
    ))
    monkeypatch.setattr(migration.op, 'get_bind', lambda: SimpleNamespace(
        execute=lambda *_args, **_kwargs: SimpleNamespace(first=lambda: None),
    ))
    changes = []
    for name in ('create_table', 'drop_table', 'execute'):
        monkeypatch.setattr(migration.op, name, lambda *args, **kwargs: changes.append((args, kwargs)))
    with pytest.raises(RuntimeError, match='需要 PostgreSQL'):
        getattr(migration, operation)()
    assert changes == []
