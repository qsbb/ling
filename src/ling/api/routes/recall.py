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

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ling.api.deps import ConversationDep, CurrentAuth, DbSession
from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode, RecallMessageOut
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
    """按窗口标识找到这个渠道的连接器实例。

    窗口 title 就是平台窗口标识（``onebot-x:group:100…``，见
    ``bridge._ensure_window``），前缀第一段是 ``instance_id``；
    连接器按渠道挂在 ``app.state`` 上（``onebot_connector`` / ``qqbot_connector``）。

    拿不到就返回 ``None``——调用方会按 ``unsupported`` 处理（**不发 action**），
    而不是去猜一个连接器。
    """
    channel = window_title.split(":", 1)[0] if ":" in window_title else ""
    # 实例名前缀是 `<connector>-<name>`（onebot-default / qqbot-test…）。
    for candidate in ("onebot", "qqbot", "qq"):
        if channel.startswith(candidate):
            connector = getattr(request.app.state, f"{candidate}_connector", None)
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


__all__ = ["RECALL_LIMITER", "router"]
