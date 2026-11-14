"""Exercise short-transaction pool pressure using normal authenticated HTTP requests."""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ling.domains.conversation.service import ConversationService
from ling.infra import db
from ling.infra.models import Window


async def test_pool_saturation_during_concurrent_first_requests(logged_in, monkeypatch):
    original_factory = db.get_session_factory()
    engine = create_async_engine(
        logged_in.settings.database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1.5,
        connect_args={'server_settings': {'search_path': logged_in.schema}},
    )
    small_factory = async_sessionmaker(engine, expire_on_commit=False)
    initial_pids = []
    attempts = []
    both_initial = asyncio.Event()
    both_attempting = asyncio.Event()
    release_initial = asyncio.Event()
    release_attempts = asyncio.Event()
    original_list = ConversationService.list_windows
    original_ensure = ConversationService.ensure_default_window

    async def barrier_list(service, subject_id, **kwargs):
        result = await original_list(service, subject_id, **kwargs)
        if not result and len(initial_pids) < 2:
            pid = await service.session.scalar(text('SELECT pg_backend_pid()'))
            initial_pids.append(pid)
            if len(initial_pids) == 2:
                both_initial.set()
            await release_initial.wait()
        return result

    async def trace_ensure(service, subject_id):
        attempts.append(subject_id)
        if len(attempts) == 2:
            both_attempting.set()
        await release_attempts.wait()
        return await original_ensure(service, subject_id)

    monkeypatch.setattr(ConversationService, 'list_windows', barrier_list)
    monkeypatch.setattr(ConversationService, 'ensure_default_window', trace_ensure)
    monkeypatch.setattr(db, '_session_factory', small_factory)
    tasks = [asyncio.create_task(logged_in.client.get('/v1/sessions')) for _ in range(2)]
    try:
        await asyncio.wait_for(both_initial.wait(), 5)
        assert len(set(initial_pids)) == 2
        assert engine.pool.checkedout() == 2
        async with original_factory() as observer:
            before = (await observer.scalars(select(Window.id).where(
                Window.subject_id == logged_in.subject_id
            ))).all()
            activity = (await observer.execute(text(
                'SELECT pid, state, wait_event_type FROM pg_stat_activity WHERE pid = ANY(:pids) ORDER BY pid'
            ), {'pids': initial_pids})).all()
        assert before == []
        print(f'pool_barrier pids={initial_pids} activity={activity} status={engine.pool.status()}')
        started = time.monotonic()
        release_initial.set()
        await asyncio.wait_for(both_attempting.wait(), 2)
        assert engine.pool.checkedout() == 2
        assert all(not task.done() for task in tasks)
        print(f'pool_waiting attempts={len(attempts)} status={engine.pool.status()}')
        release_attempts.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 6)
        elapsed = time.monotonic() - started
        details = [
            {'type': type(result).__name__, 'text': str(result)[:250],
             'status': getattr(result, 'status_code', None)}
            for result in results
        ]
        async with original_factory() as observer:
            windows = (await observer.scalars(select(Window.id).where(
                Window.subject_id == logged_in.subject_id
            ))).all()
        print(f'pool_outcomes elapsed={elapsed:.3f} results={details} windows={windows} status={engine.pool.status()}')
        if any(getattr(result, 'status_code', None) != 200 for result in results):
            deadline = time.monotonic() + 2
            while engine.pool.checkedout() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            async with original_factory() as observer:
                after_activity = (await observer.execute(text(
                    'SELECT pid, state, wait_event_type FROM pg_stat_activity WHERE pid = ANY(:pids) ORDER BY pid'
                ), {'pids': initial_pids})).all()
            print(f'pool_after_cleanup status={engine.pool.status()} activity={after_activity}')
            try:
                recovery = await asyncio.wait_for(logged_in.client.get('/v1/sessions'), 4)
                print(f'pool_recovery status={recovery.status_code} body={recovery.json()}')
            except Exception as exc:
                print(f'pool_recovery error={type(exc).__name__} detail={str(exc)[:200]}')
        assert all(getattr(result, 'status_code', None) == 200 for result in results), details
        assert len(windows) == 1
        assert [result.json()['sessions'][0]['session_id'] for result in results] == windows * 2
    finally:
        release_initial.set()
        release_attempts.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        monkeypatch.setattr(db, '_session_factory', original_factory)
        await engine.dispose()
