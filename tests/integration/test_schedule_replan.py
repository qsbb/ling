"""聊天临时提议 → 硬门 → 局部重排（阶段 3）：真 PostgreSQL 上的验收。

守设计 §4 的可程序化验收（任务书 P6 的 7 条）：

1. **过去段 / confirmed 约定 / `CommitmentRow` / running 活动在重排前后逐字段
   不变**（拿 dict + sha256 比较，不是"看起来一样"）；
2. `plan_id` 不变、`revision+1`、旧段 `superseded` 且**仍可查**（不物理删）；
3. 同一 `turn_id` 只一次决定（第二次返回原决定、不再调模型）；
   窗口每小时 ≤1 次、全 actor 每日 ≤2 次；预算满 → `defer` **且记录**；
4. 睡眠窗内提议 → 硬门拒绝且**不调模型**；与 confirmed 承诺冲突 → 拒绝；
5. 接受提议后：区间外段不动、区间内换段、**不新增 activity / self_experience**；
6. 跨午夜提议：两份 `day_plans` 的原子性、按 `plan_day` 升序加锁；
   其中一日不可改 → 整单回滚并说明；
7. 识别层：问句/否定/假设不触发重排；关键词命中但硬门不过不调模型。

时间一律用固定 ``DAY=2026-11-08`` + 本地分钟换算成 UTC，不依赖"现在几点"。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.replan import replan_work
from ling.domains.agenda.timeline import (
    ScopedDraft,
    ScopeEdit,
    ScopeError,
    SegmentDraft,
    StaleRevisionError,
    load_segments_at,
    local_minute_instant,
    plan_day_window,
    replace_scope,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    BehaviorModelCallRow,
    CommitmentRow,
    DayPlanRow,
    OutboxEvent,
    PresenceState,
    ScheduleChangeRequestRow,
    ScheduleIntentionRow,
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
NEXT_DAY = DAY + timedelta(days=1)
WINDOW_START, WINDOW_END = plan_day_window(DAY, TZ)

# 段在数据库里的**全部**列（比较"逐字段不变"用）。顺序固定，便于失败时看 diff。
SEGMENT_COLUMNS = (
    "id",
    "plan_id",
    "actor_id",
    "parent_id",
    "parent_key",
    "layer",
    "kind",
    "title",
    "start_at",
    "end_at",
    "place_id",
    "commitment_level",
    "status",
    "is_leaf",
    "anchor_kind",
    "source",
    "revision_created",
    "revision_ended",
    "source_event_ids",
    "intention_id",
    "preparation_for_day",
    "visibility",
    "created_at",
    "updated_at",
)


def _at(minute: int, *, day: date = DAY) -> datetime:
    """本地墙钟 `minute` 分钟的 UTC 瞬时。"""
    return local_minute_instant(day, minute, TZ)


def _segment_snapshot(row: ScheduleSegmentRow) -> dict[str, object]:
    payload: dict[str, object] = {}
    for column in SEGMENT_COLUMNS:
        value = getattr(row, column)
        if isinstance(value, (datetime, date)):
            payload[column] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            payload[column] = list(value)
        else:
            payload[column] = value
    return payload


def _digest(snapshot: dict[str, object]) -> str:
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _snapshots(plan_id: str) -> dict[str, dict[str, object]]:
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ScheduleSegmentRow).where(ScheduleSegmentRow.plan_id == plan_id)
                )
            ).scalars()
        )
    return {str(row.id): _segment_snapshot(row) for row in rows}


async def _seed_routine(*, wake: int = 8 * 60, rest: int = 23 * 60) -> None:
    async with session_scope() as session:
        session.add(
            ActorRoutineRow(
                actor_id=ACTOR,
                timezone=TZ,
                wake_minute=wake,
                rest_minute=rest,
                source="configured",
                revision=1,
            )
        )


async def _seed_plan(
    *, plan_day: date = DAY, revision: int = 1, state: str = "ready"
) -> str:
    start, end = plan_day_window(plan_day, TZ)
    plan_id = new_id("dayplan")
    async with session_scope() as session:
        session.add(
            DayPlanRow(
                id=plan_id,
                actor_id=ACTOR,
                plan_day=plan_day,
                timezone=TZ,
                timezone_source="preference",
                timezone_revision=0,
                routine_revision=1,
                window_start_at=start,
                window_end_at=end,
                revision=revision,
                state=state,
                generation_source="model",
                last_error_code=None,
                created_at=start,
                updated_at=start,
            )
        )
    return plan_id


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
    commitment_level: str = "tentative",
    parent_id: str | None = None,
    revision: int = 1,
    source_event_ids: list[str] | None = None,
) -> str:
    segment_id = new_id("seg")
    async with session_scope() as session:
        session.add(
            ScheduleSegmentRow(
                id=segment_id,
                plan_id=plan_id,
                actor_id=ACTOR,
                parent_id=parent_id,
                parent_key=parent_id or "",
                layer=layer,
                kind=kind,
                title=title,
                start_at=start_at,
                end_at=end_at,
                place_id=None,
                commitment_level=commitment_level,
                status="current",
                is_leaf=is_leaf,
                anchor_kind=anchor_kind,
                source="baseline" if anchor_kind else "model",
                revision_created=revision,
                revision_ended=None,
                source_event_ids=list(source_event_ids or []),
                visibility=None,
            )
        )
    return segment_id


async def _seed_day(*, plan_day: date = DAY, revision: int = 1) -> dict[str, str]:
    """晚上 18:00 的现场：睡眠锚点 + 晚饭 + 19:00 的工作 + 21:00 的读书。"""
    plan_id = await _seed_plan(plan_day=plan_day, revision=revision)
    ids: dict[str, str] = {"plan": plan_id}
    ids["sleep_night"] = await _insert_segment(
        plan_id,
        start_at=_at(0, day=plan_day),
        end_at=_at(8 * 60, day=plan_day),
        title="睡觉",
        kind="sleep",
        is_leaf=False,
        anchor_kind="sleep",
        revision=revision,
    )
    ids["dinner"] = await _insert_segment(
        plan_id,
        start_at=_at(18 * 60, day=plan_day),
        end_at=_at(19 * 60, day=plan_day),
        title="吃饭",
        kind="meal",
        revision=revision,
    )
    ids["work"] = await _insert_segment(
        plan_id,
        start_at=_at(19 * 60, day=plan_day),
        end_at=_at(21 * 60, day=plan_day),
        title="赶稿",
        revision=revision,
    )
    ids["reading"] = await _insert_segment(
        plan_id,
        start_at=_at(21 * 60, day=plan_day),
        end_at=_at(22 * 60, day=plan_day),
        title="读书",
        kind="reading",
        revision=revision,
    )
    ids["sleep_evening"] = await _insert_segment(
        plan_id,
        start_at=_at(23 * 60, day=plan_day),
        end_at=_at(24 * 60, day=plan_day),
        title="睡觉",
        kind="sleep",
        is_leaf=False,
        anchor_kind="sleep",
        revision=revision,
    )
    return ids


async def _plan_row(plan_id: str) -> DayPlanRow:
    async with session_scope() as session:
        row = await session.get(DayPlanRow, plan_id)
        assert row is not None
        return row


def _draft(
    *,
    start: datetime,
    end: datetime,
    title: str = "看电影",
    kind: str = "leisure",
    layer: str = "block",
    is_leaf: bool = True,
    parent_index: int | None = None,
) -> ScopedDraft:
    return ScopedDraft(
        draft=SegmentDraft(
            kind=kind,
            title=title,
            start_at=start,
            end_at=end,
            place_id=None,
            layer=layer,
            is_leaf=is_leaf,
            source="model",
        ),
        parent_index=parent_index,
    )


# ---------------------------------------------------------------- 1 / 2 / 5


async def test_scope_replacement_keeps_outside_segments_byte_identical(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """只换 [19:00, 21:00) 里的段：区间外逐字段不变，区间内换段，revision+1。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])

    async with session_scope() as session:
        row = await session.get(DayPlanRow, ids["plan"])
        assert row is not None
        result = await replace_scope(
            session,
            ScopeEdit(
                row=row,
                scope_start=_at(19 * 60),
                scope_end=_at(21 * 60),
                drafts=(
                    _draft(
                        start=_at(19 * 60),
                        end=_at(21 * 60),
                        title="出门看电影",
                        kind="leisure",
                    ),
                ),
                source_ids=("turn:turn-1",),
            ),
            now=_at(18 * 60 + 30),
        )

    # 计划头：plan_id 不变、revision+1
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 2
    assert result.plan_id == ids["plan"]
    assert result.revision_before == 1 and result.revision_after == 2
    assert result.superseded_ids == (ids["work"],)
    assert len(result.created_ids) == 1

    after = await _snapshots(ids["plan"])
    # 只多了这次新写的段：旧段一个都没少（不物理删）
    assert set(before) <= set(after)
    assert set(after) - set(before) == set(result.created_ids)
    # 区间外的每一个段：逐字段完全相同（含 updated_at / source_event_ids / id）
    touched = set(result.superseded_ids) | set(result.created_ids)
    for segment_id, snapshot in before.items():
        if segment_id in touched:
            continue
        assert _digest(after[segment_id]) == _digest(snapshot), segment_id

    # 旧段还在（不物理删）、状态 superseded、revision_ended = 新版本
    async with session_scope() as session:
        old = await session.get(ScheduleSegmentRow, ids["work"])
    assert old is not None
    assert old.status == "superseded"
    assert old.revision_ended == 2
    assert "reason:replan_applied" in list(old.source_event_ids)
    assert "turn:turn-1" in list(old.source_event_ids)

    # 新段落库：同一 plan、revision_created=2、在作用域内
    async with session_scope() as session:
        created = await load_segments_at(session, plan_id=ids["plan"], revision=2)
    assert [item.title for item in created if item.id == result.created_ids[0]] == [
        "出门看电影"
    ]

    # 旧版本仍然可读（"旧段可查"）：18:00 的吃饭 + 21:00 的读书都在
    async with session_scope() as session:
        at_v1 = await load_segments_at(session, plan_id=ids["plan"], revision=1)
    assert {str(item.title) for item in at_v1} >= {"吃饭", "赶稿", "读书"}

    # 计划不是事实：接受一次重排不写 activity / self_experience
    async with session_scope() as session:
        activities = int(
            (await session.execute(select(func.count()).select_from(ActivityRow))).scalar_one()
        )
        experiences = int(
            (
                await session.execute(select(func.count()).select_from(SelfExperienceRow))
            ).scalar_one()
        )
    assert activities == 0
    assert experiences == 0


async def test_scope_replacement_never_touches_started_or_confirmed_segments(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """已开始 / confirmed / 睡眠锚点在作用域内也**不动**（只被冻结，不进 supersede）。"""
    await _seed_routine()
    ids = await _seed_day()
    confirmed = await _insert_segment(
        ids["plan"],
        start_at=_at(19 * 60 + 30),
        end_at=_at(20 * 60),
        title="和她的约定",
        commitment_level="confirmed",
    )
    before = await _snapshots(ids["plan"])

    async with session_scope() as session:
        row = await session.get(DayPlanRow, ids["plan"])
        assert row is not None
        with pytest.raises(ScopeError) as caught:
            await replace_scope(
                session,
                ScopeEdit(
                    row=row,
                    scope_start=_at(19 * 60),
                    scope_end=_at(21 * 60),
                    drafts=(
                        _draft(start=_at(19 * 60), end=_at(21 * 60), title="看电影"),
                    ),
                    source_ids=("turn:turn-2",),
                ),
                now=_at(18 * 60 + 30),
            )
        assert getattr(caught.value, "reason_code", "") == "segment_conflict"

    after = await _snapshots(ids["plan"])
    assert {key: _digest(value) for key, value in after.items()} == {
        key: _digest(value) for key, value in before.items()
    }
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1, "被拒绝的修订不能抬版本号"
    assert confirmed in after


async def test_started_segments_survive_replacement(web_env: TestEnv) -> None:  # noqa: ARG001
    """已经开始的段是历史：作用域覆盖它也不 supersede。"""
    await _seed_routine()
    ids = await _seed_day()
    # 18:00–19:00 的晚饭在 18:20 已经开始
    async with session_scope() as session:
        row = await session.get(DayPlanRow, ids["plan"])
        assert row is not None
        result = await replace_scope(
            session,
            ScopeEdit(
                row=row,
                scope_start=_at(18 * 60),
                scope_end=_at(22 * 60),
                drafts=(
                    _draft(start=_at(20 * 60), end=_at(21 * 60), title="看电影"),
                ),
                source_ids=("turn:turn-3",),
            ),
            now=_at(18 * 60 + 20),
        )
    assert ids["dinner"] in result.frozen_ids
    assert ids["dinner"] not in result.superseded_ids
    assert set(result.superseded_ids) == {ids["work"], ids["reading"]}
    async with session_scope() as session:
        dinner = await session.get(ScheduleSegmentRow, ids["dinner"])
    assert dinner is not None and dinner.status == "current"
    assert dinner.revision_ended is None


# ---------------------------------------------------------------- 假模型


class _StubRouter:
    """按顺序吐出预设文本；记录被调用次数（"不调模型"的断言靠它）。"""

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


def _reply(
    *, decision: str = "accept", reason: str = "feels_right", spans: list[tuple[int, int]] | None = None,
    day_spans: list[tuple[date, int, int]] | None = None,
    kind: str = "leisure", title: str = "看电影",
) -> str:
    """模型输出（严格 JSON）。时间给本地分钟，转成 UTC ISO。"""
    segments: list[dict[str, object]] = []
    for start, end in spans or []:
        segments.append(
            {
                "kind": kind,
                "title": title,
                "start_at": _at(start).isoformat(),
                "end_at": _at(end).isoformat(),
                "place_id": None,
            }
        )
    for day, start, end in day_spans or []:
        segments.append(
            {
                "kind": kind,
                "title": title,
                "start_at": _at(start, day=day).isoformat(),
                "end_at": _at(end, day=day).isoformat(),
                "place_id": None,
            }
        )
    return json.dumps(
        {"decision": decision, "reason_code": reason, "proposed_segments": segments},
        ensure_ascii=False,
    )


async def _replan(
    now: datetime,
    text: str,
    *,
    turn_id: str,
    window_id: str | None = "win-ling",
    responses: list[str] | None = None,
    with_model: bool = True,
    with_extract: bool = True,
    router: _StubRouter | None = None,
):  # noqa: ANN202
    stub = router or _StubRouter(responses)
    config = _StubConfig(_config(with_extract=with_extract))
    async with session_scope() as session:
        outcome = await replan_work(
            session,
            actor_id=ACTOR,
            turn_id=turn_id,
            user_text=text,
            window_id=window_id,
            now=now,
            router=stub if with_model else None,
            config_reader=config if with_model else None,
            lease_owner="test",
        )
    return outcome, stub


async def _change_requests() -> list[ScheduleChangeRequestRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ScheduleChangeRequestRow).order_by(
                        ScheduleChangeRequestRow.created_at,
                        ScheduleChangeRequestRow.id,
                    )
                )
            ).scalars()
        )


async def _replan_calls() -> list[BehaviorModelCallRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(BehaviorModelCallRow)
                    .where(BehaviorModelCallRow.kind == "replan")
                    .order_by(BehaviorModelCallRow.created_at)
                )
            ).scalars()
        )


async def _count(model) -> int:  # noqa: ANN001
    async with session_scope() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


# ---------------------------------------------------------------- 接受：局部替换


async def test_replan_accept_rewrites_only_the_scope(web_env: TestEnv) -> None:  # noqa: ARG001
    """接受提议：区间外逐字段不变、区间内换段、plan_id 不变、revision+1、不写经历。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])

    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-accept-1",
        responses=[_reply(spans=[(19 * 60, 21 * 60)])],
    )

    assert outcome.state == "accepted", outcome
    assert outcome.decision == "accept"
    assert router.calls == 1
    # 作用域是 [18:30, 23:00)：下一个不可移动锚点是 23:00 的预计睡眠
    assert outcome.scope_start == _at(18 * 60 + 30)
    assert outcome.scope_end == _at(23 * 60)
    assert outcome.revision_before == 1 and outcome.revision_after == 2
    assert outcome.plan_id == ids["plan"]
    assert outcome.superseded == 2 and outcome.created == 1

    plan = await _plan_row(ids["plan"])
    assert str(plan.id) == ids["plan"], "plan_id 必须稳定"
    assert plan.revision == 2

    after = await _snapshots(ids["plan"])
    # 区间外：18:00 的晚饭、两段预计睡眠，逐字段不变
    for key in ("dinner", "sleep_night", "sleep_evening"):
        assert _digest(after[ids[key]]) == _digest(before[ids[key]]), key
    # 区间内：19:00–21:00 与 21:00–22:00 被 superseded，新段 19:00–21:00
    async with session_scope() as session:
        old_work = await session.get(ScheduleSegmentRow, ids["work"])
        old_reading = await session.get(ScheduleSegmentRow, ids["reading"])
        created = await load_segments_at(session, plan_id=ids["plan"], revision=2)
    assert old_work is not None and old_work.status == "superseded"
    assert old_reading is not None and old_reading.status == "superseded"
    assert old_work.revision_ended == 2 and old_reading.revision_ended == 2
    assert "turn:turn-accept-1" in list(old_work.source_event_ids)
    new_rows = [item for item in created if item.revision_created == 2]
    assert [(str(item.title), item.start_at, item.end_at) for item in new_rows] == [
        ("看电影", _at(19 * 60), _at(21 * 60))
    ]
    # 旧段仍可查（不物理删）：第 1 版里还有三餐与读书
    async with session_scope() as session:
        at_v1 = await load_segments_at(session, plan_id=ids["plan"], revision=1)
    assert {"吃饭", "赶稿", "读书", "睡觉"} <= {str(item.title) for item in at_v1}

    # 负向断言：接受提议**不产生经历**，也不产生活动/外部动作
    assert await _count(ActivityRow) == 0
    assert await _count(SelfExperienceRow) == 0
    assert await _count(OutboxEvent) == 0, "重排不该顺手排任何工具/外部动作"

    # 决定被记录（含来源 turn 与提交后的版本）
    requests = await _change_requests()
    assert len(requests) == 1
    row = requests[0]
    assert row.source_turn_id == "turn-accept-1"
    assert row.decision == "accept"
    assert row.plan_id == ids["plan"]
    assert row.base_revision == 1
    assert row.committed_revision == 2
    assert row.range_start_at == outcome.scope_start
    assert row.range_end_at == outcome.scope_end


async def test_replan_keeps_past_confirmed_and_commitment_rows_intact(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """过去段 / confirmed 约定 / `CommitmentRow` 在重排前后**逐字段不变**（哈希比较）。"""
    await _seed_routine()
    ids = await _seed_day()
    # 已确认约定：hour 层，21:00–21:30。它是**作用域右边界**（21:00），
    # 也是"凡 confirmed 一律不动"的取样点。
    confirmed = await _insert_segment(
        ids["plan"],
        start_at=_at(21 * 60),
        end_at=_at(21 * 60 + 30),
        title="和她的约定",
        layer="hour",
        commitment_level="confirmed",
    )
    async with session_scope() as session:
        session.add(
            CommitmentRow(
                id=new_id("cmt"),
                actor_id=ACTOR,
                subject_id="subject_1",
                window_id=None,
                text="给妈妈打电话",
                due_at=_at(22 * 60 + 30),
                timezone=TZ,
                state="active",
                source_event_ids=[],
            )
        )
    before = await _snapshots(ids["plan"])
    async with session_scope() as session:
        commitments_before = [
            (str(row.id), row.text, row.due_at, row.state)
            for row in (await session.execute(select(CommitmentRow))).scalars()
        ]

    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-keep-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert outcome.state == "accepted", outcome
    # 21:00 的 confirmed 约定成为作用域右边界 → 只改 [18:30, 21:00)
    assert outcome.scope_end == _at(21 * 60)

    after = await _snapshots(ids["plan"])
    # 区间外的段（晚饭、两段睡眠、21:00 的 confirmed 约定）逐字段不变
    for key in ("dinner", "sleep_night", "sleep_evening"):
        assert _digest(after[ids[key]]) == _digest(before[ids[key]]), key
    assert _digest(after[ids["reading"]]) == _digest(before[ids["reading"]]), (
        "作用域外的读书段被动了"
    )
    assert _digest(after[confirmed]) == _digest(before[confirmed]), "confirmed 段被动了"
    async with session_scope() as session:
        commitments_after = [
            (str(row.id), row.text, row.due_at, row.state)
            for row in (await session.execute(select(CommitmentRow))).scalars()
        ]
    assert commitments_after == commitments_before
    assert router.calls == 1



# ---------------------------------------------------------------- 同一轮只一次


async def test_same_turn_is_decided_once(web_env: TestEnv) -> None:  # noqa: ARG001
    """同一 `turn_id` 只一次决定：第二次返回原决定，不再调模型。"""
    await _seed_routine()
    ids = await _seed_day()

    first, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-once-1",
        responses=[_reply(spans=[(19 * 60, 21 * 60)])],
    )
    assert first.state == "accepted"
    assert router.calls == 1
    plan_after_first = await _plan_row(ids["plan"])
    assert plan_after_first.revision == 2

    second, _stub = await _replan(
        _at(18 * 60 + 35),
        "我们七点去看电影吧",
        turn_id="turn-once-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
        router=router,
    )
    assert second.state == "duplicate"
    assert second.decision == "accept", "第二次必须返回**原**决定"
    assert second.revision_after == 2
    assert router.calls == 1, "重复的 turn 不能再调一次模型"

    plan_after_second = await _plan_row(ids["plan"])
    assert plan_after_second.revision == 2, "重复事件不能抬版本号"
    assert len(await _change_requests()) == 1


# ---------------------------------------------------------------- 频率与预算


async def test_window_hourly_limit_defers_and_records(web_env: TestEnv) -> None:  # noqa: ARG001
    """同一聊天窗口一小时内第二次提议 → defer，**仍然记录**，且明确说"没有改动"。"""
    await _seed_routine()
    ids = await _seed_day()
    first, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-hour-1",
        window_id="win-hour",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert first.state == "accepted" and router.calls == 1

    second, _stub = await _replan(
        _at(18 * 60 + 50),
        "我们七点半去看电影吧",
        turn_id="turn-hour-2",
        window_id="win-hour",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
        router=router,
    )
    assert second.state == "deferred"
    assert second.reason_code == "window_hourly_limit", second
    assert second.calls == 0 and router.calls == 1, "超限时不能调模型"
    assert "没有改动" in second.notice

    # 记录仍然写下来了（不是"她没听见"）
    requests = await _change_requests()
    assert [row.source_turn_id for row in requests] == ["turn-hour-1", "turn-hour-2"]
    assert requests[1].decision == "defer"
    assert requests[1].reason_code == "window_hourly_limit"
    # 计划没被第二次改动
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 2


async def test_daily_limit_after_two_replans(web_env: TestEnv) -> None:  # noqa: ARG001
    """全 actor 每天 ≤2 次：第三次（另一个窗口）→ defer daily_limit。"""
    await _seed_routine()
    await _seed_day()
    for index, window in enumerate(("win-a", "win-b")):
        outcome, _stub = await _replan(
            _at(18 * 60 + 30 + index),
            "我们去看电影吧",
            turn_id=f"turn-day-{index}",
            window_id=window,
            responses=[_reply(spans=[(19 * 60, 20 * 60)])],
        )
        assert outcome.state == "accepted", outcome

    third, router = await _replan(
        _at(18 * 60 + 40),
        "我们去看电影吧",
        turn_id="turn-day-2",
        window_id="win-c",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert third.state == "deferred"
    assert third.reason_code == "daily_limit", third
    assert router.calls == 0
    assert len(await _replan_calls()) == 2


async def test_budget_exhausted_defers_and_records(web_env: TestEnv) -> None:  # noqa: ARG001
    """预算满 → defer budget_exhausted，**且记录**这次提议与 defer 结果。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        session.add(
            BehaviorModelCallRow(
                id=new_id("bcal"),
                actor_id=ACTOR,
                plan_day=DAY,
                kind="replan",
                window_id="win-old",
                idempotency_key="seed-replan-budget",
                attempt=1,
                state="reserved",
                reserved_input_tokens=2400,
                reserved_output_tokens=0,
                lease_owner="seed",
                lease_until=None,
                created_at=_at(17 * 60),
                updated_at=_at(17 * 60),
            )
        )

    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-budget-1",
        window_id="win-budget",
        responses=[_reply(spans=[(19 * 60, 21 * 60)])],
    )
    assert outcome.state == "deferred"
    assert outcome.reason_code == "budget_exhausted", outcome
    assert router.calls == 0
    assert "没有改动" in outcome.notice

    requests = await _change_requests()
    assert len(requests) == 1
    assert requests[0].decision == "defer"
    assert requests[0].reason_code == "budget_exhausted"
    assert requests[0].source_turn_id == "turn-budget-1"
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1


async def test_model_unavailable_defers_without_budget_row(web_env: TestEnv) -> None:  # noqa: ARG001
    """没有可用模型 → 0 次调用、不占预算行、记录 defer。"""
    await _seed_routine()
    ids = await _seed_day()
    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-nomodel-1",
        with_extract=False,
        responses=[],
    )
    assert outcome.state == "deferred"
    assert outcome.reason_code == "model_unavailable", outcome
    assert router.calls == 0
    assert await _replan_calls() == []
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1


# ---------------------------------------------------------------- 硬门


async def test_sleep_window_declines_without_model(web_env: TestEnv) -> None:  # noqa: ARG001
    """睡眠窗内的提议 → 硬门拒绝，**一次模型都不调**，计划不动。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])

    outcome, router = await _replan(
        _at(23 * 60 + 30),
        "我们去看电影吧",
        turn_id="turn-sleep-1",
        responses=[_reply(spans=[(23 * 60 + 40, 23 * 60 + 55)])],
    )
    assert outcome.state == "declined"
    assert outcome.reason_code == "sleep_window", outcome
    assert outcome.decision == "decline"
    assert router.calls == 0, "睡眠窗内不该调模型"
    assert outcome.notice and "安排不变" in outcome.notice

    after = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after.items()} == {
        k: _digest(v) for k, v in before.items()
    }
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1
    requests = await _change_requests()
    assert requests[0].decision == "decline"
    assert requests[0].reason_code == "sleep_window"


async def test_confirmed_commitment_conflict_declines_without_model(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """话里点名的时间撞上已确认承诺（`CommitmentRow`）→ 拒绝，不调模型。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        session.add(
            CommitmentRow(
                id=new_id("cmt"),
                actor_id=ACTOR,
                subject_id="subject_1",
                window_id=None,
                text="给妈妈打电话",
                due_at=_at(19 * 60),
                timezone=TZ,
                state="active",
                source_event_ids=[],
            )
        )
    before = await _snapshots(ids["plan"])

    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "19:00 去看电影吧",
        turn_id="turn-cmt-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert outcome.state == "declined"
    assert outcome.reason_code == "commitment_conflict", outcome
    assert router.calls == 0

    after = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after.items()} == {
        k: _digest(v) for k, v in before.items()
    }
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1


async def test_running_activity_declines(web_env: TestEnv) -> None:  # noqa: ARG001
    """有 running 活动时重排被拒绝（正在发生的事实不能被抹掉）。"""
    await _seed_routine()
    await _seed_day()
    async with session_scope() as session:
        session.add(
            ActivityRow(
                id=new_id("act"),
                actor_id=ACTOR,
                kind="work",
                state="running",
                place_id=None,
                checkpoint={},
                started_at=_at(18 * 60),
                ended_at=None,
                origin="simulated",
                source_event_ids=[],
                visibility="actor_private",
                schedule_segment_id=None,
            )
        )
    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-run-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert outcome.state == "declined"
    assert outcome.reason_code == "activity_running", outcome
    assert router.calls == 0


async def test_confirmed_segment_covering_now_declines(web_env: TestEnv) -> None:  # noqa: ARG001
    """此刻正处在一段已确认安排里 → 拒绝，**不浪费一次模型调用**。"""
    await _seed_routine()
    ids = await _seed_day()
    async with session_scope() as session:
        session.add(
            ScheduleSegmentRow(
                id=new_id("seg"),
                plan_id=ids["plan"],
                actor_id=ACTOR,
                parent_id=None,
                parent_key="",
                layer="hour",
                kind="social",
                title="已经在进行的约定",
                start_at=_at(18 * 60 + 10),
                end_at=_at(19 * 60 + 10),
                place_id=None,
                commitment_level="confirmed",
                status="current",
                is_leaf=True,
                anchor_kind="commitment",
                source="baseline",
                revision_created=1,
                revision_ended=None,
                source_event_ids=[],
                visibility=None,
            )
        )
    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-covering-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert outcome.state == "declined"
    assert outcome.reason_code == "commitment_conflict", outcome
    assert router.calls == 0


async def test_low_energy_only_blocks_optional_outings(web_env: TestEnv) -> None:  # noqa: ARG001
    """体力低只拦**可选外出**；那条门不会因为"她累了"拒绝聊天/承诺。"""
    await _seed_routine()
    await _seed_day()
    async with session_scope() as session:
        session.add(
            PresenceState(
                actor_id=ACTOR,
                energy=5,
                social_load=10,
                satiety=5,
                settled_at=_at(18 * 60),
                revision=1,
            )
        )
    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-tired-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert outcome.state == "declined"
    assert outcome.reason_code == "low_energy", outcome
    assert router.calls == 0

    # 同一份现场，换成一条**不是外出**的结构化信号：体力门不参与。
    async with session_scope() as session:
        session.add(
            ScheduleIntentionRow(
                id=new_id("sint"),
                actor_id=ACTOR,
                kind="work",
                title="在家写点东西",
                target_start_day=DAY,
                target_end_day=DAY,
                target_timezone=TZ,
                state="planned",
                source_kind="user",
                source_ref="turn:turn-indoor-1",
                visibility="actor_private",
                priority=50,
                revision=1,
                horizon_rev=1,
            )
        )
    indoor, router2 = await _replan(
        _at(18 * 60 + 30),
        "随便聊聊",  # 没有任何关键词：靠结构化信号触发
        turn_id="turn-indoor-1",
        responses=[_reply(spans=[(19 * 60, 20 * 60)])],
    )
    assert indoor.state == "accepted", indoor
    assert indoor.signal is not None and indoor.signal.source == "intention"
    assert router2.calls == 1


# ---------------------------------------------------------------- 识别层


async def test_ambiguous_proposals_do_not_touch_the_plan(web_env: TestEnv) -> None:  # noqa: ARG001
    """问句/否定/假设：都只是**候选**，不改计划、不调模型。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])

    cases = (
        ("要不要一起去看电影？", "ambiguous_question"),
        ("算了，不去看电影了", "ambiguous_negation"),
        ("如果明天下雨我们就去看电影吧", "ambiguous_hypothetical"),
    )
    for index, (text, expected) in enumerate(cases):
        outcome, router = await _replan(
            _at(18 * 60 + 30),
            text,
            turn_id=f"turn-amb-{index}",
            window_id=f"win-amb-{index}",
            responses=[_reply(spans=[(19 * 60, 21 * 60)])],
        )
        assert outcome.state == "deferred", (text, outcome)
        assert outcome.reason_code == expected, (text, outcome)
        assert router.calls == 0, (text, "歧义不该调模型")

    after = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after.items()} == {
        k: _digest(v) for k, v in before.items()
    }
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1
    requests = await _change_requests()
    # 三条记录的 created_at 相同（同一分钟），顺序不保证 → 比集合
    assert sorted(str(row.reason_code) for row in requests) == sorted(
        expected for _text, expected in cases
    )


async def test_plain_chat_is_not_a_proposal(web_env: TestEnv) -> None:  # noqa: ARG001
    """没有任何候选的闲聊：返回 no_proposal，**一行都不写**。"""
    await _seed_routine()
    await _seed_day()
    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "今天过得怎么样呀",
        turn_id="turn-chat-1",
        responses=[],
    )
    assert outcome.state == "no_proposal"
    assert router.calls == 0
    assert await _change_requests() == []
    assert await _replan_calls() == []


async def test_model_segment_out_of_scope_is_rejected_whole(web_env: TestEnv) -> None:  # noqa: ARG001
    """模型给了越界段：**整份拒收**（不裁剪），旧计划原样保留。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])

    outcome, router = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-oob-1",
        # 17:00–19:00 越过了作用域起点（18:30）——它在过去/已开始的区域里
        responses=[_reply(spans=[(17 * 60, 19 * 60)])],
    )
    assert outcome.state == "deferred"
    assert outcome.reason_code == "model_invalid", outcome
    assert router.calls == 1
    assert "没有改动" in outcome.notice

    after = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after.items()} == {
        k: _digest(v) for k, v in before.items()
    }
    plan = await _plan_row(ids["plan"])
    assert plan.revision == 1
    assert await _count(ActivityRow) == 0
    assert await _count(SelfExperienceRow) == 0


# ---------------------------------------------------------------- 跨午夜


async def _seed_late_night(*, with_next_day: bool = True) -> dict[str, str]:
    """深夜现场：作息 09:00 醒 / 02:00 睡 → 睡眠窗 [02:00, 09:00)。

    这么设的理由：23:40 的邀约要能**跨过午夜**又不压睡眠，睡眠窗必须晚于
    01:40；同时 D 和 D+1 各自只有自己那一格睡眠锚点，不会互相重叠。
    """
    await _seed_routine(wake=9 * 60, rest=2 * 60)
    plan_d = await _seed_plan(plan_day=DAY, revision=1)
    ids: dict[str, str] = {"plan": plan_d}
    ids["sleep_d"] = await _insert_segment(
        plan_d,
        start_at=_at(2 * 60),
        end_at=_at(9 * 60),
        title="睡觉",
        kind="sleep",
        is_leaf=False,
        anchor_kind="sleep",
    )
    ids["reading"] = await _insert_segment(
        plan_d,
        start_at=_at(22 * 60),
        end_at=_at(22 * 60 + 40),
        title="读书",
        kind="reading",
    )
    if with_next_day:
        plan_n = await _seed_plan(plan_day=NEXT_DAY, revision=1)
        ids["plan_next"] = plan_n
        ids["sleep_n"] = await _insert_segment(
            plan_n,
            start_at=_at(2 * 60, day=NEXT_DAY),
            end_at=_at(9 * 60, day=NEXT_DAY),
            title="睡觉",
            kind="sleep",
            is_leaf=False,
            anchor_kind="sleep",
        )
    return ids


async def test_cross_midnight_replan_is_atomic(web_env: TestEnv) -> None:  # noqa: ARG001
    """跨午夜邀约：两份 `day_plans` 各抬一版，段各自落在自己那份计划里。"""
    ids = await _seed_late_night()
    before_d = await _snapshots(ids["plan"])
    before_n = await _snapshots(ids["plan_next"])

    outcome, router = await _replan(
        _at(22 * 60 + 40),
        "23:40 去看电影吧",
        turn_id="turn-mid-1",
        responses=[
            _reply(
                day_spans=[
                    (DAY, 23 * 60 + 40, 24 * 60),
                    (NEXT_DAY, 0, 60 + 40),
                ]
            )
        ],
    )
    assert outcome.state == "accepted", outcome
    assert router.calls == 1
    # 作用域越过午夜，覆盖两个计划日
    assert outcome.scope_start == _at(23 * 60 + 40)
    assert outcome.scope_end == _at(60 + 40, day=NEXT_DAY)
    assert outcome.created == 2
    assert outcome.revision_after == 2

    plan_d = await _plan_row(ids["plan"])
    plan_n = await _plan_row(ids["plan_next"])
    assert plan_d.revision == 2, "D 必须抬一版"
    assert plan_n.revision == 2, "D+1 必须同时抬一版"

    # 每份计划里的新段都落在**自己**那一格（不跨午夜、不写进别人的行）
    async with session_scope() as session:
        created_d = await load_segments_at(session, plan_id=ids["plan"], revision=2)
        created_n = await load_segments_at(session, plan_id=ids["plan_next"], revision=2)
    fresh_d = [row for row in created_d if row.revision_created == 2]
    fresh_n = [row for row in created_n if row.revision_created == 2]
    assert [(row.start_at, row.end_at) for row in fresh_d] == [
        (_at(23 * 60 + 40), _at(24 * 60))
    ]
    assert [(row.start_at, row.end_at) for row in fresh_n] == [
        (_at(0, day=NEXT_DAY), _at(60 + 40, day=NEXT_DAY))
    ]

    # 区间外：22:00 的读书与两段睡眠逐字段不变
    after_d = await _snapshots(ids["plan"])
    for key in ("sleep_d", "reading"):
        assert _digest(after_d[ids[key]]) == _digest(before_d[ids[key]]), key
    after_n = await _snapshots(ids["plan_next"])
    assert _digest(after_n[ids["sleep_n"]]) == _digest(before_n[ids["sleep_n"]])

    # 计划不是事实
    assert await _count(ActivityRow) == 0
    assert await _count(SelfExperienceRow) == 0


async def test_cross_midnight_rolls_back_when_one_day_cannot_change(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """其中一日不可改（期间刚确认了别的安排）→ **整单回滚**，两份计划都不动。"""
    ids = await _seed_late_night()
    before_d = await _snapshots(ids["plan"])
    before_n = await _snapshots(ids["plan_next"])
    plan_n = ids["plan_next"]

    async def _confirm_something_in_next_day() -> None:
        # 模型调用的那几十秒里，D+1 上刚确认了一件事（设计 §4 要求重新校验）。
        await _insert_segment(
            plan_n,
            start_at=_at(10, day=NEXT_DAY),
            end_at=_at(40, day=NEXT_DAY),
            title="刚确认的事",
            commitment_level="confirmed",
        )

    stub = _StubRouter(
        [
            _reply(
                day_spans=[
                    (DAY, 23 * 60 + 40, 24 * 60),
                    (NEXT_DAY, 0, 60 + 40),
                ]
            )
        ]
    )
    stub.on_call = _confirm_something_in_next_day

    outcome, _stub = await _replan(
        _at(22 * 60 + 40),
        "23:40 去看电影吧",
        turn_id="turn-mid-2",
        router=stub,
    )
    assert outcome.state == "deferred", outcome
    assert outcome.reason_code == "commitment_conflict", outcome
    assert "没有改动" in outcome.notice

    # **两份**计划都没动：D 没抬版本、也没有半截的新段
    plan_d = await _plan_row(ids["plan"])
    plan_n_after = await _plan_row(plan_n)
    assert plan_d.revision == 1, "D 被改了一半：整单回滚失败"
    assert plan_n_after.revision == 1
    after_d = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after_d.items()} == {
        k: _digest(v) for k, v in before_d.items()
    }
    after_n = await _snapshots(plan_n)
    # D+1 只多了"刚确认的事"（那是别人的写入，不是这次重排）
    added = set(after_n) - set(before_n)
    assert len(added) == 1
    async with session_scope() as session:
        added_row = await session.get(ScheduleSegmentRow, next(iter(added)))
    assert added_row is not None and str(added_row.title) == "刚确认的事"
    # 其他段逐字段不变
    for key, snapshot in before_n.items():
        assert _digest(after_n[key]) == _digest(snapshot), key

    requests = await _change_requests()
    assert requests[0].decision == "defer"
    assert requests[0].reason_code == "commitment_conflict"


async def test_plans_are_locked_in_plan_day_order(web_env: TestEnv) -> None:  # noqa: ARG001
    """跨午夜取锁按 `plan_day` 升序（否则两边反向加锁会互相等死）。

    单线程测不出真正的死锁，但能测出**加锁顺序**：故意先建 D+1、后建 D，
    取锁结果必须仍是 [D, D+1]。顺序错了，多 worker 下就会有人超时。
    """
    from ling.domains.agenda.replan import _lock_plans

    await _seed_routine()
    plan_next = await _seed_plan(plan_day=NEXT_DAY, revision=1)
    plan_day = await _seed_plan(plan_day=DAY, revision=1)

    async with session_scope() as session:
        locked = await _lock_plans(session, actor_id=ACTOR, days=[NEXT_DAY, DAY])
    assert [row.plan_day for row in locked.values()] == [DAY, NEXT_DAY]
    assert {str(row.id) for row in locked.values()} == {plan_day, plan_next}


async def test_stale_expected_revision_writes_nothing(web_env: TestEnv) -> None:  # noqa: ARG001
    """CAS 失败（期望版本过期）→ 整份作用域修订不生效，段与版本号都不动。"""
    await _seed_routine()
    ids = await _seed_day()
    before = await _snapshots(ids["plan"])
    async with session_scope() as session:
        row = await session.get(DayPlanRow, ids["plan"])
        assert row is not None
        with pytest.raises(StaleRevisionError):
            await replace_scope(
                session,
                ScopeEdit(
                    row=row,
                    scope_start=_at(19 * 60),
                    scope_end=_at(21 * 60),
                    drafts=(_draft(start=_at(19 * 60), end=_at(21 * 60)),),
                    expected_revision=99,
                ),
                now=_at(18 * 60 + 30),
            )
    after = await _snapshots(ids["plan"])
    assert {k: _digest(v) for k, v in after.items()} == {
        k: _digest(v) for k, v in before.items()
    }
    assert (await _plan_row(ids["plan"])).revision == 1


# ---------------------------------------------------------------- 会话层接入


async def test_note_schedule_proposal_is_idempotent_and_leaks_no_text(  # noqa: ARG001
    web_env: TestEnv,
) -> None:
    """轮次收尾调用的那个函数：命中候选才投一条事件，普通闲聊/她自己说的都不投。"""
    from ling.domains.agenda.replan import note_schedule_proposal

    async with session_scope() as session:
        first = await note_schedule_proposal(
            session,
            actor_id=ACTOR,
            window_id="win-hook",
            turn_id="turn-hook-1",
            text="我们七点去看电影吧",
        )
        # 普通闲聊：候选都不算，什么都不投
        silent = await note_schedule_proposal(
            session,
            actor_id=ACTOR,
            window_id="win-hook",
            turn_id="turn-hook-2",
            text="今天天气不错",
        )
        # 同一轮重复投递：幂等，不投第二条
        again = await note_schedule_proposal(
            session,
            actor_id=ACTOR,
            window_id="win-hook",
            turn_id="turn-hook-1",
            text="我们七点去看电影吧",
        )

    assert first == "replan:turn-hook-1"
    assert silent is None
    assert again is None

    async with session_scope() as session:
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "schedule.replan")
                )
            ).scalars()
        )
    assert len(events) == 1
    assert events[0].id == "replan:turn-hook-1"
    assert events[0].payload["turn_id"] == "turn-hook-1"
    assert events[0].payload["window_id"] == "win-hook"
    # outbox 里**没有**聊天原文
    assert "text" not in events[0].payload
    assert "我们七点去看电影吧" not in json.dumps(events[0].payload, ensure_ascii=False)


# ---------------------------------------------------------------- 只读时间轴能看出改动


async def test_timeline_endpoint_shows_the_plan_was_changed(logged_in: TestEnv) -> None:
    """`GET /v1/life/timeline` 能看出"计划改动过"：`superseded_segment_count` > 0。

    设计 §7 给前端的契约就是这样：被替代的段**不画在当前时间轴上**（两版同时
    画会重叠），只计数——前端据此标"计划改动过"，而不是凭空猜。
    """
    await _seed_routine()
    ids = await _seed_day()
    outcome, _stub = await _replan(
        _at(18 * 60 + 30),
        "我们七点去看电影吧",
        turn_id="turn-timeline-1",
        responses=[_reply(spans=[(19 * 60, 21 * 60)])],
    )
    assert outcome.state == "accepted", outcome

    response = await logged_in.client.get(
        "/v1/life/timeline", params={"date": DAY.isoformat()}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["superseded_segment_count"] >= 1, body
    titles = [item["title"] for item in body["segments"]]
    assert "看电影" in titles
    assert "赶稿" not in titles, "被替代的段不该还画在当前时间轴上"
    assert str(body["segments"][0]["plan_id"]) == ids["plan"]
