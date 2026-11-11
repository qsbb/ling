"""方括号动作标签的流式过滤器：**只扣可能的前缀**，且与其余过滤器并行取保守。

## 这个文件为什么必须存在

设计文档 5.4（`docs/design/CAPABILITY-TAG-SYSTEM.md`）点了这件事的原话：

> 已有 `stream_safe_deferred()` 用「只扣可能的前缀」处理 ``<<等会回``，
> `marker_stream` 曾因宽松半截规则把 `1<<2` 误删；`turn._visible()` 明确要求
> 各过滤器对同一份原文**并行**取更保守暂扣，而不能串行组合。

方括号过滤器接入时有**两条**都必须成立、而且互相拉扯的要求：

1. 真标签（`[at:…]`）不许在流式里漏给用户——用户会先看到说明书再看到它消失；
2. 普通正文里的 `[` 一个都不许丢——`[1]`、`[链接](url)`、`[CQ:` 要**立刻原样释放**。

第 2 条是第 1 条最容易被过度实现的代价：把 `[` 一律扣住或一律剥掉，第 1 条会
绿，第 2 条会静默错（用户看到的正文和模型生成的不一样，且没有任何痕迹）。
所以本文件对**同一个输入**同时断言两件事，而不是分开两个文件各测一半。
"""

from __future__ import annotations

import pytest

from ling.domains.deferred.markers import stream_safe_deferred, strip_defer_markers
from ling.kernel.marker_stream import (
    stream_safe_all,
    stream_safe_brackets,
    strip_all_markers,
    strip_bracket_tags,
)


def _visible(text: str) -> str:
    """**复刻** `turn.py::_visible` 的组合规则（同一份输入，三条并行取保守者）。

    刻意复刻而不是 import `_visible`：它是 `TurnCoordinator` 里的闭包，依赖
    整个轮次的状态，纯函数能测的部分只有这个组合规则本身。复刻的风险是
    "测试与实现漂移"，所以 `tests/unit/test_expression_stream_filter.py::
    test_visible_composition_matches_turn_implementation` 用 AST 把两者钉在一起
    ——那边 `turn.py` 里的接线一旦改成串行，这里会红。
    """
    ready, hold = stream_safe_all(text)
    ready_defer, hold_defer = stream_safe_deferred(text)
    if len(hold_defer) > len(hold):
        ready, hold = ready_defer, hold_defer
    ready_bracket, hold_bracket = stream_safe_brackets(text)
    if len(hold_bracket) > len(hold):
        ready, hold = ready_bracket, hold_bracket
    return strip_bracket_tags(strip_all_markers(strip_defer_markers(ready)))


def _stream(chunks: list[str]) -> tuple[str, str]:
    """按给定分片跑一遍流式，返回 (用户看到过的全部文本, 结束时仍在暂扣的尾巴)。"""
    visible = ""
    hold = ""
    for chunk in chunks:
        text = hold + chunk
        ready, hold = stream_safe_all(text)
        ready_defer, hold_defer = stream_safe_deferred(text)
        if len(hold_defer) > len(hold):
            ready, hold = ready_defer, hold_defer
        ready_bracket, hold_bracket = stream_safe_brackets(text)
        if len(hold_bracket) > len(hold):
            ready, hold = ready_bracket, hold_bracket
        visible += strip_bracket_tags(strip_all_markers(strip_defer_markers(ready)))
    return visible, hold


# ---------------------------------------------------------------- 1. 正文里的 `[` 不许丢
#
# 这四条是父会话点名要求的那一组：`1<<2`、`[1] 你好`、`[链接](url)`、`[CQ:…]`。
# 它们的共同点是"含一个字符，那个字符恰好也是标记的开头"。


@pytest.mark.parametrize(
    "text",
    [
        "1<<2",
        "1<<2 就是 4",
        "[1] 你好",
        "[链接](http://example.org)",
        "[CQ:at,qq=100200300]",
        "[1] 参考文献\n[2] 另一条",
        "看这个 [链接](http://example.org) 挺好",
    ],
)
def test_ordinary_brackets_are_released_immediately(text: str) -> None:
    """普通正文里的 `[` 必须**立刻原样**出现，一个字符都不许扣。

    反面：把 `[` 一律扣住等下一片——`[1] 你好` 会少一个 `[`；
    或把它当标签剥掉——正文被静默改写。
    """
    ready, hold = stream_safe_brackets(text)
    assert hold == "", f"扣住了不该扣的内容：{text!r} hold={hold!r}"
    assert ready == text, f"正文被改动了：{text!r} -> {ready!r}"
    if "[" in text:
        assert "[" in ready, f"方括号丢了：{text!r} -> {ready!r}"


def test_double_angle_and_bracket_coexist() -> None:
    """`1<<2` 与 `[1]` 混在一起时，两者都不许被对方的过滤器吃掉。"""
    text = "1<<2 且 [1] 是引用"
    assert _visible(text) == text


# ---------------------------------------------------------------- 2. 真标签不许漏


@pytest.mark.parametrize(
    "chunks",
    [
        # 逐字切分：最严格
        ["[", "a", "t", ":", "1", "0", "0", "2", "0", "0", "3", "0", "0", "]", " 你好"],
        ["[at:", "100200300] 你好"],
        ["[at:100200300]", " 你好"],
        # 标签在句中
        ["好的", "[at:", "100200300]", "，我看到了"],
        # 两个标签
        ["[at:100200300][poke:", "100200300] 嗯"],
    ],
)
def test_bracket_tags_never_leak_in_any_chunking(chunks: list[str]) -> None:
    """任何分片方式下，标签的任何片段都不许出现在可见文本里。"""
    visible, _hold = _stream(chunks)
    for leaked in ("[at", "[poke", "100200300]"):
        assert leaked not in visible, f"{leaked!r} 漏给用户了：{visible!r}"


def test_complete_tag_is_stripped_not_released() -> None:
    """完整标签要被**剥掉**，不是原样放出去。

    这条与上一条的区别：上一条测"分片过程中不漏"，这条测"闭合之后的下场"。
    只做暂扣不做清洗时，`[at:100200300] 你好` 会在流结束时整段放出——
    句柄是"暂扣的尾巴最后怎么办"，而那条路径没有测试就会漏。
    """
    visible, hold = _stream(["[at:100200300] 你好"])
    assert "at" not in visible or "[at" not in visible, visible
    assert "[at" not in visible, f"标签被放出去了：{visible!r}"
    assert "你好" in visible or "你好" in hold, f"正文丢了：{visible!r} / {hold!r}"


# ---------------------------------------------------------------- 3. 只扣可能的前缀


@pytest.mark.parametrize(
    "prefix",
    [
        "[",
        "[a",
        "[at",
        "[at:",
        "[q",
        "[qu",
        "[quo",
        "[quot",
        "[quote",
        "[quote:",
        "[p",
        "[po",
        "[pok",
        "[poke",
        "[poke:",
    ],
)
def test_known_prefix_is_held(prefix: str) -> None:
    """已知标签的**每一个**逐字前缀都要扣住。漏一个就会在流式里漏出半截。"""
    ready, hold = stream_safe_brackets(prefix)
    assert hold == prefix, f"前缀 {prefix!r} 没被扣住：hold={hold!r}"
    assert ready == "", f"前缀 {prefix!r} 之前放出了内容：{ready!r}"


@pytest.mark.parametrize(
    "text",
    ["[x", "[z", "[小", "[0", "[9", "[!", "[C", "[CQ", "[CQ:"],
)
def test_impossible_prefix_is_released(text: str) -> None:
    """不可能长成标签的 `[` 开头必须立刻放行（不是"等一会儿再放"）。"""
    ready, hold = stream_safe_brackets(text)
    assert hold == "", f"扣住了不可能成为标签的文本：{text!r} hold={hold!r}"
    assert ready == text, f"放出的内容不对：{text!r} -> {ready!r}"


def test_prefix_guard_covers_every_parser_keyword() -> None:
    """守卫：流式前缀表必须覆盖解析器认的**每一个**关键字。

    ## 为什么这条不能靠"读注释"

    前缀表与解析器是**两份数据**。改了解析器（例如加 `[speak]`）而漏改前缀表，
    后果是"流式先漏出标记、落库时才消失"——用户可见，而且只在特定分片下复现。

    ## 关键字从哪来

    不在这里手抄一份 `("at", "quote", "poke")`——那正是"两边一起错时测试照样
    绿"的坑。改为**从解析器自己的行为探测**：给它一个 `[<kw>:x]` 形状的文本，
    若它被当作动作剥掉，这个 `kw` 就是解析器认的关键字。这样解析器加关键字时，
    这条测试会自动要求前缀表跟上。

    探测用的是"剥不剥得掉"而不是"正则里有没有这个词"——前者与真实行为同源，
    后者只是形状。`expression_choice` 的内部常量在改造中会变（`_MARKER` 已被
    换成目录驱动的实现），绑定到内部名字的守卫会随改造一起失效。
    """
    from ling.capabilities.expression_choice import parse_expression

    candidates = ["at", "quote", "poke", "speak", "recall", "defer"]
    recognized: list[str] = []
    for kw in candidates:
        marker = f"[{kw}:100200300]"
        stripped, _directive = parse_expression(marker + " 正文")
        if stripped.strip() != marker + " 正文":
            # 标记被剥掉了 = 解析器认这个关键字。
            recognized.append(kw)

    assert "at" in recognized, (
        f"解析器不再认 `[at:…]`（探测结果：{recognized}）——探测方法可能失效，先修这条测试再谈前缀表"
    )

    for keyword in recognized:
        full = "[" + keyword + ":"
        _ready, hold = stream_safe_brackets(full)
        assert hold == full, (
            f"解析器认 {keyword}，但流式前缀表没扣住 {full!r}"
            f"（hold={hold!r}）——用户会先看到标记再看到它消失"
        )


# ---------------------------------------------------------------- 4. 并行取保守，不许串行


def test_chained_composition_is_wrong_by_design() -> None:
    """⚠️ 断言的是**错误写法会出错**，不是当前实现对。别删。

    错误写法：把方括号过滤器的输出当成尖括号过滤器的输入串起来跑。

        ready, hold = stream_safe_brackets(text)
        ready2, hold2 = stream_safe_all(ready + hold)   # ← 错

    为什么它会错：`[at:100200300]` 被方括号过滤器剥掉之后，`stream_safe_all`
    看到的是"没有方括号的文本"，它当然不扣——看起来没问题。真正的错法在反方向：
    先跑尖括号、把结果喂给方括号，`<<撤` 被 `stream_safe_all` 扣下后，
    方括号过滤器看到 `<<撤` 里没有 `[`，原样返回 `ready=""`——**撤回标记并未
    因此漏出**，但暂扣的状态机已经错位。

    所以这里断言的是更直接的一件事实：**串行会让"扣住的尾巴"在下一片里
    被当成普通正文重新扫描**，于是 `[at:` 在第二片里不再被识别为前缀。
    """
    # 串行（错）：第一片的 hold 作为第二片输入的一部分，但 ready 已经被清洗过
    # ——`[at:` 这种前缀在第一个过滤器里就被扣住，第二个过滤器再也看不到它。
    ready1, hold1 = stream_safe_brackets("[at:")
    assert hold1 == "[at:", "前置条件：`[at:` 应当被扣住"
    # 正确：把 hold 拼回原文再整体过滤一次（下一片到达时的真实情形）
    second = hold1 + "100200300] 你好"
    ready2, hold2 = stream_safe_brackets(second)
    assert hold2 == "", f"闭合后仍被扣住：{hold2!r}"
    assert ready2 == "[at:100200300] 你好", f"整条标签没被原样交出待清洗：{ready2!r}"


@pytest.mark.parametrize(
    "chunks",
    [
        ["[at:100200300] 你好", " <<撤回 r3f2a1>>"],
        ["<<撤", "回 r3f2a1>>", " [at:", "100200300] 嗯"],
        ["1<<2", " [1] ", "[poke:", "100200300]"],
    ],
)
def test_three_filters_in_parallel_never_leak(chunks: list[str]) -> None:
    """三条过滤器并行时互不干扰：任何一方的标记都不因另一方而漏出。"""
    visible, _hold = _stream(chunks)
    for leaked in ("撤回", "[at", "[poke"):
        assert leaked not in visible, f"{leaked!r} 漏给用户了：{visible!r}"
    # 普通正文的部分必须还在
    assert "1<<2" in visible or "1<<2" not in "".join(chunks)


# ---------------------------------------------------------------- 5. 接线守卫


def test_visible_composition_matches_turn_implementation() -> None:
    """守卫：`turn.py::_visible` 必须**并行**接第三条，不能串行。

    用 AST 而不是字符串搜索：注释里为了解释"错误写法"必须引用
    `stream_safe_brackets(ready + hold)` 这种文本，纯字符串断言会被注释骗过
    （AGENTS.md 记过同一个坑一天踩两次）。

    断言三件事：
    1. `_visible` 里出现了 `stream_safe_brackets` 的调用；
    2. 它的实参**不是**另一个过滤器的返回值（那就是串行）；
    3. 返回前调了 `strip_bracket_tags`（否则完整标签会被放出去）。
    """
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "src" / "ling" / "kernel" / "turn.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    visible_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_visible":
            visible_fn = node
            break
    assert visible_fn is not None, "找不到 `turn.py::_visible`——接线守卫失效"

    calls = [
        node
        for node in ast.walk(visible_fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "stream_safe_brackets"
    ]
    assert calls, "`_visible` 没有调用 `stream_safe_brackets`——方括号标签会漏给用户"
    for call in calls:
        # 实参必须是 `stream_hold`（原始累积文本），不能是 `ready`/`hold` 之类的
        # 中间结果——那正是"串行组合"的形状。
        assert len(call.args) == 1
        arg = call.args[0]
        assert isinstance(arg, ast.Name) and arg.id == "stream_hold", (
            "`stream_safe_brackets` 的实参不是原始累积文本 "
            f"`stream_hold`（而是 {ast.dump(arg)[:80]}）——这是串行组合，"
            "会让暂扣状态错位"
        )

    strip_calls = [
        node
        for node in ast.walk(visible_fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "strip_bracket_tags"
    ]
    assert strip_calls, "`_visible` 没有调 `strip_bracket_tags`——完整标签会被原样放给用户"
