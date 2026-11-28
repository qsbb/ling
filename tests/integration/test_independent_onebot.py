"""独立验收场景。复制到验收快照的 tests/integration 后通过 pytest 执行。

本文件只替换 OneBot 平台 HTTP 响应。窗口、身份、普通 QQ 路由、记忆 worker
和鉴权 Web API 使用仓库实现及每例自己的 PostgreSQL schema。测试模式模型
可证明原文与状态进入实际上下文，不用于评估真实模型自然程度。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling import worker
from ling.infra.db import session_scope
from ling.infra.models import ConnectorInstance, Message, PromptSnapshot
from tests.integration.conftest import TestEnv
from tests.integration.test_onebot_loop import (
    CFG,
    GROUP,
    FakeOneBot,
    _bind,
    _event_body,
    _headers,
)

pytestmark = pytest.mark.w0_web

OLD_PRIVATE = "我喜欢青桐街的白鲸旧书店，票根上写着松针藏票。"
CORRECTION = "我上次说错了。我喜欢青桐街，旧书店是哪家我记不清。"
PUBLIC_GROUP = "青桐街的公共活动叫杨梅夜市。"


async def _consent(env: TestEnv, scope: str, *, granted: bool = True) -> None:
    response = await env.client.put("/v1/consents", json={"scope": scope, "granted": granted})
    assert response.status_code == 200, response.text


async def _install(
    env: TestEnv, transport: FakeOneBot, *, config: OneBotConfig = CFG,
    connector_type: type[OneBotV11Connector] = OneBotV11Connector,
    route_instance: str | None = None,
) -> OneBotV11Connector:
    connector = connector_type(config, http=httpx.AsyncClient(
        transport=httpx.MockTransport(transport.handler),
    ))

    async def reject_swap(_values):
        raise AssertionError("本地协议替身无需改配置")

    instance = route_instance or config.instance_id
    env.app.state.connector_runtime.register(
        "onebot", reject_swap, instance_id=instance,
        instances={"onebot_connector": connector},
    )
    async with session_scope() as session:
        if await session.get(ConnectorInstance, ("onebot", instance)) is None:
            session.add(ConnectorInstance(
                scope="onebot", instance_id=instance, display_name=instance,
            ))
    return connector


async def _qq(
    env: TestEnv, event_id: int, text: str, *, group: bool = False, user_id: int | None = None,
    instance_id: str = CFG.instance_id, self_id: str = "10001",
) -> dict:
    overrides = {"message_type": "group", "group_id": int(GROUP)} if group else {}
    if user_id is not None:
        overrides["user_id"] = user_id
    body = _event_body(
        message_id=event_id, message=text, raw_message=text, self_id=int(self_id), **overrides,
    )
    path = (
        "/v1/onebot/events" if instance_id == "default"
        else f"/v1/onebot/instances/{instance_id}/events"
    )
    response = await env.client.post(path, content=body, headers=_headers(body, self_id=self_id))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] is True, result
    return result


async def _library(env: TestEnv, query: str, *, history: bool = False) -> list[dict]:
    params = {"view": "library", "q": query}
    if history:
        params["include"] = "all"
    response = await env.client.get("/v1/memory", params=params)
    assert response.status_code == 200, response.text
    return response.json()["memories"]


async def _row(text: str) -> Message:
    async with session_scope() as session:
        row = await session.scalar(select(Message).where(Message.text == text))
        assert row is not None, text
        return row


async def _completed_prompt(result: dict) -> dict:
    assert result.get("delivered") is True and result.get("turn_id"), result
    async with session_scope() as session:
        snapshot = await session.scalar(
            select(PromptSnapshot).where(PromptSnapshot.turn_id == result["turn_id"])
        )
        assert snapshot is not None and snapshot.payload and not snapshot.truncated
        return snapshot.payload


def _prompt_lines(snapshot: dict, *, lanes: set[str]) -> str:
    return "\n".join(line["content"] for line in snapshot["messages"] if line["lane"] in lanes)


async def test_quoted_pin_round_trips_through_qq_and_web(logged_in: TestEnv) -> None:
    transport = FakeOneBot()
    await _install(logged_in, transport)
    await _bind(logged_in)
    source_text = "青桐街第一趟我在白鲸旧书店拿到一张松针藏票。"
    first = await _qq(logged_in, 680001, source_text)
    await _completed_prompt(first)
    received = len(transport.sent)
    assert received >= 1, transport.sent
    assert all(row["action"] == "send_private_msg" for row in transport.sent)

    # 原话正常进入会话，worker 尚未消费。引用另一个平台消息号不能固定这条。
    assert await _library(logged_in, "松针藏票") == []
    await _qq(logged_in, 680002, "[CQ:reply,id=689999] 记住这句")
    assert await _library(logged_in, "松针藏票") == []

    result = await _qq(logged_in, 680003, "[CQ:reply,id=680001] 记住这句")
    assert result.get("delivered") is True, result
    record = await _row(source_text)
    matches = [
        memory for memory in await _library(logged_in, "松针藏票")
        if source_text in memory["text"] and memory.get("source_message_id") == record.id
    ]
    assert len(matches) == 1, matches
    memory = matches[0]
    response = await logged_in.client.get(f"/v1/memory/{memory['memory_id']}/source")
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence.get("role") == "user", evidence
    assert evidence["text"] == source_text and evidence["message_id"] == record.id
    assert evidence["window_id"] == record.window_id

    async with session_scope() as session:
        prior_replies = list(await session.scalars(
            select(Message).where(Message.turn_id == first["turn_id"], Message.role == "actor")
        ))
        assert prior_replies and prior_replies[0].text
        reply_id = prior_replies[0].id
        actual_reply = prior_replies[0].text
    assert any(
        item["role"] == "actor" and item["message_id"] == reply_id and item["text"] == actual_reply
        for item in evidence["context"]
    ), evidence

    # Web 固定同一来源应沿同一语义，重复请求不新增完全相同的固定记录。
    payload = {"window_id": record.window_id, "message_id": record.id}
    for _ in range(2):
        web = await logged_in.client.post("/v1/memory/pin", json=payload)
        assert web.status_code in {200, 201}, web.text
        assert web.json()["memory_id"] == memory["memory_id"]

    corrected = "青桐街上的店名想不起来，只记得抄了松针藏票。"
    reply = await _qq(logged_in, 680004, f"/更正 {memory['memory_id']} {corrected}")
    assert reply.get("delivered") is True, reply
    current = await _library(logged_in, "松针藏票")
    assert any(row["text"] == corrected for row in current), current
    assert not any(row["memory_id"] == memory["memory_id"] for row in current), current
    past = await _library(logged_in, "松针藏票", history=True)
    assert any(row["memory_id"] == memory["memory_id"] and row["text"] == source_text for row in past)

    new = next(row for row in current if row["text"] == corrected)
    forgotten = await _qq(logged_in, 680005, f"/忘记 {new['memory_id']}")
    assert forgotten.get("delivered") is True, forgotten
    response = await logged_in.client.get(f"/v1/memory/{new['memory_id']}/source")
    assert response.status_code == 404, response.text
    remaining = await _library(logged_in, "松针藏票")
    assert not any(row["memory_id"] in {memory["memory_id"], new["memory_id"]} for row in remaining), remaining


async def test_cross_day_recall_uses_visible_original_and_group_has_own_topic(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeOneBot()
    await _install(logged_in, transport)
    await _bind(logged_in)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)

    first = await _qq(logged_in, 681000, OLD_PRIVATE)
    await _completed_prompt(first)
    original = await _row(OLD_PRIVATE)
    second = await _qq(logged_in, 681001, CORRECTION)
    await _completed_prompt(second)
    correction = await _row(CORRECTION)
    # 周一与周二历史使用生产数据列。查询当天不把相关原话垫到最近几轮。
    moment = datetime.now(UTC)
    async with session_scope() as session:
        for message_id, days in ((original.id, 3), (correction.id, 2)):
            source = await session.get(Message, message_id)
            assert source is not None
            source.created_at = moment - timedelta(days=days)
        actor_rows = list(await session.scalars(
            select(Message).where(
                Message.role == "actor",
                Message.turn_id.in_((first["turn_id"], second["turn_id"])),
            )
        ))
        for row in actor_rows:
            row.created_at = moment - timedelta(days=2)
    assert await worker.drain_outbox(limit=8, only_topic="memory.extract") >= 2

    small_talk = await _completed_prompt(await _qq(logged_in, 681002, "嗯"))
    assert "松针藏票" not in _prompt_lines(small_talk, lanes={"memory", "evidence", "summary"})

    question = "之前青桐街那件事，我最初关于旧书店的原话是什么？"
    before = len(transport.sent)
    result = await _qq(logged_in, 681003, question)
    private = await _completed_prompt(result)
    assert private["context"]["experience_recall"] == "used_source", private["context"]
    assert private["context"]["experience_source_ids"] == [f"message:{original.id}"]
    assert private["model"] == {} and private["messages"] == []
    sent_text = str(transport.sent[before:])
    assert OLD_PRIVATE in sent_text and CORRECTION not in sent_text
    async with session_scope() as session:
        message_id = await session.scalar(select(Message.id).where(
            Message.turn_id == result["turn_id"], Message.role == "actor",
        ))
    response = await logged_in.client.get(f"/v1/messages/{message_id}/prompt")
    assert response.status_code == 200, response.text
    prompt_out = response.json()
    assert prompt_out["memory_recall"]["state"] == "used_source"
    assert prompt_out["memory_recall"]["source_message_ids"] == [original.id]
    assert prompt_out["model"] is None and prompt_out["token_total"] == 0

    before = len(transport.sent)
    current = await _completed_prompt(await _qq(logged_in, 681009, "你记得我现在喜欢的是哪条街吗？"))
    assert current["context"]["experience_recall"] == "used_source"
    assert current["context"]["experience_source_ids"] == [f"message:{correction.id}"]
    sent_text = str(transport.sent[before:])
    assert "你现在喜欢的是青桐街。" in sent_text and OLD_PRIVATE not in sent_text

    public = await _qq(logged_in, 681004, f"[CQ:at,qq=10001] {PUBLIC_GROUP}", group=True)
    await _completed_prompt(public)
    scoped = await _completed_prompt(await _qq(
        logged_in, 681005,
        "[CQ:at,qq=10001] 之前青桐街的公共活动叫什么？还记得原话吗？",
        group=True,
    ))
    assert "杨梅夜市" in str(scoped["messages"]), scoped
    assert "松针藏票" not in str(scoped["messages"]), scoped
    assert original.id not in str(scoped["context"].get("experience_source_ids")), scoped["context"]


async def test_original_lookup_distinguishes_absent_and_unindexed_history(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeOneBot()
    await _install(logged_in, transport)
    await _bind(logged_in)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)

    missing_question = "还记得我上次在月蚀胡同说过的原话吗？"
    missing = await _completed_prompt(await _qq(logged_in, 681011, missing_question))
    assert missing["context"]["experience_recall"] == "no_match"
    assert missing["context"]["experience_source_ids"] == []
    assert missing["model"] == {} and missing["messages"] == []
    assert "我没找到能核对的那句原话" in str(transport.sent)

    original = "昨天我在月蚀胡同挑了一个木鸟摆件。"
    await _completed_prompt(await _qq(logged_in, 681012, original))
    original_id = (await _row(original)).id
    async with session_scope() as session:
        prior_question = await session.scalar(select(Message).where(Message.text == missing_question))
        assert prior_question is not None
        prior_question.memory_hidden_at = datetime.now(UTC)
        source = await session.get(Message, original_id)
        assert source is not None
        source.created_at -= timedelta(days=2)
    pending_question = "我之前在月蚀胡同怎么说的？"
    pending = await _completed_prompt(await _qq(logged_in, 681013, pending_question))
    assert pending["context"]["experience_recall"] == "not_extracted"
    async with session_scope() as session:
        prior_question = await session.scalar(select(Message).where(Message.text == pending_question))
        assert prior_question is not None
        prior_question.memory_hidden_at = datetime.now(UTC)
    assert await worker.drain_outbox(limit=10, only_topic="memory.extract") >= 1

    question = "请找一下我在月蚀胡同提到那件事的原话。"
    indexed = await _completed_prompt(await _qq(logged_in, 681014, question))
    assert indexed["context"]["experience_recall"] == "injected"
    assert f"message:{original_id}" in indexed["context"]["experience_source_ids"]
    assert original in _prompt_lines(indexed, lanes={"evidence"})
    assert f"message:{(await _row(question)).id}" not in indexed["context"]["experience_source_ids"]


@pytest.mark.parametrize("changed_in_reality", [False, True], ids=["correction", "change"])
@pytest.mark.parametrize("reverse_order", [False, True], ids=["forward", "reverse"])
async def test_historical_fact_distinguishes_correction_from_real_change(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, changed_in_reality: bool, reverse_order: bool,
) -> None:
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.models import OutboxEvent

    await _install(logged_in, FakeOneBot())
    await _bind(logged_in)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    original_text = "我住在青桐街的水脉小巷。"
    revision_text = (
        "我搬到榆木路了。" if changed_in_reality else "我之前说错了。我住在榆木路。"
    )
    original_turn = await _qq(logged_in, 683001, original_text)
    await _completed_prompt(original_turn)
    changed_turn = await _qq(logged_in, 683002, revision_text)
    await _completed_prompt(changed_turn)
    now = datetime.now(UTC)
    order = [changed_turn["turn_id"], original_turn["turn_id"]] if reverse_order else [
        original_turn["turn_id"], changed_turn["turn_id"],
    ]
    async with session_scope() as session:
        for turn_id, days in ((original_turn["turn_id"], 3), (changed_turn["turn_id"], 2)):
            messages = await session.scalars(select(Message).where(Message.turn_id == turn_id))
            for message in messages:
                message.created_at = now - timedelta(days=days)
        tasks = list(await session.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
        ))
        assert len(tasks) == 2, tasks
        for task in tasks:
            task.created_at = now - timedelta(hours=2 - order.index(task.payload["turn_id"]))
    assert await worker.drain_outbox(limit=2, only_topic="memory.extract") == 2

    original = await _row(original_text)
    async with session_scope() as session:
        service = MemoryService(session)
        current = await service.recall(
            subject_id=logged_in.subject_id, query="我现在住在哪里？",
            visibility_allow={Visibility.subject_private}, fact_slot="lives_in",
            trace_window_id=original.window_id, now=now,
        )
        historical = await service.recall(
            subject_id=logged_in.subject_id, query="以前住在青桐街的水脉小巷吗？",
            visibility_allow={Visibility.subject_private}, include_history=True,
            trace_window_id=original.window_id, now=now,
        )
    assert len(current.hits) == 1 and "榆木路" in current.hits[0].text, current
    # 原话自身仍可回查。这里要求历史事实，以免把一次当时的错误说法当真。
    old_fact = any(
        hit.kind == "assertion" and "水脉小巷" in hit.text for hit in historical.hits
    )
    assert old_fact is changed_in_reality, [(hit.kind, hit.text) for hit in historical.hits]


@pytest.mark.parametrize("cross_device", [True, False], ids=["granted", "revoked"])
async def test_group_public_clue_continues_in_same_persons_private_qq(
    logged_in: TestEnv, cross_device: bool,
) -> None:
    from ling.domains.identity.service import IdentityService
    from ling.infra.models import Subject

    await _install(logged_in, FakeOneBot())
    await _bind(logged_in)
    await _consent(logged_in, "memory.cross_device")
    public = "青桐街的公开集合地点叫葵影牌楼，请记得。"
    first = await _qq(logged_in, 684001, f"[CQ:at,qq=10001] {public}", group=True)
    await _completed_prompt(first)
    # Same visible nickname, distinct proven test accounts.
    other_user = 20003
    async with session_scope() as session:
        session.add(Subject(id="subj-independent-other", display_name="小不点"))
        await session.flush()
        await IdentityService(session).bind_account(
            subject_id="subj-independent-other", connector_instance=CFG.instance_id,
            external_account_id=str(other_user), proof="proven",
        )
    other = await _completed_prompt(await _qq(
        logged_in, 684002, "接着聊群里青桐街的集合地点。", user_id=other_user,
    ))
    assert "葵影牌楼" not in str(other["messages"]), other

    if not cross_device:
        await _consent(logged_in, "memory.cross_device", granted=False)
    private = await _completed_prompt(await _qq(
        logged_in, 684003, "接着聊群里青桐街的集合地点。",
    ))
    assert ("葵影牌楼" in str(private["messages"])) is cross_device, private


@pytest.mark.parametrize("still_allowed", [True, False], ids=["allowed", "revoked"])
async def test_private_to_group_requires_a_live_explicit_grant_and_source(
    logged_in: TestEnv, still_allowed: bool,
) -> None:
    await _install(logged_in, FakeOneBot())
    await _bind(logged_in)
    await _consent(logged_in, "memory.cross_device")
    await _consent(logged_in, "public_publish")
    public = "青桐街的公开活动叫野柚巡游。"
    group = await _qq(logged_in, 685001, f"[CQ:at,qq=10001] {public}", group=True)
    await _completed_prompt(group)
    async with session_scope() as session:
        source = await session.scalar(select(Message).where(
            Message.turn_id == group["turn_id"], Message.role == "user",
        ))
        assert source is not None and "野柚巡游" in source.text, source
        window_id = source.window_id

    private = "青桐街是我一个人的秘密，松针藏票放在南窗。"
    await _completed_prompt(await _qq(logged_in, 685002, private))
    allowed = "青桐街集合时间是下午三点，带月桂纸扇。"
    result = await _qq(logged_in, 685003, f"/续接群 {window_id} {allowed}")
    assert result.get("delivered") is True, result
    if not still_allowed:
        await _consent(logged_in, "public_publish", granted=False)
    first = await _completed_prompt(await _qq(
        logged_in, 685004, "[CQ:at,qq=10001] 接着聊", group=True,
    ))
    assert "松针藏票" not in str(first["messages"]), first
    assert ("月桂纸扇" in str(first["messages"])) is still_allowed, first

    second = await _completed_prompt(await _qq(
        logged_in, 685005, "[CQ:at,qq=10001] 接着聊", group=True,
    ))
    assert "月桂纸扇" not in _prompt_lines(second, lanes={"evidence", "memory", "handoff", "experience"})


class _HistoryVerifiedConnector(OneBotV11Connector):
    async def whole_group_history(self, window_ref: str) -> frozenset[str] | None:
        if window_ref.endswith(":group:88888"):
            return frozenset({"688002"})
        return None


@pytest.mark.parametrize("audience", ["private", "group"])
async def test_original_lookup_uses_separate_private_and_verified_group_sources(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, audience: str,
) -> None:
    transport = FakeOneBot()
    connector = await _install(logged_in, transport, connector_type=_HistoryVerifiedConnector)
    assert isinstance(connector, _HistoryVerifiedConnector)
    await _bind(logged_in)
    draft = await logged_in.client.put("/v1/settings/draft", json={
        "memory": {"shared_events": "enabled"},
    })
    assert draft.status_code == 200, draft.text
    published = await logged_in.client.post("/v1/settings/publish", json={
        "revision": draft.json()["revision"],
    })
    assert published.status_code == 200, published.text
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)

    private = await _qq(logged_in, 688001, "青桐街旧书店线索是松针藏票，旧封面画着鲸鱼。")
    group = await _qq(
        logged_in, 688002, "[CQ:at,qq=10001] 青桐街旧书店线索是彩陶公告，只看展览安排。", group=True,
    )
    await _completed_prompt(private)
    await _completed_prompt(group)
    async with session_scope() as session:
        for turn_id in (private["turn_id"], group["turn_id"]):
            for row in await session.scalars(select(Message).where(Message.turn_id == turn_id)):
                row.created_at -= timedelta(days=2)
    assert await worker.drain_outbox(limit=8, only_topic="memory.extract") >= 2

    prompt = "青桐街旧书店线索的原话是什么？"
    if audience == "group":
        prompt = "[CQ:at,qq=10001] " + prompt
    result = await _completed_prompt(await _qq(logged_in, 688003, prompt, group=audience == "group"))
    evidence = _prompt_lines(result, lanes={"evidence"})
    assert result["context"].get("experience_recall") == "injected", result["context"]
    assert "用户原话" in evidence
    own, foreign = ("彩陶公告", "松针藏票") if audience == "group" else ("松针藏票", "彩陶公告")
    assert own in evidence and foreign not in evidence, evidence


@pytest.mark.parametrize("mode", ["default_with_namespace", "different_bot", "expired"])
async def test_group_handoff_checks_bot_and_expiration_but_accepts_namespace(
    logged_in: TestEnv, mode: str,
) -> None:
    from ling.domains.identity.service import IdentityService
    from ling.infra.models import HandoffCapsuleRow
    from tests.integration.test_onebot_loop import QQ_USER

    transport = FakeOneBot()
    route = "default"
    await _install(logged_in, transport, route_instance=route)
    await _bind(logged_in)
    await _consent(logged_in, "memory.cross_device")
    group = await _qq(
        logged_in, 691001, "[CQ:at,qq=10001] 青桐街的露天放映在梧桐剧院。",
        group=True, instance_id=route,
    )
    await _completed_prompt(group)
    target_self = "10001"
    if mode == "expired":
        async with session_scope() as session:
            for capsule in await session.scalars(select(HandoffCapsuleRow)):
                capsule.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    elif mode == "different_bot":
        route = "other-bot"
        target_self = "10002"
        await _install(logged_in, transport, config=replace(CFG, instance_id=route, self_id=target_self))
        async with session_scope() as session:
            await IdentityService(session).bind_account(
                subject_id=logged_in.subject_id, connector_instance=route,
                external_account_id=QQ_USER, proof="proven",
            )
    direct = await _completed_prompt(await _qq(
        logged_in, 691002, "接着聊群里青桐街的露天放映。",
        instance_id=route, self_id=target_self,
    ))
    short_memory = _prompt_lines(direct, lanes={"short_memory"})
    assert ("梧桐剧院" in short_memory) is (mode == "default_with_namespace"), short_memory
