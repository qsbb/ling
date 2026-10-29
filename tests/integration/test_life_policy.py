"""⑧ 生活与活动 的设置面：生活节奏从设置写进 presence 与 agenda。

这些参数原来散在两个域的模块级常量里（场景 TTL、精力衰减、社交负担、
同时挂几件小事）。抽成策略是因为它们直接决定「像不像人」——精力掉太快
显得虚弱，社交负担恢复太慢会一直不想说话。

三条硬约束（本文件钉住）：

1. 不配置时行为不变（默认值 = 原常量）
2. 部分更新不重置未传字段
3. **作息不在这里**：它按 actor 存在 `actor_routines`，配置里再放一份
   就是第二份权威，两边不一致时没人说得清以谁为准
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.agenda.service import AgendaService
from ling.domains.presence.service import PresenceService
from ling.infra.db import session_scope
from ling.ports.config import LifePolicy
from ling.settings.service import _life_policy
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"

DEFAULTS = {
    "scene_ttl_minutes": 15,
    "energy_decay_per_hour": 1.5,
    "social_load_per_turn": 3.0,
    "social_load_decay_per_hour": 6.0,
    "max_live_plans": 4,
}


async def _publish(env: TestEnv, payload: dict) -> dict:
    draft = await env.client.put("/v1/settings/draft", json=payload)
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text
    return (await env.client.get("/v1/settings/config")).json()


# ---------------------------------------------------------------- 默认与往返


async def test_life_defaults_when_unset(logged_in: TestEnv) -> None:
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["life"] == DEFAULTS


async def test_life_roundtrip(logged_in: TestEnv) -> None:
    body = await _publish(
        logged_in,
        {
            "life": {
                "scene_ttl_minutes": 40,
                "energy_decay_per_hour": 3.0,
                "social_load_per_turn": 8.0,
                "social_load_decay_per_hour": 12.0,
                "max_live_plans": 2,
            }
        },
    )
    assert body["life"] == {
        "scene_ttl_minutes": 40,
        "energy_decay_per_hour": 3.0,
        "social_load_per_turn": 8.0,
        "social_load_decay_per_hour": 12.0,
        "max_live_plans": 2,
    }


async def test_life_partial_update_keeps_other_fields(logged_in: TestEnv) -> None:
    """只改一个字段时其他字段保持——不是被重置成默认。"""
    await _publish(
        logged_in, {"life": {"scene_ttl_minutes": 40, "max_live_plans": 2}}
    )
    body = await _publish(logged_in, {"life": {"max_live_plans": 6}})

    assert body["life"]["max_live_plans"] == 6
    assert body["life"]["scene_ttl_minutes"] == 40, "没传的字段被重置了"
    assert body["life"]["energy_decay_per_hour"] == DEFAULTS["energy_decay_per_hour"]


async def test_memory_and_life_do_not_overwrite_each_other(logged_in: TestEnv) -> None:
    """两个策略段互不干扰：改其中一个不该动到另一个。

    它们共用同一份 payload，合并逻辑最容易在这里串味。
    """
    await _publish(logged_in, {"memory": {"rerank_top_n": 7}, "life": {"max_live_plans": 2}})
    body = await _publish(logged_in, {"life": {"scene_ttl_minutes": 30}})

    assert body["memory"]["rerank_top_n"] == 7, "改生活策略时记忆策略被重置了"
    assert body["life"]["max_live_plans"] == 2
    assert body["life"]["scene_ttl_minutes"] == 30


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scene_ttl_minutes", 0),
        ("scene_ttl_minutes", 5000),
        ("energy_decay_per_hour", -1),
        ("social_load_per_turn", 200),
        ("social_load_decay_per_hour", -0.5),
        ("max_live_plans", 0),
        ("max_live_plans", 99),
    ],
)
async def test_out_of_range_is_rejected(
    logged_in: TestEnv, field: str, value: float
) -> None:
    response = await logged_in.client.put(
        "/v1/settings/draft", json={"life": {field: value}}
    )
    assert response.status_code == 422, response.text


def test_dirty_value_is_clamped_on_read() -> None:
    policy = _life_policy(
        {
            "scene_ttl_minutes": 99999,
            "energy_decay_per_hour": -10,
            "social_load_per_turn": "oops",
            "social_load_decay_per_hour": 1e9,
            "max_live_plans": 0,
        }
    )
    assert policy.scene_ttl_minutes == 1440
    assert policy.energy_decay_per_hour == 0.0
    assert policy.social_load_per_turn == DEFAULTS["social_load_per_turn"]
    assert policy.social_load_decay_per_hour == 100.0
    assert policy.max_live_plans == 1


def test_no_routine_in_policy() -> None:
    """作息不能出现在策略里——它在 actor_routines 表，按 actor 存。

    这条是防回归：将来有人觉得"顺手加个 wake_minute 很方便"，
    就会造出第二份权威。
    """
    fields = set(LifePolicy.__dataclass_fields__)
    assert not fields & {"wake_minute", "rest_minute", "timezone"}, (
        f"生活策略里混入了作息字段：{fields & {'wake_minute', 'rest_minute', 'timezone'}}"
    )


# ---------------------------------------------------------------- 真的用上了


async def test_scene_ttl_honors_policy(logged_in: TestEnv) -> None:
    """场景有效期真的按策略走。"""
    async with session_scope() as session:
        tight = PresenceService(
            session, actor_id=ACTOR, policy=LifePolicy(scene_ttl_minutes=3)
        )
        now = datetime.now(UTC)
        row = await tight.commit_scene(state="focus", now=now)
        delta = (row.valid_until - now).total_seconds() / 60.0
        assert 2.9 <= delta <= 3.1, f"TTL 没生效：{delta} 分钟"


async def test_max_live_plans_honors_policy(logged_in: TestEnv) -> None:
    """同时挂几件小事真的按策略走：超出时 propose 返回 None。"""
    now = datetime.now(UTC)
    async with session_scope() as session:
        tight = AgendaService(
            session, actor_id=ACTOR, policy=LifePolicy(max_live_plans=1)
        )
        first = await tight.propose_plan(
            kind="reading",
            title="读两页书",
            start_at=now + timedelta(minutes=5),
            end_at=now + timedelta(minutes=25),
        )
        assert first is not None
        second = await tight.propose_plan(
            kind="walk",
            title="散步",
            start_at=now + timedelta(minutes=30),
            end_at=now + timedelta(minutes=50),
        )
        assert second is None, "超过 max_live_plans 仍然排上了"


async def test_default_policy_keeps_old_behavior(logged_in: TestEnv) -> None:
    """不传 policy 时就是原来的常量行为——这是"纯增益改动"的证据。"""
    async with session_scope() as session:
        default = PresenceService(session, actor_id=ACTOR)
        now = datetime.now(UTC)
        row = await default.commit_scene(state="rest", now=now)
        delta = (row.valid_until - now).total_seconds() / 60.0
        assert 14.9 <= delta <= 15.1, f"默认值变了：{delta} 分钟"
