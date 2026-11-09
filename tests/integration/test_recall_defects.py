"""四个已核实缺陷的回归测试（每条都"改坏了会红"）。

## 缺陷 1：`POST /v1/tools/{id}/call` 的 `confirmed` 曾经来自请求体

原来 ``ToolCallBody.confirmed`` 直接透传给 ``ToolRegistry.call(confirmed=...)``
——**请求体自己给自己发确认**。这条路径没有 owner 校验、也没有窗口归属校验，
打不穿纯属巧合（撤回 handler 需要 ``session`` / ``connector`` 两个非 JSON 对象）。

断言：带 ``confirmed: true`` 的请求被**结构性拒绝**（422），平台零调用；
即使退一步让 body 侥幸通过模型层，端点也必须永远传 ``confirmed=False``。

## 缺陷 2：流式出口只挡了看视频标记，没挡撤回标记

``turn._visible()`` 原来只调 ``strip_watch_markers``，于是 ``<<撤回 r3f2a1>>``
**原样出现在流式气泡里**，落库时才洗掉。

断言：任意分片（1/4/7/1000 字）下，输出都不含 ``撤回`` 字样与 ``<<`` / ``>>``。

## 缺陷 3：半截标记正则过宽，把正常正文吃掉

``r"<<[^<>]*>?$"`` 会把 ``1<<2 就是 4`` 清洗成 ``1``——她真说的话被静默删掉。

断言：``1<<2 就是 4`` / ``a << b 表示左移`` / ``《撤回》`` 原样保留；
``<<撤`` / ``<<撤回 r3`` 这种真半截仍被暂扣。

## 缺陷 4：确认与重试的并发/幂等

``confirm`` 端点原来是"先读 ``state != proposed`` 再执行"，两个并发 confirm
都能通过 → **两次真实 delete_msg**。

断言：真并发（两个独立 session 同时打）只有一个 200，另一个 409，
平台调用次数 == 1。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.contracts.v1 import DeliveryState, OperationState
from ling.domains.conversation.recall import (
    RecallRejected,
    check_recallable_message,
    drop_partial_recall_marker,
    stream_safe_recall,
    strip_recall_markers,
)
from ling.domains.media.video import PENDING_MARKER_RE, finalize_reply_text, stream_safe
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Message, Operation, Window
from ling.infra.security import new_id
from ling.kernel.marker_stream import stream_safe_all, strip_all_markers
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
CHANNEL = "onebot"
INSTANCE = "onebot-defects"
WINDOW_REF = f"{INSTANCE}:private:20009"
PLATFORM_ID = "991001"


# ---------------------------------------------------------------- 假 OneBot


class _FakeOneBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        self.calls.append({"action": action, "body": json.loads(request.content or b"{}")})
        if action == "delete_msg":
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    def delete_calls(self) -> int:
        return len([call for call in self.calls if call["action"] == "delete_msg"])


def _install(env: TestEnv, fake: _FakeOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="tok",
            secret="sec",
            self_id="10001",
            instance_id=INSTANCE,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    env.app.state.onebot_connector = connector
    return connector


async def _seed_window(env: TestEnv) -> str:
    async with session_scope() as session:
        window = Window(
            id=new_id("win"),
            actor_id=ACTOR,
            persona_id="persona_ling",
            subject_id=env.subject_id,
            title=WINDOW_REF,
            channel=CHANNEL,
            visibility="subject_private",
        )
        session.add(window)
        await session.commit()
        return window.id


async def _seed_recallable(
    env: TestEnv, *, text: str = "我刚才说错话了", age_seconds: float = 5.0
) -> tuple[str, str]:
    window_id = await _seed_window(env)
    turn_id = new_id("turn")
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with session_scope() as session:
        row = Message(
            id=new_id("msg"),
            window_id=window_id,
            turn_id=turn_id,
            role="actor",
            text=text,
            origin="model_inference",
            sequence=1,
            state="completed",
        )
        session.add(row)
        operation = Operation(
            id=new_id("op"),
            kind="delivery",
            actor_id=ACTOR,
            state=OperationState.succeeded.value,
            payload={"reply_plan": f"plan_{turn_id}", "part_ordinal": 1, "parts_total": 1},
        )
        session.add(operation)
        await session.flush()
        session.add(
            DeliveryReceiptRow(
                id=new_id("rcpt"),
                operation_id=operation.id,
                channel=CHANNEL,
                state=DeliveryState.delivered.value,
                platform_message_id=PLATFORM_ID,
                observed_at=moment,
            )
        )
        await session.commit()
        return window_id, row.id


async def _seed_proposal(window_id: str, message_id: str) -> str:
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    async with session_scope() as session:
        proposal = Operation(
            id=new_id("recall"),
            kind=RECALL_PROPOSAL_KIND,
            actor_id=ACTOR,
            state="proposed",
            payload={"window_id": window_id, "message_id": message_id, "turn_id": "t1"},
        )
        session.add(proposal)
        await session.commit()
        return proposal.id


async def _message(message_id: str) -> Message | None:
    async with session_scope() as session:
        return await session.get(Message, message_id)


# ================================================================ 缺陷 1


async def test_tool_call_body_cannot_self_confirm(logged_in: TestEnv) -> None:  # noqa: F811
    """请求体带 `confirmed: true` 时**结构性拒绝**，平台零调用。

    「改坏了会红」：只要有人把 `confirmed` 字段加回 `ToolCallBody`（或者把
    `extra="forbid"` 去掉），这条就会从 422 变成 200 —— 而 200 意味着
    不可逆工具被请求体自己批准了。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)

    response = await logged_in.client.post(
        "/v1/tools/message.recall/call",
        json={
            "args": {"window_id": window_id, "message_id": message_id},
            "confirmed": True,
        },
    )

    assert response.status_code == 422, response.text
    assert fake.delete_calls() == 0, "请求体自证确认竟然真的执行了不可逆工具"
    row = await _message(message_id)
    assert row is not None and row.recalled_at is None

    # 错误信息要能指路（不能只说"字段多余"）
    assert "确认" in json.dumps(response.json(), ensure_ascii=False)


async def test_tool_call_without_confirmed_needs_confirmation(logged_in: TestEnv) -> None:  # noqa: F811
    """不带 `confirmed` 的请求：不可逆工具**最多**走到 needs_confirmation。

    需要先授予 `device`——权限硬门在确认之前：没有授权时连
    ``needs_confirmation`` 都走不到，直接 403（那是另一道闸，见
    `test_tool_policy_gate.py`）。这条验的是"授权之后仍然只能提议"。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)
    await logged_in.client.put("/v1/consents", json={"scope": "device", "granted": True})

    response = await logged_in.client.post(
        "/v1/tools/message.recall/call",
        json={"args": {"window_id": window_id, "message_id": message_id}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "needs_confirmation", body
    assert body["output"] is None
    assert "确认" in body["detail"], "要告诉调用方去哪儿确认"
    assert fake.delete_calls() == 0


async def test_tool_call_schema_has_no_confirmed_field(logged_in: TestEnv) -> None:  # noqa: F811
    """OpenAPI 里不能出现 `ToolCallBody.confirmed`。

    「改坏了会红」：字段一旦回到模型上，`/openapi.json` 里就有它，前端会照着
    它写代码——那时再删字段就是破坏性变更，所以先在这里钉住。
    """
    schema = await logged_in.client.get("/api/openapi.json")
    assert schema.status_code == 200, schema.text
    body_schema = schema.json()["components"]["schemas"].get("ToolCallBody")
    assert body_schema is not None, "ToolCallBody 不在 OpenAPI 里"
    assert "confirmed" not in body_schema["properties"]


async def test_read_only_tools_still_work(logged_in: TestEnv) -> None:  # noqa: F811
    """收紧不能把只读工具一起挡掉（它们本来就不需要确认）。"""
    response = await logged_in.client.post(
        "/v1/tools/diagnostics.echo/call", json={"args": {"text": "你好"}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "succeeded"
    assert body["output"] == "你好"


# ================================================================ 缺陷 2


def _stream_through(chunks: list[str]) -> str:
    """按给定的分片喂流式过滤器，返回用户**真正看到**的全部文字。

    用与 `turn._visible` 完全相同的两步（`stream_safe_all` + `strip_all_markers`），
    只是把它抽出来便于按分片驱动。
    """
    hold = ""
    visible = ""
    for chunk in chunks:
        ready, hold = stream_safe_all(hold + chunk)
        visible += strip_all_markers(ready)
    return visible


@pytest.mark.parametrize("size", [1, 4, 7, 1000])
def test_recall_marker_never_leaks_to_the_stream(size: int) -> None:
    """任意分片下，流式输出都不含 `撤回` 字样与 `<<` / `>>`。

    「改坏了会红」：把 `_visible` 改回只调 `strip_watch_markers`，1/4/7 字分片
    会把 ``<<撤回 r3f2a1>>`` 原样吐出来。
    """
    text = "我想想，这句说错了<<撤回 r3f2a1>>后来就算了"
    chunks = [text[i : i + size] for i in range(0, len(text), size)]

    visible = _stream_through(chunks)

    assert "撤回" not in visible, f"分片 {size}：撤回标记漏到气泡里：{visible!r}"
    assert "<<" not in visible and ">>" not in visible, visible
    assert "r3f2a1" not in visible
    # 正文不能被吃掉
    assert visible.startswith("我想想，这句说错了")
    assert "后来就算了" in visible


@pytest.mark.parametrize("size", [1, 4, 7, 1000])
def test_watch_marker_never_leaks_either(size: int) -> None:
    """看视频标记仍然不漏（收紧正则不能把它改坏）。"""
    text = "等我看一眼<<看视频 m3>>再告诉你"
    chunks = [text[i : i + size] for i in range(0, len(text), size)]

    visible = _stream_through(chunks)

    assert "看视频" not in visible, f"分片 {size}：{visible!r}"
    assert "<<" not in visible and ">>" not in visible
    assert "等我看一眼" in visible and "再告诉你" in visible


@pytest.mark.parametrize("size", [1, 4, 7, 1000])
def test_both_markers_in_one_reply(size: int) -> None:
    """一条回复里同时出现两种标记：两个都要剥，且不能互相吞。"""
    text = "先<<撤回 r3f2a1>>再看<<看视频 m3>>最后说"
    chunks = [text[i : i + size] for i in range(0, len(text), size)]

    visible = _stream_through(chunks)

    assert "撤回" not in visible and "看视频" not in visible, visible
    assert "<<" not in visible and ">>" not in visible
    assert visible == "先再看最后说", visible


def test_half_marker_is_held_then_released() -> None:
    """半截标记先扣住；下一片补齐成完整标记后被剥掉（不是发出去）。"""
    hold = ""
    ready, hold = stream_safe_all(hold + "说错了<<撤")
    assert ready == "说错了", ready
    assert hold == "<<撤"

    # 补齐成完整标记：标记本身被剥掉，同一片里的正文照常出去
    ready_after, hold = stream_safe_all(hold + "回 r3>>算了")
    assert strip_all_markers(ready_after) == "算了", (
        f"标记没剥干净或正文被吞：{ready_after!r}"
    )
    assert hold == ""


def test_recall_stream_safe_uses_the_shared_pending_regex() -> None:
    """撤回那条的"半截"判定必须与看视频**共用**同一个正则。

    「改坏了会红」：以前两处各写一份 ``r"<<[^<>]*>?$"``，两份迟早走散。
    现在**唯一的定义**在 `kernel/marker_stream.py`（放 kernel 而不是从 media
    取，是因为域之间不许互相 import——`test_domain_boundaries` 会拦）。
    """
    from ling.domains.conversation import recall as recall_module

    assert recall_module._PENDING_RECALL_RE is PENDING_MARKER_RE
    # 看视频那边也必须是同一个对象，不是"内容相同的两份"
    from ling.domains.media import video as video_module

    assert video_module.PENDING_MARKER_RE is PENDING_MARKER_RE


# ================================================================ 缺陷 3


@pytest.mark.parametrize(
    "text",
    [
        "1<<2 就是 4",
        "a << b 表示左移",
        "《撤回》",
        "左移两位等于乘四：x << 2",
        "他说“<<”这个符号",
    ],
)
def test_normal_text_with_double_angle_is_kept(text: str) -> None:
    """正常正文里的 ``<<`` 原样保留（以前会被静默删掉）。

    「改坏了会红」：正则退回 ``r"<<[^<>]*>?$"`` 时 ``1<<2 就是 4`` 会变成 ``1``。
    """
    assert finalize_reply_text(text) == text
    assert drop_partial_recall_marker(text) == text
    # 流式逐字也不许吞
    assert _stream_through(list(text)) == text


@pytest.mark.parametrize(
    ("text", "expected_stream_hold"),
    [
        ("她说<<撤", "<<撤"),
        ("她说<<撤回 r3", "<<撤回 r3"),
        ("她说<<看视", "<<看视"),
        ("她说<<看视频 m", "<<看视频 m"),
        ("她说<<", "<<"),
    ],
)
def test_real_half_marker_is_still_held(text: str, expected_stream_hold: str) -> None:
    """真半截标记仍被暂扣（收紧不能宽松到什么都放出去）。"""
    ready, hold = stream_safe(text)
    assert hold == expected_stream_hold, (ready, hold)
    assert ready == "她说"

    ready_recall, hold_recall = stream_safe_recall(text)
    assert hold_recall == expected_stream_hold


def test_pending_marker_regex_requires_a_known_verb() -> None:
    """正则本身：``<<`` 后面跟非动词的内容**不是**标记。"""
    assert PENDING_MARKER_RE.match("<<撤")
    assert PENDING_MARKER_RE.match("<<撤回 r3")
    assert PENDING_MARKER_RE.match("<<看视频 m3")
    assert PENDING_MARKER_RE.match("<<")  # 光秃秃两个尖括号也要扣
    assert not PENDING_MARKER_RE.match("<<2")
    assert not PENDING_MARKER_RE.match("<< b 表示左移")
    # 跨行不算标记（标记是单行的）
    assert not PENDING_MARKER_RE.match("<<撤\n回")


# ================================================================ 缺陷 4


def test_check_recallable_message_rejects_already_recalled() -> None:
    """已撤回的直接拒（防御性，别只靠上游判）。"""
    row = Message(
        id="msg_x",
        window_id="win_x",
        role="actor",
        text="有内容",
        origin="model_inference",
        state="completed",
        sequence=1,
        recalled_at=datetime.now(UTC),
    )
    with pytest.raises(RecallRejected) as exc:
        check_recallable_message(row)
    assert exc.value.code == "already_recalled"


async def test_concurrent_confirm_fires_delete_msg_only_once(logged_in: TestEnv) -> None:  # noqa: F811
    """**真并发**两个 confirm：只有一个 200，另一个 409，平台只收到一次。

    「改坏了会红」：把确认端点改回"先读 state 再执行"，两个请求都会通过那次
    检查，`delete_calls()` 变成 2。撤回不可逆，多一次就是多撤一条/对着已删的
    消息再撤一次。

    并发是真的：两个请求各走各的 HTTP 连接（`gather` 同时发），服务端各拿一个
    数据库会话，靠 `UPDATE … WHERE state='proposed'` 的行级语义分胜负。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)
    proposal_id = await _seed_proposal(window_id, message_id)

    url = f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/confirm"
    first, second = await asyncio.gather(
        logged_in.client.post(url),
        logged_in.client.post(url),
    )

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (first.status_code, first.text, second.status_code, second.text)
    assert fake.delete_calls() == 1, (
        f"平台被撤了 {fake.delete_calls()} 次——并发确认打穿了幂等"
    )
    row = await _message(message_id)
    assert row is not None and row.recalled_at is not None


async def test_concurrent_confirm_across_sessions_fires_once(logged_in: TestEnv) -> None:  # noqa: F811
    """两个**独立数据库会话**并发确认：同样只发一次 `delete_msg`。

    与上一条的区别：这条不共用 `TestEnv.client` 的连接池语义，两个请求各自
    开 session（`DbSession` 依赖每请求一个），验证的是**数据库层**的行级判据，
    而不是进程内的锁。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)
    proposal_id = await _seed_proposal(window_id, message_id)

    url = f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/confirm"
    results = await asyncio.gather(
        *[logged_in.client.post(url) for _ in range(3)],
        return_exceptions=True,
    )
    statuses = sorted(
        getattr(item, "status_code", -1) for item in results
    )

    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 2, statuses
    assert fake.delete_calls() == 1, f"平台被撤了 {fake.delete_calls()} 次"


async def test_confirm_after_confirm_is_409_without_platform_call(logged_in: TestEnv) -> None:  # noqa: F811
    """串行重复确认：第二次 409，平台调用次数不变。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)
    proposal_id = await _seed_proposal(window_id, message_id)

    url = f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/confirm"
    first = await logged_in.client.post(url)
    assert first.status_code == 200, first.text
    assert fake.delete_calls() == 1

    second = await logged_in.client.post(url)
    assert second.status_code == 409, second.text
    assert fake.delete_calls() == 1, "重复确认又发了一次撤回到平台"


async def test_recall_twice_on_the_same_message_does_not_refire(logged_in: TestEnv) -> None:  # noqa: F811
    """同一条消息撤两次（走手端点）：第二次拒，平台调用次数不变。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)
    url = f"/v1/conversations/{window_id}/messages/{message_id}/recall"

    first = await logged_in.client.post(url)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "recalled"
    assert fake.delete_calls() == 1

    second = await logged_in.client.post(url)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "already_recalled"
    assert fake.delete_calls() == 1


async def test_uncertain_then_retry_is_documented_as_not_idempotent() -> None:
    """`uncertain` 的重试语义**如实记录**：允许重试，但会再发一次 delete_msg。

    这条不是"行为测试"而是"不许撒谎"的钉子：`perform_recall` 的 docstring
    必须写明重试可能对着已删除的消息再撤一次，且平台回 not_found 是可接受的。
    谁把它改写成"重试是幂等的"（例如悄悄写 recalled_at），这条会红。
    """
    from ling.domains.conversation.recall import perform_recall

    doc = perform_recall.__doc__ or ""
    assert "uncertain" in doc.lower()
    assert "重试" in doc
    assert "not" in doc.lower() or "不幂等" in doc
    assert "幂等" in doc
    # 必须说清"会再发一次"，而不是含糊地讲"可以重试"
    assert "再撤一次" in doc or "再发一次" in doc


async def test_message_row_is_never_deleted_by_recall(logged_in: TestEnv) -> None:  # noqa: F811
    """撤回只标记不删行（缺陷 4 的"幂等"不能靠删行实现）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in, text="原话")

    await logged_in.client.post(
        f"/v1/conversations/{window_id}/messages/{message_id}/recall"
    )

    async with session_scope() as session:
        rows = (
            await session.execute(select(Message).where(Message.id == message_id))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "原话"
    assert rows[0].recalled_at is not None


def test_strip_recall_markers_is_idempotent() -> None:
    """剥离幂等：任何出口都可以再剥一次。"""
    once = strip_recall_markers("说错了<<撤回 r3f2a1>>算了")
    assert once == "说错了算了"
    assert strip_recall_markers(once) == once


# ---------------------------------------------------------------- 缺陷 2（端到端）


class _MarkerProvider:
    """会**写出撤回标记**的假供应商，按固定分片流式吐字。

    为什么不用现成的 `FakeProvider`：它的回复是按输入拼的，没法让它吐出一个
    标记；而缺陷 2 就发生在"模型真的把它流出来了"这条路上。这里把 `deltas`
    直接喂进流，`chunk_sizes` 用来复现"标记被切开"的每一种切法。
    """

    provider_id = "fake"
    kind = "fake"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    async def stream(self, request: object):  # noqa: ANN001, ANN201
        from ling.ports.model import ModelChunk

        for piece in self._deltas:
            yield ModelChunk(delta=piece)
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self):  # noqa: ANN201
        from ling.ports.model import ProbeResult

        return ProbeResult(ok=True, detail="marker", latency_ms=0, models=["deterministic"])


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _streamed_text(env: TestEnv, window_id: str, deltas: list[str]) -> str:
    """跑一轮真实 turn，把**客户端真正收到**的 delta 拼起来返回。

    断言点必须是"浏览器收到的那些片段"，而不是 `_visible()` 的返回值——
    后者是内部函数，改坏了它照样能"看起来对"。这里走完整的
    `TurnCoordinator.run_turn` → 事件队列 → `delta` 事件。
    """
    import asyncio

    from ling.kernel.arbiter import SpeakerArbiter
    from ling.kernel.turn import TurnCoordinator
    from ling.models.registry import ProviderRegistry
    from ling.models.router import ModelRouter

    # 把 provider 注入注册表的缓存：`_build` 只认 `kind="fake"` 的 FakeProvider，
    # 而这条测试需要"模型真的吐出一个撤回标记"，所以要绕过构造。
    provider = _MarkerProvider(deltas)
    registry = ProviderRegistry(lambda _pid: None)
    from ling.models.registry import ProviderRegistry as _Reg

    cfg = ProviderConfig(
        provider_id="fake", kind="fake", enabled=True, extra={"chunk_delay": 0.0}
    )
    registry._cache["fake"] = provider  # noqa: SLF001 - 测试注入
    registry._signatures["fake"] = registry._signature(cfg)  # noqa: SLF001
    assert isinstance(registry, _Reg)

    coordinator = TurnCoordinator(
        router=ModelRouter(registry),
        actor_id=ACTOR,
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        arbiter=SpeakerArbiter(default_lease_seconds=30),
        stream_log=env.app.state.stream_log,
    )

    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{new_id('x')[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text="你说吧", turn_id=turn.id)
        turn_id = turn.id

    config = EffectiveConfig(
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

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text="你说吧",
        config=config,
    )
    chunks: list[str] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=30)
        if item is None:
            break
        if item.event == "delta":
            chunks.append(str(item.data.get("text") or ""))
    return "".join(chunks)


@pytest.mark.parametrize("size", [1, 2, 4, 7, 1000])
async def test_recall_marker_never_reaches_the_client(
    logged_in: TestEnv, size: int  # noqa: F811
) -> None:
    """**端到端**：模型吐出 ``<<撤回 rX>>``，客户端收到的片段里不能有它。

    「改坏了会红」：把 `_visible` 改回只调 `strip_watch_markers`，
    1/2 字分片下 ``<<撤回 …>>`` 会整段出现在 delta 事件里。
    """
    _window_id, message_id = await _seed_recallable(logged_in)
    reply = "我说错了<<撤回 r3f2a1>>你当我没说过"

    visible = await _streamed_text(logged_in, _window_id, _split(reply, size))

    assert "撤回" not in visible, f"分片 {size}：标记漏给客户端了：{visible!r}"
    assert "<<" not in visible and ">>" not in visible, visible
    assert "r3f2a1" not in visible
    assert "我说错了" in visible and "你当我没说过" in visible, visible
    # 占位变量与缺陷无关，避免 lint 报未使用
    assert message_id


@pytest.mark.parametrize("size", [1, 4, 1000])
async def test_watch_marker_never_reaches_the_client(
    logged_in: TestEnv, size: int  # noqa: F811
) -> None:
    """看视频标记端到端也不漏（收紧正则不能把它改坏）。"""
    window_id, _message_id = await _seed_recallable(logged_in)
    reply = "我看一眼<<看视频 m3>>再说"

    visible = await _streamed_text(logged_in, window_id, _split(reply, size))

    assert "看视频" not in visible, f"分片 {size}：{visible!r}"
    assert "<<" not in visible and ">>" not in visible
    assert "我看一眼" in visible and "再说" in visible


async def test_normal_double_angle_survives_the_stream(logged_in: TestEnv) -> None:  # noqa: F811
    """正常正文里的 ``<<`` 端到端原样到客户端（缺陷 3 的端到端版本）。"""
    window_id, _message_id = await _seed_recallable(logged_in)
    reply = "1<<2 就是 4"

    visible = await _streamed_text(logged_in, window_id, _split(reply, 1))

    assert visible == reply, f"正常正文被流式过滤吃掉了：{visible!r}"
