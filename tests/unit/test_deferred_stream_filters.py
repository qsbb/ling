"""流式出口：**三种**标记过滤器必须协同，任何一条漏掉都把说明书推给用户。

## 这个文件为什么单独存在

`kernel/turn.py::_visible` 现在要挡三种标记（`<<看视频 …>>` / `<<撤回 …>>` /
`<<等会回 …>>`）。组合它们时有一个**很容易犯**、后果很直接**的错误**：

    把一条过滤器的输出当成另一条的输入串起来跑。

第一版就是 `stream_safe_deferred(ready + hold)`，看起来"只是多过一道"，
实际上 `<<撤` 对延后过滤器而言是普通正文，于是 `stream_safe_all` 刚刚扣下的
`<<撤` 被原样放出去——撤回标记当场漏给用户。三条真 PG 用例立刻变红。

正确组合：两条各跑一遍**同一份**原始文本，取更保守的暂扣（扣得多的那个）。
本文件用**纯函数**把这个不变式钉死，比集成用例快得多、定位也准。

## 为什么这是"第三次"踩同一个坑

`marker_stream.py` 的模块注释里写着第一次（`1<<2` 被当标记吃掉）和第二次
（两条标记各自跑一遍，互相看不见对方的进度）。同一个坑能踩三次，说明它需要
一条**不需要读长注释就能执行**的断言，而不是再加一段说明。
"""

from __future__ import annotations

import pytest

from ling.domains.deferred.markers import stream_safe_deferred, strip_defer_markers
from ling.kernel.marker_stream import stream_safe_all, strip_all_markers


def _visible(text: str) -> str:
    """复刻 `kernel/turn.py::_visible` 的组合规则（同一份输入，取保守者）。"""
    ready, hold = stream_safe_all(text)
    ready_defer, hold_defer = stream_safe_deferred(text)
    if len(hold_defer) > len(hold):
        ready, hold = ready_defer, hold_defer
    return strip_all_markers(strip_defer_markers(ready))


@pytest.mark.parametrize(
    "chunks",
    [
        # 三条标记各一种，逐字切分（最严格的分片）
        ["我说错了", "<<", "撤", "回", " r", "3f", "2a", "1", ">>", "你当我没说过"],
        ["我看看", "<<", "看", "视频", " m", "3", ">>", "啊"],
        ["再说", "<<", "等", "会回", " 9", "0", ">>"],
        # 三条标记接在一起
        ["<<撤回 r3f2a1>><<看视频 m3>><<等会回 90>>"],
    ],
)
def test_no_marker_leaks_in_any_chunking(chunks: list[str]) -> None:
    """任何分片方式下，三种标记的**任何片段**都不许出现在可见文本里。"""
    visible = ""
    hold = ""
    for chunk in chunks:
        text = hold + chunk
        ready, hold = stream_safe_all(text)
        ready_defer, hold_defer = stream_safe_deferred(text)
        if len(hold_defer) > len(hold):
            ready, hold = ready_defer, hold_defer
        visible += strip_all_markers(strip_defer_markers(ready))
    for leaked in ("撤回", "看视频", "等会回"):
        assert leaked not in visible, f"{leaked} 漏给用户了：{visible!r}"


def test_chained_filter_composition_leaks_recall_marker_by_design() -> None:
    """⚠️ **这条断言的是"错误写法会出错"，不是"当前实现对"。别删它。**

    用例名里的 `by_design` 就是这个意思：它**故意**去验证一种错误组合，并且
    期待它失败。它防的不是"代码被改坏"，而是"下一个人用更直觉的写法改坏它"
    ——那种改法看起来更简洁（少两个变量、少一次比较），而后果是撤回标记漏给
    用户，且**只在特定分片下复现**，靠人工测试几乎撞不到。

    被测的错误写法是：把一条过滤器的输出当成另一条的输入串起来跑

        ready, hold = stream_safe_all(text)
        ready2, hold2 = stream_safe_deferred(ready + hold)   # ← 错

    看起来只是"多过一道"，实际上 `<<撤` 对延后过滤器而言只是普通正文，于是
    `stream_safe_all` 刚刚扣下的 `<<撤` 被原样放出去。正确写法见上面的
    `_visible` 复刻：两条各跑**同一份**原始文本，取更保守的暂扣。

    如果哪天有人重构掉这个坑（例如把三条过滤器收进 `marker_stream` 统一做），
    这条用例会变红——**那时应当删掉它**，并把 `turn.py::_visible` 里对应的注释
    一起更新。留一条永远绿的"反例"反而会让后人以为这个坑还在。
    """
    text = "<<撤"
    ready, hold = stream_safe_all(text)
    assert hold == "<<撤", "前置条件：`stream_safe_all` 应当扣住半截撤回标记"
    # 错误组合：把已放出的那部分再喂给延后过滤器
    ready_wrong, hold_wrong = stream_safe_deferred(ready + hold)
    assert "撤" in ready_wrong, (
        "串行组合**居然**没漏——说明过滤器行为变了，"
        "`turn.py::_visible` 的注释与这个反例都该重写"
    )


def test_plain_text_passes_through_untouched() -> None:
    """普通正文零影响：不含 `<<` 的文本必须原样、立刻放出。"""
    for text in ("嗯", "我在的，你说。", "1 < 2 是对的", "a << b 表示左移吗"):
        ready, hold = stream_safe_all(text)
        ready_defer, hold_defer = stream_safe_deferred(text)
        if len(hold_defer) > len(hold):
            ready, hold = ready_defer, hold_defer
        assert strip_defer_markers(strip_all_markers(ready)) == text
        assert hold == ""


def _visible_step(text: str) -> tuple[str, str]:
    ready, hold = stream_safe_all(text)
    ready_defer, hold_defer = stream_safe_deferred(text)
    if len(hold_defer) > len(hold):
        ready, hold = ready_defer, hold_defer
    return strip_all_markers(strip_defer_markers(ready)), hold


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "我在。",
        "嗯",
        "a",
        "我",
    ],
)
def test_first_char_emits_immediately(text: str) -> None:
    """普通正文的首字**零暂扣**：`visible` 非空、`hold` 为空。"""
    visible, hold = _visible_step(text)
    assert visible == text, f"首字被扣住了：visible={visible!r} hold={hold!r}"
    assert hold == ""


@pytest.mark.parametrize(
    "text",
    [
        "<<< 这是引用块",
        "1 << 2 是 4",
        "a < b",
        "<<x",
    ],
)
def test_non_marker_angle_brackets_are_released(text: str) -> None:
    """不像任何标记的 `<<` 文本必须立刻放行，不能因为"可能是标记"而扣住。

    `<<x` 里 `x` 不是任何标记的动词（撤/看/等），判定为普通正文。
    """
    visible, hold = _visible_step(text)
    assert visible == text, f"没有放出内容：{text!r} visible={visible!r}"
    assert hold == ""


@pytest.mark.parametrize("text", ["<", "<<"])
def test_genuine_marker_prefix_is_held_then_released(text: str) -> None:
    """`<` 与 `<<` 是**真正的标记前缀**，扣住是对的——但要立刻放行。

    这条与上一条的区别很重要：把 `<` 也"立刻放行"等于放行一个可能变成
    `<<看视频` 的开头，那是标记泄漏的来源。所以正确的断言不是"零暂扣"，
    而是"**下一个字一到就放行**"——暂扣只持续到能判断为止。
    """
    _visible, hold = _visible_step(text)
    assert hold == text, f"{text!r} 是真正的标记前缀，应当被扣住"

    # 再来一个普通字符：立刻判定"这不是标记"，连暂扣的部分一起放出。
    visible, hold = _visible_step(text + "嗯")
    assert visible == text + "嗯", f"前缀没有及时放行：{visible!r}"
    assert hold == ""


def test_no_accumulation_across_chunks() -> None:
    """连续喂普通正文，每次都必须**立刻**有输出，不允许越积越多。"""
    hold = ""
    emitted = ""
    for chunk in ["你", "好", "，", "我", "在", "。"]:
        text = hold + chunk
        ready, hold = stream_safe_all(text)
        ready_defer, hold_defer = stream_safe_deferred(text)
        if len(hold_defer) > len(hold):
            ready, hold = ready_defer, hold_defer
        step = strip_all_markers(strip_defer_markers(ready))
        assert step, f"这一段没有立刻输出：{chunk!r}（hold={hold!r}）"
        emitted += step
    assert emitted == "你好，我在。"
    assert hold == ""
