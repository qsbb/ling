"""入站行为日志：一句人话，且同类处置不刷屏。

用户看到的是两件事：

1. 「先放着，等她刷到（群里的普通消息，先放着，等她刷到）」——同一句话
   说两遍。原因是 **动作** 写在 bridge 的 ``reason`` 里，动作又由日志层
   再加一次。现在 ``reason`` 只放理由，动作走 ``BridgeResult.action``。
2. 群聊一热闹，每来一条普通消息就复制一行同样的处置，把 500 条缓冲占满。

这两条都属于"日志让人看不懂/看不过来"，不是功能故障，所以只能靠测试钉住。
"""

from __future__ import annotations

import time

import pytest

from ling.contracts.v1 import InputPart, InteractionEnvelope, Modality
from ling.kernel import inbound_intake
from ling.kernel.bridge import BridgeResult
from ling.logging_setup import clear_log_buffer, recent_logs, setup_logging

WINDOW = "onebot-test:group:100200400"


def _envelope(*, text: str = "在吗", sender: str | None = "阿懒") -> InteractionEnvelope:
    return InteractionEnvelope(
        connector_instance="connector:onebot:test",
        external_event_id="evt_1",
        window_ref=WINDOW,
        account_ref="100200300",
        sender_name=sender,
        parts=[InputPart(modality=Modality.text, text=text)],
    )


def _deferred(reason: str = "群里的普通消息") -> BridgeResult:
    return BridgeResult(
        accepted=True,
        reason=reason,
        action="先放着，等她刷到",
        decision="deferred_attention",
    )


def _handled_lines() -> list[str]:
    return [e.message for e in recent_logs(kind="behavior") if e.event_name == "inbound_handled"]


@pytest.fixture(autouse=True)
def _fresh_logs(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """每个用例从空缓冲、空降噪状态开始（两者都是进程级全局）。"""
    setup_logging("api")
    clear_log_buffer()
    monkeypatch.setattr(inbound_intake, "_handled_notes", {})


def test_deferred_line_reads_as_one_sentence() -> None:
    """动作 + 理由拼成一句话，**不重复**。"""
    inbound_intake._log_inbound_behavior(_envelope(), _deferred(), is_group=True)

    assert _handled_lines() == ["对 阿懒 的话：先放着，等她刷到（群里的普通消息）"]


def test_deferred_line_does_not_repeat_the_action() -> None:
    """reason 里再出现"先放着"就说明 bridge 又把动作写回理由里了。"""
    inbound_intake._log_inbound_behavior(
        _envelope(), _deferred(reason="群里的普通消息"), is_group=True
    )

    line = _handled_lines()[0]
    assert line.count("先放着") == 1, line
    assert line.count("等她刷到") == 1, line


def test_silence_line_does_not_repeat_the_action() -> None:
    """"没接话（决定不回应：…）"是同一类重复，一并钉住。"""
    inbound_intake._log_inbound_behavior(
        _envelope(),
        BridgeResult(
            accepted=True,
            reason="群聊里的普通聊天，没有指向角色",
            decision="silence",
            handled_as="ignored",
        ),
        is_group=True,
    )

    assert _handled_lines() == ["对 阿懒 的话：没接话（群聊里的普通聊天，没有指向角色）"]


def test_action_comes_from_bridge_not_from_decision() -> None:
    """同样是"延后"，等的东西不一样：动作由 bridge 给，日志层不自己猜。"""
    inbound_intake._log_inbound_behavior(
        _envelope(text=""),
        BridgeResult(
            accepted=True,
            reason="被 @ 了，等补充一句正文",
            action="先放着",
            decision="deferred_attention",
        ),
        is_group=True,
    )

    assert _handled_lines() == ["对 阿懒 的话：先放着（被 @ 了，等补充一句正文）"]


def test_same_reason_in_one_window_is_logged_once_per_burst() -> None:
    """同一窗口连着 5 条同类普通消息：只完整记一次，收件行一条不少。"""
    for _ in range(5):
        inbound_intake._log_inbound_behavior(_envelope(), _deferred(), is_group=True)

    assert len(_handled_lines()) == 1
    received = [e for e in recent_logs(kind="behavior") if e.event_name == "inbound_received"]
    # **不丢信息**：每条消息本身仍然逐条记（正文在那里），省略的只是重复的处置句。
    assert len(received) == 5


def test_different_reason_in_one_window_is_logged_again() -> None:
    """理由变了就是新信息，不能被上一句的降噪吞掉。"""
    inbound_intake._log_inbound_behavior(_envelope(), _deferred(), is_group=True)
    inbound_intake._log_inbound_behavior(
        _envelope(), _deferred(reason="只能看，没资格让她主动开口"), is_group=True
    )

    assert _handled_lines() == [
        "对 阿懒 的话：先放着，等她刷到（群里的普通消息）",
        "对 阿懒 的话：先放着，等她刷到（只能看，没资格让她主动开口）",
    ]


def test_burst_reopens_after_quiet_period() -> None:
    """群安静下来之后再说话，重新记一行——省略的是"连着的重复"，不是永久静音。"""
    inbound_intake._log_inbound_behavior(_envelope(), _deferred(), is_group=True)
    # 把这一窗口的记账时间拨回很久以前，等价于"这条处置已经过去几分钟了"
    signature, _stamp = inbound_intake._handled_notes[WINDOW]
    inbound_intake._handled_notes[WINDOW] = (signature, time.monotonic() - 3600)
    inbound_intake._log_inbound_behavior(_envelope(), _deferred(), is_group=True)

    assert len(_handled_lines()) == 2


def test_replies_are_never_merged() -> None:
    """她**开口**的每一条都要留痕：合并掉等于隐瞒"她回过话"。"""
    reply = BridgeResult(
        accepted=True, reason="已回复", decision="reply", reply_text="在的", delivered=True
    )
    for _ in range(3):
        inbound_intake._log_inbound_behavior(_envelope(), reply, is_group=True)

    assert _handled_lines() == ["对 阿懒 的话：回了"] * 3


def test_rejected_message_keeps_its_received_line_only() -> None:
    """没被接受（媒体/空文本等）时不产生处置行，但收件行照旧。"""
    inbound_intake._log_inbound_behavior(
        _envelope(),
        BridgeResult(accepted=False, reason="消息没有可读文本"),
        is_group=True,
    )

    assert _handled_lines() == []
    assert len(recent_logs(kind="behavior")) == 1
