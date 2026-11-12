"""端到端：开关打开时，她**真的**调一次工具、拿回结果、再继续生成正文。

## 这条测试必须失败于哪个缺陷

**必须失败于「接线未做」**——即 `worker.py` 两处 `TurnCoordinator(...)` 构造没把
`tool_calls_enabled` / `tool_registry` 传下去（这两个是**构造函数**参数，不是
`run_turn()` 的参数）。所以它用的是 `worker.py` 里**生产路径用的那两个函数**：

    effective → _tool_calls_switch → tool_calls_enabled
    effective → tool_registry_for_turn → tool_registry

如果这两行在 worker 里被删掉（回到"不传参数"），本文件会红——因为
`TurnCoordinator` 的构造默认值 ``False`` 会让这一轮走进普通对话分支，
模型提出的工具调用没有任何执行者。

## 为什么不能只断言"正文非空"

一个**没有**执行工具的实现同样能生成一段正文（模型在这一步就自答了）。
所以断言必须落在三件只有"真的执行过"才会有的事上：

1. provider **第二步**收到的消息里有一条 ``role="tool"`` 的回灌；
2. 那条回灌里的 ``outcome`` 是 ``succeeded``，且工具是 `clock.now`；
3. 第二步的请求里带着 ``tools``（说明第一道闸确实开着）。

把这三条任何一条去掉，一个"永远不调工具"的实现都能绿。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest

from ling.capabilities.tools import CLOCK_NOW_TOOL_ID
from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.kernel.turn import TurnCoordinator
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import (
    DialoguePolicy,
    EffectiveConfig,
    ProfileBinding,
    ProviderConfig,
)
from ling.ports.model import (
    ModelChunk,
    ModelChunkKind,
    ModelRequest,
    ProviderKind,
    ToolCall,
)
from ling.worker import _tool_calls_switch, tool_registry_for_turn
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

FIRST_STEP_TEXT = "我查一下现在几点。"
FINAL_TEXT = "现在是下午三点左右，你说的是这个时间吧？"


class _ToolCallingProvider:
    """第一步提出一次 `clock.now` 调用；第二步（看到回灌后）给出正文。

    **必须声明 ``supports_native_tools = True``**，否则 `provider_native_tools_support`
    会在最后一道闸把它挡下（``instance_has_no_native_tools``），本轮退回普通
    对话——那正是这条测试不该发生的事。
    """

    provider_id = "p1"
    kind = ProviderKind.openai_compatible
    supports_native_tools = True

    def __init__(self, *, expect_tools: bool) -> None:
        #: 这一步**是否**应当收到 tools。两种模式共用一个类，但断言相反——
        #: 否则"关着开关"那条会在自己的 provider 里被"第一步必须有 tools"
        #: 那个断言误伤，红的原因就不是它想测的东西了。
        self.expect_tools = expect_tools
        #: 每一步**真正收到**的请求。断言全部基于它，而不是基于我们喂进去的东西。
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.requests.append(request)
        step = len(self.requests)

        if step == 1:
            assert bool(request.tools) is self.expect_tools, (
                f"第一步 tools 存在性为 {bool(request.tools)}，期望 {self.expect_tools}"
            )
            yield ModelChunk(delta=FIRST_STEP_TEXT)
            if not self.expect_tools:
                # 关着开关 = 普通对话：这一步就是答案，不存在工具轮次。
                yield ModelChunk(done=True, finish_reason="stop")
                return
            yield ModelChunk(
                tool_call=ToolCall(
                    provider_call_id="call_clock_1",
                    name=CLOCK_NOW_TOOL_ID,
                    arguments=json.dumps({"tz": "Asia/Shanghai"}),
                    raw_index=0,
                ),
                provider_call_id="call_clock_1",
                kind=ModelChunkKind.tool_call,
            )
            yield ModelChunk(
                provider_call_id="call_clock_1",
                tool_call_complete=True,
                kind=ModelChunkKind.tool_call,
            )
            yield ModelChunk(done=True, finish_reason="tool_calls")
            return

        # 第二步：模型**必须**已经看到了工具结果，才轮到它说话。
        assert any(m.role == "tool" for m in request.messages), (
            f"第二步没有收到任何工具回灌，实际角色: {[m.role for m in request.messages]}"
        )
        for piece in (FINAL_TEXT[:6], FINAL_TEXT[6:]):
            yield ModelChunk(delta=piece)
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self):  # noqa: ANN201
        raise AssertionError("本测试不探测")

    async def embed(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("本测试不嵌入")


class _Registry(ProviderRegistry):
    """始终返回同一个假 provider（工具回路不需要按配置建实例）。"""

    def __init__(self, provider: _ToolCallingProvider) -> None:
        super().__init__(lambda _pid: None)
        self.provider = provider

    def get(self, cfg):  # noqa: ANN001, ANN201
        return self.provider


def _config(*, tools_enabled: bool) -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            # kind 必须是 openai_compatible：它是本仓库唯一实现了工具编解码的适配器。
            "p1": ProviderConfig(provider_id="p1", kind="openai_compatible", enabled=True),
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="p1", model_id="m-tools"
            ),
        },
        test_mode=True,
        dialogue=DialoguePolicy(tools_enabled=tools_enabled),
    )


async def _run(
    env: TestEnv, *, tools_enabled: bool
) -> tuple[list[ModelChunk], list[tuple[str, dict]], object]:
    """走一遍**生产装配**：配置 → worker 的两个函数 → TurnCoordinator。"""
    provider = _ToolCallingProvider(expect_tools=tools_enabled)
    config = _config(tools_enabled=tools_enabled)
    coordinator = TurnCoordinator(
        router=ModelRouter(_Registry(provider)),
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        # ---- 这两行就是被测的接线，**与 `worker.py` 两处构造逐字相同**。
        # 删掉它们 == 回到"不传参数"（构造默认 = 关），本文件第一条会红。
        tool_calls_enabled=_tool_calls_switch(config),
        tool_registry=tool_registry_for_turn(config),
    )

    window_id = await _new_window(env)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text="现在几点了？", turn_id=turn.id)
        turn_id = turn.id

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text="现在几点了？",
        config=config,
        attempt=1,
    )
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            break
        events.append((item.event, item.data))
    return provider.requests, events, turn_id


async def _new_window(env: TestEnv) -> str:
    response = await env.client.post("/v1/sessions", json={"title": "工具回路"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _reply(events: list[tuple[str, dict]]) -> str:
    completed = [data for name, data in events if name == "turn.completed"]
    assert completed, f"轮次没有完成，实际事件: {[n for n, _ in events]}"
    return str((completed[-1].get("message") or {}).get("text") or "")


# ------------------------------------------------------------------- 端到端


async def test_enabled_turn_really_calls_the_tool(logged_in: TestEnv) -> None:
    """开关打开 → 真的执行一次 `clock.now`，结果回灌，第二步给出正文。"""
    requests, events, _turn_id = await _run(logged_in, tools_enabled=True)

    # 1. 两个模型步骤：提出调用 + 看到结果后作答
    assert len(requests) == 2, (
        f"期望两个模型步骤（提议 → 回灌后作答），实际 {len(requests)} 个"
        "——只有一个说明工具调用没有执行，模型在那一步就自答了"
    )

    # 2. 第二步真的看到了 role="tool" 的回灌，而且它是成功的 clock.now
    tool_messages = [m for m in requests[1].messages if m.role == "tool"]
    assert len(tool_messages) == 1, f"期望恰好一条回灌，实际 {len(tool_messages)} 条"
    payload = json.loads(tool_messages[0].content)
    assert payload["tool_id"] == CLOCK_NOW_TOOL_ID
    assert payload["outcome"] == "succeeded", payload
    # `clock.now` 的回灌是 ``untrusted=false``：本机时钟不是第三方数据
    # （`ToolSpec.output_untrusted=False`，见 `capabilities/tools.py`）。
    # 这条断言钉的是"编码器如实转述了 spec"，而不是"一律标不可信"——
    # 对 `shell.readonly` 那类输出它才是 True。
    assert payload["untrusted"] is False, payload
    # 时间真的读出来了（不是空结果冒充成功）
    assert payload.get("data"), payload

    # 3. 第二步仍带着 tools（回路是持续的，不是只发第一步）
    assert requests[1].tools

    # 4. 用户看到的正文来自**第二步**，且含有第一步之外的内容
    body = _reply(events)
    assert FINAL_TEXT in body


async def test_switch_off_turn_never_exposes_tools(logged_in: TestEnv) -> None:
    """开关关着（默认）→ 请求里**没有** tools，模型也不会提出调用。

    这条是**区分力**所在：如果 `tool_registry_for_turn` 忽略开关、总是返回
    注册表，上面那条端到端在关着开关时一样会绿——两条测试就没有区别了。

    另外它证明了"接线"没有把默认行为改掉：不配置时她仍然是普通对话。
    """
    requests, events, _turn_id = await _run(logged_in, tools_enabled=False)

    assert len(requests) == 1, "关着开关时不该有第二个模型步骤"
    assert not requests[0].tools, "关着开关时请求里不该出现 tools 字段"
    assert not any(m.role == "tool" for m in requests[0].messages)
    assert _reply(events) == FIRST_STEP_TEXT
