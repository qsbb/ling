"""ling 原生具身协议（W2：已实现未测试）。

与 设计文档 §7.1 对应：SessionStart / InputTurn / SpatialObservation /
AvatarIntent / ActionResult / PlaybackReceipt。

关键约束：
- 这是 **ling 原生协议**，不复用其他机器人框架的 URL、事件总线或身份配置。
- 相机、位置、空间感知**默认关闭**，必须逐项授权且有时效。
- 设备离线或授权过期后，在场证据失效；ling 不能继续声称看见/听见用户。
- AvatarIntent 只传**白名单内**的动作意图；客户端执行后回报真实结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ling.contracts.v1 import new_id

# 允许的动作意图白名单。新增动作必须在这里登记（客户端也要有实现）。
ALLOWED_AVATAR_ACTIONS = {
    "nod",
    "shake_head",
    "tilt_head",
    "smile",
    "blink",
    "look_at_user",
    "wave",
    "idle_breath",
}

# 感知能力。默认全部关闭，由用户在设备上逐项打开。
PERCEPTION_CAPABILITIES = {"microphone", "camera", "location", "spatial"}


class SessionState(StrEnum):
    starting = "starting"
    active = "active"
    paused = "paused"
    ended = "ended"
    expired = "expired"


class PresenceKind(StrEnum):
    phone_foreground = "phone_foreground"
    phone_call = "phone_call"
    vr = "vr"
    desktop_avatar = "desktop_avatar"
    offline = "offline"


@dataclass
class PerceptionGrant:
    """感知授权。有独立有效期，过期即失效。"""

    capability: str
    granted: bool = False
    expires_at: datetime | None = None

    def active(self, now: datetime | None = None) -> bool:
        if not self.granted:
            return False
        if self.expires_at is None:
            return True
        now = now or datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > now


@dataclass
class EmbodimentSession:
    """一个具身会话。session_id 与 turn_id 由服务器签发。"""

    session_id: str
    subject_id: str
    device_id: str
    presence: PresenceKind
    state: SessionState = SessionState.starting
    grants: dict[str, PerceptionGrant] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=2)
    )
    current_turn_id: str | None = None
    turn_sequence: int = 0
    scene_id: str | None = None

    def presence_valid(self, now: datetime | None = None) -> bool:
        """在场证据是否仍然有效。

        离线（``PresenceKind.offline``）一律无效：ling 不能继续声称看见或听见用户。
        """
        now = now or datetime.now(UTC)
        if self.presence is PresenceKind.offline:
            return False
        if self.state not in {SessionState.active, SessionState.paused}:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > now

    def perception_active(self, capability: str, now: datetime | None = None) -> bool:
        grant = self.grants.get(capability)
        if grant is None:
            return False
        if not self.presence_valid(now):
            return False
        return grant.active(now)


class EmbodimentError(Exception):
    pass


class EmbodimentGateway:
    """SessionGateway 的协议层实现（内存态；W3 联调时接持久化与实时传输）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, EmbodimentSession] = {}
        self._action_receipts: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------- 会话

    def start_session(
        self,
        *,
        subject_id: str,
        device_id: str,
        presence: PresenceKind,
        requested: list[str] | None = None,
        ttl_minutes: int = 120,
    ) -> EmbodimentSession:
        """开启会话。``requested`` 里的感知能力**默认不授予**，需要用户逐项确认。"""
        requested = requested or []
        unknown = set(requested) - PERCEPTION_CAPABILITIES
        if unknown:
            raise EmbodimentError(f"未知感知能力: {sorted(unknown)}")
        now = datetime.now(UTC)
        session = EmbodimentSession(
            session_id=new_id("esess"),
            subject_id=subject_id,
            device_id=device_id,
            presence=presence,
            state=SessionState.active,
            created_at=now,
            expires_at=now + timedelta(minutes=max(1, ttl_minutes)),
        )
        for capability in PERCEPTION_CAPABILITIES:
            session.grants[capability] = PerceptionGrant(capability=capability, granted=False)
        self._sessions[session.session_id] = session
        return session

    def grant_perception(
        self, session_id: str, capability: str, *, ttl_minutes: int = 30
    ) -> EmbodimentSession:
        session = self._require(session_id)
        if capability not in PERCEPTION_CAPABILITIES:
            raise EmbodimentError(f"未知感知能力: {capability}")
        session.grants[capability] = PerceptionGrant(
            capability=capability,
            granted=True,
            expires_at=datetime.now(UTC) + timedelta(minutes=max(1, ttl_minutes)),
        )
        return session

    def revoke_perception(self, session_id: str, capability: str) -> EmbodimentSession:
        session = self._require(session_id)
        grant = session.grants.get(capability)
        if grant is not None:
            grant.granted = False
            grant.expires_at = None
        return session

    def set_presence(self, session_id: str, presence: PresenceKind) -> EmbodimentSession:
        session = self._require(session_id)
        session.presence = presence
        if presence is PresenceKind.offline:
            # 离线：在场证据立即失效，感知全部作废
            for grant in session.grants.values():
                grant.granted = False
            session.state = SessionState.paused
        return session

    def end_session(self, session_id: str) -> EmbodimentSession:
        session = self._require(session_id)
        session.state = SessionState.ended
        for grant in session.grants.values():
            grant.granted = False
        return session

    def get_session(self, session_id: str) -> EmbodimentSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if not session.presence_valid() and session.state in {
            SessionState.active,
            SessionState.paused,
        }:
            session.state = SessionState.expired
        return session

    def _require(self, session_id: str) -> EmbodimentSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise EmbodimentError(f"会话不存在: {session_id}")
        return session

    # ---------------------------------------------------------- 轮次

    def begin_turn(self, session_id: str) -> tuple[str, int]:
        """开始一轮。返回 (turn_id, sequence)。插话时旧轮取消。"""
        session = self._require(session_id)
        if not session.presence_valid():
            raise EmbodimentError("在场证据已失效，不能开始新一轮")
        session.turn_sequence += 1
        session.current_turn_id = new_id("eturn")
        return session.current_turn_id, session.turn_sequence

    def interrupt_turn(self, session_id: str, turn_id: str) -> bool:
        """用户插话：取消旧轮。返回是否确实取消了当前轮。"""
        session = self._require(session_id)
        if session.current_turn_id != turn_id:
            return False
        session.current_turn_id = None
        return True

    def accept_audio(
        self, *, session_id: str, turn_id: str, sequence: int, is_final: bool
    ) -> bool:
        """接受音频片段。迟到或非当前轮的音频一律丢弃。"""
        session = self._require(session_id)
        if session.current_turn_id != turn_id:
            return False
        if not session.perception_active("microphone"):
            return False
        return is_final or sequence >= 0

    # ---------------------------------------------------------- 视觉/空间

    def accept_spatial_observation(
        self, *, session_id: str, semantics: dict[str, Any], ttl_seconds: int = 120
    ) -> dict[str, Any]:
        """只保存必要的低敏场景语义 + 过期时间。未授权 spatial 时拒绝。"""
        session = self._require(session_id)
        if not session.perception_active("spatial"):
            raise EmbodimentError("未授权空间感知")
        # 白名单字段：只允许粗粒度语义，禁止原始点云/图像
        allowed = {"room_kind", "user_near", "user_distance_bucket", "light_level"}
        filtered = {k: v for k, v in semantics.items() if k in allowed}
        return {
            "observation_id": new_id("spobs"),
            "session_id": session_id,
            "semantics": filtered,
            "observed_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
        }

    # ---------------------------------------------------------- 动作与回执

    def avatar_intent(self, *, session_id: str, action: str, intensity: float = 1.0) -> dict[str, Any]:
        """生成动作意图。只允许白名单动作。"""
        session = self._require(session_id)
        if action not in ALLOWED_AVATAR_ACTIONS:
            raise EmbodimentError(f"动作不在白名单: {action}")
        if not session.presence_valid():
            raise EmbodimentError("在场证据已失效，不能下发动作")
        return {
            "intent_id": new_id("avint"),
            "session_id": session_id,
            "action": action,
            "intensity": max(0.0, min(1.0, intensity)),
            "issued_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
        }

    def record_action_result(
        self, *, intent_id: str, executed: bool, detail: str | None = None
    ) -> dict[str, Any]:
        """记录客户端真实执行结果。没执行就不能说做过。"""
        receipt = {
            "intent_id": intent_id,
            "executed": executed,
            "detail": detail,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        self._action_receipts[intent_id] = receipt
        return receipt

    def action_receipt(self, intent_id: str) -> dict[str, Any] | None:
        return self._action_receipts.get(intent_id)

    def record_playback(
        self, *, session_id: str, turn_id: str, played_ms: int, completed: bool
    ) -> dict[str, Any]:
        session = self._require(session_id)
        return {
            "playback_id": new_id("pb"),
            "session_id": session_id,
            "turn_id": turn_id,
            "played_ms": max(0, played_ms),
            "completed": completed,
            "observed_at": datetime.now(UTC).isoformat(),
            "presence": session.presence.value,
        }


__all__ = [
    "ALLOWED_AVATAR_ACTIONS",
    "PERCEPTION_CAPABILITIES",
    "EmbodimentError",
    "EmbodimentGateway",
    "EmbodimentSession",
    "PerceptionGrant",
    "PresenceKind",
    "SessionState",
]
