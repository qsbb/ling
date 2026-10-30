"""Delivery：按渠道能力渲染、发送、记录本地 operation/回执并与平台回执对账。

不变式（设计文档 §1 第 6 条）：
- 计划不是结果。只有拿到平台真实回执才能标 ``succeeded``。
- 结果不确定标 ``uncertain`` 并进入对账，**不重发**不可逆动作。
- 渠道没有能力时返回 ``unsupported``，不假装已联系。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import DeliveryState, OperationState
from ling.infra.models import DeliveryReceiptRow, Operation, utcnow
from ling.infra.security import new_id
from ling.ports.channel import (
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    ReceiptQuery,
)


@dataclass(frozen=True)
class DeliveryOutcome:
    # 本地 operations 行主键。不是平台侧幂等键：connector 不透传 operation_id。
    operation_id: str
    state: OperationState
    receipt: DeliveryReceiptRow | None
    detail: str

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
        if message.media_refs and not caps.supports_media:
            return await self._unsupported(
                message,
                actor_id,
                subject_id,
                f"渠道不支持发送媒体（{len(message.media_refs)} 个媒体引用未发送）",
            )
        if len(message.text) > caps.max_text_length:
            return await self._unsupported(
                message,
                actor_id,
                subject_id,
                f"文本超出渠道上限（{len(message.text)} > {caps.max_text_length}）",
            )

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
                **dict(payload_extra or {}),
            },
        )
        operation.state = OperationState.running.value
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

    async def reconcile(self, connector: ChannelConnector, operation_id: str) -> DeliveryOutcome:
        """对账不确定投递：查平台真实状态，不重发。"""
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            raise KeyError(f"operation 不存在: {operation_id}")
        last = (
            await self._session.execute(
                select(DeliveryReceiptRow)
                .where(DeliveryReceiptRow.operation_id == operation_id)
                .order_by(DeliveryReceiptRow.observed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        receipt = await connector.query_receipt(
            ReceiptQuery(
                operation_id=operation_id,
                platform_message_id=last.platform_message_id if last else None,
            )
        )
        if receipt.state in {DeliveryState.sent, DeliveryState.delivered}:
            operation.state = OperationState.succeeded.value
        elif receipt.state is DeliveryState.uncertain:
            operation.state = OperationState.uncertain.value
        else:
            operation.state = OperationState.failed.value
        row = DeliveryReceiptRow(
            id=new_id("rcpt"),
            operation_id=operation_id,
            channel=connector.connector_id,
            state=receipt.state.value,
            platform_message_id=receipt.platform_message_id,
            detail=f"对账: {receipt.detail or ''}".strip(),
            observed_at=datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return DeliveryOutcome(
            operation_id=operation_id,
            state=OperationState(operation.state),
            receipt=row,
            detail=receipt.detail or "",
        )

    async def _unsupported(
        self,
        message: OutboundMessage,
        actor_id: str,
        subject_id: str | None,
        detail: str,
    ) -> DeliveryOutcome:
        operation = await self.create_operation(
            kind="delivery",
            actor_id=actor_id,
            subject_id=subject_id,
            payload={"window_ref": message.window_ref, "unsupported": detail},
        )
        operation.state = OperationState.failed.value
        operation.error_code = "unsupported_capability"
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


__all__ = ["DeliveryOutcome", "DeliveryService"]
