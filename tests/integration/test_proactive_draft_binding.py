"""PostgreSQL claim and target snapshot semantics for proactive drafts."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.proactive.binding import (
    begin_generation,
    claim_draft,
    equivalent_settings,
    finish_generation,
    snapshot_metadata,
    target_fingerprint,
)
from ling.infra.db import session_scope
from ling.infra.models import ProactiveDraftBinding, ProactiveOpportunity, Window
from tests.integration.test_persona_injection import _write_persona
from tests.integration.test_window_dialogue_policy import create_window

pytestmark = pytest.mark.w0_web


async def _snapshot(env, window_id):
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import runtime_window_snapshot

    async with session_scope() as session:
        config = ConfigService(session, SecretBox(env.settings),
                               test_mode_default=True, persona_id=env.settings.persona_id)
        return await runtime_window_snapshot(config, window_id)


async def _seed(env, opportunity_id):
    async with session_scope() as session:
        session.add(ProactiveOpportunity(
            id=opportunity_id, actor_id=env.settings.actor_id, subject_id=env.subject_id,
            kind='test', reason='synthetic reason', state='candidate',
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        ))


async def _fingerprint(window_id):
    async with session_scope() as session:
        window = await session.get(Window, window_id)
        assert window is not None
        return target_fingerprint(window)


async def test_draft_and_target_snapshot_are_committed_together(logged_in):
    await _write_persona(logged_in, system_prompt='SYNTHETIC_PERSONA_SECRET', traits={}, boundaries=[])
    window = await create_window(logged_in)
    await _seed(logged_in, 'opp-bound')
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint(window)
    async with session_scope() as session:
        claim = await claim_draft(session, opportunity_id='opp-bound', actor_id=logged_in.settings.actor_id)
        assert claim is not None
    async with session_scope() as session:
        assert await begin_generation(session, claim=claim, snapshot=snapshot,
                                      actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
    async with session_scope() as session:
        assert await finish_generation(session, claim=claim, text='synthetic draft') == 'ready'
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-bound')
        binding = await session.get(ProactiveDraftBinding, 'opp-bound')
        assert row.draft_text == 'synthetic draft'
        assert binding.state == 'ready' and binding.target_window_id == window
        assert binding.snapshot['persona_id'] == snapshot.resolved.persona_id.value
        assert 'SYNTHETIC_PERSONA_SECRET' not in json.dumps(binding.snapshot)


async def test_two_workers_cannot_claim_the_same_candidate(logged_in):
    await _seed(logged_in, 'opp-claim')
    locked, release = asyncio.Event(), asyncio.Event()

    async def worker(hold):
        async with session_scope() as session:
            claim = await claim_draft(session, opportunity_id='opp-claim', actor_id=logged_in.settings.actor_id)
            if hold:
                locked.set()
                await release.wait()
            return claim

    first = asyncio.create_task(worker(True))
    await asyncio.wait_for(locked.wait(), 5)
    second = asyncio.create_task(worker(False))
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 10)
    assert results[0] is not None and results[1] is None


async def test_foreign_or_archived_window_cannot_be_bound(logged_in):
    window = await create_window(logged_in)
    await _seed(logged_in, 'opp-foreign-target')
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint(window)
    async with session_scope() as session:
        claim = await claim_draft(session, opportunity_id='opp-foreign-target', actor_id=logged_in.settings.actor_id)
        row = await session.get(Window, window)
        row.subject_id = 'synthetic-other-subject'
    async with session_scope() as session:
        assert not await begin_generation(session, claim=claim, snapshot=snapshot,
                                          actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
        row = await session.get(Window, window)
        row.subject_id = logged_in.subject_id
        row.archived = True
    async with session_scope() as session:
        assert not await begin_generation(session, claim=claim, snapshot=snapshot,
                                          actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
        binding = await session.get(ProactiveDraftBinding, 'opp-foreign-target')
        assert binding.state == 'preparing' and binding.target_window_id is None


async def test_stale_preparing_can_recover_but_inflight_generation_is_uncertain(logged_in):
    now = datetime.now(UTC)
    window = await create_window(logged_in)
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint(window)
    await _seed(logged_in, 'opp-stale')
    async with session_scope() as session:
        old = await claim_draft(session, opportunity_id='opp-stale', actor_id=logged_in.settings.actor_id,
                                now=now - timedelta(minutes=11))
    async with session_scope() as session:
        new = await claim_draft(session, opportunity_id='opp-stale', actor_id=logged_in.settings.actor_id, now=now)
        assert new is not None and new.token != old.token
        assert not await begin_generation(session, claim=old, snapshot=snapshot,
                                          actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target, now=now)
        assert await begin_generation(session, claim=new, snapshot=snapshot,
                                      actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target, now=now)
    async with session_scope() as session:
        assert await claim_draft(session, opportunity_id='opp-stale', actor_id=logged_in.settings.actor_id,
                                 now=now + timedelta(minutes=11)) is None
        binding = await session.get(ProactiveDraftBinding, 'opp-stale')
        assert binding.state == 'uncertain'
        assert await finish_generation(session, claim=new, text='late result') == 'lost'
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-stale')
        assert row.draft_text is None


async def test_fallback_requires_equivalent_persona_and_policy(logged_in):
    await _write_persona(logged_in, system_prompt='synthetic persona', traits={}, boundaries=[])
    window = await create_window(logged_in)
    metadata = snapshot_metadata(await _snapshot(logged_in, window))
    same_values = deepcopy(metadata)
    same_values['fields']['group_turn_budget']['source'] = 'session'
    assert equivalent_settings(metadata, same_values)
    changed = deepcopy(metadata)
    changed['fields']['group_turn_budget']['value'] += 1
    assert not equivalent_settings(metadata, changed)
    changed = deepcopy(metadata)
    changed['persona_revision'] = (changed['persona_revision'] or 0) + 1
    assert not equivalent_settings(metadata, changed)
    changed = deepcopy(metadata)
    changed['persona_status'] = 'unavailable'
    assert not equivalent_settings(changed, changed)


async def test_changed_platform_route_is_not_the_original_bound_target(logged_in):
    from types import SimpleNamespace

    from ling.domains.proactive.dispatch import _bound_delivery_target
    from ling.infra.models import ConnectorInstance
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with session_scope() as session:
        session.add(ConnectorInstance(scope='onebot', instance_id='alpha', display_name='alpha'))
        session.add(Window(id='synthetic-platform-window', actor_id=logged_in.settings.actor_id,
            persona_id='synthetic', subject_id=logged_in.subject_id, channel='onebot',
            visibility='subject_private', origin_status='known', origin_scope='onebot',
            origin_instance_id='alpha', connector_window_ref='synthetic:private:old', title='synthetic'))
    await _seed(logged_in, 'opp-route-change')
    window = 'synthetic-platform-window'
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint('synthetic-platform-window')
    async with session_scope() as session:
        claim = await claim_draft(session, opportunity_id='opp-route-change', actor_id=logged_in.settings.actor_id)
    async with session_scope() as session:
        target = await session.get(Window, 'synthetic-platform-window')
        target.connector_window_ref = 'synthetic:private:new'
    async with session_scope() as session:
        assert not await begin_generation(session, claim=claim, snapshot=snapshot,
                                          actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
        target = await session.get(Window, 'synthetic-platform-window')
        target.connector_window_ref = 'synthetic:private:old'
    async with session_scope() as session:
        assert await begin_generation(session, claim=claim, snapshot=snapshot,
                                      actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
    async with session_scope() as session:
        assert await finish_generation(session, claim=claim, text='synthetic bound greeting') == 'ready'

    class Connector:
        def capabilities(self):
            return SimpleNamespace(enabled=True, supports_private=True, supports_proactive=True)

    async def connector_for(*args, **kwargs):
        return Connector()

    async def choose():
        async with session_scope() as session:
            opportunity = await session.get(ProactiveOpportunity, 'opp-route-change')
            binding = await session.get(ProactiveDraftBinding, 'opp-route-change')
            config = ConfigService(session, SecretBox(logged_in.settings),
                                   test_mode_default=True, persona_id=logged_in.settings.persona_id)
            return await _bound_delivery_target(session, opportunity=opportunity, binding=binding,
                config_reader=config, deliver_to_web=True, connector_for_window=connector_for)

    assert (await choose())[0].id == 'synthetic-platform-window'
    async with session_scope() as session:
        target = await session.get(Window, 'synthetic-platform-window')
        target.connector_window_ref = 'synthetic:private:new'
    assert await choose() is None


async def test_generated_draft_stays_with_its_target_or_equivalent_fallback(logged_in, monkeypatch):
    import ling.worker as worker
    from ling.domains.proactive.dispatch import dispatch_pending
    from ling.infra.security import SecretBox
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import save_window_policy as save_window_overrides
    from tests.integration.test_proactive_dispatch_policy import _publish_dialogue

    await _publish_dialogue(logged_in, {'quiet_start_minute': 0, 'quiet_end_minute': 0})
    global_prompt = 'SYNTHETIC_GLOBAL_PERSONA'
    await _write_persona(logged_in, system_prompt=global_prompt, traits={}, boundaries=[])
    one = await logged_in.client.post('/v1/personas', json={
        'display_name': 'one', 'system_prompt': 'SYNTHETIC_TARGET_PERSONA_ONE',
    })
    two = await logged_in.client.post('/v1/personas', json={
        'display_name': 'two', 'system_prompt': 'SYNTHETIC_TARGET_PERSONA_TWO',
    })
    assert one.status_code == two.status_code == 201
    first, second = await create_window(logged_in), await create_window(logged_in)
    now = datetime.now(UTC)
    async with session_scope() as session:
        first_row, second_row = await session.get(Window, first), await session.get(Window, second)
        first_row.last_message_at, second_row.last_message_at = now, now - timedelta(minutes=1)
        await save_window_overrides(session, window_id=first, expected_revision=0,
            set_values={'persona_id': one.json()['persona_id']}, clear_fields=[], user_id='tester')
        await save_window_overrides(session, window_id=second, expected_revision=0,
            set_values={'persona_id': two.json()['persona_id']}, clear_fields=[], user_id='tester')
    await _seed(logged_in, 'opp-target-test')

    class Router:
        def __init__(self):
            self.prompts = []

        async def stream(self, config, *, profile, messages, **kwargs):
            self.prompts.append('\n'.join(str(message.content) for message in messages))
            async with session_scope() as session:
                await save_window_overrides(session, window_id=first, expected_revision=1,
                    set_values={'persona_id': two.json()['persona_id']}, clear_fields=[], user_id='tester')
            yield ModelChunk(delta='synthetic bound greeting'), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text='synthetic bound greeting')

    async def no_connector(window):
        return None

    router = Router()
    monkeypatch.setattr(worker, '_MEMORY_ROUTER', router)
    monkeypatch.setattr(worker, '_connector_for_window', no_connector)
    await worker.handle_proactive_draft({})
    assert len(router.prompts) == 1
    assert 'SYNTHETIC_TARGET_PERSONA_ONE' in router.prompts[0]
    assert global_prompt not in router.prompts[0] and 'SYNTHETIC_TARGET_PERSONA_TWO' not in router.prompts[0]
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-target-test')
        binding = await session.get(ProactiveDraftBinding, row.id)
        assert row.draft_text == 'synthetic bound greeting'
        assert binding.state == 'ready' and binding.target_window_id == first
        assert binding.snapshot['persona_id'] == one.json()['persona_id']
        target = await session.get(Window, first)
        target.archived = True

    async def dispatch():
        async with session_scope() as session:
            config = ConfigService(session, SecretBox(logged_in.settings),
                                   test_mode_default=True, persona_id=logged_in.settings.persona_id)
            return await dispatch_pending(session, actor_id=logged_in.settings.actor_id, connector=None,
                                          config_reader=config, deliver_to_web=True,
                                          subject_timezone='UTC')

    blocked = await dispatch()
    assert blocked['sent'] == 0
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-target-test')
        assert row.state == 'bound_candidate' and row.last_gate_allowed is False
        await save_window_overrides(session, window_id=second, expected_revision=1,
            set_values={'persona_id': one.json()['persona_id']}, clear_fields=[], user_id='tester')
    delivered = await dispatch()
    assert delivered['sent'] == 1
    async with session_scope() as session:
        from sqlalchemy import select

        from ling.infra.models import Message

        messages = (await session.scalars(select(Message).where(Message.text == 'synthetic bound greeting'))).all()
        assert len(messages) == 1 and messages[0].window_id == second


async def test_concurrent_bound_drafting_invokes_model_once(logged_in):
    from ling.domains.proactive.bound_draft import draft_bound_pending
    from ling.infra.security import SecretBox
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk
    from ling.settings.service import ConfigService

    await create_window(logged_in)
    await _seed(logged_in, 'opp-concurrent-draft')
    entered, release = asyncio.Event(), asyncio.Event()

    class Router:
        calls = 0

        async def stream(self, config, *, profile, messages, **kwargs):
            self.calls += 1
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            yield ModelChunk(delta='one draft'), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text='one draft')

    async def no_connector(window):
        return None

    async def hint(window, snapshot, audio_supported):
        return 'synthetic voice hint', False

    router = Router()

    async def run():
        async with session_scope() as session:
            config = ConfigService(session, SecretBox(logged_in.settings),
                                   test_mode_default=True, persona_id=logged_in.settings.persona_id)
            return await draft_bound_pending(actor_id=logged_in.settings.actor_id, router=router,
                config=config, connector_for_window=no_connector, voice_help_for=hint,
                interpret_draft=lambda text: (text, 'text', False))

    first = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = await asyncio.wait_for(run(), 10)
        assert second['drafted'] == 0 and router.calls == 1
    finally:
        release.set()
    assert (await asyncio.wait_for(first, 10))['drafted'] == 1
    async with session_scope() as session:
        binding = await session.get(ProactiveDraftBinding, 'opp-concurrent-draft')
        assert binding.state == 'ready'


async def test_old_draft_worker_cannot_overwrite_a_new_bound_draft(logged_in):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from ling.domains.proactive.bound_draft import draft_bound_pending
    from ling.domains.proactive.draft import draft_pending
    from ling.infra.security import SecretBox
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk
    from ling.settings.service import ConfigService

    await _write_persona(logged_in, system_prompt='synthetic target persona', traits={}, boundaries=[])
    await create_window(logged_in)
    await _seed(logged_in, 'opp-mixed-version-draft')
    migration_path = Path(__file__).parents[2] / 'migrations/versions/0076_proactive_draft_binding.py'
    spec = importlib.util.spec_from_file_location('ling_test_draft_guard', migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def install_guard(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration._install_legacy_draft_guard()

    async with session_scope() as session:
        await (await session.connection()).run_sync(install_guard)
    old_started, release_old = asyncio.Event(), asyncio.Event()

    class Router:
        def __init__(self, text, pause):
            self.text, self.pause = text, pause

        async def stream(self, config, *, profile, messages, **kwargs):
            if self.pause:
                old_started.set()
                await asyncio.wait_for(release_old.wait(), 10)
            yield ModelChunk(delta=self.text), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=self.text)

    async def no_connector(window):
        return None

    async def hint(window, snapshot, audio_supported):
        return 'synthetic voice hint', False

    async def old_worker():
        try:
            async with session_scope() as session:
                config = ConfigService(session, SecretBox(logged_in.settings), test_mode_default=True)
                await draft_pending(session, actor_id=logged_in.settings.actor_id,
                                    router=Router('OLD_UNBOUND_DRAFT', True), config_reader=config)
            return False
        except IntegrityError:
            return True

    old = asyncio.create_task(old_worker())
    try:
        await asyncio.wait_for(old_started.wait(), 10)
        async with session_scope() as session:
            config = ConfigService(session, SecretBox(logged_in.settings), test_mode_default=True)
            outcome = await draft_bound_pending(
                actor_id=logged_in.settings.actor_id, router=Router('NEW_BOUND_DRAFT', False),
                config=config, connector_for_window=no_connector, voice_help_for=hint,
                interpret_draft=lambda text: (text, 'text', False),
            )
        assert outcome['drafted'] == 1
    finally:
        release_old.set()
        rejected = await asyncio.wait_for(old, 10)
    assert rejected, '旧 worker 的迟到写入没有被数据库拒绝'
    async with session_scope() as session:
        opportunity = await session.get(ProactiveOpportunity, 'opp-mixed-version-draft')
        binding = await session.get(ProactiveDraftBinding, 'opp-mixed-version-draft')
        assert binding.state == 'ready'
        assert opportunity.draft_text == 'NEW_BOUND_DRAFT'
        assert opportunity.state == 'bound_candidate'
        legacy_visible = await session.scalar(select(ProactiveOpportunity.id).where(
            ProactiveOpportunity.id == opportunity.id, ProactiveOpportunity.state == 'candidate',
            ProactiveOpportunity.draft_text.is_not(None),
        ))
        assert legacy_visible is None
    pending = (await logged_in.client.get('/v1/proactive')).json()['pending']
    assert any(item['opportunity_id'] == opportunity.id for item in pending)


async def test_uncertain_draft_is_visible_and_not_generated_again(logged_in, monkeypatch):
    import ling.worker as worker
    from ling.models.router import StreamOutcome

    await create_window(logged_in)
    await _seed(logged_in, 'opp-uncertain-draft')

    class Router:
        calls = 0

        async def stream(self, config, *, profile, messages, **kwargs):
            self.calls += 1
            raise RuntimeError('synthetic unknown model result')
            yield None, StreamOutcome()  # pragma: no cover - async generator

    async def no_connector(window):
        return None

    router = Router()
    monkeypatch.setattr(worker, '_MEMORY_ROUTER', router)
    monkeypatch.setattr(worker, '_connector_for_window', no_connector)
    await worker.handle_proactive_draft({})
    await worker.handle_proactive_draft({})
    assert router.calls == 1
    async with session_scope() as session:
        binding = await session.get(ProactiveDraftBinding, 'opp-uncertain-draft')
        row = await session.get(ProactiveOpportunity, 'opp-uncertain-draft')
        assert binding.state == 'uncertain' and row.draft_text is None
    response = await logged_in.client.get('/v1/proactive')
    assert response.status_code == 200
    item = next(item for item in response.json()['pending'] if item['opportunity_id'] == 'opp-uncertain-draft')
    assert item['draft_state'] == 'uncertain' and item['draft_available'] is False


async def test_unavailable_voice_cannot_be_saved_as_a_voice_draft(logged_in, monkeypatch):
    import ling.worker as worker
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk

    await create_window(logged_in)
    await _seed(logged_in, 'opp-unavailable-voice')

    class Router:
        async def stream(self, config, *, profile, messages, **kwargs):
            yield ModelChunk(delta='[speak] synthetic greeting'), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text='[speak] synthetic greeting')

    async def no_connector(window):
        return None

    monkeypatch.setattr(worker, '_MEMORY_ROUTER', Router())
    monkeypatch.setattr(worker, '_connector_for_window', no_connector)
    await worker.handle_proactive_draft({})
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-unavailable-voice')
        binding = await session.get(ProactiveDraftBinding, 'opp-unavailable-voice')
        assert row.state == 'failed' and row.draft_text is None
        assert row.rejected_reason == '草稿表达控制无效' and binding.state == 'cancelled'


async def test_target_archived_after_claim_is_not_sent(logged_in, monkeypatch):
    from sqlalchemy import select

    from ling.domains.proactive import dispatch
    from ling.infra.models import Message
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService
    from tests.integration.test_proactive_dispatch_policy import _publish_dialogue

    await _publish_dialogue(logged_in, {'quiet_start_minute': 0, 'quiet_end_minute': 0})
    window = await create_window(logged_in)
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint(window)
    await _seed(logged_in, 'opp-archive-after-claim')
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-archive-after-claim')
        row.draft_text = 'synthetic archived target message'
        session.add(ProactiveDraftBinding(opportunity_id=row.id, claim_token='synthetic-token',
            state='ready', target_window_id=window,
            snapshot={**snapshot_metadata(snapshot), 'target_fingerprint': selected_target}))
    original = dispatch._claim_opportunity

    async def archive_after_claim(session, row, *, now):
        outcome = await original(session, row, now=now)
        if outcome == 'claimed':
            async with session_scope() as other:
                target = await other.get(Window, window)
                target.archived = True
        return outcome

    monkeypatch.setattr(dispatch, '_claim_opportunity', archive_after_claim)
    async with session_scope() as session:
        config = ConfigService(session, SecretBox(logged_in.settings),
                               test_mode_default=True, persona_id=logged_in.settings.persona_id)
        result = await dispatch.dispatch_pending(session, actor_id=logged_in.settings.actor_id,
            connector=None, config_reader=config, deliver_to_web=True, subject_timezone='UTC')
    assert result['sent'] == 0
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-archive-after-claim')
        messages = (await session.scalars(select(Message).where(
            Message.text == 'synthetic archived target message',
        ))).all()
        assert row.state == 'bound_candidate' and messages == []


async def test_fallback_settings_change_after_claim_is_not_sent(logged_in, monkeypatch):
    from sqlalchemy import select

    from ling.domains.proactive import dispatch
    from ling.infra.models import Message
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import save_window_policy as save_window_overrides
    from tests.integration.test_proactive_dispatch_policy import _publish_dialogue

    await _publish_dialogue(logged_in, {'quiet_start_minute': 0, 'quiet_end_minute': 0})
    await _write_persona(logged_in, system_prompt='synthetic original persona', traits={}, boundaries=[])
    first, fallback = await create_window(logged_in), await create_window(logged_in)
    snapshot = await _snapshot(logged_in, first)
    selected_target = await _fingerprint(first)
    await _seed(logged_in, 'opp-fallback-after-claim')
    async with session_scope() as session:
        target = await session.get(Window, first)
        target.archived = True
        row = await session.get(ProactiveOpportunity, 'opp-fallback-after-claim')
        row.draft_text = 'synthetic fallback message'
        session.add(ProactiveDraftBinding(opportunity_id=row.id, claim_token='synthetic-token',
            state='ready', target_window_id=first,
            snapshot={**snapshot_metadata(snapshot), 'target_fingerprint': selected_target}))
    original = dispatch._claim_opportunity

    async def change_after_claim(session, row, *, now):
        outcome = await original(session, row, now=now)
        if outcome == 'claimed':
            async with session_scope() as other:
                await save_window_overrides(other, window_id=fallback, expected_revision=0,
                    set_values={'persona_id': None}, clear_fields=[], user_id='tester')
        return outcome

    monkeypatch.setattr(dispatch, '_claim_opportunity', change_after_claim)
    async with session_scope() as session:
        config = ConfigService(session, SecretBox(logged_in.settings),
                               test_mode_default=True, persona_id=logged_in.settings.persona_id)
        result = await dispatch.dispatch_pending(session, actor_id=logged_in.settings.actor_id,
            connector=None, config_reader=config, deliver_to_web=True, subject_timezone='UTC')
    assert result['sent'] == 0
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-fallback-after-claim')
        messages = (await session.scalars(select(Message).where(Message.text == 'synthetic fallback message'))).all()
        assert row.state == 'bound_candidate' and messages == []


async def test_platform_route_change_after_claim_does_not_send_old_address(logged_in, monkeypatch):
    from ling.domains.proactive import dispatch
    from ling.infra.models import ConnectorInstance
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService
    from tests.integration.test_proactive_dispatch import _Connector
    from tests.integration.test_proactive_dispatch_policy import _publish_dialogue

    await _publish_dialogue(logged_in, {'quiet_start_minute': 0, 'quiet_end_minute': 0})
    async with session_scope() as session:
        session.add(ConnectorInstance(scope='onebot', instance_id='alpha', display_name='alpha'))
        session.add(Window(id='synthetic-bound-platform', actor_id=logged_in.settings.actor_id,
            persona_id='synthetic', subject_id=logged_in.subject_id, channel='onebot',
            visibility='subject_private', origin_status='known', origin_scope='onebot',
            origin_instance_id='alpha', connector_window_ref='synthetic:private:old', title='synthetic'))
    await _seed(logged_in, 'opp-route-after-claim')
    window = 'synthetic-bound-platform'
    snapshot = await _snapshot(logged_in, window)
    selected_target = await _fingerprint('synthetic-bound-platform')
    async with session_scope() as session:
        claim = await claim_draft(session, opportunity_id='opp-route-after-claim', actor_id=logged_in.settings.actor_id)
    async with session_scope() as session:
        assert await begin_generation(session, claim=claim, snapshot=snapshot,
                                      actor_id=logged_in.settings.actor_id, target_window_id=window, expected_target=selected_target)
    async with session_scope() as session:
        assert await finish_generation(session, claim=claim, text='synthetic platform greeting') == 'ready'
    connector = _Connector()

    async def connector_for(*args, **kwargs):
        return connector

    original = dispatch._claim_opportunity

    async def rebind_after_claim(session, row, *, now):
        result = await original(session, row, now=now)
        if result == 'claimed':
            async with session_scope() as other:
                window = await other.get(Window, 'synthetic-bound-platform')
                window.connector_window_ref = 'synthetic:private:new'
        return result

    monkeypatch.setattr(dispatch, '_claim_opportunity', rebind_after_claim)
    async with session_scope() as session:
        config = ConfigService(session, SecretBox(logged_in.settings),
                               test_mode_default=True, persona_id=logged_in.settings.persona_id)
        result = await dispatch.dispatch_pending(session, actor_id=logged_in.settings.actor_id,
            connector=None, connector_for_window=connector_for, config_reader=config,
            deliver_to_web=True, subject_timezone='UTC')
    assert result['sent'] == 0
    assert connector.sent == []
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, 'opp-route-after-claim')
        assert row.state == 'bound_candidate'


async def test_bound_web_voice_rechecks_archive_after_tts(logged_in, monkeypatch):
    import ling.worker as worker
    from tests.integration.test_proactive_voice_worker_acceptance import (
        _prepare_candidate,
        _wait_for_tts,
    )
    from tests.integration.test_proactive_voice_worker_acceptance import (
        _snapshot as voice_snapshot,
    )
    from tests.integration.test_voice_reply_acceptance import _VoiceModelEndpoint, _VoiceTtsEndpoint

    tts = _VoiceTtsEndpoint(pause=True)
    candidate = await _prepare_candidate(logged_in, monkeypatch, channel='web',
        model=_VoiceModelEndpoint('unused'), tts=tts)
    snapshot = await _snapshot(logged_in, candidate.window_id)
    selected_target = await _fingerprint(candidate.window_id)
    async with session_scope() as session:
        session.add(ProactiveDraftBinding(opportunity_id=candidate.opportunity_id,
            claim_token='synthetic-token', state='ready', target_window_id=candidate.window_id,
            snapshot={**snapshot_metadata(snapshot), 'target_fingerprint': selected_target}))
    task = asyncio.create_task(worker.handle_proactive_dispatch({}))
    try:
        await _wait_for_tts(task, tts)

        async def archive():
            async with session_scope() as session:
                window = await session.get(Window, candidate.window_id)
                window.archived = True

        await asyncio.wait_for(archive(), 5)
        tts.release.set()
        await asyncio.wait_for(task, 20)
    finally:
        tts.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    opportunity, _claim, messages, jobs = await voice_snapshot(candidate)
    assert opportunity.state == 'failed' and opportunity.sent_at is None
    assert len(tts.calls) == len(messages) == len(jobs) == 1
    assert messages[0].speech_state == 'cancelled' and messages[0].audio_asset_id is None
