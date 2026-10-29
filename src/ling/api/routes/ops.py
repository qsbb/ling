"""运维状态：无人值守时需要"有事能第一时间知道"的可观测面。

运维要求覆盖：异机定时备份 + 隔离恢复，以及
（"有日志、有 healthcheck" 不足以保证本人会及时知道故障）。

设计原则：**只报可行动的问题**。返回项带 severity 与建议动作，
而不是把一堆指标堆出来——个人服务器没人天天看仪表盘。
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import CurrentAuth, DbSession
from ling.infra.models import MemoryEpisode, MemoryRow, OutboxEvent, Turn

router = APIRouter(prefix="/v1/health", tags=["health"])

# 阈值：个人服务器的合理默认值，可按需调
OUTBOX_PENDING_WARN = 50
OUTBOX_OLDEST_WARN_MINUTES = 30
DEAD_LETTER_WARN = 1
STUCK_TURN_WARN_MINUTES = 15
DISK_FREE_WARN_GB = 2.0
BACKUP_AGE_WARN_HOURS = 48


def _severity(item: dict) -> str:
    return str(item.get("severity") or "ok")


async def collect_status(session: AsyncSession) -> dict:
    """收集运维状态（供端点和告警推送共用）。

    抽成函数而不是只留在路由里：推送需要同一份判断，
    两处各写一遍迟早会不一致。
    """
    now = datetime.now(UTC)
    items: list[dict] = []

    # --- 1) outbox 积压与死信 ---
    pending = int(
        (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.state == "pending")
            )
        ).scalar_one()
        or 0
    )
    dead = int(
        (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.state == "dead")
            )
        ).scalar_one()
        or 0
    )
    oldest = (
        await session.execute(
            select(func.min(OutboxEvent.created_at)).where(
                OutboxEvent.state == "pending"
            )
        )
    ).scalar_one_or_none()
    oldest_minutes = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_minutes = round((now - oldest).total_seconds() / 60, 1)

    items.append(
        {
            "name": "outbox_pending",
            "value": pending,
            "oldest_minutes": oldest_minutes,
            "severity": (
                "warn"
                if pending >= OUTBOX_PENDING_WARN
                or (oldest_minutes or 0) >= OUTBOX_OLDEST_WARN_MINUTES
                else "ok"
            ),
            "action": (
                "检查 worker 是否在跑；积压说明记忆/关系更新没有推进"
                if pending or oldest_minutes
                else None
            ),
        }
    )
    items.append(
        {
            "name": "outbox_dead",
            "value": dead,
            "severity": "warn" if dead >= DEAD_LETTER_WARN else "ok",
            "action": (
                "有事件连续失败转入 dead；修好原因后调 replay_dead_events() 重放，"
                "否则派生记忆会一直缺席"
                if dead
                else None
            ),
        }
    )

    # --- 2) 卡住的轮次 ---
    stuck_cutoff = now - timedelta(minutes=STUCK_TURN_WARN_MINUTES)
    stuck = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Turn)
                .where(
                    Turn.state.in_(["pending", "streaming", "stopping"]),
                    Turn.updated_at < stuck_cutoff,
                )
            )
        ).scalar_one()
        or 0
    )
    items.append(
        {
            "name": "stuck_turns",
            "value": stuck,
            "severity": "warn" if stuck else "ok",
            "action": (
                "有轮次长时间没推进；worker 的悬空恢复会在 10 分钟后处理，"
                "持续出现说明模型或生成链路有问题"
                if stuck
                else None
            ),
        }
    )

    # --- 3) 备份新鲜度 ---
    backup_dir = os.environ.get("LING_BACKUP_DIR") or ""
    backup_age_hours = None
    if backup_dir and Path(backup_dir).is_dir():
        dumps = sorted(
            Path(backup_dir).glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if dumps:
            backup_age_hours = round(
                (now.timestamp() - dumps[0].stat().st_mtime) / 3600, 1
            )
    items.append(
        {
            "name": "backup_age_hours",
            "value": backup_age_hours,
            "severity": (
                "ok"
                if backup_age_hours is not None
                and backup_age_hours < BACKUP_AGE_WARN_HOURS
                else "warn"
            ),
            "action": (
                None
                if backup_age_hours is not None
                and backup_age_hours < BACKUP_AGE_WARN_HOURS
                else (
                    "超过 48 小时没有新备份，或未配置 LING_BACKUP_DIR；"
                    "个人服务器只有一份数据源，丢了不会因为只有一个用户而变轻"
                )
            ),
        }
    )

    # --- 4) 磁盘余量 ---
    data_dir = os.environ.get("LING_DATA_DIR") or "."
    free_gb = None
    try:
        free_gb = round(shutil.disk_usage(data_dir).free / (1024**3), 2)
    except OSError:
        free_gb = None
    items.append(
        {
            "name": "disk_free_gb",
            "value": free_gb,
            "severity": (
                "ok"
                if free_gb is not None and free_gb >= DISK_FREE_WARN_GB
                else "warn"
            ),
            "action": (
                f"可用空间低于 {DISK_FREE_WARN_GB}GB；磁盘写满后数据库会直接不可写"
                if free_gb is not None and free_gb < DISK_FREE_WARN_GB
                else None
            ),
        }
    )

    warnings = [i for i in items if _severity(i) != "ok"]
    return {
        "status": "attention" if warnings else "ok",
        "checked_at": now.isoformat(),
        "warnings": len(warnings),
        "items": items,
    }


@router.get("/ops")
async def ops_status(session: DbSession, auth: CurrentAuth) -> dict:
    """运维状态：把"可能出问题的地方"变成可执行的告警项。"""
    return await collect_status(session)


@router.post("/ops/notify")
async def ops_notify(session: DbSession, auth: CurrentAuth, request: Request) -> dict:
    """手动触发一次告警推送（便于验证 webhook 配好没有）。"""
    from ling.ops.notify import notify_if_needed

    status = await collect_status(session)
    url = getattr(request.app.state.settings, "alert_webhook_url", "") or ""
    return await notify_if_needed(
        session, status=status, webhook_url=url, base_url=str(request.base_url)
    )


@router.get("/stats", include_in_schema=False)
async def stats(session: DbSession, auth: CurrentAuth) -> dict:
    """轻量统计（供趋势观察，不参与告警）。"""
    memories = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryRow)
                .where(MemoryRow.archived_at.is_(None))
            )
        ).scalar_one()
        or 0
    )
    archived = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryRow)
                .where(MemoryRow.archived_at.is_not(None))
            )
        ).scalar_one()
        or 0
    )
    episodes = int(
        (
            await session.execute(
                select(func.count())
                .select_from(MemoryEpisode)
                .where(MemoryEpisode.archived_at.is_(None))
            )
        ).scalar_one()
        or 0
    )
    # 主动候选：影子模式下只能"攒着不发"，必须让人看得见，
    # 否则没法判断候选质量，也就没法决定要不要开真发送。
    from ling.infra.models import ProactiveOpportunity

    candidates = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ProactiveOpportunity)
                .where(ProactiveOpportunity.state == "candidate")
            )
        ).scalar_one()
        or 0
    )
    by_reason = {
        str(code): int(count)
        for code, count in (
            await session.execute(
                select(ProactiveOpportunity.reason_code, func.count())
                .where(
                    ProactiveOpportunity.state == "candidate",
                    ProactiveOpportunity.reason_code.is_not(None),
                )
                .group_by(ProactiveOpportunity.reason_code)
            )
        ).all()
    }
    return {
        "memories_active": memories,
        "memories_archived": archived,
        "episodes_active": episodes,
        "proactive_candidates": candidates,
        "proactive_candidates_by_reason": by_reason,
    }
