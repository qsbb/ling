"""主动联系硬门：勿扰时段、每日限额、退订、忙碌、渠道能力。

抓到两个真实缺陷：勿扰时段与"今日限额"都用**服务器**时区计算
（`now.astimezone()` 无参 = 系统本地时区）。多时区部署下，
服务器在 UTC、用户在东八区时，"22:00-08:00 勿扰"会按 UTC 判定——
等于在用户清醒时静音、在凌晨打扰。每日限额也会在错误的时刻重置。
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from ling.domains.proactive.service import ProactivityService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

QUIET = (time(22, 0), time(8, 0))


async def _gate(env: TestEnv, **kwargs: object):
    kwargs.setdefault("channel_can_proactive", True)
    async with session_scope() as session:
        return await ProactivityService(session).gate(
            subject_id=env.subject_id,
            quiet_start=QUIET[0],
            quiet_end=QUIET[1],
            **kwargs,  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------ 时区（真实缺陷）


async def test_quiet_hours_use_subject_timezone(logged_in: TestEnv) -> None:
    """同一 UTC 时刻，不同用户时区 → 勿扰判定必须不同。

    UTC 23:00 = 东八区次日 07:00（仍在勿扰），= UTC 23:00（勿扰），
    = 洛杉矶 15:00（不在勿扰）。
    """
    utc_2300 = datetime(2030, 6, 1, 23, 0, tzinfo=UTC)

    # 洛杉矶（UTC-7）：16:00，不在勿扰
    la = await _gate(logged_in, now=utc_2300, subject_timezone="America/Los_Angeles")
    assert la.allowed is True, f"洛杉矶 16:00 被误判为勿扰: {la.reasons}"

    # 上海（UTC+8）：次日 07:00，仍在 22:00-08:00 勿扰
    sh = await _gate(logged_in, now=utc_2300, subject_timezone="Asia/Shanghai")
    assert sh.allowed is False
    assert any("勿扰" in r for r in sh.reasons)

    # 东京（UTC+9）：次日 08:00，刚好出勿扰
    tokyo = await _gate(logged_in, now=utc_2300, subject_timezone="Asia/Tokyo")
    assert tokyo.allowed is True, f"东京 08:00 应已出勿扰: {tokyo.reasons}"


async def test_missing_timezone_blocks_instead_of_guessing(logged_in: TestEnv) -> None:
    """时区未知时**暂停**主动联系，而不是按 UTC 硬算并照常打扰。

    取舍：勿扰时段的全部意义就是"不在错误的时间打扰人"。不知道用户时区
    还照常投递，等于这条保护根本不存在——而用户只会觉得"她怎么这个点找我"，
    无从判断是配置问题。

    暂停 + 明说原因可解释；静音时段错位不可解释。
    """
    utc_1200 = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
    result = await _gate(logged_in, now=utc_1200, subject_timezone=None)
    assert result.allowed is False, "时区未知却放行了"
    assert any("LING_USER_TIMEZONE" in r for r in result.reasons), (
        f"没说清怎么修：{result.reasons}"
    )


async def test_known_timezone_lets_the_same_moment_through(logged_in: TestEnv) -> None:
    """同样的时刻，给了时区就放行——证明拦截的原因确实是"不知道时区"。"""
    utc_1200 = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
    result = await _gate(logged_in, now=utc_1200, subject_timezone="Asia/Shanghai")
    assert result.allowed is True, f"上海 20:00 不该是勿扰: {result.reasons}"


async def test_daily_limit_uses_subject_local_day(logged_in: TestEnv) -> None:
    """每日限额按用户当地"今天"计算，不是 UTC 日。

    这个用例**能区分**两种算法（这是关键）：

    记录：UTC 6/1 23:30 = 上海 6/2 07:30  → 当地 6/2、UTC 6/1
    查询：UTC 6/2 01:00 = 上海 6/2 09:00  → 当地 6/2、UTC 6/2

    按当地日：两者同为 6/2 → 限额已用 → 拒绝
    按 UTC 日：6/1 与 6/2 不同 → 限额未用 → 放行（错误）

    原实现按 UTC 算，会在当地同一分钟内"重置"限额，
    让用户早上刚醒就收到第二条主动消息。
    """
    from ling.infra.models import ProactiveOpportunity

    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id="opp_local_today",
                actor_id="actor_ling",
                subject_id=logged_in.subject_id,
                kind="greeting",
                reason="测试",
                state="sent",
                expires_at=datetime(2030, 6, 3, 0, 0, tzinfo=UTC),
                created_at=datetime(2030, 6, 1, 23, 30, tzinfo=UTC),
                # 限额按实际送达时间计日
                sent_at=datetime(2030, 6, 1, 23, 30, tzinfo=UTC),
            )
        )

    query_at = datetime(2030, 6, 2, 1, 0, tzinfo=UTC)  # 上海 6/2 09:00
    result = await _gate(
        logged_in, now=query_at, subject_timezone="Asia/Shanghai", daily_limit=1
    )
    assert result.allowed is False, (
        "同日（当地）已发过一条却仍被放行——说明限额按 UTC 日算，"
        "会在当地清晨错误重置"
    )
    assert any("上限" in r for r in result.reasons)


async def test_daily_limit_new_local_day_allows(logged_in: TestEnv) -> None:
    """当地日期翻页后限额重新算（不能永久锁死）。"""
    from ling.infra.models import ProactiveOpportunity

    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id="opp_yesterday",
                actor_id="actor_ling",
                subject_id=logged_in.subject_id,
                kind="greeting",
                reason="测试",
                state="sent",
                expires_at=datetime(2030, 6, 3, 0, 0, tzinfo=UTC),
                created_at=datetime(2030, 6, 1, 4, 0, tzinfo=UTC),  # 上海 6/1 12:00
                sent_at=datetime(2030, 6, 1, 4, 0, tzinfo=UTC),
            )
        )
    query_at = datetime(2030, 6, 2, 2, 0, tzinfo=UTC)  # 上海 6/2 10:00
    result = await _gate(
        logged_in, now=query_at, subject_timezone="Asia/Shanghai", daily_limit=1
    )
    assert result.allowed is True, f"当地新的一天仍被昨天占满: {result.reasons}"


# ------------------------------------------------------------------ 其他硬门


async def test_opted_out_blocks(logged_in: TestEnv) -> None:
    result = await _gate(
        logged_in,
        now=datetime(2030, 6, 1, 4, 0, tzinfo=UTC),  # 上海 12:00，非勿扰
        subject_timezone="Asia/Shanghai",
        user_opted_out=True,
    )
    assert result.allowed is False
    assert any("关闭主动" in r for r in result.reasons)


async def test_actor_busy_blocks(logged_in: TestEnv) -> None:
    """角色正在对话时不要主动插话。"""
    result = await _gate(
        logged_in,
        now=datetime(2030, 6, 1, 4, 0, tzinfo=UTC),
        subject_timezone="Asia/Shanghai",
        actor_busy=True,
    )
    assert result.allowed is False
    assert any("正在与用户对话" in r for r in result.reasons)


async def test_channel_without_proactive_blocks(logged_in: TestEnv) -> None:
    """渠道不支持主动投递时必须拦住（Web 就没有主动推送）。"""
    async with session_scope() as session:
        result = await ProactivityService(session).gate(
            subject_id=logged_in.subject_id,
            now=datetime(2030, 6, 1, 4, 0, tzinfo=UTC),
            quiet_start=QUIET[0],
            quiet_end=QUIET[1],
            subject_timezone="Asia/Shanghai",
            channel_can_proactive=False,
        )
    assert result.allowed is False
    assert any("不支持主动投递" in r for r in result.reasons)


async def test_all_reasons_collected(logged_in: TestEnv) -> None:
    """多个不满足项要一次说清，便于用户理解为什么没收到消息。"""
    result = await _gate(
        logged_in,
        now=datetime(2030, 6, 1, 23, 0, tzinfo=UTC),  # 上海次日 07:00 勿扰
        subject_timezone="Asia/Shanghai",
        user_opted_out=True,
        actor_busy=True,
        channel_can_proactive=False,
    )
    assert result.allowed is False
    assert len(result.reasons) >= 3, f"原因没说全: {result.reasons}"


def test_resolve_timezone_never_uses_server_local() -> None:
    """缺省必须 UTC——不能是 `datetime.now().astimezone()`。"""
    from ling.domains.proactive.service import _resolve_timezone

    tz, explicit = _resolve_timezone(None)
    assert tz is UTC
    assert explicit is False, "没传时区时必须能说出「我不知道」"
    bad, bad_explicit = _resolve_timezone("Not/AZone")
    assert bad is UTC
    assert bad_explicit is False, "无效时区名也不能假装知道"
    good, good_explicit = _resolve_timezone("Asia/Shanghai")
    assert str(good) == "Asia/Shanghai"
    assert good_explicit is True


async def test_cross_day_opportunity_counted_by_send_time(logged_in: TestEnv) -> None:
    """限额按**实际送达时间**计日，不按创建时间。

    具体 bug：23:50 创建、00:10 发送的机会，按 created_at 会被算进前一天，
    于是它在当地新的一天里"不算数"——用户刚过零点就又能收到一条。
    反过来若按 created_at 落在今天而实际昨天送达，又会被错误拒绝。
    """
    from ling.infra.models import ProactiveOpportunity

    # 上海时间 6/2 00:10 送达，但创建于 6/1 23:50
    created = datetime(2030, 6, 1, 15, 50, tzinfo=UTC)   # 上海 6/1 23:50
    sent = datetime(2030, 6, 1, 16, 10, tzinfo=UTC)      # 上海 6/2 00:10
    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id="opp_cross_day",
                actor_id="actor_ling",
                subject_id=logged_in.subject_id,
                kind="greeting",
                reason="测试",
                state="sent",
                expires_at=datetime(2030, 6, 3, 0, 0, tzinfo=UTC),
                created_at=created,
                sent_at=sent,
            )
        )

    # 上海 6/2 09:00 查询：同一当地日已发过一条 → 应拒绝
    query_at = datetime(2030, 6, 2, 1, 0, tzinfo=UTC)
    result = await _gate(
        logged_in, now=query_at, subject_timezone="Asia/Shanghai", daily_limit=1
    )
    assert result.allowed is False, (
        "跨日送达的机会没被算进当地今天——说明仍按 created_at 计日"
    )


async def test_unsent_opportunity_not_counted(logged_in: TestEnv) -> None:
    """标了 sent 但没有送达时间的（历史遗留/异常）不计入额度。

    宁可少算一格，也不要把没送达的当成已消耗用户的耐心。
    """
    from ling.infra.models import ProactiveOpportunity

    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id="opp_no_sent_at",
                actor_id="actor_ling",
                subject_id=logged_in.subject_id,
                kind="greeting",
                reason="测试",
                state="sent",
                expires_at=datetime(2030, 6, 3, 0, 0, tzinfo=UTC),
                created_at=datetime(2030, 6, 2, 0, 30, tzinfo=UTC),
                sent_at=None,
            )
        )
    query_at = datetime(2030, 6, 2, 1, 0, tzinfo=UTC)
    result = await _gate(
        logged_in, now=query_at, subject_timezone="Asia/Shanghai", daily_limit=1
    )
    assert result.allowed is True


async def test_utc_and_shanghai_disagree_on_the_same_moment(logged_in: TestEnv) -> None:
    """同一时刻，UTC 和东八区的勿扰判断**必须不同**。

    这是那个 bug 的直接回归：未传时区时永远按 UTC 算，于是东八区用户的
    「23:00-08:00 勿扰」实际生效在 07:00-16:00——**白天静音、深夜打扰**，
    方向正好反了。

    UTC 00:22 = 上海 08:22：前者在勿扰内，后者已出。两者必须给出不同结论，
    否则说明时区参数根本没被用上。
    """
    moment = datetime(2030, 6, 1, 0, 22, tzinfo=UTC)
    utc_gate = await _gate(logged_in, now=moment, subject_timezone="UTC")
    sh_gate = await _gate(logged_in, now=moment, subject_timezone="Asia/Shanghai")

    assert any("勿扰" in r for r in utc_gate.reasons), (
        f"UTC 00:22 应在勿扰内: {utc_gate.reasons}"
    )
    assert not any("勿扰" in r for r in sh_gate.reasons), (
        f"上海 08:22 应已出勿扰: {sh_gate.reasons}"
    )


# ---------------------------------------------------------------- 渠道能力

# 原来 /v1/proactive 把 `channel_can_proactive` **硬编码成 False**，
# 于是 gate 永远说"当前渠道不支持主动投递"——无论 OneBot 配得多完整，
# 主动联系都不可能出现。前端那一页也就永远是空的。


async def test_proactive_route_consults_real_capability(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """能力自检通过时，"渠道不支持"这条原因**必须消失**。

    断言的是原因消失而不是 gate==True：gate 还受勿扰时段和日限额影响，
    而测试不该依赖"现在几点"。
    """
    from dataclasses import replace

    from ling.capabilities import channels

    # 本用例只看「渠道」这条原因，因此把用户时区固定下来：clean env 下
    # `LING_USER_TIMEZONE` 未设置时 gate 会直接只报时区原因，渠道原因
    # 根本不会被计算。只在本用例临时替换 app 的 Settings，不动 conftest。
    monkeypatch.setattr(
        logged_in.app.state,
        "settings",
        replace(logged_in.settings, user_timezone="Asia/Shanghai"),
    )

    class _Caps:
        enabled = True
        supports_proactive = True
        supports_group = True
        supports_private = True
        supports_media = False
        max_text_length = 4000

    class _Conn:
        def capabilities(self):  # noqa: ANN202
            return _Caps()

    # 打真正的路由接缝：路由直接 import 并调用 proactive_connector_status，
    # 只替换 proactive_connector 对它无效。
    monkeypatch.setattr(
        channels, "proactive_connector_status", lambda: (_Conn(), None)
    )
    r = await logged_in.client.get("/v1/proactive")
    assert r.status_code == 200, r.text
    reasons = r.json()["gate_reasons"]
    assert not any("渠道不支持" in x for x in reasons), (
        f"能力自检通过了却仍报渠道不支持：{reasons}"
    )
    assert not any("连接器不可用" in x for x in reasons), (
        f"渠道可用却仍报连接器不可用：{reasons}"
    )


async def test_proactive_route_reports_missing_channel(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有可用渠道时必须如实说不可达——不能假装能发。"""
    from dataclasses import replace

    from ling.capabilities import channels

    # 本用例断言的是「渠道」这条原因。用户时区未知时 gate 会直接返回
    # LING_USER_TIMEZONE，渠道原因根本不会被计算（那是隔壁用例的场景）。
    # 只在本用例临时替换 app 的 Settings；不动 conftest，也不放宽生产的
    # 「未知时区默认拒绝」策略，避免影响其它用例。
    monkeypatch.setattr(
        logged_in.app.state,
        "settings",
        replace(logged_in.settings, user_timezone="Asia/Shanghai"),
    )
    # 打真正的路由接缝并给出确定的失败原因：路由直接 import 并调用
    # proactive_connector_status，返回 (None, 原因)，原因原样进入 gate_reasons。
    monkeypatch.setattr(
        channels,
        "proactive_connector_status",
        lambda: (None, "连接器不可用（测试模拟无主动渠道）"),
    )
    r = await logged_in.client.get("/v1/proactive")
    assert r.status_code == 200, r.text
    reasons = r.json()["gate_reasons"]
    assert "连接器不可用（测试模拟无主动渠道）" in reasons, (
        f"没说清渠道为什么不可达：{reasons}"
    )


async def test_proactive_separates_implemented_from_enabled(logged_in: TestEnv) -> None:
    """必须分开报「实现了吗」和「开了吗」。

    混成一个布尔会让"能发但没开"和"根本不能发"看起来一样——
    前者是配置选择，后者是能力缺失，用户该做的事完全不同。

    投递现在已经实现，但**默认关闭**。
    """
    body = (await logged_in.client.get("/v1/proactive")).json()
    assert body["delivery_implemented"] is True, "投递已实现却报未实现"
    assert body["delivery_enabled"] is False, "默认必须是关闭"
    assert "默认关闭" in body["note"], f"note 没说清默认关：{body['note']}"


async def test_gate_passing_does_not_imply_delivery(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gate 放行 ≠ 会真的发出——两者必须能被分开读。

    这条防的是：修掉硬编码 False 之后 gate 可能变 True，而用户会以为
    "那她会来找我了"。实际上还要 `proactive_delivery_enabled` 打开。
    """
    from ling.capabilities import channels

    class _Caps:
        enabled = True
        supports_proactive = True
        supports_group = True
        supports_private = True
        supports_media = False
        max_text_length = 4000

    class _Conn:
        def capabilities(self):  # noqa: ANN202
            return _Caps()

    monkeypatch.setattr(channels, "proactive_connector", lambda: _Conn())
    body = (await logged_in.client.get("/v1/proactive")).json()
    # 不管 gate 是真是假，delivery_enabled 都必须是 False（默认关）
    assert body["delivery_enabled"] is False
