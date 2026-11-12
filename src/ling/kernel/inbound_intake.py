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
import time

from ling.infra.db import session_scope
from ling.kernel.visibility import group_id_from_window_ref, is_group_window_ref

logger = logging.getLogger("ling.kernel.inbound")

# 进程内「入站被拒」计数。
#
# 为什么需要它：`LING_LOG_ACCESS_DENIALS=0` 之后逐条日志没了，但"最近到底被
# 拒了多少次"仍是安全上必须能回答的问题——完全没有记录等于看不见有人在扫
# 你的机器人。计数很小（一个 int），不需要落库；重启归零可以接受，因为它是
# **观测**指标而不是审计证据（审计走 `security_event`，那是另一条路）。
_denied_total = 0


def bump_denied_total() -> int:
    """入站被拒计数 +1，返回新值。"""
    global _denied_total
    _denied_total += 1
    return _denied_total


def denied_total() -> int:
    """本进程启动以来被拒的入站条数。"""
    return _denied_total


# 「是否逐条记录白名单拒绝」的缓存有效期。
#
# 值存在**已发布的配置修订**里（设置页的开关），所以要读一次库；但拒绝路径
# 原本**完全不碰数据库**，不能为了一个日志级别让每条陌生消息都查一次库——
# 被人刷的时候那正是最不该增加数据库压力的时刻。30 秒的 TTL 意味着：
#   * 开关最多 30 秒生效（比"改完要重启"好得多）；
#   * 无论被刷多快，最多每 30 秒一次查询。
_LOG_DENIALS_POLICY_TTL_SECONDS = 30.0


async def _log_denials_enabled(state: object) -> bool:
    """这一条拒绝要不要写日志？

    来源优先级：已发布配置修订里的值 > ``LING_LOG_ACCESS_DENIALS``
    （环境变量只在"没有已发布修订"或"旧修订没这个键"时兜底，与 ``test_mode``
    同一个模式）。

    读库失败一律退回默认值（默认 True：宁可多记几条，也不要静默）。返回的
    只是"写不写日志"，**拒绝本身在调用点无条件执行**。
    """
    settings = getattr(state, "settings", None)
    default = bool(getattr(settings, "log_access_denials", True))
    # 没有 settings 或没有 revision_watcher，说明不是完整装配的 app（单测里的
    # 桩）：不查库，直接用默认值。`SecretBox` 需要真的 `Settings`，这里挡在
    # 前面比事后 cast 干净。
    if settings is None or getattr(state, "revision_watcher", None) is None:
        return default
    cached = getattr(state, "_log_denials_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _LOG_DENIALS_POLICY_TTL_SECONDS:
        return bool(cached[1])
    enabled = default
    try:
        from ling.infra.security import SecretBox
        from ling.settings.service import ConfigService

        # 与 worker/路由同一构造口径：`SecretBox(settings)`，而不是去
        # `app.state` 上摸一个可能不存在的 `secret_box`（那是 `Any | None`，
        # 类型与运行期都可能拿到空值）。
        async with session_scope() as session:
            svc = ConfigService(
                session,
                SecretBox(settings),
                test_mode_default=bool(getattr(settings, "test_mode", True)),
                # 这里只读"拒绝日志开关"，人格参数在这里不会产生任何效果；
                # 但仍走同一个默认来源（``Settings.persona_id`` ←
                # ``LING_PERSONA_ID``），不再留一个写死的 ``"persona_ling"``
                # ——那会在 grep 里看起来像"还有一处没改成读配置"。
                persona_id=str(getattr(settings, "persona_id", "") or ""),
                log_denials_default=default,
            )
            enabled = (await svc.effective()).log_access_denials
    except Exception as exc:  # noqa: BLE001 - 读不到配置也必须能拒绝
        # 只记异常类型：这里可能拿到数据库连接错误，``str(exc)`` 有带出
        # DSN 的风险（与仓库里其它降级路径的写法一致）。
        logger.info("读取拒绝日志开关失败（沿用默认 %s）: %s", default, type(exc).__name__)
        enabled = default
    state._log_denials_cache = (now, enabled)  # type: ignore[attr-defined]
    return enabled


async def process_inbound_payload(app: object, connector: object, payload: dict,
                                  *, _asr_resume: tuple | None = None) -> dict:
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
        envelope = _asr_resume[0] if _asr_resume else connector.normalize_inbound(payload)  # type: ignore[attr-defined]
    except ChannelError as exc:
        from ling.ports.channel import ChannelErrorKind

        # ``ignored`` 是"按设计静默丢弃"（心跳等），**不记日志**：
        # 记了会刷屏，而且它证明的"连接还活着"由连接层负责。
        # 其余（notice/request 等未实现的事件）仍然记一条，那是真信息。
        if exc.kind != ChannelErrorKind.ignored:
            logger.info("忽略事件: %s", exc.message)
        return {"accepted": False, "reason": exc.message}
    except ValueError as exc:
        # 契约校验失败（典型：媒体引用带了 ``file:``/``data:`` 这类危险协议）。
        #
        # 这是**外部输入**的问题，不是本进程的故障。必须在这里收成
        # ``accepted=False``：让 HTTP 上报路径抛 500 的话，平台会按"投递失败"
        # 反复重投同一条坏事件，把一次坏输入放大成重试风暴。
        #
        # 日志里**不带 input_value**：那是攻击者可控的整段字符串（可能是一条
        # data: URI），原样写进日志等于帮他存一份内容。
        detail = type(exc).__name__
        errors = getattr(exc, "errors", None)
        if callable(errors):
            first = errors()[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            detail = f"{loc}: {first.get('msg', '')}" if loc else str(first.get("msg", ""))
        logger.warning("入站事件未通过契约校验（已丢弃）: %s", detail)
        return {"accepted": False, "reason": "事件格式不合法"}

    # **撤回通知不是一条消息**：它有自己的类型，走自己的处理路径。
    # 放在白名单/去重/决策之前一点点，但仍然走完整的去重与日志——
    # 早退会把"重复撤回只记一条日志"这件事交给调用方，那是错的。
    from ling.contracts.v1 import RecallEvent

    if isinstance(envelope, RecallEvent):
        return await _process_recall(app, envelope)

    state = app.state  # type: ignore[attr-defined]
    settings = state.settings
    # 群 id 从窗口引用里取（`window:group:<id>`），没有就是私聊。
    scene = str(getattr(envelope, "window_ref", "") or "")
    # 群号提取走**唯一实现**（`kernel/visibility.py` 的 `group_id_from_window_ref`）。
    # 原来的内联切分是同一逻辑的第二份拷贝：两处只要有一处改了判据
    # （第几段、要不要 `:group:` 字样），就会出现"一个说有群号、一个说没有"。
    group_id = group_id_from_window_ref(scene)
    decision = check_access(
        envelope.account_ref,
        mode=settings.access_mode,
        allow_list=settings.access_list,
        group_id=group_id,
        allow_groups=settings.access_groups,
    )
    if settings.access_mode == "off":
        logger.warning(
            "出入许可已关闭（LING_ACCESS_MODE=off）：任何账号都能跟她对话"
        )
    if not decision.observable:
        from ling.audit import security_event

        # 计数**永远**加：关掉日志只表示"别逐条刷屏"，不表示"不用知道有人
        # 在扫"。次数经 `/v1/logs` 与 `/v1/onebot/health` 的 `denied_total` 暴露。
        bump_denied_total()

        if await _log_denials_enabled(state):
            security_event(
                "inbound_denied",
                outcome="denied",
                account_ref=envelope.account_ref,
                reason_code=decision.reason[:64],
                attributes={"connector": getattr(connector, "connector_id", "")},
            )
        # 只有 `denied` 会走到这里——现在**只剩私聊陌生人**。群消息即使
        # 对方不在白名单里也会进上下文（`observe_only`），她得像人一样先看得见
        # 群里在聊什么，才谈得上"偶尔看一眼"。私聊不同：谁都能给她发私信的话，
        # 那道门就等于敞开了。
        #
        # 拒绝本身与日志开关**无关**：这里 return 掉，消息就不会落库、
        # 不会进注意力轮询。开关只能让日志安静，不能让白名单失效。
        return {
            "accepted": False,
            "reason": decision.reason,
            "sender": envelope.account_ref,
        }

    from ling.domains.conversation.service import ConversationService
    from ling.kernel.access import OBSERVE_ONLY
    from ling.kernel.bridge import InboundBridge

    connector_instance = connector.instance_id  # type: ignore[attr-defined]
    async with session_scope() as session:
        conversation = ConversationService(session)
        claimed = None if _asr_resume else await conversation.claim_event(
            scope=f"connector:{connector_instance}",
            key=envelope.external_event_id,
        )
        if claimed is not None:
            return {"accepted": True, "duplicate": True}
        has_audio = any(part.modality.value == "audio" for part in envelope.parts)
        if not has_audio or _asr_resume:
            await session.commit()

        # 协调器默认**每条消息新建一个**。这没有问题，因为 `TurnCoordinator`
        # 不带任何每轮状态：它的全部实例属性都在 `__init__` 里赋值，而且
        # 只是**持有** `app.state` 上那些共享对象的引用（取消表、仲裁器、
        # 流日志、跨副本停止与发言租约）。新建的只是这层薄壳，共享状态仍是
        # 同一个实例——网页停止按钮能取消到入站轮次，跨副本租约也照常生效。
        #
        # 上面的 `getattr` 是一个**覆盖钩子**，不是生产配置：生产代码从不写
        # `state.bridge_coordinator`（`app.py` 也不写），只有测试会塞一个假
        # 协调器进来模拟"模型侧抛错"，用来验证事件认领能被撤回、失败可重投
        # （见 `tests/integration/test_onebot_loop.py` 的 `Boom`）。
        #
        # **不要**在 `app.py` 里预先构造一个共享协调器挂到这个键上。那样
        # `router` 会被钉死在构造那一刻：`app.state.router` 在测试里、以及
        # 将来做密钥热替换时都会被重新赋值，缓存的协调器会继续用旧 router，
        # 表现为"换了模型/密钥但入站不生效"，而且完全不报错。实测这条改动
        # 让 `test_private_reply_is_split_and_reassembles` 直接失败
        # （`generation_calls == 0`：桩 router 被绕过）。
        #
        # `cancels` 是**必需**参数（`TurnCoordinator.__init__` 没有默认值）。
        # 原来写的是 `getattr(state, "cancels", CancelRegistry())`——默认值
        # 会被**立即求值**，无论属性在不在都白建一个 `CancelRegistry`。
        # 改成 `None` 兜底再 `or` 一个，语义相同但不白建。
        from ling.kernel.turn import CancelRegistry, TurnCoordinator

        coordinator = getattr(state, "bridge_coordinator", None)
        if coordinator is None:
            coordinator = TurnCoordinator(
                router=state.router,
                actor_id=settings.actor_id,
                persona_id=settings.persona_id,
                cancels=getattr(state, "cancels", None) or CancelRegistry(),
                arbiter=getattr(state, "arbiter", None),
                stream_log=getattr(state, "stream_log", None),
                turn_control=getattr(state, "turn_control", None),
                speak_lease=getattr(state, "speak_lease", None),
                # 与 Web 路径同源：本进程预热过的密钥缓存。入站轮次同样会
                # 走记忆检索，没有它就只能用环境变量那条腿。
                secret_reader=getattr(state, "secret_cache", {}).get,
            )
        bridge = InboundBridge(
            session,
            coordinator=coordinator,
            connector=connector,
            secret_box=state.secret_box,
            router=getattr(state, "router", None),
            test_mode_default=settings.test_mode,
            group_mention_on_reply=settings.group_mention_on_reply,
            auto_register_private=settings.auto_register_private,
            observe_only=decision.verdict == OBSERVE_ONLY,
            persona_id=settings.persona_id,
        )
        try:
            result = await bridge.handle(envelope, resume_message_id=_asr_resume[1]) if _asr_resume else await bridge.handle(envelope)
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

        # 行为日志：**她这一侧**发生了什么。放在这里而不是 bridge 内部，
        # 是因为 inbound_intake 是所有渠道的唯一汇合点——放 bridge 里，
        # QQ 官方机器人那条路会漏。
        _log_inbound_behavior(
            envelope, result, is_group=is_group_window_ref(envelope.window_ref)
        )

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


async def _process_recall(app: object, event: object) -> dict:
    """处理一条撤回通知：**不产生消息、不触发回复、不调用模型**。

    ## 为什么独立于消息链路

    消息链路会创建 `Message` 行、跑决策、调模型、投递回复。撤回通知三样都不该
    做——它要做的只有两件事：

    1. 把库里那条消息标成 `retracted_at`（条件更新，天然幂等）；
    2. 记一条人话行为日志（含"没匹配上"这种正常情况）。

    ## 为什么**不**再过访问白名单

    白名单管的是"能不能跟她说话"。撤回管的是"她已经知道的东西还成不成立"。
    某人不在白名单里，也不代表我们可以继续引用他撤回的那句话；而且白名单
    随时会改（今天放行、明天关掉），撤回的正确性不该跟着它漂。
    真正的边界是窗口：`retract_message` 只在消息所属窗口里生效。
    """
    from ling.audit import behavior_event
    from ling.contracts.v1 import RecallEvent
    from ling.domains.conversation.service import ConversationService

    assert isinstance(event, RecallEvent)
    connector_instance = event.connector_instance
    async with session_scope() as session:
        conversation = ConversationService(session)
        # 事件级幂等：同一撤回被平台重投时，第二次直接返回 duplicate。
        # 这与 `retract_message` 里的条件更新是**两层**保护——条件更新保证
        # "数据只变一次"，这里保证"日志也只记一次"。
        claimed = await conversation.claim_event(
            scope=f"connector:{connector_instance}:recall",
            key=event.external_event_id,
        )
        if claimed is not None:
            return {"accepted": True, "duplicate": True, "reason": "重复的撤回通知"}
        await session.commit()

        try:
            outcome = await conversation.retract_message(
                window_ref=str(event.window_ref or ""),
                platform_message_id=str(event.platform_message_id),
            )
        except Exception as exc:  # noqa: BLE001 - 失败要能被平台重投
            logger.exception("撤回处理失败，撤回认领以便重投")
            await conversation.release_event(
                scope=f"connector:{connector_instance}:recall",
                key=event.external_event_id,
            )
            await session.commit()
            return {
                "accepted": False,
                "reason": f"撤回处理失败，可重投: {type(exc).__name__}",
            }
        await session.commit()

    who = event.sender_name or "对方"
    where = "群里" if is_group_window_ref(event.window_ref) else "私聊里"
    matched = outcome.status in {"retracted", "already_retracted"}
    if event.self_recall:
        # 自己撤回自己的：不是"对方收回了"，不要错误归属。
        message = f"我把{where}的一条消息收回了，之后不再引用它的原文"
        action = "recall_self"
    elif not matched:
        # 未匹配是**正常情况**：撤回的可能是很久以前的消息、别的会话的消息，
        # 或者压根不是发给她的。所以是 INFO 的一行，不是告警。
        message = f"{who} 撤回了一条消息，但我这里没有对应的原文（可能是不久前没保存的）"
        action = "recall_unmatched"
    elif outcome.after_seen:
        # **她看手机之后才撤回**：她见过这条、可能已经回过话。所以不抹掉，
        # 只在上下文里补一句旁白。日志也要说清是这一种——两种撤回的处置
        # 完全不同，合成一句会让排障时分不清"她为什么还提那条"。
        message = (
            f"{who} 在{where}撤回了一条消息，我已经看过了；"
            "之后引用它时会注明「已被撤回」，之前说过的话不作废"
        )
        action = "message_retracted_after_seen"
    else:
        # **她还没看到就被撤回**：等同没看见，之后不会再进上下文。
        message = f"{who} 在{where}撤回了一条消息，我还没看到，就当没发生过"
        action = "message_retracted_unseen"

    behavior_event(
        action,
        message,
        attributes={
            "connector": connector_instance,
            "recall_kind": str(event.recall_kind),
            "status": outcome.status,
            "matched": matched,
            # 判定结果本身要可查：这是"两种撤回"唯一的机器可读依据。
            "after_seen": bool(outcome.after_seen),
            # **不放正文**：撤回事件里本来就没有原文；放 id 便于对日志与库。
            "platform_message_id": str(event.platform_message_id),
        },
    )
    logger.info(
        "撤回通知 processed: status=%s message_id=%s window=%s",
        outcome.status,
        event.platform_message_id,
        event.window_ref,
    )
    return {
        "accepted": True,
        "recalled": outcome.status == "retracted",
        "after_seen": bool(outcome.after_seen),
        "reason": outcome.status,
    }


def _log_inbound_behavior(
    envelope: object, result: object, *, is_group: bool
) -> None:
    """把一次入站处理说成一句人话。

    两条日志对应两种视角：

    - ``received``  她**收到**了什么（谁、在哪、说了什么）
    - ``handled``   她**怎么处理**的（接话 / 先放着 / 没懂）

    ## 动作与理由必须来自不同字段

    ``result.reason`` 只放**理由**（「群里的普通消息」）；动作（「先放着，
    等她刷到」）来自 ``BridgeResult.action``（延后处置由 bridge 说清楚在等
    什么），没有时由这里按 ``decision`` 补。以前 bridge 把动作也写进了
    reason，于是日志变成「先放着，等她刷到（群里的普通消息，先放着，
    等她刷到）」——同一句话说两遍，还占两倍长度。两个字段各写一份是
    冗余的根源，职责分开之后重复就无从发生。
    """
    from ling.audit import behavior_event

    sender = getattr(envelope, "sender_name", None) or "有人"
    text = ""
    for part in getattr(envelope, "parts", None) or []:
        if getattr(part, "text", None):
            text = part.text
            break
    scene = "群里" if is_group else "私聊里"

    # 正文截断：行为日志是给人快速扫的，不是全文存档（全文在 messages 表）。
    preview = text.strip().replace("\n", " ")[:60]
    received = f"{sender} 在{scene}说话" + (f"：「{preview}」" if preview else "（没有文字）")
    behavior_event(
        "inbound_received",
        received,
        attributes={
            "window_ref": getattr(envelope, "window_ref", ""),
            "is_group": is_group,
            "has_media": bool(getattr(envelope, "media_refs", None)),
        },
    )

    if not getattr(result, "accepted", False):
        return
    reason = str(getattr(result, "reason", "") or "")
    delivered = bool(getattr(result, "delivered", False))
    decision = str(getattr(result, "decision", "") or "")
    if decision == "deferred_attention":
        # 动作由 bridge 给（"先放着，等她刷到" / "先放着，等她补一句正文"）：
        # 同样是"延后"，等的东西不一样，这里不替它猜。
        action = str(getattr(result, "action", "") or "") or "先放着，等她刷到"
        handled = f"{action}（{reason}）" if reason else action
    elif getattr(result, "reply_text", None) and delivered:
        handled = "回了"
    elif getattr(result, "reply_text", None):
        handled = f"想回但没发出去（{reason}）" if reason else "想回但没发出去"
    else:
        handled = f"没接话（{reason}）" if reason else "没接话"

    window_ref = str(getattr(envelope, "window_ref", "") or "")
    # 她**没开口**的处置（先放着 / 没接话）同一窗口连着出现时只完整记一次。
    # 开了口（回了 / 想回但没发出去）**每次都记**——那才是用户真正要看的事，
    # 合并掉等于隐瞒她说过话。
    if decision in {"deferred_attention", "silence"} and _is_repeated_handled(
        window_ref, f"{decision}|{handled}"
    ):
        return
    behavior_event(
        "inbound_handled",
        f"对 {sender} 的话：{handled}",
        outcome=decision or ("replied" if delivered else "silent"),
        attributes={
            "window_ref": window_ref,
            "decision": decision,
            "delivered": delivered,
        },
    )


# ---------------------------------------------------------------- 处置日志降噪

# 同一窗口、同一句处置的"安静期"（秒）。
#
# 群聊热闹时每条普通消息都产生一行「先放着，等她刷到（群里的普通消息）」，
# 一行信息量都没有，却把 500 条缓冲里的位置占满。这个值决定"多久没有新的
# 同类处置就再记一次"——不是丢弃，是合并：窗口里重新安静下来之后，
# 下一条消息会重新完整记一行。
_HANDLED_NOTE_SECONDS = 120.0

# 记着多少个窗口：只是短时降噪状态，超了就按"最久没说话"淘汰。
# 不做无界字典——长跑进程里那会随群数一直长。
_HANDLED_NOTE_MAX_WINDOWS = 512

# ``window_ref -> (上次记下的处置签名, monotonic 时间戳)``。
_handled_notes: dict[str, tuple[str, float]] = {}


def _is_repeated_handled(window_ref: str, signature: str) -> bool:
    """这句处置刚刚在同一窗口记过吗？（是 → 本次省略，别的什么都不丢）

    只影响 ``inbound_handled`` 这一行：``inbound_received`` 仍然逐条记
    （那条里有正文，是真正要看的），``handled_reason`` 落库更是完全不受
    影响——这里从头到尾没碰过数据库和 ``BridgeResult``。
    """
    now = time.monotonic()
    previous = _handled_notes.get(window_ref)
    repeated = (
        previous is not None
        and previous[0] == signature
        and now - previous[1] < _HANDLED_NOTE_SECONDS
    )
    # 时间戳每次都推到"现在"：只要这个窗口还在连着说同类的话，就一直算
    # 同一段，不再冒新的行；真正安静下来之后再开口，才会重新记。
    _handled_notes[window_ref] = (signature, now)
    if len(_handled_notes) > _HANDLED_NOTE_MAX_WINDOWS:
        for stale, _ in sorted(_handled_notes.items(), key=lambda kv: kv[1][1])[
            : len(_handled_notes) - _HANDLED_NOTE_MAX_WINDOWS
        ]:
            _handled_notes.pop(stale, None)
    return repeated


__all__ = ["process_inbound_payload"]
