"""上游回退契约（`domains/fallback.py`）：三态判定 + "值必须来自默认，不是旧值"。

这一组钉的是**取舍**，不是实现细节：

- `ready` 才能当当前输入；`degraded` 只作计划参考（`allow_degraded` 显式开）；
- `empty` / `pending` / `failed` / `unavailable` / `unknown` 一律不可用；
- 场景过期投影成 `empty`（不是"她还在做"）；
- 回退值来自传进来的 `default`，接口上**没有**"上次的值"这个入参。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from ling.contracts.v1 import FallbackNotice
from ling.domains.fallback import (
    DependencyState,
    DependencyValue,
    FallbackSource,
    InputSources,
    is_usable,
    log_fallback,
    read_within_budget,
    resolve_input,
    state_from_timeline,
)

pytestmark = pytest.mark.w0_web


# ---------------------------------------------------------------- 三态判定


@pytest.mark.parametrize("state", ["ready"])
def test_ready_is_usable(state: str) -> None:
    assert is_usable(DependencyState(state))


@pytest.mark.parametrize("state", ["degraded", "empty", "pending", "failed", "unavailable", "unknown"])
def test_everything_else_is_not_usable_by_default(state: str) -> None:
    assert not is_usable(DependencyState(state)), state


def test_degraded_is_only_a_plan_reference() -> None:
    """规则兜底的日程能当"计划参考"，但**不能**当"她此刻在做什么"。"""
    assert is_usable(DependencyState.degraded, allow_degraded=True)
    assert not is_usable(DependencyState.degraded)


def test_timeline_state_projection() -> None:
    """`TimelineState` 的六个取值都要能投影；认不出来的按 unknown（不猜）。"""
    from ling.contracts.v1 import TimelineState

    for state in TimelineState:
        assert state_from_timeline(state).value == state.value
    assert state_from_timeline("ready") is DependencyState.ready
    assert state_from_timeline(None) is DependencyState.unknown
    assert state_from_timeline("nonsense") is DependencyState.unknown


# ---------------------------------------------------------------- 回退值来源


def test_fallback_value_comes_from_domain_default() -> None:
    """不可用时用 `default`——不是旧值、不是"上个版本"。

    接口上根本没有"旧值"这个入参，这条测试守的是"以后别偷偷加回来"。
    """
    dep = DependencyValue[int](state=DependencyState.pending, upstream="schedule")
    resolved = resolve_input(dep, default=10, default_reason="日程还没生成")
    assert resolved.value == 10
    assert resolved.source is FallbackSource.domain_default
    assert resolved.used_default
    assert resolved.dependency_state is DependencyState.pending
    assert resolved.fallback_reason == "日程还没生成"


def test_upstream_value_wins_when_ready() -> None:
    dep = DependencyValue[int](state=DependencyState.ready, value=45, upstream="schedule")
    resolved = resolve_input(dep, default=10, default_reason="不该用到")
    assert resolved.value == 45
    assert resolved.source is FallbackSource.upstream
    assert not resolved.used_default
    assert resolved.fallback_reason is None


def test_ready_without_value_still_falls_back_and_says_so() -> None:
    """状态说可用却没值（上游自相矛盾）：回退，但原因要如实说。"""
    resolved = resolve_input(
        DependencyValue[int](state=DependencyState.ready, upstream="schedule"),
        default=10,
        default_reason="按默认节奏",
    )
    assert resolved.value == 10
    assert resolved.used_default
    assert "没有给出可用的值" in (resolved.fallback_reason or "")


def test_degraded_is_rejected_unless_asked() -> None:
    dep = DependencyValue[int](state=DependencyState.degraded, value=30, upstream="schedule")
    strict = resolve_input(dep, default=10, default_reason="规则兜底只当参考")
    assert strict.value == 10 and strict.used_default
    loose = resolve_input(dep, default=10, default_reason="x", allow_degraded=True)
    assert loose.value == 30 and not loose.used_default


# ---------------------------------------------------------------- 对外标记


def test_notice_matches_the_contract_model() -> None:
    """标记必须能过契约模型（形状只有一份，前端照 schema 读）。"""
    dep = DependencyValue[int](state=DependencyState.empty, upstream="schedule")
    notice = resolve_input(dep, default=10, default_reason="今天还没有日程").notice()
    model = FallbackNotice.model_validate(notice)
    assert model.used is True
    assert model.source is FallbackSource.domain_default
    assert model.state is DependencyState.empty
    assert model.reason == "今天还没有日程"


def test_notice_without_fallback_is_explicit() -> None:
    """"没用默认值"也要说出来——缺字段会让前端分不清"没用回退"和"没实现"。"""
    dep = DependencyValue[int](state=DependencyState.ready, value=5, upstream="schedule")
    model = FallbackNotice.model_validate(
        resolve_input(dep, default=10, default_reason="x").notice()
    )
    assert model.used is False
    assert model.source is FallbackSource.upstream
    assert model.reason is None


def test_input_sources_merges_by_name() -> None:
    dep = DependencyValue[int](state=DependencyState.failed, upstream="schedule")
    first = resolve_input(dep, default=10, default_reason="日程没生成成功")
    second = resolve_input(
        DependencyValue[int](state=DependencyState.ready, value=1, upstream="scene"),
        default=0,
        default_reason="x",
    )
    sources = InputSources().add("schedule", first).add("scene", second)
    assert set(sources.as_dict()) == {"schedule", "scene"}
    assert sources.as_dict()["schedule"]["used"] is True
    assert sources.as_dict()["scene"]["used"] is False


# ---------------------------------------------------------------- 有界读取


async def test_read_within_budget_times_out_to_unavailable() -> None:
    async def slow() -> int:
        await asyncio.sleep(0.05)
        return 1

    dep = await read_within_budget(slow(), upstream="schedule", budget=0.001)
    assert dep.state is DependencyState.unavailable
    assert "预算" in (dep.reason or "")
    assert dep.value is None


async def test_read_within_budget_swallows_exceptions_but_keeps_reason() -> None:
    async def boom() -> int:
        raise RuntimeError("数据库炸了")

    dep = await read_within_budget(boom(), upstream="schedule")
    assert dep.state is DependencyState.unavailable
    assert "RuntimeError" in (dep.reason or "")


async def test_read_within_budget_passes_ready_value_through() -> None:
    async def quick() -> int:
        return 42

    dep = await read_within_budget(quick(), upstream="schedule")
    assert dep.state is DependencyState.ready
    assert dep.value == 42


# ---------------------------------------------------------------- 可观测


def test_fallback_writes_one_human_line(caplog: pytest.LogCaptureFixture) -> None:
    dep = DependencyValue[int](state=DependencyState.pending, upstream="schedule")
    resolved = resolve_input(dep, default=10, default_reason="今天的日程还没生成")
    with caplog.at_level(logging.INFO, logger="ling.behavior"):
        log_fallback(resolved, consumer="phone.next_look")
    lines = [r.getMessage() for r in caplog.records if r.name == "ling.behavior"]
    assert any("这次用了默认参数" in line for line in lines), lines
    assert any("今天的日程还没生成" in line for line in lines), lines


def test_no_fallback_writes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    dep = DependencyValue[int](state=DependencyState.ready, value=5, upstream="schedule")
    resolved = resolve_input(dep, default=10, default_reason="x")
    with caplog.at_level(logging.INFO, logger="ling.behavior"):
        log_fallback(resolved, consumer="phone.next_look")
    assert not [r for r in caplog.records if r.name == "ling.behavior"]
