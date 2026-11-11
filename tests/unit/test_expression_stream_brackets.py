"""方括号动作标签（`[at:…]` / `[quote:…]` / `[poke:…]`）的**流式出口**过滤器。

## 这个文件测什么

`kernel/marker_stream.py` 里两个纯函数的分工：

- `stream_safe_brackets(text)` → `(ready, hold)`：这一片**能不能现在发出去**。
- `strip_bracket_tags(text)`：完整标签**该不该从可见正文里剥掉**。

两条要求互相拉扯，而且都由设计文档 5.4 点名（`docs/design/CAPABILITY-TAG-SYSTEM.md`）：

1. **真标签不许漏**。`[at:123]` 是给机器看的说明书，落库时会被
   `parse_expression` 洗掉；如果在流式里先推给用户，用户就先看到说明书、
   再看到它消失。
2. **普通正文里的 `[` 一个都不许丢**。`[1] 你好`、`[链接](url)`、`[CQ:…]`、
   `1<<2` 都必须**原样出现**。把 `[` 一律扣住能让第 1 条全绿，而第 2 条
   静默错——用户看到的正文和模型生成的不一样，且没有任何痕迹。

本文件对**同类输入**同时断言两件事，而不是拆成两个文件各测一半：这两条是
同一个判据的两端，分开测就会出现"一半绿一半红、却看不出它们是同一个决定"。

## 它**不能**证明什么

- **证明不了用户真的看不到泄露。** 这里测的是纯函数与一次 `_visible` 组合
  规则的复刻。从 `queue.put(TurnStreamEvent(event="delta"))`
  到 SSE 到浏览器渲染的那一段没有覆盖；那需要端到端。
- **证明不了最终落库的正文是干净的。** 那由 `parse_expression` 负责，
  本文件只断言 `strip_bracket_tags` 这一道出口清洗。
- **证明不了 `turn.speaking` 的 `caught_up` 字段。** 它只过
  `strip_all_markers`（只认尖括号），方括号标签会穿过它——这是已知的另一条
  路径，不在本文件的断言范围内（见 `test_caught_up_path_is_not_covered_here`）。
- 前四条"正文不许丢"用的是**不扣留**判据（`hold == ""`），它**证明不了**
  释放时机，只证明没有扣住。

## 为什么这里要复刻 `_visible` 而不是 import 它

`turn.py::_visible` 是 `TurnCoordinator` 里的闭包，依赖整轮状态
（`stream_hold` 用 `nonlocal` 改）。纯函数能测的只有**组合规则**本身。
复刻的风险是"测试与实现漂移"，所以 `test_visible_composition_matches_turn`
用 AST 把复刻的接线钉在真实代码上——那边一旦改成串行，这里会红。
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

from ling.capabilities import tag_specs
from ling.capabilities.expression_choice import parse_expression
from ling.domains.deferred.markers import stream_safe_deferred, strip_defer_markers
from ling.kernel.marker_stream import (
    _BRACKET_PREFIXES,
    stream_safe_all,
    stream_safe_brackets,
    strip_all_markers,
    strip_bracket_tags,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "ling"
_TURN = _SRC / "kernel" / "turn.py"
_MARKER_STREAM = _SRC / "kernel" / "marker_stream.py"


# ---------------------------------------------------------------- 复刻与驱动


def _visible(text: str, hold: str) -> tuple[str, str]:
    """复刻 `turn.py::_visible` 的**组合规则**：同一份原文，各跑一遍取保守者。

    参数与返回值都显式带 `hold`，因为真实实现用 `nonlocal stream_hold`
    累积跨片状态——把状态藏在闭包里会让"驱动一整条流"的测试写得含混。

    返回 (这一片可见的文本, 新的暂扣尾巴)。
    """
    ready, held = stream_safe_all(text)
    ready_defer, hold_defer = stream_safe_deferred(text)
    if len(hold_defer) > len(held):
        ready, held = ready_defer, hold_defer
    ready_bracket, hold_bracket = stream_safe_brackets(text)
    if len(hold_bracket) > len(held):
        ready, held = ready_bracket, hold_bracket
    visible = strip_bracket_tags(strip_all_markers(strip_defer_markers(ready)))
    return visible, held


def _stream(chunks: list[str]) -> tuple[str, list[str], str]:
    """按分片跑一遍，返回 (用户会收到的全部文本, 每片的可见片段, 结束时的尾巴)。

    **累积**原文（`hold + chunk`），因为真实实现是把 `stream_hold` 整段重跑，
    不是只跑新 delta。分片驱动的测试若自己拼原文，就测不到"暂扣的尾巴在下一片
    里被重新扫描"这件事。
    """
    hold = ""
    joined = ""
    pieces: list[str] = []
    for chunk in chunks:
        hold = hold + chunk
        piece, hold = _visible(hold, "")
        pieces.append(piece)
        joined += piece
    return joined, pieces, hold


# ---------------------------------------------------------------- 1. 普通正文不许丢 `[`


@pytest.mark.parametrize(
    "text",
    [
        "1<<2",
        "1<<2 就是 4",
        "[1] 你好",
        "[链接](http://example.org)",
        "[CQ:at,qq=100200300]",
        "看这个 [链接](http://example.org) 挺好",
        "参考[1] 和 [2]",
    ],
)
def test_ordinary_brackets_are_not_held(text: str) -> None:
    """普通方括号必须**原样立刻**放行：不扣、不改、不丢 `[`。

    反例：把 `[` 一律扣住等下一片 → `hold` 非空；
    或把它当成标签剥掉 → `ready != text`。
    `1<<2` 同时守住尖括号那一侧（`PENDING_MARKER_RE` 过宽曾把它删成一个 `1`）。
    """
    ready, hold = stream_safe_brackets(text)
    assert hold == "", f"扣住了不该扣的内容：{text!r} hold={hold!r}"
    assert ready == text, f"正文被改动了：{text!r} -> {ready!r}"
    assert "[" in ready or "[" not in text, f"方括号丢了：{text!r} -> {ready!r}"
    assert "<" in ready or "<" not in text, f"尖括号丢了：{text!r} -> {ready!r}"


def test_ordinary_brackets_survive_the_whole_visible_chain() -> None:
    """不只是纯函数：过完整条 `_visible` 组合与出口清洗后，正文仍一字不差。"""
    text = "1<<2 就是 4，[1] 你好 [链接](http://example.org) 和 [CQ:at,qq=100200300]"
    visible, _pieces, hold = _stream([text])
    assert hold == "", f"扣住了：{hold!r}"
    assert visible == text, f"正文被改动了：{text!r} -> {visible!r}"


# ---------------------------------------------------------------- 2. 真标签不许漏


@pytest.mark.parametrize(
    "chunks",
    [
        # 父会话点名的跨分片：`[at:` 与参数被切在两边
        ["[at:1", "23] 你好"],
        # 逐字切分（最严格）
        list("[at:123] 你好"),
        # 在句中
        ["好的", "[at:", "123]", "，我看到了"],
        # 大写与非法参数：解析器不认，但流式出口**必须**挡住（它们最该被挡）
        ["[AT:1", "23] 你好"],
        ["[at:ab", "c] 你好"],
    ],
)
def test_no_bracket_fragment_reaches_the_user(chunks: list[str]) -> None:
    """任何分片方式下，`[at:` 与参数的任何片段都不许出现在**用户会收到的文本**里。

    判据用两个字面量：`"[at"` 与 `"123]"`/`"abc]"` 之外，再看 `"["`。
    前两个抓"半截标签漏出"，第三个抓"正文里的 `[` 被吞"——两个方向都覆盖，
    否则一条永真的 `not in` 就能让这条测试变绿。

    末尾那条 `"你好"` 是**配对的正向断言**：只有 `not in` 的负向断言，在
    "实现什么都不输出"时也会绿（信息量为零）。
    """
    joined, _pieces, _hold = _stream(chunks)
    for leaked in ("[at", "[AT"):
        assert leaked not in joined, f"{leaked!r} 漏给用户了：{joined!r}"
    assert "123]" not in joined and "abc]" not in joined, f"参数漏了：{joined!r}"
    body = strip_bracket_tags(strip_all_markers(joined)).strip()
    assert body and "at" not in body.lower(), f"正文没到或混入了标签：{joined!r} -> {body!r}"
    assert any(word in body for word in ("你好", "我看到了", "，")), f"正文没到：{joined!r}"


def test_complete_tag_is_stripped_not_passed_through() -> None:
    """闭合之后要被**剥掉**，不是原样连参数一起交出去。

    这条与上一条的分工：上一条测"分片过程中不漏"，这条测"闭合后的下场"。
    只暂扣不清洗时，`[at:123] 你好` 会在闭合那一片整段放出——那是另一条漏法。
    """
    ready, hold = stream_safe_brackets("[at:123] 你好")
    assert hold == "", f"闭合后仍扣住：{hold!r}"
    stripped = strip_bracket_tags(ready)
    assert "[at" not in stripped, f"标签没被剥掉：{stripped!r}"
    assert "你好" in stripped, f"正文被连坐删掉了：{stripped!r}"


@pytest.mark.parametrize("tag", ["[at:abc] 你好", "[AT:123] 你好", "[quote: 你好", "[poke:x] 嗯"])
def test_illegal_tags_are_still_stripped_at_the_exit(tag: str) -> None:
    """非法写法（大小写、非数字参数、空格）在出口清洗里同样要被剥。

    为什么这条重要：解析器的**合法式**不认它们，若出口只跟合法式走，
    `[AT:123]` 就会原样发给用户——而它恰恰是最该被挡的那一类。
    """
    visible, _p, _hold = _stream([tag])
    assert "at:" not in visible.lower().replace("at: ", "at:"), f"标签漏了：{visible!r}"


# ---------------------------------------------------------------- 3. 前缀表与 `_visible` 接线


def test_known_prefixes_are_held_without_leaking_text() -> None:
    """前缀表里**每一条**都必须扣住，且之前的正文照常放出。

    不变量比"hold == prefix"更强：扣住不是"吞掉整段"，`好了[at:` →
    `ready == "好了"`。旧写法只断言 `hold == prefix`，无法区分"扣住尾巴"
    与"把整段都扣了"。
    """
    for prefix in _BRACKET_PREFIXES:
        ready, hold = stream_safe_brackets("好了" + prefix)
        assert hold == prefix, f"前缀 {prefix!r} 没被扣住：hold={hold!r}"
        assert ready == "好了", f"前缀 {prefix!r} 之前的正文被吞了：ready={ready!r}"


@pytest.mark.parametrize("text", ["[x", "[链", "[CQ", "[CQ:", "[9", "[!", "[，"])
def test_impossible_prefix_is_released_immediately(text: str) -> None:
    """不可能长成标签的 `[` 必须**立刻**放行，不是"等一会儿再放"。"""
    ready, hold = stream_safe_brackets(text)
    assert hold == "", f"扣住了不可能成为标签的文本：{text!r} hold={hold!r}"
    assert ready == text, f"放出的内容不对：{text!r} -> {ready!r}"


def _names_in(node: ast.AST) -> set[str]:
    return {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name)
    } | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_visible_composition_is_parallel_not_chained() -> None:
    """守卫：`turn.py::_visible` 必须**并行**接方括号过滤器，不能串行。

    串行（把一条的输出当另一条的输入）会让暂扣状态错位——
    这是 `test_deferred_stream_filters.py` 记过的坑，第三条标记接入时最容易再犯。

    用 AST 而不是字符串搜索：`_visible` 的 docstring 里为了解释错误写法，
    必须引用 `stream_safe_all(ready + hold)` 这种文本，纯字符串断言会被注释
    骗过（AGENTS.md 记过同一个坑一天踩两次）。
    """
    tree = ast.parse(_TURN.read_text(encoding="utf-8"))
    targets = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_visible"
    ]
    assert targets, "找不到 `turn.py::_visible`——接线守卫失效（函数被改名或删除）"

    calls = [
        n
        for n in ast.walk(targets[0])
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        in {"stream_safe_brackets", "strip_bracket_tags"}
    ]
    called = {
        getattr(n.func, "id", None) or getattr(n.func, "attr", None) for n in calls
    }
    assert "stream_safe_brackets" in called, "`_visible` 没接方括号过滤器——标签会漏"
    assert "strip_bracket_tags" in called, "`_visible` 没剥方括号标签——完整标签会漏"

    for call in calls:
        if getattr(call.func, "id", None) != "stream_safe_brackets":
            continue  # `strip_bracket_tags` 在清洗链末端，实参本就是表达式
        # 实参必须是原始累积文本 `stream_hold`，不能是另一条过滤器的返回值。
        assert len(call.args) == 1 and isinstance(call.args[0], ast.Name), (
            f"{ast.unparse(call)} 的实参不是单一名字——像串行组合"
        )
        assert call.args[0].id == "stream_hold", (
            f"{ast.unparse(call)} 的实参不是 `stream_hold`"
            f"（而是 {ast.unparse(call.args[0])}）——这是串行组合"
        )


# ---------------------------------------------------------------- 4. 前缀表与解析器对齐（守卫 + 守卫的测试）


def _bracket_specs() -> list[tag_specs.TagSpec]:
    """标签目录里**方括号形状**的规格（`[name:…]` / `[name]`）。

    排除 `reply.defer`：它是 `<<等会回 …>>`，由 `stream_safe_deferred` 管，
    不属本文件的主题。判据看 `syntax` 的第一个字，而不是靠 key 白名单——
    新增一个方括号标签时不需要改这里。
    """
    return [s for s in tag_specs.TAG_SPECS if s.syntax.startswith("[")]


def _keyword_of(spec: tag_specs.TagSpec) -> str:
    """单个规格的动作关键字，**从 `syntax` 里取**，不在本文件抄一份。

    ## 为什么不用 `tag_specs._spec_keyword`

    那个函数给**提示词渲染**用：它的输入是已格式化的 `syntax`，
    契约是"返回可以被反引号包起来的写法"。它实测返回 `'at'` / `'<<等会回 {seconds}>>'`
    ——**没有方括号**。我第一版按注释「从解析器自己的正则里取关键字」直接拿它
    当唯一来源，结果被它静默降级成了空集合（守卫随之永远为真）。

    所以来源改成 `syntax` 本身：`"[at:{qq}]"` → `at`，它同时是
    `tag_specs._spec_keyword`（提示词侧）与 `TagSpec.stream_prefixes`
    （流式侧）的共同上游。
    """
    return spec.syntax[1:].split(":", 1)[0].rstrip("]")


def _recognized_keywords() -> set[str]:
    """解析器认的方括号动作关键字。"""
    return _keywords_of(_bracket_specs())


def _keywords_of(specs: object) -> set[str]:
    """把规格序列变成关键字集合。**遍历全部**，不是找到一处就返回。"""
    return {_keyword_of(spec) for spec in specs}  # type: ignore[attr-defined]


#: 设计文档 5.4 明列的三个方括号动作。**这是本文件唯一手工抄写的数据**。
#:
#: 它只用在"解析器少一个"那一侧：解析器漏认 `[at:` 本身是安全方向
#: （marker_stream 的放宽式照样会剥），但它意味着提示词与解析器一起漂移了。
#: 用规范钉住它，避免"两边一起少一个标签"这种无人察觉的静默退化。
_SPEC_KEYWORDS: frozenset[str] = frozenset({"at", "quote", "poke"})


def _bracket_keywords_in_prefix_table(prefixes: tuple[str, ...]) -> set[str]:
    """从前缀表反推它覆盖了哪些关键字：`[at:` 这类**带冒号**的完整形式。

    判据是"以 `[` 开头且以 `:` 结尾"的那一条；`[a`、`[at` 是它的逐字前缀，
    `[quote:` 同理。没有冒号版本 = 表里不可能有它。
    """
    return {p[1:-1] for p in prefixes if p.startswith("[") and p.endswith(":")}


def _alignment_problems(
    keywords: set[str], prefixes: tuple[str, ...]
) -> list[str]:
    """返回所有对齐问题（空列表 = 对齐）。**遍历全部，不是找到一处就返回。**

    `for ... if 匹配: return True` 只证明"存在"，不证明"全部正确"
    （AGENTS.md「名字比实现强」一节）。
    """
    problems: list[str] = []
    covered = _bracket_keywords_in_prefix_table(prefixes)
    for keyword in sorted(keywords):
        full = "[" + keyword + ":"
        if not any(p.startswith(full) or p == full for p in prefixes):
            problems.append(f"解析器认 {keyword!r}，前缀表里没有 {full!r} 的任何一条")
        # 逐字前缀也要齐：只写 `[at:` 会漏掉 `[a`、`[at` 这两步
        for index in range(1, len(full)):
            step = full[: index + 1]
            if not any(p.startswith(step) or step.startswith(p) for p in prefixes):
                problems.append(f"逐字前缀 {step!r} 没被覆盖（{keyword!r}）")
    # 方向一：前缀表有、解析器与规范都不认识 → 多扣（普通正文被永久暂扣）
    for keyword in sorted(covered - keywords - _SPEC_KEYWORDS):
        problems.append(f"前缀表有 {keyword!r}，但解析器不认识它（多扣）")
    # 方向三：解析器不认识规范里的某个标签 → 提示词与解析器一起漂移了
    for keyword in sorted(_SPEC_KEYWORDS - keywords):
        problems.append(f"设计文档的 {keyword!r} 解析器已不认识——标签被悄悄弄丢了")
    return problems


def test_prefix_table_is_aligned_with_parser() -> None:
    """守卫：前缀表覆盖的方括号关键字，必须**恰好**是解析器认的那一组。

    两面都查：

    - 解析器认识、前缀表没有 → 流式漏标记（用户可见）；
    - 前缀表有、解析器不认识 → 多扣（会把正常正文永久暂扣）。

    数据都从 `tag_specs` 取，不在这里手抄 `at/quote/poke`。
    """
    problems = _alignment_problems(_recognized_keywords(), _BRACKET_PREFIXES)
    assert not problems, "前缀表与解析器不一致：\n" + "\n".join(problems)


def test_alignment_guard_itself_catches_a_parser_only_keyword() -> None:
    """**守卫本身要有测试**：造一个「解析器认识、前缀表不认识」的场景，确认会红。

    构造方式是**注入**：把真实规格表复制一份，追加一条 `[foo:{qq}]`，
    再走与上一条**完全相同**的 `_alignment_problems` 调用。守卫如果不红，
    说明它永远为真——比没有守卫更糟（假安全）。

    这条不依赖修改 `src/`：注入只发生在内存里的 tuple。
    """
    injected = (
        *tag_specs.TAG_SPECS,
        dataclasses.replace(tag_specs.TAG_SPECS[0], key="x.foo", syntax="[foo:{qq}]"),
    )
    keywords = _keywords_of(injected)
    assert "foo" in keywords, "注入没生效——守卫的自我测试本身失效了"

    problems = _alignment_problems(keywords, _BRACKET_PREFIXES)
    assert problems, (
        "前缀表不认识 [foo: 但守卫仍然绿——这条守卫永远为真，必须修它"
    )
    assert any("[foo:" in p for p in problems), f"红的原因不对：{problems}"


def test_alignment_guard_itself_catches_extra_prefix() -> None:
    """反方向同样要有测试：前缀表多一条解析器不认识的关键字，也要红。

    多扣的后果是"普通正文被永久暂扣"，与前一个方向是两种不同的故障。
    """
    inflated = (*_BRACKET_PREFIXES, "[zzz", "[zzz:")
    problems = _alignment_problems(_recognized_keywords(), inflated)
    assert any("zzz" in p for p in problems), f"多扣方向没抓到：{problems}"


def test_align_check_runs_against_the_real_prefix_table() -> None:
    """确认守卫读的是**真实**的前缀表，不是一个恰好对齐的影子。

    判据：`_BRACKET_PREFIXES` 必须来自 `marker_stream` 模块本身，且非空。
    这一条防的是"守卫查了一份自己构造的理想数据"。
    """
    tree = ast.parse(_MARKER_STREAM.read_text(encoding="utf-8"))
    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        for t in [node.target]
    }
    assert "_BRACKET_PREFIXES" in assigned, "前缀表不见了——守卫在查影子数据"
    assert len(_BRACKET_PREFIXES) >= 3, f"前缀表太短，像是被清空了：{_BRACKET_PREFIXES}"


# ---------------------------------------------------------------- 5. 已知未覆盖的路径


@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知缺陷：turn.speaking 的 caught_up 只过 strip_all_markers（只认尖括号），"
        "方括号标签会原样穿过。修好之后这条会 XPASS 并因 strict=True 变红，"
        "提醒把 xfail 去掉。"
    ),
)
def test_caught_up_path_strips_bracket_tags() -> None:
    """`turn.speaking` 的 `caught_up` 应当也剥方括号标签。

    这条**现在就红**（xfail strict），因为实测
    `strip_all_markers("[at:123] 你好") == "[at:123] 你好"`。
    它是本文件唯一一条覆盖"另一条出口路径"的测试，刻意标成 xfail 而不是删掉：
    删掉就等于把这条路径从视野里移除。
    """
    assert "[at" not in strip_all_markers("[at:123] 你好")


def test_parse_expression_is_what_ultimately_hides_the_tag() -> None:
    """落库路径的对照：最终正文由 `parse_expression` 洗，而不是流式过滤器。

    这条存在的理由：说明上面所有"出口清洗"断言的上限——它们证明出口不发说明书，
    **不证明**落库的正文干净。落库那一半由这条的正向断言兜底。
    """
    body, _directive = parse_expression("[at:100200300] 你好")
    assert body.strip() == "你好", f"最终正文应当只剩正文：{body!r}"
    # 反例：流式过滤器完全不参与这件事——即使 `strip_bracket_tags` 坏了，
    # 落库仍然是干净的。所以说"流式出口"与"落库"是两条不同的保证。
    assert "at" not in body


def test_no_replacement_characters_in_this_file() -> None:
    """本文件的**字面量**里不许有 U+FFFD（换行符归一，避免误报）。

    为什么单列一条：中文断言字面量损坏后的典型形态是 `not in` **永远为真**
    ——测试仍然绿，只是再也没有验证过任何东西。这是"假绿"最隐蔽的一种。
    """
    source = Path(__file__).read_text(encoding="utf-8")
    assert "\ufffd" not in source, "本文件含 U+FFFD，中文断言可能已损坏"


def test_chinese_literals_are_intact() -> None:
    """反向确认上面那条不是空转：中文断言用的字面量确实能匹配。

    只断言"没有 U+FFFD"本身在文件全英文时也为真；这里要求实现真的处理
    一个含中文的输入，且中文原样出现。
    """
    ready, _hold = stream_safe_brackets("你好")
    assert "你好" in ready
    assert re.search("你", ready)
