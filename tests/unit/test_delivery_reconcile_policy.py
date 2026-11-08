"""对账策略：什么时候再查一次、什么时候算"过期"。

`operations` 表没有专门的列（加列要迁移，不在本次改动范围），所以
attempts / 下次重试时间 / "过期"标记都存在 `payload` 里。这些纯函数的规则
直接决定"每轮会不会打一遍平台"和"会不会无限扫"，值得单独钉住。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ling.capabilities.delivery import (
    RECONCILE_MAX_AGE_SECONDS,
    RECONCILE_MAX_ATTEMPTS,
    _reconcile_expired,
    reconcile_due,
)


def test_never_reconciled_is_due() -> None:
    assert reconcile_due({}) is True


def test_future_next_retry_is_not_due() -> None:
    """退避期内不许再打平台：否则每轮扫描都会外呼一次。"""
    moment = datetime.now(UTC)
    payload = {"reconcile_next_at": (moment + timedelta(seconds=120)).isoformat()}
    assert reconcile_due(payload, now=moment) is False
    assert reconcile_due(payload, now=moment + timedelta(seconds=121)) is True


def test_expired_is_never_due() -> None:
    """标了 expired 就不再扫——**状态仍然是 uncertain**（不是 failed）。"""
    payload = {"reconcile_expired": True}
    assert reconcile_due(payload, now=datetime.now(UTC)) is False


def test_broken_timestamp_does_not_park_forever() -> None:
    """读不懂的时间戳按"该查了"处理：宁可多查一次，也不能永远不收敛。"""
    assert reconcile_due({"reconcile_next_at": "不是时间"}) is True


def test_naive_timestamp_is_accepted() -> None:
    naive = (datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)).isoformat()
    assert reconcile_due({"reconcile_next_at": naive}) is True


def test_attempt_cap_expires() -> None:
    created = datetime.now(UTC) - timedelta(minutes=5)
    assert _reconcile_expired(created, attempts=RECONCILE_MAX_ATTEMPTS) is True
    assert _reconcile_expired(created, attempts=1) is False


def test_age_cap_expires() -> None:
    old = datetime.now(UTC) - timedelta(seconds=RECONCILE_MAX_AGE_SECONDS + 1)
    assert _reconcile_expired(old, attempts=1) is True


def test_missing_created_at_does_not_expire() -> None:
    assert _reconcile_expired(None, attempts=1) is False
