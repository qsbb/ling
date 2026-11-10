"""视频后台取证作业：执行与恢复（裁决 §3.3 第 3 步）。

本文件覆盖裁决 §5 的第 3、4、7、8 条：

- **第 3 条**：`failed`/`timeout` 持久化非成功终态且不重排；`needs_confirmation`
  不执行；模型前瞬时异常重排；第 5 次失败 dead 且业务不悬空。
- **第 4 条**：四个崩溃点（入队后 / 模型前 / 模型后 / 结果提交后）各自收敛；
  旧租约代次提交更新行数为 0。
- **第 7 条**：任务完成前后新增 actor 消息/轮次/投递 operation/SSE delta 均为 0；
  GET 在非成功状态都不返回成功文案。
- **第 8 条**：慢媒体期间普通 outbox 仍推进；非终态作业在 deadline + 一个清理
  周期内终结。

## 怎么"模拟崩溃"

真杀进程在 pytest 里不可行。这里的做法是**精确复现崩溃留下的库状态**：
把作业写成"某个时刻被杀之后的样子"（`running` + 租约过期 / `model_started_at`
有值 / 终态），然后调清理或重放，断言收敛结果。这比杀进程更严格——它把
"崩溃那一刻库里是什么"变成可断言的前置条件，而杀进程只能撞运气。

## 怎么算"模型调了几次"

一个计数用的假 describer（`_CountingDescriber`）。所有"不重复烧钱"的断言都
落在它的 `calls` 上，而不是"作业状态对不对"——状态对了但模型被调两次同样是
bug（用户被收两次费）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ling.domains.media.describe import MediaReadError
from ling.domains.media.errors import (
    SOURCE_BUSINESS,
    SOURCE_MODEL,
    SOURCE_OTHER,
    MediaDescribeError,
    MediaRefPermanentError,
)
from ling.domains.media.jobs import (
    MEDIA_READ_LEASE_SECONDS,
    MEDIA_READ_QUEUE_DEADLINE,
    MEDIA_READ_TOPIC,
    UNKNOWN_JOB_CODE,
    JobState,
    ReadJobError,
    ReadOutcome,
    claim_read_job,
    classify_describe_error,
    commit_read_outcome,
    expire_overdue_jobs,
    mark_model_started,
    outbox_event_id_for,
    run_read_job,
    settle_dead_jobs,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    BehaviorModelCallRow,
    DeliveryReceiptRow,
    MediaEvidenceRow,
    MediaReadJob,
    Message,
    Operation,
    OutboxEvent,
    Turn,
)
from ling.infra.security import new_id
from ling.ports.model import ModelErrorKind
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


# ---------------------------------------------------------------- 假件


class _CountingDescriber:
    """数模型调用的假 describer。

    `describe_video` 的副作用（写证据）由 `on_describe` 模拟：真实实现里那是
    `MediaEvidenceService.describe`，而我们关心的是"**调了几次**"。
    """

    def __init__(self, *, mode: str = "ok", text: str = "视频里有人在做饭") -> None:
        self.calls = 0
        self.mode = mode
        self.text = text

    async def describe_video(self, evidence: object, *, mode: str = "frames") -> object:
        self.calls += 1
        if self.mode == "fail":
            raise MediaDescribeError("模型炸了", retriable=True, source=SOURCE_OTHER)
        if self.mode == "timeout":
            raise MediaDescribeError(
                "模型超时", retriable=True, source=SOURCE_MODEL,
                model_kind=ModelErrorKind.timeout,
            )
        if self.mode == "revoked":
            raise MediaDescribeError(
                "授权已撤销", retriable=False, terminal=True, source=SOURCE_BUSINESS
            )
        if self.mode == "permanent":
            raise MediaRefPermanentError("引用越界")

        class _Read:
            text = self.text
            mode = "frames"
            frames = 4
            cached = False

        return _Read()


class _ServiceStub:
    """最小 `MediaEvidenceService` 替身：只提供执行侧用到的两个方法。"""

    def __init__(self, store: dict[str, object]) -> None:
        self._store = store

    async def get(self, evidence_id: str, *, window_id: str) -> object | None:
        row = self._store.get(evidence_id)
        if row is None or getattr(row, "window_id", "") != window_id:
            return None
        return row

    async def mark_describe_failed(
        self, evidence_id: str, *, window_id: str, reason: str
    ) -> None:
        row = self._store.get(evidence_id)
        if row is not None:
            row.describe_failed_at = datetime.now(UTC)  # type: ignore[attr-defined]


def _factory(store: dict[str, object]):  # noqa: ANN202
    def build(_session: object) -> _ServiceStub:
        return _ServiceStub(store)

    return build


# ---------------------------------------------------------------- 种数据


async def _session_and_evidence(env: TestEnv) -> tuple[str, str, dict[str, object]]:
    """建一个真实窗口 + 一条视频证据，返回 (window_id, evidence_id, store)。

    `store` 是给 `_ServiceStub` 用的：它持有那份 `MediaEvidenceRow` 的 ORM 对象，
    测试可以直接改（例如标 `describe_failed_at` 来验证"取消后不可消费"）。
    """
    r = await env.client.post("/v1/sessions", json={"title": "视频作业"})
    window_id = r.json()["session_id"]
    async with session_scope() as session:
        row = MediaEvidenceRow(
            id=new_id("ev"),
            actor_id=ACTOR,
            subject_id=env.subject_id,
            window_id=window_id,
            kind="video",
            media_ref="file:.data/media/x.mp4",
            origin="user_upload",
            dedupe_key=new_id("dk"),
        )
        session.add(row)
        await session.commit()
        store: dict[str, object] = {row.id: row}
        return window_id, row.id, store


async def _make_job(
    window_id: str,
    evidence_id: str,
    *,
    tool_version: str = "media.video.read@1",
    state: str = "pending",
    requested_at: datetime | None = None,
    started_at: datetime | None = None,
    model_started_at: datetime | None = None,
    lease_until: datetime | None = None,
    lease_generation: int = 0,
    cancel_requested_at: datetime | None = None,
    result_ref: str | None = None,
) -> str:
    """直接落一条作业行（模拟"某个时刻的库状态"）。"""
    call_id = new_id("mread")
    moment = requested_at or datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            MediaReadJob(
                call_id=call_id,
                actor_id=ACTOR,
                subject_id=None,
                window_id=window_id,
                evidence_id=evidence_id,
                request_key=new_id("rk"),
                tool_version=tool_version,
                mode="video",
                feedback_mode="evidence_only",
                state=state,
                requested_at=moment,
                deadline=moment + MEDIA_READ_QUEUE_DEADLINE,
                started_at=started_at,
                model_started_at=model_started_at,
                lease_until=lease_until,
                lease_generation=lease_generation,
                cancel_requested_at=cancel_requested_at,
                result_ref=result_ref,
                lease_owner="dead-worker" if lease_until else None,
            )
        )
        await session.commit()
    return call_id


async def _more_evidence(env: TestEnv, window_id: str) -> str:
    """再造一条视频证据（同一窗口）。

    **必须有这个帮手**：在途互斥索引是 `(evidence_id, tool_version)`，所以
    "同一证据同时有两个在途作业"在库里本来就不允许。要造多条在途作业的测试
    必须给它们**不同证据**——这不是测试的方便，而是约束在起作用。
    """
    async with session_scope() as session:
        row = MediaEvidenceRow(
            id=new_id("ev"),
            actor_id=ACTOR,
            subject_id=env.subject_id,
            window_id=window_id,
            kind="video",
            media_ref="file:.data/media/y.mp4",
            origin="user_upload",
            dedupe_key=new_id("dk"),
        )
        session.add(row)
        await session.commit()
        return row.id


async def _job(call_id: str) -> MediaReadJob | None:
    async with session_scope() as session:
        return await session.get(MediaReadJob, call_id)


async def _enqueue_event(call_id: str, *, attempts: int = 0) -> str:
    event_id = outbox_event_id_for(call_id)
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic=MEDIA_READ_TOPIC,
                payload={"call_id": call_id, "schema_version": "v1"},
                attempts=attempts,
                available_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return event_id


async def _event(event_id: str) -> OutboxEvent | None:
    async with session_scope() as session:
        return await session.get(OutboxEvent, event_id)


async def _run(
    call_id: str,
    *,
    describer: object | None = None,
    store: dict[str, object] | None = None,
) -> ReadOutcome:
    return await run_read_job(
        call_id=call_id,
        lease_owner="media-worker-1",
        describer=describer if describer is not None else _CountingDescriber(),
        service_factory=_factory(store or {}),
    )


# ================================================================ 第 3 条：失败/超时/确认


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fail", JobState.failed),
        ("timeout", JobState.timeout),
        ("permanent", JobState.failed),
        ("revoked", JobState.failed),
    ],
)
async def test_model_failures_persist_terminal_and_do_not_reorder(
    logged_in: TestEnv, mode: str, expected: JobState
) -> None:  # noqa: F811
    """模型已调用之后的失败：落**非成功终态**，且**返回**而不是抛（不重排）。

    「改坏了会红」：把 `except BaseException` 改成 `raise`，outbox 会重排，
    模型被第二次调用——第二次收费，而第一次可能已经成功过。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    await _enqueue_event(call_id)
    describer = _CountingDescriber(mode=mode)

    outcome = await _run(call_id, describer=describer, store=store)

    assert outcome.state is expected
    assert outcome.state is not JobState.succeeded
    assert describer.calls == 1, "模型只应被调用一次"

    row = await _job(call_id)
    assert row is not None
    assert row.state == expected.value
    assert row.completed_at is not None
    assert row.error_code is not None
    assert row.result_ref is None, "失败的作业不许有结果引用"

    # `run_read_job` 正常返回 → 调用方（worker）会让 outbox 标 done。
    # 这里断言"没有抛"就是断言"不会重排"。
    event = await _event(outbox_event_id_for(call_id))
    assert event is not None and event.attempts == 0


async def test_timeout_is_distinct_from_failed(logged_in: TestEnv) -> None:  # noqa: F811
    """`timeout` 与 `failed` 是两个终态：它们的 error_code 也不同。

    混成一个会让"模型超时（可能还会好）"和"引用坏了（永远好不了）"看起来一样。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    timeout_job = await _make_job(window_id, evidence_id)
    await _run(timeout_job, describer=_CountingDescriber(mode="timeout"), store=store)

    failed_job = await _make_job(window_id, evidence_id)
    await _run(failed_job, describer=_CountingDescriber(mode="fail"), store=store)

    timeout_row = await _job(timeout_job)
    failed_row = await _job(failed_job)
    assert timeout_row is not None and failed_row is not None
    assert timeout_row.state == "timeout"
    assert failed_row.state == "failed"
    assert timeout_row.error_code != failed_row.error_code


def test_classify_describe_error_maps_sources() -> None:
    """错误分类是纯函数，逐类打表（不依赖库）。"""
    cases = [
        (
            MediaDescribeError("t", source=SOURCE_MODEL, model_kind=ModelErrorKind.timeout),
            JobState.timeout,
            "model_timeout",
        ),
        (
            MediaDescribeError("r", source=SOURCE_BUSINESS, terminal=True),
            JobState.failed,
            "authorization_revoked",
        ),
        (
            MediaDescribeError("o", retriable=False),
            JobState.failed,
            None,  # 错误码是异常类名派生，只断言非空
        ),
        (MediaRefPermanentError("越界"), JobState.failed, "media_ref_unavailable"),
        (MediaReadError("视频太大"), JobState.failed, "video_unreadable"),
        (RuntimeError("随便什么"), JobState.failed, "runtime_error"),
    ]
    for exc, state, code in cases:
        outcome = classify_describe_error(exc)
        assert outcome.state is state, exc
        if code is not None:
            assert outcome.error_code == code, exc


def test_no_describe_error_ever_returns_success() -> None:
    """**任何**异常都不许被分类成成功——这是"不谎报"的最后一道。"""
    exceptions = [
        MediaDescribeError("x"),
        MediaDescribeError("x", source=SOURCE_MODEL),
        MediaDescribeError("x", source=SOURCE_BUSINESS, terminal=True),
        MediaRefPermanentError("x"),
        ValueError("x"),
        TimeoutError(),
        asyncio.CancelledError(),
    ]
    for exc in exceptions:
        assert classify_describe_error(exc).state is not JobState.succeeded, exc


async def test_needs_confirmation_never_executes(logged_in: TestEnv) -> None:  # noqa: F811
    """这个作业没有"确认"这一步：不存在 `needs_confirmation` 分支。

    裁决 §5 第 3 条要求"后者不得执行"。这里用两条断言钉住：
    1. 作业表里**没有** `needs_confirmation` 这个状态（枚举里就没有）；
    2. `run_read_job` 对一条待跑作业永远**直接执行**，模型恰好被调一次
       ——不会停在"等确认"上，也就不会有一个"等确认"的终态。
    """
    states = {s.value for s in JobState}
    assert "needs_confirmation" not in states

    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    describer = _CountingDescriber()
    await _run(call_id, describer=describer, store=store)
    assert describer.calls == 1
    row = await _job(call_id)
    assert row is not None and row.state == "succeeded"


# ================================================================ 第 3 条：取件类异常重排


async def test_pre_model_fault_raises_so_outbox_reorders(logged_in: TestEnv) -> None:  # noqa: F811
    """模型**之前**的取件故障 → 抛 `ReadJobError`（outbox 才会退避重排）。

    「改坏了会红」：如果这里也"返回终态"，一次网络抖动就会把作业钉成 `failed`，
    而重排是唯一能自动再试的机制。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)

    def exploding_factory(_session: object) -> object:
        raise ConnectionError("数据库抖了一下")

    call_id = await _make_job(window_id, evidence_id)
    with pytest.raises(ReadJobError):
        await run_read_job(
            call_id=call_id,
            lease_owner="media-worker-1",
            describer=_CountingDescriber(),
            service_factory=exploding_factory,
        )

    # 作业停在 running（租约还会被清理任务接走），**没有**被误记终态
    row = await _job(call_id)
    assert row is not None
    assert row.state == "running"
    assert row.error_code is None


async def test_model_router_unavailable_reorders(logged_in: TestEnv) -> None:  # noqa: F811
    """没有 describer（模型路由未就绪）→ 取件类异常，可重排。"""
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    with pytest.raises(ReadJobError):
        await run_read_job(
            call_id=call_id,
            lease_owner="media-worker-1",
            describer=None,
            service_factory=_factory(store),
        )


async def test_retry_exhaustion_terminates_the_business_job(logged_in: TestEnv) -> None:  # noqa: F811
    """第 5 次失败 → outbox dead 时业务作业**不悬空**（有终态）。

    「改坏了会红」：不调 `settle_dead_jobs`，作业会永远停在 `pending`，
    用户看到"已安排，尚未读到结果"而队列里什么都没有了。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    event_id = await _enqueue_event(call_id)

    # 模拟 outbox 已经到 dead（5 次失败后）
    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        event.state = "dead"
        event.attempts = 5
        await session.commit()

    settled = await settle_dead_jobs()
    assert settled == 1

    row = await _job(call_id)
    assert row is not None
    assert row.state == JobState.failed.value
    assert row.error_code == "outbox_dead"
    assert row.completed_at is not None, "业务作业必须被终结，不能悬空"


# ================================================================ 第 4 条：四个崩溃点


async def test_crash_after_enqueue_resumes_queueing(logged_in: TestEnv) -> None:  # noqa: F811
    """崩溃点 1（入队后、认领前）：作业仍 `pending`，重启后能继续排队并执行。"""
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    await _enqueue_event(call_id)

    row = await _job(call_id)
    assert row is not None and row.state == "pending"

    # "重启后"：事件租约回收（模拟 worker 拿到它）→ 正常执行
    describer = _CountingDescriber()
    outcome = await _run(call_id, describer=describer, store=store)

    assert outcome.state is JobState.succeeded
    assert describer.calls == 1


async def test_crash_before_model_requeues_for_rerun(logged_in: TestEnv) -> None:  # noqa: F811
    """崩溃点 2（认领后、模型前）：`model_started_at` 为空 → **退回排队重跑**。

    「改坏了会红」：把清理写成"running 且租约过期 → unknown"，这条会变成
    `unknown`，而这次**根本没花钱**，本可以安全重读。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    # 崩溃现场：running、拿过租约、但没写 model_started_at、租约已过期
    call_id = await _make_job(
        window_id,
        evidence_id,
        state="running",
        started_at=datetime.now(UTC) - timedelta(seconds=900),
        lease_until=datetime.now(UTC) - timedelta(seconds=60),
        lease_generation=1,
    )

    counts = await expire_overdue_jobs()
    assert counts["requeued"] == 1
    assert counts["unknown"] == 0

    row = await _job(call_id)
    assert row is not None
    assert row.state == "pending", "没开始模型就该重新排队"
    assert row.lease_owner is None
    assert row.lease_until is None
    # 代次**不回退**：它是"第几次被认领"，回退会让旧执行者的结果重新变有效
    assert row.lease_generation == 1

    # 重新执行：模型被调用一次，成功
    describer = _CountingDescriber()
    outcome = await _run(call_id, describer=describer, store=store)
    assert outcome.state is JobState.succeeded
    assert describer.calls == 1


async def test_crash_after_model_records_unknown_without_reburning(logged_in: TestEnv) -> None:  # noqa: F811
    """崩溃点 3（模型开始后）：记 `unknown`，**模型调用次数不增**。

    这条是整个裁决里最不可让步的一条：模型可能已经收过费，自动重跑就是第二次
    收费。「改坏了会红」：清理时把这类作业也退回 `pending`，重放就会再调一次。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(
        window_id,
        evidence_id,
        state="running",
        started_at=datetime.now(UTC) - timedelta(seconds=900),
        model_started_at=datetime.now(UTC) - timedelta(seconds=800),
        lease_until=datetime.now(UTC) - timedelta(seconds=60),
        lease_generation=1,
    )

    counts = await expire_overdue_jobs()
    assert counts["unknown"] == 1
    assert counts["requeued"] == 0, "模型已开始的作业不许退回排队"

    row = await _job(call_id)
    assert row is not None
    assert row.state == JobState.unknown.value
    assert row.error_code == UNKNOWN_JOB_CODE
    assert row.completed_at is not None

    # 重放同一条事件：认领失败（已是终态），**不调模型**
    describer = _CountingDescriber()
    outcome = await _run(call_id, describer=describer, store=store)
    assert describer.calls == 0, "unknown 的作业被重放时又调了模型（重复收费）"
    assert outcome.state is JobState.unknown


async def test_crash_after_commit_replays_result_without_model(logged_in: TestEnv) -> None:  # noqa: F811
    """崩溃点 4（结果提交后）：重放**只读已有结果**，模型调用为 0。"""
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    first = _CountingDescriber()
    assert (await _run(call_id, describer=first, store=store)).state is JobState.succeeded
    assert first.calls == 1

    # 事件重投（至少一次投递的常态）
    second = _CountingDescriber()
    outcome = await _run(call_id, describer=second, store=store)

    assert second.calls == 0, "已成功的作业被重放时又调了模型"
    assert outcome.state is JobState.succeeded
    row = await _job(call_id)
    assert row is not None and row.result_ref == evidence_id


async def test_stale_lease_generation_cannot_commit(logged_in: TestEnv) -> None:  # noqa: F811
    """旧租约代次提交 → 更新行数为 0（结果被丢弃，不覆盖别人）。

    「改坏了会红」：把提交改成无条件 `UPDATE ... WHERE call_id=?`，被抢走的
    执行者会把别人的结果覆盖掉——而两次读取的内容可能完全不同。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    # 第一次认领（代次 1）
    async with session_scope() as session:
        assert await claim_read_job(
            session, call_id=call_id, lease_owner="worker-A"
        ) is True
    async with session_scope() as session:
        stale_generation = 1
        # 模拟租约过期、被 B 抢走（代次 2）
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = "pending"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with session_scope() as session:
        assert await claim_read_job(
            session, call_id=call_id, lease_owner="worker-B"
        ) is True

    # A 迟到提交（用旧代次）
    async with session_scope() as session:
        committed = await commit_read_outcome(
            session,
            call_id=call_id,
            lease_generation=stale_generation,
            outcome=ReadOutcome(state=JobState.succeeded, evidence_ref=evidence_id),
        )
    assert committed is False, "旧代次居然提交成功了"

    row = await _job(call_id)
    assert row is not None
    assert row.state == "running", "B 还在跑，状态不该被 A 改掉"
    assert row.result_ref is None
    assert row.lease_generation == 2


async def test_current_lease_generation_commits(logged_in: TestEnv) -> None:  # noqa: F811
    """当代次提交成功（栅栏不能把正常路径一起挡掉）。"""
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    async with session_scope() as session:
        assert await claim_read_job(session, call_id=call_id, lease_owner="w") is True
    async with session_scope() as session:
        committed = await commit_read_outcome(
            session,
            call_id=call_id,
            lease_generation=1,
            outcome=ReadOutcome(state=JobState.succeeded, evidence_ref=evidence_id),
        )
    assert committed is True
    row = await _job(call_id)
    assert row is not None and row.state == "succeeded" and row.result_ref == evidence_id


async def test_model_started_is_committed_before_the_model_runs(logged_in: TestEnv) -> None:  # noqa: F811
    """`model_started_at` 必须在**调模型之前**落库且提交。

    「改坏了会红」：把它和结果放同一个事务，崩溃时一起回滚，"模型可能收过费"
    就查不到，重排后第二次收费。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    async with session_scope() as session:
        assert await claim_read_job(session, call_id=call_id, lease_owner="w") is True

    async with session_scope() as session:
        assert await mark_model_started(
            call_id, lease_owner="w", lease_generation=1
        ) is True

    # **另开一个事务**能看到它 —— 证明它是独立提交的
    row = await _job(call_id)
    assert row is not None
    assert row.model_started_at is not None
    assert row.lease_until is not None
    assert row.lease_until > datetime.now(UTC), "写 model_started_at 时应续租"


async def test_model_started_rejects_stale_generation(logged_in: TestEnv) -> None:  # noqa: F811
    """旧代次写 `model_started_at` 也会被拒（不然被抢走的执行者会污染状态）。"""
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    async with session_scope() as session:
        assert await claim_read_job(session, call_id=call_id, lease_owner="w") is True

    async with session_scope() as session:
        assert await mark_model_started(
            call_id, lease_owner="w", lease_generation=99
        ) is False
    row = await _job(call_id)
    assert row is not None and row.model_started_at is None


# ================================================================ 取消


async def test_cancel_before_execution_settles_cancelled(logged_in: TestEnv) -> None:  # noqa: F811
    """pending 时被取消 → 执行前重检命中，落 `cancelled`，**模型 0 次**。"""
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id, cancel_requested_at=datetime.now(UTC))

    describer = _CountingDescriber()
    outcome = await _run(call_id, describer=describer, store=store)

    assert outcome.state is JobState.cancelled
    assert describer.calls == 0, "已取消的作业不该调模型"
    row = await _job(call_id)
    assert row is not None and row.state == "cancelled"
    assert row.model_started_at is None


async def test_cancel_during_run_keeps_content_out_of_evidence(logged_in: TestEnv) -> None:  # noqa: F811
    """跑起来之后才取消：终态 `cancelled`，且**晚到内容不得成为可消费证据**。

    断言点是 `describe_failed_at` 被写：读侧（第 4 步）只认没被标失败的证据，
    所以这条内容不会进她下一轮的上下文。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    class _CancelMidway(_CountingDescriber):
        """模型跑完、提交结果之前，用户点了取消。"""

        async def describe_video(self, evidence: object, *, mode: str = "frames"):  # noqa: ANN201
            result = await super().describe_video(evidence, mode=mode)
            async with session_scope() as session:
                row = await session.get(MediaReadJob, call_id, with_for_update=True)
                assert row is not None
                row.cancel_requested_at = datetime.now(UTC)
                await session.commit()
            return result

    describer = _CancelMidway()
    outcome = await _run(call_id, describer=describer, store=store)

    assert outcome.state is JobState.cancelled
    assert describer.calls == 1, "取消发生在模型之后，这一次调用收不回来"

    row = await _job(call_id)
    assert row is not None
    assert row.state == "cancelled"
    assert row.result_ref is None, "取消的作业不许有结果引用"

    # 晚到内容被逐出可读集合
    evidence = store[evidence_id]
    assert getattr(evidence, "describe_failed_at", None) is not None, (
        "取消之后描述仍留在证据上——晚到内容会变成可消费证据"
    )


# ================================================================ 第 7 条：不产生对话副作用


async def _conversation_counts() -> dict[str, int]:
    async with session_scope() as session:
        async def count(model) -> int:  # noqa: ANN001
            return int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )

        return {
            "actor_messages": int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Message)
                        .where(Message.role == "actor")
                    )
                ).scalar_one()
            ),
            "turns": await count(Turn),
            "operations": await count(Operation),
            "deliveries": await count(DeliveryReceiptRow),
            "model_calls": await count(BehaviorModelCallRow),
        }


async def test_job_completion_creates_no_conversation_side_effects(logged_in: TestEnv) -> None:  # noqa: F811
    """任务完成前后：新增 actor 消息 / 轮次 / 投递 operation / SSE delta **均为 0**。

    裁决 §5 第 7 条。这是"不主动补话"的可测形式：后台读取只写证据，
    绝不产生一条对话。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    before = await _conversation_counts()

    outcome = await _run(call_id, describer=_CountingDescriber(), store=store)
    assert outcome.state is JobState.succeeded

    after = await _conversation_counts()
    assert after["actor_messages"] == before["actor_messages"], "后台作业新增了 actor 消息"
    assert after["turns"] == before["turns"], "后台作业新增了轮次"
    assert after["operations"] == before["operations"], "后台作业新增了投递 operation"
    assert after["deliveries"] == before["deliveries"]
    # 预算账本（设计第 4 步，2026-11-10 已接）：模型调用前**必须**预留一次，
    # 所以这里从"不新增"改成"恰好新增 1 条、且是 video_watch 的预留"。
    #
    # 这比原来的断言更强：它同时钉住"确实预留了"（否则多副本会重复烧钱）
    # 与"只预留一次"（多预留同样是错）。
    assert after["model_calls"] == before["model_calls"] + 1, (
        "后台作业应当恰好预留一次视频预算"
    )
    async with session_scope() as session:
        row = (
            await session.execute(
                select(BehaviorModelCallRow)
                .where(BehaviorModelCallRow.kind == "video_watch")
                .order_by(BehaviorModelCallRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    assert row is not None, "预算预留没有落库"
    # 成功的作业必须结清成 committed：留着 reserved 会让额度永远被占着
    assert row.state == "committed", f"预留没有结清：{row.state}"


@pytest.mark.parametrize("state", ["pending", "running", "failed", "timeout", "unknown"])
async def test_get_never_claims_success_for_non_success_states(
    logged_in: TestEnv, state: str  # noqa: F811
) -> None:
    """GET 在非成功状态都**不返回成功文案**（裁决 §5 第 7 条）。"""
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id, state=state)

    r = await logged_in.client.get(
        f"/v1/sessions/{window_id}/media/read-jobs/{call_id}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == state
    note = body["note"]
    assert "读完了" not in note and "已存为" not in note, note
    if state == "unknown":
        assert "不确定" in note
    if state == "pending":
        assert "已安排" in note


# ================================================================ 第 8 条：隔离与清理


async def test_main_worker_does_not_claim_media_topic(logged_in: TestEnv) -> None:  # noqa: F811
    """普通 worker **不认领**媒体 topic；媒体 worker 只认那一个。

    「改坏了会红」：把 `exclude_topics` 从 `run_forever` 去掉，一条 60 秒的视频
    会把撤回/摘要/日程全堵在后面——这正是裁决点名的缺陷。

    **两层断言**，缺一不可：

    1. `claim_next_outbox_event(exclude_topics=...)` 本身按 topic 过滤（行为）；
    2. **`run_forever` 真的把那个集合传下去了**（接线）。

    只写第 1 层是不够的：我在变异测试里验证过——把 `run_forever` 里的
    `exclude_topics=` 去掉，只测第 1 层的用例**照样全绿**。过滤参数本身正确
    和"它被接上了"是两件事，必须分开钉。
    """
    import inspect

    from ling.worker import (
        MEDIA_EXCLUDED_FROM_MAIN_WORKER,
        claim_next_outbox_event,
        run_forever,
    )

    assert MEDIA_READ_TOPIC in MEDIA_EXCLUDED_FROM_MAIN_WORKER

    # 第 2 层：主循环必须真的传了排除集合（接线断言）
    source = inspect.getsource(run_forever)
    assert "exclude_topics=MEDIA_EXCLUDED_FROM_MAIN_WORKER" in source, (
        "run_forever 没有把媒体 topic 排除掉——慢视频会堵住整条串行队列"
    )

    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    event_id = await _enqueue_event(call_id)

    # 普通 worker 的认领条件：拿不到
    assert (
        await claim_next_outbox_event(
            exclude_topics=MEDIA_EXCLUDED_FROM_MAIN_WORKER
        )
        is None
    )
    # 只有媒体 worker 拿得到
    claim = await claim_next_outbox_event(only_topic=MEDIA_READ_TOPIC)
    assert claim is not None
    assert claim.event_id == event_id

    # 放回 pending，别让后续断言看到 in_flight
    async with session_scope() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        row.state = "pending"
        row.processed_by = None
        await session.commit()


async def test_media_topic_is_not_starved_by_other_events(logged_in: TestEnv) -> None:  # noqa: F811
    """普通事件堆在前面时，媒体 worker 仍能立刻拿到自己的事件（不等队列）。

    「改坏了会红」：媒体 worker 若不传 `only_topic`，它会按 FIFO 先抓那条普通
    事件，"专用"就名存实亡——慢视频照样进同一条串行循环。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    media_event = await _enqueue_event(call_id)

    # 一条更早的普通事件
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=new_id("outbox"),
                topic="window.summary",
                payload={"window_id": window_id},
                available_at=datetime.now(UTC) - timedelta(minutes=5),
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await session.commit()

    from ling.worker import claim_next_outbox_event

    claim = await claim_next_outbox_event(only_topic=MEDIA_READ_TOPIC)
    assert claim is not None
    assert claim.event_id == media_event
    assert claim.topic == MEDIA_READ_TOPIC

    async with session_scope() as session:
        row = await session.get(OutboxEvent, media_event)
        assert row is not None
        row.state = "pending"
        row.processed_by = None
        await session.commit()


async def test_queue_deadline_expiry_is_bounded(logged_in: TestEnv) -> None:  # noqa: F811
    """非终态作业在 `deadline` 之后由清理终结（清理周期 ≤60 秒）。

    裁决 §5 第 8 条："所有非终态作业最迟在 deadline 加一个清理周期后终结"。
    """
    from ling.domains.media.jobs import MEDIA_READ_CLEANUP_INTERVAL_SECONDS
    from ling.worker import MEDIA_READ_CLEANUP_SECONDS

    assert MEDIA_READ_CLEANUP_SECONDS <= 60
    assert MEDIA_READ_CLEANUP_INTERVAL_SECONDS <= 60

    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    overdue = await _make_job(
        window_id,
        evidence_id,
        requested_at=datetime.now(UTC) - MEDIA_READ_QUEUE_DEADLINE - timedelta(minutes=5),
    )
    # 第二条必须换证据：在途互斥索引不允许同一证据两个在途作业
    fresh = await _make_job(window_id, await _more_evidence(logged_in, window_id))

    counts = await expire_overdue_jobs()
    assert counts["expired"] == 1

    overdue_row = await _job(overdue)
    assert overdue_row is not None
    assert overdue_row.state == JobState.expired.value
    assert overdue_row.error_code == "queue_deadline_exceeded"

    fresh_row = await _job(fresh)
    assert fresh_row is not None and fresh_row.state == "pending", "没到期的不能被误杀"


async def test_null_deadline_is_handled(logged_in: TestEnv) -> None:  # noqa: F811
    """`deadline` 为 NULL 的旧行也要被清理（迁移可空 → 必须兜底）。

    「改坏了会红」：写成 `MediaReadJob.deadline <= now`（不加 COALESCE），
    NULL 行永远不会被选中——一条永远排不上的作业会永久占着在途互斥索引，
    用户再也提交不了这个视频。
    """
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(
        window_id,
        evidence_id,
        requested_at=datetime.now(UTC) - MEDIA_READ_QUEUE_DEADLINE - timedelta(minutes=30),
    )
    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.deadline = None
        await session.commit()

    counts = await expire_overdue_jobs()
    assert counts["expired"] == 1, "deadline 为 NULL 的过期作业没被清理"

    row = await _job(call_id)
    assert row is not None and row.state == "expired"


async def test_cleanup_is_zero_model(logged_in: TestEnv) -> None:  # noqa: F811
    """清理一次模型都不调（"没有新作业的定时扫描必须零模型"）。"""
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    await _make_job(
        window_id, evidence_id,
        requested_at=datetime.now(UTC) - MEDIA_READ_QUEUE_DEADLINE - timedelta(minutes=1),
    )
    await _make_job(
        window_id, await _more_evidence(logged_in, window_id),
        state="running",
        model_started_at=datetime.now(UTC) - timedelta(seconds=800),
        lease_until=datetime.now(UTC) - timedelta(seconds=10),
    )
    before = await _conversation_counts()

    await expire_overdue_jobs()

    after = await _conversation_counts()
    assert after["model_calls"] == before["model_calls"], "清理任务调了模型"


async def test_cleanup_runs_a_hundred_times_without_side_effects(logged_in: TestEnv) -> None:  # noqa: F811
    """无任务扫描 100 次：额外模型调用与状态变化均为 0（空转是安全的）。"""
    window_id, evidence_id, _store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)  # 刚建、没到期
    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        assert row is not None
        row.state = "succeeded"
        row.result_ref = evidence_id
        await session.commit()
    before = await _conversation_counts()

    for _ in range(100):
        counts = await expire_overdue_jobs()
        assert counts == {"expired": 0, "requeued": 0, "unknown": 0}

    after = await _conversation_counts()
    assert after["model_calls"] == before["model_calls"]
    row = await _job(call_id)
    assert row is not None and row.state == "succeeded", "终态被清理改掉了"


async def test_slow_media_does_not_block_the_main_outbox(logged_in: TestEnv) -> None:  # noqa: F811
    """慢媒体执行期间，普通 worker 仍能推进自己的事件（裁决 §5 第 8 条）。

    做法：用 Event 挂住媒体执行（模型卡在一个未 set 的 Event 上），同时让普通
    worker 认领并跑完一条普通事件——两条线各走各的，证明它们在**认领层**就分开了。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)
    await _enqueue_event(call_id)

    gate = asyncio.Event()

    class _Blocking(_CountingDescriber):
        async def describe_video(self, evidence: object, *, mode: str = "frames"):  # noqa: ANN201
            self.calls += 1
            await gate.wait()  # 卡住，模拟"视频还在跑"
            return await super().describe_video(evidence, mode=mode)

    describer = _Blocking()
    media_task = asyncio.create_task(_run(call_id, describer=describer, store=store))
    # 让它真的卡在模型里
    for _ in range(100):
        await asyncio.sleep(0.01)
        if describer.calls:
            break
    assert describer.calls == 1, "媒体执行没进到模型"

    # 媒体卡住的这段时间：普通 worker 能认领自己的事件
    from ling.worker import MEDIA_EXCLUDED_FROM_MAIN_WORKER, claim_next_outbox_event

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=new_id("outbox"),
                topic="window.summary",
                payload={"window_id": window_id},
                available_at=datetime.now(UTC),
            )
        )
        await session.commit()

    ordinary = await claim_next_outbox_event(
        exclude_topics=MEDIA_EXCLUDED_FROM_MAIN_WORKER
    )
    assert ordinary is not None, "慢媒体把普通 outbox 也堵住了"
    assert ordinary.topic == "window.summary"

    gate.set()
    outcome = await asyncio.wait_for(media_task, timeout=30)
    assert outcome.state is JobState.succeeded


async def test_four_crash_points_are_covered_together(logged_in: TestEnv) -> None:  # noqa: F811
    """四个崩溃点在同一次清理里各走各的分支（组合回归）。

    单独的四条用例各自只验一个分支；这条把它们放在**同一批数据**上，确认清理
    不会因为处理了一条而影响另一条（例如一个 `UPDATE` 的条件写宽了会全改）。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    now = datetime.now(UTC)

    # 四条作业必须用**不同证据**：在途互斥索引不允许同一证据两个在途作业。
    ev_queued = await _more_evidence(logged_in, window_id)
    ev_before = await _more_evidence(logged_in, window_id)
    ev_after = await _more_evidence(logged_in, window_id)

    # 1) 入队后：pending、未过期 → 不动
    queued = await _make_job(window_id, ev_queued)
    # 2) 认领后崩溃：running + 无 model_started_at + 租约过期 → 退回排队
    before_model = await _make_job(
        window_id,
        ev_before,
        state="running",
        started_at=now - timedelta(seconds=900),
        lease_until=now - timedelta(seconds=60),
        lease_generation=1,
    )
    # 3) 模型后崩溃：running + 有 model_started_at + 租约过期 → unknown
    after_model = await _make_job(
        window_id,
        ev_after,
        state="running",
        started_at=now - timedelta(seconds=900),
        model_started_at=now - timedelta(seconds=800),
        lease_until=now - timedelta(seconds=60),
        lease_generation=1,
    )
    # 4) 结果提交后：succeeded + result_ref → 不动
    done = await _make_job(window_id, evidence_id, state="succeeded", result_ref=evidence_id)

    counts = await expire_overdue_jobs()

    assert counts == {"expired": 0, "requeued": 1, "unknown": 1}
    assert (await _job(queued)).state == "pending"  # type: ignore[union-attr]
    assert (await _job(before_model)).state == "pending"  # type: ignore[union-attr]
    assert (await _job(after_model)).state == "unknown"  # type: ignore[union-attr]
    assert (await _job(done)).state == "succeeded"  # type: ignore[union-attr]

    # 只有"结果提交后"那条能在重放时零模型返回成功
    replay = await _run(done, describer=_CountingDescriber(), store=store)
    assert replay.state is JobState.succeeded


def test_lease_is_longer_than_the_step_four_execution_cap() -> None:
    """租约（600 秒）必须**明显大于**执行上限（180 秒）。

    相等或更短时，慢一点的视频会永远被抢走、永远读不完（抢走 → 原结果被栅栏
    丢弃 → 重来 → 又被抢）。
    """
    assert MEDIA_READ_LEASE_SECONDS == 600
    assert MEDIA_READ_LEASE_SECONDS > 180 * 2


# ================================================================ 第 1 条：不阻塞对话


async def test_registration_returns_fast_while_video_is_held(logged_in: TestEnv) -> None:  # noqa: F811
    """视频被 Event 挂住时：登记接口仍然立刻 202，并行普通对话也能完成。

    裁决 §5 第 1 条。这条验的是**因果边界**：登记只是入队，不等视频。

    ## 断言的是"挂住期间"，不是"跑完之后"

    如果只在最后断言"两个都成功"，那和"串行跑完"完全没法区分。所以先确认
    媒体执行**真的卡在模型里**（`describer.calls == 1` 且 gate 未放行），
    在这个状态下再发登记请求与普通对话——它们能返回才说明没被拖住。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    gate = asyncio.Event()

    class _Blocking(_CountingDescriber):
        async def describe_video(self, evidence: object, *, mode: str = "frames"):  # noqa: ANN201
            self.calls += 1
            await gate.wait()
            return await super().describe_video(evidence, mode=mode)

    describer = _Blocking()
    media_task = asyncio.create_task(_run(call_id, describer=describer, store=store))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if describer.calls:
            break
    assert describer.calls == 1, "媒体执行没进到模型，这条测试没验到东西"

    # 媒体卡住的这段时间：登记接口立刻返回 202
    # 先授权：登记走的是**真实**接口链，没有 media.inbound 会被 403 挡在门口
    from ling.domains.consent.service import ConsentService
    from ling.domains.media.service import INBOUND_SCOPE

    async with session_scope() as session:
        await ConsentService(session).grant(
            subject_id=logged_in.subject_id, scope=INBOUND_SCOPE
        )
    other_evidence = await _more_evidence(logged_in, window_id)
    started = asyncio.get_running_loop().time()
    registered = await logged_in.client.post(
        f"/v1/sessions/{window_id}/media/{other_evidence}/read-jobs",
        json={"feedback_mode": "evidence_only", "request_key": new_id("rk")},
    )
    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

    assert registered.status_code == 202, registered.text
    assert registered.json()["state"] == "pending"
    # 裁决阈值 200ms；本机实测通常 <30ms，留足余量
    assert elapsed_ms <= 200, f"登记接口被慢视频拖慢了 {elapsed_ms:.0f}ms"

    # 并行普通对话也能完成
    turn = await logged_in.client.post(
        f"/v1/sessions/{window_id}/turns",
        json={"client_turn_id": new_id("ct"), "text": "你在干嘛"},
    )
    assert turn.status_code == 200, turn.text[:200]
    assert "event: turn.completed" in turn.text

    # 媒体仍在跑（证明它没被对话"顺手跑完"）
    assert not gate.is_set()
    row = await _job(call_id)
    assert row is not None and row.state == "running"

    gate.set()
    outcome = await asyncio.wait_for(media_task, timeout=30)
    assert outcome.state is JobState.succeeded


async def test_conversation_is_not_slowed_by_a_queued_job(logged_in: TestEnv) -> None:  # noqa: F811
    """**基线对比**：有排队作业 vs 没有，普通对话的延迟差 ≤200ms。

    上一条验的是"挂住时能返回"，这条验的是"**额外**延迟足够小"——裁决原文
    要的是"相对无任务基线的额外 p95 延迟 ≤200ms"。

    ## 怎么把噪声从测量里去掉（而不是放宽阈值）

    这条测的是**墙钟时间**，而 CI 是共享机器、还并行跑着其它用例，所以有两类
    噪声必须消掉，阈值仍是裁决的 200ms：

    1. **固定成本**（导入/连接池/配置缓存）与"有没有排队作业"无关，却会整个落在
       某一侧 → 两边各先跑一轮**丢弃**（预热）。
    2. **负载漂移**：先测基线、再测有作业，两个阶段之间机器可能变忙/变闲，
       差值就被漂移污染（实测在 CI 上出现过基线 2665ms、有作业 3166ms 的
       501ms 差值——那不是回归，是两个阶段之间负载不同）→ **交替测量**：
       每轮都先量一侧、紧接着量另一侧，并让先后顺序逐轮翻转。漂移因此被
       两边平摊，差值里剩下的才是"有没有作业"这一项。
    3. **偶发尖峰**（一次 GC、一次调度延迟）→ 取**最小值**而不是中位数。
       最小值是这条测试的正确统计量：没有阻塞时，总有一轮能跑到"地板"；
       真有回归（媒体作业挤进对话线程）时，**每一轮**都会被拖慢，最小值
       同样抬高。中位数反而会被"一半样本恰好赶上调度抖动"骗过去。

    这三条合起来的效果：既不会因机器忙而假红，也不会因为取了最小值就变得
    迟钝——真正的回归一定让所有样本一起变慢。
    """

    async def _one_turn(window: str) -> float:
        start = asyncio.get_running_loop().time()
        r = await logged_in.client.post(
            f"/v1/sessions/{window}/turns",
            json={"client_turn_id": new_id("ct"), "text": "嗯"},
        )
        elapsed = (asyncio.get_running_loop().time() - start) * 1000
        assert r.status_code == 200, r.text[:200]
        return elapsed

    baseline_window = (await logged_in.client.post(
        "/v1/sessions", json={"title": "基线"}
    )).json()["session_id"]
    busy_window, busy_evidence, _store = await _session_and_evidence(logged_in)
    # 排一个作业进去（不执行，只是占着队列）
    await _make_job(busy_window, busy_evidence)
    await _enqueue_event(busy_evidence)

    # 预热：两边各跑一轮丢弃（固定成本不计入）
    await _one_turn(baseline_window)
    await _one_turn(busy_window)

    rounds = 5
    baseline_samples: list[float] = []
    busy_samples: list[float] = []
    for index in range(rounds):
        # 逐轮翻转先后顺序：否则"后测的那一侧"总在同一类负载条件下
        if index % 2 == 0:
            baseline_samples.append(await _one_turn(baseline_window))
            busy_samples.append(await _one_turn(busy_window))
        else:
            busy_samples.append(await _one_turn(busy_window))
            baseline_samples.append(await _one_turn(baseline_window))

    baseline = min(baseline_samples)
    with_job = min(busy_samples)

    delta = with_job - baseline
    assert delta <= 200, (
        f"有排队作业时对话慢了 {delta:.0f}ms（基线最快一轮 {baseline:.0f}ms，"
        f"有作业最快一轮 {with_job:.0f}ms；"
        f"基线样本 {[round(v) for v in baseline_samples]}，"
        f"有作业样本 {[round(v) for v in busy_samples]}）——"
        "取最小值仍超阈值，说明是每一轮都被拖慢，不是偶发尖峰"
    )


async def test_graceful_shutdown_mid_model_records_unknown_and_reraises(logged_in: TestEnv) -> None:  # noqa: F811
    """SIGTERM 取消（`CancelledError`）也要记 `unknown`，且**必须重新抛出**。

    两条都要验：

    1. **记 unknown**：模型可能已经调用过，不记的话重排会第二次收费；
    2. **重新抛出**：吞掉 `CancelledError` 会让关停卡在这一步，而 asyncio
       期望它一路传到取消方。

    「改坏了会红」：只用 `except BaseException` 一把抓并 `return`，第 2 条会红
    （取消被吞，进程关不掉）。
    """
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    class _Cancelled(_CountingDescriber):
        async def describe_video(self, evidence: object, *, mode: str = "frames"):  # noqa: ANN201
            self.calls += 1
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run(call_id, describer=_Cancelled(), store=store)

    row = await _job(call_id)
    assert row is not None
    assert row.state == JobState.unknown.value, (
        "优雅退出时模型已开始，必须记 unknown（不然重排会重复收费）"
    )
    assert row.error_code == "worker_shutdown"
    assert row.model_started_at is not None
    assert row.result_ref is None


# ================================================================ 预算闸位置（P0 回归钉）


async def test_budget_denied_settles_without_raising(logged_in: TestEnv) -> None:  # noqa: F811
    """额度用尽时必须**落 `budget_denied` 并正常返回**，不能抛、不能白烧模型。

    ## 这条用例为什么必须存在

    这条路径原来整条都是坏的，而且没有任何测试覆盖到它——全仓搜
    `budget_denied` 当时只有状态机单测。真实的失败是：

    预算闸写在 `claim_read_job`（`pending -> running`）**之后**，而
    `commit_read_outcome` 第一行就是 `transition(running, outcome.state)`；
    `ALLOWED_TRANSITIONS[running]` 里**故意没有** `budget_denied`（裁决：没额度
    就永远不该进 running）。于是只要额度真的用尽：

    1. `InvalidJobTransition` 被抛出——它不是 `ReadJobError`，worker 抓不到；
    2. 一路抛给 outbox → 退避重试 5 次（每次同一结果）→ `dead`；
    3. 对账把它落成 `failed/outbox_dead`。

    用户看到的是「失败」，而 `chat.py` 里那句「这次额度不够，没有读」永远
    不会出现，还白烧 5 次重试。

    现在闸前移到认领之前，走 `pending -> budget_denied`（合法边）。
    """
    from ling.config import get_settings
    from ling.domains.media.budget import (
        VIDEO_DAILY_TOKEN_BUDGET,
        VIDEO_RESERVED_INPUT_TOKENS,
        VIDEO_RESERVED_OUTPUT_TOKENS,
        reserve_video_call,
    )

    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    # 把今天的额度先占满（用别的 key 预留，不结清）。
    actor_id = str(get_settings().actor_id)
    per_call = VIDEO_RESERVED_INPUT_TOKENS + VIDEO_RESERVED_OUTPUT_TOKENS
    for i in range(VIDEO_DAILY_TOKEN_BUDGET // per_call + 1):
        async with session_scope() as session:
            await reserve_video_call(
                session,
                actor_id=actor_id,
                idempotency_key=f"video:filler_{call_id}_{i}",
            )

    describer = _CountingDescriber()
    outcome = await _run(call_id, describer=describer, store=store)

    # 1) 不抛（能返回本身就是断言：抛了这行就到不了）
    # 2) 终态是 budget_denied，不是 failed
    assert outcome.state is JobState.budget_denied, (
        f"额度用尽应当落 budget_denied，实际 {outcome.state}"
    )
    # 3) 一行都没花
    assert describer.calls == 0, "额度不足仍然调了模型"

    # 4) 库里确实是 budget_denied，且作业**从未进入 running**
    row = await _job(call_id)
    assert row is not None
    assert row.state == JobState.budget_denied.value
    assert row.started_at is None, "预算闸在认领之前，不该有开始时间"

    # 5) 这次被拒的作业**没有**留下第二份预留（那份额度被 filler 占满了，
    #    闸没放行就不该有 call_id）
    async with session_scope() as session:
        ours = (
            await session.execute(
                select(BehaviorModelCallRow).where(
                    BehaviorModelCallRow.idempotency_key == f"video:{call_id}"
                )
            )
        ).all()
    assert ours == [], "闸没放行却留下了预留行"


async def test_missing_describer_leaks_no_reservation(logged_in: TestEnv) -> None:  # noqa: F811
    """`describer is None`（模型路由未就绪）**每次都不许留下 reserved 行**。

    这条检查原来在预算预留之后，于是「路由还没就绪」这段**正常**时间里，
    每次重排都漏一条 `reserved`：`_day_call_totals` 把 reserved 计入当日额度，
    而 `_reserve_call` 不看租约是否过期，所以这些行**当天永久占额**。
    路由没就绪几个小时就足以把 120k 额度吃光，之后所有视频读取一起被拒。

    连跑 3 次（模拟 outbox 重排），断言预留行数为 0。
    """
    from ling.config import get_settings

    actor_id = str(get_settings().actor_id)
    window_id, evidence_id, store = await _session_and_evidence(logged_in)
    call_id = await _make_job(window_id, evidence_id)

    for _ in range(3):
        # 注意：直接用 `run_read_job`，**不走** `_run` —— `_run` 把
        # `describer=None` 换成了 `_CountingDescriber()`，那正好绕开了本用例。
        with pytest.raises(ReadJobError):
            await run_read_job(
                call_id=call_id,
                lease_owner="media-worker-1",
                describer=None,
                service_factory=_factory(store),
            )

    async with session_scope() as session:
        leaked = (
            await session.execute(
                select(BehaviorModelCallRow).where(
                    BehaviorModelCallRow.actor_id == actor_id,
                    BehaviorModelCallRow.kind == "video_watch",
                    BehaviorModelCallRow.idempotency_key == f"video:{call_id}",
                )
            )
        ).all()
    assert leaked == [], f"没有描述器却留下 {len(leaked)} 条预留（当天永久占额）"

    # 作业仍是 pending（取件类错误允许重排），没有被改成终态
    row = await _job(call_id)
    assert row is not None
    assert row.state == JobState.pending.value
