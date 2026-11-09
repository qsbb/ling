"""只读生活时间轴端点（P3）：受众裁决、双时区、跨日裁剪与四态（真 PostgreSQL）。

这组用例守设计 §7/§9 的可程序化验收：

1. owner 能拿到段、意向与**梦正文**；
2. 非 owner 拿不到梦正文，也看不到 `visibility=actor_private` 的段，
   但能看到"存在私有内容"的标记；
3. 用户时区 ≠ 角色时区时，展示窗口与计划日窗口**两套都对**；
4. 23:30–00:30 的跨日段在两天各自被裁剪，并带 `continues_*`；
5. 空 / 降级 / 失败 / 不可用四态返回不同的 `state`；
6. 不存在的日期返回空态，**不是** 500。

时间一律用**固定日期**（`DAY=2026-11-08`）加本地分钟换算成 UTC，不依赖
"现在几点"；唯一需要真实时钟的是仪表盘摘要（当前段必须覆盖此刻），那里
用运行期算出的计划日。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from ling.domains.agenda.intents import IntentService
from ling.domains.agenda.timeline import local_minute_instant, plan_day_for, plan_day_window
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    DayPlanRow,
    DreamReportRow,
    ScheduleSegmentRow,
)
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
DAY = date(2026, 11, 8)
NEXT_DAY = DAY + timedelta(days=1)
MEMBER = ("member", "member-password")
DREAM_NARRATIVE = "我站在一部只会向上的电梯里，楼层按钮全是别人的名字。"


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """本地墙钟 day+hour:minute 的 UTC 瞬时。"""
    return local_minute_instant(day, hour * 60 + minute, TZ)


async def _insert_plan(
    plan_day: date, *, state: str = "ready", revision: int = 1, error: str | None = None
) -> str:
    """直接落一行计划头（模拟"夜里跑过、停在某个状态"的现场）。"""
    start, end = plan_day_window(plan_day, TZ)
    plan_id = new_id("plan")
    async with session_scope() as session:
        session.add(
            DayPlanRow(
                id=plan_id,
                actor_id=ACTOR,
                plan_day=plan_day,
                timezone=TZ,
                timezone_source="preference",
                timezone_revision=0,
                routine_revision=1,
                window_start_at=start,
                window_end_at=end,
                revision=revision,
                state=state,
                generation_source="model" if state == "ready" else "rule",
                last_error_code=error,
                created_at=start,
                updated_at=start,
            )
        )
    return plan_id


async def _insert_segment(
    plan_id: str,
    *,
    start_at: datetime,
    end_at: datetime,
    title: str,
    is_leaf: bool = True,
    parent_id: str | None = None,
    layer: str = "block",
    anchor_kind: str = "",
    commitment_level: str = "tentative",
    visibility: str | None = None,
    status: str = "current",
) -> str:
    segment_id = new_id("seg")
    async with session_scope() as session:
        session.add(
            ScheduleSegmentRow(
                id=segment_id,
                plan_id=plan_id,
                actor_id=ACTOR,
                parent_id=parent_id,
                parent_key=parent_id or "",
                layer=layer,
                kind="work",
                title=title,
                start_at=start_at,
                end_at=end_at,
                place_id=None,
                commitment_level=commitment_level,
                status=status,
                is_leaf=is_leaf,
                anchor_kind=anchor_kind,
                source="model",
                revision_created=1,
                revision_ended=None,
                source_event_ids=[],
                visibility=visibility,
            )
        )
    return segment_id


async def _insert_activity(
    *, started_at: datetime, end_at: datetime | None = None, state: str = "running"
) -> str:
    activity_id = new_id("act")
    async with session_scope() as session:
        session.add(
            ActivityRow(
                id=activity_id,
                actor_id=ACTOR,
                kind="read",
                state=state,
                place_id=None,
                checkpoint={},
                started_at=started_at,
                ended_at=end_at,
                origin="simulated",
                source_event_ids=[],
                visibility="subject_private",
            )
        )
    return activity_id


async def _insert_dream(*, plan_id: str, sleep_date: date = DAY) -> str:
    dream_id = new_id("dream")
    async with session_scope() as session:
        session.add(
            DreamReportRow(
                id=dream_id,
                actor_id=ACTOR,
                sleep_date=sleep_date,
                status="ready",
                title="只会向上的电梯",
                narrative=DREAM_NARRATIVE,
                mood="uneasy",
                cue_ids=["cue_a", "cue_b"],
                transform_ids=["swap_place"],
                origin="simulated",
                visibility="actor_private",
                plan_id=plan_id,
            )
        )
    return dream_id


async def _insert_intent(*, target_day: date, visibility: str = "subject_private") -> str:
    """造一条**有来源**的未来意向（目标日 = target_day）。"""
    moment = _at(DAY, 12)
    async with session_scope() as session:
        view = await IntentService(session, actor_id=ACTOR).create(
            kind="leisure",
            title="去看展",
            target_start_day=target_day,
            target_end_day=target_day,
            source_kind="user",
            source_ref="turn:intent-1",
            state="planned",
            target_timezone=TZ,
            visibility=visibility,
            now=moment,
        )
    return view.intention_id


async def _seed_day(plan_day: date = DAY) -> dict[str, str]:
    """造一天：父段 + 子段 + 私密段 + 跨日段 + 一个活动 + 一份私密梦 + 一条意向。"""
    plan_id = await _insert_plan(plan_day)
    parent = await _insert_segment(
        plan_id, start_at=_at(plan_day, 13), end_at=_at(plan_day, 17), title="工作", is_leaf=False
    )
    child = await _insert_segment(
        plan_id,
        start_at=_at(plan_day, 14),
        end_at=_at(plan_day, 15),
        title="写代码",
        parent_id=parent,
        layer="hour",
    )
    private = await _insert_segment(
        plan_id,
        start_at=_at(plan_day, 16),
        end_at=_at(plan_day, 17),
        title="私事",
        visibility="actor_private",
    )
    night = await _insert_segment(
        plan_id,
        start_at=_at(plan_day, 23, 30),
        end_at=_at(plan_day + timedelta(days=1), 0, 30),
        title="夜谈",
    )
    activity = await _insert_activity(started_at=_at(plan_day, 13))
    dream = await _insert_dream(plan_id=plan_id, sleep_date=plan_day)
    intention = await _insert_intent(target_day=plan_day + timedelta(days=2))
    return {
        "plan_id": plan_id,
        "parent": parent,
        "child": child,
        "private": private,
        "night": night,
        "activity": activity,
        "dream": dream,
        "intention": intention,
    }


async def _create_member() -> str:
    username, password = MEMBER
    async with session_scope() as session:
        view = await IdentityService(session).create_user(
            username=username, password=password, role="member"
        )
    return view.subject_id


def _member_client(web_env: TestEnv) -> AsyncClient:
    """另开一个客户端：cookie 罐独立，才能同时以两个身份登录。"""
    return AsyncClient(transport=ASGITransport(app=web_env.app), base_url="http://testserver")


async def _login(client: AsyncClient) -> None:
    username, password = MEMBER
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- ① owner


async def test_owner_gets_segments_intents_and_dream_narrative(logged_in: TestEnv) -> None:
    ids = await _seed_day()
    response = await logged_in.client.get(
        "/v1/life/timeline", params={"date": DAY.isoformat()}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # 双时区字段齐全（这个部署里两边都是 Asia/Shanghai，但字段必须都在）
    assert body["display_date"] == DAY.isoformat()
    assert body["display_timezone"] == TZ
    assert body["display_timezone_source"] in {"default", "env", "preference"}
    assert body["actor_timezone"] == TZ
    assert body["plan_day"] == DAY.isoformat()
    assert body["plan_window_start_at"].startswith("2026-11-07T16:00:00")
    assert body["plan_window_end_at"].startswith("2026-11-08T16:00:00")
    assert body["outline_due_at"].startswith("2026-11-07T13:00:00")

    # 计划状态与版本
    assert body["state"] == "ready"
    assert body["plan_state"] == "ready"
    assert body["revision"] == 1
    assert body["generated_at"]
    assert body["horizon_unplanned"] is True
    assert body["has_private_content"] is False

    # 段：父子段都在，私密段 owner 也能看到
    segment_ids = {s["segment_id"] for s in body["segments"]}
    assert {ids["parent"], ids["child"], ids["private"], ids["night"]} <= segment_ids
    child = next(s for s in body["segments"] if s["segment_id"] == ids["child"])
    assert child["parent_id"] == ids["parent"]
    assert child["layer"] == "hour"
    assert child["is_leaf"] is True
    assert child["source"] == "model"
    assert child["commitment_level"] == "tentative"
    assert child["display_start_at"].startswith("2026-11-08T06:00:00")  # 14:00 上海

    # 事实层：活动是**独立**的一层，且证据种类诚实（simulated → clock_only）
    assert len(body["facts"]) == 1
    fact = body["facts"][0]
    assert fact["activity_id"] == ids["activity"]
    assert fact["state"] == "running"
    assert fact["origin"] == "simulated"
    assert fact["evidence_kind"] == "clock_only"
    assert fact["scene_valid_until"] is None

    # 未来 7 天意向（D+2 落在窗口内）
    assert [i["intention_id"] for i in body["intents"]] == [ids["intention"]]
    assert body["intents"][0]["state"] == "planned"
    assert body["intents"][0]["source_kind"] == "user"
    assert body["intents"][0]["target_start_day"] == (DAY + timedelta(days=2)).isoformat()

    # 梦：owner 能看到正文，且明确标 simulated
    assert body["dream"] is not None
    assert body["dream"]["status"] == "ready"
    assert body["dream"]["origin"] == "simulated"
    assert body["dream"]["visibility"] == "actor_private"
    assert body["dream"]["narrative"] == DREAM_NARRATIVE
    assert body["dream"]["restricted"] is False


# ---------------------------------------------------------------- ② 非 owner


async def test_non_owner_cannot_read_private_segment_or_dream(
    web_env: TestEnv, logged_in: TestEnv
) -> None:
    ids = await _seed_day()
    await _create_member()
    async with _member_client(web_env) as client:
        await _login(client)
        response = await client.get("/v1/life/timeline", params={"date": DAY.isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()

    # 私密段不返回正文，但"存在私有内容"要能被界面说出来
    segment_ids = {s["segment_id"] for s in body["segments"]}
    assert ids["private"] not in segment_ids
    assert ids["child"] in segment_ids  # 非私密段照常
    assert body["hidden_segment_count"] == 1
    assert body["has_private_content"] is True

    # 梦：卡片在，正文与标题都不给
    assert body["dream"] is not None
    assert body["dream"]["restricted"] is True
    assert body["dream"]["narrative"] is None
    assert body["dream"]["title"] is None
    assert body["dream"]["status"] == "ready"


# ---------------------------------------------------------------- ③ 双时区


async def test_display_and_plan_windows_differ_when_timezones_differ(
    web_env: TestEnv, logged_in: TestEnv
) -> None:
    await _seed_day()
    await _create_member()
    async with _member_client(web_env) as client:
        await _login(client)
        # 成员把自己的展示时区设成美东；角色规范时区仍是上海（owner 的偏好）
        saved = await client.put(
            "/v1/settings/preferences", json={"timezone": "America/New_York"}
        )
        assert saved.status_code == 200, saved.text
        response = await client.get("/v1/life/timeline", params={"date": DAY.isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["display_timezone"] == "America/New_York"
    assert body["display_timezone_source"] == "preference"
    assert body["actor_timezone"] == TZ
    # 美东 2026-11-08 00:00 = 05:00Z；上海 2026-11-08 00:00 = 前一日 16:00Z
    assert body["display_window_start_at"].startswith("2026-11-08T05:00:00")
    assert body["display_window_end_at"].startswith("2026-11-09T05:00:00")
    assert body["plan_day"] == DAY.isoformat()
    assert body["plan_window_start_at"].startswith("2026-11-07T16:00:00")
    assert body["plan_window_end_at"].startswith("2026-11-08T16:00:00")
    # 展示窗口里能看到上海下午的段（14:00 = 06:00Z），说明两套换算都生效
    assert any(s["title"] == "写代码" for s in body["segments"])


# ---------------------------------------------------------------- ④ 跨日裁剪


async def test_cross_midnight_segment_is_clipped_on_both_days(logged_in: TestEnv) -> None:
    plan_id = await _insert_plan(DAY)
    night = await _insert_segment(
        plan_id,
        start_at=_at(DAY, 23, 30),
        end_at=_at(NEXT_DAY, 0, 30),
        title="夜谈",
    )
    # 次日也有自己的计划行：真实部署里午夜是被切开的，两行各自成日
    await _insert_plan(NEXT_DAY)

    first = (
        await logged_in.client.get("/v1/life/timeline", params={"date": DAY.isoformat()})
    ).json()
    seg_first = next(s for s in first["segments"] if s["segment_id"] == night)
    assert seg_first["plan_day"] == DAY.isoformat()
    assert seg_first["display_start_at"].startswith("2026-11-08T15:30:00")
    assert seg_first["display_end_at"].startswith("2026-11-08T16:00:00")
    assert seg_first["start_at"].startswith("2026-11-08T15:30:00")
    assert seg_first["end_at"].startswith("2026-11-08T16:30:00")
    assert seg_first["continues_from_previous_day"] is False
    assert seg_first["continues_next_day"] is True

    second = (
        await logged_in.client.get(
            "/v1/life/timeline", params={"date": NEXT_DAY.isoformat()}
        )
    ).json()
    assert second["plan_day"] == NEXT_DAY.isoformat()
    seg_second = next(s for s in second["segments"] if s["segment_id"] == night)
    # 段的归属仍是**前一天的计划**：plan_day 来自计划行 join，不从 start_at 反推
    assert seg_second["plan_day"] == DAY.isoformat()
    assert seg_second["display_start_at"].startswith("2026-11-08T16:00:00")
    assert seg_second["display_end_at"].startswith("2026-11-08T16:30:00")
    assert seg_second["continues_from_previous_day"] is True
    assert seg_second["continues_next_day"] is False


# ---------------------------------------------------------------- ⑤ 四态


async def test_four_states_are_distinguishable(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空 / 降级 / 失败 / 不可用 —— 不许都表现成"没有安排"。"""

    async def _fetch(day: date) -> dict:
        response = await logged_in.client.get(
            "/v1/life/timeline", params={"date": day.isoformat()}
        )
        assert response.status_code == 200, response.text
        return response.json()

    # ① 没有计划行
    empty = await _fetch(DAY + timedelta(days=3))
    assert empty["state"] == "empty"
    assert empty["plan_state"] is None
    assert empty["segments"] == []
    assert empty["note"] == "这一天还没有安排"

    # ② 规则兜底
    await _insert_plan(DAY + timedelta(days=1), state="degraded", error="model_unavailable")
    degraded = await _fetch(DAY + timedelta(days=1))
    assert degraded["state"] == "degraded"
    assert degraded["plan_state"] == "degraded"
    assert degraded["last_error_code"] == "model_unavailable"

    # ③ 生成失败（行还是 pending，但已记下原因码）
    await _insert_plan(DAY + timedelta(days=2), state="pending", error="model_unavailable")
    failed = await _fetch(DAY + timedelta(days=2))
    assert failed["state"] == "failed"
    assert failed["plan_state"] == "pending"
    assert failed["last_error_code"] == "model_unavailable"

    # ④ 读取出错
    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("模拟数据库抖动")

    monkeypatch.setattr("ling.api.routes.life_timeline._load_plan", _boom)
    unavailable = await _fetch(DAY)
    assert unavailable["state"] == "unavailable"
    assert unavailable["note"] == "暂时无法读取时间轴"
    # 时区是纯换算，读失败也照常给出——否则界面连"看的是哪一天"都显示不出来
    assert unavailable["display_timezone"] == TZ

    assert len({empty["state"], degraded["state"], failed["state"], unavailable["state"]}) == 4


# ---------------------------------------------------------------- ⑥ 不存在的日期


async def test_unknown_date_returns_empty_state_not_500(logged_in: TestEnv) -> None:
    response = await logged_in.client.get(
        "/v1/life/timeline", params={"date": "1999-01-01"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "empty"
    assert body["segments"] == []
    assert body["facts"] == []
    assert body["intents"] == []
    assert body["dream"] is None
    assert body["display_date"] == "1999-01-01"


# ---------------------------------------------------------------- 仪表盘两块


async def test_dashboard_adds_today_outline_and_intent_teaser(logged_in: TestEnv) -> None:
    """新两块与旧 `agenda` **并列**：旧块的语义一点不改。"""
    now = datetime.now(UTC)
    plan_day = plan_day_for(now, TZ)
    plan_id = await _insert_plan(plan_day)
    # 段的时间**不能**直接取 `now + 1 小时`：跑测试的时刻如果接近角色当地
    # 午夜（例如 23:29），+1 小时就落到"明天"，而 `load_actor_day` 只取
    # **角色当天**的段（窗口是当地零点到零点，见 `plan_day_window`），锚点会
    # 被窗口过滤掉 —— 用例白天绿、深夜红，而产品行为完全正确。
    #
    # 所以锚点必须落在**角色当地**的当天窗口内，而不是按 UTC 零点算。
    from ling.domains.agenda.timeline import plan_day_window

    current_start = now.replace(second=0, microsecond=0)
    await _insert_segment(
        plan_id,
        start_at=current_start,
        end_at=current_start + timedelta(minutes=30),
        title="写代码",
    )
    window_start, window_end = plan_day_window(plan_day, TZ)
    # 锚点要在"此刻之后、当地日结束之前"。优先放当地日的 3/4 处；深夜跑
    # 测试时那个点已经过去了，就贴着"此刻 +2 分钟"放，并把它压短到能塞进
    # 剩余窗口。只有当地日真的只剩不到 3 分钟时才跳过——那时不存在"未来且
    # 仍在今天"的锚点，是**数据上不可能**，不是功能缺失。
    anchor_start = window_start + (window_end - window_start) * 3 / 4
    anchor_start = max(anchor_start, current_start + timedelta(minutes=2))
    remaining = window_end - anchor_start
    if remaining <= timedelta(minutes=1):
        pytest.skip("此刻距当地午夜不足 3 分钟，无法构造同一天内的未来锚点")
    anchor_span = min(timedelta(minutes=30), remaining - timedelta(seconds=30))
    await _insert_segment(
        plan_id,
        start_at=anchor_start,
        end_at=anchor_start + anchor_span,
        title="和人约好",
        anchor_kind="commitment",
        commitment_level="confirmed",
    )
    intention = await _insert_intent(target_day=plan_day + timedelta(days=2))

    body = (await logged_in.client.get("/v1/dashboard")).json()
    sections = body["sections"]
    # 旧块仍在（语义不变：最多几件小事）
    assert "agenda" in sections

    outline = sections["today_outline"]
    assert outline["status"] == "ok", outline
    data = outline["data"]
    assert data["state"] == "ready"
    assert data["plan_day"] == plan_day.isoformat()
    assert data["display_timezone"] == TZ
    assert data["actor_timezone"] == TZ
    relations = [item["relation"] for item in data["items"]]
    assert relations[0] == "now"
    assert "anchor" in relations
    assert data["items"][0]["title"] == "写代码"

    teaser = sections["intent_teaser"]
    assert teaser["status"] == "ok", teaser
    tdata = teaser["data"]
    assert tdata["horizon_unplanned"] is True
    assert [item["intention_id"] for item in tdata["items"]] == [intention]
    assert tdata["items"][0]["source_kind"] == "user"


async def test_dashboard_today_outline_is_empty_without_a_plan(logged_in: TestEnv) -> None:
    body = (await logged_in.client.get("/v1/dashboard")).json()
    outline = body["sections"]["today_outline"]
    assert outline["status"] == "empty", outline
    assert outline["data"]["state"] == "empty"
    assert outline["data"]["items"] == []
    teaser = body["sections"]["intent_teaser"]
    assert teaser["status"] == "empty"
    assert teaser["data"]["horizon_unplanned"] is True


# ---------------------------------------------------------------- 鉴权


async def test_timeline_requires_login(web_env: TestEnv) -> None:
    """未登录一律 401（与其它端点同一条登录依赖，不另开后门）。"""
    response = await web_env.client.get("/v1/life/timeline")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"


async def test_default_date_is_the_viewers_today(logged_in: TestEnv) -> None:
    """不传 `date` = 当前登录者时区的今天（服务端解析，不信浏览器时区）。"""
    response = await logged_in.client.get("/v1/life/timeline")
    assert response.status_code == 200, response.text
    body = response.json()
    today = datetime.now(UTC).astimezone(ZoneInfo(body["display_timezone"])).date()
    assert body["display_date"] == today.isoformat()
    # 此刻必须落在展示窗口内（半开区间 [start, end)）
    start = datetime.fromisoformat(body["display_window_start_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(body["display_window_end_at"].replace("Z", "+00:00"))
    assert start <= datetime.now(UTC) < end
    assert body["state"] in {"empty", "pending", "ready", "degraded", "failed"}


async def test_bad_date_is_rejected_not_silently_ignored(logged_in: TestEnv) -> None:
    """非法日期返回 422，**不**静默当成"没安排"（那会掩盖前端的拼写错误）。"""
    response = await logged_in.client.get(
        "/v1/life/timeline", params={"date": "2026-13-45"}
    )
    assert response.status_code == 422, response.text
