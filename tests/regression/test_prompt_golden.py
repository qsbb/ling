"""Compare compiled messages, including native tool replay, not model replies."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, tzinfo
from typing import NotRequired, TypedDict

import pytest
import pytest_asyncio

from ling.capabilities import tools
from ling.domains.awareness.service import AwarenessService
from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import ConversationService
from ling.domains.media import service as media
from ling.domains.media.video import short_ref_for
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow, MemoryRow, Persona, Window
from ling.kernel import env_facts, turn
from ling.kernel.context import ContextCompiler
from ling.ports.model import ChatMessageIn, ModelChunk, ModelRequest, ToolCall
from tests.integration.conftest import TestEnv, read_sse
from tests.regression.chat_fixture import (
    ScriptedProvider,
    install_provider,
    new_window,
    publish,
)
from tests.regression.snapshots import snapshot

pytestmark = pytest.mark.w0_web
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
INPUT = "还记得我说过的样本植物吗？"
PRIVATE = "样本植物：私聊限定盆栽。"
GROUP = "样本植物：群内共享盆栽。"
CROSS_FACT = "样本黑猫甲"
CROSS_CHAT = "这句仅存在于来源窗口的聊天记录。"


@pytest_asyncio.fixture
async def prompt_env(logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch) -> TestEnv:
    monkeypatch.setattr(env_facts, "_utcnow", lambda: NOW)
    async with session_scope() as session:
        session.add(
            Persona(
                id=logged_in.settings.persona_id,
                display_name="样本人格",
                name="sample",
                system_prompt="你是用来验证提示词顺序的测试角色。",
                boundaries=["只使用合成数据"],
            )
        )
    return logged_in


class PromptMessage(TypedDict):
    role: str
    content: str
    tool_calls: NotRequired[list[dict[str, str]]]
    provider_call_id: NotRequired[str]


def project_messages(messages: list[ChatMessageIn]) -> list[PromptMessage]:
    transcript = []
    for msg in messages:
        entry: PromptMessage = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "name": call.name,
                    "provider_call_id": call.provider_call_id,
                    "arguments": call.arguments,
                }
                for call in msg.tool_calls
            ]
        if msg.provider_call_id is not None:
            entry["provider_call_id"] = msg.provider_call_id
        transcript.append(entry)
    return transcript


async def compile_prompt(
    env: TestEnv, window_id: str, *, group: bool = False
) -> list[PromptMessage]:
    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id=env.settings.actor_id, persona_id=env.settings.persona_id
        ).compile(
            subject_id=env.subject_id,
            window_id=window_id,
            user_text=INPUT,
            is_group=group,
            user_sender_name="样本群友" if group else None,
        )
        return project_messages(compiled.messages)


def text_of(messages: list[PromptMessage]) -> str:
    return "\n".join(msg["content"] for msg in messages)


@pytest.mark.parametrize("group", [False, True], ids=["web-private", "group-window"])
async def test_prompt_audiences(prompt_env: TestEnv, group: bool) -> None:
    private = await new_window(prompt_env, "测试私聊")
    group_id = await new_window(prompt_env, "测试群")
    async with session_scope() as session:
        window = await session.get(Window, group_id)
        window.visibility = "group_scope"
        for window_id, visibility, text in (
            (private, "subject_private", PRIVATE),
            (group_id, "group_scope", GROUP),
        ):
            session.add(
                MemoryRow(
                    id=f"mem_wp00_{visibility}",
                    subject_id=prompt_env.subject_id,
                    kind="assertion",
                    text=text,
                    origin="user_statement",
                    visibility=visibility,
                    source_visibility=visibility,
                    source_window_id=window_id,
                    source_event_ids=["evt_wp00_sample"],
                    confidence=0.9,
                )
            )

    messages = await compile_prompt(prompt_env, group_id if group else private, group=group)
    text = text_of(messages)
    assert INPUT in text
    assert (GROUP if group else PRIVATE) in text
    assert (PRIVATE if group else GROUP) not in text
    snapshot("prompt-group-window" if group else "prompt-web-private", messages)


async def test_prompt_isolation_modes(prompt_env: TestEnv) -> None:
    source = await new_window(prompt_env, "来源私聊")
    target = await new_window(prompt_env, "目标私聊")
    client = prompt_env.client
    assert (await client.put("/v1/isolation", json={"mode": "continuous"})).status_code == 200
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=prompt_env.subject_id,
            window_id=source,
            window_visibility="subject_private",
            user_text=f"我养了{CROSS_FACT}。",
        )
        conv = ConversationService(session, actor_id=prompt_env.settings.actor_id)
        await conv.add_message(source, role="user", text=CROSS_CHAT)

    continuous = await compile_prompt(prompt_env, target)
    assert CROSS_FACT in text_of(continuous)
    assert CROSS_CHAT not in text_of(continuous)
    snapshot("prompt-continuous", continuous)

    assert (await client.put("/v1/isolation", json={"mode": "sealed"})).status_code == 200
    sealed = await compile_prompt(prompt_env, target)
    assert INPUT in text_of(sealed)
    assert CROSS_FACT not in text_of(sealed) and CROSS_CHAT not in text_of(sealed)
    snapshot("prompt-sealed", sealed)


async def test_prompt_video_marker(prompt_env: TestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    window_id = await new_window(prompt_env, "视频测试")
    before = await compile_prompt(prompt_env, window_id)
    evidence_id = "media_wp00_sample_video"
    marker = f"<<看视频 {short_ref_for(evidence_id)}>>"
    monkeypatch.setattr(media, "new_id", lambda _: evidence_id)
    async with session_scope() as session:
        await ConsentService(session).grant(
            subject_id=prompt_env.subject_id, scope=media.INBOUND_SCOPE
        )
        recorded = await media.MediaEvidenceService(
            session, actor_id=prompt_env.settings.actor_id
        ).record(
            subject_id=prompt_env.subject_id,
            window_id=window_id,
            media_ref="https://example.test/sample.mp4",
            kind="video",
        )
        row = await session.get(MediaEvidenceRow, recorded.evidence_id)
        row.created_at = NOW
    after = await compile_prompt(prompt_env, window_id)
    assert marker not in text_of(before)
    assert any(msg["role"] == "system" and marker in msg["content"] for msg in after)
    snapshot("prompt-video-marker", after)


async def test_prompt_native_tool_round(
    prompt_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fix the tool's observation timestamp and audit ID at their sources;
    # never scrub the resulting model messages before comparison.
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    original_new_id = turn.new_id
    monkeypatch.setattr(tools, "datetime", FixedDateTime)
    monkeypatch.setattr(
        turn, "new_id", lambda name: "call_wp00" if name == "call" else original_new_id(name)
    )

    async def stream(request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if request.tools and not any(msg.role == "tool" for msg in request.messages):
            yield ModelChunk(
                tool_call=ToolCall("sample-call", "knowledge.search", '{"query":"样本植物"}'),
                tool_call_complete=True,
            )
            yield ModelChunk(done=True, finish_reason="tool_calls")
        else:
            yield ModelChunk(delta="这是合成回复。")
            yield ModelChunk(done=True, finish_reason="stop")

    provider = ScriptedProvider(stream, native_tools=True)
    install_provider(prompt_env, provider)
    await publish(
        prompt_env,
        {
            "providers": [
                {
                    "provider_id": "sample",
                    "kind": "openai_compatible",
                    "base_url": "https://example.test/v1",
                    "extra": {"native_tools": True},
                    "models": [{"model_id": "sample-chat"}],
                }
            ],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "sample", "model_id": "sample-chat"}
            ],
            "dialogue": {"tools_enabled": False, "private_may_stay_silent": False},
        },
    )

    async def send() -> None:
        window_id = await new_window(prompt_env, "工具测试")
        response = await prompt_env.client.post(
            f"/v1/sessions/{window_id}/turns", json={"client_turn_id": "wp00", "text": INPUT}
        )
        assert response.status_code == 200, response.text
        assert (await read_sse(response))[-1][0] == "turn.completed"

    await send()
    (baseline,) = provider.requests
    assert not baseline.tools and not any(msg.role == "tool" for msg in baseline.messages)
    provider.requests.clear()

    await publish(prompt_env, {"dialogue": {"tools_enabled": True}})
    await send()
    first, followup = provider.requests
    assert "knowledge.search" in {tool.name for tool in first.tools}
    (result,) = [msg for msg in followup.messages if msg.role == "tool"]
    assert result.provider_call_id == "sample-call"
    assert json.loads(result.content)["outcome"] == "succeeded"
    snapshot(
        "prompt-native-tool", [project_messages(request.messages) for request in (first, followup)]
    )
