"""渠道入站受理：**所有渠道**收到事件之后的唯一处理入口。

## 为什么抽成模块

入站来源不止一个：

1. **HTTP 上报**（NapCat 的 `httpClients`）——`POST /v1/onebot/events`
2. **WebSocket 客户端**（ling 主动连 NapCat 的 wsServer）
3. **QQ 官方机器人 Webhook**——`POST /v1/qqbot/events`

三条路必须在**同一处**处理，否则「白名单、去重、身份解析、决策、投递」
会出现多份实现——那种分叉迟早有一条忘了加某个检查，而且是静默的。

所以：协议差异（怎么收字节、怎么验签、怎么翻译成信封）留在各自的适配层，
**收到 payload 之后的一切**都在这里。本模块因此不出现任何 OneBot 专有名词：
它只依赖 `ling.ports.channel` 的契约（`normalize_inbound` / `instance_id`）。

## 为什么 WS 入站不验签

`verify_inbound` 校验的是 HTTP 头部与签名，为的是确认"这条请求确实来自
我们配的平台"。WebSocket 是**我们主动连出去**的：连接目标由配置决定，
对端身份由连接本身确定，没有"别人往我端口上乱发"这个风险面。
所以 WS 路径不重复做 HTTP 那套头部校验——但仍走白名单与去重。
"""

from __future__ import annotations

import logging

from ling.infra.db import session_scope

logger = logging.getLogger("ling.kernel.inbound")


async def process_inbound_payload(app: object, connector: object, payload: dict) -> dict:
    """处理一条已经解析好的平台事件（字典）。

    返回要回给上报方的 JSON。**业务失败也返回 accepted=False 而不是抛错**——
    HTTP 那条路已经在路由层保证了 200（非 200 会让平台反复重投），
    WS 路径也照此记录。

    ``connector`` 只要求满足 `ChannelConnector` 里被用到的部分：
    ``normalize_inbound`` 与 ``instance_id``。
    """
    from ling.kernel.access import check_access
    from ling.ports.channel import ChannelError

    try:
        envelope = connector.normalize_inbound(payload)  # type: ignore[attr-defined]
    except ChannelError as exc:
        # 心跳、通知、请求类事件目前不进入对话链路。
        logger.info("忽略事件: %s", exc.message)
        return {"accepted": False, "reason": exc.message}

    state = app.state  # type: ignore[attr-defined]
    settings = state.settings
    decision = check_access(
        envelope.account_ref,
        mode=settings.access_mode,
        allow_list=settings.access_list,
    )
    if settings.access_mode == "off":
        logger.warning(
            "出入许可已关闭（LING_ACCESS_MODE=off）：任何账号都能跟她对话"
        )
    if not decision.allowed:
        from ling.audit import security_event

        security_event(
            "inbound_denied",
            outcome="denied",
            account_ref=envelope.account_ref,
            reason_code=decision.reason[:64],
            attributes={"connector": getattr(connector, "connector_id", "")},
        )
        return {
            "accepted": False,
            "reason": decision.reason,
            "sender": envelope.account_ref,
        }

    from ling.domains.conversation.service import ConversationService
    from ling.kernel.bridge import InboundBridge

    connector_instance = connector.instance_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        conversation = ConversationService(session)
        claimed = await conversation.claim_event(
            scope=f"connector:{connector_instance}",
            key=envelope.external_event_id,
        )
        if claimed is not None:
            return {"accepted": True, "duplicate": True}
        await session.commit()

        coordinator = getattr(state, "bridge_coordinator", None)
        if coordinator is None:
            from ling.kernel.turn import CancelRegistry, TurnCoordinator

            coordinator = TurnCoordinator(
                router=state.router,
                actor_id=settings.actor_id,
                persona_id=settings.persona_id,
                cancels=getattr(state, "cancels", CancelRegistry()),
                arbiter=getattr(state, "arbiter", None),
                stream_log=getattr(state, "stream_log", None),
                turn_control=getattr(state, "turn_control", None),
                speak_lease=getattr(state, "speak_lease", None),
            )
        bridge = InboundBridge(
            session,
            coordinator=coordinator,
            connector=connector,
            secret_box=state.secret_box,
            router=getattr(state, "router", None),
            test_mode_default=settings.test_mode,
            group_mention_on_reply=settings.group_mention_on_reply,
            persona_id=settings.persona_id,
        )
        try:
            result = await bridge.handle(envelope)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要能重投
            logger.exception("事件处理失败，撤回认领以便重投")
            await conversation.release_event(
                scope=f"connector:{connector_instance}",
                key=envelope.external_event_id,
            )
            await session.commit()
            return {
                "accepted": False,
                "reason": f"处理失败，可重投: {type(exc).__name__}",
            }

        if result.accepted and result.reply_text and not result.delivered:
            await conversation.release_event(
                scope=f"connector:{connector_instance}",
                key=envelope.external_event_id,
            )
            await session.commit()

    return {
        "accepted": result.accepted,
        "reason": result.reason,
        "decision": result.decision,
        "delivered": result.delivered,
        "turn_id": result.turn_id,
        "media_evidence_ids": result.media_evidence_ids,
        "media_note": result.media_note,
        "handled_as": result.handled_as,
        "parts_total": result.parts_total,
        "parts_sent": result.parts_sent,
    }


__all__ = ["process_inbound_payload"]
