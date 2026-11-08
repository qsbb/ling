"""后台 worker：outbox 处理、悬空轮次恢复、承诺到期检查。

W0 只需要它存在且能安全空转（Compose 里有 worker 服务）。
W1/W2 会往 outbox 加 topic；本模块按 topic 注册 handler，未注册的 topic
保持 pending 并记录原因，绝不静默丢弃。

## 三种派发源（`tests/unit/test_worker_dispatch.py` 会强制对齐）

- `PERIODIC_JOBS`：进程内定时（`life.schedule` 每 60 秒扫 DB 到期边界）；
- `OUTBOX_TOPICS`：事件驱动（`schedule.outline` / `schedule.seam` /
  `schedule.refine` / `dream.generate` / `sleep.settle` 都由扫描在到期时投递）；
- `LEGACY_MANUAL_TOPICS`：**有意**只保留手工触发（`life.plan`：新时间轴
  接管后不再自动生成，见 `handle_life_plan` 的说明）。

## outbox 的事务边界

认领、执行、确认是三段：认领与确认各是一个短事务，**handler 在事务外跑**。
模型调用（大纲/细化/梦）因此不会把 `outbox_events` 的行锁和连接一起占住。
详见"outbox 事务边界"一节。
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from ling.config import get_settings
from ling.domains.media.errors import MediaRefError
from ling.infra.db import dispose_engine, init_engine, session_scope
from ling.infra.models import OutboxEvent, Turn, utcnow

if TYPE_CHECKING:
    from ling.ports.config import EffectiveConfig

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
            # token 额度按**本地日**清零，与总览/勿扰用同一份时区解析
            # （DB 偏好 > LING_USER_TIMEZONE > Asia/Shanghai）。继续写死
            # `settings.user_timezone` 的话，用户在设置页填的时区对额度日界
            # 无效——"今天已用"会在本地凌晨前的某个时刻被清零。
            from ling.settings.user_preferences import (
                effective_user_timezone,
                owner_subject_id,
            )

            quota_timezone = await effective_user_timezone(
                session,
                subject_id=await owner_subject_id(session),
                env=settings.user_timezone,
            )
        # 记忆提炼走的是同一家供应商，也就该记同一份额度——否则 worker
        # 这边用的 token 不计数，用户在设置页看到的"今日已用"会偏小，
        # 而额度判断却在 api 进程里按那个偏小的数放行。
        from ling.infra.db import get_session_factory
        from ling.models.quota import TokenQuotaStore

        _MEMORY_ROUTER = ModelRouter(
            ProviderRegistry(lambda pid: _SECRET_CACHE.get(pid)),
            TokenQuotaStore(get_session_factory(), timezone=quota_timezone),
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


async def _published_config(session) -> EffectiveConfig | None:  # noqa: ANN001
    """现读已发布配置（worker 侧每次事务读一次，不跨事件缓存）。

    给**要按职责绑定装配 provider** 的路径用（目前是向量补算：它必须和
    检索侧用同一个嵌入模型，否则补出来的向量按 model_id 对不上、等于白跑）。
    与 ``published_memory_policy`` 同一条理由：worker 没有"轮次快照"可继承，
    发布后下一个事件就该生效。读失败按"没有任何绑定"处理——那时退回环境
    变量，而不是让整个事件失败。
    """
    try:
        from ling.infra.security import SecretBox
        from ling.settings.service import ConfigService

        settings = get_settings()
        return await ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        ).effective()
    except Exception as exc:  # noqa: BLE001 - 读不到配置就走环境变量
        logger.info("已发布配置读取失败，按未绑定处理: %s", type(exc).__name__)
        return None


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
            # **只用 `memory.extract` 一个职责**，不借主对话模型。
            #
            # 从前这里是"没绑专用模型就退回 dialogue.fast"。上了默认槽位之后
            # 这条路必须删掉：那是**跨角色回退**，界面会显示「记忆提炼 不可用」，
            # 实际却在跑主对话的模型——两边说的话不一样，用户永远查不出来。
            # 现在没配就是没有，走下面的正则降级，并在设置页说清丢了什么。
            #
            # 能不能提炼**不在这里判断**：`resolve_profile` 说了算，
            # 它把「显式 → 创作槽位 → 不可用」判完，路由再据此抛错。
            chunks: list[str] = []
            async for chunk, _outcome in router.stream(  # type: ignore[attr-defined]
                effective,
                profile="memory.extract",
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
                "model_profile": "memory.extract",
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
    # 群聊判定**从 source_visibility 派生**，不读 payload 里并不存在的 is_group。
    #
    # 生产者（kernel/turn.py 的 memory.extract 入队处）只写 source_visibility，
    # 从来没有 is_group，于是原来的 `bool(payload.get("is_group"))` 恒为 False：
    # 群聊里提炼出 boundary/health/family/birthday 本该被闸门拒绝，
    # 实际全部沉淀成了长期记忆，群聊提示词里的附加约束也永不追加。
    #
    # 以 source_visibility 作唯一真源：它本来就由 window_row.visibility 派生，
    # 而"群聊"的定义就是 group_scope，两者不可能互相矛盾，也不需要第二处
    # 生产者同步维护。显式的 is_group 若将来出现，只允许它**追加**群聊语义
    # （fail-closed）：任何一方说"这是群聊"，就按群聊处理。
    is_group = source_visibility == Visibility.group_scope.value or bool(
        payload.get("is_group")
    )
    if not (subject_id and window_id and turn_id):
        logger.warning("memory.extract 缺少必要字段，跳过: %s", payload)
        return

    async with session_scope() as session:
        # 策略**现读**已发布修订：每个 outbox 事件一次事务，没有轮次快照可继承。
        # 不传的话写入的留期一律落回列默认 365，设置页的 max_retention_days
        # 永远不生效（本任务修的就是这条断链）。
        from ling.settings.service import published_memory_policy

        service = MemoryService(session, policy=await published_memory_policy(session))
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
            is_group=is_group,
        )
        gated = gate(candidates, user_text=user_text, is_group=is_group)
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


async def _commitment_window_ref(
    session: object, *, commitment: object, connector: object
) -> str:
    """承诺到期该发到哪：`Window.id` → 平台 `window_ref`（`Window.title`）。

    两跳是必须的：承诺行记的是 `window_id`（本地会话主键），而连接器要的是
    `window_ref`（如 `onebot-x:private:9001`，存在 `Window.title` 里）。
    直接把 id 当 ref 传下去，连接器会把 `win_xxx` 当成 QQ 号——
    发错人是比发不出去严重得多的事故。

    顺序：
    1. 承诺自己记的窗口，且**属于同一渠道**（`connector_id`）→ 用它。
       跨渠道发不行：OneBot 的 ref 给 qqbot 连接器解析不出目标。
    2. 承诺没记窗口（历史数据 / 面板建的单没带 `window_id`），或记的窗口
       属于别的渠道 / 已归档 / 是群聊 → 回落到"这个人最近活跃的**同渠道
       私聊**窗口"。不回落的话，库里已有的承诺行会立刻被标
       `undeliverable`，等于换一种方式继续永久失效。
    3. 都找不到 → 返回空串，由调用方如实标 `unsupported`。

    为什么只挑私聊：到期提醒是一对一的事。往群里发"该吃药了"是泄漏。
    """
    from ling.infra.models import Window

    channel = str(getattr(connector, "connector_id", "") or "")
    subject_id = str(getattr(commitment, "subject_id", "") or "")
    window_id = getattr(commitment, "window_id", None)

    if window_id:
        stmt = select(Window).where(Window.id == window_id, Window.archived.is_(False))
        if channel:
            stmt = stmt.where(Window.channel == channel)
        row = (await session.execute(stmt)).scalars().first()  # type: ignore[attr-defined]
        # 群窗口的 title 也是 window_ref，这里必须挡住：承诺提醒不发到群里
        if row is not None and row.visibility != "group_scope" and row.title:
            return str(row.title)

    if not (subject_id and channel):
        return ""
    # 与 `domains/proactive/dispatch.py::_target_window_ref` 同一条选窗规则：
    # 最近活跃的同渠道私聊窗口。这里复制查询而不是 import 那个私有函数——
    # 它是 proactive 域的投递细节，worker 不该依赖它的内部形状。
    stmt = (
        select(Window)
        .where(
            Window.subject_id == subject_id,
            Window.channel == channel,
            Window.visibility == "subject_private",
            Window.archived.is_(False),
        )
        .order_by(Window.last_message_at.desc().nulls_last())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()  # type: ignore[attr-defined]
    return str(row.title or "") if row is not None else ""


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
            # 会话定位**从承诺行自己取**，不依赖调度方传参。
            #
            # 原来这里只读 `payload["proactive_window_ref"]`，而调度方
            # （`enqueue_maintenance`）只投 `{"bucket": hour_bucket}`——
            # 于是每条到期承诺都拿不到 ref、被标 `unsupported`，行状态随之
            # 推进到 `undeliverable`；而 `due_commitments()` 只取 `active`，
            # 承诺到期即**永久失效**（创建时好好的，一到点就再也不出现）。
            #
            # 现在以承诺行记的 `window_id` 为准（视图已暴露该字段），
            # 找不到可用会话时才回落到"这个人最近的同渠道私聊窗口"。
            # payload 里的显式值仍优先：它是运维手工补投的逃生口，
            # 也保证既有手工投递用法不改变语义。
            window_ref = str(payload.get("proactive_window_ref") or "")
            if not window_ref:
                window_ref = await _commitment_window_ref(
                    session, commitment=commitment, connector=connector
                )
            if not window_ref:
                await goals.mark_delivery(
                    commitment.commitment_id,
                    operation_id=None,
                    delivery_state="unsupported",
                )
                logger.info(
                    "承诺 %s 缺少可投递的会话定位（window_id 与同渠道私聊窗口都不可用），"
                    "标记 unsupported",
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


@handler("session.purge")
async def handle_session_purge(payload: dict) -> None:
    """删除已过期的登录会话（`auth_sessions`）。

    此前**全仓没有任何清理语句**（`delete(AuthSession)` 零命中）：这张表
    只增不减，每行还带一个 token 哈希。单用户实例一天登几次、跑上几个月，
    表就会长成几千行，而"删掉过期会话"这件事从没发生过。

    清理不改变任何鉴权语义：`resolve_token` 本来就拒过期行，这里只是把
    已经无效的行真的删掉。**不动未到期的已吊销行**——`revoked_at` 是审计
    事实（谁在何时被改密踢下线），它们的 `expires_at` 到了自然一起清掉。

    幂等、可多副本并发：`DELETE ... WHERE expires_at <= now` 后跑的那次删 0 行。
    """
    from ling.domains.identity.service import IdentityService
    from ling.kernel.turn import purge_expired_prompt_snapshots

    async with session_scope() as session:
        removed = await IdentityService(session).purge_expired_sessions()
        # 提示词快照的保留期清理（7 天清正文、再 7 天删墓碑）跟同一个
        # 小时级维护入口走：都是"删过期行"，不必各起一个定时器。
        await purge_expired_prompt_snapshots(session)
    if removed:
        logger.info("已清理过期登录会话 %d 条", removed)


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
    # 视频后缀必须写在这里：本地落盘的 mp4 一旦被标成 image/jpeg，
    # 原生视频模式会发出 `data:image/jpeg;base64,<整段 mp4>`，供应商那边
    # 要么报错要么"看到坏字节"——那是**看起来能看、其实看到的是垃圾**的
    # 静默错误，比直接失败糟得多。
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}

# 后缀不认识时**不猜**：给一个中性类型，让上层（如视频描述）自己按
# `application/octet-stream` 处理/拒绝，而不是伪装成某一种已知格式。
_UNKNOWN_MEDIA_MIME = "application/octet-stream"


def _media_root_default() -> Path:
    """媒体目录：与 Web 上传落盘的位置一致（`settings.data_dir / "media"`）。"""
    return get_settings().data_dir / "media"


def make_media_fetcher(
    connector: object | None,
    *,
    media_root: Path | None = None,
    max_bytes: int | None = None,
):
    """构造取媒体内容的函数。

    `file:` 开头的是 Web 上传、落在本地磁盘的文件——它**不需要任何渠道**。
    原来这里直接用 `connector.fetch_media`，于是没有 OneBot 时整段 return，
    Web 上传的图片永远得不到描述。

    抽成模块级函数是为了能测：闭包在 handler 里取不到。

    ## `file:` 分支必须做两道约束

    原实现是 `Path(media_ref[5:]).read_bytes()`——**无前缀约束、无大小上限**。
    而 `media_ref` 来自数据库行（Web 上传时由我们自己写入，但库不是信任边界：
    任何能改库、或将来新增的写入路径都能把它变成任意路径）。于是：

    - `file:///etc/passwd` 能读任意文件；
    - `file:/dev/zero` 能直接把进程读死（OOM）。

    现在要求：解析后（**先 `resolve()` 展开 `..` 与符号链接**）必须落在
    媒体目录内，且大小不超过上限。判定用 `is_relative_to()`，
    **不是字符串前缀**——`/data/media-evil` 以 `/data/media` 开头，
    字符串前缀会误放行。
    """

    root_for_fetch = media_root
    limit_for_fetch = max_bytes

    async def _fetch(media_ref: str) -> tuple[bytes, str]:
        if media_ref.startswith("file:"):
            root = (root_for_fetch or _media_root_default()).resolve()
            limit = (
                limit_for_fetch
                if limit_for_fetch is not None
                else get_settings().max_body_bytes
            )
            raw = media_ref[len("file:") :]
            # `file:///etc/passwd` 是 URI 形式（带 authority），不是我们写入的
            # `<绝对路径>` 形式。直接拒绝，别去猜它的语义。
            if raw.startswith("//"):
                raise MediaRefError(f"不支持 file:// URI 形式的媒体引用: {media_ref[:80]}")
            path = Path(raw)
            if not path.is_absolute():
                # 相对路径会按进程 cwd 解析——那不是一个可预期的位置。
                raise MediaRefError(f"file: 引用必须是绝对路径: {media_ref[:80]}")
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise MediaRefError(
                    f"file: 引用越界（不在媒体目录 {root} 内）: {resolved}"
                )
            if not resolved.is_file():
                raise MediaRefError(f"file: 引用不是普通文件: {resolved}")
            size = resolved.stat().st_size
            if size > limit:
                raise MediaRefError(f"媒体 {size} 字节超过上限 {limit} 字节: {resolved}")
            data = resolved.read_bytes()
            # 大小在 stat 与 read 之间可能变化（TOCTOU），读完再核一次。
            if len(data) > limit:
                raise MediaRefError(
                    f"媒体 {len(data)} 字节超过上限 {limit} 字节: {resolved}"
                )
            return data, _MEDIA_SUFFIX_MIME.get(
                resolved.suffix.lower(), _UNKNOWN_MEDIA_MIME
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

    # **显式把媒体目录与上限传进去**：`file:` 分支的越界判定以它为根。
    # 不传的话会回落到 `get_settings().data_dir`——那在测试/多实例下
    # 与真实落盘位置（`ling/api/routes/chat.py` 用的同一份 settings）不一致，
    # 表现为"上传的图明明在，却报越界"。
    _fetch_media = make_media_fetcher(
        connector,
        media_root=settings.data_dir / "media",
        max_bytes=settings.max_body_bytes,
    )

    limit = int(payload.get("limit") or 5)
    described = failed = permanent = blocked = 0
    aborted = False
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
        # **视频不在这里读**：视频只在她主动写 ``<<看视频 mX>>`` 时才读
        # （见 domains/media/video.py 与 domains/media/watch.py）。自动队列
        # 里留着它，等于每 60 秒把群里每一段视频都送进视频模型——那是白烧
        # token，而她当时可能根本用不上。
        for window_id in await service.windows_with_pending(
            limit=10, exclude_kinds=("video",)
        ):
            outcome = await describer.describe_pending(window_id=window_id, limit=limit)
            described += outcome.described
            failed += outcome.failed
            permanent += outcome.permanent_failures
            blocked += outcome.blocked
            if outcome.last_error:
                logger.info("媒体描述部分失败 window=%s: %s", window_id, outcome.last_error)
            if outcome.aborted:
                # 模型/配置侧错误（401、密钥缺失、职责未绑定）：**换窗口也一样失败**，
                # 继续遍历只是把同样的调用再打一遍。整轮停在这里，证据都还在
                # 队列里，配置修好后的下一轮会自然重来。
                aborted = True
                break

    if described or failed:
        logger.info("媒体描述：成功 %d 条，失败 %d 条", described, failed)
    if aborted:
        logger.warning(
            "媒体描述整轮中止：视觉模型不可用或配置有误（见上一条 WARNING）。"
            "证据**没有**被标成终态，仍留在队列里；修好配置后会重试。"
        )
    if permanent:
        # 永久失败（引用越界/超限/形式不支持）**单独记一行**：它们不会自己好，
        # 每轮都会被 `pending` 重新捞出来。混在"失败 N 条"里的话，
        # "一直在重试临时故障"和"这条引用永远不可能成功"看起来一样。
        logger.warning(
            "媒体描述：%d 条引用本身不可用（越界/超限/形式不支持），"
            "重试不会成功；需要人工清理这些证据行",
            permanent,
        )
    if blocked:
        # 业务拒绝（撤权、过期）也落了终态，但**不是**引用不可用。
        # 不单独说一句的话，运维会把它当成"引用坏了"去查文件。
        logger.info(
            "媒体描述：%d 条因授权/过期被跳过并标记终态（重新授权后需要人工恢复）",
            blocked,
        )


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
        # 没有外部渠道时**不放弃**：Web 面板也算一个投递面。
        # 否则在 qqbot 拿不到主动权限、OneBot 没启用的实例上，"主动联系"
        # 永远只是代码里的空转——她永远不会先开口。
        # 落库同样受门控（勿扰时段/日限额）与 `proactive_delivery_enabled` 约束。
        emit_event(
            logger,
            logging.INFO,
            event_name="proactive_dispatch_skipped",
            message="没有外部主动推送渠道，改投 Web 会话",
            attributes={"reason_code": "no_proactive_channel"},
        )

    async with session_scope() as session:
        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        # 时区**必须走同一份解析**（DB 偏好 > env > 默认）。多用户部署里
        # 每个人的"今天"不同：逐个 subject 的偏好放进 mapping，没设过的人
        # 回退到实例子级默认，绝不借用别人的时区。
        from ling.settings.user_preferences import (
            load_user_timezone,
            owner_subject_id,
            timezone_by_subject,
        )

        instance_timezone, _tz_source = await load_user_timezone(
            session,
            subject_id=await owner_subject_id(session),
            env=settings.user_timezone,
        )
        stats = await dispatch_pending(
            session,
            actor_id=settings.actor_id,
            connector=connector,
            deliver_to_web=True,
            config_reader=config,
            subject_timezone=instance_timezone,
            subject_timezones=await timezone_by_subject(session),
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

    # **必须和检索侧用同一个来源**：向量行按 `(memory_id, model_id)` 存，
    # 补算若用了环境变量那套、检索却用网页绑定的模型，补出来的向量会被
    # 检索当成"另一个模型的历史数据"直接忽略——补算白跑，而且谁都看不出来。
    async with session_scope() as session:
        config = await _published_config(session)
    embedder = build_embedding(config, secret_reader=_SECRET_CACHE.get)
    if embedder is None:
        logger.info("向量补算跳过：未配置 embedding 提供方（网页绑定或 LING_EMBEDDING_*）")
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


def _group_reply_connector(channel: str) -> object | None:
    """按**窗口所属渠道**取连接器。

    **不能**拿 `proactive_connector()` 顶替：那只挑"声明支持主动推送"的
    渠道，可能和这个群所在的平台/账号不是同一个。群回复必须发回它来自的
    那个渠道，否则消息会从另一个号冒出来，或者根本发不出去。
    """
    if channel == "onebot":
        from ling.capabilities.channels import build_onebot_connector

        return build_onebot_connector()
    if channel == "qqbot":
        from ling.capabilities.channels import build_qqbot_connector

        return build_qqbot_connector()
    return None


async def _reply_in_group_window(  # noqa: ANN001
    *,
    session,
    config,
    window,
    result,
) -> tuple[bool, str]:
    """把「决定回了」变成「真的发出去了」：生成 → 投递。

    返回 ``(是否送达, 未送达原因)``。**任何一步没成都不返回 True**——
    调用方据此决定要不要记 `replied`，以及是否把记录改成 `reply_failed`。

    为什么生成/投递要放在这里而不是 attention 域：`poll_window` 的职责是
    "刷到没有、要不要接"，它跑在自己那个事务里、也受租约保护。生成要调模型
    （几十秒）、投递要拿平台回执，把这两件事塞进那条事务会让游标行一直
    `FOR UPDATE` 锁着，还会让"决策"和"结果"共用一个返回值。

    群聊恒为一段（`plan_parts(is_group=True)`）：拆成多条会绕过防刷屏上限
    （`group_turn_budget` 数的是消息条数）。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.infra.security import new_id
    from ling.kernel.turn import CancelRegistry, TurnCoordinator

    settings = get_settings()
    router = _MEMORY_ROUTER
    if router is None:
        return False, "模型路由未就绪"

    # 生成用的 subject 是**最近开口的那个人**：群窗口的 subject 是主人，
    # 拿它当"对谁"是错误归属（与决策侧取 speaker 保持一致）。
    subject_id = result.speaker_subject_id or window.subject_id
    conversation = ConversationService(
        session, actor_id=settings.actor_id, persona_id=settings.persona_id
    )
    effective = await config.effective()
    turn = await conversation.create_turn(
        window.id,
        client_turn_id=new_id("attn"),
        config_revision=effective.revision,
    )
    # 轮次任务跑在**自己的会话**里（`TurnCoordinator._execute` 用 session_scope），
    # 必须先提交，否则它看不到刚建的 turn 行——与 `InboundBridge` 同一条约束。
    await session.commit()

    coordinator = TurnCoordinator(
        # `_MEMORY_ROUTER` 只声明成 object（预热失败时为 None，上面已挡住）
        router=router,  # type: ignore[arg-type]
        actor_id=settings.actor_id,
        persona_id=settings.persona_id,
        cancels=CancelRegistry(),
        # 群聊自动回复同样走记忆检索：没有这个读取器就只能用环境变量那套。
        secret_reader=_SECRET_CACHE.get,
    )
    queue = await coordinator.run_turn(
        window_id=window.id,
        subject_id=subject_id,
        turn_id=turn.id,
        # 这一批消息在 poll 之前就已经落库，会作为窗口历史进上下文；
        # 这里把批次文本作为"她正在回什么"的输入。
        user_text=result.batch_text,
        config=effective,
    )

    reply_text = ""
    state = "unknown"
    expression: dict | None = None
    failure = ""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=180.0)
        except TimeoutError:
            return False, "生成超时"
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
                # 与入站桥同一段说明（`describe_turn_failure`）：``ModelError``
                # 那条路不填 ``detail``，只读 detail 会让日志只剩
                # 「失败：turn.failed |」——用户无从知道该改密钥、模型名还是额度。
                from ling.kernel.turn import describe_turn_failure

                failure = describe_turn_failure(data)
                logger.warning(
                    "群聊回应轮次 %s 失败（%s）：%s", turn.id, state, failure
                )
    reply_text = reply_text.strip()
    if not reply_text:
        # 失败原因要回到调用方：它会被写成"她没开口"的理由，
        # 只留 ``state=`` 等于把可执行的线索丢掉。
        why = f"state={state}" + (f"，{failure}" if failure else "")
        return False, f"生成没有产出文本（{why}）"

    channel = str(window.channel or "")
    if channel in {"", "web"}:
        # Web 面板本身就是投递面：轮次已经把她的话写进 messages，
        # 用户下次打开就能看到。没有外部渠道可发，也不算失败。
        return True, ""

    try:
        connector = _group_reply_connector(channel)
    except Exception as exc:  # noqa: BLE001 - 构造失败按"没发出去"处理
        return False, f"渠道 {channel} 连接器构造失败：{type(exc).__name__}"
    if connector is None:
        return False, f"渠道 {channel} 没有可用的连接器"

    window_ref = str(window.title or "")
    if not window_ref:
        return False, "窗口缺少平台标识（window_ref），无法投递"

    from ling.capabilities.chunking import PART_DELAY_SECONDS, plan_parts
    from ling.capabilities.delivery import DeliveryService
    from ling.ports.channel import OutboundMessage

    caps = getattr(connector, "capabilities", None)
    limit = None
    if callable(caps):
        with contextlib.suppress(Exception):
            limit = int(caps().max_text_length) or None
    is_group = str(window.visibility or "") == "group_scope"
    parts = plan_parts(reply_text, is_group=True, max_part_chars=limit)
    if not parts:
        return False, "回复文本为空"

    service = DeliveryService(session)
    plan_id = f"plan_{new_id('attn')}"
    sent = 0
    for index, part in enumerate(parts, 1):
        if index > 1:
            # 上一段已经拿到回执才走到这里。停顿只是节奏，不是回执。
            await asyncio.sleep(PART_DELAY_SECONDS)
        try:
            outcome = await service.deliver(
                connector=connector,  # type: ignore[arg-type]
                message=OutboundMessage(
                    operation_id=new_id("op"),
                    window_ref=window_ref,
                    text=part,
                    is_group=is_group,
                    # 她自己在生成时选的表达方式（@/引用/拍一拍）
                    mentions=list((expression or {}).get("at") or []),
                    reply_to=(expression or {}).get("quote"),
                    poke_targets=list((expression or {}).get("poke") or []),
                ),
                actor_id=settings.actor_id,
                subject_id=subject_id,
                payload_extra={
                    "reply_plan": plan_id,
                    "part_ordinal": index,
                    "parts_total": len(parts),
                    "source": "attention.tick",
                },
            )
        except Exception as exc:  # noqa: BLE001
            # 逃出 `deliver` 的异常可能发生在"平台已经收下"之后（连接器断流、
            # 回执写库失败）。和 uncertain 一样按**没有确认送达**处理：
            # 记失败、不重发——重复发一条比漏发更难收拾。
            return False, f"投递异常：{type(exc).__name__}"
        # `delivered` 是唯一判据，不在这里另写一种拼法。
        if not outcome.delivered:
            return False, f"投递失败：{outcome.detail or str(outcome.state)}"
        sent += 1
    return (sent == len(parts)), ""


async def _group_reply_delivery_enabled(config) -> bool:  # noqa: ANN001
    """群聊回复是否真的发出去：读**已发布配置**，默认关。

    读不到配置时返回 False（不发）——与 `proactive.dispatch` 一致：
    把"不确定"当成"不发"，而不是当成"发"。在群里说错话比不说更难收拾。
    """
    try:
        effective = await config.effective()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 读不到配置就不发
        logger.warning("群聊回复跳过：配置不可用（%s）", type(exc).__name__)
        return False
    return bool(effective.dialogue.group_reply_delivery_enabled)


@handler("delivery.reconcile")
async def handle_delivery_reconcile(payload: dict) -> None:  # noqa: ARG001
    """把"结果未知"和"该发未发"两种投递推进到确定的结局。

    `DeliveryService.reconcile()` 早就写好，但**一直没有生产调用方**
    （只有测试调它），所以线上 `uncertain` 等于永久悬空——既不会变成已送达，
    也不会有人再查一次。这条周期任务就是它的调用方
    （`docs/OUTBOUND-DELIVERY-RESEARCH.md` §5 P0-2）。

    两件事：
    1. `state='uncertain'` 的投递 → 问平台（`get_msg` / 回显 / 历史内容比对）；
    2. `kind='delivery.pending'`（生成超时留下的）→ 正文落库之后续发。

    **绝不因为对账失败就重发**：查不到 ≠ 没发出去。
    """
    from ling.domains.goals.delivery_reconcile import reconcile_deliveries

    def _connector_for(channel: str):  # noqa: ANN202
        try:
            return _group_reply_connector(channel)
        except Exception as exc:  # noqa: BLE001 - 构造失败只影响这一条
            logger.warning("对账连接器构造失败（%s）：%s", channel, type(exc).__name__)
            return None

    report = await reconcile_deliveries(connector_for=_connector_for)
    if not report.touched:
        return
    emit_event(
        logger,
        logging.INFO,
        event_name="delivery_reconciled",
        message=(
            f"投递对账：{report.scanned} 条不确定 / {report.pending_scanned} 条待续发"
        ),
        attributes={
            "scanned": report.scanned,
            "delivered": report.delivered,
            "still_uncertain": report.still_uncertain,
            "failed": report.failed,
            "pending_scanned": report.pending_scanned,
            "resumed": report.resumed,
        },
    )


@handler("attention.tick")
async def handle_attention_tick(payload: dict) -> None:
    """群聊轮询：刷到才看，**一次轮询最多一次决策调用**。

    「决定回了」之后是否真的生成并发送，由**已发布配置**项
    ``DialoguePolicy.group_reply_delivery_enabled`` 决定，默认 False：
    只做决策和记录（`decision=replied` 表示"她决定回"，不等于"已经发出去
    了"），不生成、不发送；`tests/integration/test_attention_polling.py`
    仍然按这个模式钉住决策行为。打开后，决定要回的窗口会真的走
    "生成 → 投递"，投递失败如实记成 `reply_failed`。

    这个开关**不再从 payload 读**。之前周期任务写死过 `{"shadow": True}`
    （只决策不发送，"决定回了"永远停在日志里），后来又写死成
    `{"shadow": False}`——两次都是硬编码，用户没有开关可选。现在唯一的
    来源是已发布配置；`payload` 里即使带了 `shadow` 也不生效。

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
    polls = calls = baseline = replied = reply_failed = 0
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
        due_windows = await scheduler.due_windows()
        # 是否真的发出去看**已发布配置**（默认关），不看 payload。
        # 只在真有窗口到期时读一次：tick 每 5 秒一次，空轮询不该多一次查询。
        reply_delivery_enabled = False
        if due_windows:
            reply_delivery_enabled = await _group_reply_delivery_enabled(config)
        for window_id in due_windows:
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
            failure = ""
            if result.decision == "replied":
                if not reply_delivery_enabled:
                    # 默认路径：决策即止（不花生成调用）。`replied` 在这里
                    # 表示"她决定回"，与"真的发出去了"靠 reply_delivery_enabled
                    # / reply_failure 区分。
                    replied += 1
                else:
                    delivered, failure = False, ""
                    try:
                        window = await session.get(Window, window_id)
                        if window is None:
                            delivered, failure = False, "窗口不存在"
                        else:
                            delivered, failure = await _reply_in_group_window(
                                session=session,
                                config=config,
                                window=window,
                                result=result,
                            )
                    except Exception as exc:  # noqa: BLE001 - 一个窗口失败不能拖垮整轮
                        logger.exception("群聊回应生成/投递异常 window=%s", window_id)
                        delivered, failure = False, f"生成/投递异常：{type(exc).__name__}"
                    if delivered:
                        replied += 1
                    else:
                        reply_failed += 1
                        # **如实反映"决定回但没发出去"**：消息的 handled_as
                        # 和轮询日志的 decision 都要改，不能留着 replied。
                        with contextlib.suppress(Exception):
                            result = await scheduler.mark_reply_failed(
                                result, reason=failure or "未送达"
                            )
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
                    "reply_failure": failure,
                    "reply_delivery_enabled": reply_delivery_enabled,
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
            "reply_failed": reply_failed,
            "reply_delivery_enabled": reply_delivery_enabled,
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
    """收集主动候选。**只登记，不发送。**

    这条 handler 从来没有"影子模式"开关：收集本身就只有一种行为——把候选
    写进库里。原来那个 `shadow` 只被原样抄进返回值、不参与任何逻辑，
    用户改它毫无作用，还容易让人以为"关掉影子就会真发"。

    真正发送由 `proactive.dispatch` 负责，开关是配置项
    `proactive_delivery_enabled`（默认关）。是否要发、过不过门控，
    在 `ProactivityService.gate` 与 `DeliveryService` 里，这里不重复实现。
    """
    from ling.domains.proactive.service import ProactivityService

    actor_id = get_settings().actor_id
    async with session_scope() as session:
        result = await ProactivityService(
            session, actor_id=actor_id
        ).collect_candidates()
        await ProactivityService(session, actor_id=actor_id).expire_stale()
    if result["created"] or result["skipped_duplicate"]:
        logger.info(
            "主动候选：新增 %d 条、去重跳过 %d 条",
            result["created"],
            result["skipped_duplicate"],
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



# 旧路径的周期（600 秒）**已不再使用**：`life.plan` 移出 `PERIODIC_JOBS`，
# 只剩手工触发。留着这个数字是为了让"它原本多久跑一次"在代码里可查，
# 而不是让人以为它还在按时跑。
LIFE_PLAN_SECONDS = 600.0
# 今天是否已经尝试排过日程（进程内）。多副本会各自尝试一次，
# 但 `max_live_plans` 是硬上限，不会排爆。
#
# 注意：**它不是权威**。进程内状态一重启就归零，所以"旧路径该不该跑"
# 由 `_new_timeline_owns()` 查 DB 判断；这里只防同一进程内重复排。
_LAST_PLAN_DATE: str = ""


def parse_plan_json(text: str) -> list[dict]:
    """从模型输出里抠出日程数组。**宽容但不猜**。

    模型经常加解释、用 ```json 包裹、或者只写一个对象。这里只接受
    带 title 的条目，其它一律丢弃——宁可她今天少做一件，也不要写进
    一条看不出是什么的日程。
    """
    import json
    import re

    if not text:
        return []
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        start_at = str(item.get("start") or "").strip()
        if not title or not re.fullmatch(r"\d{1,2}:\d{2}", start_at):
            continue
        try:
            minutes = int(item.get("minutes") or 60)
        except (TypeError, ValueError):
            minutes = 60
        minutes = max(15, min(180, minutes))
        out.append({"title": title[:120], "start": start_at, "minutes": minutes})
    return out[:3]


async def _new_timeline_owns(session, *, actor_id: str) -> bool:  # noqa: ANN001
    """新时间轴是否已经接管这个 actor（= 旧 `life.plan` 是否必须停手）。

    判据取**数据**而不是开关：`day_plans` 有行，说明新时间轴真的为这个 actor
    产出过计划（21:00 的 due、凌晨的 catch-up，或醒点后的 late 兜底都会建行）。
    在这种情况下再让旧路径自动排 1–3 件小事，就是同一段时间轴上两份安排，
    谁读哪一份取决于读者，没法解释也没法撤。

    不能只看"代码里注册了 life.schedule"：那是**能力**，不是**事实**。
    也不能看进程内变量：worker 重启后它归零，旧路径会偷偷复活。
    """
    from ling.infra.models import DayPlanRow

    row = (
        await session.execute(
            select(DayPlanRow.id).where(DayPlanRow.actor_id == actor_id).limit(1)
        )
    ).first()
    return row is not None


@handler("life.plan")
async def handle_life_plan(payload: dict) -> None:
    """**旧**路径：让她自己排今天的 1–3 件小事（已退出自动调度）。

    为什么曾经需要它：`life.tick` 只在"有到点的计划"时才开活动，而计划
    从来没有来源——于是活动表永远是空的，界面上"她此刻在做什么"永远
    "没有登记中的活动"。她因此没有生活，只有一个能聊天的空壳。

    设计取舍：
    - 一天只排一次（醒来之后、且当天还没有在手的计划）
    - 只安排"与人格相符的日常小事"，吃饭睡觉这类不需要记录
    - 模型给不出可用结果就**不排**，绝不硬编"她在看书"

    ## 为什么它不再是周期任务

    新时间轴（`life.schedule` + `schedule.outline`）已经接管"今天/明天做什么"。
    两套一起跑就是**两份日程**：`agenda_plans` 一套、`day_plans` 一套，界面
    与 `life.tick` 都会各看一半，谁也不知道哪份算数。所以它从
    `PERIODIC_JOBS` 移出，列进 :data:`LEGACY_MANUAL_TOPICS`——只留手工触发。

    另外还有一道**数据闸**：一旦这个 actor 已经有 `day_plans` 行（新时间轴
    确实产出过计划），即使有人手工/旧配置把它调起来，也不会再自动生成。
    旧 `agenda_plans` 仍然可读，手工小事仍走 `AgendaService.propose_plan`，
    不经过这里。确需重建旧行为时传 ``{"force": true}``——**默认关闭**。
    """
    global _LAST_PLAN_DATE

    from ling.domains.agenda.service import AgendaService
    from ling.domains.persona.service import PersonaService
    from ling.infra.security import SecretBox
    from ling.ports.model import ChatMessageIn
    from ling.settings.service import ConfigService

    settings = get_settings()

    # 数据闸放在模型路由检查**之前**：新时间轴接管后，这里该记的是"旧路径
    # 已停用"，而不是一条会把人带偏的"模型路由未就绪"。
    if not payload.get("force"):
        async with session_scope() as session:
            if await _new_timeline_owns(session, actor_id=settings.actor_id):
                emit_event(
                    logger,
                    logging.INFO,
                    event_name="life_plan_retired",
                    message="旧日程生成已停用：新时间轴已接管",
                    attributes={
                        "reason_code": "new_timeline_active",
                        "hint": "手工小事走 AgendaService.propose_plan；确需旧路径传 force=true",
                    },
                )
                return

    router = _MEMORY_ROUTER
    if router is None:
        emit_event(
            logger,
            logging.WARNING,
            event_name="life_plan_skipped",
            message="日程未生成：模型路由未就绪",
            attributes={"reason_code": "model_router_unavailable"},
        )
        return

    actor_id = settings.actor_id
    async with session_scope() as session:
        agenda = AgendaService(session, actor_id=actor_id)
        routine = await agenda.routine()
        now = utcnow()
        from ling.domains.agenda.service import _tz

        local = now.astimezone(_tz(routine.timezone))
        today = local.date().isoformat()

        if today == _LAST_PLAN_DATE:
            return
        if not routine.is_awake(now):
            return
        # 离休息时间不足 40 分钟就不排了：排了也没时间做完，
        # 只会让"她正在做一件马上要被打断的事"。
        minutes_now = local.hour * 60 + local.minute
        if 0 <= routine.rest_minute - minutes_now < 40:
            _LAST_PLAN_DATE = today
            emit_event(
                logger,
                logging.DEBUG,
                event_name="life_plans_proposed",
                message="今日日程未生成：临近休息时间",
                attributes={"created": 0, "reason_code": "too_close_to_rest"},
            )
            return
        if await agenda.upcoming():
            # 已经有在手的计划（可能是昨天排的、还没做完），不重复排
            _LAST_PLAN_DATE = today
            return

        persona = await PersonaService(session, settings.persona_id).get()
        config = ConfigService(
            session,
            SecretBox(settings),
            test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        effective = await config.effective()
        # 排日程**没有自己的职责**（`planning` 至今没有实现，见
        # RESERVED_PROFILES），只能借一个。借的顺序写在这里，判据用同一份
        # `resolve_profile`：优先 `memory.extract`（"想清楚再写"那条路），
        # 它不可用再借主对话。
        #
        # 这是**应用层给自己找模型的降级**，不是职责回退：它不会出现在设置页
        # 的「实际生效」里——那一列只报职责自己的解析结果，否则又变成
        # "界面一个说法、运行另一个说法"。
        profile = (
            "memory.extract"
            if effective.resolve_profile("memory.extract").usable
            else "dialogue.fast"
        )
        # **必须告诉它现在几点**。不说的话模型会顺着"今天"排上午的时间，
        # 而此刻已经是下午/晚上——那些时段全被"过去不排"的规则丢掉，
        # 结果是"parsed=3 但 created=0"（实测在部署实例上就是这样）。
        now_local = local.strftime("%H:%M")
        prompt = (
            f"你是{persona.display_name}。你的人设片段：\n"
            f"{(persona.system_prompt or '')[:1200]}\n\n"
            f"今天是 {today}，现在已经是 {now_local}。\n"
            "请为**今天剩下的时间**安排 1-3 件你会做的日常小事。\n"
            "要求：\n"
            f"- 所有时间必须**晚于 {now_local}**，且早于夜里休息\n"
            "- 只安排符合你性格与身份的小事（看书、散步、收拾、做手工之类）\n"
            "- 时间用 24 小时制 HH:MM，每件 30-120 分钟\n"
            "- 不要安排吃饭睡觉这种不需要记录的\n"
            "只输出 JSON 数组，不要解释：\n"
            '[{"title":"看两章小说","start":"10:30","minutes":60}]'
        )
        chunks: list[str] = []
        try:
            async for chunk, _outcome in router.stream(  # type: ignore[attr-defined]
                effective,
                profile=profile,
                messages=[ChatMessageIn(role="user", content=prompt)],
                allow_fallback=False,
            ):
                if chunk.delta:
                    chunks.append(chunk.delta)
        except Exception as exc:  # noqa: BLE001 - 排不出日程不该影响别的任务
            kind = getattr(getattr(exc, "kind", None), "value", "") or type(exc).__name__
            emit_event(
                logger,
                logging.WARNING,
                event_name="life_plan_skipped",
                message="日程未生成：模型调用失败",
                attributes={"reason_code": "model_error", "error_kind": kind},
            )
            return

        plans = parse_plan_json("".join(chunks))
        created = 0
        for item in plans:
            hour, minute = (int(x) for x in item["start"].split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            start_at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            end_at = start_at + timedelta(minutes=item["minutes"])
            if end_at <= now.astimezone(_tz(routine.timezone)):
                continue  # 已经过去的时间段不排
            try:
                view = await agenda.propose_plan(
                    kind="simulated",
                    title=item["title"],
                    start_at=start_at.astimezone(UTC),
                    end_at=end_at.astimezone(UTC),
                    commitment_level="tentative",
                )
            except ValueError:
                continue
            if view is not None:
                created += 1

        _LAST_PLAN_DATE = today
        emit_event(
            logger,
            logging.INFO if created else logging.WARNING,
            event_name="life_plans_proposed",
            message="今日日程已安排" if created else "今日日程未生成",
            attributes={
                "created": created,
                "parsed": len(plans),
                "date": today,
                "reason_code": "" if created else "model_returned_no_usable_plan",
            },
        )

# ---------------------------------------------------------------- 新时间轴的段边界（阶段 2）
#
# 设计 §3：`life.tick` 只在到期边界或事件有动作。**新时间轴（`day_plans`）**的
# 边界由段决定，不再用"旧 agenda_plans 的计划时段已结束"当时钟证据：
# 计划结束只允许 `paused` / `cancelled` + `needs_review` 原因码，
# **绝不**自动 `completed`（完成必须有 observed / user_confirmed 证据）。
#
# 旧 clock-only 路径只对旧 `agenda_plans` 的活动生效（它自己的证据闸
# `ling.domains.evidence` 也仍然兜底）；新时间轴的段整条不走那条路。

# 段类别 → 场景状态。没列到的一律 focus（"手头有事在做"）。
# `rest` 只给计划里的休息段；`free` 是"没定"，所以场景回 unknown 而不是编一个。
SCENE_STATE_BY_KIND: dict[str, str] = {
    "rest": "rest",
    "social": "chat",
    "errand": "errand",
    "walk": "errand",
    "commute": "errand",
    "exercise": "errand",
    "free": "unknown",
}


def _scene_state_for_kind(kind: str) -> str:
    return SCENE_STATE_BY_KIND.get(str(kind), "focus")


async def _advance_timeline_boundaries(
    session, *, actor_id: str, now: datetime  # noqa: ANN001
) -> list[str]:
    """把新时间轴的段边界推进一格（幂等；返回人读的短标签，仅供日志）。

    顺序固定，任何一步都**不写完成**：

    1. 活动来源段已不再覆盖 ``now``（结束 / 被改版 / 被取消）→
       ``running → paused``、``proposed → cancelled``，原因码 ``needs_review``；
    2. 覆盖 ``now`` 的睡眠锚点 → 暂停仍在跑的模拟活动、场景 ``rest``；
       **不创建睡眠活动**（睡眠锚点只是预计休息，不是"她睡着了"）；
    3. 覆盖 ``now`` 的叶段 → 按 ``schedule_segment_id`` **唯一去重**起活动，
       场景跟随它；TTL 只在"真实边界事件"或"活动仍 running"时续（设计 §3）；
    4. 没有覆盖段 → 场景回 ``unknown``（不硬编"我在看书"）。
    """
    from ling.domains.activity.service import ActivityService
    from ling.domains.agenda.timeline import boundary_view
    from ling.domains.presence.service import (
        SCENE_REFRESH_REASON_SEGMENT,
        SCENE_REFRESH_REASON_SLEEP,
        PresenceService,
    )

    activity = ActivityService(session, actor_id=actor_id)
    presence = PresenceService(session, actor_id=actor_id)
    view = await boundary_view(session, actor_id=actor_id, now=now)
    actions: list[str] = []

    current = await activity.current()
    # ① 来源段已经结束（含 superseded/cancelled 的段）：软收尾，绝不 completed。
    # 只对"还在跑/还没开始"的活动动手：已经 paused 的不重复写（幂等）。
    if (
        current is not None
        and current.schedule_segment_id
        and current.state in {"running", "proposed"}
    ):
        if not view.covers_segment(current.schedule_segment_id):
            ended = await activity.end_schedule_window(current.schedule_segment_id)
            actions.append(f"end:{current.schedule_segment_id}:{ended.action}")
            current = await activity.current()
    elif (
        current is not None
        and current.schedule_segment_id is None
        and current.plan_id
        # 切到新时间轴之前就在跑的旧活动：窗口结束也只暂停（不回退成完成路径）。
        and current.state == "running"
        and await _plan_window_ended(session, current.plan_id, now=now)
    ):
        await activity.transition(current.activity_id, "paused")
        actions.append(f"end_legacy:{current.activity_id}")
        current = await activity.current()

    # ② 睡眠锚点覆盖此刻：暂停 + 场景 rest。睡眠不产生活动。
    if view.sleep is not None:
        if (
            current is not None
            and current.state == "running"
            and current.origin == "simulated"
        ):
            await activity.transition(current.activity_id, "paused")
            actions.append(f"sleep_pause:{current.activity_id}")
            current = await activity.current()
        scene = await presence.scene(now=now)
        if scene.state != "rest":
            await presence.commit_scene(state="rest", origin="simulated", now=now)
            actions.append("scene:rest")
        elif await presence.refresh_scene_ttl(
            reason=SCENE_REFRESH_REASON_SLEEP, now=now
        ):
            actions.append("scene:rest_ttl")
        return actions

    # ③ 覆盖此刻的叶段：按段唯一去重起活动（重启/重复 tick 不会各开一个）。
    if view.active_leaf is not None:
        segment = view.active_leaf
        # 活动可见范围**只许收窄**：段哪怕是 public，"她此刻在做什么"也只是
        # subject_private；段更严时跟着段走（未知取值按最严处理）。
        from ling.domains.agenda.intents import strictest_visibility

        started = await activity.start_for_segment(
            segment_id=segment.segment_id,
            kind=segment.kind,
            place_id=segment.place_id,
            visibility=strictest_visibility(segment.visibility, "subject_private"),
            origin="simulated",
            source_event_ids=[
                f"schedule_segment:{segment.segment_id}",
                f"plan:{segment.plan_id}",
            ],
        )
        if started.action in {"created", "resumed"}:
            actions.append(f"{started.action}:{segment.segment_id}")
        if started.activity.state != "running":
            # 这一段的活动已经收尾（取消/已暂停到终态）：不再拿它当"此刻在做什么"。
            return actions
        target_scene = _scene_state_for_kind(segment.kind)
        scene = await presence.scene(now=now)
        if (
            scene.state != target_scene
            or scene.activity_id != started.activity.activity_id
        ):
            await presence.commit_scene(
                state=target_scene,
                origin="simulated",
                activity_id=started.activity.activity_id,
                place_id=segment.place_id,
                source_event_ids=[f"schedule_segment:{segment.segment_id}"],
                now=now,
            )
            actions.append(f"scene:{target_scene}")
        elif started.activity.state == "running":
            # 真实边界事件（段仍覆盖此刻）+ 活动仍在 running：允许续 TTL。
            if await presence.refresh_scene_ttl(
                reason=SCENE_REFRESH_REASON_SEGMENT,
                activity_running=True,
                now=now,
            ):
                actions.append("scene:ttl")
        return actions

    # ④ 此刻没有任何覆盖段：场景回 unknown，别拿旧场景自述。
    # 只有"她还声称在做事、但其实没有活动在跑"时才清：对话场景（chat）由
    # 对话路径维护，不在这里踩掉。
    if current is None or current.state != "running":
        scene = await presence.scene(now=now)
        if scene.state not in {"unknown", "chat"}:
            await presence.clear_scene(now=now)
            actions.append("scene:unknown")
    return actions


@handler("life.tick")
async def handle_life_tick(payload: dict) -> None:
    """生活推进：只在**时段边界或到点计划**时动手。

    为什么不能每分钟跑：那会让角色不断生成"完美日记"，
    既烧模型又不真实。真实的人是按事件推进的——起床、被打断、
    手头这件事做完了。

    两条路径**互斥**（按数据判定，不看开关）：

    - **新时间轴已接管**（`day_plans` 有行）→ 交给
      :func:`_advance_timeline_boundaries`：按段边界开/停活动，计划时段结束
      只暂停/取消 + `needs_review`，**绝不 completed**；
    - 否则走旧 `agenda_plans` 路径（下面这段逻辑保持不变）：醒着且有到点的
      计划 → 开模拟活动；该歇了 → 暂停；旧计划时段结束 → 旧的 clock-only
      收尾（它的证据种类仍是 `clock_only`，事实读侧（`ling.domains.evidence`）
      照旧排除它，所以不会因为这条旧路径产生新的"经历"）。
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

        if await _new_timeline_owns(session, actor_id=actor_id):
            actions = await _advance_timeline_boundaries(
                session, actor_id=actor_id, now=now
            )
            if actions:
                logger.info("时间轴边界推进: %s", ", ".join(actions))
            return

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
        # **只对旧 `agenda_plans` 的活动生效**：带 `schedule_segment_id` 的新时间轴
        # 活动走上面那条分支，不会到这里（多一层防御，不靠调用顺序）。
        if (
            current is not None
            and current.state == "running"
            and current.origin == "simulated"
            and current.plan_id
            and current.schedule_segment_id is None
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
    from ling.settings.service import published_memory_policy

    episode_days = int(payload.get("episode_days") or 180)
    async with session_scope() as session:
        # 归档判定要跟着已发布策略走：`max_retention_days` 是硬上限，
        # 行内 retention_days 只能更短（裁决见 docs/SETTINGS-TAXONOMY.md ④）。
        service = MemoryService(session, policy=await published_memory_policy(session))
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

# 过期登录会话清理。小时级足够：过期会话多留一小时没有任何风险，
# 而清理本身没有业务价值，不该占更密的节奏。
SESSION_PURGE_SECONDS = 3600.0

# 主动草稿：给候选生成开场白（只生成不发送）。
# 跟收集器同节奏即可——刚收集到的候选下一轮就有草稿。
PROACTIVE_DRAFT_SECONDS = 300.0

# 主动投递：看配置项 proactive_delivery_enabled（默认关）。
PROACTIVE_DISPATCH_SECONDS = 300.0

# 出站投递对账：把 `operations.state='uncertain'` 与
# `kind='delivery.pending'`（生成超时留下的"该发未发"）推向收敛。
#
# 工程初值 60 秒：uncertain 的投递最晚一分钟内会被查一次；令牌桶式的
# 每轮上限与条间间隔在 `domains/goals/delivery_reconcile.py` 里
# （`RECONCILE_BATCH` / `RECONCILE_PAUSE_SECONDS`），这里只管节奏。
# **独立 topic，不动 `life.schedule` 的主体**：它们互不依赖。
DELIVERY_RECONCILE_SECONDS = 60.0


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


# ---------------------------------------------------------------- 生活调度（life.schedule）
#
# 设计 §3：扫描**只查 DB 到期边界**，一次模型调用都不做。真正干活的在
# `schedule.outline` / `schedule.seam` / `dream.generate` / `sleep.settle`
# 四个 outbox handler 里——它们各自在自己的连接、自己的事务边界里跑模型。
#
# 为什么必须这样切：
# - 扫描 60 秒一次，拿它调模型会把"每 60 秒"变成"每 60 秒一次的 60 秒调用"；
# - 到期判断必须能在没有模型、没有出网的进程里回答"该不该跑"；
# - 投递走 outbox，重启后由 DB 状态补投（进程内计数器 `_LAST_PLAN_DATE`
#   那种做法一重启就归零，旧路径会偷偷复活）。

# 扫描周期：设计 §3 的工程初值（健康运行时在 21:00–21:01 投递）。
LIFE_SCHEDULE_SECONDS = 60.0

# 醒前 catch-up 的退避（秒）：只有**可恢复**原因才补试，且同一目标日最多
# 投递 `OUTLINE_SCHEDULE_MAX_ATTEMPTS` 次（大纲预算本身最多 2 次调用）。
OUTLINE_SCHEDULE_RETRY_BACKOFF_SECONDS = 900.0
OUTLINE_SCHEDULE_MAX_ATTEMPTS = 3

# 醒后多久之内还值得补记一条 `missed`（零模型）：只补最近这一晚，
# 不为几天前没生成的梦翻旧账。
DREAM_MISSED_MARK_HOURS = 6


def _config_reader(session):  # noqa: ANN001, ANN202
    """构造"已发布配置"读取器（worker 侧唯一来源）。

    单独抽出来是为了让四个 handler 用同一份口径（test_mode/persona 一致），
    也让集成测试能只替换这一处就拿到可用的模型职责解析结果。
    """
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    settings = get_settings()
    return ConfigService(
        session,
        SecretBox(settings),
        test_mode_default=settings.test_mode,
        persona_id=settings.persona_id,
    )


def _aware_moment(value: datetime | None) -> datetime | None:
    """DB 读回的时间可能是 naive（sqlite）；统一按 UTC 解释，别拿它直接相减。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _wake_minute_or_none(routine) -> int | None:  # noqa: ANN001
    """`outline_due_decision` 要的 wake_minute；没有有效睡眠窗时是 ``None``。

    ``wake == rest`` 在作息里的语义是"没有有效睡眠窗"，不是"睡了 24 小时"。
    把它当 08:00 传下去，会让"午夜之后就算晚到"变成"等到 08:00 才算晚到"。
    """
    wake = int(routine.wake_minute) % (24 * 60)
    rest = int(routine.rest_minute) % (24 * 60)
    return None if wake == rest else wake


async def _enqueue_once(
    session,  # noqa: ANN001
    *,
    event_id: str,
    topic: str,
    payload: dict,
    now: datetime,
) -> bool:
    """按主键幂等投递一条 outbox 事件；已经存在（任何状态）就不投。

    幂等键就是**事件 id 本身**：`outbox_events.id` 是主键，重复扫描时
    先查后插，再加上嵌套事务兜住"两个 worker 同时插"的竞态。
    """
    from sqlalchemy.exc import IntegrityError

    if await session.get(OutboxEvent, event_id) is not None:
        return False
    try:
        async with session.begin_nested():
            session.add(
                OutboxEvent(
                    id=event_id,
                    topic=topic,
                    payload=payload,
                    available_at=now,
                    created_at=now,
                )
            )
            await session.flush()
    except IntegrityError:
        # 另一个 worker 同时投了同一条：主键兜底，这不是错误。
        return False
    return True


async def _stale_outline_call_days(
    session, *, actor_id: str, now: datetime  # noqa: ANN001
) -> list[date]:
    """有"过期模型租约"的目标日：那一版大纲预留了预算但没提交。

    预算不退款（设计 §3），但**目标日本身需要补跑**——不补的话这一天永远
    停在 `pending`，直到醒点后由 late 相位兜底。这里只读，不改那行：
    过期 attempt 的最终状态归调用它的服务，worker 不替别人写状态。
    """
    from ling.domains.agenda.timeline import OUTLINE_KIND
    from ling.infra.models import BehaviorModelCallRow

    rows = (
        await session.execute(
            select(BehaviorModelCallRow.plan_day)
            .where(
                BehaviorModelCallRow.actor_id == actor_id,
                BehaviorModelCallRow.kind == OUTLINE_KIND,
                BehaviorModelCallRow.state == "reserved",
                BehaviorModelCallRow.lease_until.is_not(None),
                BehaviorModelCallRow.lease_until < now,
            )
            .distinct()
        )
    ).scalars()
    return [item for item in rows if item is not None]


def _like_prefix(value: str) -> str:
    """把字面量变成 LIKE 前缀模式（转义 ``%`` / ``_`` / ``\\``）。

    actor id 里带下划线（``actor_ling``）时，不转义的 LIKE 会把 ``_`` 当成
    "任意一个字符"，于是 ``actorXling`` 的事件也会被算进同一个作业。
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


async def _schedule_outline(
    session,  # noqa: ANN001
    *,
    actor_id: str,
    plan_day: date,
    phase: str,
    now: datetime,
    timezone: str,
    timezone_source: str,
    routine_revision: int,
) -> bool:
    """按"目标日 + 相位"投递一次大纲作业（同一天重复扫描不重复投）。

    幂等键 = 目标日 + 快照版本（tz_rev/routine_rev）+ 尝试号：
    - 快照没变时 key 稳定 → 重复扫描只看到已有事件，不会再投；
    - 快照（时区/作息）变了 → 新 key，需要重排（`ensure_day_plan` 自己会
      判断"快照没变就返回 unchanged"，所以不会白烧模型）；
    - 可恢复失败（模型暂不可用/超时/报错）→ 醒前最多再补两次，带 15 分钟
      退避；终态原因码（非法输出/预算耗尽/degraded_late）不再重试。
    """
    from ling.domains.agenda.timeline import (
        FINAL_REASON_CODES,
        PHASE_LATE,
        RETRYABLE_REASON_CODES,
        load_day_plan,
    )

    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    tz_rev = int(row.timezone_revision) if row is not None else 0
    routine_rev = int(row.routine_revision) if row is not None else routine_revision
    base = f"outline:{actor_id}:{plan_day.isoformat()}:t{tz_rev}:r{routine_rev}"
    prior = list(
        (
            await session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.id.like(_like_prefix(base), escape="\\"))
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        ).scalars()
    )
    if any(str(event.state) in ("pending", "in_flight") for event in prior):
        return False  # 已有在途作业：等它跑完再说

    snapshot_same = (
        row is not None
        and str(row.timezone) == timezone
        and int(row.routine_revision) == routine_revision
    )
    if row is None:
        if phase == PHASE_LATE:
            # 这一天**从来没跑过作业**（例如刚部署、或者已经过去很久）。
            # late 相位不会调模型，但凭空造一份"从中午开始的半日时间轴"
            # 不是补跑，是把"系统当时不在"伪装成"她今天就这样过"。
            # 真正的晚到是"夜里跑过但没成"——那种行一定存在，见下面分支。
            return False
        needs = True
    elif str(row.state) == "ready":
        needs = not snapshot_same
    elif str(row.state) == "degraded":
        reason = str(row.last_error_code or "")
        if reason in FINAL_REASON_CODES:
            needs = False
        elif not snapshot_same:
            needs = True
        else:
            needs = reason in RETRYABLE_REASON_CODES
    else:  # pending：占了行但一版都没提交（崩溃/中断）
        needs = True
    if not needs:
        return False

    attempt = len(prior) + 1
    if attempt > OUTLINE_SCHEDULE_MAX_ATTEMPTS:
        return False
    if prior:
        last = prior[-1]
        last_at = _aware_moment(last.processed_at or last.created_at)
        if (
            str(last.state) == "done"
            and phase != PHASE_LATE
            and last_at is not None
            and now - last_at
            < timedelta(seconds=OUTLINE_SCHEDULE_RETRY_BACKOFF_SECONDS)
        ):
            return False  # 退避窗口内不补试
    return await _enqueue_once(
        session,
        event_id=f"{base}:a{attempt}",
        topic="schedule.outline",
        payload={
            "actor_id": actor_id,
            "plan_day": plan_day.isoformat(),
            "phase": phase,
            "timezone": timezone,
            "timezone_source": timezone_source,
            "attempt": attempt,
            "scanned_at": now.isoformat(),
        },
        now=now,
    )


async def _schedule_seam(
    session, *, actor_id: str, plan_day: date, now: datetime  # noqa: ANN001
) -> bool:
    """投递一次午夜接缝校验（零模型，一天一次，幂等键 = 目标日）。"""
    from ling.domains.agenda.timeline import load_day_plan

    row = await load_day_plan(session, actor_id=actor_id, plan_day=plan_day)
    if row is None:
        # 还没有这一天的计划：没有"现有段"可复核。等 late/catch-up 建了行，
        # 下一轮扫描（60 秒后）自然会补上这一遍。
        return False
    window_start = _aware_moment(row.window_start_at)
    if window_start is not None and now < window_start:
        return False  # 窗口还没开始，接缝无从谈起
    return await _enqueue_once(
        session,
        event_id=f"seam:{actor_id}:{plan_day.isoformat()}",
        topic="schedule.seam",
        payload={
            "actor_id": actor_id,
            "plan_day": plan_day.isoformat(),
            "scanned_at": now.isoformat(),
        },
        now=now,
    )


async def _schedule_refine(
    session, *, actor_id: str, now: datetime, timezone: str | None = None
) -> str | None:
    """投递一次近窗细化（幂等键 = ``refine:{plan_id}:{revision}:{window_start}``）。

    扫描本身**零模型**：只回答"现在有没有该细化的父段"。选择规则完全在
    `timeline.load_refine_target` 里（最早未细化段 + 60 分钟触发 + 未来 3 小时
    窗口），所以重复扫描要么没有目标，要么落在同一个幂等键上——不会被
    每 60 秒的扫描反复投递、也不会每 5 分钟换一个父段重烧调用。

    预算/滚动额度/失败不重试这些闸门在 `refine_work` 里（那里有事务和账本）；
    这里只负责"到期投一次"。
    """
    from ling.domains.agenda.timeline import load_refine_target

    target = await load_refine_target(
        session, actor_id=actor_id, now=now, timezone=timezone
    )
    if target is None:
        return None
    key = target.idempotency_key
    enqueued = await _enqueue_once(
        session,
        event_id=key,
        topic="schedule.refine",
        payload={
            "actor_id": actor_id,
            "plan_day": target.plan_day.isoformat(),
            "parent_id": target.parent.segment_id,
            "window_start": target.window_start.isoformat(),
            "idempotency_key": key,
            "scanned_at": now.isoformat(),
        },
        now=now,
    )
    if not enqueued:
        return None
    return f"schedule.refine:{target.parent.segment_id}"


async def _schedule_dream(
    session, *, actor_id: str, window, now: datetime  # noqa: ANN001
) -> bool:
    """梦窗口内投递一次生成（一晚一条，幂等键 = sleep_date）。

    只投"还在窗口里"的：`generate_from`（醒前 60 分钟）到 `late_after`
    （醒后 30 分钟）。窗口**结束后**还会补一条 `missed` 的投递，限
    `DREAM_MISSED_MARK_HOURS` 之内——那是为了把"昨晚没生成"记成事实
    （`status=missed`），不是重试模型；`DreamService.generate` 在这个分支
    里 0 次调用就直接落行。
    """
    from ling.infra.models import DreamReportRow

    existing = (
        await session.execute(
            select(DreamReportRow.id)
            .where(
                DreamReportRow.actor_id == actor_id,
                DreamReportRow.sleep_date == window.sleep_date,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return False  # 这一晚已有记录（含 failed/missed/no_dream）——一晚一份
    in_window = window.generate_from <= now <= window.late_after
    missed_mark = (
        window.late_after
        < now
        <= window.late_after + timedelta(hours=DREAM_MISSED_MARK_HOURS)
    )
    if not (in_window or missed_mark):
        return False
    return await _enqueue_once(
        session,
        event_id=f"dream:{actor_id}:{window.sleep_date.isoformat()}",
        topic="dream.generate",
        payload={
            "actor_id": actor_id,
            "sleep_date": window.sleep_date.isoformat(),
            "bed_at": window.bed_at.isoformat(),
            "wake_at": window.wake_at.isoformat(),
            "generate_from": window.generate_from.isoformat(),
            "due_at": window.due_at.isoformat(),
            "late_after": window.late_after.isoformat(),
            "scanned_at": now.isoformat(),
            # 只让"为什么投了这一条"可解释；handler 会按自己的 now 重算窗口。
            "phase": "window" if in_window else "missed_mark",
        },
        now=now,
    )


async def _schedule_settlement(
    session, *, actor_id: str, window, now: datetime  # noqa: ANN001
) -> bool:
    """醒后补做跨夜结算（与梦无关；唯一键保证只结算一次）。"""
    from ling.infra.models import SleepSettlementRow

    if now < window.wake_at:
        return False  # 还没醒：结算没有意义，也不该提前打折
    existing = (
        await session.execute(
            select(SleepSettlementRow.sleep_date)
            .where(
                SleepSettlementRow.actor_id == actor_id,
                SleepSettlementRow.sleep_date == window.sleep_date,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return False
    return await _enqueue_once(
        session,
        event_id=f"sleep_settle:{actor_id}:{window.sleep_date.isoformat()}",
        topic="sleep.settle",
        payload={
            "actor_id": actor_id,
            "sleep_date": window.sleep_date.isoformat(),
            "wake_at": window.wake_at.isoformat(),
            "scanned_at": now.isoformat(),
        },
        now=now,
    )


@handler("life.schedule")
async def handle_life_schedule(payload: dict) -> None:
    """生活调度扫描：**只查 DB 到期边界，一次模型调用都没有**。

    每 60 秒跑一次，做五件事，全部是"到期才投递"：

    1. **过期 lease**：把租约过期的 in-flight outbox 事件放回 pending
       ——worker 被杀/重启后没确认的事件靠这一步补投；
    2. **大纲**：用 `outline_due_decision` 定"最近一个该有/该补的目标日 D 与
       相位"（21:00 后是明天/due；凌晨是今天/catchup；醒后是今天/late），
       加上"过期模型租约"指向的目标日，按幂等键投 `schedule.outline`；
    3. **午夜接缝**：本地今天已有计划行 → 投一次 `schedule.seam`（零模型）；
    4. **梦 / 跨夜结算**：对本地 `今天−1 / 今天 / 今天+1` 三晚调
       `night_window`，在梦窗口内且这一晚无 `dream_reports` 行 → 投
       `dream.generate`；`now >= wake_at` 且无 `sleep_settlements` 行 →
       投 `sleep.settle`（结算自身幂等）；
    5. **近窗细化**：距"最早未细化段"开始 ≤ 60 分钟、且未来 3 小时里确有粗段
       → 按 `refine:{plan_id}:{revision}:{window_start}` 投一次
       `schedule.refine`（零模型；规则见 `timeline.load_refine_target`）。

    **`late` 相位不凭空造行**：目标日从来没有跑过作业（库里没有 `day_plans`
    行）时，late 只说明"系统当时不在"，不能拿它伪造一份"从中午开始的一天"。
    真正需要 late 兜底的是"夜里跑过、但停在 pending/degraded"的那些行。

    幂等：事件 id 就是幂等键，先查后插 + 主键唯一约束；重复扫描只会看到
    已经存在的事件，不会重复投递。
    """
    from ling.domains.agenda.service import AgendaService
    from ling.domains.agenda.timeline import (
        outline_due_decision,
        plan_day_for,
        resolve_plan_timezone,
    )
    from ling.domains.dream.service import night_window
    from ling.infra.models import ActorRoutineRow

    actor_id = str(payload.get("actor_id") or get_settings().actor_id)
    now = utcnow()
    enqueued: list[str] = []
    reclaimed = 0
    async with session_scope() as session:
        timezone, timezone_source = await resolve_plan_timezone(session)
        # 作息走 agenda 的公开读取入口；**不**用它表里的 timezone 算日界，
        # 那个字段只用于冲突检测（设计 §2）。
        routine = await AgendaService(session, actor_id=actor_id).routine()
        routine_row = await session.get(ActorRoutineRow, actor_id)
        routine_revision = int(getattr(routine_row, "revision", 0) or 0)
        wake_minute = _wake_minute_or_none(routine)
        local_day = plan_day_for(now, timezone)

        # ① 过期租约：崩溃/重启后没确认的事件重新排队。
        reclaimed = await _reclaim_expired_outbox_leases(session, now=now)

        # ② 大纲：最近一个该有/该补的目标日（+ 过期模型租约指向的目标日）。
        candidates: dict[date, str] = {}
        target, phase = outline_due_decision(
            now, timezone=timezone, wake_minute=wake_minute
        )
        candidates[target] = phase
        for stale_day in await _stale_outline_call_days(
            session, actor_id=actor_id, now=now
        ):
            # 超过明天的旧目标日不追补（设计 §3「catch-up」）；昨天以前的
            # 窗口已经结束，补出来的只是给过去编时间轴。
            if stale_day not in (local_day, local_day + timedelta(days=1)):
                continue
            _day, stale_phase = outline_due_decision(
                now,
                timezone=timezone,
                wake_minute=wake_minute,
                plan_day=stale_day,
            )
            candidates.setdefault(stale_day, stale_phase)
        for plan_day, candidate_phase in candidates.items():
            if await _schedule_outline(
                session,
                actor_id=actor_id,
                plan_day=plan_day,
                phase=candidate_phase,
                now=now,
                timezone=timezone,
                timezone_source=timezone_source,
                routine_revision=routine_revision,
            ):
                enqueued.append(f"schedule.outline:{plan_day.isoformat()}:{candidate_phase}")

        # ③ 午夜接缝（当前本地日）。
        if await _schedule_seam(
            session, actor_id=actor_id, plan_day=local_day, now=now
        ):
            enqueued.append(f"schedule.seam:{local_day.isoformat()}")

        # ④ 梦与跨夜结算：三晚。`sleep_date` 是"醒来所在的本地日"，跨午夜
        # 作息下 bed 在前一天，但仍是同一晚（night_window 负责换算）。
        for offset in (-1, 0, 1):
            sleep_date = local_day + timedelta(days=offset)
            window = await night_window(
                session, actor_id=actor_id, sleep_date=sleep_date, timezone=timezone
            )
            if window is None:
                continue  # 没有有效睡眠窗：不排梦、不结算
            if await _schedule_dream(
                session, actor_id=actor_id, window=window, now=now
            ):
                enqueued.append(f"dream.generate:{sleep_date.isoformat()}")
            if await _schedule_settlement(
                session, actor_id=actor_id, window=window, now=now
            ):
                enqueued.append(f"sleep.settle:{sleep_date.isoformat()}")

        # ⑤ 近窗细化（阶段 2）：距最早未细化段开始 ≤ 60 分钟、且未来 3 小时里
        # 确有粗段才投一次；幂等键含版本与窗口起点，重复扫描不会重复投。
        refine_job = await _schedule_refine(
            session, actor_id=actor_id, now=now, timezone=timezone
        )
        if refine_job:
            enqueued.append(refine_job)

    if reclaimed:
        logger.info("回收过期 outbox 租约 %d 条", reclaimed)
    if enqueued:
        logger.info("生活调度投递 %d 条: %s", len(enqueued), ", ".join(enqueued))


def _parse_payload_date(payload: dict, key: str) -> date | None:
    """从载荷里取 ISO 日期；缺失/非法一律返回 None（调用方记日志并返回）。"""
    raw = str(payload.get(key) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@handler("schedule.outline")
async def handle_schedule_outline(payload: dict) -> None:
    """跑一次大纲作业：**模型调用在这里，而不在 outbox 的认领事务里**。

    相位只是"上限"：`outline_work` 会按**真实 now** 再判一次，取更保守的
    那个。所以这条事件即使被延迟执行（重启补投、退避重试），也不会拿一个
    过期的 `due` 去调模型——到了醒点就自动降成零模型的 `late`。
    """
    from ling.audit import behavior_event
    from ling.domains.agenda.timeline import PHASE_CATCHUP, PHASE_DUE, PHASE_LATE, outline_work

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    plan_day = _parse_payload_date(payload, "plan_day")
    if plan_day is None:
        logger.warning("schedule.outline 缺少可解析的 plan_day，跳过: %s", payload)
        return
    phase = str(payload.get("phase") or PHASE_DUE)
    if phase not in (PHASE_DUE, PHASE_CATCHUP, PHASE_LATE):
        logger.warning("schedule.outline 收到未知相位 %r，按 due 处理", phase)
        phase = PHASE_DUE
    now = utcnow()
    async with session_scope() as session:
        outcome = await outline_work(
            session,
            plan_day,
            phase,
            actor_id=actor_id,
            # 路由没就绪也要走：`ensure_day_plan` 会写规则锚点 + degraded，
            # 不是"什么都不做"（否则这一天永远没有睡眠/承诺锚点）。
            router=_MEMORY_ROUTER,
            config_reader=_config_reader(session),
            now=now,
            lease_owner=f"{WORKER_ID}/{plan_day.isoformat()}",
        )
    logger.info(
        "大纲作业完成 day=%s phase=%s state=%s revision=%s segments=%s calls=%s reason=%s",
        plan_day,
        outcome.phase or phase,
        outcome.state,
        outcome.revision,
        outcome.segments,
        outcome.calls,
        outcome.reason_code or "",
    )
    behavior_event(
        "schedule_outline_committed" if outcome.state == "ready" else "schedule_outline_degraded",
        (
            f"系统为 {plan_day.isoformat()} 生成 {outcome.segments} 个意向块"
            f"（{'模型计划' if outcome.source == 'model' else '规则锚点'}）"
        ),
        outcome="ok" if outcome.state == "ready" else "degraded",
        attributes={
            "actor_id": actor_id,
            "target_plan_day": plan_day.isoformat(),
            "phase": outcome.phase or phase,
            "state": outcome.state,
            "revision": outcome.revision,
            "segments": outcome.segments,
            "source": outcome.source,
            "calls": outcome.calls,
            "reason_code": outcome.reason_code or "",
            "timezone": outcome.timezone,
            "timezone_source": outcome.timezone_source,
        },
    )


@handler("schedule.seam")
async def handle_schedule_seam(payload: dict) -> None:
    """午夜接缝校验：**零模型**，只做确定性修补，不重新生成。

    `seam_check` 结构上没有 router 参数（`calls` 恒为 0）。重复执行是安全的：
    没有变化就不写任何东西、不增 revision、不写重复原因码。
    """
    from ling.audit import behavior_event
    from ling.domains.agenda.timeline import seam_check

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    plan_day = _parse_payload_date(payload, "plan_day")
    if plan_day is None:
        logger.warning("schedule.seam 缺少可解析的 plan_day，跳过: %s", payload)
        return
    async with session_scope() as session:
        report = await seam_check(session, plan_day, actor_id=actor_id, now=utcnow())
    logger.info(
        "接缝校验 day=%s state=%s revision=%s→%s superseded=%d frozen=%d",
        plan_day,
        report.state,
        report.revision_before,
        report.revision_after,
        len(report.superseded_segment_ids),
        len(report.frozen_segment_ids),
    )
    behavior_event(
        "schedule_seam_checked",
        (
            f"午夜接缝校验（{plan_day.isoformat()}）：{report.state}，"
            f"未调用模型"
        ),
        outcome="ok" if report.state in ("unchanged", "no_plan") else "changed",
        attributes={
            "actor_id": actor_id,
            "target_plan_day": plan_day.isoformat(),
            "state": report.state,
            "revision_before": report.revision_before,
            "revision_after": report.revision_after,
            "superseded": len(report.superseded_segment_ids),
            "frozen": len(report.frozen_segment_ids),
            "reason_codes": list(report.reason_codes),
            "calls": report.calls,
        },
    )


@handler("schedule.refine")
async def handle_schedule_refine(payload: dict) -> None:
    """跑一次近窗细化：**模型调用在这里**，不在 outbox 的认领事务里。

    `refine_work` 自己按"没有合格目标 → 这个窗口已经试过 → 滚动额度用完 →
    模型不可用 → 预算耗尽 → 模型超时/非法 → CAS 失败"的顺序判分支，
    绝大多数分支都是 0 次调用。失败一律**保留粗段**，不重试、不回填过去。
    """
    from ling.audit import behavior_event
    from ling.domains.agenda.timeline import refine_work

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    plan_day = _parse_payload_date(payload, "plan_day")
    expect_key = str(payload.get("idempotency_key") or "") or None
    now = utcnow()
    async with session_scope() as session:
        outcome = await refine_work(
            session,
            actor_id=actor_id,
            now=now,
            plan_day=plan_day,
            router=_MEMORY_ROUTER,
            config_reader=_config_reader(session),
            lease_owner=f"{WORKER_ID}/refine",
            expect_key=expect_key,
        )
    logger.info(
        "细化作业 day=%s parent=%s state=%s revision=%s→%s children=%s calls=%s reason=%s",
        outcome.plan_day or plan_day,
        outcome.parent_id or "",
        outcome.state,
        outcome.revision_before,
        outcome.revision_after,
        outcome.children,
        outcome.calls,
        outcome.reason_code or "",
    )
    behavior_event(
        "schedule_refined" if outcome.state == "refined" else "schedule_refine_skipped",
        (
            f"把 {outcome.plan_day.isoformat() if outcome.plan_day else '今天'} 的"
            f"{outcome.parent_id or '一段安排'}细化成 {outcome.children} 个更小的子段"
            if outcome.state == "refined"
            else "近窗细化没有改动（保留粗块，不调用或未采用模型输出）"
        ),
        outcome="ok" if outcome.state in ("refined", "unchanged", "no_target") else "skip",
        attributes={
            "actor_id": actor_id,
            "target_plan_day": (outcome.plan_day or plan_day).isoformat()
            if (outcome.plan_day or plan_day)
            else "",
            "state": outcome.state,
            "parent_id": outcome.parent_id or "",
            "window_start": outcome.window_start.isoformat()
            if outcome.window_start
            else "",
            "window_end": outcome.window_end.isoformat() if outcome.window_end else "",
            "revision_before": outcome.revision_before,
            "revision_after": outcome.revision_after,
            "children": outcome.children,
            "calls": outcome.calls,
            "reason_code": outcome.reason_code or "",
        },
    )


# ---------------------------------------------------------------- 重排回话
#
# 缺口：`schedule.replan` 拒绝/推迟一次临时提议时，原来只往
# `schedule_change_requests` 与行为日志里记一笔，**用户收不到任何回话**——
# 提议像掉进了黑洞（设计文档把这条列为"最大的产品级缺口"）。下面补的就是
# "她回一句"。
#
# 三条硬规则：
# 1. **先说在本地**：话先落成会话里她的一条消息（Web 端立刻能看到），再谈
#    发到哪个平台；连落库都失败就什么都不声称。
# 2. **没有回执不声称已发送**：只有 `DeliveryOutcome.delivered` 才记 `sent`；
#    结果未知记 `uncertain` 并**不重发**；明确没发出去记 `failed`。
# 3. **回话失败不影响主线**：走到这里时计划已经改完、或已经决定不改了。这一整
#    段自己吞掉异常，绝不往外抛——一句发不出去的"今天先不折腾"不该让整条
#    `schedule.replan` 事件重试到 dead。
REPLAN_NOTICE_SCOPE = "replan_notice"
REPLAN_NOTICE_OPERATION_PREFIX = "replan_notice:"


def _record_replan_notice(
    *,
    actor_id: str,
    turn_id: str,
    reason_code: str,
    delivery: str,
    stage: str,
    window_id: str = "",
    message_id: str = "",
    operation_id: str = "",
    text: str = "",
) -> None:
    """记一条 ``replan_notice`` 行为事件：她有没有回话、回话到了哪。

    ``delivery`` 是**送达结论**，四种取值与投递状态机同名：

    - ``sent``：拿到送达回执；
    - ``uncertain``：可能到了，等对账，**不重发**；
    - ``failed``：明确没发出去；
    - ``web_only``：只落库——没有可主动推送的渠道（例如只有 Web）。

    ``stage`` 说明停在哪一步：``persist``（话都没落下来）/ ``deliver``
    （落库了，但没送到平台）/ ``done``（该做的都做完了）。分开写是因为
    "她根本没说话"和"她说了但没送到平台"对用户是两件事，事后不该长得一样。
    """
    from ling.audit import behavior_event

    behavior_event(
        "replan_notice",
        # 说明是人话：她说了什么、有没有送出去。
        f"提议没有改动安排，她{'说了一句「' + text + '」' if text else '没回成话'}"
        f"（{delivery}）",
        outcome="ok" if delivery in {"sent", "web_only"} else "skip",
        attributes={
            "actor_id": actor_id,
            "turn_id": turn_id,
            "reason_code": reason_code,
            "delivery": delivery,
            "stage": stage,
            "window_id": window_id,
            "message_id": message_id,
            "operation_id": operation_id,
        },
    )


async def _replan_notice_target(
    session: object, *, window_id: str, connector: object
) -> tuple[str, str, bool] | None:
    """回话该发到哪：`Window.id` → ``(window_ref, subject_id, is_group)``。

    两跳是必须的：``payload["window_id"]`` 是本地会话主键，连接器要的是平台
    窗口标识（``Window.title``，如 ``onebot-x:private:9001``）。把 id 当 ref
    传下去，就是"把 ``win_xxx`` 当成 QQ 号发出去"。

    只发**这条提议所在的那个窗口**（提议从群里来就回群里）。另有两条硬约束：

    - 窗口必须属于同一渠道（``Window.channel == connector_id``）：别的渠道
      解析不出这个 ref；
    - ``title`` 必须真是平台标识（``split_window_ref`` 认得）：Web 窗口的
      title 是用户随手起的名字，长得像 ref 也不能拿来当收件人。

    任何一条不满足都返回 ``None``：调用方**只落库**，不声称发送。
    """
    from ling.domains.conversation.service import split_window_ref
    from ling.infra.models import Window

    channel = str(getattr(connector, "connector_id", "") or "")
    row = (
        (
            await session.execute(  # type: ignore[attr-defined]
                select(Window).where(
                    Window.id == window_id, Window.archived.is_(False)
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None or not row.title:
        return None
    if channel and str(row.channel or "") != channel:
        return None
    parsed = split_window_ref(str(row.title))
    if parsed is None:
        return None
    _instance, kind, _target = parsed
    return str(row.title), str(row.subject_id or ""), kind == "group"


async def _send_replan_notice(
    *,
    actor_id: str,
    persona_id: str,
    turn_id: str,
    window_id: str | None,
    text: str,
    reason_code: str,
) -> None:
    """把她那句"这次没改"送出去：先落库，再（有渠道时）真实投递。

    **本函数从不抛异常**：调用点在计划已经改完（或已经决定不改）之后，
    回话是附加品，不是主线。

    ## 幂等：同一 ``turn_id`` 最多一条回话，三层保证

    1. **决定层**（主闸）：`notice_for` 对 ``duplicate`` 返回 ``None``——同一轮
       被重复消费时连文案都不会产生（和"同一轮只改一次计划"是同一把锁）；
    2. **回话层**：落库前先 ``claim_event(scope="replan_notice", key=turn_id)``
       认领这一轮。重复/并发消费直接短路：即使决定记录被清理掉再跑一次，
       也不会多出第二条消息；
    3. **投递层**：``deliver(plan_id="replan_notice:<turn_id>", part_ordinal=0)``
       走段级幂等——已送达的段**跳过**，``uncertain`` **不重发**（等对账）。
       ``OutboundMessage.operation_id`` 只是这条回话的逻辑身份：``deliver``
       会把它换成它新建的本地 ``operations.id``，平台看不到它。

    ## 顺序

    落库（一个事务）→ 投递（另一个事务）。**必须先落库并且先提交**：投递
    抛异常时，Web 端那句话不能跟着回滚——"她说了但没送到平台"是可解释的，
    "她什么都没说"不是。
    """
    from ling.capabilities.delivery import DeliveryService
    from ling.contracts.v1 import OperationState, Origin
    from ling.domains.conversation.service import ConversationService
    from ling.ports.channel import ChannelError, ChannelErrorKind, OutboundMessage

    if not window_id:
        # 定位不到会话就只有事件可记：不能猜一个窗口把这句塞进去。
        logger.warning("重排回话拿不到 window_id，只记事件 turn=%s", turn_id)
        _record_replan_notice(
            actor_id=actor_id,
            turn_id=turn_id,
            reason_code=reason_code,
            delivery="failed",
            stage="persist",
            text=text,
        )
        return

    # ① 落库：Web 端立刻能看到。同时认领这一轮，重复消费在这里短路。
    message_id = ""
    try:
        async with session_scope() as session:
            conversation = ConversationService(
                session, actor_id=actor_id, persona_id=persona_id
            )
            claimed = await conversation.claim_event(
                scope=REPLAN_NOTICE_SCOPE, key=turn_id
            )
            if claimed is not None:
                logger.info("重排回话这一轮已经回过，跳过 turn=%s", turn_id)
                return
            view = await conversation.add_message(
                window_id,
                role="actor",
                text=text,
                # 用最贴近的**既有**取值：这是她说的一句话（不是用户陈述、
                # 不是外部观测），与 proactive 主动消息落库时同一种来源，
                # 见 `domains/proactive/dispatch.py::_append_web_message`。
                # `Origin` 里没有"系统固定文案"这一档，不为这一句新增枚举值。
                origin=Origin.simulated,
            )
            message_id = view.message_id
    except Exception:  # noqa: BLE001 - 回话失败绝不影响主线
        logger.warning("重排回话落库失败 turn=%s", turn_id, exc_info=True)
        _record_replan_notice(
            actor_id=actor_id,
            turn_id=turn_id,
            reason_code=reason_code,
            delivery="failed",
            stage="persist",
            window_id=window_id,
            text=text,
        )
        return

    # ② 有可主动推送的渠道才谈"发出去"；没有就只落库（Web 端已经能看到）。
    try:
        from ling.capabilities.channels import proactive_connector

        connector = proactive_connector()
    except Exception:  # noqa: BLE001 - 渠道探测失败按"没有渠道"处理
        logger.warning("重排回话：渠道探测失败 turn=%s", turn_id, exc_info=True)
        connector = None

    if connector is None:
        logger.info("重排回话只落库（没有可主动推送的渠道）turn=%s", turn_id)
        _record_replan_notice(
            actor_id=actor_id,
            turn_id=turn_id,
            reason_code=reason_code,
            delivery="web_only",
            stage="done",
            window_id=window_id,
            message_id=message_id,
            text=text,
        )
        return

    operation_id = ""
    delivery = "failed"
    try:
        async with session_scope() as session:
            target = await _replan_notice_target(
                session, window_id=window_id, connector=connector
            )
            if target is None:
                # 有连接器但**定位不到平台窗口**（例如 Web 会话）：同样只落库。
                logger.info(
                    "重排回话没有可投递的会话定位（window_id 不是平台窗口）turn=%s",
                    turn_id,
                )
                _record_replan_notice(
                    actor_id=actor_id,
                    turn_id=turn_id,
                    reason_code=reason_code,
                    delivery="web_only",
                    stage="done",
                    window_id=window_id,
                    message_id=message_id,
                    text=text,
                )
                return
            window_ref, subject_id, is_group = target
            outcome = await DeliveryService(session).deliver(
                connector=connector,
                message=OutboundMessage(
                    operation_id=f"{REPLAN_NOTICE_OPERATION_PREFIX}{turn_id}",
                    window_ref=window_ref,
                    text=text,
                    is_group=is_group,
                ),
                actor_id=actor_id,
                subject_id=subject_id,
                require_proactive=True,
                payload_extra={"turn_id": turn_id, "source": "schedule.replan"},
                plan_id=f"{REPLAN_NOTICE_OPERATION_PREFIX}{turn_id}",
                part_ordinal=0,
            )
        operation_id = outcome.operation_id
        if outcome.delivered:
            delivery = "sent"
        elif outcome.state is OperationState.uncertain:
            # 不确定 ≠ 失败：可能已经到了，等对账，**不重发**。
            delivery = "uncertain"
        else:
            delivery = "failed"
    except ChannelError as exc:
        delivery = "uncertain" if exc.kind is ChannelErrorKind.uncertain else "failed"
        logger.warning(
            "重排回话投递异常 turn=%s kind=%s delivery=%s", turn_id, exc.kind, delivery
        )
    except Exception:  # noqa: BLE001 - 回话失败绝不影响主线
        logger.warning("重排回话投递异常 turn=%s", turn_id, exc_info=True)
        delivery = "failed"

    _record_replan_notice(
        actor_id=actor_id,
        turn_id=turn_id,
        reason_code=reason_code,
        delivery=delivery,
        stage="done" if delivery == "sent" else "deliver",
        window_id=window_id,
        message_id=message_id,
        operation_id=operation_id,
        text=text,
    )


@handler("schedule.replan")
async def handle_schedule_replan(payload: dict) -> None:
    """聊天里的临时提议 → 硬门 → 意愿模型 → 局部修订（设计 §4）。

    这条事件由**会话层**在用户消息落库时投递（
    `domains/conversation/service.add_message` 的接入点），幂等键是
    ``replan:<turn_id>``。它和 ``schedule.outline`` / ``schedule.refine``
    一样把模型调用放在 handler 里、不放在 outbox 的认领事务里。

    Payload 里**没有**聊天正文（只带 ``turn_id`` / ``window_id``）：
    用户的话按 ``turn_id`` 从 ``messages`` 回读，outbox 里少存一份原文就少一处
    泄漏面。缺 payload 就跳过并记日志——绝不猜。

    分支（全部由 `replan_work` 决定，这里只负责取参数与记账）：
    没有候选提议 → 什么都不做；已决定过 → 返回原决定；硬门不过/预算满/
    模型不可用/输出非法/越界/CAS 失败 → ``defer``/``decline`` 并**已经落库**；
    接受 → 局部修订提交 ``revision+1``（旧段 superseded，不物理删）。

    最后一件事是**回一句话**（见 `_send_replan_notice`）：只有真的提过建议、
    而且**没改成**（``declined``/``deferred``）才回。这一步失败只记日志与
    行为事件，绝不让本作业失败——它走到这里时计划已经定下来了。
    """
    from ling.audit import behavior_event
    from ling.domains.agenda.replan import notice_for, replan_work, turn_user_text

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    turn_id = str(payload.get("turn_id") or "").strip()
    if not turn_id:
        logger.warning("schedule.replan 缺少 turn_id，跳过: %s", payload)
        return
    window_id = str(payload.get("window_id") or "").strip() or None
    now = utcnow()
    async with session_scope() as session:
        user_text = await turn_user_text(session, turn_id=turn_id)
        outcome = await replan_work(
            session,
            actor_id=actor_id,
            turn_id=turn_id,
            user_text=user_text,
            window_id=window_id,
            now=now,
            router=_MEMORY_ROUTER,
            config_reader=_config_reader(session),
            lease_owner=f"{WORKER_ID}/replan",
        )
    if outcome.state == "no_proposal":
        # 绝大多数消息走这里：没有提议，**不写任何行**（连审计事件都不发，
        # 否则每一句闲聊都会在行为日志里刷一行"没有动计划"）。
        logger.debug("schedule.replan.no_proposal turn=%s", turn_id)
        return
    logger.info(
        "重排作业 turn=%s state=%s decision=%s reason=%s plan_day=%s "
        "revision=%s→%s superseded=%s created=%s calls=%s",
        turn_id,
        outcome.state,
        outcome.decision or "",
        outcome.reason_code or "",
        outcome.plan_day.isoformat() if outcome.plan_day else "",
        outcome.revision_before,
        outcome.revision_after,
        outcome.superseded,
        outcome.created,
        outcome.calls,
    )
    behavior_event(
        "schedule_replanned"
        if outcome.state == "accepted"
        else "schedule_replan_declined"
        if outcome.state == "declined"
        else "schedule_replan_deferred",
        # 说明里只说"改没改"，不复述用户的话。
        (
            f"把接下来一段时间改成了新安排（{outcome.created} 段，"
            f"版本 {outcome.revision_before}→{outcome.revision_after}）"
            if outcome.state == "accepted"
            else "这次提议没有改动时间轴"
        ),
        outcome="ok" if outcome.state == "accepted" else "skip",
        attributes={
            "actor_id": actor_id,
            "turn_id": turn_id,
            "state": outcome.state,
            "decision": outcome.decision or "",
            "reason_code": outcome.reason_code or "",
            "target_plan_day": (
                outcome.plan_day.isoformat() if outcome.plan_day else ""
            ),
            "plan_id": outcome.plan_id or "",
            "revision_before": outcome.revision_before,
            "revision_after": outcome.revision_after,
            "superseded": outcome.superseded,
            "created": outcome.created,
            "calls": outcome.calls,
            # 给用户的那句话（"暂未改动"就在里面）：调用方可以直接说给用户听。
            "notice": outcome.notice,
        },
    )

    # 回一句话：真的提过建议、而且**没改成**时，她要应一声。
    #
    # 只有 `declined` / `deferred` 才有回话：闲聊（`no_proposal`）一个字都不
    # 多说、`accepted` 的回复本来就在谈这件事、`duplicate` 是同一轮的重复消费。
    # 文案是代码固定的（`notice_for`），**不为回一句话调模型**。
    #
    # 幂等键是 `turn_id`：同一轮最多一条回话，重复消费不会刷第二遍。
    #
    # 这里再包一层 try：`_send_replan_notice` 自己已经逐段吞异常了，这层是
    # "将来有人往里加一行没护栏的代码"时的兜底——回话**永远**不许让本作业
    # 失败，它走到这里时计划已经定下来了。
    try:
        notice_text = notice_for(outcome)
        if notice_text:
            await _send_replan_notice(
                actor_id=actor_id,
                persona_id=str(settings.persona_id),
                turn_id=turn_id,
                window_id=window_id,
                text=notice_text,
                reason_code=str(outcome.reason_code or ""),
            )
    except Exception:  # noqa: BLE001 - 回话是附加品，绝不影响主线
        logger.warning("重排回话失败（不影响本次决定）turn=%s", turn_id, exc_info=True)


@handler("dream.generate")
async def handle_dream_generate(payload: dict) -> None:
    """生成某一晚的梦（一晚一份；模型调用在 outbox 事务外）。

    `DreamService.generate` 自己按"已有记录 → 没有睡眠窗 → 还没到睡前 →
    已过醒后 30 分钟 → 候选不足两源 → 模型不可用"的顺序判分支，绝大多数
    分支都是 0 次调用。所以重复投递只是多一次 DB 判断，不会多写一份梦。
    """
    from ling.audit import behavior_event
    from ling.domains.dream.service import STATUS_READY, DreamService

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    sleep_date = _parse_payload_date(payload, "sleep_date")
    if sleep_date is None:
        logger.warning("dream.generate 缺少可解析的 sleep_date，跳过: %s", payload)
        return
    async with session_scope() as session:
        outcome = await DreamService(
            session, actor_id=actor_id, persona_id=settings.persona_id
        ).generate(
            sleep_date,
            now=utcnow(),
            router=_MEMORY_ROUTER,
            config_reader=_config_reader(session),
        )
    logger.info(
        "梦作业完成 sleep_date=%s status=%s calls=%s reason=%s",
        sleep_date,
        outcome.status,
        outcome.calls,
        outcome.reason_code or "",
    )
    behavior_event(
        "dream_ready" if outcome.status == STATUS_READY else f"dream_{outcome.status}",
        (
            f"这一晚（{sleep_date.isoformat()}）记下一份梦（模拟叙事）"
            if outcome.status == STATUS_READY
            else f"这一晚（{sleep_date.isoformat()}）没有可记录的梦：{outcome.status}"
        ),
        outcome="ok" if outcome.status == STATUS_READY else "degraded",
        attributes={
            "actor_id": actor_id,
            "sleep_date": sleep_date.isoformat(),
            "status": outcome.status,
            "calls": outcome.calls,
            "reason_code": outcome.reason_code or "",
            "sources": outcome.sources,
        },
    )


@handler("sleep.settle")
async def handle_sleep_settle(payload: dict) -> None:
    """跨夜结算（零模型，唯一键幂等）：与梦是否成功**无关**。

    迟到补做时，`AffectService.apply_sleep_settlement` 只碰 `updated_at`
    不晚于 `wake_at` 的行——醒来之后的新心情不会被回滚成睡前值。
    """
    from ling.audit import behavior_event
    from ling.domains.agenda.timeline import resolve_plan_timezone
    from ling.domains.dream.service import SleepSettlementService

    settings = get_settings()
    actor_id = str(payload.get("actor_id") or settings.actor_id)
    sleep_date = _parse_payload_date(payload, "sleep_date")
    if sleep_date is None:
        logger.warning("sleep.settle 缺少可解析的 sleep_date，跳过: %s", payload)
        return
    async with session_scope() as session:
        timezone, _source = await resolve_plan_timezone(session)
        outcome = await SleepSettlementService(session, actor_id=actor_id).settle(
            sleep_date, now=utcnow(), timezone=timezone
        )
    logger.info(
        "跨夜结算 sleep_date=%s state=%s components=%d reason=%s",
        sleep_date,
        outcome.state,
        outcome.components,
        outcome.reason_code or "",
    )
    behavior_event(
        "sleep_settled" if outcome.state == "settled" else "sleep_settle_skipped",
        (
            f"睡后心情结算完成（{sleep_date.isoformat()}）"
            if outcome.state == "settled"
            else f"睡后心情结算跳过（{sleep_date.isoformat()}）：{outcome.state}"
        ),
        attributes={
            "actor_id": actor_id,
            "sleep_date": sleep_date.isoformat(),
            "state": outcome.state,
            "components": outcome.components,
            "reason_code": outcome.reason_code or "",
        },
    )


# ---------------------------------------------------------------- outbox 事务边界
#
# ## 改之前是什么样
#
#     async with session_scope() as session:          # ← 一个事务包住整批
#         rows = select(...).with_for_update(skip_locked=True)
#         for row in rows:
#             await fn(row.payload)                    # ← 模型调用在这里
#             row.state = "done"
#
# 也就是说：一条 60 秒的大纲/梦模型调用期间，这批行的行锁一直没放。别的
# worker 用 `SKIP LOCKED` 也领不到它们（和 `pending` 队列里其它**无关**的
# 事件，只要落在同一个 limit 批次里也一样），还会把连接池里的连接一起占着。
#
# ## 现在是什么样
#
#     短事务认领并**提交**（state=in_flight + processed_by + available_at=租约到期）
#     → 事务外 `await fn(payload)`（模型调用在这里，没有任何行锁）
#     → 短事务确认（done / 退避重排 / dead）
#
# 至少一次的语义没变、也不能变：handler 必须先提交、后确认，所以"处理完了但
# 没确认"的情况一定存在。消费方按事件 id / 唯一键幂等，重复执行只是多跑一次，
# 不会多写一份数据。崩溃后没确认的 in-flight 事件由**租约到期**重新排队
# （进程正常退出时还会主动释放，见 `release_outbox_leases`）。
#
# `available_at` 在这里有**两种含义**，按 state 区分（表上没有 lease 列，
# 也不该为了这个改动加迁移）：
#   - state=pending   → "什么时候可以再试"（退避）
#   - state=in_flight → "租约什么时候到期"（认领者必须在到期前确认）

# 认领租约（秒）。必须**显著长于**单个 handler 的最坏执行时间：
# 大纲最多 2 次 60 秒模型调用（timeline.OUTLINE_MAX_CALLS × 超时），梦是 1 次
# 60 秒。租约不表达"慢"，只表达"进程可能已经死了"——超时由各服务自己的
# wait_for 负责，这里等太久只会让重启后的补投晚一点。
OUTBOX_LEASE_SECONDS = 600.0


@dataclass(frozen=True)
class OutboxClaim:
    """一条**已认领** outbox 事件的执行快照。

    认领事务已经提交，行锁已释放，ORM 行不能再当"还归我管"的东西用。
    这里只带走执行 handler 需要的字段；状态回写一律以主键重新读一遍为准。
    """

    event_id: str
    topic: str
    payload: dict
    attempts: int


async def _reclaim_expired_outbox_leases(session, *, now: datetime) -> int:  # noqa: ANN001
    """把租约过期的 in-flight 事件放回 pending（崩溃/重启后补投的唯一入口）。"""
    result = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.state == "in_flight",
            OutboxEvent.available_at <= now,
        )
        .values(
            state="pending",
            available_at=now,
            # 清掉所有者：原认领者若还活着，它迟到的确认会因为"租约已不属于
            # 本实例"而被丢弃，不会把别人刚接手的事件偷偷改掉。
            processed_by=None,
            last_error="lease_expired",
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def reclaim_expired_outbox_leases() -> int:
    """短事务：回收过期租约（`drain_outbox` 每轮开头调一次）。"""
    now = utcnow()
    async with session_scope() as session:
        return await _reclaim_expired_outbox_leases(session, now=now)


async def claim_next_outbox_event() -> OutboxClaim | None:
    """短事务认领**一条**可跑的事件并立即提交。

    为什么一次只认领一条，而不是一批：
    - 一批认领会把"还没开始跑"的行也标成 in_flight，等于用租约挡住别的
      worker——锁是没了，吞吐却没恢复，只是把等待从行锁换成了租约；
    - 一条一认领后，慢事件只拖住它自己，另一条可以立刻被另一个连接领走
      （`tests/integration/test_life_schedule_worker.py` 用两个真连接验这件事）。
    代价是每条事件多两个短事务，在这个量级上无关紧要。
    """
    now = utcnow()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.state == "pending",
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.state = "in_flight"
        row.processed_by = WORKER_ID
        row.available_at = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
        claim = OutboxClaim(
            event_id=str(row.id),
            topic=str(row.topic),
            payload=dict(row.payload or {}),
            attempts=int(row.attempts or 0),
        )
        # 出 `async with` 即提交：handler 在**没有行锁**的事务外跑。
    return claim


async def settle_outbox_event(
    claim: OutboxClaim, *, ok: bool, error: str | None = None
) -> None:
    """短事务确认：成功→done；失败→按退避重排；到上限→dead。"""
    now = utcnow()
    async with session_scope() as session:
        row = await session.get(OutboxEvent, claim.event_id, with_for_update=True)
        if row is None:  # pragma: no cover - 事件被人工删掉
            return
        # 只有**当前**租约的所有者能确认。租约过期被别人回收以后，原认领者
        # 迟到的确认一律丢弃：宁可让这条事件多跑一次（至少一次），也不能让
        # 两个 worker 互相覆盖状态。
        if str(row.state) != "in_flight" or str(row.processed_by or "") != WORKER_ID:
            logger.info(
                "outbox %s 的租约已不属于本实例（state=%s），跳过确认",
                claim.event_id,
                row.state,
            )
            return
        if ok:
            row.state = "done"
            row.processed_at = now
            row.last_error = None
            return
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = (error or "")[:500]
        if row.attempts < MAX_ATTEMPTS:
            # 指数退避（此前实现的问题）：不退避的话连续几轮轮询就会
            # 把次数耗尽，临时故障也变成永久失败。
            delay = min(MAX_BACKOFF_SECONDS, 2 ** row.attempts)
            row.state = "pending"
            row.available_at = now + timedelta(seconds=delay)
        else:
            # 不静默放弃：转 "dead" 状态等待人工/运维重放，
            # 原始事件仍在库里，派生数据只是暂缺（不是物理丢失）。
            row.state = "dead"
        logger.warning(
            "outbox %s 处理失败（第 %d 次）: %s", claim.event_id, row.attempts, error
        )


async def mark_outbox_unhandled(claim: OutboxClaim) -> None:
    """没有注册 handler：标 ``unhandled`` 保留在库里，不静默丢弃。"""
    async with session_scope() as session:
        row = await session.get(OutboxEvent, claim.event_id, with_for_update=True)
        if row is None or str(row.state) != "in_flight":
            return
        row.state = "unhandled"
        row.processed_by = WORKER_ID
        row.last_error = f"没有注册 {claim.topic} 的处理器（W1/W2 模块尚未接入）"


async def release_outbox_leases() -> int:
    """进程正常退出时主动释放自己的租约（不必等过期）。

    崩溃时走不到这里——那种情况由租约到期兜底。两边都留着：正常运行不该
    让重启后的 worker 白等 10 分钟，崩溃恢复也不能依赖"优雅退出"。
    """
    now = utcnow()
    async with session_scope() as session:
        result = await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.state == "in_flight",
                OutboxEvent.processed_by == WORKER_ID,
            )
            .values(
                state="pending",
                available_at=now,
                processed_by=None,
                last_error="lease_released",
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


async def drain_outbox(limit: int = 50) -> int:
    """至少一次处理；消费方按事件 id 幂等。

    每次认领 = 一个短事务，handler 在事务外跑（见本节顶部"改之前/现在"）。
    `limit` 是"这一轮最多跑几条"，不是"一次锁几条"。
    """
    await refresh_memory_router_if_stale()
    reclaimed = await reclaim_expired_outbox_leases()
    if reclaimed:
        logger.info("回收过期 outbox 租约 %d 条", reclaimed)
    processed = 0
    for _ in range(max(0, limit)):
        claim = await claim_next_outbox_event()
        if claim is None:
            break
        fn = HANDLERS.get(claim.topic)
        if fn is None:
            await mark_outbox_unhandled(claim)
            continue
        # 单条失败不影响这一轮的其它事件；异常只用来决定这条事件的状态。
        error = await _guarded(claim.topic, fn, claim.payload)
        await settle_outbox_event(
            claim,
            ok=error is None,
            error=None if error is None else f"{type(error).__name__}: {error}",
        )
        if error is None:
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
        # 正常退出时主动放掉自己持有的 outbox 租约：否则重启后的 worker 要
        # 白等一个租约周期（10 分钟）才补投。崩溃走不到这里，那种情况由
        # 租约到期兜底——两条路都要有，不能只依赖优雅退出。
        with contextlib.suppress(Exception):
            released = await release_outbox_leases()
            if released:
                logger.info("已释放 outbox 租约 %d 条", released)
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
    **每个注册的 topic 必须出现在这里、`OUTBOX_TOPICS`、或
    `LEGACY_MANUAL_TOPICS` 三者之一**。
    """

    topic: str
    run: object  # async (payload: dict) -> None
    payload: dict
    interval: float


PERIODIC_JOBS: tuple[PeriodicJob, ...] = (
    # 决定要回之后**是否真的生成并投递**，看已发布配置
    # `group_reply_delivery_enabled`（默认关），不看 payload。
    # 这里曾经写死过 `{"shadow": True}`（群里她永远不开口），后来又写死成
    # `{"shadow": False}`——两次都是硬编码，用户没有开关可选。
    PeriodicJob("attention.tick", handle_attention_tick, {}, ATTENTION_TICK_SECONDS),
    PeriodicJob("embedding.backfill", handle_embedding_backfill, {}, EMBEDDING_BACKFILL_SECONDS),
    # 出站投递对账 + 续发。**独立 topic**：它只查 operations 表，
    # 与生活时间轴（life.schedule）互不依赖，不能塞进那边的循环。
    PeriodicJob(
        "delivery.reconcile",
        handle_delivery_reconcile,
        {},
        DELIVERY_RECONCILE_SECONDS,
    ),
    PeriodicJob("intent.tick", handle_intent_tick, {}, PROACTIVE_COLLECT_SECONDS),
    # 生活调度：只查 DB 到期边界（21:00 目标日、00:00 接缝、梦窗口、结算），
    # 到期才把真正干活的作业投进 outbox。**这里不调模型**。
    PeriodicJob("life.schedule", handle_life_schedule, {}, LIFE_SCHEDULE_SECONDS),
    # `life.plan`（旧的"醒后排 1–3 件小事"）**不在这里**：新时间轴接管后，
    # 两套并行生成就是两份日程。它被移进 `LEGACY_MANUAL_TOPICS`，只剩手工
    # 触发；旧 `agenda_plans` 仍可读，手工小事仍走 AgendaService。
    PeriodicJob("life.tick", handle_life_tick, {}, LIFE_TICK_SECONDS),
    PeriodicJob("media.describe", handle_media_describe, {}, MEDIA_DESCRIBE_SECONDS),
    # 收集阶段没有开关：它只登记候选，发不发由 proactive.dispatch 与
    # `proactive_delivery_enabled` 决定（见 handle_proactive_collect）。
    PeriodicJob("proactive.collect", handle_proactive_collect, {}, PROACTIVE_COLLECT_SECONDS),
    PeriodicJob("proactive.draft", handle_proactive_draft, {}, PROACTIVE_DRAFT_SECONDS),
    PeriodicJob("proactive.dispatch", handle_proactive_dispatch, {}, PROACTIVE_DISPATCH_SECONDS),
    PeriodicJob("session.purge", handle_session_purge, {}, SESSION_PURGE_SECONDS),
    PeriodicJob("self.timeline", handle_self_timeline, {}, SELF_TIMELINE_SECONDS),
)

# 由 outbox 投递的 topic（不是周期任务，是事件驱动）。
# `schedule.*` / `dream.*` / `sleep.*` 由 `life.schedule` 在到期边界投递，
# 真正的模型调用发生在这些 handler 里（认领事务之外，见 outbox 小节）。
OUTBOX_TOPICS: frozenset[str] = frozenset({
    "commitment.dispatch",
    "dream.generate",
    "memory.archive",
    "memory.extract",
    "relationship.update",
    "schedule.outline",
    "schedule.refine",
    # 聊天里的临时提议：会话层在消息落库时投递（见
    # `domains/conversation/service.add_message` 的接入点），handler 在这里跑
    # 硬门 + 意愿模型 + 局部修订。**不是**周期任务：没有提议就没有事件。
    "schedule.replan",
    "schedule.seam",
    "sleep.settle",
    "window.summary",
})

# **只保留手工触发**的 handler。注册它们不是为了自动跑，而是为了：
# - 运维/接口能显式重放一次（旧数据修复、排查）；
# - 派发闸门能区分"忘了接调度"和"有意不接"——两者长得一模一样，
#   所以必须在代码里写出意图，而不是让 handler 独自躺着。
# `life.plan` 在这里的理由：新时间轴（`life.schedule` + `day_plans`）已经
# 接管"今天/明天做什么"，旧路径再自动生成就是第二份日程。
LEGACY_MANUAL_TOPICS: frozenset[str] = frozenset({"life.plan"})


async def _guarded(name: str, fn, payload: dict) -> BaseException | None:  # noqa: ANN001
    """跑一个后台任务，失败只记日志并**返回**异常（不抛）。

    每件事各自隔离：原来四件事共用一个 try，收集器一炸后面全跳过，
    而它们彼此无关。后台任务之间**不该有隐式依赖**。

    返回异常而不是 None/True：outbox 的确认步骤要拿"这次到底成没成"决定
    done / 退避重排。周期任务的调用方忽略返回值即可，语义不变。
    """
    try:
        await fn(payload)
    except Exception as exc:  # noqa: BLE001 - worker 不能因一次失败退出
        logger.warning("%s异常: %s", name, exc)
        return exc
    return None


def registered_topics() -> list[str]:
    return sorted(HANDLERS)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
