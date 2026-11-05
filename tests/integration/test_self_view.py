"""SelfView：跨域只读聚合，只输出受众安全的投影。

这是阶段 1 的隐私闸门。三条必须成立的规则：
1. `sealed` 时不注入任何跨窗口自我状态（否则"完全隔离"名不副实）
2. 群聊里不注入私密的精力状态（那会显得在向所有人索取情绪价值）
3. 群聊里不复述私聊来源的场景（私聊内容不进群）

不做这一步的后果不是"记得差一点"，而是隔离与隐私边界失效。
"""

from __future__ import annotations

import pytest

from ling.domains.presence.service import PresenceService
from ling.infra.db import session_scope
from ling.kernel.self_view import SelfViewBuilder
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _build(
    env: TestEnv,
    *,
    sealed: bool = False,
    is_group: bool = False,
) -> tuple[str, list[str], list[str]]:
    async with session_scope() as session:
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=env.subject_id,
            window_id="win_test",
            sealed=sealed,
            is_group=is_group,
        ).build()
    return view.render(), view.sources(), [f"{n}:{w}" for n, w in view.dropped]


async def _force_state(energy: int, load: int = 20) -> None:
    from sqlalchemy import update

    from ling.infra.models import PresenceState

    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).settle()
    async with session_scope() as session:
        await session.execute(
            update(PresenceState)
            .where(PresenceState.actor_id == ACTOR)
            .values(energy=energy, social_load=load)
        )


# ------------------------------------------------------------------ sealed


async def test_sealed_drops_all_self_state(logged_in: TestEnv) -> None:
    """sealed 时精力与场景都不注入。"""
    await _force_state(10)
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(state="focus")

    rendered, sources, dropped = await _build(logged_in, sealed=True)
    assert rendered == "", f"sealed 下仍有自我投影: {rendered}"
    assert sources == []
    assert any("sealed" in d for d in dropped)


async def test_normal_mode_includes_state(logged_in: TestEnv) -> None:
    """非 sealed 时正常注入（对照，证明上一条不是"永远为空"）。"""
    await _force_state(10)
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(state="focus")

    rendered, sources, _ = await _build(logged_in)
    assert rendered, "正常模式下没有注入任何自我状态"
    assert "presence" in sources


# ------------------------------------------------------------------ 群聊


async def test_group_marks_presence_actor_private(logged_in: TestEnv) -> None:
    """群聊里精力状态的可见范围是 actor_private，不是给所有人看。"""
    await _force_state(10)
    async with session_scope() as session:
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id="win_group",
            is_group=True,
        ).build()
    presence = [f for f in view.fragments if f.source == "presence"]
    if presence:
        assert presence[0].visibility.value == "actor_private"


async def test_group_does_not_repeat_private_scene(logged_in: TestEnv) -> None:
    """私聊里说过的事，不在群里复述。"""
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(
            state="focus", origin="user_stated"
        )
    rendered, sources, dropped = await _build(logged_in, is_group=True)
    assert "scene" not in sources, f"群聊注入了私聊来源的场景: {rendered}"
    assert any("群聊不复述私聊来源" in d for d in dropped)


async def test_private_chat_allows_user_stated_scene(logged_in: TestEnv) -> None:
    """私聊里可以提（这是同一受众）。"""
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(
            state="focus", origin="user_stated"
        )
    _, sources, _ = await _build(logged_in)
    assert "scene" in sources


# ------------------------------------------------------------------ 过期与预算


async def test_expired_scene_not_injected(logged_in: TestEnv) -> None:
    from datetime import UTC, datetime, timedelta

    from ling.domains.presence.service import DEFAULT_SCENE_TTL

    past = datetime.now(UTC) - DEFAULT_SCENE_TTL - timedelta(minutes=1)
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(
            state="focus", now=past
        )
    _, sources, dropped = await _build(logged_in)
    assert "scene" not in sources
    assert any("过期" in d for d in dropped)


async def test_budget_limits_rendered_length(logged_in: TestEnv) -> None:
    """预算约束生效：不能把状态塞满整个提示词。"""
    await _force_state(10)
    async with session_scope() as session:
        await PresenceService(session, actor_id=ACTOR).commit_scene(state="focus")
        view = await SelfViewBuilder(
            session,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id="win_budget",
        ).build()
    tiny = view.render(budget_chars=5)
    full = view.render(budget_chars=200)
    assert len(tiny) <= len(full)


async def test_empty_state_renders_empty(logged_in: TestEnv) -> None:
    """状态正常且无场景时，不往提示词塞任何内容。"""
    rendered, sources, _ = await _build(logged_in)
    assert rendered == ""
    assert sources == []


# ------------------------------------------------------------------ 日程注入


async def test_agenda_injected_with_hedged_wording(logged_in: TestEnv) -> None:
    """tentative 计划必须以"可能"出现，不能用确定语气。"""
    from datetime import UTC, datetime, timedelta

    from ling.domains.agenda.service import AgendaService

    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading",
            title="读书",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            commitment_level="tentative",
        )
    rendered, sources, _ = await _build(logged_in)
    assert "agenda" in sources
    assert "可能" in rendered
    assert "约好" not in rendered


async def test_confirmed_plan_uses_firm_wording(logged_in: TestEnv) -> None:
    from datetime import UTC, datetime, timedelta

    from ling.domains.agenda.service import AgendaService

    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading",
            title="读书",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            commitment_level="confirmed",
        )
    rendered, _, _ = await _build(logged_in)
    assert "约好" in rendered


async def test_agenda_not_injected_in_group(logged_in: TestEnv) -> None:
    """群聊里不报自己的行程——对别人没意义，还像自言自语。"""
    from datetime import UTC, datetime, timedelta

    from ling.domains.agenda.service import AgendaService

    now = datetime.now(UTC)
    async with session_scope() as session:
        await AgendaService(session, actor_id=ACTOR).propose_plan(
            kind="reading", title="读书", start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
        )
    _, sources, dropped = await _build(logged_in, is_group=True)
    assert "agenda" not in sources
    assert any("群聊不注入个人行程" in d for d in dropped)


async def test_no_plan_no_agenda_fragment(logged_in: TestEnv) -> None:
    """没有计划时什么都不加——不硬编"我今天很充实"。"""
    _, sources, _ = await _build(logged_in)
    assert "agenda" not in sources
