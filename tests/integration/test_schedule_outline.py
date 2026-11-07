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

阶段 1.5b 追加（同一个库、同样"零模型"的口径）：

7. ``outline_work`` 三档：``late`` 零模型、``catchup`` 只排 now 之后（不回填）
   且只补试**可恢复**原因、相位只会更保守；
8. ``build_persona_projection``：结构化、缺字段写"未知"、≤400 粗略 token、
   **完整 system_prompt 不进日程输入**；
9. ``seam_check``：冲突可选段置 superseded + ``reason:needs_review``，两日睡眠
   边界重叠只注解；幂等——无变化**不增 revision**、不写重复原因码、零模型；
10. ``on_new_confirmed_anchor``：只登记 ``todo:replan`` 待办，不改版本、不调模型。

所有时间都用固定的 ``NOW``（计划日窗口起点 + 10 分钟 → 本地 00:10），
不依赖"现在几点"。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.intents import (
    CONFIRM_ENTRY_USER,
    IntentService,
)
from ling.domains.agenda.timeline import (
    PHASE_CATCHUP,
    PHASE_DUE,
    PHASE_LATE,
    REASON_DEGRADED_LATE,
    REASON_NEEDS_REVIEW,
    REASON_SLEEP_BOUNDARY,
    REPLAN_TODO_TAG,
    Anchor,
    build_persona_projection,
    ensure_day_plan,
    intention_short_id,
    load_day_plan,
    load_segments,
    local_minute_instant,
    on_new_confirmed_anchor,
    outline_due_at,
    outline_work,
    plan_day_window,
    run_due_outline,
    seam_check,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    BehaviorModelCallRow,
    CommitmentRow,
    CoreMemoryRow,
    DayPlanRow,
    MemoryRow,
    Persona,
    ScheduleIntentionRow,
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
from ling.settings.user_preferences import owner_subject_id
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


async def _work(phase: str, *, responses: list[str], now):  # noqa: ANN001, ANN201
    """按相位跑一次大纲作业（模型调用的"次数"由 stub 记下来）。"""
    router = _StubRouter(responses)
    async with session_scope() as session:
        outcome = await outline_work(
            session,
            PLAN_DAY,
            phase,
            actor_id=ACTOR,
            router=router,
            config_reader=_StubConfig(_config()),
            now=now,
            timezone=TZ,
        )
    return outcome, router


async def _model_calls() -> int:
    """``behavior_model_calls`` 的行数：零模型的分支必须让它保持不变。"""
    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(BehaviorModelCallRow)
                )
            ).scalar_one()
        )


async def _segments() -> list[ScheduleSegmentRow]:
    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None
        return await load_segments(session, plan_id=str(row.id))


async def _day_row() -> DayPlanRow:
    async with session_scope() as session:
        row = await load_day_plan(session, actor_id=ACTOR, plan_day=PLAN_DAY)
        assert row is not None
        return row


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


# ---------------------------------------------------------------- outline_work 三档


async def test_outline_work_late_is_zero_model_and_keeps_future_anchors_only(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """已到/过了醒点：**零模型**，只补未过去的确定性锚点，原因码 degraded_late。"""
    await _seed()
    late_now = local_minute_instant(PLAN_DAY, 10 * 60, TZ)  # D 本地 10:00
    calls_before = await _model_calls()

    outcome, router = await _work(PHASE_LATE, responses=[_valid_outline()], now=late_now)

    assert outcome.phase == PHASE_LATE, outcome
    assert router.prompts == [], "late 相位结构上不许调模型"
    assert outcome.calls == 0
    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == REASON_DEGRADED_LATE, outcome
    assert await _model_calls() == calls_before, "零模型：不许留下预算行"

    segments = await _segments()
    # 早上那段 [00:00,08:00) 已经过去 → 不回填；只剩夜里 [23:00,24:00)
    assert [(item.anchor_kind, item.start_at, item.end_at) for item in segments] == [
        ("sleep", WINDOW_START + timedelta(hours=23), WINDOW_END)
    ]
    row = await _day_row()
    assert str(row.last_error_code) == REASON_DEGRADED_LATE, "原因码落在 day_plans"
    plans, activities, experiences, _segments_count = await _counts()
    assert plans == 1
    assert activities == 0 and experiences == 0, "计划不是事实"


async def test_outline_work_late_does_not_downgrade_ready_plan(web_env: TestEnv) -> None:  # noqa: ARG001
    """已经 ready 的计划**不因"到点了"被降级**：晚到只是"还没 ready 时"的处置。"""
    await _seed()
    first, _router = await _outline(responses=[_valid_outline()])
    assert first.state == "ready"
    late_now = local_minute_instant(PLAN_DAY, 10 * 60, TZ)

    outcome, router = await _work(PHASE_LATE, responses=[_valid_outline()], now=late_now)

    assert outcome.state == "unchanged", outcome
    assert outcome.reason_code is None, "ready 的计划不许被打上 degraded_late"
    assert outcome.revision == first.revision, "不变就不增 revision"
    assert router.prompts == []
    row = await _day_row()
    assert str(row.state) == "ready"
    assert row.last_error_code is None


async def test_outline_work_catchup_rejects_past_segments(web_env: TestEnv) -> None:  # noqa: ARG001
    """catchup 从 ``now`` 起排：模型写凌晨 01:00 的可选段 → 整份拒收、不回填。"""
    await _seed()
    catch_now = local_minute_instant(PLAN_DAY, 3 * 60, TZ)  # D 本地 03:00
    past = json.dumps(
        {
            "segments": [
                {
                    "kind": "study",
                    "title": "凌晨的复习",
                    "start_at": _iso(60),
                    "end_at": _iso(120),
                    "place_id": None,
                    "flexibility": "flexible",
                }
            ]
        }
    )
    outcome, router = await _work(PHASE_CATCHUP, responses=[past, past], now=catch_now)

    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == "outline_invalid"
    assert outcome.calls == 2
    assert len(router.prompts) == 2
    segments = await _segments()
    assert [item for item in segments if item.anchor_kind == ""] == [], (
        "过午夜后不许回填可选段"
    )


async def test_outline_work_catchup_places_only_future_segments(web_env: TestEnv) -> None:  # noqa: ARG001
    """catchup 正常路径：模型段只落在 ``now`` 之后，prompt 里的可变起点就是 now。"""
    await _seed()
    catch_now = local_minute_instant(PLAN_DAY, 3 * 60, TZ)
    outcome, router = await _work(
        PHASE_CATCHUP, responses=[_valid_outline()], now=catch_now
    )

    assert outcome.state == "ready", outcome
    assert outcome.calls == 1
    assert outcome.phase == PHASE_CATCHUP
    assert "03:00" in router.prompts[0], "可变起点必须是 now（本地 03:00）"
    segments = await _segments()
    model_segments = [item for item in segments if item.source == "model"]
    assert len(model_segments) == 2
    assert all(item.start_at >= catch_now for item in model_segments)


async def test_outline_work_catchup_retries_retryable_failure(web_env: TestEnv) -> None:  # noqa: ARG001
    """21:00 模型暂不可用（可恢复）→ 醒前 catch-up 用同一作业与预算补试一次。"""
    await _seed()
    due = outline_due_at(PLAN_DAY, TZ)
    async with session_scope() as session:
        first = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=None,
            config_reader=None,
            plan_day=PLAN_DAY,
            now=due,
            timezone=TZ,
        )
    assert first.state == "degraded", first
    assert first.reason_code == "model_unavailable"

    catch_now = local_minute_instant(PLAN_DAY, 3 * 60, TZ)
    outcome, router = await _work(
        PHASE_CATCHUP, responses=[_valid_outline()], now=catch_now
    )
    assert outcome.state == "ready", outcome
    assert outcome.calls == 1
    assert len(router.prompts) == 1
    assert outcome.revision == first.revision + 1


async def test_outline_work_catchup_does_not_retry_final_failure(web_env: TestEnv) -> None:  # noqa: ARG001
    """非法输出用满两次（终态）→ catch-up 不再重试：白烧钱且让"没安排"不稳定。"""
    await _seed()
    due = outline_due_at(PLAN_DAY, TZ)
    async with session_scope() as session:
        first = await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=_StubRouter(["不是 JSON", "仍然不是"]),
            config_reader=_StubConfig(_config()),
            plan_day=PLAN_DAY,
            now=due,
            timezone=TZ,
        )
    assert first.state == "degraded" and first.calls == 2
    assert first.reason_code == "outline_invalid"

    catch_now = local_minute_instant(PLAN_DAY, 3 * 60, TZ)
    outcome, router = await _work(
        PHASE_CATCHUP, responses=[_valid_outline()], now=catch_now
    )
    assert outcome.state == "unchanged", outcome
    assert outcome.calls == 0
    assert router.prompts == []


async def test_outline_work_due_after_wake_escalates_to_late_without_model(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """相位只允许更保守：给了 due、但按 now 已经过了醒点 → 降成 late、零模型。"""
    await _seed()
    late_now = local_minute_instant(PLAN_DAY, 10 * 60, TZ)
    outcome, router = await _work(PHASE_DUE, responses=[_valid_outline()], now=late_now)

    assert outcome.phase == PHASE_LATE
    assert router.prompts == []
    assert outcome.reason_code == REASON_DEGRADED_LATE


# ---------------------------------------------------------------- 人设投影


async def _seed_persona(**kwargs: object) -> None:
    """改默认人设（app 启动时已经建了 ``persona_ling``，这里只覆盖字段）。"""
    async with session_scope() as session:
        persona = await session.get(Persona, "persona_ling")
        if persona is None:
            persona = Persona(
                id="persona_ling",
                revision=1,
                name="ling",
                display_name="灵",
                published=True,
            )
            session.add(persona)
        persona.revision = int(persona.revision or 0) + 1
        persona.name = "ling"
        persona.display_name = "灵"
        persona.system_prompt = str(kwargs.get("system_prompt", ""))
        persona.traits = dict(kwargs.get("traits", {}))  # type: ignore[arg-type]
        persona.boundaries = list(kwargs.get("boundaries", []))  # type: ignore[arg-type]
        persona.scenario_prompts = dict(  # type: ignore[arg-type]
            kwargs.get("scenario_prompts", {})
        )
        persona.published = True


async def test_persona_projection_marks_unknowns_and_never_leaks_system_prompt(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """缺字段写"未知"；完整 ``system_prompt`` **不**进日程输入。"""
    from ling.kernel.context import estimate_tokens

    secret = "SYSTEM-PROMPT-SECRET-这是完整人设正文，不该进日程调用"
    await _seed_persona(system_prompt=secret)
    async with session_scope() as session:
        projection = await build_persona_projection(
            session, "persona_ling", actor_id=ACTOR, now=NOW
        )

    payload = projection.as_dict()
    dumped = json.dumps(payload, ensure_ascii=False)
    assert secret not in dumped
    assert payload["identity"] == "称呼：灵"
    for key in (
        "personality",
        "values",
        "habits",
        "interests",
        "boundaries",
        "scenario_rules",
        "relationships",
        "situation",
    ):
        assert payload[key] == ["未知"], key
    assert projection.token_estimate == estimate_tokens(dumped), (
        "token 数必须是同一个粗略估算口径，不能是编的"
    )
    assert projection.token_estimate <= projection.token_limit == 400
    assert projection.truncated is False


async def test_persona_projection_without_persona_row_is_unknown(web_env: TestEnv) -> None:  # noqa: ARG001
    """没有人设行也不能崩：写"未知"，不从空白处编人格。"""
    async with session_scope() as session:
        projection = await build_persona_projection(
            session, "persona_missing", actor_id=ACTOR, now=NOW
        )
    assert projection.identity == "未知"
    assert projection.name == ""
    assert projection.as_dict()["boundaries"] == ["未知"]


async def test_persona_projection_is_bounded_and_truncated(web_env: TestEnv) -> None:  # noqa: ARG001
    """长人设压到 ≤400 粗略 token；边界与价值观不被优先挤掉。"""
    from ling.kernel.context import estimate_tokens

    traits: dict[str, object] = {
        f"interest_{index:02d}": "喜欢在傍晚读很长的诗" * 8 for index in range(20)
    }
    traits.update(
        {f"habit_{index:02d}": "每天早上先泡一杯茶" * 8 for index in range(10)}
    )
    traits["values_core"] = "不骗人、不蹭别人的隐私" * 6
    await _seed_persona(
        traits=traits,
        boundaries=["不把模拟生活说成真实经历" * 6 for _ in range(6)],
        scenario_prompts={
            "private": "私聊时说话慢一点" * 20,
            "group": "群里少说两句" * 20,
        },
    )
    async with session_scope() as session:
        projection = await build_persona_projection(
            session, "persona_ling", actor_id=ACTOR, now=NOW
        )

    payload = projection.as_dict()
    assert projection.token_estimate <= 400, projection.token_estimate
    assert projection.token_estimate == estimate_tokens(
        json.dumps(payload, ensure_ascii=False)
    )
    assert projection.truncated is True
    assert payload["boundaries"] != ["未知"], "边界不能被挤掉"
    assert payload["values"] != ["未知"], "核心价值观不能被挤掉"
    # 可选场景先被削：最多留一条
    assert len(projection.scenario_rules) <= 1


# ---------------------------------------------------------------- 午夜接缝


async def _add_commitment(commitment_id: str, start_minute: int, text: str) -> None:
    async with session_scope() as session:
        session.add(
            CommitmentRow(
                id=commitment_id,
                actor_id=ACTOR,
                subject_id="subject_owner",
                text=text,
                due_at=local_minute_instant(PLAN_DAY, start_minute, TZ),
                timezone=TZ,
                state="active",
                source_event_ids=[],
            )
        )


# ---------------------------------------------------------------- 锚点冲突进计划


async def test_outline_keeps_commitment_before_wake_and_tags_routine_conflict(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """07:00 硬承诺 vs 08:00 作息：保留承诺、缩短预计睡眠并标 routine_conflict。"""
    await _seed()
    await _add_commitment("cm_wake", 7 * 60, "早上七点叫我起床")
    outcome, _router = await _outline(responses=[_valid_outline()])
    assert outcome.state == "ready", outcome
    assert outcome.reason_code is None, "单条 routine_conflict 不该让整天变成待复核"

    segments = await _segments()
    commitments = [item for item in segments if item.anchor_kind == "commitment"]
    assert [(item.start_at, item.end_at) for item in commitments] == [
        (
            local_minute_instant(PLAN_DAY, 7 * 60, TZ),
            local_minute_instant(PLAN_DAY, 8 * 60, TZ),
        )
    ], "承诺本身不能被子系统挪动"
    assert "commitment:cm_wake" in commitments[0].source_event_ids

    morning_sleep = [
        item
        for item in segments
        if item.anchor_kind == "sleep" and item.start_at == WINDOW_START
    ]
    assert len(morning_sleep) == 1
    assert morning_sleep[0].end_at == local_minute_instant(PLAN_DAY, 7 * 60, TZ), (
        "预计睡眠被裁到承诺开始处"
    )
    assert "reason:routine_conflict" in morning_sleep[0].source_event_ids


async def test_outline_marks_needs_review_when_sleep_falls_below_floor(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """承诺把凌晨预计睡眠压到保护下限以下 → 计划行标 needs_review。"""
    await _seed()
    # 承诺保护窗是 [due, due+60)：四个小时各一条，压掉 00:00–04:00 的预计睡眠，
    # 剩余 9h − 4h = 5h < 420 分钟。
    for hour in range(4):
        await _add_commitment(f"cm_night_{hour}", hour * 60, f"凌晨{hour}点的事")
    outcome, _router = await _outline(responses=[_valid_outline()])
    assert outcome.state == "ready", outcome
    assert outcome.reason_code == REASON_NEEDS_REVIEW, outcome
    assert (await _day_row()).last_error_code == REASON_NEEDS_REVIEW

    segments = await _segments()
    sleep = [item for item in segments if item.anchor_kind == "sleep"]
    # 凌晨那段被压到 04:00–08:00；夜里的 [23:00,24:00) 还在，两段都带 needs_review
    assert [(item.start_at, item.end_at) for item in sleep] == [
        (local_minute_instant(PLAN_DAY, 4 * 60, TZ), local_minute_instant(PLAN_DAY, 8 * 60, TZ)),
        (WINDOW_START + timedelta(hours=23), WINDOW_END),
    ]
    assert all("reason:needs_review" in item.source_event_ids for item in sleep), (
        "整天睡眠低于下限时，剩下的睡眠段都带 needs_review"
    )
    assert "reason:routine_conflict" in sleep[0].source_event_ids, (
        "被承诺裁过的那一段还要标 routine_conflict"
    )


async def test_seam_check_supersedes_conflicting_option_and_is_idempotent(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """21:00 后新确认的约定压住可选段：置 superseded + 原因码；重复调用不增版本。"""
    await _seed()
    first, _router = await _outline(responses=[_valid_outline()])
    assert first.state == "ready"
    await _add_commitment("cm_after_2100", 13 * 60, "下午三点开会")
    calls_before = await _model_calls()

    async with session_scope() as session:
        report = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=WINDOW_START)

    assert report.calls == 0, "接缝校验是零模型"
    assert await _model_calls() == calls_before
    assert report.state == "superseded", report
    assert report.revision_after == report.revision_before + 1
    assert report.reason_codes == (REASON_NEEDS_REVIEW,)
    assert len(report.superseded_segment_ids) == 1
    assert report.frozen_segment_ids == ()

    segments = await _segments()
    hit = [item for item in segments if item.status == "superseded"]
    assert [item.title for item in hit] == ["写代码"]
    assert f"reason:{REASON_NEEDS_REVIEW}" in hit[0].source_event_ids
    assert "commitment:cm_after_2100" in hit[0].source_event_ids
    row = await _day_row()
    assert str(row.last_error_code) == REASON_NEEDS_REVIEW

    # 幂等：第二次什么都不改——不增 revision、不写重复原因码
    async with session_scope() as session:
        again = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=WINDOW_START)
    assert again.state == "unchanged", again
    assert again.changed is False
    assert again.revision_after == again.revision_before == report.revision_after

    segments = await _segments()
    hit = [item for item in segments if item.id in report.superseded_segment_ids]
    assert hit[0].source_event_ids.count(f"reason:{REASON_NEEDS_REVIEW}") == 1
    assert (await _day_row()).revision == report.revision_after


async def test_seam_check_without_plan_is_a_noop(web_env: TestEnv) -> None:  # noqa: ARG001
    """还没有这一天的计划：没有可复核的段，更不能凭空建行。"""
    await _seed()
    async with session_scope() as session:
        report = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=WINDOW_START)
    assert report.state == "no_plan"
    assert report.calls == 0
    plans, activities, experiences, pieces = await _counts()
    assert (plans, activities, experiences, pieces) == (0, 0, 0, 0)


async def test_seam_check_flags_two_day_sleep_boundary_without_bumping_revision(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """两日睡眠边界重叠（旧时区残留）：只补原因码并报出来，不移动段、不增 revision。"""
    await _seed()
    previous_day = PLAN_DAY - timedelta(days=1)
    previous_start, _previous_end = plan_day_window(previous_day, TZ)
    async with session_scope() as session:
        await ensure_day_plan(
            session,
            actor_id=ACTOR,
            router=None,
            config_reader=None,
            plan_day=previous_day,
            now=previous_start.replace(minute=10),
            timezone=TZ,
        )
    await _outline(responses=[_valid_outline()])
    calls_before = await _model_calls()

    # 人为造一个"旧窗口残留"：D−1 的夜间睡眠段伸进 D 的午夜窗口。
    async with session_scope() as session:
        previous_row = await load_day_plan(
            session, actor_id=ACTOR, plan_day=previous_day
        )
        assert previous_row is not None
        previous_segments = await load_segments(session, plan_id=str(previous_row.id))
        evening = max(
            (item for item in previous_segments if item.anchor_kind == "sleep"),
            key=lambda item: item.start_at,
        )
        evening.start_at = WINDOW_START - timedelta(hours=1)
        evening.end_at = WINDOW_START + timedelta(hours=1)

    async with session_scope() as session:
        report = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=WINDOW_START)

    assert report.boundary_conflicts, "两日睡眠边界重叠必须被报出来"
    assert REASON_SLEEP_BOUNDARY in report.reason_codes
    assert report.state == "annotated", report
    assert report.revision_after == report.revision_before, "只注解，不增 revision"
    assert report.calls == 0
    assert await _model_calls() == calls_before
    assert (await _day_row()).last_error_code == REASON_SLEEP_BOUNDARY

    segments = await _segments()
    morning = [
        item
        for item in segments
        if item.anchor_kind == "sleep" and item.start_at == WINDOW_START
    ]
    assert len(morning) == 1
    assert f"reason:{REASON_SLEEP_BOUNDARY}" in morning[0].source_event_ids

    # 再来一次：同一个码不重复写、版本不动
    async with session_scope() as session:
        again = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=WINDOW_START)
    assert again.state == "unchanged"
    assert again.revision_after == again.revision_before == report.revision_after
    segments = await _segments()
    morning = [
        item
        for item in segments
        if item.anchor_kind == "sleep" and item.start_at == WINDOW_START
    ]
    assert morning[0].source_event_ids.count(f"reason:{REASON_SLEEP_BOUNDARY}") == 1


# ---------------------------------------------------------------- 新确认锚点


def _new_anchor(start_minute: int, end_minute: int) -> Anchor:
    return Anchor(
        kind="commitment",
        start_at=local_minute_instant(PLAN_DAY, start_minute, TZ),
        end_at=local_minute_instant(PLAN_DAY, end_minute, TZ),
        title="约定：临时加的会",
        protective=True,
        source_id="commitment:cm_new",
    )


async def test_on_new_confirmed_anchor_registers_replan_todo_without_model(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """只登记 todo:replan：不调模型、不改版本、标记不重复。"""
    await _seed()
    first, _router = await _outline(responses=[_valid_outline()])
    anchor = _new_anchor(13 * 60, 14 * 60)
    calls_before = await _model_calls()

    async with session_scope() as session:
        todo = await on_new_confirmed_anchor(
            session, PLAN_DAY, anchor, actor_id=ACTOR, now=WINDOW_START
        )

    assert todo is not None
    assert todo.tag == REPLAN_TODO_TAG
    assert todo.registered is True
    assert await _model_calls() == calls_before, "登记待办不调模型"
    assert len(todo.segment_ids) == 1
    # 范围 = 受影响段 ∪ 新锚点（局部，不扩展到整天）
    assert todo.range_start == local_minute_instant(PLAN_DAY, 13 * 60, TZ)
    assert todo.range_end == local_minute_instant(PLAN_DAY, 15 * 60, TZ)
    assert "commitment:cm_new" in todo.anchor_ids

    row = await _day_row()
    assert int(row.revision) == first.revision, "登记待办不等于已经改过计划"
    segments = await _segments()
    marked = [
        item
        for item in segments
        if REPLAN_TODO_TAG in {str(tag) for tag in (item.source_event_ids or [])}
    ]
    assert [item.title for item in marked] == ["写代码"]
    assert "commitment:cm_new" in marked[0].source_event_ids

    # 第二次：标记不重复，返回同一范围
    async with session_scope() as session:
        again = await on_new_confirmed_anchor(
            session, PLAN_DAY, anchor, actor_id=ACTOR, now=WINDOW_START
        )
    assert again is not None and again.segment_ids == todo.segment_ids
    segments = await _segments()
    marked = [item for item in segments if item.id == todo.segment_ids[0]]
    assert marked[0].source_event_ids.count(REPLAN_TODO_TAG) == 1


async def test_on_new_confirmed_anchor_returns_none_when_nothing_affected(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """锚点落在没有段可重排的空档 → 不登记待办（21:00 的生成会看到它）。"""
    await _seed()
    await _outline(responses=[_valid_outline()])
    anchor = _new_anchor(16 * 60, 17 * 60)  # 13:00–15:00 与 23:00 之间是空档
    async with session_scope() as session:
        todo = await on_new_confirmed_anchor(
            session, PLAN_DAY, anchor, actor_id=ACTOR, now=WINDOW_START
        )
    assert todo is None

    # 完全落在 D 之外的锚点同样不影响这一天
    outside = Anchor(
        kind="commitment",
        start_at=WINDOW_START - timedelta(hours=2),
        end_at=WINDOW_START - timedelta(hours=1),
        title="昨天的约定",
        source_id="commitment:cm_yesterday",
    )
    async with session_scope() as session:
        assert (
            await on_new_confirmed_anchor(
                session, PLAN_DAY, outside, actor_id=ACTOR, now=WINDOW_START
            )
            is None
        )


# ---------------------------------------------------------------- 域内转发入口


async def test_agenda_service_forwards_timeline_entries(web_env: TestEnv) -> None:  # noqa: ARG001
    """``AgendaService`` 的三个转发入口真的通到 timeline（其他域只认这道门）。"""
    from ling.domains.agenda.service import AgendaService

    await _seed()
    first, _router = await _outline(responses=[_valid_outline()])
    late_now = local_minute_instant(PLAN_DAY, 10 * 60, TZ)
    async with session_scope() as session:
        service = AgendaService(session, actor_id=ACTOR)
        outcome = await service.outline_work(
            PLAN_DAY, PHASE_LATE, now=late_now, timezone=TZ
        )
        assert outcome.state == "unchanged", outcome
        assert outcome.phase == PHASE_LATE
        assert outcome.revision == first.revision

        seam = await service.seam_check(PLAN_DAY, now=WINDOW_START)
        assert seam.state == "unchanged"
        assert seam.calls == 0

        todo = await service.on_new_confirmed_anchor(
            PLAN_DAY, _new_anchor(13 * 60, 14 * 60), now=WINDOW_START
        )
        assert todo is not None
        assert todo.tag == REPLAN_TODO_TAG
        assert todo.segment_ids


# ---------------------------------------------------------------- 长程意向输入（§3A）
#
# 这一组守设计 §3A 的验收：
# 1. `proposed` / `confirmed` 的意向都**不占时段**（确认目标 ≠ 确认时段）；
# 2. 引用意向只能用它给出的短 ID；候选引用必须带"可能/打算"语气；
# 3. "为未来目标做准备"的段要**同时**满足：confirmed + 另有可信截止依据
#    （`prep_source_ref`）+ D 有空档；没有依据就不许写"今天买票"；
# 4. 模型新提议只落 `proposed`，越权（给 confirmed）整份拒收；
# 5. 进 prompt 之前先做过期清理（零模型）。


async def _create_intent(
    *,
    state: str = "planned",
    source_kind: str = "user",
    source_ref: str = "turn:seed",
    title: str = "周五去杭州看展",
    start_day: date | None = None,
    end_day: date | None = None,
    prep_source_ref: str | None = None,
    prep_due_day: date | None = None,
    visibility: str = "actor_private",
    now=None,  # noqa: ANN001
):  # noqa: ANN201
    """建一条意向（可选直接 confirmed）；返回 ``IntentView``。"""
    target_start = start_day or (PLAN_DAY + timedelta(days=4))
    target_end = end_day or target_start
    # `confirmed` 只能走 confirm() 的两条授权入口；create 只建 proposed/planned。
    created_state = "planned" if state == "confirmed" else state
    async with session_scope() as session:
        service = IntentService(session, actor_id=ACTOR)
        view = await service.create(
            kind="travel",
            title=title,
            target_start_day=target_start,
            target_end_day=target_end,
            source_kind=source_kind,
            source_ref=source_ref,
            state=created_state,
            target_timezone=TZ,
            visibility=visibility,
            prep_source_ref=prep_source_ref,
            prep_due_day=prep_due_day,
            now=now or NOW,
        )
        if state == "confirmed":
            view = await service.confirm(
                view.intention_id,
                entry=CONFIRM_ENTRY_USER,
                evidence_ref="turn:confirmed-seed",
                now=now or NOW,
            )
    return view


def _outline_with_citation(
    short_id: str,
    *,
    title: str,
    start_minute: int = 10 * 60,
    end_minute: int = 11 * 60,
    proposals: list[dict[str, object]] | None = None,
) -> str:
    """一段引用某意向 + 一段普通可选块（10:00 落在睡眠锚点之间的大空档里）。"""
    segments: list[dict[str, object]] = [
        {
            "kind": "errand",
            "title": title,
            "start_at": _iso(start_minute),
            "end_at": _iso(end_minute),
            "place_id": None,
            "flexibility": "flexible",
            "intention_id": short_id,
        },
        {
            "kind": "reading",
            "title": "看会儿书",
            "start_at": _iso(14 * 60),
            "end_at": _iso(15 * 60),
            "place_id": None,
            "flexibility": "flexible",
        },
    ]
    payload: dict[str, object] = {"segments": segments}
    if proposals is not None:
        payload["intention_proposals"] = proposals
    return json.dumps(payload, ensure_ascii=False)


def _segment_titled(rows: list[ScheduleSegmentRow], needle: str) -> list[ScheduleSegmentRow]:
    return [row for row in rows if needle in str(row.title)]


async def test_proposed_intent_is_prompt_input_but_never_a_segment(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """`proposed` 只影响候选与措辞：进 prompt，但不写成时间轴正文。"""
    await _seed()
    intent = await _create_intent(state="proposed", source_kind="model", title="去看看海")
    short = intention_short_id(intent.intention_id)

    outcome, router = await _work(PHASE_DUE, responses=[_valid_outline()], now=NOW)

    assert outcome.state == "ready", outcome
    assert len(router.prompts) == 1
    prompt = router.prompts[0]
    # 进的是**短字段**：短 ID + 标题 + 状态；不是整行、也不是正文。
    assert short in prompt, "意向必须带短 ID 出现在输入里"
    assert "去看看海" in prompt
    assert '"future_intents"' in prompt
    assert intent.intention_id not in prompt, "prompt 里不应出现完整内部 id（省 token）"

    # 时间轴上只有睡眠锚点 + 模型块：没有任何段引用意向。
    rows = await _segments()
    assert [row for row in rows if row.intention_id] == []
    assert _segment_titled(rows, "去看看海") == []
    assert outcome.preparations == 0

    # 反复进入夜间输入**不构成确认证据**：状态与版本都不动。
    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, intent.intention_id)
        assert row is not None
        assert row.state == "proposed"
        assert row.revision == intent.revision

    _, activities, experiences, _segments_count = await _counts()
    assert activities == 0 and experiences == 0, "计划不是事实：不许写活动/经历"


async def test_confirmed_target_still_does_not_reserve_a_time_slot(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """确认"周五想旅行"不等于占住任何钟点（与阶段 1.75a 的断言一致）。"""
    await _seed()
    intent = await _create_intent(state="confirmed", title="周五去杭州看展")
    short = intention_short_id(intent.intention_id)

    outcome, router = await _work(PHASE_DUE, responses=[_valid_outline()], now=NOW)

    assert outcome.state == "ready", outcome
    assert outcome.intents_in_prompt == 1
    assert short in router.prompts[0]
    # 只有 D 的两段睡眠锚点 + 两个模型块；D+4 上没有任何段/计划行。
    assert outcome.segments == 4, outcome
    async with session_scope() as session:
        plans = list(
            (await session.execute(select(DayPlanRow))).scalars()
        )
        assert [row.plan_day for row in plans] == [PLAN_DAY]
        segments = list((await session.execute(select(ScheduleSegmentRow))).scalars())
    assert all(row.plan_id == plans[0].id for row in segments)
    assert [row for row in segments if row.intention_id] == []
    assert _segment_titled(segments, "周五去杭州看展") == []


async def test_preparation_without_trusted_source_is_never_written(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """D+4 旅行 confirmed 但**没有** prep_source_ref：不许生成"今天买票"。"""
    await _seed()
    intent = await _create_intent(state="confirmed", title="周五去杭州看展")
    assert intent.prep_source_ref is None
    short = intention_short_id(intent.intention_id)
    bad = _outline_with_citation(short, title="今天把去杭州的票买了")

    outcome, router = await _work(PHASE_DUE, responses=[bad, bad], now=NOW)

    # 两次都非法 → 规则兜底；关键是**没有**落下任何"准备/买票"的段。
    assert outcome.state == "degraded", outcome
    assert outcome.reason_code == "outline_invalid", outcome
    assert len(router.prompts) == 2
    assert "还没有准备资格" in router.prompts[1], router.prompts[1][-400:]
    rows = await _segments()
    assert _segment_titled(rows, "买票") == []
    assert [row for row in rows if row.intention_id] == []
    assert outcome.preparations == 0


async def test_preparation_is_attached_with_strictest_visibility(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """有可信依据 + D 有空档：生成准备段，并写 intention_id / preparation_for_day。"""
    await _seed()
    target_day = PLAN_DAY + timedelta(days=4)
    intent = await _create_intent(
        state="confirmed",
        title="周五去杭州看展",
        # 意向本身故意标成 public：准备段的可见范围**取最严**，不能被它带宽。
        visibility="public",
        prep_source_ref="commitment:seed-ticket",
        prep_due_day=PLAN_DAY,
    )
    short = intention_short_id(intent.intention_id)

    outcome, _router = await _work(
        PHASE_DUE,
        responses=[
            _outline_with_citation(short, title="给周五的旅行做准备"),
        ],
        now=NOW,
    )

    assert outcome.state == "ready", outcome
    assert outcome.preparations == 1, outcome
    rows = await _segments()
    prepared = [row for row in rows if row.intention_id]
    assert len(prepared) == 1
    segment = prepared[0]
    assert str(segment.intention_id) == intent.intention_id
    assert segment.preparation_for_day == target_day
    # 三道来源里最严的那个：段（未知）→ 意向（public）→ 来源（未知按最严）。
    assert segment.visibility == "actor_private"
    # 审计足迹：意向 id + 确认来源（不带标题）。
    assert f"intention:{intent.intention_id}" in segment.source_event_ids
    assert "turn:confirmed-seed" in segment.source_event_ids
    # 准备段仍是**计划**：没有活动、没有经历。
    _, activities, experiences, _ = await _counts()
    assert activities == 0 and experiences == 0


async def test_completion_claim_in_title_is_rejected_and_repaired(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """标题写成"已经去了" → 整份拒收；同预算内修好后仍可 ready。"""
    await _seed()
    claim = json.dumps(
        {
            "segments": [
                {
                    "kind": "leisure",
                    "title": "已经去了杭州看展",
                    "start_at": _iso(9 * 60),
                    "end_at": _iso(10 * 60),
                    "place_id": None,
                    "flexibility": "flexible",
                }
            ]
        },
        ensure_ascii=False,
    )
    outcome, router = await _work(
        PHASE_DUE, responses=[claim, _valid_outline()], now=NOW
    )

    assert outcome.state == "ready", outcome
    assert outcome.calls == 2
    assert "完成或承诺" in router.prompts[1], router.prompts[1][-400:]
    rows = await _segments()
    assert [row for row in rows if "已经" in str(row.title)] == []
    assert {row.title for row in rows if row.status == "current"} == {
        "吃早饭",
        "写代码",
        "睡觉",
    }


async def test_model_proposal_cannot_escalate_to_confirmed(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """模型新提议越权（直接给 confirmed）→ 整份拒收，一行都不落。"""
    await _seed()
    intent = await _create_intent(state="proposed", source_kind="model")
    short = intention_short_id(intent.intention_id)
    escalated = _outline_with_citation(
        short,
        title="可能去杭州看展",
        proposals=[
            {
                "theme": "周末去杭州看展",
                "target_start_day": (PLAN_DAY + timedelta(days=5)).isoformat(),
                "target_end_day": (PLAN_DAY + timedelta(days=5)).isoformat(),
                "source_ref": short,
                "state": "confirmed",
            }
        ],
    )

    outcome, router = await _work(PHASE_DUE, responses=[escalated, escalated], now=NOW)

    assert outcome.state == "degraded", outcome
    assert outcome.proposed_intentions == 0
    assert "未定义字段" in router.prompts[1]
    async with session_scope() as session:
        rows = list((await session.execute(select(ScheduleIntentionRow))).scalars())
    assert len(rows) == 1, "越权提议不许落任何新行"
    assert rows[0].state == "proposed"


async def test_model_proposal_lands_as_proposed_only(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """合法提议只能落 `proposed`，且 source_ref 用输入短 ID 回查的上游内部 id。"""
    await _seed()
    intent = await _create_intent(state="planned", title="想去看展")
    short = intention_short_id(intent.intention_id)
    target = PLAN_DAY + timedelta(days=6)
    payload = _outline_with_citation(
        short,
        title="可能去看展",
        proposals=[
            {
                "theme": "周末去杭州看展",
                "target_start_day": target.isoformat(),
                "target_end_day": target.isoformat(),
                "source_ref": short,
            }
        ],
    )

    outcome, _router = await _work(PHASE_DUE, responses=[payload], now=NOW)

    assert outcome.state == "ready", outcome
    assert outcome.proposed_intentions == 1
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ScheduleIntentionRow).order_by(
                        ScheduleIntentionRow.created_at
                    )
                )
            ).scalars()
        )
    created = [row for row in rows if row.title == "周末去杭州看展"]
    assert len(created) == 1
    assert created[0].state == "proposed"
    assert created[0].source_kind == "model"
    # 上游是内部 id：撤回上游时 `_downstream` 才找得到这条派生提议。
    assert created[0].source_ref == intent.intention_id


async def test_outline_work_expires_stale_intentions_before_prompt(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """进 prompt 前先做**零模型**惰性过期：目标日已过的意向不再影响今天。"""
    await _seed()
    long_ago = NOW - timedelta(days=30)
    stale = await _create_intent(
        state="confirmed",
        title="很久以前想去的地方",
        start_day=(long_ago + timedelta(days=3)).date(),
        end_day=(long_ago + timedelta(days=3)).date(),
        now=long_ago,
    )

    outcome, router = await _work(PHASE_DUE, responses=[_valid_outline()], now=NOW)

    assert outcome.state == "ready", outcome
    assert "很久以前想去的地方" not in router.prompts[0]
    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, stale.intention_id)
        assert row is not None
    assert row.state == "expired_unknown", "目标日已过且无证据 = 不知道，不是 missed"


async def test_memory_zone_is_audience_filtered(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """记忆区：owner 的核心记忆/结构化摘要进 prompt，窗口级内容不进。"""
    await _seed()
    # 用**真实**的 owner subject（bootstrap 管理员那一条）：owner 记忆按它归属。
    async with session_scope() as session:
        subject = await owner_subject_id(session)
    assert subject, "测试环境必须有 owner 账号"
    async with session_scope() as session:
        session.add(
            CoreMemoryRow(
                id="core_1",
                subject_id=subject,
                label="称呼",
                text="主人叫我灵",
                priority=10,
                active=True,
            )
        )
        session.add(
            MemoryRow(
                id="mem_ok",
                subject_id=subject,
                kind="assertion",
                text="她喜欢看书",
                origin="user_statement",
                visibility="subject_private",
                slot="like",
                value="看书",
                confidence=0.9,
            )
        )
        session.add(
            MemoryRow(
                id="mem_window",
                subject_id=subject,
                kind="assertion",
                text="某群的秘密",
                origin="user_statement",
                visibility="window_private",
                slot="secret",
                value="某群的秘密",
                confidence=0.9,
            )
        )

    outcome, router = await _work(PHASE_DUE, responses=[_valid_outline()], now=NOW)

    assert outcome.state == "ready", outcome
    prompt = router.prompts[0]
    assert '"memory"' in prompt
    assert "称呼：主人叫我灵" in prompt
    assert "like：看书" in prompt
    assert "某群的秘密" not in prompt, "窗口级记忆不得进按 actor 归并的夜间计划"
    assert "她喜欢看书" not in prompt, "结构化摘要只给 slot：value，不给整段正文"


async def test_late_phase_stays_zero_model_and_still_expires_intents(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """`late` 相位：仍然零模型（连一次模型调用都不许有），过期清理照做。

    设计 §3A 把"过期"定义成确定性状态机；把它放在相位判断之前，`late` 就既能
    保持零模型，又不会让一个已过目标日的意向在醒后继续污染活跃集。
    """
    await _seed()
    long_ago = NOW - timedelta(days=20)
    stale = await _create_intent(
        state="proposed",
        source_kind="model",
        source_ref="outline:stale",
        title="很久以前想去看看",
        start_day=(long_ago + timedelta(days=2)).date(),
        end_day=(long_ago + timedelta(days=2)).date(),
        now=long_ago,
    )
    calls_before = await _model_calls()
    wake = WINDOW_START + timedelta(hours=8, minutes=1)  # 醒点之后 → late

    outcome, router = await _work(PHASE_LATE, responses=[_valid_outline()], now=wake)

    assert outcome.phase == PHASE_LATE, outcome
    assert router.prompts == [], "late 相位结构上不许调模型"
    assert await _model_calls() == calls_before, "零模型分支不许新增预算行"
    async with session_scope() as session:
        row = await session.get(ScheduleIntentionRow, stale.intention_id)
        assert row is not None
    assert row.state == "expired_unknown"


async def test_seam_check_needs_no_model_and_writes_no_facts(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """午夜接缝仍是零模型：接缝校验不读意向、也不写活动/经历。"""
    await _seed()
    await _create_intent(state="confirmed", title="周五去杭州看展")
    outcome, _router = await _work(PHASE_DUE, responses=[_valid_outline()], now=NOW)
    assert outcome.state == "ready", outcome
    calls_before = await _model_calls()

    async with session_scope() as session:
        report = await seam_check(session, PLAN_DAY, actor_id=ACTOR, now=NOW)

    assert report.state in ("unchanged", "no_plan", "annotated"), report
    assert await _model_calls() == calls_before
    _, activities, experiences, _segments_count = await _counts()
    assert activities == 0 and experiences == 0
