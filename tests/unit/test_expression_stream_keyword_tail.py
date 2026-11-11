"""方括号标签的**关键字尾部**中间态：`[at `、`[quote `、`[poke ` 必须被暂扣。

## 这个文件测什么

`kernel/marker_stream.py::_is_pending_bracket_tail` 有三个判据，本文件只钉
**判据 2**（`_BRACKET_KEYWORD_TAIL_RE`）：

    关键字已经写完、后面跟了空格或制表符、但**还没到冒号**，然后这一片就结束了。

`[at `、`[at  `、`[quote `、`[poke `、`[at \\t`、`[at :` 都属于这个状态。
它不是任何已知前缀的"开头"（前缀表里 `[at` 之后直接就是 `[at:`），所以
**判据 1 接不住它**；它也还没进入参数区，**判据 3 也接不住它**。
只有判据 2 能扣住。判据 2 一旦被拆掉，`[at ` 会被立刻放出去，等
`123]` 到达时前半截已经在浏览器里，拼不回来——用户先看到一个说明书碎片。

**为什么单开一个文件**：`test_expression_stream_filter.py` 与
`test_expression_stream_brackets.py` 由别的作者维护、正在验收中。本文件
只补它们都没有覆盖的这一态，不改它们一个字节。

## 它**不能**证明什么

- **证明不了判据 2 的"必须到末尾"这一半**。`[at 1` 这种"关键字后接空格再接
  参数"**不在**本文件的断言范围内，也不在判据 2 的职责里（见文末
  `test_out_of_scope_keyword_space_then_args_is_not_asserted_here`）。
- **证明不了其他两个判据**。判据 1（前缀）与判据 3（参数区）各自另有覆盖，
  把它们拆掉本文件应当仍然全绿——本文件对它们**没有区分力**，这是故意的。
- **证明不了用户真的看不到泄露**。这里测的是纯函数。从
  `queue.put(TurnStreamEvent(event="delta"))` 到 SSE 到浏览器渲染的那一段
  没有覆盖，那需要端到端。
- **证明不了 `stream_hold` 的释放时机**。只断言"这一片被扣住"，不断言
  "下一片到达时正确拼回"。
"""

from __future__ import annotations

import pytest

from ling.kernel.marker_stream import stream_safe_brackets

#: 判据 2 管的中间态：关键字写完 + 可选空格/制表符 + 可选冒号 + 空格的末尾。
#:
#: 选这几条的理由是"每条都有一个能让别的判据接不住它的特征"：
#: - `[at ` / `[at  ` / `[at \t`：`[at` 不是任何前缀的**开头**（前缀是 `[at` 的
#:   *被* `[at` 开头），而 `[at ` 比 `[at` 长、不再是前缀的前缀 → 判据 1 落空。
#: - `[quote ` / `[quo `：同上，且 `[quo` 是前缀、`[quo ` 不是。
#: - `[p ` / `[poke `：poke 一族的前缀是 `[p`、`[po`、`[pok`、`[poke`、`[poke:`，
#:   带空格的 `[p ` 哪一个都不是 → 判据 1 落空。
#: - `[at :`：关键字 + 空格 + 冒号，比前缀 `[at:` 多一个空格 → 判据 1 落空；
#:   冒号后没有参数，判据 3 的 `[^\]\n]{0,80}` 虽能匹配空串，但这条**同时**被
#:   判据 2 覆盖，所以它证明不了"只有判据 2 能接住"（见不能证明什么那节）。
KEYWORD_TAIL_INPUTS = [
    "[at ",
    "[at  ",
    "[at \t",
    "[quote ",
    "[quo ",
    "[p ",
    "[poke ",
    "[at :",
    "[AT ",
]


@pytest.mark.parametrize("text", KEYWORD_TAIL_INPUTS)
def test_keyword_tail_is_held_back(text: str) -> None:
    """关键字尾部一律扣住：`ready` 必须是空串，`hold` 必须等于原串。

    这条断言的正向配对是下面的 `test_plain_brackets_pass_through`：只断言
    "扣住"是不够的——一个把 `[` 一律扣住的实现也能让这条绿。两条必须同时成立。
    """
    ready, hold = stream_safe_brackets(text)

    assert ready == "", f"{text!r} 的关键字区尾部必须被扣住，但 ready={ready!r}"
    assert hold == text, f"{text!r} 应原样暂扣，但 hold={hold!r}"


def test_keyword_tail_is_held_even_behind_plain_text() -> None:
    """正文在前、关键字尾部在后时，只扣尾巴，前面的正文照常放出。

    这条防的是"整片扣住"的实现：那种实现在上一条参数化里也全绿。
    """
    ready, hold = stream_safe_brackets("你好[at ")

    assert ready == "你好"
    assert hold == "[at "


#: 普通方括号正文：必须**原样放行**，一个字符都不许扣。
#:
#: 这是"扣住"那一组的反例。没有它们，把 `_is_pending_bracket_tail` 写成
#: `return True` 也能让上面两条全绿。
PLAIN_BRACKET_INPUTS = [
    "[abc] 正文",
    "[链接](url)",
    "[1] 你好",
    "[CQ:at,qq=100200300]",
]


@pytest.mark.parametrize("text", PLAIN_BRACKET_INPUTS)
def test_plain_brackets_pass_through(text: str) -> None:
    """不是标签的方括号必须原样放行（`ready` 等于原串、`hold` 为空）。"""
    ready, hold = stream_safe_brackets(text)

    assert ready == text, f"{text!r} 是普通正文，不该被扣住，但 ready={ready!r}"
    assert hold == "", f"{text!r} 不该暂扣，但 hold={hold!r}"


def test_out_of_scope_keyword_space_then_args_is_not_asserted_here() -> None:
    """记录本文件**没有**断言的一态，并钉住它当前的实际行为。

    `[at 1`（关键字 + 空格 + 参数、无冒号）既不在前缀表里，也不被判据 2 匹配
    （判据 2 要求到末尾），判据 3 要求冒号。所以它**原样放行**——如果这确实是
    缺陷，那是判据 2/3 的边界问题，不是本文件该改的地方。

    这条测试的作用是**把这个空白显式写下来**：将来若有人决定覆盖它，会看到
    这里已经声明过"未覆盖"，而不是误以为全套都测了。
    """
    ready, hold = stream_safe_brackets("[at 1")

    # 钉住现状，不断言它"正确"——这是观测，不是判据。
    assert (ready, hold) == ("[at 1", "")
