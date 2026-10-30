"""Synthetic end-to-end check: a due commitment becomes a Web actor message.

This deliberately uses the PostgreSQL W0 fixture, which gives each test a
throwaway schema. The only created records carry a ``synthetic_proactive_e2e``
text/window marker and disappear when that schema is dropped. External
connectors are forced off; the draft model is a deterministic in-process fake.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from ling.contracts.v1 import Origin
from ling.domains.goals.service import GoalsService
from ling.infra.db import session_scope
from ling.infra.models import CommitmentRow, Message, ProactiveOpportunity, Window

# Fixtures live under tests/integration.  Import them into this module so
# pytest can discover them for this root-level test file, but do not use
# ``pytest_plugins``: a test module is not a conftest, and registering the same
# conftest as a plugin breaks collection when the integration suite (which
# auto-loads it normally) is collected in the same pytest process.
from tests.integration.conftest import TEST_DB_URL, TestEnv
from tests.integration.conftest import logged_in as _integration_logged_in  # noqa: F401
from tests.integration.conftest import web_env as _integration_web_env  # noqa: F401


@pytest.fixture(name="web_env")
def _web_env_fixture(request: pytest.FixtureRequest):  # noqa: ANN202
    # Resolve the conftest fixture during setup (before pytest-asyncio starts
    # the test's event loop); calling getfixturevalue inside the async test
    # itself raises "This event loop is already running".
    return request.getfixturevalue("_integration_web_env")


@pytest.fixture(name="logged_in")
def _logged_in_fixture(request: pytest.FixtureRequest):  # noqa: ANN202
    return request.getfixturevalue("_integration_logged_in")


pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SYNTHETIC_SUBJECT_TAG = "synthetic_proactive_e2e"
SYNTHETIC_WINDOW_ID = "synthetic_proactive_e2e_window"
FROZEN_NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai
DRAFT_TEXT = "合成测试：到期的提醒我已经记下啦。"
# Exact evidence linking the persisted message back to the opportunity.
_LINKED_MESSAGE = re.compile(
    r"window=(?P<window>\S+) message=(?P<message>\S+) opportunity=(?P<opportunity>\S+)"
)


def _assert_isolated_test_database() -> None:
    """Refuse an obvious production/non-test DB before schema DDL runs.

    The local default is ``ling_test``. CI may use any host when it explicitly
    supplies LING_TEST_DATABASE_URL, but the database name must still clearly
    identify a test database. This keeps the fixture useful with CI service
    hostnames without accepting a production-named database.
    """
    url = make_url(TEST_DB_URL)
    database = (url.database or "").lower()
    is_test_database = "test" in database
    is_local = (url.host or "").lower() in {"localhost", "127.0.0.1", "::1"}
    explicitly_selected = bool(os.environ.get("LING_TEST_DATABASE_URL"))
    assert url.drivername.startswith("postgresql"), "E2E 只能使用隔离 PostgreSQL 测试库"
    assert is_test_database, "拒绝对数据库名不含 test 的库创建测试 schema"
    assert is_local or explicitly_selected, (
        "非本机测试库必须通过 LING_TEST_DATABASE_URL 显式指定"
    )


@pytest.fixture(autouse=True)
def _protect_database_before_fixture_setup() -> None:
    # Runs before web_env setup starts because it is autouse and has no fixture
    # dependencies. The DB fixture then creates/drops only its random schema.
    _assert_isolated_test_database()


class _FixedRouter:
    """Fake model provider with a stable result and observable call count."""

    def __init__(self) -> None:
        self.calls: list[list[object]] = []

    async def stream(self, _config, *, profile, messages, **_kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        assert profile == "dialogue.fast"
        self.calls.append(list(messages))
        yield ModelChunk(delta=DRAFT_TEXT), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=DRAFT_TEXT)


class _TestConfig:
    async def effective(self):  # noqa: ANN201
        return SimpleNamespace(
            dialogue=SimpleNamespace(
                proactive_delivery_enabled=True,
                proactive_daily_limit=2,
                quiet_start=(23, 0),
                quiet_end=(8, 0),
            )
        )


async def _synthetic_window(subject_id: str) -> None:
    async with session_scope() as session:
        session.add(
            Window(
                id=SYNTHETIC_WINDOW_ID,
                actor_id=ACTOR,
                persona_id="persona_ling",
                subject_id=subject_id,
                title=f"{SYNTHETIC_SUBJECT_TAG}: private test window",
                channel="web",
                visibility="subject_private",
                archived=False,
                last_message_at=FROZEN_NOW,
            )
        )


async def _messages_for_synthetic_window() -> list[Message]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.window_id == SYNTHETIC_WINDOW_ID)
                    .order_by(Message.sequence)
                )
            ).scalars()
        )


async def test_due_commitment_collects_drafts_and_dispatches_to_web(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exercise worker collect -> draft -> dispatch on a schema-isolated fixture."""
    env = logged_in
    import ling.domains.goals.service as goals_module
    import ling.domains.proactive.dispatch as dispatch_module
    import ling.domains.proactive.draft as draft_module
    import ling.domains.proactive.service as proactive_module
    import ling.settings.service as settings_module
    from ling import worker

    # Pin commitment and opportunity time checks so this test never depends on
    # wall-clock time. Dispatch still uses its worker's real business path.
    monkeypatch.setattr(goals_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(proactive_module, "utcnow", lambda: FROZEN_NOW)
    monkeypatch.setattr(proactive_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(draft_module, "utcnow", lambda: FROZEN_NOW)
    monkeypatch.setattr(dispatch_module, "utcnow", lambda: FROZEN_NOW)
    test_settings = replace(env.settings, user_timezone="Asia/Shanghai")
    monkeypatch.setattr(worker, "get_settings", lambda: test_settings)
    monkeypatch.setattr(worker, "proactive_connector", lambda: None, raising=False)
    monkeypatch.setattr("ling.capabilities.channels.proactive_connector", lambda: None)

    # The handler imports ConfigService locally. Replace that seam with a test
    # reader; no published/global development configuration is touched.
    monkeypatch.setattr(settings_module, "ConfigService", lambda *_a, **_k: _TestConfig())
    router = _FixedRouter()
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)

    subject_id = env.subject_id
    await _synthetic_window(subject_id)
    async with session_scope() as session:
        created_commitment = await GoalsService(session, actor_id=ACTOR).create_commitment(
            subject_id=subject_id,
            window_id=SYNTHETIC_WINDOW_ID,
            text=f"{SYNTHETIC_SUBJECT_TAG}: synthetic due reminder",
            due_at=FROZEN_NOW - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )
        commitment_id = created_commitment.commitment_id
        commitment_row = await session.get(CommitmentRow, commitment_id)
        assert commitment_row is not None
        # Domain-generated IDs are retained for relationship integrity; the
        # synthetic marker remains in the private test record's text and window.

    # Trace the exact ID returned by the goals domain through collection and
    # delivery; never seed an opportunity directly in this flow.

    with caplog.at_level(logging.INFO):
        await worker.handle_proactive_collect({"shadow": True})
        async with session_scope() as session:
            opportunities = list(
                (
                    await session.execute(
                        select(ProactiveOpportunity).where(
                            ProactiveOpportunity.source_event_id
                            == f"commitment:{commitment_id}"
                        )
                    )
                ).scalars()
            )
        assert len(opportunities) == 1, "合成到期承诺应只产生一个机会"
        opportunity_id = opportunities[0].id
        assert opportunities[0].state == "candidate"
        assert opportunities[0].draft_text is None

        await worker.handle_proactive_draft({})
        async with session_scope() as session:
            drafted = await session.get(ProactiveOpportunity, opportunity_id)
        assert drafted is not None and drafted.draft_text == DRAFT_TEXT
        assert len(router.calls) == 1

        await worker.handle_proactive_dispatch({})

    skipped = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "proactive_dispatch_skipped"
        and getattr(record, "attributes", {}).get("reason_code")
        == "no_proactive_channel"
    ]
    assert skipped, "没有外部渠道时应明确记录改投 Web，而不是静默跳过"
    events = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "proactive_dispatched"
    ]
    assert events, [record.getMessage() for record in caplog.records]
    attrs = getattr(events[-1], "attributes", {})
    assert attrs.get("sent") == 1, attrs
    assert attrs.get("failed") == 0, attrs
    assert attrs.get("rejected") == 0, attrs
    assert attrs.get("considered") == 1, attrs

    # The dispatch log is the only place that ties the opportunity id to the
    # persisted Web message id.  Both directions are asserted, not just the
    # presence of the opportunity id.
    links = [
        match
        for record in caplog.records
        if (match := _LINKED_MESSAGE.search(record.getMessage()))
    ]
    assert len(links) == 1, [record.getMessage() for record in caplog.records]
    link = links[0]
    assert link["window"] == SYNTHETIC_WINDOW_ID, link.groupdict()
    assert link["opportunity"] == opportunity_id, link.groupdict()

    async with session_scope() as session:
        sent = await session.get(ProactiveOpportunity, opportunity_id)
        commitment = await session.get(CommitmentRow, commitment_id)
    assert sent is not None and sent.state == "sent"
    assert sent.created_at == FROZEN_NOW, "机会创建时钟未被冻结"
    assert sent.sent_at == FROZEN_NOW, "送达时钟未被冻结"
    assert commitment is not None and commitment.text.startswith(SYNTHETIC_SUBJECT_TAG)
    actor_messages = [
        message
        for message in await _messages_for_synthetic_window()
        if message.role == "actor"
    ]
    assert len(actor_messages) == 1
    actor_message = actor_messages[0]
    assert actor_message.id == link["message"], "日志里的消息 ID 与落库消息不一致"
    assert actor_message.text == DRAFT_TEXT
    assert actor_message.origin == Origin.simulated.value

    # Repeating every stage is idempotent: the due commitment is already
    # represented, the candidate is already drafted/sent, and no second actor
    # message may be written.
    await worker.handle_proactive_collect({"shadow": True})
    await worker.handle_proactive_draft({})
    await worker.handle_proactive_dispatch({})
    assert len(router.calls) == 1
    actor_messages_after = [
        message
        for message in await _messages_for_synthetic_window()
        if message.role == "actor"
    ]
    assert len(actor_messages_after) == 1, "重跑重复写入 actor 消息"
    assert actor_messages_after[0].id == actor_message.id
    async with session_scope() as session:
        sent_after = await session.get(ProactiveOpportunity, opportunity_id)
    assert sent_after is not None and sent_after.state == "sent"
    assert sent_after.sent_at == FROZEN_NOW, "重跑改写了送达时间"


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)
