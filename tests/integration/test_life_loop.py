"""她的生活：能排出日程 → 到点真的开始做事。

这一组回答一个很具体的问题：**"她此刻在做什么"会不会永远是空的？**
在补 `life.plan` 之前就是——`life.tick` 只在"有到点的计划"时开活动，
而计划从来没有来源，活动表永远是 0 条。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.w0_web


# 测试**不能依赖墙上时钟**：`handle_life_plan` 里有一条"离休息不足 40 分钟
# 就不排"的护栏，晚上跑测试会直接命中，于是三条用例在夜间全红、白天全绿。
# 固定在一个安全的本地时间（下午 15:00），所有相对时间都从这个基准算。
FROZEN_LOCAL_HOUR = 15


def _frozen_now():
    """固定在本地 15:00 的当前时刻（转成 UTC 返回）。"""
    from ling.config import get_settings
    from ling.domains.agenda.service import _tz

    tz = _tz(get_settings().user_timezone or "Asia/Shanghai")
    local = datetime.now(tz).replace(hour=FROZEN_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def _freeze_clock(monkeypatch, worker) -> None:
    frozen = _frozen_now()
    monkeypatch.setattr(worker, "utcnow", lambda: frozen)


def _ago(hours_ago: int = 3) -> str:
    """取"N 小时前"的 HH:MM（按角色自己的时区）。"""
    from ling.config import get_settings
    from ling.domains.agenda.service import _tz

    local = (_frozen_now() - timedelta(hours=hours_ago)).astimezone(
        _tz(get_settings().user_timezone or "Asia/Shanghai")
    )
    return f"{local.hour:02d}:{local.minute:02d}"


def _soon(hours_ahead: int = 1) -> str:
    """取"从现在起 N 小时后"的 HH:MM（按角色自己的时区）。

    写死 09:00 会让用例依赖运行时刻：下午跑就会被"过去时段不排"的规则丢掉，
    于是看起来像功能坏了——实际是规则在正常工作。
    """
    from ling.config import get_settings
    from ling.domains.agenda.service import _tz

    local = (_frozen_now() + timedelta(hours=hours_ahead)).astimezone(
        _tz(get_settings().user_timezone or "Asia/Shanghai")
    )
    return f"{local.hour:02d}:{local.minute:02d}"


class _Router:
    """假模型：返回一份固定日程。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def stream(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls += 1

        class _Chunk:
            delta = self._text

        class _Outcome:
            pass

        yield _Chunk(), _Outcome()


async def _plan_count() -> int:
    from ling.domains.agenda.service import AgendaService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        return len(await AgendaService(session, actor_id="actor_ling").upcoming())


async def test_life_plan_creates_todays_plans(web_env, monkeypatch) -> None:
    """`life.plan` 要真的把日程排进库里。"""
    from ling import worker

    _freeze_clock(monkeypatch, worker)

    router = _Router('[{"title":"翻两页书","start":"' + _soon() + '","minutes":60}]')
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})

    assert router.calls == 1
    assert await _plan_count() >= 1


async def test_life_plan_does_not_run_twice_a_day(web_env, monkeypatch) -> None:
    """一天只排一次——否则每次 tick 都问一遍模型，白烧钱也白打扰。"""
    from ling import worker

    _freeze_clock(monkeypatch, worker)

    router = _Router('[{"title":"翻两页书","start":"' + _soon() + '","minutes":60}]')
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})
    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})
    assert router.calls == 1


async def test_life_tick_starts_an_activity_from_a_due_plan(web_env, monkeypatch) -> None:
    """到点的计划要真的变成一个"在做"的活动 —— 这才叫有生活。"""
    from ling import worker
    from ling.domains.activity.service import ActivityService
    from ling.domains.agenda.service import AgendaService
    from ling.infra.db import session_scope

    _freeze_clock(monkeypatch, worker)
    now = _frozen_now()
    async with session_scope() as session:
        agenda = AgendaService(session, actor_id="actor_ling")
        await agenda.propose_plan(
            kind="simulated",
            title="收拾书桌",
            start_at=now - timedelta(minutes=5),
            end_at=now + timedelta(minutes=55),
            commitment_level="tentative",
        )

    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    # 让它认为自己醒着（默认 8:00-23:00，测试可能在别的时间跑）
    await worker.handle_life_tick({})

    async with session_scope() as session:
        current = await ActivityService(session, actor_id="actor_ling").current()
    assert current is not None, "到点的计划没有变成活动"
    assert current.state == "running"
    assert current.origin == "simulated", "模拟活动必须标明来源，不能冒充真实经历"


async def test_no_plan_when_model_unavailable(web_env, monkeypatch) -> None:
    """模型不可用时不排日程——**绝不硬编"她在看书"**。"""
    from ling import worker

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})
    assert await _plan_count() == 0


async def test_garbage_model_output_creates_no_plan(web_env, monkeypatch) -> None:
    """模型输出没有可用条目时，同样不排。"""
    from ling import worker

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", _Router("我今天想做点什么……"))
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})
    assert await _plan_count() == 0


async def test_past_time_slots_are_not_scheduled(web_env, monkeypatch) -> None:
    """已经过去的时段不排。

    否则模型给一个上午的时间，用户在下午打开界面就会看到"她正在做
    一件早就该结束的事"——比什么都不显示更假。
    """
    from ling import worker

    monkeypatch.setattr(
        worker, "_MEMORY_ROUTER",
        _Router('[{"title":"晨间散步","start":"' + _ago() + '","minutes":60}]'),
    )
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})
    assert await _plan_count() == 0


async def test_prompt_tells_the_model_what_time_it_is(web_env, monkeypatch) -> None:
    """prompt 必须包含"现在几点"，并要求只排后面的时间。

    实测踩过：不说现在几点，模型会顺着"今天"排上午的时间，而此刻已是
    下午——那些时段全被"过去不排"丢掉，结果是 parsed=3 但 created=0，
    界面永远没有日程。
    """
    from ling import worker

    captured: list[str] = []

    class _CapturingRouter(_Router):
        async def stream(self, *_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            for msg in kwargs.get("messages") or []:
                captured.append(msg.content)
            async for item in super().stream():
                yield item

    monkeypatch.setattr(worker, "_MEMORY_ROUTER", _CapturingRouter("[]"))
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)
    monkeypatch.setattr(worker, "_LAST_PLAN_DATE", "")

    _freeze_clock(monkeypatch, worker)
    await worker.handle_life_plan({})

    assert captured, "没有向模型发出请求"
    prompt = captured[0]
    assert "现在已经是" in prompt, "prompt 没告诉模型当前时间"
    assert "晚于" in prompt, "prompt 没有约束只用后面的时间"
