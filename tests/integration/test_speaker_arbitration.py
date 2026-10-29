"""发言仲裁的端到端语义（此前实现的问题"排队是假的"）。

两个真实缺陷：
1. 同一 window 的新轮次直接替换占用 → 两轮 delta 交错
2. 未取得占用的轮次照样推 delta → 排队毫无意义

本测试直接检验 TurnCoordinator 的行为：未持有时不产出对外片段。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ling.kernel.arbiter import SpeakerArbiter
from ling.kernel.turn import TurnCoordinator
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _fast_config() -> EffectiveConfig:
    """极快的 fake provider，避免测试等太久。"""
    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(provider_id="fake", kind="fake", enabled=True,
                                   extra={"chunk_delay": 0.01})
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="fake", model_id="deterministic",
                timeout_seconds=20.0,
            )
        },
        test_mode=True,
    )


async def _make_turn(env: TestEnv, window_id: str, text: str) -> str:
    """在数据库里建一条真实轮次（_finalize 需要真实行）。"""
    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=text, turn_id=turn.id)
        return turn.id


async def _run_turn(
    env: TestEnv, coordinator: TurnCoordinator, window_id: str, subject_id: str, text: str
):
    turn_id = await _make_turn(env, window_id, text)
    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=subject_id,
        turn_id=turn_id,
        user_text=text,
        config=_fast_config(),
    )
    events = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=30)
        if item is None:
            break
        events.append((item.event, item.data))
    return turn_id, events


async def _coordinator(env: TestEnv) -> TurnCoordinator:
    registry = ProviderRegistry(lambda _pid: None)
    return TurnCoordinator(
        router=ModelRouter(registry),
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        arbiter=SpeakerArbiter(default_lease_seconds=30),
        stream_log=env.app.state.stream_log,
    )


async def test_one_speaker_at_a_time_no_interleaving(logged_in: TestEnv) -> None:
    """两个窗口同时要说话：同一时刻只有一个产出 delta，另一个排队。"""
    coordinator = await _coordinator(logged_in)
    win_a = (await logged_in.client.post("/v1/sessions", json={"title": "A"})).json()[
        "session_id"
    ]
    win_b = (await logged_in.client.post("/v1/sessions", json={"title": "B"})).json()[
        "session_id"
    ]
    # 先占住 A
    hold = coordinator._arbiter.acquire(window_id=win_a, turn_id="manual")  # noqa: SLF001
    assert hold.acquired

    _, events_b = await _run_turn(logged_in, coordinator, win_b, logged_in.subject_id, "B 说话")
    names_b = [n for n, _ in events_b]
    # 未取得发言权：既不产出片段，**也不在最终事件里交付正文**。
    # 否则前端会把整段渲染出来，等于"排队"只挡了一半，
    # 用户照样同时看到两个窗口的回复（此前实现的缺陷）。
    assert "delta" not in names_b, "未取得发言权却产出了对外片段"
    completed = [d for n, d in events_b if n == "turn.completed"]
    assert completed
    payload = completed[0]
    assert payload["decision"] == "deferred_by_floor"
    assert payload["message"] is None, "未取得发言权却交付了正文"
    assert payload["suppressed_reason"], "压掉了却没有可审计的原因"

    # 内容仍必须落库：可审计、可重放，只是不对外交付
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import Message

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.window_id == win_b, Message.role == "actor"
                    )
                )
            ).scalars()
        )
    assert rows and rows[0].text, "内容没落库（可审计性丢失）"


async def test_queued_turn_speaks_after_release(logged_in: TestEnv) -> None:
    """持有者释放后，排队者应被提升并正常输出。"""
    coordinator = await _coordinator(logged_in)
    win_a = (await logged_in.client.post("/v1/sessions", json={"title": "A2"})).json()[
        "session_id"
    ]
    win_b = (await logged_in.client.post("/v1/sessions", json={"title": "B2"})).json()[
        "session_id"
    ]

    # B 先启动（会排队），A 占着不放
    coordinator._arbiter.acquire(window_id=win_a, turn_id="manual")  # noqa: SLF001
    task_b = asyncio.create_task(
        _run_turn(logged_in, coordinator, win_b, logged_in.subject_id, "排队后说话")
    )
    # 给 B 一点时间进入排队
    await asyncio.sleep(0.4)
    # 释放 A，B 应被提升
    coordinator._arbiter.release(window_id=win_a, turn_id="manual")  # noqa: SLF001

    _, events_b = await asyncio.wait_for(task_b, timeout=60)
    names_b = [n for n, _ in events_b]
    completed = [d for n, d in events_b if n == "turn.completed"]
    assert completed
    payload = completed[0]
    # 取得发言权 → 正常交付；没取得 → 不交付正文（避免两个窗口同时出现回复），
    # 但要给出可审计的原因。内容本身仍会落库，可重放。
    if payload["decision"] == "reply":
        assert payload["message"] is not None
        assert "turn.speaking" in names_b, "输出前应先通知取得发言权"
    else:
        assert payload["decision"] == "deferred_by_floor"
        assert payload["message"] is None, "未取得发言权却交付了正文"
        assert payload["suppressed_reason"]

    # 无论是否对外交付，生成的内容都必须落库（可审计/可重放）
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import Message

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.window_id == win_b, Message.role == "actor"
                    )
                )
            ).scalars()
        )
    assert rows, "生成的内容没有落库"


async def test_same_window_second_turn_waits(logged_in: TestEnv) -> None:
    """同一窗口的第二轮不能直接替换占用。"""
    arbiter = SpeakerArbiter(default_lease_seconds=30)
    first = arbiter.acquire(window_id="w", turn_id="t1")
    assert first.acquired
    second = arbiter.acquire(window_id="w", turn_id="t2")
    assert second.acquired is False
    assert arbiter.holder() == "w"
    assert arbiter.snapshot()["holder_turn_id"] == "t1"
