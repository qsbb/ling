"""PostgreSQL 引擎、会话工厂与迁移入口。"""

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

from ling.config import Settings, require_database_url
from ling.infra.orm_registry import import_all_models


class Base(DeclarativeBase):
    """所有 ORM 表的基类。领域表与基础设施表都在同一个库、同一套迁移里。"""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_independent_engine: AsyncEngine | None = None
_independent_session_factory: async_sessionmaker[AsyncSession] | None = None
_engine_settings: Settings | None = None


def build_engine(
    settings: Settings, *, pool_size: int = 5, max_overflow: int = 5
) -> AsyncEngine:
    url = require_database_url(settings.database_url)
    connect_args: dict[str, object] = {}
    if settings.db_search_path:
        connect_args["server_settings"] = {"search_path": settings.db_search_path}
    return create_async_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        connect_args=connect_args,
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory, _engine_settings
    if _engine is None:
        _engine = build_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_settings = settings
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化：先调用 init_engine()")
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory, _independent_engine, _independent_session_factory
    global _engine_settings
    if _independent_engine is not None:
        await _independent_engine.dispose()
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _independent_engine = None
    _independent_session_factory = None
    _engine_settings = None


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


@asynccontextmanager
async def independent_session_scope() -> AsyncIterator[AsyncSession]:
    """短事务用独立连接池，避免等待自身已占满的请求连接池。"""
    global _independent_engine, _independent_session_factory
    if _engine is None or _engine_settings is None:
        raise RuntimeError("数据库尚未初始化：先调用 init_engine()")
    if _independent_session_factory is None:
        _independent_engine = build_engine(_engine_settings, pool_size=1, max_overflow=0)
        _independent_session_factory = async_sessionmaker(
            _independent_engine, expire_on_commit=False
        )
    async with _independent_session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def short_session_scope() -> AsyncIterator[AsyncSession]:
    """短事务使用独立连接池，避免等待自身已占满的请求连接池。"""
    async with independent_session_scope() as session:
        yield session


async def create_all_for_dev(engine: AsyncEngine) -> None:
    """开发/测试用建表。生产走 Alembic 迁移（migrations/）。"""
    import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
