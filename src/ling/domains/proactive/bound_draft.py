"""Generate proactive drafts only after selecting and binding a target window."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select

from ling.contracts.v1 import Modality
from ling.domains.proactive.binding import (
    CLAIM_STALE_SECONDS,
    begin_generation,
    claim_draft,
    finish_generation,
    release_preparing,
    target_fingerprint,
)
from ling.domains.proactive.draft import draft_for
from ling.infra.db import session_scope
from ling.infra.models import ProactiveDraftBinding, ProactiveOpportunity, Window, utcnow
from ling.settings.window_policy import FixedConfigReader, runtime_window_snapshot

if TYPE_CHECKING:
    from ling.ports.channel import ChannelConnector
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import RuntimeWindowSnapshot

logger = logging.getLogger(__name__)


async def _target(
    *, actor_id: str, subject_id: str,
    connector_for_window: Callable[[Window], Awaitable[ChannelConnector | None]],
) -> tuple[Window, bool] | None:
    async with session_scope() as session:
        windows = list((await session.scalars(
            select(Window).where(
                Window.actor_id == actor_id,
                Window.subject_id == subject_id,
                Window.visibility == "subject_private",
                Window.archived.is_(False),
                Window.channel.in_(("onebot", "qqbot", "web")),
            ).order_by(Window.last_message_at.desc().nulls_last(), Window.id)
        )).all())
    for window in windows:
        if window.channel == "web":
            continue
        if not window.platform_reference:
            continue
        connector = await connector_for_window(window)
        if connector is None:
            continue
        capabilities = connector.capabilities()
        if capabilities.enabled and capabilities.supports_private and capabilities.supports_proactive:
            return window, bool(capabilities.media_supported(Modality.audio))
    web = next((window for window in windows if window.channel == "web"), None)
    return (web, True) if web is not None else None


async def draft_bound_pending(
    *,
    actor_id: str,
    router: object,
    config: ConfigService,
    connector_for_window: Callable[[Window], Awaitable[ChannelConnector | None]],
    voice_help_for: Callable[[Window, RuntimeWindowSnapshot, bool], Awaitable[tuple[str, bool]]],
    interpret_draft: Callable[[str], tuple[str, str, bool]],
    limit: int = 5,
) -> dict[str, int]:
    """Use short claims; never hold a database transaction during model generation."""
    now = utcnow()
    async with session_scope() as session:
        ids = list((await session.scalars(
            select(ProactiveOpportunity.id)
            .outerjoin(ProactiveDraftBinding, ProactiveDraftBinding.opportunity_id == ProactiveOpportunity.id)
            .where(
                ProactiveOpportunity.actor_id == actor_id,
                ProactiveOpportunity.state.in_(("candidate", "drafting")),
                ProactiveOpportunity.draft_text.is_(None),
                ProactiveOpportunity.expires_at > now,
                or_(
                    ProactiveDraftBinding.opportunity_id.is_(None),
                    and_(
                        ProactiveDraftBinding.state.in_(("preparing", "generating")),
                        ProactiveDraftBinding.updated_at <= now - timedelta(seconds=CLAIM_STALE_SECONDS),
                    ),
                ),
            )
            .order_by(ProactiveOpportunity.created_at, ProactiveOpportunity.id)
            .limit(limit)
        )).all())
    drafted = failed = 0
    for opportunity_id in ids:
        async with session_scope() as session:
            claim = await claim_draft(session, opportunity_id=opportunity_id, actor_id=actor_id)
        if claim is None:
            continue
        started = False
        try:
            target = await _target(actor_id=actor_id, subject_id=claim.subject_id,
                                   connector_for_window=connector_for_window)
            if target is None:
                async with session_scope() as session:
                    await release_preparing(session, claim=claim)
                continue
            window, audio_supported = target
            selected_target = target_fingerprint(window)
            snapshot = await runtime_window_snapshot(config, window.id)
            voice_hint, voice_allowed = await voice_help_for(window, snapshot, audio_supported)
            async with session_scope() as session:
                started = await begin_generation(
                    session, claim=claim, snapshot=snapshot, actor_id=actor_id,
                    target_window_id=window.id, expected_target=selected_target,
                )
                opportunity = await session.get(ProactiveOpportunity, opportunity_id)
            if not started or opportunity is None:
                async with session_scope() as session:
                    await release_preparing(session, claim=claim)
                continue

            async def hint(
                _opportunity: ProactiveOpportunity, _effective: object, text: str = voice_hint,
            ) -> str:
                return text

            persona = snapshot.persona.view
            text = await draft_for(
                None, opportunity=opportunity, router=router,
                config_reader=FixedConfigReader(snapshot.config),
                persona=str(persona.system_prompt or "") if persona is not None else "",
                voice_help_for=hint,
            )
            content, modality, rejected = interpret_draft(text) if text is not None else (None, "text", False)
            rejected = rejected or (modality == "voice" and not voice_allowed)
            async with session_scope() as session:
                result = await finish_generation(
                    session, claim=claim, text=content, modality=modality, rejected=rejected,
                )
            drafted += result == "ready"
            failed += result != "ready"
        except asyncio.CancelledError:
            async with session_scope() as session:
                if started:
                    await finish_generation(session, claim=claim, text=None)
                else:
                    await release_preparing(session, claim=claim)
            raise
        except Exception as exc:  # noqa: BLE001 - one draft must not block other candidates
            logger.warning("主动草稿暂不可用 opportunity=%s type=%s", opportunity_id, type(exc).__name__)
            async with session_scope() as session:
                if started:
                    await finish_generation(session, claim=claim, text=None)
                else:
                    await release_preparing(session, claim=claim)
            failed += 1
    return {"drafted": drafted, "failed": failed, "considered": len(ids)}
