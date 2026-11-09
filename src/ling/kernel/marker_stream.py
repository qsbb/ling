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

__all__ = ["PENDING_MARKER_RE", "strip_all_markers", "stream_safe_all"]


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
