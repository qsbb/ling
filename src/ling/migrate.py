"""迁移器：schema 预检 + 建表/升级。

生产使用 Alembic（migrations/）；首次部署若还没有版本化的 Alembic 修订，
本命令退化为 ``metadata.create_all``（幂等），并明确打印所用方式。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from ling.config import get_settings
from ling.infra.db import Base, dispose_engine, init_engine


def _project_root() -> Path:
    """定位仓库根（含 alembic.ini 与 migrations/）。

    不能只用 ``__file__.parents[2]``：镜像里包可能被安装进 site-packages，
    此时该路径指向 Python 安装目录而非 /app。因此按可信度依次探测：
    1. 环境变量 LING_PROJECT_ROOT（部署时可显式指定）
    2. 从当前工作目录向上找
    3. 从包位置向上找
    """
    import os

    def _valid(candidate: Path) -> bool:
        return (candidate / "alembic.ini").exists() and (candidate / "migrations").is_dir()

    env_root = os.environ.get("LING_PROJECT_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if _valid(candidate):
            return candidate

    for base in (Path.cwd().resolve(), Path(__file__).resolve().parent):
        for candidate in [base, *base.parents]:
            if _valid(candidate):
                return candidate
    # 兜底：返回包位置的两级父目录（保持旧行为，由调用方报错）
    return Path(__file__).resolve().parents[2]


def _alembic_config():
    from alembic.config import Config

    root = _project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


async def _alembic_available() -> bool:
    root = _project_root()
    versions = root / "migrations" / "versions"
    if not versions.is_dir():
        return False
    # 跳过 macOS AppleDouble（._xxx.py）与空文件：它们不是迁移脚本，
    # Alembic 会把它们当模块加载并因 null 字节直接报错。
    for path in versions.glob("*.py"):
        if path.name.startswith("._"):
            continue
        if path.stat().st_size < 64:
            continue
        return True
    return False


async def upgrade() -> int:
    settings = get_settings()
    if settings.db_require_postgres and not settings.is_postgres:
        print("[migrate] 拒绝在非 PostgreSQL 上执行生产迁移", file=sys.stderr)
        return 2
    engine = init_engine(settings)
    if await _alembic_available():
        from alembic import command

        cfg = _alembic_config()
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        # Alembic 是同步 API：放到线程里跑，避免阻塞事件循环
        await asyncio.to_thread(command.upgrade, cfg, "head")
        print("[migrate] alembic upgrade head 完成")
    else:
        import ling.infra.models  # noqa: F401 - 注册所有表

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("[migrate] 无 Alembic 修订，已用 metadata.create_all（幂等）")
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version()"))).scalar_one()
    print(f"[migrate] 数据库版本: {str(version).split(',')[0]}")
    await dispose_engine()
    return 0


async def stamp(revision: str = "head") -> int:
    """给**已有库**登记版本，不执行建表。

    用于"库已经由 create_all 建好、现在要纳入 Alembic 管理"的场景。
    执行前必须做只读预检（见 ``preflight``），否则可能把结构差异伪装成已升级。
    """
    settings = get_settings()
    from alembic import command

    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    await asyncio.to_thread(command.stamp, cfg, revision)
    print(f"[migrate] 已登记版本 {revision}（未执行建表）")
    return 0


async def preflight() -> int:
    """只读预检：对比实际库与 metadata 的差异，不修改任何东西。

    退出码 0 表示无差异；1 表示有差异（会打印明细）。
    """
    from sqlalchemy import inspect as sa_inspect

    import ling.infra.models  # noqa: F401
    from ling.infra.db import Base

    settings = get_settings()
    engine = init_engine(settings)
    problems: list[str] = []
    async with engine.connect() as conn:
        def _tables(sync_conn) -> list[str]:
            return list(sa_inspect(sync_conn).get_table_names())

        actual_tables = set(await conn.run_sync(_tables))
        expected = {t.name for t in Base.metadata.sorted_tables}
        missing = expected - actual_tables
        if missing:
            problems.append(f"缺少表: {sorted(missing)}")
        for table in Base.metadata.sorted_tables:
            if table.name not in actual_tables:
                continue
            def _cols(sync_conn, table_name: str = table.name) -> list[dict]:
                return list(sa_inspect(sync_conn).get_columns(table_name))

            actual_cols = {c["name"] for c in await conn.run_sync(_cols)}
            expected_cols = {c.name for c in table.columns}
            missing_cols = expected_cols - actual_cols
            if missing_cols:
                problems.append(f"{table.name} 缺少列: {sorted(missing_cols)}")
    await dispose_engine()
    if problems:
        print("[preflight] 发现结构差异：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("[preflight] 实际库与 metadata 一致，可以 stamp 0001_baseline")
    return 0


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    if cmd == "upgrade":
        raise SystemExit(asyncio.run(upgrade()))
    if cmd == "stamp":
        revision = sys.argv[2] if len(sys.argv) > 2 else "head"
        raise SystemExit(asyncio.run(stamp(revision)))
    if cmd == "preflight":
        raise SystemExit(asyncio.run(preflight()))
    print(f"未知命令: {cmd}（支持 upgrade / stamp / preflight）", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
