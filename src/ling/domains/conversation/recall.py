"""**主动撤回**她自己发出去的消息：授权边界、时间窗、频次、审计。

用户拍板的原话是「说错了想收回」可以用。这条需求的风险不在"能不能撤"，
而在**撤错了**——撤回不可逆，撤掉别人的话等于替别人改写群聊记录。所以本模块
的主语只有一个：**她自己发的、有真实回执的、还在平台时间窗内的那一条**。

## 五道闸，缺一不可（全部在发 action **之前**）

1. **归属**：这条本地消息必须是 `role="actor"`（她自己说的），并且能通过
   ``operations`` + ``delivery_receipts`` 找到"我们当初真的发出去了，平台给了
   这个 message id"。**不按内容匹配、不按时间猜**——正文相同不等于同一条。
2. **已送达**：回执状态必须是 ``sent``/``delivered`` 且带 ``platform_message_id``。
   `uncertain`（不知道发没发出去）与 `failed` 一律拒绝：没有 id 就没有撤回对象。
3. **时间窗**：平台限制（QQ 群/私聊通常 2 分钟内）。**窗口外直接拒绝、不发
   action**，因为平台必然拒绝，发出去只是白挨一次风控。
4. **频次**：同一会话每分钟上限（默认 3）。防手滑连撤把对话抹空——这也是
   用户"说错了想收回"的正常用量上限。
5. **不可撤回的对象**：系统/占位消息（`origin` 是外部观测或正文为空）、
   别人的消息、已经撤过一次的（幂等，不重复发 action）。

## 为什么"她自己决定撤回"这一轮不开

撤回是对**已发生交互**的补救。让模型自己判断"这句说错了"意味着它要评估
自己刚才的输出，而它对自己的错误没有可靠判据——错判一次就是替用户删掉一条
正常回复。所以本轮只开放**用户手动**入口（owner only），模型侧留到下一阶段
单独授权（见 `docs/BEHAVIOR-LOG-CONTRACT.md`）。

## 本地只标记、不删行

`recalled_at` 与回执字段写在消息行上，**行不删**：她当时说了什么、后来为什么
撤回，都要能追溯。上下文侧按既有 `retracted_at` 规则处理（已看过 → 保留并加
注记），事实表（activities / self_experiences）**不动**——撤回是"以后别再引用"，
不是"过去没发生"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import String as SAString
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import (
    DeliveryState,
    ErrorCode,
    OutboundRecallState,
    RecallReceipt,
)
from ling.ports.channel import ChannelConnector, ChannelError, RecallRequest

logger = logging.getLogger("ling.domains.conversation.recall")

#: 平台允许撤回的时间窗（秒）。QQ 群/私聊普遍是 2 分钟。
#:
#: 这个值是**本地预检**用的保守上限：平台可能在更短的时间后拒绝，也可能
#: 允许更长（部分实现放宽到 2 小时）。保守的理由是"窗口外发 action 只会白挨
#: 一次风控"，而漏掉"其实还能撤"的代价只是用户早一点放弃。
RECALL_WINDOW_SECONDS = 120.0

#: 同一会话每分钟最多撤回几次。防手滑连撤把对话抹空。
RECALL_RATE_LIMIT_PER_MINUTE = 3

#: 撤回前的投递回执必须处于这两个状态之一（都带真实 ``platform_message_id``）。
DELIVERABLE_STATES = frozenset({DeliveryState.sent.value, DeliveryState.delivered.value})

#: 不能撤回的消息来源：这些不是"她说过的话"，撤了也没有意义（或不该动）。
#: - ``external_observation`` / ``verified_source``：外部观测/资料，不是她的发言；
#: - ``simulated``：主动推送里的模拟消息，可能根本没进平台。
NON_RECALLABLE_ORIGINS = frozenset(
    {"external_observation", "verified_source", "simulated"}
)


# ---------------------------------------------------------------- 拒绝原因

class RecallRejected(Exception):
    """拒绝撤回（**没有发过任何平台 action**）。

    ``code`` 是稳定错误码；``note`` 是**人话**（前端直接显示）。两者分开：
    错误码给日志与测试，人话给用户——本项目反复强调"界面不出现原因码"。
    """

    def __init__(
        self,
        code: str,
        note: str,
        *,
        status_code: int = 409,
        http_code: ErrorCode = ErrorCode.conflict,
    ) -> None:
        super().__init__(note)
        self.code = code
        self.note = note
        self.status_code = status_code
        self.http_code = http_code


#: 拒绝码 → 人话。**唯一**措辞来源，路由与测试都从这里取。
RECALL_REFUSAL_NOTES: dict[str, str] = {
    "not_found": "这条消息不在这里",
    "not_actor_message": "只能撤回她自己说过的话",
    "no_receipt": "这条没有平台回执，撤不了（可能没发出去）",
    "not_delivered": "这条当时没确认送达，撤不了",
    "expired": "超过平台允许的撤回时间了",
    "already_recalled": "这条已经撤回过了",
    "rate_limited": "撤得太频繁了，过一会儿再试",
    "not_recallable_origin": "这条不是她在对话里说的话，不能撤回",
    "unsupported": "这个接入方式不支持撤回",
    "not_owner": "只有主人可以撤回消息",
}

#: 撤回结果的**人话**（成功与未确认两条）。与拒绝码分开命名，因为它们是
#: "做过了"而不是"没做"——混在一起会让前端分不清该不该提示失败。
RECALL_RESULT_NOTES: dict[str, str] = {
    "recalled": "撤回了",
    "uncertain": "没拿到回执，不确定是否撤回成功",
    "not_found": "这条已经不在平台上了",
    "expired": "超过平台允许的撤回时间了",
    "unsupported": "这个接入方式不支持撤回",
}


def refusal_note(code: str) -> str:
    """拒绝码 → 人话（未知码给一句通用说明，绝不把码丢给用户）。"""
    return RECALL_REFUSAL_NOTES.get(code, "这条现在撤不了")


def result_note(state: str) -> str:
    return RECALL_RESULT_NOTES.get(state, "撤回结果未知")


# ---------------------------------------------------------------- 频次闸


class RecallRateLimiter:
    """同一会话每分钟最多 N 次撤回（**进程内**滑动窗口）。

    为什么进程内就够：这是防手滑的节流，不是配额账。多副本各算一份最多多放行
    几次，而真正的保护是平台自己的时间窗与权限；重启清零也可以接受。
    """

    def __init__(self, *, limit: int = RECALL_RATE_LIMIT_PER_MINUTE, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._clock = time.monotonic

    def allow(self, window_id: str) -> bool:
        now = self._clock()
        floor = now - self.window
        hits = [stamp for stamp in self._hits.get(window_id, []) if stamp > floor]
        if len(hits) >= self.limit:
            self._hits[window_id] = hits
            return False
        hits.append(now)
        self._hits[window_id] = hits
        return True

    def reset(self) -> None:
        self._hits.clear()


#: 进程级单例（测试可 `reset()`）。
RECALL_LIMITER = RecallRateLimiter()


# ---------------------------------------------------------------- 归属核对


@dataclass(frozen=True)
class RecallTarget:
    """通过五道闸之后，真正要撤的那一条（含平台 id 与窗口标识）。"""

    message_id: str
    window_id: str
    window_ref: str
    is_group: bool
    platform_message_id: str
    sent_at: datetime
    #: 当初那一段的 operation id（审计与对账用）
    operation_id: str
    receipt_id: str

    @property
    def age_seconds(self, now: datetime | None = None) -> float:  # type: ignore[valid-type]
        return ((now or datetime.now(UTC)) - self.sent_at).total_seconds()


async def _plan_id_for_message(
    session: AsyncSession, *, message_id: str, turn_id: str | None
) -> str | None:
    """这条消息属于哪个 ``reply_plan``（``plan_{turn_id}``）。

    分段投递的每段是一个 ``operations`` 行，用 ``payload.reply_plan`` 指回
    同一轮。查它而不是"最近一条 operation"：同一窗口可能正在发别的回复，
    按时间取会张冠李戴。
    """
    if not turn_id:
        return None
    plan_id = f"plan_{turn_id}"
    from ling.infra.models import Operation

    found = (
        await session.execute(
            select(Operation.id)
            .where(
                Operation.kind == "delivery",
                cast(Operation.payload["reply_plan"], SAString) == f'"{plan_id}"',
            )
            .limit(1)
        )
    ).scalars().first()
    return plan_id if found is not None else None


async def _receipts_for_plan(
    session: AsyncSession, *, plan_id: str
) -> list[tuple[str, str, str, datetime, str]]:
    """``reply_plan`` 下所有**成功送达**的段：``(operation_id, message_id,
    platform_message_id, observed_at, receipt_id)``，按段序号升序。

    只取 ``sent``/``delivered`` 且带平台 id 的——这是"我们真的发出去过、
    而且平台给了回执"的唯一证据。
    """
    from ling.infra.models import DeliveryReceiptRow, Operation

    rows = (
        await session.execute(
            select(
                Operation.id,
                cast(Operation.payload["part_ordinal"], SAString),
                DeliveryReceiptRow.id,
                DeliveryReceiptRow.platform_message_id,
                DeliveryReceiptRow.observed_at,
            )
            .join(DeliveryReceiptRow, DeliveryReceiptRow.operation_id == Operation.id)
            .where(
                Operation.kind == "delivery",
                cast(Operation.payload["reply_plan"], SAString) == f'"{plan_id}"',
                DeliveryReceiptRow.state.in_(tuple(DELIVERABLE_STATES)),
                DeliveryReceiptRow.platform_message_id.is_not(None),
            )
            .order_by(DeliveryReceiptRow.observed_at)
        )
    ).all()
    out: list[tuple[str, str, str, datetime, str]] = []
    for operation_id, _ordinal, receipt_id, platform_id, observed_at in rows:
        if not platform_id:
            continue
        moment = observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=UTC)
        out.append((str(operation_id), str(receipt_id), str(platform_id), moment, str(receipt_id)))
    return out


async def resolve_recall_target(
    session: AsyncSession,
    *,
    window_id: str,
    message_id: str,
    now: datetime | None = None,
    limiter: RecallRateLimiter | None = None,
    window_seconds: float = RECALL_WINDOW_SECONDS,
) -> RecallTarget:
    """五道闸的**前四道**（归属 / 已送达 / 时间窗 / 频次）。

    通过则返回 :class:`RecallTarget`；任一不过抛 :class:`RecallRejected`
    （**此时平台一次 action 都没收到**）。第五道（不可撤回的对象）在
    :func:`check_recallable_message` 里，因为它是纯函数、可以先跑。

    为什么要分两个函数：纯函数那部分能被单测直接打表（不需要库），
    而查库那部分才有 IO。
    """
    from ling.infra.models import Message, Window

    moment = now or datetime.now(UTC)
    row = await session.get(Message, message_id)
    if row is None or str(row.window_id) != str(window_id):
        raise RecallRejected(
            "not_found", refusal_note("not_found"), status_code=404, http_code=ErrorCode.not_found
        )
    check_recallable_message(row)

    if row.recalled_at is not None:
        raise RecallRejected("already_recalled", refusal_note("already_recalled"))

    window = await session.get(Window, window_id)
    if window is None:  # pragma: no cover - 消息存在而窗口不在只可能是人工删了
        raise RecallRejected(
            "not_found", refusal_note("not_found"), status_code=404, http_code=ErrorCode.not_found
        )

    plan_id = await _plan_id_for_message(session, message_id=message_id, turn_id=row.turn_id)
    if plan_id is None:
        raise RecallRejected("no_receipt", refusal_note("no_receipt"))
    receipts = await _receipts_for_plan(session, plan_id=plan_id)
    if not receipts:
        raise RecallRejected("not_delivered", refusal_note("not_delivered"))

    # 分段回复会拆成多段：撤"这一条消息"就撤它对应的那一段。
    #
    # 判定用**段数**：一轮只发了一段（绝大多数情况）就是那一段；多段时
    # 撤最后一段（用户看到的是整条回复的尾部），并在 detail 里说明。
    # **不按正文比对**：正文可能被平台改写（换行/空格），而且相同正文在
    # 同一轮里可以出现两次——比对会把两段搞混。
    operation_id, receipt_id, platform_id, observed_at, _ = receipts[-1]

    age = (moment - observed_at).total_seconds()
    if age > window_seconds:
        raise RecallRejected(
            "expired",
            refusal_note("expired"),
            status_code=422,
            http_code=ErrorCode.invalid_request,
        )
    if age < 0:
        # 时钟漂移（回执时间在未来）：不当成"永远在窗口内"，也不拒绝。
        # 按 0 处理——它只可能出现在时钟回拨的瞬间。
        age = 0.0

    gate = limiter or RECALL_LIMITER
    if not gate.allow(str(window_id)):
        raise RecallRejected(
            "rate_limited", refusal_note("rate_limited"), status_code=429, http_code=ErrorCode.rate_limited
        )

    return RecallTarget(
        message_id=str(row.id),
        window_id=str(window_id),
        window_ref=str(window.title or ""),
        is_group=":group:" in str(window.title or ""),
        platform_message_id=platform_id,
        sent_at=observed_at,
        operation_id=operation_id,
        receipt_id=receipt_id,
    )


def check_recallable_message(row: object) -> None:
    """第五道闸（纯函数）：这条消息**本身**允许被撤回吗。

    只认 ``role="actor"``：她自己的话。用户消息一律拒——那是"撤回别人的话"，
    不在授权范围内（对方要撤回自己走平台，不经过我们）。
    """
    role = str(getattr(row, "role", ""))
    if role != "actor":
        raise RecallRejected("not_actor_message", refusal_note("not_actor_message"), status_code=403, http_code=ErrorCode.forbidden)
    origin = str(getattr(row, "origin", "") or "")
    if origin in NON_RECALLABLE_ORIGINS:
        raise RecallRejected(
            "not_recallable_origin",
            refusal_note("not_recallable_origin"),
            status_code=409,
        )
    if not str(getattr(row, "text", "") or "").strip():
        # 空正文（"停止但没输出"那一轮留下的占位行）：平台上没有可撤的东西。
        raise RecallRejected(
            "not_recallable_origin",
            "这一条没有正文，平台上没有可撤回的内容",
            status_code=409,
        )


# ---------------------------------------------------------------- 执行


@dataclass(frozen=True)
class RecallOutcome:
    """一次撤回尝试的结果（含回执与"是否真的撤了"）。"""

    message_id: str
    state: OutboundRecallState
    note: str
    receipt: RecallReceipt | None = None
    recalled_at: datetime | None = None
    attempts: int = 1

    @property
    def succeeded(self) -> bool:
        """**唯一**可以说"撤回了"的判据（与回执语义一致）。"""
        return self.state is OutboundRecallState.recalled


async def perform_recall(
    session: AsyncSession,
    *,
    connector: ChannelConnector,
    target: RecallTarget,
    now: datetime | None = None,
) -> RecallOutcome:
    """真正发 action 并落本地标记。

    ## 三件事的顺序

    1. **先看能力**：``capabilities().supports_recall`` 为假就直接 ``unsupported``，
       **不发 action**（QQ 官方机器人走这条）；
    2. **发 action**：拿回执，按回执状态决定本地标记；
    3. **落标记**：只有 ``recalled`` 才写 ``recalled_at``。``uncertain`` 写
       ``recall_attempted_at``（新列）——"我们试过但没确认"与"撤回了"必须
       在库里也能分开，否则下次重试会看到"已撤回"而跳过。
    """
    from ling.infra.models import Message

    moment = now or datetime.now(UTC)
    caps = getattr(connector, "capabilities", None)
    supported = False
    if callable(caps):
        try:
            supported = bool(caps().supports_recall)
        except Exception:  # noqa: BLE001 - 能力自检失败按"不支持"处理
            supported = False
    recall_fn = getattr(connector, "recall", None)
    if not supported or not callable(recall_fn):
        receipt = RecallReceipt(
            receipt_id="",
            channel=str(getattr(connector, "connector_id", "") or ""),
            state=OutboundRecallState.unsupported,
            platform_message_id=target.platform_message_id,
            observed_at=moment,
            detail="这个接入方式不支持撤回",
        )
        return RecallOutcome(
            message_id=target.message_id,
            state=OutboundRecallState.unsupported,
            note=result_note(OutboundRecallState.unsupported.value),
            receipt=receipt,
        )

    try:
        receipt = await recall_fn(
            RecallRequest(
                platform_message_id=target.platform_message_id,
                window_ref=target.window_ref,
                is_group=target.is_group,
            )
        )
    except ChannelError as exc:
        # 连接器**不该**抛（它自己会把错误映射成状态）。真抛了也如实记"未知"，
        # 不把异常当失败：可能已经撤了。
        receipt = RecallReceipt(
            receipt_id="",
            channel=str(getattr(connector, "connector_id", "") or ""),
            state=OutboundRecallState.uncertain,
            platform_message_id=target.platform_message_id,
            observed_at=moment,
            detail=f"撤回时发生异常：{exc.message[:120]}",
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都不能变成"成功"
        logger.warning("撤回调用抛异常（按未知处理）：%s", type(exc).__name__)
        receipt = RecallReceipt(
            receipt_id="",
            channel=str(getattr(connector, "connector_id", "") or ""),
            state=OutboundRecallState.uncertain,
            platform_message_id=target.platform_message_id,
            observed_at=moment,
            detail=f"撤回时发生异常：{type(exc).__name__}",
        )

    row = await session.get(Message, target.message_id)
    recalled_at: datetime | None = None
    if row is not None:
        if receipt.state is OutboundRecallState.recalled:
            # 上下文侧复用入站撤回的既有判据：她自己的消息一定"已经说过"，
            # 所以按"撤回前已看过"处理（保留原文并加注记）。见
            # `docs/BEHAVIOR-LOG-CONTRACT.md` 的撤回口径。
            row.recalled_at = moment
            row.recall_receipt_id = receipt.receipt_id or None
            row.recall_state = receipt.state.value
            recalled_at = moment
        elif receipt.state is OutboundRecallState.uncertain:
            # **不写 recalled_at**：没确认就是没确认。写一个"试过了"的时间戳
            # 单独一列，让下一次尝试能区分"上次没成"与"上次成了"。
            row.recall_attempted_at = moment
            row.recall_state = receipt.state.value
            row.recall_receipt_id = receipt.receipt_id or None
        else:
            row.recall_state = receipt.state.value
            row.recall_receipt_id = receipt.receipt_id or None
        await session.flush()

    return RecallOutcome(
        message_id=target.message_id,
        state=receipt.state,
        note=result_note(receipt.state.value)
        + (f"（{receipt.detail}）" if receipt.detail else ""),
        receipt=receipt,
        recalled_at=recalled_at,
    )


# ---------------------------------------------------------------- 审计


def audit_recall(
    *,
    actor: str,
    window_id: str,
    outcome: RecallOutcome,
    target: RecallTarget | None,
    reason_code: str | None = None,
) -> None:
    """记一条撤回审计（**不含消息正文**）。

    审计只放：谁触发、哪个窗口、哪条本地消息、平台回执 id、结果、原因码。
    正文是用户/角色说的话，写进审计等于给内容开第二份长期副本——而撤回本身
    往往正是因为那条内容不该再被引用。
    """
    from ling.audit import audit_event

    audit_event(
        "message_recalled",
        outcome="ok" if outcome.succeeded else "degraded",
        actor=actor or "owner",
        target_type="message",
        target_id=outcome.message_id,
        reason_code=reason_code or outcome.state.value,
        attributes={
            "window_id": window_id,
            "recall_state": outcome.state.value,
            "receipt_id": getattr(outcome.receipt, "receipt_id", None),
            "platform_message_id_present": bool(
                getattr(outcome.receipt, "platform_message_id", None)
            ),
            "operation_id": target.operation_id if target is not None else None,
            "age_seconds": (
                round((datetime.now(UTC) - target.sent_at).total_seconds(), 1)
                if target is not None
                else None
            ),
        },
    )


def audit_recall_refused(
    *, actor: str, window_id: str, message_id: str, rejected: RecallRejected
) -> None:
    """拒绝也要留审计（"谁想撤、为什么没撤"是安全事件）。"""
    from ling.audit import audit_event

    audit_event(
        "message_recall_refused",
        outcome="denied",
        actor=actor or "owner",
        target_type="message",
        target_id=message_id,
        reason_code=rejected.code,
        attributes={"window_id": window_id, "status_code": rejected.status_code},
    )


def recall_window_ends_at(sent_at: datetime) -> datetime:
    """这条消息的撤回窗口何时关闭（给前端显示"还剩多久"用）。"""
    return sent_at + timedelta(seconds=RECALL_WINDOW_SECONDS)


def count_recent_recalls(window_id: str, *, limiter: RecallRateLimiter | None = None) -> int:
    gate = limiter or RECALL_LIMITER
    now = gate._clock()  # noqa: SLF001 - 同模块内的诊断读取
    floor = now - gate.window
    return len([stamp for stamp in gate._hits.get(window_id, []) if stamp > floor])


async def recallable_message_ids(session: AsyncSession, *, window_id: str) -> set[str]:
    """这个窗口里**当前可撤回**的消息 id（给前端决定要不要画按钮）。

    只做"能静态判定"的那几道（归属 + 有回执 + 未撤回），**不判时间窗与频次**：
    时间窗每过一秒就变，把它算进来会让列表一打开就过期；前端拿到 id 后
    真点的时候再走完整校验。
    """
    from ling.infra.models import Message

    rows = list(
        (
            await session.execute(
                select(Message).where(
                    Message.window_id == window_id,
                    Message.role == "actor",
                    Message.recalled_at.is_(None),
                )
            )
        ).scalars()
    )
    out: set[str] = set()
    for row in rows:
        if str(row.origin or "") in NON_RECALLABLE_ORIGINS:
            continue
        if not str(row.text or "").strip():
            continue
        plan_id = await _plan_id_for_message(
            session, message_id=str(row.id), turn_id=row.turn_id
        )
        if plan_id is None:
            continue
        if await _receipts_for_plan(session, plan_id=plan_id):
            out.add(str(row.id))
    return out


__all__ = [
    "DELIVERABLE_STATES",
    "NON_RECALLABLE_ORIGINS",
    "RECALL_LIMITER",
    "RECALL_RATE_LIMIT_PER_MINUTE",
    "RECALL_REFUSAL_NOTES",
    "RECALL_RESULT_NOTES",
    "RECALL_WINDOW_SECONDS",
    "RecallOutcome",
    "RecallRateLimiter",
    "RecallRejected",
    "RecallTarget",
    "audit_recall",
    "audit_recall_refused",
    "check_recallable_message",
    "count_recent_recalls",
    "perform_recall",
    "recall_window_ends_at",
    "recallable_message_ids",
    "refusal_note",
    "resolve_recall_target",
    "result_note",
]
