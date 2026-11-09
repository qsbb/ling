"""媒体注意力唯一槽位：全系统同时只允许一个耗时媒体动作。

设计见 `docs/MEDIA-EXCLUSIVE-SLOT.md`。本模块是它的实现。

## 它解决什么问题

用户原话：「看视频的线程只允许存在一个全局，当这个会话在看视频的时候，
另一个会话可以说在忙」「与刷 B 站共用」。

没有槽位时，两个窗口的轮次可以**同时**下载/抽帧/调视觉模型。后果不只是多花
钱：她的"我正在看点东西"会同时出现在两个窗口，而两边的用户会读到互相矛盾的
"她此刻在做什么"。**人格一致性**在这里比成本更重要。

## 为什么是三段式而不是一把锁

`acquire` → 干耗时活 → `release`，中间**不持有数据库事务**。看一个视频要几十秒
到几分钟，把事务开着会占住连接、拖长行锁，还会让连接池耗尽。

代价是必须自己处理崩溃：进程死在中间时槽位不会自动释放。所以有**租约**
（`lease_until`）——到期即可被别人接手，不需要重放任何日志。

## 为什么还要代次（generation）

租约解决"别永久占住"，解决不了"旧持有者晚到写库"：

    A 卡住 4 分钟 → 租约过期 → B 接手 → A 醒过来把结果写进库

没有代次时，A 的写入会覆盖 B 的。所以每次占用 `generation + 1`，所有持久副作用
都必须带 `WHERE generation = <我的代次>`；对不上就是 0 行，整笔作废。
`fence_ok()` 就是这个检查。

## 抢不到时返回什么

**立即返回 `busy`，不排队**。理由见设计 §1 第 3 条：排队会让"她什么时候能看到"
变成不可预测的等待，而用户问的是"你现在怎么不理我"。让她此刻如实说"我正忙着
看点东西"比排队更符合一个真实的人。

`unavailable`（数据库异常、没有预置行）**一律 fail closed**：不开始任何媒体
动作。把"数据库坏了"当成"别人占着槽"是在编造她的活动；当成"拿到了"则会破坏
全局单飞。两条都错，所以宁可这一次看不了。

## 这个模块不做什么

- **不判断该不该看**：那是她自己的决定（写不写 `<<看视频 mX>>` 标记），
  槽位只管"能不能同时看两个"。
- **不重试**：抢不到就是抢不到，重试会把"立即返回 busy"变成"偷偷等一会"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("ling.domains.media.slot")

#: 全系统唯一的资源键。将来加刷流/直播也只是换 `kind`，不新增槽位。
RESOURCE_KEY = "media.attention"

#: 租约时长（秒）。设计初值 240：足够覆盖一次观看（硬截止 180 秒）加上
#: 收尾与网络抖动。租约必须**大于**执行硬截止，否则正常的慢观看会被
#: 别人抢走，出现两个执行者。
DEFAULT_LEASE_SECONDS = 240

#: 一次观看的执行硬截止（秒）。到点必须停止取件/取消模型请求再收尾。
EXECUTION_HARD_CUTOFF_SECONDS = 180

#: 槽位种类。三者互斥，共争同一行。
KIND_VIDEO_WATCH = "video_watch"
KIND_BILIBILI_FEED = "bilibili_feed"
KIND_LIVESTREAM = "livestream"
VALID_KINDS = frozenset({KIND_VIDEO_WATCH, KIND_BILIBILI_FEED, KIND_LIVESTREAM})

#: 抢占结果。
OUTCOME_ACQUIRED = "acquired"
OUTCOME_BUSY = "busy"
OUTCOME_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SlotHandle:
    """一次占用的凭据。**每次持久副作用都要带上它**。"""

    holder_id: str
    generation: int
    kind: str
    window_id: str
    lease_until: datetime | None = None

    @property
    def acquired(self) -> bool:
        return True


@dataclass(frozen=True)
class SlotOutcome:
    """抢占结果。

    ``holder_window_id`` 只有确认了**有效持有者**时才非空。`busy` 但查不到
    持有者（竞争窗口太短）时它保持 None——不能为了让"忙"更好看而编一个窗口。
    """

    outcome: str
    handle: SlotHandle | None = None
    holder_window_id: str | None = None
    holder_phase: str | None = None
    detail: str = ""

    @property
    def acquired(self) -> bool:
        return self.outcome == OUTCOME_ACQUIRED

    @property
    def busy(self) -> bool:
        return self.outcome == OUTCOME_BUSY

    @property
    def unavailable(self) -> bool:
        return self.outcome == OUTCOME_UNAVAILABLE


def _is_lock_conflict(exc: BaseException) -> bool:
    """这个异常是不是"行锁暂时拿不到"（而不是真的连不上库）。

    必须顺着异常链找：SQLAlchemy 把驱动异常包成 `DBAPIError`，最外层的类型名
    永远是它。真实类型在 `.orig`（SQLAlchemy 约定）或 `__cause__` / `__context__`
    上。asyncpg 抛 `LockNotAvailableError`，文本里带
    ``could not obtain lock``。
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        name = type(node).__name__.lower()
        text_ = str(node).lower()
        if (
            "locknotavailable" in name
            or "lock_not_available" in text_
            or "could not obtain lock" in text_
            # PostgreSQL 的行锁竞争在别的驱动上可能叫这个
            or ("deadlock" in name and "lock" in name)
        ):
            return True
        node = (
            getattr(node, "orig", None)
            or node.__cause__
            or node.__context__
        )
    return False


async def acquire(
    session: AsyncSession,
    *,
    kind: str,
    window_id: str,
    actor_id: str = "",
    subject_id: str = "",
    call_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> SlotOutcome:
    """尝试抢占媒体槽位。**立即返回**，不等待。

    实现要点（对应设计 §3 第 14–15 条）：

    1. `FOR UPDATE NOWAIT` 锁那一行。行暂被别的短事务锁住时立刻报错 ——
       这里是"抢占瞬时竞争"，不是"有人在看"，所以返回 `busy` 但**不带**
       持有者信息（查不到就不说）。
    2. 只有空槽或租约已过期才写入新持有者；否则 `busy`。
    3. 时间全部用数据库的 `clock_timestamp()`，不用应用时钟。

    调用方必须自己提交这个事务。本函数只做占用，不做业务。
    """
    if kind not in VALID_KINDS:
        # 拼错的 kind 不该悄悄占住槽位——那样一个笔误会让整个媒体链路
        # 永久"忙"。宁可在这里炸。
        raise ValueError(f"未知的媒体槽位类型：{kind!r}")

    from ling.infra.security import new_id

    holder_id = new_id("mhold")
    try:
        row = (
            await session.execute(
                text(
                    """
                    SELECT holder_id, lease_until, generation, phase, window_id,
                           clock_timestamp() AS now
                      FROM media_slots
                     WHERE resource_key = :key
                     FOR UPDATE NOWAIT
                    """
                ),
                {"key": RESOURCE_KEY},
            )
        ).first()
    except Exception as exc:  # noqa: BLE001 - 行锁竞争或连接问题都要分类
        # NOWAIT 拿不到锁会抛 LockNotAvailable。它与"连不上库"必须区分：
        # 前者说明有人正在改这一行（瞬时，可以当作忙），后者是真的不可用。
        # SQLAlchemy 会把驱动异常包成 `DBAPIError`，所以 `type(exc).__name__`
        # 永远是 `DBAPIError` —— 只看最外层类型会把"有人正在同时抢"误报成
        # "数据库不可用"。两者对上层完全不同：前者可以如实说"我正忙着"，
        # 后者只能说"这事现在确认不了"。
        #
        # 真实类型在 `.orig`（SQLAlchemy 的约定）与 `__cause__` 链上
        # （asyncpg 抛的是 `LockNotAvailableError`）。
        if _is_lock_conflict(exc):
            return SlotOutcome(
                outcome=OUTCOME_BUSY,
                detail="另一个请求正在抢占媒体槽位",
            )
        logger.warning("媒体槽位不可用：%s", type(exc).__name__)
        return SlotOutcome(
            outcome=OUTCOME_UNAVAILABLE,
            detail=f"无法确认媒体槽位（{type(exc).__name__}）",
        )

    if row is None:
        # 没有预置行。**不能**只靠迁移保证它存在：
        # `create_all()`（开发/测试建库、以及任何不走 Alembic 的路径）只建表，
        # 不会执行迁移里的 `INSERT`。那样媒体链路会永久返回 unavailable，
        # 而且日志只说"未初始化"，排查的人要翻到迁移文件才知道缺什么。
        #
        # 所以在这里**补一行**再重试一次。`ON CONFLICT DO NOTHING` 保证并发
        # 补行不会打架。补完仍然读不到才是真的坏了（表结构不对）。
        await session.execute(
            text(
                """
                INSERT INTO media_slots (resource_key, holder_id, phase, kind,
                                         actor_id, window_id, subject_id, generation)
                VALUES (:key, NULL, 'held', '', '', '', '', 0)
                ON CONFLICT (resource_key) DO NOTHING
                """
            ),
            {"key": RESOURCE_KEY},
        )
        await session.flush()
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT holder_id, lease_until, generation, phase, window_id,
                               clock_timestamp() AS now
                          FROM media_slots
                         WHERE resource_key = :key
                         FOR UPDATE NOWAIT
                        """
                    ),
                    {"key": RESOURCE_KEY},
                )
            ).first()
        except Exception as exc:  # noqa: BLE001 - 与首次读取同一套分类
            # **补行后的重试同样会撞上行锁**：20 个并发补行时，第一个持锁
            # 提交前，其余的 NOWAIT 必然失败。漏掉这里会让并发抢占直接抛
            # 异常出去（表现为"看视频炸了"），而不是干净地返回 busy。
            if _is_lock_conflict(exc):
                return SlotOutcome(
                    outcome=OUTCOME_BUSY,
                    detail="另一个请求正在抢占媒体槽位",
                )
            logger.warning("媒体槽位补行后不可用：%s", type(exc).__name__)
            return SlotOutcome(
                outcome=OUTCOME_UNAVAILABLE,
                detail=f"无法确认媒体槽位（{type(exc).__name__}）",
            )
        if row is None:  # pragma: no cover - 表结构不对才会到这
            logger.error("media_slots 补建预置行后仍读不到，媒体动作拒绝执行")
            return SlotOutcome(
                outcome=OUTCOME_UNAVAILABLE,
                detail="媒体槽位未初始化",
            )

    now = row.now
    holder_id_now = row.holder_id
    lease_until = row.lease_until
    free = holder_id_now is None or (
        lease_until is not None and lease_until <= now
    )

    if not free:
        # 有人持有且租约有效。把持有者信息带回去，让上层能说"我忙着"——
        # 但**不透露**对方窗口的细节（那是另一个人的会话）。
        return SlotOutcome(
            outcome=OUTCOME_BUSY,
            holder_window_id=str(row.window_id) if row.window_id else None,
            holder_phase=str(row.phase) if row.phase else None,
            detail="她此刻正在处理别的媒体",
        )

    updated = (
        await session.execute(
            text(
                """
                UPDATE media_slots
                   SET holder_id = :holder,
                       phase = 'held',
                       kind = :kind,
                       actor_id = :actor,
                       window_id = :window,
                       subject_id = :subject,
                       call_id = :call_id,
                       activity_id = NULL,
                       started_at = clock_timestamp(),
                       lease_until = clock_timestamp() + (:secs || ' seconds')::interval,
                       generation = generation + 1
                 WHERE resource_key = :key
                 RETURNING generation, lease_until
                """
            ),
            {
                "holder": holder_id,
                "kind": kind,
                "actor": actor_id,
                "window": window_id,
                "subject": subject_id,
                "call_id": call_id,
                "secs": str(int(lease_seconds)),
                "key": RESOURCE_KEY,
            },
        )
    ).first()

    if updated is None:  # pragma: no cover - 行一定存在（上面刚读到）
        return SlotOutcome(
            outcome=OUTCOME_UNAVAILABLE, detail="抢占后读不到槽位"
        )

    return SlotOutcome(
        outcome=OUTCOME_ACQUIRED,
        handle=SlotHandle(
            holder_id=holder_id,
            generation=int(updated.generation),
            kind=kind,
            window_id=window_id,
            lease_until=updated.lease_until,
        ),
    )


async def mark_active(session: AsyncSession, handle: SlotHandle) -> bool:
    """把占用从 `held` 推进到 `active`：耗时动作**真的开始了**。

    为什么需要这一步：拿到执行权不等于已经在看（还要解析短号、看缓存、
    准备下载）。只有 `active` 才能对外表达"她正在看东西"——把 `held` 也当
    成在忙，就是编造她的状态。

    代次不符（被抢走了）时返回 False，调用方应放弃后续动作。
    """
    result = await session.execute(
        text(
            """
            UPDATE media_slots
               SET phase = 'active'
             WHERE resource_key = :key
               AND holder_id = :holder
               AND generation = :gen
               AND lease_until > clock_timestamp()
            """
        ),
        {"key": RESOURCE_KEY, "holder": handle.holder_id, "gen": handle.generation},
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def renew(
    session: AsyncSession,
    handle: SlotHandle,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """续租。返回 False 表示**已经失去槽位**，调用方必须立刻停手。

    失败即失权：更新行数为 0 说明持有者/代次/租约有一项对不上——通常是被
    别人接手了。继续干活写出来的东西会成为别人那一轮的副作用。
    """
    result = await session.execute(
        text(
            """
            UPDATE media_slots
               SET lease_until = clock_timestamp() + (:secs || ' seconds')::interval
             WHERE resource_key = :key
               AND holder_id = :holder
               AND generation = :gen
               AND lease_until > clock_timestamp()
            """
        ),
        {
            "key": RESOURCE_KEY,
            "holder": handle.holder_id,
            "gen": handle.generation,
            "secs": str(int(lease_seconds)),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def release(session: AsyncSession, handle: SlotHandle) -> bool:
    """释放槽位。只有**当前持有者**能释放（代次不符则不生效）。

    为什么带代次：旧持有者的 `finally` 里会调它。如果它已经失权、槽位被
    别人拿走，这次释放必须落空——否则它会把**新持有者**踢掉。
    """
    result = await session.execute(
        text(
            """
            UPDATE media_slots
               SET holder_id = NULL,
                   phase = 'held',
                   kind = '',
                   actor_id = '',
                   window_id = '',
                   subject_id = '',
                   call_id = NULL,
                   activity_id = NULL,
                   started_at = NULL,
                   lease_until = NULL
             WHERE resource_key = :key
               AND holder_id = :holder
               AND generation = :gen
            """
        ),
        {"key": RESOURCE_KEY, "holder": handle.holder_id, "gen": handle.generation},
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def fence_ok(session: AsyncSession, handle: SlotHandle) -> bool:
    """这次写入还允许落库吗？**所有持久副作用提交前都要调它。**

    与 `renew` 的区别：`renew` 会顺手延长租约，而这里只读——业务写入的
    事务里不该带副作用。
    """
    row = (
        await session.execute(
            text(
                """
                SELECT 1
                  FROM media_slots
                 WHERE resource_key = :key
                   AND holder_id = :holder
                   AND generation = :gen
                   AND lease_until > clock_timestamp()
                """
            ),
            {"key": RESOURCE_KEY, "holder": handle.holder_id, "gen": handle.generation},
        )
    ).first()
    return row is not None


async def current_holder(session: AsyncSession) -> dict[str, object] | None:
    """当前有效占用（没有则 None）。给"她此刻在做什么"的读侧用。

    只返回 **`active` 且租约有效**的占用：`held` 只是"正在准备"，说成
    "她正在看"就是编造。过期的不清也不报——清理交给
    `release_expired()`，读路径不该顺手改数据。
    """
    row = (
        await session.execute(
            text(
                """
                SELECT holder_id, phase, kind, window_id, subject_id, activity_id,
                       started_at, generation
                  FROM media_slots
                 WHERE resource_key = :key
                   AND holder_id IS NOT NULL
                   AND phase = 'active'
                   AND lease_until > clock_timestamp()
                """
            ),
            {"key": RESOURCE_KEY},
        )
    ).first()
    if row is None:
        return None
    return {
        "holder_id": row.holder_id,
        "phase": row.phase,
        "kind": row.kind,
        "window_id": row.window_id,
        "subject_id": row.subject_id,
        "activity_id": row.activity_id,
        "started_at": row.started_at,
        "generation": int(row.generation),
    }


async def release_expired(session: AsyncSession) -> int:
    """回收过期槽位（崩溃后残留）。返回清掉的条数。零模型、幂等。

    只清**租约已过期**的：清掉一个还有效的占用会破坏全局单飞。
    """
    result = await session.execute(
        text(
            """
            UPDATE media_slots
               SET holder_id = NULL,
                   phase = 'held',
                   kind = '',
                   actor_id = '',
                   window_id = '',
                   subject_id = '',
                   call_id = NULL,
                   activity_id = NULL,
                   started_at = NULL,
                   lease_until = NULL
             WHERE resource_key = :key
               AND holder_id IS NOT NULL
               AND lease_until IS NOT NULL
               AND lease_until <= clock_timestamp()
            """
        ),
        {"key": RESOURCE_KEY},
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "EXECUTION_HARD_CUTOFF_SECONDS",
    "KIND_BILIBILI_FEED",
    "KIND_LIVESTREAM",
    "KIND_VIDEO_WATCH",
    "OUTCOME_ACQUIRED",
    "OUTCOME_BUSY",
    "OUTCOME_UNAVAILABLE",
    "RESOURCE_KEY",
    "VALID_KINDS",
    "SlotHandle",
    "SlotOutcome",
    "acquire",
    "current_holder",
    "fence_ok",
    "mark_active",
    "release",
    "release_expired",
    "renew",
]
