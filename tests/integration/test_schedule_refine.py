"""近窗细化（阶段 2）：真 PostgreSQL 上的父段选择、上限与失败降级。

守设计 §3 的可程序化验收：

1. **8 点后 9–11 点有子段、14 点仍无子段**：只细化"最早未细化段"，
   且只在距开始 ≤ 60 分钟时跑；
2. 子段**全落父块内**、兄弟不重叠、父段**保留**（不删不改时间，只翻
   ``is_leaf``）——父段 ``is_leaf`` 翻转与子段插入在同一事务；
3. 每个父段的子段数、最小时长、递归层级、每 3 小时滚动次数都有硬上限；
4. 失败（非法/超时/CAS 失败/空结果）**保留粗段**，同一窗口不重试，
   也**不追补**已经过去的窗口；
5. 细化不写 activity / self_experience：计划不是事实。

时间全部用固定 ``DAY`` + 本地分钟换算，不依赖"现在几点"。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.timeline import (
    REFINE_KIND,
    REFINE_MAX_ATTEMPTS_PER_WINDOW,
    StaleRevisionError,
    _commit_refinement,
    load_coarse_segments,
    load_refine_target,
    local_minute_instant,
    plan_day_window,
    refine_key,
    refine_work,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    BehaviorModelCallRow,
    DayPlanRow,
    OutboxEvent,
    ScheduleSegmentRow,
    SelfExperienceRow,
)
from ling.infra.security import new_id
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
DAY = date(2026, 11, 8)
WINDOW_START, WINDOW_END = plan_day_window(DAY, TZ)


def _at(minute: int) -> datetime:
    """本地墙钟 `minute` 分钟的 UTC 瞬时。"""
    return local_minute_instant(DAY, minute, TZ)


def _iso(minute: int) -> str:
    return _at(minute).isoformat()


# ---------------------------------------------------------------- 假模型


class _StubRouter:
    """按顺序吐出预设文本；记录被调用次数（失败路径也要能被数出来）。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls = 0
        self.prompts: list[str] = []
        self.on_call: object = None  # 可在调用期间做点别的事（模拟并发提交）

    async def stream(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls += 1
        if callable(self.on_call):
            await self.on_call()  # type: ignore[operator]
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


def _children(
    parent_id: str,
    *,
    spans: list[tuple[int, int]],
    layer: str = "hour",
    kind: str = "work",
    title: str = "干活",
) -> str:
    return json.dumps(
        {
            "parent_id": parent_id,
            "children": [
                {
                    "layer": layer,
                    "start_at": _iso(start),
                    "end_at": _iso(end),
                    "title": f"{title}{index + 1}",
                    "kind": kind,
                    "place_id": None,
                }
                for index, (start, end) in enumerate(spans)
            ],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- 播种


async def _seed_routine() -> None:
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


async def _insert_segment(
    plan_id: str,
    *,
    start_at: datetime,
    end_at: datetime,
    title: str,
    kind: str = "work",
    layer: str = "block",
    is_leaf: bool = True,
    anchor_kind: str = "",
    revision: int = 1,
) -> str:
    segment_id = new_id("seg")
    async with session_scope() as session:
        session.add(
            ScheduleSegmentRow(
                id=segment_id,
                plan_id=plan_id,
                actor_id=ACTOR,
                parent_id=None,
                parent_key="",
                layer=layer,
                kind=kind,
                title=title,
                start_at=start_at,
                end_at=end_at,
                place_id=None,
                commitment_level="confirmed" if anchor_kind else "tentative",
                status="current",
                is_leaf=is_leaf,
                anchor_kind=anchor_kind,
                source="baseline" if anchor_kind else "model",
                revision_created=revision,
                revision_ended=None,
                source_event_ids=[],
                visibility=None,
            )
        )
    return segment_id


async def _seed_day(
    *,
    work_leaf: bool = False,
    reading_leaf: bool = True,
) -> dict[str, str]:
    """一天的计划：两段睡眠锚点 + 09:00–11:00 工作（粗）+ 14:00–16:00 读书。

    ``work_leaf=False`` 是任务书里"粗段 = ``is_leaf=False``"的形状；
    ``reading_leaf=True`` 是大纲产出的普通大块（``is_leaf=True`` 且没有子段）。
    两种都属于"还没细化"，代码必须都认——否则普通一天永远没有候选。
    """
    plan_id = new_id("dayplan")
    async with session_scope() as session:
        session.add(
            DayPlanRow(
                id=plan_id,
                actor_id=ACTOR,
                plan_day=DAY,
                timezone=TZ,
                timezone_source="preference",
                timezone_revision=0,
                routine_revision=1,
                window_start_at=WINDOW_START,
                window_end_at=WINDOW_END,
                revision=1,
                state="ready",
                generation_source="model",
                last_error_code=None,
                created_at=WINDOW_START,
                updated_at=WINDOW_START,
            )
        )
    ids = {
        "plan": plan_id,
        "work": await _insert_segment(
            plan_id,
            start_at=_at(9 * 60),
            end_at=_at(11 * 60),
            title="工作",
            is_leaf=work_leaf,
        ),
        "reading": await _insert_segment(
            plan_id,
            start_at=_at(14 * 60),
            end_at=_at(16 * 60),
            title="读书",
            kind="reading",
            is_leaf=reading_leaf,
        ),
    }
    for key, start, end in (
        ("sleep_night", 0, 8 * 60),
        ("sleep_evening", 23 * 60, 24 * 60),
    ):
        ids[key] = await _insert_segment(
            plan_id,
            start_at=_at(start),
            end_at=_at(end),
            title="睡觉",
            kind="sleep",
            is_leaf=False,
            anchor_kind="sleep",
        )
    return ids


async def _plan_row() -> DayPlanRow:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DayPlanRow).where(DayPlanRow.actor_id == ACTOR)
            )
        ).scalar_one()
        return row


async def _children_of(parent_id: str) -> list[ScheduleSegmentRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ScheduleSegmentRow)
                    .where(ScheduleSegmentRow.parent_id == parent_id)
                    .order_by(ScheduleSegmentRow.start_at)
                )
            ).scalars()
        )


async def _count(model) -> int:  # noqa: ANN001
    async with session_scope() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _calls() -> list[BehaviorModelCallRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(BehaviorModelCallRow).order_by(
                        BehaviorModelCallRow.created_at
                    )
                )
            ).scalars()
        )


async def _refine(
    now: datetime,
    *,
    responses: list[str] | None = None,
    with_model: bool = True,
    router: _StubRouter | None = None,
):  # noqa: ANN202
    stub = router or _StubRouter(responses)
    config = _StubConfig(_config(with_extract=with_model))
    async with session_scope() as session:
        outcome = await refine_work(
            session,
            actor_id=ACTOR,
            now=now,
            router=stub if with_model else None,
            config_reader=config if with_model else None,
            lease_owner="test",
        )
    return outcome, stub


# ---------------------------------------------------------------- 触发与选择


async def test_eight_oclock_refines_morning_block_only(web_env: TestEnv) -> None:  # noqa: ARG001
    """8 点：9–11 点被细化成小时子段，14 点仍无子段。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, router = await _refine(
        _at(8 * 60), responses=[_children(ids["work"], spans=[(9 * 60, 10 * 60), (10 * 60, 11 * 60)])]
    )

    assert outcome.state == "refined", outcome
    assert outcome.calls == 1 and router.calls == 1
    assert outcome.parent_id == ids["work"]
    assert outcome.children == 2
    assert outcome.window_start == _at(9 * 60)
    assert outcome.window_end == _at(11 * 60)
    assert outcome.revision_before == 1 and outcome.revision_after == 2

    # 9–11 点有子段：全落父块内、兄弟不重叠、层是 hour
    children = await _children_of(ids["work"])
    assert len(children) == 2
    assert [item.layer for item in children] == ["hour", "hour"]
    assert all(item.is_leaf for item in children), "子段必须是叶段（可启动活动）"
    assert children[0].start_at == _at(9 * 60)
    assert children[-1].end_at == _at(11 * 60)
    assert children[0].end_at <= children[1].start_at, "兄弟段重叠了"
    assert all(item.revision_created == 2 for item in children)

    # 父段保留，只翻 is_leaf
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
    assert parent is not None and str(parent.status) == "current"
    assert parent.start_at == _at(9 * 60) and parent.end_at == _at(11 * 60)
    assert parent.is_leaf is False

    # 14 点仍然没有子段
    assert await _children_of(ids["reading"]) == []

    # 计划不是事实：细化不写活动/经历
    assert await _count(ActivityRow) == 0
    assert await _count(SelfExperienceRow) == 0


async def test_only_earliest_unrefined_segment_is_selected(web_env: TestEnv) -> None:  # noqa: ARG001
    """8 点只看最早未细化段；14 点那段还早，不跑。"""
    await _seed_routine()
    ids = await _seed_day(work_leaf=True, reading_leaf=True)
    async with session_scope() as session:
        target = await load_refine_target(session, actor_id=ACTOR, now=_at(8 * 60))
    assert target is not None
    assert target.parent.segment_id == ids["work"]

    # 把工作段挪到中午（12:00–14:00）：最早的一段离 8 点还有 4 小时，不该跑
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
        parent.start_at = _at(12 * 60)
        parent.end_at = _at(14 * 60)
        parent.is_leaf = False
    outcome, router = await _refine(_at(8 * 60), responses=[])
    assert outcome.state == "no_target", outcome
    assert outcome.calls == 0 and router.calls == 0


async def test_refine_skips_when_earliest_is_far_away(web_env: TestEnv) -> None:  # noqa: ARG001
    """7 点：最早未细化段 9 点开始，还差 120 分钟 → 0 次调用。"""
    await _seed_routine()
    await _seed_day()
    outcome, router = await _refine(_at(7 * 60), responses=[])
    assert outcome.state == "no_target"
    assert router.calls == 0
    assert await _calls() == [], "没有目标却写了预算行"


async def test_window_never_backfills_past(web_env: TestEnv) -> None:  # noqa: ARG001
    """10:30 细化只剩最后 30 分钟；10:31 起剩余不足 30 分钟 → 不追补。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        target = await load_refine_target(session, actor_id=ACTOR, now=_at(10 * 60 + 30))
    assert target is not None
    assert target.window_start == _at(10 * 60 + 30), "窗口起点必须取 now，而不是父段起点"
    assert target.window_end == _at(11 * 60)

    async with session_scope() as session:
        late = await load_refine_target(session, actor_id=ACTOR, now=_at(10 * 60 + 31))
    assert late is None, "剩余窗口不足 30 分钟时不该再细化"
    outcome, router = await _refine(_at(10 * 60 + 31), responses=[])
    assert outcome.state == "no_target"
    assert router.calls == 0
    assert await _children_of(ids["work"]) == []


async def test_coarse_segments_include_leaf_blocks_without_children(
    web_env: TestEnv,  # noqa: ARG001
) -> None:
    """大纲产出的普通大块（is_leaf=True、没有子段）也算"未细化"。"""
    await _seed_routine()
    ids = await _seed_day(work_leaf=True, reading_leaf=True)
    async with session_scope() as session:
        plan = await _plan_row()
        coarse = await load_coarse_segments(
            session, plan_id=str(plan.id), now=_at(8 * 60)
        )
    assert [item.segment_id for item in coarse] == [ids["work"], ids["reading"]]

    outcome, _router = await _refine(
        _at(8 * 60),
        responses=[_children(ids["work"], spans=[(9 * 60, 10 * 60), (10 * 60, 11 * 60)])],
    )
    assert outcome.state == "refined"
    async with session_scope() as session:
        coarse_after = await load_coarse_segments(
            session, plan_id=str(plan.id), now=_at(8 * 60)
        )
    remaining = {item.segment_id for item in coarse_after}
    assert ids["work"] not in remaining, "细化过的父段不该再是候选"
    assert ids["reading"] in remaining, "14 点那段还没细化，必须还在候选里"
    # 子段（hour）自己可以被再细化一次成 minute：递归层级上限是 2 层。
    assert {item.layer for item in coarse_after if item.segment_id not in (ids["reading"],)} <= {
        "hour"
    }


# ---------------------------------------------------------------- 幂等与额度


async def test_same_window_is_not_retried(web_env: TestEnv) -> None:  # noqa: ARG001
    """同一窗口只尝试一次：失败后再跑同一时刻不会第二次调模型。"""
    await _seed_routine()
    ids = await _seed_day()
    first, router_first = await _refine(_at(8 * 60), responses=["这不是 JSON"])
    assert first.state == "failed"
    assert first.reason_code == "refine_invalid"
    assert first.calls == 1 and router_first.calls == 1
    assert await _children_of(ids["work"]) == []

    second, router_second = await _refine(_at(8 * 60), responses=[_children(ids["work"], spans=[(9 * 60, 10 * 60)])])
    assert second.state == "unchanged", second
    assert second.reason_code == "window_attempted"
    assert router_second.calls == 0, "同一窗口重试了模型（白烧调用）"

    calls = await _calls()
    assert len(calls) == REFINE_MAX_ATTEMPTS_PER_WINDOW
    assert calls[0].kind == REFINE_KIND
    assert calls[0].state == "failed"
    assert calls[0].idempotency_key == refine_key(str((await _plan_row()).id), 1, _at(9 * 60))


async def test_rolling_three_hour_limit(web_env: TestEnv) -> None:  # noqa: ARG001
    """每 3 小时最多 1 次：9 点想细化 10 点那段会被滚动额度挡住，11 点才放行。"""
    await _seed_routine()
    ids = await _seed_day(work_leaf=True)
    # 先把 9–11 拆成两个粗块，使 9 点时"下一段"（10 点）成为最早未细化段
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
        parent.end_at = _at(10 * 60)
    second_block = await _insert_segment(
        ids["plan"],
        start_at=_at(10 * 60),
        end_at=_at(12 * 60),
        title="接着干活",
        is_leaf=False,
    )

    first, _router = await _refine(
        _at(8 * 60),
        responses=[_children(ids["work"], spans=[(9 * 60, 10 * 60)])],
    )
    assert first.state == "refined", first

    blocked, router_blocked = await _refine(
        _at(9 * 60 + 30),
        responses=[_children(second_block, spans=[(10 * 60, 11 * 60)])],
    )
    assert blocked.state == "blocked", blocked
    assert blocked.reason_code == "rolling_limit"
    assert router_blocked.calls == 0
    assert await _children_of(second_block) == []

    # 距上一次细化满 3 小时才放行
    allowed, _router2 = await _refine(
        _at(11 * 60 + 5),
        responses=[_children(second_block, spans=[(11 * 60 + 5, 12 * 60)])],
    )
    assert allowed.state == "refined", allowed
    assert len(await _children_of(second_block)) == 1


# ---------------------------------------------------------------- 输出约束


async def test_sibling_overlap_is_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """兄弟段重叠 → 整份拒收，粗段保留。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, _router = await _refine(
        _at(8 * 60),
        responses=[
            _children(ids["work"], spans=[(9 * 60, 10 * 60 + 30), (10 * 60, 11 * 60)])
        ],
    )
    assert outcome.state == "failed"
    assert outcome.reason_code == "refine_invalid"
    assert await _children_of(ids["work"]) == []
    plan = await _plan_row()
    assert int(plan.revision) == 1, "非法输出推进了计划版本"
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
    assert parent.is_leaf is False, "失败的细化不该翻父段的 is_leaf"


async def test_child_outside_parent_is_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """子段越出父块/未来窗口 → 拒收。"""
    await _seed_routine()
    ids = await _seed_day()
    # 父块是 09:00–11:00；这里给一段 11:00–12:00
    outcome, _router = await _refine(
        _at(8 * 60),
        responses=[_children(ids["work"], spans=[(11 * 60, 12 * 60)])],
    )
    assert outcome.state == "failed"
    assert outcome.reason_code == "refine_invalid"
    assert await _children_of(ids["work"]) == []


async def test_too_many_children_is_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """每个父段最多 4 个子段（硬上限）。"""
    await _seed_routine()
    ids = await _seed_day()
    spans = [(8 * 60 + 60 + i * 20, 8 * 60 + 75 + i * 20) for i in range(5)]
    outcome, _router = await _refine(
        _at(7 * 60 + 59), responses=[_children(ids["work"], spans=spans)]
    )
    # 7:59 起最早段还差 61 分钟：先确认它没被当成"到期"
    assert outcome.state == "no_target", outcome

    async with session_scope() as session:
        target = await load_refine_target(session, actor_id=ACTOR, now=_at(8 * 60))
    assert target is not None
    bad_spans = [(9 * 60 + i * 20, 9 * 60 + 15 + i * 20) for i in range(5)]
    outcome2, _router2 = await _refine(
        _at(8 * 60), responses=[_children(ids["work"], spans=bad_spans)]
    )
    assert outcome2.state == "failed"
    assert outcome2.reason_code == "refine_invalid"
    assert await _children_of(ids["work"]) == []


async def test_minor_layer_and_min_duration_are_enforced(web_env: TestEnv) -> None:  # noqa: ARG001
    """layer 必须是父层下一层；子段至少 5 分钟。"""
    await _seed_routine()
    ids = await _seed_day()
    wrong_layer, _router = await _refine(
        _at(8 * 60),
        responses=[
            _children(ids["work"], spans=[(9 * 60, 10 * 60)], layer="minute")
        ],
    )
    assert wrong_layer.state == "failed"

    # 换一个窗口重试（同一窗口不重试，所以用 9:30 的新窗口 + 滚动额度）：
    # 直接把刚才那条预算行删掉，模拟"另一个窗口"。
    async with session_scope() as session:
        rows = list((await session.execute(select(BehaviorModelCallRow))).scalars())
        for row in rows:
            await session.delete(row)
    too_short, _router2 = await _refine(
        _at(8 * 60),
        responses=[_children(ids["work"], spans=[(9 * 60, 9 * 60 + 4)])],
    )
    assert too_short.state == "failed"
    assert too_short.reason_code == "refine_invalid"
    assert await _children_of(ids["work"]) == []


async def test_empty_children_is_a_failure_not_a_noop(web_env: TestEnv) -> None:  # noqa: ARG001
    """空结果按失败处理：不重试、不写段。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, _router = await _refine(
        _at(8 * 60), responses=[json.dumps({"parent_id": ids["work"], "children": []})]
    )
    assert outcome.state == "failed"
    assert outcome.reason_code == "refine_empty"
    assert await _children_of(ids["work"]) == []


async def test_wrong_parent_id_is_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """模型只能细化本次给出的父段。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, _router = await _refine(
        _at(8 * 60),
        responses=[_children(ids["reading"], spans=[(9 * 60, 10 * 60)])],
    )
    assert outcome.state == "failed"
    assert outcome.reason_code == "refine_invalid"
    assert await _children_of(ids["work"]) == []


async def test_claim_marker_in_child_title_is_rejected(web_env: TestEnv) -> None:  # noqa: ARG001
    """子段标题不许把计划写成完成/承诺。"""
    await _seed_routine()
    ids = await _seed_day()
    payload = json.loads(_children(ids["work"], spans=[(9 * 60, 10 * 60)]))
    payload["children"][0]["title"] = "已经写完了"
    outcome, _router = await _refine(
        _at(8 * 60), responses=[json.dumps(payload, ensure_ascii=False)]
    )
    assert outcome.state == "failed"
    assert await _children_of(ids["work"]) == []


# ---------------------------------------------------------------- 失败降级


async def test_model_unavailable_costs_zero_calls(web_env: TestEnv) -> None:  # noqa: ARG001
    """没有可用模型：0 次调用、不占预算行，粗段保持。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, _router = await _refine(_at(8 * 60), with_model=False)
    assert outcome.state == "blocked"
    assert outcome.reason_code == "model_unavailable"
    assert outcome.calls == 0
    assert await _calls() == []
    assert await _children_of(ids["work"]) == []


async def test_model_timeout_keeps_coarse_segment(web_env: TestEnv) -> None:  # noqa: ARG001
    """模型超时：保留粗段，记账为失败，不重试。"""

    class _TimeoutRouter:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            self.calls += 1
            raise TimeoutError("超时")
            yield  # pragma: no cover - 生成器语法占位

    router = _TimeoutRouter()
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        outcome = await refine_work(
            session,
            actor_id=ACTOR,
            now=_at(8 * 60),
            router=router,
            config_reader=_StubConfig(_config()),
            lease_owner="test",
        )
    assert outcome.state == "failed"
    assert outcome.calls == 1
    assert await _children_of(ids["work"]) == []
    calls = await _calls()
    assert len(calls) == 1 and calls[0].state == "failed"


async def test_cas_failure_discards_response(web_env: TestEnv) -> None:  # noqa: ARG001
    """模型调用期间别人先提交了一版 → CAS 失败，整份细化丢弃。"""
    await _seed_routine()
    ids = await _seed_day()
    plan = await _plan_row()

    async def _bump_revision() -> None:
        # 模拟并发：模型还在跑，另一个 worker 已经把这一天推进到 revision 2
        async with session_scope() as session:
            row = await session.get(DayPlanRow, str(plan.id))
            row.revision = int(row.revision) + 1

    router = _StubRouter(
        [_children(ids["work"], spans=[(9 * 60, 10 * 60), (10 * 60, 11 * 60)])]
    )
    router.on_call = _bump_revision
    async with session_scope() as session:
        outcome = await refine_work(
            session,
            actor_id=ACTOR,
            now=_at(8 * 60),
            router=router,
            config_reader=_StubConfig(_config()),
            lease_owner="test",
        )
    assert outcome.state == "busy", outcome
    assert outcome.reason_code == "stale_revision"
    assert await _children_of(ids["work"]) == []
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
    assert parent.is_leaf is False, "CAS 失败却留下了父段翻转"


async def test_commit_is_atomic_with_parent_flip(web_env: TestEnv) -> None:  # noqa: ARG001
    """父段 is_leaf 翻转与子段插入同一事务：父段被别人改掉就整份回滚。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        target = await load_refine_target(session, actor_id=ACTOR, now=_at(8 * 60))
    assert target is not None
    # 父段被改版（superseded）：提交必须失败，且不写任何子段
    async with session_scope() as session:
        parent = await session.get(ScheduleSegmentRow, ids["work"])
        parent.status = "superseded"
        parent.revision_ended = 2
    from ling.domains.agenda.timeline import RefineChild

    async with session_scope() as session:
        with pytest.raises(StaleRevisionError):
            await _commit_refinement(
                session,
                target=target,
                children=[
                    RefineChild(
                        layer="hour",
                        kind="work",
                        title="干活",
                        start_at=_at(9 * 60),
                        end_at=_at(10 * 60),
                    )
                ],
                now=_at(8 * 60),
            )
    assert await _children_of(ids["work"]) == []
    row = await _plan_row()
    assert int(row.revision) == 1


# ---------------------------------------------------------------- 调度接线


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """把 worker 的 `utcnow` 固定住，并钉住规范时区（不读开发机 shell）。"""
    from ling import worker

    monkeypatch.setattr(worker, "utcnow", lambda: moment)
    monkeypatch.setenv("LING_USER_TIMEZONE", TZ)


async def _refine_events() -> list[OutboxEvent]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "schedule.refine")
                )
            ).scalars()
        )


async def test_scan_enqueues_refine_once_per_window(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """`life.schedule` 只投一次细化作业，幂等键 = refine:{plan}:{rev}:{window}。"""
    from ling import worker

    await _seed_routine()
    ids = await _seed_day()
    _freeze(monkeypatch, _at(8 * 60))
    plan = await _plan_row()

    await worker.handle_life_schedule({})
    events = await _refine_events()
    assert len(events) == 1, f"细化作业投递了 {len(events)} 次"
    assert events[0].id == refine_key(str(plan.id), 1, _at(9 * 60))
    assert events[0].payload["parent_id"] == ids["work"]

    # 重复扫描（下一个 60 秒）不重复投递：窗口起点按分钟取整，键稳定。
    monkeypatch.setattr(worker, "utcnow", lambda: _at(8 * 60 + 1))
    await worker.handle_life_schedule({})
    assert len(await _refine_events()) == 1


async def test_scan_enqueues_nothing_when_no_coarse_nearby(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """最近一段还早 → 扫描不投递细化作业（零模型、零事件）。"""
    from ling import worker

    await _seed_routine()
    await _seed_day()
    _freeze(monkeypatch, _at(6 * 60))
    await worker.handle_life_schedule({})
    assert await _refine_events() == []


async def test_handler_refines_end_to_end(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """`schedule.refine` handler：模型调用在 outbox 事务外，子段落库。"""
    from ling import worker

    await _seed_routine()
    ids = await _seed_day()
    _freeze(monkeypatch, _at(8 * 60))
    plan = await _plan_row()
    key = refine_key(str(plan.id), 1, _at(9 * 60))
    router = _StubRouter(
        [_children(ids["work"], spans=[(9 * 60, 10 * 60), (10 * 60, 11 * 60)])]
    )
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_config_reader", lambda _session: _StubConfig(_config()))

    await worker.handle_schedule_refine(
        {
            "actor_id": ACTOR,
            "plan_day": DAY.isoformat(),
            "parent_id": ids["work"],
            "idempotency_key": key,
        }
    )
    assert router.calls == 1
    assert len(await _children_of(ids["work"])) == 2
    row = await _plan_row()
    assert int(row.revision) == 2


async def test_handler_discards_stale_event(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    """扫描时的目标已经变了（版本/窗口不同）→ 丢弃旧事件，不调模型。"""
    from ling import worker

    await _seed_routine()
    ids = await _seed_day()
    _freeze(monkeypatch, _at(8 * 60))
    router = _StubRouter(
        [_children(ids["work"], spans=[(9 * 60, 10 * 60), (10 * 60, 11 * 60)])]
    )
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "_config_reader", lambda _session: _StubConfig(_config()))

    await worker.handle_schedule_refine(
        {
            "actor_id": ACTOR,
            "plan_day": DAY.isoformat(),
            "parent_id": ids["work"],
            "idempotency_key": "refine:dayplan_stale:1:20260101T0000Z",
        }
    )
    assert router.calls == 0
    assert await _children_of(ids["work"]) == []
