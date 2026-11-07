"""上下文装配的**纯规则**回归：截断、丢弃顺序、短记忆时效。

这些用例不需要数据库——它们守的是"预算怎么算"这一类纯函数行为。
真实编译（要库）在 ``tests/integration/test_context_history_window.py``。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ling.contracts.v1 import ContextContribution, Origin, Visibility
from ling.kernel.context import (
    DEFAULT_BUDGETS,
    HISTORY_TOKEN_BUDGET,
    PROTECTED_LANES,
    SACRIFICE_ORDER,
    _apply_total_budget,
    _fit_message_text,
    _total_soft_hard,
    _window_topic_live,
    estimate_tokens,
)


def _c(lane: str, n: int, *, budget: int = 100) -> ContextContribution:
    return ContextContribution(
        lane=lane,
        text="x" * n,
        origin=Origin.user_statement,
        visibility=Visibility.subject_private,
        token_budget=budget,
    )


# ------------------------------------------------------------------ 单条截断


def test_fit_message_text_leaves_fitting_text_untouched() -> None:
    """装得下的消息**一个字都不改**——截断只在必要时发生。"""
    text = "阿岚：晚上好"
    assert _fit_message_text(text, HISTORY_TOKEN_BUDGET) == text


def test_fit_message_text_keeps_head_and_marks_the_cut() -> None:
    """超预算的单条消息：保留**头部**、留痕，而不是整条丢掉。

    为什么不丢尾部：粘贴长文时，通常前半段才是"我要说的事"；
    而且真正不能丢的是"有这一条"这件事本身。
    """
    body = "很长的内容。" * 5000
    fitted = _fit_message_text(body, 1000)
    assert estimate_tokens(fitted) <= 1000, estimate_tokens(fitted)
    assert fitted.startswith("很长的内容。")
    assert fitted.endswith("…（这条太长，后文被截掉了）")


def test_fit_message_text_survives_absurd_budget() -> None:
    """预算被调到极小也不能崩、不能返回空串。"""
    fitted = _fit_message_text("一二三四五", 1)
    assert fitted.endswith("…（这条太长，后文被截掉了）")
    assert fitted.startswith("一")


# ------------------------------------------------------------------ 丢弃顺序


def test_sacrifice_order_uses_real_lane_names() -> None:
    """牺牲顺序里的车道名必须**真的存在**。

    原来写的是 ``"scene"``，而编译期产出的车道叫 ``"scenario"``：
    名字对不上不会有任何报错，只会让场景提示在"超软上限"这一层
    永远不被整条牺牲——一直占着预算，直到撞硬上限被从中间截一半。
    """
    assert "scene" not in SACRIFICE_ORDER, "车道名写错了（编译期产出的是 scenario）"
    assert "scenario" in SACRIFICE_ORDER
    # 受保护车道绝不能出现在牺牲顺序里（双保险，见 _apply_total_budget）
    assert not (set(SACRIFICE_ORDER) & PROTECTED_LANES)
    # 顺序表里的每一项都得是真车道名（与 DEFAULT_BUDGETS 对齐）
    assert set(SACRIFICE_ORDER) <= set(DEFAULT_BUDGETS)


def test_scenario_lane_is_droppable_when_total_budget_is_blown() -> None:
    """场景提示超总预算时**能**被整条丢掉（不再因为名字写错而漏网）。"""
    soft, _ = _total_soft_hard()
    contribs = [
        _c("persona", soft // 2),
        _c("constraints", 2000),
        _c("scenario", soft),
    ]
    meta: dict = {}
    kept = _apply_total_budget(contribs, meta)
    assert "scenario" not in {c.lane for c in kept}
    assert any(d.startswith("scenario:") for d in meta["budget_dropped"])


def test_hard_limit_truncation_does_not_explode(monkeypatch) -> None:  # noqa: ANN001
    """硬上限仍超时的截断**不能抛异常**。

    `ContextContribution` 是 frozen 的契约模型：原地 `c.text = ...` 会抛
    `ValidationError`（整轮直接失败），而不是截断。这段代码原来就是那么写的，
    只因为默认预算下这句永远走不到才没被发现。

    这里把牺牲顺序清空，逼出"硬上限仍超、只能截断"那条分支。
    """
    from ling.kernel import context as context_module

    soft, hard = _total_soft_hard()
    monkeypatch.setattr(context_module, "SACRIFICE_ORDER", ())
    contribs = [_c("knowledge", soft * 3, budget=10**9)]
    meta: dict = {}
    kept = _apply_total_budget(contribs, meta)
    assert len(kept) == 1
    assert kept[0].text.endswith("…（已截断）")
    assert len(kept[0].text) < soft * 3


# ------------------------------------------------------------ 短记忆时效


def test_window_topic_live_respects_expiry() -> None:
    """短记忆那一段有 TTL；没有 expires_at 的行视为不过期。"""

    class _Ctx:
        def __init__(self, expires_at):  # noqa: ANN001
            self.expires_at = expires_at

    now = datetime.now(UTC)
    assert _window_topic_live(_Ctx(None)) is True
    assert _window_topic_live(_Ctx(now + timedelta(minutes=5))) is True
    assert _window_topic_live(_Ctx(now - timedelta(minutes=5))) is False
    # 库里取出来的可能是 naive datetime（老数据/无时区列）——按 **UTC** 解释，
    # 与 `_as_utc` 的约定一致，不能按服务器本地时区猜。
    naive_past = (now - timedelta(hours=1)).replace(tzinfo=None)
    assert _window_topic_live(_Ctx(naive_past)) is False


# ------------------------------------------------------------------ 接线


def test_turn_does_not_cap_history_by_count() -> None:
    """生产调用不能给"取多少条"的小上限。

    `history_limit` 曾经被 `turn._execute` 写成 20：那**不是**"上下文里放 20 条"，
    而是"一次性只从库里取 20 条"，于是第 21 条往回的消息既不在原文、
    也不在编译器的 `dropped` 里，永远压不进摘要——长会话里出现一段
    谁都看不见的永久缺口。
    """
    src = Path("src/ling/kernel/turn.py").read_text()
    call = re.search(r"\)\.compile\((.*?)\n\s*\)", src, re.S)
    assert call is not None, "找不到 ContextCompiler.compile 的调用点"
    # 只看**代码行**：注释里会提到这个参数，那不是调用
    code_lines = [
        line.split("#", 1)[0]
        for line in call.group(1).splitlines()
        if not line.strip().startswith("#")
    ]
    assert "history_limit" not in "\n".join(code_lines), (
        "生产调用又在给条数上限了：取多少条由 HISTORY_TOKEN_BUDGET 决定"
    )
