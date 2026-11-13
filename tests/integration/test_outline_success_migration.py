"""验证旧日程升级后不猜测模型大纲的成功时间。"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_upgrade_and_downgrade_keep_legacy_rows(web_env: TestEnv) -> None:  # noqa: ARG001
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    migration = ScriptDirectory.from_config(config).get_revision("0071_outline_success_time")
    assert migration is not None

    def check(connection) -> None:  # noqa: ANN001
        connection.execute(sa.text(
            "CREATE TEMPORARY TABLE day_plans "
            "(id varchar(64) primary key, state varchar(16), updated_at timestamptz) ON COMMIT DROP"
        ))
        connection.execute(sa.text(
            "INSERT INTO day_plans VALUES ('legacy-ready', 'ready', '2026-11-08 09:00:00+00')"
        ))
        with Operations.context(MigrationContext.configure(connection)):
            migration.module.upgrade()
            row = connection.execute(sa.text(
                "SELECT state, updated_at, last_model_outline_at FROM pg_temp.day_plans"
            )).one()
            assert row.state == "ready"
            assert row.last_model_outline_at is None
            migration.module.downgrade()
            exists = connection.execute(sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_attribute "
                "WHERE attrelid = 'pg_temp.day_plans'::regclass "
                "AND attname = 'last_model_outline_at' AND NOT attisdropped)"
            )).scalar_one()
            assert exists is False
            migration.module.upgrade()
            row_after = connection.execute(sa.text(
                "SELECT state, updated_at, last_model_outline_at FROM pg_temp.day_plans"
            )).one()
            assert row_after == row

    async with session_scope() as session:
        connection = await session.connection()
        await connection.run_sync(check)
