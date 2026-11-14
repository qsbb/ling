"""Shared PostgreSQL fixtures for database behavior tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ling.infra import models as _models  # noqa: F401 - register all tables
from ling.infra.db import Base


@pytest_asyncio.fixture
async def postgres_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create an isolated schema on the local ling_test PostgreSQL database."""
    database_url = os.environ.get(
        "LING_TEST_DATABASE_URL",
        "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test",
    )
    url = make_url(database_url)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.database != "ling_test"
    ):
        pytest.fail("数据库行为测试必须使用本机 ling_test PostgreSQL 测试库")

    schema = f"unit_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
