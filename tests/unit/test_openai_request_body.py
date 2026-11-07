"""普通文本对话的请求体**不许变形**（回归闸）。

为什么值得单独一个文件盯着它：供应商适配器的请求体是"改一行、全网静默坏掉"
的那种地方。``062499d`` 给消息加了 ``videos``（原生视频输入），只需一个
``if not images`` → ``if not images and not videos`` 写反、或者把纯文本也包成
内容数组，就会让**每一条普通对话**变成 400，而所有单元/集成测试仍然全绿——
它们跑的是 fake provider，不发 HTTP。

所以这里不看"能不能拿到回复"，而是**逐字节看请求体**：

- 纯文本 → ``{"role", "content": "字符串"}``，不是数组、不带 ``video_url``；
- 带图 → 只有 ``text`` + ``image_url`` 两段；
- 带视频 → 才出现 ``video_url``（且只有那一条消息）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ling.models.openai_compatible import OpenAICompatibleProvider
from ling.ports.model import ChatMessageIn, ImagePart, ModelRequest, VideoPart

pytestmark = pytest.mark.w0_web

_SSE = (
    'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
).encode()


async def _sent_body(messages: list[ChatMessageIn], *, profile: str = "dialogue.fast") -> dict:
    """跑一次适配器的 stream，返回它**真正发出去**的 JSON。"""
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, content=_SSE, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        provider_id="main", base_url="https://api.example.com/v1", api_key="sk-test"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        request = ModelRequest(
            model_id="gpt-x", messages=messages, profile=profile, config_revision=7
        )
        async for _chunk in provider.stream(request):
            pass
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    assert len(captured) == 1
    return captured[0]


async def test_plain_dialogue_body_is_unchanged() -> None:
    """普通文本对话：整份请求体与「视频职责」之前**逐字段相同**。"""
    body = await _sent_body(
        [
            ChatMessageIn(role="system", content="角色名：ling\n你在跟 Co 聊天。"),
            ChatMessageIn(role="user", content="111"),
        ]
    )
    assert body == {
        "model": "gpt-x",
        "messages": [
            {"role": "system", "content": "角色名：ling\n你在跟 Co 聊天。"},
            {"role": "user", "content": "111"},
        ],
        "stream": True,
        # 只有 dialogue.* 才带它，目的是让兼容端点回 usage（额度记账用）
        "stream_options": {"include_usage": True},
    }
    # 三条"不许出现"，比整体相等更容易在失败时看懂
    assert "temperature" not in body, "温度应交给上游默认值"
    blob = json.dumps(body, ensure_ascii=False)
    assert "video_url" not in blob, "纯文本对话里不该出现 video_url"
    assert "image_url" not in blob
    assert all(isinstance(m["content"], str) for m in body["messages"]), (
        "纯文本消息不许被包成内容数组"
    )


async def test_image_message_uses_content_array_without_video() -> None:
    body = await _sent_body(
        [
            ChatMessageIn(
                role="user",
                content="这图什么意思",
                images=(ImagePart(url="data:image/jpeg;base64,AAAA"),),
            )
        ]
    )
    content = body["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert "video_url" not in json.dumps(body)


async def test_video_part_appears_only_on_its_own_message() -> None:
    """带视频的那条才出现 ``video_url``，其余消息保持纯文本。"""
    body = await _sent_body(
        [
            ChatMessageIn(role="system", content="角色名：ling"),
            ChatMessageIn(
                role="user",
                content="看这个",
                videos=(VideoPart(url="https://cdn.example.com/a.mp4"),),
            ),
        ]
    )
    assert body["messages"][0] == {"role": "system", "content": "角色名：ling"}
    content = body["messages"][1]["content"]
    assert [part["type"] for part in content] == ["text", "video_url"]
    assert content[1]["video_url"] == {"url": "https://cdn.example.com/a.mp4"}
