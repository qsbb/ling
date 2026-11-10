"""延后回复的**标记**与**状态机**：时长自由、上限硬、钳过要说、正文不丢。

用户裁决原话：「只在她主动选择时才延后，且延后多久让她自由发挥，上限 5 分钟」。

这组用例钉的是四条容易悄悄退化的性质：

1. **默认立即回复**：解析不出标记时 ``intent is None``，且正文**一个字都不改**。
  「没有标记」是正常路径，不是错误。
2. **不按档位挑**：任意秒数/分数写法都成立（旧设计里的 ``0/15/45/90`` 档位已被
   裁决推翻）。同时坏格式（空、非数字、负号）**不挂起**，落坏格式原因。
3. **钳过要如实记**：999 秒 → 用 300 秒但保留 ``requested_seconds=999`` 且
  ``clamped=True``。把系统上限伪装成她的选择是本项目明令禁止的一类伪造。
4. **正文绝不静默丢失**：标记旁边有正文时**不延后**，剥标记后正文照发。
"""

from __future__ import annotations

import pytest

from ling.domains.deferred.markers import (
    BAD_FORMAT_BODY_PRESENT,
    BAD_FORMAT_EMPTY,
    BAD_FORMAT_MULTIPLE,
    BAD_FORMAT_UNPARSABLE,
    parse_defer_marker,
    parse_duration_seconds,
    stream_safe_deferred,
    strip_defer_markers,
)
from ling.domains.deferred.state import (
    ALLOWED_TRANSITIONS,
    MAX_DELAY_SECONDS,
    TERMINAL_STATES,
    DeferredReplyState,
    InvalidDeferredTransition,
    is_terminal,
    transition,
)

# ---------------------------------------------------------------- 时长解析


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("90", 90.0),
        ("90秒", 90.0),
        ("90 秒钟", 90.0),
        ("2分", 120.0),
        ("2分钟", 120.0),
        ("1分30秒", 90.0),
        ("0.5分", 30.0),
    ],
)
def test_duration_accepts_free_form(raw: str, seconds: float) -> None:
    """时长是**她给**的，不是从档位里挑的。"""
    assert parse_duration_seconds(raw) == seconds


@pytest.mark.parametrize("raw", ["", "   ", "一会儿", "soon", "-5", "5秒后", "两分钟"])
def test_duration_rejects_garbage(raw: str) -> None:
    assert parse_duration_seconds(raw) is None


def test_duration_does_not_clamp() -> None:
    """钳制是策略、解析是事实。999 秒必须原样解析出来，否则"她要求过 16 分钟"
    这个事实在解析阶段就消失了。"""
    assert parse_duration_seconds("999") == 999.0


# ---------------------------------------------------------------- 标记解析


def test_marker_only_yields_intent() -> None:
    text, intent, bad = parse_defer_marker("<<等会回 90>>")
    assert bad is None
    assert text == ""
    assert intent is not None
    assert intent.applied_seconds == 90
    assert intent.requested_seconds == 90.0
    assert intent.clamped is False
    assert intent.applied_ms == 90_000


def test_marker_tolerates_spacing_and_newlines_around_it() -> None:
    text, intent, bad = parse_defer_marker("  \n<< 等会回  2分 >>\n ")
    assert bad is None
    assert text == ""
    assert intent is not None
    assert intent.applied_seconds == 120


def test_no_marker_leaves_text_untouched() -> None:
    """没有标记是**正常路径**：正文一个字都不能改。"""
    original = "我刚才在看那个视频，挺有意思的。"
    text, intent, bad = parse_defer_marker(original)
    assert (text, intent, bad) == (original, None, None)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("<<等会回>>", BAD_FORMAT_EMPTY),
        ("<<等会回   >>", BAD_FORMAT_EMPTY),
        ("<<等会回 一会儿>>", BAD_FORMAT_UNPARSABLE),
        ("<<等会回 soon>>", BAD_FORMAT_UNPARSABLE),
        ("<<等会回 -5>>", BAD_FORMAT_UNPARSABLE),
        ("<<等会回 30>><<等会回 60>>", BAD_FORMAT_MULTIPLE),
    ],
)
def test_bad_format_does_not_suspend(raw: str, reason: str) -> None:
    """坏格式**不挂起**，只落原因——绝不能静默把她这一轮变成不回复。"""
    text, intent, bad = parse_defer_marker(raw)
    assert intent is None
    assert bad == reason
    assert "等会回" not in text


def test_marker_with_body_does_not_defer() -> None:
    """标记 + 正文不延后：她既然写了回复，那就是回了。

    反过来（保留延后、丢掉正文）会让她生成的内容凭空消失，用户看到的是一轮
    空回复——宁可让她多回一句，也不要静默丢内容。
    """
    text, intent, bad = parse_defer_marker("<<等会回 90>>我现在就说两句")
    assert intent is None
    assert bad == BAD_FORMAT_BODY_PRESENT
    assert text == "我现在就说两句"


def test_marker_is_scrubbed_from_output() -> None:
    """标记是说明书，绝不能漏给用户。"""
    cleaned, _, _ = parse_defer_marker("<<等会回 90>>")
    assert cleaned == ""
    assert strip_defer_markers("<<等会回 90>>") == ""


def test_parse_is_idempotent() -> None:
    """出口会剥多次，剥第二次必须什么都不变。"""
    once, intent, bad = parse_defer_marker("<<等会回 90>>")
    assert intent is not None
    again, intent2, bad2 = parse_defer_marker(once)
    assert (again, intent2, bad2) == ("", None, None)
    stripped = strip_defer_markers("<<等会回 90>>")
    assert strip_defer_markers(stripped) == stripped


def test_left_shift_is_not_a_marker() -> None:
    """``1<<2`` 是左移，不是标记。放宽正则会把她的正文静默删掉。"""
    for text in ("1<<2 就是 4", "a << b 表示左移", "<<等一下再说"):
        cleaned, intent, bad = parse_defer_marker(text)
        assert intent is None and bad is None
        assert cleaned == text


# ---------------------------------------------------------------- 上限与钳位


def test_over_limit_is_clamped_and_recorded() -> None:
    """999 秒 → 用 300 秒，但**原值必须留着**，且标记为被钳过。

    这一条对应"不许把系统限制伪装成她的选择"：库里有 ``clamped``，SSE 带
    ``requested_seconds``，前端才说得出"她本来想等更久"。
    """
    _, intent, bad = parse_defer_marker("<<等会回 999>>")
    assert bad is None
    assert intent is not None
    assert intent.applied_seconds == MAX_DELAY_SECONDS == 300
    assert intent.requested_seconds == 999.0
    assert intent.clamped is True
    assert intent.requested_ms == 999_000


def test_at_limit_is_not_clamped() -> None:
    _, intent, _ = parse_defer_marker("<<等会回 300>>")
    assert intent is not None
    assert intent.applied_seconds == 300
    assert intent.clamped is False


def test_zero_delay_is_not_a_deferral_intent() -> None:
    """``<<等会回 0>>`` 等于"现在就回"：应用 0 秒但不算钳过，调用方按立即回复处理
    （不建延后行，也就不谎报她延后了）。"""
    _, intent, bad = parse_defer_marker("<<等会回 0>>")
    assert bad is None
    assert intent is not None
    assert intent.applied_seconds == 0
    assert intent.clamped is False


# ---------------------------------------------------------------- 流式过滤


def test_stream_safe_holds_half_marker() -> None:
    """半截标记必须暂扣，否则 SSE 里会先把说明书推给用户。"""
    ready, hold = stream_safe_deferred("她说了句<<等会")
    assert ready == "她说了句"
    assert hold == "<<等会"


def test_stream_safe_releases_normal_text_immediately() -> None:
    """**普通正文零影响**：不含 ``<<`` 的文本必须原样立刻放出，不攒字。

    这条钉的是"加了闸不能让首字延迟变长"——那是流式手感最容易悄悄退化的一处。
    """
    for text in ("嗯", "好", "我在的，你说。", "a < b 是小于"):
        ready, hold = stream_safe_deferred(text)
        assert ready == text
        assert hold == ""


def test_stream_safe_does_not_hold_complete_marker() -> None:
    ready, hold = stream_safe_deferred("<<等会回 90>>")
    assert ready == "<<等会回 90>>"
    assert hold == ""


def test_stream_safe_gives_up_on_non_defer_text() -> None:
    """``<<看视频`` 不是延后标记，不该被这个过滤器继续扣住（另一个过滤器管它）。"""
    ready, hold = stream_safe_deferred("<<看视频 m3")
    assert hold == ""


def test_stream_safe_does_not_hold_left_shift() -> None:
    """``1<<2`` 是左移。放宽成 ``r"<<[^<>]*>?$"`` 会让它被扣住甚至删掉——
    ``kernel/marker_stream.py`` 的模块注释记录过这个坑。"""
    ready, hold = stream_safe_deferred("1<<2 就是 4")
    assert ready == "1<<2 就是 4"
    assert hold == ""


# ---------------------------------------------------------------- 状态机


def test_terminal_states_have_no_outgoing_edges() -> None:
    """终态吸收：这是状态机唯一真正重要的性质。"""
    expected = frozenset(
        {
            DeferredReplyState.delivered,
            DeferredReplyState.cancelled,
            DeferredReplyState.failed,
        }
    )
    assert expected == TERMINAL_STATES
    for state in TERMINAL_STATES:
        assert is_terminal(state)
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_legal_transitions() -> None:
    assert transition(DeferredReplyState.pending, DeferredReplyState.claimed) == (
        DeferredReplyState.claimed
    )
    assert transition(DeferredReplyState.claimed, DeferredReplyState.delivered) == (
        DeferredReplyState.delivered
    )
    assert transition(DeferredReplyState.claimed, DeferredReplyState.pending) == (
        DeferredReplyState.pending
    )


def test_self_transition_is_illegal() -> None:
    """同状态不是转移，是空写。允许它会让"重复扫描"看起来像成功。"""
    for state in DeferredReplyState:
        with pytest.raises(InvalidDeferredTransition):
            transition(state, state)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeferredReplyState.pending, DeferredReplyState.delivered),
        (DeferredReplyState.delivered, DeferredReplyState.cancelled),
        (DeferredReplyState.cancelled, DeferredReplyState.claimed),
        (DeferredReplyState.failed, DeferredReplyState.pending),
    ],
)
def test_illegal_transitions_raise(
    current: DeferredReplyState, target: DeferredReplyState
) -> None:
    with pytest.raises(InvalidDeferredTransition) as exc:
        transition(current, target)
    assert exc.value.code == f"illegal_transition:{current.value}->{target.value}"


def test_every_state_is_reachable() -> None:
    """没有孤儿状态：每个非初始状态都必须有一条进来的边。"""
    incoming = {target for edges in ALLOWED_TRANSITIONS.values() for target in edges}
    assert incoming | {DeferredReplyState.pending} == set(DeferredReplyState)
