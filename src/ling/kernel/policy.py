"""PolicyGate：权限、风险、成本、频道可达性的确定性硬门。

设计文档 §1 第 7 条：**模型提出解释和候选，系统提交状态**。
权限、身份、隐私、时序、预算及不可逆动作由本模块最终裁决，不由模型决定。

本模块不调用模型，也不写数据库；它只根据已解析的作用域与授权给出裁决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ling.contracts.v1 import ActionIntent, Visibility


@dataclass(frozen=True)
class PolicyContext:
    """裁决所需的确定性事实。全部由服务端解析，客户端/模型不能伪造。"""

    subject_id: str
    window_id: str
    visibility: Visibility
    is_group: bool
    cross_device_granted: bool = False
    camera_granted: bool = False
    location_granted: bool = False
    publish_granted: bool = False
    # 设备/真实世界动作的授权（关灯、发消息到已连接设备等）。
    # 之前 ACTION_REQUIREMENTS 声明了 "device.action": "device"
    # 但既没有这个字段也没有对应检查，等于该要求从未被强制执行。
    device_granted: bool = False
    channel_supports_proactive: bool = False
    channel_supports_group: bool = False
    actor_busy: bool = False
    budget_remaining: float | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reasons": self.reasons,
            "requires_confirmation": self.requires_confirmation,
        }


# 动作类别 → 需要的授权 scope（None 表示不需要额外授权）
ACTION_REQUIREMENTS: dict[str, str | None] = {
    "reply.text": None,
    "reply.speech": None,
    "avatar.action": None,
    "memory.cross_device_recall": "memory.cross_device",
    "perception.camera": "camera",
    "perception.location": "location",
    "publish.public": "public_publish",
    "proactive.contact": "proactive_contact",
    "device.action": "device",
    # 工具执行：只读与可逆无需额外授权，但**必须过裁决**（失败关闭）。
    # 不可逆工具等同"真实世界动作"，需要设备级授权；
    # 公开发布工具等同 publish.public。
    "tool.read_only": None,
    "tool.reversible": None,
    "tool.irreversible": "device",
    "tool.external_publish": "public_publish",
}

# 必须二次确认的动作类别
CONFIRM_REQUIRED = {
    "publish.public",
    "device.action",
    "tool.irreversible",
    "tool.external_publish",
}

# 绝对禁止的类别（任何授权都不放行）
FORBIDDEN = {"memory.export_raw_others", "identity.impersonate"}


class PolicyGate:
    """确定性裁决器。所有拒绝都给出可解释原因。"""

    def evaluate(self, intent: ActionIntent, ctx: PolicyContext) -> PolicyDecision:
        reasons: list[str] = []

        if intent.kind in FORBIDDEN:
            return PolicyDecision(allowed=False, reasons=[f"该动作类别被禁止: {intent.kind}"])

        requirement = ACTION_REQUIREMENTS.get(intent.kind)
        if requirement is None and intent.kind not in ACTION_REQUIREMENTS:
            # 未登记的动作类别一律拒绝（失败关闭）
            return PolicyDecision(
                allowed=False, reasons=[f"未登记的动作类别，默认拒绝: {intent.kind}"]
            )

        if requirement == "memory.cross_device" and not ctx.cross_device_granted:
            reasons.append("未授权跨端记忆召回（memory.cross_device）")
        if requirement == "camera" and not ctx.camera_granted:
            reasons.append("未授权相机（默认关闭）")
        if requirement == "location" and not ctx.location_granted:
            reasons.append("未授权位置（默认关闭）")
        if requirement == "public_publish" and not ctx.publish_granted:
            reasons.append("未授权公开发布（默认关闭）")
        if requirement == "device" and not ctx.device_granted:
            reasons.append("未授权设备/真实世界动作（默认关闭）")
        if requirement == "proactive_contact":
            if not ctx.channel_supports_proactive:
                reasons.append("当前渠道不支持主动投递")
            if ctx.actor_busy:
                reasons.append("角色正在与用户对话，避免打断")

        # 受众规则：群聊不能读取/输出私聊内容
        if ctx.is_group and ctx.visibility in {
            Visibility.subject_private,
            Visibility.window_private,
            Visibility.actor_private,
        }:
            reasons.append("群聊场景不能使用私聊可见范围的内容")

        if intent.kind == "reply.text" and ctx.is_group and not ctx.channel_supports_group:
            reasons.append("当前渠道不支持群聊发送")

        if intent.requires_confirmation and intent.kind not in CONFIRM_REQUIRED:
            # 模型自己要求确认是允许的；但系统对高风险类别强制确认
            pass
        needs_confirmation = intent.kind in CONFIRM_REQUIRED

        if ctx.budget_remaining is not None and ctx.budget_remaining <= 0:
            reasons.append("预算已用尽")

        return PolicyDecision(
            allowed=not reasons,
            reasons=reasons,
            requires_confirmation=needs_confirmation,
        )

    def visible_memory_scopes(self, ctx: PolicyContext) -> set[Visibility]:
        """本轮可读取的记忆可见范围（委托到唯一权威实现）。"""
        from ling.kernel.audience import readable_visibilities

        return readable_visibilities(
            is_group=ctx.is_group,
            cross_device_granted=ctx.cross_device_granted,
        )


def context_from_snapshot(
    snapshot: object,
    *,
    channel_supports_group: bool = True,
    actor_busy: bool = False,
    budget_remaining: float | None = None,
) -> PolicyContext:
    """从本轮冻结的受众快照构造裁决上下文。

    这样 PolicyGate 看到的身份/受众/授权与 ContextCompiler 完全一致，
    不会出现"取数一套、裁决另一套"的偏差。
    """
    return PolicyContext(
        subject_id=getattr(snapshot, "subject_id", ""),
        window_id=getattr(snapshot, "window_id", ""),
        visibility=(
            Visibility.group_scope
            if getattr(snapshot, "is_group", False)
            else Visibility.subject_private
        ),
        is_group=bool(getattr(snapshot, "is_group", False)),
        cross_device_granted=bool(getattr(snapshot, "cross_device_granted", False)),
        channel_supports_group=channel_supports_group,
        actor_busy=actor_busy,
        budget_remaining=budget_remaining,
    )


__all__ = [
    "ACTION_REQUIREMENTS",
    "CONFIRM_REQUIRED",
    "FORBIDDEN",
    "PolicyContext",
    "PolicyDecision",
    "PolicyGate",
    "context_from_snapshot",
]
