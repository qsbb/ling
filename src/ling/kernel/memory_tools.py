"""Authorize a model's current-fact lookup in the active conversation."""

from typing import Any

from ling.domains.memory.service import MemoryService, render_evidence
from ling.infra.db import session_scope
from ling.infra.models import Window
from ling.kernel.audience import AudienceSnapshot, visibility_set
from ling.settings.service import published_memory_policy


async def recall_current_fact(args: dict[str, Any], *, audience: AudienceSnapshot) -> str:
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
    return render_evidence(result.hits) or empty
