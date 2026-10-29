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
        # 拼接构造，避免源码里出现**令牌形状的字符串**：
        # GitHub secret scanning 会对 ghp_ 开头的字面量报警，
        # 这里是纯假的测试输入，不该污染告警。
        "ghp_" + "a" * 24,
        "token " + "84b3453ef8654af2f7f782e1c8f99bff",
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


# ---------------------------------------------------------------- 出网探测


async def test_unreachable_provider_is_reported_not_silent(monkeypatch) -> None:
    """连不上供应商时要喊出来，而不是只让能力悄悄变差。

    实测背景：worker 只挂 `internal`（无外网）时，所有模型调用失败、
    记忆提炼降级成正则，日志里只有一句"降级"，api 那边一切正常——
    几天都没人发现"她怎么记不住事"。
    """
    import logging as _logging
    import socket

    from ling import worker

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("Network is unreachable")

    monkeypatch.setattr(socket, "create_connection", _boom)

    records: list[_logging.LogRecord] = []

    class _Grab(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab()
    worker.logger.addHandler(handler)
    worker.logger.setLevel(_logging.INFO)
    try:
        # 直接测探测函数对"连不上"的处理：用一个必然连不上的地址

        class _P:
            base_url = "http://127.0.0.1:1/v1"

        class _Eff:
            providers = {"p": _P()}

        class _Svc:
            def __init__(self, *_a, **_k) -> None:
                pass

            async def effective(self):  # noqa: ANN201
                return _Eff()


        class _Scope:
            async def __aenter__(self):  # noqa: ANN204
                return None

            async def __aexit__(self, *_exc):  # noqa: ANN002
                return False

        monkeypatch.setattr(worker, "session_scope", lambda: _Scope())
        monkeypatch.setattr("ling.settings.service.ConfigService", _Svc)
        monkeypatch.setattr(
            "ling.infra.security.SecretBox", lambda *_a, **_k: object()
        )
        await worker._probe_provider_reachability()
    finally:
        worker.logger.removeHandler(handler)

    events = [
        r for r in records
        if getattr(r, "event_name", "") == "worker_provider_unreachable"
    ]
    assert events, [r.getMessage() for r in records]
    attrs = events[-1].attributes
    assert attrs["port"] == 1
    assert "hint" in attrs


# ---------------------------------------------------------------- 群聊与主动


def test_worker_emits_structured_attention_and_proactive_events() -> None:
    """群聊轮询与主动联系必须有可检索的事件名。

    这两条是"她为什么在群里不吭声""她今天为什么没主动找我"的唯一答案。
    原来只有自然语言日志，而且"没回"的情况完全不记——而那才是绝大多数。
    """
    import inspect

    from ling import worker

    source = inspect.getsource(worker)
    for event in (
        "attention_window_polled",
        "attention_tick_completed",
        "attention_tick_skipped",
        "proactive_dispatch_skipped",
        "proactive_dispatched",
        "proactive_draft_skipped",
    ):
        assert event in source, f"缺少事件 {event}"

    # 每个窗口都要记，不能只在"决定回应"时记
    assert "result.decision == \"replied\"" in source
    idx = source.index("attention_window_polled")
    assert idx > 0


def test_empty_attention_tick_is_debug_not_info() -> None:
    """空轮询必须降到 DEBUG。

    无条件记录是对的（否则"没轮询"和"轮询了但没窗口到期"分不清），
    但 tick 间隔只有几秒——实测在部署实例上每 6 秒一条 INFO，
    几天就能把日志刷满，真正的信号全被淹掉。
    """
    import inspect

    from ling import worker

    source = inspect.getsource(worker)
    assert "logging.INFO if polls else logging.DEBUG" in source, (
        "空轮询没有降级——会刷屏"
    )


# ---------------------------------------------------------------- 日程解析


@pytest.mark.parametrize(
    "raw",
    [
        '[{"title":"看两章小说","start":"10:30","minutes":60}]',
        '```json\n[{"title":"看两章小说","start":"10:30","minutes":60}]\n```',
        '好的，我安排一下：\n[{"title":"看两章小说","start":"10:30","minutes":60}]\n就这样。',
        '{"title":"看两章小说","start":"10:30","minutes":60}',
    ],
)
def test_parse_plan_json_is_lenient_but_strict(raw: str) -> None:
    """模型输出常见三种干扰：代码块包裹、前后解释、只给一个对象。

    都可以接受，但**条目本身必须合格**——没有 title 或时间格式不对的一律丢。
    宁可今天少做一件，也不要写进一条看不出是什么的日程。
    """
    from ling.worker import parse_plan_json

    plans = parse_plan_json(raw)
    assert len(plans) == 1
    assert plans[0]["title"] == "看两章小说"
    assert plans[0]["start"] == "10:30"
    assert plans[0]["minutes"] == 60


@pytest.mark.parametrize(
    "raw",
    ["", "我没什么安排", "[", "[]", '[{"start":"10:30"}]', '[{"title":"x","start":"十点半"}]'],
)
def test_parse_plan_json_rejects_garbage(raw: str) -> None:
    from ling.worker import parse_plan_json

    assert parse_plan_json(raw) == []


def test_plan_minutes_are_clamped() -> None:
    """模型给离谱时长时收进合理区间，而不是照单全收。"""
    from ling.worker import parse_plan_json

    assert parse_plan_json('[{"title":"x","start":"9:00","minutes":9999}]')[0]["minutes"] == 180
    assert parse_plan_json('[{"title":"x","start":"9:00","minutes":1}]')[0]["minutes"] == 15


def test_at_most_three_plans_a_day() -> None:
    from ling.worker import parse_plan_json

    many = "[" + ",".join(
        f'{{"title":"t{i}","start":"1{i}:00","minutes":30}}' for i in range(6)
    ) + "]"
    assert len(parse_plan_json(many)) == 3


# ---------------------------------------------------------------- 名字前缀


def test_strip_self_prefix_removes_only_her_own_name() -> None:
    """她照抄示例对话格式给自己加的前缀要剥掉；引用别人的话不动。

    实测来源：人格里「示例语气」是一段带名字的对话记录（`她：…`），
    模型照着学，开始在每条消息前加「她：」。这是格式残留，不是她的表达。
    """
    from ling.capabilities.naming import strip_self_prefix

    text = "她：下午维护完就闲啦。\n她：打算去溜溜。\n她：你呢？"
    cleaned, changed = strip_self_prefix(text, "她")
    assert changed is True
    assert cleaned == "下午维护完就闲啦。\n打算去溜溜。\n你呢？"

    # 引用对方的话不能动
    other = "对方：你说的对，我记下了。"
    assert strip_self_prefix(other, "她") == (other, False)

    # 名字为空时不剥任何东西
    assert strip_self_prefix(text, "") == (text, False)


def test_strip_self_prefix_handles_halfwidth_colon_and_spaces() -> None:
    from ling.capabilities.naming import strip_self_prefix

    cleaned, changed = strip_self_prefix("她: 在呢", "她")
    assert changed is True
    assert cleaned == "在呢"
