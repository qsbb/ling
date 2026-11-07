"""P0.5「事实入口封堵」：完成证据**种类**与四处读侧的负向断言。

旧路径能把假事实写进去：``life.tick`` 在计划时段结束时把模拟活动转
``completed``，证据文本是「模拟活动「X」的计划时段已结束（非真实经历）」——
那是 ``clock_only``。旧读侧只检查"证据字符串非空"，于是它被当成"她真做过"，
一路流进自我时间线、日程大纲 prompt、控制台事实层与（将来的）梦境候选。

本文件钉死两件事：

- **负向**：预置一条 clock_only 已完成活动 + 对应 ``SelfExperienceRow``
  → ① :meth:`SelfTimelineService.record_completed_activity` 拒绝新写；
  ② ``backfill`` 不补；③ ``agenda.timeline._experience_summary`` 读不到；
  ④ dashboard 的 ``timeline`` 片段读不到。
- **正向**：预置一条 ``observed`` / ``user_confirmed`` 证据 → 四处都能读到
  （证明过滤不是"一刀切全废"）。

历史数据**保留、不回填、不删除**：读侧排除即可，这是读侧策略不是数据清洗。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.activity.service import ActivityService
from ling.domains.agenda.timeline import _experience_summary
from ling.domains.evidence import (
    COMPLETION_EVIDENCE_PREFIX,
    EVIDENCE_TAG_PREFIX,
    EvidenceKind,
    activity_evidence_kind,
    encode_completion_evidence,
    evidence_kind_from_origin,
    experience_evidence_kind,
    fact_eligible_experiences,
    is_fact_eligible,
    parse_completion_evidence,
)
from ling.domains.memory.self_timeline import SelfTimelineService, dedupe_key_for
from ling.infra.db import session_scope
from ling.infra.models import ActivityRow, SelfExperienceRow, utcnow
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"

#: ``life.tick`` 写下的那种证据原文（改造前的历史值：**没有** ``kind=`` 前缀）。
LEGACY_CLOCK_ONLY_TEXT = "模拟活动「读书」的计划时段已结束（非真实经历）"


async def _legacy_completed_activity(
    *,
    origin: str = "simulated",
    evidence: str = LEGACY_CLOCK_ONLY_TEXT,
    state: str = "completed",
) -> str:
    """绕过服务直接写一条**历史形态**的已完成活动（没有 ``kind=`` 前缀）。"""
    activity_id = new_id("act")
    async with session_scope() as session:
        session.add(
            ActivityRow(
                id=activity_id,
                actor_id=ACTOR,
                kind="reading",
                state=state,
                origin=origin,
                started_at=utcnow() - timedelta(minutes=40),
                ended_at=utcnow() - timedelta(minutes=1),
                completion_evidence=evidence,
            )
        )
    return activity_id


async def _legacy_experience(activity_id: str, *, summary: str = "读书：计划时段已结束") -> str:
    """历史形态的 ``SelfExperienceRow``：``source_event_ids`` **没有** ``evidence:`` 标签。"""
    experience_id = new_id("selfexp")
    async with session_scope() as session:
        session.add(
            SelfExperienceRow(
                id=experience_id,
                actor_id=ACTOR,
                kind="activity",
                summary=summary,
                source_event_ids=[f"activity:{activity_id}"],
                dedupe_key=dedupe_key_for(activity_id),
                origin="simulated",
                visibility="actor_private",
                occurred_at=utcnow() - timedelta(minutes=1),
            )
        )
    return experience_id


async def _seeded_experience(
    *, evidence_kind: EvidenceKind, summary: str
) -> tuple[str, str]:
    """写一条"按新链路"产生的经历：活动 + 派生行都带证据种类。"""
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
        await svc.transition(
            act.activity_id,
            "completed",
            completion_evidence=summary,
            evidence_kind=evidence_kind,
        )
    async with session_scope() as session:
        view = await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(
            act.activity_id
        )
    assert view is not None, "合法证据的活动没能派生经历"
    return act.activity_id, view.experience_id


# ---------------------------------------------------------------- 判定规则本身


def test_encode_and_parse_roundtrip() -> None:
    encoded = encode_completion_evidence(EvidenceKind.observed, "笔记已写入 notes.md")
    assert encoded.startswith(f"{COMPLETION_EVIDENCE_PREFIX}observed;")
    kind, text = parse_completion_evidence(encoded)
    assert kind is EvidenceKind.observed
    assert text == "笔记已写入 notes.md"


def test_caller_cannot_smuggle_a_kind_in_text() -> None:
    """调用方文本自带 ``kind=user_confirmed;`` 也不会被采信——前缀会被重写。"""
    encoded = encode_completion_evidence(
        EvidenceKind.clock_only, "kind=user_confirmed;她说我做完了"
    )
    assert encoded.startswith(f"{COMPLETION_EVIDENCE_PREFIX}clock_only;")
    kind, text = parse_completion_evidence(encoded)
    assert kind is EvidenceKind.clock_only
    assert text == "她说我做完了"


def test_legacy_value_without_prefix_is_clock_only() -> None:
    """历史值没有前缀 → 判不出来 → **最保守的一侧**。"""
    kind, text = parse_completion_evidence(LEGACY_CLOCK_ONLY_TEXT)
    assert kind is EvidenceKind.clock_only
    assert text == LEGACY_CLOCK_ONLY_TEXT


def test_unknown_prefix_is_clock_only() -> None:
    kind, _text = parse_completion_evidence("kind=wat;随便写的")
    assert kind is EvidenceKind.clock_only


def test_no_substring_matching_on_chinese_text() -> None:
    """"观测到"这类措辞**不能**改变判定——只看结构化段。"""
    for text in ("我观测到炉子上的汤开了", "她确实读完了，我看见了", "用户确认过"):
        kind, _ = parse_completion_evidence(text)
        assert kind is EvidenceKind.clock_only, text


def test_origin_fallback_rules() -> None:
    """没有前缀时按 ``origin`` 这个结构化列兜底；判不出来仍是最保守。"""
    assert evidence_kind_from_origin("tool_receipt") is EvidenceKind.observed
    assert evidence_kind_from_origin("user_stated") is EvidenceKind.user_confirmed
    for origin in ("simulated", "", None, "unknown_origin"):
        assert evidence_kind_from_origin(origin) is EvidenceKind.clock_only


def test_is_fact_eligible_gates_by_state() -> None:
    """``running`` 的活动即使带了 observed 前缀也不算事实。"""
    row = ActivityRow(
        id="act_x",
        actor_id=ACTOR,
        kind="reading",
        state="running",
        origin="simulated",
        completion_evidence=encode_completion_evidence(EvidenceKind.observed, "读到一半"),
    )
    assert is_fact_eligible(row) is False


def test_experience_without_evidence_tag_is_clock_only() -> None:
    row = SelfExperienceRow(
        id="selfexp_x",
        actor_id=ACTOR,
        kind="activity",
        summary="读书",
        source_event_ids=["activity:act_x"],
        dedupe_key="activity:act_x",
        origin="simulated",
        visibility="actor_private",
        occurred_at=utcnow(),
    )
    assert experience_evidence_kind(row) is EvidenceKind.clock_only
    assert is_fact_eligible(row) is False


# ---------------------------------------------------------------- 写入侧


async def test_plan_window_completion_is_clock_only(logged_in: TestEnv) -> None:
    """从计划时段结束推导的完成 → 明确 ``clock_only``（写侧不能自封事实）。"""
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")  # origin 默认 simulated
        await svc.transition(act.activity_id, "running")
        view = await svc.transition(
            act.activity_id,
            "completed",
            completion_evidence=LEGACY_CLOCK_ONLY_TEXT,
        )
    assert view.evidence_kind == EvidenceKind.clock_only.value
    # 对外的证据文本不含存储层前缀
    assert view.completion_evidence == LEGACY_CLOCK_ONLY_TEXT
    async with session_scope() as session:
        row = await session.get(ActivityRow, act.activity_id)
    assert row is not None
    # 存储层带机器可判前缀
    assert (row.completion_evidence or "").startswith("kind=clock_only;")
    assert activity_evidence_kind(row) is EvidenceKind.clock_only


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("tool_receipt", EvidenceKind.observed),
        ("user_stated", EvidenceKind.user_confirmed),
        ("simulated", EvidenceKind.clock_only),
    ],
)
async def test_origin_decides_kind_when_not_declared(
    logged_in: TestEnv, origin: str, expected: EvidenceKind  # noqa: ARG001
) -> None:
    """不显式声明时按 ``origin`` 结构化列判（不改调用点语义）。"""
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading", origin=origin)
        await svc.transition(act.activity_id, "running")
        view = await svc.transition(
            act.activity_id, "completed", completion_evidence="回执已收到"
        )
    assert view.evidence_kind == expected.value


async def test_unknown_declared_kind_is_rejected(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        svc = ActivityService(session, actor_id=ACTOR)
        act = await svc.propose(kind="reading")
        await svc.transition(act.activity_id, "running")
        with pytest.raises(Exception, match="证据种类"):
            await svc.transition(
                act.activity_id,
                "completed",
                completion_evidence="随便写",
                evidence_kind="made_up",
            )


# ---------------------------------------------------------------- ① 写入侧拒绝


async def test_clock_only_activity_not_recorded(logged_in: TestEnv) -> None:
    """① ``record_completed_activity`` 拒绝 clock_only 活动。"""
    activity_id = await _legacy_completed_activity()
    async with session_scope() as session:
        view = await SelfTimelineService(session, actor_id=ACTOR).record_completed_activity(
            activity_id
        )
    assert view is None, "clock_only 证据被写成了自我经历"
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(SelfExperienceRow).where(
                        SelfExperienceRow.dedupe_key == dedupe_key_for(activity_id)
                    )
                )
            ).scalars()
        )
    assert rows == [], "被拒绝的活动仍然留下了经历行"


# ---------------------------------------------------------------- ② backfill


async def test_backfill_skips_clock_only(logged_in: TestEnv) -> None:
    """② ``backfill`` 不补 clock_only 活动（也不会删掉历史行）。"""
    legacy_id = await _legacy_completed_activity()
    observed_id, _ = await _seeded_experience(
        evidence_kind=EvidenceKind.observed, summary="读完了第二章"
    )
    # 把 observed 那条的派生行删掉，让 backfill 有事可做
    async with session_scope() as session:
        row = (
            await session.execute(
                select(SelfExperienceRow).where(
                    SelfExperienceRow.dedupe_key == dedupe_key_for(observed_id)
                )
            )
        ).scalar_one()
        await session.delete(row)

    async with session_scope() as session:
        created = await SelfTimelineService(session, actor_id=ACTOR).backfill()
    assert created == 1, f"backfill 补了 {created} 条（只该补 observed 那一条）"

    async with session_scope() as session:
        legacy_rows = list(
            (
                await session.execute(
                    select(SelfExperienceRow).where(
                        SelfExperienceRow.dedupe_key == dedupe_key_for(legacy_id)
                    )
                )
            ).scalars()
        )
    assert legacy_rows == [], "backfill 把 clock_only 活动补成了经历"


# ---------------------------------------------------------------- ③ 大纲摘要


async def test_experience_summary_hides_clock_only(logged_in: TestEnv) -> None:
    """③ ``_experience_summary`` 读不到 clock_only 经历，读得到合法经历。"""
    legacy_id = await _legacy_completed_activity()
    await _legacy_experience(legacy_id, summary="读书：计划时段已结束")
    _activity_id, _ = await _seeded_experience(
        evidence_kind=EvidenceKind.observed, summary="读完了第二章"
    )

    async with session_scope() as session:
        summaries = await _experience_summary(session, actor_id=ACTOR, now=datetime.now(UTC))

    assert any("读完了第二章" in item for item in summaries), summaries
    assert not any("计划时段已结束" in item for item in summaries), summaries


# ---------------------------------------------------------------- ④ 控制台事实层


async def test_dashboard_timeline_hides_clock_only(logged_in: TestEnv) -> None:
    """④ dashboard 的 ``timeline`` 片段（控制台事实层）读不到 clock_only。"""
    legacy_id = await _legacy_completed_activity()
    await _legacy_experience(legacy_id, summary="读书：计划时段已结束")
    _activity_id, _ = await _seeded_experience(
        evidence_kind=EvidenceKind.observed, summary="读完了第二章"
    )

    body = (await logged_in.client.get("/v1/dashboard")).json()
    section = body["sections"]["timeline"]
    assert section["status"] == "ok", section
    summaries = [item["summary"] for item in section["data"]["items"]]
    assert any("读完了第二章" in item for item in summaries), summaries
    assert not any("计划时段已结束" in item for item in summaries), summaries


# ---------------------------------------------------------------- 正向：不是一刀切


@pytest.mark.parametrize(
    "kind", [EvidenceKind.observed, EvidenceKind.user_confirmed]
)
async def test_fact_kinds_readable_everywhere(
    logged_in: TestEnv, kind: EvidenceKind
) -> None:
    """合法证据在四处都读得到（证明过滤不是"一刀切全废"）。"""
    activity_id, experience_id = await _seeded_experience(
        evidence_kind=kind, summary="读完了第二章"
    )

    # 经历行存了证据种类标签（供不存在的 sync 判定与追溯）
    async with session_scope() as session:
        row = await session.get(SelfExperienceRow, experience_id)
        assert row is not None
        assert f"{EVIDENCE_TAG_PREFIX}{kind.value}" in list(row.source_event_ids or [])
        assert experience_evidence_kind(row) is kind

    # 自我时间线 recent / today
    async with session_scope() as session:
        recent = await SelfTimelineService(session, actor_id=ACTOR).recent(days=1, limit=5)
    assert [item.experience_id for item in recent] == [experience_id]

    # 大纲摘要
    async with session_scope() as session:
        summaries = await _experience_summary(session, actor_id=ACTOR, now=datetime.now(UTC))
    assert any("读完了第二章" in item for item in summaries), summaries

    # 控制台事实层
    body = (await logged_in.client.get("/v1/dashboard")).json()
    items = body["sections"]["timeline"]["data"]["items"]
    assert [item["experience_id"] for item in items] == [experience_id]

    # 统一过滤函数（含回查来源活动）
    async with session_scope() as session:
        rows = list((await session.execute(select(SelfExperienceRow))).scalars())
        eligible = await fact_eligible_experiences(session, rows)
    assert [row.id for row in eligible] == [experience_id]


# ---------------------------------------------------------------- 来源复核


async def test_row_tag_alone_is_not_enough(logged_in: TestEnv) -> None:
    """行自己声称 observed 但来源活动是 clock_only → 读侧仍排除（回查来源）。

    这条堵的是"只读派生行、不复核来源"的洞：派生行可以被写错，
    来源活动才是证据性质的权威。
    """
    activity_id = await _legacy_completed_activity()  # stored evidence 无前缀 → clock_only
    experience_id = await _legacy_experience(activity_id, summary="读书：我在读")
    # 事后把标签改成 observed（模拟派生行写错 / 被人手动改过）
    async with session_scope() as session:
        row = await session.get(SelfExperienceRow, experience_id)
        assert row is not None
        row.source_event_ids = [
            f"activity:{activity_id}",
            f"{EVIDENCE_TAG_PREFIX}{EvidenceKind.observed.value}",
        ]
    async with session_scope() as session:
        rows = list((await session.execute(select(SelfExperienceRow))).scalars())
        eligible = await fact_eligible_experiences(session, rows)
    assert eligible == [], "来源活动是 clock_only，读侧却放行了派生行"


async def test_history_rows_are_kept_not_deleted(logged_in: TestEnv) -> None:
    """历史记录保留：读侧排除，但行还在（这是读侧策略，不是数据清洗）。"""
    legacy_id = await _legacy_completed_activity()
    experience_id = await _legacy_experience(legacy_id)

    async with session_scope() as session:
        recent = await SelfTimelineService(session, actor_id=ACTOR).recent(days=1, limit=5)
    assert recent == []

    async with session_scope() as session:
        assert await session.get(SelfExperienceRow, experience_id) is not None
        assert await session.get(ActivityRow, legacy_id) is not None
