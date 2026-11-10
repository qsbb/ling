"""延后意图在**轮次内部**的产生、清洗与事件顺序。

## 这个文件钉的四件事（对应 lead 的 1–4 条）

1. **`turn.deferred` 先于 `turn.completed`**：前端靠这个顺序决定显示"她说等会儿"
   还是"她回了一条空的"。只看"有没有发"不够，必须断言**顺序**。
2. **`clamped` / `requested_seconds` 在 SSE 载荷里**：999 秒被钳到 300 时，
   载荷必须同时带三个数（要的、用的、是否被钳）。只在库里记 `clamped=true`
   等于把系统上限说成她的选择——用户看到的仍是"她等了 5 分钟"。
3. **坏格式不影响正常回复**：标记 + 正文、非数字、负数、缺秒数、多个标记
   → 剥掉标记、正文照发、立即回复，且**不产生延后记录**。
4. **`reply_defer_rejected` 与 `reply_deferred` 严格分开**：坏格式记前者，
   绝不记后者。混在一起会让"她真的延后了"这个统计失去意义。

## 为什么用可编程 provider 而不是 `FakeProvider`

`FakeProvider` 的输出由输入哈希决定，无法让它"只吐一个标记"。这里的用例
需要精确控制模型输出（`<<等会回 900>>` 这种），所以用一个按脚本吐文本的
最小 provider——它仍然走**完整的** router → turn 链路，不是把 turn 层 mock 掉。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.domains.deferred.state import MAX_DELAY_SECONDS
from ling.infra.db import session_scope
from ling.infra.models import DeferredReply, Message
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

CHUNK_DELAY = 0.0


class _ScriptedProvider:
    """按脚本吐一段固定文本。走真实 router，不 mock turn 层。"""

    provider_id = "scripted"

    def __init__(self, text: str) -> None:
        self.text = text

    async def stream(self, request):  # noqa: ANN001, ANN201
        # 每 4 字一段，模拟真实流式（这样半截标记的暂扣路径真的会被走到）
        for index in range(0, len(self.text), 4):
            await asyncio.sleep(CHUNK_DELAY)
            yield ModelChunk(delta=self.text[index : index + 4])
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, detail="scripted", latency_ms=0, models=["deterministic"])


class _ScriptedRegistry(ProviderRegistry):
    def __init__(self, text: str) -> None:
        super().__init__(lambda _pid: None)
        self.provider = _ScriptedProvider(text)

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
                extra={"chunk_delay": CHUNK_DELAY},
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="scripted", model_id="deterministic"
            )
        },
        test_mode=True,
    )


async def _new_window(client, title: str = "延后意图") -> str:
    response = await client.post("/v1/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _make_turn(window_id: str, text: str) -> str:
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=text, turn_id=turn.id)
        return turn.id


async def _drain(queue) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            return events
        events.append((item.event, dict(item.data)))


async def _run(env: TestEnv, *, window_id: str, text: str, model_output: str):
    coordinator = TurnCoordinator(
        router=ModelRouter(_ScriptedRegistry(model_output)),
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
    )
    turn_id = await _make_turn(window_id, text)
    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text=text,
        config=_config(),
        attempt=1,
    )
    events = await _drain(queue)
    return turn_id, events


async def _deferred_rows(window_id: str) -> list[DeferredReply]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(DeferredReply).where(DeferredReply.window_id == window_id)
            )
        ).scalars()
        return list(rows)


async def _actor_texts(window_id: str) -> list[str]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Message.text).where(
                    Message.window_id == window_id, Message.role == "actor"
                )
            )
        ).scalars()
        return list(rows)


# ------------------------------------------------------------------ 事件顺序


async def test_deferred_event_precedes_completed(logged_in: TestEnv) -> None:  # noqa: F811
    """**`turn.deferred` 必须先于 `turn.completed`**（lead 第 1 条要求）。

    顺序反了，前端会先收到"这一轮结束了、没有消息"（渲染成空回复），再收到
    "她在等"——用户看到的是一次闪烁的空回复。只断言"两个事件都在"抓不到它。
    """
    window_id = await _new_window(logged_in.client)
    _, events = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 90>>"
    )
    names = [name for name, _ in events]
    assert "turn.deferred" in names, f"没有发 turn.deferred：{names}"
    assert "turn.completed" in names, f"没有发 turn.completed：{names}"
    assert names.index("turn.deferred") < names.index("turn.completed"), (
        f"turn.deferred 必须在前，实际顺序：{names}"
    )


async def test_deferred_turn_delivers_no_actor_message(logged_in: TestEnv) -> None:  # noqa: F811
    """延后那一轮**不产生她的消息**——标记不进库、不外发。"""
    window_id = await _new_window(logged_in.client)
    await _run(logged_in, window_id=window_id, text="在吗", model_output="<<等会回 90>>")
    assert await _actor_texts(window_id) == []
    _, second = await _run(
        logged_in, window_id=window_id, text="还在吗", model_output="<<等会回 30>>"
    )
    for _, data in second:
        assert "等会回" not in str(data)


async def test_user_message_marked_deferred(logged_in: TestEnv) -> None:  # noqa: F811
    """用户那条消息的 `handled_as` 必须是 `deferred`。

    默认的三条分支都在看"有没有正文"，延后必然落进"洗空了→failed"那条。
    库里写着 failed、用户看到的却是"她说等会儿"——两处矛盾。
    """
    window_id = await _new_window(logged_in.client)
    turn_id, _ = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 90>>"
    )
    async with session_scope() as session:
        row = await session.scalar(
            select(Message).where(Message.turn_id == turn_id, Message.role == "user")
        )
    assert row is not None
    assert row.handled_as == "deferred", f"handled_as 是 {row.handled_as!r}"


# ------------------------------------------------------------------ 钳位可见


async def test_clamped_values_reach_the_client(logged_in: TestEnv) -> None:  # noqa: F811
    """999 秒 → `requested_seconds=999, applied_seconds=300, clamped=true`。

    这是 lead 第 2 条：只在库里记 `clamped` 不够，SSE 载荷必须带上，前端才
    说得出"她本来想等更久"。把系统上限伪装成她的选择是本项目明令禁止的。
    """
    window_id = await _new_window(logged_in.client)
    _, events = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 999>>"
    )
    payload = next(data for name, data in events if name == "turn.deferred")
    assert payload["requested_seconds"] == 999, payload
    assert payload["applied_seconds"] == MAX_DELAY_SECONDS, payload
    assert payload["clamped"] is True, payload
    assert payload["due_at"] and payload["hard_deadline_at"], payload


async def test_unclamped_values_are_honest(logged_in: TestEnv) -> None:  # noqa: F811
    """没被钳时 `clamped` 必须是 False——不能为了保险一律标 true。"""
    window_id = await _new_window(logged_in.client)
    _, events = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 45>>"
    )
    payload = next(data for name, data in events if name == "turn.deferred")
    assert payload["requested_seconds"] == 45
    assert payload["applied_seconds"] == 45
    assert payload["clamped"] is False


# ------------------------------------------------------------------ 坏格式


@pytest.mark.parametrize(
    "model_output",
    [
        "<<等会回>>我现在就说",
        "<<等会回 abc>>我在的",
        "<<等会回 -5>>嗯嗯",
        "<<等会回 30>><<等会回 60>>那我随便说一句",
        "<<等会回 90>>我其实现在就想说两句",
    ],
    ids=["empty", "non_numeric", "negative", "multiple", "body_present"],
)
async def test_bad_format_replies_immediately(
    logged_in: TestEnv, model_output: str  # noqa: F811
) -> None:
    """坏格式 → 剥掉标记、正文照发、立即回复（lead 第 3 条）。

    **不挂起**是硬要求：解析不出合法标记时静默不回，等于把她这一轮吞掉。
    """
    window_id = await _new_window(logged_in.client)
    turn_id, events = await _run(
        logged_in, window_id=window_id, text="在吗", model_output=model_output
    )
    names = [name for name, _ in events]
    assert "turn.deferred" not in names, f"坏格式却延后了：{model_output!r}"
    assert "turn.completed" in names

    texts = await _actor_texts(window_id)
    assert len(texts) == 1, f"应当立即回复一条，实际 {texts}"
    assert "等会回" not in texts[0], f"标记漏给用户了：{texts[0]!r}"
    assert await _deferred_rows(window_id) == [], "坏格式不该登记延后记录"


async def test_bad_format_does_not_silently_swallow_text(logged_in: TestEnv) -> None:  # noqa: F811
    """`<<等会回 90>>我其实现在就想说两句` → 正文**一个字都不许少**。

    标记+正文按坏格式处理（不延后、正文照发），因为丢掉正文会让用户看到一轮
    空回复——那是可观察的损坏；而"嘴上说等会又说了话"最多是语气不完美。
    """
    window_id = await _new_window(logged_in.client)
    await _run(
        logged_in,
        window_id=window_id,
        text="在吗",
        model_output="<<等会回 90>>我其实现在就想说两句",
    )
    texts = await _actor_texts(window_id)
    assert len(texts) == 1
    assert "我其实现在就想说两句" in texts[0], f"正文被吞了：{texts[0]!r}"


# ------------------------------------------------------------------ 日志三分


async def test_bad_format_logs_rejected_not_deferred(logged_in: TestEnv) -> None:  # noqa: F811
    """坏格式记 `reply_defer_rejected`，**绝不**记 `reply_deferred`（lead 第 4 条）。

    混在一起会让"她真的延后了"这个统计失去意义：用户看到的每一次"她答应等会儿"
    都算进去，而其中一部分其实立刻回了。
    """
    from ling.logging_setup import recent_logs

    window_id = await _new_window(logged_in.client)
    turn_id, _ = await _run(
        logged_in,
        window_id=window_id,
        text="在吗",
        model_output="<<等会回 90>>我其实现在就想说两句",
    )

    # ## 为什么不数"新增了哪些 action"
    #
    # 日志缓冲是**进程全局**的 500 条环形缓冲：要可靠地做"前后差集"，基准必须
    # 是紧邻的 before/after，且中间不能让出给别的用例。这在同一个文件里跑多条
    # 用例时无法保证（每条用例都会新建 app、打几十条 http_request_completed）。
    #
    # 更稳的做法是**按这一轮的 turn_id 精确筛**：`reply_defer_rejected` 的
    # attributes 里就带着 turn_id，直接把"这一轮都记了什么"取出来即可，
    # 不依赖任何时间窗口或缓冲状态。
    mine = [
        entry
        for entry in recent_logs(limit=500)
        if str((entry.attributes or {}).get("turn_id") or "") == turn_id
    ]
    actions = [entry.event_name for entry in mine]
    assert "reply_defer_rejected" in actions, (
        f"没有记 reply_defer_rejected（这一轮只有 {actions}）"
    )
    assert "reply_deferred" not in actions, "坏格式被记成了'她真的延后了'"


async def test_valid_defer_logs_reply_deferred(logged_in: TestEnv) -> None:  # noqa: F811
    """合法延后记 `reply_deferred`，且 `cause=volition`（不是 resource/system）。"""
    from ling.logging_setup import recent_logs

    window_id = await _new_window(logged_in.client)
    turn_id, _ = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 90>>"
    )
    # 与上一条同一理由：按 turn_id 精确定位，不依赖环形缓冲的时间窗口。
    matched = [
        entry
        for entry in recent_logs(limit=500)
        if entry.event_name == "reply_deferred"
        and str((entry.attributes or {}).get("turn_id") or "") == turn_id
    ]
    assert matched, "合法延后没有记 reply_deferred"
    attributes = matched[-1].attributes
    assert attributes.get("cause") == "volition", attributes
    assert attributes.get("requested_seconds") == 90, attributes


# ------------------------------------------------------------------ 前后端契约


async def test_payload_fields_match_frontend_contract(logged_in: TestEnv) -> None:  # noqa: F811
    """`turn.deferred` 的载荷字段必须与前端 `TurnDeferredData` 一一对上。

    为什么值得一条专门用例：后端加字段、前端漏掉，或者反过来，**两边都不会
    报错**——前端只会在界面上少显示一句话，而没人知道那里本该有内容。这类
    "契约漂移"只能靠把字段名写死在测试里挡住。

    字段清单与 `clients/web-console/src/types.ts::TurnDeferredData` 保持一致。
    """
    window_id = await _new_window(logged_in.client)
    _, events = await _run(
        logged_in, window_id=window_id, text="在吗", model_output="<<等会回 999>>"
    )
    payload = next(data for name, data in events if name == "turn.deferred")

    required = {
        "turn_id",
        "delay_id",
        "requested_seconds",
        "applied_seconds",
        "clamped",
        "due_at",
        "hard_deadline_at",
        "note",
    }
    missing = required - set(payload)
    assert not missing, f"载荷缺字段（前端会少显示东西）：{sorted(missing)}"

    # 类型也要对：前端按 number/boolean 用，字符串会让 `formatDuration` 静默变 NaN。
    assert isinstance(payload["requested_seconds"], int)
    assert isinstance(payload["applied_seconds"], int)
    assert isinstance(payload["clamped"], bool)
    # 时刻必须是可被 `new Date()` 解析的 ISO 串。
    for key in ("due_at", "hard_deadline_at"):
        assert isinstance(payload[key], str) and "T" in payload[key], payload[key]
