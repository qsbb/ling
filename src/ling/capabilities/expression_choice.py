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

from ling.capabilities.tag_specs import (
    MAX_TARGETS,
    TagOutcome,
    TurnEnvironment,
    strip_candidates,
)
from ling.ports.speech import SPEAK_TAG

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

# `MAX_TARGETS` 现在从 `tag_specs` 取（那里也写在给模型的说明里）。
# 这里重新导出是为了不破坏既有 import（`tests/unit/test_w2_rules.py` 直接用它）。


@dataclass(frozen=True)
class Directive:
    """她从文本里选定的表达方式。**意图，不是结果。**

    `outcomes` 记录**每个候选的处置**——包括那些没有执行、也从正文里被剥掉的。
    这是本次修复的核心：旧实现里"标记消失"同时代表"执行了""参数不合法"
    "被重复/超限丢掉"三种完全不同的情况，而三者对调用方和日志是不可区分的。
    """

    at: list[str] = field(default_factory=list)
    quote: str | None = None
    poke: list[str] = field(default_factory=list)
    #: 每个候选的处置，按出现顺序。成功的也在里面（`kind="applied"`），
    #: 因为"有 4 个 @ 请求、3 个执行了"这个比例本身就是要观测的东西。
    outcomes: tuple[TagOutcome, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.at and not self.quote and not self.poke

    @property
    def ignored(self) -> tuple[TagOutcome, ...]:
        """没执行的候选。调用方据此留痕（原因码，不含正文与目标值）。"""
        return tuple(o for o in self.outcomes if not o.applied)


def _collect(text: str, env: TurnEnvironment) -> tuple[list[TagOutcome], Directive]:
    """按**合法式**收集动作，并把"像标签但不合法"的记成 ignored。

    这一步**不做正文清洗**（清洗在 `_clean_body` 里，用宽松式再扫一遍）。
    两件事分开做，是因为它们的判据本来就不一样：

    - 执行：合法式 + 本轮可用 + 目标在本轮白名单里；
    - 清洗：只要是"已知动作名 + ASCII 方括号"，就不该原样外发。
    """
    from ling.capabilities import tag_specs

    outcomes: list[TagOutcome] = []
    at: list[str] = []
    poke: list[str] = []
    quote: str | None = None
    usable = {spec.key for spec in tag_specs.advertise(env)}
    # **未采集** id 表时，引用只做形状校验（见 `_validate_quote` 的三态说明）。
    # `advertise()` 里 `quote_available` 必须为 False——提示词不能在没有 id
    # 时宣告引用；但解析这一侧不能因此把形状合法的引用判成"不可用"，
    # 否则所有还没接上采集的调用点上的引用都会静默失效。
    shape_only_quote = env.visible_message_ids is None

    for spec in tag_specs.TAG_SPECS:
        if spec.key == "reply.defer":
            # 延后由 `domains/deferred/markers.parse_defer_marker` 解析
            # （它有自己的"整条独占/时长钳制"语义）。目录只提供可用性，
            # 见 `context.py` 的渲染与 `turn.py` 的登记闸。
            continue
        for match in spec.pattern.finditer(text):
            arg = match.group(1)
            if spec.key not in usable and not (
                shape_only_quote and spec.key == "message.quote"
            ):
                # 这一轮根本不可用（私聊的 @、没有 id 时的引用、渠道不支持拍一拍）。
                # **记下来**而不是静默剥掉：提示词与解析不一致时，这是唯一的证据。
                outcomes.append(TagOutcome(spec.key, arg, "ignored", "unavailable"))
                continue
            reason = spec.validate_target(arg, env)
            if reason:
                outcomes.append(TagOutcome(spec.key, arg, "ignored", reason))
                continue
            if spec.key == "message.quote":
                if quote is not None:
                    # 只认第一条。第二条**显式记 duplicate**——旧实现里它被
                    # 静默丢弃，模型不知道自己写的引用没生效。
                    outcomes.append(TagOutcome(spec.key, arg, "ignored", "duplicate"))
                else:
                    quote = arg
                    outcomes.append(TagOutcome(spec.key, arg, "applied"))
                continue
            bucket = at if spec.key == "message.mention" else poke
            if arg in bucket:
                outcomes.append(TagOutcome(spec.key, arg, "ignored", "duplicate"))
            elif len(bucket) >= spec.max_occurrences:
                outcomes.append(TagOutcome(spec.key, arg, "ignored", "limit_exceeded"))
            else:
                bucket.append(arg)
                outcomes.append(TagOutcome(spec.key, arg, "applied"))

    return outcomes, Directive(at=at, quote=quote, poke=poke, outcomes=tuple(outcomes))


def _record_invalid_candidates(text: str, env: TurnEnvironment) -> list[TagOutcome]:
    """发现"像标签但不合法"的候选，记原因。**不执行。**

    `[at:abc]` / `[AT:123]` / `[at:12 34]` / `[quote:消息 id]` 都属于这一类。

    为什么必须有这一步：它们被清洗掉之后，**从调用方的视角完全消失**——
    旧实现就是这样把 `[at:abc]` 剥掉、再把 `abc` 当 QQ 号传下去的。
    现在它们既不会变成动作，也不会无声消失。
    """
    from ling.capabilities import tag_specs

    usable = {spec.key for spec in tag_specs.advertise(env)}
    findings: list[TagOutcome] = []
    for spec in tag_specs.TAG_SPECS:
        if spec.key == "reply.defer":
            continue
        legal_spans = {(m.start(), m.end()) for m in spec.pattern.finditer(text)}
        for match in spec.candidate_pattern.finditer(text):
            if (match.start(), match.end()) in legal_spans:
                continue  # 已由合法式处理过（含 applied / limit_exceeded 等）
            raw = match.group(0)
            keyword = match.group("kind")
            if keyword.lower() != spec.arg_kind:
                # 宽松式一次扫出**所有**动作名，而 `spec` 只是其中一条。
                # 不按动作名过滤的话，一个 `[at:…]` 会被记成 `message.quote`
                # 的 `invalid_syntax`——原因码对了、归属错了，而归属正是
                # "按动作聚合计数"这件事唯一依赖的东西。
                continue
            # 大小写不对单列一节：它和"参数形状不对"是两个不同的模型错误，
            # 混成一个原因码会让"提示词是不是该提醒她别写大写"无从判断。
            reason = "invalid_case" if keyword != keyword.lower() else "invalid_syntax"
            if spec.key not in usable and not (
                env.visible_message_ids is None and spec.key == "message.quote"
            ):
                reason = "unavailable"
            findings.append(TagOutcome(spec.key, raw, "ignored", reason))
    return findings


def _clean_body(stripped: str) -> str:
    """把剥完标记的正文收拾成人话。

    ## 为什么只收水平空白、不动换行

    换行是她**故意分段**的地方（与 `domains/deferred/markers.py` 同规则）。
    旧实现对每一行 `strip()`，会把"她自己排的版"改掉——那是她的表达，
    代码没有理由动它（设计文档 5.2「不能再全文逐行 `strip()`」）。

    `[at:123] 你说得对` → `你说得对`：标记后面垫的空格要收掉，
    否则正文会以一个空格开头。`好的[at:123]，我看到了` → `好的，我看到了`：
    删掉处两侧的水平空白缩成一个，标点原样保留。
    """
    cleaned = re.sub(r"[ \t]{2,}", " ", stripped)
    return cleaned.strip(" \t\n")


def parse_expression(
    text: str, *, env: TurnEnvironment | None = None
) -> tuple[str, Directive]:
    """剥掉表达标记，返回 (正文, 她选了什么)。

    ## ``env`` 为什么可空

    不传 = **按"最宽松的环境"解析**，即保持旧调用点（与既有测试）可用：
    群聊、OneBot、有引用 id、无目标白名单。这是一个**过渡默认值**，不是
    "无所谓"——它让没接线的地方继续按旧行为工作，同时让接了线的地方能
    按真实环境拒绝。`kernel/turn.py` 是**真正的调用点**，它必须传。

    全仓只有 `turn.py` 一处生产调用；测试里不传是为了单独测语法形状。
    """
    environment = env if env is not None else _ENV_PERMISSIVE
    raw = text or ""

    outcomes, directive = _collect(raw, environment)

    # 正文清洗用**宽松式**：合法式没匹配上的坏候选（`[at:abc]`）也必须剥掉，
    # 否则说明书原样外发。剥掉之后原因已在 `_record_invalid_candidates` 里记过。
    stripped = strip_candidates(raw)
    cleaned = _clean_body(stripped)

    if not cleaned:
        # 只有标记、没有正文。
        #
        # ## 这里为什么仍然还原原文（与设计文档 5.2 的差别，刻意保留）
        #
        # 设计文档建议：`poke` 可作为纯动作请求执行而不发空消息；单独的
        # `at/quote` 不执行、不发空消息、记 `empty_body`。
        #
        # 本仓库现在**没有纯动作路径**：`turn.py` 的 `if delivered:` 决定
        # 是否落库一条 actor 消息，空的 `delivered` 会走"没有可见记录"分支，
        # 而 `bridge._deliver` 只按段发正文。要真正实现"只为 poke 发一次动作"，
        # 得改投递层（超出本任务作用域，见报告"没有做什么"）。
        #
        # 而**不**还原原文的代价是：用户收到一条**空消息**（或什么都收不到），
        # 那是更糟的失败。所以保持旧行为，但把它**显式标成 empty_body**：
        # `outcomes` 里每个候选都会被改写为 ignored，调用方可以据此知道
        # "这一轮其实什么都没执行"。
        marked = tuple(
            TagOutcome(o.key, o.arg, "ignored", "empty_body") for o in directive.outcomes
        )
        empty_directive = Directive(at=[], quote=None, poke=[], outcomes=marked)
        return raw, empty_directive

    invalid = _record_invalid_candidates(raw, environment)
    return cleaned, Directive(
        at=directive.at,
        quote=directive.quote,
        poke=directive.poke,
        outcomes=directive.outcomes + tuple(invalid),
    )


#: "最宽松"的解析环境：群聊、OneBot、引用表"非空但未给出具体 id"不可满足，
#: 因此不用 `visible_message_ids={}`（那表示"这轮一条 id 都没有"）。
#:
#: 单独放一个哨兵而不是在函数里现造：`validate_target` 对引用要查表，
#: 而"不传 env"的调用方期望的是**形状校验**，不是"引用永远不可用"。
_ENV_PERMISSIVE = TurnEnvironment(
    channel="onebot",
    is_group=True,
    allowed_targets=frozenset(),
)


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


def render_expression_help(
    *,
    is_group: bool,
    supports_poke: bool,
    channel: str = "onebot",
    supports_mention: bool | None = None,
    supports_quote: bool | None = None,
    has_message_ids: bool = True,
) -> str:
    """把**本轮真的可用**的表达方式告诉模型。

    只列当前渠道真的做到的——列了做不到的，她就会选一个做不出来的动作，
    然后"选择了却没发生"，比不给选项更糟。

    ## `supports_poke` 为什么是**必填关键字参数**，没有默认值

    这里曾经是 `supports_poke: bool = True`。那个默认值与需求**方向相反**：
    它等于"没探测过就宣称能拍一拍"，而事实是多数渠道拍不了。

    更糟的是它让"漏传"变成**静默退化成 True**：一个新增的调用点忘了传，
    代码照常运行、测试照常绿、用户看到的是"她说要拍你但没拍"。改成必填之后，
    漏传是 `TypeError`——**在第一次运行就炸**，而不是上线后以产品缺陷的形式
    出现。这就是设计文档第 3 节第 1 行那条缺陷的最后一环。

    ## 其余参数为什么带默认值

    `channel` / `supports_mention` / `supports_quote` / `has_message_ids` 是为
    统一目录新加的，默认值都取"**最保守**"方向（OneBot 群聊之外一切从简、
    未声明即按渠道推导）。这不会掩盖漏传：漏传时得到的是**更少**的动作
    宣告，而不是更多——少列最多让她少用一次，多列才会让她"选了却没发生"。
    """
    from ling.capabilities.tag_specs import TurnEnvironment, advertise, describe_inline

    # **`supports_poke` 是必填的**：调用方必须回答"这个渠道能不能拍一拍"。
    # 其余能力没给时按渠道推导（`ENV_CHANNEL_FEATURES`），给了就**显式**生效
    # （`channel_features` / `excluded_features`），推导不会把它们加回来。
    features: set[str] = set()
    excluded: set[str] = set()
    if supports_poke:
        features.add("poke")
    else:
        excluded.add("poke")
    if supports_mention is not None:
        (features if supports_mention else excluded).add("mention")
    if supports_quote is not None:
        (features if supports_quote else excluded).add("quote")

    env = TurnEnvironment(
        channel=channel,
        is_group=is_group,
        channel_features=frozenset(features),
        excluded_features=frozenset(excluded),
        # ## `has_message_ids` 的默认值为什么是 True
        #
        # 引用说明里那句"id 就是上下文里每条消息前面的 `[msg_id:…]`"在
        # **群聊**里是有意义的：上下文确实会给最近若干条入站消息标 id。
        # 默认 False 会让 `render_expression_help(is_group=True, supports_poke=True)`
        # 悄悄**少列引用**，而调用点并没有说"这轮没有 id"。
        #
        # 真正的判据在 `context.py`：它按 `len(reply_ids)` 传进来（采集过才为真）。
        # 这里传 `{"0": "0"}` 表示"采集过、有 id"，不是伪造一个可引用的 id——
        # 本函数只产出**说明文本**，不产出任何引用动作。
        visible_message_ids={} if not has_message_ids else {"0": "0"},
    )
    # `describe_actions` 同时产出**行内**与**独占整条**两类说明。本函数只负责
    # 行内那一类：延后的措辞要引用 `MAX_DELAY_SECONDS`、且必须独占整条，
    # 由调用点（`context.py`）按 `defer_available(env)` 单独注入。
    #
    # ## 这里**不能**按关键词过滤行
    #
    # 第一版写的是「把含"等会回"的行去掉」。延后那段有**两行**，第二行
    # （"不想等就别写它……这行标记不会发给对方。"）不含关键词，于是它作为
    # 一句**没有主语的孤句**留在了输出的末尾。`test_private_helper_never_
    # advertises_anything_unexecutable` 当场抓到。
    #
    # 现在按目录里的 `placement` 取子集，而不是对成品文本做文本处理——
    # 结构判断不会随措辞变化而失效。
    from ling.capabilities.tag_specs import TAG_SPECS as _ALL_SPECS

    inline_keys = {spec.key for spec in _ALL_SPECS if spec.placement == "inline"}
    usable_inline = [spec for spec in advertise(env) if spec.key in inline_keys]
    if not usable_inline:
        return ""
    # 只把"可用的行内动作"交回去重渲染，避免上面那类文本处理。
    return "\n".join(describe_inline(usable_inline)).strip()


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
