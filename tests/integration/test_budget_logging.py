"""超预算必须在日志里看得到——这条是用户单独提的验收要求。

用户原话：「**如果超预算，记得在日志显示出来**」。

他说的"日志"是 **Web 控制台那个日志页**，不是 `docker logs`。所以本文件
的验收点不是"有没有调用 `logger.warning()`"，而是：

1. 超限时确实产生了一条日志；
2. 那条日志**进了环形缓冲**（`ling.logging_setup`）；
3. 因此 `/v1/logs` 接口能读到它，前端日志页会显示。

## 为什么这条日志必须是 `warning` 而非 `info`

实测过：只调 `install_log_buffer()` 而没走 `setup_logging()` 时，
root logger 停在默认的 `WARNING`，`logger.info(...)` **在源头就被丢弃**，
缓冲里一条都没有。任何绕过 `setup_logging()` 的路径（独立脚本、
未来的 worker、pytest 里忘了配）都会静默丢光 info 级日志。

用户要求"在日志显示出来"，那就不能存在一个配置能让它消失。
`test_budget_log_survives_default_root_level` 专门钉住这件事。

## 这条日志不是设计文档的内容

`docs/design/PROMPT-ZONE-SPEC.md` 只做分区，**全文没有一个字提超预算日志**。
本文件的验收依据是用户原话，不是那份文档。写在这里是为了让后来的人
不要以为它是 Codex 分区设计的一部分，然后按错误的理解去改。
"""

from __future__ import annotations

import logging

import pytest

from ling.contracts.v1 import ContextContribution, Origin, Visibility
from ling.kernel.context import _apply_total_budget

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _c(lane: str, chars: int) -> ContextContribution:
    return ContextContribution(
        lane=lane,  # type: ignore[arg-type]
        text="字" * chars,
        origin=Origin.simulated,
        visibility=Visibility.window_private,
    )


@pytest.fixture
def clean_buffer():
    """每条测试前后清空环形缓冲，避免互相污染。"""
    from ling.logging_setup import clear_log_buffer, install_log_buffer

    install_log_buffer()
    clear_log_buffer()
    yield
    clear_log_buffer()


@pytest.fixture
def tight_budget(monkeypatch: pytest.MonkeyPatch):
    """把软／硬上限压到很小，才能在不构造巨型提示词的情况下触发超限。"""
    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "200")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "400")


def _messages(records: list) -> list[str]:
    return [entry.message for entry in records]


def _budget_messages() -> list[str]:
    """取出这次超预算相关的**所有**日志条目。

    注意不能只按"上下文超"过滤：说明"为何没截断"的那条并不含这个词
    （它是"硬上限仍超，但剩余内容全部是受保护车道…"）。排障时这两条
    要一起看，所以一起取。
    """
    from ling.logging_setup import recent_logs

    records = recent_logs(limit=500)
    return [
        entry.message
        for entry in records
        if "上下文超" in entry.message or "硬上限仍超" in entry.message
    ]


# --------------------------------------------------------------------------- #
# 核心：超限时缓冲里一定有日志
# --------------------------------------------------------------------------- #


def test_over_soft_writes_a_log_into_the_ring_buffer(clean_buffer, tight_budget) -> None:
    """超软上限 → 环形缓冲里出现一条包含区域名的日志。"""
    from ling.logging_setup import recent_logs

    contributions = [_c("capabilities", 120), _c("goals", 120), _c("media", 120)]
    meta: dict = {}
    _apply_total_budget(contributions, meta)

    assert meta["budget_state"] in {"over_soft", "over_hard"}

    records = recent_logs(limit=500)
    budgets = [entry for entry in records if "上下文超" in entry.message]
    assert budgets, "超预算了但环形缓冲里没有任何日志——Web 日志页看不到"


def test_log_names_the_zones_that_were_dropped(clean_buffer, tight_budget) -> None:
    """日志必须说清**哪个区域**丢了，而不只是"丢了几段"。

    用户划分提示词时用的概念是"区域"。只说 `capabilities:120` 这种内部
    车道名，他得回头查代码才知道那是"可执行能力"。

    断言的是"**实际被丢的**区域都出现在日志里"，而不是"所有区域都被丢"——
    丢够就停，360 → 200 只需丢两段，`media` 根本没轮到。
    """
    contributions = [_c("capabilities", 120), _c("goals", 120), _c("media", 120)]
    meta: dict = {}
    _apply_total_budget(contributions, meta)

    text = " ".join(_budget_messages())
    assert text, "没有超预算日志"

    # 从 meta 拿真实被丢的车道，逐个映射成区域名去日志里找
    dropped_lanes = [item.split(":", 1)[0] for item in meta["budget_dropped"]]
    assert dropped_lanes, "meta 里没有记录丢弃明细"

    from ling.kernel.prompt_zones import lane_zone

    for lane in dropped_lanes:
        zone = lane_zone(lane)
        assert zone is not None
        assert zone.label in text, f"日志里没有区域「{zone.label}」（车道 {lane}）：{text}"
        assert lane in text, f"日志里没有车道名 {lane}：{text}"


def test_log_reports_the_characters_lost_per_zone(clean_buffer, tight_budget) -> None:
    """日志要能回答「超了多少」，所以得有字符数。"""
    contributions = [_c("capabilities", 120), _c("goals", 120), _c("media", 120)]
    meta: dict = {}
    _apply_total_budget(contributions, meta)

    text = _budget_messages()[0]
    assert "字符" in text
    assert "120" in text, f"日志里没有具体字符数：{text}"


def test_log_reports_totals_and_limits(clean_buffer, tight_budget) -> None:
    contributions = [_c("capabilities", 120), _c("goals", 120), _c("media", 120)]
    meta: dict = {}
    _apply_total_budget(contributions, meta)

    text = _budget_messages()[0]
    assert str(meta["budget_total_chars"]) in text
    assert "软" in text and "硬" in text


def test_soft_and_hard_overflow_read_differently(clean_buffer, monkeypatch) -> None:
    """`over_soft` 与 `over_hard` 必须给不同的措辞。

    "整条丢了一段"和"丢了之后还超、内容要被截一半"是两种严重程度，
    措辞一样就分不出该不该去调预算。
    """
    from ling.logging_setup import clear_log_buffer

    # 软超、硬不超
    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "200")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "400")
    meta_soft: dict = {}
    _apply_total_budget([_c("capabilities", 120), _c("goals", 120), _c("media", 120)], meta_soft)
    soft_text = _budget_messages()[0]

    clear_log_buffer()

    # 硬也超（受保护车道撑爆）
    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "100")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "150")
    meta_hard: dict = {}
    _apply_total_budget([_c("capabilities", 20), _c("persona", 500)], meta_hard)
    hard_text = _budget_messages()[0]

    assert meta_soft["budget_state"] == "over_soft"
    assert meta_hard["budget_state"] == "over_hard"
    assert "软" in soft_text and "硬" in hard_text
    assert soft_text != hard_text, "两种超限状态的日志一模一样，看不出严重程度"


def test_within_budget_writes_nothing(clean_buffer, monkeypatch) -> None:
    """**正常情况一个字都不记。**

    这条很关键：如果每次请求都刷一条"上下文正常"，日志页会被淹没，
    用户真正要找的超预算提示反而找不到。
    """
    from ling.logging_setup import recent_logs

    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "100000")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "100000")
    meta: dict = {}
    _apply_total_budget([_c("persona", 1000), _c("knowledge", 500)], meta)

    assert meta["budget_state"] == "within"
    assert _budget_messages() == [], "预算正常时不该产生超预算日志"
    assert recent_logs(limit=500) == [] or _budget_messages() == []


# --------------------------------------------------------------------------- #
# 级别：不能有一个配置能把它藏起来
# --------------------------------------------------------------------------- #


def test_budget_log_is_warning_not_info(clean_buffer, tight_budget) -> None:
    """必须是 `warning`。

    理由是实测的陷阱：root logger 默认停在 `WARNING`，不调 `setup_logging()`
    时 `info` 在**源头**就被丢弃，环形缓冲收不到。用 `warning` 让它不依赖
    root 级别配置——用户要求"在日志显示出来"，那就不该有开关能让它消失。
    """
    from ling.logging_setup import recent_logs

    _apply_total_budget([_c("capabilities", 120), _c("goals", 120)], {})

    entries = [entry for entry in recent_logs(limit=500) if "上下文超" in entry.message]
    assert entries
    assert entries[0].level == "warning", f"级别是 {entries[0].level}，应为 warning"


def test_budget_log_survives_default_root_level(clean_buffer, tight_budget) -> None:
    """**不配 logging 也要进缓冲**。

    这条直接复现那个陷阱：把 root 级别压回默认的 `WARNING`，确认日志仍在。
    如果实现改回 `logger.info`，这条会红。
    """
    from ling.logging_setup import recent_logs

    root = logging.getLogger()
    saved = root.level
    try:
        root.setLevel(logging.WARNING)  # 模拟"没人调过 setup_logging()"
        _apply_total_budget([_c("capabilities", 120), _c("goals", 120)], {})
    finally:
        root.setLevel(saved)

    entries = [entry for entry in recent_logs(limit=500) if "上下文超" in entry.message]
    assert entries, (
        "root 级别为 WARNING 时超预算日志消失了——"
        "说明它用的是 info 级，任何绕过 setup_logging() 的路径都会静默丢光"
    )


# --------------------------------------------------------------------------- #
# 车道级超额（不只是总预算）
# --------------------------------------------------------------------------- #


def test_single_lane_over_its_own_budget_is_reported(clean_buffer) -> None:
    """某条车道**自己**超预算时也要出现在日志里。

    总预算超了只说明"整包太大"。真正有用的是"`capabilities` 只有 300 token
    却塞了 1000 字"——那是它自己撑爆的，该去改那条车道的预算或生产者。
    """
    # capabilities 预算 300 token = 600 字符；给 1000 字即超额
    # 同时把总预算压低，确保进入超限分支
    import os

    os.environ["LING_CONTEXT_SOFT_CHARS"] = "100"
    os.environ["LING_CONTEXT_HARD_CHARS"] = "100"
    try:
        _apply_total_budget(
            [_c("capabilities", 1000), _c("goals", 50), _c("persona", 10)],
            {},
        )
        text = " ".join(_budget_messages())
        assert "超预算车道" in text, f"没报出车道级超额：{text}"
        assert "capabilities" in text
    finally:
        del os.environ["LING_CONTEXT_SOFT_CHARS"]
        del os.environ["LING_CONTEXT_HARD_CHARS"]


# --------------------------------------------------------------------------- #
# Web 侧可见性：接口真的能读到
# --------------------------------------------------------------------------- #


def test_recent_logs_api_source_returns_the_budget_entry(clean_buffer, tight_budget) -> None:
    """`/v1/logs` 的数据源（`recent_logs`）能取到这条日志。

    接口本身只是把 `recent_logs()` 的结果序列化出去，所以这里验证到
    "数据源里有"就等价于"接口能返回"。接口的鉴权与分页另有测试。
    """
    from ling.logging_setup import recent_logs

    _apply_total_budget([_c("capabilities", 120), _c("goals", 120), _c("media", 120)], {})

    entries = recent_logs(limit=200)
    assert entries, "recent_logs 是空列表——Web 日志页会显示为空"
    matched = [entry for entry in entries if "上下文超" in entry.message]
    assert matched, f"日志页数据源里没有超预算条目，实际有：{_messages(entries)[:5]}"
    assert matched[0].level == "warning"


def test_budget_entry_carries_no_secret(clean_buffer, tight_budget) -> None:
    """超预算日志里不许出现正文字符——它只是长度统计。

    环形缓冲是通到 HTTP 的第二条路（`/v1/logs`）。把片段正文写进日志，
    等于把用户对话内容暴露给任何 owner/admin 之外不该看到的人，
    而且完全没必要：排障只需要知道"哪个区域、多少字符"。
    """
    from ling.logging_setup import recent_logs

    secret = "这段正文不该出现在日志里"
    contribution = ContextContribution(
        lane="capabilities",
        text=secret * 20,
        origin=Origin.simulated,
        visibility=Visibility.window_private,
    )
    _apply_total_budget([contribution, _c("goals", 120)], {})

    entries = [entry for entry in recent_logs(limit=500) if "上下文超" in entry.message]
    assert entries
    for entry in entries:
        assert secret not in entry.message, "日志里出现了片段正文"


def test_meta_still_exposes_budget_state_for_programmatic_use(
    clean_buffer, tight_budget
) -> None:
    """`budget_state` 除了进日志，也要继续留在 meta 里。

    日志是给人看的，meta 是给程序看的（快照接口、排障接口可能要用）。
    加了日志不等于可以把字段删掉。
    """
    meta: dict = {}
    _apply_total_budget([_c("capabilities", 120), _c("goals", 120), _c("media", 120)], meta)

    assert meta["budget_state"] == "over_soft"
    assert meta["budget_dropped"]
    assert meta["budget_soft"] == 200
    assert meta["budget_hard"] == 400
    assert meta["budget_total_chars"] == 360


def test_meta_truncation_list_is_empty_when_nothing_truncated(
    clean_buffer, tight_budget
) -> None:
    """没有截断时留空列表，不要留 `None`——调用方少一次判空。"""
    meta: dict = {}
    _apply_total_budget([_c("capabilities", 120), _c("goals", 120)], meta)
    assert meta["budget_truncated"] == []


def test_hard_overflow_with_only_protected_lanes_says_so(clean_buffer, monkeypatch) -> None:
    """只剩受保护车道却仍超硬上限时，日志要解释"为什么没有截断"。

    牺牲表覆盖所有可牺牲车道，所以丢完以后剩下的必然是受保护车道。
    这时硬截断循环无事可做——不说清楚的话，用户看到"超硬上限"却没有
    截断记录，会以为功能坏了。
    """
    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "100")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "150")

    meta: dict = {}
    _apply_total_budget([_c("capabilities", 20), _c("persona", 500)], meta)

    assert meta["budget_state"] == "over_hard"
    text = " ".join(_budget_messages())
    assert "受保护车道" in text, f"没有解释为何未截断：{text}"
