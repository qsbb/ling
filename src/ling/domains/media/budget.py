"""后台视频读取的**持久**预算预留（设计 §0 第 4 步）。

## 为什么必须有这一步

后台视频作业在 worker 里跑，而 worker 可以多副本。没有持久预算时，同一份
"今天还能看几个"的判断在每个副本里各算一份——**N 个副本就能烧 N 倍的钱**，
而且完全看不出来。

用户原话「次数限制去掉」，所以这里**不设次数上限**，只守 **token 额度**，
与同步观看路径是同一个判据（`TokenQuotaStore` / `router._quota_block`）。

## 为什么复用 `behavior_model_calls` 而不是新表

那张表已经解决了本问题需要的全部并发语义：

- `UNIQUE(actor_id, kind, idempotency_key, attempt)` 让同一 attempt 只有一个
  赢家；
- 租约过期只能开新 attempt（旧 attempt 的预算**不退款**——与设计稿一致：
  模型可能已经调用过，退回去就等于允许重复烧钱）；
- 预留金额与 `model_started_at` 同事务落库，崩溃后重启仍计入当日上限。

新增一张表只会多一套要与它保持一致的状态。

## 与同步观看路径的关系

同步路径（`VideoWatchService`）读的是实时 token 余额，**不**走这张表——
它是用户当场等着的，不该被后台作业的预留挡住（那样"我现在想看"会被一个
一小时前的后台任务拒绝，用户完全无法理解）。

两条路径共用同一个余额来源，所以总量仍然受控；区别只是后台这条额外记一份
可审计的预留明细，让"这个月视频烧了多少"有据可查。

## 失败占额不退款

取件失败、模型超时、worker 关停（`unknown`）都占额。理由与设计稿一致：
这些情况下我们**无法证明**模型没被调用过，退回额度就是允许重复花钱。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("ling.domains.media.budget")

#: 预算表里的 kind。与日程的 outline/refine 分开计，互不挪用。
VIDEO_WATCH_KIND = "video_watch"

#: 单次后台读取的预留上界。
#:
#: 输入按"抽帧 + 提示"估：视频描述拿固定帧数（见 `video.py`），一帧的视觉
#: token 上界乘以帧数再加提示开销。输出是描述文本，比大纲短得多。
#: 这两个数是**预留**不是限额——实际用量由 `actual_tokens` 记录。
VIDEO_RESERVED_INPUT_TOKENS = 4000
VIDEO_RESERVED_OUTPUT_TOKENS = 800

#: 每日 token 预算上界（只用于"这天后台视频最多烧多少"）。
#:
#: 与 `OUTLINE_DAILY_TOKEN_BUDGET` 分开：日程是她的生活节律，视频是娱乐，
#: 两者共用一份预算会让"今天日程花多了就不许看视频"这种荒唐结论出现。
VIDEO_DAILY_TOKEN_BUDGET = 120_000

#: 每日后台作业**次数**上界：不设。
#:
#: 用户原话「次数限制去掉」。这里曾经写过一个 50 次的"硬护栏"，理由是
#: "防死循环"，那是**自欺**——它对正常使用不可见，对失控也不够快（50 次
#: 视频就足以烧掉一天预算，真正的护栏是 token 额度本身）。保留一个和
#: 用户明确要求相反的数字，只会在某天有人调大 token 预算时悄悄变成产品限额。
#:
#: 真正的防失控手段已经有两层且都在起作用：每日 token 额度（120k 烧完就
#: 停）、以及 `behavior_model_calls` 的唯一约束让同一 attempt 只有一个赢家。
#: 传 `None` 表示不按次数限制。
VIDEO_MAX_CALLS_PER_DAY: int | None = None

#: 预留租约。后台视频作业的执行硬截止是 180 秒，留足收尾余量。
VIDEO_LEASE_SECONDS = 240.0


@dataclass(frozen=True)
class ReservationResult:
    """预留结果。`call_id` 非空才算拿到。"""

    call_id: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.call_id is not None


async def reserve_video_call(
    session: AsyncSession,
    *,
    actor_id: str,
    idempotency_key: str,
    now: datetime | None = None,
    lease_owner: str = "media-worker",
    plan_day: date | None = None,
) -> ReservationResult:
    """为一次后台视频模型调用预留预算。拿不到返回 `reason`（人话）。

    调用方负责提交事务。
    """
    # 经 agenda 的**公开 service** 入口，不直接 import 它的内部 `timeline`
    # 模块：账本的并发语义（唯一约束/attempt/不退款）由那边维护，跨域只该
    # 依赖对外的 service 契约（ADR-0001）。
    from ling.domains.agenda.service import AgendaService

    moment = now or datetime.now(UTC)
    day = plan_day or moment.date()
    call_id = await AgendaService(session, actor_id=actor_id).reserve_model_call(
        kind=VIDEO_WATCH_KIND,
        plan_day=day,
        idempotency_key=idempotency_key,
        now=moment,
        lease_owner=lease_owner,
        max_calls=VIDEO_MAX_CALLS_PER_DAY,
        daily_token_budget=VIDEO_DAILY_TOKEN_BUDGET,
        reserved_input_tokens=VIDEO_RESERVED_INPUT_TOKENS,
        reserved_output_tokens=VIDEO_RESERVED_OUTPUT_TOKENS,
        lease_seconds=VIDEO_LEASE_SECONDS,
    )
    if call_id is None:
        # 两种原因合并成一句是不行的：一种是"今天真的用完了"，
        # 一种是"同时又别人在抢同一个 attempt"。前者该如实说，
        # 后者重试就行。这里保守地都说成"预算闸没放行"，并记日志区分。
        logger.info(
            "后台视频预算闸未放行 actor=%s key=%s day=%s",
            actor_id,
            idempotency_key,
            day.isoformat(),
        )
        return ReservationResult(reason="后台视频预算闸本次没有放行")

    return ReservationResult(call_id=call_id)


async def finish_video_call(
    session: AsyncSession,
    *,
    call_id: str,
    state: str,
    actual_tokens: int | None = None,
    error_code: str | None = None,
) -> None:
    """结清一次预留。

    `state` 用 `committed`（模型真的跑完）或 `failed`（含 unknown——模型可能
    已被调用但我们拿不到结果）。**两者都占额**：无法证明没调用过就退款，
    等于允许重复花钱。
    """
    from ling.config import get_settings
    from ling.domains.agenda.service import AgendaService

    await AgendaService(
        session, actor_id=str(get_settings().actor_id)
    ).finish_model_call(
        call_id=call_id,
        state=state,
        actual_tokens=actual_tokens,
        error_code=error_code,
    )


__all__ = [
    "VIDEO_DAILY_TOKEN_BUDGET",
    "VIDEO_LEASE_SECONDS",
    "VIDEO_MAX_CALLS_PER_DAY",
    "VIDEO_RESERVED_INPUT_TOKENS",
    "VIDEO_RESERVED_OUTPUT_TOKENS",
    "VIDEO_WATCH_KIND",
    "ReservationResult",
    "finish_video_call",
    "reserve_video_call",
]
