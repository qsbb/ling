"""Worker 派发闸：注册了 handler，就必须有派发源（或**显式**声明只能手工跑）。

## 为什么需要这个文件

handler 注册了却没有任何调度来源时，它**等于不存在**：
代码在这儿、测试全绿、日志无异常，功能就是不发生。

实际查出来两个躺了很久的：

- `media.describe`：`MediaDescriber` 从未被实例化，整条媒体描述链路是死的
- `self.timeline`：`self_view` 会**读**她的时间线，但没有任何东西**写**它，
  所以读到的永远是空

所以定时任务统一收进 `PERIODIC_JOBS`，事件驱动的收进 `OUTBOX_TOPICS`。
两边都查得到才算"能跑"。

## 第三类：只保留手工触发（`LEGACY_MANUAL_TOPICS`）

有些 handler **有意**不再自动跑。最典型的是 `life.plan`：新时间轴
（`life.schedule` + `schedule.outline` + `day_plans`）已经接管"今天/明天做
什么"，旧路径再自动生成就是同一段时间轴上的第二份安排。

但"不再自动跑"和"忘了接调度"在代码里长得一模一样。如果只是把它从
`PERIODIC_JOBS` 删掉，闸门会红；如果给它随便塞一个假派发源，闸门会绿而
事实是假的。所以显式列出 `LEGACY_MANUAL_TOPICS`：**写清楚意图**，同时保留
手工/运维触发的能力（旧数据修复还得用得上）。

## 四条不变式

1. 每个 `HANDLERS` 里的 topic 必须在 `PERIODIC_JOBS` / `OUTBOX_TOPICS` /
   `LEGACY_MANUAL_TOPICS` 之一
2. 后三者里的每个 topic 必须真的注册了 handler
3. `OUTBOX_TOPICS` 里的每个 topic 必须真的有人在 enqueue 它
4. `LEGACY_MANUAL_TOPICS` 不得同时出现在自动派发源里（否则"手工"就是空话）
"""

from __future__ import annotations

import pathlib
import re

from ling.worker import (
    HANDLERS,
    LEGACY_MANUAL_TOPICS,
    OUTBOX_TOPICS,
    PERIODIC_JOBS,
    _should_run_maintenance,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"

PERIODIC_TOPICS = frozenset(job.topic for job in PERIODIC_JOBS)


def test_every_handler_has_a_dispatch_source() -> None:
    """注册了却没派发源的 handler = 功能不存在。

    这是最要紧的一条：`media.describe` 和 `self.timeline` 都是这么白躺的。
    有意不自动跑的，必须在 `LEGACY_MANUAL_TOPICS` 里写出来。
    """
    dispatched = PERIODIC_TOPICS | OUTBOX_TOPICS | LEGACY_MANUAL_TOPICS
    orphans = sorted(set(HANDLERS) - dispatched)
    assert not orphans, (
        f"这些 handler 注册了但没有任何派发源：{orphans}\n"
        f"要么加进 PERIODIC_JOBS（周期任务），要么加进 OUTBOX_TOPICS（事件驱动），"
        f"要么加进 LEGACY_MANUAL_TOPICS（有意只留手工触发）。"
    )


def test_no_dispatch_entry_without_a_handler() -> None:
    """反过来：声明了要派发却没人注册 handler，会静默什么都不发生。"""
    declared = PERIODIC_TOPICS | OUTBOX_TOPICS | LEGACY_MANUAL_TOPICS
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


def test_legacy_topics_are_not_auto_scheduled() -> None:
    """`LEGACY_MANUAL_TOPICS` 不能同时挂在自动派发源上。

    两边都列等于"假装手工"：跑到的时候还是会自动生成，闸门却绿着。
    """
    overlap = sorted(LEGACY_MANUAL_TOPICS & (PERIODIC_TOPICS | OUTBOX_TOPICS))
    assert not overlap, f"这些 topic 同时被声明为「只手工」和「自动派发」：{overlap}"


def test_legacy_life_plan_is_not_auto_generated_anymore() -> None:
    """旧 `life.plan` 必须退出自动调度。

    新时间轴启用后，两套并行生成就是两份日程：`agenda_plans` 一套、
    `day_plans` 一套，`life.tick` 与界面各看一半。这条把"退出自动调度"
    钉在结构上——不是靠某个进程内变量或某次部署的注释。
    """
    assert "life.plan" in LEGACY_MANUAL_TOPICS
    assert "life.plan" not in PERIODIC_TOPICS, "旧 life.plan 又被排进周期任务了"
    assert "life.plan" not in OUTBOX_TOPICS, "旧 life.plan 又被当成事件驱动任务了"


def test_life_schedule_scan_is_minute_grained() -> None:
    """`life.schedule` 的扫描周期必须是分钟级。

    21:00 到期点靠这个扫描发现；周期一旦被调成小时级，最坏会晚 59 分钟
    才投递大纲——而"21:00 前夜为明天生成"是这个方案的全部前提。
    """
    job = next(job for job in PERIODIC_JOBS if job.topic == "life.schedule")
    assert job.interval <= 60.0, f"life.schedule 周期是 {job.interval} 秒，超过 60 秒"


def test_new_schedule_topics_are_registered() -> None:
    """时间轴/梦/结算/重排的 outbox topic 必须都在。

    `schedule.replan` 是阶段 3 的：**由会话层投递**（用户消息落库时命中提议
    候选），不是周期任务——没有提议就没有事件，所以它只能在
    `OUTBOX_TOPICS` 里、不在 `PERIODIC_JOBS` 里。
    """
    expected = {
        "schedule.outline",
        "schedule.seam",
        "schedule.refine",
        "schedule.replan",
        "dream.generate",
        "sleep.settle",
    }
    assert expected <= set(HANDLERS)
    assert expected <= OUTBOX_TOPICS


def test_replan_topic_is_enqueued_from_the_turn_side() -> None:
    """`schedule.replan` 必须由**轮次收尾**投递，而且不带聊天正文。

    两个都重要：

    - 入队方只能有一处（轮次收尾），否则两条链路各自投一次，同一轮会被判
      两次——`turn_id` 的幂等键就白设了；
    - payload 里不能出现用户原文（`text`），否则每一句可能成提议的聊天都会被
      抄进 `outbox_events.payload`，凭空多一处隐私副本。worker 按 `turn_id`
      回读那一条消息即可。
    """
    turn = (SRC / "kernel" / "turn.py").read_text(encoding="utf-8")
    assert "note_schedule_proposal" in turn, "轮次收尾没有接提议提取的入口"
    replan = (SRC / "domains" / "agenda" / "replan.py").read_text(encoding="utf-8")
    assert '"schedule.replan"' in replan
    payload_block = replan.split('topic="schedule.replan"', 1)[1].split(")", 1)[0]
    assert '"text"' not in payload_block, "outbox payload 里不该出现聊天原文"


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
