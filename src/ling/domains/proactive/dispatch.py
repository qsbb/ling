"""主动机会的真实投递。**默认关闭。**

## 为什么默认关

主动联系会真的打扰人。前面几步（收集、门控、生成草稿）都可以在影子模式下
验证，但投递不行——它一旦发生就是"她突然找你"。

所以这里用 `DialoguePolicy.proactive_delivery_enabled`（默认 False）挡着：
用户先看几天草稿、确认语气合适，再自己打开。

## 目标窗口怎么找

平台侧的 `window_ref`（如 `onebot-e2e:private:20002`）存在
`Window.title` 里——这是 bridge 建窗口时就定下的约定。
投递需要的是它，不是 `Window.id`。

只挑**私聊**窗口（`visibility='subject_private'`）：主动找你时往群里发
一句"在吗"是不可接受的。

## 送达判据

只有 `DeliveryOutcome.delivered` 为真才标 `sent`。`uncertain` 留给对账，
**不盲目重发**——重复发一条"在吗"比没发更糟。

## 异常也要结算（fail-closed）

回执说"失败"是一种情况，**抛异常**是另一种，而且更危险：`deliver` 抛异常
时不会把 operation 交回调用方，而异常完全可能发生在"平台已经收下这条消息"
之后（连接器断流、回执写库报错）。原来这里只把 `failed` 计数加一，机会仍留
在 `candidate`——下一轮会把它当成没发过的机会**再发一遍**，而消息发出去是
撤不回的。

所以失败路径一律**就地结算**，绝不留候选：

- `sent`：拿到真实送达回执（含"回执到了但落库失败"——平台确实收到了）
- `failed`：确定没发出去（没走到平台，或平台明确失败）
- `uncertain`：可能已经发出去了（平台被调用过、但没有回执）——等对账，不重发

每次结算都把本地 operation ID（`operation_id`）记到机会上（能拿到时）。它是
`DeliveryService` 在本机新建的 operation 行 ID，**不是平台侧幂等键**：
onebot/qqbot/qq 的 `connector.send` 只负责把消息发出去，不向平台透传
operation_id，平台也无从按它去重或对账。

## 认领、崩溃与异常边界

发送前会用 `idempotency_keys` 的唯一键认领机会，并单独提交认领状态。并发 worker
只有一个能越过该闸。认领超过 `STALE_CLAIM_SECONDS` 仍未结算时，按
`uncertain` 收敛，**不自动重发**：平台可能已经收下，但本地无法证明。

**未捕获异常的路径上，本地 operation/receipt 行仍不存在**。`deliver` 自己捕获
  `ChannelError` 并写好 `operation` + `delivery_receipts` 后正常返回，这类失败
  （含 `uncertain`）的本地行留得下；只有**逃出 `deliver` 的异常**——connector
  抛出的异常不是 `ChannelError`（没被那层 except 接住），或 `deliver` 建
  operation / 写回执时的数据库错误——才让这次投递的 `operation` /
  `delivery_receipts` 行随 SAVEPOINT 一起回滚。此时机会上的
  `delivery_operation_id` 只是**本地 operation ID 的残留**：本地查不到对应行，
  只能和同一轮那条 `logger.warning`（`operation=<id>`）对上。
  它不能用来向平台去重/对账（连接器本就不把 operation_id 透传给平台），
  也不代表现在有可用的对账：目前没有任务消费它；将来接
  `DeliveryService.reconcile()` 前，需要先把 operation 的持久化边界挪到
  SAVEPOINT 外（属 delivery 域改动，不在本轮范围）。
- **`uncertain` 不计入每日限额**（`sent_at` 只有确认送达才写）。代价是"可能已
  送达但没计数"，最多多出当轮的几次；这是有意的——宁可少算，也不把没确认的
  当成送达。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ling.infra.models import (
    IdempotencyKey,
    ProactiveDraftBinding,
    ProactiveOpportunity,
    Window,
    utcnow,
)
from ling.infra.proactive_quota import ProactiveQuotaReservation
from ling.infra.security import new_id
from ling.logging_setup import emit_event
from ling.ports.channel import ChannelConnector

logger = logging.getLogger("ling.domains.proactive.dispatch")

MAX_PER_ROUND = 3
STALE_CLAIM_SECONDS = 300
PROACTIVE_DISPATCH_SCOPE = "proactive_dispatch"
_DISPATCH_IN_PROGRESS = "v1:in_progress"
_DISPATCH_FINAL_STATES = frozenset({"sent", "rejected", "failed", "uncertain"})


def _local_quota_day(now: datetime, timezone_name: str | None) -> tuple[date, datetime, datetime]:
    try:
        timezone = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    local_day = now.astimezone(timezone).date()
    start = datetime.combine(local_day, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
    return local_day, start, end


@asynccontextmanager
async def _short_quota_session(session: AsyncSession):
    """Yield a fresh PostgreSQL session so its transaction never spans a send."""
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("主动联系日额度预留需要 PostgreSQL")
    bind = session.bind
    engine = bind if isinstance(bind, AsyncEngine) else bind.engine
    async with (
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as short,
        short.begin(),
    ):
        yield short


async def _lock_subject_quota(
    session: AsyncSession, *, actor_id: str, subject_id: str
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "proactive-daily-quota:" + json.dumps([actor_id, subject_id])},
    )


async def _reserve_daily_quota(
    session: AsyncSession,
    *,
    actor_id: str,
    subject_id: str,
    opportunity_id: str,
    daily_limit: int,
    timezone_name: str | None,
    now: datetime | None,
) -> bool:
    async with _short_quota_session(session) as short:
        await _lock_subject_quota(short, actor_id=actor_id, subject_id=subject_id)
        reserved_at = now or utcnow()
        local_day, start, end = _local_quota_day(reserved_at, timezone_name)
        existing = await short.get(
            ProactiveQuotaReservation, opportunity_id, populate_existing=True
        )
        if existing is not None and existing.state in {"reserved", "sent", "uncertain"}:
            return True
        active_count = await short.scalar(
            select(func.count()).select_from(ProactiveQuotaReservation).where(
                ProactiveQuotaReservation.actor_id == actor_id,
                ProactiveQuotaReservation.subject_id == subject_id,
                ProactiveQuotaReservation.local_day == local_day,
                ProactiveQuotaReservation.state.in_(("reserved", "sent", "uncertain")),
            )
        )
        legacy_sent_count = await short.scalar(
            select(func.count()).select_from(ProactiveOpportunity).where(
                ProactiveOpportunity.actor_id == actor_id,
                ProactiveOpportunity.subject_id == subject_id,
                ProactiveOpportunity.sent_at >= start,
                ProactiveOpportunity.sent_at < end,
                ~select(ProactiveQuotaReservation.opportunity_id)
                .where(ProactiveQuotaReservation.opportunity_id == ProactiveOpportunity.id)
                .exists(),
            )
        )
        if int(active_count or 0) + int(legacy_sent_count or 0) >= max(0, daily_limit):
            return False
        if existing is None:
            existing = ProactiveQuotaReservation(
                opportunity_id=opportunity_id,
                actor_id=actor_id,
                subject_id=subject_id,
                local_day=local_day,
                state="reserved",
                reserved_at=reserved_at,
                updated_at=reserved_at,
            )
            short.add(existing)
        else:
            existing.actor_id = actor_id
            existing.subject_id = subject_id
            existing.local_day = local_day
            existing.state = "reserved"
            existing.reserved_at = reserved_at
            existing.updated_at = reserved_at
        return True


async def _settle_daily_quota(
    session: AsyncSession, *, opportunity_id: str, delivery_state: str
) -> None:
    state = (
        "sent" if delivery_state in {"sent", "delivered"}
        else "uncertain" if delivery_state == "uncertain"
        else "released"
    )
    row = await session.get(ProactiveQuotaReservation, opportunity_id, populate_existing=True)
    if row is not None and row.state != "released":
        row.state = state
        row.updated_at = utcnow()


async def _release_daily_quota(session: AsyncSession, *, opportunity_id: str) -> None:
    row = await session.get(ProactiveQuotaReservation, opportunity_id, populate_existing=True)
    if row is not None and row.state == "reserved":
        row.state = "released"
        row.updated_at = utcnow()


def _claim_is_stale(created_at: datetime | None, now: datetime) -> bool:
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return now - created_at >= timedelta(seconds=STALE_CLAIM_SECONDS)


def _claim_state(result_ref: str | None) -> str:
    parts = str(result_ref or "").split(":", 2)
    if len(parts) >= 2 and parts[0] == "v1" and parts[1] in _DISPATCH_FINAL_STATES:
        return parts[1]
    return "uncertain"


async def _recover_abandoned_claims(
    session: AsyncSession, *, actor_id: str, now: datetime
) -> int:
    """Settle old in-flight sends as uncertain; never return them to the send queue."""
    claimed = (
        await session.execute(
            select(ProactiveOpportunity, IdempotencyKey)
            .join(
                IdempotencyKey,
                and_(
                    IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
                    IdempotencyKey.key == ProactiveOpportunity.id,
                ),
            )
            .where(
                ProactiveOpportunity.actor_id == actor_id,
                ProactiveOpportunity.state == "dispatching",
                IdempotencyKey.result_ref == _DISPATCH_IN_PROGRESS,
            )
        )
    ).all()
    recovered = 0
    for opportunity, claim in claimed:
        if not _claim_is_stale(claim.created_at, now):
            continue
        opportunity.state = "uncertain"
        opportunity.delivery_state = "uncertain"
        opportunity.rejected_reason = "投递认领已超时，平台可能已收到"
        claim.result_ref = "v1:uncertain:abandoned"
        quota = await session.get(
            ProactiveQuotaReservation, opportunity.id, populate_existing=True
        )
        if quota is not None and quota.state == "reserved":
            quota.state = "uncertain"
            quota.updated_at = now
        recovered += 1
    if recovered:
        await session.commit()
        logger.warning("主动投递回收 %d 条超时认领，均记为 uncertain", recovered)
    return recovered


async def _claim_opportunity(
    session: AsyncSession, opportunity: ProactiveOpportunity, *, now: datetime
) -> str:
    """Atomically reserve one opportunity before any external send begins."""
    key = str(opportunity.id)
    claim = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
            IdempotencyKey.key == key,
        )
    )
    if claim is not None:
        if claim.result_ref == _DISPATCH_IN_PROGRESS:
            if not _claim_is_stale(claim.created_at, now):
                return "busy"
            final_state = "uncertain"
            claim.result_ref = "v1:uncertain:abandoned"
        else:
            final_state = _claim_state(claim.result_ref)
            claim.result_ref = f"v1:{final_state}"
        opportunity.state = final_state
        opportunity.delivery_state = final_state
        if final_state == "sent" and opportunity.sent_at is None:
            opportunity.sent_at = now
        if final_state == "uncertain":
            opportunity.rejected_reason = "投递结果不确定，禁止自动重发"
        await session.commit()
        return final_state

    claim = IdempotencyKey(
        id=new_id("idem"),
        scope=PROACTIVE_DISPATCH_SCOPE,
        key=key,
        result_ref=_DISPATCH_IN_PROGRESS,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(claim)
            await session.flush()
    except IntegrityError:
        # Another dispatcher committed the same key while this one was inserting.
        existing = await session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if existing is None:
            raise
        return await _claim_opportunity(session, opportunity, now=now)

    opportunity.state = "dispatching"
    await session.commit()
    return "claimed"


async def _set_claim_result(
    session: AsyncSession, *, opportunity_id: str, state: str
) -> None:
    claim = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
            IdempotencyKey.key == opportunity_id,
        )
    )
    if claim is not None:
        claim.result_ref = f"v1:{state}"
        await session.flush()


async def _target_window(
    session: AsyncSession, *, subject_id: str, channel: str = "onebot"
) -> Window | None:
    """这个人最近活跃的**私聊**窗口。

    `channel` 决定投递到哪个渠道：`onebot` 是平台 ref（发给 QQ），
    `web` 是本地面板（消息落库，用户下次打开就能看到）。
    """
    stmt = (
        select(Window)
        .where(
            Window.subject_id == subject_id,
            Window.channel == channel,
            Window.visibility == "subject_private",
            Window.archived.is_(False),
        )
        .order_by(Window.last_message_at.desc().nulls_last())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _target_window_ref(
    session: AsyncSession, *, subject_id: str, channel: str = "onebot"
) -> str | None:
    """平台 ref（发给外部渠道用）。"""
    row = await _target_window(session, subject_id=subject_id, channel=channel)
    if row is None:
        return None
    # 群窗口的 title 也是 window_ref，但私聊条件已经挡住了
    return row.platform_reference or None


async def _bound_delivery_target(
    session: AsyncSession,
    *,
    opportunity: ProactiveOpportunity,
    binding: ProactiveDraftBinding,
    config_reader: object,
    deliver_to_web: bool,
    connector_for_window: Callable[..., Awaitable[ChannelConnector | None]] | None,
) -> tuple[Window, ChannelConnector | None] | None:
    """Use the drafted target, or a currently eligible equivalent fallback."""
    from typing import cast

    from ling.contracts.v1 import Modality
    from ling.domains.proactive.binding import (
        equivalent_settings,
        snapshot_metadata,
        target_fingerprint,
    )
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import runtime_window_snapshot

    if (binding.state != "ready" or not binding.target_window_id or not isinstance(binding.snapshot, dict)
            or not isinstance(binding.snapshot.get("target_fingerprint"), str)):
        return None
    windows = list((await session.scalars(select(Window).where(
        Window.actor_id == opportunity.actor_id,
        Window.subject_id == opportunity.subject_id,
        Window.visibility == "subject_private",
        Window.archived.is_(False),
        Window.channel.in_(("onebot", "qqbot", "web")),
    ).order_by(Window.last_message_at.desc().nulls_last(), Window.id))).all())

    async def eligible(window: Window) -> tuple[Window, ChannelConnector | None] | None:
        if window.channel == "web":
            return (window, None) if deliver_to_web else None
        if connector_for_window is None or not window.platform_reference:
            return None
        if window.trusted_instance_key is not None:
            connector = await connector_for_window(
                str(window.channel), window.platform_reference, instance_key=window.trusted_instance_key,
            )
        else:
            connector = await connector_for_window(str(window.channel), window.platform_reference)
        if connector is None:
            return None
        caps = connector.capabilities()
        if not caps.enabled or not caps.supports_private or not caps.supports_proactive:
            return None
        if opportunity.output_modality == "voice" and not caps.media_supported(Modality.audio):
            return None
        return window, connector

    original = next((window for window in windows if window.id == binding.target_window_id), None)
    if original is not None and target_fingerprint(original) == binding.snapshot["target_fingerprint"]:
        selected = await eligible(original)
        if selected is not None:
            return selected
    for window in sorted(windows, key=lambda item: item.channel == "web"):
        if window.id == binding.target_window_id:
            continue
        selected = await eligible(window)
        if selected is None:
            continue
        try:
            current = await runtime_window_snapshot(cast(ConfigService, config_reader), window.id)
            if equivalent_settings(binding.snapshot, snapshot_metadata(current)):
                return selected
        except Exception:  # noqa: BLE001 - an unverified fallback must not be used
            logger.info("无法核实主动草稿的候选目标，暂不投递")
    return None


async def bound_delivery_target_allowed(
    session: AsyncSession, *, opportunity_id: str, window_id: str, config_reader: object,
    channel: str, expected_target: str,
) -> bool:
    """Finish the guarded configuration check before the caller can enter send()."""
    async with _short_quota_session(session) as short:
        return await _bound_delivery_target_allowed_in_session(
            short,
            opportunity_id=opportunity_id,
            window_id=window_id,
            config_reader=config_reader,
            channel=channel,
            expected_target=expected_target,
        )


async def _bound_delivery_target_allowed_in_session(
    session: AsyncSession, *, opportunity_id: str, window_id: str, config_reader: object,
    channel: str, expected_target: str,
) -> bool:
    """Recheck a bound delivery under transaction locks before its final write or send."""
    from typing import cast

    from ling.domains.proactive.binding import (
        equivalent_settings,
        snapshot_metadata,
        target_fingerprint,
    )
    from ling.settings.service import ConfigService
    from ling.settings.window_policy import lock_dialogue_config, runtime_window_snapshot

    await lock_dialogue_config(session)
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == opportunity_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    binding = await session.get(ProactiveDraftBinding, opportunity_id, populate_existing=True)
    window = await session.scalar(
        select(Window).where(Window.id == window_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if (
        opportunity is None or opportunity.state != "dispatching" or binding is None
        or binding.state != "ready" or not isinstance(binding.snapshot, dict)
        or window is None or window.archived or window.channel != channel
        or window.visibility != "subject_private" or window.actor_id != opportunity.actor_id
        or window.subject_id != opportunity.subject_id
        or not isinstance(binding.snapshot.get("target_fingerprint"), str)
        or target_fingerprint(window) != expected_target
    ):
        return False
    expires_at = opportunity.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= utcnow() or not opportunity.draft_text:
        return False
    try:
        config = cast(ConfigService, config_reader)
        effective = await config.with_session(session).effective()
        if not effective.dialogue.proactive_delivery_enabled:
            return False
        if window.id == binding.target_window_id:
            return target_fingerprint(window) == binding.snapshot["target_fingerprint"]
        if window.id != binding.target_window_id:
            current = await runtime_window_snapshot(config, window.id)
            if not equivalent_settings(binding.snapshot, snapshot_metadata(current)):
                return False
    except Exception:  # noqa: BLE001 - an unverified target must not be used
        logger.info("无法再次核实主动草稿的目标，暂不投递")
        return False
    return True


async def _release_pre_send_claim(session: AsyncSession, *, opportunity_id: str) -> None:
    """Release a claim only while no external delivery has begun."""
    opportunity = await session.scalar(
        select(ProactiveOpportunity).where(ProactiveOpportunity.id == opportunity_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    claim = await session.scalar(select(IdempotencyKey).where(
        IdempotencyKey.scope == PROACTIVE_DISPATCH_SCOPE,
        IdempotencyKey.key == opportunity_id,
    ).with_for_update())
    if (opportunity is not None and opportunity.state == "dispatching" and claim is not None
            and claim.result_ref == _DISPATCH_IN_PROGRESS):
        await session.delete(claim)
        await _release_daily_quota(session, opportunity_id=opportunity_id)
        opportunity.state = "bound_candidate"
        opportunity.last_gate_allowed = False
        opportunity.last_gate_reasons = ["草稿对应的目标暂不可用"]
    await session.commit()


class _SendProbe:
    """在连接器外面包一层，只为回答两个问题：平台被调用了吗、这次投递的本地
    operation ID 是什么。

    `DeliveryService.deliver` 抛异常时不会把 operation 交回调用方，而异常
    完全可能发生在"平台已经收下这条消息"之后（连接器断流、回执写库失败）。
    没有这一层，失败路径只看得见一个异常类型，判不出消息到底出去没有，
    于是只能在"重发"和"永远留在候选"之间猜——两个都不对。

    只代理 `deliver` 真正用到的两个方法，其余属性（`connector_id` 等）透传。
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        # 平台是否真的被调用过。进了 send 就算：它可能已经在平台侧生效。
        self.send_attempted = False
        # 平台回执。None = 没拿到回执（不是"拿到了空回执"）。
        self.receipt: object | None = None
        # 这次投递的**本地** operation ID：`deliver` 会把自己新建的
        # operation.id 替进 message，所以这里拿到的是本地行 ID。注意
        # onebot/qqbot/qq 的 send 都不把这个值透传给平台，平台侧看不到它。
        self.operation_id: str | None = None

    def capabilities(self) -> object:
        return self._inner.capabilities()  # type: ignore[attr-defined]

    async def send(self, message: object) -> object:
        self.send_attempted = True
        self.operation_id = getattr(message, "operation_id", None)
        receipt = await self._inner.send(message)  # type: ignore[attr-defined]
        self.receipt = receipt
        return receipt

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _settle_exception(probe: _SendProbe, exc: Exception) -> tuple[str, str, str]:
    """把一次投递异常结算成 (机会状态, 投递状态, 给运维看的说明)。

    三种情况必须分开，因为它们对"能不能重发"的回答不同：

    1. 回执拿到了、抛在**写回执**时（数据库异常）→ 送达结果其实是确定的，
       按回执记。这是唯一"平台已经确认"的异常。
    2. 平台被调用过、没拿到回执 → 结果**未知**，记 `uncertain` 等对账。
    3. 根本没走到平台（能力自检、建 operation 就炸）→ 确定没发，记 `failed`。
    """
    name = type(exc).__name__
    receipt = probe.receipt
    if receipt is not None:
        raw = getattr(receipt, "state", None)
        state_value = getattr(raw, "value", None) or str(raw or "")
        if state_value in {"sent", "delivered"}:
            return ("sent", state_value, f"回执已到达但落库失败（{name}），按回执记送达")
        if state_value == "uncertain":
            return (
                "uncertain",
                state_value,
                f"回执不确定且落库失败（{name}），等对账，不重发",
            )
        return ("failed", state_value or "failed", f"平台回执为失败（{name}）")
    if probe.send_attempted:
        return (
            "uncertain",
            "uncertain",
            f"已调用平台但抛异常（{name}），可能已送达：等对账，不重发",
        )
    return ("failed", "failed", f"投递前失败（{name}），消息未发出")


async def dispatch_pending(
    session: AsyncSession,
    *,
    actor_id: str,
    connector: ChannelConnector | None,
    config_reader: object,
    subject_timezone: str | None = None,
    subject_timezones: Mapping[str, str] | None = None,
    now: datetime | None = None,
    limit: int = MAX_PER_ROUND,
    deliver_to_web: bool = False,
    connector_for_window: Callable[..., Awaitable[ChannelConnector | None]] | None = None,
    voice_dispatch: Callable[[ProactiveOpportunity, Window, object | None], Awaitable[str]]
    | None = None,
) -> dict[str, int | str]:
    """把有草稿的候选真的发出去。

    `deliver_to_web=True` 时，没有外部连接器也能投递：把草稿落成**会话里的
    一条她的消息**，用户下次打开面板就能看到"她主动发过话"。

    为什么需要这条路：部署实例上 qqbot 平台不给主动推送权限、OneBot 没启用，
    于是"主动联系"这条需求永远只是代码里的空转——她永远不会先开口。
    Web 投递不带推送通知，但"她确实主动过"这件事成立。

    ``subject_timezone`` 是**实例级兜底**；``subject_timezones`` 是逐个
    subject 的偏好（``ling/settings/user_preferences.py::timezone_by_subject``）。
    两者都要：多用户部署里每个人的"今天"和勿扰时段不同，而没设过偏好的
    subject 必须回退到同一个默认值，不能顺手借用别人的时区。

    返回统计，便于 worker 日志与运维观察。
    """
    from ling.capabilities.delivery import DeliveryService
    from ling.contracts.v1 import OperationState
    from ling.domains.proactive.service import ProactivityService
    from ling.ports.channel import OutboundMessage

    moment = now or utcnow()
    recovered_uncertain = await _recover_abandoned_claims(
        session, actor_id=actor_id, now=moment
    )
    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 读不到配置就不发
        logger.info("主动投递跳过：配置不可用（%s）", type(exc).__name__)
        return {
            "skipped": "config_unavailable",
            "failed": recovered_uncertain,
            "uncertain": recovered_uncertain,
        }

    if not bool(effective.dialogue.proactive_delivery_enabled):
        # 默认路径：只生成不发送。要让用户能看见"没发"，而不是以为发了。
        return {
            "skipped": "disabled",
            "failed": recovered_uncertain,
            "uncertain": recovered_uncertain,
        }

    # **必须把已发布的 dialogue 策略传下去**。不传时 `ProactivityService`
    # 会退回 `DialoguePolicy()` 的默认值（日限额 2、勿扰 0-0 → 恒不生效），
    # 于是用户在面板上设的勿扰时段和每日限额对**真实发送**完全无效：
    # 半夜照发、每天固定 2 条。`gate()` 的这两项只从 policy 或显式参数来，
    # 而这里没有显式参数，所以只能把生效快照里的 policy 交下去。
    service = ProactivityService(
        session, actor_id=actor_id, policy=effective.dialogue
    )

    async def settle(
        row: ProactiveOpportunity,
        *,
        state: str,
        reason: str | None = None,
        operation_id: str | None = None,
        delivery_state: str | None = None,
    ) -> None:
        await service.mark(
            row.id,
            state=state,
            reason=reason,
            operation_id=operation_id,
            delivery_state=delivery_state,
        )
        await _settle_daily_quota(
            session,
            opportunity_id=row.id,
            delivery_state=state,
        )
        await _set_claim_result(session, opportunity_id=row.id, state=state)
        await session.commit()

    rows = (
        (
            await session.execute(
                select(ProactiveOpportunity)
                .where(
                    ProactiveOpportunity.actor_id == actor_id,
                    ProactiveOpportunity.state.in_(("candidate", "bound_candidate")),
                    ProactiveOpportunity.draft_text.is_not(None),
                    ProactiveOpportunity.expires_at > moment,  # type: ignore[arg-type]
                )
                .order_by(ProactiveOpportunity.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    sent = rejected = 0
    failed = uncertain = recovered_uncertain
    async def reserve(row: ProactiveOpportunity) -> bool:
        nonlocal failed, uncertain
        checked_at = now or utcnow()
        claim_state = await _claim_opportunity(session, row, now=checked_at)
        if claim_state == "uncertain":
            failed += 1
            uncertain += 1
        if claim_state != "claimed":
            return False
        reserved = await _reserve_daily_quota(
            session,
            actor_id=actor_id,
            subject_id=row.subject_id,
            opportunity_id=row.id,
            daily_limit=int(effective.dialogue.proactive_daily_limit),
            timezone_name=(
                subject_timezones.get(row.subject_id)
                if subject_timezones is not None
                else None
            ) or subject_timezone,
            now=now,
        )
        if not reserved:
            await _release_pre_send_claim(session, opportunity_id=row.id)
            row.state = "bound_candidate" if await session.get(
                ProactiveDraftBinding, row.id
            ) is not None else "candidate"
            row.last_gate_allowed = False
            row.last_gate_reasons = ["已达到每日主动联系额度"]
            row.last_gate_at = checked_at
            await session.commit()
            return False
        return True

    for row in rows:
        checked_at = now or utcnow()
        # Each candidate receives a fresh gate evaluation. A same-subject cached
        # result would let several opportunities reuse one daily-quota decision.
        gate = await service.gate(
            subject_id=row.subject_id,
            now=checked_at,
            subject_timezone=(
                subject_timezones.get(row.subject_id)
                if subject_timezones is not None
                else None
            ) or subject_timezone,
            channel_can_proactive=True,
        )
        # 记录调度器当时真正执行的检查，不把页面上的当前快照冒充历史决策。
        # 关停发送、缺草稿或尚未扫描到的候选不会经过这里，保持未知。
        row.last_gate_allowed = bool(getattr(gate, "allowed", False))
        row.last_gate_reasons = list(getattr(gate, "reasons", []))
        row.last_gate_at = checked_at
        if not getattr(gate, "allowed", False):
            # 门控没过就留着，不标 rejected——它只是"现在还不是时候"
            continue

        binding = await session.get(ProactiveDraftBinding, row.id)
        bound_web_window: Window | None = None
        selected_target_fingerprint: str | None = None
        selected = connector
        window_ref = None
        selected_window: Window | None = None
        if binding is not None:
            bound = await _bound_delivery_target(
                session, opportunity=row, binding=binding, config_reader=config_reader,
                deliver_to_web=deliver_to_web, connector_for_window=connector_for_window,
            )
            if bound is None:
                row.last_gate_allowed = False
                row.last_gate_reasons = ["草稿对应的目标暂不可用"]
                continue
            bound_target, selected = bound
            from ling.domains.proactive.binding import target_fingerprint

            selected_target_fingerprint = target_fingerprint(bound_target)
            if bound_target.channel == "web":
                bound_web_window = bound_target
            else:
                selected_window = bound_target
                window_ref = bound_target.platform_reference
        elif connector_for_window is not None:
            target = await session.scalar(select(Window).where(
                Window.subject_id == row.subject_id,
                Window.channel.in_(("onebot", "qqbot")),
                Window.visibility == "subject_private", Window.archived.is_(False),
            ).order_by(Window.last_message_at.desc().nulls_last()).limit(1))
            if target is not None:
                window_ref = target.platform_reference
                if target.trusted_instance_key is not None:
                    selected = await connector_for_window(
                        str(target.channel), window_ref, instance_key=target.trusted_instance_key,
                    )
                else:
                    selected = await connector_for_window(str(target.channel), window_ref)
                if selected is not None:
                    capabilities = selected.capabilities()
                    supports_voice = True
                    if row.output_modality == "voice":
                        from ling.contracts.v1 import Modality

                        supports_voice = capabilities.media_supported(Modality.audio)
                    if (
                        not capabilities.enabled
                        or not capabilities.supports_proactive
                        or not supports_voice
                    ):
                        selected = None
                    else:
                        selected_window = target
        if selected is None:
            # Web 投递：没有外部渠道时的唯一路径。
            if not deliver_to_web:
                continue
            window = bound_web_window or await _target_window(
                session, subject_id=row.subject_id, channel="web"
            )
            if window is None:
                if await reserve(row):
                    await settle(
                        row, state="rejected", reason="没有可投递的 Web 会话"
                    )
                    rejected += 1
                continue
            emit_event(
                logger,
                logging.INFO,
                event_name="proactive_dispatch_skipped",
                message="没有可用的外部主动推送实例，改投 Web 会话",
                attributes={"reason_code": "no_proactive_channel", "opportunity_id": row.id},
            )
            if row.output_modality == "voice":
                if not await reserve(row):
                    continue
                try:
                    result = (
                        await voice_dispatch(row, window, None)
                        if voice_dispatch
                        else "failed"
                    )
                except asyncio.CancelledError:
                    await settle(
                        row,
                        state="uncertain",
                        reason="语音投递期间任务取消，结果无法确认",
                        delivery_state="uncertain",
                    )
                    raise
                if result == "target_unavailable" and binding is not None:
                    await _release_pre_send_claim(session, opportunity_id=row.id)
                    continue
                await settle(
                    row,
                    state="sent" if result == "delivered" else result,
                    delivery_state=result,
                )
                sent += result == "delivered"
                failed += result != "delivered"
                uncertain += result == "uncertain"
                continue
            if not await reserve(row):
                continue
            if binding is not None and not await bound_delivery_target_allowed(
                session, opportunity_id=row.id, window_id=window.id, config_reader=config_reader,
                channel="web", expected_target=selected_target_fingerprint or "",
            ):
                await _release_pre_send_claim(session, opportunity_id=row.id)
                continue
            await _append_web_message(
                session,
                window_id=window.id,
                text=str(row.draft_text),
                opportunity_id=row.id,
            )
            await settle(row, state="sent", reason="web")
            sent += 1
            continue

        if window_ref is None:
            window_ref = await _target_window_ref(session, subject_id=row.subject_id)
        if window_ref is None:
            if await reserve(row):
                await settle(
                    row, state="rejected", reason="没有可投递的私聊会话"
                )
                rejected += 1
            continue

        if row.output_modality == "voice":
            if not await reserve(row):
                continue
            window = selected_window
            if window is None:
                window = await session.scalar(
                    select(Window)
                    .where(
                        Window.subject_id == row.subject_id,
                        func.coalesce(Window.connector_window_ref, Window.title) == window_ref,
                        Window.channel.in_(("onebot", "qqbot")),
                        Window.visibility == "subject_private",
                        Window.archived.is_(False),
                    )
                    .order_by(Window.last_message_at.desc().nulls_last())
                    .limit(1)
                )
            if binding is not None and window is not None and not await bound_delivery_target_allowed(
                session, opportunity_id=row.id, window_id=window.id, config_reader=config_reader,
                channel=str(window.channel), expected_target=selected_target_fingerprint or "",
            ):
                await _release_pre_send_claim(session, opportunity_id=row.id)
                continue
            try:
                result = (
                    await voice_dispatch(row, window, selected)
                    if voice_dispatch and window
                    else "failed"
                )
            except asyncio.CancelledError:
                await settle(
                    row,
                    state="uncertain",
                    reason="语音投递期间任务取消，结果无法确认",
                    delivery_state="uncertain",
                )
                raise
            await settle(
                row,
                state="sent" if result == "delivered" else result,
                delivery_state=result,
            )
            sent += result == "delivered"
            failed += result != "delivered"
            uncertain += result == "uncertain"
            continue

        if not await reserve(row):
            continue
        if binding is not None and selected_window is not None and not await bound_delivery_target_allowed(
            session, opportunity_id=row.id, window_id=selected_window.id, config_reader=config_reader,
            channel=str(selected_window.channel), expected_target=selected_target_fingerprint or "",
        ):
            await _release_pre_send_claim(session, opportunity_id=row.id)
            continue
        delivery = DeliveryService(session)
        probe = _SendProbe(selected)
        try:
            # 每次投递各自一个 SAVEPOINT。
            #
            # 数据库异常（建 operation 或写回执时）会让**整个事务**进入
            # aborted：之后连 `service.mark` 都会失败，那一轮里已经发出去的
            # 记录也会随整轮回滚——下一轮就重发了。SAVEPOINT 把爆炸范围
            # 限制在这一条投递上，本轮其余部分照常结算。
            async with session.begin_nested():
                outcome = await delivery.deliver(
                    connector=probe,  # type: ignore[arg-type]
                    message=OutboundMessage(
                        operation_id=f"proactive:{row.id}",
                        window_ref=window_ref,
                        text=str(row.draft_text),
                        is_group=False,
                    ),
                    actor_id=actor_id,
                    subject_id=row.subject_id,
                    require_proactive=True,
                    # 记下"这条 operation 属于哪个机会"。异常路径下 deliver
                    # 不会把 operation 交回来，事后只能靠 payload 反查。
                    payload_extra={"opportunity_id": row.id, "source": "proactive"},
                )
        except asyncio.CancelledError:
            await settle(
                row,
                state="uncertain",
                reason="投递期间任务取消，平台可能已收到",
                operation_id=probe.operation_id,
                delivery_state="uncertain",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 一次失败不该中断整轮
            state, delivery_state, note = _settle_exception(probe, exc)
            logger.warning(
                "主动投递异常 %s → %s operation=%s：%s",
                row.id,
                state,
                probe.operation_id or "-",
                note,
            )
            # **就地结算，绝不留 candidate。** 留着它下一轮就会再发一遍，
            # 而发出去的消息撤不回来——宁可少发一条，也不能重复打扰。
            #
            # 这次 `mark` 写在 SAVEPOINT **外面**：回执写库失败时事务已经
            # aborted，只有先让 `begin_nested()` 退出并把 SAVEPOINT 回滚掉，
            # 这条 `mark` 才执行得下去。代价是这次投递的 operation / 回执行
            # 也一起被撤掉——机会上留下的 `delivery_operation_id` 只是本地
            # operation ID 的残留，本地查不到对应行，只能和上面那条 warning
            # 日志对上（见模块文档「已知限制」）。
            await settle(
                row,
                state=state,
                reason=note,
                operation_id=probe.operation_id,
                delivery_state=delivery_state,
            )
            if state == "sent":
                sent += 1
            else:
                failed += 1
                uncertain += state == "uncertain"
            continue

        # `delivery_state` 记**平台回执**的词汇（sent/delivered/uncertain/
        # failed/unsupported），与 goals / commitments 两处同名结算字段一致。
        # `outcome.state` 是 operation 的词汇（succeeded/…），混进来会让
        # "送达"在这一列里出现两种拼法，对账和面板都得猜。
        receipt_state = (
            str(outcome.receipt.state) if outcome.receipt is not None else outcome.state.value
        )

        if outcome.delivered:
            await settle(
                row,
                state="sent",
                operation_id=outcome.operation_id,
                delivery_state=receipt_state,
            )
            sent += 1
        else:
            # 不确定 ≠ 失败：结果未知只能等对账，**不能盲目重发**。
            # 重复发一条"在吗"比没发更糟。
            unknown = outcome.state is OperationState.uncertain
            await settle(
                row,
                state="uncertain" if unknown else "failed",
                reason=outcome.detail or "投递未成功",
                operation_id=outcome.operation_id,
                delivery_state=receipt_state,
            )
            failed += 1
            uncertain += unknown

    return {
        "sent": sent,
        "failed": failed,
        "rejected": rejected,
        # `failed` 仍是"这一轮没送出去"的总数（worker 的聚合事件按它定性，
        # 不改语义）；`uncertain` 是其中一个子集：**可能已经送达**。
        "uncertain": uncertain,
        "considered": len(rows),
    }


async def _append_web_message(
    session: AsyncSession, *, window_id: str, text: str, opportunity_id: str
) -> None:
    """把主动消息落成会话里的一条她的消息。

    `origin=simulated` 标明这是**模拟生成**的内容（不是真实经历、不是用户的
    话），前端与审计据此区分"她主动说的"和"用户问出来的"。
    """
    from ling.contracts.v1 import Origin
    from ling.domains.conversation.service import ConversationService

    conversation = ConversationService(session)
    message = await conversation.add_message(
        window_id, role="actor", text=text, origin=Origin.simulated
    )
    logger.info(
        "主动消息已落库 window=%s message=%s opportunity=%s",
        window_id,
        message.message_id,
        opportunity_id,
    )


__all__ = ["MAX_PER_ROUND", "dispatch_pending"]
