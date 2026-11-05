"""基础设施：数据库引擎、会话工厂与迁移。

PostgreSQL 是生产权威存储；本地开发允许 sqlite 以便零依赖启动，但
``LING_REQUIRE_POSTGRES=1`` 时拒绝 sqlite，避免把开发库当成生产事实源。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ling.config import Settings


class Base(DeclarativeBase):
    """所有 ORM 表的基类。领域表与基础设施表都在同一个库、同一套迁移里。"""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    if settings.db_require_postgres and not settings.is_postgres:
        raise RuntimeError("LING_REQUIRE_POSTGRES=1 但 LING_DATABASE_URL 不是 PostgreSQL")
    if url.startswith("sqlite"):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url, future=True, pool_pre_ping=True)
    connect_args: dict[str, object] = {}
    if settings.db_search_path:
        connect_args["server_settings"] = {"search_path": settings.db_search_path}
    return create_async_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args=connect_args,
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = build_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化：先调用 init_engine()")
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """一个事务边界：成功提交，异常回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all_for_dev(engine: AsyncEngine) -> None:
    """开发/测试用建表。生产走 Alembic 迁移（migrations/）。"""
    # 必须先 import 所有模型模块，让 metadata 完整
    from ling.infra import models as _models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
