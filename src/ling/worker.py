"""后台 worker：outbox 处理、悬空轮次恢复、承诺到期检查。

W0 只需要它存在且能安全空转（Compose 里有 worker 服务）。
W1/W2 会往 outbox 加 topic；本模块按 topic 注册 handler，未注册的 topic
保持 pending 并记录原因，绝不静默丢弃。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

from ling.config import get_settings
from ling.infra.db import dispose_engine, init_engine, session_scope
from ling.infra.models import OutboxEvent, Turn, utcnow

# 单条 outbox 事件的最多尝试次数。达到后转 "dead" 等人工重放，
# 不静默丢弃（注意：五次失败不等于记忆应该永久缺席）。
MAX_ATTEMPTS = 5
# 指数退避上限（秒）
MAX_BACKOFF_SECONDS = 300
# 本 worker 实例标识，写入 outbox.processed_by 便于多副本诊断
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# 用统一配置：worker 和 api 的日志格式、级别、第三方降噪必须一致，
# 否则一条链路在两个进程里长得不一样，对不起来。
from ling.logging_setup import emit_event, setup_logging  # noqa: E402

setup_logging(role="worker")
logger = logging.getLogger("ling.worker")

# worker 侧的密钥缓存与模型路由：Provider 构造需要**同步**读密钥，
# 而数据库是异步的，所以启动时预热一次，之后从内存取。
_SECRET_CACHE: dict[str, str] = {}
_MEMORY_ROUTER: object | None = None
# 本进程加载到的配置修订号；变了就重载路由与密钥
_SECRET_CACHE_REVISION: int = -1


async def init_memory_router() -> bool:
    """启动时预热模型路由（供记忆提炼使用）。

    失败不致命：拿不到路由时提炼走正则降级，记忆不会完全停止生长。
    """
    global _MEMORY_ROUTER, _SECRET_CACHE_REVISION
    try:
        from ling.infra.security import SecretBox
        from ling.models.registry import ProviderRegistry
        from ling.models.router import ModelRouter
        from ling.settings.refresh import current_revision, refresh_secrets
        from ling.settings.service import ConfigService

        settings = get_settings()
        box = SecretBox(settings)
        # 用**已发布配置**而不是原始凭据表：按凭据表读的话，
        # 用户在设置里禁用的 provider，worker 仍会继续用。
        async with session_scope() as session:
            written = await refresh_secrets(
                session,
                cache=_SECRET_CACHE,
                box=box,
                persona_id=settings.persona_id,
                test_mode_default=settings.test_mode,
            )
            revision = await current_revision(session)
            # 已下线的 provider 从缓存里清掉，避免继续被当候选
            svc = ConfigService(
                session, box, test_mode_default=settings.test_mode,
                persona_id=settings.persona_id,
            )
            enabled = set((await svc.effective()).providers)
            for pid in list(_SECRET_CACHE):
                if pid not in enabled:
                    _SECRET_CACHE.pop(pid, None)
        _MEMORY_ROUTER = ModelRouter(
            ProviderRegistry(lambda pid: _SECRET_CACHE.get(pid))
        )
        _SECRET_CACHE_REVISION = revision
        logger.info(
            "记忆提炼模型路由已就绪（%d 个凭据，配置 rev=%d）", written, revision
        )
        # **主动探一次出网**。凭据齐、路由就绪，不等于这个进程能连到模型：
        # 实测踩过 worker 只挂 `internal`（无外网）而 api 正常——所有模型
        # 调用静默失败、降级成正则，日志里只有一句"降级"，看起来毫无异常。
        # 这里在启动时把"连不上"喊出来，而不是等人几天后觉得"她怎么记不住事"。
        await _probe_provider_reachability()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("模型路由预热失败，记忆提炼将走正则降级: %s", type(exc).__name__)
        _MEMORY_ROUTER = None
        return False


# 槽位 → 实体类型。只有"可被反复提及的对象"才建实体；
# 偏好类（likes/dislikes）不建，否则实体会被"喝茶""咖啡"塞满。
_SLOT_ENTITY_KIND = {
    "has_pet": "pet",
    "family": "person",
    "ongoing_project": "project",
    "lives_in": "place",
    "works_at": "org",
}


async def _link_entity_for(
    session, *, subject_id: str, slot: str | None, value: str | None  # noqa: ANN001
) -> None:
    """按槽位为断言建立实体关联（失败不影响记忆写入）。"""
    kind = _SLOT_ENTITY_KIND.get(str(slot or ""))
    if not kind or not (value or "").strip():
        return
    try:
        from ling.domains.memory.entities import EntityService

        entities = EntityService(session)
        row = await entities.upsert(subject_id=subject_id, name=str(value), kind=kind)
        # memory_id 由 propose 返回，但这里已在同一事务里，直接查最新一条
        from sqlalchemy import select as sa_select

        from ling.infra.models import MemoryRow

        memory = (
            await session.execute(
                sa_select(MemoryRow)
                .where(
                    MemoryRow.subject_id == subject_id,
                    MemoryRow.slot == slot,
                    MemoryRow.archived_at.is_(None),
                )
                .order_by(MemoryRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if memory is not None:
            await entities.link(memory_id=memory.id, entity_id=row.id)
    except Exception as exc:  # noqa: BLE001 - 实体关联失败不影响记忆本身
        logger.info("实体关联失败（不影响记忆写入）: %s", type(exc).__name__)


async def _extract_candidates(
    *, user_text: str, actor_text: str, is_group: bool
) -> list:
    """用模型提炼候选断言；模型不可用时退回正则。

    失败一律降级、不抛：提炼失败只意味着"这轮没长记忆"，
    不该让 outbox 事件反复重试。
    """
    from ling.domains.memory.extract import (
        build_prompt,
        fallback_candidates,
        parse_candidates,
    )

    router = _MEMORY_ROUTER
    if router is None:
        return fallback_candidates(user_text)
    try:
        from ling.infra.security import SecretBox
        from ling.ports.model import ChatMessageIn
        from ling.settings.service import ConfigService

        settings = get_settings()
        async with session_scope() as session:
            effective = await ConfigService(
                session,
                SecretBox(settings),
                test_mode_default=settings.test_mode,
                persona_id=settings.persona_id,
            ).effective()
            messages = [
                ChatMessageIn(
                    role="user",
                    content=build_prompt(
                        user_text=user_text, actor_text=actor_text, is_group=is_group
                    ),
                )
            ]
            # **优先用 memory.extract 职责**，没绑才退回 dialogue.fast。
            #
            # 原来这里硬编码 `profile="dialogue.fast"`，于是设置页里的
            # `memory.extract` 是个幽灵：绑了也没有任何效果，因为从来没人
            # 按这个职责请求模型。用户会以为自己换了提炼模型，其实没换。
            #
            # 退回 dialogue.fast 是刻意的：没绑专用模型时提炼仍然工作，
            # 而不是整段失效——提炼质量差点好过完全不提炼。
            profile = (
                "memory.extract"
                if effective.profiles.get("memory.extract")
                else "dialogue.fast"
            )
            chunks: list[str] = []
            async for chunk, _outcome in router.stream(  # type: ignore[attr-defined]
                effective,
                profile=profile,
                messages=messages,
                # 提炼失败就用降级路径，不要让路由再换模型重试
                allow_fallback=False,
            ):
                if chunk.delta:
                    chunks.append(chunk.delta)
        # 模型明确给出空结果，说明"确实没有可记的"，不用正则硬凑
        return parse_candidates("".join(chunks))
    except Exception as exc:  # noqa: BLE001 - 提炼失败不能影响主流程
        # **只记异常类型是不够的**：`ModelError` 有十几种原因（超时、限流、
        # 密钥无效、职责没绑…），类型名一律是 "ModelError"，光看它没法排查。
        # 这里记确定性分类（kind）与是否可重试——它们不含正文，可以安全落日志。
        kind = getattr(getattr(exc, "kind", None), "value", "") or type(exc).__name__
        emit_event(
            logger,
            logging.WARNING,
            event_name="memory_extract_degraded",
            message="模型提炼不可用，已降级为正则",
            attributes={
                "error_kind": kind,
                "retriable": bool(getattr(exc, "retriable", False)),
                # 字段名避开 SENSITIVE_KEYS 里的 "profile"：那个词是为
                # 用户画像设的，模型职责名不该被一起打码
                "model_profile": "memory.extract -> dialogue.fast",
            },
        )
        return fallback_candidates(user_text)


async def _probe_provider_reachability() -> None:
    """探测本进程能否连到已绑定的模型供应商。

    只做 TCP 连接（不发业务请求）：目的是区分"配置错了"和"这个进程压根
    出不了网"。连不上就报 WARNING 并指明 host——这类故障不会让进程退出，
    只会让能力悄悄变差，所以必须显式喊出来。
    """
    import socket
    from urllib.parse import urlparse

    try:
        settings = get_settings()
        from ling.infra.security import SecretBox
        from ling.settings.service import ConfigService

        async with session_scope() as session:
            effective = await ConfigService(
                session,
                SecretBox(settings),
                test_mode_default=settings.test_mode,
                persona_id=settings.persona_id,
            ).effective()
            targets = {
                (getattr(p, "base_url", None) or "")
                for p in effective.providers.values()
            }
    except Exception as exc:  # noqa: BLE001 - 探测本身失败不该影响启动
        logger.info("供应商可达性探测跳过: %s", type(exc).__name__)
        return

    for base_url in sorted(t for t in targets if t):
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            continue
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError as exc:
            emit_event(
                logger,
                logging.WARNING,
                event_name="worker_provider_unreachable",
                message="worker 连不上模型供应商：本进程可能没有出网",
                attributes={
                    "host": host,
                    "port": port,
                    "error_type": type(exc).__name__,
                    # 给出可执行方向，而不是让人去猜
                    "hint": "检查该容器是否同时接入了可出网网络（compose networks）",
                },
            )
            continue
        logger.info("供应商可达：%s:%d", host, port)



Handler = Callable[[dict], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}


def handler(topic: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[topic] = fn
        return fn

    return register


async def wait_for_schema(*, attempts: int = 60, interval: float = 2.0) -> bool:
    """等 schema 就绪。

    api 容器负责建表/迁移；worker 可能在表还没建好时启动。
    这里显式等待而不是让恢复逻辑抛 UndefinedTableError。
    """
    from sqlalchemy import text

    for attempt in range(attempts):
        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1 FROM turns LIMIT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - 表不存在等都属于未就绪
            if attempt % 5 == 0:
                logger.info("等待数据库 schema 就绪（%d/%d）: %s", attempt, attempts, str(exc)[:80])
            await asyncio.sleep(interval)
    return False


@handler("memory.extract")
async def handle_memory_extract(payload: dict) -> None:
    """从一轮已完成对话中提取记忆（设计文档 §8.1 memory.extract 职责）。

    设计（取方案 b）：
    - 权威输入落库时在**同一事务**登记 outbox 任务
    - worker 异步提取，主回复不被阻塞
    - 提取失败只是"暂缓写长期记忆"，原始事件不丢，可重放

    这里用**确定性规则**提取，不调用模型：
    - 用户明确陈述的偏好/事实 → Assertion 候选（带来源事件）
    - 每轮对话 → Episode（情节）
    需要更强的语义提取时，把本函数换成调用 memory.extract 模型即可，
    接口与准入规则不变。
    """
    from ling.contracts.v1 import Origin, Visibility
    from ling.domains.memory.service import MemoryService

    subject_id = payload.get("subject_id")
    window_id = payload.get("window_id")
    turn_id = payload.get("turn_id")
    user_text = str(payload.get("user_text") or "")
    actor_text = str(payload.get("actor_text") or "")
    # 来源作用域必须跟随产生它的窗口，不能固定写 subject_private：
    # 群里说的话固定写成私聊事实，就是跨受众泄漏（此前实现的问题）。
    source_visibility = str(payload.get("source_visibility") or "subject_private")
    if not (subject_id and window_id and turn_id):
        logger.warning("memory.extract 缺少必要字段，跳过: %s", payload)
        return

    async with session_scope() as session:
        service = MemoryService(session)
        source_ids = [f"turn:{turn_id}"]
        # 情节记忆：这一轮发生了什么（长期可检索，承接"上次那个事"）
        summary_parts = []
        if user_text.strip():
            summary_parts.append(f"用户：{user_text.strip()[:500]}")
        if actor_text.strip():
            summary_parts.append(f"ling：{actor_text.strip()[:500]}")
        if summary_parts:
            # Episode 的 origin 必须如实反映"这是一轮对话的记录"，
            # 不能标成 user_statement —— 它同时包含**角色自己说的话**，
            # 而角色的话未经证实。标成用户陈述会让"角色编的内容"随
            # "用户亲口说过的经历"一起进入可直接表述的证据组。
            await service.record_episode(
                subject_id=subject_id,
                window_id=window_id,
                summary="\n".join(summary_parts),
                source_event_ids=source_ids,
                origin=Origin.external_observation,
                visibility=Visibility(source_visibility),
                source_visibility=source_visibility,
                turn_id=turn_id,
            )

        # 断言记忆：**模型提炼 + 代码闸门**。
        # 旧实现只匹配「我叫/我喜欢/我住在」三个正则，参照稿明确记录过这条路线的
        # 失败：「我一累就咬后槽牙」「我妈忌日是十一月三号」这类细节全部落空，
        # 且落空后没有兜底。现在交给模型提炼，正则只作为模型不可用时的降级路径。
        from ling.contracts.v1 import MemoryProposal
        from ling.domains.memory.extract import (
            gate,
        )

        candidates = await _extract_candidates(
            user_text=user_text,
            actor_text=actor_text,
            is_group=bool(payload.get("is_group")),
        )
        gated = gate(
            candidates, user_text=user_text, is_group=bool(payload.get("is_group"))
        )
        if gated.rejected:
            # 拒绝原因要可查：不然"为什么这条没记住"无从排查
            logger.info(
                "memory.extract 闸门拒绝 %d 条 turn=%s: %s",
                len(gated.rejected),
                turn_id,
                "; ".join(f"{c.slot}={c.value}←{r}" for c, r in gated.rejected[:4]),
            )
        for cand in gated.accepted:
            await service.propose(
                MemoryProposal(
                    proposal_id=f"auto:{turn_id}:{cand.slot}",
                    kind="assertion",
                    text=cand.text(),
                    origin=Origin.user_statement,
                    source_event_ids=source_ids,
                    visibility=Visibility(source_visibility),
                    confidence=cand.confidence,
                ),
                subject_id=subject_id,
                source_window_id=window_id,
                source_visibility=source_visibility,
                turn_id=turn_id,
                slot=cand.slot,
                value=cand.value,
                # 记录产生这条记忆的渠道账号：同一人多个号合并后要能无损拆回
                source_account=payload.get("source_account"),
            )
            # 实体关联：把"养了豆豆""家人是妈""在做 X 项目"这类
            # 可被反复提到的对象建成实体，供"沿关系定位"用。
            # 槽位→实体类型是确定性映射，不依赖模型输出格式。
            await _link_entity_for(
                session, subject_id=subject_id, slot=cand.slot, value=cand.value
            )
    logger.info("memory.extract 完成 turn=%s", turn_id)


@handler("relationship.update")
async def handle_relationship_update(payload: dict) -> None:
    """根据一轮对话更新关系与情绪（设计文档 §6.3）。

    设计要点：
    - 关系由**有归因的互动事件**驱动；讽刺/转述/身份不明不直接改投影
    - 情绪是短时衰减状态，只影响措辞，不放宽权限
    - 这里用确定性规则判类型，不调用模型（避免每轮加延迟与费用）

    判断依据（保守，宁愿少记也不误判）：
    - 用户表达感谢/关心 → warmth
    - 用户明确设边界（"别…"/"不要…"）→ boundary
    - 用户分享了个人信息（我叫/我喜欢/我住）→ disclosure
    - 用户表现出不满/指责 → conflict
    """
    import re

    from ling.contracts.v1 import Origin
    from ling.domains.affect.service import AffectService
    from ling.domains.relationship.service import RelationshipService

    subject_id = payload.get("subject_id")
    turn_id = payload.get("turn_id")
    user_text = str(payload.get("user_text") or "")
    if not (subject_id and turn_id) or not user_text.strip():
        return

    def _classify(text: str) -> str | None:
        """判定互动类型。

        顺序很重要：**冲突信号优先于边界信号**。
        "你错了，别这样" 同时含"别"和"你错了"，若先判 boundary，
        就丢掉了"信任下降"这个更重要的信号（实测暴露）。
        """
        if re.search(r"(烦|讨厌|生气|失望|你错了|受够了|不高兴)", text):
            return "conflict"
        if re.search(r"(谢谢|辛苦|感谢|有你真好|抱抱)", text):
            return "warmth"
        if re.search(r"(不要|别|不准|我不喜欢这样|别这样)", text):
            return "boundary"
        if re.search(r"(我叫|我喜欢|我住|我在做|我的)", text):
            return "disclosure"
        return None

    kind = _classify(user_text)
    async with session_scope() as session:
        if kind is not None:
            await RelationshipService(session).record_event(
                subject_id=subject_id,
                kind=kind,
                origin=Origin.user_statement,
                source_event_ids=[f"turn:{turn_id}"],
                note=user_text[:200],
                turn_id=turn_id,
            )
            # 情绪跟随互动类型微调（幅度小、可衰减）
            # 窗口局部情绪：带具体原因，绝不跨窗口（ADR-0004 修正）。
            # 另给一条强度很低的全局趋势，让"一个人有连续心情"成立，
            # 但不携带原因与对象。
            affect = AffectService(session)
            window_id = payload.get("window_id")

            async def _bump(
                emotion: str, strong: float, global_delta: float, cause: str
            ) -> None:
                # 有窗口上下文：窗口局部（带原因、不跨窗口）+ 极弱全局趋势
                # 没有窗口上下文（例如离线补处理）：只动全局，避免伪造归属
                if window_id:
                    await affect.nudge(
                        emotion,
                        strong,
                        cause=cause,
                        scope="window",
                        window_id=window_id,
                        turn_id=turn_id,
                    )
                await affect.nudge(emotion, global_delta, scope="global", turn_id=turn_id)

            # 情绪也传 turn_id：nudge 内部按 turn 幂等，
            # 同一轮重复处理不会叠加强度（此前实现的缺陷）。
            if kind == "warmth":
                await _bump("warm", 0.25, 0.08, "用户表达了善意")
            elif kind == "conflict":
                await _bump("uneasy", 0.3, 0.06, "这轮对话有点冲突")
            elif kind == "disclosure":
                await _bump("content", 0.15, 0.05, "用户愿意分享")
            # boundary：设边界不改熟悉度/信任，也不额外造情绪。
            # 边界本身已由 RelationshipService.record_event 记录为关系事实。
        else:
            # 没有明确信号也要衰减：推进时间边界（设计文档 §6.3 不空转调用模型）
            await AffectService(session).current()
    logger.info("relationship.update 完成 turn=%s kind=%s", turn_id, kind)


@handler("commitment.dispatch")
async def handle_commitment_dispatch(payload: dict) -> None:
    """把到期的承诺推进到真实投递状态（或明确标记不可达）。

    此前实现的缺口：`due_commitments()` 存在，但 worker 主循环从没调用它——
    承诺能创建、能查询、能取消，**却永远不会触发**。用户把它当提醒器就会失灵，
    而且失灵是静默的（状态一直停在 active）。

    这里不做假交付：
    - 有支持主动推送的渠道 → 走 DeliveryService 真实投递，按回执记状态
    - 没有任何渠道能主动推送（例如只有 Web）→ 记 undeliverable 并写明原因。
      ``undeliverable`` 与 ``missed`` 语义不同：前者是"根本没能力发"，
      不是"角色没做到"，用户看到的解释应该不一样。
    """
    from ling.domains.goals.service import GoalsService

    async with session_scope() as session:
        goals = GoalsService(session)
        due = await goals.due_commitments()
        if not due:
            return

        # 只认**真实投递**：没有渠道就如实标不可达，绝不写 sent。
        #
        # 原实现有渠道时直接 `delivery_state="sent"` 却没有发任何消息——
        # 这违反项目自己的硬规则"没有回执不声称已发送"，
        # 用户会以为提醒到了，实际什么都没发生（而且是静默的）。
        from ling.capabilities.channels import proactive_connector
        from ling.capabilities.delivery import DeliveryService
        from ling.ports.channel import ChannelError, OutboundMessage

        connector = proactive_connector()
        if connector is None:
            for commitment in due:
                await goals.mark_delivery(
                    commitment.commitment_id,
                    operation_id=None,
                    delivery_state="unsupported",
                )
                logger.info(
                    "承诺 %s 到期但无可主动推送的渠道（能力自检未通过），标记 unsupported",
                    commitment.commitment_id,
                )
            return

        delivery = DeliveryService(session)
        dispatched = 0
        for commitment in due:
            window_ref = payload.get("proactive_window_ref") or ""
            if not window_ref:
                await goals.mark_delivery(
                    commitment.commitment_id,
                    operation_id=None,
                    delivery_state="unsupported",
                )
                logger.info(
                    "承诺 %s 缺少可投递的会话定位（proactive_window_ref），标记 unsupported",
                    commitment.commitment_id,
                )
                continue
            try:
                outcome = await delivery.deliver(
                    connector=connector,
                    message=OutboundMessage(
                        operation_id=f"commit:{commitment.commitment_id}",
                        window_ref=window_ref,
                        text=commitment.text,
                        is_group=":group:" in window_ref,
                    ),
                    actor_id=get_settings().actor_id,
                    subject_id=commitment.subject_id,
                    require_proactive=True,
                )
            except ChannelError as exc:
                # 不确定 ≠ 失败：结果未知只能等对账，不能当成功也不能盲目重发
                state = (
                    "uncertain"
                    if exc.kind.value == "delivery_uncertain"
                    else "failed"
                )
                await goals.mark_delivery(
                    commitment.commitment_id, operation_id=None, delivery_state=state
                )
                logger.warning("承诺 %s 投递异常: %s", commitment.commitment_id, exc.kind)
                continue
            # 只有真正送达才算送达（proposed/running 是"还没结果"，
            # uncertain 要等对账）。判据集中在 DeliveryOutcome.delivered，
            # 不在这里另写一种拼法。
            _delivered = outcome.delivered
            state = "sent" if _delivered else "failed"
            await goals.mark_delivery(
                commitment.commitment_id,
                operation_id=outcome.operation_id,
                delivery_state=state,
            )
            if _delivered:
                dispatched += 1
            else:
                logger.info(
                    "承诺 %s 未送达: %s", commitment.commitment_id, outcome.detail
                )
        if dispatched:
            logger.info("承诺投递成功 %d 条", dispatched)


@handler("self.timeline")
async def handle_self_timeline(payload: dict) -> None:
    """把已完成且有证据的活动派生为"我做过这件事"。

    幂等：去重键用活动 id，重复投递只产生一条经历。
    没有完成证据的活动会被跳过——宁可少记一条，也不能编。
    """
    from ling.domains.memory.self_timeline import SelfTimelineService

    actor_id = get_settings().actor_id
    async with session_scope() as session:
        created = await SelfTimelineService(
            session, actor_id=actor_id
        ).backfill(limit=int(payload.get("limit") or 50))
    if created:
        logger.info("自我时间线：新增 %d 条经历", created)


_MEDIA_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def make_media_fetcher(connector: object | None):
    """构造取媒体内容的函数。

    `file:` 开头的是 Web 上传、落在本地磁盘的文件——它**不需要任何渠道**。
    原来这里直接用 `connector.fetch_media`，于是没有 OneBot 时整段 return，
    Web 上传的图片永远得不到描述。

    抽成模块级函数是为了能测：闭包在 handler 里取不到。
    """

    async def _fetch(media_ref: str) -> tuple[bytes, str]:
        if media_ref.startswith("file:"):
            from pathlib import Path as _Path

            path = _Path(media_ref[5:])
            return path.read_bytes(), _MEDIA_SUFFIX_MIME.get(
                path.suffix.lower(), "image/jpeg"
            )
        if connector is None:
            raise RuntimeError("没有可用渠道下载远程媒体")
        return await connector.fetch_media(media_ref)  # type: ignore[attr-defined]

    return _fetch


@handler("media.describe")
async def handle_media_describe(payload: dict) -> None:
    """给待描述的媒体证据跑一遍视觉模型。

    **拿不到就留空**：模型没绑定、下载失败、返回空——都不写描述，
    让它留在队列里下次再试。写一句占位符会把"没看到"伪装成
    "看到了但没内容"，既不诚实，又让重试永远发生不了。
    """
    from ling.capabilities.channels import build_onebot_connector
    from ling.domains.media.describe import MediaDescriber
    from ling.domains.media.service import MediaEvidenceService
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    router = _MEMORY_ROUTER
    if router is None:
        logger.info("媒体描述跳过：模型路由未就绪")
        return

    settings = get_settings()
    try:
        connector = build_onebot_connector()
    except Exception as exc:  # noqa: BLE001 - 没有渠道时只能读本地文件
        connector = None
        logger.info("媒体描述：没有可用渠道（%s），只处理本地上传的文件", type(exc).__name__)

    _fetch_media = make_media_fetcher(connector)

    limit = int(payload.get("limit") or 5)
    described = failed = 0
    async with session_scope() as session:
        service = MediaEvidenceService(session, actor_id=settings.actor_id)
        describer = MediaDescriber(
            service=service,
            fetch_media=_fetch_media,
            router=router,
            config_reader=ConfigService(
                session,
                SecretBox(settings),
                test_mode_default=settings.test_mode,
                persona_id=settings.persona_id,
            ),
        )
        for window_id in await service.windows_with_pending(limit=10):
            outcome = await describer.describe_pending(window_id=window_id, limit=limit)
            described += outcome.described
            failed += outcome.failed
            if outcome.last_error:
                logger.info("媒体描述部分失败 window=%s: %s", window_id, outcome.last_error)

    if described or failed:
        logger.info("媒体描述：成功 %d 条，失败 %d 条", described, failed)


@handler("proactive.dispatch")
async def handle_proactive_dispatch(payload: dict) -> None:
    """把有草稿的主动候选真的发出去。**默认关闭**（配置项控制）。

    默认关的理由：前面几步能在影子模式下验证，但投递一旦发生就是
    "她突然找你"。用户先看几天草稿、确认语气合适，再自己打开。
    """
    from ling.capabilities.channels import proactive_connector
    from ling.domains.proactive.dispatch import dispatch_pending
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    settings = get_settings()
    connector = proactive_connector()
    if connector is None:
        # 没有可用渠道时**什么都不做**，不标失败——这不是候选的问题。
        emit_event(
            logger,
            logging.INFO,
            event_name="proactive_dispatch_skipped",
            message="主动投递跳过：没有可主动推送的渠道",
            attributes={"reason_code": "no_proactive_channel"},
        )
        return

    async with session_scope() as session:
        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        stats = await dispatch_pending(
            session,
            actor_id=settings.actor_id,
            connector=connector,
            config_reader=config,
            subject_timezone=settings.user_timezone,
        )
    if stats.get("skipped"):
        # 门控跳过要写清**为什么**：勿扰时段、日限额、没有会话…
        # 这些是"她今天没主动找你"的全部原因，不记就只能猜。
        emit_event(
            logger,
            logging.INFO,
            event_name="proactive_dispatch_skipped",
            message="主动投递被门控跳过",
            attributes={
                "reason_code": str(stats["skipped"])[:120],
                "considered": stats.get("considered"),
            },
        )
    elif stats.get("sent") or stats.get("failed") or stats.get("rejected"):
        emit_event(
            logger,
            logging.INFO if not stats.get("failed") else logging.WARNING,
            event_name="proactive_dispatched",
            message="主动投递完成",
            attributes={
                "sent": stats.get("sent"),
                "failed": stats.get("failed"),
                "rejected": stats.get("rejected"),
                "considered": stats.get("considered"),
            },
        )


@handler("proactive.draft")
async def handle_proactive_draft(payload: dict) -> None:
    """给主动候选生成开场白。**只生成、不发送。**

    主动联系的派发整段原本缺失。直接上线"发送"太冒险——用户没法判断
    "她今天为什么突然找我"。所以先让草稿可观察：生成出来存进 draft_text，
    让人看到"她会说什么"，确认之后再开真发送。
    """
    from ling.domains.persona.service import PersonaService
    from ling.domains.proactive.draft import draft_pending
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    router = _MEMORY_ROUTER
    if router is None:
        emit_event(
            logger,
            logging.WARNING,
            event_name="proactive_draft_skipped",
            message="草稿生成跳过：模型路由未就绪",
            attributes={"reason_code": "model_router_unavailable"},
        )
        return

    settings = get_settings()
    async with session_scope() as session:
        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        view = await PersonaService(session, settings.persona_id).get(settings.persona_id)
        stats = await draft_pending(
            session,
            actor_id=settings.actor_id,
            router=router,
            config_reader=config,
            persona=str(view.system_prompt or ""),
            limit=int(payload.get("limit") or 5),
        )
    if stats["drafted"] or stats["failed"]:
        logger.info(
            "主动草稿：生成 %d 条、失败 %d 条（候选 %d）",
            stats["drafted"],
            stats["failed"],
            stats["considered"],
        )


@handler("embedding.backfill")
async def handle_embedding_backfill(payload: dict) -> None:
    """后台预计算向量，把「检索时同步补」的积压挪到看不见的地方。

    为什么需要：召回路径上的补向量是**一次外部 HTTP 调用一条**，
    而它发生在用户等回复的时候。积压一多，一次召回就是几百次串行请求。
    `_ensure_embeddings_for` 现在有 8 条上限兜底，但那是止血，不是治本——
    治本是让积压根本不存在。

    幂等：已存在的 `(memory_id, model_id)` 跳过；重复跑没有副作用。
    """
    from sqlalchemy import select

    from ling.domains.memory.providers import build_embedding
    from ling.infra.models import MemoryEmbeddingRow, MemoryEpisode, MemoryRow
    from ling.ports.embedding import EmbeddingError

    embedder = build_embedding()
    if embedder is None:
        logger.info("向量补算跳过：未配置 embedding 提供方")
        return
    model_id = str(getattr(embedder, "model_id", "") or "")
    batch = int(payload.get("batch") or 32)

    done = 0
    async with session_scope() as session:
        existing_ids = set(
            (await session.execute(
                select(MemoryEmbeddingRow.memory_id).where(
                    MemoryEmbeddingRow.model_id == model_id
                )
            )).scalars()
        )
        # 断言用 text，情节用 summary——与召回侧保持一致，
        # 否则补出来的向量和查询向量的语义空间对不上。
        targets: list[tuple[str, str]] = [
            (row.id, row.text or "")
            for row in (
                await session.execute(
                    select(MemoryRow).order_by(MemoryRow.created_at).limit(batch * 4)
                )
            ).scalars()
            if row.id not in existing_ids
        ]
        if len(targets) < batch:
            targets += [
                (row.id, row.summary or "")
                for row in (
                    await session.execute(
                        select(MemoryEpisode).order_by(MemoryEpisode.created_at).limit(batch * 4)
                    )
                ).scalars()
                if row.id not in existing_ids
            ]
        targets = [t for t in targets if t[1].strip()][:batch]
        if not targets:
            return
        try:
            vectors = await embedder.embed([text for _id, text in targets])
        except EmbeddingError as exc:
            logger.info("向量补算失败（可重试）: %s", exc)
            return
        dim = len(vectors[0]) if vectors else 0
        for (mid, _text), vec in zip(targets, vectors, strict=False):
            session.add(
                MemoryEmbeddingRow(memory_id=mid, model_id=model_id, dim=dim, vector=vec)
            )
            done += 1
    if done:
        logger.info("向量补算：写入 %d 条", done)


@handler("attention.tick")
async def handle_attention_tick(payload: dict) -> None:
    """群聊轮询：刷到才看，**一次轮询最多一次决策调用**。

    `shadow=True`（默认）时只做决策和记录，**不发送**——
    与主动候选一样，先验证"她刷到时会怎么判断"，确认无误再开真发送。

    关键成本事实：这里省的是**决策次数**。"说什么"的生成是另一次调用，
    发生在决定要回之后。把两者混成一个数字会让"省了成本"变成空话。
    """
    from ling.domains.attention.service import (
        GroupAttentionScheduler,
        make_group_chooser,
    )
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    actor_id = get_settings().actor_id
    shadow = bool(payload.get("shadow", True))
    router = _MEMORY_ROUTER
    if router is None:
        emit_event(
            logger,
            logging.WARNING,
            event_name="attention_tick_skipped",
            message="群聊轮询跳过：模型路由未就绪",
            attributes={"reason_code": "model_router_unavailable"},
        )
        return

    settings = get_settings()
    polls = calls = baseline = replied = 0
    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=actor_id)
        # 有未读的群窗口才需要游标
        from sqlalchemy import func, select

        from ling.infra.models import Message, Window

        rows = (
            await session.execute(
                select(Message.window_id, func.count(Message.id))
                .join(Window, Window.id == Message.window_id)
                .where(
                    Window.visibility == "group_scope",
                    Message.role == "user",
                    Message.attention_state == "unseen",
                )
                .group_by(Message.window_id)
            )
        ).all()
        for window_id, _count in rows:
            await scheduler.ensure_cursor(window_id)

        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        # 上限由 attention 域统一管（MAX_WINDOWS_PER_TICK）：
        # 单次 tick 里串行打太多次模型调用会拖慢整个循环。
        for window_id in await scheduler.due_windows():
            # 决策上下文由 attention 域统一构造——worker 不再自己拼，
            # 之前这里硬编码 recent_actor_turns=0，等于防刷屏上限在轮询
            # 路径上永远不生效。
            _chooser = make_group_chooser(
                session=session,
                actor_id=actor_id,
                window_id=window_id,
                router=router,
                config_reader=config,
            )

            result = await scheduler.poll_window(window_id, chooser=_chooser)
            polls += 1
            calls += result.llm_calls
            baseline += result.baseline_calls
            if result.decision == "replied":
                replied += 1
            # **每个窗口都记一条**，包括"没回"。
            # 只记"决定回应"的话，"她为什么在群里不吭声"就查不到了——
            # 而那才是绝大多数情况。decision/reason 与私聊用同一套语义。
            emit_event(
                logger,
                logging.INFO,
                event_name="attention_window_polled",
                message="群聊轮询：窗口决策完成",
                attributes={
                    "window_id": window_id,
                    "decision": result.decision,
                    "reason": result.reason or "",
                    "unseen_count": result.unseen_count,
                    "llm_calls": result.llm_calls,
                    "baseline_calls": result.baseline_calls,
                    "shadow": shadow,
                },
            )

    # **无条件记一条**，但空轮询降到 DEBUG。
    #
    # 无条件：否则"轮询跑了但没窗口到期"和"轮询压根没跑"在日志里长得一样，
    # 而那正是排查"她为什么几小时没在群里说话"时的第一个岔路口。
    # 降级：tick 间隔只有几秒，空轮询全记 INFO 会把日志刷满（实测部署实例上
    # 每 6 秒一条），真正的信号就被淹了。要排查时把级别调到 DEBUG 即可。
    saved = max(0, baseline - calls)
    emit_event(
        logger,
        logging.INFO if polls else logging.DEBUG,
        event_name="attention_tick_completed",
        message="群聊轮询完成",
        attributes={
            "windows_polled": polls,
            "llm_calls": calls,
            "baseline_calls": baseline,
            "saved_calls": saved,
            "replied": replied,
            "shadow": shadow,
        },
    )


@handler("intent.tick")
async def handle_intent_tick(payload: dict) -> None:
    """把到期的念头变成主动机会。**仍然只登记，不发送。**

    意图到期 ≠ 立刻发消息。它只是"更想说了"，仍要过
    `ProactivityService.gate()` 那套硬门（勿扰/日限额/渠道能力）。
    这样影子模式依旧有效，自主性不会绕过安全边界。

    过期未兑现的念头标记 expired，**不补发**——攒一批一起丢出去
    比不发更糟。
    """
    from ling.domains.intent.service import IntentService
    from ling.domains.proactive.service import ProactivityService
    from ling.domains.proactive.sources import REASON_INTENT_DUE

    actor_id = get_settings().actor_id
    limit = int(payload.get("limit") or 20)
    created = 0
    async with session_scope() as session:
        service = IntentService(session, actor_id=actor_id)
        expired = await service.expire_overdue()
        proactive = ProactivityService(session, actor_id=actor_id)
        for intent in await service.due(limit=limit):
            if intent.subject_id is None:
                # 没有当事人的念头没法投递给谁，直接丢弃（也不该被产生）
                await service.mark(intent.intent_id, "dropped", reason="没有当事人")
                continue
            opportunity = await proactive.propose(
                subject_id=intent.subject_id,
                kind=intent.kind,
                reason=intent.goal,
                # 来源必须是具体意图 id：事后能回答"她当时为什么想提这个"
                source_event_id=f"intent:{intent.intent_id}",
                dedupe_key=f"intent:{intent.intent_id}",
                reason_code=REASON_INTENT_DUE,
                not_before=intent.not_before,
                best_until=intent.expires_at,
                visibility=intent.visibility,
            )
            await service.mark(
                intent.intent_id,
                "consumed",
                reason="已登记为主动机会",
            )
            if opportunity is not None:
                created += 1
    if created or expired:
        logger.info("意图队列：新增机会 %d 条、过期 %d 条", created, expired)


@handler("proactive.collect")
async def handle_proactive_collect(payload: dict) -> None:
    """收集主动候选。**默认影子模式：只记录，不发送。**

    路线图阶段 4 的推进顺序：先验证候选质量，再开真发送。
    一上来就发会立刻变成定时骚扰，而且用户没法判断"它今天为什么找我"。

    真正发送要额外满足：授权、用户时区勿扰、渠道实测能力、限额、
    联系负担，以及**真实投递回执**——那些在 `ProactivityService.gate`
    与 `DeliveryService` 里，这里不重复实现。
    """
    from ling.domains.proactive.service import ProactivityService

    shadow = bool(payload.get("shadow", True))
    actor_id = get_settings().actor_id
    async with session_scope() as session:
        result = await ProactivityService(
            session, actor_id=actor_id
        ).collect_candidates(shadow=shadow)
        await ProactivityService(session, actor_id=actor_id).expire_stale()
    if result["created"] or result["skipped_duplicate"]:
        logger.info(
            "主动候选：新增 %d 条、去重跳过 %d 条（影子=%s）",
            result["created"],
            result["skipped_duplicate"],
            shadow,
        )


# 活动类别的中文说法。给用户看的文案里不该出现 `reading` 这种槽位名——
# 那是内部标识，漏到回复里会立刻显得机械。
_ACTIVITY_LABELS = {
    "reading": "读书",
    "writing": "写东西",
    "resting": "休息",
    "cooking": "做饭",
    "walking": "散步",
}


def _activity_label(kind: str) -> str:
    return _ACTIVITY_LABELS.get(str(kind), "手头的事")


async def _plan_window_ended(session, plan_id: str, *, now):  # noqa: ANN001, ANN202
    """这条计划的时间窗是否已经结束。"""
    from ling.domains.agenda.service import _aware as _as_aware
    from ling.infra.models import AgendaPlanRow

    plan_row = await session.get(AgendaPlanRow, plan_id)
    return plan_row is not None and _as_aware(plan_row.end_at) <= now


@handler("life.tick")
async def handle_life_tick(payload: dict) -> None:
    """生活推进：只在**时段边界或到点计划**时动手。

    为什么不能每分钟跑：那会让角色不断生成"完美日记"，
    既烧模型又不真实。真实的人是按事件推进的——起床、被打断、
    手头这件事做完了。

    动作范围很小：
    - 醒着且有到点的计划 → 开一个模拟活动并提交场景
    - 该歇了 → 暂停当前活动、场景切休息
    - 其余情况什么都不做（保持现状就是最自然的行为）
    """
    from ling.domains.activity.service import ActivityService
    from ling.domains.agenda.service import AgendaService
    from ling.domains.presence.service import PresenceService

    actor_id = get_settings().actor_id
    now = utcnow()
    async with session_scope() as session:
        agenda = AgendaService(session, actor_id=actor_id)
        routine = await agenda.routine()
        presence = PresenceService(session, actor_id=actor_id)
        activity = ActivityService(session, actor_id=actor_id)

        awake = routine.is_awake(now)
        current = await activity.current()

        if not awake:
            # 该歇了：把进行中的模拟活动暂停（不是完成——没做完就不算做完）
            if current is not None and current.state == "running":
                if current.origin == "simulated":
                    await activity.transition(current.activity_id, "paused")
                await presence.commit_scene(state="rest", origin="simulated")
            elif current is None:
                await presence.commit_scene(state="rest", origin="simulated")
            return

        # 计划窗口已结束且活动还在跑 → 收尾（**模拟活动**才有这条路径）。
        # 没有这条路径的话，活动永远不会 completed，自我时间线永远是空的。
        # 证据只描述"模拟活动的窗口结束了"，不冒充真实世界发生了什么。
        if (
            current is not None
            and current.state == "running"
            and current.origin == "simulated"
            and current.plan_id
            and await _plan_window_ended(session, current.plan_id, now=now)
        ):
            try:
                await activity.transition(
                    current.activity_id,
                    "completed",
                    completion_evidence=(
                        f"模拟活动「{current.kind}」的计划时段已结束（非真实经历）"
                    ),
                )
                await agenda.on_activity_completed(current.plan_id)
                await presence.clear_scene()
                logger.info("生活推进：收尾 %s", current.kind)
            except Exception as exc:  # noqa: BLE001
                logger.info("活动收尾失败: %s", exc)
            current = await activity.current()

        # 醒着：有到点的计划就开一件
        if current is None or current.state in {"proposed", "paused"}:
            due = await agenda.due_plans(now=now)
            if due:
                plan = due[0]
                if current is not None and current.state == "paused":
                    await activity.transition(current.activity_id, "running")
                    await presence.commit_scene(
                        state="focus",
                        activity_id=current.activity_id,
                        origin="simulated",
                    )
                else:
                    proposed = await activity.propose(
                        kind=plan.kind,
                        plan_id=plan.plan_id,
                        origin="simulated",
                        source_event_ids=[f"plan:{plan.plan_id}"],
                    )
                    await activity.transition(proposed.activity_id, "running")
                    await agenda.set_state(plan.plan_id, "active")
                    await presence.commit_scene(
                        state="focus",
                        activity_id=proposed.activity_id,
                        origin="simulated",
                    )
                logger.info("生活推进：开始 %s", plan.title)
            elif current is None:
                # 没有计划也没有活动：不硬编一个"我在看书"。
                # 把场景收回到 unknown，让"被问在干什么"能诚实回答。
                await presence.clear_scene()


@handler("memory.archive")
async def handle_memory_archive(payload: dict) -> None:
    """把保留期到期的记忆归档（不物理删除）。

    由 worker 定期触发（运维可投递本 topic，或由 schedule 循环产生）。
    归档不是删除：superseded_by 链与 Episode.source_event_ids 都引用 id，
    物理删除会断链，且备份恢复可能让已删内容复活。
    """
    from ling.domains.memory.service import MemoryService

    episode_days = int(payload.get("episode_days") or 180)
    async with session_scope() as session:
        service = MemoryService(session)
        archived_rows = await service.archive_expired()
        archived_episodes = await service.archive_episodes(older_than_days=episode_days)
        # 压缩级联：同槽位的多条低价值记忆并成一条高层摘要。
        # 参照实现把它放在"睡眠维护"里——不压缩的话，用几个月后
        # 证据车道会被一堆"用户喜欢过 X"的碎片占满。
        compacted = await service.compact_low_value()
    if compacted["groups"]:
        logger.info(
            "memory.archive 压缩级联：合并 %d 组、归档 %d 条",
            compacted["groups"],
            compacted["archived"],
        )
    if archived_rows or archived_episodes:
        logger.info(
            "memory.archive 完成：归档 %d 条记忆、%d 条情节",
            archived_rows,
            archived_episodes,
        )


@handler("window.summary")
async def handle_window_summary(payload: dict) -> None:
    """用模型把确定性摘要在后台换成更连贯的版本（可选，失败不影响主链）。

    分工是刻意的：
    - 主链上的 ``_rollup_summary`` 做**确定性压缩**（逐条截取要点）。
      每轮调模型做摘要会让首字延迟和费用都翻倍，而摘要只需要"接上话题"。
    - 这里在后台把它换成更连贯的版本，用 ``memory.extract`` 职责。

    三条不变式：
    1. **不推进 ``through_sequence``**。这个处理器只改善措辞，
       不改变"摘要覆盖到第几条"。推进了会跳过没进摘要的消息。
    2. **拿不到就保留原摘要**。模型不可用、返回空、抛错——一律不写。
       丢掉旧摘要换一段空话，比不改更糟。
    3. **重写后长度不能明显缩水**。模型很容易把十条压成一句"聊了聊近况"，
       那等于悄悄丢信息；缩到 40% 以下就不采纳。
    """
    from sqlalchemy import select

    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import WindowContext
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    window_id = str(payload.get("window_id") or "")
    if not window_id:
        return

    router = _MEMORY_ROUTER
    if router is None:
        logger.info("window.summary 跳过：模型路由未就绪 window=%s", window_id)
        return

    settings = get_settings()
    async with session_scope() as session:
        ctx = (
            await session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one_or_none()
        original = (ctx.summary or "") if ctx is not None else ""
        if ctx is None or not original.strip():
            return

        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        try:
            effective = await config.effective()
            rewritten = await _rewrite_summary(router, effective, original)
        except Exception as exc:  # noqa: BLE001 - 摘要失败不影响主链
            logger.info("window.summary 失败，保留原摘要 window=%s: %s", window_id, type(exc).__name__)
            return

        if not rewritten:
            return
        if len(rewritten) < len(original) * 0.4:
            logger.info(
                "window.summary 重写后明显缩水（%d -> %d），保留原摘要 window=%s",
                len(original),
                len(rewritten),
                window_id,
            )
            return

        await ConversationService(
            session, actor_id=settings.actor_id, persona_id=settings.persona_id
        ).save_summary(
            window_id,
            summary=rewritten,
            # **原样传回**：只换措辞，不动覆盖范围
            through_sequence=int(ctx.summary_through_sequence or 0),
            message_count=int(ctx.summary_message_count or 0),
        )
        logger.info("window.summary 已重写 window=%s（%d -> %d 字）", window_id, len(original), len(rewritten))


SUMMARY_PROMPT = (
    "下面是一段对话的压缩摘要草稿。请把它改写得更连贯、更像回忆："
    "保留所有人名、时间、约定和用户明确说过的偏好，不要新增原文没有的内容，"
    "不要评论，不要分点，直接给出改写结果。中文。"
)


async def _rewrite_summary(router: object, effective: object, original: str) -> str:
    """调 memory.extract 职责重写摘要。

    只用这一个职责，**不做跨职责回退**——用 dialogue 模型顺手重写摘要，
    会让对话预算被摘要任务吃掉，而且摘要质量与对话质量是两回事。
    """
    from ling.ports.model import ChatMessageIn

    chunks: list[str] = []
    async for chunk, outcome in router.stream(  # type: ignore[attr-defined]
        effective,
        profile="memory.extract",
        messages=[
            ChatMessageIn(role="system", content=SUMMARY_PROMPT),
            ChatMessageIn(role="user", content=original[:6000]),
        ],
        allow_fallback=False,
    ):
        if chunk.delta:
            chunks.append(chunk.delta)
        if outcome.error is not None:
            raise RuntimeError(outcome.error.message)
    return "".join(chunks).strip()


# 轮次多久没更新就认定"悬空"# 轮次多久没更新就认定"悬空"。真实模型一轮可能几十秒，
# 取 10 分钟留足余量；比这个更久的一定是崩溃遗留。
DANGLING_TURN_MINUTES = 10


async def recover_dangling_turns() -> int:
    """把**确实悬空**的轮次标记为 failed（可重试）。

    此前实现的真实缺陷：原实现无条件把所有 pending/streaming 轮次标为
    failed——多副本部署时 worker 启动会误杀另一个实例正在处理的轮次，
    用户看到"生成中"突然变失败。

    修正：只处理 ``updated_at`` 早于阈值的。活跃轮次每次落库都会刷新它。
    """
    from datetime import timedelta

    cutoff = utcnow() - timedelta(minutes=DANGLING_TURN_MINUTES)
    async with session_scope() as session:
        result = await session.execute(
            update(Turn)
            .where(
                Turn.state.in_(["pending", "streaming", "stopping"]),
                Turn.updated_at < cutoff,
            )
            .values(
                state="failed",
                error_code="cancelled",
                error_detail=f"超过 {DANGLING_TURN_MINUTES} 分钟无更新，判定为中断",
            )
        )
        count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        logger.warning("恢复 %d 个长时间无更新的轮次为 failed", count)
    return count


async def replay_dead_events() -> int:
    """把 dead 状态的事件重新入队（运维接口，不做自动重试）。

    为什么要人工：连续失败通常意味着规则或数据有问题，
    自动重放只会反复失败并刷日志。修好原因后再调用本函数。
    """
    async with session_scope() as session:
        result = await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.state == "dead")
            .values(state="pending", attempts=0, available_at=utcnow())
        )
        count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        logger.info("已重新入队 %d 条 dead 事件", count)
    return count


async def check_and_notify_alerts() -> dict:
    """检查运维状态并在需要时推送告警。

    由 worker 定期调用。不做这一步的话 `/v1/health/ops` 只是一个
    "要主动去看才有用"的端点——而无人值守时没人会去看。
    """
    from ling.ops.notify import notify_if_needed

    settings = get_settings()
    url = getattr(settings, "alert_webhook_url", "") or ""
    try:
        from ling.api.routes.ops import collect_status

        async with session_scope() as session:
            status = await collect_status(session)
            if not url:
                # 没配推送目标也要留下痕迹：日志里能看出"当时有告警"
                if status.get("status") != "ok":
                    logger.warning(
                        "运维告警（未配置 LING_ALERT_WEBHOOK_URL，仅记日志）: %s",
                        ", ".join(
                            i["name"]
                            for i in status["items"]
                            if i.get("severity") != "ok"
                        ),
                    )
                return {"sent": [], "skipped": [], "cleared": []}
            result = await notify_if_needed(
                session, status=status, webhook_url=url, base_url="ling-worker"
            )
        if result.get("sent"):
            logger.info("已推送告警: %s", ", ".join(result["sent"]))
        return result
    except Exception as exc:  # noqa: BLE001 - 告警检查失败不能拖垮 worker
        logger.info("告警检查失败: %s", type(exc).__name__)
        return {"sent": [], "skipped": [], "cleared": []}


async def enqueue_maintenance() -> int:
    """把周期性维护任务投递到 outbox。

    此前实现的缺口：memory.archive handler 写好但**没有调度来源**，
    永远不会自动运行（只能手工触发），到期记忆就不会被归档。

    这里由 worker 主循环定期投递。为什么用 outbox 而不是直接调用：
    - 与 设计文档 §4.3 的"同库任务 + outbox worker"一致
    - 多副本时靠 outbox 状态机去重（同一维护任务不会并行跑）
    - 失败可重试、可审计

    幂等：维护任务带日期桶（如 ``archive:2026-10-27``），
    同一桶已存在（pending/done）就不重复投递。
    """
    from datetime import UTC, datetime

    bucket = datetime.now(UTC).strftime("%Y-%m-%d")
    # 承诺投递用小时桶：到期提醒需要比每日归档更及时，
    # 但也不必每分钟扫；一小时粒度对"提醒"够用。
    hour_bucket = datetime.now(UTC).strftime("%Y-%m-%dT%H")
    task_id = f"maint_archive_{bucket}"

    created = 0
    async with session_scope() as session:
        existing = await session.get(OutboxEvent, task_id)
        if existing is None:
            session.add(
                OutboxEvent(
                    id=task_id,
                    topic="memory.archive",
                    payload={"bucket": bucket, "episode_days": 180},
                )
            )
            created += 1
        # 到期承诺：每小时扫一次，避免"能创建但永远不触发"
        dispatch_id = f"maint_commitment_{hour_bucket}"
        if await session.get(OutboxEvent, dispatch_id) is None:
            session.add(
                OutboxEvent(
                    id=dispatch_id,
                    topic="commitment.dispatch",
                    payload={"bucket": hour_bucket},
                )
            )
            created += 1
    if created:
        logger.info("已投递维护任务 %d 条（归档/承诺投递）", created)
    return created


async def enqueue_window_summaries(limit: int = 20) -> int:
    """给最近有摘要的窗口投递 ``window.summary``。

    为什么需要这个："handler 写好了但没有调度来源"已经犯过一次
    （``memory.archive`` 的注释里记着）。树倒不响没人听见——
    处理器写得再好，没人投递就等于不存在。

    筛选条件刻意保守：
    - 只取 24 小时内更新过的窗口摘要。历史窗口的摘要不会再变，
      反复重写只是白花钱。
    - 每次最多 ``limit`` 个，避免一次投递把 outbox 刷满。
    - 用日期桶做幂等键：同一天同一个窗口只投一次。

    **重复投递是安全的**：处理器只换措辞、不推进覆盖范围，
    最坏情况是多花一次模型调用，不会让数据错。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from ling.infra.models import WindowContext

    bucket = datetime.now(UTC).strftime("%Y-%m-%d")
    since = datetime.now(UTC) - timedelta(hours=24)
    created = 0
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(WindowContext.window_id, WindowContext.summary)
                    .where(
                        WindowContext.summary.is_not(None),
                        WindowContext.updated_at >= since,
                    )
                    .order_by(WindowContext.updated_at.desc())
                    .limit(limit)
                )
            )
            .all()
        )
        for window_id, summary in rows:
            # 只有空白的摘要没必要重写——那不是"能改善的措辞"，
            # 是"还没有摘要"。投进去只会让 handler 空跑一次。
            if not (summary or "").strip():
                continue
            task_id = f"maint_summary_{bucket}_{window_id}"
            if await session.get(OutboxEvent, task_id) is not None:
                continue
            session.add(
                OutboxEvent(
                    id=task_id,
                    topic="window.summary",
                    payload={"window_id": window_id, "bucket": bucket},
                )
            )
            created += 1
    if created:
        logger.info("已投递窗口摘要重写 %d 条", created)
    return created


# 维护任务投递间隔（秒）。归档是低频操作，1 小时检查一次足够。
MAINTENANCE_INTERVAL_SECONDS = 3600


# 告警检查间隔：比维护任务密得多——"它不回话了"要尽快知道
ALERT_CHECK_SECONDS = 300.0
# 生活推进间隔。注意：这**不是**"每 5 分钟生成一份日记"——
# handler 只在时段边界或计划到点时真的动作，其余时候一次查询就返回。
LIFE_TICK_SECONDS = 300.0
# 主动候选收集间隔。影子模式，只记录不发送。
PROACTIVE_COLLECT_SECONDS = 300.0

# 注意力轮询的**调度**间隔。必须显著小于轮询器自己的间隔
# （基础 30 秒 / 聚焦 8 秒），否则那些间隔只是纸面数字。
# 原先它挂在 300 秒的收集器块里，等于"聚焦 8 秒"永远不会发生。
ATTENTION_TICK_SECONDS = 5.0

# 向量预计算可以慢：它是后台补积压，早几秒晚几秒无所谓。
EMBEDDING_BACKFILL_SECONDS = 300.0

# 媒体描述：给待描述的媒体证据跑视觉模型。拿不到就留空、下次再试。
MEDIA_DESCRIBE_SECONDS = 60.0

# 自我时间线：把已完成且有证据的活动派生为"我做过这件事"。
SELF_TIMELINE_SECONDS = 300.0

# 主动草稿：给候选生成开场白（只生成不发送）。
# 跟收集器同节奏即可——刚收集到的候选下一轮就有草稿。
PROACTIVE_DRAFT_SECONDS = 300.0

# 主动投递：看配置项 proactive_delivery_enabled（默认关）。
PROACTIVE_DISPATCH_SECONDS = 300.0


def _should_run_maintenance(last: float, now: float, *, interval: float | None = None) -> bool:
    return (now - last) >= (interval or MAINTENANCE_INTERVAL_SECONDS)


async def refresh_memory_router_if_stale() -> None:
    """配置变更后重载 worker 侧的模型路由与密钥缓存。

    worker 只在启动时预热一次（`init_memory_router`）。不重载的话，
    在网页里换了密钥后，记忆提炼会一直用旧密钥静默失败并退回正则——
    用户看到的是"换了模型它还是记不住话"。
    """
    global _MEMORY_ROUTER, _SECRET_CACHE_REVISION
    if _MEMORY_ROUTER is None:
        return
    try:
        from ling.settings.refresh import current_revision

        async with session_scope() as session:
            latest = await current_revision(session)
        if latest == _SECRET_CACHE_REVISION:
            return
    except Exception as exc:  # noqa: BLE001
        logger.info("worker 配置版本检查失败: %s", type(exc).__name__)
        return
    # 走与 API 相同的重建路径
    await init_memory_router()


async def drain_outbox(limit: int = 50) -> int:
    """至少一次处理；消费方按事件 id 幂等。"""
    await refresh_memory_router_if_stale()
    processed = 0
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.state == "pending", OutboxEvent.available_at <= utcnow())
                    .order_by(OutboxEvent.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for row in rows:
            fn = HANDLERS.get(row.topic)
            if fn is None:
                row.state = "unhandled"
                row.last_error = f"没有注册 {row.topic} 的处理器（W1/W2 模块尚未接入）"
                continue
            try:
                await fn(dict(row.payload or {}))
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                row.attempts = int(row.attempts or 0) + 1
                row.last_error = str(exc)[:500]
                if row.attempts < MAX_ATTEMPTS:
                    # 指数退避（此前实现的问题）：不退避的话连续几轮轮询就会
                    # 把次数耗尽，临时故障也变成永久失败。
                    delay = min(MAX_BACKOFF_SECONDS, 2 ** row.attempts)
                    row.state = "pending"
                    row.available_at = utcnow() + timedelta(seconds=delay)
                else:
                    # 不静默放弃：转 "dead" 状态等待人工/运维重放，
                    # 原始事件仍在库里，派生记忆只是暂缺（不是物理丢失）。
                    row.state = "dead"
                logger.warning(
                    "outbox %s 处理失败（第 %d 次）: %s", row.id, row.attempts, exc
                )
            else:
                row.state = "done"
                row.processed_at = utcnow()
                row.processed_by = WORKER_ID
                processed += 1
    return processed


async def run_forever(poll_seconds: float = 2.0) -> None:
    settings = get_settings()
    init_engine(settings)
    last_maintenance = 0.0
    last_alert_check = 0.0
    await init_memory_router()
    if not await wait_for_schema():
        logger.error("数据库 schema 迟迟未就绪，worker 退出（由 Compose 重启策略重试）")
        await dispose_engine()
        raise SystemExit(1)
    await recover_dangling_turns()
    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - 某些平台
            loop.add_signal_handler(sig, _stop)

    logger.info("worker 启动 env=%s handlers=%s", settings.env, sorted(HANDLERS))
    # 启动即投递一次维护任务，避免"刚部署还没到整点"的窗口
    try:
        await enqueue_maintenance()
        # 摘要重写跟维护同一个节奏：都是小时级、都不影响主回复，
        # 但**必须有调度来源**，否则 handler 等于不存在。
        await enqueue_window_summaries()
    except Exception as exc:  # noqa: BLE001 - 维护投递失败不阻塞主循环
        logger.warning("维护任务投递失败: %s", exc)

    try:
        # 周期任务统一走注册表：加了一个 handler 就必须在 PERIODIC_JOBS
        # 里登记，否则 test_worker_dispatch 会红。这样"注册了但没人派发"
        # 在结构上就不可能——media.describe 和 self.timeline 就是这么白躺的。
        next_run: dict[str, float] = {job.topic: 0.0 for job in PERIODIC_JOBS}
        while not stop.is_set():
            try:
                if _should_run_maintenance(last_maintenance, time.monotonic()):
                    await enqueue_maintenance()
                    await enqueue_window_summaries()
                    last_maintenance = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.warning("维护任务投递失败: %s", exc)
            for job in PERIODIC_JOBS:
                if not _should_run_maintenance(
                    next_run[job.topic], time.monotonic(), interval=job.interval
                ):
                    continue
                next_run[job.topic] = time.monotonic()
                # 每件事各自 try：一件失败不能吞掉后面几件，它们彼此无关
                await _guarded(job.topic, job.run, job.payload)
            # 告警检查与维护分开计时：维护是小时级，告警要分钟级，
            # 否则"worker 刚挂"这类事要等一小时才发现。
            if _should_run_maintenance(last_alert_check, time.monotonic(), interval=ALERT_CHECK_SECONDS):
                try:
                    await check_and_notify_alerts()
                    last_alert_check = time.monotonic()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("告警检查异常: %s", exc)
            try:
                count = await drain_outbox()
                if count:
                    logger.info("outbox 处理 %d 条", count)
            except Exception as exc:  # noqa: BLE001 - worker 不能因一次失败退出
                logger.exception("outbox 轮询异常: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue
    finally:
        await dispose_engine()
        logger.info("worker 已停止")


@dataclass(frozen=True)
class PeriodicJob:
    """一个周期性后台任务。

    存在的理由：handler 注册了却没有任何调度来源时，它**等于不存在**——
    代码在这儿、测试全绿、日志无异常，功能就是不发生。
    `media.describe` 和 `self.timeline` 都这样躺了很久
    （前者让整条媒体描述链路是死的，后者让她的时间线永远为空）。

    所以定时任务统一收进 `PERIODIC_JOBS`，配合
    `tests/unit/test_worker_dispatch.py` 的闸：
    **每个注册的 topic 必须出现在这里或 outbox 里**。
    """

    topic: str
    run: object  # async (payload: dict) -> None
    payload: dict
    interval: float


PERIODIC_JOBS: tuple[PeriodicJob, ...] = (
    PeriodicJob("attention.tick", handle_attention_tick, {"shadow": True}, ATTENTION_TICK_SECONDS),
    PeriodicJob("embedding.backfill", handle_embedding_backfill, {}, EMBEDDING_BACKFILL_SECONDS),
    PeriodicJob("intent.tick", handle_intent_tick, {}, PROACTIVE_COLLECT_SECONDS),
    PeriodicJob("life.tick", handle_life_tick, {}, LIFE_TICK_SECONDS),
    PeriodicJob("media.describe", handle_media_describe, {}, MEDIA_DESCRIBE_SECONDS),
    PeriodicJob("proactive.collect", handle_proactive_collect, {"shadow": True}, PROACTIVE_COLLECT_SECONDS),
    PeriodicJob("proactive.draft", handle_proactive_draft, {}, PROACTIVE_DRAFT_SECONDS),
    PeriodicJob("proactive.dispatch", handle_proactive_dispatch, {}, PROACTIVE_DISPATCH_SECONDS),
    PeriodicJob("self.timeline", handle_self_timeline, {}, SELF_TIMELINE_SECONDS),
)

# 由 outbox 投递的 topic（不是周期任务，是事件驱动）
OUTBOX_TOPICS: frozenset[str] = frozenset({
    "commitment.dispatch",
    "memory.archive",
    "memory.extract",
    "relationship.update",
    "window.summary",
})


async def _guarded(name: str, fn, payload: dict) -> None:  # noqa: ANN001
    """跑一个后台任务，失败只记日志。

    每件事各自隔离：原来四件事共用一个 try，收集器一炸后面全跳过，
    而它们彼此无关。后台任务之间**不该有隐式依赖**。
    """
    try:
        await fn(payload)
    except Exception as exc:  # noqa: BLE001 - worker 不能因一次失败退出
        logger.warning("%s异常: %s", name, exc)


def registered_topics() -> list[str]:
    return sorted(HANDLERS)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
