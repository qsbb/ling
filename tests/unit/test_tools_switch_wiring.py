"""原生工具回路的**生产接线**：开关、注册表、以及五种"不可用"必须各自可区分。

## 这个文件钉的是什么

`kernel/turn.py` 从落地起就有一套完整的同轮工具回路（`_ToolRounds` 普通轮与
工具轮共用同一个循环、两道能力闸、角色白名单），但 ``tool_calls_enabled``
**全仓零调用点**——线上她永远不会调用任何工具。这个文件守的是"接上线之后
它仍然可以是关的，而且关得有理由"：

1. 不配置 → ``switch_off``（**默认关**，不是接线时顺手打开）；
2. 开了但没给注册表 → ``no_registry``（"只给开关"是一个独立的成因）；
3. 开了、给了注册表，角色不在白名单 → ``profile_not_wired``；
4. 开了、给了注册表、角色在白名单，供应商 kind 没有编解码 →
   ``kind_has_no_native_tools``；
5. 配置项本身能存能读（``voice_willingness`` 那个坑：`DialoguePolicyIn` 不声明
   → pydantic 静默丢弃 → "打开了、保存成功、刷新回到关"）。

## 为什么第 1-4 条要分别单测而不是一条端到端

四种成因的**修法不同**（改配置 / 注入注册表 / 换角色 / 换供应商），压成一个
布尔之后，线上表现统一是"她从来不用工具"，谁都查不出是哪一环。所以每条都
断言 ``reason`` 的**字面值**，而不只是 ``enabled is False``——后者在四种成因
下都是 ``False``，它证明不了任何事。
"""

from __future__ import annotations

import pytest

from ling.api.routes.settings import DialoguePolicyIn
from ling.capabilities.tools import CLOCK_NOW_TOOL_ID, build_default_registry
from ling.kernel.turn import TurnCoordinator
from ling.ports.config import (
    DialoguePolicy,
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
)
from ling.worker import tool_registry_for_turn

#: 一个在角色白名单里（`ports/model.py::NATIVE_TOOLS_PROFILES`）的角色。
WIRED_PROFILE = "dialogue.fast"

#: 一个**不在**白名单里的角色。看视频/记忆提炼各有自己的生成循环，本期没接
#: 工具回路——给它们声明可用会让"这一轮有工具"变成一句空话。
UNWIRED_PROFILE = "memory.extract"


class _NoopRouter:
    """只回答"这一轮能不能用工具"的桩 router。

    真实的 `ModelRouter` 也是这么被 `turn.py` 问的（`native_tools_support`），
    这里不需要生成能力：本文件测的是**闸门**，不是生成。
    """

    def __init__(self, *, declared: bool) -> None:
        self._declared = declared
        self.calls: list[tuple[str, str]] = []

    def native_tools_support(self, config: EffectiveConfig, profile: str) -> bool:
        self.calls.append((config.revision.__str__(), profile))
        return self._declared


def _config(*, kind: str = "openai_compatible", tools_enabled: bool = False) -> EffectiveConfig:
    """一份最小但**完整**的生效配置：一个供应商 + 一条 dialogue.fast 绑定。

    kind 默认 `openai_compatible`（本仓库唯一实现了工具编解码的适配器），
    这样第 1-3 条不会因为 kind 闸先出局、从而测不到自己要测的那一道。
    """
    return EffectiveConfig(
        revision=7,
        providers={
            "p1": ProviderConfig(provider_id="p1", kind=kind, enabled=True),
        },
        profiles={
            WIRED_PROFILE: ProfileBinding(profile=WIRED_PROFILE, provider_id="p1", model_id="m1"),
            UNWIRED_PROFILE: ProfileBinding(
                profile=UNWIRED_PROFILE, provider_id="p1", model_id="m1"
            ),
        },
        test_mode=True,
        dialogue=DialoguePolicy(tools_enabled=tools_enabled),
    )


def _ledger(*, enabled: bool, registry: object, declared: bool = True, config=None):  # noqa: ANN001, ANN202
    coordinator = TurnCoordinator(
        router=_NoopRouter(declared=declared),  # type: ignore[arg-type]
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=object(),  # type: ignore[arg-type] - 本文件不走生成
        tool_calls_enabled=enabled,
        tool_registry=registry,  # type: ignore[arg-type]
    )
    return coordinator._tool_ledger_for_turn(config=config or _config(), profile=WIRED_PROFILE)


# ------------------------------------------------------- 1. 默认（不配置）


def test_default_config_is_switch_off() -> None:
    """不配置 → ``switch_off``。

    这是"默认关"的可执行形式。变异验证：把 `DialoguePolicy.tools_enabled`
    的默认值改成 ``True``，这条会红（``reason`` 变成别的值）。
    """
    assert DialoguePolicy().tools_enabled is False

    ledger = _ledger(enabled=False, registry=build_default_registry())
    assert ledger.availability.enabled is False
    assert ledger.availability.reason == "switch_off"
    # 只用来记审计的两个字段仍然要填得上——否则日志里"到底是哪个模型没成"
    # 就只剩一句 switch_off。
    assert ledger.availability.provider_kind == "openai_compatible"
    assert ledger.availability.model_id == "m1"


def test_switch_off_wins_over_everything_else() -> None:
    """开关关着时，别的条件再好也不会有工具。

    反例输入的意义：如果实现是"只要注册表和角色都对就给工具"，这条会红。
    """
    coordinator_ledger = _ledger(
        enabled=False,
        registry=build_default_registry(),
        declared=True,
    )
    assert coordinator_ledger.availability.tools == ()


# ------------------------------------------------------- 2. 开了但没注册表


def test_enabled_without_registry_is_no_registry() -> None:
    """开了开关、但没给注册表 → ``no_registry``，**不是** ``switch_off``。

    两者都要修，但修的不是同一件事：前者去注入注册表，后者去打开设置。
    如果实现把 `False` 与"给了空注册表"混成一个值，这条会红。
    """
    assert _ledger(enabled=True, registry=None).availability.reason == "no_registry"


# ------------------------------------------------------- 3. 角色不在白名单


def test_wired_profile_only() -> None:
    """开了、有注册表、但角色不在白名单 → ``profile_not_wired``。

    注意这一条的 kind 是 ``openai_compatible``、router 也声明支持——也就是
    **只有角色这一道**能让它出局。若把 `NATIVE_TOOLS_PROFILES` 放宽成
    "全部角色"，这条会红。
    """
    coordinator = TurnCoordinator(
        router=_NoopRouter(declared=True),  # type: ignore[arg-type]
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=object(),  # type: ignore[arg-type]
        tool_calls_enabled=True,
        tool_registry=build_default_registry(),
    )
    ledger = coordinator._tool_ledger_for_turn(config=_config(), profile=UNWIRED_PROFILE)
    assert ledger.availability.enabled is False
    assert ledger.availability.reason == "profile_not_wired"


# ------------------------------------------------------- 4. 供应商 kind 不支持


@pytest.mark.parametrize("kind", ["anthropic", "chat_responses", "fake"])
def test_kind_without_codec_is_kind_has_no_native_tools(kind: str) -> None:
    """角色对、开关开、注册表也有，但 kind 没有编解码 → ``kind_has_no_native_tools``。

    三个 kind 一起参数化，是因为它们的**修法相同**（换供应商类型），但成因
    不同：两个是"协议有工具、本仓库适配器未实现"，一个是"根本没有这个概念"。

    这一条的打点很关键：`_config(kind=...)` 把 provider 的 kind 换掉，而
    router 仍声明支持。如果实现先问 router 再问 kind，这里会变成
    ``instance_has_no_native_tools``（因为 router 桩没看 kind）——两条断言
    一起钉住了**顺序**。
    """
    ledger = _ledger(
        enabled=True,
        registry=build_default_registry(),
        declared=True,
        config=_config(kind=kind, tools_enabled=True),
    )
    assert ledger.availability.enabled is False
    assert ledger.availability.reason == "kind_has_no_native_tools"


# -------------------------------------------- 5. 全部打通时确实是"可用"


def test_all_gates_open_exposes_only_clock_now() -> None:
    """五道闸全过 → 可用，且**只有** `clock.now` 对模型可见。

    "可用"本身要有正向断言，否则上面四条负向断言在"功能从未实现"时也全绿
    （负向断言的信息量 = 它配对的正向断言的信息量）。

    只有 `clock.now` 是需求原文：只读、无副作用、幂等。`shell.readonly`
    能读宿主机状态、`message.recall` 是不可逆动作，都不该出现在模型侧清单里。
    """
    ledger = _ledger(
        enabled=True,
        registry=build_default_registry(),
        declared=True,
        config=_config(tools_enabled=True),
    )
    assert ledger.availability.enabled is True
    assert ledger.availability.reason == ""
    names = {definition.name for definition in ledger.availability.tools}
    assert names == {CLOCK_NOW_TOOL_ID}


# ------------------------------------------------------- 6. 配置项能存能读


def test_dialogue_policy_in_declares_tools_enabled() -> None:
    """`DialoguePolicyIn` **必须**声明 `tools_enabled`。

    这是 `voice_willingness` 那个坑的守卫：`save_draft` 用
    `model_dump(exclude_none=True)` 组装补丁，未声明的字段被 pydantic 静默
    丢弃——症状是"打开了开关、保存成功、刷新回到关"。

    直接问 `model_fields`，而不是构造一个实例再看属性：pydantic 对**未声明**
    的字段根本不会挂到实例上，但"有没有声明"才是这条要钉的事实。
    """
    assert "tools_enabled" in DialoguePolicyIn.model_fields
    body = DialoguePolicyIn(tools_enabled=True)
    assert body.model_dump(exclude_none=True) == {"tools_enabled": True}


def test_unknown_dialogue_field_is_still_silently_dropped() -> None:
    """对照组：**没声明**的字段确实会被静默丢掉。

    没有这一条，「声明了 tools_enabled」这个断言证明不了什么——读者无法
    判断"丢掉"是这个模型的行为，还是只是注释里的一句话。
    """
    body = DialoguePolicyIn.model_validate({"tools_enabled": True, "not_a_field": 1})
    assert "not_a_field" not in body.model_dump(exclude_none=True)


def test_tools_enabled_round_trips_through_settings_serde() -> None:
    """`_dialogue_to_dict` → `_dialogue_policy` 往返保住 True / False。

    **两个方向都要测**：只测 True 的话，"读的时候恒返回默认值"的实现
    （默认刚好是 False）在 True 那条会红，但在"存了 False"那条仍然会绿——
    而 False 正是产线上最常见的值。

    另外必须确认键真的进了 dict：漏了它 `_dialogue_to_dict` 的键集合就不等于
    `DialoguePolicy` 的字段集合，`test_speech_media_contracts.py` 那条守卫会红。
    """
    from ling.settings.service import _dialogue_policy, _dialogue_to_dict

    assert "tools_enabled" in _dialogue_to_dict(DialoguePolicy())

    for value in (True, False):
        policy = DialoguePolicy(tools_enabled=value)
        stored = _dialogue_to_dict(policy)
        assert stored["tools_enabled"] is value
        assert _dialogue_policy(stored).tools_enabled is value


def test_tools_enabled_defaults_to_false_when_absent_from_payload() -> None:
    """payload 里**没有**这个键 → 默认 False（不是"上次的值"、不是 True）。

    旧草稿（本字段落地之前存下来的）会落到这一条：升级之后行为必须不变。
    """
    from ling.settings.service import _dialogue_policy

    assert _dialogue_policy({}).tools_enabled is False
    assert _dialogue_policy({"group_turn_budget": 5}).tools_enabled is False


# ------------------------------------------ 7. worker 真的把参数传下去了


def _code_only(path) -> str:  # noqa: ANN001
    """只保留**代码**行：剥掉注释与 docstring。

    **这不是技巧，是必要条件。** 解释一条禁令必须引用被禁的词——`worker.py`
    的注释里写着 `tool_calls_enabled=...`，朴素字符串搜索会被注释骗过。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = path.read_text(encoding="utf-8").splitlines()
    keep: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and node.end_lineno is not None:
            for lineno in range(node.lineno, node.end_lineno + 1):
                keep.add(lineno)
    return "\n".join(line for i, line in enumerate(lines, 1) if i in keep)


def _coordinator_constructions(node, found):  # noqa: ANN001, ANN202
    import ast

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "TurnCoordinator":
                found.append({kw.arg for kw in child.keywords})
    return found


def test_every_turn_coordinator_in_worker_wires_the_tool_switch() -> None:
    """`worker.py` 里**每一处** `TurnCoordinator(...)` 都必须传这两个参数。

    这是唯一能抓住"接线未做"的守卫，而且必须是**结构级**的：端到端测试自己
    写一份 `TurnCoordinator(...)` 传参时，删掉 worker 里的接线它照样绿——
    已实测（变异 1 未红）。绕过全部调用点的 e2e 证明不了调用点被接上了。

    为什么不用字符串搜索：`worker.py` 的注释里就写着这两个参数名。
    `_code_only` 用 AST 只留代码行，删掉真代码后注释不会让它变绿。

    ## `allow_irreversible` 不在断言里

    它由 `tool_registry_for_turn` → `build_default_registry()` 的默认值决定，
    另有一条测试盯它（`test_default_registry_never_allows_irreversible_by_default`），
    这里只管"参数传没传"。
    """
    from pathlib import Path

    src = _code_only(Path(__file__).resolve().parents[2] / "src/ling/worker.py")
    assert "TurnCoordinator" in src, "守卫没抓到任何构造点——AST 或路径坏了"

    import ast

    found = _coordinator_constructions(ast.parse(src), [])
    assert len(found) == 2, f"期望 worker 里有 2 处 TurnCoordinator 构造，实际 {len(found)}"
    for index, names in enumerate(found):
        assert "tool_calls_enabled" in names, f"第 {index + 1} 处没传 tool_calls_enabled"
        assert "tool_registry" in names, f"第 {index + 1} 处没传 tool_registry"


def test_guard_would_catch_a_reintroduction() -> None:
    """守卫本身要有测试：造一个**没接线**的源码，确认它真能被判成缺参数。

    一个永远为真的守卫比没有守卫更糟——它给人虚假的安全感。
    """
    import ast

    broken = """
def build():
    return TurnCoordinator(router=None, actor_id="a", persona_id=None, cancels=None)


def build_ok():
    return TurnCoordinator(
        router=None,
        tool_calls_enabled=True,
        tool_registry=None,
    )
"""
    found = _coordinator_constructions(ast.parse(broken), [])
    assert len(found) == 2
    assert "tool_calls_enabled" not in found[0], "守卫读不出缺参数的情况"
    assert "tool_calls_enabled" in found[1], "守卫会把接好的也判成缺"


def test_guard_ignores_comments_that_mention_the_parameters() -> None:
    """注释里写参数名**不能**让守卫通过。

    这是"被注释骗过"那个坑的直接对照：`_code_only` 必须真的剥掉注释。
    变异方式：把 `_code_only` 换成直接返回原文，这条会红。
    """
    import ast
    from pathlib import Path

    src = _code_only(Path(__file__).resolve().parents[2] / "src/ling/worker.py")
    assert "MUTATION" not in src

    total = Path(__file__).resolve().parents[2] / "src/ling/worker.py"
    raw = total.read_text(encoding="utf-8")
    # 前提：原文里这两个词**确实**在注释中出现过（否则这条对照没有意义）
    assert raw.count("tool_calls_enabled=") > 0
    # 而只留代码行时，出现的次数只能是真正传参的那几处（2 处 × 1 次）
    assert src.count("tool_calls_enabled=") == 2, src.count("tool_calls_enabled=")
    assert "TurnCoordinator" in src
    assert ast.parse(src) is not None


def test_default_registry_never_allows_irreversible_by_default() -> None:
    """`tool_registry_for_turn` 用的默认集**不允许**不可逆工具被直接执行。

    需求原文：「不可逆动作仍需服务端确认」。所以：
    - 构造参数 `allow_irreversible` 保持默认 False；
    - `message.recall` **可以**在默认集里注册（它经过逐次授权），但它必须
      `requires_confirmation=True`，且**不是** ``model_callable``——
      模型看不到它，就不可能在同一轮里直接执行。
    """
    from ling.capabilities.tools import RECALL_TOOL_ID, ToolRisk, build_default_registry
    from ling.ports.config import DialoguePolicy

    registry = tool_registry_for_turn(
        EffectiveConfig(revision=1, dialogue=DialoguePolicy(tools_enabled=True))
    )
    assert registry is not None

    # 模型侧清单里没有任何不可逆工具
    model_tools = {spec.tool_id for spec in registry.list_model_tools()}
    assert model_tools == {CLOCK_NOW_TOOL_ID}
    assert RECALL_TOOL_ID not in model_tools

    recall = registry.spec(RECALL_TOOL_ID)
    assert recall is not None, "撤回应当仍在默认集里（走确认回路）"
    assert recall.risk is ToolRisk.irreversible
    assert recall.requires_confirmation is True

    # 注册表级的总开关保持关：新建一个注册表时，不可逆工具默认注册不进来。
    from ling.capabilities.tools import ToolRegistry, ToolSpec

    strict = ToolRegistry()
    with pytest.raises(PermissionError):
        strict.register(
            ToolSpec(
                tool_id="danger.delete_all",
                version="1.0.0",
                description="不可逆动作，默认不许注册",
                risk=ToolRisk.irreversible,
                requires_confirmation=True,
                reversible=False,
            ),
            lambda _args: None,  # noqa: ARG005
        )
    assert build_default_registry is not None
