"""供应商每日 token 额度：按天记账，用完就不再调用这一家。

## 为什么按 token 而不是按钱

按钱需要一张单价表：每家供应商、每个模型、每次调价都要维护。而 token 数是
供应商**回执里就有**的数字——记它不需要价目表，也不会因为对方调价而失真。
代价是"额度"和"花了多少钱"不再一一对应，但对"今天别把它用爆"这个目的，
token 是更直接、更不容易算错的那个量。

## 为什么额度挂在供应商，不挂在职责

额度是"这家一天能用多少"。同一家下面的五个职责共用一份额度，才符合
"我买的是这家的 200k tokens/天"这件事。按职责分别限额会造出
"看图片还有额度、日常对话已经用完"这种没人能解释的状态。

## 超限之后

这一家在这一天里被当作**不可用**：有备用供应商就转过去，没有就这一轮失败，
并给出可执行提示（换供应商 / 提高额度 / 明天再说）。

不做"自动降级到更便宜的模型"：那需要一份"谁比谁便宜"的表，而错判会把贵的
当便宜的用，账单比失败更难受。

## 记账的准确性

- 供应商回了 usage 就用它（`prompt_tokens + completion_tokens`）。
- 没回就**估算**，并把这一笔记成"估算"（`estimated=True`），界面上可以据此
  说明"这个数不是供应商给的"。不估算的话，不支持 usage 的兼容端点会永远
  显示 0 用量，额度形同虚设——那比不准更糟。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ling.infra.models import ProviderTokenUsage, utcnow

logger = logging.getLogger("ling.models.quota")


def local_day(now: datetime | None = None, timezone: str | None = None) -> date:
    """额度用的"今天"。

    用**本地日历日**，不是 UTC 日：东八区的 00:00–08:00 若按 UTC 算，
    "今天"会退回前一天，用户在早上八点看到的"今天已用"其实是昨天的账。
    """
    moment = now or utcnow()
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            return moment.astimezone(ZoneInfo(timezone)).date()
        except Exception:  # noqa: BLE001 - 时区名非法时退回 UTC，不让记账崩掉
            logger.warning("额度记账的时区 %r 无效，退回 UTC", timezone)
    return moment.astimezone().date()


class TokenQuotaStore:
    """按 (供应商, 本地日) 累计已用 token。

    每次调用前读一次、调用后写一次。表很小（每供应商每天一行，带唯一约束），
    两个查询都是索引命中，不值得为此加进程内缓存——缓存会让多副本部署时
    "这台觉得还有额度、那台已经把额度用完了"。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = timezone
        # 还没成功写进库的增量，按 (供应商, 日) 累计。
        #
        # ## 为什么需要这一层
        #
        # 写账发生在**这一轮对话的事务还没提交的时候**：sqlite 下同库第二个
        # 写入者会直接撞 `database is locked`。原来这里是"失败就记一条
        # warning 算了"，结果是——真实跑一遍，回复正常、界面上的"今日已用"
        # 永远是 0，额度等于不存在。而且这种失效**没有任何用户可见的症状**。
        #
        # 所以先把增量放进内存：额度判断立刻算上它（进程内准确），
        # 落库变成"尽力而为"，下一次记账/查询时一起补写。多副本部署时
        # 各副本仍以库里的账为准，误差只在这几秒内。
        self._pending: dict[tuple[str, date], int] = {}
        self._pending_estimated: set[tuple[str, date]] = set()
        # 一个常驻的后台落库任务（不是每次记账起一个）。见 `_ensure_flusher`。
        self._flusher: object | None = None

    def today(self) -> date:
        return local_day(timezone=self._timezone)

    async def used(self, provider_id: str) -> int:
        day = self.today()
        # 读之前先把暂存的增量落库：调用发生在**上一轮提交之后、这一轮开始之前**，
        # 是稳定安全的落库时机（不像 `record()` 那样正卡在事务中间）。
        await self._flush(provider_id, day)
        return await self._stored(provider_id, day) + self._pending.get(
            (provider_id, day), 0
        )

    async def _stored(self, provider_id: str, day: date) -> int:
        """库里已落盘的当日用量。读不到就当 0——额度判断照内存里的账走。"""
        try:
            async with self._session_factory() as session:
                row = await self._row(session, provider_id, day)
                return int(row.tokens) if row is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "读取 token 用量失败（%s）：%s；本次按进程内记账判断", provider_id, exc
            )
            return 0

    async def record(
        self, provider_id: str, tokens: int, *, estimated: bool = False
    ) -> int:
        """累加一次调用的 token，返回当天累计值。

        增量先进内存（**额度判断立刻生效**），落库留给 `used()` / `aclose()`。
        落库失败不回滚内存里的账——宁可库里的数偏小，也不要这一轮悄悄
        用了额度却不计数。
        """
        if tokens <= 0 and not estimated:
            return await self.used(provider_id)
        day = self.today()
        key = (provider_id, day)
        self._pending[key] = self._pending.get(key, 0) + max(0, tokens)
        if estimated:
            self._pending_estimated.add(key)
        # 立刻试写一次，**失败不算错**，然后交给后台重试。
        #
        # 这次写几乎一定失败：记账发生在**本轮对话的事务还没提交的时候**，
        # 而 sqlite 是单写者（一轮对话从开始到提交全程持锁，实测即使开 WAL
        # 也会 `database is locked`）。所以"这一轮结束时同步写"这条路本身
        # 就是错的，不是超时不够长。
        #
        # 于是：增量先进内存（额度判断立刻准确），落库交给后台重试——
        # 等那一轮提交完自然就写进去了。另外 `used()` 在每次调用前也会补写，
        # 进程退出时 `aclose()` 再补一次。
        await self._flush(provider_id, day)
        self._ensure_flusher()
        # 用 `get` 而不是 `[key]`：**记账成功时** `_flush` 会把这一笔从内存里
        # 扣掉并在扣到 0 时删键，紧接着 `self._pending[key]` 就是 KeyError。
        #
        # 这个缺陷的后果不是"记账不准"（库已经写进去了），而是**每一次成功的
        # 生成都往日志里扔一条** `token 用量记账失败（…）：('fake', date(…))`：
        # 一条假警报，把真正要看的失败盖掉；`record()` 的返回值也永远拿不到。
        # 读不懂这句 warning 的人会去查数据库，而那里一切正常。
        return self._pending.get(key, 0) + await self._stored(provider_id, day)

    def _ensure_flusher(self) -> None:
        """确保有一个后台任务在补写。

        只起**一个**常驻任务，而不是每次记账起一个：每轮一个任务时，
        一次长对话会攒下几十个几乎同时在重试的任务，它们互相抢写锁，
        反而让每一次都失败。
        """
        import asyncio

        task = self._flusher
        if task is not None and not task.done():  # type: ignore[attr-defined]
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flusher = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """等本轮事务提交后把内存里的账写进库，直到内存清空。"""
        import asyncio

        try:
            for _ in range(24):
                await asyncio.sleep(0.5)
                if not self._pending:
                    return
                for provider_id, day in list(self._pending):
                    await self._flush(provider_id, day)
                if not self._pending:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            logger.warning("额度后台落库任务异常退出：%s", exc)

    async def _flush(self, provider_id: str, day: date) -> None:
        """把内存里的增量写进库；失败就留着，下次再来。

        不在这里重试等待：记账发生在流式回复的末尾，阻塞会让用户多等；
        下一次调用（读或写）自然会再试一次。
        """
        key = (provider_id, day)
        delta = self._pending.get(key, 0)
        if delta <= 0:
            return
        estimated = key in self._pending_estimated
        try:
            async with self._session_factory() as session:
                row = await self._row(session, provider_id, day)
                if row is None:
                    from ling.infra.security import new_id

                    row = ProviderTokenUsage(
                        id=new_id("tok"),
                        provider_id=provider_id,
                        day=day,
                        tokens=0,
                        estimated=estimated,
                    )
                    session.add(row)
                row.tokens = int(row.tokens) + delta
                # 只要这一天的账里有一条是估算的，整格就标成估算：
                # 精确数与估算数混在一起时，显示"精确"是在撒谎。
                row.estimated = bool(row.estimated) or estimated
                row.updated_at = utcnow()
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次判断
            logger.warning(
                "token 用量落库失败（%s, %s，%d tokens 暂存在内存）：%s",
                provider_id,
                day.isoformat(),
                delta,
                exc,
            )
            return
        # 只有真的写成功了才清掉内存里的增量；否则下次会重复写。
        self._pending[key] = max(0, self._pending.get(key, 0) - delta)
        if self._pending[key] == 0:
            self._pending.pop(key, None)
        self._pending_estimated.discard(key)

    async def aclose(self) -> None:
        """停掉后台任务并尽力把暂存的账写进去。

        进程退出前调用：否则"最后一轮的用量"只留在内存里，下次启动就没了。
        写不进去也无所谓——那时已经没有额度判断要做了。
        """
        import asyncio
        import contextlib

        task = self._flusher
        self._flusher = None
        if task is not None:
            task.cancel()  # type: ignore[attr-defined]
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task  # type: ignore[misc]
        for (provider_id, day), delta in list(self._pending.items()):
            if delta > 0:
                await self._flush(provider_id, day)

    async def snapshot(
        self, provider_ids: Sequence[str] | None = None
    ) -> dict[str, dict[str, object]]:
        """今天的用量快照，给设置页显示「今日已用 x / y」。"""
        day = self.today()
        try:
            async with self._session_factory() as session:
                stmt = select(ProviderTokenUsage).where(ProviderTokenUsage.day == day)
                if provider_ids:
                    stmt = stmt.where(ProviderTokenUsage.provider_id.in_(list(provider_ids)))
                rows = (await session.execute(stmt)).scalars().all()
        except Exception as exc:  # noqa: BLE001 - 读不到库就只报内存里的账
            logger.warning("读取 token 用量快照失败：%s；只显示进程内记账", exc)
            rows = []
        result: dict[str, dict[str, object]] = {
            row.provider_id: {
                "day": day.isoformat(),
                "used_tokens": int(row.tokens),
                "estimated": bool(row.estimated),
            }
            for row in rows
        }
        # 还没落库的增量也要算进去：否则刚聊完一轮，界面上显示的还是旧数字，
        # 用户会以为"额度根本没在记"。
        for (pid, pending_day), delta in self._pending.items():
            if pending_day != day:
                continue
            if provider_ids and pid not in provider_ids:
                continue
            entry = result.setdefault(
                pid,
                {"day": day.isoformat(), "used_tokens": 0, "estimated": False},
            )
            entry["used_tokens"] = int(str(entry["used_tokens"])) + delta
            entry["estimated"] = bool(entry["estimated"]) or ((pid, day) in self._pending_estimated)
        return result

    @staticmethod
    async def _row(
        session: AsyncSession, provider_id: str, day: date
    ) -> ProviderTokenUsage | None:
        return (
            await session.execute(
                select(ProviderTokenUsage).where(
                    ProviderTokenUsage.provider_id == provider_id,
                    ProviderTokenUsage.day == day,
                )
            )
        ).scalar_one_or_none()


def estimate_tokens_for(messages: Sequence[object], reply: str) -> int:
    """供应商没给 usage 时的估算。

    复用上下文预算那套粗略算法（中文 1 字≈1 token，英文≈4 字符/token），
    避免两处估算口径不同——否则"预算裁剪说放得下"和"记账说超了"会互相矛盾。
    """
    from ling.kernel.context import estimate_tokens

    prompt = 0
    for message in messages:
        prompt += estimate_tokens(str(getattr(message, "content", "") or ""))
    return prompt + estimate_tokens(reply)


__all__ = ["TokenQuotaStore", "estimate_tokens_for", "local_day"]
