"""实际 OneBot HTTP 入站必须消费 global/instance/session 对话策略。"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import ConversationAttemptSnapshot, Turn, Window
from ling.models.router import StreamOutcome
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ONEBOT_INSTANCE = "default"
ONEBOT_SECRET = "test-signature-secret"
ONEBOT_SELF_ID = "10001"
SENDER_ID = "20002"
GROUP_ID = "88888"
ORIGIN = {"Origin": "http://testserver"}


class FakeOneBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(
            {
                "action": request.url.path.rsplit("/", 1)[-1],
                "body": json.loads(request.content or b"{}"),
            }
        )
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}},
        )


class CapturingRouter:
    """First unaddressed group message replies; later choices decline."""

    def __init__(self, *, choice_response: bool | None = None) -> None:
        self.choice_prompts: list[str] = []
        self.generation_prompts: list[str] = []
        self.choice_response = choice_response

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        prompt = "\n".join(message.content or "" for message in messages)
        is_choice = bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")
        if is_choice:
            self.choice_prompts.append(prompt)
            respond = (
                self.choice_response
                if self.choice_response is not None
                else len(self.choice_prompts) == 1
            )
            output = json.dumps(
                {"respond": respond, "reason": "测试决策", "length": "normal"},
                ensure_ascii=False,
            )
        else:
            self.generation_prompts.append(prompt)
            output = "来自当前入站配置的回复"

        yield ModelChunk(delta=output), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=output)


async def _create_persona(env: TestEnv, name: str, prompt: str) -> str:
    response = await env.client.post(
        "/v1/personas",
        json={"display_name": name, "system_prompt": prompt},
    )
    assert response.status_code == 201, response.text
    return response.json()["persona_id"]


async def _publish_global_policy(env: TestEnv, values: dict) -> None:
    draft = await env.client.put("/v1/settings/draft", json={"dialogue": values})
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text


async def _save_instance_policy(env: TestEnv, values: dict) -> dict:
    body = {"set": values, "clear": []}
    path = f"/v1/settings/connectors/onebot/instances/{ONEBOT_INSTANCE}/dialogue-policy"
    preview = await env.client.post(f"{path}/preview", json=body, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    saved = await env.client.put(
        path,
        json={**body, "preview_token": preview.json()["preview_token"]},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


async def _save_session_policy(env: TestEnv, window_id: str, values: dict) -> dict:
    body = {"set": values, "clear": []}
    path = f"/v1/sessions/{window_id}/dialogue-policy"
    preview = await env.client.post(f"{path}/preview", json=body, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    saved = await env.client.put(
        path,
        json={**body, "preview_token": preview.json()["preview_token"]},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def _event_body(
    message_id: int,
    message: str,
    *,
    message_type: str = "group",
) -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": int(ONEBOT_SELF_ID),
        "post_type": "message",
        "message_type": message_type,
        "sub_type": "friend" if message_type == "private" else "normal",
        "message_id": message_id,
        "user_id": int(SENDER_ID),
        "message": message,
        "raw_message": message,
        "font": 0,
        "sender": {"nickname": "验收用户"},
    }
    if message_type == "group":
        payload["group_id"] = int(GROUP_ID)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _signed_headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(ONEBOT_SECRET.encode(), body, hashlib.sha1).hexdigest()
    return {
        "X-Signature": f"sha1={signature}",
        "X-Self-ID": ONEBOT_SELF_ID,
        "Content-Type": "application/json",
    }


async def _post_event(env: TestEnv, body: bytes) -> httpx.Response:
    return await env.client.post(
        "/v1/onebot/events", content=body, headers=_signed_headers(body)
    )


async def test_onebot_http_uses_instance_then_session_dialogue_policy(
    logged_in: TestEnv,
) -> None:
    global_persona = await _create_persona(
        logged_in, "全局验收人格", "GLOBAL_PERSONA_MARKER"
    )
    instance_persona = await _create_persona(
        logged_in, "实例验收人格", "INSTANCE_PERSONA_MARKER"
    )
    session_persona = await _create_persona(
        logged_in, "会话验收人格", "SESSION_PERSONA_MARKER"
    )
    activated = await logged_in.client.post(
        f"/v1/personas/{global_persona}/activate"
    )
    assert activated.status_code == 200, activated.text

    await _publish_global_policy(
        logged_in,
        {
            "private_may_stay_silent": False,
            "group_mention_requires_reply": True,
            "group_attention_deferred": True,
            "group_turn_budget": 4,
            "rest_may_skip": False,
        },
    )
    instance_policy = await _save_instance_policy(
        logged_in,
        {
            "persona_id": instance_persona,
            "private_may_stay_silent": True,
            "group_mention_requires_reply": False,
            "group_attention_deferred": False,
            "group_turn_budget": 9,
            "rest_may_skip": True,
        },
    )
    assert instance_policy["instance_revision"] == 1

    fake_platform = FakeOneBot()
    platform_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake_platform.handle)
    )
    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="test-token",
            secret=ONEBOT_SECRET,
            self_id=ONEBOT_SELF_ID,
            instance_id=ONEBOT_INSTANCE,
        ),
        http=platform_client,
    )
    logged_in.app.state.onebot_connector = connector
    router = CapturingRouter()
    logged_in.app.state.router = router

    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=logged_in.subject_id,
            connector_instance=ONEBOT_INSTANCE,
            external_account_id=SENDER_ID,
            proof="proven",
        )

    try:
        first = await _post_event(
            logged_in, _event_body(1001, "INSTANCE_GROUP_ATTENTION")
        )
        assert first.status_code == 200, first.text
        first_result = first.json()
        assert first_result["accepted"] is True, first_result
        assert first_result["decision"] == "reply", first_result
        assert first_result["delivered"] is True, first_result
        async with session_scope() as session:
            first_window = await session.scalar(
                select(Window).where(
                    Window.channel == "onebot",
                    Window.connector_window_ref == f"{ONEBOT_INSTANCE}:group:{GROUP_ID}",
                )
            )
        assert first_window is not None
        assert len(router.choice_prompts) == 1
        assert len(router.generation_prompts) == 1
        for prompt in (router.choice_prompts[0], router.generation_prompts[0]):
            assert "INSTANCE_PERSONA_MARKER" in prompt
            assert "GLOBAL_PERSONA_MARKER" not in prompt
            assert "SESSION_PERSONA_MARKER" not in prompt

        instance_mention = await _post_event(
            logged_in,
            _event_body(1002, "[CQ:at,qq=10001] INSTANCE_MENTION_POLICY"),
        )
        assert instance_mention.status_code == 200, instance_mention.text
        assert instance_mention.json()["accepted"] is True, instance_mention.text
        assert instance_mention.json()["decision"] == "silence", instance_mention.text
        assert not instance_mention.json().get("delivered", False)
        assert len(router.choice_prompts) == 2, (
            "实例 group_mention_requires_reply=false 应允许模型作出沉默决策"
        )

        session_policy = await _save_session_policy(
            logged_in,
            first_window.id,
            {
                "persona_id": session_persona,
                "group_mention_requires_reply": True,
                "group_attention_deferred": True,
                "group_turn_budget": 15,
            },
        )
        assert session_policy["session_revision"] == 1

        session_mention = await _post_event(
            logged_in,
            _event_body(1003, "[CQ:at,qq=10001] SESSION_MENTION_POLICY"),
        )
        assert session_mention.status_code == 200, session_mention.text
        assert session_mention.json()["accepted"] is True, session_mention.text
        assert session_mention.json()["decision"] == "reply", session_mention.text
        assert session_mention.json()["delivered"] is True, session_mention.text
        assert len(router.choice_prompts) == 2, (
            "会话 group_mention_requires_reply=true 应阻止模型推翻点名必回规则"
        )
        assert len(router.generation_prompts) == 2
        assert "SESSION_PERSONA_MARKER" in router.generation_prompts[1]
        assert "INSTANCE_PERSONA_MARKER" not in router.generation_prompts[1]
        assert "GLOBAL_PERSONA_MARKER" not in router.generation_prompts[1]

        private = await _post_event(
            logged_in,
            _event_body(1004, "PRIVATE_INSTANCE_SILENCE", message_type="private"),
        )
        assert private.status_code == 200, private.text
        assert private.json()["accepted"] is True, private.text
        assert private.json()["decision"] == "silence", private.text
        assert len(router.choice_prompts) == 3, (
            "实例 private_may_stay_silent=true 应允许模型在私聊中选择沉默"
        )
        assert not any(
            call["action"] == "send_private_msg" for call in fake_platform.sent
        )
    finally:
        await platform_client.aclose()

    async with session_scope() as session:
        group_window = (
            await session.execute(
                select(Window).where(
                    Window.channel == "onebot",
                    Window.connector_window_ref == f"{ONEBOT_INSTANCE}:group:{GROUP_ID}",
                )
            )
        ).scalar_one()
        assert group_window.origin_status == "known"
        assert group_window.origin_scope == "onebot"
        assert group_window.origin_instance_id == ONEBOT_INSTANCE

        snapshots = list(
            (
                await session.execute(
                    select(ConversationAttemptSnapshot)
                    .join(Turn, Turn.id == ConversationAttemptSnapshot.turn_id)
                    .where(Turn.window_id == group_window.id)
                    .order_by(ConversationAttemptSnapshot.created_at)
                )
            ).scalars()
        )

    assert [snapshot.persona_id for snapshot in snapshots] == [
        instance_persona,
        session_persona,
    ]
    instance_snapshot, session_snapshot = snapshots
    assert instance_snapshot.origin_status == "known"
    assert instance_snapshot.instance_scope == "onebot"
    assert instance_snapshot.instance_id == ONEBOT_INSTANCE
    assert instance_snapshot.instance_revision == 1
    assert instance_snapshot.session_revision == 0
    assert instance_snapshot.effective_values == {
        "private_may_stay_silent": True,
        "group_mention_requires_reply": False,
        "group_attention_deferred": False,
        "group_turn_budget": 9,
        "rest_may_skip": True,
    }
    assert instance_snapshot.field_sources == {
        "persona_id": "instance",
        "private_may_stay_silent": "instance",
        "group_mention_requires_reply": "instance",
        "group_attention_deferred": "instance",
        "group_turn_budget": "instance",
        "rest_may_skip": "instance",
    }
    assert session_snapshot.instance_revision == 1
    assert session_snapshot.session_revision == 1
    assert session_snapshot.effective_values["group_turn_budget"] == 15
    assert session_snapshot.effective_values["group_mention_requires_reply"] is True
    assert session_snapshot.effective_values["group_attention_deferred"] is True
    assert session_snapshot.field_sources["persona_id"] == "session"
    assert session_snapshot.field_sources["group_turn_budget"] == "session"
    assert session_snapshot.field_sources["group_mention_requires_reply"] == "session"
    assert session_snapshot.field_sources["group_attention_deferred"] == "session"


async def test_onebot_event_does_not_bind_legacy_unknown_window_to_current_instance(
    logged_in: TestEnv,
) -> None:
    global_persona = await _create_persona(
        logged_in, "未知来源全局人格", "UNKNOWN_GLOBAL_PERSONA_MARKER"
    )
    instance_persona = await _create_persona(
        logged_in, "未知来源实例人格", "UNKNOWN_INSTANCE_PERSONA_MARKER"
    )
    activated = await logged_in.client.post(
        f"/v1/personas/{global_persona}/activate"
    )
    assert activated.status_code == 200, activated.text

    global_values = {
        "private_may_stay_silent": False,
        "group_mention_requires_reply": True,
        "group_attention_deferred": False,
        "group_turn_budget": 4,
        "rest_may_skip": False,
    }
    await _publish_global_policy(logged_in, global_values)
    await _save_instance_policy(
        logged_in,
        {
            "persona_id": instance_persona,
            "private_may_stay_silent": True,
            "group_mention_requires_reply": False,
            "group_attention_deferred": True,
            "group_turn_budget": 9,
            "rest_may_skip": True,
        },
    )

    legacy_window_id = "legacy_unknown_policy_window"
    legacy_reference = f"{ONEBOT_INSTANCE}:group:{GROUP_ID}"
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=logged_in.subject_id,
            connector_instance=ONEBOT_INSTANCE,
            external_account_id=SENDER_ID,
            proof="proven",
        )
        session.add(
            Window(
                id=legacy_window_id,
                actor_id=logged_in.settings.actor_id,
                subject_id=logged_in.subject_id,
                # A legacy persona pointer must not establish platform provenance.
                persona_id=instance_persona,
                title=legacy_reference,
                channel="onebot",
                visibility="group_scope",
                connector_window_ref=None,
                origin_status="unknown",
                origin_scope=None,
                origin_instance_id=None,
                origin_manually_bound=True,
            )
        )

    policy_view = await logged_in.client.get(
        f"/v1/sessions/{legacy_window_id}/dialogue-policy"
    )
    assert policy_view.status_code == 200, policy_view.text
    policy = policy_view.json()
    assert policy["origin_status"] == "unknown"
    assert policy["instance_scope"] is None
    assert policy["instance_id"] is None
    assert policy["fields"]["persona_id"]["effective"] == global_persona
    assert policy["fields"]["persona_id"]["source"] == "global"
    assert policy["fields"]["group_mention_requires_reply"]["effective"] is True
    assert policy["fields"]["group_mention_requires_reply"]["source"] == "global"

    fake_platform = FakeOneBot()
    platform_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake_platform.handle)
    )
    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="test-token",
            secret=ONEBOT_SECRET,
            self_id=ONEBOT_SELF_ID,
            instance_id=ONEBOT_INSTANCE,
        ),
        http=platform_client,
    )
    logged_in.app.state.onebot_connector = connector
    router = CapturingRouter(choice_response=False)
    logged_in.app.state.router = router

    try:
        response = await _post_event(
            logged_in,
            _event_body(1101, "[CQ:at,qq=10001] LEGACY_UNKNOWN_POLICY"),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["accepted"] is True, result
        assert result["decision"] == "reply", result
        assert result["delivered"] is True, result
        assert router.choice_prompts == [], (
            "unknown 来源应使用全局 group_mention_requires_reply=true，直接回复"
        )
        assert len(router.generation_prompts) == 1
        assert "UNKNOWN_GLOBAL_PERSONA_MARKER" in router.generation_prompts[0]
        assert "UNKNOWN_INSTANCE_PERSONA_MARKER" not in router.generation_prompts[0]
    finally:
        await platform_client.aclose()

    async with session_scope() as session:
        legacy_window = await session.get(Window, legacy_window_id)
        assert legacy_window is not None
        assert legacy_window.title == legacy_reference
        assert legacy_window.connector_window_ref is None
        assert legacy_window.origin_status == "unknown"
        assert legacy_window.origin_scope is None
        assert legacy_window.origin_instance_id is None
        assert legacy_window.origin_manually_bound is True
        snapshots = list(
            (
                await session.execute(
                    select(ConversationAttemptSnapshot)
                    .join(Turn, Turn.id == ConversationAttemptSnapshot.turn_id)
                    .where(Turn.window_id == legacy_window_id)
                )
            ).scalars()
        )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.origin_status == "unknown"
    assert snapshot.instance_scope is None
    assert snapshot.instance_id is None
    assert snapshot.instance_revision == 0
    assert snapshot.session_revision == 0
    assert snapshot.persona_id == global_persona
    assert snapshot.effective_values == {
        "private_may_stay_silent": False,
        "group_mention_requires_reply": True,
        "group_attention_deferred": False,
        "group_turn_budget": 4,
        "rest_may_skip": False,
    }
    assert snapshot.field_sources == {
        "persona_id": "global",
        "private_may_stay_silent": "global",
        "group_mention_requires_reply": "global",
        "group_attention_deferred": "global",
        "group_turn_budget": "global",
        "rest_may_skip": "global",
    }
