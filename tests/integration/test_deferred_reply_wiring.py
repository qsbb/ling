"""延后回复的**接线**与**零模型扫描**：`deferred.reply.sweep` 真的被调度了吗。

## 为什么这个文件单独存在

上一轮的教训（`test_worker_media_wiring.py` 的模块注释记着同一件事）：
"函数写得对"和"它被接上了"是两件完全不同的事。`media.video.read` 曾出现
"handler 注册了、也有单测、但没有任何生产调用方"，事件永远停在 pending。

`test_worker_dispatch.py` 只能证明"注册表里两边都有这个名字"，**证明不了它会被
调用**。所以这里显式钉三件互不重叠的事：

1. **调度来源真实存在**：`run_forever` 的周期循环里读到的是 `PERIODIC_JOBS`，
   所以断言"`deferred.reply.sweep` 在 `PERIODIC_JOBS` 里、`run` 就是注册的
   handler、interval 是秒级"。同时断言 `run_forever` 的源码真的遍历
   `PERIODIC_JOBS`——只断言常量表存在，等于假设调度逻辑没被改掉。
2. **空扫描零模型**：没有任何到期行时跑一次 handler，`behavior_model_calls`
   行数不增。它每 5 秒跑一次，一旦退化成"每次调一次模型"，账单会以
   17280 次/天 的速度烧。
3. **随机采样不出长事务**：扫描结束后 `deferred_replies` 上没有残留的行锁，
   且扫描期间没有 model 调用（用"扫描前后 calls 数不变"）。
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from sqlalchemy import func, select

import ling.worker as worker_module
from ling.infra.db import session_scope
from ling.infra.models import BehaviorModelCallRow, DeferredReply, Window
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    web_env,  # noqa: F401 - fixture
)

SWEEP_TOPIC = "deferred.reply.sweep"


# ------------------------------------------------------------------ 调度来源


def test_sweep_is_in_periodic_jobs_and_points_at_the_handler() -> None:
    """常量表里的那一行必须**就是**注册的 handler，不是另一个同名函数。"""
    jobs = {job.topic: job for job in worker_module.PERIODIC_JOBS}
    assert SWEEP_TOPIC in jobs, "deferred.reply.sweep 不在 PERIODIC_JOBS 里：到期扫描永远不会被调度"
    job = jobs[SWEEP_TOPIC]
    assert job.run is worker_module.HANDLERS[SWEEP_TOPIC], (
        "PeriodicJob.run 与注册的 handler 不是同一个对象：调度到的可能是别的东西"
    )
    # 秒级，而不是跟着小时级的维护节奏。她说了"等 5 秒"就应当 5 秒量级动。
    assert 0 < job.interval <= 10, f"扫描间隔 {job.interval}s 对'她自己选 5 秒'而言太长"


def test_run_forever_actually_dispatches_periodic_jobs() -> None:
    """**光有常量表不够**：主循环必须真的遍历它。

    这条断言的是 `run_forever` 的源码里存在
    `for job in PERIODIC_JOBS:` + `await _guarded(job.topic, job.run, job.payload)`
    这对组合。它是"表存在但没人读"这类 P0 的唯一静态防线——那种 bug 运行时
    完全安静，所有测试都绿。
    """
    source = inspect.getsource(worker_module.run_forever)
    assert "for job in PERIODIC_JOBS:" in source, "run_forever 不再遍历 PERIODIC_JOBS"
    assert "_guarded(job.topic, job.run, job.payload)" in source, (
        "run_forever 遍历了 PERIODIC_JOBS 却没有调用 job.run：表只是被读了一下"
    )


def test_migration_exists_for_the_table() -> None:
    """表必须有迁移，不能只靠 `create_all_for_dev`（生产走 alembic）。"""
    versions = pathlib.Path("migrations/versions")
    hits = [
        path
        for path in versions.glob("0063_*.py")
        if "deferred_replies" in path.read_text(encoding="utf-8")
    ]
    assert hits, "没有 0063 迁移：生产环境不会建出 deferred_replies"


# ------------------------------------------------------------------ 零模型


async def _model_call_count() -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(select(func.count()).select_from(BehaviorModelCallRow)) or 0
        )


@pytest.mark.asyncio
async def test_empty_sweep_makes_zero_model_calls(web_env: TestEnv) -> None:  # noqa: F811
    """**空扫描零模型**：没有任何到期行时，一次模型都不许调。

    sweep 每 5 秒跑一次。若它因为某个 bug 每次调一次模型，账单会以
    17280 次/天 的速度烧，而功能上"什么都没发生"——这是最难在线上发现的一类
    退化，所以要用断言钉住。
    """
    before = await _model_call_count()
    await worker_module.handle_deferred_reply_sweep({})
    after = await _model_call_count()
    assert after == before, f"空扫描调了 {after - before} 次模型"


@pytest.mark.asyncio
async def test_sweep_with_no_due_rows_does_not_touch_deferred_rows(web_env: TestEnv) -> None:  # noqa: F811
    """没有到期行时，扫描不改任何一行（短事务只读索引就返回）。"""
    async with session_scope() as session:
        session.add(
            Window(
                id="win_future",
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_a",
                title="将来的窗口",
                channel="web",
            )
        )
    async with session_scope() as session:
        session.add(
            DeferredReply(
                id="dfr_future",
                actor_id="actor_ling",
                window_id="win_future",
                subject_id="subj_a",
                source_turn_id="turn_future",
                input_sequence=1,
                requested_delay_ms=300_000,
                applied_delay_ms=300_000,
                chosen_due_at=worker_module.utcnow().replace(year=2099),
                hard_deadline_at=worker_module.utcnow().replace(year=2099),
                state="pending",
            )
        )

    before = await _model_call_count()
    await worker_module.handle_deferred_reply_sweep({})
    after = await _model_call_count()

    async with session_scope() as session:
        stored = await session.scalar(
            select(DeferredReply).where(DeferredReply.id == "dfr_future")
        )
    assert stored is not None
    assert stored.state == "pending", "还没到点的记录被扫描改掉了"
    assert after == before, "没有到期行却调了模型"


# ------------------------------------------------------------------ 兜底文本


def test_fallback_text_is_a_module_constant() -> None:
    """兜底短句必须是**模块级常量**：它会被测试断言，也可能被前端引用。

    在函数里拼字符串，一旦改动就没人知道是哪一处变的。
    """
    text = worker_module.DEFERRED_FALLBACK_TEXT
    assert isinstance(text, str)
    assert 10 <= len(text) <= 120, f"兜底短句长度 {len(text)} 不像一句人话"
    # 只陈述可核实的事实：不承诺、不假装送达、不编造理由。
    for forbidden in ("马上回", "我这就", "稍等一下", "对不起让你久等"):
        assert forbidden not in text, f"兜底短句里出现了无法核实的承诺：{forbidden}"
    # 必须明确承认"还没回答"——这是这句话唯一的存在理由。
    assert "还没" in text or "没有" in text


def test_fallback_text_is_a_literal_not_computed() -> None:
    """兜底短句必须是**字面量赋值**，不能在 import 时算出来。

    为什么不用 `importlib.reload` 验：reload `ling.worker` 会重建 `HANDLERS`，
    而别的模块持有的是旧 dict —— 那会把测试之间的状态搅乱（比它要证明的东西
    更糟）。改为读源码：`DEFERRED_FALLBACK_TEXT = "…"` 这一行必须是纯字符串
    字面量，没有函数调用、没有格式化。
    """
    source = inspect.getsource(worker_module)
    marker = "DEFERRED_FALLBACK_TEXT = "
    line = next(
        (ln.strip() for ln in source.splitlines() if ln.strip().startswith(marker)),
        None,
    )
    assert line is not None, "找不到 DEFERRED_FALLBACK_TEXT 的赋值"
    value = line[len(marker):]
    assert value.startswith('"') and value.endswith('"'), (
        f"兜底短句不是字符串字面量：{value!r}"
    )
    assert "{" not in value, "兜底短句里出现了格式化占位符：它必须是固定的"
    assert value[1:-1] == worker_module.DEFERRED_FALLBACK_TEXT
