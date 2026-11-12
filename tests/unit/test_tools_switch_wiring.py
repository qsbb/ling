"""同轮工具调用的配置往返与能力门。"""

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


def _ledger(*, enabled: bool | None, registry: object, declared: bool = True, config=None):  # noqa: ANN001, ANN202
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


# ------------------------------------------ 7. 按轮读取已发布配置


def test_published_setting_selects_registry_for_each_turn() -> None:
    coordinator = TurnCoordinator(
        router=_NoopRouter(declared=True),  # type: ignore[arg-type]
        actor_id="actor_ling",
        persona_id=None,
        cancels=object(),  # type: ignore[arg-type]
    )
    for enabled in (False, True, False):
        ledger = coordinator._tool_ledger_for_turn(
            config=_config(tools_enabled=enabled), profile=WIRED_PROFILE
        )
        assert ledger.enabled is enabled
        assert (ledger.registry is not None) is enabled
        assert {tool.name for tool in ledger.availability.tools} == (
            {CLOCK_NOW_TOOL_ID} if enabled else set()
        )


def test_enabled_setting_with_unsupported_instance_sends_no_tools() -> None:
    ledger = _ledger(
        enabled=None, registry=None, declared=False, config=_config(tools_enabled=True)
    )
    assert ledger.availability.reason == "instance_has_no_native_tools"
    assert ledger.availability.tools == ()


def test_default_registry_never_allows_irreversible_by_default() -> None:
    from ling.capabilities.tools import RECALL_TOOL_ID, ToolRegistry, ToolRisk, ToolSpec

    registry = _ledger(
        enabled=None, registry=None, config=_config(tools_enabled=True)
    ).registry
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


@pytest.mark.parametrize("has_tool_history", [False, True])
async def test_router_does_not_send_native_tools_to_incapable_fallback(has_tool_history: bool) -> None:
    from ling.models.registry import ProviderRegistry
    from ling.models.router import ModelRouter
    from ling.ports.model import (
        ChatMessageIn,
        ModelChunk,
        ModelChunkKind,
        ModelError,
        ModelErrorKind,
        ToolDefinition,
    )

    requests = []

    class Main:
        kind = "openai_compatible"
        supports_native_tools = True

        async def stream(self, request):
            if not request.tools:
                yield ModelChunk(delta="unexpected")
                return
            raise ModelError(ModelErrorKind.unavailable, "fixture primary failed", retriable=True)

    class Fallback:
        kind = "openai_compatible"
        supports_native_tools = False

        async def stream(self, request):
            requests.append(request)
            yield ModelChunk(kind=ModelChunkKind.tool_call, provider_call_id="unadvertised")
            yield ModelChunk(delta="fallback")

    class Registry(ProviderRegistry):
        def get(self, cfg):
            return Main() if cfg.provider_id == "main" else Fallback()

    config = EffectiveConfig(
        revision=1,
        providers={
            provider_id: ProviderConfig(provider_id=provider_id, kind="openai_compatible", enabled=True)
            for provider_id in ("main", "fallback")
        },
        profiles={
            WIRED_PROFILE: ProfileBinding(
                profile=WIRED_PROFILE,
                provider_id="main",
                model_id="m1",
                fallback_provider_id="fallback",
                fallback_model_id="m2",
            ),
        },
    )
    messages = [ChatMessageIn(role="user", content="clock")]
    if has_tool_history:
        messages.append(ChatMessageIn(role="tool", content="result", provider_call_id="fixture-id"))
    router = ModelRouter(Registry(lambda _: None))
    stream = router.stream(
        config, profile=WIRED_PROFILE, messages=messages,
        tools=(ToolDefinition(name=CLOCK_NOW_TOOL_ID, description="Clock"),),
        tool_choice="auto",
        parallel_tool_calls=True,
    )
    if has_tool_history:
        with pytest.raises(ModelError, match="fixture primary failed"):
            async for _ in stream:
                pass
        assert not requests
    else:
        assert [chunk.delta async for chunk, _ in stream] == ["fallback"]
        assert len(requests) == 1
        assert requests[0].tools == ()
        assert requests[0].tool_choice is None
        assert requests[0].parallel_tool_calls is None
