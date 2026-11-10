# 提示词分区与注入规范（设计稿）

适用范围：`ContextCompiler.compile()` 生成的**主对话**模型请求，以及向这条链路增加片段的生产者、预算逻辑和「查看本次提示词」界面。读者是以后维护这些代码的开发者，也包括需要判断「某段提示为何出现、消失或换位」的排障者。本文件先记录现状，再给出目标规范；下文的“应／必须／禁止”是**待迁移的设计约束**，不是声称当前代码已做到。

引用格式 `文件:行号` 指本设计时的工作树源码。工作树已有其他未提交改动；以下事实以当前文件为准。

## 1. 现状诊断：代码事实与设计判断

### 1.1 代码事实

1. `ContextContribution` 有 `lane`、`text`、`origin`、`visibility`、`valid_until`、`token_budget`；`lane` 是契约里的 `Literal` 闭集，而不是由预算表定义。契约仍允许 `scene`，同时也允许 `scenario`。`src/ling/contracts/v1.py:573-607`
2. `DEFAULT_BUDGETS` 列 13 条车道；默认合计 128600 个名义 token，其中 `persona=100000`。`world`、`goals` 有预算但在 `compile()` 中没有内置生产者；`scene` 在契约里却没有预算。历史原文另有 `HISTORY_TOKEN_BUDGET=20000`，摘要另有 `SUMMARY_TOKEN_BUDGET=400`。`src/ling/kernel/context.py:49-55`, `src/ling/kernel/context.py:94-98`, `src/ling/kernel/context.py:125-153`, `src/ling/contracts/v1.py:577-601`
3. 展示分组由 `LANE_TO_KEY` 映射到 `system/memory/plan/media`，未知车道落到 `other`；`KEY_ORDER` 是 UI 分组次序，不决定模型消息顺序。它还含当前编译器没有用作展示 key 的 `persona`。API 按 `KEY_ORDER` 重排分组，但组内用真实消息下标排序。`src/ling/kernel/context.py:293-354`, `src/ling/api/routes/sessions.py:410-448`
4. 总预算整条牺牲的顺序另写在 `SACRIFICE_ORDER`；`PROTECTED_LANES={persona,constraints,short_memory}`。总预算先按片段 `token_budget*2` 字符截断，再用预算和推导出的软／硬上限处理 `contributions`。软上限只遍历牺牲表；硬上限仍超时按当前保留列表顺序截断非保护片段。因此**未列入牺牲表的车道不会被软上限淘汰**，拼错名字可静默改变行为。`src/ling/kernel/context.py:1349-1394`, `src/ling/kernel/context.py:1800-1831`, `src/ling/kernel/context.py:1861-1922`
5. 人设为空时跳过 `persona/constraints/scenario` 三段，但记忆、知识、媒体、状态、关系、能力片段仍继续编译。`scenario` 还要求当前私聊／群聊对应的用户覆盖文本非空。`src/ling/kernel/context.py:553-644`
6. 内置 `contributions.append()` 从人设、场景、记忆和媒体一路到能力片段；`extra_contributions` 在能力片段前直接 `extend`，没有注册、位置或预算归一化检查。多个同车道片段分别用自己的 `token_budget` 截断，而 `DEFAULT_BUDGETS` 的总和只计算一次车道预算；例如重复提醒虽标成 `capabilities`，却取 `constraints` 的预算。`src/ling/kernel/context.py:589-644`, `src/ling/kernel/context.py:716-803`, `src/ling/kernel/context.py:896-1207`, `src/ling/kernel/context.py:1243-1256`, `src/ling/kernel/context.py:1349-1378`
7. 所有保留下来的 `contributions` **各变成一条 system 消息**，保留列表顺序；随后追加一条可选的 system 摘要、按时间从旧到新的 `user/assistant` 历史，最后追加本轮 `user` 正文。`messages` 从同一份 `annotated` 生成。`short_memory` 是前置 system 片段，承载窗口话题／未答问题及跨端胶囊，**不是**历史原文；当前轮落库消息会从历史里排除，避免正文重复。`src/ling/kernel/context.py:1086-1113`, `src/ling/kernel/context.py:1173-1201`, `src/ling/kernel/context.py:1396-1424`, `src/ling/kernel/context.py:1450-1493`, `src/ling/kernel/context.py:1557-1614`, `src/ling/kernel/context.py:1661-1684`, `src/ling/kernel/turn.py:870-887`
8. `summary/history/input` 是 `CompiledMessage.lane`，但不属于 `ContextContribution.lane`，也不经过 `DEFAULT_BUDGETS`／`SACRIFICE_ORDER`。摘要是预先滚动生成并截长，原文历史独立使用 20000 token 窗口；当前正文直接追加。`src/ling/kernel/context.py:49-55`, `src/ling/kernel/context.py:1409-1424`, `src/ling/kernel/context.py:1439-1475`, `src/ling/kernel/context.py:1661-1671`, `src/ling/kernel/context.py:1761-1778`
9. 这里的“工具”主要是 `capabilities` 里的**动作／表达方式说明**，不是模型 API 的原生 tool schema：`ChatMessageIn` 只有 `system/user/assistant` 文本及媒体部分，`ModelRequest` 只有 `messages` 等请求字段。观看视频后的同轮补生成另在 `turn.py` 末尾追加一条 `user` 观察事实；自主决策等其他模型调用有自己的消息装配，不能误称已受本规范约束。`src/ling/kernel/context.py:1259-1346`, `src/ling/ports/model.py:256-290`, `src/ling/kernel/turn.py:1708-1724`, `src/ling/kernel/turn.py:1757-1770`, `src/ling/kernel/autonomy.py:377-379`

### 1.2 设计判断

现有顺序是“哪段代码先 `append`”，并非可检查的协议；展示分组、牺牲表、保护集合和契约枚举还各存一份语义。虽然测试检查了部分车道映射和牺牲表成员，但没有证明**所有能发出的车道**恰好属于一个区域，也没有证明片段实际预算等于登记预算。`src/ling/kernel/context.py:312-354`, `src/ling/kernel/context.py:1206-1207`, `src/ling/kernel/context.py:1804-1819`, `tests/unit/test_prompt_snapshot_payload.py:41-68`, `tests/unit/test_context_history_fit.py:77-82`

## 2. 分区模型

**设计提案：10 个区域。** 区域是语义和治理单元；车道是单一来源／预算单元；片段是一次具体注入。区域有固定的消息顺序与整条牺牲优先级。相同区域内按车道消息顺序、再按片段原有的稳定序号排序；牺牲时另按区内牺牲顺序。以下预算是目标注册值；`summary/history/input` 用各自的窗口策略，不参加 system 片段的软／硬总预算。

| 消息顺序 | 区域（ID） | 它回答什么 | 所属车道及目标预算（名义 token） | 软上限牺牲次序 | 现行 UI key |
|---:|---|---|---|---:|---|
| 10 | 身份 `identity` | 她是谁；用户设定的角色和边界 | `persona` 100000、`constraints` 1200 | 不整条牺牲 | `system` |
| 20 | 场景 `scene` | 当前私聊／群聊适用的用户场景覆盖 | `scenario` 600；旧 `scene` 仅作禁发迁移项 | 4 | `system` |
| 30 | 当前处境 `situation` | 她现在的状态、环境和与眼前人的关系 | `world` 2000、`self_state` 2000、`relationship` 1200 | 5 | `memory` |
| 40 | 可用背景 `memory` | 她此轮可用的已知事实、检索结果和短时话题 | `short_memory` 4000、`core_memory` 6000、`evidence` 6000、`knowledge` 3000 | 6；`short_memory` 不整条牺牲 | `memory` |
| 50 | 意图 `plan` | 此轮可参考的目标，而非已发生事实 | `goals` 800 | 2 | `plan` |
| 60 | 媒体 `media` | 已看见／待查看的图、音、视频证据与标记 | `media` 1500 | 3 | `media` |
| 70 | 可执行能力 `capabilities` | 当前渠道真正能做到的动作、标记与表达格式 | `capabilities` 300 | 1（最先） | `system` |
| 80 | 早期摘要 `recap` | 本窗口更早发生了什么 | `summary` 400；独立摘要长度限制 | 独立窗口策略 | `summary` |
| 90 | 近段对话 `dialogue` | 双方最近实际说过什么 | `history` 20000；独立历史窗口 | 先滚动旧消息 | `history` |
| 100 | 本轮输入 `input` | 正在回复的原始用户正文 | `input`；无 `ContextContribution` 预算 | 必须保留 | `input` |

上述是**目标排布，不是当前发送顺序**。当前 `self_state/relationship/short_memory` 在能力说明之前但交错于证据、知识、媒体之间；当前 `core_memory` 在 `evidence` 后追加，虽然代码注释说它放在证据之前。`src/ling/kernel/context.py:716-761`, `src/ling/kernel/context.py:789-803`, `src/ling/kernel/context.py:895-1023`, `src/ling/kernel/context.py:1059-1113`, `src/ling/kernel/context.py:1259-1279`

区内消息顺序定为：`identity: persona → constraints`；`scene: scenario`；`situation: world → self_state → relationship`；`memory: short_memory → core_memory → evidence → knowledge`。区内牺牲顺序则为 `situation: world → relationship → self_state`、`memory: knowledge → evidence → core_memory`；受保护的 `short_memory` 不参加。其余活动 system 区都只有一条车道。这里两种顺序故意不完全一致，分别由 `message_order` 和 `shed_order` 表达。

**优先级理由（设计推断）：** 身份与本轮正文一旦丢失，回复对象或角色就不再正确；近段对话、摘要已有专门的滚动窗口，不能拿 system 片段的牺牲表直接管理。`short_memory` 维持当前保护承诺。可执行能力最易由渠道侧再确认，目标、媒体、用户场景覆盖可在拥挤时先让位；处境与记忆更能保持连贯性。场景是**用户写的补丁**，不应比稳定身份更难牺牲。这里把 `relationship/self_state` 与 `world` 一起放入处境区，整区在场景之后、记忆之前牺牲；这相对现有 `SACRIFICE_ORDER` 会改变边缘超预算结果，必须在迁移 PR 明示和测试。`src/ling/kernel/context.py:150-152`, `src/ling/kernel/context.py:1804-1819`

**三种顺序的关系（设计约束）：**

- **必须一致：** 模型的 system 片段顺序 = 注册表的区域消息顺序、车道顺序、稳定片段序号；快照的 `index` 必须反映这一真实顺序。UI 同区项目按 `index` 显示。未注册片段不得通过 `other` 回退发给模型。
- **允许不同：** 牺牲顺序按**保留价值**，不是阅读先后；例如 `capabilities` 虽靠近正文，仍最先让位。当前 UI 可暂时把多个区域合并到旧 `key`，但不能把一个车道拆到多个区域；API 在过渡期可以保持旧 `KEY_ORDER`，同时提供真实 `index`。最终 UI 应按区域展示，展示顺序与消息顺序一致。
- **预算域不同：** 区域优先级只推导 system 片段的整条牺牲；`summary/history/input` 的滚动、保留和上游输入限长另行定义。不能把它们伪装成可被 `SACRIFICE_ORDER` 裁剪的 `ContextContribution`。

## 3. 单一数据模型（目标代码形状，非已实现）

注册表应放在低依赖模块（例如 `ling/contracts/prompt_zones.py`），由契约、编译器、API 和测试共同读取，避免 `contracts.v1` 反向导入 `kernel.context`。**唯一手写的车道清单是 `ZoneSpec.lanes`**；预算、消息位置、保护、门槛、展示均从它读出。示例省略无关文字标签，但保留所有字段的形状：

```python
from dataclasses import dataclass
from enum import StrEnum

class Kind(StrEnum):
    SYSTEM = "system"       # ContextContribution -> system 消息
    SUMMARY = "summary"     # 独立滚动摘要
    HISTORY = "history"     # user/assistant 原文
    INPUT = "input"         # 当前 user 正文

class Gate(StrEnum):
    PERSONA_PRESENT = "persona_present"
    SCENARIO_OVERRIDE_PRESENT = "scenario_override_present"
    AUDIENCE_ALLOWED = "audience_allowed"
    POKE_SUPPORTED = "poke_supported"
    RECALLABLE = "recallable"
    PRIVATE_CHAT = "private_chat"

@dataclass(frozen=True)
class CompileFacts:
    persona_present: bool
    scenario_override_present: bool
    audience_allowed: bool
    poke_supported: bool
    recallable: bool
    private_chat: bool

@dataclass(frozen=True)
class Candidate:
    lane: str
    text: str
    origin: object                 # 实现时使用现有 Origin
    visibility: object             # 实现时使用现有 Visibility
    fragment_gates: frozenset[Gate] = frozenset()
    token_budget_override: int | None = None

@dataclass(frozen=True)
class LaneSpec:
    name: str
    budget_tokens: int | None
    message_order: int             # 区内发送位置；不是 append 的先后
    shed_order: int | None         # 区内牺牲位置；受保护或结构车道为 None
    protected: bool = False        # 禁止整条牺牲；不代表忽略单片段上限
    gates: frozenset[Gate] = frozenset()
    active: bool = True            # 仅迁移中的旧 scene 为 False

@dataclass(frozen=True)
class ZoneSpec:
    id: str
    label: str
    meaning: str
    kind: Kind
    message_order: int             # 最终 messages 中的位置
    shed_rank: int | None          # 越小越先牺牲；非 SYSTEM 用 None
    legacy_ui_key: str             # 过渡期旧快照/API 映射
    lanes: tuple[LaneSpec, ...]

ZONES: tuple[ZoneSpec, ...] = (
    ZoneSpec("identity", "身份", "她是谁", Kind.SYSTEM, 10, None, "system", (
        LaneSpec("persona", 100_000, 10, None, True,
                 frozenset({Gate.PERSONA_PRESENT})),
        LaneSpec("constraints", 1_200, 20, None, True,
                 frozenset({Gate.PERSONA_PRESENT})),
    )),
    ZoneSpec("scene", "场景", "当前场景覆盖", Kind.SYSTEM, 20, 4, "system", (
        LaneSpec("scenario", 600, 10, 10, gates=frozenset({
            Gate.PERSONA_PRESENT, Gate.SCENARIO_OVERRIDE_PRESENT,
        })),
        LaneSpec("scene", None, 99, None, active=False),  # 迁移后删除
    )),
    ZoneSpec("situation", "当前处境", "环境、状态、关系", Kind.SYSTEM, 30, 5, "memory", (
        LaneSpec("world", 2_000, 10, 10),
        LaneSpec("self_state", 2_000, 20, 30),
        LaneSpec("relationship", 1_200, 30, 20),
    )),
    ZoneSpec("memory", "可用背景", "短时话题与可信事实", Kind.SYSTEM, 40, 6, "memory", (
        LaneSpec("short_memory", 4_000, 10, None, True),
        LaneSpec("core_memory", 6_000, 20, 30),
        LaneSpec("evidence", 6_000, 30, 20),
        LaneSpec("knowledge", 3_000, 40, 10),
    )),
    ZoneSpec("plan", "意图", "当前目标", Kind.SYSTEM, 50, 2, "plan", (
        LaneSpec("goals", 800, 10, 10),
    )),
    ZoneSpec("media", "媒体", "媒体证据与查看提示", Kind.SYSTEM, 60, 3, "media", (
        LaneSpec("media", 1_500, 10, 10),
    )),
    ZoneSpec("capabilities", "可执行能力", "可用动作和格式", Kind.SYSTEM, 70, 1, "system", (
        LaneSpec("capabilities", 300, 10, 10),
    )),
    ZoneSpec("recap", "早期摘要", "滚动回顾", Kind.SUMMARY, 80, None, "summary", (
        LaneSpec("summary", 400, 10, None),
    )),
    ZoneSpec("dialogue", "近段对话", "近期原文", Kind.HISTORY, 90, None, "history", (
        LaneSpec("history", 20_000, 10, None),
    )),
    ZoneSpec("input", "本轮输入", "正在回复的正文", Kind.INPUT, 100, None, "input", (
        LaneSpec("input", None, 10, None),
    )),
)

LANES = {lane.name: (zone, lane) for zone in ZONES for lane in zone.lanes}
DEFAULT_BUDGETS = {
    name: lane.budget_tokens for name, (zone, lane) in LANES.items()
    if zone.kind == Kind.SYSTEM and lane.active
}
PROTECTED_LANES = frozenset(
    name for name, (_, lane) in LANES.items() if lane.active and lane.protected
)
```

牺牲顺序须先过滤再排序，不能把 `shed_rank=None` 的结构区放入排序：

```python
def sacrifice_order() -> tuple[str, ...]:
    candidates = [
        (zone, lane) for zone in ZONES for lane in zone.lanes
        if zone.kind is Kind.SYSTEM and zone.shed_rank is not None
        and lane.active and not lane.protected
    ]
    candidates.sort(key=lambda pair: (
        pair[0].shed_rank, pair[1].shed_order, pair[1].name
    ))
    return tuple(lane.name for _, lane in candidates)

SACRIFICE_ORDER = sacrifice_order()
```

区域注册表中的预算是每车道的**聚合上限**，不是每个片段都能再拿一份：先按车道汇总／分配片段预算，再按区域牺牲。仅 `self_state` 的意识块这类子片段可用 `min(车道剩余额度, 明示子上限)`；重复提醒不得继续借 `constraints` 的额度。此项会改变当前同车道多片段的截断量，应在迁移前录制对照。`src/ling/kernel/context.py:1015-1049`, `src/ling/kernel/context.py:1158-1167`, `src/ling/kernel/context.py:1243-1256`, `src/ling/kernel/context.py:1364-1378`

能力前置条件有两层：**车道门槛**由 `LaneSpec.gates` 规定（如 `scenario` 需要有效人设及本场景覆盖），**片段门槛**由候选片段声明并由同一门闸执行（如拍一拍需连接器支持、撤回需有可撤回记录、延后仅私聊）。受众／隐私仍要先在数据层裁决，注册表不能代替 `AudienceSnapshot`。编译器只接收结构化的 `Candidate(lane, text, origin, visibility, fragment_gates, ...)`，门闸检查后生成 `ContextContribution`；外部 `extra_contributions` 也必须走同一入口。`src/ling/kernel/context.py:646-683`, `src/ling/kernel/context.py:1206-1207`, `src/ling/kernel/context.py:1271-1295`, `src/ling/kernel/context.py:1327-1346`

现有 `ContextContribution.lane: Literal[...]` 要在迁移中改为经过注册表校验的 `str`（或专用 `LaneId` 类型）；否则新增车道仍要同时修改 Literal 与注册表。建议在创建候选片段、接收额外片段、组装消息三个边界都 `require_lane(name)`；未知或禁发车道抛明确异常并记录名称，**不**映射成 `other` 继续发送。`origin`、`visibility`、有效期仍由原契约和受众规则检查。`src/ling/contracts/v1.py:573-607`, `src/ling/kernel/context.py:293-300`, `src/ling/kernel/context.py:1206-1207`

## 4. 机制性约束与校验（目标代码形状）

注册表构建时和 CI 中调用同一个校验函数；使用显式 `raise ValueError`，不要依赖可被 `python -O` 关闭的 `assert`。运行时对每个片段再次检查，不能只靠测试发现动态生产者／额外片段的错名。

```python
def validate_registry(zones: tuple[ZoneSpec, ...]) -> None:
    ids: set[str] = set()
    names: set[str] = set()
    positions: set[int] = set()
    shed_ranks: set[int] = set()
    structural: dict[Kind, tuple[str, int]] = {}
    for zone in zones:
        if (zone.id in ids or zone.message_order in positions or not zone.lanes
            or zone.message_order <= 0):
            raise ValueError(f"重复或空区域: {zone.id}")
        ids.add(zone.id)
        positions.add(zone.message_order)
        if zone.legacy_ui_key not in {
            "system", "memory", "plan", "media", "summary", "history", "input"
        }:
            raise ValueError(f"展示 key 未登记: {zone.id}")
        if zone.shed_rank is not None:
            if zone.shed_rank <= 0 or zone.shed_rank in shed_ranks:
                raise ValueError(f"牺牲优先级重复或无效: {zone.id}")
            shed_ranks.add(zone.shed_rank)
        if (zone.kind is Kind.SYSTEM) != (zone.shed_rank is not None):
            # 身份区特例：SYSTEM 可以全保护且 shed_rank=None。
            if not (zone.kind is Kind.SYSTEM and all(l.protected or not l.active for l in zone.lanes)):
                raise ValueError(f"区域缺少牺牲优先级: {zone.id}")
        if zone.kind is not Kind.SYSTEM:
            if zone.kind in structural or len(zone.lanes) != 1:
                raise ValueError(f"结构区域须唯一且只有一条车道: {zone.id}")
            structural[zone.kind] = (zone.lanes[0].name, zone.message_order)
        message_orders: set[int] = set()
        shed_orders: set[int] = set()
        for lane in zone.lanes:
            if (lane.name in names or lane.message_order in message_orders
                or lane.message_order <= 0):
                raise ValueError(f"重复车道或区内位置: {lane.name}")
            names.add(lane.name)
            message_orders.add(lane.message_order)
            if lane.active and zone.kind is Kind.SYSTEM:
                if lane.protected != (lane.shed_order is None):
                    raise ValueError(f"保护状态与区内牺牲位置不符: {lane.name}")
                if lane.shed_order is not None:
                    if lane.shed_order <= 0 or lane.shed_order in shed_orders:
                        raise ValueError(f"区内牺牲位置重复: {zone.id}")
                    shed_orders.add(lane.shed_order)
            if zone.kind is Kind.SYSTEM and lane.active and (
                lane.budget_tokens is None or lane.budget_tokens <= 0
            ):
                raise ValueError(f"system 车道缺预算: {lane.name}")
            if zone.kind is not Kind.SYSTEM and lane.protected:
                raise ValueError(f"结构车道不得使用 system 保护标志: {lane.name}")
            if zone.kind is not Kind.SYSTEM and lane.shed_order is not None:
                raise ValueError(f"结构车道不得参与牺牲: {lane.name}")
            if not lane.active and lane.budget_tokens is not None:
                raise ValueError(f"禁发车道不得占预算: {lane.name}")
    if set(structural) != {Kind.SUMMARY, Kind.HISTORY, Kind.INPUT}:
        raise ValueError("summary/history/input 区域缺失")
    if [structural[k][0] for k in (Kind.SUMMARY, Kind.HISTORY, Kind.INPUT)] != [
        "summary", "history", "input"
    ]:
        raise ValueError("结构车道名称不符")
    if not (structural[Kind.SUMMARY][1] < structural[Kind.HISTORY][1]
            < structural[Kind.INPUT][1]):
        raise ValueError("摘要、历史、正文顺序不符")
    # 上面的重名检查阻止构造 LANES 字典时静默覆盖已有车道。

validate_registry(ZONES)  # 模块初始化时 fail fast

def require_lane(name: str, *, kind: Kind | None = None) -> tuple[ZoneSpec, LaneSpec]:
    found = LANES.get(name)
    if found is None or not found[1].active:
        raise ValueError(f"未知或禁发的提示词车道: {name}")
    if kind is not None and found[0].kind is not kind:
        raise ValueError(f"车道所在消息类型不符: {name}")
    return found

def check_gate(gate: Gate, facts: CompileFacts) -> bool:
    if not isinstance(gate, Gate):
        raise ValueError(f"未知注入门槛: {gate}")
    return bool(getattr(facts, gate.value, False))

def validate_candidate(candidate: Candidate, facts: CompileFacts) -> None:
    _, lane = require_lane(candidate.lane, kind=Kind.SYSTEM)
    if not all(check_gate(gate, facts) for gate in lane.gates | candidate.fragment_gates):
        raise ValueError(f"片段不满足注入前置条件: {candidate.lane}")
    if candidate.token_budget_override is not None:
        if not 0 < candidate.token_budget_override <= lane.budget_tokens:
            raise ValueError(f"片段预算超出车道范围: {candidate.lane}")
    # 另查受众/来源/有效期；再进入按车道聚合预算和统一排序。
```

`check_gate()` 只读取 `CompileFacts` 中同名布尔值：未知门槛抛错，缺事实按 `False` 处理；片段门槛必须在渲染前执行。`validate_registry()` 在模块初始化时运行；CI 再检查预算缩放只从注册值推导一次、`DEFAULT_BUDGETS` 键集恰好等于所有活动 system 车道、`set(sacrifice_order())` 恰好等于所有活动非保护 system 车道，以及三类结构车道分别生成 system、user/assistant、user 角色。新门槛原语需要扩充 `Gate` 和事实采集，但不产生第二份车道定义。

CI 除纯注册表测试外，还要有编译器契约测试：空人设三段均缺席而功能层仍在；私／群场景覆盖只发对应一段；随机打乱生产者返回次序仍得到固定区域顺序；`extra_contributions` 使用 `scene`、错字或未知车道立即报错；同车道多片段合计不超预算；预算收缩时牺牲顺序与注册表一致、受保护车道不整条丢；`annotated` 与 `messages` 同序同内容，UI 同区仍按真实 `index`。已有测试只检查局部映射与 `scenario` 可牺牲，不能替代这些不变量。`src/ling/kernel/context.py:1396-1407`, `src/ling/kernel/context.py:1676-1684`, `tests/unit/test_prompt_snapshot_payload.py:41-68`, `tests/unit/test_context_history_fit.py:77-95`

## 5. 正文与 messages 排布

目标主对话数组（省略空区，系统片段可以有多条，**不**强合并成单条 system）：

```text
0..N   system  identity -> scene -> situation -> memory -> plan -> media -> capabilities
N+1    system  recap/summary（若有）
...    user/assistant  dialogue/history（真实时间从旧到新）
last   user    input（本轮正文；群聊可加姓名与 msg_id 标记）
```

当前数组也遵循“全部贡献片段 → 摘要 → 历史 → 当前正文”的**大层顺序**，但贡献片段内部尚按生产者追加次序，而不是上述区域次序。当前正文在 `user_text` 之后可添群聊署名及消息 ID；本轮已落库的消息从历史筛掉。`src/ling/kernel/context.py:1399-1424`, `src/ling/kernel/context.py:1462-1464`, `src/ling/kernel/context.py:1557-1614`, `src/ling/kernel/context.py:1632-1671`

`short_memory` 保持在 memory 区的 system 前置片段；它概括窗口话题或跨端交接，不冒充历史中的任何一条 `user` 话。`history` 保留原角色与时序，`input` 必须是最后一条主对话输入。媒体观察后的补生成是主数组之后追加事实的**另一条调用路径**，需单列为后续纳管项，不能悄悄塞入 `capabilities` 或伪称用户新发正文。`src/ling/kernel/context.py:1086-1113`, `src/ling/kernel/context.py:1173-1201`, `src/ling/kernel/context.py:1557-1614`, `src/ling/kernel/turn.py:1757-1770`

## 6. 迁移路径（建议 4 个 PR；本文件不实施）

1. **PR 1：注册表与事实锁定。** 建立完整 10 区注册表和生成式校验；把所有现存契约车道连同 `scene` 迁移项、`summary/history/input` 登记。记录当前真实消息顺序、UI 分组和超预算结果的对照测试。先不切换排序和预算行为。处理 `ContextContribution` 的双重车道枚举，让注册表成为唯一车道清单。核查有无外部 `scene` 生产者后拒发／移除它。
2. **PR 2：统一入口与预算。** 让内置及 `extra_contributions` 都经过 `require_lane`、门槛、受众和片段校验；从注册表推导预算及保护集合，按车道聚合额度；修复重复提醒借 `constraints` 预算及多片段重复领取额度。补充预算缩放、空人设、能力门槛和异常车道回归。
3. **PR 3：固定发送顺序与牺牲顺序。** 按区域／车道／片段稳定序号组装 system 消息，并从 `shed_rank` 生成牺牲顺序；保留摘要、历史、正文的尾部结构。明确记录 `situation` 合区造成的超预算顺序变化，核对快照内容和模型实收消息一致。
4. **PR 4：UI 与外围调用纳管。** 快照/API 增加 `zone`，旧 `key` 保持兼容并可从注册表导出；界面改按区域消息顺序展示，再评估移除旧 `KEY_ORDER/LANE_TO_KEY`。对视频补生成、自主决策等其他请求建立各自的分区规则或显式标为独立协议，禁止将主对话规范误套到未迁移调用。`src/ling/api/routes/sessions.py:410-448`, `src/ling/kernel/turn.py:1757-1770`, `src/ling/kernel/autonomy.py:377-379`

每个 PR 先跑对应回归和 `git diff --check`；迁移中凡是改变片段存在性、位置、可见性或裁剪结果，都要以真实 `annotated/messages` 对照说明。预算表、展示映射、牺牲表只在最后一条消费者改由注册表推导后才能删去旧实现。

## 7. 正式规范正文（迁移完成后作为维护契约）

### 7.1 固定规则

1. 主对话提示词只能通过注册车道注入。每个车道恰好归属一个区域；区域决定语义、消息位置、预算域与超限牺牲层级。未知／禁发车道在编译边界报错，不得静默落入 `other`。
2. 任何新注入都必须声明 `lane`、可信来源、受众范围、有效期（适用时）和片段门槛；外部网页、OCR、群友消息、工具结果等只能作为不可信**数据**进入，不能通过换车道获得身份或系统规则权限。当前编译器已有外部内容包裹原则。`src/ling/kernel/context.py:1-7`, `src/ling/kernel/context.py:35-38`, `src/ling/kernel/context.py:1583-1584`
3. `identity` 仅存用户配置的角色与边界；人设为空时 `persona/constraints/scenario` 都不发，功能与事实区域照常根据各自条件运行。`scenario` 只发当前私／群场景的非空用户覆盖，禁止自动回填内置默认场景。`src/ling/kernel/context.py:553-644`
4. `capabilities` 只声明当前渠道确实可执行的动作；能力的缺失不能借用受保护的 `constraints` 车道。源数据／权限检查必须先于区域排序，区域优先级不能抬高数据可信度。`src/ling/kernel/context.py:646-683`, `src/ling/kernel/context.py:1243-1256`, `src/ling/kernel/context.py:1271-1295`
5. 车道预算是同车道所有片段的合计上限，`persona` 的注册上限维持用户指定的 100000 名义 token；超额需可观测并留截断标记。整条牺牲顺序只能从区域注册表推导；受保护车道不得被整条丢弃。角色区域虽不整条牺牲，仍受其单车道上限约束。`src/ling/kernel/context.py:125-153`, `src/ling/kernel/context.py:1349-1394`, `src/ling/kernel/context.py:1818-1819`
6. 每条最终消息的 `lane/zone/key/index` 在编译时确定；`messages` 必须从同一份标注消息派生。UI 可以合并展示区，但不得更改真实 `index`，不得用 UI 分组倒推出发送顺序。`src/ling/kernel/context.py:265-289`, `src/ling/kernel/context.py:1396-1407`, `src/ling/api/routes/sessions.py:431-448`
7. `summary` 位于全部 system 片段后；`history` 按真实时间接在摘要后；`input` 是主对话最后一条用户消息。`short_memory` 仍是前置 system 话题线索，不等同于历史原文。`src/ling/kernel/context.py:1086-1113`, `src/ling/kernel/context.py:1409-1424`, `src/ling/kernel/context.py:1557-1614`, `src/ling/kernel/context.py:1661-1677`

### 7.2 禁止清单

- 禁止在注册表外另写车道预算表、牺牲表、保护表或展示映射作为第二权威。
- 禁止用 `append()` 的源码位置决定最终消息区域顺序；禁止 `extra_contributions` 直接绕过注册和门槛。
- 禁止给一个片段标 A 车道却从 B 车道取额度，或让同车道多个片段各领取完整额度。
- 禁止把系统能力说明当作原生 tool schema，也禁止把工具／外部数据包装成高可信身份指令。
- 禁止借“空人设”自动补默认角色、角色名、叙事边界或场景；禁止把摘要、历史或当前正文按普通 system 贡献片段牺牲。

### 7.3 新增车道的流程

先确定它回答的问题、来源可信度、受众、有效期、出现条件和是否真需要独立预算；优先复用已有车道。确需新增时，**只在 `ZONES` 中某个区域的 `lanes` 增加一条 `LaneSpec`**，然后让生产者使用该名称和结构化候选片段。派生预算、保护集合、牺牲顺序、消息位置、展示归属和契约运行时校验均不得再手写一份。补充该车道的“出现／不出现／超预算／快照位置”行为测试；运行注册表校验及主对话集成回归。若它无法放进现有区域，应先解释新区域的语义和牺牲层级，再新增区域和车道。
