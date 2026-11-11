"""观看路径的「正文非空」正向配对断言。

## 这个文件补的是什么缺口

`test_watch_claim_truthfulness.py` 与 `test_video_watch_quota.py` 覆盖的观看分支是
完整的，但其中**大部分断言是负向的**：

- 「不许出现『看了』」——空串当然不含「看了」；
- 「提示词里不许有『余力』」——那一轮什么都没发给用户，提示词再对也说明不了
  用户收到了话；
- 「`observed` 必须为空」——空回复同样满足。

负向断言的信息量等于与它配对的正向断言的信息量。**空串在全部负向断言上为真**，
所以「整轮正文消失」这个缺陷可以在负向断言全绿的情况下出厂。

这不是假设：2026-11-11 的真实回归里，`delivered += chunk.delta` 被放到了
"只有外发成功才累积"的位置，观看标记出现之后闭包一律返回空串，于是
`parse_watch_markers` 拿到空串、视频根本不读、整轮正文消失（`raw_chars=0`）。
当时只有 `test_failed_watch_never_sends_a_watch_claim` 里那句
``assert "没看成" in joined`` 把它兜住了——**它是全文件唯一的正向正文断言**。

## 所以这里的**每一条**都以「正文里有具体内容」收尾

写法上刻意把正向断言放在最后、并且**先断言长度 > 0**：

```python
assert official, f"...：{official!r}"      # 先证明有话说
assert "紫色" in official                  # 再证明说的是对的话
```

只写第二条也行；写成两条是为了让失败信息能区分
「说了别的话」与「什么都没说」——这两种故障的排查方向完全不同
（前者查补生成的输入，后者查累积点）。

## 同时守一条不能退化的

普通回复（不含标记）必须**逐字流式**且正文完整。它不经过观看路径，但它是
「给观看轮次加暂扣判据」时最容易被一起改坏的地方：一旦扣住就再也不放出来，
所有对话都会变成空回复。
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
        # 每 2 字一段：足够细，能复现"正文分成多个 delta 分片推送"的形态。
        for start in range(0, len(text), 2):
            yield ModelChunk(delta=text[start : start + 2])
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            ok=True, detail="scripted", latency_ms=0, models=["deterministic"]
        )


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
                provider_id="scripted",
                kind="fake",
                enabled=True,
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
    user_text: str = "你看看这个视频",
) -> tuple[_ScriptedProvider, list[tuple[str, dict[str, Any]]]]:
    """跑一轮真实轮次，返回 (provider, 事件列表)。"""
    provider = _ScriptedProvider([first_reply, follow_up_reply])
    coordinator = TurnCoordinator(
        router=ModelRouter(_Registry(provider)),
        actor_id=ACTOR,
        persona_id=PERSONA,
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        watch_video=watch_video,
    )
    response = await env.client.post("/v1/sessions", json={"title": "正文非空"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=user_text, turn_id=turn.id)
        turn_id = turn.id

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text=user_text,
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
    """用户实时看到的文本分片。"""
    return [str(data.get("text") or "") for name, data in events if name == "delta"]


def _final_body(events: list[tuple[str, dict[str, Any]]]) -> str:
    """收尾事件里落库/交付的正文（`turn.completed` / `turn.stopped`）。

    与 `_deltas` 是**两个不同的出口**：流式那个只证明"过程里分了片"，
    这个证明"最终说出口的那句话"。两者都要非空，缺一个就说明有一个出口
    把正文吞了。
    """
    for name in ("turn.completed", "turn.stopped", "turn.failed"):
        tail = [data for event, data in events if event == name]
        if tail:
            message = tail[-1].get("message") or {}
            return str(message.get("text") or "")
    return ""


def _both_outs(events: list[tuple[str, dict[str, Any]]]) -> tuple[str, str]:
    """(流式拼接, 最终正文)。"""
    return "".join(_deltas(events)), _final_body(events)


async def _good_watch(_window_id: str, _refs: list[str]):  # noqa: ANN202
    return {
        "observed": "画面是纯紫色背景，中央写着 D2。",
        "skipped": None,
        "denied_kind": None,
        "reads": [],
    }


async def _failed_watch(_window_id: str, _refs: list[str]):  # noqa: ANN202
    """读不到：模拟 `MediaRefPermanentError`（视频文件被删）。"""
    return {
        "observed": "",
        "skipped": "missing",
        "denied_kind": "missing",
        "reads": [],
    }


async def _quota_watch(_window_id: str, _refs: list[str]):  # noqa: ANN202
    return {
        "observed": "",
        "skipped": "今天的看视频额度用完了（已用 200000 / 上限 200000 tokens）",
        "denied_kind": "quota",
        "reads": 0,
    }


# ============================================================ 1. 看到了


async def test_watched_video_still_produces_a_non_empty_body(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """**首轮带正文**时看到了视频：用户收到的正文非空，且是观察重写的那句。

    与 `test_successful_watch_still_streams_observation` 的区别在输入：
    那条的 `first_reply` 只有标记、没有正文，于是「保留旧正文」与
    「用观察重写」两种实现在它上面结果相同——它没有判别力。

    这里首轮正文是 ``我看看啊``：如果实现是"保留旧正文"，正文里会留下
    「我看看啊」而没有「紫色」；如果是"用观察重写"，就是「紫色背景」。
    两种实现给出**不同**的结果，这条断言才有信息量。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m26defe>>\n我看看啊",
        follow_up_reply="紫色背景，中央写着 D2。",
        watch_video=_good_watch,
        user_text="你看看这个视频里是什么",
    )
    streamed, official = _both_outs(events)

    # (a) 最终正文非空 —— 这一条单独存在就是为了让"正文整段消失"必红。
    assert official.strip(), f"看到了视频，最终正文却是空的：{official!r}"
    # (b) 它说的是观察到的内容，不是一句空泛的应答。
    assert "紫色" in official or "D2" in official, (
        f"看到了视频，正文里却没有观察内容：{official!r}"
    )
    # (c) 用户**实时**也收到了东西：流式出口同样不能被吞。
    assert streamed.strip(), f"看到了视频，用户实时收到的却是空串：{streamed!r}"
    assert "紫色" in streamed or "D2" in streamed, (
        f"流式出口里没有观察内容：{streamed!r}"
    )


async def test_watched_video_rewrites_instead_of_keeping_the_stale_body(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """首轮正文是"看之前的瞎猜"，看到后必须被重写，不能原样发出去。

    `first_reply` 里那句「我看看啊」是在她还什么都没看到的时候写的。
    保留它就是让一句可能不成立的话出厂（D2 的形状）。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m26defe>>\n我看看啊，好像是个游戏",
        follow_up_reply="紫色背景，中央写着 D2，不像游戏。",
        watch_video=_good_watch,
        user_text="你看看这个视频里是什么",
    )
    streamed, official = _both_outs(events)

    assert official.strip(), f"最终正文是空的：{official!r}"
    assert "紫色" in official, f"正文里没有观察内容：{official!r}"
    assert "好像是个游戏" not in official, (
        f"看之前瞎猜的正文没被重写，原样发了出去：{official!r}"
    )
    assert streamed.strip(), f"用户实时收到的也是空串：{streamed!r}"
    assert "好像是个游戏" not in streamed, (
        f"看之前瞎猜的正文实时发了出去：{streamed!r}"
    )


# ============================================================ 2. 读取失败


async def test_failed_watch_still_produces_a_non_empty_body(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """读取失败：正文非空，且是「没看成」这类如实说明。

    `test_watch_claim_truthfulness.py` 里已有这句 ``"没看成" in joined``，
    但这里额外要求 `first_reply` 带正文，把"旧正文被丢弃"与"新正文非空"
    同时钉住：空串会让 (a) 红，保留旧正文会让 (c) 红。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m26defe>>\n看了，你这视频想让我看啥呀？",
        follow_up_reply="没看成，文件好像没了。你直接说讲的是什么吧。",
        watch_video=_failed_watch,
        user_text="你看看这个视频",
    )
    streamed, official = _both_outs(events)
    joined = streamed or official

    # (a) 非空 —— 空串在这里必红。
    assert official.strip(), f"读取失败，最终正文却是空的：{official!r}"
    # (b) 如实说明，不是糊弄过去。
    assert "没看成" in official or "没了" in official, (
        f"读取失败后没有如实说明，正文是：{official!r}"
    )
    # (c) 那句假的「看了」不许留下。
    assert "看了，" not in official, f"假的观看声称没有删掉：{official!r}"
    assert joined.strip(), f"用户实时收到的也是空串：{streamed!r}"


# ============================================================ 3. 配额耗尽


async def test_quota_exhausted_still_produces_a_non_empty_body(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """额度用完：正文非空，且是「今天没余力」那类措辞。

    `test_video_watch_quota.py` 断言的是**提示词**（喂进去的 messages），
    没有一条断言用户**收到了什么**。提示词正确而正文为空时，那边的断言
    全绿——这就是这里要补的那一格。
    """
    _provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m3f2a1>>\n我瞧瞧。",
        follow_up_reply="今天有点累，懒得看了，你直接说给我听吧。",
        watch_video=_quota_watch,
        user_text="那个视频里是什么",
    )
    streamed, official = _both_outs(events)

    assert official.strip(), f"额度用完了，最终正文却是空的：{official!r}"
    assert "累" in official or "懒得" in official or "不想" in official, (
        f"额度用完后没有按人设诚实回应，正文是：{official!r}"
    )
    # 负向那半边配对：说她看过就是谎话。
    assert "我看过了" not in official and "视频里是" not in official, (
        f"额度用完却谎称看过了：{official!r}"
    )
    assert streamed.strip(), f"用户实时收到的也是空串：{streamed!r}"


# ============================================================ 4. 普通回复


async def test_plain_reply_streams_verbatim_and_non_empty(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """不含标记的普通回复：逐字流式、非空、最终正文与原文一致。

    `test_plain_reply_is_not_held` 已经有一条 ``== "今天天气不错，我在看书。"``
    的正向断言（它不会被空串骗过）。这条的价值在于**同一个文件里**给出
    观看路径之外的非空基线：当观看测试红了时，它能回答"是不是所有轮次都空了"
    ——那个问题决定了该查观看收尾还是查累积点本身。
    """
    text = "今天天气不错，我在看书。"
    _provider, events = await _run_turn(
        logged_in,
        first_reply=text,
        user_text="你在干嘛",
    )
    streamed, official = _both_outs(events)
    deltas = _deltas(events)

    assert "".join(deltas) == text, f"普通回复的流式正文被改动了：{deltas!r}"
    assert len(deltas) > 1, f"普通回复被攒成了一段：{deltas!r}"
    assert streamed.strip(), "普通回复用户收到的实时内容是空串"
    assert official.strip(), f"普通回复的最终正文是空串：{official!r}"
    assert official == text, f"普通回复的最终正文不完整：{official!r}"
