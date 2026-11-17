"""Short-transaction claims and settings bindings for proactive drafts."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from ling.infra.models import ProactiveDraftBinding, ProactiveOpportunity, Window, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ling.settings.window_policy import RuntimeWindowSnapshot

CLAIM_STALE_SECONDS = 600
_DIALOGUE_FIELDS = (
    "private_may_stay_silent", "group_mention_requires_reply",
    "group_attention_deferred", "group_turn_budget", "rest_may_skip",
)


@dataclass(frozen=True)
class DraftClaim:
    opportunity_id: str
    token: str
    subject_id: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def target_fingerprint(window: Window) -> str:
    """Bind a draft to the original route without retaining the platform address."""
    route = "" if window.channel == "web" else window.platform_reference
    value = [window.id, window.actor_id, window.subject_id, window.visibility, window.channel,
             window.origin_status, window.origin_scope, window.origin_instance_id, route]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def snapshot_metadata(snapshot: RuntimeWindowSnapshot) -> dict[str, Any]:
    """只记录草稿生效配置的来源和值，不记录人格正文或密钥。"""
    selected = snapshot.resolved.persona_id.value
    persona = snapshot.persona.view
    if persona is not None and persona.persona_id != selected:
        raise ValueError("人格快照与已解析的设置不一致")
    if persona is not None:
        status = "resolved"
    elif selected is None:
        status = "none"
    else:
        status = "unavailable"
    fields = {}
    for name in ("persona_id", *_DIALOGUE_FIELDS):
        field = getattr(snapshot.resolved, name)
        fields[name] = {"value": field.value, "source": field.source}
    return {
        "global_revision": snapshot.global_revision,
        "instance_revision": snapshot.instance_revision,
        "window_revision": snapshot.session_revision,
        "origin": snapshot.origin_status,
        "instance_scope": snapshot.instance_scope,
        "instance_id": snapshot.instance_id,
        "persona_id": selected,
        "persona_revision": snapshot.persona_revision,
        "persona_status": status,
        "voice_willingness": snapshot.config.dialogue.voice_willingness,
        "fields": fields,
    }


def equivalent_settings(recorded: dict[str, Any], current: dict[str, Any]) -> bool:
    """Conservative fallback: unavailable persona data is never assumed equal."""
    if recorded.get("persona_status") not in {"none", "resolved"}:
        return False
    if any(recorded.get(key) != current.get(key) for key in (
        "persona_status", "persona_id", "persona_revision", "voice_willingness",
    )):
        return False
    old_fields, new_fields = recorded.get("fields"), current.get("fields")
    if not isinstance(old_fields, dict) or not isinstance(new_fields, dict):
        return False
    for key in _DIALOGUE_FIELDS:
        old, new = old_fields.get(key), new_fields.get(key)
        if not isinstance(old, dict) or not isinstance(new, dict) or "value" not in old or "value" not in new:
            return False
        if type(old["value"]) is not type(new["value"]) or old["value"] != new["value"]:
            return False
    return True


async def claim_draft(
    session: AsyncSession, *, opportunity_id: str, actor_id: str, now: datetime | None = None,
) -> DraftClaim | None:
    """Claim before any model call; caller commits this short transaction."""
    moment = now or utcnow()
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == opportunity_id).with_for_update()
    )
    if (
        opportunity is None or opportunity.actor_id != actor_id or opportunity.state not in {"candidate", "drafting"}
        or opportunity.draft_text is not None or _aware(opportunity.expires_at) <= _aware(moment)
    ):
        return None
    binding = await session.get(ProactiveDraftBinding, opportunity_id, populate_existing=True)
    if binding is not None:
        stale = _aware(binding.updated_at) <= _aware(moment) - timedelta(seconds=CLAIM_STALE_SECONDS)
        if not stale or binding.state in {"ready", "uncertain", "cancelled"}:
            return None
        if binding.state == "generating":
            binding.state = "uncertain"
            binding.updated_at = moment
            await session.flush()
            return None
        if binding.state != "preparing":
            return None
    token = secrets.token_hex(16)
    if binding is None:
        if opportunity.state != "candidate":
            return None
        binding = ProactiveDraftBinding(
            opportunity_id=opportunity_id, claim_token=token, state="preparing", updated_at=moment,
        )
        session.add(binding)
    else:
        binding.claim_token = token
        binding.updated_at = moment
        binding.target_window_id = None
        binding.snapshot = None
    opportunity.state = "drafting"
    await session.flush()
    return DraftClaim(opportunity_id=opportunity_id, token=token, subject_id=opportunity.subject_id)


async def begin_generation(
    session: AsyncSession, *, claim: DraftClaim, snapshot: RuntimeWindowSnapshot,
    actor_id: str, target_window_id: str, expected_target: str, now: datetime | None = None,
) -> bool:
    """Persist the target and uncertainty boundary before invoking the model."""
    moment = now or utcnow()
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == claim.opportunity_id).with_for_update()
    )
    binding = await session.get(ProactiveDraftBinding, claim.opportunity_id, populate_existing=True)
    target = await session.scalar(
        select(Window).where(Window.id == target_window_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        opportunity is None or opportunity.actor_id != actor_id or opportunity.subject_id != claim.subject_id
        or opportunity.state != "drafting" or opportunity.draft_text is not None
        or _aware(opportunity.expires_at) <= _aware(moment) or binding is None
        or binding.claim_token != claim.token or binding.state != "preparing"
        or target is None or target.actor_id != actor_id or target.subject_id != claim.subject_id
        or target.visibility != "subject_private" or target.archived
        or target_fingerprint(target) != expected_target
        or target.origin_status != snapshot.origin_status
        or target.origin_scope != snapshot.instance_scope
        or target.origin_instance_id != snapshot.instance_id
    ):
        return False
    binding.state = "generating"
    binding.target_window_id = target.id
    binding.snapshot = {**snapshot_metadata(snapshot), "target_fingerprint": expected_target}
    binding.updated_at = moment
    await session.flush()
    return True


async def release_preparing(session: AsyncSession, *, claim: DraftClaim) -> None:
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == claim.opportunity_id).with_for_update()
    )
    result = await session.execute(delete(ProactiveDraftBinding).where(
        ProactiveDraftBinding.opportunity_id == claim.opportunity_id,
        ProactiveDraftBinding.claim_token == claim.token,
        ProactiveDraftBinding.state == "preparing",
    ))
    if getattr(result, "rowcount", 0) and opportunity is not None and opportunity.state == "drafting":
        opportunity.state = "candidate"


async def finish_generation(
    session: AsyncSession, *, claim: DraftClaim, text: str | None, modality: str = "text",
    rejected: bool = False, now: datetime | None = None,
) -> str:
    """Commit text and binding together, or retain an uncertain model result."""
    moment = now or utcnow()
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == claim.opportunity_id).with_for_update()
    )
    binding = await session.get(ProactiveDraftBinding, claim.opportunity_id, populate_existing=True)
    if binding is None or binding.claim_token != claim.token or binding.state != "generating":
        return "lost"
    if (opportunity is None or opportunity.state != "drafting" or opportunity.draft_text is not None
            or _aware(opportunity.expires_at) <= _aware(moment)):
        binding.state = "cancelled"
    elif rejected:
        binding.state = "cancelled"
        opportunity.state = "failed"
        opportunity.rejected_reason = "草稿表达控制无效"
    elif text is None or not text.strip() or modality not in {"text", "voice"}:
        binding.state = "uncertain"
    else:
        opportunity.state = "bound_candidate"
        opportunity.draft_text = text[:2000]
        opportunity.output_modality = modality
        # The database guard accepts this transition only while the matching
        # binding is still generating; commit the two writes together.
        await session.flush([opportunity])
        binding.state = "ready"
    binding.updated_at = moment
    await session.flush()
    return binding.state
