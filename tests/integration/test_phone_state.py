"""看手机状态机：上次 / 下次 / 此刻在看什么。

三条规则各配一条测试，其中第 2 条是用户明确要求过的
（「从本次看手机**结束**之后开始重新计时」）——写错了不会报错，
只会让界面上那个数字看起来不对劲，所以必须钉住。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.phone import DEFAULT_IDLE_MINUTES, PhoneService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv, web_env  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

T0 = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


async def test_first_visit_is_idle_with_nothing_scheduled(web_env: TestEnv) -> None:  # noqa: F811
    async with session_scope() as session:
        view = await PhoneService(session).current()
    assert view.state == "idle"
    assert view.next_look_at is None
    assert not view.is_watching


async def test_start_watching_clears_next_time(web_env: TestEnv) -> None:  # noqa: F811
    """正在看的时候不该还挂着一个"下次"——界面要显示「等她收起」。"""
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.schedule_next(minutes=10, now=T0)
        view = await svc.start_watching(app="QQ · 摸鱼群", now=T0 + timedelta(minutes=1))
    assert view.is_watching
    assert view.watching_app == "QQ · 摸鱼群"
    assert view.next_look_at is None, "看的时候不能排下一次"


async def test_next_time_counts_from_when_she_put_it_down(web_env: TestEnv) -> None:  # noqa: F811
    """**下次从"看完"开始算，不是从"开始看"算。**

    用户原话：「刷新下次看手机时间，从本次看手机结束之后开始重新计时」。

    从开始算的话，一次看得久了下次就会提前到来——界面上那个数字会
    比预期早，而且是那种"说不上哪错了但就是不对"的错。
    """
    start = T0
    end = T0 + timedelta(minutes=7)  # 看了 7 分钟
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.start_watching(now=start)
        view = await svc.stop_watching(now=end, next_in_minutes=10)

    assert view.state == "idle"
    assert view.last_looked_at == end
    assert view.next_look_at == end + timedelta(minutes=10), (
        "应从结束时刻 +10 分钟，而不是开始时刻 +10 分钟"
    )
    # 明确排掉"从开始算"这个错误实现
    assert view.next_look_at != start + timedelta(minutes=10)


async def test_default_interval_is_ten_minutes(web_env: TestEnv) -> None:  # noqa: F811
    """不指定间隔时的默认值：10 分钟。

    经验依据：真人一天主动解锁 80–150 次，清醒 15 小时约 10 分钟一次。
    """
    assert DEFAULT_IDLE_MINUTES == 10
    async with session_scope() as session:
        view = await PhoneService(session).stop_watching(now=T0)
    assert view.next_look_at == T0 + timedelta(minutes=10)


async def test_due_is_false_while_watching(web_env: TestEnv) -> None:  # noqa: F811
    """正在看的时候不该被再次叫醒——否则会一直重入。"""
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.start_watching(now=T0)
        assert not await svc.due(now=T0 + timedelta(hours=5))


async def test_due_becomes_true_after_next_time(web_env: TestEnv) -> None:  # noqa: F811
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.stop_watching(now=T0, next_in_minutes=10)
        assert not await svc.due(now=T0 + timedelta(minutes=5))
        assert await svc.due(now=T0 + timedelta(minutes=11))


async def test_sleeping_schedules_nothing(web_env: TestEnv) -> None:  # noqa: F811
    """睡着时用 0 分钟表示"先不排"——醒来再说，而不是每 10 分钟醒一次。"""
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.stop_watching(now=T0, next_in_minutes=0)
        assert not await svc.due(now=T0 + timedelta(hours=8))


async def test_schedule_next_does_not_disturb_watching(web_env: TestEnv) -> None:  # noqa: F811
    """正在看的时候改"下次"是无效操作，不能把 watching 状态弄丢。"""
    async with session_scope() as session:
        svc = PhoneService(session)
        await svc.start_watching(app="QQ", now=T0)
        view = await svc.schedule_next(minutes=5, now=T0 + timedelta(minutes=1))
    assert view.is_watching
    assert view.next_look_at is None
