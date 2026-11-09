"""工具执行必须过权限硬门。

设计文档 §1 第 7 条：权限、风险、成本、可达性与不可逆动作由系统裁决，
不由模型决定。工具执行正是"系统执行能力"的入口，绕开裁决就等于
把不可逆动作的执行权交给了调用方。

之前 call_tool 直接调 registry.call()，完全没有裁决。
"""

from __future__ import annotations

import pytest

from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _install_irreversible_tool(env: TestEnv) -> None:
    """在应用里装一个**允许不可逆**的注册表。

    默认 build_default_registry() 拒绝注册不可逆工具
    （PermissionError: 需要显式授权与审计），这正是我们想要的防线。
    测试要验证"注册之后仍然被裁决拦住"，所以显式开这个开关。
    """
    executed: list[dict] = []
    env.app.state.tool_executed = executed

    async def handler(args: dict) -> str:
        # ToolHandler 契约返回 str（结果会被切片后进提示）
        executed.append(dict(args))
        return "done"

    registry = ToolRegistry(allow_irreversible=True)
    registry.register(
        ToolSpec(
            tool_id="test.irreversible",
            version="1",
            description="测试用不可逆工具",
            risk=ToolRisk.irreversible,
            requires_confirmation=True,
            reversible=False,
        ),
        handler,
    )
    registry.register(
        ToolSpec(
            tool_id="diagnostics.echo",
            version="1",
            description="回显（只读）",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
        ),
        lambda args: _echo(args),  # type: ignore[arg-type]
    )
    env.app.state.tools = registry


async def _echo(args: dict) -> str:
    return str(args.get("text", ""))


async def test_readonly_tool_needs_no_grant(logged_in: TestEnv) -> None:
    """只读工具不需要任何授权，但**仍然过裁决**（未登记就会被拒）。"""
    response = await logged_in.client.post(
        "/v1/tools/diagnostics.echo/call", json={"args": {"text": "hi"}}
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "succeeded"


async def test_irreversible_tool_blocked_without_device_consent(
    logged_in: TestEnv,
) -> None:
    """未授权 device → 不可逆工具必须 403，且**不能被执行**。"""
    await _install_irreversible_tool(logged_in)
    # 请求体里**不能**带 `confirmed`（带了就 422，见
    # `test_wire_cannot_self_confirm_irreversible_tool`）。这条验的是权限门。
    response = await logged_in.client.post(
        "/v1/tools/test.irreversible/call", json={"args": {}}
    )
    assert response.status_code == 403, response.text
    assert "device" in response.text or "未授权" in response.text
    assert logged_in.app.state.tool_executed == [], "工具在未授权时仍被执行了"


async def test_irreversible_tool_requires_confirmation(logged_in: TestEnv) -> None:
    """已授权但未确认 → needs_confirmation，不执行。"""
    await _install_irreversible_tool(logged_in)
    await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": True}
    )
    response = await logged_in.client.post(
        "/v1/tools/test.irreversible/call", json={"args": {}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "needs_confirmation"
    assert logged_in.app.state.tool_executed == [], "未确认就执行了不可逆工具"


async def test_wire_cannot_self_confirm_irreversible_tool(
    logged_in: TestEnv,
) -> None:
    """**HTTP 请求体不能自证确认**——这是"确认"这条硬门的唯一正确语义。

    原来 `ToolCallBody.confirmed` 直接透传给 `registry.call(confirmed=...)`，
    于是任何拿到 token 的调用方在 body 里写 ``"confirmed": true`` 就等于自己
    批准了不可逆动作（这条路径连 owner/窗口归属都不校验）。

    现在：字段不存在 → 422，handler 一次都没进。**即使 device 已授权**也一样，
    因为"有没有确认"不该由调用方说了算。
    """
    await _install_irreversible_tool(logged_in)
    await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": True}
    )
    response = await logged_in.client.post(
        "/v1/tools/test.irreversible/call",
        json={"args": {"k": "v"}, "confirmed": True},
    )
    assert response.status_code == 422, response.text
    assert "确认" in response.text, "拒绝文案要指向『确认走提议端点』"
    assert logged_in.app.state.tool_executed == [], "请求体自证确认竟然执行了"


async def test_irreversible_tool_runs_only_via_server_side_confirmation(
    logged_in: TestEnv,
) -> None:
    """授权 + **服务端**确认 → 正常执行（不能为了安全把功能锁死）。

    确认的唯一来源是服务端自己（例如用户在网页点了"确认"，`recall.py` 的
    confirm 端点随后调 `registry.call(confirmed=True)`）。这条断言证明
    "锁死了 wire"没有连功能一起锁死。
    """
    await _install_irreversible_tool(logged_in)
    await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": True}
    )
    registry = logged_in.app.state.tools
    result = await registry.call("test.irreversible", {"k": "v"}, confirmed=True)

    assert result.outcome.value == "succeeded", result
    assert logged_in.app.state.tool_executed == [{"k": "v"}]


async def test_revoking_device_consent_blocks_again(logged_in: TestEnv) -> None:
    """授权可以撤回，撤回后必须立刻拦住。"""
    await _install_irreversible_tool(logged_in)
    await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": True}
    )
    await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": False}
    )
    response = await logged_in.client.post(
        "/v1/tools/test.irreversible/call", json={"args": {}}
    )
    assert response.status_code == 403


async def test_device_scope_is_grantable(logged_in: TestEnv) -> None:
    """device 必须是可授予的范围。

    之前 KNOWN_SCOPES 里没有 device，而 ACTION_REQUIREMENTS 里
    device.action / tool.irreversible 都要求它——要求无法被满足。
    """
    from ling.domains.consent.service import KNOWN_SCOPES

    assert "device" in KNOWN_SCOPES
    response = await logged_in.client.put(
        "/v1/consents", json={"scope": "device", "granted": True}
    )
    assert response.status_code == 200
