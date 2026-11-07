"""TurnCoordinator + ActorSequencer：一轮对话的权威提交。

职责边界（设计文档 §3）：
- TurnCoordinator：轮次状态、停止令牌、迟到结果丢弃。
- ActorSequencer：权威事件提交与 revision 栅栏。
- 本模块不直接发平台消息、不授予权限；它只协调模型流与领域提交。

会话生命周期：轮次任务**自带数据库会话**，不复用请求会话。这样客户端断线、
请求结束都不会截断权威提交。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import ChatMessage, ErrorCode, Origin, TurnState, new_id
from ling.domains.conversation.service import ConversationService, MessageView
from ling.domains.media.video import (
    finalize_reply_text,
    parse_watch_markers,
    stream_safe,
    strip_watch_markers,
)
from ling.infra.db import session_scope
from ling.infra.models import ActorEvent, ActorState, OutboxEvent, PromptSnapshot, utcnow
from ling.kernel.arbiter import SpeakerArbiter
from ling.kernel.context import ContextCompiler, estimate_tokens
from ling.kernel.stream_log import StreamLog
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig
from ling.ports.model import ModelError, ModelErrorKind

if TYPE_CHECKING:
    from ling.kernel.context import CompiledContext, CompiledMessage

# 排队等待参数：等不到就放弃对外流式（内容仍落库，前端能看到最终消息）
# 排队最长等待：8 秒（32 × 0.25）。超过就放弃对外流式，
# 内容仍落库，前端最终能拿到完整消息。
logger = logging.getLogger("ling.kernel.turn")

ARBITER_WAIT_STEPS = 32
ARBITER_WAIT_SECONDS = 0.25
# 停止请求后多久强制取消：给优雅停止一点时间（保留已交付文本），
# 但不能无限期停在 stopping。真实模型的 chunk 间隔可能数秒。
FORCE_CANCEL_AFTER_SECONDS = 3.0
# 单条消息的文本上限（Web 很大；QQ/微信接入后按渠道能力下调）
MAX_TEXT_PER_MESSAGE = 4096

# ---------------------------------------------------------------- 提示词快照
#
# 「查看本次提示词」：把**这一轮真正发给模型的 messages** 存一份只读快照。
#
# 三条硬约束（每一条都对应一个真实故障模式）：
# 1. **写快照绝不阻塞出话**：任何异常只记日志（带 reason_code）并继续。
#    快照是审计旁路，不是对话事实；它失败不该让用户等不到回复。
# 2. **单条 ≤256KB**：超了就丢中间段（保留 system 与最后 N 条）并置
#    `truncated`。不设上限的话，一个粘贴长文的窗口就能把行写到几 MB。
# 3. **一轮一份**：`turn_id` 唯一，重试覆盖（不是每 attempt 一条）。
PROMPT_SNAPSHOT_SCHEMA_VERSION = 1
PROMPT_SNAPSHOT_MAX_BYTES = 256 * 1024
# 保留期：7 天。到期后维护任务先清正文、再删墓碑。
PROMPT_SNAPSHOT_RETENTION_DAYS = 7
# 墓碑（正文已清空、只剩"这一轮曾经有过快照"）再保留 7 天。
#
# 为什么要墓碑：读接口必须把「过期」与「从没存下来」分开。到期直接删行的话，
# 前端只能把"内容按保留期清掉了"显示成"从来没存过"——那是在报假错。
# 墓碑本身只有几十字节，不会撑表。
PROMPT_SNAPSHOT_TOMBSTONE_DAYS = 7
# 截断时单条正文至少留这么多字符；再小就失去可读性，不如整条丢掉。
_PROMPT_SNAPSHOT_MIN_CONTENT_CHARS = 200
_PROMPT_SNAPSHOT_TRUNCATION_MARK = "…（快照超上限，已截断）"


def _snapshot_json(value: object) -> str:
    """稳定序列化：紧凑分隔符 + 不转义中文（体积与可读性都按真实字符算）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _snapshot_size(value: object) -> int:
    return len(_snapshot_json(value).encode("utf-8"))


def _snapshot_entry(item: CompiledMessage, index: int) -> dict[str, object]:
    """一条消息 → 快照里的一项（含语义块标签与 token 估算）。"""
    return {
        "index": index,
        "role": item.role,
        "key": item.key,
        "lane": item.lane,
        "tokens": estimate_tokens(item.content),
        "content": item.content,
        "sender": item.sender,
        "at": item.at.isoformat() if item.at is not None else None,
        "truncated": False,
    }


def build_prompt_snapshot_payload(
    annotated: list[CompiledMessage],
    *,
    model: dict[str, object] | None = None,
    captured_at: object | None = None,
) -> tuple[dict[str, object], bool]:
    """把**真正要发出去的那份 messages** 装成快照 payload。

    返回值是 ``(payload, truncated)``。``messages`` 必须与 provider 收到的同源
    （调用方直接传编译结果，不要重建一份），否则"她看到的"和"我们记的"会是
    两份各自拼出来的近似物。

    超 ``PROMPT_SNAPSHOT_MAX_BYTES`` 时的截断顺序：

    1. 先保留**所有非历史段**（人设/规则/记忆/摘要/媒体…）与**最后 N 条历史**
       ——"她当时是谁、记得什么"比"更早那些对话"重要得多；
    2. 仍超（少见：某一段自己就巨大）→ 从最长的那条正文开始砍半，逐轮直到
       装得下，并给它打上 ``truncated``；
    3. 无论走哪条路，``truncated=True`` 且 ``dropped_items`` 如实记丢了几条。
    """
    entries = [_snapshot_entry(item, index) for index, item in enumerate(annotated)]
    token_total = sum(int(entry["tokens"]) for entry in entries)
    payload: dict[str, object] = {
        "schema_version": PROMPT_SNAPSHOT_SCHEMA_VERSION,
        "captured_at": (
            captured_at.isoformat() if captured_at is not None else utcnow().isoformat()
        ),
        "model": model or {},
        # 这是**这一轮实际发出去**的总量（含后面因超限没存进快照的条目），
        # 所以它不等于 sections 里 tokens 的加总——两者刻意不同，别互相校对。
        "token_total": token_total,
        "truncated": False,
        "dropped_items": 0,
        "messages": entries,
    }
    if _snapshot_size(payload) <= PROMPT_SNAPSHOT_MAX_BYTES:
        return payload, False

    # 1) 非历史段全留，历史段二分"还能留最后多少条"。
    locked = [e for e in entries if e["key"] != "history"]
    history = [e for e in entries if e["key"] == "history"]

    def _probe(keep_history: int) -> tuple[list[dict[str, object]], int]:
        keep = {id(e) for e in locked}
        if keep_history:
            keep |= {id(e) for e in history[len(history) - keep_history :]}
        kept = [e for e in entries if id(e) in keep]
        candidate = {**payload, "truncated": True, "messages": kept}
        return kept, _snapshot_size(candidate)

    best_kept: list[dict[str, object]] = locked
    lo, hi = 0, len(history)
    while lo <= hi:
        mid = (lo + hi) // 2
        kept, size = _probe(mid)
        if size <= PROMPT_SNAPSHOT_MAX_BYTES:
            best_kept, lo = kept, mid + 1
        else:
            hi = mid - 1

    # 2) 锁定段自己就超：从最长的正文砍半（保留头部，标出截断）。
    while _snapshot_size({**payload, "truncated": True, "messages": best_kept}) > (
        PROMPT_SNAPSHOT_MAX_BYTES
    ):
        target = max(best_kept, key=lambda e: len(str(e["content"])), default=None)
        if target is None:
            break
        text = str(target["content"])
        if len(text) <= _PROMPT_SNAPSHOT_MIN_CONTENT_CHARS:
            # 已经没有可读的余量了：丢掉最短的那条（保留顺序），如实记数。
            if len(best_kept) <= 1:
                break
            shortest = min(best_kept, key=lambda e: len(str(e["content"])))
            best_kept = [e for e in best_kept if e is not shortest]
            continue
        cut = max(_PROMPT_SNAPSHOT_MIN_CONTENT_CHARS, len(text) // 2)
        target["content"] = text[:cut] + _PROMPT_SNAPSHOT_TRUNCATION_MARK
        target["truncated"] = True
        target["tokens"] = estimate_tokens(str(target["content"]))

    payload["messages"] = best_kept
    payload["truncated"] = True
    payload["dropped_items"] = len(entries) - len(best_kept)
    return payload, True


def _snapshot_model_info(
    config: EffectiveConfig, profile: str
) -> dict[str, object]:
    """记录"这一轮打算跑在哪个模型上"（拿不到就记空）。

    用 ``resolve_profile`` 而不是自己读绑定：全系统只有一份解析规则，
    快照里写一套、路由里跑另一套的话，这份快照会解释错原因。
    """
    info: dict[str, object] = {"profile": profile, "config_revision": config.revision}
    try:
        resolved = config.resolve_profile(profile)
    except Exception:  # noqa: BLE001 - 记不下来不该影响快照本身
        return info
    info["provider_id"] = getattr(resolved, "provider_id", None)
    info["model_id"] = getattr(resolved, "model_id", None)
    info["source"] = getattr(getattr(resolved, "source", None), "value", None)
    return info


async def save_prompt_snapshot(
    *,
    actor_id: str,
    turn_id: str,
    window_id: str,
    payload: dict[str, object],
    truncated: bool,
    retention_days: int = PROMPT_SNAPSHOT_RETENTION_DAYS,
) -> bool:
    """写入/覆盖这一轮的快照（**独立事务**，best-effort）。

    ## 为什么必须是独立事务

    捕获点跑在轮次事务**里面**。用同一个会话写快照有两个真实风险：

    1. 写失败会把整轮事务带脏（PostgreSQL 里一条失败的语句之后所有语句都报
       ``current transaction is aborted``）——用户等不到回复；
    2. 轮次事务回滚时快照一起消失，可"模型确实已经被喂了这段内容"。

    独立事务解决两者：快照一旦写成功就是独立的观测记录。代价是它可能记下
    "发了但整轮失败了"的上下文——那正是事实（模型确实看到了）。

    返回 ``True`` 表示已写入，``False`` 表示失败（调用方只记日志，不抛）。
    """
    size_bytes = _snapshot_size(payload)
    now = utcnow()
    expires_at = now + timedelta(days=retention_days)

    async def _insert() -> None:
        async with session_scope() as session:
            session.add(
                PromptSnapshot(
                    id=new_id("psnap"),
                    actor_id=actor_id,
                    turn_id=turn_id,
                    window_id=window_id,
                    message_id=None,
                    created_at=now,
                    expires_at=expires_at,
                    schema_version=PROMPT_SNAPSHOT_SCHEMA_VERSION,
                    size_bytes=size_bytes,
                    truncated=truncated,
                    payload=payload,
                )
            )

    async def _overwrite() -> None:
        async with session_scope() as session:
            await session.execute(
                update(PromptSnapshot)
                .where(PromptSnapshot.turn_id == turn_id)
                .values(
                    window_id=window_id,
                    # 这一轮新的回复还没落库：旧的 message_id 指向上一 attempt
                    # 的消息（可能已被 retry 删掉），必须先清空。
                    message_id=None,
                    created_at=now,
                    expires_at=expires_at,
                    schema_version=PROMPT_SNAPSHOT_SCHEMA_VERSION,
                    size_bytes=size_bytes,
                    truncated=truncated,
                    payload=payload,
                )
            )

    try:
        existing = None
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(PromptSnapshot.id).where(
                        PromptSnapshot.turn_id == turn_id
                    )
                )
            ).scalar_one_or_none()
        if existing is None:
            await _insert()
        else:
            await _overwrite()
        return True
    except IntegrityError:
        # 同一轮的两个 attempt 同时写（「停止 → 立即重试」时真的会同时跑）：
        # 唯一约束挡住了后到的那份。改成覆盖——后写的那份才是这一轮真正
        # 要发给模型的内容。
        try:
            await _overwrite()
            return True
        except Exception as exc:  # noqa: BLE001 - 快照失败不影响本轮
            logger.warning(
                "提示词快照写入失败（不影响本轮回复） turn=%s reason_code=%s detail=%s",
                turn_id,
                "prompt_snapshot_write_conflict",
                f"{type(exc).__name__}: {exc}"[:200],
            )
            return False
    except Exception as exc:  # noqa: BLE001 - 快照失败不影响本轮
        logger.warning(
            "提示词快照写入失败（不影响本轮回复） turn=%s reason_code=%s detail=%s",
            turn_id,
            "prompt_snapshot_write_failed",
            f"{type(exc).__name__}: {exc}"[:200],
        )
        return False


async def purge_expired_prompt_snapshots(
    session: AsyncSession,
    *,
    tombstone_days: int = PROMPT_SNAPSHOT_TOMBSTONE_DAYS,
) -> dict[str, int]:
    """清理过期快照：**先清正文，再删墓碑**（维护任务调用）。

    为什么不是到期直接删行：读接口要能说清"这一轮**曾经**有过快照，只是
    过了保留期"（``expired``）与"从来没存下来"（``unavailable``）。删了行
    就再也分不开，前端只能把"内容按保留期清掉了"报成"从来没存过"。

    两段：

    1. 已过期且还有正文 → 只保留**非内容**元数据（model / token_total），
       ``size_bytes`` 归零。读接口据此仍报 ``expired``（不返回 sections）。
    2. 墓碑再放 ``tombstone_days`` 天 → 真删。

    幂等：两段都是条件 UPDATE/DELETE，重复跑就是 0 行；多副本并发也安全。
    """
    now = utcnow()
    deleted = 0
    scrubbed = 0

    result = await session.execute(
        PromptSnapshot.__table__.delete().where(
            PromptSnapshot.expires_at <= now - timedelta(days=tombstone_days)
        )
    )
    deleted = int(getattr(result, "rowcount", 0) or 0)

    rows = list(
        (
            await session.execute(
                select(PromptSnapshot).where(
                    PromptSnapshot.expires_at <= now,
                    PromptSnapshot.size_bytes > 0,
                )
            )
        ).scalars()
    )
    for row in rows:
        meta = row.payload if isinstance(row.payload, dict) else {}
        row.payload = {
            "schema_version": int(row.schema_version or PROMPT_SNAPSHOT_SCHEMA_VERSION),
            "model": meta.get("model") or {},
            "token_total": int(meta.get("token_total") or 0),
            "scrubbed": True,
            "messages": [],
        }
        row.size_bytes = 0
        row.truncated = False
        scrubbed += 1
    if rows:
        await session.flush()

    if scrubbed or deleted:
        logger.info(
            "提示词快照清理：清空正文 %d 条，删除过期墓碑 %d 条", scrubbed, deleted
        )
    return {"scrubbed": scrubbed, "deleted": deleted}



@dataclass
class TurnStreamEvent:
    """SSE 事件的服务端表示。"""

    event: str
    data: dict[str, object] = field(default_factory=dict)


class StaleRevisionError(RuntimeError):
    pass


class ActorSequencer:
    """角色权威事件提交。只串行提交，不在持锁时等待模型。"""

    def __init__(self, session: AsyncSession, *, actor_id: str, persona_id: str) -> None:
        self._session = session
        self._actor_id = actor_id
        self._persona_id = persona_id

    async def _state(self) -> ActorState:
        state = await self._session.get(ActorState, self._actor_id)
        if state is None:
            state = ActorState(actor_id=self._actor_id, revision=0, persona_id=self._persona_id)
            self._session.add(state)
            await self._session.flush()
        return state

    async def next_revision(self) -> int:
        state = await self._state()
        return int(state.revision) + 1

    async def commit_event(
        self,
        *,
        type: str,
        payload: dict,
        origin: Origin,
        visibility: str = "subject_private",
        subject_id: str | None = None,
        window_id: str | None = None,
        expected_revision: int | None = None,
        causation_id: str | None = None,
    ) -> ActorEvent:
        state = await self._state()
        if expected_revision is not None and int(state.revision) != expected_revision:
            raise StaleRevisionError(
                f"角色状态已推进到 revision {state.revision}，期望 {expected_revision}"
            )
        state.revision = int(state.revision) + 1
        state.last_event_at = utcnow()
        event = ActorEvent(
            id=new_id("evt"),
            actor_id=self._actor_id,
            persona_id=self._persona_id,
            subject_id=subject_id,
            window_id=window_id,
            type=type,
            origin=origin.value,
            visibility=visibility,
            occurred_at=utcnow(),
            observed_at=utcnow(),
            causation_id=causation_id,
            revision=state.revision,
            payload=payload,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def timeline(self, *, limit: int = 50) -> list[ActorEvent]:
        from sqlalchemy import select

        stmt = (
            select(ActorEvent)
            .where(ActorEvent.actor_id == self._actor_id)
            .order_by(ActorEvent.revision.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list((await self._session.execute(stmt)).scalars())


class StopToken:
    """停止信号。用事件而不是 task.cancel()，保证能优雅写完 stopped 状态。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def stop(self) -> None:
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:  # pragma: no cover - 供外部等待
        await self._event.wait()


class CancelRegistry:
    """轮次登记表（单进程）。多实例部署时改用数据库标志。"""

    def __init__(self) -> None:
        self._tokens: dict[str, StopToken] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # 流式中的半成品文本（内存态）：刷新后前端可继续显示
        self._partial: dict[str, str] = {}

    def set_partial(self, turn_id: str, text: str) -> None:
        self._partial[turn_id] = text

    def partial(self, turn_id: str) -> str:
        return self._partial.get(turn_id, "")

    def register(self, turn_id: str, token: StopToken, task: asyncio.Task) -> None:
        self._tokens[turn_id] = token
        self._tasks[turn_id] = task

    def discard(self, turn_id: str) -> None:
        self._tokens.pop(turn_id, None)
        self._tasks.pop(turn_id, None)
        self._partial.pop(turn_id, None)

    def stop(self, turn_id: str) -> bool:
        """请求停止。

        先设优雅停止令牌（保留已交付文本），再安排一个**有界兜底**：
        真实模型的 chunk 间隔可能很长，只等令牌会让轮次长时间停在
        stopping（前端一直显示生成中）。超时后强制取消任务。
        """
        token = self._tokens.get(turn_id)
        if token is None:
            return False
        token.stop()
        task = self._tasks.get(turn_id)
        if task is not None and not task.done():
            async def _force_cancel() -> None:
                await asyncio.sleep(FORCE_CANCEL_AFTER_SECONDS)
                if not task.done():
                    task.cancel()

            with contextlib.suppress(RuntimeError):  # 无事件循环时忽略
                asyncio.get_running_loop().create_task(_force_cancel())
        return True

    def is_running(self, turn_id: str) -> bool:
        task = self._tasks.get(turn_id)
        return task is not None and not task.done()

    def running_turns(self) -> list[str]:
        return [tid for tid, t in self._tasks.items() if not t.done()]


class TurnCoordinator:
    """一轮对话的执行器。每次运行开自己的数据库会话。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        actor_id: str,
        persona_id: str,
        cancels: CancelRegistry,
        # 她的显示名。用于剥掉"她：…"这种照抄示例格式的前缀。
        display_name: str = "",
        arbiter: SpeakerArbiter | None = None,
        stream_log: StreamLog | None = None,
        turn_control: object | None = None,
        speak_lease: object | None = None,
        # 已解密供应商密钥的同步读取器（provider_id → secret）。嵌入/重排
        # 的 provider 要在构造时同步拿密钥，而数据库是异步的，所以两条路径
        # 都是"进程内预热一份缓存、构造时同步读"（API 用 app.state.secret_cache，
        # worker 用 _SECRET_CACHE）。不传 = 只用环境变量装配语义检索。
        secret_reader: Callable[[str], str | None] | None = None,
        # "她说要看视频"时的读取回调：``(window_id, refs) -> 观察文本 | None``。
        # 不传就用默认装配（自己去找设置/连接器/媒体取用函数，见
        # `domains/media/watch.py::default_watch_callback`）。
        watch_video: Callable[[str, list[str]], Awaitable[str | None]] | None = None,
    ) -> None:
        self._router = router
        self._watch_video = watch_video
        self._actor_id = actor_id
        self._persona_id = persona_id
        self._display_name = display_name
        self._cancels = cancels
        # 多端同时说话时，同一角色同一时刻只在一个窗口"说"（设计文档 §5）
        self._arbiter = arbiter
        # 流式片段日志：支持断线续传（多实例时换成持久化实现）
        self._stream_log = stream_log
        # 跨实例停止信号（单进程时可为 None）
        self._turn_control = turn_control
        # 跨实例发言租约（数据库）。提供时优先于进程内仲裁器：
        # 多副本部署下"同一角色同时只在一个窗口说话"必须靠数据库，
        # 进程内 arbiter 只覆盖单进程。
        self._speak_lease = speak_lease
        self._secret_reader = secret_reader

    async def run_turn(
        self,
        *,
        window_id: str,
        subject_id: str,
        turn_id: str,
        user_text: str,
        config: EffectiveConfig,
        profile: str = "dialogue.fast",
        attempt: int = 1,
        # 产生这一轮的渠道账号（"instance:external_id"）。记忆要记下它，
        # 否则同一人多个号合并后无法无损拆分。
        source_account: str | None = None,
        # 「这条消息是谁说的」——本次称呼（群里 = 群名片优先）。
        # 只用于给当轮上下文里**最后那句**署名，不落库：库里的
        # `subjects.display_name` 是人级旁路（见 identity.observe_display_name）。
        sender_name: str | None = None,
    ) -> asyncio.Queue[TurnStreamEvent | None]:
        queue: asyncio.Queue[TurnStreamEvent | None] = asyncio.Queue()
        token = StopToken()
        task = asyncio.create_task(
            self._execute(
                window_id=window_id,
                subject_id=subject_id,
                turn_id=turn_id,
                user_text=user_text,
                config=config,
                profile=profile,
                queue=queue,
                token=token,
                attempt=attempt,
                source_account=source_account,
                sender_name=sender_name,
            ),
            name=f"turn:{turn_id}",
        )
        self._cancels.register(turn_id, token, task)
        # 跨实例停止：轮询持久化信号，发现就设置本地令牌。
        # **必须带上 attempt**：retry 复用同一个 turn_id，只把 attempt 加一。
        # 信号若只按 turn_id 绑定，就是"与轮次永久绑定"——用户「停止 → 重试」
        # 时新一轮的首次轮询（watch 先睡 0.5s 再查）会命中上一轮留下的标记，
        # 直接收敛成 stopped，重试多少次都停。
        watcher: asyncio.Task | None = None
        control = self._turn_control
        if control is not None and hasattr(control, "watch"):
            watcher = asyncio.create_task(
                control.watch(turn_id, token, attempt),  # type: ignore[attr-defined]
                name=f"stopwatch:{turn_id}",
            )
        def _cleanup(_: asyncio.Task) -> None:
            self._cancels.discard(turn_id)
            if watcher is not None:
                watcher.cancel()

        task.add_done_callback(_cleanup)
        return queue

    async def _execute(
        self,
        *,
        window_id: str,
        subject_id: str,
        turn_id: str,
        user_text: str,
        config: EffectiveConfig,
        profile: str,
        queue: asyncio.Queue[TurnStreamEvent | None],
        token: StopToken,
        attempt: int = 1,
        source_account: str | None = None,
        sender_name: str | None = None,
    ) -> None:
        delivered = ""
        target_label: str | None = None
        stopped = False
        error: ModelError | None = None
        held_by_me = False
        # 仲裁只尝试一次：未取得就本轮不再等，避免每个 delta 都白等 8 秒
        floor_settled = False
        # 曾经持有过、后来续约失败（租约被别人拿走/过期接管）。
        # 用来在收尾时如实说明"为什么最后没对外交付"。
        lease_lost = False

        try:
            # 发言权：只在真正要产出文本时占用。
            # 被别的窗口占着时不阻塞——继续生成候选，只是不对外"说"。
            if self._speak_lease is not None or self._arbiter is not None:
                if self._speak_lease is not None:
                    acquired = await self._speak_lease.acquire(  # type: ignore[attr-defined]
                        actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                    )
                else:
                    acquired = self._arbiter.acquire(  # type: ignore[union-attr]
                        window_id=window_id, turn_id=turn_id
                    )
                held_by_me = acquired.acquired
                if not acquired.acquired:
                    await queue.put(
                        TurnStreamEvent(
                            event="turn.queued",
                            data={
                                "turn_id": turn_id,
                                "state": TurnState.streaming.value,
                                "queue_position": acquired.queue_position,
                                "holder_window_id": acquired.holder_window_id,
                                "reason": acquired.reason,
                            },
                        )
                    )

            async with session_scope() as session:
                conversation = ConversationService(
                    session, actor_id=self._actor_id, persona_id=self._persona_id
                )
                # 先把状态推进到 streaming（= 服务端已受理）。
                # 上下文编译含记忆检索/关系/情绪等多条查询，真实模型场景可能耗时
                # 数百毫秒；这段时间如果仍是 pending，出现"已受理但看不到在生成"
                # 的错觉，停止信号也容易被误判为悬空轮次。
                await conversation.set_turn_state(turn_id, TurnState.streaming)
                await session.commit()

                if token.stopped:
                    # 编译前就被要求停止：直接收敛，不浪费模型调用
                    await self._finalize(
                        conversation=conversation,
                        window_id=window_id,
                        subject_id=subject_id,
                        turn_id=turn_id,
                        delivered="",
                        target_label=None,
                        stopped=True,
                        error=None,
                        queue=queue,
                        attempt=attempt,
                        user_text=user_text,
                        floor_held=held_by_me,
                        source_account=source_account,
                    )
                    return

                # 上下文只有一个来源：ContextCompiler。
                # 角色/约束/自我状态/关系/短记忆/证据/交接胶囊都由它按受众过滤后装配，
                # 避免"两套拼提示"导致记忆与关系永远进不了对话（也无法审计来源）。
                # 群窗口对所有成员开放（归属主人，但发言人是别人）；私聊仍严格校验
                window = await conversation.get_window_for_turn(
                    window_id, subject_id=subject_id
                )
                # **记忆策略随本轮配置快照一起进来**：`config` 是这一轮开始时
                # 取到的已发布配置，所以"发布后下一轮生效"是天然成立的——
                # 进行中的轮次继续用旧快照，不会被热改半路换掉。
                compiled = await ContextCompiler(
                    session,
                    actor_id=self._actor_id,
                    persona_id=self._persona_id,
                    memory_policy=config.memory,
                    # 嵌入/重排的绑定也随这一轮快照走：同一轮内的检索不该
                    # 因为别人在设置页换了模型而中途换掉向量空间。
                    model_config=config,
                    secret_reader=self._secret_reader,
                ).compile(
                    subject_id=subject_id,
                    window_id=window_id,
                    user_text=user_text,
                    is_group=window.visibility == "group_scope",
                    # **不要在这里给条数上限。** 这里原来是 `history_limit=20`：
                    # 那个值不是"上下文里放 20 条"，而是"一次性只从库里取 20 条"，
                    # 于是第 21 条往回的消息既不在原文里、也不在编译器的
                    # `dropped` 里，**永远不会被压进摘要**——长会话里会出现一段
                    # 谁都看不见的永久缺口（实测 31 条的窗口丢掉第 1~11 条）。
                    # 放多少由 `HISTORY_TOKEN_BUDGET` 决定，取数按页往回走，
                    # 见 `ContextCompiler.compile` 里"原文层"的装配。
                    # 请求路径已把本轮用户消息落库，这里必须排除以免重复输入
                    current_turn_id=turn_id,
                    # **当前这句**用本次称呼（群里 = 群名片优先）署名，而不是
                    # 库里那一列：`compile` 会优先用它，改名下一句就生效，
                    # 不必等 `identity.observe_display_name` 的旁路落库。
                    #
                    # 私聊不署名：那里只有一个说话人，前缀只是噪音；而且
                    # `compile` 的接口文档写明这个参数"群聊才有意义"
                    # （`is_group=False` 时它也不去库里补名字）。
                    user_sender_name=(
                        sender_name if window.visibility == "group_scope" else None
                    ),
                )
                messages = list(compiled.messages)

                # 权限硬门（设计文档 §1 第 7 条）：
                # 产出"回复"这个动作本身也要过裁决。正常对话一定通过，
                # 但群聊通道不支持群发、或受众规则不允许时必须在这里拦下。
                from ling.contracts.v1 import ActionIntent
                from ling.kernel.policy import PolicyGate, context_from_snapshot

                verdict = PolicyGate().evaluate(
                    ActionIntent(intent_id=f"reply:{turn_id}", kind="reply.text"),
                    context_from_snapshot(compiled.snapshot),
                )
                if not verdict.allowed:
                    logger.warning(
                        "轮次被权限硬门拦下 turn=%s: %s", turn_id, verdict.reasons
                    )
                    await self._finalize(
                        conversation=conversation,
                        window_id=window_id,
                        subject_id=subject_id,
                        turn_id=turn_id,
                        delivered="",
                        target_label=None,
                        stopped=False,
                        error=ModelError(
                            ModelErrorKind.unsupported,
                            "本轮不允许在此窗口回复：" + "；".join(verdict.reasons),
                            retriable=False,
                        ),
                        queue=queue,
                        attempt=attempt,
                        user_text=user_text,
                        floor_held=held_by_me,
                        source_account=source_account,
                    )
                    return

                if token.stopped:
                    # 编译期间被要求停止
                    await self._finalize(
                        conversation=conversation,
                        window_id=window_id,
                        subject_id=subject_id,
                        turn_id=turn_id,
                        delivered="",
                        target_label=None,
                        stopped=True,
                        error=None,
                        queue=queue,
                        attempt=attempt,
                        user_text=user_text,
                        floor_held=held_by_me,
                        source_account=source_account,
                    )
                    return

                # 标记可能在流式里被切开（``<<看视`` + ``频 m3>>``）。压住的
                # 只是"看起来是半个标记"的尾巴，其余照常立刻发——用户不该先看到
                # 说明书再看到它消失。
                stream_hold = ""

                # 「查看本次提示词」：在**调模型之前**把真正要发出去的那份
                # messages 存一份快照（独立事务、best-effort）。
                #
                # 位置刻意在权限硬门与停止检查**之后**：被拦下/被停止的轮次
                # 根本没有调用模型，也就不该留下"她看到过这段"的记录。
                await self._capture_prompt_snapshot(
                    window_id=window_id,
                    turn_id=turn_id,
                    compiled=compiled,
                    messages=messages,
                    config=config,
                    profile=profile,
                )

                def _visible(delta: str) -> str:
                    nonlocal stream_hold
                    stream_hold += delta
                    ready, stream_hold = stream_safe(stream_hold)
                    return strip_watch_markers(ready)

                try:
                    async for chunk, outcome in self._router.stream(
                        config, profile=profile, messages=messages
                    ):
                        if outcome.target is not None and target_label is None:
                            target_label = outcome.target.label
                        if token.stopped:
                            stopped = True
                            break
                        if not chunk.delta:
                            continue
                        delivered += chunk.delta
                        self._cancels.set_partial(turn_id, delivered)

                        # 没有发言权就不对外输出（注意：原先照样推 delta，
                        # 等于"排队"是假的）。文字仍累积在缓冲，取得占用后
                        # 作为补齐发送：不丢内容，也不会两轮交错。
                        #
                        # 续约失败（`lease_lost`）后**不再重新争取**：那意味着
                        # 这条租约已经被别人合法接管（过期后被接手/行被删），
                        # 再去抢只会让两个窗口轮流外发。本轮就此停止对外输出，
                        # 已生成的内容仍落库、可审计、可重放。
                        if not held_by_me and not floor_settled:
                            floor_settled = True
                            if self._arbiter is None and self._speak_lease is None:
                                held_by_me = True
                            elif await self._wait_for_floor(
                                window_id=window_id, turn_id=turn_id, token=token
                            ):
                                held_by_me = True
                                await queue.put(
                                    TurnStreamEvent(
                                        event="turn.speaking",
                                        data={
                                            "turn_id": turn_id,
                                            "state": TurnState.streaming.value,
                                            "caught_up": strip_watch_markers(delivered),
                                            "note": "排到发言权，补齐排队期间产生的文字",
                                        },
                                    )
                                )

                        if not held_by_me:
                            # 仍在排队：本轮不产生任何对外片段
                            continue

                        visible = _visible(chunk.delta)
                        if self._stream_log is not None:
                            self._stream_log.append(
                                turn_id=turn_id, attempt=attempt, text=visible
                            )
                        if self._speak_lease is not None:
                            renewed = await self._speak_lease.extend(  # type: ignore[attr-defined]
                                actor_id=self._actor_id,
                                window_id=window_id,
                                turn_id=turn_id,
                            )
                            if not renewed:
                                # 续约失败 = 这条租约已经不属于我了（被别人抢走、
                                # 过期被接管、或行被删）。**不能再装成还持有**：
                                # 原实现忽略返回值，于是继续吐 delta、`held_by_me`
                                # 仍为 True，前端看到两个窗口同时在说话；而且
                                # 此后 `release()` 也因为 window/turn 不匹配而失败，
                                # 这条租约没有任何回收路径，只能等 180s 过期。
                                #
                                # 处理：立刻交出持有标志并停止对外输出（文字继续
                                # 累积在 delivered，最终仍落库，只是不对外交付）。
                                # 若还拿得回来（只是被别人短暂改写又释放），下面
                                # 的 `_wait_for_floor` 会重新争取。
                                logger.warning(
                                    "发言租约续约失败，停止对外输出 turn=%s window=%s",
                                    turn_id,
                                    window_id,
                                )
                                held_by_me = False
                                floor_settled = True
                                lease_lost = True
                                continue
                        elif self._arbiter is not None and not self._arbiter.extend(
                            window_id=window_id, turn_id=turn_id
                        ):
                            # 嵌套 if 合成一个条件：语义完全相同（都是"有仲裁器且
                            # 续约失败"才停），但少一层缩进。CI 的 ruff 版本比本地
                            # 更严，SIM102 在这里报错会直接挡住发版。
                            logger.warning(
                                "发言占用续约失败，停止对外输出 turn=%s window=%s",
                                turn_id,
                                window_id,
                            )
                            held_by_me = False
                            floor_settled = True
                            lease_lost = True
                            continue
                        if not visible:
                            # 这一段全是标记（或半个标记）：不要往前端推空片段
                            continue
                        await queue.put(
                            TurnStreamEvent(
                                event="delta",
                                data={"text": visible, "turn_id": turn_id},
                            )
                        )
                except ModelError as exc:
                    error = exc

                # 出口清洗：剥掉 <<看视频 mX>>（幂等，再剥一次也不出错）。
                # 顺带丢掉"半个标记"的尾巴——模型被打断时会出现这种残片。
                #
                # 清洗前先记下**原始字数**：清洗之后正文可能是空的，而"模型
                # 一个字都没说"与"说了 37 个字、全是标记被洗空了"是两种完全
                # 不同的故障（前者查模型/上下文，后者查标记与出口清洗）。
                # 只报"没有可交付文本"会把两者混成一种，运维无从下手。
                raw_chars = len(delivered)
                delivered, watch_refs = parse_watch_markers(delivered)
                if watch_refs:
                    # 她这一轮想看一眼视频。**先读，再决定怎么收尾**：
                    # 读到后如果正文是空的，就让她接着说一句（见下面），
                    # 而不是先发一条空消息再补一条。
                    observation = await self._watch_videos(window_id, watch_refs)
                    if observation and not delivered.strip():
                        delivered = await self._follow_up_after_watch(
                            messages=messages,
                            observation=observation,
                            config=config,
                            profile=profile,
                            turn_id=turn_id,
                            token=token,
                            queue=queue,
                            held_by_me=held_by_me,
                        )
                    # 正文非空时：观察已经写进证据，下一轮上下文会自动带上
                    # （context.py 的描述块），**不再额外发一条消息**。

                await self._finalize(
                    conversation=conversation,
                    window_id=window_id,
                    subject_id=subject_id,
                    turn_id=turn_id,
                    delivered=delivered,
                    target_label=target_label,
                    stopped=stopped,
                    error=error,
                    queue=queue,
                    attempt=attempt,
                    user_text=user_text,
                    floor_held=held_by_me,
                    floor_lost=lease_lost,
                    source_account=source_account,
                    raw_chars=raw_chars,
                )
        except asyncio.CancelledError:
            # 进程关闭或超时强制取消：尽力落库
            try:
                async with session_scope() as session:
                    conversation = ConversationService(
                        session, actor_id=self._actor_id, persona_id=self._persona_id
                    )
                    await self._finalize(
                        conversation=conversation,
                        window_id=window_id,
                        subject_id=subject_id,
                        turn_id=turn_id,
                        delivered=delivered,
                        target_label=target_label,
                        stopped=True,
                        error=None,
                        queue=queue,
                        attempt=attempt,
                        user_text=user_text,
                        floor_held=held_by_me,
                        floor_lost=lease_lost,
                        source_account=source_account,
                    )
            except Exception:  # noqa: BLE001 - 关闭路径不掩盖原始取消
                pass
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都必须让前端看到
            await queue.put(
                TurnStreamEvent(
                    event="turn.failed",
                    data={
                        "turn_id": turn_id,
                        "state": TurnState.failed.value,
                        "text": delivered,
                        "error": {
                            "code": ErrorCode.internal.value,
                            "message": "服务端内部错误，请重试",
                            "retriable": True,
                            "detail": str(exc)[:300],
                        },
                    },
                )
            )
        finally:
            # 收尾：**持有与否都要回收**。
            #
            # - 持有 → `release()`：它释放租约并提升队首。返回 False 说明
            #   这条租约已经不属于我（被抢走/过期被接管/行已删）——那是
            #   "已经有人接管"的正常回收，但必须留下痕迹，否则排查"两个窗口
            #   同时说话"时看不到任何线索。
            # - 不持有 → `leave()`：`acquire()` 一定把我塞进了队列，而
            #   `_wait_for_floor()` 只在**等满 8 秒**时才离队。若本轮在等到
            #   第一个 delta 之前就失败/被停止（模型报错、权限硬门拦下），
            #   这条队列项没人清，下次 `release()` 就会把一条早已放弃的轮次
            #   提升为持有者 —— 队列只增不减，后面所有轮次被永久压住。
            if self._speak_lease is not None:
                if held_by_me:
                    released = await self._speak_lease.release(  # type: ignore[attr-defined]
                        actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                    )
                    if not released:
                        logger.warning(
                            "发言租约释放未命中（租约已被他人接管或已过期）"
                            " turn=%s window=%s lease_lost=%s",
                            turn_id,
                            window_id,
                            lease_lost,
                        )
                else:
                    # 先离队：`acquire()` 只要没拿到就一定把我塞进了队列。
                    with contextlib.suppress(Exception):
                        left = await self._speak_lease.leave(  # type: ignore[attr-defined]
                            actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                        )
                        if not left:
                            # 离队没命中 = 我不在队列里。可能的原因之一：
                            # `release()` 已经把队首提升成了我，而我从没察觉
                            # （`held_by_me` 仍是 False）。此时那条租约没有任何
                            # 人释放，只能等 180s 过期 —— 这里补一次释放作为
                            # 回收路径（`release()` 只在 window/turn 匹配时生效，
                            # 不是我的租约它什么也不做）。
                            recovered = await self._speak_lease.release(  # type: ignore[attr-defined]
                                actor_id=self._actor_id,
                                window_id=window_id,
                                turn_id=turn_id,
                            )
                            if recovered:
                                logger.warning(
                                    "回收一条未被察觉的提升租约 turn=%s window=%s",
                                    turn_id,
                                    window_id,
                                )
            elif self._arbiter is not None:
                # 进程内仲裁器的 `release()` 本来就同时清队列项，持不持有都可调
                self._arbiter.release(window_id=window_id, turn_id=turn_id)
            # 这一轮（这个 attempt）已经终结，它的停止标记没有读者了。
            # **只清自己这一行**：`stop → 立即 retry（旧轮次还没终结）` 时
            # 新旧两个 attempt 同时在跑，清掉旧 attempt 的行会让旧轮次
            # 永远停不下来（那正是方案 3 的坑）。
            await self._clear_stop_marker(turn_id, attempt)
            await queue.put(None)

    async def _capture_prompt_snapshot(
        self,
        *,
        window_id: str,
        turn_id: str,
        compiled: CompiledContext,
        messages: list[object],
        config: EffectiveConfig,
        profile: str,
    ) -> None:
        """存一份"这次回复看到的上下文"（best-effort，**绝不阻塞出话**）。

        任何异常都只记日志（带 ``reason_code``）并继续：快照是审计旁路，
        不是对话事实。它失败时读接口会如实报 ``unavailable``——"没存下来"
        与"存了但过期"是两件事，不能混。

        保存的是 ``compiled.annotated``（与 ``messages`` 同一份数据 + 语义块
        标签），长度对不上时退回 ``messages`` 本身并标成 ``other``：**宁可丢掉
        块标签，也不能把没发出去的东西记成她看到的**。
        """
        try:
            annotated = list(getattr(compiled, "annotated", None) or [])
            if len(annotated) != len(messages):
                from ling.kernel.context import CompiledMessage

                logger.info(
                    "提示词快照的来源与标注不一致，退回未标注版本 turn=%s "
                    "reason_code=prompt_snapshot_source_mismatch annotated=%d sent=%d",
                    turn_id,
                    len(annotated),
                    len(messages),
                )
                annotated = [
                    CompiledMessage(
                        role=str(getattr(m, "role", "") or ""),
                        content=str(getattr(m, "content", "") or ""),
                        key="other",
                        lane="other",
                    )
                    for m in messages
                ]
            payload, truncated = build_prompt_snapshot_payload(
                annotated, model=_snapshot_model_info(config, profile)
            )
            await save_prompt_snapshot(
                actor_id=self._actor_id,
                turn_id=turn_id,
                window_id=window_id,
                payload=payload,
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败绝不影响回复
            logger.warning(
                "提示词快照捕获失败（本轮照常回复） turn=%s reason_code=%s detail=%s",
                turn_id,
                "prompt_snapshot_capture_failed",
                f"{type(exc).__name__}: {exc}"[:200],
            )

    async def _bind_snapshot_message(
        self, conversation: ConversationService, *, turn_id: str, message_id: str
    ) -> None:
        """回复落库后把快照指到这条消息（同一事务，best-effort）。

        用 SAVEPOINT 而不是裸 UPDATE：这是**旁路**写入，它失败绝不能把
        这一轮的事务带脏（PostgreSQL 里一条失败的语句之后所有语句都会报
        ``current transaction is aborted``）——那会让"回复已经生成好了"
        变成"整轮失败"。
        """
        try:
            async with conversation.session.begin_nested():
                await conversation.session.execute(
                    update(PromptSnapshot)
                    .where(PromptSnapshot.turn_id == turn_id)
                    .values(message_id=message_id)
                )
        except Exception as exc:  # noqa: BLE001 - 回填失败只少一个入口
            logger.info(
                "提示词快照回填 message_id 失败（不影响回复） turn=%s "
                "reason_code=prompt_snapshot_bind_failed detail=%s",
                turn_id,
                f"{type(exc).__name__}: {exc}"[:200],
            )

    async def _clear_stop_marker(self, turn_id: str, attempt: int) -> None:
        """终结后清理本 attempt 的跨实例停止标记（尽力而为）。

        根因修复在存储层：信号按 (turn_id, attempt) 隔离，旧标记不会再命中
        新一轮。这里只是不让表无限堆积；失败不影响任何正确性。
        """
        control = self._turn_control
        clearer = getattr(control, "clear", None)
        if not callable(clearer):
            return
        with contextlib.suppress(Exception):
            await clearer(turn_id, attempt)

    async def _wait_for_floor(
        self, *, window_id: str, turn_id: str, token: StopToken
    ) -> bool:
        """真正要说话时才等发言权。

        只在**第一个 delta 到达**时调用：失败轮次没有 delta，就不该为
        仲裁白等 8 秒（前端会以为卡住）。有界等待，超时放弃对外流式。
        """
        for _ in range(ARBITER_WAIT_STEPS):
            if token.stopped:
                return False
            if self._speak_lease is not None:
                promoted = await self._speak_lease.try_promote(  # type: ignore[attr-defined]
                    actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                )
            elif self._arbiter is not None:
                promoted = self._arbiter.try_promote(window_id=window_id, turn_id=turn_id)
            else:
                return True
            if promoted:
                return True
            await asyncio.sleep(ARBITER_WAIT_SECONDS)
        # 等超时：必须离队，否则 release() 会把这条已放弃的记录提升为持有者，
        # 而它不会再来释放 —— 队列只增不减，后面所有轮次被永久压住。
        if self._speak_lease is not None:
            with contextlib.suppress(Exception):
                await self._speak_lease.leave(  # type: ignore[attr-defined]
                    actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                )
        return False

    async def _watch_videos(self, window_id: str, refs: list[str]) -> str | None:
        """按标记读视频，返回"她看到了什么"（失败/没预算 → ``None``）。

        失败**不抛**：看一眼视频失败不该把整轮做成失败（用户已经等到回复了）。
        具体原因由 ``media/watch.py`` 写成行为日志，这里只负责不让她假装看过。
        """
        callback = self._watch_video
        if callback is None:
            from ling.domains.media.watch import default_watch_callback

            callback = default_watch_callback(router=self._router, actor_id=self._actor_id)
            self._watch_video = callback  # 只装配一次
        try:
            return await callback(window_id, list(refs))
        except Exception as exc:  # noqa: BLE001 - 看视频失败不影响本轮
            logger.info("看视频回调失败（按没看成处理）：%s", type(exc).__name__)
            return None

    async def _follow_up_after_watch(
        self,
        *,
        messages: list[object],
        observation: str,
        config: EffectiveConfig,
        profile: str,
        turn_id: str,
        token: object,
        queue: asyncio.Queue[TurnStreamEvent | None],
        held_by_me: bool,
    ) -> str:
        """她这轮只写了标记（正文为空）时，**在同一轮里**再生成一次。

        ## 为什么不是"发一条空消息再补一条"

        用户看到的第一条会是空气泡（或者干脆什么都没有），第二条才是她
        真正想说的话——中间那段静默没法解释。同一轮里补一次生成，对外
        仍然只有一条回复，符合"她看了一眼才说话"的直觉。

        读失败/超预算时**根本不会走到这里**（调用方只有在 ``observation``
        非空时才调）：不能让她说"我看了"，也不能凭空编一句。
        """
        from ling.ports.model import ChatMessageIn

        observation_line = ChatMessageIn(
            role="user",
            content=(
                "（你刚看了一眼这个视频，看到的是：\n"
                f"{observation}\n"
                "现在用你自己的话接着说，不要提到这条提示，也不要复述标记。）"
            ),
        )
        text = ""
        hold = ""
        try:
            async for chunk, _outcome in self._router.stream(
                config,
                profile=profile,
                messages=[*messages, observation_line],  # type: ignore[list-item]
            ):
                if getattr(token, "stopped", False):
                    break
                if not chunk.delta:
                    continue
                text += chunk.delta
                hold += chunk.delta
                ready, hold = stream_safe(hold)
                visible = strip_watch_markers(ready)
                if visible and held_by_me:
                    await queue.put(
                        TurnStreamEvent(
                            event="delta",
                            data={"text": visible, "turn_id": turn_id},
                        )
                    )
        except ModelError as exc:
            # 补一次生成失败：正文仍然是空的 → 什么都不发（不是伪装成功）
            logger.info("看视频后的跟进生成失败：%s", exc.kind.value)
            return ""
        cleaned, _refs = parse_watch_markers(text)
        return cleaned

    async def _finalize(
        self,
        *,
        conversation: ConversationService,
        window_id: str,
        subject_id: str,
        turn_id: str,
        delivered: str,
        target_label: str | None,
        stopped: bool,
        error: ModelError | None,
        queue: asyncio.Queue[TurnStreamEvent | None],
        attempt: int = 1,
        user_text: str = "",
        floor_held: bool = True,
        # 曾经持有发言权、后来续约失败（租约被他人合法接管）。
        # 与"从未取得"是两件事，对外要给出不同的可审计原因。
        floor_lost: bool = False,
        source_account: str | None = None,
        # 出口清洗**之前**模型流出来的原始字数。用来区分「模型没说话」与
        # 「说了但被洗空（全是标记/半截标记）」——两者处置完全不同，
        # 只报"没有可交付文本"等于把它们混成一种。
        raw_chars: int = 0,
    ) -> None:
        if error is not None:
            state = TurnState.failed
            code: ErrorCode | None = ErrorCode(error.kind.value)
        elif stopped:
            state = TurnState.stopped
            code = None
        else:
            state = TurnState.completed
            code = None

        # 最后一道闸：**完整标记与半截标记**都不许进库/外发。
        #
        # 半截标记（``<<看视频 m3``）是模型被打断、或流式最后一段留下的残片：
        # 它是说明书的碎片，落库后历史渲染里会露出来，外发更糟。这里用的
        # `finalize_reply_text` = 剥完整标记 + 去半截尾巴，幂等，重复调用无害。
        delivered = finalize_reply_text(delivered)

        actor_message: MessageView | None = None
        # 表达层：先去掉客服式追问，再按语义分段。
        # 顺序不能反——先分段会把"还有什么可以帮你吗"单独切成一段，
        # 之后就不好判断它是尾句了。
        segments: list[str] = [delivered] if delivered else []
        expression = None
        if delivered:
            from ling.capabilities.chunking import plan_parts
            from ling.capabilities.followup import strip_service_tail

            cleaned, stripped = strip_service_tail(delivered)
            if stripped:
                delivered = cleaned

            # 她偶尔会照抄人设里"示例语气"的对话记录格式，给自己每条加
            # 「她：」前缀。这是格式残留，不是她的表达——剥掉。
            from ling.capabilities.naming import display_name_for, strip_self_prefix

            _name = self._display_name or await display_name_for(self._persona_id)
            _cleaned, _stripped = strip_self_prefix(delivered, _name)
            if _stripped:
                delivered = _cleaned
                logger.info("已剥离她自己加的名字前缀")
            # 她可能选了 @/引用/拍一拍。**标记要从正文里剥掉**，
            # 否则发出去的话里会残留"[at:123]"这种说明书。
            # 解析顺序在分段之前：标记不该参与分段边界判断。
            from ling.capabilities.expression_choice import parse_expression

            delivered, expression = parse_expression(delivered)
            # 用与渠道投递**同一套**分段规则：模型的换行就是她决定"分几条发"，
            # 不该被"短句合并"抹掉。以前这里用 split_semantic 算出一个
            # planned_chunks 只塞进事件里当装饰，消息仍然存成一条——
            # Web 上就永远看不到"连发"。
            segments = plan_parts(delivered, is_group=False)
        planned_chunks = max(1, len(segments))

        # 用户主动停止且尚无输出：也要留一条可见记录。
        # 否则前端会"消息凭空消失"，用户不知道自己是停成功了还是坏了
        # （设计文档 §2：沉默/失败都要能被解释）。
        if state is TurnState.stopped and not delivered:
            actor_message = await conversation.add_message(
                window_id,
                role="actor",
                text="",
                turn_id=turn_id,
                state=state,
                origin=Origin.model_inference,
                model_label=target_label,
                attempt=attempt,
            )
        if delivered:
            # 仍然**存一条**消息（含换行）。
            #
            # 试过"一段一条消息"，但那会连带改事件结构、前端气泡、历史计数，
            # 而且我当场撞上一个真 bug：`turn.completed` 的 message 只会是
            # 最后一段，QQ 那边就只发最后一句。改动面太大、收益不确定。
            # 分段意图在**真实渠道**（QQ）上由 `_deliver` 的 plan_parts 生效，
            # 那才是用户看到的地方；Web 上先是一个气泡里的多行。
            actor_message = await conversation.add_message(
                window_id,
                role="actor",
                text=delivered,
                turn_id=turn_id,
                state=state,
                origin=Origin.model_inference,
                error_code=code,
                model_label=target_label,
                attempt=attempt,
            )
        await conversation.set_turn_state(
            turn_id,
            state,
            error_code=code,
            error_detail=error.message if error else None,
            model_label=target_label,
        )
        # 回复已经落库 → 把提示词快照指到**这条**消息，前端才能从消息点开。
        # 停止但没正文那一轮也回填：那时消息正文是空的，"她为什么什么都没说"
        # 恰恰只有快照能解释（被注入的内容里可能就有原因）。
        if actor_message is not None:
            await self._bind_snapshot_message(
                conversation, turn_id=turn_id, message_id=actor_message.message_id
            )
        # **按实际结果校正**用户消息的处置结果。
        # 它是在消息创建时按"她的决定"写的（决定回 → replied），但决定回
        # 不等于真的回了——用户可能在生成到一半按了停止，那时一个字都没发。
        # 记意图却让外面读成结果，就是假的完整感。
        #
        # 「完成」也有两种：真的把她的话交出去了，以及**生成了却没交出去**
        # （发言权被另一个窗口占着 / 正文被标记洗空）。后者以前会落进第一个
        # 分支，于是库里写着 `replied`、对方一个字都没收到——最难查的组合。
        if state is TurnState.completed and delivered and floor_held:
            await conversation.correct_handled_for_turn(turn_id, outcome="replied")
        elif state is TurnState.completed and delivered:
            await conversation.correct_handled_for_turn(
                turn_id,
                outcome="failed",
                reason="另一个窗口正在说话，本轮内容没有对外交付",
            )
        elif state is TurnState.completed and not delivered:
            await conversation.correct_handled_for_turn(
                turn_id,
                outcome="failed",
                reason=(
                    f"正文被出口清洗洗空了（原始 {raw_chars} 字）"
                    if raw_chars
                    else "生成结束但没有任何正文"
                ),
            )
        elif state is TurnState.stopped and not delivered:
            await conversation.correct_handled_for_turn(
                turn_id, outcome="stopped", reason="被中断，没有生成内容"
            )
        elif state is TurnState.failed and not delivered:
            await conversation.correct_handled_for_turn(
                turn_id, outcome="failed", reason="生成失败，没有内容"
            )
        # 跨会话意识：从本轮用户输入派生"事实"条目（ADR-0004）。
        # 只在 continuous 模式生效；sealed/per_window 时 service 内部直接返回空。
        try:
            from ling.domains.awareness.service import AwarenessService

            window_row = await conversation.get_window_for_turn(
                window_id, subject_id=subject_id
            )
            await AwarenessService(conversation.session).derive_from_turn(
                subject_id=subject_id,
                window_id=window_id,
                window_visibility=window_row.visibility,
                user_text=user_text,
            )
        except Exception:  # noqa: BLE001 - 派生失败不影响已完成的回复
            pass

        # 记忆提取：在**同一事务**里登记 outbox 任务（不是同步调用模型）。
        # 失败只是"暂缓写长期记忆"，原始事件已落库，可重放（设计文档 §4.3）。
        if state is TurnState.completed and delivered:
            window_row = await conversation.get_window_for_turn(
                window_id, subject_id=subject_id
            )
            conversation.session.add(
                OutboxEvent(
                    id=new_id("obx"),
                    topic="relationship.update",
                    payload={
                        "subject_id": subject_id,
                        "window_id": window_id,
                        "turn_id": turn_id,
                        "user_text": user_text,
                        "attempt": attempt,
                        "source_account": source_account,
                    },
                )
            )
            conversation.session.add(
                OutboxEvent(
                    id=new_id("obx"),
                    topic="memory.extract",
                    payload={
                        "subject_id": subject_id,
                        "window_id": window_id,
                        "turn_id": turn_id,
                        "actor_text": delivered,
                        "user_text": user_text,
                        "source_visibility": window_row.visibility,
                        "attempt": attempt,
                        "source_account": source_account,
                    },
                )
            )
            await conversation.session.flush()

        sequencer = ActorSequencer(
            conversation_session(conversation), actor_id=self._actor_id, persona_id=self._persona_id
        )
        await sequencer.commit_event(
            type=f"turn.{state.value}",
            payload={
                "turn_id": turn_id,
                "window_id": window_id,
                "chars": len(delivered),
                "model": target_label,
                "error": code.value if code else None,
            },
            origin=Origin.model_inference,
            subject_id=subject_id,
            window_id=window_id,
            causation_id=turn_id,
        )

        if state is TurnState.failed and error is not None:
            await queue.put(
                TurnStreamEvent(
                    event="turn.failed",
                    data={
                        "turn_id": turn_id,
                        "state": state.value,
                        "text": delivered,
                        "model_label": target_label,
                        "error": {
                            "code": code.value if code else ErrorCode.internal.value,
                            "message": error.message,
                            "retriable": error.retriable,
                        },
                        "message": actor_message.to_contract().model_dump(mode="json")
                        if actor_message
                        else None,
                    },
                )
            )
            return

        event_name = "turn.stopped" if state is TurnState.stopped else "turn.completed"
        # 没有取得发言权就不能把正文交出去。
        # 此前实现的缺陷：原先 delta 被压住了，但最终 turn.completed 仍带
        # 完整 message —— 前端会把整段渲染出来，等于"排队"只挡了一半，
        # 用户照样同时看到两个窗口的回复。
        # 生成的内容仍落库（可审计、可重放），只是不对外交付。
        suppressed = bool(delivered) and not floor_held
        await queue.put(
            TurnStreamEvent(
                event=event_name,
                data={
                    "turn_id": turn_id,
                    "state": state.value,
                    "model_label": target_label,
                    # 每一轮都要能回答"为什么说/没说"（可审计）
                    "decision": "deferred_by_floor" if suppressed else "reply",
                    "chunks": planned_chunks,
                    # 清洗前的原始字数：调用方据此区分「模型没说话」与
                    # 「说了但被洗空」。**不记正文**——它可能正是被禁止外发的
                    # 标记残片，而且日志只需要一个数字。
                    "raw_chars": raw_chars,
                    # 她选了什么表达方式（@ / 引用 / 拍一拍）。
                    # 投递层据此真的做出来；正文里已经剥掉了标记。
                    "expression": (
                        {
                            "at": list(expression.at),
                            "quote": expression.quote,
                            "poke": list(expression.poke),
                        }
                        if expression is not None
                        else None
                    ),
                    "suppressed_reason": (
                        (
                            "本轮中途失去发言权（租约被另一个窗口接管），"
                            "后续内容不再对外交付"
                            if floor_lost
                            else "另一个窗口正在说话，本轮未取得发言权，内容未对外交付"
                        )
                        if suppressed
                        else None
                    ),
                    "message": (
                        None
                        if suppressed
                        else (
                            actor_message.to_contract().model_dump(mode="json")
                            if actor_message
                            else None
                        )
                    ),
                },
            )
        )


def describe_turn_failure(data: dict[str, Any] | None) -> str:
    """把 ``turn.failed`` 事件的 ``error`` 说成一句**能据以行动**的话。

    为什么必须有这个函数：`turn.failed` 的载荷有两条完全不同的来源，而
    两条路填的字段不一样——

    - ``ModelError`` 那条（`_finalize`，见本模块 §1050 附近）：填
      ``code / message / retriable``，**没有 ``detail``**；
    - 未捕获异常那条（`_execute` 的兜底 except）：填 ``code=internal``
      并把异常原文塞进 ``detail``。

    调用方（`bridge._run_turn`、`worker` 的群聊回应）原来只读 ``detail``。
    于是**最常见的那条路**（模型/供应商/额度/配置失败）在日志里只剩
    ``失败：turn.failed |``——冒号后面什么都没有。用户截图里那行
    「轮次 turn_… 失败: turn.failed」就是这么来的，运维因此无法区分
    「密钥失效」「模型名不存在」「今天额度用完」「这个职责没有可用模型」
    ——四种原因的处置完全不同。

    这里把三者拼成一句：``code=… message retriable=…``，且**绝不假装知道**
    （什么都没有时明确写"没有留下原因"）。

    ``retriable`` 是给"要不要重试"的判断依据，所以只要有就一并说出来：
    ``quota_exhausted`` / ``unauthorized`` / ``unsupported_capability`` 是
    **重试无用**（先去改配置或等明天），``provider_timeout`` / 熔断才是
    "过一会儿再试"。
    """
    error = (data or {}).get("error") or {}
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    detail = str(error.get("detail") or "").strip()
    retriable = error.get("retriable")
    parts: list[str] = []
    if code:
        parts.append(f"code={code}")
    if message:
        parts.append(message)
    if detail and detail != message:
        # 未捕获异常那条路：原文只在这里，丢了就等于没记
        parts.append(f"detail={detail}")
    if retriable is not None:
        parts.append(f"retriable={retriable}")
    return " ".join(parts) if parts else "没有留下原因（error 字段为空）"


def conversation_session(conversation: ConversationService) -> AsyncSession:
    """从 ConversationService 取回它自己的会话（避免重复传入）。"""
    return conversation.session


def message_contract(view: MessageView) -> ChatMessage:
    return view.to_contract()


async def drain_queue(queue: asyncio.Queue[Any]) -> None:
    """客户端断线后继续排空队列，保证轮次能跑完并落库。"""
    while True:
        item = await queue.get()
        if item is None:
            return


__all__ = [
    "ActorSequencer",
    "CancelRegistry",
    "StaleRevisionError",
    "StopToken",
    "TurnCoordinator",
    "TurnStreamEvent",
    "describe_turn_failure",
    "drain_queue",
    "message_contract",
]
