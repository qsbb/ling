"""梦与跨夜结算（P7a）：真 PostgreSQL 上的唯一键、隔离与失败降级。

守这些事实（设计 §5/§9 阶段 4 的可程序化验收）：

1. 同一 ``(actor, sleep_date)`` 只有**一份梦**、**一次结算**，重复调用不重复
   调模型、不重复打折情绪；
2. 候选素材不足两个来源 → ``no_dream`` 且**零模型调用**；
3. 模型失败 → ``failed``，**但跨夜结算照常发生**（梦与结算解耦）；
4. 午夜把预计睡眠切成两段，仍然只算**一晚**（不会跑两次）；
5. **梦不出现在 activities / self_experiences / 记忆检索里**（负向断言）；
6. 威胁比例是硬闸门：模型不能自己把某一晚变成威胁梦；
7. P0.5 的 ``fact_filter`` 注入点真的能挡住一条"不算事实"的经历。

时间全部用固定值（不依赖"现在几点"），与 ``test_schedule_outline.py`` 同一套做法。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.agenda.timeline import RoutineSnapshot, sleep_anchors_for_day
from ling.domains.dream.fragments import threat_plan
from ling.domains.dream.service import (
    DreamService,
    SleepSettlementService,
    local_minute_instant,
    night_window,
)
from ling.domains.memory.self_timeline import SelfTimelineService
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    ActorRoutineRow,
    AffectSignalRow,
    AffectState,
    DreamReportRow,
    MemoryEpisode,
    MemoryRow,
    SelfExperienceRow,
    SleepSettlementRow,
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
SLEEP_DATE = date(2026, 11, 8)
WAKE_MINUTE = 8 * 60
REST_MINUTE = 23 * 60


def _wake(night: date = SLEEP_DATE) -> datetime:
    return local_minute_instant(night, WAKE_MINUTE, TZ)


def _bed(night: date = SLEEP_DATE) -> datetime:
    return local_minute_instant(night - timedelta(days=1), REST_MINUTE, TZ)


def _now(night: date = SLEEP_DATE) -> datetime:
    """醒前 45 分钟——正是设计里的生成时刻。"""
    return _wake(night) - timedelta(minutes=45)


def _non_threat_night(start: date = SLEEP_DATE, span: int = 8) -> date:
    for offset in range(span):
        night = start + timedelta(days=offset)
        if not threat_plan(ACTOR, night).present:
            return night
    raise AssertionError("八个晚上都没有无威胁的一晚，分布闸门写错了")


# ------------------------------------------------------------------ 夹具


class _StubRouter:
    """按顺序吐出预设文本；没给预设时**从 prompt 里造一份合法输出**。

    从 prompt 反造而不是硬编码 cue id：cue id 带真实行 id，测试不该猜。
    """

    def __init__(
        self,
        *,
        payloads: list[str] | None = None,
        error: Exception | None = None,
        transform=None,  # noqa: ANN001 - 把自动生成的合法输出改坏，用于负例
    ) -> None:
        self._payloads = list(payloads or [])
        self._error = error
        self._transform = transform
        self.prompts: list[str] = []

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.prompts.append(messages[0].content)
        if self._error is not None:
            raise self._error
        text = self._payloads.pop(0) if self._payloads else self._auto(messages[0].content)
        if self._transform is not None:
            text = self._transform(text)
        yield ModelChunk(delta=text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=text)

    @staticmethod
    def _auto(prompt: str) -> str:
        snapshot = json.loads(prompt.split("输入快照（JSON，只有四元组与分数）：", 1)[1])
        cues = snapshot["cues"]
        # 按来源去重后任意两条 cue 必然来自两个不同来源。
        used = [item["cue_id"] for item in cues[:2]]
        return json.dumps(
            {
                "title": "走廊里的水",
                "narrative": (
                    "我在一条没有尽头的走廊里走，脚下的水涨到脚踝，"
                    "门牌上写着别人的名字，回头时门已经不见了。"
                ),
                "mood": "uneasy",
                "used_cue_ids": used,
                "transforms_used": [snapshot["transform"][0]["transform_id"]],
                "threat_present": snapshot["threat"]["present"],
            },
            ensure_ascii=False,
        )


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
            models=(ProviderModel("dream-lite"),),
        )
    }
    profiles = (
        {
            "memory.extract": ProfileBinding(
                profile="memory.extract",
                provider_id="fake-main",
                model_id="dream-lite",
            )
        }
        if with_extract
        else {}
    )
    return EffectiveConfig(revision=1, providers=providers, profiles=profiles)


async def _seed_routine(*, wake: int = WAKE_MINUTE, rest: int = REST_MINUTE) -> None:
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


async def _seed_experiences(
    count: int = 2, *, night: date = SLEEP_DATE, kind: str = "observed"
) -> list[str]:
    """造 ``count`` 条**带来源活动**的自我经历。

    默认 ``kind=observed``（已证实）：P0.5 的事实闸会回查来源活动，所以测试
    不能只插一行 ``self_experiences`` —— 那正是要被排除的"只读派生行"。
    """
    ids: list[str] = []
    async with session_scope() as session:
        for index in range(count):
            activity_id = f"act_dream_seed_{kind}_{index}"
            experience_id = f"selfexp_dream_{kind}_{index}"
            ids.append(experience_id)
            occurred = _bed(night) - timedelta(hours=2 + index)
            session.add(
                ActivityRow(
                    id=activity_id,
                    actor_id=ACTOR,
                    kind="reading",
                    state="completed",
                    started_at=occurred - timedelta(hours=1),
                    ended_at=occurred,
                    completion_evidence=f"kind={kind};读完了第{index}章",
                    origin="tool_receipt",
                    visibility="actor_private",
                )
            )
            session.add(
                SelfExperienceRow(
                    id=experience_id,
                    actor_id=ACTOR,
                    kind="reading",
                    summary=f"读完了第{index}章，屋子里很安静",
                    source_event_ids=[f"activity:{activity_id}", f"evidence:{kind}"],
                    dedupe_key=f"dream-seed-{kind}-{index}",
                    origin="simulated",
                    visibility="actor_private",
                    occurred_at=occurred,
                )
            )
    return ids


async def _seed_affect(*, night: date = SLEEP_DATE) -> None:
    """睡前一条**显著且负性/高唤醒**的 global 情绪 + 一条日内峰值信号。"""
    async with session_scope() as session:
        session.add(
            AffectState(
                id="aff_dream_uneasy",
                actor_id=ACTOR,
                emotion="uneasy",
                intensity=0.9,
                decay_per_hour=0.05,
                scope="global",
                window_id="",
                valence=-0.6,
                arousal=0.8,
                updated_at=_bed(night),
            )
        )
        session.add(
            AffectSignalRow(
                id="signal_dream_1",
                actor_id=ACTOR,
                source_event_id="activity:seed0",
                scope="global",
                window_id="",
                emotion="uneasy",
                intensity=0.9,
                valence=-0.6,
                arousal=0.8,
                occurred_at=_bed(night) - timedelta(hours=1),
            )
        )


async def _seed_paused_activity(*, night: date = SLEEP_DATE) -> None:
    async with session_scope() as session:
        session.add(
            ActivityRow(
                id="act_dream_paused",
                actor_id=ACTOR,
                kind="writing",
                state="paused",
                started_at=_bed(night) - timedelta(hours=3),
                origin="simulated",
                visibility="actor_private",
            )
        )


async def _generate(
    *,
    router: _StubRouter | None = None,
    night: date = SLEEP_DATE,
    now: datetime | None = None,
    fact_filter=None,  # noqa: ANN001
    with_extract: bool = True,
):  # noqa: ANN201
    stub = router or _StubRouter()
    async with session_scope() as session:
        outcome = await DreamService(session, actor_id=ACTOR).generate(
            night,
            now=now or _now(night),
            router=stub,
            config_reader=_StubConfig(_config(with_extract=with_extract)),
            fact_filter=fact_filter,
        )
    return outcome, stub


async def _settle(*, night: date = SLEEP_DATE, now: datetime | None = None):  # noqa: ANN201
    async with session_scope() as session:
        return await SleepSettlementService(session, actor_id=ACTOR).settle(
            night, now=now or (_wake(night) + timedelta(minutes=5))
        )


async def _count(model: type) -> int:  # noqa: ANN001
    async with session_scope() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _dream_row(night: date = SLEEP_DATE) -> DreamReportRow | None:
    async with session_scope() as session:
        return (
            await session.execute(
                select(DreamReportRow).where(
                    DreamReportRow.actor_id == ACTOR,
                    DreamReportRow.sleep_date == night,
                )
            )
        ).scalar_one_or_none()


async def _affect_row() -> AffectState:
    async with session_scope() as session:
        return (
            await session.execute(
                select(AffectState).where(AffectState.id == "aff_dream_uneasy")
            )
        ).scalar_one()


# ------------------------------------------------------------------ 唯一键


async def test_one_dream_and_one_settlement_per_sleep_date(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)
    await _seed_affect()

    first, router = await _generate()
    assert first.status == "ready", first
    assert first.calls == 1
    assert len(router.prompts) == 1
    assert first.cue_ids, "梦必须带可追溯的 cue_ids"
    assert first.transform_ids, "梦必须记录用到的重组算子"
    assert first.sources >= 2
    assert await _count(DreamReportRow) == 1

    # 第二次：唯一键占用 → 不调模型、不写第二份。
    second, same_router = await _generate()
    assert second.status == "ready"
    assert second.calls == 0
    assert same_router.prompts == []
    assert await _count(DreamReportRow) == 1

    row = await _dream_row()
    assert row is not None
    assert row.origin == "simulated" and row.visibility == "actor_private"
    assert all(str(item).startswith("cue:") for item in row.cue_ids)
    assert set(row.transform_ids) <= {
        "swap_place",
        "swap_person",
        "time_reverse",
        "object_animation",
        "scene_splice",
    }

    settled = await _settle()
    assert settled.state == "settled", settled
    again = await _settle()
    assert again.state == "unchanged", again
    assert await _count(SleepSettlementRow) == 1

    affect = await _affect_row()
    # I_wake = min(0.35, max(I_linear=0.9-0.05*8=0.5, 0.6*I_bed=0.54)) = 0.35
    assert affect.intensity == pytest.approx(0.35, abs=1e-4)
    # 二维各向 0 回归 40%：-0.6 → -0.36；0.8 → 0.48
    assert affect.valence == pytest.approx(-0.36, abs=1e-4)
    assert affect.arousal == pytest.approx(0.48, abs=1e-4)
    # 第二次结算没有再次打折（否则重启几次就把情绪吃光）。
    assert affect.valence == pytest.approx(-0.36, abs=1e-4)


async def test_settlement_snapshot_records_bed_and_wake(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_affect()
    outcome = await _settle()
    assert outcome.state == "settled"
    assert outcome.bed_at == _bed() and outcome.wake_at == _wake()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(SleepSettlementRow).where(SleepSettlementRow.sleep_date == SLEEP_DATE)
            )
        ).scalar_one()
    assert row.source_routine_revision == 1
    assert row.global_affect_before["components"]["uneasy"]["intensity"] == pytest.approx(
        0.9, abs=1e-3
    )
    assert row.global_affect_after["components"]["uneasy"]["intensity"] == pytest.approx(
        0.35, abs=1e-3
    )
    assert row.global_affect_after["components"]["uneasy"]["arousal"] == pytest.approx(
        0.48, abs=1e-3
    )


# ------------------------------------------------------------------ 降级


async def test_insufficient_material_is_no_dream_without_model_call(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(1)  # 只有一条素材：不足以跨来源重混

    outcome, router = await _generate()
    assert outcome.status == "no_dream", outcome
    assert outcome.calls == 0
    assert router.prompts == [], "候选不足时不许调模型"
    assert await _count(DreamReportRow) == 1

    # 第二次仍然 0 次调用：no_dream 也占用唯一键，不会反复重试。
    again, same_router = await _generate()
    assert again.status == "no_dream"
    assert again.calls == 0
    assert same_router.prompts == []


async def test_model_failure_marks_failed_but_settlement_still_runs(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)
    await _seed_paused_activity()
    await _seed_affect()

    outcome, router = await _generate(router=_StubRouter(error=RuntimeError("provider down")))
    assert outcome.status == "failed", outcome
    assert outcome.calls == 1, "只允许一次调用，不补第二次"
    assert len(router.prompts) == 1
    row = await _dream_row()
    assert row is not None and row.status == "failed"
    assert row.narrative is None, "失败不许补编正文"

    settled = await _settle()
    assert settled.state == "settled", "梦失败也必须照常结算"
    assert await _count(SleepSettlementRow) == 1


async def test_model_unavailable_is_failed_with_zero_calls(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)
    outcome, router = await _generate(with_extract=False)
    assert outcome.status == "failed"
    assert outcome.calls == 0 and router.prompts == []
    assert outcome.reason_code == "model_unavailable"


async def test_after_wake_window_is_missed_without_model_call(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)
    outcome, router = await _generate(now=_wake() + timedelta(minutes=31))
    assert outcome.status == "missed", outcome
    assert outcome.calls == 0 and router.prompts == []
    row = await _dream_row()
    assert row is not None and row.status == "missed"
    # 梦 missed 不影响结算。
    assert (await _settle()).state == "settled"


async def test_no_sleep_window_writes_nothing(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine(wake=WAKE_MINUTE, rest=WAKE_MINUTE)
    await _seed_experiences(2)
    outcome, router = await _generate()
    assert outcome.status == "no_sleep_window", outcome
    assert outcome.calls == 0 and router.prompts == []
    assert await _count(DreamReportRow) == 0, "没有一晚可记录时不写产品记录"
    assert (await _settle()).state == "no_sleep_window"
    assert await _count(SleepSettlementRow) == 0


async def test_threat_flag_cannot_be_invented_by_the_model(web_env: TestEnv) -> None:  # noqa: ARG001
    """威胁是分布上限：模型不能自己把某一晚变成威胁梦。"""
    night = _non_threat_night()
    await _seed_routine()
    await _seed_experiences(2, night=night)

    def _flip_threat(raw: str) -> str:
        payload = json.loads(raw)
        assert payload["threat_present"] is False
        payload["threat_present"] = True
        return json.dumps(payload, ensure_ascii=False)

    outcome, stub = await _generate(
        router=_StubRouter(transform=_flip_threat), night=night
    )
    assert outcome.status == "failed", outcome
    assert outcome.reason_code == "dream_threat_mismatch"
    assert outcome.calls == 1 and len(stub.prompts) == 1
    row = await _dream_row(night)
    assert row is not None and row.status == "failed"
    assert row.narrative is None, "不合分布约束的梦不许落正文"


# ------------------------------------------------------------------ 午夜与隔离


async def test_midnight_split_is_still_one_night(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)

    anchors = sleep_anchors_for_day(
        SLEEP_DATE,
        TZ,
        RoutineSnapshot(wake_minute=WAKE_MINUTE, rest_minute=REST_MINUTE, revision=1, timezone=TZ),
    )
    assert len(anchors) == 2, "23:00 睡、08:00 起：D 内有两段预计睡眠"
    async with session_scope() as session:
        window = await night_window(
            session, actor_id=ACTOR, sleep_date=SLEEP_DATE, timezone=TZ
        )
    assert window is not None
    # 两段计划睡眠 = 一晚：bed 在前一天 23:00，wake 在当天 08:00。
    assert window.bed_at == _bed() and window.wake_at == anchors[0].end_at

    outcome, _router = await _generate()
    assert outcome.status == "ready"
    settled = await _settle()
    assert settled.state == "settled"
    assert settled.bed_at == _bed() and settled.wake_at == _wake()
    assert await _count(DreamReportRow) == 1
    assert await _count(SleepSettlementRow) == 1


async def test_dream_never_becomes_a_fact(web_env: TestEnv) -> None:  # noqa: ARG001
    await _seed_routine()
    await _seed_experiences(2)
    await _seed_paused_activity()
    before = (
        await _count(ActivityRow),
        await _count(SelfExperienceRow),
        await _count(MemoryRow),
        await _count(MemoryEpisode),
    )
    outcome, _router = await _generate()
    assert outcome.status == "ready"
    after = (
        await _count(ActivityRow),
        await _count(SelfExperienceRow),
        await _count(MemoryRow),
        await _count(MemoryEpisode),
    )
    assert after == before, "梦绝不能写活动/经历/长期记忆/情节记忆"

    # 事实检索的负向断言：梦的正文与标题都不在任何事实表里。
    needle = "门牌上写着别人的名字"
    async with session_scope() as session:
        leaked_experiences = (
            await session.execute(
                select(SelfExperienceRow.id).where(
                    SelfExperienceRow.summary.ilike(f"%{needle}%")
                )
            )
        ).scalars().all()
        leaked_memories = (
            await session.execute(
                select(MemoryRow.id).where(MemoryRow.text.ilike(f"%{needle}%"))
            )
        ).scalars().all()
        leaked_episodes = (
            await session.execute(
                select(MemoryEpisode.id).where(MemoryEpisode.summary.ilike(f"%{needle}%"))
            )
        ).scalars().all()
        recent = await SelfTimelineService(session, actor_id=ACTOR).recent(days=3, limit=20)
    assert not leaked_experiences
    assert not leaked_memories
    assert not leaked_episodes
    assert all("走廊" not in item.summary for item in recent)
    # 而梦本身只在 dream_reports 里，带着"模拟 + 角色私有"的标签。
    row = await _dream_row()
    assert row is not None and row.origin == "simulated"
    assert row.visibility == "actor_private"
    assert row.narrative and needle in row.narrative


# ------------------------------------------------------------------ P0.5 接口


async def test_clock_only_experience_never_reaches_the_prompt(web_env: TestEnv) -> None:  # noqa: ARG001
    """P0.5 的默认事实闸：clock_only 证据不算事实，连 prompt 都不进。"""
    await _seed_routine()
    observed = await _seed_experiences(2, kind="observed")
    clock_only = await _seed_experiences(1, kind="clock_only")

    outcome, router = await _generate()
    assert outcome.status == "ready", outcome
    assert clock_only[0] not in router.prompts[0], "clock_only 经历不许进 prompt"
    assert f"cue:experience:{clock_only[0]}" not in outcome.cue_ids
    assert observed[0] in router.prompts[0], "已证实经历仍然要进候选"


async def test_fact_filter_blocks_a_cue_before_the_prompt(web_env: TestEnv) -> None:  # noqa: ARG001
    """P0.5 的"证据种类过滤"接口：被判成非事实的经历连 prompt 都不进。"""
    await _seed_routine()
    ids = await _seed_experiences(3)
    blocked = ids[1]

    outcome, router = await _generate(fact_filter=lambda row: row.id != blocked)
    assert outcome.status == "ready", outcome
    assert len(router.prompts) == 1
    assert blocked not in router.prompts[0], "被封堵的经历不许进 prompt"
    assert f"cue:experience:{blocked}" not in outcome.cue_ids
