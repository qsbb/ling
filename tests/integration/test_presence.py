"""Presence 域：生活状态与当前场景。

三条硬规则，每条都有对应测试：
1. **状态只影响表达，不影响权利** —— 累不累都要回答用户
2. **过期状态不能当现状** —— 否则"我还在读书"会说三天
3. **结算幂等** —— 重放/双 worker 只按真正流逝的时间扣一次
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.presence.service import (
    DEFAULT_SCENE_TTL,
    SCENE_STATES,
    PresenceService,
)
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


def _svc(session):  # noqa: ANN001, ANN202
    return PresenceService(session, actor_id=ACTOR)


# ------------------------------------------------------------------ 精力与负担


async def test_defaults_are_available(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        view = await _svc(session).view()
    assert view.availability == "available"
    assert view.describe() == "", "状态正常时不该往提示词里塞废话"


async def test_energy_decays_with_elapsed_time(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        svc = _svc(session)
        await svc.settle(now=now)
        before = (await svc.view(now=now)).energy
        after = (await svc.view(now=now + timedelta(hours=20))).energy
    assert after < before, "20 小时后精力没有下降"


async def test_settle_is_idempotent(logged_in: TestEnv) -> None:
    """同一时刻重复结算不能重复扣减。

    这是最容易写错的地方：如果每次读都扣一次，
    读三次提示词就会把精力扣光。
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        svc = _svc(session)
        await svc.settle(now=now)
    async with session_scope() as session:
        svc = _svc(session)
        first = (await svc.view(now=now + timedelta(hours=5))).energy
    async with session_scope() as session:
        svc = _svc(session)
        second = (await svc.view(now=now + timedelta(hours=5))).energy
    assert first == second, f"重复读取改变了精力: {first} → {second}"


async def test_replay_does_not_double_charge(logged_in: TestEnv) -> None:
    """重放同一时间点不重复扣减（双 worker 场景）。"""
    now = datetime.now(UTC)
    target = now + timedelta(hours=10)
    values = []
    for _ in range(3):
        async with session_scope() as session:
            values.append((await _svc(session).view(now=target)).energy)
    assert len(set(values)) == 1, f"重放导致精力漂移: {values}"


async def test_low_energy_shortens_but_does_not_refuse(logged_in: TestEnv) -> None:
    """低精力只改变表达，**不能变成拒绝**。

    `describe()` 只输出"说话更短"，不输出任何"不能回答"的意思。
    """
    from sqlalchemy import update

    from ling.infra.models import PresenceState

    async with session_scope() as session:
        await _svc(session).settle()
    async with session_scope() as session:
        await session.execute(
            update(PresenceState).where(PresenceState.actor_id == ACTOR).values(energy=10)
        )
    async with session_scope() as session:
        view = await _svc(session).view()
    assert view.availability == "resting"
    text = view.describe()
    assert "更短" in text
    assert "不能" not in text and "拒绝" not in text


async def test_social_load_rises_per_turn(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        svc = _svc(session)
        await svc.settle(now=now)
        before = (await svc.view(now=now)).social_load
    async with session_scope() as session:
        await _svc(session).on_turn(now=now)
    async with session_scope() as session:
        after = (await _svc(session).view(now=now)).social_load
    assert after > before


async def test_high_social_load_marks_low(logged_in: TestEnv) -> None:
    from sqlalchemy import update

    from ling.infra.models import PresenceState

    async with session_scope() as session:
        await _svc(session).settle()
    async with session_scope() as session:
        await session.execute(
            update(PresenceState)
            .where(PresenceState.actor_id == ACTOR)
            .values(energy=90, social_load=90)
        )
    async with session_scope() as session:
        view = await _svc(session).view()
    assert view.availability == "low"


# ------------------------------------------------------------------ 当前场景


def test_scene_states_include_unknown() -> None:
    """``unknown`` 是合法答案——没有证据就不硬编。"""
    assert "unknown" in SCENE_STATES


async def test_scene_unknown_by_default(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        scene = await _svc(session).scene()
    assert scene.state == "unknown"
    assert scene.describe() == "", "unknown 不该产出提示词内容"


async def test_scene_commit_and_read(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        await _svc(session).commit_scene(state="focus", activity_id="act_1")
    async with session_scope() as session:
        scene = await _svc(session).scene()
    assert scene.state == "focus"
    assert scene.activity_id == "act_1"
    assert "手头有事" in scene.describe()


async def test_scene_expires_to_unknown(logged_in: TestEnv) -> None:
    """过期后不能继续自述——这是"我还在读书说三天"的根因。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        await _svc(session).commit_scene(state="focus", now=now)
    async with session_scope() as session:
        scene = await _svc(session).scene(now=now + DEFAULT_SCENE_TTL + timedelta(seconds=1))
    assert scene.state == "unknown"
    assert scene.expired is True
    assert scene.describe() == ""


async def test_scene_within_ttl_is_visible(logged_in: TestEnv) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        await _svc(session).commit_scene(state="rest", now=now)
    async with session_scope() as session:
        scene = await _svc(session).scene(now=now + timedelta(minutes=5))
    assert scene.state == "rest"
    assert "歇着" in scene.describe()


async def test_simulated_scene_is_labelled(logged_in: TestEnv) -> None:
    """模拟生活必须明示，不能冒充真实经历。"""
    async with session_scope() as session:
        await _svc(session).commit_scene(state="focus", origin="simulated")
    async with session_scope() as session:
        scene = await _svc(session).scene()
    assert "模拟" in scene.describe()


async def test_tool_receipt_scene_not_labelled_simulated(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        await _svc(session).commit_scene(state="errand", origin="tool_receipt")
    async with session_scope() as session:
        scene = await _svc(session).scene()
    assert "模拟" not in scene.describe()


async def test_clear_scene_resets_to_unknown(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        await _svc(session).commit_scene(state="focus")
    async with session_scope() as session:
        await _svc(session).clear_scene()
    async with session_scope() as session:
        scene = await _svc(session).scene()
    assert scene.state == "unknown"


async def test_unknown_scene_state_rejected(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        with pytest.raises(ValueError):
            await _svc(session).commit_scene(state="做梦")


async def _force_energy(energy: int) -> None:
    from sqlalchemy import update

    from ling.infra.models import PresenceState

    async with session_scope() as session:
        await _svc(session).settle()
    async with session_scope() as session:
        await session.execute(
            update(PresenceState).where(PresenceState.actor_id == ACTOR).values(energy=energy)
        )


async def test_resting_energy_recovers(logged_in: TestEnv) -> None:
    """累到该歇了的时候，精力必须**回升**。

    只减不增的话，跑够小时数就永远 resting，于是永久不接话——外面看到的是
    "她突然不理我了"，日志里只有一条正常的 `skip_code=resting`。
    """
    await _force_energy(10)
    now = datetime.now(UTC)
    async with session_scope() as session:
        svc = _svc(session)
        low = await svc.view(now=now)
        later = await svc.view(now=now + timedelta(hours=5))
    assert low.availability == "resting"
    assert later.energy > low.energy, f"歇了 5 小时精力没回升：{low.energy} → {later.energy}"


async def test_energy_never_sticks_at_the_floor(logged_in: TestEnv) -> None:
    """跑很久之后仍然能聊——不允许永久卡在 resting。

    这条是上面那个 bug 的回归位：把恢复速率改成 0，它会红。
    """
    await _force_energy(10)
    now = datetime.now(UTC)
    async with session_scope() as session:
        svc = _svc(session)
        # 每 6 小时看一次，模拟"她过日子"式的时间推进，共 3 天
        views = [await svc.view(now=now + timedelta(hours=6 * i)) for i in range(1, 13)]
    assert any(v.availability != "resting" for v in views), "三天里一次都没恢复到可以说话"
