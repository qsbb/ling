"""提示词分区注册表：**问分区、问预算、问牺牲顺序，都只问这里**。

## 这个模块解决什么问题

编译器原先有三份互相独立的"车道清单"：`DEFAULT_BUDGETS` 写预算、
`SACRIFICE_ORDER` 写超限牺牲次序、`PROTECTED_LANES` 写谁不许丢，
而 `LANE_TO_KEY` 又写第四份展示分组。四份清单靠人手保持同步，
于是有了 `src/ling/kernel/context.py:1804` 那段注释记载的真实故障：
车道名写错成 `scene`（实际叫 `scenario`）不会报错，只会让场景提示
**永远不被整条牺牲**——一直占着预算，直到撞硬上限被从中间截一半。

本模块把"有哪些区域、每个区域有哪些车道、谁先死"变成**单一数据源**，
其余四份清单全部从它派生。改分区只改 `ZONES`，别处不许再写一份。

## 区域与车道是两个层级

- **区域（`ZoneSpec`）** 是语义与治理单元：它回答什么问题、
  在最终 `messages` 里排第几、超软上限时整区排第几让位。
- **车道（`LaneSpec`）** 是单一来源与预算单元：谁产出的、
  它有多少 token、超限时区内谁先走、是否受保护。

用户的原话是「按区域划分，不然太乱了」——所以对外解释"为什么这次
回复少了东西"时讲的是**区域**，而预算落在**车道**上。

## 三种顺序，故意不一样

调用方最容易搞混的就是这点，这里一次说清：

| 顺序 | 字段 | 回答的问题 |
|---|---|---|
| 消息顺序 | `ZoneSpec.message_order` / `LaneSpec.message_order` | 模型**读**到的先后 |
| 区内牺牲顺序 | `LaneSpec.shed_order` | 区内超限先丢谁 |
| 区间牺牲顺序 | `ZoneSpec.shed_rank` | 跨区超限先丢哪个区 |

牺牲顺序按**保留价值**排，不是阅读先后。所以 `capabilities`
消息位置靠后（靠近正文），却**最先**被牺牲——渠道侧还能再确认一次，
而身份丢了整段回复就不再是她。

## 派生出来什么

`DEFAULT_BUDGETS`、`SACRIFICE_ORDER`、`PROTECTED_LANES`、`LANE_TO_KEY`
都是本表推导的产物，不是第二份权威。`validate_registry()` 在导入期
fail fast：表写错时**启动就崩**，而不是等某次超预算才表现成怪行为。

## 迁移状态：本轮是 PR 1

`docs/design/PROMPT-ZONE-SPEC.md` §6 把迁移分成 4 个 PR。**本模块是 PR 1**：
只登记、校验、暴露查询，**不切换发送顺序、不改预算行为**。

因此本表同时记录**目标顺序**（`message_order` 等字段）与**当前实际顺序**
（`OBSERVED_APPEND_ORDER`）。两者此刻**并不一致**，这不是 bug：

- 现在是"哪段代码先 `append`"决定的顺序，`self_state` 分散在 3 处、
  `short_memory` 2 处、`capabilities` 4 处，交错在 evidence/knowledge/media
  之间。设计文档 `:41` 自己写着「上述是**目标排布，不是当前发送顺序**」。
- PR 3 才会把发送顺序切到目标排布。届时 `tests/unit/test_prompt_zones.py`
  里针对 `OBSERVED_APPEND_ORDER` 的断言从"对照模式"删掉即可，
  而目标顺序的断言**当时就已经写好了**——差异表就是 PR 3 的验收清单。

## 将来可能挪位置（留给 PR 2 的决策点）

设计文档 `:246` 建议把注册表放在 `ling/contracts/prompt_zones.py`，
理由是"避免 `contracts.v1` 反向导入 `kernel.context`"。

本轮放在 `kernel/` 下，因为 PR 1 不需要契约层引用注册表。
**PR 2 若要让 `ContextContribution.lane` 接受注册表运行时校验，
应重新评估这个位置**：`ling.contracts` 是低依赖层，反向导入
`ling.kernel` 会形成坏依赖方向，届时应把本模块整体搬到
`ling/contracts/prompt_zones.py` 并让 `kernel` 反过来导入它。

把这个决策点写在这里，是为了让它被**显式处理**，而不是以后有人
发现"注册表在 kernel 下、契约却在另一个地方校验"时才发现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DEFAULT_BUDGETS",
    "LANES",
    "LANE_TO_KEY",
    "OBSERVED_APPEND_ORDER",
    "PROTECTED_LANES",
    "SACRIFICE_ORDER",
    "ZONES",
    "Gate",
    "Kind",
    "LaneSpec",
    "ZoneSpec",
    "budget_overruns",
    "lane_zone",
    "require_lane",
    "sacrifice_order",
    "validate_registry",
    "zone_message_order",
]


class Kind(StrEnum):
    """区域的**消息类型**。决定它进 `messages` 的哪个位置。"""

    SYSTEM = "system"
    """`ContextContribution` → 一条 system 消息。受软／硬总预算与牺牲表管。"""

    SUMMARY = "summary"
    """独立滚动摘要。有自己的长度策略，**不**参与 system 片段牺牲。"""

    HISTORY = "history"
    """user/assistant 原文。有独立历史窗口，按消息条数滚动。"""

    INPUT = "input"
    """本轮用户正文。**必须保留**——丢了就没有回复对象。"""


class Gate(StrEnum):
    """注入前置条件。

    编译器只按名字读 `CompileFacts` 上的同名布尔值，不认识就抛错。
    本轮只登记，**执行留给 PR 2**（见模块 docstring「迁移状态」）。
    """

    PERSONA_PRESENT = "persona_present"
    """人设为空时整段跳过——不许自动补默认角色。"""

    SCENARIO_OVERRIDE_PRESENT = "scenario_override_present"
    """当前私聊／群聊有用户写的场景覆盖才发。"""

    AUDIENCE_ALLOWED = "audience_allowed"
    POKE_SUPPORTED = "poke_supported"
    RECALLABLE = "recallable"
    PRIVATE_CHAT = "private_chat"


@dataclass(frozen=True)
class LaneSpec:
    """一条注入车道。**预算、位置、保护、门槛的唯一登记处。**"""

    name: str
    budget_tokens: int | None
    """名义 token 预算。结构车道（summary/history/input）用各自窗口策略，故为 None。"""

    message_order: int
    """区内发送位置。**不是** `append()` 的先后。"""

    shed_order: int | None
    """区内牺牲位置，越小越先丢。受保护或结构车道为 None。"""

    protected: bool = False
    """禁止**整条**牺牲。不代表忽略单片段上限。"""

    gates: frozenset[Gate] = frozenset()
    active: bool = True
    """False 表示禁发（迁移中的旧车道）。"""


@dataclass(frozen=True)
class ZoneSpec:
    """一个语义区域。对外解释"少了什么"时讲的是它。"""

    id: str
    label: str
    """给人看的中文名。日志和 UI 直接用这个，**别在前端再翻译一遍**。"""

    meaning: str
    """这个区域回答什么问题。写给排障的人看。"""

    kind: Kind
    message_order: int
    """最终 `messages` 里的位置。"""
    shed_rank: int | None
    """跨区牺牲次序，越小越先丢。结构性区域与本区全保护时为 None。"""
    legacy_ui_key: str
    """过渡期旧快照／API 的展示分组 key。PR 4 才换掉。"""
    lanes: tuple[LaneSpec, ...]


# --------------------------------------------------------------------------- #
# 唯一手写的车道清单
# --------------------------------------------------------------------------- #

ZONES: tuple[ZoneSpec, ...] = (
    ZoneSpec(
        "identity",
        "身份",
        "她是谁：用户设定的角色、边界与禁区",
        Kind.SYSTEM,
        10,
        None,  # 永不整区牺牲
        "system",
        (
            LaneSpec("persona", 100_000, 10, None, True, frozenset({Gate.PERSONA_PRESENT})),
            LaneSpec("constraints", 1_200, 20, None, True, frozenset({Gate.PERSONA_PRESENT})),
        ),
    ),
    ZoneSpec(
        "scene",
        "场景",
        "当前私聊／群聊适用的用户场景覆盖",
        Kind.SYSTEM,
        20,
        4,
        "system",
        (
            LaneSpec(
                "scenario",
                600,
                10,
                10,
                gates=frozenset({Gate.PERSONA_PRESENT, Gate.SCENARIO_OVERRIDE_PRESENT}),
            ),
            # 契约 `ContextContribution.lane` 里仍允许 `scene`，但编译器
            # 从没有过生产者，也没有预算。登记成禁发，让"它存在但不发"
            # 这件事可查；PR 2 再把"收到 scene 就报错"接上。
            LaneSpec("scene", None, 99, None, active=False),
        ),
    ),
    ZoneSpec(
        "situation",
        "当前处境",
        "她现在的状态、环境，以及与眼前这个人的关系",
        Kind.SYSTEM,
        30,
        5,
        "memory",
        (
            LaneSpec("world", 2_000, 10, 10),
            # 区内消息顺序 self_state → relationship，牺牲顺序却反过来：
            # 关系比此刻的心情更值得留。
            LaneSpec("self_state", 2_000, 20, 30),
            LaneSpec("relationship", 1_200, 30, 20),
        ),
    ),
    ZoneSpec(
        "memory",
        "可用背景",
        "她此轮可用的已知事实、检索结果与短时话题",
        Kind.SYSTEM,
        40,
        6,
        "memory",
        (
            LaneSpec("short_memory", 4_000, 10, None, True),
            LaneSpec("core_memory", 6_000, 20, 30),
            LaneSpec("evidence", 6_000, 30, 20),
            LaneSpec("knowledge", 3_000, 40, 10),
        ),
    ),
    ZoneSpec(
        "plan",
        "意图",
        "此轮可参考的目标。是打算做什么，不是已发生的事实",
        Kind.SYSTEM,
        50,
        2,
        "plan",
        (LaneSpec("goals", 800, 10, 10),),
    ),
    ZoneSpec(
        "media",
        "媒体",
        "已看见／待查看的图、音、视频证据与标记",
        Kind.SYSTEM,
        60,
        3,
        "media",
        (LaneSpec("media", 1_500, 10, 10),),
    ),
    ZoneSpec(
        "capabilities",
        "可执行能力",
        "当前渠道真正能做到的动作、标记与表达格式",
        Kind.SYSTEM,
        70,
        1,  # 最先牺牲：渠道侧还能再确认一次
        "system",
        (LaneSpec("capabilities", 300, 10, 10),),
    ),
    ZoneSpec(
        "recap",
        "早期摘要",
        "本窗口更早发生了什么（滚动生成，非原始消息）",
        Kind.SUMMARY,
        80,
        None,
        "summary",
        (LaneSpec("summary", 400, 10, None),),
    ),
    ZoneSpec(
        "dialogue",
        "近段对话",
        "双方最近实际说过什么（原始 user/assistant 消息）",
        Kind.HISTORY,
        90,
        None,
        "history",
        (LaneSpec("history", 20_000, 10, None),),
    ),
    ZoneSpec(
        "input",
        "本轮输入",
        "正在回复的原始用户正文",
        Kind.INPUT,
        100,
        None,
        "input",
        (LaneSpec("input", None, 10, None),),
    ),
)


# --------------------------------------------------------------------------- #
# 派生表：以下全部从 ZONES 读出，不许手写第二份
# --------------------------------------------------------------------------- #

LANES: dict[str, tuple[ZoneSpec, LaneSpec]] = {
    lane.name: (zone, lane) for zone in ZONES for lane in zone.lanes
}

DEFAULT_BUDGETS: dict[str, int] = {
    name: lane.budget_tokens
    for name, (zone, lane) in LANES.items()
    if zone.kind is Kind.SYSTEM and lane.active and lane.budget_tokens is not None
}

PROTECTED_LANES: frozenset[str] = frozenset(
    name for name, (_, lane) in LANES.items() if lane.active and lane.protected
)

LANE_TO_KEY: dict[str, str] = {
    name: zone.legacy_ui_key for name, (zone, lane) in LANES.items() if lane.active
}


def sacrifice_order() -> tuple[str, ...]:
    """**目标**整条牺牲顺序：先按区间 `shed_rank`，再按区内 `shed_order`。

    这是设计文档 §2 定义的目标次序，**尚未生效**（见模块 docstring
    「迁移状态」）。PR 3 切换到它之前，运行时用的是
    `SACRIFICE_ORDER_CURRENT`。

    必须**先过滤再排序**：`shed_rank=None` 的结构区与本区全保护的
    `identity` 不能进排序（`None` 与 `int` 相比会 `TypeError`）。
    """
    candidates = [
        (zone, lane)
        for zone in ZONES
        for lane in zone.lanes
        if zone.kind is Kind.SYSTEM
        and zone.shed_rank is not None
        and lane.active
        and not lane.protected
        and lane.shed_order is not None
    ]
    candidates.sort(
        key=lambda pair: (pair[0].shed_rank or 0, pair[1].shed_order or 0, pair[1].name)
    )
    return tuple(lane.name for _, lane in candidates)


SACRIFICE_ORDER_TARGET: tuple[str, ...] = sacrifice_order()
"""设计文档 §2 的目标牺牲次序：`capabilities → goals → media → scenario →
world → relationship → self_state → knowledge → evidence → core_memory`。

`tests/unit/test_prompt_zones.py` 钉住它，供 PR 3 验收。
"""


SACRIFICE_ORDER_CURRENT: tuple[str, ...] = (
    "capabilities",
    "goals",
    "media",
    "world",
    "scenario",
    "knowledge",
    "evidence",
    "relationship",
    "self_state",
    "core_memory",
)
"""**现在实际生效**的牺牲次序，即重构前 `context.py:1805` 手写的那一份。

为什么留两份而不是直接用注册表推导出的目标次序：

用户的要求是「如果超预算，在日志显示出来」，本轮是**加可观测性**，
不是改超限策略。设计文档 `:45` 自己写着「这相对现有 `SACRIFICE_ORDER`
会改变边缘超预算结果，必须在迁移 PR 明示和测试」——所以切换次序是
PR 3 的事，并且在那个 PR 里要**单独**说明"哪些内容会因此多留/少留"。

目标与现状的差异（`tests/unit/test_prompt_zones.py` 逐项断言）：

| 位置 | 现在 | 目标 | 原因 |
|---|---|---|---|
| 3 | `world` | `scenario` | 用户写的场景补丁不该比环境更难牺牲 |
| 4 | `scenario` | `world` | 同上 |
| 5 | `knowledge` | `relationship` | 处境整区（`shed_rank=5`）排在记忆区（6）之前 |
| 6 | `evidence` | `self_state` | 同上 |
| 7 | `relationship` | `knowledge` | 记忆整区后移 |
| 8 | `self_state` | `evidence` | 同上 |

也就是说：目标次序把 **`situation` 整区提前**到记忆区之前，并让
`scenario` 先于 `world` 让位。这两条都是设计判断，不是本次重构成
副作用——所以必须显式记录，不能悄悄生效。
"""


SACRIFICE_ORDER: tuple[str, ...] = SACRIFICE_ORDER_CURRENT
"""运行时使用的次序。**当前 = 重构前的行为，未改动。**

名字保持为 `SACRIFICE_ORDER`，这样 `context.py` 里引用它的地方
无需改名；PR 3 只需把这一行换成 `= SACRIFICE_ORDER_TARGET`，
差异和验收清单都已经在测试里写好了。
"""


# --------------------------------------------------------------------------- #
# 当前实际发送顺序（PR 3 之前的事实记录）
# --------------------------------------------------------------------------- #

OBSERVED_APPEND_ORDER: tuple[str, ...] = (
    "persona",
    "constraints",
    "scenario",
    "evidence",
    "core_memory",
    "knowledge",
    "media",
    "media",
    "self_state",
    "self_state",
    "relationship",
    "short_memory",
    "self_state",
    "short_memory",
    "capabilities",
    "capabilities",
    "capabilities",
    "capabilities",
)
"""`ContextCompiler.compile()` 现在**实际**按源码 `append()` 顺序发出的车道序列。

同一车道出现多次是真的：`self_state` 3 处（`:1018` `:1044` `:1160`）、
`short_memory` 2 处（`:1106` `:1195`）、`media` 2 处（`:897` `:961`）、
`capabilities` 4 处（`:1251` `:1270` `:1290` `:1299`）。

**这不是 bug**，是"生产者各自 append"的结果；但也不是目标排布。
本元组存在的唯一目的，是让 `tests/unit/test_prompt_zones.py` 能钉住
"当前顺序没有变得更乱"，并在 PR 3 切换顺序时给出逐项对照。
"""


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def validate_registry(zones: tuple[ZoneSpec, ...] = ZONES) -> None:
    """注册表自检。导入期就抛 `ValueError`，让写错的表**启动即崩**。

    不用 `assert`：`python -O` 会把 assert 整条删掉，而这是安全边界，
    不能因为一个优化开关就消失。错误信息一律带上具体区域／车道名，
    否则排障时只知道"表错了"，不知道错在哪一行。
    """
    ids: set[str] = set()
    names: set[str] = set()
    positions: set[int] = set()
    shed_ranks: set[int] = set()
    structural: dict[Kind, tuple[str, int]] = {}
    allowed_keys = {"system", "memory", "plan", "media", "summary", "history", "input"}

    for zone in zones:
        if zone.id in ids:
            raise ValueError(f"区域 id 重复: {zone.id}")
        if not zone.lanes:
            raise ValueError(f"区域没有车道: {zone.id}")
        if zone.message_order <= 0:
            raise ValueError(f"区域消息位置必须为正: {zone.id}")
        if zone.message_order in positions:
            raise ValueError(f"区域消息位置重复: {zone.id} @ {zone.message_order}")
        ids.add(zone.id)
        positions.add(zone.message_order)

        if zone.legacy_ui_key not in allowed_keys:
            raise ValueError(f"展示 key 未登记: {zone.id} -> {zone.legacy_ui_key}")

        if zone.shed_rank is not None:
            if zone.shed_rank <= 0:
                raise ValueError(f"牺牲优先级必须为正: {zone.id}")
            if zone.shed_rank in shed_ranks:
                raise ValueError(f"牺牲优先级重复: {zone.id} @ {zone.shed_rank}")
            shed_ranks.add(zone.shed_rank)

        if (zone.kind is Kind.SYSTEM) != (zone.shed_rank is not None):
            # 身份区特例：SYSTEM 可以整区全保护，此时 shed_rank=None。
            all_protected = all(lane.protected or not lane.active for lane in zone.lanes)
            if not (zone.kind is Kind.SYSTEM and all_protected):
                raise ValueError(f"区域缺少牺牲优先级: {zone.id}")

        if zone.kind is not Kind.SYSTEM:
            if zone.kind in structural:
                raise ValueError(f"结构区域只能有一个: {zone.id}")
            if len(zone.lanes) != 1:
                raise ValueError(f"结构区域只能有一条车道: {zone.id}")
            structural[zone.kind] = (zone.lanes[0].name, zone.message_order)

        message_orders: set[int] = set()
        shed_orders: set[int] = set()
        for lane in zone.lanes:
            if lane.name in names:
                raise ValueError(f"车道名重复: {lane.name}")
            if lane.message_order <= 0:
                raise ValueError(f"车道消息位置必须为正: {lane.name}")
            if lane.message_order in message_orders:
                raise ValueError(f"区内车道位置重复: {zone.id} / {lane.name}")
            names.add(lane.name)
            message_orders.add(lane.message_order)

            if zone.kind is Kind.SYSTEM and lane.active:
                if lane.protected != (lane.shed_order is None):
                    raise ValueError(
                        f"保护状态与区内牺牲位置不符（受保护就必须没有 shed_order，"
                        f"反之亦然）: {lane.name}"
                    )
                if lane.shed_order is not None:
                    if lane.shed_order <= 0:
                        raise ValueError(f"区内牺牲位置必须为正: {lane.name}")
                    if lane.shed_order in shed_orders:
                        raise ValueError(f"区内牺牲位置重复: {zone.id} / {lane.name}")
                    shed_orders.add(lane.shed_order)
                if lane.budget_tokens is None or lane.budget_tokens <= 0:
                    raise ValueError(f"启用的 system 车道必须有正预算: {lane.name}")

            if zone.kind is not Kind.SYSTEM:
                if lane.protected:
                    raise ValueError(f"结构车道不得使用 system 保护标志: {lane.name}")
                if lane.shed_order is not None:
                    raise ValueError(f"结构车道不得参与牺牲: {lane.name}")

            if not lane.active and lane.budget_tokens is not None:
                raise ValueError(f"禁发车道不得占用预算: {lane.name}")

    missing = {Kind.SUMMARY, Kind.HISTORY, Kind.INPUT} - set(structural)
    if missing:
        raise ValueError(f"缺少结构区域: {sorted(k.value for k in missing)}")

    expected_struct = {Kind.SUMMARY: "summary", Kind.HISTORY: "history", Kind.INPUT: "input"}
    for kind, lane_name in expected_struct.items():
        if structural[kind][0] != lane_name:
            raise ValueError(f"结构车道名称不符: {kind.value} 应为 {lane_name}")

    if not (
        structural[Kind.SUMMARY][1]
        < structural[Kind.HISTORY][1]
        < structural[Kind.INPUT][1]
    ):
        raise ValueError("摘要、历史、正文三者的消息顺序不符（应依次递增）")

    # 牺牲表必须覆盖所有"可牺牲"的 system 车道：漏一条的症状是那条车道
    # **永不**被软上限淘汰，一直占着预算直到被硬上限从中间截断。
    savable = {
        name
        for name, (zone, lane) in LANES.items()
        if zone.kind is Kind.SYSTEM and lane.active and not lane.protected
    }
    if set(sacrifice_order()) != savable:
        missing_shed = savable - set(sacrifice_order())
        raise ValueError(f"这些车道没有进入牺牲表: {sorted(missing_shed)}")


validate_registry()


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #


def lane_zone(name: str) -> ZoneSpec | None:
    """这条车道属于哪个区域。未知返回 `None`（查询用，不做校验）。"""
    found = LANES.get(name)
    return found[0] if found else None


def require_lane(name: str, *, kind: Kind | None = None) -> tuple[ZoneSpec, LaneSpec]:
    """取车道并校验，不认识就抛错。**不要**把它降级成 `other` 继续发送。"""
    found = LANES.get(name)
    if found is None:
        raise ValueError(f"未知的提示词车道: {name}")
    zone, lane = found
    if not lane.active:
        raise ValueError(f"提示词车道已禁发: {name}")
    if kind is not None and zone.kind is not kind:
        raise ValueError(
            f"车道所在消息类型不符: {name} 属于 {zone.kind.value}，期望 {kind.value}"
        )
    return zone, lane


def zone_message_order() -> tuple[str, ...]:
    """区域的目标消息顺序，按 `message_order` 升序。

    PR 3 之后编译器应按它排列 system 片段；在此之前它只是登记值。
    """
    return tuple(zone.id for zone in sorted(ZONES, key=lambda z: z.message_order))


def budget_overruns(
    lane_chars: dict[str, int],
) -> list[tuple[str, str, int, int]]:
    """哪些**系统片段**车道超了自己的预算。返回 `(区域 id, 车道, 实际, 预算)`。

    这是给日志和排障用的**可读结论**：用户看日志想知道"为什么这次少了一些
    东西"，答案必须是"哪个区域、超了多少"，而不是一大堆 token 数字。

    换算口径与编译器一致：`budget_chars = token_budget * 2`
    （见 `context.py` 里 `DEFAULT_BUDGETS` 上方对 10 万 token 的说明）。

    **只比较 system 车道。** `summary` / `history` 上的 `budget_tokens`
    登记的是它们**各自独立窗口策略**的目标值（摘要多长、历史窗口多大），
    不是"这条车道在总预算里占多少"，也不能被 `SACRIFICE_ORDER` 裁剪。
    把它们算进车道超额会让日志报出"摘要超了 400 token"这种没意义的告警——
    摘要本来就是滚动生成的，超长时由自己的策略处理。
    """
    overruns: list[tuple[str, str, int, int]] = []
    for lane_name, chars in lane_chars.items():
        found = LANES.get(lane_name)
        if found is None:
            continue
        zone, lane = found
        if zone.kind is not Kind.SYSTEM:
            continue
        if lane.budget_tokens is None:
            continue
        budget_chars = lane.budget_tokens * 2
        if chars > budget_chars:
            overruns.append((zone.id, lane_name, chars, budget_chars))
    overruns.sort(key=lambda item: item[2] - item[3], reverse=True)
    return overruns
