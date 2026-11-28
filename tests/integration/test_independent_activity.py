"""独立验收的活动与主动分享场景。只在冻结的验收快照执行。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from ling import worker
from ling.domains.activity.service import ActivityService
from ling.domains.proactive.dispatch import dispatch_pending
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Message, ProactiveOpportunity, Window, WorldPlace
from ling.infra.security import SecretBox
from ling.kernel.continuity_sharing import share_decision
from ling.settings.service import ConfigService
from tests.integration.conftest import TestEnv
from tests.integration.test_onebot_loop import CFG, FakeOneBot, _bind
from tests.integration.test_proactive_dispatch_policy import _publish_dialogue

from .test_independent_onebot import _completed_prompt, _consent, _install, _qq, _row

pytestmark = pytest.mark.w0_web
FACT = "我喜欢青桐街。"


class _Platform(FakeOneBot):
    def __init__(self) -> None:
        super().__init__()
        self.result = "success"
        self.confirmed: list[dict] = []
        self.receipt_requested = 0
        self.sending: asyncio.Event | None = None
        self.resume: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get_friend_msg_history":
            self.receipt_requested += 1
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"messages": self.confirmed}})
        response = await super().handler(request)
        if request.url.path != "/send_private_msg":
            return response
        if self.sending is not None and self.resume is not None:
            self.sending.set()
            await self.resume.wait()
        if self.result == "timeout":
            raise httpx.ReadTimeout("fixture: request received but response lost", request=request)
        if self.result == "failure":
            return httpx.Response(200, json={"status": "failed", "retcode": 100, "data": None})
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9000 + len(self.sent)}},
        )


async def _base(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, *, quiet_at: datetime | None = None,
    route_id: str = CFG.instance_id,
) -> tuple[_Platform, str]:
    transport = _Platform()
    connector = await _install(
        logged_in, transport, config=replace(CFG, supports_proactive=True), route_instance=route_id,
    )
    await _bind(logged_in)
    await _consent(logged_in, "proactive_contact")
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    monkeypatch.setattr(worker, "get_settings", lambda: logged_in.settings)
    preferences = {"proactive_daily_limit": 5}
    if quiet_at is not None:
        local = quiet_at.astimezone(ZoneInfo("Asia/Shanghai"))
        minute = local.hour * 60 + local.minute
        preferences.update(quiet_start_minute=(minute - 1) % 1440, quiet_end_minute=(minute + 1) % 1440)
    await _publish_dialogue(logged_in, preferences)
    result = await _qq(logged_in, 682001, FACT, instance_id=route_id)
    await _completed_prompt(result)
    source = await _row(FACT)
    async with session_scope() as session:
        window = await session.get(Window, source.window_id)
        assert window is not None
        past = datetime.now(UTC) - timedelta(days=1)
        window.last_message_at = past
        for message in await session.scalars(select(Message).where(Message.window_id == window.id)):
            message.created_at = past
        session.add(WorldPlace(id="qa-qingtong", actor_id=logged_in.settings.actor_id, name="青桐街"))
        session.add(WorldPlace(id="qa-room", actor_id=logged_in.settings.actor_id, name="房间"))
    assert await worker.drain_outbox(limit=3, only_topic="memory.extract") == 1

    async def for_draft(window: Window):
        assert window.id == source.window_id
        return connector

    async def for_dispatch(*_args, **_kwargs):
        return connector

    monkeypatch.setattr(worker, "_connector_for_window", for_draft)
    monkeypatch.setattr(worker, "_group_reply_connector", for_dispatch)
    return transport, source.window_id


async def _activity(
    env: TestEnv, *, place_id: str, state: str, evidence_kind: str | None = None,
) -> str:
    async with session_scope() as session:
        service = ActivityService(session, actor_id=env.settings.actor_id)
        activity = await service.propose(kind="walking", place_id=place_id, origin="simulated")
        if state != "cancelled":
            await service.transition(activity.activity_id, "running")
        kwargs = {}
        if state == "completed":
            kwargs["completion_evidence"] = (
                "完成青桐街散步" if place_id == "qa-qingtong" else "在房间独自走了一圈"
            )
        if evidence_kind:
            kwargs["evidence_kind"] = evidence_kind
        await service.transition(activity.activity_id, state, **kwargs)
        return activity.activity_id


async def _activity_history(env: TestEnv) -> dict[str, dict]:
    response = await env.client.get("/v1/activity")
    assert response.status_code == 200, response.text
    return {entry["activity_id"]: entry for entry in response.json()["history"]}


async def _candidates() -> list[ProactiveOpportunity]:
    async with session_scope() as session:
        return list(await session.scalars(
            select(ProactiveOpportunity).where(ProactiveOpportunity.reason_code == "activity_done")
        ))


async def _proactive_history(env: TestEnv, candidate_id: str) -> dict:
    response = await env.client.get("/v1/proactive")
    assert response.status_code == 200, response.text
    result = response.json()
    rows = result["recent"] + result["pending"]
    return next(row for row in rows if row["opportunity_id"] == candidate_id)


async def test_only_related_completed_activity_enters_shares(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _transport, window_id = await _base(logged_in, monkeypatch)
    relevant = await _activity(
        logged_in, place_id="qa-qingtong", state="completed", evidence_kind="simulated_outcome",
    )
    independent = await _activity(
        logged_in, place_id="qa-room", state="completed", evidence_kind="simulated_outcome",
    )
    cancelled = await _activity(logged_in, place_id="qa-qingtong", state="cancelled")
    clock_only = await _activity(logged_in, place_id="qa-qingtong", state="completed")

    timeline = await _activity_history(logged_in)
    assert timeline[relevant]["state"] == timeline[independent]["state"] == "completed"
    assert timeline[cancelled]["state"] == "cancelled"
    assert timeline[clock_only]["state"] == "completed"
    await worker.handle_proactive_collect({})
    await worker.handle_proactive_collect({})
    candidates = await _candidates()
    assert len(candidates) == 1, [(row.id, row.source_event_id) for row in candidates]
    share = candidates[0]
    assert share.subject_id == logged_in.subject_id and not share.reply_expected
    assert share.source_event_id and relevant in share.source_event_id
    assert share.best_until and share.not_before and share.best_until > share.not_before
    web = await _proactive_history(logged_in, share.id)
    assert web.get("activity_id") == relevant and web.get("target_window_id") == window_id, web
    assert web.get("related_memory_id"), web


async def _dispatch_at(env: TestEnv, now: datetime) -> dict:
    async with session_scope() as session:
        config = ConfigService(
            session, SecretBox(env.settings), test_mode_default=env.settings.test_mode,
            persona_id=env.settings.persona_id,
        )
        return await dispatch_pending(
            session, actor_id=env.settings.actor_id, connector=None, config_reader=config,
            subject_timezone="Asia/Shanghai", connector_for_window=worker._group_reply_connector,
            deliver_to_web=True, now=now,
            review_share=lambda row, moment: share_decision(session, row, now=moment),
        )


@pytest.mark.parametrize("result", ["late_confirmation", "revoked_after_send"])
async def test_delivery_completion_keeps_actual_result_and_uses_default_bot_route(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, result: str,
) -> None:
    transport, window_id = await _base(logged_in, monkeypatch, route_id="default")
    await _activity(logged_in, place_id="qa-qingtong", state="completed", evidence_kind="simulated_outcome")
    await worker.handle_proactive_collect({})
    await worker.handle_proactive_draft({})
    candidate, = await _candidates()
    initial_calls = len(transport.sent)
    if result == "late_confirmation":
        transport.result = "timeout"
        await worker.handle_proactive_dispatch({})
        async with session_scope() as session:
            row = await session.get(ProactiveOpportunity, candidate.id)
            assert row is not None and row.state == "uncertain" and row.sent_at is None
        transport.confirmed = [{
            "message_id": 901282, "sender": {"user_id": int(CFG.self_id)},
            "message": transport.sent[-1]["body"]["message"], "time": int(datetime.now(UTC).timestamp()),
        }]
        from ling.domains.goals import delivery_reconcile

        monkeypatch.setattr(delivery_reconcile, "RECONCILE_PAUSE_SECONDS", 0)
        resolver = worker._group_reply_connector

        async def reconcile_connector(channel, window_ref, *, instance_key=None):
            assert (channel, instance_key) == ("onebot", ("onebot", "default"))
            assert window_ref.startswith(f"{CFG.instance_id}:private:")
            return await resolver(channel, window_ref, instance_key=instance_key)

        monkeypatch.setattr(worker, "_group_reply_connector", reconcile_connector)
        await worker.handle_delivery_reconcile({})
        await worker.handle_delivery_reconcile({})
        assert transport.receipt_requested == 1
    else:
        transport.sending, transport.resume = asyncio.Event(), asyncio.Event()
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.handle_proactive_dispatch({}))
            try:
                await asyncio.wait_for(transport.sending.wait(), timeout=5)
                await _consent(logged_in, "proactive_contact", granted=False)
            finally:
                transport.resume.set()
        await worker.handle_proactive_collect({})
        await worker.handle_proactive_dispatch({})

    assert len(transport.sent) == initial_calls + 1
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, candidate.id)
        assert row is not None and row.state == "sent" and row.sent_at and row.delivery_operation_id
        receipt = await session.scalar(select(DeliveryReceiptRow).where(
            DeliveryReceiptRow.operation_id == row.delivery_operation_id,
            DeliveryReceiptRow.state.in_(("sent", "delivered")),
        ))
        assert receipt is not None
        history = list(await session.scalars(select(Message).where(
            Message.window_id == window_id, Message.role == "actor", Message.text == row.draft_text,
        )))
        assert len(history) == 1
        if result == "revoked_after_send":
            from ling.kernel.continuity_sharing import share_context

            window = await session.get(Window, window_id)
            assert window is not None
            assert await share_context(session, window=window, subject_id=row.subject_id, text="青桐街") == ""


async def test_qq_user_reminder_is_delivered_during_share_pause(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling.infra.models import CommitmentRow

    transport, window_id = await _base(logged_in, monkeypatch, route_id="default")
    due = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=2)
    content = "查看青桐街的演出时间"
    request = f"请在 {due:%Y-%m-%d %H:%M} 提醒我{content}"
    before = len(transport.sent)
    booked = await _qq(logged_in, 682004, request, instance_id="default")
    assert booked["delivered"] and "已约好" in str(transport.sent[before:])
    before = len(transport.sent)
    paused = await _qq(logged_in, 682005, "暂停主动联系", instance_id="default")
    assert paused["delivered"] and "约好的提醒仍会处理" in str(transport.sent[before:])
    async with session_scope() as session:
        reminder = await session.scalar(select(CommitmentRow).where(CommitmentRow.text == content))
        assert reminder is not None and reminder.window_id == window_id and reminder.state == "active"
        reminder_id = reminder.id
        reminder.due_at = datetime.now(UTC) - timedelta(seconds=1)

    original_count = len(transport.sent)
    await worker.handle_commitment_dispatch({})
    await worker.handle_commitment_dispatch({})
    assert len(transport.sent) == original_count + 1
    assert transport.sent[-1]["action"] == "send_private_msg" and content in str(transport.sent[-1]["body"])
    async with session_scope() as session:
        reminder = await session.get(CommitmentRow, reminder_id)
        assert reminder is not None and reminder.state == "delivered" and reminder.delivery_operation_id
        assert await session.scalar(select(DeliveryReceiptRow.id).where(
            DeliveryReceiptRow.operation_id == reminder.delivery_operation_id,
            DeliveryReceiptRow.state.in_(("sent", "delivered")),
        ))


@pytest.mark.parametrize("mode", ["success", "quiet", "topic", "paused", "failure", "timeout"])
async def test_same_activity_candidate_reaches_only_allowed_qq_delivery(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    quiet_at = datetime.now(UTC) + timedelta(minutes=5) if mode == "quiet" else None
    transport, window_id = await _base(logged_in, monkeypatch, quiet_at=quiet_at)
    await _activity(
        logged_in, place_id="qa-qingtong", state="completed", evidence_kind="simulated_outcome",
    )
    await worker.handle_proactive_collect({})
    candidate, = await _candidates()
    class _UnexpectedModel:
        calls = 0

        async def stream(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("具备确定性来源的生活分享应直接生成，不能调用模型")

    model = _UnexpectedModel()
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", model)
    await worker.handle_proactive_draft({})
    assert model.calls == 0
    async with session_scope() as session:
        before = await session.get(ProactiveOpportunity, candidate.id)
        assert before is not None and before.draft_text, before
        share_text = before.draft_text
    assert "青桐街" in share_text and "想起" in share_text, share_text
    assert "怀表" not in share_text and "铜铃" not in share_text, share_text

    if mode == "topic":
        await _qq(logged_in, 682002, "别再提青桐街")
    elif mode == "paused":
        await _qq(logged_in, 682003, "暂停主动联系")
    elif mode in {"failure", "timeout"}:
        transport.result = mode
    baseline_platform_calls = len(transport.sent)
    if quiet_at is None:
        await worker.handle_proactive_dispatch({})
        await worker.handle_proactive_dispatch({})
    else:
        await _dispatch_at(logged_in, quiet_at)
        assert len(transport.sent) == baseline_platform_calls
        async with session_scope() as session:
            deferred = await session.get(ProactiveOpportunity, candidate.id)
            assert deferred is not None and deferred.state in {"candidate", "bound_candidate"}
            assert deferred.last_gate_allowed is False and deferred.sent_at is None
        awake_at = quiet_at + timedelta(minutes=3)
        await _dispatch_at(logged_in, awake_at)
        await _dispatch_at(logged_in, awake_at)
    platform_calls = len(transport.sent) - baseline_platform_calls
    blocked = mode in {"topic", "paused"}
    assert platform_calls == (0 if blocked else 1), transport.sent[baseline_platform_calls:]

    delivered = mode in {"success", "quiet"}
    if platform_calls:
        assert share_text in str(transport.sent[-1]["body"]), transport.sent[-1]
    async with session_scope() as session:
        after = await session.get(ProactiveOpportunity, candidate.id)
        assert after is not None
        if delivered:
            assert after.sent_at is not None and after.delivery_operation_id and after.state == "sent"
            receipt = await session.scalar(select(DeliveryReceiptRow).where(
                DeliveryReceiptRow.operation_id == after.delivery_operation_id,
            ))
            assert receipt is not None and receipt.state in {"delivered", "sent"}
        elif mode == "timeout":
            assert after.sent_at is None and after.delivery_state == "uncertain"
        else:
            assert after.sent_at is None and after.state != "sent"
        visible = list(await session.scalars(select(Message).where(
            Message.window_id == window_id, Message.role == "actor", Message.text == share_text,
        )))
        assert bool(visible) is delivered, [(m.id, m.text) for m in visible]
        assert len(visible) <= 1


async def test_calendar_advances_offline_and_uses_only_a_simulated_outcome(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling.infra.models import ActivityRow, ScheduleSegmentRow
    from tests.integration.test_activity_boundaries import _insert_segment
    from tests.integration.test_life_timeline import _insert_plan

    _transport, _window_id = await _base(logged_in, monkeypatch)
    # Choose an afternoon earlier than the test clock. The source exchange was
    # one day earlier, so both the forward and replayed worker see the facts.
    local = datetime.now(ZoneInfo("Asia/Shanghai"))
    if local.hour < 10:
        local = (local - timedelta(days=1)).replace(hour=11)
    else:
        local = local.replace(hour=8)
    base = local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
    plan = await _insert_plan(local.date())
    inputs = [
        ("去青桐街散步", "qa-qingtong"),
        ("去房间走一圈", "qa-room"),
        ("再去青桐街散步", "qa-qingtong"),
        ("准备去青桐街散步", "qa-qingtong"),
    ]
    segments = []
    for index, (title, place) in enumerate(inputs):
        start = base + timedelta(minutes=25 * index)
        end = start + timedelta(minutes=20)
        segment = await _insert_segment(plan, start_at=start, end_at=end, title=title, kind="walk")
        async with session_scope() as session:
            row = await session.get(ScheduleSegmentRow, segment)
            assert row is not None
            row.place_id = place
        segments.append((segment, start, end))

    async def tick(moment: datetime) -> None:
        monkeypatch.setattr(worker, "utcnow", lambda: moment)
        await worker.handle_life_tick({})

    async def activities() -> dict[str, ActivityRow]:
        async with session_scope() as session:
            rows = list(await session.scalars(select(ActivityRow)))
            return {row.schedule_segment_id: row for row in rows if row.schedule_segment_id}

    for index, (segment, start, end) in enumerate(segments):
        await tick(start + timedelta(minutes=1))
        running = await activities()
        assert running[segment].state == "running", (segment, running)
        if index < 3:
            await tick(start + timedelta(minutes=12))
        if index == 2:
            async with session_scope() as session:
                stopped = await session.get(ScheduleSegmentRow, segment)
                assert stopped is not None
                stopped.status = "cancelled"
            await tick(start + timedelta(minutes=13))
        await tick(end + timedelta(minutes=1))

    expected = await activities()
    keys = [row_id for row_id, _start, _end in segments]
    assert expected[keys[0]].state == expected[keys[1]].state == "completed", {
        key: entry.state for key, entry in expected.items()
    }
    assert expected[keys[2]].state == "cancelled", expected[keys[2]].state
    assert expected[keys[3]].state in {"paused", "cancelled"}, expected[keys[3]].state
    with pytest.MonkeyPatch.context() as replay:
        replay.setattr(worker, "utcnow", lambda: segments[-1][2] + timedelta(minutes=2))
        await worker.handle_life_tick({})
    assert {key: row.id for key, row in (await activities()).items()} == {
        key: row.id for key, row in expected.items()
    }

    await worker.handle_proactive_collect({})
    share, = await _candidates()
    assert expected[keys[0]].id in share.source_event_id


async def _accepted_share(logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[_Platform, str]:
    transport, _window_id = await _base(logged_in, monkeypatch)
    await _activity(
        logged_in, place_id="qa-qingtong", state="completed", evidence_kind="simulated_outcome",
    )
    await worker.handle_proactive_collect({})
    candidate, = await _candidates()
    return transport, candidate.id


@pytest.mark.parametrize(
    "text,level,summary",
    [
        ("青桐街我看到了。", "received", "对方说已收到"),
        ("青桐街我明白你的意思。", "agreed", "对方明确认可"),
        ("关于青桐街，我之前理解错了。", "misunderstood", "不同的理解"),
    ],
)
async def test_acknowledgement_requires_an_explicit_user_message(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, text: str, level: str, summary: str,
) -> None:
    _transport, candidate_id = await _accepted_share(logged_in, monkeypatch)
    await worker.handle_proactive_dispatch({})
    before = await _completed_prompt(await _qq(logged_in, 686001, "你给我发过青桐街的事吗？"))
    assert "已确认发出" in str(before["messages"]), before["messages"]
    assert "是否阅读、理解或认可仍未知" in str(before["messages"]), before["messages"]

    result = await _qq(logged_in, 686002, text)
    await _completed_prompt(result)
    user_message = await _row(text)
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, candidate_id)
        assert row is not None and row.state == "sent", row
        assert row.acknowledgement == level and row.acknowledged_message_id == user_message.id
    after = await _completed_prompt(await _qq(logged_in, 686003, "我们后来怎么聊青桐街的？"))
    assert summary in str(after["messages"]), after["messages"]


async def test_user_can_revoke_queued_share_before_platform_call(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, candidate_id = await _accepted_share(logged_in, monkeypatch)
    calls = len(transport.sent)
    await _consent(logged_in, "proactive_contact", granted=False)
    await worker.handle_proactive_dispatch({})
    await _consent(logged_in, "proactive_contact")
    await worker.handle_proactive_dispatch({})
    assert len(transport.sent) == calls
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, candidate_id)
        assert row is not None and row.sent_at is None
        assert row.state == "rejected" and row.draft_text is None
    history = await _proactive_history(logged_in, candidate_id)
    assert "主动联系授权已失效" in str(history), history


async def test_related_chat_can_share_then_consume_candidate_without_duplicate(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk

    transport, candidate_id = await _accepted_share(logged_in, monkeypatch)
    unrelated = await _completed_prompt(await _qq(logged_in, 687001, "刚才跟同事吵架了，有点烦。"))
    assert "刚在模拟活动中经过青桐街" not in str(unrelated["messages"])
    before = len(transport.sent)
    await worker.handle_proactive_dispatch({})
    assert len(transport.sent) == before
    async with session_scope() as session:
        item = await session.get(ProactiveOpportunity, candidate_id)
        assert item is not None and item.state == "candidate" and item.sent_at is None

    router = logged_in.app.state.router
    original_stream = router.stream
    naturally_shared = "我刚才在青桐街走了一圈，想起你聊过这条街。"

    async def generate(config, *, profile, messages, **kwargs):
        evidence = "\n".join(str(entry.content) for entry in messages)
        if profile == "dialogue.fast" and "刚在模拟活动中经过青桐街" in evidence:
            yield ModelChunk(delta=naturally_shared), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=naturally_shared)
            return
        async for chunk in original_stream(config, profile=profile, messages=messages, **kwargs):
            yield chunk

    monkeypatch.setattr(router, "stream", generate)
    related = await _completed_prompt(await _qq(logged_in, 687002, "聊聊青桐街吧，你有想到什么吗？"))
    assert "刚在模拟活动中经过青桐街" in str(related["messages"]), related["messages"]
    assert any(naturally_shared in str(record["body"]) for record in transport.sent[before:])
    async with session_scope() as session:
        item = await session.get(ProactiveOpportunity, candidate_id)
        assert item is not None and item.state == "continued" and item.sent_at is None
    after = len(transport.sent)
    await worker.handle_proactive_dispatch({})
    assert len(transport.sent) == after
