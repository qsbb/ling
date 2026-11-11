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

import asyncio
import contextlib
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.audit import behavior_event, hash_account_ref
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
from ling.kernel.context import PLATFORM_MESSAGE_ID_SCOPE
from ling.kernel.decision import DecisionContext, classify_text
from ling.kernel.turn import describe_turn_failure
from ling.kernel.visibility import is_group_window_ref
from ling.logging_setup import emit_event

if TYPE_CHECKING:
    from ling.infra.models import Turn

logger = logging.getLogger("ling.kernel.bridge")

# ---------------------------------------------------------------- 出站节奏/限速
#
# 依据 docs/OUTBOUND-DELIVERY-RESEARCH.md §5 P1-2：
# - AstrBot 的 1.5–3.5 秒随机停顿是**拟人节奏**，不是限速；
# - MaiBot 用"打字时长"（中文 0.3 s/字）拟人，也不是限速；
# - 两家都没有真正的令牌桶，NapCat 侧也没有频控（§4.5）。
#
# 所以这里补三件真需要的事，全部是**工程初值、未经真实会话验证**：
# 1. 段间**最小间隔 + 抖动**：避免机械节奏被识别成机器人；
# 2. 每窗口**令牌桶**：同一个人连收 5 条会烦，群里更是刷屏；
# 3. 同窗口**串行发送**：两个并发轮次不能把两段话交错着发出去。
#
# 基础间隔**复用** `chunking.PART_DELAY_SECONDS`（1.2 s）：它本来就是这个
# 用途，而且测试把它改成 0 时整套节奏（含令牌桶）一起关掉，不必另加开关。
PART_INTERVAL_JITTER = (0.85, 1.25)
SEND_BUCKET_CAPACITY = 3.0  # 每窗口突发 3 段
SEND_BUCKET_REFILL_SECONDS = 3.0  # 之后每 3 秒补 1 段
WINDOW_GATE_LIMIT = 256  # 进程内最多保留多少个窗口的闸门

# 一轮生成最多等多久（秒）。**这是工程初值**：180 秒是原来写死的值，
# 保留它，但抽成常量便于测试注入（测试不可能真等三分钟）。
TURN_WAIT_TIMEOUT_SECONDS = 180.0


async def _sleep(seconds: float) -> None:
    """段间停顿。单独抽出来是为了测试能把它换掉（不必真等）。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _part_interval_base() -> float:
    """段间基础间隔（秒）。0 = 关掉节奏与令牌桶（测试用）。"""
    from ling.capabilities.chunking import PART_DELAY_SECONDS

    return float(PART_DELAY_SECONDS or 0.0)


class WindowSendGate:
    """一个窗口的出站闸门：**串行** + 最小间隔 + 令牌桶。

    进程内实现。跨进程的"谁在说话"由发言租约（`kernel/speak_lease.py`）管，
    但那条租约在生成结束时就释放了，而投递发生在那之后——所以发送这一层
    仍然需要自己的串行闸：两个并发轮次各自分段发送时，绝不能让同一窗口的
    两段话交错（用户看到的是 A1 B1 A2 B2）。

    跨进程的重复发送由**段级幂等**（`IdempotencyKey("delivery_part", …)`）
    挡住；这里只管节奏与顺序。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tokens = SEND_BUCKET_CAPACITY
        self._stamp = time.monotonic()

    async def pace(self, *, first: bool) -> float:
        """在**已经持有锁**时调用：等够最小间隔并取走一个令牌。

        返回实际等待秒数（只用于日志/测试断言）。
        """
        waited = 0.0
        base = _part_interval_base()
        if base <= 0:
            return waited
        if not first:
            low, high = PART_INTERVAL_JITTER
            delay = base * random.uniform(low, high)
            await _sleep(delay)
            waited += delay
        self._refill()
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) * SEND_BUCKET_REFILL_SECONDS
            await _sleep(wait)
            waited += wait
            self._refill()
        self._tokens = max(0.0, self._tokens - 1.0)
        return waited

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._stamp)
        self._stamp = now
        self._tokens = min(
            SEND_BUCKET_CAPACITY,
            self._tokens + elapsed / SEND_BUCKET_REFILL_SECONDS,
        )


_SEND_GATES: dict[str, WindowSendGate] = {}


def _gate_for(window_ref: str) -> WindowSendGate:
    gate = _SEND_GATES.get(window_ref)
    if gate is None:
        if len(_SEND_GATES) >= WINDOW_GATE_LIMIT:
            _SEND_GATES.pop(next(iter(_SEND_GATES)), None)
        gate = WindowSendGate()
        _SEND_GATES[window_ref] = gate
    return gate


def reset_send_gates() -> None:
    """清空进程内闸门（测试用；生产不需要）。"""
    _SEND_GATES.clear()


def _event_message_id_hint(external_event_id: str) -> str | None:
    """从外部事件 id 里猜出平台 message_id。

    兜底用：连接器提供了 `platform_message_id_for_event` 时优先问它。
    OneBot 与 qqbot 的拼法都是 ``"<账号>:<平台 message_id>"``，
    所以取第一个冒号后面的部分。
    """
    value = str(external_event_id or "")
    if not value:
        return None
    if ":" in value:
        tail = value.split(":", 1)[1]
        return tail or None
    return value


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


@dataclass
class TurnResult:
    """一轮生成跑完之后，`bridge` 需要知道的**全部**事实。

    为什么不是一个四元组：这份东西每加一项都在回答同一个问题——"她到底
    说出去没有、没说出去是因为什么"。四元组只能塞下正文/状态/表达/错误，
    于是「生成了但被发言仲裁压住」「说了 37 个字全被标记洗空」这两类
    **必须区分**的结局被压成同一个"没有可交付文本"，运维只能猜。
    """

    # 清洗过、真正可以交付的正文（空 = 这一轮没有可交付的话）
    text: str = ""
    state: str = "unknown"
    expression: dict | None = None
    # ``turn.failed`` 的 error 原样字典（code/message/retriable[/detail]）
    error: dict | None = None
    # 生成了内容但**没有对外交付**时的原因（`turn.completed` 的
    # ``suppressed_reason``：另一个窗口正在说话 / 中途失去发言权）。
    suppressed_reason: str | None = None
    # 出口清洗**之前**的原始字数：区分「模型没说话」与「说了被洗空」
    raw_chars: int = 0
    # 这一轮**生成超时**了（没有等到 turn.completed/failed）。
    # 正文可能还在路上，也可能已经落库；总之**不能记成"回了"**。
    timed_out: bool = False
    # 超时时已经流出来的字数（0 = 一个字都还没生成）。
    partial_chars: int = 0
    # 她想引用但 id 对不上（被降级为普通消息）。留痕，别让它静默消失。
    quote_dropped: str | None = None

    @property
    def suppressed(self) -> bool:
        return bool(self.suppressed_reason)


def describe_no_text(result: TurnResult) -> str:
    """轮次跑完了却没有可交付正文：**必须说出是哪一种**。

    三条完全不同的路，处置也完全不同，以前它们都只说一句
    「轮次结束但没有可交付文本（state=completed）」：

    - 被发言仲裁压住 → 另一个窗口正在说话（等内容/等租约，不是模型问题）
    - 正文被出口清洗洗空 → 只写了 ``<<看视频 m3>>`` / 半截标记（查标记）
    - 模型真的什么都没输出 → 查模型/上下文/提示词

    带 ``error`` 的那条（``state=failed``）保持原有措辞不变：它是线上
    2026-11-07 事故里唯一能据以行动的句子，改它等于把已知的锚点拔掉。
    """
    state = result.state
    code = str((result.error or {}).get("code") or "")
    if result.suppressed_reason:
        return (
            f"她生成了 {result.raw_chars} 字但没有对外交付："
            f"{result.suppressed_reason}"
        )
    if code:
        return f"轮次结束但没有可交付文本（state={state}，error={code}）"
    if result.raw_chars:
        return (
            f"正文被出口清洗洗空了（原始 {result.raw_chars} 字，"
            "例如只写了「<<看视频 m3>>」这类标记或半截标记）"
        )
    return f"轮次结束但没有可交付文本（state={state}）"


def describe_delivery(delivered: bool, *, parts_sent: int, parts_total: int) -> str:
    """投递之后的一句话结论：**发了几段就是几段**。

    为什么不能一律写"已回复"：`parts_sent == 0` 时对方一个字都没收到，
    而 `0 < parts_sent < parts_total` 是"她说了前半句、后半句永远没到"——
    后者比全失败更难发现，因为用户会以为她只想说那么一句。

    措辞里带上 ``1/3`` 这种数字，运维不用翻 DB 就能判断是"哪一次没发全"。
    """
    if delivered:
        return "已回复"
    if parts_total <= 0:
        # 一段都没规划出来：正文非空却切不出段，属于上游 bug，别声称发过
        return "已回复（未规划出可发送的段）"
    if parts_sent <= 0:
        return f"投递失败（0/{parts_total} 段发出，对方没有收到）"
    return f"部分送达（{parts_sent}/{parts_total} 段发出，后面的段没到）"


def describe_timeout(result: TurnResult) -> str:
    """生成超时时**必须说清**"有没有东西、在哪儿"。

    三种结局完全不同，而它们以前共用同一句「轮次结束但没有可交付文本」：
    - 已经生成了一部分（且已落库）→ 正文没丢，只是还没投递；
    - 一个字都没生成 → 这一轮真的没有内容；
    - 请求了停止但收尾还没跑完 → 正文会在收尾时落库。
    """
    seconds = int(TURN_WAIT_TIMEOUT_SECONDS)
    if result.partial_chars <= 0:
        return (
            f"生成超时（{seconds} 秒）：还没有生成任何内容，已请求停止；"
            "这一轮没有投递（不会记成已回复）"
        )
    return (
        f"生成超时（{seconds} 秒）：已生成 {result.partial_chars} 字，"
        "正文已落库、尚未投递（交给对账任务续发，不会重复发送已发段）"
    )


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
        # ``None`` = 用**当前激活**的人格（`domains/persona/current.py`，
        # 真相来源是已发布配置的 `persona_id`）。写死一个 id 等于"在设置里
        # 切换人格之后，入站消息仍然按旧人格处理"。
        persona_id: str | None = None,
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
        # 群聊判定走**唯一实现**（`kernel/visibility.py`）。原来是
        # `":group:" in window_ref` 的**子串**匹配，会把
        # `onebot:private:group:123` 这类私聊标识误判成群聊——而这个值下面
        # 还要决定往 `Window.visibility` 写什么，所以错误会落进数据库，
        # 之后 turn.py / worker.py 全都跟着错。
        is_group = is_group_window_ref(envelope.window_ref)
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
            pending_row = await conversation.add_message(
                window.id,
                role="user",
                text=text,
                attention_state="unseen",
                # 群窗口是多个人的：不记谁说的，之后就没法回指"你刚问的"
                sender_subject_id=subject_id,
            )
            # 这条消息**落库了**，所以也要记平台 id：撤回时靠它才能找到本地那行。
            # 只在她回复的路径上记是不够的——群里被撤回的多半是她还没接话的那些。
            await self._remember_platform_message_id(pending_row.message_id, envelope)
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
            observed_row = await conversation.add_message(
                window.id,
                role="user",
                text=text,
                attention_state="unseen",
                sender_subject_id=subject_id,
            )
            # 同上：这条也可能被撤回。映射不属于"回复"，属于"消息存在过"。
            await self._remember_platform_message_id(observed_row.message_id, envelope)
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
            await self._remember_platform_message_id(msg.message_id, envelope)
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
        # 重投（事件认领被释放后 OneBot 重发）时，这一轮可能**已经生成过正文、
        # 也已经发出去几段**。那种情况只续发剩下的段：**不再调一次模型**
        # （二次生成会让同一轮出现两版不同的话，而段级幂等键是按第一版记的），
        # 也不重复落一条用户消息。
        resumed_text = await self._resumable_reply(turn.id)
        if resumed_text:
            return await self._resume_delivery(
                envelope=envelope,
                turn=turn,
                window=window,
                subject_id=subject_id,
                text=resumed_text,
                is_group=is_group,
                media_ids=media_ids,
                media_note=media_note,
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
        # 记下"这条入站消息的真实平台 id"：上下文要给模型看 `[msg_id:…]`，
        # 而 `messages` 表只有本地 id。映射写进 `idempotency_keys`
        # （scope=platform_msgid，key=本地消息 id）——不新增表、不改迁移。
        await self._remember_platform_message_id(user_row.message_id, envelope)
        await self._record_followup(
            decision,
            subject_id=subject_id,
            window_id=window.id,
            message_id=user_row.message_id,
        )
        # 轮次任务用自己的会话，必须先提交，否则后台看不到 turn 行
        await self._session.commit()

        # 5) 跑轮次，收集最终文本
        turn_result = await self._run_turn(
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
            current_message_platform_id=self._platform_message_id(envelope),
        )
        reply_text = turn_result.text
        if turn_result.timed_out:
            # **生成超时**：不许当作"没有可交付文本"草草收场。
            #
            # 两条事实必须同时成立：① 正文不能丢；② 不能记成"已回复"。
            # 做法是让这一轮**优雅停止**（收尾路径把已生成的部分写进
            # `messages`），再落一条 `delivery.pending`——周期对账任务看到
            # 它就把正文发出去。续发走段级幂等，所以即使 bridge 已经发过
            # 某几段也不会重复。
            reason = describe_timeout(turn_result)
            await self._request_turn_stop(turn.id)
            await self._record_pending_delivery(
                envelope=envelope,
                turn_id=turn.id,
                subject_id=subject_id,
                text_len_readable=len(reply_text),
            )
            await conversation.correct_handled_for_turn(
                turn.id, outcome="failed", reason=reason
            )
            await self._session.commit()
            emit_event(
                logger,
                logging.WARNING,
                event_name="turn_completed",
                message="轮次超时：正文未丢，等待续发",
                attributes={
                    "turn_id": turn.id,
                    "outcome": "timeout",
                    "is_group": is_group,
                    "partial_chars": turn_result.partial_chars,
                    "raw_chars": turn_result.raw_chars,
                    "reason": reason,
                },
            )
            return BridgeResult(
                accepted=True,
                reason=reason,
                subject_id=subject_id,
                window_id=window.id,
                turn_id=turn.id,
                decision="reply",
                media_evidence_ids=media_ids,
                media_note=media_note,
                # **不许记 replied**：这一轮还没有一个字发出去。
                handled_as="failed",
                delivered=False,
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
            #
            # 但 **error 不是唯一的原因**：被发言仲裁压住、以及"说了但正文
            # 全被标记洗空"都不会带 error。以前这两条都和"模型什么都没说"
            # 共用同一句 `state=completed`，于是"她明明回了却没发出来"
            # 在日志里长得跟"她不想说"一模一样。`describe_no_text` 把它们
            # 分开说。
            code = str((turn_result.error or {}).get("code") or "")
            reason = describe_no_text(turn_result)
            emit_event(
                logger,
                logging.WARNING,
                event_name="turn_completed",
                message="轮次结束但没有可交付文本",
                attributes={
                    "turn_id": turn.id,
                    "outcome": "no_text",
                    "turn_state": str(turn_result.state),
                    "is_group": is_group,
                    "error_code": code or None,
                    "detail": (
                        describe_turn_failure({"error": turn_result.error})
                        if turn_result.error
                        else None
                    ),
                    # 三个可区分的数字/字符串：原始字数、是否被压、
                    # 以及那句人话原因（行为日志里用的是同一个值）。
                    "raw_chars": turn_result.raw_chars,
                    "suppressed": turn_result.suppressed,
                    "suppressed_reason": turn_result.suppressed_reason,
                    "reason": reason,
                },
            )
            # 生成出来了、却一个字都没发出去：那条用户消息不能记成"已回复"。
            # 库里原来会留着创建时的 `replied`（`_finalize` 只校正
            # "完成且有正文"这一种），于是"她说回了"与"对方没收到"
            # 同时成立，排查时只能靠猜。
            await conversation.correct_handled_for_turn(
                turn.id, outcome="failed", reason=reason
            )
            return BridgeResult(
                accepted=True,
                reason=reason,
                subject_id=subject_id,
                window_id=window.id,
                turn_id=turn.id,
                decision="reply",
                media_evidence_ids=media_ids,
                media_note=media_note,
                # 生成出来了、却一个字都没发出去：那条用户消息不能记成"已回复"。
                handled_as="failed",
            )

        # 6) 投递（分段顺序发，每段独立 Operation 与回执，可对账）
        delivered, parts_sent, parts_total = await self._deliver(
            envelope=envelope,
            text=reply_text,
            subject_id=subject_id,
            expression=turn_result.expression or {},
            # 段级幂等键由本轮 turn 派生（重投复用同一个 turn）
            turn_id=turn.id,
        )
        if turn_result.quote_dropped:
            emit_event(
                logger,
                logging.INFO,
                event_name="quote_degraded",
                message="引用目标不是真消息 id，已降级为普通消息",
                attributes={
                    "turn_id": turn.id,
                    "quote_dropped": turn_result.quote_dropped,
                },
            )
        emit_event(
            logger,
            logging.INFO if delivered else logging.WARNING,
            event_name="turn_completed",
            message="轮次完成" if delivered else "轮次完成但投递不完整",
            attributes={
                "turn_id": turn.id,
                "outcome": "delivered" if delivered else "partial_delivery",
                "turn_state": str(turn_result.state),
                "is_group": is_group,
                "parts_total": parts_total,
                "parts_sent": parts_sent,
            },
        )
        # 「回了」只能在**真的发出去**之后说。
        #
        # 原来这里无论 `delivered` 是什么都写 ``reason="已回复"``，于是投递
        # 全失败时行为日志成了「想回但没发出去（已回复）」——同一行里自相
        # 矛盾，运维读到的是"她说回了"，而对方一个字都没收到。逐段发出的
        # 情况则必须说清楚发了几段，否则"她只说了半句"永远查不出来。
        reason = describe_delivery(delivered, parts_sent=parts_sent, parts_total=parts_total)
        handled_as = "replied" if delivered else "failed"
        if not delivered:
            # 消息处置结果按**投递事实**改写：`turn._finalize` 记的是"生成了
            # 正文"，而"生成成功"不等于"对方收到了"。两处都记同一件事时，
            # 以更晚、更接近投递末端的那一处为准。
            await conversation.correct_handled_for_turn(
                turn.id, outcome="failed", reason=reason
            )
        return BridgeResult(
            accepted=True,
            reason=reason,
            subject_id=subject_id,
            window_id=window.id,
            turn_id=turn.id,
            decision="reply",
            reply_text=reply_text,
            delivered=delivered,
            media_evidence_ids=media_ids,
            media_note=media_note,
            handled_as=handled_as,
            parts_total=parts_total,
            parts_sent=parts_sent,
        )

    # ------------------------------------------------------------ 内部

    # ------------------------------------------------------------ 消息 id

    def _platform_message_id(self, envelope: InteractionEnvelope) -> str | None:
        """这条入站消息的平台 message id。

        只有连接器知道这件事（OneBot 是 ``f"{self_id}:{message_id}"``）。
        连接器提供了 `platform_message_id_for_event` 就问它；没有就退回
        "取第一个冒号后面的部分"——OneBot / qqbot 都是这个拼法。
        """
        probe = getattr(self._connector, "platform_message_id_for_event", None)
        if callable(probe):
            with contextlib.suppress(Exception):
                found = probe(envelope.external_event_id)
                if found:
                    return str(found)
        return _event_message_id_hint(envelope.external_event_id)

    async def _remember_platform_message_id(
        self, message_id: str, envelope: InteractionEnvelope
    ) -> None:
        """记下"本地消息 id → 平台 message id"，供上下文渲染 `[msg_id:…]`。

        写进现成的 `idempotency_keys` 表（不建新表、不动迁移）。写失败只影响
        "她能不能引用这条消息"，**绝不能**因此让这条消息处理失败。

        ## 为什么是「SAVEPOINT + 吞」

        这个函数在 `handle()` 主流程里、`commit()` **之前**被 await。原来没有
        任何保护，一次异常就穿透到 `inbound_intake.process_inbound_payload`
        的 `except Exception`（"事件处理失败，撤回认领以便重投"）——用户看到的
        正是**这条消息没收到回复**，而且每次重投都在同一处再炸一遍。

        两条具体的失败路径：

        1. **唯一键冲突**：同一个 `message_id` 被并发写（两个 worker 抢同一条
           重投事件），或在同一事务里写两次。所以这里用
           ``ON CONFLICT DO UPDATE`` 而不是"先查再插"：查与插之间没有锁，
           天生会撞；撞上就当成功，映射值以最后一次为准（重投拿到的平台 id
           才是最新的那个）。
        2. **任何 DB 错误**（连接抖动、NOT NULL 违约）。一次失败的 flush 会把
           整个 session 标成待回滚，调用方随后的 `commit()` 会拿到
           ``PendingRollbackError``——消息照样被拒收，只是换了个地方炸。
           包在 SAVEPOINT 里之后，失败只回滚这一段，主流程照常提交。
        """
        platform_id = self._platform_message_id(envelope)
        if not platform_id:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from ling.infra.models import IdempotencyKey
        from ling.infra.security import new_id

        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(IdempotencyKey)
                    .values(
                        id=new_id("idem"),
                        scope=PLATFORM_MESSAGE_ID_SCOPE,
                        key=message_id,
                        result_ref=platform_id,
                    )
                    .on_conflict_do_update(
                        index_elements=[IdempotencyKey.scope, IdempotencyKey.key],
                        set_={"result_ref": platform_id},
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 旁路失败绝不能拒收消息
            # 为什么可以吞：这条映射只决定"上下文里能不能给模型看
            # `[msg_id:…]`"（同时是出站引用白名单的依据）。没有它，她仍然会
            # 回复、仍然落库、仍然投递，只是说不出"我引用的那条"；下一条消息
            # 会再试。而抛出去等于拿"引用元数据"换"整条消息"，代价完全不对等。
            emit_event(
                logger,
                logging.WARNING,
                event_name="platform_message_id_record_failed",
                message="没记下这条消息的平台 id（不影响回复，只是她没法引用它）",
                attributes={
                    "reason_code": "platform_message_id_record_failed",
                    "error_type": type(exc).__name__,
                },
            )
            # 行为日志：这是"她这一侧"的残缺能力（无法引用那条消息），
            # 与消息收没收到是两件事，用 outcome=degraded 标出来。
            behavior_event(
                "platform_message_id_record_failed",
                "这条消息的平台 id 没记下来（她没法用它引用这条消息，但回复照常）",
                outcome="degraded",
                attributes={"error_type": type(exc).__name__},
            )

    # ------------------------------------------------------------ 重投续发

    async def _resumable_reply(self, turn_id: str) -> str | None:
        """这一轮**已经生成过正文、而且已经发过至少一段**时，返回那段正文。

        为什么要续发而不是重新生成：① 二次生成会让同一轮出现两版不一样的话；
        ② 段级幂等键是按第一版的段序号记的，换一版文字等于"已发段被跳过、
        剩下的段发成另一句话"——那样用户会看到两段互不衔接的内容。
        """
        from ling.capabilities.delivery import PART_SCOPE
        from ling.infra.models import IdempotencyKey, Message

        text = (
            await self._session.execute(
                select(Message.text)
                .where(Message.turn_id == turn_id, Message.role == "actor")
                .order_by(Message.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not text:
            return None
        claimed = (
            await self._session.execute(
                select(IdempotencyKey.key)
                .where(
                    IdempotencyKey.scope == PART_SCOPE,
                    IdempotencyKey.key.like(f"plan_{turn_id}:%"),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return str(text) if claimed else None

    async def _resume_delivery(
        self,
        *,
        envelope: InteractionEnvelope,
        turn: Turn,
        window: Window,
        subject_id: str,
        text: str,
        is_group: bool,
        media_ids: list[str],
        media_note: str | None,
    ) -> BridgeResult:
        """重投时只**续发剩下的段**（已发段由段级幂等跳过）。"""
        from ling.domains.conversation.service import ConversationService

        delivered, parts_sent, parts_total = await self._deliver(
            envelope=envelope,
            text=text,
            subject_id=subject_id,
            expression={},
            turn_id=turn.id,
        )
        reason = describe_delivery(delivered, parts_sent=parts_sent, parts_total=parts_total)
        emit_event(
            logger,
            logging.INFO if delivered else logging.WARNING,
            event_name="turn_completed",
            message="续发剩余段",
            attributes={
                "turn_id": turn.id,
                "outcome": "resumed" if delivered else "partial_delivery",
                "resumed": True,
                "parts_total": parts_total,
                "parts_sent": parts_sent,
                "is_group": is_group,
            },
        )
        if not delivered:
            conversation = ConversationService(
                self._session, actor_id=self._actor_id, persona_id=self._persona_id
            )
            await conversation.correct_handled_for_turn(
                turn.id, outcome="failed", reason=reason
            )
        await self._session.commit()
        return BridgeResult(
            accepted=True,
            reason=reason,
            subject_id=subject_id,
            window_id=window.id,
            turn_id=turn.id,
            decision="reply",
            reply_text=text,
            delivered=delivered,
            media_evidence_ids=media_ids,
            media_note=media_note,
            handled_as="replied" if delivered else "failed",
            parts_total=parts_total,
            parts_sent=parts_sent,
        )

    # ------------------------------------------------------------ 超时收尾

    def _coordinator_call(self, name: str, *args: object) -> object | None:
        """调用协调器上的可选方法（测试会塞一个假协调器进来）。"""
        fn = getattr(self._coordinator, name, None)
        if not callable(fn):
            return None
        with contextlib.suppress(Exception):
            return fn(*args)
        return None

    async def _request_turn_stop(self, turn_id: str) -> None:
        """让超时的这一轮优雅停下（把已生成的部分落库）。"""
        self._coordinator_call("request_stop", turn_id)

    async def _record_pending_delivery(
        self,
        *,
        envelope: InteractionEnvelope,
        turn_id: str,
        subject_id: str,
        text_len_readable: int,
    ) -> None:
        """落一条"该发但还没发"的投递记录。

        为什么必须落库：进程一重启，"还有话没发"这件事在内存里就没了。
        `delivery.pending` 行由周期对账任务捡起来续发（段级幂等保证不重复）。
        """
        from ling.capabilities.delivery import DeliveryService

        await DeliveryService(self._session).record_pending(
            connector_id=getattr(self._connector, "connector_id", None),
            actor_id=self._actor_id,
            subject_id=subject_id,
            window_ref=envelope.window_ref,
            is_group=is_group_window_ref(envelope.window_ref),
            plan_id=f"plan_{turn_id}",
            turn_id=turn_id,
            reason="generation_timeout",
            detail=(
                f"生成超过 {int(TURN_WAIT_TIMEOUT_SECONDS)} 秒未结束；"
                f"返回时可见正文 {text_len_readable} 字"
            ),
        )

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

        **写不进去也不能拒收消息**：这条意图只是"过一会儿主动提一句"的备忘，
        而调用点就在 `handle()` 的主流程里（落完消息、`commit()` 之前）。
        `record_followup` 内部会 `flush()`，并发/重投撞同一个 dedupe_key 时
        抛 `IntegrityError`；不包 SAVEPOINT 的话，这个异常会穿透成
        "事件处理失败，撤回认领以便重投"——为了丢一句提醒，把整条消息的处置
        也丢了（见 `_remember_platform_message_id` 的同类说明）。
        """
        followup = getattr(decision, "followup", None)
        if followup is None:
            return
        from ling.domains.intent.service import record_followup

        try:
            async with self._session.begin_nested():
                await record_followup(
                    self._session,
                    actor_id=self._actor_id,
                    followup=followup,
                    subject_id=subject_id,
                    window_id=window_id,
                    source_ref=f"msg:{message_id}",
                )
        except Exception as exc:  # noqa: BLE001 - 旁路失败绝不能拒收消息
            # 可以吞：她"晚点再提"的念头丢了，这条消息本身仍然会照常回/照常沉默，
            # 而且下一条消息里她还能重新产生同样的念头。
            emit_event(
                logger,
                logging.WARNING,
                event_name="followup_record_failed",
                message="没记下「稍后再提」的意图（不影响这条消息的处理）",
                attributes={
                    "reason_code": "followup_record_failed",
                    "error_type": type(exc).__name__,
                },
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
                # 每个媒体一个 SAVEPOINT：登记是**旁路**（本函数的文档就在说
                # "媒体登记失败不阻断文本轮次"），而 `record()` 内部会
                # `flush()`——并发/重投撞 `uq_media_evidence_dedupe`（或任何
                # DB 错误）时抛的是 `IntegrityError`，不在下面那个
                # `MediaEvidenceError` 分支里。不包 SAVEPOINT 的话，一次撞键
                # 就会穿透到 `handle()` 外面变成"事件处理失败、撤回认领"
                # （用户看到的是"发了图，她完全没反应"），而且失败的 flush
                # 会把 session 标成待回滚，后面 `commit()` 还会再炸一次。
                async with self._session.begin_nested():
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
            except Exception as exc:  # noqa: BLE001 - 旁路失败不能拒收消息
                # 为什么可以吞：这条证据没登记上，最坏结果只是这一张图没被描述；
                # 同一张图往往已被并发的另一次处理登记过（撞键就是这么来的），
                # 正文轮次更不该因此中断。原因带回去给调用方，不静默。
                # 但它不属于"预期内的拒绝"（那是 MediaEvidenceError），
                # 所以单独记一条 WARNING，别和授权拒绝混在同一行 INFO 里。
                emit_event(
                    logger,
                    logging.WARNING,
                    event_name="media_evidence_record_failed",
                    message="媒体证据没登记上（不影响这条消息的正文轮次）",
                    attributes={
                        "reason_code": "media_evidence_record_failed",
                        "error_type": type(exc).__name__,
                    },
                )
                failures.append(f"媒体登记失败（{type(exc).__name__}）")
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

    def _current_capabilities(self):
        """这条入站消息**所属渠道**的能力声明，喂给提示词决定能列哪些动作。

        返回 `CapabilityDescriptor`，取不到时返回 ``None``（按"没有外部渠道"
        处理，提示词不列渠道动作）。

        ## 为什么可以放心用 `self._connector`

        `InboundBridge` 是**每条入站事件构造一次**的（见
        `inbound_intake`：`InboundBridge(session, connector=connector, ...)`），
        而那个 `connector` 就是**这条消息来的那个实例**。所以这里取到的必然是
        对的号，不是"随便哪个连接器"。

        ## 但这个前提将来会变

        多实例运行时改造（`b323ed6` 那轮只做了数据层，运行时还没做）之后，
        如果 `InboundBridge` 被改成复用、或改成持有"渠道 → 连接器"的表，
        那么 `self._connector` 就不再天然等于"这条消息的来源实例"了。
        **到那时这里必须改成按 `envelope.instance_id` 取对应的那个**，
        否则能力会从一个实例串到另一个实例上——而这不会报错，只会让她
        在 A 号上看到 B 号才能做的动作。

        写下这段是因为"取错号"的失败是**静默**的：提示词里多一个 `[poke:…]`
        没有任何日志。
        """
        caps = getattr(self._connector, "capabilities", None)
        if not callable(caps):
            return None
        try:
            return caps()
        except Exception:  # noqa: BLE001 - 取不到能力不该让整轮失败
            # 能力只影响提示词**列什么**，不影响这一轮能不能跑。
            # 取不到就按无渠道处理（fail-closed：不列做不到的动作）。
            return None

    async def _run_turn(
        self,
        *,
        window_id: str,
        subject_id: str,
        turn_id: str,
        text: str,
        effective: object,
        source_account: str | None = None,
        # 当前这条入站消息的平台 message id（连接器给的）。上下文靠它渲染
        # `[msg_id:…]`——她最常引用的就是"正在回的这条"。
        current_message_platform_id: str | None = None,
        # 这条消息里的称呼（群里 = 群名片优先）。与 `source_account` 一样，
        # 它属于"这一条消息"，不是"这个人"，所以只能沿调用链透传，
        # 不能从库里读——库里那一列是旁路同步的结果，可能还没写进去。
        sender_name: str | None = None,
    ) -> TurnResult:
        """跑一轮，返回 ``TurnResult``（正文/状态/表达方式/失败原因/为什么没交付）。

        失败原因（``error``）是 ``turn.failed`` 事件里的原样字典
        （``code / message / retriable``，未捕获异常那条路多一个 ``detail``）。
        它必须往上传：调用方要拿 ``code`` 写进"没有可交付文本"的原因里，
        否则运维只知道轮次失败、不知道去改哪里。正常结束时为 ``None``。

        ``suppressed_reason`` 是 ``turn.completed`` 自带的"生成了但没对外
        交付"的原因（发言权被另一个窗口占着）。它以前被这里**整条丢掉**：
        事件里写着"另一个窗口正在说话，内容未对外交付"，而 bridge 只能说
        "没有可交付文本（state=completed）"——两种完全不同的故障在日志里
        长得一模一样。
        """
        import asyncio

        queue = await self._coordinator.run_turn(  # type: ignore[attr-defined]
            window_id=window_id,
            subject_id=subject_id,
            turn_id=turn_id,
            user_text=text,
            config=effective,
            # 这一轮所在渠道的能力声明，决定提示词能列哪些动作
            # （`[poke:…]` 只有真的能拍的渠道才该出现）。
            channel_capabilities=self._current_capabilities(),
            # 记忆要记下"是哪个号说的"，合并后才能无损拆分
            source_account=source_account,
            # 「当前这句是谁说的」——群里用它给最后一行署名（群名片优先）
            sender_name=sender_name,
            current_message_platform_id=current_message_platform_id,
        )
        reply_text = ""
        partial_text = ""
        state = "unknown"
        expression: dict | None = None
        error: dict | None = None
        suppressed_reason: str | None = None
        raw_chars = 0
        quote_dropped: str | None = None
        timed_out = False
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=TURN_WAIT_TIMEOUT_SECONDS
                )
            except TimeoutError:
                # **超时不等于没内容**：已经流出来的部分还在（`CancelRegistry`
                # 逐 delta 记着），而且生成任务仍在跑、最终会把正文写进
                # `messages`。所以这里**不能**当作"没有可交付文本"收场。
                timed_out = True
                logger.warning(
                    "轮次 %s 超过 %.0f 秒未结束（已生成 %d 字），转交收尾/续发",
                    turn_id,
                    TURN_WAIT_TIMEOUT_SECONDS,
                    len(partial_text),
                )
                break
            if event is None:
                break
            name = getattr(event, "event", "")
            data = getattr(event, "data", {}) or {}
            if name == "delta":
                # 记下已流出的部分：超时收场时要用它判断"是不是真的什么都没说"。
                partial_text += str(data.get("text") or "")
                continue
            if name in {"turn.completed", "turn.stopped", "turn.failed"}:
                state = str(data.get("state") or name)
                message = data.get("message") or {}
                reply_text = str(message.get("text") or "")
                expression = data.get("expression") or expression
                # 事件里已经有这两个事实，就必须接住（见 docstring）。
                suppressed_reason = str(data.get("suppressed_reason") or "") or None
                quote_dropped = str(data.get("quote_dropped") or "") or quote_dropped
                with contextlib.suppress(TypeError, ValueError):
                    raw_chars = int(data.get("raw_chars") or 0)
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

        cleaned = clean_reply_text(reply_text).strip()
        if not cleaned and reply_text:
            # 事件里带来的正文非空，在**这一层**被洗空了：这不是"模型没说话"。
            # 原始字数取两者之间较大的那个，`describe_no_text` 才说得清。
            raw_chars = max(raw_chars, len(reply_text))
        return TurnResult(
            text=cleaned,
            state="timeout" if timed_out else state,
            expression=expression,
            error=error,
            suppressed_reason=suppressed_reason,
            raw_chars=raw_chars,
            timed_out=timed_out,
            # 只报字数、不报正文：日志/返回值里不需要把她的话再抄一份。
            partial_chars=len(partial_text),
            quote_dropped=quote_dropped,
        )

    async def _deliver(
        self,
        *,
        envelope: InteractionEnvelope,
        text: str,
        subject_id: str,
        expression: dict | None = None,
        # 段级幂等键的来源：()`plan_{turn_id}`, 序号)`。**必须由 turn 派生**——
        # 原实现用的是 `plan_{uuid4()}`，重投必然拿到新 plan_id，幂等直接失效，
        # 那正是"重投必然重发第 1 段"的根因（调研 §5 P0-1）。
        turn_id: str | None = None,
    ) -> tuple[bool, int, int]:
        """把一条回复按语义分段顺序投递。返回 (是否全部送达, 已发段数, 总段数)。

        **每段只在上一段拿到真实回执之后才发**——这就是"不是一次全倒出去"。

        六条硬约束：
        1. 群聊恒为 1 段：拆成多条会绕过防刷屏上限（`group_turn_budget`
           数的是消息条数）
        2. **分段必须按渠道硬上限切**：`plan_parts` 收
           `max_part_chars=capabilities().max_text_length`。这是物理约束，
           与 `MAX_SEGMENT_CHARS`（220，只管"切不切"）不是一回事。
           拿不到能力时 `plan_parts` 用保守兜底值，也绝不产出"肯定发不出去"的段
        3. **段级幂等**：`(plan_id, ordinal)` 已送达 → **跳过**；
           结果未知（uncertain / 运行中）→ **不重发**，交给对账。
           这一条让"事件认领被释放后的重投"变成"续发剩下的段"。
        4. 回执缺失/不明/失败 → **停止后续段**，不盲目重发
        5. 段间节奏与限速：最小间隔 + 抖动 + 每窗口令牌桶（见文件顶部常量），
           并把**同一窗口**的发送串行起来（两个轮次不能交错说话）
        6. 段间停顿是**模拟节奏**，绝不写进 delivery 回执

        为什么超限不再导致"部分送达 + 尾段丢失"：切分在这里就按渠道上限做完
        （见约束 2），所以 `DeliveryService.deliver` 里的超限拦截只是防御性的，
        正常情况下不可达。原先的 `break` 也因此只对**真发不出去**的失败生效
        （渠道不可达/平台拒收），而不是"这一段太长"。
        """
        from ling.capabilities.chunking import plan_parts
        from ling.capabilities.delivery import DeliveryService
        from ling.contracts.v1 import OperationState
        from ling.ports.channel import OutboundMessage

        is_group = is_group_window_ref(envelope.window_ref)
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

        # **确定性** plan_id：同一轮重投必须落回同一组段键。
        plan_id = f"plan_{turn_id}" if turn_id else f"plan_{uuid.uuid4().hex[:16]}"
        use_plan = turn_id is not None
        service = DeliveryService(self._session)

        # 引用段已经是**核对过的真 id**：`turn._finalize` 用上下文里真的渲染
        # 出来的 `[msg_id:…]` 白名单核过一遍，连接器发之前还会再核一遍
        # "这个 id 我到底见过没有"（NapCat 对无效 reply 只会静默丢该段）。
        # 这里只负责把它塞进出站消息。
        reply_to = (expression or {}).get("quote")

        gate = _gate_for(envelope.window_ref)
        sent = 0
        skipped = 0
        attempted = 0
        for index, part in enumerate(parts, 1):
            payload_extra = {
                "reply_plan": plan_id,
                "part_ordinal": index,
                "parts_total": len(parts),
            }
            async with gate.lock:
                # 节奏与令牌桶都在锁内：同一窗口的两个轮次不会交错发送。
                await gate.pace(first=attempted == 0)
                outcome = await service.deliver(
                    connector=self._connector,  # type: ignore[arg-type]
                    message=OutboundMessage(
                        operation_id=f"op_{uuid.uuid4().hex[:16]}",
                        window_ref=envelope.window_ref,
                        text=part,
                        is_group=is_group,
                        # **她自己选的**表达方式。代码不再替她决定"群里就该 @"——
                        # 那是把人设的判断写死在代码里。这里只负责"选了就做到"。
                        mentions=list((expression or {}).get("at") or []),
                        reply_to=reply_to,
                        poke_targets=list((expression or {}).get("poke") or []),
                    ),
                    actor_id=self._actor_id,
                    subject_id=subject_id,
                    payload_extra=payload_extra,
                    plan_id=plan_id if use_plan else None,
                    part_ordinal=index if use_plan else None,
                )
                # 每段结束就提交：已发的段与段键必须**先落库**，否则重投时
                # 看不到它们，等于幂等键不存在。
                await self._session.commit()

            if outcome.skipped:
                if outcome.state is OperationState.succeeded:
                    # 这一段上一次真的发出去过 → 跳过，但算进"已送达段数"：
                    # 平台上确实有这一段，报 0/N 会误导运维。
                    sent += 1
                    skipped += 1
                    continue
                # uncertain / 运行中：**绝不重发**（重发就是重复消息）。
                # 后面的段也不能抢跑：顺序错乱比少一段更难解释。
                emit_event(
                    logger,
                    logging.WARNING,
                    event_name="delivery_uncertain_skipped",
                    message="这一段上一次结果未知，不重发；本轮停在这里等对账",
                    attributes={
                        "channel": getattr(self._connector, "connector_id", ""),
                        "operation_id": outcome.operation_id,
                        "part_ordinal": index,
                        "parts_total": len(parts),
                        "reason": outcome.detail[:200],
                    },
                )
                break
            attempted += 1
            # `delivered` 是**唯一**判据，不再在这里另写一种拼法
            if not outcome.delivered:
                if outcome.state is OperationState.uncertain and outcome.operation_id:
                    # 不确定 → **立刻对一次账**（有 id 走 get_msg，没 id 走历史比对）。
                    # 查到就继续；查不到就保持 uncertain 交给周期对账，**不重发**。
                    reconciled = await service.reconcile(
                        self._connector, outcome.operation_id  # type: ignore[arg-type]
                    )
                    await self._session.commit()
                    if reconciled.state is OperationState.succeeded:
                        sent += 1
                        continue
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

        if sent < len(parts):
            # 分段投递的**汇总**：前面几段已经真的发出去了，后面的段既不重试
            # 也不补发（渠道明确拒收时盲目重发只会刷屏）。所以必须把
            # 「发了几段、还剩几段没尝试」说清楚——否则事后只能从
            # `operations` 里数行，而**没尝试发的那几段根本没有行**
            # （没有 operation / 没有回执），DB 里看不出它们存在。
            emit_event(
                logger,
                logging.WARNING,
                event_name="delivery_aborted",
                message=(
                    f"投递中止：{sent}/{len(parts)} 段已发出，"
                    f"剩余 {len(parts) - sent} 段没有尝试发送"
                ),
                attributes={
                    "channel": getattr(self._connector, "connector_id", ""),
                    "parts_sent": sent,
                    "parts_total": len(parts),
                    "parts_not_attempted": len(parts) - sent,
                    "parts_skipped_already_sent": skipped,
                    "is_group": is_group,
                },
            )

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
