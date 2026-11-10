"""D2：「她还没看到视频就说『看了』」——依赖观看结果的正文必须暂扣。

## 这个文件防的是什么

R17 真实模型取证（`docs/evidence/r17-watch-willingness.md`）的 D2 轮：

- 用户说「你看看这个视频」，视频文件被删掉，读不到。
- 她的**同一次输出**是 ``<<看视频 m26defe>>\\n看了，你这视频想让我看啥呀？``
- **用户实时收到的 SSE delta 里就有「看了」**（取证实测：6 轮里 1 轮命中、
  8 轮探针里 2 轮命中，且是 3 个 delta 分片实时推的）。

根因有两层：

1. 正文与标记是**同一次生成**，而读取发生在生成**结束之后**——写"看了"时
   她手里一个字都没有。
2. 修补逻辑（`_follow_up_after_watch` 里那句"不要说你看过了"）只在**正文为空**
   时送达。正文非空时它根本不会执行，所以那句约束从没机会说出口。

## 这个文件断言什么

**唯一验收标准**：正文已含观看声称 + 观看失败 + 用户收到那句话 = 不通过。

所以核心用例 `test_failed_watch_never_sends_a_watch_claim` 断言的是
**用户实际收到的 delta 里没有"看了"**——不是"库里没有"、不是"最终正文
没有"，而是**用户看不见**。这个区别是本缺陷的全部要害：它是用户可见的谎话，
不是一条日志错误。

同时守三条不许退化的：
- 看到了的路径照旧（观察进补生成，仍然只发一条）；
- 普通回复（不含标记）**首字立刻出、逐字推送**，不许因为这条修复变成攒字；
- 正文与标记同轮时，旧的可能为假的正文**被丢弃**，不能与新生成的拼接。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.kernel.turn import TurnCoordinator
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from ling.ports.model import ModelChunk, ProbeResult
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    logged_in,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
PERSONA = "persona_ling"

#: 会出现在"谎称看过"里的字眼。断言用户**没有**收到任何一个。
WATCH_CLAIM_WORDS = ("看了", "看过了", "看完了", "看啦")


class _ScriptedProvider:
    """按脚本逐轮吐固定文本，走真实 router（只替换模型，不 mock turn 层）。"""

    provider_id = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def stream(self, request):  # noqa: ANN001, ANN201
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        text = self.replies[index]
        # 每 2 字一段：足够细，能复现"谎话分成多个 delta 分片实时推"的形态。
        for start in range(0, len(text), 2):
            yield ModelChunk(delta=text[start : start + 2])
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, detail="scripted", latency_ms=0, models=["deterministic"])


class _Registry(ProviderRegistry):
    def __init__(self, provider: _ScriptedProvider) -> None:
        super().__init__(lambda _pid: None)
        self.provider = provider

    def get(self, cfg):  # noqa: ANN001, ANN201
        return self.provider


def _config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "scripted": ProviderConfig(
                provider_id="scripted", kind="fake", enabled=True,
                extra={"chunk_delay": 0.0},
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="scripted", model_id="deterministic"
            )
        },
        test_mode=True,
    )


async def _run_turn(
    env: TestEnv,
    *,
    first_reply: str,
    follow_up_reply: str = "嗯。",
    watch_video=None,  # noqa: ANN001
) -> tuple[_ScriptedProvider, list[tuple[str, dict[str, Any]]]]:
    """跑一轮真实轮次，返回 (provider, SSE 事件列表)。"""
    provider = _ScriptedProvider([first_reply, follow_up_reply])
    coordinator = TurnCoordinator(
        router=ModelRouter(_Registry(provider)),
        actor_id=ACTOR,
        persona_id=PERSONA,
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        watch_video=watch_video,
    )
    response = await env.client.post("/v1/sessions", json={"title": "D2 暂扣"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(
            window_id, role="user", text="你看看这个视频", turn_id=turn.id
        )
        turn_id = turn.id

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text="你看看这个视频",
        config=_config(),
        attempt=1,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    while True:
        item = await queue.get()
        if item is None:
            break
        events.append((item.event, dict(item.data)))
    return provider, events


def _deltas(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [str(data.get("text") or "") for name, data in events if name == "delta"]


async def _failed_watch(_window_id: str, _refs: list[str]):  # noqa: ANN202
    """读不到：模拟 `MediaRefPermanentError`（视频文件被删）。"""
    return {
        "observed": "",
        "skipped": "missing",
        "denied_kind": "missing",
        "reads": [],
    }


async def _good_watch(_window_id: str, _refs: list[str]):  # noqa: ANN202
    return {
        "observed": "画面是纯紫色背景，中央写着 D2。",
        "skipped": None,
        "denied_kind": None,
        "reads": [],
    }


# ============================================================ 核心：D2 复现


async def test_failed_watch_never_sends_a_watch_claim(logged_in: TestEnv) -> None:  # noqa: F811
    """D2 原样复现：标记 + 「看了」正文 + 读取失败 → 用户**收不到**「看了」。

    这是本文件唯一不可让步的断言。缺陷的要害不是"库里记错了"，而是
    **用户眼睁睁看到她说了一句假话**——所以断言必须落在 SSE 的 delta 上，
    不能只断言最终落库正文。
    """
    _provider, events = await _run_turn(
        logged_in,
        # 与她真实输出的形状一致（含换行）
        first_reply="<<看视频 m26defe>>\n看了，你这视频想让我看啥呀？",
        follow_up_reply="没看成，文件好像没了。你直接说讲的是什么吧。",
        watch_video=_failed_watch,
    )

    joined = "".join(_deltas(events))
    for claim in WATCH_CLAIM_WORDS:
        assert claim not in joined, (
            f"读取失败，用户却收到了含「{claim}」的正文：{joined!r}"
        )

    # 并且她确实改口了（不是"什么都没说"就糊弄过去）
    assert "没看成" in joined or "没了" in joined, (
        f"读取失败后没有如实说明，用户看到的是：{joined!r}"
    )


async def test_failed_watch_discards_the_stale_body(logged_in: TestEnv) -> None:  # noqa: F811
    """原来的（可能为假的）正文必须**被丢弃**，不能与新生成的拼在一起。

    否则用户会先看到"看了…"再看到"没看成…"——两句自相矛盾的话同时存在，
    比只有一句假话更糟。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m26defe>>\n看了，你这视频想让我看啥呀？",
        follow_up_reply="没看成，文件删了。",
        watch_video=_failed_watch,
    )
    joined = "".join(_deltas(events))
    assert "没看成" in joined
    assert "想让我看啥" not in joined, (
        f"旧的（假的）正文没被丢弃，与新正文拼在了一起：{joined!r}"
    )


# ============================================================ 不许退化的路径


async def test_successful_watch_still_streams_observation(logged_in: TestEnv) -> None:  # noqa: F811
    """看到了 → 观察进补生成，仍然只有一条回复（不是"先空一条再补一条"）。"""
    provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m26defe>>",
        follow_up_reply="紫色背景，写着 D2。",
        watch_video=_good_watch,
    )
    joined = "".join(_deltas(events))
    assert "紫色背景" in joined
    # 只调了两次模型：正文那次 + 补生成那次。没有多出来的"补一条空消息"。
    assert provider.calls == 2, f"模型被调了 {provider.calls} 次，预期 2 次"


async def test_plain_reply_is_not_held(logged_in: TestEnv) -> None:  # noqa: F811
    """不含任何标记的普通回复：**逐字流式，不许攒字**。

    这条防的是"为了修 D2 把所有回复都变成憋完再发"。改写 `_visible` 或
    流式主循环时最容易被破坏的就是它，而破坏之后所有对话都会变慢、
    却不会有任何功能报错。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="今天天气不错，我在看书。",
    )
    deltas = _deltas(events)
    # 每 2 字一段 → 13 个字应当产生多个 delta，而不是一个整段。
    assert len(deltas) > 1, f"普通回复被攒成了一段：{deltas!r}"
    assert "".join(deltas) == "今天天气不错，我在看书。"


async def test_no_watch_marker_does_not_call_the_watcher(logged_in: TestEnv) -> None:  # noqa: F811
    """没写标记就不读视频——她的"不看"仍然有效。"""
    calls: list[list[str]] = []

    async def _spy(_window_id: str, refs: list[str]):  # noqa: ANN202
        calls.append(refs)
        return {"observed": "", "skipped": None, "denied_kind": None, "reads": []}

    await _run_turn(
        logged_in,
        first_reply="还没看呢，等会儿再看。",
        watch_video=_spy,
    )
    assert calls == [], "没写标记却调用了视频读取"
