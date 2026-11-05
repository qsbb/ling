"""Alembic 环境。目标 metadata 来自 ling.infra.models。"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from ling.infra import models as _models  # noqa: F401 - 注册全部表
from ling.infra.db import Base

config = context.config
if config.config_file_name is not None:
    # ``fileConfig`` 默认 ``disable_existing_loggers=True``，会把**已经存在**的
    # logger 全部置为 disabled —— 包括 ``ling`` 自己的那些。
    #
    # 命令行跑 alembic 时无所谓（进程马上结束）。但在**进程内**调用
    # ``alembic.command.upgrade()``（迁移链冒烟测试就是这么做的）之后，
    # 同一个进程里后续所有 ``caplog`` 断言都会拿到空列表：测试看日志的
    # 逻辑静默失效，而且是"看起来通过"的那种失效。所以这里显式关掉。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get(
        "LING_DATABASE_URL",
        config.get_main_option("sqlalchemy.url")
        or "postgresql+asyncpg://ling@127.0.0.1:55432/ling",
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), future=True)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
