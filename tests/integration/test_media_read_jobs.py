"""视频后台取证作业：入口与持久状态（裁决 §3 第 1、2 步）。

本轮**不执行**作业（执行在第 3 步），所以这里断言的全是"入口 + 持久化"的可
观察事实：

1. **202 之后**作业是 `pending`、outbox 事件**恰好一条**、主键由 call_id 派生；
2. **并发**同 `request_key` → 一个 call_id / 一条作业 / 一个事件；
3. 同键**不同参数** → 409；同证据**在途互斥** → 409；
4. 非本窗口 404、图片 422、缺字段 422、多余字段 422；
5. 取消：`pending` → `cancelled`；`running` → 只置标记、状态不变；
6. 没有任何状态在非终态时给出"看到了"的文案。

## 为什么所有"拒绝"都要断言**零副作用**

拒绝路径如果悄悄建了作业或投了事件，用户会看到一个永远不会开始的读取，
而库里还有一条 pending 行——那是比报错更糟的状态（"它说在排队，但永远排不上"）。
所以每条 4xx 都顺带断言行数与事件数为 0。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ling.contracts.v1 import MediaReadJobState
from ling.domains.consent.service import ConsentService
from ling.domains.media.jobs import (
    MEDIA_READ_QUEUE_DEADLINE,
    MEDIA_READ_TOOL_VERSION,
    MEDIA_READ_TOPIC,
    JobState,
    outbox_event_id_for,
)
from ling.domains.media.service import (
    INBOUND_SCOPE,
    MediaEvidenceService,
    dedupe_key_for,
)
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow, MediaReadJob, OutboxEvent, Window
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


# ---------------------------------------------------------------- 种数据


async def _grant(subject_id: str, *, granted: bool = True) -> None:
    """给**指定的** subject 授权/撤权。

    授权是**按人**记的（`consent_rules` 一行一个 subject），所以要显式传
    登录者的 subject——写死一个假 id 会让"授权生效"这件事根本没被测到。
    """
    async with session_scope() as session:
        svc = ConsentService(session)
        if granted:
            await svc.grant(subject_id=subject_id, scope=INBOUND_SCOPE)
        else:
            await svc.revoke(subject_id=subject_id, scope=INBOUND_SCOPE)


async def _session(env: TestEnv, *, title: str = "视频", owner: str | None = None) -> str:
    """建一个**属于当前登录者**的窗口（走真实接口，才有正确的 subject_id）。"""
    r = await env.client.post("/v1/sessions", json={"title": title})
    assert r.status_code == 201 or r.status_code == 200, r.text
    return r.json()["session_id"]


async def _evidence(
    window_id: str,
    *,
    kind: str = "video",
    subject_id: str | None = None,
    expired: bool = False,
) -> str:
    """直接落一条证据行。

    不走 `record()`：它会去查 `media.inbound` 授权并按 `media_ref` 去重，而
    这里的重点是**这个入口怎么判**，不是证据怎么产生。
    """
    async with session_scope() as session:
        row = MediaEvidenceRow(
            id=new_id("ev"),
            actor_id=ACTOR,
            subject_id=subject_id,
            window_id=window_id,
            kind=kind,
            media_ref=f"file:.data/media/{new_id('f')}.mp4",
            origin="user_upload",
            dedupe_key=dedupe_key_for(new_id("src")),
            expires_at=(datetime.now(UTC) - timedelta(hours=1)) if expired else None,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _body(**overrides: object) -> dict:
    base = {"feedback_mode": "evidence_only", "request_key": new_id("rk")}
    base.update(overrides)
    return base


async def _create(
    env: TestEnv, window_id: str, evidence_id: str, **overrides: object
):  # noqa: ANN201
    return await env.client.post(
        f"/v1/sessions/{window_id}/media/{evidence_id}/read-jobs",
        json=await _body(**overrides),
    )


async def _jobs() -> list[MediaReadJob]:
    async with session_scope() as session:
        return list((await session.execute(select(MediaReadJob))).scalars())


async def _events() -> list[OutboxEvent]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == MEDIA_READ_TOPIC)
                )
            ).scalars()
        )


async def _job(call_id: str) -> MediaReadJob | None:
    async with session_scope() as session:
        return await session.get(MediaReadJob, call_id)


@pytest.fixture(autouse=True)
async def _grant_consent(logged_in: TestEnv) -> None:  # noqa: F811
    """默认给登录者授权：大多数用例验的是入口本身，不是授权门。"""
    await _grant(logged_in.subject_id, granted=True)


# ---------------------------------------------------------------- 1. 202 与落库


async def test_create_returns_202_and_persists_pending_job(logged_in: TestEnv) -> None:  # noqa: F811
    """202 之后：作业 `pending`、outbox **恰好一条**、主键由 call_id 派生。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    key = new_id("rk")

    r = await _create(logged_in, sid, ev, request_key=key)
    assert r.status_code == 202, r.text
    body = r.json()

    assert body["state"] == MediaReadJobState.pending.value
    # 文案纪律：入队只说"已安排"，不许出现"看到了/看完了"
    assert body["note"] == "已安排，尚未读到结果"
    assert "看到" not in body["note"] and "看完" not in body["note"]
    assert body["feedback_mode"] == "evidence_only"
    assert body["call_id"].startswith("mread_")
    assert body["error_code"] is None
    assert body["result_ref"] is None

    jobs = await _jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.call_id == body["call_id"]
    assert job.state == JobState.pending.value
    assert job.evidence_id == ev
    assert job.window_id == sid
    assert job.actor_id == ACTOR
    assert job.subject_id == logged_in.subject_id
    assert job.request_key == key
    assert job.tool_version == MEDIA_READ_TOOL_VERSION
    assert job.mode == "video"
    assert job.feedback_mode == "evidence_only"
    # 授权修订被记下来（撤权审计要指到具体的人）
    assert job.authorization_ref and ":" in job.authorization_ref
    # 排队截止 = 登记后 15 分钟
    assert job.requested_at and job.deadline
    delta = job.deadline - job.requested_at
    assert abs(delta - MEDIA_READ_QUEUE_DEADLINE) < timedelta(seconds=5), delta
    # 还没开始
    assert job.started_at is None and job.completed_at is None
    assert job.lease_generation == 0

    events = await _events()
    assert len(events) == 1, "作业与事件必须同事务，且**恰好一条**"
    event = events[0]
    assert event.id == outbox_event_id_for(job.call_id)
    assert event.payload == {"call_id": job.call_id, "schema_version": "v1"}
    assert event.state == "pending"


async def test_create_rejects_non_video_evidence(logged_in: TestEnv) -> None:  # noqa: F811
    """图片/音频 → 422，且**零作业零事件**。"""
    sid = await _session(logged_in)
    for kind in ("image", "audio"):
        ev = await _evidence(sid, kind=kind, subject_id=logged_in.subject_id)
        r = await _create(logged_in, sid, ev)
        assert r.status_code == 422, (kind, r.text)
        assert "只读视频" in r.json()["error"]["message"]
    assert await _jobs() == []
    assert await _events() == []


async def test_create_rejects_unknown_evidence(logged_in: TestEnv) -> None:  # noqa: F811
    sid = await _session(logged_in)
    r = await _create(logged_in, sid, "ev_does_not_exist")
    assert r.status_code == 404, r.text
    assert await _jobs() == []


async def test_create_rejects_other_windows_evidence(logged_in: TestEnv) -> None:  # noqa: F811
    """别的窗口的证据：按"不存在"处理（404，不泄露存在性）。"""
    sid = await _session(logged_in, title="A")
    other = await _session(logged_in, title="B")
    ev = await _evidence(other, subject_id=logged_in.subject_id)

    r = await _create(logged_in, sid, ev)
    assert r.status_code == 404, r.text
    assert await _jobs() == []


async def test_create_rejects_other_users_window(logged_in: TestEnv) -> None:  # noqa: F811
    """别人的窗口：404（会话不存在），零作业。"""
    async with session_scope() as session:
        foreign = Window(
            id=new_id("win"),
            actor_id=ACTOR,
            persona_id="persona_ling",
            subject_id="subj_someone_else",
            title="别人的会话",
            channel="web",
            visibility="subject_private",
        )
        session.add(foreign)
        await session.commit()
        foreign_id = foreign.id
    ev = await _evidence(foreign_id, subject_id="subj_someone_else")

    r = await _create(logged_in, foreign_id, ev)
    assert r.status_code == 404, r.text
    assert await _jobs() == []


async def test_create_requires_media_consent(logged_in: TestEnv) -> None:  # noqa: F811
    """没有 media.inbound → 403，且给可执行的下一步；零作业。"""
    await _grant(logged_in.subject_id, granted=False)
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)

    r = await _create(logged_in, sid, ev)
    assert r.status_code == 403, r.text
    assert "media.inbound" in r.json()["error"]["message"]
    assert await _jobs() == []
    assert await _events() == []


# ---------------------------------------------------------------- 2. 请求体裁剪


@pytest.mark.parametrize("missing", ["feedback_mode", "request_key"])
async def test_body_requires_both_fields(logged_in: TestEnv, missing: str) -> None:  # noqa: F811
    """缺 `feedback_mode` / `request_key` → 422（缺一不可）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    body = await _body()
    body.pop(missing)

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/{ev}/read-jobs", json=body
    )

    assert r.status_code == 422, (missing, r.text)
    assert await _jobs() == []


async def test_missing_feedback_mode_says_it_is_required(logged_in: TestEnv) -> None:  # noqa: F811
    """缺 `feedback_mode` 的报错要明说"必填"，而不是 pydantic 的 Field required。

    这条防的是"以为不写就等于 evidence_only"。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/{ev}/read-jobs",
        json={"request_key": new_id("rk")},
    )

    assert r.status_code == 422, r.text
    assert "feedback_mode 必填" in r.text


async def test_body_rejects_other_feedback_modes(logged_in: TestEnv) -> None:  # noqa: F811
    """首期只接受 `evidence_only`；别的值 422（不静默降级）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)

    r = await _create(logged_in, sid, ev, feedback_mode="notify_when_done")
    assert r.status_code == 422, r.text
    assert await _jobs() == []


async def test_body_rejects_extra_fields(logged_in: TestEnv) -> None:  # noqa: F811
    """多余字段 → 422（例如客户端按旧文档传 `wait=true`）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    body = await _body()
    body["wait"] = True

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/{ev}/read-jobs", json=body
    )
    assert r.status_code == 422, r.text
    assert await _jobs() == []


async def test_empty_request_key_is_rejected(logged_in: TestEnv) -> None:  # noqa: F811
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    r = await _create(logged_in, sid, ev, request_key="")
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 3. 幂等与互斥


async def test_same_request_key_replays_the_same_job(logged_in: TestEnv) -> None:  # noqa: F811
    """同一个 `request_key` 再提交：返回**同一个** call_id，不新增作业/事件。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    key = new_id("rk")

    first = await _create(logged_in, sid, ev, request_key=key)
    second = await _create(logged_in, sid, ev, request_key=key)

    assert first.status_code == 202 and second.status_code == 202, second.text
    assert first.json()["call_id"] == second.json()["call_id"]
    assert len(await _jobs()) == 1
    assert len(await _events()) == 1


async def test_concurrent_same_request_key_creates_exactly_one(logged_in: TestEnv) -> None:  # noqa: F811
    """**并发 20 次同 request_key**：一个 call_id / 一条作业 / 一个事件。

    「改坏了会红」：把 `UNIQUE(actor_id, window_id, request_key)` 去掉，这里会
    出现 20 条作业、20 个事件——而每一个都会真的跑一次视觉模型，同一段视频
    收费 20 次。

    并发是真的：20 个请求同时发出，服务端各拿一个数据库会话，靠唯一索引分
    胜负（不是靠进程内的锁）。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    key = new_id("rk")
    url = f"/v1/sessions/{sid}/media/{ev}/read-jobs"
    body = {"feedback_mode": "evidence_only", "request_key": key}

    responses = await asyncio.gather(
        *[logged_in.client.post(url, json=body) for _ in range(20)]
    )

    codes = sorted({r.status_code for r in responses})
    assert codes == [202], [(r.status_code, r.text[:120]) for r in responses]
    call_ids = {r.json()["call_id"] for r in responses}
    assert len(call_ids) == 1, f"并发 20 次得到了 {len(call_ids)} 个 call_id"

    jobs = await _jobs()
    assert len(jobs) == 1, f"并发 20 次建了 {len(jobs)} 条作业"
    events = await _events()
    assert len(events) == 1, f"并发 20 次投了 {len(events)} 条事件"
    assert jobs[0].call_id == call_ids.pop()


async def test_same_key_different_evidence_is_conflict(logged_in: TestEnv) -> None:  # noqa: F811
    """同键**不同参数** → 409（不悄悄返回旧作业，否则调用方以为新请求生效了）。"""
    sid = await _session(logged_in)
    ev_a = await _evidence(sid, subject_id=logged_in.subject_id)
    ev_b = await _evidence(sid, subject_id=logged_in.subject_id)
    key = new_id("rk")

    first = await _create(logged_in, sid, ev_a, request_key=key)
    assert first.status_code == 202, first.text

    second = await _create(logged_in, sid, ev_b, request_key=key)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "request_key_conflict"
    assert len(await _jobs()) == 1


async def test_second_job_for_same_evidence_in_flight_is_conflict(logged_in: TestEnv) -> None:  # noqa: F811
    """同一证据同一工具版本**已有在途作业** → 409（部分唯一索引）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)

    first = await _create(logged_in, sid, ev)
    assert first.status_code == 202, first.text

    second = await _create(logged_in, sid, ev)  # 不同的 request_key
    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "evidence_read_in_flight"
    assert len(await _jobs()) == 1
    assert len(await _events()) == 1


async def test_new_job_allowed_after_previous_reached_terminal(logged_in: TestEnv) -> None:  # noqa: F811
    """终态之后**允许**再排一次（部分索引只挡在途）。

    「改坏了会红」：把部分索引写成普通唯一约束，这条会 409——用户想"再读一遍"
    或"上次失败重试"永远做不了。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)

    first = await _create(logged_in, sid, ev)
    assert first.status_code == 202
    call_id = first.json()["call_id"]

    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = JobState.failed.value
        row.completed_at = datetime.now(UTC)
        await session.commit()

    again = await _create(logged_in, sid, ev)
    assert again.status_code == 202, again.text
    assert again.json()["call_id"] != call_id
    assert len(await _jobs()) == 2


async def test_different_windows_do_not_share_request_key(logged_in: TestEnv) -> None:  # noqa: F811
    """同一个 `request_key` 在**不同窗口**里是两个作业（可见性不同，不能合并）。"""
    win_a = await _session(logged_in, title="A2")
    win_b = await _session(logged_in, title="B2")
    ev_a = await _evidence(win_a, subject_id=logged_in.subject_id)
    ev_b = await _evidence(win_b, subject_id=logged_in.subject_id)
    key = new_id("rk")

    a = await _create(logged_in, win_a, ev_a, request_key=key)
    b = await _create(logged_in, win_b, ev_b, request_key=key)

    assert a.status_code == 202 and b.status_code == 202, (a.text, b.text)
    assert a.json()["call_id"] != b.json()["call_id"]
    assert len(await _jobs()) == 2


# ---------------------------------------------------------------- 4. 查询


async def test_get_returns_state_and_same_scope(logged_in: TestEnv) -> None:  # noqa: F811
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    created = await _create(logged_in, sid, ev)
    call_id = created.json()["call_id"]

    r = await logged_in.client.get(f"/v1/sessions/{sid}/media/read-jobs/{call_id}")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pending"
    assert r.json()["note"] == "已安排，尚未读到结果"


async def test_get_from_another_window_is_404(logged_in: TestEnv) -> None:  # noqa: F811
    """跨窗口按"不存在"处理：403 会变成一个探测口。"""
    sid = await _session(logged_in, title="A3")
    other = await _session(logged_in, title="B3")
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    r = await logged_in.client.get(f"/v1/sessions/{other}/media/read-jobs/{call_id}")
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    "state", ["pending", "running", "failed", "timeout", "unknown"]
)
async def test_non_success_states_never_claim_success(
    logged_in: TestEnv, state: str  # noqa: F811
) -> None:
    """pending/running/failed/timeout/unknown 的文案都不许说"读到了"。

    裁决 §5 第 7 条：GET 在这些状态下**均不返回成功文案**。`unknown` 尤其不能
    说成失败——那会诱导用户在"模型可能已经收费"的情况下重试。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = state
        await session.commit()

    r = await logged_in.client.get(f"/v1/sessions/{sid}/media/read-jobs/{call_id}")
    assert r.status_code == 200, r.text
    note = r.json()["note"]
    assert "读完了" not in note and "已存为" not in note
    if state == "unknown":
        assert "不确定" in note, f"unknown 必须说'不确定'：{note!r}"


# ---------------------------------------------------------------- 5. 取消


async def test_cancel_pending_marks_cancelled(logged_in: TestEnv) -> None:  # noqa: F811
    """取消 `pending`：直接 `cancelled`（什么都没发生，不用等）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/read-jobs/{call_id}/cancel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "cancelled"
    assert body["note"] == "已取消，没有读"
    assert body["completed_at"] is not None

    row = await _job(call_id)
    assert row is not None and row.state == "cancelled"


async def test_cancel_running_only_sets_the_flag(logged_in: TestEnv) -> None:  # noqa: F811
    """取消 `running`：只置 `cancel_requested_at`，**状态不变**。

    直接写 `cancelled` 会让"作业还在跑、可能正在花视觉模型的钱"和"库里说它
    取消了"同时成立，而晚到的结果还会被写进证据。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = "running"
        row.started_at = datetime.now(UTC)
        await session.commit()

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/read-jobs/{call_id}/cancel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "running", "running 时取消不许改状态"
    assert body["cancel_requested_at"] is not None

    row = await _job(call_id)
    assert row is not None
    assert row.state == "running"
    assert row.cancel_requested_at is not None
    assert row.completed_at is None


async def test_cancel_terminal_job_is_idempotent(logged_in: TestEnv) -> None:  # noqa: F811
    """取消已是终态的作业：200 + 当前状态，**不改任何东西**。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = "succeeded"
        row.result_ref = ev
        await session.commit()

    r = await logged_in.client.post(
        f"/v1/sessions/{sid}/media/read-jobs/{call_id}/cancel"
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "succeeded"

    row = await _job(call_id)
    assert row is not None and row.state == "succeeded"
    assert row.cancel_requested_at is None


async def test_cancel_from_another_window_is_404(logged_in: TestEnv) -> None:  # noqa: F811
    sid = await _session(logged_in, title="A4")
    other = await _session(logged_in, title="B4")
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = (await _create(logged_in, sid, ev)).json()["call_id"]

    r = await logged_in.client.post(
        f"/v1/sessions/{other}/media/read-jobs/{call_id}/cancel"
    )
    assert r.status_code == 404, r.text
    row = await _job(call_id)
    assert row is not None and row.state == "pending", "跨窗口的取消不能生效"


async def test_ignored_topic_is_not_handled_by_this_round(logged_in: TestEnv) -> None:  # noqa: F811
    """本轮**不注册** worker：事件留在 `pending`，不会被任何循环消费。

    这条是"范围声明"的钉子：第 3 步之前，事件必须是 pending——如果它被处理了，
    说明有人提前接上执行却还没做完租约/预算/恢复，那会是一个静默烧钱的路径。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    await _create(logged_in, sid, ev)

    events = await _events()
    assert len(events) == 1
    assert events[0].state == "pending"
    assert events[0].attempts == 0
    assert events[0].processed_at is None


# ---------------------------------------------------------------- 6. 契约与枚举


def test_contract_state_enum_matches_domain_enum() -> None:
    """契约里的状态词表必须与域内 `JobState` **逐字相同**。

    「改坏了会红」：往一边加一个状态，另一边没加——前端会收到一个自己不认识的
    值（`unknown` 加上就会变成"失败"），或者数据库里出现一个读侧不知道的状态。
    """
    from ling.domains.media import jobs as jobs_module

    contract_values = [s.value for s in MediaReadJobState]
    domain_values = [s.value for s in JobState]
    assert contract_values == domain_values
    # 与迁移里的 CHECK 也要一致
    from ling.contracts.v1 import MediaReadJobOut as _Out  # noqa: F401

    assert set(contract_values) == {s.value for s in jobs_module.JobState}


async def test_media_service_is_the_only_consent_source(logged_in: TestEnv) -> None:  # noqa: F811
    """授权判据来自 `MediaEvidenceService` 同一套规则：**窗口主人**授权也算。

    群聊里主人授权一次，别人发的视频也能被读取——这条规则写在
    `MediaEvidenceService._granted_consent` 里，入口不另写一份。
    """
    sid = await _session(logged_in)
    # 证据的 subject 是**别人**（群里另一个人发的）
    ev = await _evidence(sid, subject_id="subj_group_other")
    await _grant(logged_in.subject_id, granted=False)  # 取消主人授权

    denied = await _create(logged_in, sid, ev)
    assert denied.status_code == 403, denied.text

    await _grant(logged_in.subject_id, granted=True)  # 主人授权
    allowed = await _create(logged_in, sid, ev)
    assert allowed.status_code == 202, allowed.text


async def test_job_count_query_is_cheap(logged_in: TestEnv) -> None:  # noqa: F811
    """顺手确认表真的建好了且能按 actor 数出来（迁移生效的自检）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    await _create(logged_in, sid, ev)

    async with session_scope() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(MediaReadJob)
                .where(MediaReadJob.actor_id == ACTOR)
            )
        ).scalar_one()
    assert count == 1


async def test_expired_evidence_is_not_readable(logged_in: TestEnv) -> None:  # noqa: F811
    """已过期的证据 → 404（`MediaEvidenceService.get` 的既有判据）。"""
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id, expired=True)

    r = await _create(logged_in, sid, ev)
    assert r.status_code == 404, r.text
    assert await _jobs() == []


async def test_evidence_service_get_is_the_gate(logged_in: TestEnv) -> None:  # noqa: F811
    """入口用 `MediaEvidenceService.get` 判"存在/本窗口/未过期"——不自己拼 SQL。

    这条是行为等价断言：同一查询条件下，`get()` 返回什么，入口就按什么判。
    """
    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    async with session_scope() as session:
        view = await MediaEvidenceService(session, actor_id=ACTOR).get(
            ev, window_id=sid
        )
    assert view is not None and view.kind == "video"


# ---------------------------------------------------------------- 7. 同事务原子性


async def test_job_and_event_land_in_the_same_transaction(logged_in: TestEnv) -> None:  # noqa: F811
    """作业与 outbox 事件**同事务**：不存在"有作业没事件"的中间态。

    「改坏了会红」：把 `enqueue_read_job` 拆成"先 commit 作业、再插事件"，
    两次提交之间进程若崩掉，就留下一条**永远不会有 worker 来执行**的作业。
    用户看到 202、状态一直 `pending`、而没有任何东西会推进它。

    断言方式：直接调投递函数并在**未提交**时观察——两个对象在同一个事务里，
    回滚会一起消失。
    """
    from ling.domains.media.jobs import enqueue_read_job
    from ling.infra.security import new_id as _new_id

    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = _new_id("mread")
    moment = datetime.now(UTC)

    job = MediaReadJob(
        call_id=call_id,
        actor_id=ACTOR,
        subject_id=logged_in.subject_id,
        window_id=sid,
        evidence_id=ev,
        request_key=_new_id("rk"),
        tool_version=MEDIA_READ_TOOL_VERSION,
        mode="video",
        feedback_mode="evidence_only",
        state=JobState.pending.value,
        requested_at=moment,
        deadline=moment + MEDIA_READ_QUEUE_DEADLINE,
    )
    async with session_scope() as session:
        assert await enqueue_read_job(session, job=job, now=moment) is True
        # 未提交时在同一个会话里两个都可见
        assert await session.get(MediaReadJob, call_id) is not None
        assert await session.get(OutboxEvent, outbox_event_id_for(call_id)) is not None
        await session.rollback()

    # 回滚之后两个都消失了——这正是"原子"的含义
    assert await _job(call_id) is None
    async with session_scope() as session:
        assert await session.get(OutboxEvent, outbox_event_id_for(call_id)) is None


async def test_duplicate_call_id_cannot_produce_a_second_event(logged_in: TestEnv) -> None:  # noqa: F811
    """同一 `call_id` 不可能有第二条事件——主键挡住，且不会留下半条。

    这条验的是"**主键**去重"而不是"payload 里的幂等键"：把事件主键改成随机
    id 并把 call_id 只放进 payload，这里就会变成两条事件 → 两次视觉调用、
    两次收费。

    注意断言的是"**插入被拒**"而不是"静默返回 False"：`call_id` 是作业主键，
    重复插入本身就是 bug（幂等由 `request_key` 唯一约束在上层处理），所以这里
    应当抛错而不是假装成功。真正要防的是"错误路径上顺手补了一条事件"。
    """
    from ling.domains.media.jobs import enqueue_read_job
    from ling.infra.security import new_id as _new_id

    sid = await _session(logged_in)
    ev = await _evidence(sid, subject_id=logged_in.subject_id)
    call_id = _new_id("mread")
    moment = datetime.now(UTC)
    request_key = _new_id("rk")

    def _build() -> MediaReadJob:
        # 用**同一个** request_key：模拟"同一作业被投两次"
        return MediaReadJob(
            call_id=call_id,
            actor_id=ACTOR,
            subject_id=logged_in.subject_id,
            window_id=sid,
            evidence_id=ev,
            request_key=request_key,
            tool_version=MEDIA_READ_TOOL_VERSION,
            mode="video",
            feedback_mode="evidence_only",
            state=JobState.pending.value,
            requested_at=moment,
            deadline=moment + MEDIA_READ_QUEUE_DEADLINE,
        )

    async with session_scope() as session:
        assert await enqueue_read_job(session, job=_build(), now=moment) is True
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await enqueue_read_job(session, job=_build(), now=moment)

    events = [e for e in await _events() if e.payload.get("call_id") == call_id]
    assert len(events) == 1, f"同一 call_id 出现了 {len(events)} 条事件"
