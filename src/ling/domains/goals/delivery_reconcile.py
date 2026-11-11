"""投递对账与续发：让 `uncertain` 与"该发未发"**真的收敛**。

## 为什么需要这一层

`DeliveryService.reconcile()` 早就写好了，但**没有任何生产调用方**——
线上 `uncertain` 等于永久悬空：既不会变成已送达，也不会有人再去查一次。
`docs/OUTBOUND-DELIVERY-RESEARCH.md` 把这条列为 P0-2。

这一层负责两件事（都由 worker 的 `delivery.reconcile` 周期任务调用）：

1. **对账**：扫 `operations.kind='delivery' AND state='uncertain'`，
   逐条问平台（`get_msg` / `message_sent` 回显 / 历史内容比对）。
2. **续发**：扫 `kind='delivery.pending'`（生成超时之类"有正文但还没发"），
   等正文落库后按段级幂等补发——**已发段不会重发**。

## 三条不能违反的规则

- **对账失败绝不重发**。查不到 ≠ 没发出去（NapCat 的短 id 表是 5000 条
  FIFO，重启/超量/撤回都会"消息不存在"，见调研 §4.2）。所以只有连接器给出
  **两个独立否证**时才记 `failed`，而且 `failed` 也只由调用方明确决定重发。
- **不确定就保持不确定**。超过重试窗口/次数后标 `reconcile_expired`，
  状态**仍是** `uncertain`：那是"我们永远不知道了"，不是"没发出去"。
  改成 failed 会让上游误判成可重发，直接导致重复消息。
- **每次外呼之间要歇一下**。这是外呼 NapCat，一轮几十条会把本机 NapCat 打满；
  每轮有上限（`RECONCILE_BATCH`），条与条之间有间隔。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ling.capabilities.delivery import DeliveryService, reconcile_due
from ling.contracts.v1 import OperationState
from ling.infra.db import session_scope
from ling.infra.models import Message, Operation
from ling.kernel.visibility import is_group_window_ref

if TYPE_CHECKING:
    # 只在注解里用：`ling.ports.channel` 是这个模块的下游依赖，
    # 运行时导入没有意义（本模块的函数体里按需导入它）。
    from ling.ports.channel import ChannelConnector

logger = logging.getLogger("ling.domains.goals.delivery_reconcile")

# 一轮最多处理多少条 uncertain（工程初值）。20 条 × 1 次外呼，最坏几十秒。
RECONCILE_BATCH = 20
# 两条对账之间的间隔（秒）。工程初值：NapCat 侧没有频控，这是自我约束。
RECONCILE_PAUSE_SECONDS = 1.0
# pending 投递的宽限期（秒）：生成超时后收尾要一点时间才把正文写进 messages，
# 太早去续发会读到"还没有正文"。工程初值。
PENDING_MIN_AGE_SECONDS = 5.0
# pending 最多等多久（秒）。超过就不再续发，只留一条可查的记录。
PENDING_MAX_AGE_SECONDS = 24 * 3600


@dataclass
class ReconcileReport:
    """这一轮对账/续发做了什么（给日志与测试断言）。"""

    scanned: int = 0
    delivered: int = 0
    still_uncertain: int = 0
    failed: int = 0
    pending_scanned: int = 0
    resumed: int = 0
    # 逐条结果，便于测试/排查（operation_id → 结局）
    details: list[dict[str, object]] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return self.scanned + self.pending_scanned


async def _pause(seconds: float) -> None:
    import asyncio

    if seconds > 0:
        await asyncio.sleep(seconds)


async def _due_uncertain() -> list[tuple[str, str, str | None]]:
    """取出这一轮该对账的 uncertain 投递：`[(operation_id, channel, instance)]`。"""
    rows: list[tuple[str, str, str | None]] = []
    async with session_scope() as session:
        result = await session.execute(
            select(Operation)
            .where(
                Operation.kind == "delivery",
                Operation.state == OperationState.uncertain.value,
            )
            .order_by(Operation.created_at.asc())
            # 多取一些：payload 里"还没到重试时间"的要跳过，不能把批次占满。
            .limit(RECONCILE_BATCH * 5)
        )
        for operation in result.scalars():
            payload = operation.payload or {}
            if not reconcile_due(payload):
                continue
            rows.append(
                (
                    operation.id,
                    str(payload.get("channel") or ""),
                    payload.get("instance"),
                )
            )
            if len(rows) >= RECONCILE_BATCH:
                break
    return rows


async def _due_pending() -> list[tuple[str, str, str, str, str, str | None]]:
    """取出这一轮该续发的 pending 投递。

    每项是 `(op_id, turn_id, window_ref, channel, actor_id, subject_id)`。
    """
    rows: list[tuple[str, str, str, str, str, str | None]] = []
    async with session_scope() as session:
        result = await session.execute(
            select(Operation)
            .where(
                Operation.kind == "delivery.pending",
                Operation.state.in_(
                    [OperationState.proposed.value, OperationState.running.value]
                ),
            )
            .order_by(Operation.created_at.asc())
            .limit(RECONCILE_BATCH)
        )
        for operation in result.scalars():
            payload = operation.payload or {}
            turn_id = str(payload.get("turn_id") or "")
            window_ref = str(payload.get("window_ref") or "")
            if not turn_id or not window_ref:
                # 没有定位信息就无法续发；标出来，别让它无限被扫。
                operation.state = OperationState.failed.value
                operation.detail = "pending 投递缺少 turn_id/window_ref，无法续发"
                continue
            rows.append(
                (
                    operation.id,
                    turn_id,
                    window_ref,
                    str(payload.get("channel") or ""),
                    str(operation.actor_id),
                    operation.subject_id,
                )
            )
    return rows


async def _actor_text(turn_id: str) -> str | None:
    """这一轮已经落库的正文（生成超时后由收尾路径写入）。"""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message.text)
                .where(Message.turn_id == turn_id, Message.role == "actor")
                .order_by(Message.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return str(row) if row else None


async def _settle_pending(
    operation_id: str, *, ok: bool, detail: str, retry: bool = False
) -> None:
    """给 pending 投递定结局。

    ``retry=True`` 表示"这次没发全，但还值得再试一轮"——状态退回
    ``proposed``，下一轮继续（已发段由段级幂等跳过，不会重复）。
    """
    async with session_scope() as session:
        operation = await session.get(Operation, operation_id)
        if operation is None:
            return
        if retry:
            operation.state = OperationState.proposed.value
        else:
            operation.state = (
                OperationState.succeeded.value if ok else OperationState.failed.value
            )
        operation.detail = detail[:500]


async def reconcile_deliveries(
    *,
    # 值的真实契约：一个渠道连接器（`DeliveryService.reconcile/deliver` 要的
    # 就是 `ChannelConnector`），同步返回或协程返回都可以，拿不到给 None。
    # 原来写成 `object`，等于把"它到底是什么"丢给了调用处。
    connector_for: Callable[[str], Awaitable[ChannelConnector] | ChannelConnector | None],
    limit: int = RECONCILE_BATCH,
    pause: Callable[[float], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> ReconcileReport:
    """对账 + 续发各跑一轮。**任何一条失败都不影响其它条**。

    ``connector_for(channel)`` 由调用方给（worker 里按渠道构造连接器）。
    拿不到连接器就跳过——没有渠道可问时"保持 uncertain"才是诚实的。
    """
    from ling.capabilities.chunking import plan_parts
    from ling.ports.channel import OutboundMessage

    report = ReconcileReport()
    sleeper = pause or _pause
    moment = now or datetime.now(UTC)

    # ---------------------------------------------------------------- 1) 对账
    for operation_id, channel, _instance in await _due_uncertain():
        connector = await _resolve(connector_for, channel)
        if connector is None:
            continue
        report.scanned += 1
        try:
            async with session_scope() as session:
                outcome = await DeliveryService(session).reconcile(
                    connector, operation_id
                )
        except Exception as exc:  # noqa: BLE001 - 单条失败不能吞掉整轮
            logger.warning(
                "对账失败（保持 uncertain）：op=%s %s", operation_id, type(exc).__name__
            )
            report.details.append(
                {"operation_id": operation_id, "state": "error", "error": type(exc).__name__}
            )
            await sleeper(RECONCILE_PAUSE_SECONDS)
            continue
        if outcome.state is OperationState.succeeded:
            report.delivered += 1
        elif outcome.state is OperationState.failed:
            # 连接器只在"两个独立否证"时给 failed；**仍然不重发**。
            report.failed += 1
        else:
            report.still_uncertain += 1
        report.details.append(
            {
                "operation_id": operation_id,
                "state": str(outcome.state),
                "detail": (outcome.detail or "")[:200],
            }
        )
        logger.info(
            "投递对账 op=%s → %s（%s）",
            operation_id,
            outcome.state,
            (outcome.detail or "")[:120],
        )
        await sleeper(RECONCILE_PAUSE_SECONDS)

    # ---------------------------------------------------------------- 2) 续发
    for (
        operation_id,
        turn_id,
        window_ref,
        channel,
        actor_id,
        subject_id,
    ) in await _due_pending():
        report.pending_scanned += 1
        text = await _actor_text(turn_id)
        if not text:
            # 正文还没落库：给收尾一点时间；太久了就放弃（留痕）。
            age = await _pending_age_seconds(operation_id, moment=moment)
            if age >= PENDING_MAX_AGE_SECONDS:
                await _settle_pending(
                    operation_id,
                    ok=False,
                    detail="生成超时后一直没有等到正文落库，放弃续发（未投递）",
                )
            continue
        connector = await _resolve(connector_for, channel)
        if connector is None:
            continue
        # 群聊判定走**唯一实现**（`kernel/visibility.py`）。
        #
        # 这里是**降级路径**：`window_ref` 来自 operation 的 payload（:138），
        # 手上没有 Window 行，读不到权威的 `visibility` 列。所以用
        # `is_group_window_ref()` 从标识反推——它与原来的
        # `":group:" in window_ref` **子串**匹配的区别是：不会把
        # `onebot:private:group:123` 这类私聊标识误判成群聊。
        #
        # 误判的后果在这里是分条策略错：`plan_parts` 会按群聊的上限切分，
        # 而实际是私聊——切出来的条数不对，可能多切（多发几条消息）或少切
        # （被平台整条拒收）。
        is_group = is_group_window_ref(window_ref)
        caps = getattr(connector, "capabilities", None)
        part_limit = None
        if callable(caps):
            with contextlib.suppress(Exception):
                part_limit = int(caps().max_text_length) or None
        parts = plan_parts(text, is_group=is_group, max_part_chars=part_limit)
        plan_id = f"plan_{turn_id}"
        sent = 0
        ok = True
        for index, part in enumerate(parts, 1):
            try:
                async with session_scope() as session:
                    outcome = await DeliveryService(session).deliver(
                        connector=connector,
                        message=OutboundMessage(
                            operation_id=f"op_resume_{index}",
                            window_ref=window_ref,
                            text=part,
                            is_group=is_group,
                        ),
                        actor_id=actor_id,
                        subject_id=subject_id,
                        payload_extra={
                            "reply_plan": plan_id,
                            "part_ordinal": index,
                            "parts_total": len(parts),
                            "source": "delivery.reconcile",
                        },
                        plan_id=plan_id,
                        part_ordinal=index,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "续发失败：op=%s 第 %d 段 %s", operation_id, index, type(exc).__name__
                )
                ok = False
                break
            if outcome.delivered or outcome.skipped:
                # skipped 且已送达 = 这一段早就发出去了（段级幂等命中）
                sent += 1
                # 续发也要有节奏：连发 5 段同样是刷屏（与 bridge 同一个理由，
                # 只是这里不需要抖动——它本来就不是"她正在说"）。
                await sleeper(RECONCILE_PAUSE_SECONDS)
                continue
            ok = False
            break
        if ok and sent == len(parts):
            report.resumed += 1
            await _settle_pending(
                operation_id,
                ok=True,
                detail=f"续发成功：{sent}/{len(parts)} 段（段级幂等跳过已发段）",
            )
        else:
            # 没发全：还年轻就再试一轮（已发段会被跳过）；太久了才放弃，
            # 否则这条 pending 会永远在扫、永远发不完。
            age = await _pending_age_seconds(operation_id, moment=moment)
            await _settle_pending(
                operation_id,
                ok=False,
                detail=f"续发未完成：{sent}/{len(parts)} 段",
                retry=age < PENDING_MAX_AGE_SECONDS,
            )
        await sleeper(RECONCILE_PAUSE_SECONDS)

    return report


async def _pending_age_seconds(operation_id: str, *, moment: datetime) -> float:
    async with session_scope() as session:
        operation = await session.get(Operation, operation_id)
        if operation is None or operation.created_at is None:
            return 0.0
        created = operation.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (moment - created).total_seconds()


async def _resolve(
    connector_for: Callable[[str], Awaitable[ChannelConnector] | ChannelConnector | None],
    channel: str,
) -> ChannelConnector | None:
    if not channel:
        return None
    with contextlib.suppress(Exception):
        found = connector_for(channel)
        if hasattr(found, "__await__"):
            found = await found  # type: ignore[misc]
        return found
    return None


__all__ = [
    "PENDING_MAX_AGE_SECONDS",
    "PENDING_MIN_AGE_SECONDS",
    "RECONCILE_BATCH",
    "RECONCILE_PAUSE_SECONDS",
    "ReconcileReport",
    "reconcile_deliveries",
]
