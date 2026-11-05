"""「回不回」的自主选择：Web 与 QQ 共用同一个选择器。

这一层存在的理由：在这之前"回不回"不是决定，而是**规则的结果**——
私聊有文本必回、群聊被点名必回、普通群聊必沉默。模型从未参与过：
Web 路径想调模型但它读 `app.state.config_service`（**该属性不存在**），
永远静默退回规则；QQ 路径连模型都不问。

本文件钉三件事：

1. **模型真的被调用**（这是给那个死代码 bug 收尸的断言）
2. 模型不可用 → 退回基线，且**如实标记状态**，不假装参与过
3. L0 硬约束（防刷屏上限）模型无权推翻
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ling.kernel.autonomy import AutonomyTurnService, ModelStatus, Situation
from ling.kernel.decision import DecisionContext
from ling.ports.model import ModelChunk, ModelError, ModelErrorKind
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class _Config:
    def __init__(self, **dialogue: object) -> None:
        self._dialogue = dialogue
        self.calls = 0

    async def effective(self):  # noqa: ANN202
        from types import SimpleNamespace

        self.calls += 1
        base = {
            "private_may_stay_silent": True,
            "group_mention_requires_reply": True,
        }
        base.update(self._dialogue)
        return SimpleNamespace(dialogue=SimpleNamespace(**base))


class _Router:
    def __init__(
        self, *, reply: str = '{"respond": true, "reason": "想回", "length": "normal"}',
        error: ModelError | None = None,
    ) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[dict] = []

    async def stream(self, effective, *, profile, messages, **kw) -> AsyncIterator:  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome

        self.calls.append({"profile": profile, "messages": messages})
        if self._error is not None:
            raise self._error
        if self._reply:
            yield ModelChunk(delta=self._reply), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._reply)


def _private() -> DecisionContext:
    return DecisionContext(
        is_group=False, mentioned=False, addressed_to_actor=True, has_meaningful_text=True
    )


def _group(*, mentioned: bool = False, recent: int = 0, budget: int = 3) -> DecisionContext:
    return DecisionContext(
        is_group=True,
        mentioned=mentioned,
        addressed_to_actor=mentioned,
        is_question=False,
        recent_actor_turns=recent,
        group_turn_budget=budget,
        has_meaningful_text=True,
    )


# ---------------------------------------------------------------- 模型真的被调用


async def test_private_calls_model_by_default(logged_in: TestEnv) -> None:  # noqa: ARG001
    """私聊默认会问模型——这是"人不是句句有回应"的开关。

    断言 `calls` 非空是关键：只断言"返回了 reply"抓不住"模型从未被调用"
    这个 bug（上一版就是这样活了很久）。
    """
    cfg, router = _Config(), _Router(reply='{"respond": true, "reason": "接话", "length": "brief"}')
    choice = await AutonomyTurnService(router=router, config_reader=cfg).choose(
        _private(), text="你好"
    )
    assert cfg.calls >= 1, "根本没有读配置"
    assert router.calls, "模型没有被调用"
    assert choice.respond is True
    assert choice.length == "brief"
    assert choice.model_status is ModelStatus.ok


async def test_private_can_stay_silent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型说不回，就真的不回——私聊也一样。"""
    router = _Router(reply='{"respond": false, "reason": "不想接", "length": "normal"}')
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _private(), text="你好"
    )
    assert choice.respond is False
    assert choice.model_status is ModelStatus.ok
    assert choice.baseline == "reply", "基线本来是必回，模型改变了结论"


async def test_private_silence_switch_off_restores_old_behavior(logged_in: TestEnv) -> None:  # noqa: ARG001
    """关掉开关就退回"私聊必回"，且不再调模型。"""
    router = _Router(reply='{"respond": false, "reason": "不想接"}')
    cfg = _Config(private_may_stay_silent=False)
    choice = await AutonomyTurnService(router=router, config_reader=cfg).choose(
        _private(), text="你好"
    )
    assert choice.respond is True
    assert choice.model_status is ModelStatus.not_asked
    assert router.calls == [], "开关关着却仍然调了模型"


async def test_group_mention_switch_is_honored(logged_in: TestEnv) -> None:  # noqa: ARG001
    """被 @ 默认必回；关掉开关后可以选择不回。"""
    router = _Router(reply='{"respond": false, "reason": "忙"}')

    strict = await AutonomyTurnService(
        router=router, config_reader=_Config(group_mention_requires_reply=True)
    ).choose(_group(mentioned=True), text="@ling 在吗")
    assert strict.respond is True
    assert strict.model_status is ModelStatus.not_asked
    assert router.calls == []

    loose = await AutonomyTurnService(
        router=_Router(reply='{"respond": false, "reason": "忙"}'),
        config_reader=_Config(group_mention_requires_reply=False),
    ).choose(_group(mentioned=True), text="@ling 在吗")
    assert loose.respond is False
    assert loose.model_status is ModelStatus.ok


# ---------------------------------------------------------------- 退回与状态


async def test_unbound_role_falls_back_and_says_so(logged_in: TestEnv) -> None:  # noqa: ARG001
    """职责没绑定时退回基线，**但状态如实标记**。

    不能因为"有回退很安全"就把 unbound 显示成 ok——那正是上一版 bug 的成因：
    外面看起来一切正常，实际模型从未参与。
    """
    router = _Router(
        error=ModelError(ModelErrorKind.unsupported, "职责 dialogue.fast 尚未绑定模型")
    )
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _private(), text="你好"
    )
    assert choice.respond is True, "应当退回基线（必回）"
    assert choice.model_status is ModelStatus.unbound
    assert choice.model_participated is False


async def test_garbage_output_falls_back(logged_in: TestEnv) -> None:  # noqa: ARG001
    router = _Router(reply="我觉得应该回一下吧")
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _private(), text="你好"
    )
    assert choice.respond is True
    assert choice.model_status is ModelStatus.error


async def test_missing_router_marks_unbound(logged_in: TestEnv) -> None:  # noqa: ARG001
    choice = await AutonomyTurnService(router=None, config_reader=_Config()).choose(
        _private(), text="你好"
    )
    assert choice.model_status is ModelStatus.unbound
    assert choice.respond is True


async def test_json_wrapped_in_code_fence_is_accepted(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型常把 JSON 包在 ``` 里。剥掉即可，但不要"猜"别的格式。"""
    router = _Router(
        reply='```json\n{"respond": false, "reason": "不相关", "length": "brief"}\n```'
    )
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _private(), text="你好"
    )
    assert choice.respond is False
    assert choice.model_status is ModelStatus.ok


# ---------------------------------------------------------------- L0 硬约束


async def test_frequency_cap_is_never_overridden(logged_in: TestEnv) -> None:  # noqa: ARG001
    """防刷屏上限是 L0。模型说"要回"也没用。

    "再像人也不能刷屏"——这条不能因为要显得自主就放开。
    """
    router = _Router(reply='{"respond": true, "reason": "想说话"}')
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _group(recent=3, budget=3), text="随便聊聊"
    )
    assert choice.respond is False
    assert choice.model_status is ModelStatus.not_asked
    assert router.calls == [], "被 L0 挡住时不该浪费一次模型调用"
    assert "上限" in choice.reason


async def test_plain_group_message_asks_model(logged_in: TestEnv) -> None:  # noqa: ARG001
    """群里没点名 → 弹性区间 → 模型参与（这条路径以前在 QQ 上根本不存在）。"""
    router = _Router(reply='{"respond": true, "reason": "这个话题我懂"}')
    choice = await AutonomyTurnService(router=router, config_reader=_Config()).choose(
        _group(), text="今天好热"
    )
    assert router.calls, "群聊弹性区间没有问模型"
    assert choice.respond is True
    assert choice.model_status is ModelStatus.ok


# ---------------------------------------------------------------- 处境：在休息


def _situation_with_affect(text: str):  # noqa: ANN202
    async def _read() -> Situation:
        return Situation(availability="low", affect_text=text)

    return _read


def _persona_with_traits(text: str, traits: dict):  # noqa: ANN202
    """模拟 make_persona_reader：system_prompt + traits。"""
    from ling.domains.persona.service import format_traits

    async def _read() -> str:
        parts = [text] if text else []
        rendered = format_traits(traits)
        if rendered:
            parts.append(rendered)
        return "\n".join(parts)

    return _read


def _persona(text: str):  # noqa: ANN202
    async def _read() -> str:
        return text

    return _read


def _group_ctx() -> DecisionContext:
    return DecisionContext(
        is_group=True,
        mentioned=False,
        addressed_to_actor=False,
        is_question=False,
        recent_actor_turns=0,
        has_meaningful_text=True,
        group_turn_budget=3,
    )


def _situation(availability: str = "available", load_text: str = "", band: str = "low"):  # noqa: ANN202
    async def _read() -> Situation:
        return Situation(availability=availability, load_band=band, load_text=load_text)

    return _read


async def test_resting_skips_the_model(logged_in: TestEnv) -> None:  # noqa: ARG001
    """在休息时先不进入决策——这就是"没空回消息"。

    这是**时机**问题不是语气问题，所以做成确定性门是合理的：
    模型在提示词里看到"你在休息"仍然很可能选择回，那就不像"没空"了。
    """
    router = _Router()
    choice = await AutonomyTurnService(
        router=router, config_reader=_Config(), situation_reader=_situation("resting")
    ).choose(_private(), text="在吗")
    assert choice.respond is False
    assert choice.model_status is ModelStatus.not_asked
    assert "休息" in choice.reason
    assert router.calls == [], "在休息不该浪费一次模型调用"
    assert choice.availability == "resting"


async def test_resting_skip_can_be_turned_off(logged_in: TestEnv) -> None:  # noqa: ARG001
    """关掉开关就交回模型判断——有人希望她休息也回。"""
    router = _Router(reply='{"respond": true, "reason": "想聊", "length": "normal"}')
    choice = await AutonomyTurnService(
        router=router,
        config_reader=_Config(rest_may_skip=False),
        situation_reader=_situation("resting"),
    ).choose(_private(), text="在吗")
    assert router.calls, "开关关着却仍然没问模型"
    assert choice.respond is True


async def test_urgent_is_never_eaten_by_rest(logged_in: TestEnv) -> None:  # noqa: ARG001
    """紧急求助不能被"在休息"吃掉。

    这不是语气问题，是**功能失败**：把一条求救默默吞掉，和"语气不好"
    不是一个量级。所以宁可误判为紧急，也不要漏掉。
    """
    for text in ("救命", "出事了帮我", "快回我一下", "urgent"):
        router = _Router()
        choice = await AutonomyTurnService(
            router=router,
            config_reader=_Config(),
            situation_reader=_situation("resting"),
        ).choose(_private(), text=text)
        assert router.calls, f"紧急内容「{text}」被休息门吞掉了"
        assert choice.model_status is ModelStatus.ok


async def test_situation_reaches_the_prompt(logged_in: TestEnv) -> None:  # noqa: ARG001
    """处境要进 prompt——只陈述状态，**不规定语气**。

    用户要求行为风格由人设决定，所以这里只给"我此刻：有点累"这类事实，
    怎么写由模型和人设决定。
    """
    router = _Router(reply='{"respond": true, "reason": "ok"}')
    await AutonomyTurnService(
        router=router,
        config_reader=_Config(),
        situation_reader=_situation("low", load_text="这个对话已经有点倦了", band="medium"),
    ).choose(_private(), text="聊聊天")
    assert router.calls
    prompt = router.calls[0]["messages"][1].content
    assert "有点累" in prompt
    assert "有点倦了" in prompt
    for forbidden in ("冷淡", "生气", "你应该"):
        assert forbidden not in prompt, f"prompt 里出现了语气指令：{forbidden}"


async def test_situation_failure_is_neutral(logged_in: TestEnv) -> None:  # noqa: ARG001
    """处境读不到时按中性处理，不假设忙也不假设闲。"""

    async def _boom() -> Situation:
        raise RuntimeError("状态库挂了")

    router = _Router(reply='{"respond": true, "reason": "ok"}')
    choice = await AutonomyTurnService(
        router=router, config_reader=_Config(), situation_reader=_boom
    ).choose(_private(), text="在吗")
    assert choice.model_status is ModelStatus.ok
    assert choice.availability == "available"


# ---------------------------------------------------------------- 人设进决策


def test_choice_prompt_has_no_behavioural_rules() -> None:
    """决策提示词里**不许**写行为规定。

    这里曾经写着「如果消息是别人之间的对话、广告、或已经有其他人回答了，
    通常不该插话」——那是把一种性格（谨慎、不抢话）写死进代码。
    爱说话的人在群里恰恰会插嘴，而那应该由人设决定。

    用户明确要求：「这类都要根据人设来，我想给 llm 一个高自由度」。
    这条测试是那道闸：谁再往提示词里加"通常不该 X"，这里就红。
    """
    from ling.kernel.autonomy import CHOICE_PROMPT

    forbidden = [
        "通常不该",
        "不要插话",
        "不该插话",
        "不要回复",
        "必须回复",
        "不应该回",
        "别插嘴",
    ]
    for phrase in forbidden:
        assert phrase not in CHOICE_PROMPT, f"提示词里写死了行为规定：{phrase}"


async def test_persona_is_injected_into_choice_prompt(logged_in: TestEnv) -> None:  # noqa: ARG001
    """人设要进决策 prompt——否则"要不要开口"是在没有性格的情况下判断的。"""
    router = _Router(reply='{"respond": false, "reason": "懒得说"}')
    await AutonomyTurnService(
        router=router,
        config_reader=_Config(),
        persona_reader=_persona("你在群里话多，爱接话，喜欢吐槽。"),
    ).choose(_group_ctx(), text="大家在聊周末去哪")
    assert router.calls
    system = router.calls[0]["messages"][0].content
    assert "爱接话" in system, "人设没进 system prompt"
    assert "喜欢吐槽" in system


async def test_persona_reaches_group_silence_decision(logged_in: TestEnv) -> None:  # noqa: ARG001
    """人设和"回不回"的结论在同一段 system 里，不是分开两条 message。

    模型对单条 system 的遵循度明显更高，而这里最需要它真的按人设来。
    """
    router = _Router(reply='{"respond": true, "reason": "想搭话"}')
    choice = await AutonomyTurnService(
        router=router,
        config_reader=_Config(),
        persona_reader=_persona("你话很多。"),
    ).choose(_group_ctx(), text="今天好热")
    assert len(router.calls[0]["messages"]) == 2, "应该是 system + user 两条"
    assert choice.respond is True


async def test_no_persona_does_not_invent_one(logged_in: TestEnv) -> None:  # noqa: ARG001
    """读不到人设就**不编默认性格**。

    编默认性格比"没有人设"更糟：没人会想到去改它，而它会一直生效。
    """
    from ling.kernel.autonomy import CHOICE_PROMPT

    router = _Router(reply='{"respond": false, "reason": "x"}')
    await AutonomyTurnService(
        router=router, config_reader=_Config(), persona_reader=_persona("")
    ).choose(_group_ctx(), text="随便说点什么")
    system = router.calls[0]["messages"][0].content
    assert system == CHOICE_PROMPT, "空人设却改变了 system prompt"


# ---------------------------------------------------------------- traits 进决策


async def test_traits_reach_the_choice_prompt(logged_in: TestEnv) -> None:  # noqa: ARG001
    """`traits` 必须进"要不要开口"的判断。

    它一直是**存了但没人读**的字段：人设页有专门的 JSON 编辑器，
    而提示词里只有 system_prompt 和 boundaries。用户在那儿写
    `{"group_chat": "群里爱搭话"}` 完全无效。
    """
    router = _Router(reply='{"respond": false, "reason": "x"}')
    await AutonomyTurnService(
        router=router,
        config_reader=_Config(),
        persona_reader=_persona_with_traits(
            "你是一个角色。", {"group_chat": "群里很爱搭话"}
        ),
    ).choose(_group_ctx(), text="大家在聊周末去哪")
    assert router.calls
    system = router.calls[0]["messages"][0].content
    assert "group_chat" in system, f"traits 没进 prompt：{system[:200]}"
    assert "爱搭话" in system


async def test_empty_traits_add_nothing(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有 traits 时不能凭空多一段空话。"""
    router = _Router(reply='{"respond": false, "reason": "x"}')
    await AutonomyTurnService(
        router=router, config_reader=_Config(), persona_reader=_persona("你是一个角色。")
    ).choose(_group_ctx(), text="随便说点什么")
    system = router.calls[0]["messages"][0].content
    assert "性格倾向" not in system, system[:200]


async def test_real_persona_reader_includes_traits(logged_in: TestEnv) -> None:  # noqa: ARG001
    """用**真的** make_persona_reader 验一遍。

    上面那条用的是手写的假 reader，只能证明"我写的辅助函数会拼 traits"，
    证明不了生产路径会。这条直接读库。
    """
    from ling.domains.persona.service import PersonaService
    from ling.infra.db import session_scope
    from ling.infra.security import new_id
    from ling.kernel.autonomy import make_persona_reader

    pid = new_id("persona")
    async with session_scope() as session:
        svc = PersonaService(session)
        created = await svc.ensure_default()
        # 复制一份默认人设，改 traits，避免污染测试环境的默认行
        from ling.infra.models import Persona

        session.add(
            Persona(
                id=pid,
                revision=1,
                name="probe",
                display_name="probe",
                system_prompt=created.system_prompt,
                traits={"group_chat": "群里很爱搭话"},
                boundaries=list(created.boundaries),
                published=True,
            )
        )

    async with session_scope() as session:
        text = await make_persona_reader(session, persona_id=pid)()
    assert "group_chat" in text, f"真的 reader 没带 traits：{text[:200]}"
    assert "爱搭话" in text


# ---------------------------------------------------------------- 情绪进决策


def test_affect_is_a_situation_field() -> None:
    """情绪必须是 `Situation` 的字段，否则"要不要开口"对情绪完全免疫。"""
    import dataclasses

    from ling.kernel.autonomy import Situation

    names = {f.name for f in dataclasses.fields(Situation)}
    assert "affect_text" in names, f"Situation 里没有情绪：{names}"


async def test_affect_reaches_the_choice_prompt(logged_in: TestEnv) -> None:  # noqa: ARG001
    """有情绪时要在判断依据里出现，且**只陈述状态**、不规定该怎么用。"""
    router = _Router(reply='{"respond": false, "reason": "累"}')
    await AutonomyTurnService(
        router=router,
        config_reader=_Config(),
        situation_reader=_situation_with_affect("有点低落"),
    ).choose(_private(), text="聊聊天吧")
    prompt = router.calls[0]["messages"][1].content
    assert "有点低落" in prompt, f"情绪没进判断依据：{prompt[:300]}"
    for forbidden in ("应该", "必须", "不许"):
        assert forbidden not in prompt, f"prompt 里出现了语气指令：{forbidden}"


async def test_context_line_does_not_forbid_affect_from_affecting_judgement(
    logged_in: TestEnv,
) -> None:
    """上下文里那句"情绪不影响判断"必须已经删掉。

    那是**代码在替人设决定**"情绪不许影响要不要回"——而用户的明确要求
    正好相反：「情绪只影响回复意愿和简短程度，这类都要根据人设来」。

    权限那半句要保留：情绪不该改变谁能看什么，那是正确性。

    这里查**产出的提示文本**而不是源码：源码里留着解释这条历史的注释，
    搜源码会打到注释上（第一次就是这么红的）。
    """
    from ling.domains.affect.service import AffectService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await AffectService(session, actor_id="actor_ling").nudge(
            "tired", 0.6, cause="测试"
        )

    from tests.integration.test_core_memory import _compile, _window

    text = await _compile(logged_in, await _window(logged_in))
    # **必须先确认那句话真的产生了**，否则这条测试是空的——
    # 情绪没注入时也能通过，等于什么都没验。
    assert "你现在的状态" in text, f"情绪没注入上下文，这条测试失去意义：{text[:300]}"
    assert "不影响判断" not in text, (
        "写死的「情绪不影响判断」又出现了——那是在替人设做决定"
    )
    assert "不改变任何权限" in text, "权限那半句被误删了"
