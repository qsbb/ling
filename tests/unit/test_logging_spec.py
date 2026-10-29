"""日志规范：结构化信封、关联 ID、**机制上**的脱敏。

这一组测的是"机制"而不是"某次调用有没有记得脱敏"——所以用例直接构造
LogRecord 走 Formatter，以及走真实请求拿响应头。
"""

from __future__ import annotations

import json
import logging

import pytest

from ling.logging_setup import (
    REDACTED,
    JsonFormatter,
    bind_context,
    current_context,
    emit_event,
    logging_context,
    new_trace_id,
    redact_mapping,
    redact_value,
    reset_context,
)

# 用来证明"它绝不该出现在日志里"的探针字符串
CANARY = "今天发生了一件只有我和她知道的事情"


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter(service="ling", version="t", role="api").format(record))


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="ling.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="事件", args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------- 字段名脱敏


def test_sensitive_field_names_are_redacted() -> None:
    """字段名命中黑名单就整值替换——不看内容长什么样。"""
    clean, hit = redact_mapping(
        {"provider_id": "p1", "content": CANARY, "access_token": "abc123"}
    )
    assert clean["provider_id"] == "p1"
    assert clean["content"] == REDACTED
    assert clean["access_token"] == REDACTED
    assert set(hit) == {"content", "access_token"}


def test_canary_never_reaches_the_output() -> None:
    """把探针塞进敏感字段，格式化后的整行里不能出现它。"""
    record = _record(
        event_name="turn_started",
        attributes={"content": CANARY, "prompt": CANARY, "memory": CANARY, "turn": 7},
    )
    line = JsonFormatter(service="ling", version="t", role="api").format(record)
    assert CANARY not in line
    payload = json.loads(line)
    assert payload["attributes"]["turn"] == 7
    assert payload["redaction_applied"] is True
    # 留痕：让人知道"这里本该有内容但被拦了"，而不是以为字段本来就空
    assert "content" in payload["redaction_fields"]


def test_nested_sensitive_keys_are_redacted() -> None:
    """嵌套字典里的敏感字段同样要拦，不能只看第一层。"""
    clean, hit = redact_mapping({"outer": {"content": CANARY, "count": 3}})
    assert clean["outer"]["content"] == REDACTED
    assert clean["outer"]["count"] == 3
    assert "outer.content" in hit


# ---------------------------------------------------------------- 值模式脱敏


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abcdefghijklmnop123456",
        "postgresql+asyncpg://ling:s3cret@db:5432/ling",
        "ghp_abcdefghijklmnopqrstuvwx",
        "token 84b3453ef8654af2f7f782e1c8f99bff",
    ],
)
def test_credential_patterns_in_values_are_redacted(raw: str) -> None:
    """值里夹带的凭据也要拦——不能只靠"别把敏感字段传进来"这一条。"""
    cleaned = redact_value(raw)
    assert REDACTED in cleaned
    assert "s3cret" not in cleaned
    assert "abcdefghijklmnop" not in cleaned


def test_long_values_are_truncated() -> None:
    """超长值截断：日志里出现大段文本通常意味着把正文带进来了。"""
    cleaned = redact_value("啊" * 1000)
    assert len(cleaned) < 400
    assert cleaned.endswith("…")


# ---------------------------------------------------------------- 信封字段


def test_envelope_has_the_observability_fields() -> None:
    token = bind_context(trace_id="a" * 32, turn_id="turn_1")
    try:
        record = _record(event_name="connector_connected", attributes={"channel": "qqbot"})
        # 走真实 filter 才能带上 contextvars 里的字段
        from ling.logging_setup import ContextFilter

        ContextFilter().filter(record)
        payload = _format(record)
    finally:
        reset_context(token)

    for key in (
        "schema_version", "timestamp", "severity_text", "service",
        "process_role", "logger", "event_name", "message",
    ):
        assert key in payload, key
    assert payload["trace_id"] == "a" * 32
    assert payload["turn_id"] == "turn_1"
    assert payload["event_name"] == "connector_connected"


def test_empty_fields_are_omitted() -> None:
    """没有的字段不输出：一屏 20 个 null 会淹掉有用信息。"""
    payload = _format(_record(event_name="ping"))
    assert "trace_id" not in payload
    assert "attributes" not in payload


# ---------------------------------------------------------------- 上下文隔离


def test_context_does_not_leak_between_scopes() -> None:
    """上一个请求的字段绝不能漏到下一个——那是最难查的一类串味。"""
    with logging_context(trace_id="first"):
        assert current_context()["trace_id"] == "first"
    assert "trace_id" not in current_context()

    with logging_context(trace_id="second"):
        assert current_context()["trace_id"] == "second"


def test_event_name_is_stable_not_interpolated() -> None:
    """事件名必须可聚合：不能把动态值拼进去。"""
    record = _record(
        event_name="connector_reconnect_failed",
        attributes={"attempt": 3, "backoff_s": 8},
    )
    payload = _format(record)
    assert payload["event_name"] == "connector_reconnect_failed"
    assert payload["attributes"]["attempt"] == 3
    # 动态信息不该出现在事件名里
    assert "3" not in payload["event_name"]


def test_new_trace_id_is_32_hex() -> None:
    value = new_trace_id()
    assert len(value) == 32
    assert all(c in "0123456789abcdef" for c in value)


def test_emit_event_carries_attributes() -> None:
    """emit_event 是最常用的入口，属性要真的落进 record。"""
    captured: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("ling.test.emit")
    handler = _Grab()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        emit_event(
            logger, logging.INFO,
            event_name="worker_outbox_claimed",
            message="领取了一条待办",
            attributes={"queue_age_ms": 12, "count": 1},
        )
    finally:
        logger.removeHandler(handler)

    assert captured
    assert captured[0].event_name == "worker_outbox_claimed"
    assert captured[0].attributes["queue_age_ms"] == 12


# ---------------------------------------------------------------- 降级可诊断


def test_extract_degradation_records_reason_not_just_type() -> None:
    """降级日志必须带**原因**，不能只记异常类型名。

    实测背景：worker 报"模型提炼不可用，降级为正则: ModelError" ——
    可 `ModelError` 有十几种 kind（超时/限流/密钥无效/职责没绑…），
    类型名一律相同，光看它根本没法排查，只能去读代码。
    """
    import inspect

    from ling import worker

    source = inspect.getsource(worker)
    assert "memory_extract_degraded" in source, "降级事件名缺失"
    assert "error_kind" in source, "降级日志没有记录原因分类"
    # 也不能退回去只打类型名
    assert 'type(exc).__name__)\n        return fallback_candidates' not in source
