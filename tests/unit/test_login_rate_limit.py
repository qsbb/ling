"""登录失败限速计数器（纯内存结构，不碰数据库）。

这里测的是 `LoginRateLimiter` 的**策略**：阈值、退避增长、锁定期间不延长、
成功清零、静默衰减、容量上限。API 层的接线（错误形状一致、成功清零）在
`tests/integration/test_login_hardening.py`。
"""

from __future__ import annotations

import time

import pytest

from ling.domains.identity.service import LoginRateLimiter


def _keys(username: str = "alice", ip: str | None = "1.2.3.4") -> tuple[str, ...]:
    return LoginRateLimiter.keys_for(username, ip)


def test_keys_lowercase_username_and_include_ip() -> None:
    """用户名不区分大小写（重名检查就是这么判的），计数也必须一致。"""
    assert _keys("Alice", "1.2.3.4") == ("user:alice", "ip:1.2.3.4")
    assert _keys("ALICE", "1.2.3.4") == _keys("alice", "1.2.3.4")


def test_no_ip_means_only_username_key() -> None:
    assert _keys("alice", None) == ("user:alice",)


def test_threshold_then_lock() -> None:
    """失败 5 次后才锁；前 4 次不锁（正常手滑不该被拦）。"""
    limiter = LoginRateLimiter()
    keys = _keys()
    for attempt in range(1, LoginRateLimiter.FAILURE_THRESHOLD):
        assert limiter.record_failure(keys) == 0.0, f"第 {attempt} 次就锁了"
    assert limiter.retry_after(keys) == 0.0

    locked = limiter.record_failure(keys)
    assert locked == pytest.approx(LoginRateLimiter.BASE_LOCK_SECONDS)
    assert limiter.retry_after(keys) > 0


def test_lock_grows_exponentially_up_to_cap() -> None:
    """过了阈值每多失败一次翻倍，封顶 MAX_LOCK_SECONDS。"""
    limiter = LoginRateLimiter()
    keys = _keys(ip=None)  # 只留用户名键，避免 per-IP 的更高阈值干扰
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD):
        limiter.record_failure(keys)
    assert limiter.retry_after(keys) == pytest.approx(LoginRateLimiter.BASE_LOCK_SECONDS)

    # 模拟"锁定期已过"：清掉锁定时间，否则 record_failure 在锁定期不累计。
    # 第 5 次失败已经给出 60s（上面断言过），循环里从第 6 次开始：
    # 120, 240, 480, 960, 1920, 3600(封顶), 3600, ...
    seen: list[float] = []
    for _ in range(10):
        limiter._locked_until.clear()  # noqa: SLF001 - 测的就是内部时钟推进
        seen.append(limiter.record_failure(keys))
    assert seen[:5] == [
        pytest.approx(LoginRateLimiter.BASE_LOCK_SECONDS * 2.0**i) for i in range(1, 6)
    ]
    assert all(v == pytest.approx(LoginRateLimiter.MAX_LOCK_SECONDS) for v in seen[5:])
    # 单次锁定绝不超过上限
    assert max(seen) == pytest.approx(LoginRateLimiter.MAX_LOCK_SECONDS)


def test_failure_during_lock_does_not_extend_it() -> None:
    """锁定期间继续失败**不延长**窗口。

    否则一次误锁会被后续无关请求越推越远，真用户永远出不来。
    """
    limiter = LoginRateLimiter()
    keys = _keys(ip=None)
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD):
        limiter.record_failure(keys)
    # 比**锁定截止时刻**，不比剩余秒数。
    #
    # `retry_after()` 返回「截止时刻 − 当前时间」，随真实时间**递减**：两次调用
    # 之间隔几十微秒就少几十微秒，而 `pytest.approx` 默认相对容差 1e-6 在截止
    # 时刻约 60 秒时只允许 **60 微秒**的差。串行跑通常刚好在容差内；`pytest-xdist`
    # 并行跑时 CPU 争用把间隔拉大，就会稳定假红（CI 上 py3.12 与 py3.13 同时红过）。
    #
    # 截止时刻是「有没有被延长」的精确答案，与当前时间无关，也不受调度抖动影响
    # ——要测的不变量本来就是它不变。
    deadline = dict(limiter._locked_until)  # noqa: SLF001 - 测的就是内部时钟
    for _ in range(20):
        assert limiter.record_failure(keys) == 0.0
    assert limiter._locked_until == deadline  # noqa: SLF001


def test_success_resets() -> None:
    limiter = LoginRateLimiter()
    keys = _keys()
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD - 1):
        limiter.record_failure(keys)
    limiter.reset(keys)
    assert limiter.retry_after(keys) == 0.0
    # 清零后重新从 1 开始：再失败 4 次仍不锁
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD - 1):
        assert limiter.record_failure(keys) == 0.0


def test_per_ip_threshold_is_higher() -> None:
    """同一个出口 IP 后面可能是整栋楼，per-IP 阈值不能按 5 次。"""
    limiter = LoginRateLimiter()
    ip_only = ("ip:9.9.9.9",)
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD):
        assert limiter.record_failure(ip_only) == 0.0
    assert limiter.retry_after(ip_only) == 0.0
    # 一直累到 per-IP 阈值才锁
    for _ in range(LoginRateLimiter.IP_FAILURE_THRESHOLD - LoginRateLimiter.FAILURE_THRESHOLD - 1):
        limiter.record_failure(ip_only)
    assert limiter.retry_after(ip_only) == 0.0
    assert limiter.record_failure(ip_only) > 0


def test_idle_entries_decay() -> None:
    """静默超过 IDLE_RESET_SECONDS 的计数从零开始，不永久占额度。"""
    limiter = LoginRateLimiter()
    keys = _keys(ip=None)
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD - 1):
        limiter.record_failure(keys)
    # 伪造"很久以前"的记录，再触发一次 prune
    stale_now = time.monotonic() + LoginRateLimiter.IDLE_RESET_SECONDS + 1
    limiter._last_seen[keys[0]] = stale_now - LoginRateLimiter.IDLE_RESET_SECONDS - 5  # noqa: SLF001
    with limiter._lock:  # noqa: SLF001
        limiter._prune(stale_now)  # noqa: SLF001
    assert limiter.record_failure(keys) == 0.0


def test_tracked_keys_are_capped() -> None:
    """键由外部输入决定，不设上限就是内存放大器。"""
    limiter = LoginRateLimiter()
    for i in range(LoginRateLimiter.MAX_TRACKED_KEYS + 200):
        limiter.record_failure((f"user:user{i}",))
    with limiter._lock:  # noqa: SLF001
        assert len(limiter._failures) <= LoginRateLimiter.MAX_TRACKED_KEYS
