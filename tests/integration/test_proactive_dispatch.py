"""主动投递：真的发出去，但**默认关闭**。

投递是这条链路里唯一会真的打扰人的一步。前面几步（收集、门控、生成草稿）
都能在影子模式下验证，投递不行——一旦发生就是"她突然找你"。

所以默认 `proactive_delivery_enabled=False`，只生成不发送。

四条硬规则：

1. **默认关**，用户确认过草稿再自己打开
2. **只发私聊**：主动找你时往群里发一句"在吗"是不可接受的
3. **只有真正的送达才标 sent**（`DeliveryOutcome.delivered`）；
   不确定就标 failed 等对账，**不盲目重发**——重复发比没发更糟
4. **门控没过就留着**，不标 rejected：那只是"现在还不是时候"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ling.domains.proactive.dispatch import dispatch_pending
from ling.infra.db import session_scope
from ling.infra.models import ProactiveOpportunity, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SUBJ = "subj_dispatch_test"
# UTC 04:00 = 上海 12:00：稳稳在勿扰时段（23:00-08:00）之外，
# 测试结果不该依赖"现在几点"。
SAFE_NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)
TZ = "Asia/Shanghai"


class _Caps:
    enabled = True
    supports_group = True
    supports_private = True
    supports_proactive = True
    supports_media = False
    max_text_length = 4000


class _Connector:
    """只记录发送，不发网络请求。"""

    connector_id = "fake"
    instance_id = "fake:test"

    def __init__(self, *, state: str = "delivered") -> None:
        from ling.contracts.v1 import DeliveryState

        self.sent: list[object] = []
        self._state = DeliveryState(state)

    def capabilities(self):  # noqa: ANN202
        return _Caps()

    async def send(self, message):  # noqa: ANN001, ANN202
        from ling.contracts.v1 import DeliveryReceipt

        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=self._state,
            platform_message_id=f"pm_{len(self.sent)}",
            observed_at=datetime.now(UTC),
            detail=None,
        )

    async def verify_inbound(self, headers, body):  # noqa: ANN001, ANN202
        return True


class _Config:
    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    async def effective(self):  # noqa: ANN202
        return SimpleNamespace(
            dialogue=SimpleNamespace(
                proactive_delivery_enabled=self._enabled,
                proactive_daily_limit=2,
                quiet_start=(23, 0),
                quiet_end=(8, 0),
            )
        )


async def _clean() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(ProactiveOpportunity).where(ProactiveOpportunity.actor_id == ACTOR)
        )
        await session.execute(delete(Window).where(Window.subject_id == SUBJ))


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _window(*, visibility: str = "subject_private", ref: str = "onebot-x:private:9001") -> str:
    async with session_scope() as session:
        wid = f"win_disp_{visibility[:6]}_{abs(hash(ref)) % 9999}"
        session.add(
            Window(
                id=wid,
                actor_id=ACTOR,
                persona_id="persona_ling",
                subject_id=SUBJ,
                title=ref,
                channel="onebot",
                visibility=visibility,
                archived=False,
                last_message_at=datetime.now(UTC),
            )
        )
    return wid


async def _candidate(*, draft: str | None = "在吗，问个事。") -> str:
    oid = f"opp_disp_{abs(hash(draft or 'none')) % 9999}"
    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id=oid,
                actor_id=ACTOR,
                subject_id=SUBJ,
                kind="reach_out",
                reason="想问对方一件事",
                state="candidate",
                expires_at=SAFE_NOW + timedelta(hours=2),
                created_at=SAFE_NOW,
                draft_text=draft,
            )
        )
    return oid


async def _state(oid: str) -> tuple[str, datetime | None]:
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, oid)
        assert row is not None
        return row.state, row.sent_at


# ---------------------------------------------------------------- 默认关


async def test_disabled_by_default_sends_nothing(logged_in: TestEnv) -> None:  # noqa: ARG001
    """默认不发送——这是最重要的那条。"""
    await _window()
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=False),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["skipped"] == "disabled"
    assert conn.sent == [], "默认关却发了"
    assert (await _state(oid))[0] == "candidate", "没发却改了状态"


# ---------------------------------------------------------------- 正常发送


async def test_enabled_sends_and_marks_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _window()
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["sent"] == 1, stats
    assert len(conn.sent) == 1
    assert conn.sent[0].text == "在吗，问个事。"
    state, sent_at = await _state(oid)
    assert state == "sent"
    assert sent_at is not None, "sent 必须记送达时间（限额按它计日）"


async def test_candidate_without_draft_is_not_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没草稿不发——那是"还没想好说什么"，不是"有话要说"。"""
    await _window()
    await _candidate(draft=None)
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["considered"] == 0
    assert conn.sent == []


# ---------------------------------------------------------------- 只发私聊


async def test_group_window_is_never_used(logged_in: TestEnv) -> None:  # noqa: ARG001
    """只有群窗口时**不发**并标 rejected。

    主动找你时往群里发一句"在吗"是不可接受的。
    """
    await _window(visibility="group_scope", ref="onebot-x:group:8888")
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["rejected"] == 1
    assert conn.sent == [], "往群里发了"
    state, _ = await _state(oid)
    assert state == "rejected"
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, oid)
        assert "私聊" in (row.rejected_reason or "")


async def test_no_window_marks_rejected(logged_in: TestEnv) -> None:  # noqa: ARG001
    oid = await _candidate(draft="在吗")
    conn = _Connector()
    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["rejected"] == 1
    assert (await _state(oid))[0] == "rejected"


# ---------------------------------------------------------------- 门控


async def test_gate_blocked_leaves_candidate_alone(logged_in: TestEnv) -> None:  # noqa: ARG001
    """门控没过就**留着**，不标 rejected——那只是"现在还不是时候"。"""
    await _window()
    oid = await _candidate()
    conn = _Connector()

    # 时区未知 → gate 直接拦下（详见 data 里"勿扰时段按 UTC 算"那个修复）
    async with session_scope() as session:
        await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=None,
            now=SAFE_NOW,
        )
    assert conn.sent == [], "门控没过却发了"
    assert (await _state(oid))[0] == "candidate", "门控没过却被标成 rejected"


# ---------------------------------------------------------------- 失败


async def test_failed_delivery_is_not_marked_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """投递失败不能标 sent——那会让限额算错，而且用户什么都没收到。"""
    await _window()
    oid = await _candidate()
    conn = _Connector(state="failed")

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["failed"] == 1
    state, sent_at = await _state(oid)
    assert state == "failed"
    assert sent_at is None, "没送到却记了送达时间"


async def test_uncertain_is_not_retried_as_success(logged_in: TestEnv) -> None:  # noqa: ARG001
    """回执不确定时标 failed 等对账，不当作成功，也不盲目重发。"""
    await _window()
    oid = await _candidate()
    conn = _Connector(state="uncertain")

    async with session_scope() as session:
        await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    state, sent_at = await _state(oid)
    assert state == "failed", "不确定被当成成功了"
    assert sent_at is None
    assert len(conn.sent) == 1, "不该重发"
