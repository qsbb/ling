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

## 已知限制（本轮只记录，留给 task-13 验收）

- **不是 crash-safe 的**：结算与整轮投递共用一个事务。消息已经发出、进程在
  提交前崩掉时，机会仍是 `candidate`——下一轮会重发。要真正防重复，得在发送
  **之前**原子认领机会（`UPDATE … WHERE state='candidate' RETURNING`）并单独
  提交，而不是发完再改状态。
- **不是多实例幂等的**：候选查询没有行锁，也没有"先认领再发"的原子动作。两个
  worker、两个重叠的 tick、或同一条调度消息被重投时，同一批候选会被各发一条。
  单副本部署通常串行，所以现在没暴露——但那靠调度顺序，不是靠约束。
- **未捕获异常的路径上，本地 operation/receipt 行不存在**。`deliver` 自己捕获
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

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ProactiveOpportunity, Window, utcnow

logger = logging.getLogger("ling.domains.proactive.dispatch")

MAX_PER_ROUND = 3


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
    return str(row.title or "") or None


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
    connector: object,
    config_reader: object,
    subject_timezone: str | None = None,
    now: datetime | None = None,
    limit: int = MAX_PER_ROUND,
    deliver_to_web: bool = False,
) -> dict[str, int | str]:
    """把有草稿的候选真的发出去。

    `deliver_to_web=True` 时，没有外部连接器也能投递：把草稿落成**会话里的
    一条她的消息**，用户下次打开面板就能看到"她主动发过话"。

    为什么需要这条路：部署实例上 qqbot 平台不给主动推送权限、OneBot 没启用，
    于是"主动联系"这条需求永远只是代码里的空转——她永远不会先开口。
    Web 投递不带推送通知，但"她确实主动过"这件事成立。

    返回统计，便于 worker 日志与运维观察。
    """
    from ling.capabilities.delivery import DeliveryService
    from ling.contracts.v1 import OperationState
    from ling.domains.proactive.service import ProactivityService
    from ling.ports.channel import OutboundMessage

    moment = now or utcnow()
    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 读不到配置就不发
        logger.info("主动投递跳过：配置不可用（%s）", type(exc).__name__)
        return {"skipped": "config_unavailable"}

    if not bool(effective.dialogue.proactive_delivery_enabled):
        # 默认路径：只生成不发送。要让用户能看见"没发"，而不是以为发了。
        return {"skipped": "disabled"}

    service = ProactivityService(session, actor_id=actor_id)
    rows = (
        (
            await session.execute(
                select(ProactiveOpportunity)
                .where(
                    ProactiveOpportunity.actor_id == actor_id,
                    ProactiveOpportunity.state == "candidate",
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

    sent = failed = rejected = uncertain = 0
    gate_cache: dict[str, object] = {}
    for row in rows:
        gate = gate_cache.get(row.subject_id)
        if gate is None:
            gate = await service.gate(
                subject_id=row.subject_id,
                now=moment,
                subject_timezone=subject_timezone,
                channel_can_proactive=True,
            )
            gate_cache[row.subject_id] = gate
        if not getattr(gate, "allowed", False):
            # 门控没过就留着，不标 rejected——它只是"现在还不是时候"
            continue

        if connector is None:
            # Web 投递：没有外部渠道时的唯一路径。
            if not deliver_to_web:
                continue
            window = await _target_window(
                session, subject_id=row.subject_id, channel="web"
            )
            if window is None:
                await service.mark(
                    row.id, state="rejected", reason="没有可投递的 Web 会话"
                )
                rejected += 1
                continue
            await _append_web_message(
                session,
                window_id=window.id,
                text=str(row.draft_text),
                opportunity_id=row.id,
            )
            await service.mark(row.id, state="sent", reason="web")
            sent += 1
            continue

        window_ref = await _target_window_ref(session, subject_id=row.subject_id)
        if window_ref is None:
            await service.mark(
                row.id, state="rejected", reason="没有可投递的私聊会话"
            )
            rejected += 1
            continue

        delivery = DeliveryService(session)
        probe = _SendProbe(connector)
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
            await service.mark(
                row.id,
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
            await service.mark(
                row.id,
                state="sent",
                operation_id=outcome.operation_id,
                delivery_state=receipt_state,
            )
            sent += 1
        else:
            # 不确定 ≠ 失败：结果未知只能等对账，**不能盲目重发**。
            # 重复发一条"在吗"比没发更糟。
            unknown = outcome.state is OperationState.uncertain
            await service.mark(
                row.id,
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
