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
        # sqlite 的默认 busy_timeout 是 **0**：第二个写入者立刻收到
        # `database is locked`，而不是等前者提交。本地开发/单机部署里
        # 这几乎必然发生——额度记账就正好撞在一轮对话的事务还没提交时，
        # 于是记账失败、用量永远显示 0。
        #
        # 给 5 秒等待窗口即可：写账是几十毫秒的事，等一等就能成功，
        # 而真出现长时间持锁（例如另一个进程在跑迁移）时仍会失败，
        # 那时失败是**对的**——不该无限期卡住回复。
        # WAL 是这里的关键：默认的 rollback journal 下，一个长事务（一轮对话
        # 从开始到提交）会把**所有**写入者挡在门外，而额度记账恰好发生在
        # 这轮事务还没提交的时候。开 WAL 后读不再阻塞写，写入者只需等彼此，
        # 配合上面的 busy_timeout 就能排上队。
        engine = create_async_engine(
            url, future=True, pool_pre_ping=True, connect_args={"timeout": 5.0}
        )

        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            # 记账必须真的落盘：进程崩掉时丢的应该是最后一轮的估算值，
            # 而不是整天的账。
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine
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
