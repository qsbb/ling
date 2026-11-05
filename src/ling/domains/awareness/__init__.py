"""Awareness 域：会话隔离模式与跨会话意识（唯一写入方，ADR-0004）。"""

from ling.domains.awareness.service import (
    FORBIDDEN_CROSS_WINDOW,
    Audience,
    AwarenessKind,
    AwarenessService,
    AwarenessView,
    IsolationMode,
    PolicyView,
)

__all__ = [
    "FORBIDDEN_CROSS_WINDOW",
    "Audience",
    "AwarenessKind",
    "AwarenessService",
    "AwarenessView",
    "IsolationMode",
    "PolicyView",
]
