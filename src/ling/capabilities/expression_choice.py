"""表达方式：把「她能用的动作」交给她自己选。

## 为什么不是代码替她决定

上一版是「群里回复就 @ 发言人」——那是**代码在替人设做决定**。
用户对此的原话是：「把能怎么做告诉她，怎么做让她自己选择」。

所以这里只做两件事：

1. 把可用的表达方式及其**社交含义**告诉模型（`render_expression_help`）
2. 从她输出的文本里解析出她的选择，剥掉标记，交给投递层执行

选不选、选哪个、什么时候选，全在她。代码只管"选了就真的做出来"。

## 与 `capabilities/expression.py` 的分工

那个模块是**输出端口**（文本/TTS/动作的交付单元与回执语义）；
这里只负责"她选了什么"的解析与说明。两者不重叠，所以分文件。

## 标记语法

```
[at:770140]        群里点明在回谁
[quote:123456]     引用某条消息回复
[poke:770140]      拍一拍（不打断地提醒/亲昵）
[speak]            这条用语音说（无参数）
```

`[speak]` 与上面三个的区别：那三个是**投递给谁**的问题，`[speak]` 是
**用什么形态**的问题。所以它单独解析（`parse_speak_choice`），也不进
`Directive`——混在一起会让"没选语音"和"没选 @ 谁"变成同一个字段的两种空值。

用方括号而不是自然语言（"@小明"），因为要**可逆地剥掉**：
发出去的正文里不能残留这些标记，否则她的话就变成了说明书。

## 为什么解析失败要还原原文

如果整段话只有标记（例如只发了一个 `[poke:x]`），剥完就空了。
空消息投递会被平台拒绝，而且用户看不到任何东西。
所以剥完为空时**保留原文**——宁可让她说错一句，也不要发一条空消息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ling.ports.speech import SPEAK_TAG

_MARKER = re.compile(r"\[(at|quote|poke)\s*:\s*([^\]\s]{1,40})\]")

#: `[speak]` 只允许出现在**起始控制区**：前导空白之后、正文第一个字之前。
#:
#: ## 为什么必须锚在开头
#:
#: 端口 §17.4 的原话：「不从用户内容、引用、工具结果、代码块或正文中间提取」。
#: 不锚定的后果很具体：她写「他说『[speak] 是什么意思』」时，正文里的字面量
#: 会被当成她的选择，于是**一条解释语法的文字回复变成了一条语音**。
#: 更糟的是用户发来的内容里带这四个字，也能让她的回复变成语音。
#:
#: 允许多个前导空白与换行（模型常在标记前留空行），但**只认一个**标签，
#: 且剥掉后必须还有正文。
_SPEAK_PREFIX = re.compile(r"^\s*" + re.escape(SPEAK_TAG) + r"\s*")

MAX_TARGETS = 3


@dataclass(frozen=True)
class Directive:
    """她从文本里选定的表达方式。"""

    at: list[str] = field(default_factory=list)
    quote: str | None = None
    poke: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.at and not self.quote and not self.poke


def parse_expression(text: str) -> tuple[str, Directive]:
    """剥掉表达标记，返回 (正文, 她选了什么)。"""
    at: list[str] = []
    poke: list[str] = []
    quote: str | None = None

    def _take(match: re.Match[str]) -> str:
        nonlocal quote
        kind, value = match.group(1), match.group(2).strip()
        if kind == "at":
            if value not in at and len(at) < MAX_TARGETS:
                at.append(value)
        elif kind == "poke":
            if value not in poke and len(poke) < MAX_TARGETS:
                poke.append(value)
        elif kind == "quote" and quote is None:
            quote = value
        return ""

    stripped = _MARKER.sub(_take, text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", stripped)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n")).strip()

    if not cleaned:
        return (text or ""), Directive()

    return cleaned, Directive(at=at, quote=quote, poke=poke)


@dataclass(frozen=True)
class SpeakChoice:
    """她对「这条用文字还是语音」的选择。**是意图，不是结果。**

    `explicit` 区分"她写了 `[speak]`"与"她什么都没写"：两者都是文字，
    但原因完全不同——一个是主动选文字，一个是没表达。事后统计"她多久选一次
    语音"必须能分开这两者，否则"选了文字"会被"没说话"稀释掉。
    """

    speak: bool
    explicit: bool
    #: 剥掉标签之后的正文。**调用方必须用这个**，不能用原始文本——
    #: 否则 `[speak]` 会念出来。
    text: str
    #: 解析被拒时的原因（供日志与审计）。正常路径为空。
    rejected: str = ""


def parse_speak_choice(text: str) -> SpeakChoice:
    """从她的最终回复里解析 `[speak]`。

    ## 只认开头，且剥完必须还有正文

    端口 §17.4 的四条规则这里逐条落地：

    1. 只认**起始控制区**（`_SPEAK_PREFIX` 锚 `^`）；
    2. 只允许一个**无参数**的 `[speak]`（正则里没留参数位）；
    3. 不从正文中间/代码块/引用里提取（锚定天然排除）；
    4. 剥完为空 → **明确无有效外发**，不沿用旧"剥离为空就恢复标签"的兜底。

    第 4 条与 `parse_expression` 的取向**故意相反**：那里剥空要还原原文
    （否则用户收到空消息），这里剥空意味着"她只写了个 `[speak]` 没有正文"——
    发一条没有内容的语音毫无意义，而还原成 `[speak]` 会把说明书念给用户听。
    两个兜底方向不同，是因为它们的失败后果不同。
    """
    raw = text or ""
    match = _SPEAK_PREFIX.match(raw)
    if match is None:
        # 正文中间出现 `[speak]`：**不认**，但要如实报告，便于发现模型用错。
        if SPEAK_TAG in raw:
            return SpeakChoice(
                speak=False,
                explicit=False,
                text=raw,
                rejected=f"{SPEAK_TAG} 不在起始控制区，已忽略（不从正文中间提取）",
            )
        return SpeakChoice(speak=False, explicit=False, text=raw)

    stripped = raw[match.end() :].strip()
    if not stripped:
        return SpeakChoice(
            speak=False,
            explicit=True,
            text="",
            rejected=f"去掉 {SPEAK_TAG} 后没有正文，无有效外发",
        )
    if _SPEAK_PREFIX.match(stripped):
        # 写了两个：不猜哪个才算，按非法处理。
        return SpeakChoice(
            speak=False,
            explicit=True,
            text=stripped,
            rejected=f"重复的 {SPEAK_TAG}，按非法控制处理",
        )
    return SpeakChoice(speak=True, explicit=True, text=stripped)


def render_expression_help(*, is_group: bool, supports_poke: bool = True) -> str:
    """把可用的表达方式告诉模型。

    只列**当前渠道真的支持**的——列了做不到的，她就会选一个做不出来的动作，
    然后"选择了却没发生"，比不给选项更糟。
    """
    lines = ["你可以选择怎么表达（不选就只发文字，那是最常见的）："]
    if is_group:
        lines.append(
            "- `[at:QQ号]`：点一下对方。群里好几个人在说话时，"
            "用它能明确你是在回谁——不是为了礼貌，是为了让人看得懂。"
        )
        lines.append(
            "- `[quote:消息 id]`：引用某条消息。id 就是上下文里每条消息前面的"
            " `[msg_id:…]`——只能用上面真的出现过的 id，编一个会被忽略。"
            "适合回一条较早的、或者话题已经翻过去了的消息。"
        )
    if supports_poke:
        lines.append(
            "- `[poke:QQ号]`：拍一拍。不打断地提醒一下、或者表示亲近，"
            "比发一句「在吗」轻。"
        )
    lines.append(
        "标记写在正文里会被自动执行并从发出的话里去掉，"
        "所以不要把标记当成给用户看的内容。不想用就完全不用。"
    )
    return "\n".join(lines)


def render_recall_help(refs: list[str]) -> str:
    """把"你能撤回哪几条"告诉她（只列**真的**撤得动的）。

    ## 为什么这是独立一段而不是并进 `render_expression_help`

    表达方式（@/引用/拍一拍）是**发消息时**的选择；撤回是**事后**对已经发出去
    的东西的补救，而且不可逆、要用户确认。混在一段里会让她以为撤回和 @ 一样
    随手可用。

    ## 为什么必须列出短号

    不列她就只能写 `<<撤回>>`（撤最近一条），而她常常想撤的是上一条里的某一句。
    但**只列真撤得动的**（`recallable_refs` 查过）：列了做不到的，她就会选一个
    做不出来的动作，然后"选了却没发生"。

    ``refs`` 为空时**整段不出现**：没有可撤回的东西时提这件事，只会让她
    想着去撤一条撤不了的。
    """
    if not refs:
        return ""
    listed = "、".join(f"`{ref}`" for ref in refs)
    return (
        "如果你觉得自己刚才说错了、想收回，可以写标记：\n"
        f"- `<<撤回 {refs[-1]}>>`：撤回对应的那一条（上面标着 `[recall:…]` 的发言）；\n"
        "- `<<撤回>>`：撤回你最近发的那一条。\n"
        f"现在能撤回的是：{listed}。\n"
        "撤回**不可逆**，而且需要用户点确认才会真的执行——所以只在确实说错了"
        "的时候提，不要拿它当「再说一遍」的手段。标记会从正文里去掉，用户看不到。"
    )


__all__ = [
    "MAX_TARGETS",
    "Directive",
    "SpeakChoice",
    "parse_expression",
    "parse_speak_choice",
    "render_expression_help",
    "render_recall_help",
]
