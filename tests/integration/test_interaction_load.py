"""可用性与疲劳：忙/休息时没空回，"回烦了"也会不回。

用户需求原话：「有时候会在忙/休息，没空回消息」「群聊回消息回烦了也会选择不回」。

两条设计边界，本文件钉住：

1. **疲劳按窗口隔离**——群里的烦躁不能影响对私聊对象的态度，那是跨受众泄露
2. **只管时机与长短，不管语气**——用户明确要求行为风格由人设决定，
   所以这里不产生任何"冷淡值/攻击性"信号，只给模型一句状态描述
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.presence.load import (
    COST_FINISHED_REPLY,
    COST_SAW_MESSAGE,
    MAX_LOAD,
    RECOVERY_HALF_LIFE_MINUTES,
    InteractionLoadService,
)
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _clean() -> None:
    from sqlalchemy import delete

    from ling.infra.models import InteractionLoadRow

    async with session_scope() as session:
        await session.execute(
            delete(InteractionLoadRow).where(InteractionLoadRow.actor_id == ACTOR)
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


# ---------------------------------------------------------------- 累积与恢复


async def test_starts_at_zero(logged_in: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        view = await InteractionLoadService(session, actor_id=ACTOR).level(
            scope_key="win_new"
        )
    assert view.level == 0.0
    assert view.band == "low"
    assert view.describe() == ""


async def test_accumulates_cost(logged_in: TestEnv) -> None:  # noqa: ARG001
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        for i in range(5):
            view = await service.add(
                scope_key="win_a", cost=COST_SAW_MESSAGE, now=now + timedelta(seconds=i)
            )
    assert view.level == pytest.approx(5.0, abs=0.2)


async def test_recovers_by_half_life(logged_in: TestEnv) -> None:  # noqa: ARG001
    """半衰期后负担减半——"烦"会自然消退，不需要后台任务。"""
    start = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        await service.add(scope_key="win_b", cost=80.0, now=start)
        later = await service.level(
            scope_key="win_b", now=start + timedelta(minutes=RECOVERY_HALF_LIFE_MINUTES)
        )
    assert 38 <= later.level <= 42, f"半衰期后应约为 40，实际 {later.level}"


async def test_long_absence_fully_recovers(logged_in: TestEnv) -> None:  # noqa: ARG001
    start = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        await service.add(scope_key="win_c", cost=90.0, now=start)
        later = await service.level(
            scope_key="win_c", now=start + timedelta(hours=8)
        )
    assert later.level < 1.0
    assert later.band == "low"


async def test_clock_going_backwards_does_not_zero_it(logged_in: TestEnv) -> None:  # noqa: ARG001
    """时间倒流（时钟回拨）时**不衰减**。

    宁可高估负担，也不要在时间异常时凭空清零——那会让"回烦了"
    被一次时钟跳变抹掉。
    """
    start = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        await service.add(scope_key="win_d", cost=60.0, now=start)
        earlier = await service.level(
            scope_key="win_d", now=start - timedelta(hours=2)
        )
    assert earlier.level == pytest.approx(60.0, abs=0.5)


async def test_clamped_to_max(logged_in: TestEnv) -> None:  # noqa: ARG001
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        for _ in range(30):
            view = await service.add(
                scope_key="win_e", cost=COST_FINISHED_REPLY, now=now
            )
    assert view.level <= MAX_LOAD


# ---------------------------------------------------------------- 隔离


async def test_load_is_isolated_per_window(logged_in: TestEnv) -> None:  # noqa: ARG001
    """群里的烦躁不能影响对私聊对象的态度。

    这是**跨受众泄露**，比"数值不准"严重得多。
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        service = InteractionLoadService(session, actor_id=ACTOR)
        await service.add(scope_key="win_group", cost=90.0, now=now)
        private = await service.level(scope_key="win_private", now=now)
        group = await service.level(scope_key="win_group", now=now)
    assert group.level >= 80
    assert private.level == 0.0, "群聊的负担泄漏到了私聊"


# ---------------------------------------------------------------- 分桶与描述


@pytest.mark.parametrize(
    ("level", "band", "has_text"),
    [
        (0.0, "low", False),
        (39.9, "low", False),
        (40.0, "medium", True),
        (69.9, "medium", True),
        (70.0, "high", True),
    ],
)
def test_band_thresholds(level: float, band: str, has_text: bool) -> None:
    from ling.domains.presence.load import LoadView

    view = LoadView(scope_key="w", level=level)
    assert view.band == band
    assert bool(view.describe()) is has_text


def test_describe_states_no_tone_instruction() -> None:
    """描述只陈述状态，**不规定语气**。

    用户明确要求行为风格由人设决定。这里一旦写成"你应该冷淡一点"，
    就等于把人格写死进代码了。
    """
    from ling.domains.presence.load import LoadView

    for level in (0.0, 50.0, 95.0):
        text = LoadView(scope_key="w", level=level).describe()
        for forbidden in ("冷淡", "生气", "攻击", "刻薄", "应该"):
            assert forbidden not in text, f"描述里出现了语气指令：{text}"
