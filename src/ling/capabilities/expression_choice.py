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
```

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

_MARKER = re.compile(r"\[(at|quote|poke)\s*:\s*([^\]\s]{1,40})\]")

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
            "- `[quote:消息序号]`：引用某条消息。适合回一条较早的、"
            "或者话题已经翻过去了的消息。"
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


__all__ = [
    "MAX_TARGETS",
    "Directive",
    "parse_expression",
    "render_expression_help",
]
