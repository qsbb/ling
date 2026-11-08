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
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ling.domains.conversation.service import ConversationService
from ling.domains.persona.service import DEFAULT_SYSTEM_PROMPT, PersonaService
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


# ---------------------------------------------------------------- 1. 默认人设


async def test_default_persona_reaches_the_model(logged_in: TestEnv) -> None:
    """全新库的默认人设：首句 + 至少一个 trait 都在提示词里。"""
    provider = await _run_turn(logged_in)
    first = _first_system(provider)

    assert first, "第一条 system 消息是空的——人设根本没有发出去"
    # 首句（前 30 字，足够定位是同一段设定）
    assert DEFAULT_SYSTEM_PROMPT[:30] in first
    # 至少一个 trait（traits 是"写了就生效"的自由字段，必须真的进去）
    assert "tone：" in first
    assert "自然、略带温度" in first


# ---------------------------------------------------------------- 2. 改 traits 后下一轮生效


async def test_updated_traits_take_effect_next_turn(logged_in: TestEnv) -> None:
    """人设页改 traits → 下一轮提示词里就是新的（不需要重启）。"""
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


async def test_empty_persona_sends_no_empty_system_message(logged_in: TestEnv) -> None:
    """人设为空时**不发**空 system 消息，并且这件事在日志里说得清。

    空人设 ≠ 注入故障。原来会发一条 `content=""` 的 system 消息，于是
    「查看本次提示词」里那块「人设」是空的——用户看到的就是"人设没注入"。
    """
    async with session_scope() as session:
        row = await session.get(Persona, PERSONA)
        assert row is not None
        row.system_prompt = ""
        row.traits = {}
        session.add(row)

    provider = await _run_turn(logged_in)
    messages = provider.requests[0]
    assert all(str(getattr(m, "content", "")).strip() for m in messages), (
        "提示词里有空消息——空车道不该占消息位"
    )
    # 约束车道仍在（角色名/边界），人设车道整条不发
    joined = _system_text(provider)
    assert "角色名：" in joined
    assert "性格倾向" not in joined


async def test_persona_restored_after_being_cleared(logged_in: TestEnv) -> None:
    """清空后再填回去，下一轮立刻恢复（证明链路一直是通的）。"""
    async with session_scope() as session:
        row = await session.get(Persona, PERSONA)
        row.system_prompt = ""
        row.traits = {}
        session.add(row)
    assert "性格倾向" not in _system_text(await _run_turn(logged_in))

    response = await logged_in.client.put(
        "/v1/persona",
        json={"system_prompt": "你是阿岚。", "traits": {"tone": "安静"}},
    )
    assert response.status_code == 200, response.text
    restored = _system_text(await _run_turn(logged_in))
    assert "你是阿岚。" in restored
    assert "tone：安静" in restored


# ---------------------------------------------------------------- 6. 群聊路径


async def test_group_turn_also_carries_persona(logged_in: TestEnv) -> None:
    """群聊走的是同一个 `compile`（全仓只有一处），人设照样要在。"""
    provider = await _run_turn(logged_in, visibility="group_scope")
    first = _first_system(provider)
    assert DEFAULT_SYSTEM_PROMPT[:30] in first
    assert "tone：" in first
    joined = _system_text(provider)
    # 场景提示是群聊那条，说明走的确实是群聊分支
    assert "【群聊】" in joined


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
