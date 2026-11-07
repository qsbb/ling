"""她的一天（阶段 1.5）：真 PostgreSQL 上的大纲写入与幂等。

这组用例守这几件事（设计 §9 阶段 1.5 的可程序化验收）：

1. 同一个 actor 同一个计划日**只有一行** `day_plans`，重复调用只返回原结论
   （不重复生成、不重复烧预算）；
2. 计划日按**午夜**切分：D 的 ``[00:00,08:00)`` 预计睡眠由 D 自己的计划产出，
   两段睡眠锚点都在 D 内、没有跨夜段；
3. 模型坏 JSON → ``degraded`` + **规则大纲**（睡眠锚点还在），不把非法结果入库；
4. 非法结果可以在同预算内修复一次，修好就 ``ready``；
5. 显式目标日 D 允许在窗口开始前（D−1 21:00）预生成；
6. **没有新增 activity / self_experience**——计划不是事实，这是项目硬规则。

所有时间都用固定的 ``NOW``（计划日窗口起点 + 10 分钟 → 本地 00:10），
不依赖"现在几点"。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.timeline import (
    ensure_day_plan,
    load_day_plan,
    load_segments,
    local_minute_instant,
    outline_due_at,
    plan_day_window,
    run_due_outline,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    DayPlanRow,
    ScheduleSegmentRow,
    SelfExperienceRow,
)
from ling.models.router import StreamOutcome
from ling.ports.config import (
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
)
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
PLAN_DAY = date(2026, 11, 7)
WINDOW_START, WINDOW_END = plan_day_window(PLAN_DAY, TZ)
NOW = WINDOW_START.replace(minute=10)  # 本地 00:10，午夜日界后 10 分钟


def _iso(minute: int) -> str:
    return local_minute_instant(PLAN_DAY, minute, TZ).isoformat()


def _valid_outline() -> str:
    """两段在清醒时段（08:00–23:00）内的可选块，不压两段睡眠锚点。"""
    return json.dumps(
        {
            "segments": [
                {
                    "kind": "meal",
                    "title": "吃早饭",
                    "start_at": _iso(9 * 60),
                    "end_at": _iso(10 * 60),
                    "place_id": None,
                    "flexibility": "flexible",
                },
                {
                    "kind": "work",
                    "title": "写代码",
                    "start_at": _iso(13 * 60),
                    "end_at": _iso(15 * 60),
                    "place_id": None,
                    "flexibility": "fixed",
                },
            ]
        },
        ensure_ascii=False,
    )


class _StubRouter:
    """按顺序吐出预设文本；记录每次 prompt（只数次数，不落库）。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.prompts.append(messages[0].content)
        text = self._responses.pop(0) if self._responses else ""
        yield ModelChunk(delta=text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=text)


class _StubConfig:
    def __init__(self, config: EffectiveConfig) -> None:
        self._config = config

    async def effective(self) -> EffectiveConfig:
        return self._config


def _config(*, with_extract: bool = True) -> EffectiveConfig:
    providers = {
        "fake-main": ProviderConfig(
            provider_id="fake-main",
            kind="fake",
            models=(ProviderModel("plan-lite"),),
        )
    }
    profiles = (
        {
            "memory.extract": ProfileBinding(
                profile="memory.extract",
                provider_id="fake-main",
                model_id="plan-lite",
            )
        }
        if with_extract
        else {}
    )
    return EffectiveConfig(revision=1, providers=providers, profiles=profiles)


async def _seed() -> None:
    async with session_scope() as session:
        session.add(
            ActorRoutineRow(
                actor_id=ACTOR,
                timezone=TZ,
                wake_minute=8 * 60,
                rest_minute=23 * 60,
                source="configured",
                revision=1,
            )
        )


async def _counts() -> tuple[int, int, int, int]:
    async with session_scope() as session:
        plans = int(
            (
                await session.execute(
                    select(func.count()).select_from(DayPlanRow)
                )
            ).scalar_one()
        )
        activities = int(
            (
                await session.execute(
                    select(func.count()).select_from(ActivityRow)
                )
            ).scalar_one()
        )
        experiences = int(
            (
                await session.execute(
                    select(func.count()).select_from(SelfExperienceRow)
                )
            ).scalar_one()
        )
        segments = int(
            (
                await session.execute(
                    select(func.count()).select_from(ScheduleSegmentRow)
                )
            ).scalar_one()
        )
    return plans, activities, experiences, segments


async def _outline(*, responses: list[str], with_extract: bool = True):  # noqa: ANN201
    router = _StubRouter(responses)
    async with session_scope() as session:
        outcome = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=router,
            config_reader=_StubConfig(_config(with_extract=with_extract)),
            plan_day=PLAN_DAY,
            now=NOW,
            timezone=TZ,
        )
    return outcome, router


# ---------------------------------------------------------------- 幂等


async def test_outline_creates_one_row_and_is_idempotent(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed()
    first, router = await _outline(responses=[_valid_outline()])

    assert first.state == "ready", first
    assert first.source == "model"
    assert first.calls == 1
    assert first.revision == 1
    # 两段睡眠锚点（[00:00,08:00) 与 [23:00,24:00)）+ 两个模型块
    assert first.segments == 4, first
    assert len(router.prompts) == 1

    second, same_router = await _outline(responses=[])
    assert second.state == "unchanged", second
    assert second.revision == first.revision
    assert second.calls == 0
    # 没有第二次模型调用 → 也没有第二份 prompt
    assert len(same_router.prompts) == 0

    plans, activities, experiences, segments = await _counts()
    assert plans == 1, "同一计划日必须只有一行 day_plans"
    assert activities == 0 and experiences == 0, "计划不是事实：不许写活动/经历"
    assert segments == 4

    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None
        draft = await load_segments(session, plan_id=str(row.id), revision=row.revision)
    anchors = [item for item in draft if item.anchor_kind == "sleep"]
    # D 的 00:00–08:00 由 **D 自己**产出，两段都完全归属 D，没有跨夜段。
    assert [(item.start_at, item.end_at) for item in anchors] == [
        (WINDOW_START, WINDOW_START + timedelta(hours=8)),
        (WINDOW_START + timedelta(hours=23), WINDOW_END),
    ], "睡眠锚点应当是 D 内的 [00:00,08:00) 与 [23:00,24:00)"
    assert all(item.is_leaf is False for item in anchors)
    leaves = [item for item in draft if item.is_leaf]
    assert len(leaves) == 2
    # 叶段不重叠（UTC 半开区间）
    ordered = sorted(leaves, key=lambda item: item.start_at)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        assert previous.end_at <= current.start_at


# ---------------------------------------------------------------- 坏输出降级


async def test_bad_json_degrades_to_rule_outline(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed()
    outcome, router = await _outline(responses=["这不是 JSON", "仍然不是"])

    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == "outline_invalid", outcome
    assert outcome.calls == 2, "非法结果在同预算内修复一次"
    assert len(router.prompts) == 2
    assert outcome.segments == 2, "规则大纲只有两段睡眠锚点，不编可选活动"

    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None and row.state == "degraded"
        segments = await load_segments(session, plan_id=str(row.id))
    assert [item.anchor_kind for item in segments] == ["sleep", "sleep"]

    plans, activities, experiences, _ = await _counts()
    assert plans == 1
    assert activities == 0 and experiences == 0


async def test_invalid_then_repaired_outline_is_ready(web_env: TestEnv) -> None:  # noqa: ARG001
    """第一次越界/重叠，第二次修好 → ready（仍然只花 2 次预算）。"""
    bad = json.dumps(
        {
            "segments": [
                {
                    "kind": "meal",
                    "title": "吃早饭",
                    "start_at": _iso(5 * 60),
                    "end_at": _iso(6 * 60),
                    "place_id": None,
                    "flexibility": "flexible",
                },
                {
                    # 与上一段重叠：整份拒收
                    "kind": "work",
                    "title": "写代码",
                    "start_at": _iso(5 * 60 + 30),
                    "end_at": _iso(7 * 60),
                    "place_id": None,
                    "flexibility": "flexible",
                },
            ]
        }
    )
    outcome, router = await _outline(responses=[bad, _valid_outline()])
    assert outcome.state == "ready", outcome
    assert outcome.calls == 2
    assert len(router.prompts) == 2
    assert "重叠" in router.prompts[1], "修复提示必须带上下文里的具体原因"


async def test_past_segments_are_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """模型写过去 → 整份拒收 → 两次都坏就 degraded。"""
    past = json.dumps(
        {
            "segments": [
                {
                    "kind": "work",
                    "title": "昨天的事",
                    "start_at": (WINDOW_START - timedelta(minutes=30)).isoformat(),
                    "end_at": WINDOW_START.isoformat(),
                    "place_id": None,
                    "flexibility": "flexible",
                }
            ]
        }
    )
    outcome, _router = await _outline(responses=[past, past])
    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == "outline_invalid"
    plans, activities, experiences, _ = await _counts()
    assert plans == 1
    assert activities == 0 and experiences == 0


# ---------------------------------------------------------------- 模型不可用


async def test_model_unavailable_uses_rule_outline(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed()
    async with session_scope() as session:
        outcome = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=None,
            config_reader=None,
            plan_day=PLAN_DAY,
            now=NOW,
            timezone=TZ,
        )
    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == "model_unavailable"
    assert outcome.calls == 0
    assert outcome.segments == 2


async def test_extract_profile_unbound_does_not_borrow_dialogue(web_env: TestEnv) -> None:  # noqa: ARG001
    """`memory.extract` 没绑就不调模型：不借 `dialogue.fast`（设计 §0）。"""
    await _seed()
    router = _StubRouter([_valid_outline()])
    async with session_scope() as session:
        outcome = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=router,
            config_reader=_StubConfig(_config(with_extract=False)),
            plan_day=PLAN_DAY,
            now=NOW,
            timezone=TZ,
        )
    assert outcome.state == "degraded"
    assert outcome.reason_code == "model_unavailable"
    assert router.prompts == [], "没绑职责时不能偷偷换模型"


# ---------------------------------------------------------------- 显式目标日


async def test_run_due_outline_accepts_explicit_target_day(web_env: TestEnv) -> None:  # noqa: ARG001
    """显式传 D + D−1 21:00 到期点 → 可以**预生成**（旧代码此时会直接返回 None）。"""
    await _seed()
    due = outline_due_at(PLAN_DAY, TZ)
    assert due < WINDOW_START, "21:00 到期点早于 D 的午夜窗口起点"
    async with session_scope() as session:
        outcome = await run_due_outline(
            session,
            actor_id=ACTOR,
            router=None,
            config_reader=None,
            plan_day=PLAN_DAY,
            now=due,
        )
    assert outcome is not None, "显式目标日必须能在窗口开始前生成"
    assert outcome.plan_day == PLAN_DAY
    assert outcome.state == "degraded"
    assert outcome.reason_code == "model_unavailable"
    assert outcome.segments == 2

    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None
        assert row.window_start_at == WINDOW_START, "D 的窗口起点是本地午夜"
        assert row.window_end_at == WINDOW_END
        segments = await load_segments(session, plan_id=str(row.id))
    assert [item.anchor_kind for item in segments] == ["sleep", "sleep"]
    # 预生成也绝不回填过去、不越过午夜
    assert all(item.start_at >= WINDOW_START for item in segments)
    assert all(item.end_at <= WINDOW_END for item in segments)


# ---------------------------------------------------------------- 时区/作息变更


async def test_timezone_change_opens_new_revision(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed()
    first, _router = await _outline(responses=[_valid_outline()])
    assert first.revision == 1

    # 同一计划日、换成另一个规范时区：只增 revision，不新建日期行；
    # 快照版本 +1（幂等键随之变化）。
    async with session_scope() as session:
        second = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=_StubRouter([_valid_outline()]),
            config_reader=_StubConfig(_config()),
            plan_day=PLAN_DAY,
            now=NOW,
            timezone="Europe/London",
            timezone_source="explicit",
        )
    assert second.revision == 2, second
    assert second.timezone_revision == 1
    plans, _, _, _ = await _counts()
    assert plans == 1, "时区变更不能插出第二行日期行"

    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None and str(row.timezone) == "Europe/London"
        segments = await load_segments(session, plan_id=str(row.id))
    # 旧版本的段是 superseded（不物理删除），新版本有 current 段
    assert any(item.status == "superseded" for item in segments)
    assert any(item.status == "current" for item in segments)
