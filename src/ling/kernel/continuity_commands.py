"""Explicit chat controls shared with the corresponding management operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin
from ling.domains.memory.experience import pin_message
from ling.domains.memory.forget_source import forget_source
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.service import MemoryService
from ling.domains.relationship.preferences import (
    boundary_from_message,
    preference_scope,
    save_preference,
    set_share_pause,
)
from ling.domains.relationship.service import RelationshipService
from ling.infra.models import MemoryRow, Message, Window, utcnow
from ling.kernel.handoff import HandoffService
from ling.ports.config import EffectiveConfig
from ling.settings.user_preferences import load_user_timezone


@dataclass(frozen=True)
class ContinuityCommand:
    kind: str
    argument: str = ""


def read_command(text: str) -> ContinuityCommand | None:
    plain = text.strip()
    short = plain.rstrip("。！! ")
    if short in {"暂停主动联系", "暂停主动分享", "先别主动联系我", "/暂停"}:
        return ContinuityCommand("pause")
    if short in {"今天别联系我", "今天别主动联系我", "今天先别联系我"}:
        return ContinuityCommand("pause_today")
    if short in {"恢复主动联系", "恢复主动分享", "/恢复"}:
        return ContinuityCommand("resume")
    if short in {"记住这句", "记住这条", "记住这句话", "/记住"}:
        return ContinuityCommand("pin_quote")
    if re.match(r"^(?:记住这件事|帮我记住|记住)\s*[：:]\s*\S", plain) or plain.startswith("/记住 "):
        return ContinuityCommand("pin_text")
    for prefix, kind in (("/更正 ", "correct"), ("/忘记 ", "forget"), ("/续接群 ", "handoff_group")):
        if plain.startswith(prefix):
            return ContinuityCommand(kind, plain.removeprefix(prefix).strip())
    if match := re.fullmatch(
        r"(?:请|帮我)?(?:在\s*)?(\d{4}-\d{2}-\d{2}(?:[ T])\d{2}:\d{2})\s*(?:提醒我|提醒)\s*(.{2,180})",
        plain.removeprefix("/提醒 "),
    ):
        return ContinuityCommand("reminder", f"{match[1].replace(' ', 'T')} {match[2]}")
    if boundary := boundary_from_message(plain):
        return ContinuityCommand("boundary", boundary)
    return None


async def execute_command(
    session: AsyncSession, *,
    command: ContinuityCommand,
    window: Window,
    subject_id: str,
    source: Message,
    quoted_id: str | None,
    config: EffectiveConfig,
) -> str:
    group = window.visibility == "group_scope"
    kind = command.kind
    if group and kind not in {"pin_text", "pin_quote", "boundary"}:
        return "这项设置可以在私聊里操作。"
    if kind in {"pause", "pause_today", "resume"}:
        until = None
        if kind == "pause_today":
            from ling.config import get_settings

            tz_name, _ = await load_user_timezone(
                session, subject_id=subject_id, env=get_settings().user_timezone,
            )
            tz = ZoneInfo(tz_name)
            local = utcnow().astimezone(tz)
            until = datetime.combine(local.date() + timedelta(days=1), time(), tz).astimezone(UTC)
        state = await set_share_pause(session, subject_id, paused=kind != "resume", until=until)
        if state.paused:
            return "今天先不主动分享。约好的提醒仍会处理。" if until else "已暂停主动分享。约好的提醒仍会处理。"
        return "已恢复主动分享。"
    if kind == "boundary":
        try:
            rule = await save_preference(
                session, actor_id=window.actor_id, subject_id=subject_id, scope=preference_scope(window),
                kind="boundary", text=command.argument, state="active", source_message_id=source.id,
            )
        except ValueError as exc:
            return str(exc)
        if source.turn_id:
            await RelationshipService(session, actor_id=window.actor_id).record_event(
                subject_id=subject_id, kind="boundary", origin=Origin.user_statement,
                source_event_ids=[f"message:{source.id}", f"preference:{rule.id}"],
                turn_id=source.turn_id, apply_to_projection=False,
            )
        return "好，我会守住这个话题边界。"
    if kind == "reminder":
        from ling.config import get_settings
        from ling.domains.consent.service import ConsentService
        from ling.domains.goals.service import GoalsService

        when, _, content = command.argument.partition(" ")
        try:
            local_time = datetime.fromisoformat(when)
        except ValueError:
            return "请用 YYYY-MM-DD HH:MM 填写提醒时间。"
        tz_name, _ = await load_user_timezone(
            session, subject_id=subject_id, env=get_settings().user_timezone,
        )
        due_at = local_time.replace(tzinfo=ZoneInfo(tz_name))
        if due_at <= utcnow():
            return "提醒时间已经过去，请选一个未来时间。"
        if window.channel != "web" and not await ConsentService(session).is_active(subject_id, "proactive_contact"):
            return "发送定时提醒需要先在设置中授权主动联系。"
        await GoalsService(session, actor_id=window.actor_id).create_commitment(
            subject_id=subject_id, window_id=window.id, text=content, due_at=due_at,
            timezone=tz_name, user_message_id=source.id,
        )
        return f"已约好在 {due_at:%Y-%m-%d %H:%M} 提醒你。"
    if kind == "handoff_group":
        target_id, _, public_text = command.argument.partition(" ")
        target = await session.get(Window, target_id)
        origin = window.trusted_instance_key
        if target is None and origin is not None and re.fullmatch(r"[1-9][0-9]{0,19}", target_id):
            namespace = window.platform_reference.partition(":private:")[0]
            matches = list(await session.scalars(select(Window).where(
                Window.actor_id == window.actor_id, Window.channel == window.channel,
                Window.origin_scope == origin[0], Window.origin_instance_id == origin[1],
                Window.origin_status == "known", Window.visibility == "group_scope", Window.archived.is_(False),
                Window.connector_window_ref == f"{namespace}:group:{target_id}",
            ).limit(2)))
            target = matches[0] if len(matches) == 1 else None
        if not target_id or not public_text:
            return "请发送「/续接群 群号 要公开的短句」。"
        if target is not None and 2 <= len(public_text.strip()) <= 180 and await HandoffService(
            session, actor_id=window.actor_id,
        ).capture(
            subject_id=subject_id, source_window=window, message=source,
            target_window=target, public_text=public_text.strip(),
        ):
            return "已授权这句在指定群接续，有效时间 30 分钟。"
        return "无法续接。请核对群会话、本人账号和跨端公开授权。"
    if kind in {"pin_text", "pin_quote"}:
        target_message_id = source.id if kind == "pin_text" else quoted_id
        if target_message_id is None:
            return "请引用要记住的消息，或发「记住：内容」。"
        memory_id = await pin_message(
            session, subject_id=subject_id, window=window, message_id=target_message_id,
            service=MemoryService(session, policy=config.memory),
        )
        return f"已固定这条记忆（{memory_id}）。" if memory_id else "现在无法固定这条原话。"
    if kind == "forget":
        if await forget_source(session, subject_id=subject_id, memory_id=command.argument[:64]):
            return "已经忘记这段来源及关联记忆。"
        return "没有找到可以忘记的来源。"
    memory_id, _, replacement = command.argument.partition(" ")
    row = await session.get(MemoryRow, memory_id) if 0 < len(memory_id) <= 64 else None
    if row is None or row.subject_id != subject_id or not replacement.strip():
        return "请发送「/更正 记忆编号 新内容」。"
    if await has_open_purge(session, subject_id=subject_id):
        return "正在清理记忆，暂时无法更正。"
    updated = await MemoryService(session, policy=config.memory).supersede(
        row.id, replacement.strip()[:4000], reason="用户在聊天中更正",
        correction_message_id=source.id if source.window_id == row.source_window_id else None,
    )
    return f"已更正这条记忆（{updated}）。" if updated else "现在无法更正这条记忆。"
