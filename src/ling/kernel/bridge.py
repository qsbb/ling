"""入站桥：把渠道事件跑成一轮真实对话。

**为什么需要这一层**：`connectors/qq/ingress.py` 之前只做到"验签 → 去重 →
解析身份 → 准备窗口"就返回了，不创建轮次、不调接话决策、不发送回复。
结果连接器能收到消息，用户却没法在 QQ 里真的聊天。

Web 路径（`api/routes/chat.py`）有完整的轮次流水线，但它依赖 FastAPI 的
Request/SSE。渠道入站没有 Request，所以把那段流水线抽到这里，
让渠道和 Web 共用同一套决策、上下文、生成与投递逻辑。

两条硬约束：
1. **不能绕过接话决策**：群里该沉默就沉默（`decision=silence` 不发送）
2. **不能绕过投递层**：发送走 DeliveryService，留下 Operation 与回执，
   这样"发了没有"可对账，而不是发出去就不管了
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.audit import hash_account_ref
from ling.contracts.v1 import InteractionEnvelope
from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.domains.media.service import MediaEvidenceError, MediaEvidenceService
from ling.infra.models import Window
from ling.kernel.autonomy import (
    AutonomyTurnService,
    _looks_urgent,
    make_persona_reader,
    make_situation_reader,
    scenario_key_for,
)
from ling.kernel.decision import DecisionContext, classify_text
from ling.kernel.turn import describe_turn_failure
from ling.logging_setup import emit_event

if TYPE_CHECKING:
    from ling.infra.models import Turn

logger = logging.getLogger("ling.kernel.bridge")


@dataclass
class BridgeResult:
    """一次入站处理的结局（供日志与测试断言）。"""

    accepted: bool
    reason: str
    # 她**做了什么**（一句话，例如「先放着，等她刷到」）。与 ``reason``
    # （为什么这么做）刻意分开：日志层把它拼成「动作（理由）」。
    # 以前动作也写在 reason 里，日志就成了「先放着，等她刷到（群里的普通
    # 消息，先放着，等她刷到）」——同一句话说两遍。
    # 为空表示由日志层按 decision 决定（"回了" / "没接话"）。
    action: str | None = None
    subject_id: str | None = None
    window_id: str | None = None
    turn_id: str | None = None
    decision: str | None = None
    reply_text: str | None = None
    delivered: bool = False
    # 本条消息登记的媒体证据 id（图片/语音等）。空表示没有媒体。
    media_evidence_ids: list[str] = field(default_factory=list)
    # 媒体没能登记时的原因（通常是缺授权）。**不能静默忽略**。
    media_note: str | None = None
    # 看见之后怎么处置的：replied | ignored。
    # 沉默时**消息仍然落库**——"她选择不回"和"她没收到"必须能分开。
    handled_as: str | None = None
    # 分段投递：一条逻辑回复拆成几段、成功发出去几段。
    # parts_sent < parts_total 表示**部分送达**，不能对外说"发出去了"。
    parts_total: int = 0
    parts_sent: int = 0


class InboundBridge:
    """渠道事件 → 一轮对话 → 回复。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        coordinator: object,
        connector: object,
        secret_box: object,
        router: object | None = None,
        test_mode_default: bool = True,
        # 群里回复是否 @ 发言人。多人说话时靠它分清在回谁。
        group_mention_on_reply: bool = True,
        # 私聊里没绑定过的号是否自动注册成独立自然人（不合并）。默认关：
        # 白名单已经在管"谁能说话"，这一项管的是"说完之后挂在谁名下"。
        auto_register_private: bool = False,
        # 这条消息只能看、不能主动回（准入判定的 `observe_only`）。
        # 与 `_defer_group_attention` 的区别：那个是**配置**说"群消息先放着"，
        # 这个是**这条消息**没有回复资格——哪怕配置允许立即回，也不该开口。
        observe_only: bool = False,
        persona_id: str = "persona_ling",
        actor_id: str = "actor_ling",
    ) -> None:
        self._session = session
        self._coordinator = coordinator
        self._connector = connector
        # 「回不回」需要模型参与（可选）。没有 router 时退回确定性规则，
        # 但**会如实标记 model_status**，不再静默。
        self._router = router
        # 配置读失败时的兜底。默认延后——见 _defer_group_attention 的说明。
        self._group_attention_deferred = True
        self._secret_box = secret_box
        self._test_mode_default = test_mode_default
        self._group_mention_on_reply = group_mention_on_reply
        self._auto_register_private = auto_register_private
        self._observe_only = observe_only
        self._persona_id = persona_id
        self._actor_id = actor_id

    async def handle(self, envelope: InteractionEnvelope) -> BridgeResult:
        # 1) 身份：私聊只接受**已证明**的绑定（不猜）；群聊允许"只认识账号"。
        #
        # 原来群里没走过绑定流程的人整条被拒——她只能听见已绑定的人说话，
        # 等于在有别人的群里是聋的。现在群里自动建 known 级 subject：
        # 能听见，但记忆只限本群、跨窗召回不开放（见 IDENTITY-MULTIUSER-DESIGN）。
        is_group = ":group:" in envelope.window_ref
        identity = IdentityService(self._session)
        subject_id = await identity.resolve_account(
            envelope.connector_instance, envelope.account_ref or ""
        )
        if subject_id is None and is_group:
            subject_id = await identity.resolve_or_provision(
                envelope.connector_instance,
                envelope.account_ref or "",
                # 建档写的是**昵称**（人级、跨群稳定），不是群名片。
                # 用名片建号 = "首条消息就把这个群的称呼写成这个人的名字"，
                # 之后 A/B 群互相覆盖，是跨群污染的源头。老连接器不发
                # ``sender_nickname`` 时才退回 ``sender_name``。
                display_name=envelope.sender_nickname or getattr(envelope, "sender_name", None),
            )
        # 私聊里没绑定过的号**注册成独立自然人**，不并到任何人身上。
        #
        # 为什么只注册不合并：给一个号分配身份，和"认定它和主人是同一个人"
        # 是两件事。前者只是让消息有个主体可挂，后者会**共享主人的全部记忆**
        # ——那是安全决定，必须由管理员在后台看过是谁之后手动做
        # （`/relationships` 页的合并/拆开），不能由一条私聊自动达成。
        if subject_id is None and not is_group and self._auto_register_private:
            subject_id = await identity.resolve_or_provision(
                envelope.connector_instance,
                envelope.account_ref or "",
                # 同上一处：人名取昵称，不取群名片（私聊本来就没有名片，
                # 但连接器实现可能把备注/别名塞进 sender_name，逻辑统一）。
                display_name=envelope.sender_nickname or getattr(envelope, "sender_name", None),
            )
            if subject_id is not None:
                logger.info(
                    "私聊账号未绑定，已注册为独立自然人（待管理员确认是否合并）：%s/%s",
                    envelope.connector_instance,
                    envelope.account_ref,
                )
                emit_event(
                    logger,
                    logging.INFO,
                    event_name="identity_auto_registered",
                    message="私聊账号自动注册为自然人",
                    attributes={
                        "connector": envelope.connector_instance,
                        "account_ref_hash": hash_account_ref(envelope.account_ref or ""),
                    },
                )
        if subject_id is None:
            return BridgeResult(
                accepted=False,
                reason="渠道账号尚未绑定到已证明的自然人，需走绑定流程",
            )

        # 1.5) 名字旁路：把这次看到的**昵称**同步进 `subjects.display_name`。
        #
        # 为什么需要：上面两处建号只在"没绑过"时跑（`resolve_or_provision`），
        # 已绑定的主人/管理员走的是 `resolve_account`，**永远进不去**——于是
        # 名字一写进库就固定不变。这正是"群名片不要固定不变"要修的那一半：
        # 会话标题、历史署名读的都是这一列，不更新它们就永远叫旧名字。
        #
        # 为什么只传昵称：`subjects.display_name` 是**人级**列，群名片是群级
        # 字段；用它写人级列会让同一个人在 A/B 群之间来回抖（跨群污染）。
        # 群里"此刻该叫这个名片"由当轮上下文负责（见下面 `sender_name` 的透传）。
        #
        # 为什么放在 subject_id 判空**之后**：没有 subject 就没有可写的行，
        # 而这条分支本来就整条不收，名字也就无从谈起。
        #
        # 它是**旁路**，不是前置条件：`observe_display_name` 内部吞异常并用
        # SAVEPOINT 包住写库（失败不会把调用方事务标成待回滚），所以
        # "改名失败"绝不会变成"这条消息拒收"。建号那一步
        # （`resolve_or_provision`）内部已经 flush 过，这里 SAVEPOINT 里只有
        # 名字这一步的读写。
        await identity.observe_display_name(
            envelope.connector_instance,
            envelope.account_ref or "",
            nickname=envelope.sender_nickname,
            sender_name=envelope.sender_name,
            subject_id=subject_id,
        )

        # 2) 窗口：群聊按**群**共享，不按发言人区分。
        #
        # 群窗口归属**主人**，不是"谁先说话谁拥有这个群"：
        # 原来用发言人的 subject 建群窗口，第一个说话的人（可能是陌生人）
        # 就成了这个窗口的主人，之后任何读 window.subject_id 的地方
        # （关系、主动推送、意识）都会用错人。
        window_subject = subject_id
        if is_group:
            window_subject = await identity.owner_subject_id() or subject_id
        window = await self._ensure_window(
            subject_id=window_subject, envelope=envelope, is_group=is_group
        )

        # envelope.parts 是契约里的 InputPart，只有 text 段进正文
        text = "".join(
            part.text or "" for part in envelope.parts if part.modality.value == "text"
        ).strip()

        # 媒体必须在文本判断**之前**登记，否则纯图片消息会被整条丢掉，
        # 连"收到过一张图"都留不下痕迹，描述任务也就无从捡起。
        media_ids, media_note = await self._ingest_media(
            envelope,
            subject_id=subject_id,
            window_id=window.id,
            # 群窗口的 subject_id 是**主人**（见上面 window_subject 的取值）：
            # 主人授权一次 media.inbound，群里谁发的图都能被描述。
            # 私聊窗口下两者本来就是同一个人。
            owner_subject_id=window.subject_id,
        )

        # 「是不是冲她说的」要在"有没有可读文本"**之前**算：
        # 只有 @ 没有正文是真实场景，先按"没文本"拒掉就永远等不到补充。
        addressed = self._is_addressed(
            envelope,
            text,
            channel_mentioned=envelope.mentions_actor,
            replied_to_actor=bool(envelope.reply_to),
        )

        # 群里被 @ 但**没有正文**：等对方补一句。
        # QQ 里「先 @ 再发正文」很常见，立刻决策只能看到孤零零一个 @。
        if is_group and addressed and not text.strip() and not media_ids:
            from ling.domains.attention.service import GroupAttentionScheduler

            conversation = ConversationService(
                self._session, actor_id=self._actor_id, persona_id=self._persona_id
            )
            await conversation.add_message(
                window.id,
                role="user",
                text=text,
                attention_state="unseen",
                # 群窗口是多个人的：不记谁说的，之后就没法回指"你刚问的"
                sender_subject_id=subject_id,
            )
            await GroupAttentionScheduler(
                self._session, actor_id=self._actor_id
            ).await_supplement(window.id)
            await self._session.commit()
            return BridgeResult(
                accepted=True,
                # 「被 @ 了，等补充一句正文」是**理由**；动作由 action 给，
                # 两者拼起来才是「先放着（被 @ 了，等补充一句正文）」。
                # reason 里保留"补充"字样：接口返回与排查日志都靠它说明
                # "还在等对方补一句"，不能让调用方只剩一个 decision 值。
                reason="被 @ 了，等补充一句正文",
                action="先放着",
                subject_id=subject_id,
                window_id=window.id,
                decision="deferred_attention",
                media_evidence_ids=media_ids,
                media_note=media_note,
            )

        # 连接器会把媒体换成 ``[image]`` / ``[voice]`` 这类占位标记塞进正文。
        # 它们是**连接器的痕迹，不是用户说的话**：只发一张图时正文等于
        # "[image]"，若照字面判"有文本"就会拿 "[image]" 当用户发言去回复。
        # 占位标记仍保留在 text 里给模型当提示，只在"有没有真话"的判断上剥掉。
        readable = _MEDIA_MARKERS.sub("", text).strip()

        if not readable:
            if media_ids:
                return BridgeResult(
                    accepted=False,
                    reason="已记录媒体证据，但还没有描述可读（纯媒体暂不建轮次）",
                    subject_id=subject_id,
                    window_id=window.id,
                    media_evidence_ids=media_ids,
                    media_note=media_note,
                )
            reason = media_note or "消息没有可读文本"
            return BridgeResult(
                accepted=False,
                reason=reason,
                subject_id=subject_id,
                window_id=window.id,
                media_note=media_note,
            )

        # 3) 注意力：群里的**普通**消息先不即时决策。
        # 用户要求"人不是整天都看着群，有人 @ 就看一下，或者刷到刚好就看一下"。
        # 这条只推迟**决策**，消息仍然落库并标 unseen——"还没看"和
        # "看了不回"必须能分开。被 @ / 叫名字 / 紧急内容不受影响。
        # `observe_only`：她看得见，但**不主动开口**。强制走延迟路径，
        # 不看 `_defer_group_attention` 的配置——那个开关是"要不要把普通群消息
        # 先放着"，管的是策略；这里管的是资格（这个人/这个群有没有资格让她
        # 主动说话）。把两件事合并，配置一改就会让陌生人获得发言权。
        observe = self._observe_only
        plain_group = is_group and not addressed and not _looks_urgent(text)
        if observe or (plain_group and await self._defer_group_attention()):
            conversation = ConversationService(
                self._session, actor_id=self._actor_id, persona_id=self._persona_id
            )
            await conversation.add_message(
                window.id,
                role="user",
                text=text,
                attention_state="unseen",
                sender_subject_id=subject_id,
            )
            await self._session.commit()
            # ``reason`` 只写**理由**，不写动作。
            #
            # 动作（"先放着，等她刷到"）由日志层按 ``decision`` 补——两处
            # 各写一份必然重复，这正是「先放着，等她刷到（群里的普通消息，
            # 先放着，等她刷到）」那条日志的成因。
            #
            # 两种走法的理由不同，**不能共用一句**：
            #   observe_only  是**资格**问题（这个人/群没权力让她主动开口），
            #                 换个配置也不该变；
            #   普通群消息     是**策略**问题（配置说群消息先攒着）。
            # 混着说会让"为什么没回"在日志里说谎。
            reason = "只能看，没资格让她主动开口" if observe else "群里的普通消息"
            return BridgeResult(
                accepted=True,
                reason=reason,
                action="先放着，等她刷到",
                subject_id=subject_id,
                window_id=window.id,
                decision="deferred_attention",
                media_evidence_ids=media_ids,
                media_note=media_note,
            )

        # 4) 接话决策：硬规则优先，模型只在弹性区间
        decision = await self._decide(
            window, text, subject_id, is_group,
            channel_mentioned=envelope.mentions_actor,
            replied_to_actor=bool(envelope.reply_to),
        )
        if not decision.respond:
            # **沉默不等于没看见。** 消息必须落库，否则她不会记得有人
            # 跟她说过话，而"她选择不回"和"她根本没收到"也再也分不开。
            conversation = ConversationService(
                self._session, actor_id=self._actor_id, persona_id=self._persona_id
            )
            msg = await conversation.add_message(
                window.id,
                role="user",
                text=text,
                handled_as="ignored",
                handled_reason=decision.reason,
                sender_subject_id=subject_id,
                # 走到这里说明已经进过决策，属于"看见但不回"
                attention_state="seen",
            )
            await self._record_followup(
                decision, subject_id=subject_id, window_id=window.id, message_id=msg.message_id
            )
            # 「她为什么没回」的唯一权威记录。skip_code 是受控枚举，
            # reason 是措辞——只看日志就能回答这个问题，不用读代码。
            emit_event(
                logger,
                logging.INFO,
                event_name="reply_decision_made",
                message="决定不回应",
                attributes={
                    "decision": "silence",
                    "skip_code": decision.skip_code,
                    "hard_rule": decision.hard,
                    "is_group": is_group,
                    "reason": decision.reason,
                },
            )
            await self._session.commit()
            return BridgeResult(
                accepted=True,
                # 只给**理由**，动作（"没接话"）由日志层按 decision 补。
                # 这里原来写的是「决定不回应：{reason}」，日志于是成了
                # 「没接话（决定不回应：…）」——同一句话说了两遍。
                # 去掉前缀之后，返回给调用方的 reason 与**落库的
                # handled_reason** 更是同一个值（上面 add_message 存的就是
                # ``decision.reason``），不会出现"日志里的理由和库里的不一样"。
                reason=decision.reason,
                subject_id=subject_id,
                window_id=window.id,
                decision="silence",
                media_evidence_ids=media_ids,
                media_note=media_note,
                handled_as="ignored",
            )

        emit_event(
            logger,
            logging.INFO,
            event_name="reply_decision_made",
            message="决定回应",
            attributes={
                "decision": decision.decision.value,
                "skip_code": decision.skip_code,
                "hard_rule": decision.hard,
                "is_group": is_group,
                "reason": decision.reason,
            },
        )

        # 4) 建轮次 + 落用户消息
        conversation = ConversationService(
            self._session, actor_id=self._actor_id, persona_id=self._persona_id
        )
        config_service = self._config_service()
        effective = await config_service.effective()
        turn = await self._ensure_turn(
            conversation,
            window_id=window.id,
            client_turn_id=envelope.external_event_id,
            config_revision=effective.revision,
        )
        emit_event(
            logger,
            logging.INFO,
            event_name="turn_started",
            message="轮次开始",
            attributes={
                "turn_id": turn.id,
                "window_id": window.id,
                "is_group": is_group,
                "source": "connector",
                "config_revision": effective.revision,
            },
        )
        user_row = await conversation.add_message(
            window.id,
            role="user",
            text=text,
            turn_id=turn.id,
            handled_as="replied",
            handled_reason=decision.reason,
            attention_state="seen",
            sender_subject_id=subject_id,
        )
        await self._record_followup(
            decision,
            subject_id=subject_id,
            window_id=window.id,
            message_id=user_row.message_id,
        )
        # 轮次任务用自己的会话，必须先提交，否则后台看不到 turn 行
        await self._session.commit()

        # 5) 跑轮次，收集最终文本
        reply_text, state, expression, error = await self._run_turn(
            window_id=window.id,
            subject_id=subject_id,
            turn_id=turn.id,
            text=text,
            effective=effective,
            # "instance:external_id" —— 记忆按它记录来源账号
            source_account=f"{envelope.connector_instance}:{envelope.account_ref or ''}",
            # **这一条消息**里的称呼（群名片优先）。必须透传到
            # `compile(user_sender_name=...)`：库里那一列是旁路，改名后
            # 当轮就该用新名片，而不是等落库再下一轮才生效。
            sender_name=envelope.sender_name,
        )
        if not reply_text:
            # 轮次跑完了却没产出文本：这是"她说不出话"，与"决定不说"不同。
            #
            # ``error`` 是**为什么**（code + 原因）。它必须同时进结构化事件
            # 与 ``reason``：
            # - 事件里可检索（"今天有多少轮是 quota_exhausted"）；
            # - ``reason`` 会被行为日志拼成「没接话（…）」，而那是用户唯一
            #   会去读的那一行。原来这里只有 ``state=failed``，等于告诉他
            #   "出错了"，却不告诉他去改哪里。
            code = str((error or {}).get("code") or "")
            emit_event(
                logger,
                logging.WARNING,
                event_name="turn_completed",
                message="轮次结束但没有可交付文本",
                attributes={
                    "turn_id": turn.id,
                    "outcome": "no_text",
                    "turn_state": str(state),
                    "is_group": is_group,
                    "error_code": code or None,
                    "detail": describe_turn_failure({"error": error}) if error else None,
                },
            )
            return BridgeResult(
                accepted=True,
                reason=(
                    f"轮次结束但没有可交付文本（state={state}，error={code}）"
                    if code
                    else f"轮次结束但没有可交付文本（state={state}）"
                ),
                subject_id=subject_id,
                window_id=window.id,
                turn_id=turn.id,
                decision="reply",
                media_evidence_ids=media_ids,
                media_note=media_note,
            )

        # 6) 投递（分段顺序发，每段独立 Operation 与回执，可对账）
        delivered, parts_sent, parts_total = await self._deliver(
            envelope=envelope,
            text=reply_text,
            subject_id=subject_id,
            expression=expression or {},
        )
        emit_event(
            logger,
            logging.INFO if delivered else logging.WARNING,
            event_name="turn_completed",
            message="轮次完成" if delivered else "轮次完成但投递不完整",
            attributes={
                "turn_id": turn.id,
                "outcome": "delivered" if delivered else "partial_delivery",
                "turn_state": str(state),
                "is_group": is_group,
                "parts_total": parts_total,
                "parts_sent": parts_sent,
            },
        )
        return BridgeResult(
            accepted=True,
            reason="已回复",
            subject_id=subject_id,
            window_id=window.id,
            turn_id=turn.id,
            decision="reply",
            reply_text=reply_text,
            delivered=delivered,
            media_evidence_ids=media_ids,
            media_note=media_note,
            handled_as="replied",
            parts_total=parts_total,
            parts_sent=parts_sent,
        )

    # ------------------------------------------------------------ 内部

    async def _record_followup(
        self,
        decision: object,
        *,
        subject_id: str,
        window_id: str,
        message_id: str,
    ) -> None:
        """她说"稍后再提"时落一条意图。

        来源固定为这条消息——说不出为什么会有这个念头的，不允许产生。
        """
        followup = getattr(decision, "followup", None)
        if followup is None:
            return
        from ling.domains.intent.service import record_followup

        await record_followup(
            self._session,
            actor_id=self._actor_id,
            followup=followup,
            subject_id=subject_id,
            window_id=window_id,
            source_ref=f"msg:{message_id}",
        )

    async def _ingest_media(
        self,
        envelope: InteractionEnvelope,
        *,
        subject_id: str,
        window_id: str,
        owner_subject_id: str | None = None,
    ) -> tuple[list[str], str | None]:
        """登记这条消息里的媒体证据，返回 (证据 id 列表, 失败原因)。

        设计取舍：**媒体登记失败不阻断文本轮次**。一张图描述不了不应该
        让整句"这图什么意思"也聊不下去。但失败原因必须带出去，
        不能静默——否则用户不知道为什么发了图角色毫无反应。

        ``owner_subject_id``：会话主人。授权判据是「发言人本人**或**主人」
        之一——主人授权一次，群里别人发的图也能被描述。不传就退化成
        "只认发言人"，与加这个参数之前的行为一致。

        注意这里只登记、不描述：描述要调视觉模型，属于另一条链路。
        """
        refs: list[str] = []
        kinds: dict[str, str] = {}
        for part in envelope.parts:
            if part.modality.value == "text" or not part.media_ref:
                continue
            refs.append(part.media_ref)
            kinds[part.media_ref] = _kind_of(part.modality.value, part.mime_type)
        for ref in envelope.media_refs:
            if ref not in refs:
                refs.append(ref)
                kinds[ref] = "image"

        if not refs:
            return [], None

        service = MediaEvidenceService(self._session, actor_id=self._actor_id)
        recorded: list[str] = []
        failures: list[str] = []
        for ref in refs:
            try:
                view = await service.record(
                    subject_id=subject_id,
                    window_id=window_id,
                    media_ref=ref,
                    kind=kinds.get(ref, "image"),
                    source_event_id=f"{envelope.external_event_id}:{ref}",
                    consent_subjects=(owner_subject_id,) if owner_subject_id else (),
                )
            except MediaEvidenceError as exc:
                failures.append(str(exc))
                continue
            recorded.append(view.evidence_id)
        if failures:
            # 一条消息里 N 个媒体只记**一行**汇总。
            #
            # 原来每个 ref 单独一行 INFO：群里有人一次发 9 张图，就是 9 行
            # 一模一样的「未授权描述入站媒体」——真问题被自己的重复淹掉。
            # 拒绝本身没变：仍然逐条失败、仍然把原因带回给调用方，
            # 只是日志从"每个 ref 一行"压成"这条消息一行"。
            reasons = "；".join(dict.fromkeys(failures))
            logger.info(
                "收到 %d 个媒体，跳过 %d 个：%s", len(refs), len(failures), reasons
            )
        return recorded, (failures[0] if failures else None)

    async def _ensure_turn(
        self,
        conversation: ConversationService,
        *,
        window_id: str,
        client_turn_id: str,
        config_revision: int,
    ) -> Turn:
        """取或建轮次，允许**失败后重投**复用同一轮。

        渠道事件的 ``client_turn_id`` 用事件 id（天然的幂等键）。但一次
        处理失败后（模型报错/发送失败），第一次的 turn 行还在，
        直接 create 会撞 ``(window_id, client_turn_id)`` 唯一约束 ——
        重投就永远失败，用户收不到回复。

        所以：已存在且处于失败/挂起态就重置复用（保留审计），
        已是终态（完成/停止）则说明这轮真的办过了，直接返回让它别再跑。
        """
        from ling.contracts.v1 import TurnState
        from ling.infra.models import Turn

        existing = (
            await self._session.execute(
                select(Turn).where(
                    Turn.window_id == window_id,
                    Turn.client_turn_id == client_turn_id[:128],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return await conversation.create_turn(
                window_id, client_turn_id=client_turn_id, config_revision=config_revision
            )
        if existing.state in {
            TurnState.failed.value,
            TurnState.pending.value,
            "stopping",
        }:
            existing.state = TurnState.pending.value
            existing.error_code = None
            existing.error_detail = None
            existing.config_revision = config_revision
            await self._session.flush()
        return existing

    def _config_service(self):  # noqa: ANN202
        from ling.settings.service import ConfigService

        return ConfigService(
            self._session,
            self._secret_box,
            test_mode_default=self._test_mode_default,
            persona_id=self._persona_id,
        )

    async def _ensure_window(
        self,
        *,
        subject_id: str,
        envelope: InteractionEnvelope,
        is_group: bool,
    ) -> Window:
        """按渠道窗口标识复用/创建窗口。

        群聊窗口按 ``window_ref`` 找，**不带 subject_id 条件**：
        同一个群里不同成员必须落到同一个窗口，否则角色看到的是
        一段段孤立的私聊，无法判断群里的气氛与是否该接话。
        """
        channel = str(getattr(self._connector, "connector_id", "unknown"))
        stmt = select(Window).where(
            Window.actor_id == self._actor_id,
            Window.channel == channel,
            Window.title == envelope.window_ref,
        )
        if not is_group:
            stmt = stmt.where(Window.subject_id == subject_id)
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is not None:
            return existing

        window = Window(
            id=f"win_{__import__('uuid').uuid4().hex[:24]}",
            actor_id=self._actor_id,
            persona_id=self._persona_id,
            subject_id=subject_id,
            title=envelope.window_ref,
            channel=channel,
            visibility="group_scope" if is_group else "subject_private",
        )
        self._session.add(window)
        await self._session.flush()
        return window

    async def _defer_group_attention(self) -> bool:
        """当前配置下，普通群消息是否延后决策。读不到配置按**延后**处理——

        宁可少说一句，也不要在"她本来没在看"的时候突然插话。
        """
        if not self._group_attention_deferred:
            return True
        with contextlib.suppress(Exception):
            effective = await self._config_service().effective()
            return bool(effective.dialogue.group_attention_deferred)
        return True

    def _is_addressed(
        self,
        envelope: InteractionEnvelope,
        text: str,
        *,
        channel_mentioned: bool = False,
        replied_to_actor: bool = False,
    ) -> bool:
        """这条消息是不是冲她说的。

        抽出来是因为**两条路径都要用**：决策路径要判断"该不该接话"，
        注意力路径要判断"要不要立刻看"。两处各写一遍必然漂。
        """
        _is_question, text_mentioned, _asked = classify_text(text)
        # 渠道提供的点名信息优先：正文里的 @ 已被剥离，
        # 只靠文本反推会永远认为"没点名"，群聊就永远不接话。
        # 「引用机器人上一条消息」同样是明确指向，必须算作被点名——
        # 否则群里接着它的话往下问，它会当成没叫它而沉默。
        return channel_mentioned or text_mentioned or replied_to_actor

    async def _decide(
        self,
        window: Window,
        text: str,
        subject_id: str,
        is_group: bool,
        *,
        channel_mentioned: bool = False,
        replied_to_actor: bool = False,
    ):  # noqa: ANN202
        is_question, text_mentioned, explicitly_asked = classify_text(text)
        mentioned = channel_mentioned or text_mentioned or replied_to_actor
        recent = await self._recent_actor_turns(window.id)
        budget = DecisionContext.group_turn_budget
        # 配置读失败退回默认，不阻断对话——"设置服务挂了"不该表现成"群里永远不接话"
        with contextlib.suppress(Exception):
            budget = int(
                (await self._config_service().effective()).dialogue.group_turn_budget
            )
        ctx = DecisionContext(
            is_group=is_group,
            mentioned=mentioned,
            # 私聊恒为"对我说"；群聊看是否点名/引用机器人
            addressed_to_actor=(not is_group) or mentioned,
            is_question=is_question,
            recent_actor_turns=recent,
            explicitly_asked=explicitly_asked,
            has_meaningful_text=bool(text.strip()),
            group_turn_budget=budget,
        )
        # 「回不回」走与 Web 路径同一个选择器。默认配置下结论与
        # DecisionEngine 完全一致（自主度开关默认关闭），但模型是否
        # 参与、为什么这样决定，现在都可查。
        return await AutonomyTurnService(
            router=self._router,
            config_reader=self._config_service(),
            situation_reader=make_situation_reader(
                self._session,
                actor_id=self._actor_id,
                window_id=window.id,
                # 只有私聊传当事人。群聊不传：群窗口的 subject_id 指向角色主人，
                # 而那批消息可能来自陌生人——把"对主人的熟悉度"当成"对说话人
                # 的熟悉度"是错误归属。
                subject_id=None if is_group else subject_id,
            ),
            persona_reader=make_persona_reader(
                self._session, persona_id=self._persona_id, scenario=scenario_key_for(ctx)
            ),
        ).choose(ctx, text=text)

    async def _recent_actor_turns(self, window_id: str) -> int:
        """该窗口近期机器人已发言次数（防刷屏用）。

        只数最近 5 分钟：更早的不该继续压着新话题。
        """
        from datetime import timedelta

        from ling.infra.models import Message, utcnow

        cutoff = utcnow() - timedelta(minutes=5)
        rows = (
            await self._session.execute(
                select(Message).where(
                    Message.window_id == window_id,
                    Message.role == "actor",
                    Message.created_at >= cutoff,
                )
            )
        ).scalars()
        return len(list(rows))

    async def _run_turn(
        self,
        *,
        window_id: str,
        subject_id: str,
        turn_id: str,
        text: str,
        effective: object,
        source_account: str | None = None,
        # 这条消息里的称呼（群里 = 群名片优先）。与 `source_account` 一样，
        # 它属于"这一条消息"，不是"这个人"，所以只能沿调用链透传，
        # 不能从库里读——库里那一列是旁路同步的结果，可能还没写进去。
        sender_name: str | None = None,
    ) -> tuple[str, str, dict | None, dict | None]:
        """跑一轮，返回 ``(正文, 状态, 表达方式, 失败原因)``。

        第四个返回值是 ``turn.failed`` 事件里的 ``error`` 原样字典
        （``code / message / retriable``，未捕获异常那条路多一个 ``detail``）。
        它必须往上传：调用方要拿 ``code`` 写进"没有可交付文本"的原因里，
        否则运维只知道轮次失败、不知道去改哪里。正常结束时为 ``None``。
        """
        import asyncio

        queue = await self._coordinator.run_turn(  # type: ignore[attr-defined]
            window_id=window_id,
            subject_id=subject_id,
            turn_id=turn_id,
            user_text=text,
            config=effective,
            # 记忆要记下"是哪个号说的"，合并后才能无损拆分
            source_account=source_account,
            # 「当前这句是谁说的」——群里用它给最后一行署名（群名片优先）
            sender_name=sender_name,
        )
        reply_text = ""
        state = "unknown"
        expression: dict | None = None
        error: dict | None = None
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=180.0)
            except TimeoutError:
                logger.warning("轮次 %s 超时未结束", turn_id)
                break
            if event is None:
                break
            name = getattr(event, "event", "")
            data = getattr(event, "data", {}) or {}
            if name in {"turn.completed", "turn.stopped", "turn.failed"}:
                state = str(data.get("state") or name)
                message = data.get("message") or {}
                reply_text = str(message.get("text") or "")
                expression = data.get("expression") or expression
                if name == "turn.failed":
                    # 失败原因原来只塞在 SSE 事件里，**日志里一个字都没有**。
                    # 于是"她没回"在运维侧不可解释：既可能是模型挂了，
                    # 也可能是权限/窗口/上下文的问题。这里必须留痕。
                    #
                    # 而且不能只读 ``detail``：``ModelError`` 那条路填的是
                    # ``code/message``，``detail`` 是空的——只读 detail 正是
                    # "失败：turn.failed |" 后面什么都没有的原因。
                    error = dict(data.get("error") or {})
                    logger.warning(
                        "轮次 %s 失败（%s）：%s",
                        turn_id,
                        state,
                        describe_turn_failure(data),
                    )
        # 出站前再洗一次：`turn.completed` 的正文已经干净，但"她的话"最终
        # 是从这里发出去的——半截标记（模型被打断时的残片）必须在这一步也挡住。
        from ling.domains.media.watch import clean_reply_text

        return clean_reply_text(reply_text).strip(), state, expression, error

    async def _deliver(
        self,
        *,
        envelope: InteractionEnvelope,
        text: str,
        subject_id: str,
        expression: dict | None = None,
    ) -> tuple[bool, int, int]:
        """把一条回复按语义分段顺序投递。返回 (是否全部送达, 已发段数, 总段数)。

        **每段只在上一段拿到真实回执之后才发**——这就是"不是一次全倒出去"。

        四条硬约束：
        1. 群聊恒为 1 段：拆成多条会绕过防刷屏上限（`group_turn_budget`
           数的是消息条数）
        2. **分段必须按渠道硬上限切**：`plan_parts` 收
           `max_part_chars=capabilities().max_text_length`。这是物理约束，
           与 `MAX_SEGMENT_CHARS`（220，只管"切不切"）不是一回事。
           拿不到能力时 `plan_parts` 用保守兜底值，也绝不产出"肯定发不出去"的段
        3. 回执缺失/不明/失败 → **停止后续段**，不盲目重发
        4. 段间停顿是**模拟节奏**，绝不写进 delivery 回执

        为什么超限不再导致"部分送达 + 尾段丢失"：切分在这里就按渠道上限做完
        （见约束 2），所以 `DeliveryService.deliver` 里的超限拦截只是防御性的，
        正常情况下不可达。原先的 `break` 也因此只对**真发不出去**的失败生效
        （渠道不可达/平台拒收），而不是"这一段太长"。
        """
        import asyncio

        from ling.capabilities.chunking import PART_DELAY_SECONDS, plan_parts
        from ling.capabilities.delivery import DeliveryService
        from ling.ports.channel import OutboundMessage

        is_group = ":group:" in envelope.window_ref
        # 渠道硬上限：能拿到就用，拿不到让 `plan_parts` 用保守兜底。
        # 不传这个值就等于"假设渠道没有上限"——正是尾段被整条拒收的成因。
        caps = getattr(self._connector, "capabilities", None)
        limit = None
        if callable(caps):
            with contextlib.suppress(Exception):
                limit = int(caps().max_text_length) or None
        parts = plan_parts(text, is_group=is_group, max_part_chars=limit)
        if not parts:
            return (False, 0, 0)

        plan_id = f"plan_{__import__('uuid').uuid4().hex[:16]}"
        service = DeliveryService(self._session)
        sent = 0
        for index, part in enumerate(parts, 1):
            if index > 1:
                # 上一段已经拿到回执才走到这里。停顿只是节奏，不是回执。
                await asyncio.sleep(PART_DELAY_SECONDS)
            outcome = await service.deliver(
                connector=self._connector,  # type: ignore[arg-type]
                message=OutboundMessage(
                    operation_id=f"op_{__import__('uuid').uuid4().hex[:16]}",
                    window_ref=envelope.window_ref,
                    text=part,
                    is_group=is_group,
                    # **她自己选的**表达方式。代码不再替她决定"群里就该 @"——
                    # 那是把人设的判断写死在代码里。这里只负责"选了就做到"。
                    mentions=list((expression or {}).get("at") or []),
                    reply_to=(expression or {}).get("quote"),
                    poke_targets=list((expression or {}).get("poke") or []),
                ),
                actor_id=self._actor_id,
                subject_id=subject_id,
                payload_extra={
                    "reply_plan": plan_id,
                    "part_ordinal": index,
                    "parts_total": len(parts),
                },
            )
            # `delivered` 是**唯一**判据，不再在这里另写一种拼法
            if not outcome.delivered:
                # 「为什么没送达」的唯一权威记录：状态、错误码、第几段。
                # 不记正文——投递失败不代表可以把内容抄进日志。
                emit_event(
                    logger,
                    logging.WARNING,
                    event_name="delivery_failed",
                    message="这一段没送达，停止后续段",
                    attributes={
                        "channel": getattr(self._connector, "connector_id", ""),
                        "delivery_state": str(getattr(outcome, "state", "")),
                        "error_code": str(
                            getattr(getattr(outcome, "receipt", None), "state", "") or ""
                        ),
                        "detail": (outcome.detail or "")[:200],
                        "part_ordinal": index,
                        "parts_total": len(parts),
                        "retriable": bool(getattr(outcome, "retriable", False)),
                        "is_group": is_group,
                    },
                )
                break
            sent += 1

        return (sent == len(parts), sent, len(parts))


__all__ = ["BridgeResult", "InboundBridge"]


_MEDIA_MARKERS = re.compile(r"\[(?:image|voice|video|file)\]")

_MIME_KIND = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}


def _kind_of(modality: str, mime_type: str | None) -> str:
    """从 modality / mime 推断媒体类型。未知一律按 image——它是最常见的入站媒体。"""
    if modality in _MIME_KIND:
        return _MIME_KIND[modality]
    if mime_type:
        head = mime_type.split("/", 1)[0].lower()
        if head in _MIME_KIND:
            return _MIME_KIND[head]
    return "image"
