"""出站链路的"没发出去"必须说得出是哪一种（单测，不连库）。

线上症状只有一句「消息没有发出来」，而它至少有五种完全不同的成因：

1. 模型报错（`turn.failed`）—— 这一条 2026-11-07 已经修过，带 `error=code`；
2. 模型正常，但正文**被出口清洗洗空了**（只写了 `<<看视频 m3>>`）；
3. 模型正常，但**发言权被另一个窗口占着**，内容没对外交付；
4. 模型真的什么都没输出；
5. 生成了、也规划了段，但**投递失败/只发出去一部分**。

本模块钉住 2/3/4/5 的措辞——它们以前（除了 1）全都是同一句
「轮次结束但没有可交付文本（state=completed）」或者干脆「已回复」，
运维无法据此判断该去改模型、改标记，还是查渠道。
"""

from __future__ import annotations

import pytest

from ling.kernel.bridge import TurnResult, describe_delivery, describe_no_text


def test_suppressed_turn_says_it_was_suppressed() -> None:
    """生成了内容却被发言仲裁压住：不能说成"模型没说话"。"""
    result = TurnResult(
        text="",
        state="completed",
        suppressed_reason="另一个窗口正在说话，本轮未取得发言权，内容未对外交付",
        raw_chars=99,
    )
    reason = describe_no_text(result)
    assert "99 字" in reason
    assert "没有对外交付" in reason
    assert "另一个窗口" in reason
    # 不能让人误以为模型挂了 / 什么都没生成
    assert "没有可交付文本" not in reason


def test_washed_empty_body_says_cleaning_emptied_it() -> None:
    """正文被出口清洗洗空：原始字数必须出现，否则和"模型没说话"分不开。"""
    result = TurnResult(text="", state="completed", raw_chars=10)
    reason = describe_no_text(result)
    assert "洗空" in reason
    assert "10 字" in reason
    assert "标记" in reason


def test_model_really_empty_stays_plain() -> None:
    """模型一个字都没输出：没有原始字数，就不要编原因。"""
    result = TurnResult(text="", state="completed", raw_chars=0)
    assert describe_no_text(result) == "轮次结束但没有可交付文本（state=completed）"


@pytest.mark.parametrize("code", ["unauthorized", "quota_exhausted", "unsupported"])
def test_model_error_wording_is_unchanged(code: str) -> None:
    """带 error 的那条措辞**一个字都不许变**。

    它是 2026-11-07 事故里唯一能据以行动的句子（线上日志与回归用例
    都按它对齐），改它等于把已知的锚点拔掉。
    """
    result = TurnResult(text="", state="failed", error={"code": code})
    assert describe_no_text(result) == (
        f"轮次结束但没有可交付文本（state=failed，error={code}）"
    )


def test_all_parts_delivered_says_replied() -> None:
    assert describe_delivery(True, parts_sent=3, parts_total=3) == "已回复"


def test_zero_parts_delivered_does_not_claim_replied() -> None:
    """一段都没发出去：以前这里写的是"已回复"，行为日志于是自相矛盾。"""
    reason = describe_delivery(False, parts_sent=0, parts_total=3)
    assert "已回复" not in reason
    assert "0/3" in reason


def test_partial_delivery_reports_counts() -> None:
    """只发出去一部分：必须带 `1/3`，否则"她只说了半句"永远查不出来。"""
    reason = describe_delivery(False, parts_sent=1, parts_total=3)
    assert "1/3" in reason
    assert "已回复" not in reason


def test_no_planned_parts_is_not_reported_as_failure() -> None:
    """正文非空却切不出段（上游 bug）：不谎称"发出去了"，也不谎称"对方没收到"。"""
    reason = describe_delivery(False, parts_sent=0, parts_total=0)
    assert "0/" not in reason
    assert "未规划" in reason
