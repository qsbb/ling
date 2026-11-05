"""Worker 派发闸：注册了 handler，就必须有派发源。

## 为什么需要这个文件

handler 注册了却没有任何调度来源时，它**等于不存在**：
代码在这儿、测试全绿、日志无异常，功能就是不发生。

实际查出来两个躺了很久的：

- `media.describe`：`MediaDescriber` 从未被实例化，整条媒体描述链路是死的
- `self.timeline`：`self_view` 会**读**她的时间线，但没有任何东西**写**它，
  所以读到的永远是空

所以定时任务统一收进 `PERIODIC_JOBS`，事件驱动的收进 `OUTBOX_TOPICS`。
两边都查得到才算"能跑"。

## 三条不变式

1. 每个 `HANDLERS` 里的 topic 必须在 `PERIODIC_JOBS` 或 `OUTBOX_TOPICS` 里
2. `PERIODIC_JOBS` / `OUTBOX_TOPICS` 里的每个 topic 必须真的注册了 handler
3. `OUTBOX_TOPICS` 里的每个 topic 必须真的有人在 enqueue 它
"""

from __future__ import annotations

import pathlib
import re

from ling.worker import (
    HANDLERS,
    OUTBOX_TOPICS,
    PERIODIC_JOBS,
    _should_run_maintenance,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"


def test_every_handler_has_a_dispatch_source() -> None:
    """注册了却没派发源的 handler = 功能不存在。

    这是最要紧的一条：`media.describe` 和 `self.timeline` 都是这么白躺的。
    """
    dispatched = {job.topic for job in PERIODIC_JOBS} | set(OUTBOX_TOPICS)
    orphans = sorted(set(HANDLERS) - dispatched)
    assert not orphans, (
        f"这些 handler 注册了但没有任何派发源：{orphans}\n"
        f"要么加进 PERIODIC_JOBS（周期任务），要么加进 OUTBOX_TOPICS（事件驱动）。"
    )


def test_no_dispatch_entry_without_a_handler() -> None:
    """反过来：声明了要派发却没人注册 handler，会静默什么都不发生。"""
    declared = {job.topic for job in PERIODIC_JOBS} | set(OUTBOX_TOPICS)
    ghosts = sorted(declared - set(HANDLERS))
    assert not ghosts, f"这些 topic 声明了但没注册 handler：{ghosts}"


def test_periodic_jobs_point_at_the_registered_function() -> None:
    """`PeriodicJob.run` 必须是对应 topic 真正注册的那个函数。

    写错成别的函数时，调度器会勤快地调用一个不相干的东西——
    表面上"任务跑了"，实际上什么都没做。
    """
    for job in PERIODIC_JOBS:
        registered = HANDLERS[job.topic]
        assert job.run is registered, (
            f"{job.topic} 登记的 run 不是注册的 handler"
            f"（{getattr(job.run, '__name__', job.run)} != "
            f"{getattr(registered, '__name__', registered)}）"
        )


def test_outbox_topics_are_actually_enqueued() -> None:
    """`OUTBOX_TOPICS` 里的每个 topic 必须真的有人在入队。"""
    blob = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    missing: list[str] = []
    for topic in sorted(OUTBOX_TOPICS):
        if not re.search(rf'["\']{re.escape(topic)}["\']', blob):
            missing.append(topic)
    assert not missing, f"这些 outbox topic 没人入队：{missing}"


def test_job_intervals_are_sane() -> None:
    """周期必须是正数，且不能长到等于不存在。

    上限取 24 小时：比一天还长的周期在单用户场景下等于"永不运行"，
    真需要那种节奏应该是事件驱动，不是定时。
    """
    for job in PERIODIC_JOBS:
        assert job.interval > 0, f"{job.topic} 的周期不是正数"
        assert job.interval <= 24 * 3600, f"{job.topic} 的周期超过 24 小时，等于永不运行"


def test_should_run_fires_immediately_on_first_call() -> None:
    """首次调用必须立即触发（last=0）。

    这条防的是"新加的任务要等一个完整周期才开始跑"——
    表现是刚部署完什么都不动，过一阵突然开始。
    """
    assert _should_run_maintenance(0.0, 1000.0, interval=300.0) is True
    assert _should_run_maintenance(1000.0, 1001.0, interval=300.0) is False
    assert _should_run_maintenance(0.0, 1001.0, interval=300.0) is True
