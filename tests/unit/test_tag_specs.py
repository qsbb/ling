"""`TagSpec` 统一能力目录：语法 / 参数校验 / 可用性 / 说明的**唯一出处**。

依据 `docs/design/CAPABILITY-TAG-SYSTEM.md` 第 2a 节（统一能力目录）与第 3 节
（具体缺陷清单）。

## 这个文件按"缺陷"组织，不按"函数"组织

每条测试开头写清它**必须失败于哪个缺陷**（AGENTS.md「补测试时，要写清它必须
失败于哪个缺陷」）。没有对应缺陷的测试就是"测到了实现"，不是"测到了需求"。

## 负向断言必须配对正向断言（AGENTS.md「负向断言的效力」）

只断言"`[at:` 不出现"是没信息量的：一个什么都不渲染的实现在所有负向断言下
全绿。所以每一条"不出现"都配一条"同时出现"，而且**打同一个注入点**
（同一次 `describe_actions()` / 同一次 `parse_expression()`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ling.capabilities.expression_choice import parse_expression, render_expression_help
from ling.capabilities.tag_specs import (
    MAX_TARGETS,
    REASON_CODES,
    TAG_SPECS,
    TagSpec,
    TurnEnvironment,
    advertise,
    by_key,
    defer_available,
    describe_actions,
    quote_available,
    strip_candidates,
)

# ---------------------------------------------------------------- 目录完整性


def test_every_deferred_string_key_has_a_parser() -> None:
    """**每条展示的语法都必须有解析器**（设计文档"建议的实施顺序"第 2 步验收）。

    这个目录的价值全在"唯一出处"上。只要有一处说明引用了目录里不存在的语法，
    它立刻退化成又一份手写清单——而那份清单不会和解析器一起改。

    做法：目录里的每个 `syntax`，它的**固定骨架**必须能被对应的 `pattern`
    匹配到某个具体参数上。骨架取 `{` 之前的部分 + `]` / `>>` 结尾。
    """
    samples = {"qq": "100200300", "message_id": "771", "seconds": "90"}
    for spec in TAG_SPECS:
        assert isinstance(spec, TagSpec)
        rendered = spec.syntax.format(**{spec.arg_name: samples[spec.arg_name]})
        match = spec.pattern.search(rendered)
        assert match is not None, (
            f"{spec.key} 的 syntax {spec.syntax!r} 渲染成 {rendered!r} 之后，"
            f"自己的 pattern 拒绝它——说明展示的语法和解析器认的语法已经分叉"
        )


def test_catalog_has_no_duplicate_keys_and_no_orphan_reasons() -> None:
    """键唯一；原因码只用 `REASON_CODES` 里列出的那些。

    原因码是**会进日志并按它聚合计数**的稳定字符串。随手写一个新的字面量，
    统计就会悄悄分裂成两个桶，而两边都看起来"正常工作"。
    """
    keys = [spec.key for spec in TAG_SPECS]
    assert len(keys) == len(set(keys)), f"重复的 key：{keys}"

    known = set(REASON_CODES)
    for spec in TAG_SPECS:
        # 规格本身的合法性：max_occurrences 至少 1，否则它永远不可执行。
        assert spec.max_occurrences >= 1, f"{spec.key} 的 max_occurrences 不合理"
        assert spec.arg_name in spec.syntax, (
            f"{spec.key} 的 syntax 里没有参数位 {{{spec.arg_name}}}——那样它就不是可参数化的动作"
        )
    assert "invalid_target" in known


def test_by_key_raises_on_unknown_instead_of_returning_none() -> None:
    """查不存在的键必须**炸**，不能返回 None。

    `by_key(...)` 返回 None 之后，调用方下一个动作通常是对它取属性——
    于是错误发生在离根因很远的地方，而且报的是 `AttributeError`，
    看起来像"某个字段没配"。这里把失败点钉在查表处。
    """
    assert by_key("message.mention").key == "message.mention"
    with pytest.raises(KeyError):
        by_key("message.nonexistent")


# ---------------------------------------------------------------- 缺陷 #3：@/拍一拍参数


@pytest.mark.parametrize(
    "raw",
    [
        "[at:abc] 你好",  # 非数字 QQ 号
        "[at:12 34] 你好",  # 参数里有空格
        "[at:] 你好",  # 空参数
        "[at:0123] 你好",  # 首位零，不是 QQ 号形状
        "[at:123456789012345678901] 你好",  # 超 20 位
    ],
)
def test_non_numeric_mention_target_is_never_an_action(raw: str) -> None:
    """**缺陷 #3**：旧正则把任意 1-40 个非空白非 `]` 字符当 QQ 号。

    `[at:abc] 你好` 以前会变成：正文"你好"、@ 列表 `["abc"]`——而 `abc` 随后
    被原样塞进 OneBot 的 `at.qq` 字段。

    ## 这条的配对正向断言

    同一个函数、同一个测试里，`[at:100200300] 你好` **必须**产生 @ 目标。
    只测"abc 没进列表"的话，一个永远返回空列表的实现也能通过。
    """
    text, directive = parse_expression(raw, env=_group_env())
    assert directive.at == [], f"{raw!r} 不该产生 @ 目标（它形状不合法）"
    # 正文里也不许残留这个说明书（清洗掉，而不是原样外发）
    assert "[at:" not in text, f"{raw!r} 的标记本体漏进了正文：{text!r}"
    assert any(o.reason == "invalid_syntax" for o in directive.ignored), (
        f"{raw!r} 被剥掉了，却没有任何原因码——这正是「标记消失=成功」那类假象"
    )

    # 配对正向：合法形状**必须**产生目标
    ok_text, ok = parse_expression("[at:100200300] 你好", env=_group_env())
    assert ok.at == ["100200300"], f"合法 QQ 号没被识别：{ok.at}"
    assert ok_text == "你好"


def test_uppercase_tag_is_reported_as_invalid_case_not_silently_kept() -> None:
    """**缺陷 #3（另一半）**：`[AT:123]` 以前原样留在正文里 → 说明书外发。

    那一半比"太宽"更难发现：它不产生错误动作，它产生的是**用户读到说明书**。

    修法不是启用 `IGNORECASE`（那会把它当动作执行），而是"认出候选、
    不执行、清洗掉、记 `invalid_case`"。

    配对正向：小写 `[at:123]` **必须**执行。
    """
    text, directive = parse_expression("[AT:123] 你好", env=_group_env())
    assert directive.at == [], "[AT:123] 不该被执行（关键字固定小写）"
    assert "[AT:123]" not in text, f"大写标记原样漏进了正文：{text!r}"
    reasons = {o.reason for o in directive.ignored}
    assert "invalid_case" in reasons, (
        f"大小写错误没有单独的原因码，实际是 {reasons}——"
        "它和「参数形状不对」是两个不同的模型错误，混在一起就无法判断该改提示词还是改解析"
    )

    _, lower = parse_expression("[at:123] 你好", env=_group_env())
    assert lower.at == ["123"], "小写合法标记反倒没被执行——清洗把真动作也吞了"


# ---------------------------------------------------------------- 缺陷 #2：引用语法


@pytest.mark.parametrize(
    "raw",
    [
        "[quote:abc] 你好",
        "[quote:] 你好",
        # ## 这两条是变异 2 补进来的
        #
        # 原来的 `[quote:消息 id]` 里**含空格**，而宽松正则
        # `[^\\s\\]]{1,40}` 也排除空格——两种实现在这个输入上结果相同，
        # 它**什么都没证明**（变异 2 的红名单里没有它）。
        #
        # 下面这两条只含非空白字符，宽松版会当作合法 id：
        #   - `消息`：中文，不是数字也不是 `m`+6 位 base32
        #   - `12ab`：数字开头但含字母
        "[quote:消息] 你好",
        "[quote:12ab] 你好",
    ],
)
def test_quote_requires_a_real_id_shape(raw: str) -> None:
    """**缺陷 #3（引用侧）**：`[quote:消息 id]` 这种说明书以前能通过宽松正则。

    它随后必然在 `reply_ids` 里查不到，被静默降级——正文照发、引用消失，
    本地只有一行 info 日志。

    配对正向：`[quote:771]`（本轮真实 id）**必须**产生 quote。
    """
    text, directive = parse_expression(raw, env=_group_env({"771": "srv:771"}))
    assert directive.quote is None, f"{raw!r} 不该被当成引用"
    assert "[quote:" not in text, f"引用标记本体漏进正文：{text!r}"

    # ## 必须断言**原因码**，不能只断言"没执行"
    #
    # 变异 2（把正则放回宽松形状）下这条原本**不红**：放宽之后 `[quote:消息]`
    # 依然产生不了引用——因为 `visible_message_ids` 那道校验兜住了，只是原因码
    # 从 `invalid_syntax` 变成了 `invalid_target`。
    #
    # 也就是说"引用没发生"这个结果，在正确实现和变异实现下**完全一样**。
    # 有区分力的只有原因码：形状错误（该改解析）与 id 不存在（该改提示词）
    # 是两种不同的模型错误，混成一个桶就没人能判断该修哪边。
    reasons = {o.reason for o in directive.ignored}
    assert "invalid_syntax" in reasons, (
        f"{raw!r} 的参数形状不合法，原因码却是 {reasons}——"
        "它应当被判成 invalid_syntax（形状），而不是 invalid_target（id 不存在）"
    )

    ok_text, ok = parse_expression("[quote:771] 这条我回一下", env=_group_env({"771": "srv:771"}))
    assert ok.quote == "771", f"本轮真实 id 没被接受：{ok.quote}"
    assert ok_text == "这条我回一下"


def test_quote_to_unknown_id_is_invalid_target() -> None:
    """**缺陷 #7**：形状合法但 id 不存在 → `invalid_target`（不是静默丢弃）。

    旧实现里"引用被丢"只有一行 `logger.info`，而调用方拿到的是一个
    看不出差别的 `Directive`。

    三态区分（AGENTS.md「一个断言能证明什么」）：

    - 非空 id 表 + 命中 → 接受
    - 非空 id 表 + 未命中 → `invalid_target`
    - **空** id 表（采集过、一条都没有）→ `invalid_target`
    - `None`（没采集）→ 只做形状校验、接受（旧调用点的兼容路径）

    四个值各一条，因为只测其中两三个时，其余几个的实现可以任意错。
    """
    _, hit = parse_expression("[quote:771] 好", env=_group_env({"771": "srv:771"}))
    assert hit.quote == "771"

    _, miss = parse_expression("[quote:3] 好", env=_group_env({"771": "srv:771"}))
    assert miss.quote is None
    assert any(o.reason == "invalid_target" for o in miss.ignored), (
        "引用了不存在的 id 却没记 invalid_target"
    )

    _, empty_table = parse_expression("[quote:3] 好", env=_group_env({}))
    assert empty_table.quote is None, "采集过但一条 id 都没有时，任何引用都必须拒绝"

    _, uncollected = parse_expression(
        "[quote:3] 好",
        env=TurnEnvironment(channel="onebot", is_group=True, visible_message_ids=None),
    )
    assert uncollected.quote == "3", (
        "`None`（没采集 id 表）被当成了 `{}`（采集过但没有）——"
        "这两态的正确答案相反：前者只做形状校验，后者全拒"
    )


# ---------------------------------------------------------------- 缺陷 #4：重复与超限


def test_duplicate_and_over_limit_targets_are_reported_not_silently_dropped() -> None:
    """**缺陷 #4**：重复或超 `MAX_TARGETS` 的标记被删，但**只保留前三个**，
    模型写出的其余选择没有任何执行结果。

    ## 为什么要断言"个数"而不是"前三个还在"

    只断言"留下的正好是前三个"的话，一个"把第四个也执行了"的实现同样通过
    （前三个还在，只是多了一个）。所以这里同时断言：
      - 执行的是前 N 个（`at` 正好 N 个且顺序正确）
      - 第 N+1 个**有** `limit_exceeded`
      - 重复的那个**有** `duplicate`
    """
    raw = "".join(f"[at:{1000 + i}] " for i in range(MAX_TARGETS + 2)) + "大家好"
    text, directive = parse_expression(raw, env=_group_env())

    assert directive.at == [str(1000 + i) for i in range(MAX_TARGETS)], (
        f"执行的目标不是前 {MAX_TARGETS} 个：{directive.at}"
    )
    reasons = [o.reason for o in directive.ignored]
    assert reasons.count("limit_exceeded") == 2, (
        f"超限的两个目标没有各记一条 limit_exceeded，实际原因码：{reasons}"
    )
    assert text == "大家好"

    # 重复：同一个目标写两次，第二次必须是 duplicate（而不是"又执行了一次"）
    _, dup = parse_expression("[at:100200300] 你[at:100200300]好", env=_group_env())
    assert dup.at == ["100200300"], f"重复目标被执行了两次：{dup.at}"
    assert any(o.reason == "duplicate" for o in dup.ignored), (
        "重复目标被静默丢掉，没有 duplicate 原因码"
    )


def test_second_quote_is_duplicate_not_silently_ignored() -> None:
    """引用只取第一条（`max_occurrences=1`），第二条要**显式**记 `duplicate`。"""
    _, directive = parse_expression(
        "[quote:771] [quote:772] 好", env=_group_env({"771": "a", "772": "b"})
    )
    assert directive.quote == "771"
    assert any(o.key == "message.quote" and o.reason == "duplicate" for o in directive.ignored), (
        "第二条引用被静默丢弃，没有任何痕迹"
    )


# ---------------------------------------------------------------- 缺陷：只有标记没有正文


@pytest.mark.parametrize("raw", ["[at:100200300]", "[poke:100200300]", "[quote:771]"])
def test_marker_only_body_is_flagged_empty_body(raw: str) -> None:
    """只有标记、没有正文：**不能静默外发说明书，也不能发空消息**。

    ## 这一条现状是什么、为什么是这样

    设计文档 5.2 建议：`poke` 可作为纯动作请求、单独的 `at/quote` 不执行且
    不补回原文。本仓库**没有纯动作路径**（`turn.py` 只在 `delivered` 非空时
    落库 actor 消息），改成"不补回原文"会让用户收到一条空消息或什么都收不到
    ——比发一句说明书更糟。

    所以保留"补回原文"的行为，但把结果**显式**标成 `empty_body`：
    调用方现在能知道"这一轮其实什么都没执行"，而这在旧实现里是完全看不出来的
    （`Directive.empty` 为 True 时，`at`/`poke` 列表**已经被清空**了，
    连"她本来想 @ 谁"都丢了）。
    """
    text, directive = parse_expression(raw, env=_group_env({"771": "a"}))
    assert directive.empty, "只有标记时不该产生任何动作"
    assert text == raw, f"只有标记的回复被改写了：{text!r}——若这里变成空串，调用方会去发一条空消息"
    assert directive.ignored, "只有标记时没有任何原因码，调用方无从知道发生了什么"
    assert {o.reason for o in directive.ignored} == {"empty_body"}, (
        f"原因码应统一为 empty_body，实际：{[o.reason for o in directive.ignored]}"
    )


# ---------------------------------------------------------------- 缺陷 #2：提示词自相矛盾


def test_private_chat_body_never_mentions_quote_or_mention() -> None:
    """**缺陷 #2**：私聊不列引用，另一段又无条件教 `[quote:…]` → 自相矛盾。

    修法是把引用收进**唯一**的渲染入口。注意：这条用例构造的私聊**没有传
    `visible_message_ids`**（``None``，即"没采集过 id"），所以它现在证明的是
    "没有 id → 不宣告引用"，**不是**"私聊 → 不宣告引用"——
    私聊 + 有 id 时必须宣告，那是
    `test_private_chat_with_visible_id_advertises_quote` 钉住的。

    `[at:` 仍然在私聊里不出现：@ 那条判据（`_private_or_group`）**没有**改动。

    ## 这条断言必须打"真的编译出来的提示词"，不能只测 render 函数

    `context.py` 以前正是"两段各写一份"的现场：即使 `render_expression_help`
    在私聊不列引用，另一段照样注入 `[quote:…]`。只测 render 函数的话，
    删掉那段重复注入也不会红（它不在被测范围内）。

    配对正向：群聊 + 有 id 时 `[quote:` **必须**出现。
    """
    private = describe_actions(TurnEnvironment(channel="onebot", is_group=False))
    assert "[quote:" not in private, f"私聊里宣告了引用：{private!r}"
    assert "[at:" not in private, f"私聊里宣告了 @：{private!r}"

    group = describe_actions(_group_env({"771": "a"}))
    assert "[quote:" in group, f"群聊有 id 却没宣告引用：{group!r}"
    assert "[at:" in group, f"群聊没宣告 @：{group!r}"


def test_quote_is_not_advertised_without_any_message_id() -> None:
    """群聊但**一条 id 都没有** → 不提引用。

    提了她只能瞎编（线上见到过 `[quote:3]`），然后被校验丢掉——正文照发，
    用户看不出任何异常。所以「渠道支持」与「本轮真有 id」**缺一个都不该宣告**。
    （会话类型不是这条判据，见 `quote_available`：私聊 + 有 id 同样宣告。）

    配对正向：同样环境 + 有 id → 出现。
    """
    no_ids = describe_actions(TurnEnvironment(channel="onebot", is_group=True))
    assert "[quote:" not in no_ids, f"没有 id 却宣告了引用：{no_ids!r}"
    assert "[at:" in no_ids, "同样这次的对照组：@ 与 id 无关，必须仍出现"

    with_ids = describe_actions(_group_env({"771": "a"}))
    assert "[quote:" in with_ids


def test_private_chat_with_visible_id_advertises_quote() -> None:
    """私聊 + 有可见 message id → 引用可用。

    依据：connector 的 `send_private_msg` 与 `send_group_msg` 都传 reply 段，
    引用没有私聊/群聊分支。真正的约束是 `_known_message_set` 白名单
    （引用目标必须是真见过的入站 message_id）。

    ## 这条必须失败于哪个缺陷

    上面那条 `test_private_chat_body_never_mentions_quote_or_mention` 的私聊
    用例**没传 `visible_message_ids`**，于是它绿的理由从"因为私聊"变成了
    "因为没有 id"——即使把 `quote_available()` 改回 `... and env.is_group`
    （旧行为），它**照样绿**。本用例才是钉住"会话类型不是条件"的那一条：
    变异成旧行为时它是红的（`quote_available` 返回 False、`message.quote`
    从 `advertise()` 里消失）。

    ## 为什么必须带第三条配对断言

    前两条只证明"引用放开了"。顺手把 @ 也一并放开（比如误删
    `_private_or_group`）同样能让它们全绿，而那是另一个产品错误：私聊里
    @ 只会给对方多一条通知。所以第三条断言 `message.mention` **不在**
    里面，与"引用在"打的是同一个 `advertise()` 注入点。
    """
    # 构造：私聊 + channel="onebot" + visible_message_ids 非空
    env = TurnEnvironment(
        channel="onebot",
        is_group=False,
        visible_message_ids={"771": "srv:771"},
    )

    # 断言 1: 引用可用
    assert quote_available(env) is True, (
        "私聊 + 有可见 id 时把引用判成了不可用——"
        "OneBot 的 `send_private_msg` 与 `send_group_msg` 都用同一段引用构造，"
        "引用没有会话类型这道条件"
    )
    # 断言 2: 引用被宣告（同一次 advertise，与断言 3 同一注入点）
    keys = {spec.key for spec in advertise(env)}
    assert "message.quote" in keys, f"私聊 + 有 id 却没宣告引用：{sorted(keys)}"
    # 断言 3: 配对负向——@ 不许跟着一起放开
    assert "message.mention" not in keys, (
        f"私聊里把 @ 也放开了：{sorted(keys)}——"
        "`_private_or_group()` 只服务 `mention`，修好引用不等于 @ 也能用"
    )


def test_private_helper_never_advertises_anything_unexecutable() -> None:
    """`render_expression_help` 在"什么都没有"的私聊里返回**空串**。

    旧实现无论如何都会输出一句"你可以选择怎么表达…"以及 `[speak]` 之外的
    收尾说明——在一个既不能 @、不能引用、不能拍一拍、也不能延后的私聊里，
    她读到的是"有这些选择，但你一个都用不了"。

    配对正向：群聊 + OneBot + 有 id 时返回非空，且三种动作都在。
    """
    nothing = render_expression_help(is_group=False, supports_poke=False, channel="")
    assert nothing == "", f"没有任何可用动作时仍输出了说明：{nothing!r}"

    full = render_expression_help(is_group=True, supports_poke=True)
    assert "[at:" in full and "[quote:" in full and "[poke:" in full


# ---------------------------------------------------------------- supports_poke 必填


def test_supports_poke_is_required_keyword() -> None:
    """**漏传必须炸，不能静默退化成 True。**

    这是设计文档第 3 节第 1 行的最后一环：`supports_poke: bool = True` 这个
    默认值与需求方向相反。它让"新增调用点忘了传"变成"对所有渠道宣称能拍一拍"
    ——代码照跑、测试照绿、用户看到"她说要拍你但没拍"。

    改成必填后的判据很直接：**调用失败的类型必须是 `TypeError`**，
    而不是"返回了一段不含 poke 的文本"。
    """
    with pytest.raises(TypeError):
        render_expression_help(is_group=True)  # type: ignore[call-arg]

    # 配对正向：显式传了就正常工作（两种取值都测，避免"只要传了就都一样"）
    assert "[poke:" in render_expression_help(is_group=True, supports_poke=True)
    assert "[poke:" not in render_expression_help(is_group=True, supports_poke=False)


def test_explicit_poke_false_is_not_overridden_by_channel_inference() -> None:
    """显式 `supports_poke=False` **不能**被"渠道名推导"加回来。

    ## 这条抓的是一个真实发生过的失效

    第一版里 `render_expression_help(supports_poke=False)` 的返回值**仍含
    `[poke:…]`**：参数被读到了、也被用到了，然后 onebot 的渠道推导把它加回来。
    传了参数却没生效——**不报错的失效**。

    所以这里必须用 `channel="onebot"`（推导为"有 poke"的那个渠道）：
    用别的渠道测，推导本来就给 False，两种实现结果相同，**证明不了任何事**。
    """
    text = render_expression_help(is_group=True, supports_poke=False, channel="onebot")
    assert "[poke:" not in text, f"显式关闭拍一拍之后，onebot 的渠道推导又把它加回来了：{text!r}"
    # 配对正向：同一个渠道、显式打开 → 必须出现（证明推导本身没被删掉）
    assert "[poke:" in render_expression_help(is_group=True, supports_poke=True, channel="onebot")


# ---------------------------------------------------------------- 缺陷 #1：延后渠道


@pytest.mark.parametrize("channel", ["onebot", "qqbot", "qq", "wechat"])
def test_defer_is_not_offered_on_external_channels(channel: str) -> None:
    """**缺陷 #1**：任意私聊都收到延后说明，但白名单只有 `web`/空渠道。

    后果是外部私聊里她选了延后 → 登记处拒（`channel_unsupported`）→ 按立即
    回复处理。用户看到"她说要晚点回，结果立刻回了"。

    配对正向：`web` 与空渠道（真的支持延后）**必须**出现。
    """
    env = TurnEnvironment(channel=channel, is_group=False)
    assert defer_available(env) is False, (
        f"{channel} 不在 SUPPORTED_CHANNELS 里，却判定可延后——提示词会给出一个登记处一定拒绝的选项"
    )
    assert "等会回" not in describe_actions(env), (
        f"{channel} 私聊里仍渲染了延后说明——提示词与登记闸不一致"
    )

    for supported in ("web", ""):
        assert defer_available(TurnEnvironment(channel=supported, is_group=False)), (
            f"{supported!r} 是支持的渠道，却被判定不可延后"
        )


def test_group_chat_is_never_offered_defer() -> None:
    """群聊不给延后，**即使**它被加进白名单也要单独评估。

    这里断言的是"群聊当前不可用"这件事本身（外发闸管不到群渠道的投递）。
    与上面的外部渠道私有不同：那条讲的是"渠道白名单"，这条讲的是"会话类型"。
    """
    assert defer_available(TurnEnvironment(channel="web", is_group=True)) is False
    assert "等会回" not in describe_actions(TurnEnvironment(channel="web", is_group=True))


# ---------------------------------------------------------------- 缺陷 #5：目录驱动的流式前缀


def test_every_inline_spec_declares_stream_prefixes() -> None:
    """行内动作必须声明流式暂扣前缀，否则 `[at:` 会先漏给客户端。

    这条只断言**声明存在**，不测流式行为本身（那在 `marker_stream` 的测试里，
    属于另一个改动面）。它要防的是"目录加了新动作、忘了声明前缀"——
    那种漏法在流式测试里表现为"新动作漏一轮"，而没人会想到去目录里找。

    配对正向：正文清洗真的会剥掉完整标签（`strip_candidates`）。
    """
    for spec in TAG_SPECS:
        if spec.placement != "inline":
            continue
        assert spec.stream_prefixes, f"{spec.key} 没有声明流式暂扣前缀"
        # 前缀必须包含动作名的完整骨架，否则"扣住了再放开"那一步会漏
        assert any(p.endswith(":") for p in spec.stream_prefixes), (
            f"{spec.key} 的前缀里没有带冒号的完整骨架：{spec.stream_prefixes}"
        )

    assert strip_candidates("[at:100200300] 你好") == " 你好"
    assert strip_candidates("[1] 参考文献") == "[1] 参考文献"


# ---------------------------------------------------------------- 不误触发


@pytest.mark.parametrize(
    "raw",
    ["[1] 参考文献", "[链接](http://192.0.2.9)", "[CQ:at,qq=100200300]", "1<<2 就是 4"],
)
def test_ordinary_bracket_text_is_untouched(raw: str) -> None:
    """普通方括号不是动作，**一个字都不许动**。

    设计文档 5.4 的反例表点名了这几个。把 `[` 一律扣住或一律剥掉能让
    "不泄露标签"全绿，代价是用户看到的正文和模型生成的不一样——而且没有痕迹。
    """
    text, directive = parse_expression(raw, env=_group_env())
    assert text == raw, f"{raw!r} 被改写了：{text!r}"
    assert directive.empty
    assert not directive.ignored, f"{raw!r} 被当成候选并记了原因：{directive.ignored}"


# ---------------------------------------------------------------- 可用性快照


def test_advertise_only_lists_what_is_executable_now() -> None:
    """`advertise()` 是"本轮可用"的唯一判据，`describe_actions` 与解析都读它。

    这条钉的是"两处不许各筛一遍"：把同一个 env 交给 advertise 与
    describe_actions，前者给出的 key 集合必须与后者文本里出现的动作**完全对应**。
    """
    env = _group_env({"771": "a"})
    keys = {spec.key for spec in advertise(env)}
    text = describe_actions(env)
    for spec in TAG_SPECS:
        if spec.placement != "inline":
            continue
        marker = spec.syntax.split("{")[0]
        if spec.key in keys:
            assert marker in text, f"{spec.key} 被 advertise 了，却没出现在说明里"
        else:
            assert marker not in text, f"{spec.key} 没被 advertise，却出现在了说明里"


# ---------------------------------------------------------------- helpers


def _group_env(ids: dict[str, str] | None = None) -> TurnEnvironment:
    """一个"什么都能做"的 OneBot 群聊环境。

    默认给一条 id，因为"有 id"是引用可用的必要条件之一；要测"没有 id"
    的路径请显式传 ``{}`` 或 ``None``。
    """
    return TurnEnvironment(
        channel="onebot",
        is_group=True,
        visible_message_ids={"771": "srv:771"} if ids is None else ids,
    )


# ------------------------------------------------- 接线：目录必须**真的**被调用


def _call_names_in(path: Path) -> set[str]:
    """把文件里所有 `xxx(...)` 形式的调用名收集起来（AST，不看注释）。

    ## 为什么必须用 AST

    本文件要断言的正是"`context.py` 调用了某个函数"。用 `"render_expression_help"
    in source` 这种字符串搜索，**注释里提一句就算命中**——而这个仓库的注释
    以"解释为什么这么改"为风格，被禁/被要求的名字在注释里出现的概率很高。
    那样的守卫是假绿的（AGENTS.md「守卫测试必须用 AST 过滤注释」）。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_context_lane_contains_the_catalog_callsite() -> None:
    """**接线守卫（存在性，非有效性）**：能力车道里**有**目录的调用点。

    ## 名字为什么是 `contains_callsite` 而不是 `actually_delegates`

    第一版叫 `..._actually_delegates_...`。做变异时发现：把渲染结果换成手写
    文案、只保留一行 `_unused = render_expression_help(...)`，这条测试**照样绿**。
    "actually" 这个词承诺了它做不到的事——正是 `AGENTS.md` 说的
    「名字比实现强」那一类假通过。所以名字改成它真正证明的东西。

    真正证明"渲染结果来自目录"的是同文件的
    `test_catalog_is_the_only_place_that_writes_the_syntax`（变异 1 下它会红）。
    两条合起来才够：这条说"调用在"，那条说"别处没手写"。

    ## 这条测试是被一次失败的变异逼出来的

    变异 1 的做法是"目录已经建好，但 `context.py` 仍用自己拼的一段旧文案"。
    当时 `test_context_compile.py` 与整个 `test_tag_specs.py` **全绿**——
    因为旧文案里恰好也有 `[at:` 和「你可以选择怎么表达」，而引用那条断言是
    `"[quote:" not in blob`，旧文案本来就没提引用，于是照样通过。

    也就是说：**那两个文件加起来都证明不了"目录是唯一出处"**，而"唯一出处"
    正是本次改造的全部内容。全绿在这里只说明"结果长得像"，不说明"走了这条路"。

    ## 为什么它必须用结构而不是行为

    行为层面的观察点（提示词里出现了什么）在"目录"和"手写一段话"两种实现下
    可以完全一致——**没有可观测的行为差别**。能区分的只剩"代码调用了谁"。
    这类"两条路径行为相同"的情形，`AGENTS.md` 明确要求另配一条测试，
    否则"语义不同"只是注释里的话。

    ## 这一条不能证明什么

    它只证明**调用存在**，不证明调用时传对了参数。参数的正确性由
    `test_explicit_poke_false_is_not_overridden_by_channel_inference` 和
    `test_quote_is_not_advertised_without_any_message_id` 覆盖。
    """
    from pathlib import Path

    context_path = Path(__file__).resolve().parents[2] / "src/ling/kernel/context.py"
    names = _call_names_in(context_path)

    assert "render_expression_help" in names, (
        "`context.py` 没有调用目录的渲染入口——它一定在自己拼一份说明，"
        "而那正是本次要消灭的「第二份出处」"
    )
    assert "defer_available" in names, (
        "`context.py` 没有调用 `defer_available()`——延后可用的判据又回到了"
        "自己写一个（旧实现是 `not is_group`，它把全部外部私聊都算成了可用）"
    )
    assert "TurnEnvironment" in names, (
        "`context.py` 没有构造 `TurnEnvironment`——说明它传给渲染入口的是"
        "零散布尔值，而不是可复用的环境快照"
    )


def test_catalog_is_the_only_place_that_writes_the_syntax() -> None:
    """其他模块**不许**再手写动作语法字面量（docstring 与注释不算）。

    ## 为什么这条必须存在

    `render_expression_help` 的说明措辞曾经在三处各写一份
    （`expression_choice`、`context` 的另一段）。三份里只要有一份写着
    `[quote:消息序号]`，模型就照着那一份写，然后被执行侧丢弃。
    **语法只能有一个写入点**，否则"唯一出处"就只是口号。

    ## 为什么必须用 AST 跳过 docstring

    第一版是对 `ast.Constant` 里的字符串一刀切，于是命中的全是 **docstring**：

    - `expression_choice.py:1` 的模块说明
    - `expression_choice.py:162` 解释 `invalid_syntax` 的段落
    - `context.py:491` 讲 `supports_poke` 的段落

    这正是 `AGENTS.md` 说的那个坑：**解释一条禁令必须引用被禁的词**，
    所以注释越写清楚，朴素的字符串搜索越容易误报。而误报的守卫会被人
    加白名单加掉，最后一条也不剩。

    ## 允许的例外（各附理由，不是"让它绿"）

    - `tag_specs.py`：语法的唯一写入点。
    - `marker_stream.py`：流式**暂扣前缀**（`stream_prefixes`）的常量表。
      它们是"半截标记长什么样"的判断依据，与"教模型怎么写"是两件事；
      但它们同样只该出现一次，所以这里断言的是"没有**第四**个地方"。
    - `MUTATION` 标记的字符串不算（变异实验用的临时形状）。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/ling"
    offenders: list[str] = []
    suspicious = ("[at:", "[quote:", "[poke:")

    allowed_files = {"tag_specs.py", "marker_stream.py"}

    for path in src.rglob("*.py"):
        if path.name in allowed_files:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 收集所有 docstring 的 Constant 节点 identity，后面整批跳过。
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:  # ← 关键：跳过 docstring
                continue
            if any(tag in node.value for tag in suspicious):
                offenders.append(f"{path}:{node.lineno} {node.value[:40]!r}")

    offenders = [o for o in offenders if "MUTATION" not in o]
    assert not offenders, (
        "这些地方仍在手写动作语法（应当只在 `tag_specs.py` 里出现一次）：\n" + "\n".join(offenders)
    )
