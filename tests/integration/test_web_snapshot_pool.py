"""Web snapshots and secret refresh must make progress when the request pool is full."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ling.api.routes import chat
from ling.domains.conversation.service import ConversationService
from ling.infra import db


async def test_web_turns_progress_when_request_connections_are_exhausted(logged_in, monkeypatch):
    created = await logged_in.client.post("/v1/sessions", json={"title": "pool test"})
    assert created.status_code == 201
    window_id = created.json()["session_id"]
    original_factory = db.get_session_factory()
    engine = create_async_engine(
        logged_in.settings.database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1.5,
        connect_args={"server_settings": {"search_path": logged_in.schema}},
    )
    monkeypatch.setattr(db, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    original_refresh = chat._refresh_config_if_stale
    original_get = ConversationService.get_window
    original_stale = logged_in.app.state.revision_watcher.stale
    request_pids: list[int] = []
    refresh_pids: list[int] = []
    before_refresh = asyncio.Event()
    before_snapshot = asyncio.Event()
    release_refresh = asyncio.Event()
    release_snapshot = asyncio.Event()
    refresh_count = 0

    async def observe_stale(session):  # noqa: ANN001, ANN202
        refresh_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        return await original_stale(session)

    async def refresh_barrier(request):  # noqa: ANN001, ANN202
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            before_refresh.set()
        await release_refresh.wait()
        return await original_refresh(request)

    async def snapshot_barrier(service, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        window = await original_get(service, *args, **kwargs)
        if len(request_pids) < 2:
            request_pids.append(await service.session.scalar(text("SELECT pg_backend_pid()")))
            if len(request_pids) == 2:
                before_snapshot.set()
            await release_snapshot.wait()
        return window

    monkeypatch.setattr(logged_in.app.state.revision_watcher, "stale", observe_stale)
    monkeypatch.setattr(chat, "_refresh_config_if_stale", refresh_barrier)
    monkeypatch.setattr(ConversationService, "get_window", snapshot_barrier)
    tasks = [
        asyncio.create_task(
            logged_in.client.post(
                f"/v1/sessions/{window_id}/turns",
                json={"client_turn_id": f"pool-turn-{index}", "text": "你好"},
            )
        )
        for index in range(2)
    ]
    try:
        await asyncio.wait_for(before_refresh.wait(), 8)
        assert engine.pool.checkedout() == 2
        release_refresh.set()
        await asyncio.wait_for(before_snapshot.wait(), 8)
        assert len(set(request_pids)) == 2 and engine.pool.checkedout() == 2
        assert len(refresh_pids) == 2
        assert set(refresh_pids).isdisjoint(request_pids)
        release_snapshot.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 12)
        assert all(getattr(result, "status_code", None) == 200 for result in results), results
    finally:
        release_refresh.set()
        release_snapshot.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        monkeypatch.setattr(db, "_session_factory", original_factory)
        await engine.dispose()
