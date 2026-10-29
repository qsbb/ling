"""告警推送：让"有事能第一时间知道"真的成立。

`/v1/health/ops` 只解决"能查"，不解决"会知道"。个人服务器没人天天盯仪表盘，
所以必须主动推。

设计取舍：
- **只推需要人处理的事**，不推心跳。心跳推送会让人很快开始忽略通知，
  真正出事的那条就淹在噪声里。
- **同一告警不重复轰炸**：按「检查项 + 严重度」去重，状态好转才允许再次推送。
  否则 worker 挂一小时会推几百条，反而把通知渠道用废。
- 走通用 webhook（POST JSON）：不绑定具体平台，Telegram/Discord/钉钉/自建
  都能接。**不内置任何第三方凭据**。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import OpsAlertState

logger = logging.getLogger(__name__)

# 同一告警的最小重复间隔。30 分钟：够快，又不至于把渠道打废。
REPEAT_INTERVAL = timedelta(minutes=30)


def _fingerprint(item: dict) -> str:
    """告警指纹：只按"哪一项 + 什么严重度"，不按具体数值。

    数值变化（积压 51 → 52）不该算新告警，否则每条都推一次。
    """
    return f"{item.get('name')}:{item.get('severity')}"


async def should_notify(
    session: AsyncSession, *, fingerprint: str, now: datetime | None = None
) -> bool:
    """是否需要推送。状态未变且未过间隔则跳过。"""
    now = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(OpsAlertState).where(OpsAlertState.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()
    if row is None:
        return True
    last = row.last_sent_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last >= REPEAT_INTERVAL


async def mark_sent(
    session: AsyncSession, *, fingerprint: str, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(OpsAlertState).where(OpsAlertState.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            OpsAlertState(fingerprint=fingerprint, last_sent_at=now, count=1)
        )
    else:
        row.last_sent_at = now
        row.count = int(row.count or 0) + 1
    await session.flush()


async def mark_cleared(session: AsyncSession, *, fingerprint: str) -> None:
    """状态好转：删掉记录，下次再出问题能立刻推送。"""
    row = (
        await session.execute(
            select(OpsAlertState).where(OpsAlertState.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.flush()


def build_payload(*, status: dict, base_url: str = "") -> dict:
    """组装推送内容。只带需要处理的信息，不带全量指标。"""
    warnings = [i for i in status.get("items", []) if i.get("severity") != "ok"]
    lines = []
    for item in warnings:
        action = item.get("action") or ""
        lines.append(f"· {item['name']} = {item.get('value')} — {action}")
    text = "ling 需要处理：\n" + "\n".join(lines) if lines else "ling 状态正常"
    return {
        "text": text,
        "status": status.get("status"),
        "checked_at": status.get("checked_at"),
        "warnings": warnings,
        "source": base_url or "ling",
    }


async def send_webhook(
    url: str, payload: dict, *, timeout_seconds: float = 10.0
) -> tuple[bool, str]:
    """POST JSON 到 webhook。返回 (成功, 说明)。

    失败只记日志不抛：告警发不出去不该让检查本身失败，
    否则会因为"通知渠道坏了"而看不到真实故障。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                url,
                content=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        return False, f"不可达: {type(exc).__name__}"
    if response.status_code >= 300:
        return False, f"HTTP {response.status_code}: {response.text[:120]}"
    return True, "ok"


async def notify_if_needed(
    session: AsyncSession,
    *,
    status: dict,
    webhook_url: str,
    base_url: str = "",
    now: datetime | None = None,
) -> dict:
    """按需推送并维护去重状态。

    返回 {"sent": [...], "skipped": [...], "cleared": [...]}，便于测试与审计。
    """
    now = now or datetime.now(UTC)
    sent: list[str] = []
    skipped: list[str] = []
    cleared: list[str] = []

    active = {
        _fingerprint(i)
        for i in status.get("items", [])
        if i.get("severity") != "ok"
    }
    # 已恢复的告警清掉记录，下次能立刻推
    known = list((await session.execute(select(OpsAlertState))).scalars())
    for row in known:
        if row.fingerprint not in active:
            await mark_cleared(session, fingerprint=row.fingerprint)
            cleared.append(row.fingerprint)

    if not active:
        return {"sent": sent, "skipped": skipped, "cleared": cleared}
    if not webhook_url:
        return {"sent": sent, "skipped": sorted(active), "cleared": cleared}

    # 一次推送里合并所有新告警，避免同一时刻发多条
    pending = [
        fp for fp in sorted(active) if await should_notify(session, fingerprint=fp, now=now)
    ]
    skipped.extend(sorted(active - set(pending)))
    if not pending:
        return {"sent": sent, "skipped": skipped, "cleared": cleared}

    ok, detail = await send_webhook(webhook_url, build_payload(status=status, base_url=base_url))
    if ok:
        for fp in pending:
            await mark_sent(session, fingerprint=fp, now=now)
            sent.append(fp)
    else:
        logger.warning("告警推送失败（下次重试）: %s", detail)
        skipped.extend(pending)
    return {"sent": sent, "skipped": skipped, "cleared": cleared}


__all__ = [
    "REPEAT_INTERVAL",
    "build_payload",
    "mark_cleared",
    "mark_sent",
    "notify_if_needed",
    "send_webhook",
    "should_notify",
]
