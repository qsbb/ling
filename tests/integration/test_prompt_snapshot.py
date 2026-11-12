"""「查看本次提示词」：快照必须等于**真正发出去的东西**，且三态可解释。

## 这个文件钉的是什么

1. **同源**：快照里的 messages 与 provider 收到的逐条一致（顺序、角色、正文）。
   快照若由另一套拼装生成，"她看到的"和"我们记的"迟早会分叉，而这份快照
   存在的唯一理由就是可信。
2. **不串轮**：连续两轮，各自的快照只含自己的输入（第二轮的历史里当然有
   第一轮，那是上下文；但第一轮不可能有第二轮的输入）。
3. **捕获失败不阻塞出话**：注入异常 → 回复照常产生，接口如实报
   ``unavailable``（不是假装成功，也不是让整轮失败）。
4. **超 256KB 截断**：`truncated=true`、跳号的 index、仍可读。
5. **过期与清理**：`expired`（曾经有过）与 `unavailable`（从没存过）必须分开；
   清理任务跑过之后行为一致。
6. **越权一律 404**：非 owner 与不存在返回**同一个** 404。
7. **块标签来自编译器**：历史块的条数必须等于编译时喂进去的历史条数——
   这是"标签不是靠猜"的可验证形式。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import PromptSnapshot
from ling.kernel.turn import (
    PROMPT_SNAPSHOT_MAX_BYTES,
    TurnCoordinator,
    purge_expired_prompt_snapshots,
)
from ling.models.fake import FakeProvider
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

# fake provider 每段 4 个字符，0 延迟足够——这里测的是快照，不是流式时序。
FAST_CHUNK_DELAY = 0.0


def _config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(
                provider_id="fake",
                kind="fake",
                enabled=True,
                extra={"chunk_delay": FAST_CHUNK_DELAY},
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast",
                provider_id="fake",
                model_id="deterministic",
            )
        },
        test_mode=True,
    )


class _CapturingProvider(FakeProvider):
    """fake provider 的抓包版：记下它**真正收到**的 messages。

    必须抓 provider 这一层（而不是 router 的入参）：两者之间还有回退、
    额度、熔断，任何一层改了内容，"快照 == 真正发出去的"就不成立了。
    """

    def __init__(self) -> None:
        super().__init__(chunk_delay=FAST_CHUNK_DELAY)
        self.requests: list[list[object]] = []

    async def stream(self, request):  # noqa: ANN001, ANN201
        self.requests.append(list(request.messages))
        async for chunk in super().stream(request):
            yield chunk


class _CapturingRegistry(ProviderRegistry):
    """始终返回同一个抓包 provider（快照测试不需要真的按配置建实例）。"""

    def __init__(self) -> None:
        super().__init__(lambda _pid: None)
        self.provider = _CapturingProvider()

    def get(self, cfg):  # noqa: ANN001, ANN201
        return self.provider


async def _new_window(client, title: str = "提示词快照") -> str:
    response = await client.post("/v1/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _make_turn(window_id: str, text: str) -> str:
    """在库里建一条真实轮次（`_finalize` 需要真实行，记忆提炼也挂在它上面）。"""
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=text, turn_id=turn.id)
        return turn.id


async def _drain(queue) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            return events
        events.append((item.event, item.data))


async def _run_turn(
    env: TestEnv,
    *,
    window_id: str,
    text: str,
    provider: _CapturingProvider | None = None,
) -> tuple[str, list[tuple[str, dict]]]:
    registry = _CapturingRegistry()
    if provider is not None:
        registry.provider = provider
    coordinator = TurnCoordinator(
        router=ModelRouter(registry),
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


def _reply(events: list[tuple[str, dict]]) -> tuple[str, str]:
    """(message_id, 正文)。没完成就断言失败——先把因摆出来再谈别的。"""
    completed = [data for name, data in events if name == "turn.completed"]
    assert completed, f"轮次没有完成，实际事件: {[n for n, _ in events]}"
    message = completed[-1].get("message") or {}
    return str(message.get("message_id") or ""), str(message.get("text") or "")


async def _snapshot_row(*, turn_id: str) -> PromptSnapshot | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


async def _get_prompt(client, message_id: str) -> dict:
    response = await client.get(f"/v1/messages/{message_id}/prompt")
    assert response.status_code == 200, response.text
    return response.json()


def _sections_by_key(body: dict) -> dict[str, dict]:
    return {section["key"]: section for section in body["sections"]}


# ---------------------------------------------------------------- 1. 同源


async def test_snapshot_equals_what_provider_received(logged_in: TestEnv) -> None:
    """快照里的 messages 必须与 provider 收到的**逐条一致**。"""
    window_id = await _new_window(logged_in.client)
    provider = _CapturingProvider()
    turn_id, events = await _run_turn(
        logged_in, window_id=window_id, text="快照要对得上", provider=provider
    )
    message_id, reply = _reply(events)
    assert reply, "这一轮没有产出正文"
    assert message_id

    row = await _snapshot_row(turn_id=turn_id)
    assert row is not None, "捕获点没有写下快照"
    payload = row.payload or {}
    sent = list(payload.get("messages") or [])
    assert sent, "快照里的 messages 是空的"

    # provider 真的收到过请求（不是从缓存/桩里看的）
    assert provider.requests, "provider 没有收到任何请求"
    sent_to_model = provider.requests[-1]
    assert [(item["role"], item["content"]) for item in sent] == [
        (m.role, m.content) for m in sent_to_model
    ], "快照与 provider 收到的内容不一致（顺序/角色/正文）"

    # 落库的回填也成立：快照指到了这条回复
    assert row.message_id == message_id
    assert row.size_bytes > 0 and row.truncated is False

    # 只读接口能读到同一份内容
    body = await _get_prompt(logged_in.client, message_id)
    assert body["state"] == "available"
    assert body["message_id"] == message_id
    assert body["turn_id"] == turn_id
    sections = _sections_by_key(body)
    assert "input" in sections
    assert sections["input"]["items"][0]["content"] == "快照要对得上"
    assert body["truncated"] is False


# ---------------------------------------------------------------- 2. 不串轮


async def test_two_turns_do_not_leak_into_each_other(logged_in: TestEnv) -> None:
    window_id = await _new_window(logged_in.client, "两轮不串")
    first_turn, first_events = await _run_turn(
        logged_in, window_id=window_id, text="第一轮的问题"
    )
    first_message, _ = _reply(first_events)
    second_turn, second_events = await _run_turn(
        logged_in, window_id=window_id, text="第二轮的问题"
    )
    second_message, _ = _reply(second_events)

    first = await _snapshot_row(turn_id=first_turn)
    second = await _snapshot_row(turn_id=second_turn)
    assert first is not None and second is not None

    first_text = "\n".join(str(m["content"]) for m in (first.payload or {})["messages"])
    second_text = "\n".join(str(m["content"]) for m in (second.payload or {})["messages"])

    # 第二轮能看到第一轮（那是历史）；第一轮**不可能**看到第二轮。
    assert "第二轮的问题" not in first_text, "第一轮的快照里出现了第二轮的输入（串轮）"
    assert "第一轮的问题" in second_text, "第二轮的历史里丢了第一轮（上下文没接上）"

    first_body = await _get_prompt(logged_in.client, first_message)
    second_body = await _get_prompt(logged_in.client, second_message)
    assert _sections_by_key(first_body)["input"]["items"][0]["content"] == "第一轮的问题"
    assert _sections_by_key(second_body)["input"]["items"][0]["content"] == "第二轮的问题"
    # 第二轮的 input 段只有一条：这一轮她只收到这一句
    assert len(_sections_by_key(second_body)["input"]["items"]) == 1


# ---------------------------------------------------------------- 3. 捕获失败


async def test_capture_failure_does_not_block_reply(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入异常 → 回复照常产生，接口报 `unavailable`。"""
    import ling.kernel.turn as turn_module

    async def _boom(**_kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("注入的快照写入故障")

    monkeypatch.setattr(turn_module, "save_prompt_snapshot", _boom)

    window_id = await _new_window(logged_in.client, "捕获失败")
    turn_id, events = await _run_turn(
        logged_in, window_id=window_id, text="快照挂了也要回话"
    )
    message_id, reply = _reply(events)
    assert reply, "快照失败把回复也带走了（违反「绝不阻塞出话」）"
    assert await _snapshot_row(turn_id=turn_id) is None

    body = await _get_prompt(logged_in.client, message_id)
    assert body["state"] == "unavailable"
    assert body["sections"] == []
    assert body["note"], "unavailable 必须说明「没存下来」，不能只是空白"


# ---------------------------------------------------------------- 4. 超 256KB


async def test_oversized_snapshot_is_truncated_but_readable(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 256KB → `truncated=true`、中间被丢、仍可读。"""
    import ling.kernel.context as context_module

    # 让历史车道装得下足够多的长消息（默认 20k token 根本到不了 256KB，
    # 而这条用例要验的正是"到了上限会怎样"）。
    monkeypatch.setattr(context_module, "HISTORY_TOKEN_BUDGET", 500_000)

    window_id = await _new_window(logged_in.client, "超限截断")
    async with session_scope() as session:
        conv = ConversationService(session)
        # 6 × 60KB ≈ 360KB > 256KB。用 ASCII 让 token 估算稳定（4 字符/token）。
        for index in range(6):
            await conv.add_message(
                window_id, role="user", text=f"长文{index}：" + "a" * 60_000
            )
            await conv.add_message(window_id, role="actor", text=f"收到第{index}条")

    turn_id, events = await _run_turn(
        logged_in, window_id=window_id, text="上面那些长文你看了吗"
    )
    message_id, _ = _reply(events)

    row = await _snapshot_row(turn_id=turn_id)
    assert row is not None
    assert row.truncated is True, "超过 256KB 却没有标 truncated"
    assert row.size_bytes <= PROMPT_SNAPSHOT_MAX_BYTES, (
        f"快照落库 {row.size_bytes} 字节，超过上限 {PROMPT_SNAPSHOT_MAX_BYTES}"
    )
    payload = row.payload or {}
    kept = list(payload.get("messages") or [])
    assert payload.get("dropped_items", 0) > 0, "truncated=true 却一条都没丢"

    body = await _get_prompt(logged_in.client, message_id)
    assert body["state"] == "available"
    assert body["truncated"] is True
    assert body["dropped_items"] > 0
    sections = _sections_by_key(body)
    # system 段必须被保留：那是"她是谁、有哪些规则"
    assert "system" in sections and sections["system"]["items"], "system 段被截没了"
    # 这一轮的输入必须保留
    assert sections["input"]["items"][0]["content"] == "上面那些长文你看了吗"
    # 丢的必须是**中间较早的历史**：index 保留原值，所以会出现跳号；
    # 而且最后一条（这一轮的输入）一定还在。
    all_indexes = [int(item["index"]) for item in kept]
    assert all_indexes == sorted(all_indexes)
    # 原始条数 = 留下的 + 丢掉的（最大 index + 1 就是原始条数）
    assert max(all_indexes) + 1 == len(all_indexes) + payload["dropped_items"]
    assert any(
        later - earlier > 1
        for earlier, later in zip(all_indexes, all_indexes[1:], strict=False)
    ), "index 一个都不跳 = 根本没丢条目（truncated 却什么都没丢）"
    # 跳号只出现在历史段：system 段与最后这一句输入必须原样保留
    system_indexes = [
        int(item["index"])
        for item in kept
        if item["key"] not in {"history", "input", "summary"}
    ]
    assert system_indexes == list(range(len(system_indexes))), "system 段被挖洞了"
    assert all_indexes[-1] == max(all_indexes), "最后一条（本次输入）被丢掉了"

    # 留下来的正文没有被清空（截断保留的是整条，不是空壳）
    history_items = sections["history"]["items"]
    assert history_items and all(item["content"] for item in history_items)


# ---------------------------------------------------------------- 5. 过期与清理


async def test_expired_state_and_purge_keep_semantics(logged_in: TestEnv) -> None:
    window_id = await _new_window(logged_in.client, "过期")
    turn_id, events = await _run_turn(logged_in, window_id=window_id, text="会过期的话")
    message_id, _ = _reply(events)

    # 把过期时间推到过去：等价于"保留期到了、清理任务还没跑"
    async with session_scope() as session:
        row = (
            await session.execute(
                select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
            )
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()

    expired_body = await _get_prompt(logged_in.client, message_id)
    assert expired_body["state"] == "expired"
    assert expired_body["sections"] == [], "过期后不得再返回正文"
    assert expired_body["created_at"] and expired_body["expires_at"]
    assert expired_body["model"]["profile"] == "dialogue.fast"
    assert expired_body["note"]

    # 清理任务真的清掉正文（不是删行）：之后仍是 expired，不是 unavailable
    async with session_scope() as session:
        removed = await purge_expired_prompt_snapshots(session)
    assert removed["scrubbed"] == 1

    after_purge = await _get_prompt(logged_in.client, message_id)
    assert after_purge["state"] == "expired", "清理把「过期」变成了「没存过」"
    async with session_scope() as session:
        row = (
            await session.execute(
                select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
            )
        ).scalar_one()
        assert row.size_bytes == 0, "清理之后正文必须真的没了"
        assert row.payload and row.payload.get("scrubbed") is True

    # 再跑一次：幂等，0 行
    async with session_scope() as session:
        again = await purge_expired_prompt_snapshots(session)
    assert again == {"scrubbed": 0, "deleted": 0}

    # 墓碑再放一段时间后才真删（那时才会退化成 unavailable）
    async with session_scope() as session:
        row = (
            await session.execute(
                select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
            )
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(days=30)
        await session.flush()
    async with session_scope() as session:
        gone = await purge_expired_prompt_snapshots(session)
    assert gone["deleted"] == 1
    assert (await _get_prompt(logged_in.client, message_id))["state"] == "unavailable"


# ---------------------------------------------------------------- 6. 越权


async def _second_user_client(env: TestEnv):
    from ling.domains.identity.service import IdentityService

    username = f"other_{uuid.uuid4().hex[:8]}"
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username=username, password="other-password", role="member"
        )
    await env.client.post("/v1/auth/logout")
    login = await env.client.post(
        "/v1/auth/login", json={"username": username, "password": "other-password"}
    )
    assert login.status_code == 200, login.text
    return env.client


async def test_non_owner_gets_404_same_as_missing(logged_in: TestEnv) -> None:
    window_id = await _new_window(logged_in.client, "越权")
    _, events = await _run_turn(logged_in, window_id=window_id, text="只有主人能看")
    message_id, _ = _reply(events)
    owner_body = await _get_prompt(logged_in.client, message_id)
    assert owner_body["state"] == "available"

    client = await _second_user_client(logged_in)
    stolen = await client.get(f"/v1/messages/{message_id}/prompt")
    missing = await client.get("/v1/messages/msg_does_not_exist/prompt")
    assert stolen.status_code == 404, stolen.text
    assert missing.status_code == 404, missing.text
    # **响应体也要一样**：不一样就等于泄露"这条消息存在"。
    # 只有 trace_id 例外——它是每次请求各自的追踪号，与"是否存在"无关。
    stolen_error = dict(stolen.json()["error"])
    missing_error = dict(missing.json()["error"])
    stolen_error.pop("trace_id", None)
    missing_error.pop("trace_id", None)
    assert stolen_error == missing_error


# ---------------------------------------------------------------- 7. 块标签


async def test_block_labels_come_from_compiler(logged_in: TestEnv) -> None:
    """历史块的条数 == 编译时给的历史条数；块顺序与标签正确。"""
    window_id = await _new_window(logged_in.client, "块标签")
    async with session_scope() as session:
        conv = ConversationService(session)
        # 3 条已完成历史（2 句用户 + 1 句她），没有摘要 → history 段应恰好 3 条
        await conv.add_message(window_id, role="user", text="历史一")
        await conv.add_message(window_id, role="actor", text="历史二（她说的）")
        await conv.add_message(window_id, role="user", text="历史三")
        # 再给一条摘要，验证 summary 段由编译器单独标注（不是靠正文猜）
        await conv.save_summary(
            window_id, summary="更早的对话摘要内容", through_sequence=0, message_count=2
        )

    _, events = await _run_turn(logged_in, window_id=window_id, text="这轮的新输入")
    message_id, _ = _reply(events)
    body = await _get_prompt(logged_in.client, message_id)
    sections = {section["key"]: section for section in body["sections"]}

    assert "system" in sections, "人设与规则段不见了"
    assert sections["system"]["label"] == "人设与规则"
    assert sections["system"]["role"] == "system"

    assert "history" in sections
    assert len(sections["history"]["items"]) == 3, (
        "历史块条数与编译时喂进去的历史条数不一致（标签/分组不对）"
    )
    assert [item["content"].split("\n")[-1] for item in sections["history"]["items"]] == [
        "历史一",
        "历史二（她说的）",
        "历史三",
    ]
    assert sections["history"]["collapsed"] is True
    assert sections["history"]["role"] == "mixed"

    assert "summary" in sections
    assert "更早的对话摘要内容" in sections["summary"]["items"][0]["content"]

    assert sections["input"]["items"][0]["content"] == "这轮的新输入"
    assert sections["input"]["role"] == "user"

    # 顺序固定：system 在最前，input 在 history 之后
    keys = [section["key"] for section in body["sections"]]
    assert keys.index("system") < keys.index("history") < keys.index("input")
    # token 计数存在且非零（前端用它显示"这段多长"）
    assert sections["history"]["tokens"] > 0
    assert body["token_total"] >= sections["history"]["tokens"]


# ---------------------------------------------------------------- 8. 重试覆盖


async def test_retry_overwrites_instead_of_appending(logged_in: TestEnv) -> None:
    """同一轮重试**覆盖**同一行，不按 attempt 追加。

    按 attempt 存会让表随重试次数膨胀，而且同一轮的旧提示词既没人看、
    又多一份泄漏面。这里用 `save_prompt_snapshot` 直接模拟两次写入
    （真实的 retry 路径只是把 attempt 加一，turn_id 不变）。
    """
    from ling.kernel.turn import save_prompt_snapshot

    window_id = await _new_window(logged_in.client, "重试覆盖")
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        turn_id = turn.id

    first = {"attempt": 1, "messages": [{"index": 0, "role": "user", "key": "input",
                           "content": "第一次尝试", "tokens": 4}]}
    second = {"attempt": 2, "messages": [{"index": 0, "role": "user", "key": "input",
                            "content": "第二次尝试", "tokens": 4}]}
    await save_prompt_snapshot(
        actor_id="actor_ling", turn_id=turn_id, window_id=window_id,
        payload=first, truncated=False,
    )
    await save_prompt_snapshot(
        actor_id="actor_ling", turn_id=turn_id, window_id=window_id,
        payload=second, truncated=False,
    )
    assert not await save_prompt_snapshot(
        actor_id="actor_ling", turn_id=turn_id, window_id=window_id,
        payload=first, truncated=False,
    )

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
                )
            ).scalars()
        )
    assert len(rows) == 1, f"同一轮存了 {len(rows)} 条快照（应当覆盖成一条）"
    assert rows[0].payload["messages"][0]["content"] == "第二次尝试"
