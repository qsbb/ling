"""QQ 渠道管理设置与停用开关（W1：已开发，未测试）。

管理台通过鉴权后的命令 API 操作；不允许直接改表。
未验证的能力默认关闭：``proactive_enabled`` 只能由管理员显式打开，
并且必须记录账号权限探测结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import utcnow
from ling.infra.security import new_id


@dataclass
class QQInstanceSettings:
    """一个 QQ 连接器实例的可管理设置。"""

    instance_id: str
    app_id: str = ""
    has_secret: bool = False
    secret_last4: str = ""
    enabled: bool = False
    proactive_enabled: bool = False
    allowed_groups: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)
    # 账号权限探测结果：只有探测通过才允许打开对应能力
    probe_result: dict[str, object] = field(default_factory=dict)
    probed_at: datetime | None = None
    note: str | None = None


class QQSettingsService:
    """把 QQ 实例设置存进统一配置修订的 payload（不新建第二套配置层）。

    这里只提供纯逻辑与校验；持久化由 ConfigService 的 provider ``extra`` 承载，
    避免 QQ 连接器拥有独立配置表。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def describe(self, settings: QQInstanceSettings) -> dict[str, object]:
        return {
            "instance_id": settings.instance_id,
            "app_id": settings.app_id,
            "has_secret": settings.has_secret,
            "secret_last4": settings.secret_last4,
            "enabled": settings.enabled,
            "proactive_enabled": settings.proactive_enabled,
            "allowed_groups": list(settings.allowed_groups),
            "allowed_users": list(settings.allowed_users),
            "probe_result": dict(settings.probe_result),
            "probed_at": settings.probed_at.isoformat() if settings.probed_at else None,
            "note": settings.note,
            "warnings": self.warnings(settings),
        }

    @staticmethod
    def warnings(settings: QQInstanceSettings) -> list[str]:
        out: list[str] = []
        if settings.proactive_enabled and not settings.probe_result.get("proactive"):
            out.append(
                "主动推送已打开，但没有任何账号权限探测通过；"
                "QQ 官方自 2025-04-21 起不再提供主动推送能力，请先探测再启用"
            )
        if settings.enabled and not settings.has_secret:
            out.append("连接器已启用但没有保存 AppSecret，入站签名校验会全部失败")
        if settings.enabled and not settings.probe_result:
            out.append("从未做过能力探测：私聊/群聊/主动推送能力均未验证")
        return out

    def can_enable_proactive(self, settings: QQInstanceSettings) -> bool:
        """确定性硬门：没有探测通过的主动能力就不允许打开。"""
        return bool(settings.probe_result.get("proactive"))

    async def record_probe(
        self, settings: QQInstanceSettings, result: dict[str, object]
    ) -> QQInstanceSettings:
        settings.probe_result = dict(result)
        settings.probed_at = utcnow()
        if not result.get("proactive"):
            settings.proactive_enabled = False
        return settings


async def list_qq_instances(session: AsyncSession) -> list[str]:
    """列出已登记的 QQ 连接器实例（从配置修订里读，不读渠道私有表）。"""
    from ling.infra.models import ConfigRevision

    rows = list(
        (
            await session.execute(
                select(ConfigRevision).where(ConfigRevision.state == "published")
            )
        ).scalars()
    )
    instances: set[str] = set()
    for row in rows:
        for provider in (row.payload or {}).get("providers", []):
            pid = provider.get("provider_id", "")
            if pid.startswith("qq"):
                instances.add(pid)
    return sorted(instances)


def new_instance_id() -> str:
    return new_id("qq")


__all__ = [
    "QQInstanceSettings",
    "QQSettingsService",
    "list_qq_instances",
    "new_instance_id",
]
