"""按需看视频的编排：预算闸 + 读 + 行为日志。

三个部分刻意分开：

- ``domains/media/video.py``：**机制**（标记解析、短号、抽帧、预算计数）；
- ``domains/media/describe.py``：**怎么问模型**（抽帧/原生两条路径 + 写回证据）；
- 本模块：**什么时候读、读没读成、为什么**——也就是行为日志要说的那些。

## 为什么"没看成"必须说清楚

用户问"那个视频里是什么"，她要么真的看了、要么没看。中间状态（想读但超预算/
取不到内容）如果只在代码里悄悄 return，用户看到的是"她答非所问"，而运维在
日志里也找不到原因。所以每一次尝试——成功、被预算拦、失败——都留一行
**人话**行为日志，且**不能让她以为自己看了**（失败时不产出任何"我看了"的
观察文本）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from ling.audit import behavior_event
from ling.domains.media.describe import MediaDescriber, MediaReadError
from ling.domains.media.service import (
    EvidenceView,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.domains.media.video import (
    WATCH_BUDGET,
    WatchBudget,
    ambiguous_refs,
    finalize_reply_text,
    resolve_watch_refs,
    short_ref_for,
)

logger = logging.getLogger("ling.domains.media.watch")

# 一次回复里最多看几条。她一次写五个标记也很可能是幻觉，读一条够判断了。
MAX_READS_PER_REPLY = 2


@dataclass
class WatchOutcome:
    """一次"看视频"的结果（供调用方决定是否跟进、供日志解释）。"""

    # 她"看到了什么"（一行行观察）。失败/没读时为空——调用方据此**不跟进**。
    observed: str = ""
    # 没看成的可解释原因（None = 没尝试或全部成功）
    skipped: str | None = None
    refs: list[str] = field(default_factory=list)
    reads: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.observed)


class VideoWatchService:
    """在**指定窗口**里按短号读视频。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str,
        describer: MediaDescriber,
        budget: WatchBudget = WATCH_BUDGET,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._describer = describer
        self._budget = budget
        self._service = MediaEvidenceService(session, actor_id=actor_id)

    async def videos_in_window(self, window_id: str, *, limit: int = 20) -> list[EvidenceView]:
        """窗口内所有视频（看过与没看过的都要）——短号解析要用全集。"""
        unseen = await self._service.undescribed_videos(window_id=window_id, limit=limit)
        seen = await self._service.described_in_window(window_id=window_id, limit=limit)
        videos = [item for item in (*unseen, *seen) if item.kind == "video"]
        # 去重（同一 id 同时出现在两个查询里的情况）
        unique: dict[str, EvidenceView] = {}
        for item in videos:
            unique.setdefault(item.evidence_id, item)
        return list(unique.values())

    async def watch(
        self, *, window_id: str, refs: list[str], mode: str = "frames"
    ) -> WatchOutcome:
        """按短号读视频。**永不抛**：所有失败都变成 ``skipped`` + 一行行为日志。"""
        outcome = WatchOutcome(refs=list(refs))
        videos = await self.videos_in_window(window_id)
        # 先看撞车：**撞车不读**（可能读错人），也不能"取第一条凑合"。
        collisions = ambiguous_refs(refs, videos)
        if collisions:
            outcome.skipped = f"短号撞车（{collisions[0]}），无法确定是哪一条"
            _log_skipped(collisions[0], "短号撞车，无法确定是哪一条视频")
            return outcome
        resolved = resolve_watch_refs(refs, videos)
        if not resolved:
            # 短号对不上：可能是幻觉、也可能是别的窗口的视频、或者已过期。
            # **静默退化**（不报错、不追问），只留一行日志便于排查。
            outcome.skipped = "短号不存在或已过期"
            _log_skipped(refs[0] if refs else "", "短号不存在或已过期")
            return outcome

        lines: list[str] = []
        for evidence in resolved[:MAX_READS_PER_REPLY]:
            ref = short_ref_for(evidence.evidence_id)
            if evidence.description:
                # 同一条证据只读一次：有描述就直接用，不再调模型。
                lines.append(f"- {ref}（之前看过的）：{evidence.description}")
                _log_viewed(ref, "上次已经看过，用已有描述", cached=True)
                continue

            denied = self._budget.deny_reason(window_id)
            if denied is not None:
                outcome.skipped = denied
                _log_skipped(ref, denied)
                break

            started = time.perf_counter()
            self._budget.record(window_id)
            try:
                read = await self._describer.describe_video(evidence, mode=mode)
            except (MediaReadError, MediaEvidenceError) as exc:
                outcome.skipped = str(exc)
                _log_skipped(ref, str(exc))
                break
            except Exception as exc:  # noqa: BLE001 - 读失败不能打断对话
                outcome.skipped = f"取不到视频内容（{type(exc).__name__}）"
                logger.info("看视频失败 ref=%s: %s", ref, type(exc).__name__)
                _log_skipped(ref, outcome.skipped)
                break

            elapsed = time.perf_counter() - started
            outcome.reads += 1
            lines.append(f"- {ref}：{read.text}")
            detail = _describe_read(read, elapsed)
            _log_viewed(ref, detail, cached=read.cached)
            if read.fallback_reason:
                logger.info("看视频退回抽帧 ref=%s: %s", ref, read.fallback_reason)

        if lines:
            outcome.observed = "\n".join(lines)
        return outcome


def _describe_read(read: object, elapsed: float) -> str:
    mode = getattr(read, "mode", "frames")
    frames = int(getattr(read, "frames", 0) or 0)
    if mode == "native":
        body = "原生视频"
    elif mode == "cached":
        body = "上次已经看过，用已有描述"
    else:
        body = f"抽帧 {frames} 帧" + ("（长视频，只看了等间隔抽取的一部分）"
                                     if getattr(read, "long_video", False) else "")
        fallback = getattr(read, "fallback_reason", None)
        if fallback:
            body += f"，原生模式没成功：{fallback}"
    return f"{body}，{elapsed:.1f} 秒"


def _log_viewed(ref: str, detail: str, *, cached: bool) -> None:
    behavior_event(
        "video_watched",
        f"看了 {ref}（{detail}）",
        attributes={"ref": ref, "cached": cached},
    )


def _log_skipped(ref: str, reason: str) -> None:
    label = ref or "视频"
    behavior_event(
        "video_watch_skipped",
        f"没看成 {label}：{reason}",
        attributes={"ref": ref, "reason": reason},
    )


# ---------------------------------------------------------------- 回调装配


WatchCallback = Callable[[str, list[str]], Awaitable[str | None]]


def make_watch_callback(
    *,
    fetch_media: Callable[[str], Awaitable[tuple[bytes, str]]],
    router: object,
    config_reader: object,
    actor_id: str,
    budget: WatchBudget = WATCH_BUDGET,
    mode_reader: Callable[[], Awaitable[str]] | None = None,
) -> WatchCallback:
    """把"读视频"包成 ``TurnCoordinator`` 能调的回调。

    回调签名 ``(window_id, refs) -> 观察文本 | None``：返回 ``None`` 表示
    没看成（调用方**不要**跟进，也不要假装看了）。

    每次调用**自己开一个数据库会话**：它可能比这一轮活得更久/更早失败，
    跟轮次共用一个事务会把"看一眼视频"的失败传染成"整轮失败"。
    """

    async def watch(window_id: str, refs: list[str]) -> str | None:
        from ling.infra.db import session_scope

        mode = await mode_reader() if mode_reader is not None else "frames"
        async with session_scope() as session:
            service = VideoWatchService(
                session,
                actor_id=actor_id,
                describer=MediaDescriber(  # type: ignore[arg-type]
                    service=MediaEvidenceService(session, actor_id=actor_id),
                    fetch_media=fetch_media,
                    router=router,
                    config_reader=config_reader,
                ),
                budget=budget,
            )
            outcome = await service.watch(window_id=window_id, refs=refs, mode=mode)
            await session.commit()
        return outcome.observed or None

    return watch


def default_watch_callback(*, router: object, actor_id: str) -> WatchCallback:
    """兜底装配：自己去找设置、连接器、媒体取用函数与偏好。

    `TurnCoordinator` 有两个构造点（Web 聊天路由与渠道入站），它们的上下文里
    都能拿到这些对象；但为了**不动那两个文件**（作用域限制），这里在真的
    要看视频时才惰性装配。装不起来就返回"没看成"，绝不影响正常对话。
    """
    cache: dict[str, object] = {}

    async def watch(window_id: str, refs: list[str]) -> str | None:
        try:
            from ling.capabilities.channels import build_onebot_connector
            from ling.config import get_settings
            from ling.infra.db import session_scope
            from ling.infra.models import Window
            from ling.infra.security import SecretBox
            from ling.settings.service import ConfigService
            from ling.settings.user_preferences import load_video_mode
            from ling.worker import make_media_fetcher

            settings = cache.get("settings") or get_settings()
            cache["settings"] = settings
            async with session_scope() as session:
                window = await session.get(Window, window_id)
                mode = await load_video_mode(
                    session, subject_id=getattr(window, "subject_id", None)
                )
                fetcher = cache.get("fetcher")
                if fetcher is None:
                    try:
                        connector = build_onebot_connector()
                    except Exception:  # noqa: BLE001 - 没有渠道时只能读本地文件
                        connector = None
                    fetcher = make_media_fetcher(
                        connector,
                        media_root=settings.data_dir / "media",
                        max_bytes=settings.max_body_bytes,
                    )
                    cache["fetcher"] = fetcher
                describer = MediaDescriber(
                    service=MediaEvidenceService(session, actor_id=actor_id),
                    fetch_media=fetcher,
                    router=router,
                    config_reader=ConfigService(
                        session,
                        SecretBox(settings),
                        test_mode_default=settings.test_mode,
                        persona_id=settings.persona_id,
                    ),
                )
                outcome = await VideoWatchService(
                    session, actor_id=actor_id, describer=describer
                ).watch(window_id=window_id, refs=refs, mode=mode)
                await session.commit()
            return outcome.observed or None
        except Exception as exc:  # noqa: BLE001 - 装配失败 = 没看成
            # **不能静默**：用户问"那个视频里是什么"却什么都没发生，原因
            # 必须能在行为日志里读出来（拿不到连接器/取不到媒体都走这里）。
            logger.info("看视频能力未就绪（%s）", type(exc).__name__)
            _log_skipped(
                refs[0] if refs else "", f"取不到视频内容（{_assembly_reason(exc)}）"
            )
            return None

    return watch


def _assembly_reason(exc: BaseException) -> str:
    """装配失败的一句话原因（给人看，不暴露内部细节）。"""
    name = type(exc).__name__
    if isinstance(exc, (ImportError, AttributeError)):
        return f"看视频能力未装配（{name}）"
    return f"看视频能力未就绪（{name}）"


def clean_reply_text(text: str) -> str:
    """出口统一用：剥掉完整标记与半截标记（幂等）。

    放在这里只是为了让调用方少一个 import；真正的规则在
    ``video.py::finalize_reply_text``——**一个出口一套规则**，各处自己写
    迟早出现"一处剥了半截、另一处没剥"。
    """
    return finalize_reply_text(text)


__all__ = [
    "MAX_READS_PER_REPLY",
    "VideoWatchService",
    "WatchCallback",
    "WatchOutcome",
    "clean_reply_text",
    "default_watch_callback",
    "make_watch_callback",
]
