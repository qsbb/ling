"""第三方客户端 SDK 的端到端测试。

`connectors/sdk/client.py` 是文档里给第三方（手机、VR、桌面形象）的接入路径。
**未测的对外接口等于没有接口**——第三方照着用会在第一轮就撞坑，
而问题会算在 ling 头上。

这里用 SDK 走完整闭环：登录 → 建会话 → 发消息读流 → 读历史 → 健康检查。
"""

from __future__ import annotations

import uuid

import pytest
from connectors.sdk.client import LingClient, LingClientConfig

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _client(env: TestEnv) -> LingClient:
    """用 ASGI transport 直连应用，不起真实网络。

    SDK 的 `http` 参数是公开签名的一部分——第三方也会这样注入自己的 client
    （代理、重试、日志），所以这条路径本身值得被测。
    """
    import httpx

    from ling.app import create_app  # noqa: F401

    return LingClient(
        LingClientConfig(base_url="http://testserver"),
        http=httpx.AsyncClient(
            transport=env.client._transport,  # type: ignore[attr-defined]
            base_url="http://testserver",
        ),
    )


async def _login(env: TestEnv) -> LingClient:
    client = _client(env)
    await client.login("tester", "tester-password")
    return client


async def test_sdk_login_and_me(logged_in: TestEnv) -> None:
    client = await _login(logged_in)
    try:
        me = await client.me()
        assert me.get("user") or me.get("subject_id"), f"me() 返回异常: {me}"
    finally:
        await client.close()


async def test_sdk_full_turn_roundtrip(logged_in: TestEnv) -> None:
    """SDK 能发一轮并读到完整回复。

    这是第三方接入的最小可用路径，必须一次跑通。
    """
    client = await _login(logged_in)
    try:
        session = await client.create_session("sdk 测试")
        assert session.get("session_id"), f"create_session 返回异常: {session}"

        chunks: list[dict] = []
        async for event in client.send(
            session["session_id"], "你好", client_turn_id=str(uuid.uuid4())
        ):
            chunks.append(event)
        assert chunks, "SDK 没有拿到任何事件"

        kinds = {e.get("event") or e.get("type") for e in chunks}
        assert any("complete" in str(k) for k in kinds), (
            f"没有收到完成事件，实际: {kinds}"
        )
        # 完成事件里必须带最终文本（否则第三方不知道显示了什么）
        final = [
            e for e in chunks if "complete" in str(e.get("event") or e.get("type"))
        ][-1]
        assert (final.get("data") or {}).get("message"), "完成事件里没有最终消息"
    finally:
        await client.close()


async def test_sdk_reads_history(logged_in: TestEnv) -> None:
    client = await _login(logged_in)
    try:
        session = await client.create_session("sdk 历史")
        async for _ in client.send(
            session["session_id"], "记一下：我喜欢下雨天", client_turn_id=str(uuid.uuid4())
        ):
            pass
        history = await client.messages(session["session_id"])
        items = history.get("messages") or []
        assert len(items) >= 2, f"历史条数不足: {len(items)}"
        assert any(m.get("role") == "user" for m in items)
        assert any(m.get("role") == "actor" for m in items)
    finally:
        await client.close()


async def test_sdk_lists_sessions(logged_in: TestEnv) -> None:
    client = await _login(logged_in)
    try:
        await client.create_session("sdk 列表")
        sessions = await client.list_sessions()
        assert isinstance(sessions, list) and sessions
    finally:
        await client.close()


async def test_sdk_liveness_no_login_needed(logged_in: TestEnv) -> None:
    """存活探针不需要登录——重连判断要用它。"""
    client = _client(logged_in)
    try:
        body = await client.liveness()
        assert body.get("status") == "alive"
    finally:
        await client.close()


async def test_sdk_model_health(logged_in: TestEnv) -> None:
    client = await _login(logged_in)
    try:
        body = await client.health()
        # 模型健康回答"能不能思考"，字段与存活探针不同
        assert "providers" in body or "status" in body, f"模型健康返回异常: {body}"
    finally:
        await client.close()


async def test_sdk_requires_login(logged_in: TestEnv) -> None:
    """未登录调用必须是明确错误，不能静默返回空。"""
    client = _client(logged_in)
    try:
        # 未登录必须给出明确错误（SDK 会把 HTTP 错误抛出来），
        # 不能静默返回空列表——静默会让第三方以为"确实没有会话"。
        import httpx as _httpx

        with pytest.raises(_httpx.HTTPStatusError) as exc:
            await client.list_sessions()
        assert exc.value.response.status_code == 401
    finally:
        await client.close()
