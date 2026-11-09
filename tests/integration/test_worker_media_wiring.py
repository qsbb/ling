"""媒体 worker 的**接线**测试：`run_forever` 真的把媒体循环拉起来了吗。

## 为什么单独一个文件

上一轮的教训：`settle_dead_jobs` 写得对、单测也过，但**没有任何调用方**——
而"函数本身对"和"它被接上了"是两件完全不同的事。媒体 worker 犯了同一个错：
`run_media_worker_forever` 定义了、也没人调，于是 `media.video.read` 事件永远
停在 pending，入口 202 说"已安排"，而永远不会有结果。

所以这里每条用例都锚在**接线**上，不锚在循环本体的行为：

1. `run_forever` 启动后，媒体事件会被消费（作业从 pending 到终态）；
2. `stop.set()` 后两条线都退出，**没有 task 泄漏**；
3. 主循环在跑慢媒体时仍能推进普通事件（隔离）；
4. `init_engine` / `init_memory_router` 在 `run_forever` 里只调用**一次**
   （同进程双循环不能重复初始化）。

## 怎么让 `run_forever` 可控地跑

`run_forever` 会装信号处理器、初始化引擎、等 schema。测试里：
- 用 monkeypatch 把 `wait_for_schema` / `recover_dangling_turns` /
  `enqueue_maintenance` / `enqueue_window_summaries` 换成空实现；
- `init_engine` / `init_memory_router` 换成**计数器**（第 4 条用例靠它）；
- 把 `poll_seconds` 调小，跑几百毫秒就 `stop.set()`。

这样就能在进程内真跑 `run_forever` 的**完整接线**，而不是只调 `_media_worker_loop`。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

import ling.worker as worker_module
from ling.domains.media.jobs import (
    MEDIA_READ_QUEUE_DEADLINE,
    MEDIA_READ_TOOL_VERSION,
    MEDIA_READ_TOPIC,
    JobState,
    outbox_event_id_for,
)
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow, MediaReadJob, OutboxEvent
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


# ---------------------------------------------------------------- 种数据


async def _window_and_video(env: TestEnv) -> tuple[str, str]:
    r = await env.client.post("/v1/sessions", json={"title": "媒体接线"})
    window_id = r.json()["session_id"]
    async with session_scope() as session:
        row = MediaEvidenceRow(
            id=new_id("ev"),
            actor_id=ACTOR,
            subject_id=env.subject_id,
            window_id=window_id,
            kind="video",
            media_ref="file:.data/media/wiring.mp4",
            origin="user_upload",
            dedupe_key=new_id("dk"),
        )
        session.add(row)
        await session.commit()
        return window_id, row.id


async def _pending_job_with_event(window_id: str, evidence_id: str) -> str:
    """落一条 `pending` 作业 + 它的 outbox 事件（模拟入口端点做过的事）。"""
    call_id = new_id("mread")
    moment = datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            MediaReadJob(
                call_id=call_id,
                actor_id=ACTOR,
                subject_id=None,
                window_id=window_id,
                evidence_id=evidence_id,
                request_key=new_id("rk"),
                tool_version=MEDIA_READ_TOOL_VERSION,
                mode="video",
                feedback_mode="evidence_only",
                state=JobState.pending.value,
                requested_at=moment,
                deadline=moment + MEDIA_READ_QUEUE_DEADLINE,
            )
        )
        session.add(
            OutboxEvent(
                id=outbox_event_id_for(call_id),
                topic=MEDIA_READ_TOPIC,
                payload={"call_id": call_id, "schema_version": "v1"},
                available_at=moment,
            )
        )
        await session.commit()
    return call_id


async def _job(call_id: str) -> MediaReadJob | None:
    async with session_scope() as session:
        return await session.get(MediaReadJob, call_id)


async def _event(call_id: str) -> OutboxEvent | None:
    async with session_scope() as session:
        return await session.get(OutboxEvent, outbox_event_id_for(call_id))


# ---------------------------------------------------------------- 装置


@pytest.fixture()
def _no_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_forever` 会装 SIGTERM/SIGINT——测试里别动真进程的信号。

    直接替换事件循环类上的 `add_signal_handler`（monkeypatch 会自动还原）。
    `asyncio.BaseEventLoop.add_signal_handler` 是类属性，改类比改实例可靠：
    测试和 `run_forever` 用的是同一个运行中的循环实例。
    """
    def _record(self: object, sig: int, *_args: object, **_kwargs: object) -> None:
        # 什么都不做：真注册会把测试进程的 SIGTERM 抢走
        return None

    monkeypatch.setattr(
        asyncio.unix_events._UnixSelectorEventLoop,  # type: ignore[attr-defined]
        "add_signal_handler",
        _record,
        raising=False,
    )


@pytest.fixture()
def _fast_worker(
    monkeypatch: pytest.MonkeyPatch, _no_signal_handlers: None
) -> dict[str, int]:
    """把 `run_forever` 的外部依赖换成可控替身，并计数初始化调用。

    依赖 `_no_signal_handlers`：`run_forever` 一进来就装信号处理器，
    不先打补丁会把测试进程的 SIGTERM 抢走。
    """
    counts = {"init_engine": 0, "init_memory_router": 0}

    def fake_init_engine(_settings: object) -> None:
        # **不真 init**：conftest 已经为测试库初始化过引擎，再 init 一次会把
        # 现有连接池换掉，后续 fixture 的 session 就指向另一个池。
        counts["init_engine"] += 1

    async def fake_init_router() -> None:
        counts["init_memory_router"] += 1

    async def fake_wait_schema(*_a: object, **_k: object) -> bool:
        return True

    async def fake_recover() -> int:
        return 0

    async def fake_noop(*_a: object, **_k: object) -> int:
        return 0

    async def fake_dispose() -> None:
        # **不真拆**：`run_forever` 的 finally 会 `dispose_engine()`，而测试
        # 后面（以及 stub handler）还要用 conftest 建好的那个引擎。
        # 真拆掉的话，`session_scope()` 会报"数据库尚未初始化"——那正是
        # 这条用例第一次跑出来的错误。
        return None

    monkeypatch.setattr(worker_module, "init_engine", fake_init_engine)
    monkeypatch.setattr(worker_module, "dispose_engine", fake_dispose)
    monkeypatch.setattr(worker_module, "init_memory_router", fake_init_router)
    monkeypatch.setattr(worker_module, "wait_for_schema", fake_wait_schema)
    monkeypatch.setattr(worker_module, "recover_dangling_turns", fake_recover)
    monkeypatch.setattr(worker_module, "enqueue_maintenance", fake_noop)
    monkeypatch.setattr(worker_module, "enqueue_window_summaries", fake_noop)
    # 周期任务与告警不参与接线验证，关掉以免它们抢事件/打外呼
    monkeypatch.setattr(worker_module, "PERIODIC_JOBS", ())
    monkeypatch.setattr(worker_module, "check_and_notify_alerts", fake_noop)
    monkeypatch.setattr(worker_module, "ALERT_CHECK_SECONDS", 10**9)
    return counts


_STUB_HANDLED: list[str] = []


async def _stub_read_handler(payload: dict) -> None:
    """把媒体 handler 换成"立刻把作业标成 succeeded"。

    为什么不跑真 handler：真 handler 需要模型路由/连接器/下载。接线测试要验的
    是"这条事件被**消费**了"，用 stub 才能几百毫秒跑完并且结果确定。

    **stub 仍然记一笔**（`_STUB_HANDLED`），这样"事件被消费"与"handler 被调用"
    两件事都能断言。
    """
    call_id = str(payload.get("call_id") or "")
    _STUB_HANDLED.append(call_id)
    async with session_scope() as session:
        row = await session.get(MediaReadJob, call_id)
        if row is not None:
            row.state = JobState.succeeded.value
            row.completed_at = datetime.now(UTC)
            await session.commit()


@pytest.fixture(autouse=True)
def _reset_stub() -> None:
    _STUB_HANDLED.clear()
    yield
    _STUB_HANDLED.clear()


async def _run_worker_briefly(
    *, seconds: float = 1.5, poll: float = 0.05
) -> None:
    """真跑一次 `run_forever`（接线完整），`seconds` 后置 stop。

    这里**不 monkeypatch `_media_worker_loop`**：那正是要验证的东西。
    """
    task = asyncio.create_task(worker_module.run_forever(poll_seconds=poll))
    try:
        await asyncio.sleep(seconds)
    finally:
        # `run_forever` 自己的 stop 事件由信号设置；测试里直接取消它。
        # 走 `cancel()` 而不是等内部 stop，是为了**同时**验证 finally 里
        # 媒体任务的取消与 await 是完整的（取消后不应有残留 task）。
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ================================================================ 1. 接线


async def test_run_forever_consumes_the_media_topic(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """**接线**：`run_forever` 启动后，`media.video.read` 事件会被消费。

    「改坏了会红」：把 `run_forever` 里那个 `create_task(_media_worker_loop(...))`
    删掉（也就是上一版的状态），作业会**永远停在 pending**——入口 202 说
    "已安排"，但永远没有结果。这条用例第一次运行前它就是红的。

    只测 `_media_worker_loop` 本身**不算**：那样删掉 create_task 依然全绿。
    """
    monkeypatch.setattr(
        worker_module, MEDIA_READ_TOPIC, _stub_read_handler, raising=False
    )
    monkeypatch.setitem(worker_module.HANDLERS, MEDIA_READ_TOPIC, _stub_read_handler)

    window_id, evidence_id = await _window_and_video(logged_in)
    call_id = await _pending_job_with_event(window_id, evidence_id)

    row = await _job(call_id)
    assert row is not None and row.state == "pending"

    await _run_worker_briefly(seconds=1.5)

    # 事件被消费（handler 真的被调用）
    assert call_id in _STUB_HANDLED, "media.video.read 事件没有被消费"
    # 作业走到终态（stub 直接标 succeeded）
    row = await _job(call_id)
    assert row is not None
    assert row.state != "pending", f"作业还停在 {row.state}——事件被消费了但作业没推进"
    event = await _event(call_id)
    assert event is not None
    assert event.state == "done", f"事件状态是 {event.state}，应为 done"


async def test_media_task_is_created_by_run_forever(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """更强的接线断言：`run_forever` 期间**存在**名为 `media-worker` 的 task。

    上一条靠"事件被消费"间接证明；这条直接看任务名。两条一起才够——
    如果哪天有人把 create_task 藏进某个条件分支里，事件那条可能still偶然
    通过（例如别的路径处理了它），任务名这条不会。
    """
    seen: list[str] = []

    async def _watch() -> None:
        for _ in range(40):
            seen.extend(
                task.get_name() for task in asyncio.all_tasks() if task.get_name()
            )
            await asyncio.sleep(0.05)

    watch = asyncio.create_task(_watch())
    await _run_worker_briefly(seconds=1.5)
    await watch

    assert "media-worker" in seen, (
        "run_forever 期间没有名为 media-worker 的任务——媒体循环没被拉起"
    )


# ================================================================ 2. 停止


async def test_stop_shuts_down_both_loops_without_leaking_tasks(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """`stop.set()` 后两条线都退出，**没有 task 泄漏**。

    「改坏了会红」：`finally` 里只 `media_task.cancel()` 而不 `await`，
    取消请求会挂着；更糟的是紧接着 `dispose_engine()` 把连接池拆掉，
    那个还在跑的任务会炸 "Event loop is closed"。
    """
    monkeypatch.setitem(worker_module.HANDLERS, MEDIA_READ_TOPIC, _stub_read_handler)

    before = {t.get_name() for t in asyncio.all_tasks()}
    await _run_worker_briefly(seconds=1.0)

    # ## 为什么要给两拍、还要按对象比对
    #
    # `cancel()` 只是**请求**取消；被取消的任务要到下一个循环迭代才真正结束。
    # 如果 `finally` 里 cancel 之后不 await，那个任务在读到这里时可能还没
    # 收尾——只比对"名字"会漏掉它（名字还在，但状态是 cancelling，不是 done）。
    #
    # 所以：(1) 多给几拍让它有机会完成；(2) 除了看名字，还看**数量**——
    # 残留一个 cancelling 状态的任务同样是泄漏。
    for _ in range(20):
        await asyncio.sleep(0.02)
        leftover = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and t.get_name() not in before
        ]
        if not leftover:
            break

    names = [t.get_name() for t in leftover]
    assert "media-worker" not in names, f"media-worker 任务没被取消/等待：{names}"
    assert not leftover, f"worker 退出后还有残留任务：{names}"
    # 没有任何残留任务处于 pending/cancelling 状态
    assert all(t.done() for t in leftover) if leftover else True


async def test_engine_is_disposed_only_after_the_media_loop_stops(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """**顺序**：`dispose_engine()` 必须在媒体任务真正收尾**之后**。

    这是"cancel 了却不 await"的真实危害：连接池先被拆掉，那个还在跑的任务
    下一次用连接就撞 "Event loop is closed" / "pool is closed"，而错误发生在
    关停路径上，日志里看不出根因。

    ## 怎么让这个竞态变成确定性的

    纯 `cancel()` 之后紧跟的 `await release_outbox_leases()` 有数据库往返，
    足够让被取消的任务跑完——所以"不 await"在上一条泄漏测试里**测不出来**
    （我验证过：变异后那条照样绿）。

    办法是让媒体任务**收尾得慢**：给它一个 0.3 秒的收尾（模拟关子进程/写日志），
    并记下"我收尾完了"。然后断言 `dispose_engine` 被调用时那个标记已经写下。
    `await media_task` 保证成立；不 await 时 0.3 秒的收尾一定还没跑完，
    标记为空——变异稳定为红。
    """
    stopped: list[str] = []
    gate = asyncio.Event()

    async def _slow_unwind(payload: dict) -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # 模拟收尾：关子进程、写日志
            stopped.append("media-loop-finished")
            raise

    monkeypatch.setitem(worker_module.HANDLERS, MEDIA_READ_TOPIC, _slow_unwind)

    # 让 dispose 时检查顺序
    observed_at_dispose: list[bool] = []

    async def fake_dispose() -> None:
        observed_at_dispose.append(bool(stopped))

    monkeypatch.setattr(worker_module, "dispose_engine", fake_dispose)

    window_id, evidence_id = await _window_and_video(logged_in)
    await _pending_job_with_event(window_id, evidence_id)

    await _run_worker_briefly(seconds=0.8)

    assert observed_at_dispose, "dispose_engine 没被调用"
    assert all(observed_at_dispose), (
        "dispose_engine 在媒体任务收尾之前就执行了——连接池被拆时它还在跑"
    )
    assert stopped == ["media-loop-finished"]


async def test_media_loop_stops_when_the_shared_event_is_set() -> None:
    """`_media_worker_loop` 复用调用方的 stop 事件（不用自己的）。

    这是"同进程双循环"的核心：一个 SIGTERM 同时停两条线。如果循环内部
    自建 `asyncio.Event()`，外部 set 的那个永远不会被它看到——主循环停了，
    媒体线还在跑，进程退不干净。
    """
    import inspect

    source = inspect.getsource(worker_module._media_worker_loop)
    # 签名里接受 stop，且函数体内**没有**自建 Event
    assert "stop: asyncio.Event" in source
    assert "asyncio.Event()" not in source, "循环内部不该自建 stop 事件"

    stop = asyncio.Event()
    task = asyncio.create_task(worker_module._media_worker_loop(stop, poll_seconds=0.05))
    await asyncio.sleep(0.15)
    assert not task.done(), "还没 set，循环不该退出"
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert task.done()


# ================================================================ 3. 隔离


async def test_slow_media_does_not_block_ordinary_events(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """主循环在跑慢媒体时仍能推进普通事件。

    `run_forever` 用 `exclude_topics` 排除媒体 topic，所以主循环根本不会抓到
    它——它的 outbox 轮次照常跑。这里用"媒体 handler 卡住"复现慢视频，
    断言普通事件仍被主循环消费。
    """
    gate = asyncio.Event()
    ordinary_handled: list[str] = []

    async def _slow_media(payload: dict) -> None:
        await gate.wait()

    async def _ordinary(payload: dict) -> None:
        ordinary_handled.append(str(payload.get("window_id") or ""))

    monkeypatch.setitem(worker_module.HANDLERS, MEDIA_READ_TOPIC, _slow_media)
    monkeypatch.setitem(worker_module.HANDLERS, "window.summary", _ordinary)

    window_id, evidence_id = await _window_and_video(logged_in)
    await _pending_job_with_event(window_id, evidence_id)
    # 一条普通事件
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

    try:
        await _run_worker_briefly(seconds=1.5)
    finally:
        gate.set()

    assert ordinary_handled, "慢媒体把普通事件也堵住了"


# ================================================================ 4. 不重复 init


async def test_run_forever_initialises_once_for_both_loops(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, _fast_worker: dict[str, int]  # noqa: F811
) -> None:
    """`init_engine` / `init_memory_router` 在 `run_forever` 里只调用**一次**。

    「改坏了会红」：如果 `run_forever` 调的是 `run_media_worker_forever()`
    （那个会自己 init），计数会变成 2——同进程两套连接池、两次 schema 等待、
    后者覆盖前者的信号处理器。

    这条是"拆成 `_media_worker_loop` + `run_media_worker_forever`"的**理由**
    的测试形式。
    """
    await _run_worker_briefly(seconds=0.8)

    assert _fast_worker["init_engine"] == 1, (
        f"init_engine 被调了 {_fast_worker['init_engine']} 次（同进程应只 1 次）"
    )
    assert _fast_worker["init_memory_router"] == 1, (
        f"init_memory_router 被调了 {_fast_worker['init_memory_router']} 次"
    )


# ================================================================ 5. 排除集合的边界


def test_exclusion_set_contains_only_the_outbox_media_topic() -> None:
    """排除集合里**只有** `media.video.read`——不能顺手排掉别的。

    三条确认：

    - `media.video.read` 被排除（它是慢的 outbox 事件，由媒体线消费）；
    - **`media.describe` 不在其中**（旧的自动描述队列仍在主 worker 里跑，
      它按 60 秒周期批量跑，不是分钟级的单条慢任务）；
    - **`media.read.cleanup` 也不在其中**（它是 `PERIODIC_JOBS` 里的周期任务，
      走 `_guarded` 直接调用，**不经 outbox**，所以根本不受这个集合影响）。
    """
    excluded = worker_module.MEDIA_EXCLUDED_FROM_MAIN_WORKER

    assert MEDIA_READ_TOPIC in excluded
    assert "media.describe" not in excluded, "旧的自动描述队列被误排除了"
    assert "media.read.cleanup" not in excluded

    # cleanup 必须仍在 PERIODIC_JOBS 里（它不走 outbox）
    periodic_topics = {job.topic for job in worker_module.PERIODIC_JOBS}
    assert "media.read.cleanup" in periodic_topics
    assert "media.describe" in periodic_topics
    # 而 media.video.read **不在**周期表里（它是事件驱动的）
    assert MEDIA_READ_TOPIC not in periodic_topics


def test_media_handler_is_registered_and_routed() -> None:
    """`media.video.read` 有 handler、在 `OUTBOX_TOPICS` 里、且被排除集合点名。

    三者缺一都会静默失效：缺 handler → 事件 `unhandled`；缺 topic 登记 →
    `test_worker_dispatch` 会红但运行期也少一层校验；缺排除 → 主循环去接慢任务。
    """
    assert MEDIA_READ_TOPIC in worker_module.HANDLERS
    assert MEDIA_READ_TOPIC in worker_module.OUTBOX_TOPICS
    assert MEDIA_READ_TOPIC in worker_module.MEDIA_EXCLUDED_FROM_MAIN_WORKER


async def test_stop_event_is_shared_between_both_loops() -> None:
    """`run_forever` 传给媒体循环的是**它自己的** stop 事件（源码级断言）。

    「改坏了会红」：传一个新建的 Event，主循环收到 SIGTERM 后停下，媒体线
    永远等不到——进程退不掉。行为上很难稳定复现（要看时序），所以直接断言
    接线形状。
    """
    import inspect

    source = inspect.getsource(worker_module.run_forever)
    assert "_media_worker_loop(stop" in source, (
        "run_forever 没有把自己的 stop 事件传给媒体循环"
    )
    assert "media_task.cancel()" in source
    assert "await media_task" in source, "取消后必须 await，否则任务可能还在跑"
