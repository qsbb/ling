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
                persona_id=str(getattr(settings, "persona_id", "persona_ling")),
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

    state = app.state  # type: ignore[attr-defined]
    settings = state.settings
    # 群 id 从窗口引用里取（`window:group:<id>`），没有就是私聊。
    scene = str(getattr(envelope, "window_ref", "") or "")
    group_id = scene.split(":group:", 1)[1].split(":", 1)[0] if ":group:" in scene else None
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
        claimed = await conversation.claim_event(
            scope=f"connector:{connector_instance}",
            key=envelope.external_event_id,
        )
        if claimed is not None:
            return {"accepted": True, "duplicate": True}
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

        # 行为日志：**她这一侧**发生了什么。放在这里而不是 bridge 内部，
        # 是因为 inbound_intake 是所有渠道的唯一汇合点——放 bridge 里，
        # QQ 官方机器人那条路会漏。
        _log_inbound_behavior(envelope, result, is_group=":group:" in envelope.window_ref)

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


def _log_inbound_behavior(
    envelope: object, result: object, *, is_group: bool
) -> None:
    """把一次入站处理说成一句人话。

    两条日志对应两种视角：

    - ``received``  她**收到**了什么（谁、在哪、说了什么）
    - ``handled``   她**怎么处理**的（接话 / 先放着 / 没懂）

    ``result.reason`` 本来就是可读句子（bridge 各处写的都是人话，例如
    「群里的普通消息，先放着，等她刷到」），这里只做拼接，不重新解释。
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
        handled = f"先放着，等她刷到（{reason}）" if reason else "先放着，等她刷到"
    elif getattr(result, "reply_text", None) and delivered:
        handled = "回了"
    elif getattr(result, "reply_text", None):
        handled = f"想回但没发出去（{reason}）" if reason else "想回但没发出去"
    else:
        handled = f"没接话（{reason}）" if reason else "没接话"
    behavior_event(
        "inbound_handled",
        f"对{sender}的话：{handled}",
        outcome=decision or ("replied" if delivered else "silent"),
        attributes={
            "window_ref": getattr(envelope, "window_ref", ""),
            "decision": decision,
            "delivered": delivered,
        },
    )


__all__ = ["process_inbound_payload"]
