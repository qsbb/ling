"""视频后台取证作业的状态机：**纯函数**，穷举每条边。

裁决 `docs/ASYNC-TOOL-DECISION.md` §3 第 2 步要求"状态机的合法转移要写成一个
纯函数并单测（非法转移抛错）"。这里就是那个单测。

## 为什么必须穷举

状态机的错都是**并发下才暴露**的：两个 worker 抢同一行、晚到的结果想覆盖已终结
的作业。这些路径很难在集成里复现，但"允许哪些边"是一个有限集合——把 9 个状态
的 81 种组合全打一遍表，就能证明"没有任何一条非法边被放过"。

## 两条不可让的性质

1. **终态是吸收态**：`succeeded` 之后不能再变 `failed`。否则"结果已落库"和
   "作业说失败了"会同时成立，读侧与对账各按一个行动。
2. **同状态转移非法**：`pending -> pending` 不是转移，是一次空写。允许它会让
   重复投递看起来像成功，从而把真正的状态冲突掩盖掉。
"""

from __future__ import annotations

import pytest

from ling.domains.media.jobs import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidJobTransition,
    JobState,
    is_terminal,
    outbox_event_id_for,
    transition,
)

ALL_STATES = list(JobState)

#: 每条**合法**边。新增状态时必须同时在 `ALLOWED_TRANSITIONS` 和这里加。
LEGAL_EDGES: list[tuple[JobState, JobState]] = [
    # pending：还没开始，可以进执行、可以被取消、可以排到过期、可以没额度
    (JobState.pending, JobState.running),
    (JobState.pending, JobState.cancelled),
    (JobState.pending, JobState.expired),
    (JobState.pending, JobState.budget_denied),
    # running：执行中的六种结局
    (JobState.running, JobState.succeeded),
    (JobState.running, JobState.failed),
    (JobState.running, JobState.timeout),
    (JobState.running, JobState.unknown),
    (JobState.running, JobState.cancelled),
    (JobState.running, JobState.expired),
]


@pytest.mark.parametrize(("current", "target"), LEGAL_EDGES)
def test_every_legal_edge_is_accepted(current: JobState, target: JobState) -> None:
    """每种合法转移都通过，且返回目标状态（纯函数：不改入参）。"""
    assert transition(current, target) is target


def test_legal_edges_are_exactly_the_table() -> None:
    """`LEGAL_EDGES` 与 `ALLOWED_TRANSITIONS` **完全相等**。

    只查"每条合法边都过"会让表里多出来的边没人验；反过来只查"非法边都拒"
    会让少写的边变成静默拒绝（作业永远卡住）。两个方向都要钉。
    """
    from_table = {
        (current, target)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert from_table == set(LEGAL_EDGES)


def _all_pairs() -> list[tuple[JobState, JobState]]:
    return [(a, b) for a in ALL_STATES for b in ALL_STATES]


@pytest.mark.parametrize(("current", "target"), _all_pairs())
def test_every_other_pair_is_rejected(current: JobState, target: JobState) -> None:
    """**其余全部组合**都必须抛 `InvalidJobTransition`（逐条断言，不是抽样）。

    这覆盖了三类非法：
    1. 终态出发的任何边（吸收态）；
    2. 同状态转移（空写）；
    3. 表里没写的跨状态跳跃（例如 `pending -> succeeded`——没跑就成功）。
    """
    if (current, target) in set(LEGAL_EDGES):
        pytest.skip("合法边，由 test_every_legal_edge_is_accepted 覆盖")
    with pytest.raises(InvalidJobTransition) as exc:
        transition(current, target)
    assert exc.value.current is current
    assert exc.value.target is target
    assert exc.value.code == f"illegal_transition:{current.value}->{target.value}"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_outgoing_edges(state: JobState) -> None:
    """终态的出边为空——**这是整个状态机唯一真正重要的性质**。"""
    assert ALLOWED_TRANSITIONS[state] == frozenset()
    assert is_terminal(state)
    for target in ALL_STATES:
        with pytest.raises(InvalidJobTransition, match="终态不可再变"):
            transition(state, target)


@pytest.mark.parametrize("state", sorted(ACTIVE_STATES, key=lambda s: s.value))
def test_active_states_are_exactly_pending_and_running(state: JobState) -> None:
    """在途状态只有 `pending` / `running`（部分唯一索引按这个集合过滤）。"""
    assert state in {JobState.pending, JobState.running}
    assert not is_terminal(state)
    assert ALLOWED_TRANSITIONS[state], "非终态必须至少有一条出边，否则作业会卡死"


def test_all_states_are_covered_by_terminal_or_active() -> None:
    """每个状态要么终态、要么在途——没有第三种（否则清理任务捞不到它）。"""
    assert set(JobState) == TERMINAL_STATES | ACTIVE_STATES
    assert not (TERMINAL_STATES & ACTIVE_STATES)


def test_cannot_skip_execution() -> None:
    """`pending -> succeeded` 非法：没跑就成功是"入队当成完成"的经典错误。"""
    with pytest.raises(InvalidJobTransition):
        transition(JobState.pending, JobState.succeeded)


def test_cannot_go_back_to_pending() -> None:
    """任何状态都回不到 `pending`：重试必须**新建作业**（新 call_id）。

    允许回退会让同一行同时承载两次执行的历史，而租约代次（`lease_generation`）
    是按行算的——回退之后旧代次的结果提交会写进"新一次"的作业。
    """
    for state in ALL_STATES:
        with pytest.raises(InvalidJobTransition):
            transition(state, JobState.pending)


def test_unknown_is_not_failed_and_cannot_be_retried_in_place() -> None:
    """`unknown` 是终态，且**不等于** `failed`。

    `failed` 可以按策略重试；`unknown` 不行——模型可能已经调用过并收了钱，
    自动重跑就是第二次收费。两者的区别必须在状态机里是硬的，不能靠调用方记得。
    """
    assert JobState.unknown is not JobState.failed
    assert is_terminal(JobState.unknown)
    with pytest.raises(InvalidJobTransition):
        transition(JobState.unknown, JobState.failed)
    with pytest.raises(InvalidJobTransition):
        transition(JobState.unknown, JobState.running)


def test_budget_denied_only_from_pending() -> None:
    """`budget_denied` 只能从 `pending` 进来（预算检查在**开工前**）。

    `running -> budget_denied` 非法：已经开跑才发现没额度是 bug，不是一种结局
    ——把它合法化会让"花了钱但记成没额度"变成正常状态。
    """
    assert JobState.budget_denied in ALLOWED_TRANSITIONS[JobState.pending]
    with pytest.raises(InvalidJobTransition):
        transition(JobState.running, JobState.budget_denied)


def test_invalid_transition_message_is_human() -> None:
    """错误信息是人话（给日志与前端），且不把枚举名当句子。"""
    with pytest.raises(InvalidJobTransition) as exc:
        transition(JobState.succeeded, JobState.running)
    message = str(exc.value)
    assert "succeeded" in message and "running" in message
    assert "终态" in message
    assert exc.value.code == "illegal_transition:succeeded->running"


# ---------------------------------------------------------------- outbox 主键


def test_outbox_event_id_is_derived_from_call_id() -> None:
    """事件主键由 call_id **稳定派生**：同一个 call_id 永远同一个主键。

    「改坏了会红」：把派生换成 `new_id()`，同一作业被投两次就会是两条互不相识
    的事件、两次执行——而 job 表那边唯一约束挡不住 second event。
    """
    call_id = "mread_abc123"
    first = outbox_event_id_for(call_id)
    second = outbox_event_id_for(call_id)
    assert first == second
    assert call_id in first
    assert first.startswith("outbox_")
    # 不同作业必须得到不同主键
    assert outbox_event_id_for("mread_other") != first


def test_outbox_event_id_has_no_dots(logged_in: object = None) -> None:  # noqa: ARG001
    """主键里不能有 `.`：topic 名带点，直接拼进去会让日志/URL 里出现歧义。"""
    assert "." not in outbox_event_id_for("mread_x")


# ---------------------------------------------------------------- 常量纪律


def test_queue_deadline_is_fifteen_minutes() -> None:
    """排队截止 = 登记后 15 分钟（裁决 §3 第 2 步写死的值）。"""
    from datetime import timedelta

    from ling.domains.media.jobs import MEDIA_READ_QUEUE_DEADLINE

    assert timedelta(minutes=15) == MEDIA_READ_QUEUE_DEADLINE


def test_tool_version_is_stable_and_versioned() -> None:
    """工具版本带 `@N` 后缀：它是**行为的身份**，改行为就要改它。

    没有 `@` 的版本号等于一个可以随手改的字符串，而它决定了部分唯一索引的
    分组（同一证据同一版本才互斥）——改了它，旧的在途作业就不再挡新作业。
    """
    from ling.domains.media.jobs import MEDIA_READ_TOOL_VERSION

    assert "@" in MEDIA_READ_TOOL_VERSION
    assert MEDIA_READ_TOOL_VERSION.startswith("media.video.read@")
