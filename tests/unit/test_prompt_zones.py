"""提示词分区注册表的契约测试。

这个文件守的是「**哪个区域在哪、超了谁先死**」这类问题必须能被断言，
而不是散在一堆 `append()` 的源码位置里。

对应 `task-38` 的三条要求：
1. 分区注册表（结构化、可查询）
2. 超预算记日志（在 `tests/integration/test_budget_logging.py`）
3. 分区顺序可断言（本文件主体）

## 为什么有「当前 vs 目标」两套断言

设计文档 `PROMPT-ZONE-SPEC.md` §6 把迁移分成 4 个 PR。本轮是 **PR 1**：
只登记与校验，**不切换发送顺序、不改预算行为**（用户要的是「超预算能被
看到」，不是「换一套超限策略」）。

所以这里同时钉住两件事：
- `OBSERVED_APPEND_ORDER`：当前编译器**实际**发出的车道序列，防它变乱；
- `SACRIFICE_ORDER_TARGET`：文档定义的目标牺牲次序，作为 PR 3 的验收清单。

两者此刻**不一致**，测试把差异逐项写出来。等 PR 3 切换时，改的是
`prompt_zones.SACRIFICE_ORDER` 那一行，而目标顺序的断言早就写好了。
"""

from __future__ import annotations

import dataclasses

import pytest

from ling.kernel.context import (
    DEFAULT_BUDGETS as CTX_BUDGETS,
)
from ling.kernel.context import (
    LANE_TO_KEY as CTX_LANE_TO_KEY,
)
from ling.kernel.context import (
    PROTECTED_LANES as CTX_PROTECTED,
)
from ling.kernel.context import (
    SACRIFICE_ORDER as CTX_SACRIFICE,
)
from ling.kernel.prompt_zones import (
    DEFAULT_BUDGETS,
    LANE_TO_KEY,
    LANES,
    OBSERVED_APPEND_ORDER,
    PROTECTED_LANES,
    SACRIFICE_ORDER,
    SACRIFICE_ORDER_CURRENT,
    SACRIFICE_ORDER_TARGET,
    ZONES,
    Gate,
    Kind,
    LaneSpec,
    ZoneSpec,
    budget_overruns,
    lane_zone,
    require_lane,
    sacrifice_order,
    validate_registry,
    zone_message_order,
)

# --------------------------------------------------------------------------- #
# 注册表本身
# --------------------------------------------------------------------------- #


def test_registry_validates_at_import() -> None:
    """导入即校验。手工再跑一次确认它没被短路。"""
    validate_registry()


def test_zones_are_unique_and_ordered() -> None:
    ids = [z.id for z in ZONES]
    assert len(ids) == len(set(ids)), f"区域 id 重复: {ids}"
    orders = [z.message_order for z in ZONES]
    assert len(orders) == len(set(orders)), f"区域消息位置重复: {orders}"


def test_zone_message_order_matches_spec_table() -> None:
    """消息顺序钉死成设计文档 §2 那张表的数字。"""
    assert zone_message_order() == (
        "identity",
        "scene",
        "situation",
        "memory",
        "plan",
        "media",
        "capabilities",
        "recap",
        "dialogue",
        "input",
    )


@pytest.mark.parametrize(
    ("zone_id", "order"),
    [
        ("identity", 10),
        ("scene", 20),
        ("situation", 30),
        ("memory", 40),
        ("plan", 50),
        ("media", 60),
        ("capabilities", 70),
        ("recap", 80),
        ("dialogue", 90),
        ("input", 100),
    ],
)
def test_each_zone_position_is_pinned(zone_id: str, order: int) -> None:
    """逐个区域钉位置。有人插一个区域到错误的地方，总会撞上这里。"""
    zone = next(z for z in ZONES if z.id == zone_id)
    assert zone.message_order == order


def test_every_active_lane_belongs_to_exactly_one_zone() -> None:
    """**核心不变量**：没有车道能在两个区域里，也没有车道能无处可归。"""
    seen: dict[str, str] = {}
    for zone in ZONES:
        for lane in zone.lanes:
            assert lane.name not in seen, (
                f"车道 {lane.name} 同时属于 {seen[lane.name]} 与 {zone.id}——"
                f"预算归属会变成未定义"
            )
            seen[lane.name] = zone.id
    assert set(seen) == set(LANES)


def test_structural_zones_come_last_in_message_order() -> None:
    """摘要 → 历史 → 正文，必须依次在后。"""
    by_kind = {z.kind: z for z in ZONES}
    assert by_kind[Kind.SUMMARY].message_order < by_kind[Kind.HISTORY].message_order
    assert by_kind[Kind.HISTORY].message_order < by_kind[Kind.INPUT].message_order


def test_structural_lanes_are_named_exactly() -> None:
    for kind, expected in (
        (Kind.SUMMARY, "summary"),
        (Kind.HISTORY, "history"),
        (Kind.INPUT, "input"),
    ):
        zone = next(z for z in ZONES if z.kind is kind)
        assert [lane.name for lane in zone.lanes] == [expected]


def test_structural_budgets_are_window_targets_not_lane_budgets() -> None:
    """结构车道的 `budget_tokens` 是**各自窗口策略的目标值**，不是车道预算。

    区别很重要：`summary` 登记 400 表示"摘要目标长度 400 token"，
    `history` 登记 20000 表示"历史窗口 20000 token"。它们**不**参与
    `DEFAULT_BUDGETS` 求和（否则总预算会被撑大），也不进牺牲表。
    而 `input` 连这个目标值都没有——本轮正文必须完整保留。
    """
    recap = next(z for z in ZONES if z.kind is Kind.SUMMARY)
    assert recap.lanes[0].budget_tokens == 400
    assert "summary" not in DEFAULT_BUDGETS, "摘要目标长度不该进车道预算求和"

    dialogue = next(z for z in ZONES if z.kind is Kind.HISTORY)
    assert dialogue.lanes[0].budget_tokens == 20_000
    assert "history" not in DEFAULT_BUDGETS

    inp = next(z for z in ZONES if z.kind is Kind.INPUT)
    assert inp.lanes[0].budget_tokens is None, "正文没有上限，不该登记一个数字"


def test_zone_specs_are_frozen() -> None:
    """注册表必须不可变——运行期改预算是最难查的一类故障。"""
    zone = ZONES[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        zone.message_order = 999  # type: ignore[misc]


def test_lane_specs_are_frozen() -> None:
    lane = LANES["persona"][1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        lane.budget_tokens = 1  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 派生表：不许有第二份权威
# --------------------------------------------------------------------------- #


def test_context_reexports_the_registry_tables() -> None:
    """`context.py` 里的四张表必须**就是**注册表那份，不是复制品。"""
    assert CTX_BUDGETS == DEFAULT_BUDGETS
    assert CTX_SACRIFICE == SACRIFICE_ORDER
    assert CTX_PROTECTED == PROTECTED_LANES
    assert {
        lane: key for lane, key in LANE_TO_KEY.items() if lane not in {"summary", "history", "input"}
    } == CTX_LANE_TO_KEY


def test_default_budgets_keys_are_exactly_active_system_lanes() -> None:
    expected = {
        name
        for name, (zone, lane) in LANES.items()
        if zone.kind is Kind.SYSTEM and lane.active
    }
    assert set(DEFAULT_BUDGETS) == expected


def test_default_budgets_values_are_unchanged_from_the_original_table() -> None:
    """**行为不变证据**：逐项对照重构前 `context.py` 手写的那张表。

    数值一旦漂移，`_total_soft_hard()` 推出来的软／硬上限就跟着变，
    于是"什么时候开始丢东西"也变——那不是本次重构该做的事。
    """
    original = {
        "persona": 100_000,
        "short_memory": 4_000,
        "core_memory": 6_000,
        "evidence": 6_000,
        "knowledge": 3_000,
        "world": 2_000,
        "self_state": 2_000,
        "relationship": 1_200,
        "media": 1_500,
        "goals": 800,
        "capabilities": 300,
        "constraints": 1_200,
        "scenario": 600,
    }
    assert original == DEFAULT_BUDGETS


def test_protected_lanes_are_unchanged() -> None:
    assert frozenset({"persona", "constraints", "short_memory"}) == PROTECTED_LANES


def test_disabled_lane_takes_no_budget() -> None:
    """禁发的 `scene` 不许占预算——否则总预算会莫名变大。"""
    assert "scene" not in DEFAULT_BUDGETS
    _, lane = LANES["scene"]
    assert lane.active is False


# --------------------------------------------------------------------------- #
# 牺牲顺序：当前 vs 目标
# --------------------------------------------------------------------------- #


def test_current_sacrifice_order_matches_pre_refactor_behavior() -> None:
    """**行为不变证据**：运行时次序必须与重构前手写的那一份逐项相同。"""
    assert SACRIFICE_ORDER == SACRIFICE_ORDER_CURRENT
    assert SACRIFICE_ORDER == (
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


def test_target_sacrifice_order_matches_spec() -> None:
    """设计文档 §2 的目标次序（供 PR 3 验收）。"""
    assert SACRIFICE_ORDER_TARGET == (
        "capabilities",
        "goals",
        "media",
        "scenario",
        "world",
        "relationship",
        "self_state",
        "knowledge",
        "evidence",
        "core_memory",
    )


def test_target_and_current_cover_the_same_lanes() -> None:
    """两套次序只是排列不同，**集合必须相同**。

    这条防的是"目标表里漏了一条车道"——漏掉的后果是那条车道在 PR 3
    之后永远不会被软上限淘汰。
    """
    assert set(SACRIFICE_ORDER_TARGET) == set(SACRIFICE_ORDER_CURRENT)


def test_target_diff_from_current_is_exactly_six_positions() -> None:
    """目标与现状的差异逐项写出来，作为 PR 3 的验收清单。

    如果将来有人无意改动注册表，这里会立刻红，而不用等到 PR 3 才发现
    "目标顺序变了但没人说要变"。
    """
    diffs = [
        (idx, cur, tgt)
        for idx, (cur, tgt) in enumerate(
            zip(SACRIFICE_ORDER_CURRENT, SACRIFICE_ORDER_TARGET, strict=True)
        )
        if cur != tgt
    ]
    assert diffs == [
        (3, "world", "scenario"),
        (4, "scenario", "world"),
        (5, "knowledge", "relationship"),
        (6, "evidence", "self_state"),
        (7, "relationship", "knowledge"),
        (8, "self_state", "evidence"),
    ]


def test_sacrifice_order_is_a_deterministic_function() -> None:
    assert sacrifice_order() == sacrifice_order() == SACRIFICE_ORDER_TARGET


def test_sacrifice_covers_every_savable_lane() -> None:
    """**关键不变量**：所有可牺牲车道都在表里。

    漏一条的症状不是报错，而是那条车道在超软上限时**永远不会被整条牺牲**，
    一直占着预算直到撞硬上限被从中间截断——正是 `context.py:1804` 那段
    旧注释警告过的故障。现在由 `validate_registry()` 在导入期拦下。
    """
    savable = {
        name
        for name, (zone, lane) in LANES.items()
        if zone.kind is Kind.SYSTEM and lane.active and not lane.protected
    }
    assert set(SACRIFICE_ORDER) == savable


def test_protected_lanes_never_appear_in_sacrifice_order() -> None:
    for name in PROTECTED_LANES:
        assert name not in SACRIFICE_ORDER, f"受保护车道 {name} 出现在牺牲表里"


def test_disabled_lanes_never_appear_in_sacrifice_order() -> None:
    assert "scene" not in SACRIFICE_ORDER


def test_most_savable_zone_is_capabilities() -> None:
    """`capabilities` 最先让位：渠道侧还能再确认一次。"""
    assert SACRIFICE_ORDER[0] == "capabilities"
    zone = lane_zone("capabilities")
    assert zone is not None
    assert zone.shed_rank == 1


def test_identity_zone_is_never_shed() -> None:
    """身份区整区不参与牺牲。"""
    identity = next(z for z in ZONES if z.id == "identity")
    assert identity.shed_rank is None
    assert {lane.name for lane in identity.lanes} == {"persona", "constraints"}


def test_short_memory_is_protected_within_memory_zone() -> None:
    """记忆区里 `short_memory` 受保护，即使整区被牺牲也不整条丢。"""
    memory = next(z for z in ZONES if z.id == "memory")
    short = next(lane for lane in memory.lanes if lane.name == "short_memory")
    assert short.protected is True
    assert short.shed_order is None


def test_inside_zone_shed_order_differs_from_message_order() -> None:
    """区内「阅读顺序」和「牺牲顺序」故意不同。

    `situation` 里消息是 `self_state → relationship`，但牺牲时
    `relationship` 先走（关系比此刻心情更值得留）。两条顺序分开存，
    就是为了能表达这件事。
    """
    situation = next(z for z in ZONES if z.id == "situation")
    by_message = [
        lane.name for lane in sorted(situation.lanes, key=lambda spec: spec.message_order)
    ]
    by_shed = [
        lane.name
        for lane in sorted(
            (lane for lane in situation.lanes if lane.shed_order is not None),
            key=lambda spec: spec.shed_order or 0,
        )
    ]
    assert by_message == ["world", "self_state", "relationship"]
    assert by_shed == ["world", "relationship", "self_state"]


# --------------------------------------------------------------------------- #
# 当前实际发送顺序（防它变乱）
# --------------------------------------------------------------------------- #


def test_observed_append_order_only_uses_registered_lanes() -> None:
    """记录下来的实际顺序里不许出现未登记的车道名。"""
    assert set(OBSERVED_APPEND_ORDER) <= set(LANES)


def test_observed_append_order_starts_with_identity() -> None:
    """人设永远最先注入。"""
    assert OBSERVED_APPEND_ORDER[:2] == ("persona", "constraints")


def test_observed_append_order_ends_with_capabilities() -> None:
    """能力说明现在最后注入（靠近正文），这也是它最先被牺牲的原因。"""
    assert OBSERVED_APPEND_ORDER[-1] == "capabilities"


def test_observed_append_order_has_duplicates_that_are_documented() -> None:
    """同一车道多次出现是**已知现状**，不是新问题。

    这条测试的作用：如果 PR 3 把重复合并了，这里会红，提醒同步更新
    `OBSERVED_APPEND_ORDER` 和本文件的对照断言。
    """
    from collections import Counter

    counts = Counter(OBSERVED_APPEND_ORDER)
    assert counts["capabilities"] == 4
    assert counts["self_state"] == 3
    assert counts["short_memory"] == 2
    assert counts["media"] == 2


def test_observed_vs_target_message_order_differs_only_inside_groups() -> None:
    """目标是"同区内按区域排"，当前是"生产者各自 append"。

    断言的是**区域级别的相对次序**：`identity` 一定在最前、
    `capabilities` 一定在记忆类之后（当前也是），所以两者的宏观骨架一致，
    差别只在 `situation` / `memory` 两区内部的交错。
    """
    first_zone_of: dict[str, int] = {}
    for idx, lane in enumerate(OBSERVED_APPEND_ORDER):
        zone = lane_zone(lane)
        assert zone is not None
        first_zone_of.setdefault(zone.id, idx)

    # 人设最先
    assert first_zone_of["identity"] == 0
    # 能力说明在记忆类之后
    assert first_zone_of["capabilities"] > first_zone_of["memory"]


# --------------------------------------------------------------------------- #
# 校验函数本身：写错的注册表必须被抓住
# --------------------------------------------------------------------------- #


def _zone(**overrides: object) -> ZoneSpec:
    base = ZoneSpec(
        "probe",
        "探针",
        "测试用",
        Kind.SYSTEM,
        5,
        9,
        "system",
        (LaneSpec("probe_lane", 100, 10, 10),),
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _replace_zone(zone_id: str, **overrides: object) -> tuple[ZoneSpec, ...]:
    out = []
    for zone in ZONES:
        out.append(dataclasses.replace(zone, **overrides) if zone.id == zone_id else zone)  # type: ignore[arg-type]
    return tuple(out)


def _replace_lane(zone_id: str, lane_name: str, **overrides: object) -> tuple[ZoneSpec, ...]:
    out = []
    for zone in ZONES:
        if zone.id != zone_id:
            out.append(zone)
            continue
        lanes = tuple(
            dataclasses.replace(lane, **overrides) if lane.name == lane_name else lane  # type: ignore[arg-type]
            for lane in zone.lanes
        )
        out.append(dataclasses.replace(zone, lanes=lanes))
    return tuple(out)


def test_duplicate_zone_id_is_rejected() -> None:
    zones = (*ZONES, _zone(id="identity", message_order=999, shed_rank=99))
    with pytest.raises(ValueError, match="区域 id 重复"):
        validate_registry(zones)


def test_duplicate_zone_position_is_rejected() -> None:
    zones = (*ZONES, _zone(id="probe", message_order=10, shed_rank=99))
    with pytest.raises(ValueError, match="区域消息位置重复"):
        validate_registry(zones)


def test_empty_zone_is_rejected() -> None:
    zones = (*ZONES[:-1], dataclasses.replace(ZONES[-1], lanes=()))
    with pytest.raises(ValueError, match="没有车道"):
        validate_registry(zones)


def test_unknown_ui_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="展示 key 未登记"):
        validate_registry(_replace_zone("plan", legacy_ui_key="nowhere"))


def test_duplicate_shed_rank_is_rejected() -> None:
    with pytest.raises(ValueError, match="牺牲优先级重复"):
        validate_registry(_replace_zone("media", shed_rank=2))  # plan 已是 2


def test_system_zone_without_shed_rank_is_rejected() -> None:
    with pytest.raises(ValueError, match="区域缺少牺牲优先级"):
        validate_registry(_replace_zone("media", shed_rank=None))


def test_structural_zone_may_not_have_shed_rank() -> None:
    with pytest.raises(ValueError, match="区域缺少牺牲优先级|牺牲优先级"):
        validate_registry(_replace_zone("recap", shed_rank=7))


def test_duplicate_lane_name_across_zones_is_rejected() -> None:
    """同名车道出现在两个区域 -> 预算归属未定义。"""
    probe = _zone(id="probe", message_order=75, shed_rank=99)
    probe = dataclasses.replace(
        probe, lanes=(LaneSpec("persona", 100, 10, 10),)
    )
    with pytest.raises(ValueError, match="车道名重复"):
        validate_registry((*ZONES, probe))


def test_protected_lane_with_shed_order_is_rejected() -> None:
    """受保护车道却登记了牺牲位置——语义自相矛盾。"""
    with pytest.raises(ValueError, match="保护状态与区内牺牲位置不符"):
        validate_registry(_replace_lane("memory", "short_memory", shed_order=99))


def test_unprotected_lane_without_shed_order_is_rejected() -> None:
    """不可牺牲又没标保护 -> 它永远不会被丢，但也没人知道为什么。"""
    with pytest.raises(ValueError, match="保护状态与区内牺牲位置不符"):
        validate_registry(_replace_lane("media", "media", shed_order=None))


def test_system_lane_without_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="必须有正预算"):
        validate_registry(_replace_lane("media", "media", budget_tokens=None))


def test_zero_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="必须有正预算"):
        validate_registry(_replace_lane("media", "media", budget_tokens=0))


def test_disabled_lane_with_budget_is_rejected() -> None:
    """禁发车道占预算 -> 总预算会莫名变大。"""
    with pytest.raises(ValueError, match="禁发车道不得占用预算"):
        validate_registry(_replace_lane("scene", "scene", budget_tokens=100))


def test_structural_lane_cannot_be_protected() -> None:
    with pytest.raises(ValueError, match="结构车道不得使用 system 保护标志"):
        validate_registry(_replace_lane("input", "input", protected=True))


def test_structural_lane_cannot_have_shed_order() -> None:
    with pytest.raises(ValueError, match="结构车道不得参与牺牲"):
        validate_registry(_replace_lane("input", "input", shed_order=10))


def test_missing_structural_zone_is_rejected() -> None:
    zones = tuple(z for z in ZONES if z.kind is not Kind.INPUT)
    with pytest.raises(ValueError, match="缺少结构区域"):
        validate_registry(zones)


def test_lane_dropped_from_sacrifice_coverage_is_rejected() -> None:
    """把一条车道标成 protected -> 它仍在 sacrifice 覆盖里，但会破坏别的不变量。

    这里验证的是**反向**：如果某条车道既不在牺牲表、也不受保护，
    校验必须报错。做法是把一条车道改成 active=False 又不给预算冲突——
    行不通，于是直接构造一个"可牺牲却排不进表"的场景：
    给 system 区加一条既非 protected 又无 shed_order 的车道。
    """
    with pytest.raises(ValueError):
        validate_registry(_replace_lane("media", "media", shed_order=None))


def test_structural_order_violation_is_rejected() -> None:
    """摘要在历史之后 -> 顺序错乱。"""
    zones = []
    for zone in ZONES:
        if zone.kind is Kind.SUMMARY:
            zones.append(dataclasses.replace(zone, message_order=95))
        else:
            zones.append(zone)
    with pytest.raises(ValueError, match="消息顺序不符|位置重复"):
        validate_registry(tuple(zones))


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #


def test_require_lane_returns_zone_and_lane() -> None:
    zone, lane = require_lane("persona")
    assert zone.id == "identity"
    assert lane.name == "persona"
    assert lane.budget_tokens == 100_000


def test_require_lane_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="未知的提示词车道"):
        require_lane("no_such_lane")


def test_require_lane_rejects_disabled() -> None:
    """`scene` 在契约里存在但禁发。**不要**静默降级成 `other` 继续发送。"""
    with pytest.raises(ValueError, match="已禁发"):
        require_lane("scene")


def test_require_lane_enforces_kind() -> None:
    with pytest.raises(ValueError, match="消息类型不符"):
        require_lane("history", kind=Kind.SYSTEM)
    assert require_lane("history", kind=Kind.HISTORY)[1].name == "history"


def test_lane_zone_returns_none_for_unknown() -> None:
    assert lane_zone("nope") is None
    assert lane_zone("persona") is not None


def test_gate_enum_is_closed() -> None:
    """门槛是一组固定原语，不许临时起名。"""
    assert {gate.value for gate in Gate} == {
        "persona_present",
        "scenario_override_present",
        "audience_allowed",
        "poke_supported",
        "recallable",
        "private_chat",
    }
    scenario = LANES["scenario"][1]
    assert Gate.PERSONA_PRESENT in scenario.gates
    assert Gate.SCENARIO_OVERRIDE_PRESENT in scenario.gates


# --------------------------------------------------------------------------- #
# 车道预算超额检测（给日志用）
# --------------------------------------------------------------------------- #


def test_budget_overruns_flags_a_lane_over_its_own_budget() -> None:
    """`capabilities` 预算 300 token = 600 字符。塞 1000 就是超了。"""
    overruns = budget_overruns({"capabilities": 1000})
    assert len(overruns) == 1
    zone_id, lane, chars, budget_chars = overruns[0]
    assert zone_id == "capabilities"
    assert lane == "capabilities"
    assert chars == 1000
    assert budget_chars == 600


def test_budget_overruns_is_silent_within_budget() -> None:
    assert budget_overruns({"capabilities": 600}) == []
    assert budget_overruns({"capabilities": 100}) == []


def test_budget_overruns_sorts_by_overflow_desc() -> None:
    overruns = budget_overruns({"capabilities": 700, "persona": 100_000_001})
    assert [item[1] for item in overruns] == ["persona", "capabilities"]


def test_budget_overruns_ignores_structural_lanes() -> None:
    """结构车道不参与车道超额比较。

    它们的 `budget_tokens` 是窗口目标值，不是"在总预算里占多少"。
    把摘要算进超额会让日志报出"摘要超了 400 token"这种无意义告警——
    摘要超长由它自己的滚动策略处理，跟软／硬上限没关系。
    """
    assert budget_overruns({"history": 10**9, "summary": 10**9, "input": 10**9}) == []
    # 但 system 车道照常能报出来，证明上面不是因为整个函数空转
    assert budget_overruns({"knowledge": 10**9}) != []


def test_budget_overruns_ignores_unknown_lanes() -> None:
    assert budget_overruns({"no_such_lane": 10**9}) == []


def test_budget_overruns_uses_two_chars_per_token() -> None:
    """换算口径必须与 `_total_soft_hard()` 一致，否则日志会自相矛盾。"""
    from ling.kernel.context import _total_soft_hard

    soft, _ = _total_soft_hard()
    assert soft == sum(DEFAULT_BUDGETS.values()) * 2
