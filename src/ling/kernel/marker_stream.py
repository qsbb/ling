"""标记"半截"的**共用**判定 + 流式出口的合并过滤（`<<看视频 …>>` / `<<撤回 …>>`）。

## 为什么这个常量必须在 kernel，而不是任何一个 domain

两条标记分属两个域（`domains/media/video.py` 的 `<<看视频 m3>>`、
`domains/conversation/recall.py` 的 `<<撤回 rXXXX>>`），但它们**共用同一个
"半截"形状**：以 ``<<`` 开头、还没等到 ``>>``、且第一个字必须是已知动词。

这个形状以前在两个域里**各写了一份** ``r"<<[^<>]*>?$"``，结果是两边都过宽：
``finalize_reply_text("1<<2 就是 4")`` 会变成 ``"1"``——她真说的话被静默删掉。
两份正则的唯一结局是必有一处先被改松，而"哪一处松了"极难发现。

放在 ``kernel`` 是因为它**谁都能 import 而不产生跨域依赖**：
`domains/A` 不许 import `domains/B`（见 `tests/unit/test_domain_boundaries.py`），
而 kernel 是被 import 的那一层。这个常量本身不依赖任何域，所以两个域在**顶层**
从这里取它不构成环。

（本模块的 `stream_safe_all` / `strip_all_markers` 需要调两个域的实现，所以它们
是**函数内延迟 import**：模块加载期不碰域，调用期才碰。反过来把这两个函数写在
域模块里做顶层 import 就会成环。）

## 为什么流式出口必须**同时**剥两种

曾经 `turn._visible()` 只调 `strip_watch_markers`：``<<撤回 r3f2a1>>`` 会
**原样出现在流式气泡里**，等落库时才洗掉——用户先看到说明书，再看到它消失。
流式是本项目唯一"用户看得见中间态"的出口，漏一种就等于漏给用户。

而"两种标记各自跑一遍再串起来"也不行：两个"半截暂扣"互相看不见对方的进度。
``…<<看视`` 扣住 ``<<看视``；下一片是 ``频 m3>> 然后<<撤``——先跑看视频那条会
放出 ``频 m3>> 然后`` 并扣住 ``<<撤``，再跑撤回那条时输入是**已放出的部分**，
它看不到 ``<<撤``。所以收成**一个**过滤器：整条 delta 流只跑一次。
"""

from __future__ import annotations

import re

__all__ = [
    "PENDING_MARKER_RE",
    "strip_all_markers",
    "strip_bracket_tags",
    "stream_safe_all",
    "stream_safe_brackets",
]


#: "可能是半个标记"的形状：以 ``<<`` 开头、尚未出现 ``>>``。
#:
#: ## 必须限定动词前缀，否则会吃掉正常正文
#:
#: 放宽成 ``r"<<[^<>]*>?$"`` 时，``1<<2 就是 4`` / ``a << b 表示左移`` 这种
#: **正常正文**会被当半截标记删掉。``<<`` 在 C/JS/Python 里是左移，在中文里
#: 也常被当引号——静默删掉她的正文是最糟的一类错误（用户看到的和模型生成的
#: 不一样，而且没有任何痕迹）。
#:
#: ## 为什么动词那一段是**可选**的
#:
#: 逐字流式时先到 ``<``、再到 ``<<``、然后才是 ``撤``。若 ``<<`` 本身不匹配，
#: 它会被**立刻放出去**；等 ``撤`` 到达时前半截已经进了浏览器，拼不回来。
#: 所以"只有 ``<<``"也足以暂扣；但只要后面出现一个不是动词开头的字
#: （``1<<2`` 的 ``2``），立刻判定"这不是标记"，原样放出。
#:
#: ``[^<>\n]*``：中间允许短号/空格/排版，但不跨行——标记是单行的，
#: 跨行的"半截"更可能是正常正文。
PENDING_MARKER_RE = re.compile(r"<<\s*(?:(?:撤|看)[^<>\n]*)?>?$")


def stream_safe_all(text: str) -> tuple[str, str]:
    """流式过滤**两种**标记：返回 (可以立刻发出去的部分, 需要暂扣的尾巴)。

    两条标记各跑一遍，**取更保守的那个**（待扣更长的）。两条域函数用的是
    **同一个** ``PENDING_MARKER_RE``，所以"取更长的待扣"就是两条规则的并集，
    不重不漏；将来若某一条的"半截"形状真的分叉，这里仍取保守的那一个
    （宁可多扣一片，也不把说明书推给用户）。
    """
    if not text:
        return "", ""
    # 延迟 import：两个域在**顶层**从本模块取常量，顶层互相 import 会成环。
    from ling.domains.conversation.recall import stream_safe_recall
    from ling.domains.media.video import stream_safe

    ready, hold = stream_safe(text)
    ready2, hold2 = stream_safe_recall(text)
    if len(hold2) > len(hold):
        return ready2, hold2
    return ready, hold


def strip_all_markers(text: str) -> str:
    """剥掉**两种完整标记**（幂等）。流式气泡的输出口必须过这一道。"""
    if not text:
        return ""
    from ling.domains.conversation.recall import strip_recall_markers
    from ling.domains.media.video import strip_watch_markers

    return strip_watch_markers(strip_recall_markers(text))


#: 方括号动作标签的**已知前缀**（`[at:` / `[quote:` / `[poke:` 的逐字前缀）。
#:
#: ## 为什么要逐字展开，而不是写一把 `\[(?:at|quote|poke)[\s]*:` 的正则
#:
#: 流式是**逐字到达**的：先到 `[`，再到 `[a`、`[at`、`[at:`。若只有"完整标签"
#: 才算候选，那么 `[` 会在第一步就被放出去——等后面的字到齐时，`[` 已经在浏览器
#: 里了，拼不回来，用户会先看到一个孤零零的 `[`（或者更糟：`[at` + 正文）。
#:
#: 所以 `[` 之后**只要还可能是某个标签的前缀**就继续扣；一旦出现任何不可能的字
#: （`[1]`、`[链接]`、`[CQ:`），立刻判定"这不是标签"并**原样放出**。
#:
#: ## 这份前缀表从哪来
#:
#: 与解析器认的三个关键字同源：`expression_choice._MARKER` 认 `at|quote|poke`。
#: 改解析器而漏改这里，后果是"流式先漏出标记、落库时才消失"——**用户可见**，
#: 所以 `tests/unit/test_expression_stream_filter.py` 里有一条守卫测试把两边
#: 对齐（从解析器自己的正则里取关键字，而不是在这份表里再抄一遍）。
_BRACKET_PREFIXES: tuple[str, ...] = (
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
)

#: 已闭合的完整方括号标签（**只用于"该不该剥"的形状判定**）。
#:
#: 合法性与目标校验仍归 `parse_expression`。这里刻意**宽松**（`IGNORECASE`、
#: 参数不校验形状）：`[at:abc]`、`[AT:123]` 正是最该被挡住的说明书，流式出口
#: 的任务只是"别把说明书推给用户"。**两件事不要在这里合并。**
_BRACKET_CLOSED_RE = re.compile(r"\[(?:at|quote|poke)\s*:[^\]\n]*\]", re.IGNORECASE)

#: **仍在关键字区**：`[` + 关键字的一部分 + 可选空格/冒号，然后**就结束了**。
#:
#: 匹配 `[`、`[a`、`[at`、`[at `、`[at :`、`[quo`、`[poke:`……
#: **不匹配** `[abc`——那是 `[a` 后面接 `bc`，而 `[at` 写完必须是空格/冒号
#: 或结尾。这条反向断言是"普通正文不许丢 `[`"的落地：`[abc] 是参考` 这样的
#: 正文不能被当成"还在写标签"永久扣住。
_BRACKET_KEYWORD_TAIL_RE = re.compile(
    r"\[(?:a|at|q|qu|quo|quot|quote|p|po|pok|poke)[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)

#: **已进入参数区**：关键字与冒号都写完，参数还在一个字一个字到。
#:
#: 这是逐字流式真正的常态（`[at:1`、`[at:1002…`），必须单独判——前缀表只覆盖
#: "还没写完关键字"的那几个状态，越过 `[at:` 之后就不再是任何前缀的"开头"了。
#: 只看前缀表，用户会眼看着 `[at:100200300]` 一个字一个字拼出来。
#:
#: ``{0,80}`` 封顶与解析器候选式同源：不封顶的话一个永远不闭合的 `[at:` + 长文
#: 会被**永久扣住**（暂扣尾巴只在闭合时释放），那是把正文吞掉的另一种方式。
_BRACKET_ARGS_TAIL_RE = re.compile(r"\[(?:at|quote|poke)[ \t]*:[^\]\n]{0,80}$", re.IGNORECASE)


def _is_pending_bracket_tail(tail: str) -> bool:
    """`tail`（从最后一个 `[` 起、未闭合）是不是"还在写标签中间"。

    ## 三个条件，缺一个就会错一个方向

    1. 它是某个已知前缀的**开头**（`[`、`[a`、`[at`、`[at:`）——最早期状态；
    2. 它在**关键字区**且已到末尾（含 `[at `、`[at :` 这类带空格的写法）；
    3. 它**已进入参数区**（`[at:1`、`[quote:12`）且参数不超过上限。

    只写 1 会漏掉 2/3（整个标签逐字漏给用户，测试抓到过）；
    把 2 写成"`[a` 开头就扣"会误扣 `[abc`（普通正文永久被吞）。

    两条显式断言的结果:**是标签中间**才扣，否则调用方原样放行。
    """
    if any(prefix.startswith(tail) for prefix in _BRACKET_PREFIXES):
        return True
    if _BRACKET_KEYWORD_TAIL_RE.match(tail):
        return True
    return _BRACKET_ARGS_TAIL_RE.match(tail) is not None


def stream_safe_brackets(text: str) -> tuple[str, str]:
    """方括号动作标签的流式过滤：返回 (可以立刻发出去的部分, 需要暂扣的尾巴)。

    ## 只扣"可能的前缀"——这条是本函数的全部要害

    设计文档 5.4：``[1]``、``[链接](url)``、``[CQ:`` 要**立刻原样释放**。
    扣住它们会让普通正文丢 `[`，而 `[` 在中文排版里到处都是——静默吞掉是最糟的
    一类错误（用户看到的和模型生成的不一样，且没有任何痕迹）。

    ## 判据只有一条

    取末尾**最后一个未闭合的 `[`** 起的尾巴（没有 `]`、不含换行），若它本身是
    某个已知前缀的前缀（`[`、`[a`、`[at`、`[at:`），扣住；否则**原样放行**。

    - `[`、`[a`、`[at`、`[at:`、`[quo`、`[poke:` → **扣住**（等下一片）
    - `[1`、`[链`、`[CQ`、`[链接](url` → **原样放行**（不可能长成标签）

    ## 为什么半截标签在流结束时也不在这里放行

    ``[quote:`` 停在流末尾时不可能是一条完整标签，但它极可能是一条**被截断的**
    完整标签。放行就等于把 `[quote:` 推给用户。设计文档 5.4 的处置是"末尾清除
    并记 `incomplete_marker`"，那属于**最终出口**（`turn.py` 落库前的清洗），
    不在这个纯函数里做——本函数只回答"这一片该不该现在放出去"。
    """
    if not text or "[" not in text:
        # 绝大多数 delta 走这条：一个字节都不扣。
        return text, ""

    tail = text[text.rfind("[") :]
    # 已闭合（后面出现过 `]`）或跨行 → 不是待扣的半截。跨行的 `[` 更可能是
    # 正常正文（Markdown 脚注 `[1]:` 就长这样）。
    if "]" in tail or "\n" in tail:
        return text, ""

    if _is_pending_bracket_tail(tail):
        return text[: text.rfind("[")], tail
    return text, ""


def strip_bracket_tags(text: str) -> str:
    """剥掉方括号动作标签（`[at:…]` / `[quote:…]` / `[poke:…]`）。流式出口用。

    ## 为什么流式出口**必须**剥它

    `strip_all_markers()` 剥的是 ``<<看视频>>`` / ``<<撤回>>`` 两种尖括号标记，
    **不认方括号**。而方括号标签在流式里同样会被推给用户：`[at:123] 你好`
    的前 8 个字符会先出现在气泡里，然后落库时由 `parse_expression` 剥掉、
    最终正文才变成"你好"——用户看到了一个随后才消失的说明书。

    ## 为什么用宽松式而不是解析器的合法式

    这里做的是**出口清洗**，不是解析：`[at:abc]`（非法参数）、`[AT:123]`
    （大小写错）都必须被挡住——它们恰恰是最该被挡的那一类。合法性与目标校验
    由 `parse_expression` 负责，**两件事分开**，谁也别替谁做决定。

    ## 普通方括号不受影响

    `[1] 参考文献`、`[链接](url)`、`[CQ:at,qq=…]` 的名词不是 `at/quote/poke`，
    正则不匹配，原样返回。这是本函数唯一需要守住的不变式。
    """
    if not text or "[" not in text:
        return text
    return _BRACKET_CLOSED_RE.sub("", text)
