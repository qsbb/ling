"""Existing local SQLite modes continue to serve a default session."""

import httpx
import pytest

from ling.app import create_app
from ling.config import Settings
from ling.infra.db import dispose_engine


@pytest.mark.parametrize("memory", [True, False], ids=["memory", "file"])
async def test_local_sqlite_default_session_remains_available(tmp_path, memory):
    database_url = (
        "sqlite+aiosqlite:///:memory:"
        if memory
        else f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    )
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
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            login = await client.post(
                "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
            )
            assert login.status_code == 200
            first = await client.get("/v1/sessions")
            assert first.status_code == 200
            assert len(first.json()["sessions"]) == 1
            session_id = first.json()["sessions"][0]["session_id"]
            second = await client.get("/v1/sessions")
            assert second.status_code == 200
            assert [row["session_id"] for row in second.json()["sessions"]] == [session_id]
    finally:
        await dispose_engine()
