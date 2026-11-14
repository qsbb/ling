"""旧 SQLite 配置启动时得到明确提示，现存文件保持原样。"""

import hashlib
import sqlite3

import pytest

from ling.app import create_app
from ling.config import Settings
from ling.infra.db import dispose_engine


@pytest.mark.parametrize("memory", [True, False], ids=["memory", "file"])
async def test_sqlite_app_startup_is_rejected_without_modifying_old_data(tmp_path, memory):
    legacy = tmp_path / "local.db"
    if memory:
        database_url = "sqlite+aiosqlite:///:memory:"
    else:
        with sqlite3.connect(legacy) as connection:
            connection.execute("CREATE TABLE user_data (value TEXT NOT NULL)")
            connection.execute("INSERT INTO user_data VALUES ('keep this data')")
        before = hashlib.sha256(legacy.read_bytes()).digest()
        database_url = f"sqlite+aiosqlite:///{legacy}"
    settings = Settings(
        env="test",
        database_url=database_url,
        db_require_postgres=False,
        db_search_path=None,
        data_dir=tmp_path,
        bootstrap_admin_user="tester",
        bootstrap_admin_password="tester-password",
        root_key="test-default-session-root-key",
        test_mode=True,
        cookie_secure=False,
        access_mode="off",
        access_list=frozenset(),
        onebot_ws_port=None,
        onebot_ws_url=None,
        qqbot_enabled=False,
    )
    await dispose_engine()
    app = create_app(settings)
    try:
        with pytest.raises(RuntimeError, match="LING_DATABASE_URL.*PostgreSQL"):
            async with app.router.lifespan_context(app):
                pytest.fail("SQLite 配置不应启动应用")
    finally:
        await dispose_engine()
    if not memory:
        assert hashlib.sha256(legacy.read_bytes()).digest() == before
