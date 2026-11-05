"""活动：状态机完整性、完成证据、检查点。

设计文档 要求"计划不等于完成"——标记 completed 必须有真实证据，
不能因为模型说"我做完了"就当真。状态文档里"活动证据与检查点"是缺口。
"""

from __future__ import annotations

import pytest

from ling.domains.activity.service import (
    TRANSITIONS,
    VALID_STATES,
    ActivityService,
    InvalidTransition,
)
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 结构性


def test_every_state_has_transition_entry() -> None:
    """每个声明状态都必须在 TRANSITIONS 里有条目。

    漏一个会让该状态永久卡死（`TRANSITIONS.get(state, set())` 返回空集，
    任何转换都被拒），而且报错信息是"不允许 X → Y"，看不出是漏配。
    """
    missing = VALID_STATES - set(TRANSITIONS)
    assert not missing, f"这些状态没有转移表: {sorted(missing)}"


def test_every_state_reachable_from_proposed() -> None:
    """每个状态都必须能从 proposed 走到，否则就是死状态。"""
    reachable = {"proposed"}
    frontier = ["proposed"]
    while frontier:
        current = frontier.pop()
        for nxt in TRANSITIONS.get(current, set()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    unreachable = VALID_STATES - reachable
    assert not unreachable, f"这些状态从 proposed 不可达: {sorted(unreachable)}"


def test_transitions_only_target_valid_states() -> None:
    """转移目标必须是已声明状态（防止打错字造成隐形状​态）。"""
    for src, targets in TRANSITIONS.items():
        assert src in VALID_STATES, f"转移表里有未声明状态: {src}"
        bad = targets - VALID_STATES
        assert not bad, f"{src} 的转移目标里有未声明状态: {sorted(bad)}"


# ------------------------------------------------------------------ 行为


async def _propose(env: TestEnv, *, kind: str = "write") -> object:
    async with session_scope() as session:
        return await ActivityService(session).propose(kind=kind)


async def test_propose_starts_as_proposed(logged_in: TestEnv) -> None:
    activity = await _propose(logged_in)
    assert activity.state == "proposed"


async def test_cannot_complete_without_evidence(logged_in: TestEnv) -> None:
    """**计划不等于完成**：没有完成证据不能标 completed。

    这是 DESIGN 的硬要求——不能因为模型自称做完就当真。
    """
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "running")
    async with session_scope() as session:
        with pytest.raises(InvalidTransition, match="证据"):
            await ActivityService(session).transition(activity.activity_id, "completed")


async def test_complete_with_evidence(logged_in: TestEnv) -> None:
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "running")
        view = await service.transition(
            activity.activity_id,
            "completed",
            completion_evidence="笔记已写入 notes.md",
        )
    assert view.state == "completed"
    assert view.ended_at is not None
    assert view.completion_evidence == "笔记已写入 notes.md"


async def test_invalid_transition_rejected(logged_in: TestEnv) -> None:
    """proposed 不能直接到 completed（必须先 running）。"""
    activity = await _propose(logged_in)
    async with session_scope() as session:
        with pytest.raises(InvalidTransition, match="不允许"):
            await ActivityService(session).transition(
                activity.activity_id, "completed", completion_evidence="假装完成"
            )


async def test_unknown_state_rejected(logged_in: TestEnv) -> None:
    activity = await _propose(logged_in)
    async with session_scope() as session:
        with pytest.raises(InvalidTransition, match="未知状态"):
            await ActivityService(session).transition(activity.activity_id, "finished")


async def test_pause_and_resume(logged_in: TestEnv) -> None:
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "running")
        paused = await service.transition(activity.activity_id, "paused")
        assert paused.state == "paused"
        resumed = await service.transition(activity.activity_id, "running")
        assert resumed.state == "running"


async def test_checkpoint_is_recorded(logged_in: TestEnv) -> None:
    """检查点用于"接着做"——必须能存能读。"""
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "running")
        await service.transition(
            activity.activity_id,
            "paused",
            checkpoint={"progress": "写完第一节", "next": "写第二节"},
        )
    async with session_scope() as session:
        view = await ActivityService(session).current()
    assert view is not None
    assert view.checkpoint, "检查点没记录下来"
    assert "写完第一节" in str(view.checkpoint)


async def test_terminal_states_can_restart_or_stay(logged_in: TestEnv) -> None:
    """失败/取消可以回到 proposed 重来（不能永久锁死）。"""
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "failed")
        again = await service.transition(activity.activity_id, "proposed")
    assert again.state == "proposed"


async def test_completed_is_terminal(logged_in: TestEnv) -> None:
    """完成是终态，不能回退（否则"做完了"会变回"进行中"）。"""
    activity = await _propose(logged_in)
    async with session_scope() as session:
        service = ActivityService(session)
        await service.transition(activity.activity_id, "running")
        await service.transition(
            activity.activity_id, "completed", completion_evidence="已完成"
        )
    async with session_scope() as session:
        with pytest.raises(InvalidTransition):
            await ActivityService(session).transition(activity.activity_id, "running")
