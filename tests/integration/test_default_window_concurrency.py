"""Concurrent first reads must share a persisted default window."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ling.config import Settings
from ling.domains.conversation.service import ConversationService
from ling.infra.db import build_engine, create_all_for_dev
from ling.infra.models import Window


async def test_concurrent_first_session_requests_share_one_window(logged_in, monkeypatch):
    """Use separate HTTP requests and keep the first creation uncommitted."""
    first_created = asyncio.Event()
    second_entered = asyncio.Event()
    second_created = asyncio.Event()
    release_first = asyncio.Event()
    original = ConversationService.ensure_default_window
    calls = 0

    async def coordinated(service, subject_id):
        nonlocal calls
        calls += 1
        number = calls
        if number == 2:
            second_entered.set()
        window = await original(service, subject_id)
        if number == 1:
            first_created.set()
            await release_first.wait()
        elif number == 2:
            second_created.set()
        return window

    monkeypatch.setattr(ConversationService, "ensure_default_window", coordinated)
    first = asyncio.create_task(logged_in.client.get("/v1/sessions"))
    try:
        await asyncio.wait_for(first_created.wait(), 5)
        second = asyncio.create_task(logged_in.client.get("/v1/sessions"))
        try:
            await asyncio.wait_for(second_entered.wait(), 5)
            with suppress(TimeoutError):
                await asyncio.wait_for(second_created.wait(), 0.5)
        finally:
            release_first.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 10)
    finally:
        release_first.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    assert all(response.status_code == 200 for response in responses)
    ids = [response.json()["sessions"][0]["session_id"] for response in responses]
    assert ids[0] == ids[1]
    from ling.infra.db import session_scope

    async with session_scope() as session:
        windows = (await session.scalars(select(Window).where(
            Window.subject_id == logged_in.subject_id, Window.archived.is_(False),
        ))).all()
        assert [window.id for window in windows] == [ids[0]]


async def test_sqlite_concurrent_creation_and_subject_isolation(tmp_path):
    """Separate SQLite connections must recheck after a concurrent commit."""
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}",
        data_dir=tmp_path, db_require_postgres=False,
    )
    engine = build_engine(settings)
    await create_all_for_dev(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_created = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def create_first():
        async with factory() as session:
            window = await ConversationService(session, persona_id="persona_test").ensure_default_window("subject_a")
            first_created.set()
            await release_first.wait()
            await session.commit()
            return window.id

    async def create_second():
        async with factory() as session:
            service = ConversationService(session, persona_id="persona_test")
            assert await service.list_windows("subject_a") == []
            second_entered.set()
            window = await service.ensure_default_window("subject_a")
            await session.commit()
            return window.id

    first = asyncio.create_task(create_first())
    try:
        await asyncio.wait_for(first_created.wait(), 5)
        second = asyncio.create_task(create_second())
        try:
            await asyncio.wait_for(second_entered.wait(), 5)
            await asyncio.sleep(0.2)
        finally:
            release_first.set()
        first_id, second_id = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert first_id == second_id
        async with factory() as session:
            service = ConversationService(session, persona_id="persona_test")
            existing = await service.ensure_default_window("subject_a")
            other = await service.ensure_default_window("subject_b")
            await session.commit()
            assert existing.id == first_id
            assert other.id != first_id
            rows = (await session.scalars(select(Window).order_by(Window.subject_id))).all()
            assert [(row.subject_id, row.id) for row in rows] == [
                ("subject_a", first_id), ("subject_b", other.id),
            ]
    finally:
        release_first.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await engine.dispose()


async def test_postgres_existing_window_and_other_subject(logged_in):
    from ling.infra.db import session_scope

    async with session_scope() as session:
        service = ConversationService(session, persona_id="persona_test")
        original = await service.create_window(subject_id=logged_in.subject_id, title="已有会话")
        original_id = original.id
        original.title = "已重命名"

    async with session_scope() as session:
        service = ConversationService(session, persona_id="persona_test")
        existing = await service.ensure_default_window(logged_in.subject_id)
        other = await service.ensure_default_window("other_subject")
        assert existing.id == original_id
        assert existing.title == "已重命名"
        assert other.id != original_id
        assert other.subject_id == "other_subject"


async def test_first_window_is_committed_before_display_name_lookup(logged_in, monkeypatch):
    from ling.api.routes import sessions
    from ling.infra.db import session_scope

    display_started = asyncio.Event()
    finish_display = asyncio.Event()
    original = sessions._display_names

    async def delayed_display(*args, **kwargs):
        display_started.set()
        await finish_display.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(sessions, "_display_names", delayed_display)
    request = asyncio.create_task(logged_in.client.get("/v1/sessions"))
    try:
        await asyncio.wait_for(display_started.wait(), 5)
        async with session_scope() as session:
            windows = (await session.scalars(select(Window).where(
                Window.subject_id == logged_in.subject_id, Window.archived.is_(False),
            ))).all()
            assert len(windows) == 1
            window_id = windows[0].id
    finally:
        finish_display.set()
        await asyncio.wait_for(request, 5)
    assert request.result().status_code == 200
    assert request.result().json()["sessions"][0]["session_id"] == window_id
