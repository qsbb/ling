"""Consent 域：授权范围、有效期、撤权与受众规则。

唯一写入方。硬规则（设计文档 §4.2）：
- 可见范围是明确集合，不是三级继承。
- 跨端交接只允许"同一 subject 的已绑定私聊 → 已授权私聊/手机"。
- 私聊内容进入群聊需要**当次**明确同意，且只对当前输出有效。
- 撤权后派生投影（短记忆、胶囊、索引）必须失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import AudienceRule, Visibility
from ling.infra.models import ConsentRule, utcnow
from ling.infra.security import new_id

# 已知授权范围。新增范围必须先在这里登记，避免"随手加一个 scope"绕过审核。
KNOWN_SCOPES = {
    "memory.cross_device",  # 同人私聊跨端召回
    "camera",  # 相机（默认关闭）
    "location",  # 位置（默认关闭）
    "public_publish",  # 公开发布（默认关闭）
    "proactive_contact",  # 主动联系
    "media_upload",  # 上传媒体
    # 入站媒体（用户发来的图片/音频）能否被描述。默认关闭：
    # "能看图"和"能把你发的图交给模型描述"是两件事，后者要单独授权。
    "media.inbound",
    # 设备/真实世界动作（默认关闭）。ACTION_REQUIREMENTS 里
    # device.action 与 tool.irreversible 都要求它，之前无法被授予。
    "device",
}


@dataclass(frozen=True)
class ConsentView:
    scope: str
    granted: bool
    revision: int
    valid_until: datetime | None
    note: str | None


class ConsentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, subject_id: str, scope: str) -> ConsentView:
        row = (
            await self._session.execute(
                select(ConsentRule).where(
                    ConsentRule.subject_id == subject_id, ConsentRule.scope == scope
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # 默认拒绝（尤其是相机/位置/公开发布）
            return ConsentView(scope=scope, granted=False, revision=0, valid_until=None, note=None)
        return self._view(row)

    async def grant(
        self,
        *,
        subject_id: str,
        scope: str,
        valid_until: datetime | None = None,
        note: str | None = None,
    ) -> ConsentView:
        if scope not in KNOWN_SCOPES:
            raise ValueError(f"未登记的授权范围: {scope}")
        if valid_until is not None and valid_until.tzinfo is None:
            raise ValueError("授权有效期必须带时区")
        row = (
            await self._session.execute(
                select(ConsentRule).where(
                    ConsentRule.subject_id == subject_id, ConsentRule.scope == scope
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = ConsentRule(
                id=new_id("consent"),
                subject_id=subject_id,
                scope=scope,
                granted=True,
                revision=1,
                valid_until=valid_until,
                note=note,
            )
            self._session.add(row)
        else:
            row.granted = True
            row.revision = int(row.revision or 1) + 1
            row.valid_until = valid_until
            row.note = note
            row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def revoke(self, *, subject_id: str, scope: str, note: str | None = None) -> ConsentView:
        row = (
            await self._session.execute(
                select(ConsentRule).where(
                    ConsentRule.subject_id == subject_id, ConsentRule.scope == scope
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return ConsentView(
                scope=scope, granted=False, revision=0, valid_until=None, note=note
            )
        row.granted = False
        row.revision = int(row.revision or 1) + 1
        row.note = note
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def is_active(self, subject_id: str, scope: str, *, now: datetime | None = None) -> bool:
        view = await self.get(subject_id, scope)
        if not view.granted:
            return False
        if view.valid_until is None:
            return True
        now = now or datetime.now(UTC)
        return view.valid_until > now

    async def granted_scopes(self, subject_id: str) -> set[str]:
        """当前仍然有效的授权范围（供权限硬门填充上下文）。

        只返回 granted 且未过期的；默认拒绝——没在这里出现的范围就是没授权。
        """
        now = datetime.now(UTC)
        return {
            view.scope
            for view in await self.list_all(subject_id)
            if view.granted and (view.valid_until is None or view.valid_until > now)
        }

    async def list_all(self, subject_id: str) -> list[ConsentView]:
        rows = list(
            (
                await self._session.execute(
                    select(ConsentRule).where(ConsentRule.subject_id == subject_id)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    # ---------------------------------------------------------- 受众

    def audience_for(self, visibility: Visibility, *, owner_subject_id: str | None = None) -> AudienceRule:
        """把可见范围翻译成受众规则。public 不授权读取任何私聊。"""
        cross_window = visibility in {Visibility.subject_private}
        return AudienceRule(
            visibility=visibility,
            owner_subject_id=owner_subject_id,
            allow_cross_window=cross_window,
            requires_explicit_consent=visibility in {
                Visibility.group_scope,
                Visibility.public,
                Visibility.admin_only,
            },
            note=None,
        )

    def allowed_visibilities(self, *, is_group: bool, cross_device_granted: bool) -> set[Visibility]:
        """本轮可读取的记忆可见范围集合（委托到唯一权威实现）。

        保留这个方法是为了让 consent 域的调用方不必直接依赖 kernel，
        但规则本身必须来自 kernel.audience.readable_visibilities。
        """
        from ling.kernel.audience import readable_visibilities

        return readable_visibilities(
            is_group=is_group,
            cross_device_granted=cross_device_granted,
        )

    @staticmethod
    def _view(row: ConsentRule) -> ConsentView:
        valid = row.valid_until
        if valid is not None and valid.tzinfo is None:
            valid = valid.replace(tzinfo=UTC)
        return ConsentView(
            scope=row.scope,
            granted=bool(row.granted),
            revision=int(row.revision or 1),
            valid_until=valid,
            note=row.note,
        )
