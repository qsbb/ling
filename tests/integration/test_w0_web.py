"""W0 Web 回归：发送、流式、停止、失败重试、历史、隔离、重启恢复、设置生效。

这些是**允许在任何阶段运行**的定向 Web 回归（``-m w0_web``）。
它们证明的是 Web 聊天路径，不代表 QQ/微信/具身等模块可用。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import TurnState
from ling.infra.db import session_scope
from ling.infra.models import Turn
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


async def _new_session(client, title: str | None = None) -> str:
    response = await client.post("/v1/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _send(client, session_id: str, text: str, *, client_turn_id: str | None = None):
    body = {"client_turn_id": client_turn_id or str(uuid.uuid4()), "text": text}
    response = await client.post(f"/v1/sessions/{session_id}/turns", json=body)
    assert response.status_code == 200, response.text
    return await read_sse(response)


# ---------------------------------------------------------------- 认证与隔离


async def test_login_required_and_isolated(web_env: TestEnv) -> None:
    anonymous = await web_env.client.get("/v1/sessions")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"

    login = await web_env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert login.status_code == 200
    assert login.json()["test_mode"] is True

    bad = await web_env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "wrong"}
    )
    assert bad.status_code == 401

    session_id = await _new_session(web_env.client, "owner 的会话")
    await _send(web_env.client, session_id, "只有我能看到")

    # 第二个用户：看不到别人的窗口，也读不到别人的消息
    other = await web_env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert other.status_code == 200
    from ling.domains.identity.service import IdentityService

    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="second", password="second-password", role="member"
        )
    await web_env.client.post("/v1/auth/logout")
    second_login = await web_env.client.post(
        "/v1/auth/login", json={"username": "second", "password": "second-password"}
    )
    assert second_login.status_code == 200

    listing = await web_env.client.get("/v1/sessions")
    assert listing.status_code == 200
    ids = [s["session_id"] for s in listing.json()["sessions"]]
    assert session_id not in ids

    stolen = await web_env.client.get(f"/v1/sessions/{session_id}/messages")
    assert stolen.status_code == 404


# ---------------------------------------------------------------- 发送与流式


async def test_send_streams_incrementally(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    events = await _send(logged_in.client, session_id, "你好")

    names = [name for name, _ in events]
    assert names[0] == "turn.started"
    assert names[-1] == "turn.completed"
    deltas = [data["text"] for name, data in events if name == "delta"]
    assert len(deltas) > 3, "必须逐段流式，而不是一次性返回"
    assert "".join(deltas).strip() != ""
    # 测试模式必须有可识别标记，不能冒充真实模型
    assert "测试模式" in "".join(deltas)

    completed = events[-1][1]
    assert completed["state"] == "completed"
    assert completed["model_label"].startswith("fake/")
    assert completed["message"]["role"] == "actor"


async def test_history_persists_and_refresh_reads_authoritative_state(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    await _send(logged_in.client, session_id, "第一条")
    await _send(logged_in.client, session_id, "第二条")

    listing = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    assert listing.status_code == 200
    payload = listing.json()
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "actor", "user", "actor"]
    assert payload["active_turn"] is None
    sequences = [m["sequence"] for m in payload["messages"]]
    assert sequences == sorted(sequences)


async def test_idempotent_client_turn_id(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    turn_id = str(uuid.uuid4())
    first = await _send(logged_in.client, session_id, "只应发送一次", client_turn_id=turn_id)
    assert first[-1][0] == "turn.completed"
    first_turn = first[0][1]["turn_id"]

    replay = await _send(logged_in.client, session_id, "只应发送一次", client_turn_id=turn_id)
    assert replay[0][1]["replayed"] is True
    assert replay[0][1]["turn_id"] == first_turn
    assert replay[-1][0] == "turn.completed"

    listing = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    user_messages = [m for m in listing.json()["messages"] if m["role"] == "user"]
    assert len(user_messages) == 1, "重复提交不得产生第二条用户消息"


async def test_multiple_sessions_are_switchable(logged_in: TestEnv) -> None:
    first = await _new_session(logged_in.client, "会话 A")
    second = await _new_session(logged_in.client, "会话 B")
    await _send(logged_in.client, first, "A 的内容")
    await _send(logged_in.client, second, "B 的内容")

    a = await logged_in.client.get(f"/v1/sessions/{first}/messages")
    b = await logged_in.client.get(f"/v1/sessions/{second}/messages")
    assert all(m["text"] != "B 的内容" for m in a.json()["messages"])
    assert any(m["text"] == "B 的内容" for m in b.json()["messages"])

    listing = await logged_in.client.get("/v1/sessions")
    ids = {s["session_id"] for s in listing.json()["sessions"]}
    assert {first, second} <= ids


# ---------------------------------------------------------------- 停止


async def test_stop_keeps_delivered_text(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    body = {"client_turn_id": str(uuid.uuid4()), "text": "讲一个很长的故事"}
    # 流式读取时中途停止
    turn_id: str | None = None
    seen_deltas: list[str] = []
    async with logged_in.client.stream(
        "POST", f"/v1/sessions/{session_id}/turns", json=body
    ) as response:
        assert response.status_code == 200
        buffer = ""
        stopped = False
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                event_name = ""
                data: dict = {}
                for line in raw.splitlines():
                    if line.startswith("event: "):
                        event_name = line[7:].strip()
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                if event_name == "turn.started":
                    turn_id = data["turn_id"]
                elif event_name == "delta":
                    seen_deltas.append(data["text"])
                    if len(seen_deltas) >= 2 and turn_id and not stopped:
                        stop = await logged_in.client.post(f"/v1/turns/{turn_id}/stop")
                        assert stop.status_code == 200
                        stopped = True
                elif event_name in {"turn.stopped", "turn.completed"}:
                    break
    assert turn_id is not None
    assert stopped, "必须能在流式中途停止"

    messages = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    actor = [m for m in messages.json()["messages"] if m["role"] == "actor"]
    assert actor, "已交付的部分必须保留"
    assert actor[0]["state"] in {"stopped", "completed"}
    assert len(actor[0]["text"]) >= len("".join(seen_deltas)) - 8


# ---------------------------------------------------------------- 失败与重试


async def _publish_failing_profile(env: TestEnv) -> None:
    """把 dialogue.fast 指向会失败的 fake provider，验证错误与重试路径。"""
    draft = await env.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {"provider_id": "broken", "kind": "fake", "enabled": True,
                 "extra": {"mode": "failing"}},
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "broken", "model_id": "deterministic"}
            ],
        },
    )
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text


async def _publish_fake_profile(env: TestEnv) -> None:
    draft = await env.client.put(
        "/v1/settings/draft",
        json={
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "fake", "model_id": "deterministic"}
            ]
        },
    )
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text


async def test_failure_reports_reason_and_retry_succeeds(logged_in: TestEnv) -> None:
    await _publish_failing_profile(logged_in)
    session_id = await _new_session(logged_in.client)
    events = await _send(logged_in.client, session_id, "这条会失败")
    assert events[-1][0] == "turn.failed"
    error = events[-1][1]["error"]
    assert error["code"] in {"provider_unavailable", "provider_error", "provider_timeout"}
    assert error["retriable"] is True
    turn_id = events[-1][1]["turn_id"]

    # 修复配置后重试
    await _publish_fake_profile(logged_in)
    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200
    retry_events = await read_sse(retry)
    assert retry_events[0][1]["retried"] is True
    assert retry_events[-1][0] == "turn.completed"

    messages = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    actors = [m for m in messages.json()["messages"] if m["role"] == "actor"]
    # 失败时没有产出文本，所以只有重试成功那一条；
    # 若失败时已有部分文本，必须保留（见 test_retry_preserves_delivered_text）
    assert len(actors) == 1, "失败且无产出时不应留下重复回复"
    assert actors[0]["state"] == "completed"
    assert actors[0]["attempt"] == 2, "重试产生的消息应标 attempt=2"
    assert actors[0]["state"] == "completed"


async def test_retry_rejected_for_completed_turn(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    events = await _send(logged_in.client, session_id, "正常完成")
    turn_id = events[-1][1]["turn_id"]
    response = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert response.status_code == 409


# ---------------------------------------------------------------- 重启恢复


async def test_restart_recovery_marks_dangling_turn_failed(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    turn_id = str(uuid.uuid4())
    # 模拟"进程在生成中崩溃"：直接写一条 streaming 状态的轮次
    async with session_scope() as session:
        from ling.domains.conversation.service import ConversationService

        conv = ConversationService(session)
        turn = await conv.create_turn(session_id, client_turn_id=turn_id, config_revision=0)
        await conv.set_turn_state(turn.id, TurnState.streaming)

    # 模拟"进程崩溃后很久没人管"：把 updated_at 推到超过阈值。
    # 只用刚创建的轮次不够——修复后的实现只处理长时间无更新的轮次，
    # 避免多副本时误杀另一个实例正在跑的请求。
    from datetime import UTC, datetime, timedelta

    from ling.infra.models import Turn as TurnRow

    async with session_scope() as session:
        stale = (
            await session.execute(select(TurnRow).where(TurnRow.window_id == session_id))
        ).scalars().first()
        assert stale is not None
        stale.updated_at = datetime.now(UTC) - timedelta(minutes=30)

    # 模拟重启：调用应用的重启恢复逻辑
    from ling.app import _recover_dangling_turns

    await _recover_dangling_turns()

    listing = await logged_in.client.get(f"/v1/sessions/{session_id}/messages")
    assert listing.json()["active_turn"] is None, "重启后不得永远显示生成中"

    async with session_scope() as session:

        rows = list((await session.execute(select(Turn).where(Turn.window_id == session_id))).scalars())
    states = {r.client_turn_id: r.state for r in rows}
    assert states[turn_id] == "failed"


# ---------------------------------------------------------------- 设置生效


async def test_published_config_takes_effect_and_rollback(logged_in: TestEnv) -> None:
    # 先发布一个已知良好的基线修订，才有可回滚的目标
    good = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [{"provider_id": "fake", "kind": "fake", "enabled": True}],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "fake", "model_id": "deterministic"}
            ],
            "test_mode": True,
            "note": "W0 回归：良好基线",
        },
    )
    base_revision = good.json()["revision"]
    assert (
        await logged_in.client.post("/v1/settings/publish", json={"revision": base_revision})
    ).status_code == 200

    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True,
                 "extra": {"chunk_delay": 0.01}},
                {"provider_id": "broken", "kind": "fake", "enabled": True,
                 "extra": {"mode": "failing"}},
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "broken", "model_id": "deterministic"}
            ],
            "test_mode": True,
            "note": "W0 回归：让对话失败",
        },
    )
    revision = draft.json()["revision"]
    published = await logged_in.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200

    session_id = await _new_session(logged_in.client)
    events = await _send(logged_in.client, session_id, "验证新配置生效")
    assert events[-1][0] == "turn.failed", "发布后的配置必须对下一轮生效"

    rollback = await logged_in.client.post(
        "/v1/settings/rollback", json={"to_revision": base_revision}
    )
    assert rollback.status_code == 200
    events2 = await _send(logged_in.client, session_id, "回滚后应恢复")
    assert events2[-1][0] == "turn.completed"


async def test_invalid_config_is_rejected(logged_in: TestEnv) -> None:
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "does-not-exist", "model_id": "x"}
            ]
        },
    )
    body = draft.json()
    assert body["validation_errors"], "未登记的 provider 必须被校验拦下"
    published = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert published.status_code == 422
    assert published.json()["error"]["code"] == "config_invalid"


async def test_secret_is_masked_and_never_echoed(logged_in: TestEnv) -> None:
    draft = await logged_in.client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {"provider_id": "fake", "kind": "fake", "enabled": True},
                {"provider_id": "compat", "kind": "openai_compatible",
                 "base_url": "http://127.0.0.1:9999/v1", "enabled": True},
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "fake", "model_id": "deterministic"}
            ],
            "test_mode": True,
        },
    )
    saved = await logged_in.client.put(
        "/v1/settings/providers/compat/secret", json={"secret": "sk-test-abcdefgh1234"}
    )
    assert saved.status_code == 200
    assert saved.json()["last4"] == "1234"
    assert "sk-test" not in saved.text

    # 未保存密钥时发布会提示（has_secret=false），保存后发布应通过
    published = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text

    config = await logged_in.client.get("/v1/settings/config")
    assert "sk-test" not in config.text, "配置接口绝不能回显密钥原文"
    compat = [p for p in config.json()["providers"] if p["provider_id"] == "compat"]
    assert len(compat) == 1
    assert compat[0]["has_secret"] is True
    assert compat[0]["secret_last4"] == "1234"


# ---------------------------------------------------------------- 角色与时间线


async def test_persona_update_affects_next_turn_and_timeline_records(logged_in: TestEnv) -> None:
    updated = await logged_in.client.put(
        "/v1/persona",
        json={"display_name": "小ling", "system_prompt": "你是 ling，说话简短。"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "小ling"

    session_id = await _new_session(logged_in.client)
    events = await _send(logged_in.client, session_id, "你好")
    assert events[-1][0] == "turn.completed"

    timeline = await logged_in.client.get("/v1/actor/timeline")
    assert timeline.status_code == 200
    types = [e["type"] for e in timeline.json()["events"]]
    assert "turn.completed" in types


# ---------------------------------------------------------------- 健康


async def test_health_endpoints(logged_in: TestEnv) -> None:
    alive = await logged_in.client.get("/healthz")
    assert alive.status_code == 200
    ready = await logged_in.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["backend"] == "postgresql"
    model = await logged_in.client.get("/v1/health/model")
    assert model.status_code == 200
    assert "dialogue.fast" in model.json()["profiles"]


async def test_validation_errors_are_structured(logged_in: TestEnv) -> None:
    session_id = await _new_session(logged_in.client)
    empty = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "   "},
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_request"

    missing = await logged_in.client.get("/v1/sessions/win_missing/messages")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def _unused(_: TestEnv) -> None:  # pragma: no cover
    _ = update


def test_turn_pipeline_applies_expression_cleanup() -> None:
    """回复链路必须**真的调用**表达清理。

    这里检查的是接线，不是函数行为（函数行为在
    `tests/unit/test_expression_naturalness.py`）。

    为什么需要单独一条：`ExpressionService` 之前只被用来数段数，
    文本从未被处理——"有实现但没接线"是这类改动最容易出的问题，
    而端到端测试测不出来（假模型的回复里本来就不含客服腔）。
    """
    import inspect

    from ling.kernel import turn

    source = inspect.getsource(turn)
    assert "strip_service_tail" in source, "回复链路没有调用客服腔清理"
    # 分段用的是与渠道投递同一套 `plan_parts`（尊重模型换行、代码块不拆），
    # 不是裸的 split_semantic——两处规则必须一致，否则"Web 存成一条、
    # QQ 分三条"就对不上了。
    assert "plan_parts" in source, "回复链路没有调用分段"
    # 顺序：必须先清理再分段，否则尾句被切成独立段后就不认得了。
    # 比的是**调用**位置（带实参），不是 import 行——import 顺序不代表调用顺序。
    call_strip = source.index("strip_service_tail(delivered)")
    call_split = source.index("plan_parts(delivered")
    assert call_strip < call_split, "先分段后清理：尾句被切成独立段，清理会失效"


async def test_merge_subjects_api_round_trip(logged_in) -> None:  # noqa: ANN001
    """通过 HTTP 合并再拆分：合并只写指针，拆分立刻还原。

    合并是**人的决定**，所以走 API 由人触发；系统只提供操作和现状，
    不自动判断"这两个号是同一个人"。
    """

    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope
    from ling.infra.models import Subject

    async with session_scope() as session:
        src = await IdentityService(session).resolve_or_provision(
            "napcat-2", "5560001", display_name="小号X"
        )
        owner = await IdentityService(session).owner_subject_id()
    assert src and owner and src != owner

    merged = (
        await logged_in.client.post(
            "/v1/accounts/subjects/merge",
            json={"source_id": src, "target_id": owner},
        )
    ).json()
    assert merged["moved"] == 0, "合并搬了数据——那就拆不干净了"

    listing = (await logged_in.client.get("/v1/accounts/subjects")).json()
    row = next(s for s in listing["subjects"] if s["subject_id"] == src)
    assert row["merged_into"] == owner

    unmerged = (
        await logged_in.client.post(f"/v1/accounts/subjects/{src}/unmerge")
    ).json()
    assert unmerged["was_merged_into"] == owner

    async with session_scope() as session:
        back = await session.get(Subject, src)
    assert getattr(back, "merged_into", None) is None


async def test_merge_api_rejects_self(logged_in) -> None:  # noqa: ANN001
    """自己并自己要被拒——这是"操作者点错了"，早失败比事后串号好。"""
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        owner = await IdentityService(session).owner_subject_id()
    response = await logged_in.client.post(
        "/v1/accounts/subjects/merge",
        json={"source_id": owner, "target_id": owner},
    )
    assert response.status_code == 422, response.text
