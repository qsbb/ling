"""结构完整性：声明的授权要求必须真的被执行。

这条测试是为了防住一类最难发现的缺陷：**规则看起来实现了，其实没有**。

实际例子：`ACTION_REQUIREMENTS` 声明了 `"device.action": "device"`，
但 `PolicyContext` 没有 `device_granted` 字段，`evaluate` 里也没有对应检查。
结果 device.action 永远不会因为"未授权设备"被拒绝——把最危险的一类
真实世界动作静默放行了。

个案测试防不住这类问题（没人想起来给 device 写用例）。
所以这里遍历所有声明的要求，逐个验证"未授权时必被拒绝"。
新增要求却忘了写检查，这条测试会立刻失败。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import ActionIntent, Visibility
from ling.kernel.policy import ACTION_REQUIREMENTS, PolicyContext, PolicyGate

# 要求 → 授予它的字段名。新增要求时必须补进这张表，
# 否则下面的完整性断言会失败（强制作者想清楚"这个要求靠什么满足"）。
REQUIREMENT_GRANTS: dict[str, str] = {
    "memory.cross_device": "cross_device_granted",
    "camera": "camera_granted",
    "location": "location_granted",
    "public_publish": "publish_granted",
    "proactive_contact": "channel_supports_proactive",
    "device": "device_granted",
}


def _base_ctx(**overrides: object) -> PolicyContext:
    base = {
        "subject_id": "subj_x",
        "window_id": "win_x",
        "visibility": Visibility.subject_private,
        "is_group": False,
        # 默认**全部授权**，只把被测要求关掉——否则会混入其他拒绝原因
        "cross_device_granted": True,
        "camera_granted": True,
        "location_granted": True,
        "publish_granted": True,
        "device_granted": True,
        "channel_supports_proactive": True,
        "channel_supports_group": True,
    }
    base.update(overrides)
    return PolicyContext(**base)  # type: ignore[arg-type]


def test_every_declared_requirement_has_a_grant_field() -> None:
    """每个声明的要求都必须有对应的授权字段。"""
    declared = {r for r in ACTION_REQUIREMENTS.values() if r is not None}
    missing = declared - set(REQUIREMENT_GRANTS)
    assert not missing, (
        f"这些要求声明了但没有对应的授权字段校验: {sorted(missing)}。"
        "要么补字段与检查，要么从 ACTION_REQUIREMENTS 删掉。"
    )


@pytest.mark.parametrize("requirement", sorted(r for r in ACTION_REQUIREMENTS.values() if r))
def test_requirement_is_denied_when_not_granted(requirement: str) -> None:
    """未授权时，属于该要求的动作必须被拒绝。"""
    field = REQUIREMENT_GRANTS[requirement]
    kinds = [k for k, v in ACTION_REQUIREMENTS.items() if v == requirement]
    assert kinds, f"要求 {requirement} 没有任何动作类别使用"

    for kind in kinds:
        verdict = PolicyGate().evaluate(
            ActionIntent(intent_id="i", kind=kind),
            _base_ctx(**{field: False}),
        )
        assert verdict.allowed is False, (
            f"{kind} 要求 {requirement}，但未授权时仍被放行"
        )
        assert verdict.reasons, f"{kind} 被拒但没有给出原因"


@pytest.mark.parametrize("requirement", sorted(r for r in ACTION_REQUIREMENTS.values() if r))
def test_requirement_is_allowed_when_granted(requirement: str) -> None:
    """授权后必须放行（不能为了安全把功能全锁死）。"""
    field = REQUIREMENT_GRANTS[requirement]
    kinds = [k for k, v in ACTION_REQUIREMENTS.items() if v == requirement]
    for kind in kinds:
        verdict = PolicyGate().evaluate(
            ActionIntent(intent_id="i", kind=kind),
            _base_ctx(**{field: True}),
        )
        assert verdict.allowed is True, (
            f"{kind} 已授权 {requirement} 却被拒: {verdict.reasons}"
        )


def test_all_context_grant_fields_are_used() -> None:
    """反向检查：PolicyContext 里的授权字段都应该被某个要求用到。

    否则就是"加了字段但没人读"——同样是假实现。
    """
    used = set(REQUIREMENT_GRANTS.values())
    fields = {
        f
        for f in PolicyContext.__dataclass_fields__  # type: ignore[attr-defined]
        if f.endswith("_granted") or f == "channel_supports_proactive"
    }
    unused = fields - used
    assert not unused, f"这些授权字段从未被任何要求检查: {sorted(unused)}"


def test_unregistered_action_is_denied() -> None:
    """失败关闭：未登记类别一律拒绝。"""
    verdict = PolicyGate().evaluate(
        ActionIntent(intent_id="i", kind="totally.unknown"), _base_ctx()
    )
    assert verdict.allowed is False


def test_forbidden_action_denied_even_with_all_grants() -> None:
    """绝对禁止的类别，任何授权都不放行。"""
    from ling.kernel.policy import FORBIDDEN

    for kind in FORBIDDEN:
        verdict = PolicyGate().evaluate(ActionIntent(intent_id="i", kind=kind), _base_ctx())
        assert verdict.allowed is False, f"{kind} 属于绝对禁止却放行了"


def test_tool_risks_map_to_registered_actions() -> None:
    """工具风险等级必须都有对应的已登记动作类别。

    否则工具执行会走到"未登记 → 拒绝"或干脆绕开裁决。
    """
    from ling.capabilities.tools import ToolRisk

    for risk in ToolRisk:
        kind = f"tool.{risk.value}"
        assert kind in ACTION_REQUIREMENTS, f"工具风险 {risk.value} 没有对应的动作类别"
