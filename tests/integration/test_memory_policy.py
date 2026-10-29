"""④ 记忆与学习 的设置面：策略从设置写进召回与保留。

这一类的参数原来是模块级常量（``RELATIVE_SCORE_FLOOR`` 等）。抽成策略
是因为它们是**用户会想调的东西**——觉得角色记性太好/太差、翻旧账太频繁，
都该能在设置里改，而不是改代码重部署。

本文件钉住四件事：

1. 不配置时行为不变（默认值 = 原常量）
2. 草稿 → 发布 → 生效，整条链路通
3. 只改一个字段时其他字段保持，**不是重置成默认**
4. 非法值在入口被拒，库里出现脏值时读取夹回合法区间
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.ports.config import MemoryPolicy
from ling.settings.service import _memory_policy
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

DEFAULTS = {
    "relative_score_floor": 0.35,
    "rerank_top_n": 20,
    "recency_half_life_days": 120.0,
    "max_retention_days": 3650,
}


async def _publish(env: TestEnv, payload: dict) -> dict:
    draft = await env.client.put("/v1/settings/draft", json=payload)
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text
    return (await env.client.get("/v1/settings/config")).json()


# ---------------------------------------------------------------- 默认与往返


async def test_memory_defaults_when_unset(logged_in: TestEnv) -> None:
    """没配置过时，接口回显的就是原常量值——行为不变。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["memory"] == DEFAULTS


async def test_memory_roundtrip(logged_in: TestEnv) -> None:
    """草稿 → 发布 → 生效。"""
    body = await _publish(
        logged_in,
        {
            "memory": {
                "relative_score_floor": 0.6,
                "rerank_top_n": 8,
                "recency_half_life_days": 30.0,
                "max_retention_days": 180,
            }
        },
    )
    assert body["memory"] == {
        "relative_score_floor": 0.6,
        "rerank_top_n": 8,
        "recency_half_life_days": 30.0,
        "max_retention_days": 180,
    }


async def test_memory_partial_update_keeps_other_fields(logged_in: TestEnv) -> None:
    """只改一个字段时，其他字段保持原值——不是被重置成默认。

    这条最容易写错：合并时把没传的字段当成 None 写回去，
    用户改个重排条数，结果召回下限和保留期一起被重置。
    """
    await _publish(logged_in, {"memory": {"rerank_top_n": 8, "max_retention_days": 180}})
    body = await _publish(logged_in, {"memory": {"rerank_top_n": 12}})

    assert body["memory"]["rerank_top_n"] == 12
    assert body["memory"]["max_retention_days"] == 180, "没传的字段被重置了"
    assert body["memory"]["relative_score_floor"] == DEFAULTS["relative_score_floor"]


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_score_floor", 1.5),
        ("relative_score_floor", -0.1),
        ("rerank_top_n", 0),
        ("rerank_top_n", 1000),
        ("recency_half_life_days", 0),
        ("max_retention_days", 0),
    ],
)
async def test_out_of_range_is_rejected(logged_in: TestEnv, field: str, value: float) -> None:
    """非法值在入口就拒，不进草稿。"""
    response = await logged_in.client.put(
        "/v1/settings/draft", json={"memory": {field: value}}
    )
    assert response.status_code == 422, response.text


def test_dirty_value_is_clamped_on_read() -> None:
    """库里出现脏值（或旧修订手改过）时，读取夹到合法区间。

    不能因为一个越界数字就让记忆检索崩掉，也不能让下限变成负数导致
    "什么都召回"。夹回来是唯一安全的处理。
    """
    policy = _memory_policy(
        {
            "relative_score_floor": 99,
            "rerank_top_n": -5,
            "recency_half_life_days": "oops",
            "max_retention_days": 10**9,
        }
    )
    assert policy.relative_score_floor == 1.0
    assert policy.rerank_top_n == 1
    assert policy.recency_half_life_days == DEFAULTS["recency_half_life_days"]
    assert policy.max_retention_days == 3650


def test_missing_key_falls_back_to_default() -> None:
    assert _memory_policy(None) == MemoryPolicy()
    assert _memory_policy({}) == MemoryPolicy()


# ---------------------------------------------------------------- 真的用上了


async def test_default_retention_honors_policy(logged_in: TestEnv) -> None:
    """策略不只是存在配置里，检索侧真的读它。"""
    async with session_scope() as session:
        loose = MemoryService(session)
        tight = MemoryService(session, policy=MemoryPolicy(max_retention_days=10))

        now = datetime.now(UTC)
        loose_days = (await loose.default_retention(365) - now).days
        tight_days = (await tight.default_retention(365) - now).days

        assert loose_days >= 300, "默认策略不该缩短保留期"
        assert tight_days <= 10, f"策略没生效：{tight_days} 天"
