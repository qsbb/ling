"""工具注册表接线：能力必须真的可调用，不是只定义。

之前 ToolRegistry 只有单测，从未装配进应用——等于死代码。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_tools_are_registered(logged_in: TestEnv) -> None:
    """默认只注册只读/诊断工具，不可逆与公开发布不注册。"""
    response = await logged_in.client.get("/v1/tools")
    assert response.status_code == 200
    tools = response.json()["tools"]
    ids = {t["tool_id"] for t in tools}
    assert ids == {"clock.now", "diagnostics.echo", "shell.readonly"}
    assert all(t["risk"] == "read_only" for t in tools)


async def test_call_readonly_tool(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/tools/diagnostics.echo/call", json={"args": {"text": "ping"}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "succeeded"
    assert body["output"] == "ping"
    assert body["untrusted"] is True, "工具结果必须标为不可信数据"


async def test_call_unregistered_tool_is_404(logged_in: TestEnv) -> None:
    """未注册的工具返回 404。

    之前返回 200 + outcome=unsupported，看着像"调用成功但工具不支持"。
    实际上工具不存在就是 404，与 API 其他错误（401/403/422）保持一致。
    """
    response = await logged_in.client.post("/v1/tools/nope/call", json={"args": {}})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unsupported_capability"


async def test_shell_tool_rejects_metacharacters(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/tools/shell.readonly/call", json={"args": {"command": "date; rm -rf /"}}
    )
    body = response.json()
    assert body["outcome"] == "failed"
    assert "元字符" in (body["detail"] or "")


async def test_shell_tool_rejects_non_whitelisted(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/tools/shell.readonly/call", json={"args": {"command": "curl http://example.com"}}
    )
    body = response.json()
    assert body["outcome"] == "failed"
    assert "白名单" in (body["detail"] or "")


async def test_tools_require_auth(web_env: TestEnv) -> None:
    response = await web_env.client.get("/v1/tools")
    assert response.status_code == 401
