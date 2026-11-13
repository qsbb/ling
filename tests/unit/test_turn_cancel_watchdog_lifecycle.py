"""Stop watchdog task lifecycle regression tests."""

from __future__ import annotations

import asyncio

import pytest

import ling.kernel.turn as turn_module


@pytest.mark.asyncio
async def test_finished_stopped_turn_removes_delayed_force_cancel_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    force_cancel_tasks: list[asyncio.Task] = []

    class LoopProxy:
        def __getattr__(self, name: str) -> object:
            return getattr(loop, name)

        def create_task(self, coroutine, *args, **kwargs) -> asyncio.Task:  # noqa: ANN001
            task = loop.create_task(coroutine, *args, **kwargs)
            force_cancel_tasks.append(task)
            return task

    class AsyncioProxy:
        def __getattr__(self, name: str) -> object:
            return getattr(asyncio, name)

        def get_running_loop(self) -> LoopProxy:
            return LoopProxy()

        def create_task(self, coroutine, *args, **kwargs) -> asyncio.Task:  # noqa: ANN001
            task = loop.create_task(coroutine, *args, **kwargs)
            force_cancel_tasks.append(task)
            return task

    monkeypatch.setattr(turn_module, "asyncio", AsyncioProxy())
    monkeypatch.setattr(turn_module, "FORCE_CANCEL_AFTER_SECONDS", 3600)

    registry = turn_module.CancelRegistry()
    turn_id = "turn-watchdog-lifecycle"
    token = turn_module.StopToken()
    finished = asyncio.Event()

    async def run_turn() -> None:
        await token.wait()

    turn_task = asyncio.create_task(run_turn(), name=f"turn:{turn_id}")
    registry.register(turn_id, token, turn_task)

    def cleanup(_: asyncio.Task) -> None:
        registry.discard(turn_id)
        finished.set()

    turn_task.add_done_callback(cleanup)

    assert registry.stop(turn_id)
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert len(force_cancel_tasks) == 1

    watchdog = force_cancel_tasks[0]
    await asyncio.sleep(0)
    assert watchdog.cancelled(), "已结束的轮次仍留下待执行的强制取消任务"
    assert watchdog not in asyncio.all_tasks(loop), "watchdog 仍残留在事件循环任务集中"
    assert not registry.is_running(turn_id)
