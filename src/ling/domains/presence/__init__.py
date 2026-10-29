"""Presence 域：角色此刻的生活状态与当前场景（唯一写入方）。"""

from ling.domains.presence.service import (
    DEFAULT_SCENE_TTL,
    SCENE_STATES,
    PresenceService,
    PresenceView,
    SceneView,
)

__all__ = [
    "DEFAULT_SCENE_TTL",
    "SCENE_STATES",
    "PresenceService",
    "PresenceView",
    "SceneView",
]
