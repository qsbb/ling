r"""能力标签目录：**四组动作的语法、参数校验、可用性与说明的唯一出处**。

依据 `docs/design/CAPABILITY-TAG-SYSTEM.md` 第 2a 节（统一能力目录）与
第 5 节（标签语法与抗误触发）。

## 这个模块解决的是什么

修这个模块之前，"她能用哪些动作"这件事在**至少六个地方**各写了一遍：

| 关注点 | 以前写在哪 |
|---|---|
| 语法正则 | `expression_choice._MARKER` |
| 参数合法形状 | 同上（其实没有——任何 1-40 个非空白字符都算） |
| 给模型的说明 | `expression_choice.render_expression_help`、`render_recall_help` |
| 引用说明（第二份） | `context.py` 里另一段无条件的 `[quote:…]` 话术 |
| 延后说明 | `context.py` 里 `if not is_group` 那一段 |
| 什么时候可用 | 各处隐式假设（群/私聊、渠道、`supports_poke`） |

**它们已经漂移了**，而且漂移的方式是"提示词自相矛盾"：表达说明在私聊
不列引用，`context.py` 另一段却无条件教她写 `[quote:…]`；任意私聊都收到
延后说明，而 `domains/deferred/service.SUPPORTED_CHANNELS` 只放行
`web` / 空渠道——外部私聊选了延后会被拒登记。

本模块把这些**收成一份数据**：一条 `TagSpec` 同时回答"怎么写""参数什么形状"
"什么条件下才出现""怎么跟她解释""结果怎么分类"。

## 目录**不**做执行

动作仍由原领域执行器完成，这里不搬实现：

- `[at:…]` / `[quote:…]` / `[poke:…]` → `expression_choice.parse_expression()`
- `<<等会回 …>>` → `domains/deferred/markers.parse_defer_marker()`

目录只提供**唯一的语法/参数/前置条件/结果契约**。这是设计文档第 2a 节
明确的分工（"目录登记的是动作协议，不是把业务实现搬进一个大文件"）。

## 为什么合法式和候选式是**两个**正则

一把正则同时干"识别候选"和"认定合法"两件事，就是这一组缺陷的共同根因：

- 旧式 ``\[(at|quote|poke)\s*:\s*([^\]\s]{1,40})\]`` 对 `[at:abc]` 判定"是动作"，
  于是 `abc` 被当成 QQ 号剥掉、传给 OneBot 的 `at.qq` 字段（`[at:abc] 你好`
  → 正文只剩"你好"，动作列表里多了一个 `abc`）。
- 同时它对 `[AT:123]`（大小写）、`[at:12 34]`（空格）判定"不是动作"，
  **原样留在正文里**——说明书就这样发给了用户。

两个方向都是错的，而它们出自**同一行正则**：它既太宽（参数）又太窄（写法）。

所以拆成两个：

- `pattern`：**合法式**，参数形状严格（`[at:([1-9][0-9]{0,19})]`）。命中才执行。
- `candidate_pattern`：**宽松候选式**，只用来"发现像动作但不合法的文本"，
  **绝不据此执行**，只用来清洗 + 记原因（`invalid_syntax` / `invalid_case`）。

## 为什么 noqa: D 这么多

本项目的注释密度是刻意的（见 AGENTS.md「观测和归因要分开报」）——每条不显然的
取舍都要写清"为什么"，否则下一个改它的人只会看到一行正则，然后放宽它。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from re import Pattern
from typing import Literal

__all__ = [
    "CANDIDATE_AT",
    "CANDIDATE_DEFER",
    "CANDIDATE_POKE",
    "CANDIDATE_QUOTE",
    "ENV_CHANNEL_FEATURES",
    "MAX_TARGETS",
    "REASON_CODES",
    "TAG_SPECS",
    "TagOutcome",
    "TagSpec",
    "TurnEnvironment",
    "advertise",
    "by_key",
    "defer_available",
    "describe_actions",
    "describe_inline",
    "quote_available",
    "reachable_platform_ids",
    "spec_for_channel_feature",
    "strip_candidates",
]

#: 单条回复里 @ / 拍一拍各自的个数上限。
#:
#: 与 `expression_choice.MAX_TARGETS` 是**同一个数**（那边从这里取），
#: 不许各写一份：两处各写一个 3 时，"提示词说最多 3 个、解析器只留 2 个"
#: 这种漂移不会有任何报错。
MAX_TARGETS = 3

#: 原因码（稳定字符串，会进行为日志 / 运行日志）。
#:
#: 设计文档 5.4 节列出的那一组。**每个"没有执行"的选择都必须留下其中一个**，
#: 因为旧实现最大的问题不是"没执行"，而是"没执行的选项目从正文里消失了，
#: 而且没有任何痕迹"——模型写了四个 @，只有三个生效，第四个无声无息。
REASON_CODES: tuple[str, ...] = (
    "invalid_syntax",  # 像标签，但参数形状不合法（如 [at:abc]）
    "invalid_case",  # 关键字大小写不对（如 [AT:123]）——固定小写，不启用 IGNORECASE
    "invalid_target",  # 形状合法，但不在本轮允许的目标里（如 [quote:3] 无此 id）
    "unavailable",  # 这个动作在本轮环境里根本不可用（渠道/会话类型不支持）
    "duplicate",  # 同一个目标写了两次
    "limit_exceeded",  # 超过 MAX_TARGETS
    "empty_body",  # 只有标记、没有正文
    "incomplete_marker",  # 末尾半截标记（如 "…[quote:"）
    "escaped_literal",  # 反斜杠转义，按正文处理
)

# ---------------------------------------------------------------- 语法


#: `[at:QQ号]` —— **合法式**。
#:
#: `[1-9][0-9]{0,19}` 而不是 `\d{1,20}`：`\d` 在 Python 里匹配**任何 Unicode
#: 数字**（包括全角 `１２３`），而平台的 QQ 号字段只认 ASCII。1–20 位、首位
#: 非零是 QQ 号的实际形状（0 开头不可能是 QQ 号）。
#:
#: 注意它**只是形状校验**：`[at:100200300]` 语法合法，但不证明这个人是本轮
#: 真的在群里。目标白名单是 `TurnEnvironment.allowed_targets` 的职责。
CANDIDATE_AT = re.compile(r"\[at:(?P<qq>[1-9][0-9]{0,19})\]")

#: `[poke:QQ号]` —— 同上。
CANDIDATE_POKE = re.compile(r"\[poke:(?P<qq>[1-9][0-9]{0,19})\]")

#: `[quote:消息id]` —— `message_id_token()` 的两种产出：短 id 原样（数字），
#: 长 id 是 `m` + 6 位小写 base32（见 `kernel/context.py::message_id_token`）。
#:
#: 为什么**不是**"任意 1-40 字符"：旧式让 `[quote:消息 id]` 这种明显是说明书
#: 的文本也通过，然后拿 `消息 id` 去查 `reply_ids` 必然查不到，被静默降级。
CANDIDATE_QUOTE = re.compile(r"\[quote:(?P<id>[0-9]{1,20}|m[a-z2-7]{6})\]")

#: `<<等会回 90>>` —— 延后。**整条回复只能有它**（见 `placement`）。
#:
#: 这里只做"是不是一条合乎形状的延后标记"，时长本身的合法性仍交给
#: `domains/deferred/markers.parse_duration_seconds()`：``2分`` / ``90秒`` /
#: ``1分30秒`` 的写法有无数种，把它们写进目录的正则会让每加一种写法都要改
#: 这里——而这正是"语法有两份出处"要消灭的东西。
CANDIDATE_DEFER = re.compile(r"<<[ \t]*等会回[ \t]+(?P<duration>[^<>\n]*?)[ \t]*>>")

#: **宽松候选式**：只用来"发现像动作但不合法的文本"，绝不据此执行。
#:
#: 它故意比合法式宽——要的就是把 `[AT:123]`、`[at:abc]`、`[at:12 34]`、
#: `[quote:消息 id]` 这些都**认出来**（认出来才能清洗 + 记原因），而不是
#: 让它们作为正文漏给用户。`IGNORECASE` 只在**这一把**上启用：合法式绝不能
#: 大小写不敏感，否则 `[AT:123]` 会被当成真动作执行。
_JUNK = re.compile(r"\[(?P<kind>at|quote|poke)[ \t]*:[^\]\n]{0,80}\]", re.IGNORECASE)


# ---------------------------------------------------------------- 环境快照


@dataclass(frozen=True)
class TurnEnvironment:
    """**这一轮**能做什么。每轮采集一次，供"告知"与"执行前校验"共用同一份。

    ## 为什么必须是快照而不是现场查

    提示词在轮次开始时生成，解析与执行在轮次结束时发生。中间隔了几十秒——
    期间连接器可能断了、她可能已经把那条消息撤回了。**现场重查会给出与
    提示词不一致的答案**，那种不一致不会有任何报错，只会表现为"提示词说能
    引用，结果引用被丢"。

    所以：本快照负责"提示生成当时知道什么"，**执行前仍要按同一份快照二次校验**
    （`available()` + `validate_target()`），而不是无条件执行。

    ## 字段为什么这么少

    设计文档第 2a 节的示意是完整形态（含 `connector_instance`、`relationship`、
    `recallable_refs`、`unread_video_refs`）。这里只收**当前四组动作真的用到**的
    那几个：多一个没人读的字段，就是一处"看着像在保护我们、其实永远不执行"的
    假防线（AGENTS.md 记过同一类问题）。撤回与看视频的目录化是后续工作，
    它们的字段在**接入时**再加，不提前占位。
    """

    #: 渠道标识，与 `Window.channel` 同一来源（`web` / `onebot` / `qqbot` / ``""``）。
    #:
    #: ``""`` 与 ``web`` 都表示"没有外部渠道"（落库即送达），见
    #: `domains/deferred/service.SUPPORTED_CHANNELS`。
    channel: str = ""
    #: 私聊 / 群聊。
    is_group: bool = False
    #: **显式排除**的能力（优先于本条消息以下的任何推导）。
    #:
    #: 没有它的话，"调用方说 `supports_poke=False`"会被渠道推导（onebot →
    #: 有 poke）**加回来**，于是关闭拍一拍的实例照样收到 `[poke:…]` 说明。
    #: 这正是 `render_expression_help(supports_poke=False)` 第一版的行为：
    #: 参数传了、也被读到了，然后被推导覆盖——**一个不会报错的失效**。
    excluded_features: frozenset[str] = field(default_factory=frozenset)
    #: 本渠道经连接器**显式声明**的能力位。
    #:
    #: 只放当前真的有人读的那几个。`supports_poke` 来自
    #: `contracts.v1.CapabilityDescriptor.supports_poke`（默认 False，
    #: "未知即不可用"）；`supports_mention` / `supports_quote` 目前**还不存在**
    #: 于 `CapabilityDescriptor`——它们的推导规则见 `ENV_CHANNEL_FEATURES`。
    channel_features: frozenset[str] = field(default_factory=frozenset)
    #: 本轮真的渲染给她的消息 id（`{token: 原始平台 id}`）。`quote` 只能引这些。
    #:
    #: **三态**：``None`` = 没有采集（只做形状校验）；``{}`` = 采集过、一条都没有
    #: （一切引用都拒）；非空 = 只认这些。见 `_validate_quote`。
    visible_message_ids: Mapping[str, str] | None = None
    #: 本轮真实可指向的账号（QQ 号）。为空 = **不校验**（见下列说明）。
    allowed_targets: frozenset[str] = field(default_factory=frozenset)


#: 「缺少 `supports_mention` / `supports_quote` 时，用哪些既有事实推导」。
#:
#: ## 这不是能力位，是一处**显式的妥协**，必须写清楚
#:
#: `CapabilityDescriptor` 当前只有 `supports_poke` / `supports_recall`，**没有**
#: @ 和引用的能力位（设计文档第 2b 节要求新增）。加字段要改
#: `contracts/v1.py` 与四个连接器，不在本次改动的作用域内。
#:
#: 而"不宣告 @/引用"与"宣告了却做不到"哪个更糟？——**后者**（设计文档
#: 反复强调的"选了却没发生"）。但在缺字段时**一律不宣告**又会让所有渠道都失去
#: 这两个动作，包括确实支持的 OneBot。
#:
#: 所以这里按**渠道的已知实现**推导，并且：
#:   1. 只有明确实现了 @/引用的渠道（onebot / qqbot）才推导为 True；
#:   2. 其余一律 False（未知即不可用，与 `supports_poke` 同一条原则）；
#:   3. 连接器将来**显式声明**了字段时，字段优先——推导只为过渡，不是权威。
#:
#: 这一处是**推测**（按渠道名而不是按实例能力），不是实测。所以它的每一条
#: 推导都必须能被"加字段"直接取代，且 `test_tag_specs.py` 里有一条测试把
#: "有字段时字段优先"钉住，防止推导悄悄变成权威。
ENV_CHANNEL_FEATURES: Mapping[str, frozenset[str]] = {
    "onebot": frozenset({"mention", "quote", "poke"}),
    "qqbot": frozenset({"mention", "quote"}),
    "qq": frozenset({"mention"}),
    "wechat": frozenset(),
    "web": frozenset(),
    "": frozenset(),
}

#: "渠道特性名 → `CapabilityDescriptor` 上对应的字段名"。
#:
#: 存在的**唯一原因**是让"字段优先于推导"这条规则集中在一处，而不是在
#: 每个 `available()` 里各写一遍 `hasattr`（那种写法会让某一条遗忘 unwrap，
#: 而症状是"某个渠道悄悄用上了推导值"——静默的）。
_FEATURE_FIELDS: Mapping[str, str] = {
    "mention": "supports_mention",
    "quote": "supports_quote",
    "poke": "supports_poke",
}


# ---------------------------------------------------------------- 结果契约


@dataclass(frozen=True)
class TagOutcome:
    """一个候选的结果。**"标记消失"不等于"执行成功"**——这里把二者分开。

    旧实现里判断成功的方式是"正文里还有没有这个标记"，而那件事同时表示
    四种完全不同的情况：真执行了、参数不合法被剥、被重复/超限丢弃、
    以及**根本不可用**。用户看到的都是一样的消息，日志里什么都没有。

    `kind` 取值（与设计文档 2a 的 `result_semantics` 对齐）：

    - ``applied``：作为动作请求交给执行器（**仍不等于平台成功**，平台回执另算）
    - ``ignored``：没执行，`reason` 说明为什么
    """

    key: str
    arg: str
    kind: Literal["applied", "ignored"]
    reason: str = ""

    @property
    def applied(self) -> bool:
        return self.kind == "applied"


# ---------------------------------------------------------------- 规格


#: 放在哪：行内（`[at:…] 你说得对`）、还是必须独占整条（延后）。
Placement = Literal["inline", "only_line"]


@dataclass(frozen=True)
class TagSpec:
    """一条动作协议。**语法 / 参数 / 前置条件 / 说明的唯一出处。**

    字段比设计文档第 2a 节的示意少——只保留当前四组动作真的会读的那些。
    缺的是 `model_help` 的模板化、`user_result` 文案表、`requires_confirmation`、
    `tool_id`：撤回与工具映射是后续工作（撤回现在走 `ToolRegistry`
    的确认门，见设计文档 2e）。提前加没人读的字段就是假防线。
    """

    #: 稳定标识。**不会**被当作日志里的自由文本：按它聚合计数。
    key: str
    #: 合法式，参数形状严格。命中才可能执行。
    pattern: Pattern[str]
    #: 宽松候选式，只用于"认出来 → 清洗 → 记原因"。**绝不据此执行。**
    candidate_pattern: Pattern[str]
    #: 给模型看的**唯一**模板，如 `[at:{qq}]`。
    syntax: str
    #: 语法里的参数名，用于原因码/日志（不放真实值）。
    arg_name: str
    #: 本动作在本轮环境里是否可用。
    available: Callable[[TurnEnvironment], bool]
    #: 参数是否指向**本轮真的存在**的目标。形状合法但目标不存在 → `invalid_target`。
    #:
    #: 接收 ``(arg, env)``，返回原因码（``""`` = 通过）。返回原因码而不是
    #: 布尔值，是因为"不在白名单里"与"这轮完全没有白名单"是两件事，而
    #: 它们的处理不同（前者拒绝，后者放行——见 `validate_target` 里的说明）。
    validate_target: Callable[[str, TurnEnvironment], str]
    #: 给模型的说明。**不是**一整段提示词：`describe_actions()` 负责拼装措辞，
    #: 这里只有"这个动作是什么、参数从哪来"的事实。
    model_help: str
    #: 本轮最多几次。超出的记为 `limit_exceeded`（**显式**，不再静默丢弃）。
    max_occurrences: int = 1
    #: 行内 or 独占整条。
    placement: Placement = "inline"
    #: 流式暂扣用的已知前缀（同一条规格驱动 `kernel/marker_stream.py`）。
    stream_prefixes: tuple[str, ...] = ()
    #: 只有这些渠道才可能可用。空 = 不按渠道限制。
    #:
    #: 这是**条件**，不是全部判据：还要同时满足 `available()` 里的会话类型等。
    channels: frozenset[str] | None = None

    def parse(self, text: str) -> list[str]:
        """按**合法式**取出全部参数（已过形状校验）。顺序保留。"""
        return [m.group(1) for m in self.pattern.finditer(text or "")]

    def find_candidates(self, text: str) -> list[str]:
        """按**宽松式**找出属于本动作的全部候选 span。只用于诊断/清洗对照。"""
        return [
            m.group(0)
            for m in self.candidate_pattern.finditer(text or "")
            if m.group("kind").lower() == self.arg_kind
        ]

    @property
    def arg_kind(self) -> str:
        """`[at:…]` 里的 `at`。从 `syntax` 取，**不另写第二份**。"""
        match = re.match(r"\[([a-z]+):", self.syntax)
        return match.group(1) if match else ""


# ---------------------------------------------------------------- 可用性


def _feature(env: TurnEnvironment, name: str) -> bool:
    """这个渠道有没有 `name` 这个能力。**显式声明优先，推导兜底。**

    优先级（**从上到下，不要调换**）：

    1. `channel_features` 里**显式**列出的（含"显式排除"——见 `_EXCLUDED`）；
    2. `CapabilityDescriptor` 上的同名字段（`supports_poke` 已有；
       `supports_mention` / `supports_quote` 还没有）；
    3. `ENV_CHANNEL_FEATURES` 按渠道名的推导。

    顺序不能反：连接器**显式声明**的事实必须压过按渠道名猜的推导，否则
    将来某个 OneBot 变体关掉 @ 时，推导会把它的声明悄悄盖掉。

    ``supports_mention`` / ``supports_quote`` 字段目前**不存在**
    （见 `ENV_CHANNEL_FEATURES` 的说明），所以现在 `getattr` 的默认值
    确实会被走��——这与 `ContextCompiler.supports_poke` 里那个"永不执行的
    getattr 默认值"不同，**这里是真兜底**。加字段之后它才会变成同一类问题，
    届时这条注释和 `ENV_CHANNEL_FEATURES` 要一起删掉。
    """
    # 1. 调用方显式说了算：列在 `channel_features` 里 = 有；列在
    #    `excluded_features` 里 = 没有。两者都出现时**排除优先**（fail closed）。
    if name in env.excluded_features:
        return False
    if name in env.channel_features:
        return True
    # 2. 连接器实例**显式声明**的能力位。
    declared = getattr(env, _FEATURE_FIELDS.get(name, ""), None)
    if isinstance(declared, bool):
        return declared
    # 3. 过渡期的按渠道推导（见 `ENV_CHANNEL_FEATURES` 的论证）。
    return name in ENV_CHANNEL_FEATURES.get(env.channel, frozenset())


def _private_or_group(env: TurnEnvironment) -> bool:
    """@ 只在**群聊**里给。**这一条只服务 `mention`，与引用无关。**

    依据：私聊里只有对方一个人，"回你"没有任何歧义，@ 她只会让平台多发一个
    多出来的通知。

    ## 引用为什么**不**在这里

    这里曾经还写着"引用在私聊里平台不支持（QQ 私聊没有引用段）"，并据此把
    引用也锁进群聊。**那句话是没有证据的断言，且与连接器实现矛盾**：

    - `connectors/onebot/connector.py::send()` 只在**选哪个 API**
      （`send_group_msg` / `send_private_msg`）上分群/私聊；
    - `reply_to` 的读取与 `build_outbound_segments(..., reply_to, ...)` 的
      传入**没有任何会话类型分支**，私聊照常构造引用段；
    - 真正的约束在那一层：`reply_to` 必须命中 `_known_message_set`（我们真的
      见过的入站 `message_id`），否则本地就去掉引用段、正文照发。

    产品事实（用户裁决 2026-11-11）：「onebot 支持引用」，私聊也支持。
    所以引用的可用性判据在 `quote_available()`，**不再经过这里**。
    """
    return env.is_group


def quote_available(env: TurnEnvironment) -> bool:
    """本轮能不能引用。**与群聊/私聊无关。**

    **两个条件同时成立**：渠道声明了引用能力、本轮真的渲染过消息 id。

    ## 「群聊」不是条件（这里原来错把它写成了第三个条件）

    旧实现要求 `is_group`，依据是一句"QQ 私聊没有引用段"——**没有证据，
    而且与连接器实现相反**。`connectors/onebot/connector.py::send()` 里
    `reply_to` 的读取与传入 `build_outbound_segments()` 都在群/私聊分支**之外**：
    分支只决定 `send_group_msg` / `send_private_msg`，引用段的构造完全共用。

    真正的约束也不在会话类型上，而在**目标 id 的来源**：

    - 连接器有一份"见过的入站 message_id"白名单（`_known_message_set`），
      `reply_to` 不在其中时**只丢引用段**、正文照发（`dropped_reply`）；
    - 解析侧同样只认本轮真的渲染过的 id（`_validate_quote` → `invalid_target`）。

    两处判据都是「这个 id 我们真的见过」，**不是**「这个窗口是不是群」。
    所以本函数只问那件事：本轮有没有可见 id。

    ## 第二个条件为什么最容易被漏掉

    它的缺法最隐蔽：没有 id 的时候照样告诉她"可以引用"，她只能瞎编
    （线上见到过 `[quote:3]`），然后被 `reply_ids` 校验丢掉——正文照发，
    看起来"什么都没发生"。
    """
    # ``None``（未采集）时**不宣告**引用：提示词里说"只能用上面出现过的 id"，
    # 而没有 id 时那句话她无从遵守，只会编一个。宣告靠采集，不靠形状。
    return _feature(env, "quote") and bool(env.visible_message_ids)


def _mention_available(env: TurnEnvironment) -> bool:
    """@ 需要：群聊 + 本渠道声明了 mention 能力。"""
    return _private_or_group(env) and _feature(env, "mention")


def _poke_available(env: TurnEnvironment) -> bool:
    """拍一拍需要：本渠道**显式**声明 `supports_poke`。

    **不看群/私聊**：OneBot 私聊也能拍一拍。旧实现只把拍一拍挂在
    `supports_poke` 上、不区分会话类型，这里保持不变——收紧成"只群聊"
    会让私聊里现有的拍一拍消失，那是行为变更，不属于本任务。
    """
    return _feature(env, "poke")


def defer_available(env: TurnEnvironment) -> bool:
    """本轮能不能延后。**与登记闸同一判据。**

    ## 这条判据以前在两处各写了一份，而且不一致

    `context.py` 只写 `if not is_group:` 就给她延后选项；真正登记的地方
    （`kernel/turn.py::_schedule_deferred` 与 `worker.py` 的外发闸）却要求
    `channel in deferred.SUPPORTED_CHANNELS`（只有 `web` / ``""``）。

    结果：**任意外部私聊（QQ / OneBot / 微信）都会收到延后说明**，而她真的
    选了之后被拒登记，按立即回复处理——用户看到的是"她说要晚点回，结果
    立刻回了"，而且日志里只有一条 info。

    现在两处都问这里。渠道白名单本身仍由 `domains/deferred/service` 维护
    （那是那个域的事实），这里只是**读**它，不复制一份。

    ## 为什么不能只看 `is_group`

    群聊不在白名单里（外发闸管不到群渠道的投递），所以群聊即使将来被加进
    白名单也要单独评估。这里用"白名单"而不是"not is_group"作为判据，正是
    为了让两处不可能再漂移。
    """
    if env.is_group:
        return False
    from ling.domains.deferred.service import SUPPORTED_CHANNELS

    return env.channel in SUPPORTED_CHANNELS


# ---------------------------------------------------------------- 目标校验


def reachable_platform_ids(env: TurnEnvironment) -> frozenset[str]:
    """本轮她**能引用**的全部 id（token 形式）。

    `quote` 的参数是 `[msg_id:…]` 里那个 token，不是原始平台 id——
    写原始 id 时 `reply_ids` 里查不到（那是另一套命名空间）。

    ## 为什么这里 `m` 前缀的长 id 也要收

    `message_id_token()` 对**不超过 20 个字符**的平台 id **原样保留**（NapCat
    的数字短 id 就是这样）。那么 qqbot 那种 `ROBOT1.0_…` 会变成 `m…`，
    而某些渠道若存在 <=20 字符的**非数字**短 id，它会原样进 `reply_ids`，
    我们的 `CANDIDATE_QUOTE` 却只认"数字"或"`m`+6 位 base32"——那种 id
    会被判 `invalid_syntax`。

    设计文档 5.3 明确点出了这个风险，并给了两条出路：改 token 生成，或
    "用本轮 ID 集合动态编译 `re.escape` 白名单"。**这里选后者**（不动
    `context.py` 的 token 生成，那会影响已有会话的 id 映射）：先把本轮
    真实存在的 id 认定为合法，静态式只是形状兜底。
    """
    return frozenset(str(k) for k in (env.visible_message_ids or {}))


def _validate_quote(arg: str, env: TurnEnvironment) -> str:
    """引用的 id 必须在本轮**真的渲染过**的 id 里。

    ## ``visible_message_ids is None`` 与 ``{}`` 的区别（三态，不许合并）

    设计文档 5.4 的反例表里有一条 ``[quote:3]``（本轮无 id ``3``）→ 不执行、
    记 ``invalid_target``。那条要求调用方**真的采集过** id 表。

    - ``None``：**没有采集**（调用方还没接上，或这是个只管语法形状的调用）→
      只做形状校验，放行。收紧它会让所有没接线的调用点上的引用全部失效，
      而那种失效是静默的（正文照发、引用消失）。
    - ``{}``：采集过，**这一轮一条 id 都没有** → 全部拒（``invalid_target``）。

    这两态合并成一个之后，"没接线"与"确实没有可引用消息"会给出同一个结果，
    而它们该给的结果恰好相反。
    """
    if env.visible_message_ids is None:
        return ""
    if not env.visible_message_ids:
        return "invalid_target"
    if arg in reachable_platform_ids(env):
        return ""
    return "invalid_target"


def _validate_at(arg: str, env: TurnEnvironment) -> str:
    """@ 的目标必须是**本轮真的可指向**的账号。

    ## 为什么 `allowed_targets` 为空时**放行**

    空集合有两种含义，而它们必须区分开：

    - "这一轮采集到了白名单，里面恰好没人" → 应该**全拒**；
    - "这个调用方还没接上白名单采集" → 收紧会让 @ 在所有渠道上**全部失效**。

    现在全仓**没有任何地方**生产 `allowed_targets`（采集它要做群成员列表
    查询，那是另一次改造）。所以此时放行，并且 `TurnEnvironment` 用
    ``frozenset()`` 而不是 ``None``——等接线做了，改为 ``None`` 表示"没采集"，
    空集合表示"采集到了但没人"，两态就分开了。

    **这是明确的妥协，不是"已经安全"**：在没有采集的今天，`[at:100200300]`
    形状合法就会进动作列表，平台侧 @ 一个不在群里的人会失败或静默无效。
    设计文档 5.3 要求的对象白名单**尚未落地**，这一点必须如实写在报告里。
    """
    if not env.allowed_targets:
        return ""
    if arg in env.allowed_targets:
        return ""
    return "invalid_target"


def _validate_poke(arg: str, env: TurnEnvironment) -> str:
    """同 @：拍一拍也只能指向本轮认识的账号。理由同 `_validate_at`。"""
    return _validate_at(arg, env)


# ---------------------------------------------------------------- 目录


TAG_SPECS: tuple[TagSpec, ...] = (
    TagSpec(
        key="message.mention",
        pattern=CANDIDATE_AT,
        candidate_pattern=_JUNK,
        syntax="[at:{qq}]",
        arg_name="qq",
        available=_mention_available,
        validate_target=_validate_at,
        model_help=(
            "点一下对方。群里好几个人在说话时，用它能明确你是在回谁——"
            "不是为了礼貌，是为了让人看得懂。QQ 号只能取本轮给出的成员号。"
        ),
        max_occurrences=MAX_TARGETS,
        placement="inline",
        stream_prefixes=("[a", "[at", "[at:"),
    ),
    TagSpec(
        key="message.quote",
        pattern=CANDIDATE_QUOTE,
        candidate_pattern=_JUNK,
        syntax="[quote:{message_id}]",
        arg_name="message_id",
        available=quote_available,
        validate_target=_validate_quote,
        model_help=(
            "引用某条消息。id 就是上下文里每条消息前面的 `[msg_id:…]`——"
            "只能用上面真的出现过的 id，编一个会被忽略。"
            "适合回一条较早的、或者话题已经翻过去了的消息。"
        ),
        max_occurrences=1,
        placement="inline",
        stream_prefixes=("[q", "[qu", "[quo", "[quot", "[quote", "[quote:"),
    ),
    TagSpec(
        key="message.poke",
        pattern=CANDIDATE_POKE,
        candidate_pattern=_JUNK,
        syntax="[poke:{qq}]",
        arg_name="qq",
        available=_poke_available,
        validate_target=_validate_poke,
        model_help=(
            "拍一拍。不打断地提醒一下、或者表示亲近，比发一句「在吗」轻。"
        ),
        max_occurrences=MAX_TARGETS,
        placement="inline",
        stream_prefixes=("[p", "[po", "[pok", "[poke", "[poke:"),
    ),
    TagSpec(
        key="reply.defer",
        pattern=CANDIDATE_DEFER,
        candidate_pattern=CANDIDATE_DEFER,
        syntax="<<等会回 {seconds}>>",
        arg_name="seconds",
        available=defer_available,
        validate_target=lambda _arg, _env: "",
        model_help=(
            "写了这行，这一轮就不会立刻发出去，到点你会重新看到这个对话再回。"
        ),
        max_occurrences=1,
        # **独占整条**：延后的语义是"我现在不回"，标记旁边有正文就是坏格式
        # （`markers.BAD_FORMAT_BODY_PRESENT`），否则会让她辛苦生成的正文凭空消失。
        placement="only_line",
        stream_prefixes=("<<", "<<等", "<<等会", "<<等会回"),
    ),
)


_BY_KEY: Mapping[str, TagSpec] = {spec.key: spec for spec in TAG_SPECS}
_BY_FEATURE: Mapping[str, str] = {
    "mention": "message.mention",
    "quote": "message.quote",
    "poke": "message.poke",
    "defer": "reply.defer",
}


def by_key(key: str) -> TagSpec:
    """按键取规格。键不存在 → `KeyError`（不是静默返回 None）。"""
    return _BY_KEY[key]


def spec_for_channel_feature(name: str) -> TagSpec:
    """按渠道特性名取规格（`delivery.py` 的闸用它）。"""
    return _BY_KEY[_BY_FEATURE[name]]


def advertise(env: TurnEnvironment) -> list[TagSpec]:
    """本轮**真正可执行**的动作（顺序稳定，便于提示词快照比对）。

    这是"只列本轮真的可用的动作"的唯一实现：`describe_actions()` 用它筛选，
    `turn.py` 的最终解析也用它判定 `unavailable`。**两处不许各筛一遍**——
    那正是本次要修的那类漂移。
    """
    return [spec for spec in TAG_SPECS if spec.available(env)]


# ---------------------------------------------------------------- 渲染


def _syntax_of(spec: TagSpec) -> str:
    """把模板里的参数换成给模型看的写法。

    `[at:{qq}]` → ``[at:QQ号]``。**不另写一份示例**：说明里的语法必须与
    解析器认的语法是同一个字符串，否则"教她写 A、只认 B"会再次发生。
    """
    labels = {
        "qq": "QQ号",
        "message_id": "消息 id",
        "seconds": "秒数",
    }
    return spec.syntax.format(**{spec.arg_name: labels.get(spec.arg_name, spec.arg_name)})


def _spec_keyword(spec: TagSpec) -> str:
    """动作关键字（`[at:…]` → ``at``）。**从 syntax 取**，不另写第二份。"""
    return spec.syntax[1 : spec.syntax.index(":")] if ":" in spec.syntax else spec.syntax


_OPENING = {
    "message.mention": "- `[at:QQ号]`：",
    "message.quote": "- `[quote:消息 id]`：",
    "message.poke": "- `[poke:QQ号]`：",
}


def describe_inline(specs: list[TagSpec]) -> list[str]:
    """**行内**动作的说明段落。`describe_actions` 与 `render_expression_help` 共用。

    ## 为什么单独抽出来

    `render_expression_help` 只想要行内那一段（延后由 `context.py` 单独注入，
    因为它要引用 `MAX_DELAY_SECONDS` 且必须独占整条）。第一版是对
    `describe_actions()` 的**成品文本按关键词过滤行**——延后那段有两行，
    第二行不含关键词，于是它作为一句没有主语的孤句留在了末尾。

    **对成品文本做文本处理，等于把措辞当成了协议。** 抽成按 `placement`
    取子集的函数之后，措辞怎么改都不会让这段逻辑失效。
    """
    lines: list[str] = []
    if not specs:
        return lines
    lines.append("你可以选择怎么表达（不选就只发文字，那是最常见的）：")
    for spec in specs:
        opening = _OPENING.get(spec.key, f"- `{_syntax_of(spec)}`：")
        lines.append(f"{opening}{spec.model_help}")
    capped = [s for s in specs if s.max_occurrences > 1]
    if capped:
        caps = "、".join(
            f"`{_spec_keyword(s)}` 最多 {s.max_occurrences} 个" for s in capped
        )
        lines.append(f"{caps}，多写的不执行。")
    lines.append(
        "标记写在正文里会被自动执行并从发出的话里去掉，"
        "所以不要把标记当成给用户看的内容。不想用就完全不用。"
    )
    return lines


def describe_actions(env: TurnEnvironment) -> str:
    """把**本轮可用**的动作说给她听。唯一渲染入口。

    ## 与旧 `render_expression_help` 的两点区别

    1. **引用说明只有一处**。旧实现里 `expression_choice` 渲染一段（群聊才有）、
       `context.py` 又无条件追加一段 `[quote:…]`——私聊里两段互相矛盾。
       现在引用只由这里渲染一次（`quote_available`），"矛盾"从结构上消失。
       **注意**：私聊现在**也会**列引用（OneBot 私聊支持引用，见
       `quote_available` 的说明）；它是否出现只看本轮有没有可见的 message id，
       与会话类型无关。
    2. **参数个数上限也写进来**：`MAX_TARGETS` 只在解析器里生效时，她写四个
       @ 是完全合理的（没人告诉过她最多三个）。上限必须与解析器同源。

    无可执行动作时返回空串（**不输出一句空的"你可以选择怎么表达"**——
    那句在什么都没有的时候只会让她以为漏掉了什么）。
    """
    usable = advertise(env)
    inline = [s for s in usable if s.placement == "inline"]
    only_line = [s for s in usable if s.placement == "only_line"]

    lines: list[str] = []
    if inline:
        lines.extend(describe_inline(inline))

    for spec in only_line:
        # 独占整条的动作**不与行内动作并列在同一段**：混在一起会让她以为
        # 可以"@ 某人 + 延后"同时发生，而延后独占时行内标记毫无意义。
        #
        # 措辞里的数字全部**从规格与那个域的常量取**，不在这里抄：上限写死
        # 在提示词里、而钳制常量改小时，用户会看到"她等满 300 秒被截断"——
        # 正是 `deferred/service.py` 论证过的"把系统上限说成她的选择"。
        example = 90
        lines.append(
            "如果这一轮你现在不想马上回，可以只写一行 "
            f"`{spec.syntax.format(**{spec.arg_name: example})}`"
            f"（把 {example} 换成你想等的秒数，最长 {_max_delay_seconds()} 秒），"
            "然后**不要再写别的正文**。" + spec.model_help
        )
        lines.append("不想等就别写它——绝大多数时候直接回就好。这行标记不会发给对方。")
    return "\n".join(line for line in lines if line)


def _max_delay_seconds() -> int:
    """延后上限。**读那个域的常量**，不在这里抄一个 300。"""
    from ling.domains.deferred.state import MAX_DELAY_SECONDS

    return int(MAX_DELAY_SECONDS)


# ---------------------------------------------------------------- 清洗


def strip_candidates(text: str, *, specs: Iterable[TagSpec] | None = None) -> str:
    """剥掉**全部已知形状的候选标记**（不管是否合法、是否可用）。

    这是**出口清洗**，不是解析：它的任务只有一个——别让一个方括号说明书
    原样发到用户面前（`[at:abc] 你好` 的正文不该是 `[at:abc] 你好`）。

    ## 为什么清洗用宽松式而执行用合法式

    正是设计文档 5.3 的"坏格式处置"：不合法 ≠ 当正文发出去。`[at:abc]`
    既不执行、也不该原样外发，而是**剥掉 + 记原因**。

    ## 与 `parse_expression` 的关系

    `parse_expression` 调它做最后一列兜底（见那里的说明），所以这里的
    "剥掉"与"执行"的判定是同一轮里同一份原文上的两次扫描，不串行改写。
    """
    chosen = tuple(specs) if specs is not None else TAG_SPECS
    # 一次扫完全部方括号标记，按命中的动作名决定剥不剥。**不要**每个 spec
    # 各扫一遍：那会把同一个 span 判 N 次（且每遍都要判断该不该剥）。
    kinds = {
        spec.arg_kind
        for spec in chosen
        # 延后的清洗归 `markers.strip_defer_markers`（它有自己的"剥空后正文
        # 照发"语义）。目录只提供形状，不改它的清洗。
        if spec.key != "reply.defer"
    }
    return _JUNK.sub(
        lambda m: "" if m.group("kind").lower() in kinds else m.group(0),
        text or "",
    )
