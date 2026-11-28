"""Authorize a model's current-fact lookup in the active conversation."""

from typing import Any

from ling.domains.memory.service import MemoryService, render_evidence
from ling.infra.db import session_scope
from ling.infra.models import Window
from ling.kernel.audience import AudienceSnapshot, visibility_set
from ling.settings.service import published_memory_policy


async def recall_current_fact(
    args: dict[str, Any], *, audience: AudienceSnapshot, context_sources: set[str] | None = None,
) -> str:
    slot = args.get("slot")
    if slot != "works_at" and slot != "lives_in":
        raise ValueError("事实类型应为 works_at 或 lives_in。")
    empty = "没有查到可用的当前记录。"
    async with session_scope() as session:
        window = await session.get(Window, audience.window_id)
        if window is None or window.archived or window.visibility != audience.window_visibility:
            return empty
        if not audience.is_group and window.subject_id != audience.subject_id:
            return empty
        current = await AudienceSnapshot.freeze(
            session, subject_id=audience.subject_id,
            window_id=window.id, window_visibility=window.visibility,
            whole_group_source_ids=audience.whole_group_source_ids,
        )
        policy = await published_memory_policy(session)
        if not current.allows_long_term_memory() or policy.max_injected_memories <= 0:
            return empty
        result = await MemoryService(session, policy=policy).recall(
            subject_id=audience.subject_id, query=slot, fact_slot=slot, limit=1,
            visibility_allow=visibility_set(audience) & visibility_set(current),
            trace_window_id=window.id, audience=current,
        )
    if result.strategy == "fact_conflict":
        return "记录中有相互冲突的本人陈述，请询问哪一条现在有效。"
    if context_sources is not None:
        context_sources.update(
            ref for hit in result.hits for ref in hit.source_event_ids if ref.startswith("message:")
        )
    return render_evidence(result.hits) or empty


async def recall_shared_arrangement(
    args: dict[str, Any], *, audience: AudienceSnapshot, context_sources: set[str] | None = None,
) -> str:
    """Search only the original messages eligible for this whole group reply."""
    from sqlalchemy.exc import SQLAlchemyError

    from ling.domains.memory.shared_events import recall_arrangements

    empty = "未找到本群可用的共同约定。"
    query = args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 160:
        raise ValueError("请提供要补查的约定或问题。")
    if not audience.is_group or not audience.allows_long_term_memory() or not audience.whole_group_source_ids:
        return empty
    try:
        async with session_scope() as session:
            window = await session.get(Window, audience.window_id)
            if window is None or window.archived or window.visibility != audience.window_visibility:
                return empty
            current = await AudienceSnapshot.freeze(
                session, subject_id=audience.subject_id,
                window_id=window.id, window_visibility=window.visibility,
            )
            policy = await published_memory_policy(session)
            if (
                policy.shared_events != "enabled" or not current.allows_long_term_memory()
                or policy.max_injected_memories <= 0
            ):
                return empty
            hits = await recall_arrangements(
                session, window_id=window.id, query=query,
                whole_group_source_ids=audience.whole_group_source_ids,
                retention_days=policy.max_retention_days, limit=1,
            )
    except SQLAlchemyError:
        return empty
    if context_sources is not None:
        context_sources.update(
            f"shared:{ref.removeprefix('message:')}"
            for hit in hits for ref in hit.source_event_ids if ref.startswith("message:")
        )
    return render_evidence(hits) or empty


async def recall_experience(
    args: dict[str, Any], *, audience: AudienceSnapshot, current_turn_id: str | None = None,
) -> str:
    """Look up a topic or time within the authenticated speaker's current window."""
    from datetime import UTC, datetime, timedelta

    from ling.domains.memory.experience import (
        asks_for_earliest,
        render_experience,
        search_facts,
        search_originals,
    )

    query = args.get("query", "")
    mode = args.get("mode", "originals")
    if not isinstance(query, str) or not query.strip() or len(query) > 160:
        raise ValueError("请输入要核对的话题。")
    if mode not in {"originals", "past", "current"}:
        raise ValueError("选择原话、过去的事实或当前事实。")
    at = None
    if raw_date := args.get("date"):
        if not isinstance(raw_date, str) or len(raw_date) != 10:
            raise ValueError("日期格式为 YYYY-MM-DD。")
        at = datetime.fromisoformat(raw_date).replace(tzinfo=UTC)
    if not audience.allows_long_term_memory():
        return "当前窗口没有可用的原话证据。"
    async with session_scope() as session:
        window = await session.get(Window, audience.window_id)
        if window is None or window.archived or window.visibility != audience.window_visibility:
            return "当前窗口没有可用的原话证据。"
        current = await AudienceSnapshot.freeze(
            session, subject_id=audience.subject_id, window_id=window.id,
            window_visibility=window.visibility, whole_group_source_ids=audience.whole_group_source_ids,
        )
        policy = await published_memory_policy(session)
        if not current.allows_long_term_memory() or policy.max_injected_memories <= 0:
            return "当前窗口没有可用的原话证据。"
        if mode == "originals":
            result = await search_originals(
                session, window=window, subject_id=audience.subject_id, query=query,
                retention_days=policy.max_retention_days, group_source_ids=current.whole_group_source_ids,
                since=at, before=at + timedelta(days=1) if at else None,
                earliest=asks_for_earliest(query), current_turn_id=current_turn_id,
            )
        else:
            slot = args.get("slot")
            if slot not in {"likes", "works_at", "lives_in"}:
                raise ValueError("请指定要回查的事实类型。")
            result = await search_facts(
                session, window=window, subject_id=audience.subject_id, slot=slot, mode=mode,
                retention_days=policy.max_retention_days, group_source_ids=current.whole_group_source_ids,
                at=at,
            )
    return render_experience(result)
