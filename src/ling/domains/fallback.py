"""上游依赖不可用时，回退到**领域自己的默认参数**。

## 这个模块解决什么

有些行为参数不归本域所有，而是"看上游现在怎样"决定的：

- 「多久看一次手机」要看日程（在忙就拉长、在睡就不排）；
- 「当前场景的有效期」要看日程某一段什么时候结束。

上游（日程）可能暂时**没有内容**（`empty` / `pending` / `failed`）或**根本读不到**
（`unavailable` / 超时）。这些情况下有两个错误做法：

1. **硬崩**：整块功能跟着上游一起不可用（用户看到"看手机状态读取失败"）；
2. **拿旧值/脏值顶**：把上一次的日程当成现在还成立——这会把"计划"写成
   "事实"，是本项目明令禁止的那一类伪造。

正确做法是第三条：**用本领域自己的默认参数**跑这一次，并**说清楚这是默认值**。
本模块就是这条规则的唯一实现，避免每个消费点各写一套判断。

## 三层语义（与既有代码保持一致）

- **可用**：`ready`（模型或规则大纲已提交）、`degraded`（规则兜底但内容可读）。
  但 `degraded` 只当"计划参考"，不当"当前场景事实"——它说的是"她大概要做这个"，
  不是"她正在做这个"。
- **不可用**：`pending` / `empty` / `failed` / `unavailable` / `unknown`。
- **过期即未知**：`scene_valid_until` 已过 → `unknown`，不沿用旧场景
  （`domains/presence/service.py::scene()` 已经这么做了，这里只统一判定）。

## 三条硬规矩

1. **回退值只能来自领域/人设默认**，不能来自旧值、历史脏值、上一次读到的上游。
   所以 `resolve_input()` **不接受**"上次的值"这个入参——从接口上就堵死。
2. **绝不把默认值写成事实**：本模块只返回一个带来源标记的值，不写
   `activities` / `self_experiences` / `day_plans` / `scene_commits`。
   写入权限仍在各自的状态机里。
3. **可观测**：回退发生时记一条人话行为日志，并把 `used / reason / state`
   放进对外响应（`input_sources`），用户能看到"这次用了默认值，因为……"。

## 为什么回退不是"降级告警"

上游为空（今天没安排）是**正常状态**，不是故障。所以这里用 `INFO` 级、
一句人话，而不是 WARNING——把"今天没安排日程"打成告警，会让真正的故障
淹没在噪声里（本项目已经踩过这个坑）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

# 状态与来源的**取值**由契约给出（`src/ling/contracts/v1.py`），这里只是给
# 消费方一个域名友好的别名。**不另起一套枚举**：
# 两套同名枚举会让 `is` 比较悄悄失败、schema 里出现两份 enum，
# 而前端只按 schema 读——那种分叉最难查（本模块的单元测试第一时间踩到了）。
from ling.contracts.v1 import FallbackSource, FallbackState

logger = logging.getLogger("ling.domains.fallback")


# 读上游的**有界预算**（秒）。设计 §4：上游超时要有界，不能因为日程读不回来
# 就把"看手机"这类轻量状态一起拖住。300 ms 是"本机数据库一次索引查询"的
# 量级；超过它说明上游真的出问题了，那就按不可用回退。
UPSTREAM_READ_BUDGET_SECONDS = 0.3


# 消费方看到的上游状态。**投影自** `contracts.v1.TimelineState` 等既有状态；
# 取值与契约的 `FallbackState` 完全一致（这里是别名，不是第二套枚举）。
DependencyState = FallbackState


# 「能作为当前输入」的状态。degraded **不在**这里：它只能作计划参考，
# 由调用方显式选择 `allow_degraded=True` 才接受。
USABLE_STATES: frozenset[DependencyState] = frozenset({DependencyState.ready})
# 可以拿来"当计划参考"的状态（表达层可用，事实层不可用）。
PLAN_REFERENCE_STATES: frozenset[DependencyState] = frozenset(
    {DependencyState.ready, DependencyState.degraded}
)


def is_usable(state: DependencyState, *, allow_degraded: bool = False) -> bool:
    """这个状态能不能当**当前输入**用。

    ``allow_degraded`` 只对"行为参数"（多久看一次手机之类）开放；
    **任何**要断言"她正在做什么"的地方都必须传默认值 ``False``——
    规则兜底的日程不是她此刻的事实。
    """
    if state in USABLE_STATES:
        return True
    return allow_degraded and state in PLAN_REFERENCE_STATES


def state_from_timeline(value: object) -> DependencyState:
    """把 `TimelineState`（或它的字符串）投影成上游状态。

    认不出来的一律 `unknown`：**不猜**，猜错会把"读不到"说成"没安排"。
    """
    raw = getattr(value, "value", value)
    try:
        return DependencyState(str(raw))
    except ValueError:
        return DependencyState.unknown


@dataclass(frozen=True)
class DependencyValue[T]:
    """一次上游读取的结果：状态 + 值（可能为空）+ 读取时刻。"""

    state: DependencyState
    value: T | None = None
    as_of: Any = None
    upstream: str = ""
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return is_usable(self.state)

    @property
    def has_value(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class ResolvedInput[T]:
    """消费边界拿到的最终输入：值 + 来源 + 上游状态 + 人话原因。"""

    value: T
    source: FallbackSource
    dependency_state: DependencyState
    upstream: str
    # 给人看的一句：为什么用了默认值（进响应）
    fallback_reason: str | None = None
    # 给排查看的一句：上游读取本身说了什么（**只进日志**，比如"超过 300 ms 预算"）
    dependency_reason: str | None = None

    @property
    def used_default(self) -> bool:
        return self.source is FallbackSource.domain_default

    def notice(self) -> dict[str, object]:
        """对外响应用的紧凑标记（**不是**内部错误码表）。

        只放用户/前端需要知道的四件事：上游是谁、这算不算回退、上游当时
        什么状态、为什么。内部堆栈、查询语句一律不进这里。
        """
        return {
            "upstream": self.upstream,
            "used": self.used_default,
            "state": self.dependency_state.value,
            "source": self.source.value,
            "reason": self.fallback_reason,
        }


def resolve_input[T](
    dependency: DependencyValue[T],
    *,
    default: T,
    default_reason: str,
    allow_degraded: bool = False,
) -> ResolvedInput[T]:
    """上游可用就用上游；不可用就用 ``default``（领域自己的默认参数）。

    ``default_reason`` 是**人话**，会在回退时原样进响应与行为日志，
    例如「这次用了默认节奏，因为今天的日程还没生成」。
    """
    if dependency.usable or (allow_degraded and is_usable(dependency.state, allow_degraded=True)):
        if dependency.value is not None:
            return ResolvedInput(
                value=dependency.value,
                source=FallbackSource.upstream,
                dependency_state=dependency.state,
                upstream=dependency.upstream,
                dependency_reason=dependency.reason,
            )
        # 状态说可用但**没有值**：这是上游自相矛盾（ready 却没有段）。
        # 结论是回退，但原因要如实说，不要把"空"伪装成"上游给的"。
        reason = f"{dependency.upstream} 状态是 {dependency.state.value}，但没有给出可用的值；{default_reason}"
        return ResolvedInput(
            value=default,
            source=FallbackSource.domain_default,
            dependency_state=dependency.state,
            upstream=dependency.upstream,
            fallback_reason=reason,
            dependency_reason=dependency.reason,
        )
    return ResolvedInput(
        value=default,
        source=FallbackSource.domain_default,
        dependency_state=dependency.state,
        upstream=dependency.upstream,
        fallback_reason=default_reason,
        dependency_reason=dependency.reason,
    )


async def read_within_budget[T](
    awaitable: Awaitable[T],
    *,
    upstream: str,
    budget: float = UPSTREAM_READ_BUDGET_SECONDS,
) -> DependencyValue[T]:
    """在有限预算内读一次上游；超时/异常一律折成 ``unavailable``。

    为什么把异常也吃掉：调用点（看手机、场景投影）是**轻量读**，上游的一次
    查询失败不该把它们整块带崩。但吞掉不等于隐瞒——异常类型会留在
    ``reason`` 里，并进行为日志。
    """
    try:
        value = await asyncio.wait_for(awaitable, timeout=budget)
    except TimeoutError:
        return DependencyValue(
            state=DependencyState.unavailable,
            upstream=upstream,
            reason=f"读 {upstream} 超过 {int(budget * 1000)} ms 预算",
        )
    except Exception as exc:  # noqa: BLE001 - 上游读失败按不可用回退，但要留原因
        logger.info("上游读取失败 upstream=%s error=%s", upstream, type(exc).__name__)
        return DependencyValue(
            state=DependencyState.unavailable,
            upstream=upstream,
            reason=f"读 {upstream} 失败（{type(exc).__name__}）",
        )
    return DependencyValue(value=value, state=DependencyState.ready, upstream=upstream)


def log_fallback[T](resolved: ResolvedInput[T], *, consumer: str) -> None:
    """回退时记一条**人话**行为日志（INFO，不是 WARNING）。

    只在真的回退时记：上游可用时这里一声不响，否则日志会被"一切正常"刷满。
    日志只放域/上游/状态，不放值本身——值里可能有自由文本。
    """
    if not resolved.used_default:
        return
    try:
        from ling.audit import behavior_event

        behavior_event(
            "upstream_fallback",
            f"{consumer}：这次用了默认参数（{resolved.fallback_reason or '上游不可用'}）",
            attributes={
                "consumer": consumer,
                "upstream": resolved.upstream,
                "upstream_state": resolved.dependency_state.value,
                "source": resolved.source.value,
                # 上游自己说了什么（超时 / 查询异常）：只进日志，不进响应
                "upstream_detail": resolved.dependency_reason,
            },
        )
    except Exception:  # noqa: BLE001 - 观测失败不能影响业务
        logger.info(
            "上游回退 consumer=%s upstream=%s state=%s",
            consumer,
            resolved.upstream,
            resolved.dependency_state.value,
        )


def not_fallback(upstream: str, state: DependencyState = DependencyState.ready) -> ResolvedInput[None]:
    """构造一个"没有回退"的标记（给不需要值的调用点用，如响应拼装）。"""
    return ResolvedInput(
        value=None, source=FallbackSource.upstream, dependency_state=state, upstream=upstream
    )


@dataclass(frozen=True)
class InputSources:
    """对外响应里的 `input_sources`：名字 → 紧凑标记。"""

    notices: dict[str, dict[str, object]] = field(default_factory=dict)

    def add(self, name: str, resolved: ResolvedInput[Any]) -> InputSources:
        merged = dict(self.notices)
        merged[name] = resolved.notice()
        return InputSources(notices=merged)

    def as_dict(self) -> dict[str, dict[str, object]]:
        return dict(self.notices)


__all__ = [
    "DependencyState",
    "DependencyValue",
    "FallbackSource",
    "InputSources",
    "PLAN_REFERENCE_STATES",
    "ResolvedInput",
    "UPSTREAM_READ_BUDGET_SECONDS",
    "USABLE_STATES",
    "is_usable",
    "log_fallback",
    "not_fallback",
    "read_within_budget",
    "resolve_input",
    "state_from_timeline",
]
