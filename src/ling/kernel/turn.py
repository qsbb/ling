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
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import ChatMessage, ErrorCode, Origin, TurnState, new_id
from ling.domains.conversation.service import ConversationService, MessageView
from ling.infra.db import session_scope
from ling.infra.models import ActorEvent, ActorState, OutboxEvent, utcnow
from ling.kernel.arbiter import SpeakerArbiter
from ling.kernel.context import ContextCompiler
from ling.kernel.stream_log import StreamLog
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig
from ling.ports.model import ModelError, ModelErrorKind

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
        arbiter: SpeakerArbiter | None = None,
        stream_log: StreamLog | None = None,
        turn_control: object | None = None,
        speak_lease: object | None = None,
    ) -> None:
        self._router = router
        self._actor_id = actor_id
        self._persona_id = persona_id
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
            ),
            name=f"turn:{turn_id}",
        )
        self._cancels.register(turn_id, token, task)
        # 跨实例停止：轮询持久化信号，发现就设置本地令牌
        watcher: asyncio.Task | None = None
        control = self._turn_control
        if control is not None and hasattr(control, "watch"):
            watcher = asyncio.create_task(
                control.watch(turn_id, token),  # type: ignore[attr-defined]
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
    ) -> None:
        delivered = ""
        target_label: str | None = None
        stopped = False
        error: ModelError | None = None
        held_by_me = False
        # 仲裁只尝试一次：未取得就本轮不再等，避免每个 delta 都白等 8 秒
        floor_settled = False

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
                compiled = await ContextCompiler(
                    session, actor_id=self._actor_id, persona_id=self._persona_id
                ).compile(
                    subject_id=subject_id,
                    window_id=window_id,
                    user_text=user_text,
                    is_group=window.visibility == "group_scope",
                    history_limit=20,
                    # 请求路径已把本轮用户消息落库，这里必须排除以免重复输入
                    current_turn_id=turn_id,
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
                                            "caught_up": delivered,
                                            "note": "排到发言权，补齐排队期间产生的文字",
                                        },
                                    )
                                )

                        if not held_by_me:
                            # 仍在排队：本轮不产生任何对外片段
                            continue

                        if self._stream_log is not None:
                            self._stream_log.append(
                                turn_id=turn_id, attempt=attempt, text=chunk.delta
                            )
                        if self._speak_lease is not None:
                            await self._speak_lease.extend(  # type: ignore[attr-defined]
                                actor_id=self._actor_id,
                                window_id=window_id,
                                turn_id=turn_id,
                            )
                        elif self._arbiter is not None:
                            self._arbiter.extend(window_id=window_id, turn_id=turn_id)
                        await queue.put(
                            TurnStreamEvent(
                                event="delta",
                                data={"text": chunk.delta, "turn_id": turn_id},
                            )
                        )
                except ModelError as exc:
                    error = exc

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
                    source_account=source_account,
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
            if held_by_me:
                if self._speak_lease is not None:
                    await self._speak_lease.release(  # type: ignore[attr-defined]
                        actor_id=self._actor_id, window_id=window_id, turn_id=turn_id
                    )
                elif self._arbiter is not None:
                    self._arbiter.release(window_id=window_id, turn_id=turn_id)
            await queue.put(None)

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
        source_account: str | None = None,
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
        # **按实际结果校正**用户消息的处置结果。
        # 它是在消息创建时按"她的决定"写的（决定回 → replied），但决定回
        # 不等于真的回了——用户可能在生成到一半按了停止，那时一个字都没发。
        # 记意图却让外面读成结果，就是假的完整感。
        if state is TurnState.completed and delivered:
            await conversation.correct_handled_for_turn(turn_id, outcome="replied")
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
                        "另一个窗口正在说话，本轮未取得发言权，内容未对外交付"
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
    "drain_queue",
    "message_contract",
]
