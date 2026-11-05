"""W0 回归：停止与重试的竞态（子智能体实测发现的真实缺陷）。

背景：stop 路由无条件把轮次写成 ``stopping``，会覆盖协调器已经提交的
``stopped``，导致该轮永久不可重试（``/retry`` 永远 409）。
修复方式：stop 用条件更新（只从 pending/streaming 迁到 stopping），
retry 允许把悬空的 stopping 修正为 stopped 后继续。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import Turn
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


async def _new_session(client) -> str:
    response = await client.post("/v1/sessions", json={"title": "竞态"})
    assert response.status_code == 201
    return response.json()["session_id"]


async def _use_slow_provider(client, *, chunk_delay: float = 0.25) -> None:
    """把 dialogue.fast 切到慢速 fake provider。

    ASGI 内存传输下默认 fake provider 会在 stop 生效前就跑完，
    必须放慢才能测到"中途停止"。
    """
    draft = await client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True,
                 "extra": {"chunk_delay": chunk_delay}}
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "fake", "model_id": "deterministic"}
            ],
            "test_mode": True,
        },
    )
    assert draft.status_code == 200, draft.text
    published = await client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text


async def _start_turn_in_background(client, session_id: str, text: str):
    """后台消费 SSE，返回 (turn_id, task)。

    必须持续读到结束：提前关闭流会让 StreamingResponse 的生成器被取消，
    轮次会被当作中断处理，测不到"中途停止"。
    """
    import json

    body = {"client_turn_id": str(uuid.uuid4()), "text": text}
    started = asyncio.Event()
    state: dict[str, str] = {}

    async def consume() -> None:
        try:
            async with client.stream(
                "POST", f"/v1/sessions/{session_id}/turns", json=body
            ) as response:
                event = ""
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event == "turn.started":
                        state["turn_id"] = json.loads(line[6:])["turn_id"]
                        started.set()
        except Exception as exc:  # noqa: BLE001 - 测试里只记录
            state.setdefault("error", str(exc))
        finally:
            started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=10)
    return state.get("turn_id"), task


async def _turn_state(turn_id: str) -> str:
    async with session_scope() as session:
        row = (
            await session.execute(select(Turn).where(Turn.id == turn_id))
        ).scalar_one()
        return row.state


async def test_stop_signal_is_conditional_and_preserves_terminal_state(
    logged_in: TestEnv,
) -> None:
    """停止信号必须是条件更新：不能把已终结的轮次改回 stopping。

    注意：ASGITransport 会缓冲整个 SSE 响应，无法在 HTTP 层观察"流到一半"，
    所以这里直接测领域与路由的状态迁移语义（这才是缺陷所在）。
    """
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        turn_id = turn.id
        await conv.add_message(session_id, role="user", text="原始输入", turn_id=turn_id)
        # 协调器已提交 stopped
        await conv.set_turn_state(turn_id, TurnState.stopped)

    # 迟到的停止请求不得覆盖终态
    stop = await logged_in.client.post(f"/v1/turns/{turn_id}/stop")
    assert stop.status_code == 200
    assert stop.json()["state"] == "stopped", stop.text
    assert await _turn_state(turn_id) == "stopped"

    # 该轮次可重试
    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200, retry.text[:200]


async def test_stop_transitions_streaming_to_stopping(logged_in: TestEnv) -> None:
    """未终结的轮次收到停止信号时迁到 stopping。"""
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.set_turn_state(turn.id, TurnState.streaming)
        turn_id = turn.id

    stop = await logged_in.client.post(f"/v1/turns/{turn_id}/stop")
    assert stop.status_code == 200
    assert stop.json()["state"] == "stopping"


async def test_dangling_stopping_can_be_retried(logged_in: TestEnv) -> None:
    """模拟进程重启留下的悬空 stopping：retry 应先修正再执行。"""
    session_id = await _new_session(logged_in.client)
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.add_message(session_id, role="user", text="悬空", turn_id=turn.id)
        await conv.set_turn_state(turn.id, TurnState.stopping)
        turn_id = turn.id

    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200
    events = await read_sse(retry)
    assert events[-1][0] in {"turn.completed", "turn.failed"}


async def test_partial_text_is_exposed_in_active_turn(logged_in: TestEnv) -> None:
    """流式中的半成品文本通过 active_turn.partial_text 暴露给前端。

    直接写入 CancelRegistry 的缓冲，避免依赖 HTTP 层能否"流到一半"。
    """
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.set_turn_state(turn.id, TurnState.streaming)
        turn_id = turn.id

    # 模拟协调器已产出部分文本（用测试应用自己的 CancelRegistry）
    logged_in.app.state.cancels.set_partial(turn_id, "已经说了半句话")

    listing = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    active = listing.json()["active_turn"]
    assert active is not None
    assert active["partial_text"] == "已经说了半句话"
    assert active["state"] == "streaming"


async def test_persona_update_emits_timeline_event(logged_in: TestEnv) -> None:
    response = await logged_in.client.put("/v1/persona", json={"display_name": "时间线角色"})
    assert response.status_code == 200
    timeline = await logged_in.client.get("/v1/actor/timeline?limit=10")
    types = [e["type"] for e in timeline.json()["events"]]
    assert "persona.updated" in types


async def test_logout_clears_configured_cookie(logged_in: TestEnv) -> None:
    settings = logged_in.settings
    response = await logged_in.client.post("/v1/auth/logout")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert settings.cookie_name in set_cookie, "登出必须清配置里的 cookie 名"
    after = await logged_in.client.get("/v1/sessions")
    assert after.status_code == 401


async def test_retry_preserves_delivered_text(logged_in: TestEnv) -> None:
    """重试不得删除上一轮已交付的文本（设计文档 §4.3）。

    "已经送达的句子不能假装撤回"：用户可能已经看到半句话，
    重试应当生成一条新的、带 attempt+1 的消息，而不是让旧文本凭空消失。
    """
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.add_message(session_id, role="user", text="讲个故事", turn_id=turn.id)
        # 模拟"流到一半失败"：已交付部分文本，状态 failed
        await conv.add_message(
            session_id,
            role="actor",
            text="从前有一只小水母，它",
            turn_id=turn.id,
            state=TurnState.failed,
            attempt=1,
        )
        await conv.set_turn_state(turn.id, TurnState.failed)
        turn_id = turn.id

    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200, retry.text
    await read_sse(retry)

    messages = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    actors = [m for m in messages.json()["messages"] if m["role"] == "actor"]
    texts = [m["text"] for m in actors]
    assert any("从前有一只小水母" in t for t in texts), "已交付文本被删除了"
    assert any(m["attempt"] == 2 for m in actors), "重试应产生 attempt=2 的新消息"
    # 旧的失败段仍标记为 failed，不会被伪装成完整回复
    assert any(m["state"] == "failed" for m in actors)


async def test_resume_endpoint_replays_and_reports_cursor(logged_in: TestEnv) -> None:
    """断线续传端点：能回放片段；游标无效时明确给快照而不是静默丢字。"""
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.add_message(session_id, role="user", text="续传", turn_id=turn.id)
        await conv.add_message(
            session_id,
            role="actor",
            text="已落库的部分",
            turn_id=turn.id,
            state=TurnState.stopped,
            attempt=1,
        )
        await conv.set_turn_state(turn.id, TurnState.stopped)
        turn_id = turn.id

    # 写入两条流式片段
    log = logged_in.app.state.stream_log
    log.append(turn_id=turn_id, attempt=1, text="第一段")
    log.append(turn_id=turn_id, attempt=1, text="第二段")

    # 从 0 续传：拿到两段 + 终态
    response = await logged_in.client.get(f"/v1/turns/{turn_id}/resume?since=0")
    assert response.status_code == 200
    events = await read_sse(response)
    names = [n for n, _ in events]
    assert names[0] == "turn.resumed"
    assert events[0][1]["complete"] is True
    assert [d["text"] for n, d in events if n == "delta"] == ["第一段", "第二段"]
    assert "turn.finished" in names

    # 从游标 1 续传：只拿到第二段
    r2 = await logged_in.client.get(f"/v1/turns/{turn_id}/resume?since=1")
    e2 = await read_sse(r2)
    assert [d["text"] for n, d in e2 if n == "delta"] == ["第二段"]


async def test_resume_unknown_turn_gives_snapshot_not_silence(logged_in: TestEnv) -> None:
    """片段已清理时必须返回权威快照，而不是让前端以为没内容。"""
    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    session_id = await _new_session(logged_in.client)
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            session_id, client_turn_id=str(uuid.uuid4()), config_revision=0
        )
        await conv.add_message(session_id, role="user", text="快照", turn_id=turn.id)
        await conv.add_message(
            session_id,
            role="actor",
            text="这是落库的完整内容",
            turn_id=turn.id,
            state=TurnState.stopped,
            attempt=1,
        )
        await conv.set_turn_state(turn.id, TurnState.stopped)
        turn_id = turn.id

    response = await logged_in.client.get(f"/v1/turns/{turn_id}/resume?since=0")
    events = await read_sse(response)
    resumed = events[0][1]
    assert resumed["complete"] is False, "无片段时应标明接不上"
    snapshots = [d["text"] for n, d in events if n == "turn.snapshot"]
    assert snapshots and "落库的完整内容" in snapshots[0]
