"""Delivery：按渠道能力渲染、发送、记录本地 operation/回执并与平台回执对账。

不变式（设计文档 §1 第 6 条）：
- 计划不是结果。只有拿到平台真实回执才能标 ``succeeded``。
- 结果不确定标 ``uncertain`` 并进入对账，**不重发**不可逆动作。
- 渠道没有能力时返回 ``unsupported``，不假装已联系。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import DeliveryState, ErrorCode, OperationState
from ling.infra.models import DeliveryReceiptRow, IdempotencyKey, Operation, utcnow
from ling.infra.security import new_id
from ling.ports.channel import (
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    ReceiptQuery,
    delivery_text_digest,
    iter_outbound_attachments,
)

# ---------------------------------------------------------------- 段级幂等
#
# 幂等键作用域：一条逻辑回复的**第几段**。
#   IdempotencyKey(scope=PART_SCOPE, key=f"{plan_id}:{ordinal}")
#   result_ref = 这一段的 operation_id
#
# `plan_id` **必须由本轮 turn 派生**（`bridge._deliver` 用 `plan_{turn_id}`），
# 否则重投会拿到一个新的 plan_id，幂等直接失效——那正是"重投必然重发第 1 段"
# 的根因（原实现的 plan_id 是 `plan_{uuid4()}`）。
PART_SCOPE = "delivery_part"

# 对账状态机的工程初值（依据 docs/OUTBOUND-DELIVERY-RESEARCH.md §5 P0-2）。
# **都没有经过真实 NapCat 长跑验证**，需要按线上观察调整。
RECONCILE_MAX_ATTEMPTS = 6
RECONCILE_BACKOFF_SECONDS = (60, 120, 300, 600, 1200, 1800)
RECONCILE_MAX_AGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class DeliveryOutcome:
    # 本地 operations 行主键。不是平台侧幂等键：connector 不透传 operation_id。
    operation_id: str
    state: OperationState
    receipt: DeliveryReceiptRow | None
    detail: str
    # 这一段**没有真的再发一次**：它上一次已经送达（succeeded），
    # 或还停在 uncertain（等对账）。重投时靠它区分"跳过"与"刚发出去"。
    skipped: bool = False

    @property
    def delivered(self) -> bool:
        """**唯一**的"这条真的送达了吗"判据。

        为什么要有这个属性：原来两个调用点各写一种拼法——
        `bridge` 判 `receipt.state in {"delivered", "sent"}`，
        `worker` 判 `state == "succeeded"`。

        两者当前等价（`deliver()` 里 sent/delivered 都映射成 succeeded），
        但这是**两份定义同一个事实**。将来若给 `deliver` 加一个新成功态，
        只有一处会被记得改，另一处会静默把成功当失败。

        集中成一个属性之后，改规则只有一处。

        这条判据同时是「**没有回执不声称已发送**」的落地：`receipt is not None`
        在当前代码里不可达（`deliver()` 的每条返回路径都写了回执，能力不通过时
        是 `state=failed` / `receipt.state=unsupported`），保留它是为了让
        将来某次重构漏写回执时**默认为未送达**，而不是默认为送达。

        方向很重要：宁可把"其实发了"记成没发（用户会看到重试或对账），
        也不要把"没发出去"记成送达（用户什么都看不到，而本地记录说成功了）。
        """
        return self.receipt is not None and self.state is OperationState.succeeded


class DeliveryService:
    """唯一写入 operations / delivery_receipts 的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_operation(
        self,
        *,
        kind: str,
        actor_id: str,
        subject_id: str | None,
        payload: dict,
    ) -> Operation:
        row = Operation(
            id=new_id("op"),
            kind=kind,
            actor_id=actor_id,
            subject_id=subject_id,
            state=OperationState.proposed.value,
            payload=payload,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def deliver(
        self,
        *,
        connector: ChannelConnector,
        message: OutboundMessage,
        actor_id: str,
        subject_id: str | None,
        require_proactive: bool = False,
        payload_extra: dict | None = None,
        # 段级幂等：`(plan_id, part_ordinal)` 唯一确定"这一条逻辑回复的第几段"。
        # 两个都给了才启用；只给一个（或都不给）等于"这一次没有计划身份"，
        # 退化成老行为（每次都发）——调用方必须自己保证这是有意的。
        plan_id: str | None = None,
        part_ordinal: int | None = None,
    ) -> DeliveryOutcome:
        caps = connector.capabilities()
        if not caps.enabled:
            return await self._unsupported(
                message, actor_id, subject_id, "渠道未启用（能力自检未通过或管理员关闭）"
            )
        if message.is_group and not caps.supports_group:
            return await self._unsupported(message, actor_id, subject_id, "渠道不支持群聊发送")
        if not message.is_group and not caps.supports_private:
            return await self._unsupported(message, actor_id, subject_id, "渠道不支持私聊发送")
        if require_proactive and not caps.supports_proactive:
            return await self._unsupported(
                message, actor_id, subject_id, "该渠道当前不支持主动推送（需以账号权限探测为准）"
            )
        # 媒体能力必须单独挡。不挡的话会出现最糟的一种"成功"：
        # 文字发出去、图被静默丢弃、回执照样拿到——本地记成已送达，
        # 用户却什么都没看到。宁可在发送前就拒绝。
        #
        # **按类型逐个判断**，不再只看一个 `supports_media` 位。一个渠道能发
        # 图片不代表能发语音：过去用一个 bit 时，发语音的请求在只支持图片的
        # 渠道上会被放行，然后静默变成"只发了文字"或"发了一张图"。
        #
        # 兼容性：`media_refs`（legacy）经 `iter_outbound_attachments()` 一律
        # 被当作 image，而 `media_supported(image)` 在旧声明
        # `supports_media=True` 下返回 True——所以旧行为**完全等价**
        # （旧渠道 + 旧 refs → 一律放行，与改动前一致）。
        unsupported = [
            attachment.kind.value
            for attachment in iter_outbound_attachments(message)
            if not caps.media_supported(attachment.modality)
        ]
        if unsupported:
            return await self._unsupported(
                message,
                actor_id,
                subject_id,
                "渠道不支持发送媒体"
                f"（{len(unsupported)} 个未发送：{'、'.join(sorted(set(unsupported)))}）",
            )
        # 文本超限：**这里只做防御性拦截，不做切分**。
        #
        # 切分属于规划层（`chunking.plan_parts`），因为它必须知道"这一段属于
        # 同一条回复的第几段"，并且要在**同一个 reply_plan 下**逐段确认回执。
        # 在 `deliver` 里偷偷切成多条，会绕过 `plan_parts` 的防刷屏上限
        # （`MAX_PARTS_PRIVATE`），还会让一条 operation 变成多条却没有计划记录。
        #
        # 所以走到这里说明**上游规划有 bug**（没按渠道上限切，或新增的调用路径
        # 直接投递未切分的文本）。错误码用 `internal` 而不是
        # `unsupported_capability`：后者会被读成"这个渠道不支持长文本"，
        # 而事实是"我们本该切好再发"。
        #
        # 反过来说，**不能**在这里静默截断：截断会让用户只看到半句话，
        # 本地却记成已送达。
        if len(message.text) > caps.max_text_length:
            detail = (
                f"文本超出渠道上限（{len(message.text)} > {caps.max_text_length}）："
                "上游规划未按渠道上限切分（应先用 chunking.plan_parts 切段再投递）"
            )
            return await self._unsupported(
                message, actor_id, subject_id, detail, error_code=ErrorCode.internal
            )

        # **段级幂等的闸门放在能力校验之后、建 operation 之前。**
        #
        # 为什么必须在建 operation 之前：重投（OneBot 超时重发 / 事件认领被
        # 释放）会重新规划一遍分段。`plan_id` 由 turn 派生、段序号稳定，所以
        # 同一个 `(plan_id, ordinal)` 就是同一段内容。查这个键就能知道：
        #   - succeeded → 已经真的发出去过 → **跳过**（重投不许重发第 1 段）
        #   - uncertain → 可能发出去了 → **不重发**，交给对账（`reconcile`）
        #   - failed    → 明确没发出去 → 可以再发
        #   - 其它（running/proposed）= 正在发/上次发到一半 → 当 uncertain
        #
        # 为什么必须在能力校验**之后**：渠道不支持 / 文本超限这类"根本没打算
        # 发"的情况，不该在幂等表里留下一个"这一段可能发过了"的记录——
        # 那会让以后每一次重投都跳过它（而且是静默的）。
        claim: IdempotencyKey | None = None
        if plan_id is not None and part_ordinal is not None:
            claim, claimed = await self._claim_part(plan_id, part_ordinal)
            if not claimed:
                existing = await self._replay_outcome(claim)
                if existing is not None:
                    return existing

        operation = await self.create_operation(
            kind="delivery",
            actor_id=actor_id,
            subject_id=subject_id,
            payload={
                "channel": connector.connector_id,
                "instance": connector.instance_id,
                "window_ref": message.window_ref,
                "chars": len(message.text),
                "is_group": message.is_group,
                # 记录**意图**：这条 operation 本应带几个媒体。
                # 事后对账时才能区分"压根没想发媒体"和"想发但失败了"。
                "media_count": len(message.media_refs),
                "media_refs": list(message.media_refs),
                # 分段信息：一条逻辑回复可能拆成多段，每段一个 operation。
                # 事后能回答"这条回复发全了吗"。
                "reply_plan": plan_id,
                "part_ordinal": part_ordinal,
                # 对账用：**内容摘要而不是正文**。没有平台 id 的投递只能靠
                # "平台历史里有没有同文消息"作证据，但把正文抄进 operations
                # 等于给内容开第二份副本；摘要能比对、不能还原。
                "text_digest": delivery_text_digest(message.text),
                **dict(payload_extra or {}),
            },
        )
        operation.state = OperationState.running.value
        await self._session.flush()
        if claim is not None:
            # 抢占行要**立刻**记下它对应哪条 operation：重投/对账都要靠
            # `result_ref` 找回上一段的状态。行本身在 `_claim_part` 里已 flush。
            claim.result_ref = operation.id
            await self._session.flush()

        # 把本地 operation ID 替进 message，保证 connector 拿到的
        # ``operation_id`` 与刚建的 operation 行是同一个 ID——回执里的
        # ``operation_id`` 和日志都靠它回到本地记录。
        #
        # 它**不是平台侧幂等键**：onebot / qqbot / qq 的 ``send`` 都不把这个值
        # 放进出站 payload（qq 只拿它拼本地 ``receipt_id``），平台看不到它，
        # 也就不能按它去重或对账。真正的平台凭据是回执里的
        # ``platform_message_id``；这里的 replace 只解决本地关联。
        outbound = replace(message, operation_id=operation.id)

        try:
            receipt = await connector.send(outbound)
        except ChannelError as exc:
            state = (
                OperationState.uncertain
                if exc.kind is ChannelErrorKind.uncertain
                else OperationState.failed
            )
            operation.state = state.value
            operation.detail = exc.message
            await self._session.flush()
            row = DeliveryReceiptRow(
                id=new_id("rcpt"),
                operation_id=operation.id,
                channel=connector.connector_id,
                state=DeliveryState.uncertain.value
                if state is OperationState.uncertain
                else DeliveryState.failed.value,
                detail=exc.message,
                observed_at=utcnow(),
            )
            self._session.add(row)
            await self._session.flush()
            return DeliveryOutcome(
                operation_id=operation.id, state=state, receipt=row, detail=exc.message
            )

        # 只有确认送达/已发送才算成功
        if receipt.state in {DeliveryState.sent, DeliveryState.delivered}:
            operation.state = OperationState.succeeded.value
        elif receipt.state is DeliveryState.uncertain:
            operation.state = OperationState.uncertain.value
        else:
            operation.state = OperationState.failed.value
        operation.detail = receipt.detail
        row = DeliveryReceiptRow(
            id=new_id("rcpt"),
            operation_id=operation.id,
            channel=connector.connector_id,
            state=receipt.state.value,
            platform_message_id=receipt.platform_message_id,
            detail=receipt.detail,
            observed_at=utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return DeliveryOutcome(
            operation_id=operation.id,
            state=OperationState(operation.state),
            receipt=row,
            detail=receipt.detail or "",
        )

    # ------------------------------------------------------------ 段级幂等

    async def _claim_part(
        self, plan_id: str, ordinal: int
    ) -> tuple[IdempotencyKey | None, bool]:
        """抢占"这一条回复的第 N 段"。返回 `(抢占行, 是否本次抢到)`。

        用现成的 `idempotency_keys` 表（`(scope, key)` 唯一），**不建新表**；
        它是 MaiBot `OutboundTracker.begin_tracking` 的持久化版本
        （那是内存态、TTL 30 分钟、重启即失，见调研 §3.3）。
        """
        key = f"{plan_id}:{ordinal}"
        existing = (
            await self._session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == PART_SCOPE, IdempotencyKey.key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        row = IdempotencyKey(id=new_id("idem"), scope=PART_SCOPE, key=key)
        self._session.add(row)
        try:
            # SAVEPOINT：并发下另一个 worker 可能刚好抢先插入了同一个键。
            # 唯一约束一炸就把这一次插入单独回滚，不能把外层事务整体标脏。
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            existing = (
                await self._session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.scope == PART_SCOPE, IdempotencyKey.key == key
                    )
                )
            ).scalar_one_or_none()
            return existing, False
        return row, True

    async def _replay_outcome(self, claim: IdempotencyKey | None) -> DeliveryOutcome | None:
        """重投时看**上一段**发生了什么。返回 None 表示"可以再发"。"""
        operation: Operation | None = None
        if claim is not None and claim.result_ref:
            operation = await self._session.get(Operation, claim.result_ref)
        if operation is None:
            # 抢占行存在、但找不到它对应的 operation：上一次在
            # "抢占成功、建 operation 之前"中断了。发没发出去无法确认，
            # 只能按 uncertain 处理——**绝不重发**（重发就是重复消息）。
            return DeliveryOutcome(
                operation_id=(claim.result_ref or "") if claim else "",
                state=OperationState.uncertain,
                receipt=None,
                detail="上一次投递在记录 operation 之前中断，结果未知（不重发，等对账）",
                skipped=True,
            )
        try:
            state = OperationState(operation.state)
        except ValueError:  # pragma: no cover - 库里出现了未知状态
            state = OperationState.uncertain
        if state is OperationState.failed:
            # 明确失败 = 那一段确实没发出去（连接器只在"确定没发出"时给 failed）。
            # 允许再发；抢占行会改成指向新的 operation。
            return None
        last = await self._latest_receipt(operation.id)
        if state is OperationState.succeeded:
            return DeliveryOutcome(
                operation_id=operation.id,
                state=state,
                receipt=last,
                detail="这一段上一次已送达，跳过（重投不重发已发段）",
                skipped=True,
            )
        # proposed / running / uncertain：都不能重发。running 尤其危险——
        # 那可能是另一个 worker 正在发，重发就是两条。
        return DeliveryOutcome(
            operation_id=operation.id,
            state=OperationState.uncertain,
            receipt=last,
            detail="这一段上一次结果未知（运行中/不确定），不重发，交给对账",
            skipped=True,
        )

    async def _latest_receipt(self, operation_id: str) -> DeliveryReceiptRow | None:
        return (
            await self._session.execute(
                select(DeliveryReceiptRow)
                .where(DeliveryReceiptRow.operation_id == operation_id)
                .order_by(DeliveryReceiptRow.observed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------ 对账

    async def reconcile(self, connector: ChannelConnector, operation_id: str) -> DeliveryOutcome:
        """对账不确定投递：查平台真实状态，**不重发**。

        收敛规则（`docs/OUTBOUND-DELIVERY-RESEARCH.md` §5 P0-2）：

        - 查到 → ``succeeded``（delivered）
        - 仍不确定 → **保持** ``uncertain``，累加 `reconcile_attempts`、
          写下一次可重试时间（指数退避）；**绝不因为"查不到"就判失败或重发**
        - 超过 `RECONCILE_MAX_AGE_SECONDS`（或次数上限）仍未收敛 →
          标 ``reconcile_expired``（**状态仍是 uncertain**）。这是"我们永远
          不知道了"，不是"没发出去"：改成 failed 会让上游误判成可重发。
        - 平台给出**两个独立否证**（连接器只在那种情况下给 ``failed``）→
          ``failed``。此时仍然**不重发**（重发由调用方明确决定）。
        """
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            raise KeyError(f"operation 不存在: {operation_id}")
        payload = dict(operation.payload or {})
        last = await self._latest_receipt(operation_id)
        receipt = await connector.query_receipt(
            ReceiptQuery(
                operation_id=operation_id,
                platform_message_id=last.platform_message_id if last else None,
                # 没有平台 id 时的兜底证据：会话 + 内容摘要 + 发出时间。
                window_ref=payload.get("window_ref"),
                text_digest=payload.get("text_digest"),
                sent_at=operation.created_at,
                # 发往群聊还是私聊：**这里手上有权威数据，不要让它去推断**。
                # 这个值是发送时写进 payload 的（`:217` 正常投递、`:498`
                # 超时续发的 `delivery.pending` 也会写），来自
                # `OutboundMessage.is_group`——是显式的业务语义。
                #
                # 为什么必须传：不传的话连接器只能从 `window_ref` 里找
                # `":group:"` 反推。字符串是拼接出来的约定，不是受约束的
                # 类型，会误判（见 `ports/channel.py:167` 起的契约说明）。
                # 而查历史接口选错就直接查不到 → 那条消息永远停在
                # `uncertain`，用户看到的是"发出去了但一直不确定"。
                #
                # **绝不写成 `bool(payload.get("is_group"))`**：
                # `bool(None)` 是 `False`，会把这个字段上线前发出的旧
                # operation（payload 里没这个 key）全部当成"确认私聊"，
                # 反而把误判方向固定死。`None` 的语义是"调用方不知道，
                # 请连接器退回自己的推断"——这正是旧数据该走的路。
                # 见 `ports/channel.py` 里 `is_group` 的三态说明，以及
                # 该字段下方「为什么不是 `bool = False`」一节。
                is_group=payload.get("is_group"),
            )
        )
        attempts = int(payload.get("reconcile_attempts") or 0) + 1
        now = datetime.now(UTC)
        expired = _reconcile_expired(operation.created_at, attempts=attempts)
        if receipt.state in {DeliveryState.sent, DeliveryState.delivered}:
            operation.state = OperationState.succeeded.value
            payload.pop("reconcile_next_at", None)
            payload["reconciled"] = True
        elif receipt.state is DeliveryState.uncertain:
            # **保持 uncertain**：既不能算成功，也不能改成可重发的失败态。
            operation.state = OperationState.uncertain.value
            if expired:
                payload["reconcile_expired"] = True
                payload.pop("reconcile_next_at", None)
            else:
                delay = RECONCILE_BACKOFF_SECONDS[
                    min(attempts - 1, len(RECONCILE_BACKOFF_SECONDS) - 1)
                ]
                payload["reconcile_next_at"] = (now + timedelta(seconds=delay)).isoformat()
        else:
            operation.state = OperationState.failed.value
            payload.pop("reconcile_next_at", None)
            payload["reconciled"] = True
        payload["reconcile_attempts"] = attempts
        payload["reconcile_at"] = now.isoformat()
        # JSON 列必须整体赋值才会被 SQLAlchemy 认为"变了"。原地改 dict 会被
        # 静默丢弃（这也是 attempts 计数一开始不生效的那类坑）。
        operation.payload = payload
        row = DeliveryReceiptRow(
            id=new_id("rcpt"),
            operation_id=operation_id,
            channel=connector.connector_id,
            state=receipt.state.value,
            platform_message_id=receipt.platform_message_id,
            detail=f"对账: {receipt.detail or ''}".strip(),
            observed_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return DeliveryOutcome(
            operation_id=operation_id,
            state=OperationState(operation.state),
            receipt=row,
            detail=receipt.detail or "",
        )

    async def record_pending(
        self,
        *,
        connector_id: str | None,
        actor_id: str,
        subject_id: str | None,
        window_ref: str,
        is_group: bool,
        plan_id: str,
        turn_id: str,
        reason: str,
        detail: str,
    ) -> Operation:
        """记一条"**该发但还没发**"的投递（例如生成超时）。

        为什么必须落库而不是只记日志：进程一重启，内存里"还有话没发出去"
        这件事就没了。DB 里有一条 `kind='delivery.pending'` 的行，周期对账
        任务就能把它捡起来续发（续发靠段级幂等，天然不会重复）。
        """
        return await self.create_operation(
            kind="delivery.pending",
            actor_id=actor_id,
            subject_id=subject_id,
            payload={
                "channel": connector_id,
                "window_ref": window_ref,
                "is_group": is_group,
                "reply_plan": plan_id,
                "turn_id": turn_id,
                "reason": reason,
                "detail": detail[:500],
            },
        )


    async def _unsupported(
        self,
        message: OutboundMessage,
        actor_id: str,
        subject_id: str | None,
        detail: str,
        *,
        error_code: ErrorCode = ErrorCode.unsupported_capability,
    ) -> DeliveryOutcome:
        operation = await self.create_operation(
            kind="delivery",
            actor_id=actor_id,
            subject_id=subject_id,
            payload={"window_ref": message.window_ref, "unsupported": detail},
        )
        operation.state = OperationState.failed.value
        operation.error_code = error_code.value
        operation.detail = detail
        row = DeliveryReceiptRow(
            id=new_id("rcpt"),
            operation_id=operation.id,
            channel="unknown",
            state=DeliveryState.unsupported.value,
            detail=detail,
            observed_at=utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return DeliveryOutcome(
            operation_id=operation.id,
            state=OperationState.failed,
            receipt=row,
            detail=detail,
        )

def reconcile_due(payload: dict, *, now: datetime | None = None) -> bool:
    """这条 uncertain operation 现在该不该再对一次账。

    依据 `payload` 里由 :meth:`DeliveryService.reconcile` 写下的
    `reconcile_next_at` / `reconcile_expired`，**不另存状态**：
    operations 表没有专门的列，而加列要迁移（迁移不在本次改动范围）。
    """
    moment = now or datetime.now(UTC)
    if payload.get("reconcile_expired"):
        return False
    next_at = payload.get("reconcile_next_at")
    if not next_at:
        return True
    try:
        due = datetime.fromisoformat(str(next_at))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return moment >= due


def _reconcile_expired(created_at: datetime | None, *, attempts: int) -> bool:
    if attempts >= RECONCILE_MAX_ATTEMPTS:
        return True
    if created_at is None:
        return False
    moment = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - moment).total_seconds()
    return age >= RECONCILE_MAX_AGE_SECONDS

__all__ = ["PART_SCOPE", "DeliveryOutcome", "DeliveryService", "reconcile_due"]
