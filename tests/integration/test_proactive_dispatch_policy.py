"""主动**真实投递**必须读已发布的 `DialoguePolicy`。

真实缺陷（已代码确证）：`dispatch_pending` 构造 `ProactivityService` 时
没传 `policy`，于是服务退回 `DialoguePolicy()` 的默认值——
`proactive_daily_limit=2`、`quiet_start_minute=quiet_end_minute=0`。
`_in_quiet_hours(start <= end)` 在 start==end 时恒为 False，所以：

- 用户在面板上配的**勿扰时段**对真实发送完全无效（半夜照发）
- 用户配的**日限额**对真实发送完全无效（每天固定最多 2 次）

两个用例都从「已发布设置」出发，走 `dispatch_pending` 这一条**真投递路径**，
而不是直接调 `gate()` 传参——传参版本本来就通得过，那正是缺陷藏起来的原因。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# UTC 15:00 = 东八区 23:00（在 22:00-08:00 勿扰里）
NOW_IN_QUIET = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
# UTC 04:00 = 东八区 12:00（不在勿扰里）
NOW_AWAKE = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)

TZ = "Asia/Shanghai"


async def _publish_dialogue(env: TestEnv, dialogue: dict) -> None:
    """把 ⑤ 对话与主动性 这一段真的发布出去（dispatch 只读已发布快照）。"""
    body = {
        "dialogue": {
            "proactive_delivery_enabled": True,
            **dialogue,
        }
    }
    draft = await env.client.put("/v1/settings/draft", json=body)
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": revision}
    )
    assert published.status_code == 200, published.text


async def _seed_opportunity(
    env: TestEnv,
    *,
    moment: datetime,
    draft_text: str = "在忙吗？",
    state: str = "candidate",
    sent_at: datetime | None = None,
) -> str:
    from ling.infra.db import session_scope
    from ling.infra.models import ProactiveOpportunity
    from ling.infra.security import new_id

    async with session_scope() as session:
        opportunity_id = new_id("opp")
        session.add(
            ProactiveOpportunity(
                id=opportunity_id,
                actor_id=env.settings.actor_id,
                subject_id=env.subject_id,
                kind="check_in",
                reason="connector_of_thought",
                state=state,
                # 有效期必须相对**被测的那一刻**算：dispatch 用显式的
                # `now` 筛 `expires_at > now`，用真实时钟种下的候选会被判过期。
                expires_at=moment + timedelta(hours=2),
                draft_text=draft_text,
                sent_at=sent_at,
            )
        )
        await session.commit()
    return opportunity_id


async def _dispatch(env: TestEnv, *, now: datetime) -> dict:
    """走真实投递路径：配置来自已发布修订，不是测试桩。"""
    from ling.domains.proactive.dispatch import dispatch_pending
    from ling.infra.db import session_scope
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with session_scope() as session:
        config = ConfigService(
            session,
            SecretBox(env.settings),
            test_mode_default=env.settings.test_mode,
            persona_id=env.settings.persona_id,
        )
        return await dispatch_pending(
            session,
            actor_id=env.settings.actor_id,
            connector=None,
            config_reader=config,
            deliver_to_web=True,
            subject_timezone=TZ,
            now=now,
        )


async def _state(opportunity_id: str) -> str:
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import ProactiveOpportunity

    async with session_scope() as session:
        row = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.id == opportunity_id
                )
            )
        ).scalars().first()
    assert row is not None
    return str(row.state)


# ------------------------------------------------------- 勿扰时段（真实缺陷）


async def test_dispatch_respects_published_quiet_hours(web_env, logged_in) -> None:
    """已发布 22:00-08:00 勿扰，此刻 23:00 → **一条都不发**。

    不传 policy 时 `quiet_start == quiet_end == 0`，`_in_quiet_hours` 恒 False，
    于是这条会照发——用户设的勿扰时段等于不存在。
    """
    await _publish_dialogue(
        logged_in,
        {
            "quiet_start_minute": 1320,  # 22:00
            "quiet_end_minute": 480,  # 08:00
            "proactive_daily_limit": 5,
        },
    )
    await logged_in.client.post("/v1/sessions", json={"title": "勿扰"})
    opportunity_id = await _seed_opportunity(web_env, moment=NOW_IN_QUIET, draft_text="这么晚还没睡？")

    stats = await _dispatch(web_env, now=NOW_IN_QUIET)

    assert stats["sent"] == 0, f"勿扰时段内仍投递了: {stats}"
    # 只是"现在还不是时候"，不能标 rejected
    assert await _state(opportunity_id) == "candidate"


async def test_dispatch_lets_the_same_moment_through_outside_quiet(
    web_env, logged_in,
) -> None:
    """同一份配置，中午 12:00 → 正常发出。证明拦下的是勿扰，不是别的。"""
    await _publish_dialogue(
        logged_in,
        {
            "quiet_start_minute": 1320,
            "quiet_end_minute": 480,
            "proactive_daily_limit": 5,
        },
    )
    await logged_in.client.post("/v1/sessions", json={"title": "非勿扰"})
    opportunity_id = await _seed_opportunity(web_env, moment=NOW_AWAKE, draft_text="中午好")

    stats = await _dispatch(web_env, now=NOW_AWAKE)

    assert stats["sent"] == 1, stats
    assert await _state(opportunity_id) == "sent"


# ------------------------------------------------------- 日限额（真实缺陷）


@pytest.mark.parametrize(
    ("already_sent", "expect_sent"),
    [
        (3, 1),  # 3/5 → 还有额度，允许
        (5, 0),  # 5/5 → 到上限，拒绝
    ],
)
async def test_dispatch_uses_published_daily_limit(
    web_env, logged_in, already_sent: int, expect_sent: int,
) -> None:
    """日限额必须用**配置值 5**，不是默认的 2。

    默认 2 时：3 条已发就已经"超限"，于是这条被拒——用户明明配了 5。
    """
    await _publish_dialogue(
        logged_in,
        {
            # 勿扰关掉（0-0），把变量隔离到日限额这一项
            "quiet_start_minute": 0,
            "quiet_end_minute": 0,
            "proactive_daily_limit": 5,
        },
    )
    await logged_in.client.post("/v1/sessions", json={"title": "限额"})

    for index in range(already_sent):
        await _seed_opportunity(
            web_env,
            moment=NOW_AWAKE,
            draft_text=f"已发 {index}",
            state="sent",
            # 记在**用户当地今天**：东八区 12:00 的当天零点之后
            sent_at=NOW_AWAKE - timedelta(minutes=10 * (index + 1)),
        )
    opportunity_id = await _seed_opportunity(
        web_env, moment=NOW_AWAKE, draft_text="还发得出去吗"
    )

    stats = await _dispatch(web_env, now=NOW_AWAKE)

    assert stats["sent"] == expect_sent, f"已发 {already_sent}/5: {stats}"
    assert await _state(opportunity_id) == (
        "sent" if expect_sent else "candidate"
    )
