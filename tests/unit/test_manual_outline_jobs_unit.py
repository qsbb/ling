"""手动生成日程的纯逻辑：状态合成、人话、限流、可查询性。

这些不碰数据库，所以能钉住"界面看到什么"：
状态必须能区分在途/成功/降级/失败；原因码不许出现在人话里；限流是请求级的
（与模型的预算闸是两件事）。
"""

from __future__ import annotations

from datetime import date

import pytest

from ling.domains.agenda import outline_jobs as jobs


def test_job_id_shape_matches_automatic_events() -> None:
    """手动事件 id 必须与自动事件**同前缀**，否则"在途检查"看不见它。

    这条以前踩过：``outline:manual:{actor}:{day}`` 与自动的
    ``outline:{actor}:{day}:t0:r1:a1`` 前缀不同，导致当天第二次点击又投一条。
    """
    day = date(2026, 11, 7)
    manual = jobs.job_event_id(actor_id="actor_ling", plan_day=day, run=1)
    automatic = f"outline:actor_ling:{day.isoformat()}:t0:r1:a1"
    assert manual.startswith(jobs.job_like_prefix(actor_id="actor_ling", plan_day=day))
    assert automatic.startswith(jobs.job_like_prefix(actor_id="actor_ling", plan_day=day))
    assert jobs.job_event_id(actor_id="actor_ling", plan_day=day, run=2) != manual


def test_like_prefix_escapes_underscores() -> None:
    """``actor_ling`` 里的下划线不能被当成 LIKE 通配。"""
    from ling.kernel.outbox import like_prefix

    pattern = like_prefix("outline:actor_ling:2026-11-07:")
    assert "\\_" in pattern
    assert pattern.endswith("%")


@pytest.mark.parametrize(
    ("outbox", "plan_state", "revision", "expected"),
    [
        ("pending", None, 0, jobs.JOB_QUEUED),
        ("in_flight", None, 0, jobs.JOB_RUNNING),
        ("done", "ready", 2, jobs.JOB_READY),
        ("done", "degraded", 1, jobs.JOB_DEGRADED),
        # 跑过但一版都没提交：失败，不是"还在生成"
        ("done", None, 0, jobs.JOB_FAILED),
        ("dead", "ready", 1, jobs.JOB_FAILED),
        ("pending", "ready", 1, jobs.JOB_QUEUED),
    ],
)
def test_state_mapping(outbox: str, plan_state: str | None, revision: int, expected: str) -> None:
    assert (
        jobs.job_state(outbox_state=outbox, plan_state=plan_state, plan_revision=revision)
        == expected
    )


def test_notes_are_human_and_never_leak_reason_codes() -> None:
    """原因码只给日志与测试；界面拿到的是人话。"""
    for code in (*jobs.FAILURE_NOTES, None, "never_seen_code"):
        note = jobs.failure_note(code)
        assert note
        assert code is None or code not in note
    assert "用完了" in jobs.failure_note("budget_exhausted")


def test_unchanged_note_explains_the_next_step() -> None:
    note = jobs.note_for("unchanged", already_planned=True)
    assert "已经生成过" in note
    assert "重新" in note, "要告诉用户'想重排该点哪个按钮'"


def test_allowed_plan_days_is_today_and_tomorrow() -> None:
    today = date(2026, 11, 7)
    first, last = jobs.allowed_plan_days(today=today)
    assert (first, last) == (today, today.replace(day=8))


def test_rate_limiter_is_per_key_and_expires() -> None:
    limiter = jobs.OutlineRateLimiter(window_seconds=60.0, limit=2)
    clock = [1000.0]
    limiter._clock = lambda: clock[0]  # noqa: SLF001

    assert limiter.allow("owner") is True
    assert limiter.allow("owner") is True
    assert limiter.allow("owner") is False, "超过 limit 就该拦"
    assert limiter.allow("other") is True, "限流按人算，别人不受影响"

    clock[0] += 61
    assert limiter.allow("owner") is True, "窗口滑过之后应当放行"


def test_rate_limiter_retry_after_is_positive() -> None:
    limiter = jobs.OutlineRateLimiter(window_seconds=60.0, limit=1)
    clock = [500.0]
    limiter._clock = lambda: clock[0]  # noqa: SLF001
    limiter.allow("k")
    assert limiter.retry_after("k") > 0
    assert limiter.retry_after("never") == 0
