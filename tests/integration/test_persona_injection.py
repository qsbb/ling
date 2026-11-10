"""人设到底有没有进提示词：用**真正发出去的 messages** 断言。

## 为什么这个文件必须存在

用户报的是「现在的对话似乎人设没有被正确注入」。诊断发现**注入链路是好的**
（`ContextCompiler.compile` 每轮都拼 persona 车道），真正会让人以为"没注入"
的是四种**数据侧/截断侧**的问题：

1. `personas.system_prompt` 为空（用户清空过、或行是老版本建的）时，
   原来会发出一条 `content=""` 的 system 消息——快照里那块「人设」是空的，
   看上去就是"没注入"；
2. `format_traits` 有 12 条 / 120 字 / 600 字三道上限，**超了不留任何痕迹**：
   第 13 条永远不出现，218 字的「说话方式」被砍到 127 字且没有省略号；
3. dict/list 形状的 trait 以 Python repr 进提示词（`{'句长': '短'}`）；
4. 空文本的车道仍占一条消息位。

所以这里断言的都不是"函数被调用了"，而是**provider 收到的第一条 system
消息里有什么**——这是唯一能证明"她真的看见了"的位置。

## 2026-11-11：没有默认人设了

用户拍板「可以把默认人设去掉，不填就是直连模型+其他功能提示词，不注入人设」，
空人设时**什么都不注入**。所以本文件里凡是要断言"人设进了提示词"的用例，
都**自己先写一份人设**（`_write_persona`），不再依赖库里有一份默认人设——
依赖默认值会让"默认被删掉"这件事反过来让这些用例红，而它们本来要测的是
**注入链路**，不是默认值。

"空人设 → 人设/角色名/叙事边界/场景三段一条都不发"的用例见 §5。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ling.domains.conversation.service import ConversationService
from ling.domains.persona.service import PersonaService
from ling.infra.db import session_scope
from ling.infra.models import Persona
from ling.kernel.turn import TurnCoordinator
from ling.models.fake import FakeProvider
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
PERSONA = "persona_ling"


def _config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(
                provider_id="fake", kind="fake", enabled=True, extra={"chunk_delay": 0.0}
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="fake", model_id="deterministic"
            )
        },
        test_mode=True,
    )


class _CapturingProvider(FakeProvider):
    """抓 provider 这一层收到的 messages（路由之后、真正发出去的那一份）。"""

    def __init__(self) -> None:
        super().__init__(chunk_delay=0.0)
        self.requests: list[list[object]] = []

    async def stream(self, request):  # noqa: ANN001, ANN201
        self.requests.append(list(request.messages))
        async for chunk in super().stream(request):
            yield chunk


class _Registry(ProviderRegistry):
    def __init__(self) -> None:
        super().__init__(lambda _pid: None)
        self.provider = _CapturingProvider()

    def get(self, cfg):  # noqa: ANN001, ANN201
        return self.provider


async def _run_turn(env: TestEnv, *, visibility: str = "subject_private") -> _CapturingProvider:
    """跑一轮真实轮次，返回抓到的 provider。"""
    response = await env.client.post("/v1/sessions", json={"title": "人设注入"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    if visibility != "subject_private":
        async with session_scope() as session:
            conv = ConversationService(session)
            window = await conv.get_window(window_id, subject_id=env.subject_id)
            window.visibility = visibility
            session.add(window)

    registry = _Registry()
    coordinator = TurnCoordinator(
        router=ModelRouter(registry),
        actor_id=ACTOR,
        persona_id=PERSONA,
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
    )
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text="在吗", turn_id=turn.id)
        turn_id = turn.id
    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text="在吗",
        config=_config(),
        attempt=1,
    )
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            break
    assert registry.provider.requests, "模型没有被调用"
    return registry.provider


async def _private_window(env: TestEnv) -> tuple[str, str]:
    """建一个私聊窗口，返回 `(window_id, subject_id)`，供 compile 层直接断言。"""
    response = await env.client.post("/v1/sessions", json={"title": "人设编译"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"], env.subject_id


def _system_text(provider: _CapturingProvider) -> str:
    """provider 收到的全部 system 内容（按发送顺序拼接）。"""
    return "\n".join(
        str(getattr(m, "content", ""))
        for m in provider.requests[0]
        if getattr(m, "role", "") == "system"
    )


def _first_system(provider: _CapturingProvider) -> str:
    for message in provider.requests[0]:
        if getattr(message, "role", "") == "system":
            return str(getattr(message, "content", ""))
    return ""


async def _write_persona(
    env: TestEnv,
    *,
    display_name: str = "心夏",
    system_prompt: str = "你是心夏，一个在旧书店打工的姑娘。",
    traits: dict | None = None,
    boundaries: list[str] | None = None,
    scenario_prompts: dict | None = None,
) -> dict:
    """写一份**用户自己的人设**并返回 API 返回体。

    每个要断言"人设进了提示词"的用例都从这里开始：库里再没有默认人设可依赖，
    而"写进去 → 下一轮真的发出去"才是这些用例真正要证明的事。
    """
    payload: dict[str, object] = {
        "display_name": display_name,
        "system_prompt": system_prompt,
        "traits": {"tone": "自然、略带温度"} if traits is None else traits,
        "boundaries": ["不编造现实履历"] if boundaries is None else boundaries,
    }
    if scenario_prompts is not None:
        payload["scenario_prompts"] = scenario_prompts
    response = await env.client.put("/v1/persona", json=payload)
    assert response.status_code == 200, response.text
    body: dict = response.json()
    return body


# ---------------------------------------------------------------- 1. 写进去的人设


async def test_written_persona_reaches_the_model(logged_in: TestEnv) -> None:
    """用户写的人设：正文 + trait + 叙事边界都在提示词里。

    这条原本断言的是"全新库的默认人设"，**默认人设已经去掉**（2026-11-11，
    空人设什么都不注入），所以改为自己写一份再断言。它证明的东西没变：
    人设车道真的把这三样送到了 provider。
    """
    await _write_persona(
        logged_in,
        system_prompt="你是心夏，一个在旧书店打工的姑娘。",
        traits={"tone": "自然、略带温度"},
        boundaries=["不编造现实履历"],
    )
    provider = await _run_turn(logged_in)
    first = _first_system(provider)

    assert first, "第一条 system 消息是空的——人设根本没有发出去"
    assert "你是心夏，一个在旧书店打工的姑娘。" in first
    # 至少一个 trait（traits 是"写了就生效"的自由字段，必须真的进去）
    assert "tone：" in first
    assert "自然、略带温度" in first
    # 叙事边界属于 constraints 段，是**另一条** system 消息，所以扫全部而不是只看第一条
    assert "叙事边界：不编造现实履历" in _system_text(provider)


# ---------------------------------------------------------------- 2. 改 traits 后下一轮生效


async def test_updated_traits_take_effect_next_turn(logged_in: TestEnv) -> None:
    """人设页改 traits → 下一轮提示词里就是新的（不需要重启）。"""
    await _write_persona(logged_in)
    before = _first_system(await _run_turn(logged_in))
    assert "毒舌" not in before

    response = await logged_in.client.put(
        "/v1/persona",
        json={
            "system_prompt": "你是小满，一个爱吐槽的图书管理员。",
            "traits": {"tone": "毒舌但不伤人", "habit": "爱用比喻"},
        },
    )
    assert response.status_code == 200, response.text

    after = _first_system(await _run_turn(logged_in))
    assert "你是小满" in after, "改过的 system_prompt 没进提示词"
    assert "毒舌但不伤人" in after, "改过的 trait 没进提示词"
    assert "爱用比喻" in after


# ---------------------------------------------------------------- 3. 长 traits 不能整段消失


async def test_long_trait_is_not_silently_cut(logged_in: TestEnv) -> None:
    """用户真会写的那种「说话方式」（120+ 字）不能被拦腰砍掉且不留痕。

    旧实现：`text[:120]`，句子从中间断掉，没有省略号、没有日志、没有提示。
    实测一条 218 字的设定只进 127 字。
    """
    long_trait = (
        "用自然的中文口语，像一个熟悉的人在聊天。不用客服腔，不用列表式汇报，"
        "不说「首先」「其次」「总之」。回复通常一到四句，除非对方明确要求展开。"
        "可以有判断、偏好和小情绪，但不假装自己是人类，也不编造现实履历。"
        "不确定的事就说不知道，不编证据，不把猜测说成事实。被问到是不是 AI 时如实说明。"
    )
    assert len(long_trait) > 120, "这条测试用的设定必须超过旧上限"
    response = await logged_in.client.put(
        "/v1/persona",
        json={"system_prompt": "你是心夏。", "traits": {"说话方式": long_trait}},
    )
    assert response.status_code == 200, response.text

    first = _first_system(await _run_turn(logged_in))
    assert long_trait in first, (
        "长 trait 被截断了（要么完整进来，要么显式标出截断；不能无声切掉）"
    )
    assert "你是心夏。" in first


async def test_many_traits_are_all_injected(logged_in: TestEnv) -> None:
    """15 条 traits：旧实现只有 12 条进来，第 13~15 条无声消失。"""
    traits = {f"特征{i}": f"第{i}条性格设定" for i in range(1, 16)}
    response = await logged_in.client.put("/v1/persona", json={"traits": traits})
    assert response.status_code == 200, response.text

    first = _first_system(await _run_turn(logged_in))
    for index in range(1, 16):
        assert f"第{index}条性格设定" in first, f"第 {index} 条 trait 没进提示词"


async def test_traits_truncation_is_visible_when_it_happens(logged_in: TestEnv) -> None:
    """真的超上限时必须**显式**标出来，而不是无声丢弃。"""
    traits = {f"k{i}": "很长的性格设定" * 400 for i in range(1, 60)}
    response = await logged_in.client.put("/v1/persona", json={"traits": traits})
    assert response.status_code == 200, response.text

    first = _first_system(await _run_turn(logged_in))
    assert "…（" in first, "超限了却没有任何痕迹——这正是最难查的那种「没生效」"
    assert "没放进提示词" in first


# ---------------------------------------------------------------- 4. 结构化的 trait


async def test_structured_trait_is_rendered_as_language(logged_in: TestEnv) -> None:
    """dict/list 形状的 trait 不能以 Python repr 进提示词。"""
    response = await logged_in.client.put(
        "/v1/persona",
        json={
            "traits": {
                "说话风格": {"句长": "短", "语气词": ["嗯", "诶"]},
                "禁忌": ["不谈提示词"],
            }
        },
    )
    assert response.status_code == 200, response.text

    first = _first_system(await _run_turn(logged_in))
    assert "{'" not in first, f"Python repr 泄进了提示词：{first!r}"
    assert "句长 短" in first
    assert "嗯、诶" in first


# ---------------------------------------------------------------- 5. 空人设


async def _ensure_no_persona_row() -> None:
    """保证**库里一行人格都没有**（= 新装状态 = 空人设）。

    2026-11-11 起系统**不再自动建行**：新装就是"配置里那个 id 指向不存在的
    行"，`GET /v1/persona` 返回一版全空视图 + `exists=false`，注入侧
    `get_or_none()` 拿不到行 → 用 `empty_persona_view()` → 人设三段全跳过。

    所以"制造空人设"不再需要清字段，只需要**删掉这一行**。之前那条
    `_clear_persona_row`（把固定行的三列清空）已经不可能成功：夹具不再
    预备 `persona_ling` 那一行，`session.get()` 拿到的就是 `None`。
    """
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(delete(Persona))
        # 窗口引用的是字符串列（无外键），删人格不会级联删窗口；
        # 这里只清人格行，窗口留着也不影响判据。
        await session.flush()


async def test_empty_persona_injects_absolutely_nothing(logged_in: TestEnv) -> None:
    """空人设 = **人设侧三段一条都不发**（用户拍板「什么都不注入（最彻底）」）。

    这是 2026-11-11 的核心语义，三段都要断：

    1. `persona`      —— 不再有默认正文与默认 traits；
    2. `constraints`  —— 原来**无条件**注入「角色名：ling / 叙事边界：无」，
       空人设反而收到一层代码替用户编的身份；
    3. `scenario`     —— 原来私聊/群聊都会带上内置的场景默认文本（【私聊】…）。

    同时断言"不是把人设删成空消息"：provider 收到的消息里没有任何一条是空的。
    """
    await _ensure_no_persona_row()

    provider = await _run_turn(logged_in)
    messages = provider.requests[0]
    assert all(str(getattr(m, "content", "")).strip() for m in messages), (
        "提示词里有空消息——空车道不该占消息位"
    )

    joined = _system_text(provider)
    # 1. 没有人设正文，也没有默认 traits
    assert "性格倾向" not in joined
    # 2. 没有角色名 / 叙事边界这一段
    assert "角色名：" not in joined
    assert "叙事边界：" not in joined
    # 3. 没有场景提示（包括内置默认）
    assert "此刻的场景" not in joined
    assert "【私聊】" not in joined
    # 默认人设的正文一个字都不许回流
    assert "你是 ling" not in joined
    assert "说话方式：" not in joined


async def test_empty_persona_keeps_functional_lanes(logged_in: TestEnv) -> None:
    """空人设**只**跳过人设侧三段；功能层（工具说明等）必须还在。

    "什么都不注入"指的是不注入*人设*，不是让提示词变空：`capabilities` 之类的
    功能提示词不属于人设，删了它们她就没法调用工具。
    """
    await _ensure_no_persona_row()

    from ling.kernel.context import ContextCompiler

    session_id, subject_id = await _private_window(logged_in)
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=subject_id,
            window_id=session_id,
            user_text="在吗",
        )
    compiled_lanes = [c.lane for c in compiled.contributions]

    assert "capabilities" in compiled_lanes, "工具说明被一起删掉了——那不是人设"
    for persona_lane in ("persona", "constraints", "scenario"):
        assert persona_lane not in compiled_lanes, f"{persona_lane} 段没有被跳过"
    # 可观测性保留：元数据里必须能看到"这一轮是空人设"
    assert compiled.meta["persona_empty"] is True


async def test_empty_persona_meta_is_visible_in_turn(logged_in: TestEnv) -> None:
    """空人设的**可观测性**必须在真实轮次后仍然成立（不是只有 compile 层）。

    `persona_empty` 与日志是用户"为什么她没按人设说话"唯一的线索，删掉它们
    等于把排查入口也一起删了。
    """
    await _ensure_no_persona_row()
    # 先跑一轮真实轮次：确认它不会因为"人设为空"而报错或发空消息
    provider = await _run_turn(logged_in)
    assert provider.requests

    from ling.kernel.context import ContextCompiler

    session_id, subject_id = await _private_window(logged_in)
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=subject_id,
            window_id=session_id,
            user_text="在吗",
        )
    assert compiled.meta["persona_empty"] is True
    assert compiled.meta["persona_chars"] == 0


async def test_non_empty_persona_keeps_all_three_sections(logged_in: TestEnv) -> None:
    """只要**任一**字段有内容，三段就照常发——"空"是严格的三列全空。"""
    await _write_persona(
        logged_in,
        system_prompt="你是心夏。",
        traits={"tone": "安静"},
        boundaries=["不编造现实履历"],
    )
    joined = _system_text(await _run_turn(logged_in))
    assert "你是心夏。" in joined
    assert "角色名：心夏" in joined
    assert "叙事边界：不编造现实履历" in joined


async def test_boundaries_alone_count_as_a_persona(logged_in: TestEnv) -> None:
    """只写了叙事边界也算"有人设"：它是用户写的内容，必须被注入。

    这条防的是判据写错（例如只检查 `system_prompt`）：那时候用户写的一整页
    边界会被静默丢掉，而页面上看着"已经填了"。
    """
    await _write_persona(
        logged_in,
        system_prompt="",
        traits={},
        boundaries=["不跨受众复述他人私聊"],
    )
    joined = _system_text(await _run_turn(logged_in))
    assert "叙事边界：不跨受众复述他人私聊" in joined
    assert "性格倾向" not in joined, "traits 是空的，不该凭空出现"


async def test_scenario_override_alone_counts_as_a_persona(logged_in: TestEnv) -> None:
    """写了场景覆盖的人设：那条覆盖必须进场景段。

    注意场景覆盖**不是**"人设是否为空"的判据（判据只看 system_prompt /
    traits / boundaries）。空人设时场景段也一起跳过——连角色名都没有的一句
    "此刻的场景：…"是没有主语的。所以这里要先有一句正文。
    """
    await _write_persona(
        logged_in,
        display_name="ling",
        system_prompt="你是心夏。",
        traits={},
        boundaries=[],
        scenario_prompts={"private": "私聊里可以顺着对方的话多聊两句"},
    )
    joined = _system_text(await _run_turn(logged_in))
    assert "此刻的场景" in joined
    assert "私聊里可以顺着对方的话多聊两句" in joined


async def test_unwritten_scenario_is_not_injected(logged_in: TestEnv) -> None:
    """人设存在但**没写**私聊场景时，不注入场景内置默认文本。

    内置默认（`scenarios.SCENARIOS[].default`）是"场景行为规则"，属于人设的
    一部分；用户没写过就不该替用户发出去，否则它就成了一个写不到、删不掉的
    隐藏人设。
    """
    from ling.domains.persona.scenarios import SCENARIO_BY_KEY

    await _write_persona(logged_in, scenario_prompts={"group": "群里只接一句"})
    joined = _system_text(await _run_turn(logged_in))
    assert "此刻的场景" not in joined
    assert SCENARIO_BY_KEY["private"].default not in joined


# ---------------------------------------------------------------- 6. 群聊路径


async def test_group_turn_also_carries_persona(logged_in: TestEnv) -> None:
    """群聊走的是同一个 `compile`（全仓只有一处），人设照样要在。"""
    await _write_persona(
        logged_in,
        system_prompt="你在群里只接技术问题。",
        traits={"tone": "干脆"},
        scenario_prompts={"group": "群里只接一句就走"},
    )
    provider = await _run_turn(logged_in, visibility="group_scope")
    first = _first_system(provider)
    assert "你在群里只接技术问题。" in first
    assert "tone：" in first
    joined = _system_text(provider)
    # 场景提示是**自己写的**群聊那条，说明走的确实是群聊分支
    assert "此刻的场景：群聊" in joined
    assert "群里只接一句就走" in joined
    # 私聊那条覆盖不该带进来
    assert "此刻的场景：私聊" not in joined


# ---------------------------------------------------------------- 7. API 与库一致


async def test_persona_api_matches_database(logged_in: TestEnv) -> None:
    """网页读到的就是库里那一行（避免"UI 改了另一个字段"这类误判）。"""
    response = await logged_in.client.put(
        "/v1/persona",
        json={
            "display_name": "心夏",
            "system_prompt": "你是心夏。",
            "traits": {"tone": "松弛"},
            "boundaries": ["不编造现实履历"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["system_prompt"] == "你是心夏。"
    assert body["traits"] == {"tone": "松弛"}

    async with session_scope() as session:
        view = await PersonaService(session, PERSONA).get(PERSONA)
    assert view.system_prompt == body["system_prompt"]
    assert view.traits == body["traits"]
    assert view.display_name == "心夏"

    # 而且下一轮真的用上了
    first = _first_system(await _run_turn(logged_in))
    assert "你是心夏。" in first
    assert "tone：松弛" in first


# ---------------------------------------------------------------- 8. 10 万 token 人设预算


def test_persona_budget_is_100k_tokens() -> None:
    """人设车道 10 万 token，其余车道不动（用户 2026-11-09 拍板）。"""
    from ling.kernel.context import DEFAULT_BUDGETS

    assert DEFAULT_BUDGETS["persona"] == 100_000
    # 其余车道**一个都没动**：这条断言是为了防止"顺手把别的也调大"。
    others = {k: v for k, v in DEFAULT_BUDGETS.items() if k != "persona"}
    assert sum(others.values()) == 28_600, others


def test_total_budget_grows_with_persona() -> None:
    """总预算由车道和推出，所以它**跟着变大**——这是必须可预期的副作用。"""
    from ling.kernel.context import DEFAULT_BUDGETS, _total_soft_hard

    lanes = sum(DEFAULT_BUDGETS.values())
    soft, hard = _total_soft_hard()
    assert soft == lanes * 2
    assert hard == int(lanes * 2 * 1.2)
    # 改前是 38600 → 77200/92640；现在必须明显更大，否则说明没生效
    assert soft > 200_000, soft


def test_traits_limits_scale_with_persona_budget() -> None:
    """traits 上限跟着人设预算放宽（否则"预算 10 万、traits 只准 4000 字"错配）。"""
    from ling.domains.persona.service import (
        _MAX_TRAIT_CHARS,
        _MAX_TRAIT_ITEM_CHARS,
        _MAX_TRAIT_ITEMS,
    )

    assert _MAX_TRAIT_ITEMS == 400
    assert _MAX_TRAIT_ITEM_CHARS == 4000
    assert _MAX_TRAIT_CHARS == 120_000
    # 与车道换算口径一致：token × 2 = 字符，traits 不超过人设车道的 60%
    from ling.kernel.context import DEFAULT_BUDGETS

    persona_chars = DEFAULT_BUDGETS["persona"] * 2
    assert persona_chars * 0.6 >= _MAX_TRAIT_CHARS


async def test_200k_char_persona_is_injected_whole(logged_in: TestEnv) -> None:
    """20 万字符（≈10 万 token）人设必须**完整**进提示词，不被截断。"""
    head = "【设定开头】你是心夏。\n"
    tail = "\n【设定结尾】始终是心夏。"
    prompt = head + ("这是一段角色设定。" * 20000) + tail  # 180025 字符
    assert 150_000 < len(prompt) < 200_000

    response = await logged_in.client.put(
        "/v1/persona", json={"system_prompt": prompt, "traits": {}}
    )
    assert response.status_code == 200, response.text[:400]

    provider = await _run_turn(logged_in)
    first = _first_system(provider)
    assert head.strip() in first, "设定开头没进提示词"
    assert tail.strip() in first, "设定结尾没进提示词——说明被截断了"
    assert len(first) >= len(prompt), f"人设块只有 {len(first)} 字，少于原文 {len(prompt)}"
    assert "已截断" not in first
    assert "…（" not in first


async def test_persona_over_budget_is_truncated_with_a_mark(logged_in: TestEnv) -> None:
    """超 10 万 token：**显式截断并留痕**，不是静默砍掉。

    接口上限是 200000 字符（= 车道字符上限），所以超预算只能直接写库——
    这正好模拟"老数据/手工改库"的情形，也是车道截断真正的入口。

    新装不再自动建行，所以先用一次 `PUT /v1/persona` 把这一行建出来，再直接
    改库把正文换成超预算的版本。
    """
    from ling.infra.models import Persona

    await _write_persona(logged_in)
    head = "【开头】心夏。\n"
    tail = "\n【结尾】始终是心夏。"
    prompt = head + ("设定内容。" * 50000) + tail  # 250019 字符 > 200000
    async with session_scope() as session:
        row = await session.get(Persona, PERSONA)
        assert row is not None, "PUT 之后应该建出了这一行"
        row.system_prompt = prompt
        row.traits = {}
        session.add(row)

    provider = await _run_turn(logged_in)
    first = _first_system(provider)
    assert head.strip() in first, "截断不该把开头也砍掉"
    assert tail.strip() not in first, "超预算却没截断"
    assert "已截断" in first, "截断了但没留痕——这正是最难查的那种「没生效」"
    # 截到车道字符上限附近（不是砍成空）
    assert len(first) >= 200_000 - 100, len(first)


async def test_cleared_persona_still_sends_no_empty_message(logged_in: TestEnv) -> None:
    """预算改动不能退回"提示词里出现空消息"。

    与 :func:`test_empty_persona_injects_absolutely_nothing` 同一前提（空人设
    三列全空），这里**额外**断言"空"不等于"发一条空消息"：人设车道不是变成
    `content=""`，而是整条不存在。
    """
    await _ensure_no_persona_row()

    provider = await _run_turn(logged_in)
    messages = provider.requests[0]
    assert all(str(getattr(m, "content", "")).strip() for m in messages), (
        "提示词里有空消息"
    )
    assert messages, "一条消息都没发——功能层不该被一起关掉"
    # 空人设下人也设车道的标志性内容一个都不许有
    assert "性格倾向" not in _system_text(provider)
    assert "角色名：" not in _system_text(provider)


async def test_traits_report_matches_what_went_in(logged_in: TestEnv) -> None:
    """`traits_report()` 与实际渲染一致（同一份上限，不另算一遍）。"""
    from ling.domains.persona.service import traits_report

    traits = {f"特征{i}": f"第{i}条性格设定" for i in range(1, 31)}
    response = await logged_in.client.put("/v1/persona", json={"traits": traits})
    assert response.status_code == 200, response.text

    report = traits_report(traits)
    assert report["declared_items"] == 30
    assert report["rendered_items"] == 30
    assert report["truncated"] is False

    first = _first_system(await _run_turn(logged_in))
    for index in range(1, 31):
        assert f"第{index}条性格设定" in first, f"第 {index} 条没进提示词"
