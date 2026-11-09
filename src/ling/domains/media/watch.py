"""按需看视频的编排：额度闸 + 读 + 行为日志。

三个部分刻意分开：

- ``domains/media/video.py``：**机制**（标记解析、短号、抽帧）；
- ``domains/media/describe.py``：**怎么问模型**（抽帧/原生两条路径 + 写回证据）；
- 本模块：**什么时候读、读没读成、为什么**——也就是行为日志要说的那些。

## 为什么"没看成"必须说清楚

用户问"那个视频里是什么"，她要么真的看了、要么没看。中间状态（想读但额度用完/
取不到内容）如果只在代码里悄悄 return，用户看到的是"她答非所问"，而运维在
日志里也找不到原因。所以每一次尝试——成功、被额度拦、失败——都留一行
**人话**行为日志，且**不能让她以为自己看了**（失败时不产出任何"我看了"的
观察文本）。

## 没看成之后：把**事实**交给主回复链路

用户拍板：「看视频额度如果没了的话，直接把事实告诉主回复链路，让他进行按
人设自然的回复」。所以 :class:`WatchOutcome` 除了 ``observed``（看到了什么）
还有一个结构化的"为什么没看成"，调用方（`kernel/turn.py`）据此在同轮再生成
一次，并把下面这句话作为**事实**喂进去：

    你没能看这个视频（<原因>）。你可以用自己的方式回应，但**不要说你看过了**。

两个必须分开的措辞（用户原话把这条讲得很清楚）：

- **额度不足** → 偏"今天不想看了/有点累/改天吧"。这不是故障，是她今天真的
  没有余力去看——说成"打不开"就变成谎报技术故障了。
- **其他失败**（取不到内容/短号不存在/读取报错）→ 偏"看不了/打不开"。
  这是真的没读到，说成"不想看"会让用户以为她敷衍。

``WatchOutcome.denied_kind`` 就是给调用方分这两条用的。
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

from sqlalchemy.ext.asyncio import AsyncSession

from ling.audit import behavior_event
from ling.domains.media.describe import (
    VIDEO_PROFILE,
    MediaDescriber,
    MediaFetcher,
    MediaReadError,
)
from ling.domains.media.service import (
    EvidenceView,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.domains.media.video import (
    ambiguous_refs,
    finalize_reply_text,
    resolve_watch_refs,
    short_ref_for,
)

if TYPE_CHECKING:
    from ling.config import Settings

logger = logging.getLogger("ling.domains.media.watch")

# 一次回复里最多看几条。她一次写五个标记也很可能是幻觉，读一条够判断了。
MAX_READS_PER_REPLY = 2


#: 没看成的三类原因。调用方据此决定**怎么说**这件事。
#:
#: - ``quota``：今天的 token 额度用完了 → 说"今天不想看了/有点累/改天吧"。
#:   这不是故障，是她今天真的没有余力——说成"打不开"就是谎报技术故障。
#: - ``missing``：短号不存在/已过期/撞车 → 说"看不了/找不到"。
#: - ``error``：读取失败（下载失败、模型超时、抽帧失败…） → 说"打不开"。
#: - ``busy``：**她自己正在看/刷别的媒体**（全局唯一槽位被同一次执行之外的
#:   占用）。这不是故障，也不是没余力——是"我正忙着看点东西"。说成"打不开"
#:   是谎报技术故障，说成"今天不想看"是冒充她的意愿。
DENIED_QUOTA = "quota"
DENIED_MISSING = "missing"
DENIED_ERROR = "error"
DENIED_BUSY = "busy"

#: 额度拒绝的**人话**里必须带这几个数（已用/上限），否则用户与运维都看不出
#: "为什么今天不能看了"。文案在 `video_quota_denial` 里统一生成。
QUOTA_NOTE = "今天的看视频额度用完了（已用 {used} / 上限 {limit} tokens）"


@dataclass
class WatchOutcome:
    """一次"看视频"的结果（供调用方决定是否跟进、供日志解释）。"""

    # 她"看到了什么"（一行行观察）。失败/没读时为空——调用方据此**不跟进**。
    observed: str = ""
    # 没看成的可解释原因（None = 没尝试或全部成功）
    skipped: str | None = None
    #: 没看成属于哪一类（`quota` / `missing` / `error`）。``None`` = 看成了没失败。
    #:
    #: 为什么单独一个字段而不是让调用方去解析 ``skipped`` 的文案：文案是给人
    #: 看的，随时会改；而"额度不足"与"取不到内容"要说两句完全不同的话。
    #: 靠 `"额度" in skipped` 这种判断，改一次措辞就静默失效。
    denied_kind: str | None = None
    refs: list[str] = field(default_factory=list)
    reads: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.observed)

    def as_dict(self) -> dict[str, object]:
        """给跨层传的那份结构（见 ``WatchCallback``）。

        逐字段抄而不是 ``dataclasses.asdict``：调用方要的只是这四个键，
        多给的字段会变成没人维护的"顺手塞点东西"的通道。
        """
        return {
            "observed": self.observed,
            "skipped": self.skipped,
            "denied_kind": self.denied_kind,
            "reads": self.reads,
        }


async def video_quota_denial(
    *,
    config_reader: object,
    quota: object | None,
    profile: str,
) -> str | None:
    """看视频的额度还够不够？不够返回**人话**原因，够返回 ``None``。

    ## 查的是哪一份账

    **不自己维护计数器**（原来那套"每窗口每小时 2 次"已经删掉了）。查的是
    `models/quota.py::TokenQuotaStore` —— 按 (供应商, 本地日) 累计的真实 token，
    也就是 `router` 每次调用前自己会查的那一份。看视频走同一个 router、同一家
    供应商，所以两边**天然共用一份额度**。

    ## 为什么在这里提前查，而不是等 router 抛错

    router 会在调用时抛 `quota_exhausted`，那时摄像头已经抽过帧、图片已经
    编码好了——白烧一次 CPU。更重要的是：**这条路径要给用户一句人话**
    （"今天的看视频额度用完了（已用 X / 上限 Y）"），而 `ModelError` 的文案
    是给运维看的。提前查一次，就能把"为什么没看成"作为事实交给主回复链路。

    ## 判据与 router 完全一致

    ``limit = pcfg.daily_limit_effective()``；``limit is None`` = 没设限额 = 放行。
    这里**不另立一套规则**：两边判据不同的话，会出现"额度页说还有、
    看视频说没有"这种没人能解释的状态。

    ``quota is None``（没装配账本，例如单测）时视为**没设限额**：放行。
    这与 router 的行为一致（`_quota_block` 在 `self._quota is None` 时直接返回
    None），不是"偷偷放开"。
    """
    if quota is None:
        return None
    used_fn = getattr(quota, "used", None)
    if not callable(used_fn):
        return None
    effective = await config_reader.effective()  # type: ignore[attr-defined]
    resolved = effective.resolve_profile(profile)
    provider_id = getattr(resolved, "provider_id", None)
    if not provider_id:
        # 没有可用模型：交给调用方按"取不到内容"处理（这条不是额度问题）。
        return None
    pcfg = effective.providers.get(str(provider_id))
    if pcfg is None:
        return None
    limit = pcfg.daily_limit_effective()
    if limit is None:
        return None
    used = int(await used_fn(str(provider_id)))
    if used < limit:
        return None
    note = QUOTA_NOTE.format(used=used, limit=limit)
    behavior_event(
        "video_quota_exhausted",
        f"看视频额度已用完（{used}/{limit} tokens，供应商 {provider_id}）",
        attributes={
            "provider_id": str(provider_id),
            "used_tokens": used,
            "limit_tokens": limit,
            "profile": profile,
        },
    )
    return note


class VideoWatchService:
    """在**指定窗口**里按短号读视频。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str,
        describer: MediaDescriber,
        quota_denial: Callable[[], Awaitable[str | None]] | None = None,
        enforce_slot: bool = False,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._describer = describer
        #: 额度闸：返回人话原因表示"今天看不了"，``None`` 表示放行。
        #:
        #: 注入而不是在服务里直接查账：这个类不该知道"额度挂在哪家供应商上"
        #: （那是配置的事）。默认 ``None`` = 不设闸（单测/没装配账本的场景），
        #: 与 `router` 在 `quota is None` 时放行是同一个口径。
        self._quota_denial = quota_denial
        self._service = MediaEvidenceService(session, actor_id=actor_id)
        #: 当前持有的媒体槽位（一次 `watch()` 最多持有一个）。
        #:
        #: 放在实例上而不是局部变量：释放发生在 `finally` 里，而此时
        #: ``handle`` 还在 `try` 之前的作用域。这个类每次 `watch()` 都是
        #: 新实例（见 `default_watch_callback`），所以不担心跨调用串味。
        self._slot_handle: object | None = None
        #: 是否启用**全局媒体槽位**（跨进程单飞）。
        #:
        #: 默认 False 是为了让单元测试能直接构造本服务——它们传的是 mock
        #: session，跑不了 SQL。**生产路径必须打开**：由
        #: `default_watch_callback` 显式传 True，并有测试钉住这一点
        #: （`test_default_watch_callback_enforces_the_slot`）。
        #:
        #: 为什么用开关而不是"检测 session 是不是真的"：后者会让"忘了装配"
        #: 变成静默失效——媒体单飞没了，但一切看起来正常。
        self._enforce_slot = enforce_slot

    async def _enter_slot(self, window_id: str) -> str | None:
        """抢全局媒体槽位。返回 ``None`` 表示拿到了；返回字符串表示没拿到的人话原因。

        ## 为什么抢不到要如实说"我正忙着"

        用户原话：「当这个会话在看视频的时候，另一个会话可以说在忙」。
        所以 `busy` 既不是"打不开"（谎报故障）也不是"今天不想看"
        （冒充她的意愿），而是第三种事实：她此刻确实在看别的东西。

        ## 数据库不可用时不假装"忙"

        `unavailable` 意味着**确认不了**有没有人在用。这时按 fail closed
        处理——不开始媒体动作——但措辞不能是"我正忙着"（那是在编造她的活动）。
        所以两种情况给两句话。
        """
        # `getattr` 兜底：本类有几个测试用 `__new__` 手工装配属性绕过
        # `__init__`（它们只想测"读一次/用缓存"的分支，不需要会话）。
        # 直接访问会 AttributeError，把"没装配"变成"看视频整体损坏"——
        # 一个可选特性不该有这种耦合。
        if not getattr(self, "_enforce_slot", False):
            return None
        from ling.domains.media import slot as slot_mod

        try:
            outcome = await slot_mod.acquire(
                self._session,
                kind=slot_mod.KIND_VIDEO_WATCH,
                window_id=window_id,
                actor_id=self._actor_id,
            )
            await self._session.commit()
        except Exception as exc:  # noqa: BLE001 - 槽位故障不能打断对话
            logger.info("媒体槽位抢占异常：%s", type(exc).__name__)
            return "现在没法确认能不能看这个视频（稍后再试）"

        if outcome.acquired:
            self._slot_handle = outcome.handle
            # 拿到执行权 ≠ 已经在看：还要走下载/抽帧。先标 active 再往下，
            # 因为接下来那一步就是真正的耗时动作。
            if outcome.handle is not None:
                with contextlib.suppress(Exception):
                    await slot_mod.mark_active(self._session, outcome.handle)
                    await self._session.commit()
            return None

        if outcome.busy:
            return "她这会儿正在看别的视频"
        return "现在没法确认能不能看这个视频（稍后再试）"

    async def _leave_slot(self) -> None:
        """放掉媒体槽位。**幂等**，失败只记日志（不能让释放拖垮对话）。"""
        from ling.domains.media import slot as slot_mod

        # 同 `_enter_slot`：`__new__` 装配的测试实例可能没有这个属性。
        handle = getattr(self, "_slot_handle", None)
        self._slot_handle = None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            await slot_mod.release(self._session, handle)  # type: ignore[arg-type]
            await self._session.commit()

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
        # video.py's list[object] signatures preserve these EvidenceView instances.
        video_refs = cast(list[object], videos)
        collisions = ambiguous_refs(refs, video_refs)
        if collisions:
            outcome.skipped = f"短号撞车（{collisions[0]}），无法确定是哪一条"
            outcome.denied_kind = DENIED_MISSING
            _log_skipped(collisions[0], "短号撞车，无法确定是哪一条视频")
            return outcome
        resolved = cast(list[EvidenceView], resolve_watch_refs(refs, video_refs))
        if not resolved:
            # 短号对不上：可能是幻觉、也可能是别的窗口的视频、或者已过期。
            # **静默退化**（不报错、不追问），只留一行日志便于排查。
            outcome.skipped = "短号不存在或已过期"
            outcome.denied_kind = DENIED_MISSING
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

            denied = (
                await self._quota_denial() if self._quota_denial is not None else None
            )
            if denied is not None:
                # 额度不足是**最温和**的一种"没看成"：不是视频坏了、也不是她
                # 敷衍，是今天真的没有余力。`denied_kind` 让调用方按这条去说
                # "今天不想看了"，而不是"打不开"（后者是谎报技术故障）。
                outcome.skipped = denied
                outcome.denied_kind = DENIED_QUOTA
                _log_skipped(ref, denied)
                break

            # 真正耗时的动作（下载/抽帧/调视觉模型）——**这一段要在全局槽位
            # 里跑**。用户原话：「看视频的线程只允许存在一个全局，当这个会话
            # 在看视频的时候，另一个会话可以说在忙」。
            #
            # 只保护这里、不保护整轮，是因为解析短号、看缓存描述都不耗时；
            # 把整轮都塞进槽位会让"她正在说话"也占住媒体执行权。
            slot_denial = await self._enter_slot(window_id)
            if slot_denial is not None:
                outcome.skipped = slot_denial
                outcome.denied_kind = DENIED_BUSY
                _log_skipped(ref, slot_denial)
                break

            started = time.perf_counter()
            try:
                read = await self._describer.describe_video(evidence, mode=mode)
            except (MediaReadError, MediaEvidenceError) as exc:
                outcome.skipped = str(exc)
                outcome.denied_kind = DENIED_ERROR
                _log_skipped(ref, str(exc))
                break
            except Exception as exc:  # noqa: BLE001 - 读失败不能打断对话
                outcome.skipped = f"取不到视频内容（{type(exc).__name__}）"
                outcome.denied_kind = DENIED_ERROR
                logger.info("看视频失败 ref=%s: %s", ref, type(exc).__name__)
                _log_skipped(ref, outcome.skipped)
                break

            finally:
                # 无论成功、失败、超时都要放掉槽位，否则一次异常就让媒体链路
                # 永久"忙"——这是这类单飞设计最常见的死法。
                await self._leave_slot()

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


#: 回调返回的**结构化**结果（见 `WatchOutcome.as_dict`）。
#:
#: 为什么不是 ``str | None``：``None`` 把"她不想看（额度用完）"与"视频打不开"
#: 压成同一个值，调用方就无从区分该说"今天有点累"还是"这个打不开"。用户原话
#: 明确要前者，而它俩是两句话。
WatchResult = dict[str, object]
WatchCallback = Callable[[str, list[str]], Awaitable[WatchResult]]


def _quota_gate(config_reader: object, quota: object | None, profile: str):  # noqa: ANN202
    """把额度查询包成 ``VideoWatchService`` 要的那个零参回调。"""

    async def _check() -> str | None:
        return await video_quota_denial(
            config_reader=config_reader, quota=quota, profile=profile
        )

    return _check


def make_watch_callback(
    *,
    fetch_media: Callable[[str], Awaitable[tuple[bytes, str]]],
    router: object,
    config_reader: object,
    actor_id: str,
    quota: object | None = None,
    profile: str = VIDEO_PROFILE,
    mode_reader: Callable[[], Awaitable[str]] | None = None,
) -> WatchCallback:
    """把"读视频"包成 ``TurnCoordinator`` 能调的回调。

    返回结构化结果 ``{observed, skipped, denied_kind, reads}``：

    - ``observed`` 非空 = 真的看到了（调用方可以直接用它）；
    - ``observed`` 空 + ``skipped`` 非空 = 没看成，``denied_kind`` 说明是哪一类。

    每次调用**自己开一个数据库会话**：它可能比这一轮活得更久/更早失败，
    跟轮次共用一个事务会把"看一眼视频"的失败传染成"整轮失败"。
    """

    async def watch(window_id: str, refs: list[str]) -> WatchResult:
        from ling.infra.db import session_scope

        mode = await mode_reader() if mode_reader is not None else "frames"
        async with session_scope() as session:
            service = VideoWatchService(
                session,
                actor_id=actor_id,
                describer=MediaDescriber(
                    service=MediaEvidenceService(session, actor_id=actor_id),
                    fetch_media=fetch_media,
                    router=router,
                    config_reader=config_reader,
                    # **显式指定视频职责**（用户原话：「直接使用设定的看视频模型」）。
                    # 不按 kind 自动切：图片走 perception.vision，视频走
                    # perception.video，两者可以绑不同的供应商。
                    profile=profile,
                ),
                quota_denial=_quota_gate(config_reader, quota, profile),
                # **生产路径必须打开全局媒体槽位**。用户原话：「看视频的线程
                # 只允许存在一个全局，当这个会话在看视频的时候，另一个会话
                # 可以说在忙」。这里不打开就等于没有单飞，而且不会有任何报错。
                enforce_slot=True,
            )
            outcome = await service.watch(window_id=window_id, refs=refs, mode=mode)
            await session.commit()
        return outcome.as_dict()

    return watch


class _WatchCache(TypedDict, total=False):
    settings: Settings
    fetcher: MediaFetcher


def default_watch_callback(
    *,
    router: object,
    actor_id: str,
    quota: object | None = None,
    profile: str = VIDEO_PROFILE,
) -> WatchCallback:
    """兜底装配：自己去找设置、连接器、媒体取用函数与偏好。

    `TurnCoordinator` 有两个构造点（Web 聊天路由与渠道入站），它们的上下文里
    都能拿到这些对象；但为了**不动那两个文件**（作用域限制），这里在真的
    要看视频时才惰性装配。装不起来就返回"没看成"，绝不影响正常对话。

    ``quota``：`TokenQuotaStore`。给了就按 token 账本判额度；``None`` = 不设闸
    （与 router 在没装配账本时放行是同一个口径）。
    """
    cache: _WatchCache = {}

    async def watch(window_id: str, refs: list[str]) -> WatchResult:
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
                config_reader = ConfigService(
                    session,
                    SecretBox(settings),
                    test_mode_default=settings.test_mode,
                    persona_id=settings.persona_id,
                )
                describer = MediaDescriber(
                    service=MediaEvidenceService(session, actor_id=actor_id),
                    fetch_media=fetcher,
                    router=router,
                    config_reader=config_reader,
                    profile=profile,
                )
                outcome = await VideoWatchService(
                    session,
                    actor_id=actor_id,
                    describer=describer,
                    quota_denial=_quota_gate(config_reader, quota, profile),
                ).watch(window_id=window_id, refs=refs, mode=mode)
                await session.commit()
            return outcome.as_dict()
        except Exception as exc:  # noqa: BLE001 - 装配失败 = 没看成
            # **不能静默**：用户问"那个视频里是什么"却什么都没发生，原因
            # 必须能在行为日志里读出来（拿不到连接器/取不到媒体都走这里）。
            logger.info("看视频能力未就绪（%s）", type(exc).__name__)
            reason = f"取不到视频内容（{_assembly_reason(exc)}）"
            _log_skipped(refs[0] if refs else "", reason)
            # 装配失败属于**取不到内容**，不是额度问题——要说"打不开"。
            return {
                "observed": "",
                "skipped": reason,
                "denied_kind": DENIED_ERROR,
                "reads": 0,
            }

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
    "DENIED_ERROR",
    "DENIED_MISSING",
    "DENIED_QUOTA",
    "MAX_READS_PER_REPLY",
    "QUOTA_NOTE",
    "VideoWatchService",
    "WatchCallback",
    "WatchOutcome",
    "WatchResult",
    "clean_reply_text",
    "default_watch_callback",
    "make_watch_callback",
    "video_quota_denial",
]
