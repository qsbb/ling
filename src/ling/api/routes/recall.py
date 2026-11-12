"""主动撤回她自己发出去的消息：`POST /v1/sessions/{id}/messages/{mid}/recall`。

`/v1/conversations/...` 保留为兼容别名（不进 OpenAPI），因为仓库其余端点都叫 sessions。

## 为什么是一个独立路由模块

撤回是**不可逆**的破坏性动作，它的授权边界（只撤自己的、时间窗、频次、
不可撤回对象）与对话路由完全是两套判据。塞进 `chat.py` 会让"读消息/发消息"
那条主链路多出一组与它无关的分支，而授权代码散在热路径里迟早被顺手改松。

## 谁能触发

**owner only**。本轮**不**开放给模型自主调用（理由见
`domains/conversation/recall.py` 的模块注释：撤回是对已发生交互的补救，
不能让模型揣测自己的输出后替用户删掉一条正常回复）。

## 拒绝时**不发任何平台 action**

五道闸（归属 / 已送达 / 时间窗 / 频次 / 不可撤回对象）全部在发 action 之前。
所以这里的 4xx 一定意味着"平台那边什么都没发生"——这也是验收里
"平台调用次数 = 0"那条断言的对象。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select, update

from ling.api.deps import ConversationDep, CurrentAuth, DbSession
from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode, RecallMessageOut, RecallProposalOut
from ling.domains.conversation.recall import (
    RECALL_LIMITER,
    RecallRejected,
    audit_recall,
    audit_recall_refused,
    perform_recall,
    resolve_recall_target,
)

logger = logging.getLogger("ling.api.recall")

router = APIRouter(tags=["conversations"])


class RecallContext(BaseModel):
    """（保留位）撤回的可选参数。

    目前没有任何可选项：撤回的判据全是服务端事实（归属/回执/时间窗），
    不接受客户端指定"平台 id"或"跳过时间窗"——那等于把边界交给调用方。
    保留这个类是为了将来加"原因"之类的**非安全**字段时有地方放。
    """


def _connector_for_window(request: Request, window_title: str) -> object | None:
    """用平台窗口的账号命名空间选择原实例。"""
    from ling.capabilities.channels import connector_for_namespace

    namespace = window_title.split(":", 1)[0]
    for scope in ("onebot", "qqbot", "qq"):
        connector = connector_for_namespace(request.app, scope, namespace)
        if connector is not None:
            return connector
    return None


@router.post(
    "/sessions/{conversation_id}/messages/{message_id}/recall",
    response_model=RecallMessageOut,
)
@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/recall",
    response_model=RecallMessageOut,
    include_in_schema=False,  # 兼容别名：早期文档里用过 conversations，别让旧调用方断掉
)
async def recall_message(
    conversation_id: str,
    message_id: str,
    auth: CurrentAuth,
    conversation: ConversationDep,
    session: DbSession,
    request: Request,
) -> RecallMessageOut:
    """撤回**她自己**的一条消息（owner only）。

    状态码：

    - ``200`` + ``state=recalled``：平台确认撤回；
    - ``200`` + ``state=uncertain``：没拿到回执，**不确定**是否撤回（**不是成功**）；
    - ``200`` + ``state=expired/not_found/unsupported``：平台侧的回答（已尝试）；
    - ``403`` 非 owner / 不是她的发言；
    - ``404`` 这条消息不在这个会话里；
    - ``409`` 没有回执、未确认送达、已撤回、不是对话里的发言；
    - ``422`` 超过平台允许的撤回时间窗（**不发 action**）；
    - ``429`` 同一会话撤得太频繁（**不发 action**）。

    所有拒绝都带 ``note``（人话），``reason_code`` 只给日志与测试。
    """
    if str(getattr(auth.user, "role", "")) != "owner":
        raise LingError(
            ErrorCode.forbidden,
            "只有主人可以撤回消息",
            status_code=403,
            detail="not_owner",
        )

    # 归属校验：窗口必须属于当前登录者（跨用户一律 404，不泄露存在性）。
    window = await conversation.get_window(conversation_id, subject_id=auth.user.subject_id)

    try:
        target = await resolve_recall_target(
            session, window_id=conversation_id, message_id=message_id
        )
    except RecallRejected as rejected:
        audit_recall_refused(
            actor=str(auth.user.subject_id),
            window_id=conversation_id,
            message_id=message_id,
            rejected=rejected,
        )
        raise LingError(
            rejected.http_code,
            rejected.note,
            status_code=rejected.status_code,
            detail=rejected.code,
        ) from rejected

    connector = _connector_for_window(request, str(window.title or ""))
    if connector is None:
        # 没有连接器 = 这个渠道现在不在这儿（例如纯 Web 会话）。
        # **不发 action**，也不当失败：如实说"这个接入方式不支持撤回"。
        from ling.contracts.v1 import OutboundRecallState
        from ling.domains.conversation.recall import result_note

        audit_recall(
            actor=str(auth.user.subject_id),
            window_id=conversation_id,
            outcome=_unsupported_outcome(message_id),
            target=target,
            reason_code="unsupported",
        )
        await session.commit()
        return RecallMessageOut(
            message_id=message_id,
            state=OutboundRecallState.unsupported,
            note=result_note(OutboundRecallState.unsupported.value),
            reason_code="unsupported",
        )

    outcome = await perform_recall(
        session, connector=connector, target=target  # type: ignore[arg-type]
    )
    await session.commit()
    audit_recall(
        actor=str(auth.user.subject_id),
        window_id=conversation_id,
        outcome=outcome,
        target=target,
    )
    if not outcome.succeeded:
        logger.info(
            "撤回未确认：window=%s message=%s state=%s",
            conversation_id,
            message_id,
            outcome.state.value,
        )
    return RecallMessageOut(
        message_id=message_id,
        state=outcome.state,
        note=outcome.note,
        recalled_at=outcome.recalled_at,
        receipt_id=getattr(outcome.receipt, "receipt_id", None) or None,
        attempts=outcome.attempts,
        reason_code=None if outcome.succeeded else outcome.state.value,
    )


def _unsupported_outcome(message_id: str):  # noqa: ANN202
    """没有连接器时的"结果"：不发 action，状态 unsupported。"""
    from ling.contracts.v1 import OutboundRecallState, RecallReceipt
    from ling.domains.conversation.recall import RecallOutcome
    from ling.infra.models import utcnow

    return RecallOutcome(
        message_id=message_id,
        state=OutboundRecallState.unsupported,
        note="这个接入方式不支持撤回",
        receipt=RecallReceipt(
            receipt_id="",
            channel="",
            state=OutboundRecallState.unsupported,
            observed_at=utcnow(),
            detail="没有可用的连接器",
        ),
    )


# ---------------------------------------------------------------- 模型提议的确认
#
# 模型不能直接撤（见 `domains/conversation/recall.py` 模块注释）。她能做的只有
# 在回复里写 `<<撤回 rXXXX>>`，代码把它落成一条**待确认提议**；下面两个端点
# 是"列出待确认的"与"用户点确认 / 拒绝"。
#
# 为什么确认走 `POST /v1/tools/message.recall/call`（`manage.py` 那个通用工具端点）
# 而不在这里再开一个 `/confirm`：
# - 工具注册表已经实现了"`confirmed=False` 就不执行 handler"的语义；
# - 权限硬门（PolicyGate）也在那条路上；
# - 再开一条就等于给同一件事写第二个执行入口，两者迟早判据不一致
#   （而"哪条路判松了"正是最难发现的错）。
# 所以这里只负责"把待确认项列出来 / 把她说不要的那条划掉"。


@router.get(
    "/sessions/{conversation_id}/recall-proposals",
    response_model=list[RecallProposalOut],
)
async def list_recall_proposals(
    conversation_id: str,
    auth: CurrentAuth,
    conversation: ConversationDep,
    session: DbSession,
) -> list[RecallProposalOut]:
    """她提议撤回、还没被处理的那些（给网页画「确认吗？」用）。

    只返回 ``state='proposed'`` 的：``confirmed`` / ``declined`` 已经是历史，
    再显示在确认框里会让人重复点。
    """
    await conversation.get_window(conversation_id, subject_id=auth.user.subject_id)
    from ling.infra.models import Operation
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    rows = list(
        (
            await session.execute(
                select(Operation)
                .where(
                    Operation.kind == RECALL_PROPOSAL_KIND,
                    Operation.state == "proposed",
                )
                .order_by(Operation.created_at.desc())
            )
        ).scalars()
    )
    out: list[RecallProposalOut] = []
    for row in rows:
        payload = dict(row.payload or {})
        # 窗口归属在**这里**再判一次：`operations` 行不带 window_id 列
        # （它在 payload 里），SQL 过滤不了，所以只能读出来再筛。
        # 跨用户/跨窗口的提议绝不能出现在别人的列表里。
        if str(payload.get("window_id") or "") != conversation_id:
            continue
        out.append(
            RecallProposalOut(
                proposal_id=str(row.id),
                message_id=str(payload.get("message_id") or ""),
                window_id=conversation_id,
                note="她要撤回这条，确认吗？",
                created_at=row.created_at,
            )
        )
    return out


@router.post(
    "/sessions/{conversation_id}/recall-proposals/{proposal_id}/confirm",
    response_model=RecallMessageOut,
)
async def confirm_recall_proposal(
    conversation_id: str,
    proposal_id: str,
    auth: CurrentAuth,
    conversation: ConversationDep,
    session: DbSession,
    request: Request,
) -> RecallMessageOut:
    """用户点确认 → **这时才**真的执行撤回。

    ## 为什么走 `ToolRegistry.call(confirmed=True)`

    撤回是注册给模型的**工具**（`message.recall`，`requires_confirmation=True`）。
    用户点确认正是那个 `confirmed` 的来源。所以这里不另写一套执行逻辑，而是
    用 `confirmed=True` 调同一个工具——**同一条 handler、同一套五道闸**。

    如果这里自己拼一遍 `resolve_recall_target` + `perform_recall`，就出现了
    第二个执行入口；两个入口的判据迟早走散，而"哪条路判松了"是最难发现的错。

    ## 状态码

    与 `POST .../messages/{id}/recall` 完全一致（它们最终跑的是同一段代码），
    区别只在入口：这个入口还要求"确实有一条待确认提议"。
    """
    if str(getattr(auth.user, "role", "")) != "owner":
        raise LingError(
            ErrorCode.forbidden, "只有主人可以确认撤回", status_code=403, detail="not_owner"
        )

    window = await conversation.get_window(conversation_id, subject_id=auth.user.subject_id)

    # **先原子地"占有"这条提议，再执行撤回。**
    #
    # 原来是"`row.state != "proposed"` → 执行 → 改状态"：先读后写，中间没有
    # 任何锁。两个并发 confirm（双击、两个标签页、前端重试）都能通过那一次
    # 检查，于是**两次真实的 delete_msg** 打到平台。撤回不可逆，这不是"多一次
    # 无害请求"，而是把"确认只有一次"这条承诺弄丢了。
    #
    # 用条件更新（`WHERE state='proposed'` + rowcount）而不是 `SELECT … FOR
    # UPDATE`：两行代码解决同一件事，但条件更新**本身就是判据**——谁把 state
    # 从 proposed 改走了，谁就拿到了执行权，另一个人 rowcount=0 直接 409。
    # 加锁版本仍然要读-判-写三步，遗漏一步就又回到原来的问题。
    #
    # `synchronize_session=False`：这一行下面还要按对象读 `payload`，不需要让
    # session 里的对象状态跟着改（改状态放到执行完之后）。
    from sqlalchemy import CursorResult

    from ling.infra.models import Operation
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    claimed = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Operation)
            .where(
                Operation.id == proposal_id,
                Operation.kind == RECALL_PROPOSAL_KIND,
                Operation.state == "proposed",
            )
            .values(state="confirming")
            .execution_options(synchronize_session=False)
        ),
    )
    if claimed.rowcount != 1:
        await session.rollback()
        # 分清"没有这条"与"已经处理过"：前者 404，后者 409。
        row = await session.get(Operation, proposal_id)
        if (
            row is None
            or row.kind != RECALL_PROPOSAL_KIND
            or str(dict(row.payload or {}).get("window_id") or "") != conversation_id
        ):
            raise LingError(ErrorCode.not_found, "没有这条撤回提议", status_code=404)
        raise LingError(
            ErrorCode.conflict,
            "这条撤回提议已经处理过了",
            status_code=409,
            detail=f"state={row.state}",
        )

    # 占有成功之后再读一次拿 payload（这次读是"我拿到执行权之后"的事实）。
    row = await session.get(Operation, proposal_id, populate_existing=True)
    if row is None:  # pragma: no cover - 刚更新过
        raise LingError(ErrorCode.not_found, "没有这条撤回提议", status_code=404)
    if str(dict(row.payload or {}).get("window_id") or "") != conversation_id:
        # 跨窗口的提议：把抢占回滚掉，再 404（不能让别人的提议卡在 confirming）。
        await session.rollback()
        raise LingError(ErrorCode.not_found, "没有这条撤回提议", status_code=404)
    message_id = str(dict(row.payload or {}).get("message_id") or "")
    # 抢占要**立刻落库**：否则另一个请求的事务还看得到 proposed，两次都会
    # 抢到执行权。这一 commit 之后，第二次 confirm 的条件更新必然 rowcount=0。
    #
    # commit 之后不再重新读：会话工厂是 `expire_on_commit=False`，`row` 仍是
    # 同一个可用对象；再 `session.get` 一次只是多一次往返，还会引入一个
    # 不可能为 None 的分支（刚更新过，行一定在）。
    await session.commit()

    registry = getattr(request.app.state, "tools", None)
    if registry is None:
        raise LingError(
            ErrorCode.unsupported_capability, "工具注册表未装配", status_code=503
        )

    connector = _connector_for_window(request, str(window.title or ""))
    from ling.capabilities.tools import RECALL_TOOL_ID, ToolOutcome

    result = await registry.call(
        RECALL_TOOL_ID,
        {
            "window_id": conversation_id,
            "message_id": message_id,
            "session": session,
            "connector": connector,
        },
        confirmed=True,
    )
    # 提议状态只在**真的执行过**之后才推进：`needs_confirmation` /
    # `unsupported` 落回 `proposed`，让用户还能再点一次或改去手端点。
    #
    # 为什么是"落回"而不是"留在"：抢占那一步已经把它写成 `confirming` 并提交了
    # （见上面"先原子占有"的说明）。留一个 `confirming` 会让列表页看不到它、
    # 再点也拿不到执行权——那条提议就永远卡住了。
    if result.outcome in {ToolOutcome.succeeded, ToolOutcome.failed}:
        row.state = "confirmed"
    else:
        row.state = "proposed"
    await session.commit()

    if result.outcome is ToolOutcome.failed:
        # handler 抛的 `RecallToolError` 里就是人话（五道闸的 note）。
        # 这里把它包成 4xx 会丢掉"到底是哪一道"，所以按 200 + state 返回：
        # 前端看 `note` 就知道为什么没撤成，`reason_code` 给日志与测试。
        from ling.contracts.v1 import OutboundRecallState

        return RecallMessageOut(
            message_id=message_id,
            state=OutboundRecallState.not_found,
            note=result.detail or "没能撤回",
            reason_code="tool_refused",
        )
    if result.outcome is ToolOutcome.needs_confirmation:
        raise LingError(ErrorCode.conflict, result.detail or "需要确认", status_code=409)

    from ling.contracts.v1 import OutboundRecallState

    # handler 成功时 `output` 是 `outcome.note`（人话）。状态取不到精确值，
    # 但"成功了"与"不确定"必须分开——所以从 note 的措辞反推是错的，
    # 改为重新读一次本地标记：`recalled_at` 非空才是真撤回。
    from ling.infra.models import Message

    message_row = await session.get(Message, message_id)
    recalled_at = getattr(message_row, "recalled_at", None) if message_row else None
    state = (
        OutboundRecallState.recalled
        if recalled_at is not None
        else OutboundRecallState.uncertain
    )
    return RecallMessageOut(
        message_id=message_id,
        state=state,
        note=result.output or "已尝试撤回",
        recalled_at=recalled_at,
        reason_code=None if state is OutboundRecallState.recalled else "unconfirmed",
    )


@router.post(
    "/sessions/{conversation_id}/recall-proposals/{proposal_id}/decline",
    status_code=204,
)
async def decline_recall_proposal(
    conversation_id: str,
    proposal_id: str,
    auth: CurrentAuth,
    conversation: ConversationDep,
    session: DbSession,
) -> None:
    """用户说"不要撤"：把提议标成 ``declined``，**不发任何平台 action**。

    拒绝也要落状态而不是删行：撤回提议是"她想做过什么"的记录，删掉就查不到
    "她当时想撤、用户没同意"这件事了（审计与调试都要它）。
    """
    await conversation.get_window(conversation_id, subject_id=auth.user.subject_id)
    from ling.infra.models import Operation
    from ling.kernel.turn import RECALL_PROPOSAL_KIND

    row = await session.get(Operation, proposal_id)
    if (
        row is None
        or row.kind != RECALL_PROPOSAL_KIND
        or str(dict(row.payload or {}).get("window_id") or "") != conversation_id
    ):
        raise LingError(ErrorCode.not_found, "没有这条撤回提议", status_code=404)
    if row.state != "proposed":
        # 已经处理过（确认/拒绝/过期）：幂等地当成功，不重复改状态。
        return
    row.state = "declined"
    await session.commit()


__all__ = ["RECALL_LIMITER", "router"]
