"""Publishing：需要专门授权的公开输出（W2：已实现未测试，默认关闭）。

硬规则（设计文档 §3 E / §9）：
- 公开发布是**高影响、不可逆**动作：必须专门授权 + 二次确认 + 审计。
- 没有真实平台回执就不能标"已发布"。
- 默认关闭（``LING_ALLOW_PUBLIC_PUBLISH=0``）。

授权分两层，**不可互相替代**（见 ``docs/SETTINGS-TAXONOMY.md`` §5.1）：

1. **宿主能力**（``LING_ALLOW_PUBLIC_PUBLISH``）：这台机器/这个部署是否提供
   发布能力。属于启动配置，进程起来就定了。
2. **主体授权**（``ConsentRule(scope="public_publish")``）：这个 subject 是否
   被允许发布。属于业务表，可随时授予、撤销、带有效期。

此前只有第 1 层，且被当作第 2 层用：env=1 就绕过了一切授权记录，
consent 表里授权了又会因为 env=0 发不出去。两层分开后各自只答自己的问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from ling.contracts.v1 import new_id

if TYPE_CHECKING:
    from ling.domains.consent.service import ConsentService

PUBLISH_SCOPE = "public_publish"


class PublishState(StrEnum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    published = "published"
    failed = "failed"
    rejected = "rejected"


class PublishError(Exception):
    pass


@dataclass
class PublishDraft:
    draft_id: str
    subject_id: str
    channel: str
    title: str
    body: str
    state: PublishState = PublishState.draft
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None
    published_at: datetime | None = None
    platform_ref: str | None = None
    rejection_reason: str | None = None


class PublishingService:
    """公开输出审批流。默认关闭；没有授权一律拒绝。

    ``capability_enabled`` 只回答"这台部署是否提供发布能力"（来自启动配置）。
    主体是否被授权要查 ``ConsentRule``，**不能**用这个布尔代替。
    """

    def __init__(self, *, capability_enabled: bool = False) -> None:
        self._capability_enabled = capability_enabled
        self._drafts: dict[str, PublishDraft] = {}

    @property
    def capability_enabled(self) -> bool:
        return self._capability_enabled

    @property
    def enabled(self) -> bool:
        """向后兼容：仅表示宿主能力，不代表某个主体已获授权。"""
        return self._capability_enabled

    async def create_draft(
        self,
        *,
        subject_id: str,
        channel: str,
        title: str,
        body: str,
        consent: ConsentService | None,
    ) -> PublishDraft:
        if not self._capability_enabled:
            raise PublishError(
                "宿主未启用公开发布能力（启动配置 LING_ALLOW_PUBLIC_PUBLISH=0）"
            )
        if consent is None or not await consent.is_active(subject_id, PUBLISH_SCOPE):
            raise PublishError(
                f"需要先授予公开发布权限（授权记录 scope={PUBLISH_SCOPE}），"
                "授予后仍需二次确认与真实平台回执"
            )
        draft = PublishDraft(
            draft_id=new_id("pub"),
            subject_id=subject_id,
            channel=channel,
            title=title[:200],
            body=body[:20000],
        )
        self._drafts[draft.draft_id] = draft
        return draft

    def submit_for_approval(self, draft_id: str) -> PublishDraft:
        draft = self._require(draft_id)
        if draft.state is not PublishState.draft:
            raise PublishError(f"只有草稿可以提交审批（当前 {draft.state.value}）")
        draft.state = PublishState.pending_approval
        return draft

    def approve(self, draft_id: str) -> PublishDraft:
        draft = self._require(draft_id)
        if draft.state is not PublishState.pending_approval:
            raise PublishError("只有待审批的草稿可以批准")
        draft.state = PublishState.approved
        draft.approved_at = datetime.now(UTC)
        return draft

    def reject(self, draft_id: str, *, reason: str) -> PublishDraft:
        draft = self._require(draft_id)
        draft.state = PublishState.rejected
        draft.rejection_reason = reason[:500]
        return draft

    def mark_published(self, draft_id: str, *, platform_ref: str) -> PublishDraft:
        """只有拿到真实平台引用才能标已发布。"""
        draft = self._require(draft_id)
        if draft.state is not PublishState.approved:
            raise PublishError("未批准的草稿不能发布")
        if not platform_ref.strip():
            raise PublishError("缺少平台回执，不能标记为已发布")
        draft.state = PublishState.published
        draft.published_at = datetime.now(UTC)
        draft.platform_ref = platform_ref
        return draft

    def get(self, draft_id: str) -> PublishDraft | None:
        return self._drafts.get(draft_id)

    def _require(self, draft_id: str) -> PublishDraft:
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise KeyError(f"发布草稿不存在: {draft_id}")
        return draft


__all__ = ["PublishDraft", "PublishError", "PublishState", "PublishingService"]
