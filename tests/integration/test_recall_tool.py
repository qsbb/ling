"""撤回注册成工具：模型只能**提议**，用户确认才执行。

用户拍板「撤回消息只做注册工具开放给 llm」。这里的"开放"有明确边界：

1. `message.recall` 出现在 `GET /v1/tools`，`requires_confirmation=True`；
2. 模型在回复里写 ``<<撤回 rXXXX>>`` → 代码落一条**待确认提议**，不执行；
3. 用户在网页点确认 → 走**同一个工具 handler**（`confirmed=True`）→ 五道闸；
4. 不确认 → **平台零调用**；
5. 标记永远从正文里剥掉（用户看不到说明书）。

## 为什么这些断言长这样

`requires_confirmation` 不是在 handler 里 `if not confirmed: return` 实现的，
而是在 `ToolRegistry.call` 里——**handler 根本不会被进**。所以"不确认就零调用"
这条必须在 registry 层断言（`fake.delete_calls() == 0`），不能只断 handler 的
返回值：后者即使通过，也证明不了"平台没被碰"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.capabilities.tools import RECALL_TOOL_ID, ToolOutcome, build_default_registry
from ling.contracts.v1 import DeliveryState, OperationState
from ling.domains.conversation.recall import (
    finalize_recall_text,
    parse_recall_markers,
    recall_ref_for,
    recallable_refs,
    resolve_recall_ref,
)
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Message, Operation, Window
from ling.infra.security import new_id
from ling.models.fake import FakeProvider
from ling.models.registry import ProviderRegistry
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
CHANNEL = "onebot"
INSTANCE = "onebot-recall-tool"
WINDOW_REF = f"{INSTANCE}:private:20003"
PLATFORM_ID = "881001"


# ---------------------------------------------------------------- 假 OneBot


class _FakeOneBot:
    """记录 ``delete_msg``；`calls` 是"平台到底被碰了几次"的唯一证据。"""

    def __init__(self, *, mode: str = "ok") -> None:
        self.calls: list[dict] = []
        self.mode = mode

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append({"action": action, "body": body})
        if action == "delete_msg":
            if self.mode == "ok":
                return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})
            if self.mode == "timeout":
                raise httpx.ConnectTimeout("boom", request=request)
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


# ---------------------------------------------------------------- 种子


def _config() -> EffectiveConfig:
    """测试用的最小配置：假 provider、确定性模型。"""
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


async def _seed_message(
    window_id: str,
    *,
    role: str = "actor",
    text: str = "我刚才说错话了",
    origin: str = "model_inference",
    turn_id: str | None = None,
    sequence: int = 1,
) -> str:
    async with session_scope() as session:
        row = Message(
            id=new_id("msg"),
            window_id=window_id,
            turn_id=turn_id,
            role=role,
            text=text,
            origin=origin,
            sequence=sequence,
            state="completed",
        )
        session.add(row)
        await session.commit()
        return row.id


async def _seed_delivery(
    *, turn_id: str, platform_id: str | None = PLATFORM_ID, observed_at: datetime | None = None
) -> str:
    moment = observed_at or datetime.now(UTC)
    async with session_scope() as session:
        operation = Operation(
            id=new_id("op"),
            kind="delivery",
            actor_id=ACTOR,
            subject_id=None,
            state=OperationState.succeeded.value,
            payload={
                "channel": CHANNEL,
                "instance": INSTANCE,
                "window_ref": WINDOW_REF,
                "reply_plan": f"plan_{turn_id}",
                "part_ordinal": 1,
                "parts_total": 1,
            },
        )
        session.add(operation)
        await session.flush()
        session.add(
            DeliveryReceiptRow(
                id=new_id("rcpt"),
                operation_id=operation.id,
                channel=CHANNEL,
                state=DeliveryState.delivered.value,
                platform_message_id=platform_id,
                observed_at=moment,
            )
        )
        await session.commit()
        return operation.id


async def _seed_recallable(
    env: TestEnv, *, text: str = "我刚才说错话了", age_seconds: float = 5.0
) -> tuple[str, str]:
    window_id = await _seed_window(env)
    turn_id = new_id("turn")
    message_id = await _seed_message(window_id, text=text, turn_id=turn_id)
    await _seed_delivery(
        turn_id=turn_id, observed_at=datetime.now(UTC) - timedelta(seconds=age_seconds)
    )
    return window_id, message_id


async def _message(message_id: str) -> Message | None:
    async with session_scope() as session:
        return await session.get(Message, message_id)


# ---------------------------------------------------------------- 1. 注册


async def test_recall_tool_is_registered_with_confirmation(logged_in: TestEnv) -> None:
    """`GET /v1/tools` 里能看到它，且 `requires_confirmation=True`。"""
    response = await logged_in.client.get("/v1/tools")
    assert response.status_code == 200, response.text
    tools = {item["tool_id"]: item for item in response.json()["tools"]}

    assert RECALL_TOOL_ID in tools, "撤回工具没注册"
    spec = tools[RECALL_TOOL_ID]
    assert spec["risk"] == "irreversible"
    assert spec["requires_confirmation"] is True
    assert spec["reversible"] is False
    # 描述要是人话、说清"需要确认"
    assert "确认" in spec["description"]


def test_default_registry_keeps_irreversible_gate_for_others() -> None:
    """逐次授权只对撤回这一条生效；别的不可逆工具仍然注册不进来。"""
    from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec

    async def handler(args):  # noqa: ANN001
        return "x"

    registry = ToolRegistry()
    with pytest.raises(PermissionError):
        registry.register(
            ToolSpec(
                tool_id="other.irreversible",
                version="1",
                description="别的不可逆工具",
                risk=ToolRisk.irreversible,
                requires_confirmation=True,
                reversible=False,
            ),
            handler,
        )
    # 默认注册表里只有撤回这一条不可逆工具
    specs = build_default_registry().list_specs()
    irreversible = [s.tool_id for s in specs if s.risk is ToolRisk.irreversible]
    assert irreversible == [RECALL_TOOL_ID]


# ---------------------------------------------------------------- 2. 不确认 = 零调用


async def test_unconfirmed_call_never_reaches_the_platform(
    logged_in: TestEnv,
) -> None:
    """`requires_confirmation=True` 的防线在**注册表**里，不在 handler 里。

    所以这条断言的落点是 `fake.delete_calls() == 0`——handler 连进都没进。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)

    registry = build_default_registry()
    result = await registry.call(
        RECALL_TOOL_ID,
        {"window_id": window_id, "message_id": message_id},
        confirmed=False,
    )
    assert result.outcome is ToolOutcome.needs_confirmation
    assert result.output == ""
    assert fake.delete_calls() == 0, "没确认却碰了平台"

    row = await _message(message_id)
    assert row is not None and row.recalled_at is None


async def test_confirmed_call_executes_exactly_once(logged_in: TestEnv) -> None:
    """用户确认后 → 走同一个 handler → 平台只收到**一次** `delete_msg`。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)

    async with session_scope() as session:
        registry = build_default_registry()
        result = await registry.call(
            RECALL_TOOL_ID,
            {
                "window_id": window_id,
                "message_id": message_id,
                "session": session,
                "connector": logged_in.app.state.onebot_connector,
            },
            confirmed=True,
        )

    assert result.outcome is ToolOutcome.succeeded, result.detail
    assert fake.delete_calls() == 1
    row = await _message(message_id)
    assert row is not None and row.recalled_at is not None


# ---------------------------------------------------------------- 3. 五道闸复用


async def test_tool_reuses_the_five_gates(logged_in: TestEnv) -> None:
    """别人的消息 / 超时窗 → 工具**拒绝**且平台零调用（判据来自既有五道闸）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)

    # (a) 用户的消息：不是她的发言
    window_id = await _seed_window(logged_in)
    turn_id = new_id("turn")
    user_message = await _seed_message(
        window_id, role="user", text="我说的话", turn_id=turn_id
    )
    await _seed_delivery(turn_id=turn_id)

    # (b) 她自己的、但已经超出 2 分钟窗口
    old_window, old_message = await _seed_recallable(
        logged_in, text="很久以前说的", age_seconds=600
    )

    registry = build_default_registry()
    for wid, mid in ((window_id, user_message), (old_window, old_message)):
        async with session_scope() as session:
            result = await registry.call(
                RECALL_TOOL_ID,
                {
                    "window_id": wid,
                    "message_id": mid,
                    "session": session,
                    "connector": logged_in.app.state.onebot_connector,
                },
                confirmed=True,
            )
        assert result.outcome is ToolOutcome.failed, f"{mid} 竟然成功了"
        # **人话**：不含原因码（not_actor_message / expired 这些不该露给用户）
        assert result.detail, "拒绝时没给人话"
        assert "not_actor_message" not in result.detail
        assert "expired" not in result.detail

    assert fake.delete_calls() == 0, "被拒绝却碰了平台"


async def test_tool_without_connector_says_unsupported(logged_in: TestEnv) -> None:
    """纯 Web 会话（没有连接器）：如实说"不支持撤回"，不当成失败。"""
    window_id, message_id = await _seed_recallable(logged_in)

    async with session_scope() as session:
        registry = build_default_registry()
        result = await registry.call(
            RECALL_TOOL_ID,
            {"window_id": window_id, "message_id": message_id, "session": session},
            confirmed=True,
        )
    assert result.outcome is ToolOutcome.succeeded
    assert "不支持撤回" in result.output


# ---------------------------------------------------------------- 4. 标记解析


def test_marker_roundtrip_and_idempotence() -> None:
    """`<<撤回 rXXXX>>` 解析、剥离、幂等。"""
    text = "说错了<<撤回 r3f2a1>>，当没听见"
    cleaned, refs = parse_recall_markers(text)
    assert refs == ["r3f2a1"]
    assert "<<撤回" not in cleaned
    assert "说错了" in cleaned and "当没听见" in cleaned

    # 幂等：再剥一次不变
    again, refs_again = parse_recall_markers(cleaned)
    assert again == cleaned
    assert refs_again == []

    # 无参形式 = 撤最近一条
    _, refs_bare = parse_recall_markers("<<撤回>>好")
    assert refs_bare == [""]


def test_partial_marker_is_dropped() -> None:
    """半截标记（模型被打断）也要清掉，否则残片会落库/外发。"""
    assert "<<" not in finalize_recall_text("说错了<<撤回 r3")
    assert finalize_recall_text("正常的句子") == "正常的句子"


def test_short_ref_is_derived_from_message_id() -> None:
    """短号由 id 唯一决定（同一句话两轮里拿到同一个短号）。"""
    assert recall_ref_for("m3f2a1") == "r3f2a1"
    # 不是 m 开头：加前缀而不是猜
    assert recall_ref_for("abc123") == "rabc123"
    assert recall_ref_for("") == ""


async def test_recallable_refs_only_lists_delivered_own_messages(
    logged_in: TestEnv,
) -> None:
    """只有"她自己的 + 有回执 + 未撤回"的才带 `[recall:…]` 标记。"""
    window_id, message_id = await _seed_recallable(logged_in)
    # 一条没有回执的（发失败了）
    no_receipt = await _seed_message(
        window_id, text="这条没发出去", turn_id=new_id("turn"), sequence=2
    )
    # 一条别人的
    user_message = await _seed_message(
        window_id, role="user", text="用户说的", turn_id=new_id("turn"), sequence=3
    )

    async with session_scope() as session:
        refs = await recallable_refs(session, window_id=window_id)

    listed = {local_id for local_id, _ in refs}
    assert message_id in listed
    assert no_receipt not in listed
    assert user_message not in listed

    # 短号能反查回来
    tokens = dict((local_id, token) for local_id, token in refs)
    async with session_scope() as session:
        refs_again = await recallable_refs(session, window_id=window_id)
    assert resolve_recall_ref(tokens[message_id], refs_again) == message_id
    # 空串 = 最近一条
    assert resolve_recall_ref("", refs_again) == refs_again[-1][0]
    # 编的短号 = 对不上
    assert resolve_recall_ref("rzzzzzz", refs_again) is None


# ---------------------------------------------------------------- 5. 端到端：提议 → 确认


async def test_proposal_endpoint_lists_and_confirms(logged_in: TestEnv) -> None:
    """她提议 → 网页能列出 → 确认后只发一次 `delete_msg`。"""
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)

    # 模拟"她写了标记、代码落了提议"
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
        proposal_id = proposal.id

    listed = await logged_in.client.get(f"/v1/sessions/{window_id}/recall-proposals")
    assert listed.status_code == 200, listed.text
    items = listed.json()
    assert len(items) == 1
    assert items[0]["proposal_id"] == proposal_id
    assert items[0]["message_id"] == message_id
    assert "确认" in items[0]["note"]

    # 确认前：平台零调用
    assert fake.delete_calls() == 0

    confirmed = await logged_in.client.post(
        f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["state"] == "recalled"
    assert fake.delete_calls() == 1

    row = await _message(message_id)
    assert row is not None and row.recalled_at is not None

    # 提议已处理：列表里不再出现，重复确认 409
    after = await logged_in.client.get(f"/v1/sessions/{window_id}/recall-proposals")
    assert after.json() == []
    again = await logged_in.client.post(
        f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/confirm"
    )
    assert again.status_code == 409
    assert fake.delete_calls() == 1, "重复确认却又碰了平台"


async def test_declined_proposal_never_touches_the_platform(
    logged_in: TestEnv,
) -> None:
    """用户说"不要撤" → 提议标 declined，平台零调用。"""
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(logged_in)

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
        proposal_id = proposal.id

    declined = await logged_in.client.post(
        f"/v1/sessions/{window_id}/recall-proposals/{proposal_id}/decline"
    )
    assert declined.status_code == 204
    assert fake.delete_calls() == 0

    async with session_scope() as session:
        row = await session.get(Operation, proposal_id)
    assert row is not None and row.state == "declined"
    message_row = await _message(message_id)
    assert message_row is not None and message_row.recalled_at is None


async def test_proposal_of_another_window_is_invisible(logged_in: TestEnv) -> None:
    """跨窗口的提议不能出现在别人的列表里（窗口归属在服务端再判一次）。"""
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    window_a, message_id = await _seed_recallable(logged_in)
    window_b = await _seed_window(logged_in)

    async with session_scope() as session:
        session.add(
            Operation(
                id=new_id("recall"),
                kind=RECALL_PROPOSAL_KIND,
                actor_id=ACTOR,
                state="proposed",
                payload={"window_id": window_a, "message_id": message_id, "turn_id": "t1"},
            )
        )
        await session.commit()

    listed = await logged_in.client.get(f"/v1/sessions/{window_b}/recall-proposals")
    assert listed.status_code == 200
    assert listed.json() == [], "别的窗口的提议漏过来了"


# ---------------------------------------------------------------- 6. 审计


async def test_tool_attempt_is_audited_without_body(logged_in: TestEnv) -> None:
    """每次模型发起撤回都留审计：含 tool_id / call_id / 结果，**不含正文**。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, message_id = await _seed_recallable(
        logged_in, text="这句不该被记进审计"
    )

    registry = build_default_registry()
    # 不确认：注册表就挡住了
    blocked = await registry.call(
        RECALL_TOOL_ID, {"window_id": window_id, "message_id": message_id}
    )
    assert blocked.outcome is ToolOutcome.needs_confirmation
    assert blocked.call_id, "没有 call_id，审计对不上"

    # 确认后执行（走服务端审计）
    async with session_scope() as session:
        await registry.call(
            RECALL_TOOL_ID,
            {
                "window_id": window_id,
                "message_id": message_id,
                "session": session,
                "connector": logged_in.app.state.onebot_connector,
            },
            confirmed=True,
        )

    # 审计写在 audit 日志里（`audit_event`），不含正文
    from ling.logging_setup import clear_log_buffer, recent_logs

    clear_log_buffer()
    # 再撤一次（已撤回，会被拒）——拒绝也要留审计
    async with session_scope() as session:
        await registry.call(
            RECALL_TOOL_ID,
            {
                "window_id": window_id,
                "message_id": message_id,
                "session": session,
                "connector": logged_in.app.state.onebot_connector,
            },
            confirmed=True,
        )
    entries = [e for e in recent_logs(limit=200) if e.event_name.startswith("audit")]
    blob = json.dumps([e.message for e in entries], ensure_ascii=False)
    assert "这句不该被记进审计" not in blob, "审计里出现了消息正文"


# ---------------------------------------------------------------- 7. 真轮次：标记 → 提议


async def test_marker_in_real_turn_becomes_a_pending_proposal(
    logged_in: TestEnv,
) -> None:
    """模型在真轮次里写标记 → 落提议 + 标记被剥掉 + 提示词里有撤回选项。

    这一条是**端到端**的：从"上下文告诉她能撤哪几条"，到"她写标记"，到
    "代码落提议并把标记剥掉"。中间任何一环断了，用户在网页上就看不到确认框。
    """
    import asyncio
    import uuid

    from ling.domains.conversation.recall import recallable_refs
    from ling.domains.conversation.service import ConversationService
    from ling.kernel.turn import RECALL_PROPOSAL_KIND, TurnCoordinator
    from ling.models.router import ModelRouter
    from ling.ports.model import ModelChunk

    fake = _FakeOneBot()
    _install(logged_in, fake)
    window_id, prev_message = await _seed_recallable(
        logged_in, text="我刚才说了句蠢话"
    )

    async with session_scope() as session:
        refs = await recallable_refs(session, window_id=window_id)
    assert refs, "可撤回的消息没被识别出来"
    ref = refs[0][1]

    class _MarkerProvider(FakeProvider):
        """回一段带撤回标记的正文。"""

        def __init__(self) -> None:
            super().__init__(chunk_delay=0.0)
            self.requests: list[list[object]] = []

        async def stream(self, request):  # noqa: ANN001, ANN201
            self.requests.append(list(request.messages))
            text = f"我想收回刚才那句。<<撤回 {ref}>>"
            for index in range(0, len(text), 4):
                yield ModelChunk(delta=text[index : index + 4])
            yield ModelChunk(done=True, finish_reason="stop")

    class _Registry(ProviderRegistry):
        def __init__(self) -> None:
            super().__init__(lambda _pid: None)
            self.provider = _MarkerProvider()

        def get(self, cfg):  # noqa: ANN001, ANN201
            return self.provider

    registry = _Registry()
    coordinator = TurnCoordinator(
        router=ModelRouter(registry),
        actor_id=ACTOR,
        persona_id="persona_ling",
        cancels=logged_in.app.state.cancels,
        stream_log=logged_in.app.state.stream_log,
    )
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text="怎么了", turn_id=turn.id)
        turn_id = turn.id

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=logged_in.subject_id,
        turn_id=turn_id,
        user_text="怎么了",
        config=_config(),
        attempt=1,
    )
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            break
        events.append((item.event, item.data))

    completed = [data for name, data in events if name == "turn.completed"]
    assert completed, f"轮次没完成：{[n for n, _ in events]}"
    payload = completed[-1]

    # (a) 提议被记下来
    proposals = payload.get("recall_proposals") or []
    assert proposals, "标记没有变成待确认提议"
    assert proposals[0]["message_id"] == prev_message
    assert "确认" in proposals[0]["note"]

    # (b) 标记**从正文里剥掉**（用户不该看到说明书）
    message = payload.get("message") or {}
    assert "<<" not in str(message.get("text") or ""), message.get("text")
    assert "撤回" not in str(message.get("text") or "")
    assert "我想收回刚才那句。" in str(message.get("text") or "")

    # (c) 落库的正文也是干净的
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.window_id == window_id, Message.role == "actor"
                    )
                )
            ).scalars()
        )
    for row in rows:
        assert "<<" not in str(row.text or ""), f"标记落库了：{row.text!r}"

    # (d) 提议行在库里、状态是 proposed
    async with session_scope() as session:
        stored = list(
            (
                await session.execute(
                    select(Operation).where(Operation.kind == RECALL_PROPOSAL_KIND)
                )
            ).scalars()
        )
    assert len(stored) == 1
    assert stored[0].state == "proposed"
    assert dict(stored[0].payload or {})["message_id"] == prev_message

    # (e) 提示词里既告诉她"能撤哪几条"，也标了她自己那条的短号
    prompt = "\n".join(str(getattr(m, "content", "")) for m in registry.provider.requests[0])
    assert "<<撤回" in prompt, "提示词里没有撤回标记的用法"
    assert f"[recall:{ref}]" in prompt, "她自己的发言没有撤回短号标记"

    # (f) **平台一次都没被碰**（提议 ≠ 执行）
    assert fake.delete_calls() == 0, "提议阶段就碰了平台"
