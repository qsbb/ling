"""内存日志环形缓冲：容量、顺序、过滤、脱敏、幂等安装。

这一组测的是**缓冲本身**（`ling.logging_setup` 里那几个模块级函数），不是 HTTP。
HTTP 那层（鉴权、参数、响应形状）在 `tests/integration/test_logs_api.py`。

为什么缓冲要有自己的测试：
- 它是固定容量结构，容量失效的症状是**几小时后 OOM**，不会在功能测试里露头；
- 它是一条新的读取路径，脱敏要是漏在这一层，明文就躺在内存里等接口读走；
- 它挂在 root logger 上，多装一次会让每条日志重复出现——这个错误只在
  数字上看得出来（条数翻倍），不会报错。

每条用例都先清空缓冲：缓冲是模块级单例，不隔离的话"最后 N 条"取决于
前面跑过什么，测试会变成顺序相关的。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from ling.logging_setup import (
    DEFAULT_LOG_BUFFER_SIZE,
    MAX_LOG_BUFFER_SIZE,
    REDACTED,
    LogEntry,
    LogRingBuffer,
    RingBufferHandler,
    _buffer_capacity_from_env,
    clear_log_buffer,
    configure_log_buffer,
    install_log_buffer,
    log_buffer_stats,
    recent_logs,
    setup_logging,
)

TEST_LOGGER = "ling.test.logbuffer"


@pytest.fixture(autouse=True)
def _isolated_buffer() -> None:
    """每条用例独占缓冲，并把容量恢复到默认值。"""
    setup_logging(role="test")
    configure_log_buffer(DEFAULT_LOG_BUFFER_SIZE)
    clear_log_buffer()
    yield
    clear_log_buffer()
    configure_log_buffer(DEFAULT_LOG_BUFFER_SIZE)


def _entry(index: int, *, logger: str = TEST_LOGGER) -> LogEntry:
    return LogEntry(
        timestamp=datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC).isoformat(),
        level="info",
        levelno=logging.INFO,
        logger=logger,
        message=f"第 {index} 条",
        event_name="",
        attributes={},
    )


def _emit(level: int, message: str, **kwargs: object) -> None:
    """走真实 logger，证明 handler 真的挂在 root 上（不是直接往缓冲里塞）。"""
    logger = logging.getLogger(TEST_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.log(level, message, **kwargs)


# ---------------------------------------------------------------- 容量与顺序


def test_ring_buffer_keeps_only_the_latest_n() -> None:
    """超过容量只保留最新 N 条，且顺序是写入顺序（最旧在前）。

    **不用 list 装**：这里 5 条只是缩影，真正的场景是「连续跑一周不重启」，
    无限增长的 list 是 OOM，而不是慢一点。
    """
    buffer = LogRingBuffer(capacity=3)
    for index in range(5):
        buffer.append(_entry(index))

    snapshot = buffer.snapshot()
    assert [entry.message for entry in snapshot] == ["第 2 条", "第 3 条", "第 4 条"]
    # 容量是结构性的：maxlen 保证它不会悄悄变长
    assert len(snapshot) <= 3
    assert buffer.capacity == 3


def test_ring_buffer_resize_keeps_the_newest_entries() -> None:
    """缩容丢最旧的、扩容不丢已有的——两种方向都不能丢掉最新的那批。"""
    buffer = LogRingBuffer(capacity=4)
    for index in range(4):
        buffer.append(_entry(index))

    buffer.resize(2)
    assert [entry.message for entry in buffer.snapshot()] == ["第 2 条", "第 3 条"]

    buffer.resize(5)
    assert len(buffer.snapshot()) == 2, "扩容不该凭空补条目"


def test_handler_records_envelope_fields_via_root_logger() -> None:
    """经 root logger 写一条，缓冲里要拿到时间戳、级别、logger 名与关联 ID。

    走真实链路（而不是直接调 `_entry_from_record`）是因为 `trace_id` 这类字段
    靠 `ContextFilter` 从 contextvars 搬进 LogRecord；过滤器没挂上时它们全是空，
    而直接调构造函数的测试看不出来。
    """
    from ling.logging_setup import logging_context

    with logging_context(trace_id="a" * 32, turn_id="turn_x", session_id="sess_x"):
        _emit(logging.WARNING, "缓冲里的第一条", extra={"event_name": "buffer_probe",
                                                        "attributes": {"attempt": 2}})

    entries = recent_logs(limit=10, logger=TEST_LOGGER)
    assert len(entries) == 1, [entry.message for entry in entries]
    entry = entries[0]
    assert entry.message == "缓冲里的第一条"
    assert entry.level == "warning"
    assert entry.levelno == logging.WARNING
    assert entry.logger == TEST_LOGGER
    assert entry.event_name == "buffer_probe"
    assert entry.attributes["attempt"] == 2
    assert entry.trace_id == "a" * 32
    assert entry.turn_id == "turn_x"
    assert entry.session_id == "sess_x"

    # 时间戳是 ISO8601 且带 UTC 偏移（前端要能直接 new Date() 解析）
    stamp = datetime.fromisoformat(entry.timestamp)
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == datetime.now(UTC).utcoffset()


def test_level_filter_is_not_lower_than() -> None:
    """`level=warning` 是「不低于 warning」，不是「等于 warning」。"""
    _emit(logging.DEBUG, "调试")
    _emit(logging.INFO, "信息")
    _emit(logging.WARNING, "警告")
    _emit(logging.ERROR, "错误")
    _emit(logging.CRITICAL, "严重")

    def messages(level: str) -> list[str]:
        return [entry.message for entry in recent_logs(limit=50, level=level, logger=TEST_LOGGER)]

    assert messages("debug") == ["调试", "信息", "警告", "错误", "严重"]
    assert messages("info") == ["信息", "警告", "错误", "严重"]
    assert messages("warning") == ["警告", "错误", "严重"]
    assert messages("error") == ["错误", "严重"]


def test_logger_filter_is_a_prefix_match() -> None:
    """logger 前缀匹配：`ling.test.log` 要能命中 `ling.test.logbuffer`。"""
    _emit(logging.INFO, "本 logger 的")
    logging.getLogger("ling.test.other").warning("别的 logger 的")

    messages = [entry.message for entry in recent_logs(limit=50, logger="ling.test.log")]
    assert messages == ["本 logger 的"]


def test_query_filter_is_case_insensitive_substring() -> None:
    """q 对 message 做**不区分大小写**的子串匹配。"""
    _emit(logging.INFO, "Provider TIMEOUT after 30s")
    _emit(logging.INFO, "连接正常")

    hits = [entry.message for entry in
            recent_logs(limit=50, logger=TEST_LOGGER, query="provider time")]
    assert hits == ["Provider TIMEOUT after 30s"]

    assert [entry.message for entry in
            recent_logs(limit=50, logger=TEST_LOGGER, query="正常")] == ["连接正常"]
    # 不匹配就是空，不是"全部"——静默忽略搜索词会让人以为搜到了
    assert recent_logs(limit=50, logger=TEST_LOGGER, query="不存在的词") == []


def test_limit_returns_the_tail_in_ascending_order() -> None:
    """limit 取的是**最后** N 条，顺序仍是时间正序（最新的在最后）。"""
    for index in range(6):
        _emit(logging.INFO, f"第 {index} 条")

    entries = recent_logs(limit=3, logger=TEST_LOGGER)
    assert [entry.message for entry in entries] == ["第 3 条", "第 4 条", "第 5 条"]


def test_unknown_level_is_an_error_not_a_silent_pass() -> None:
    """不认识的级别要炸，不能当成"不过滤"。

    静默放行的话，前端传错一个值就会把全部日志（含 debug）当 error 显示，
    而使用者以为自己看到的是过滤后的结果。
    """
    with pytest.raises(ValueError):
        recent_logs(level="fatal")


# ------------------------------------------------------------------- 脱敏


def test_message_secrets_are_redacted_before_buffer_keeps_them() -> None:
    """消息里手写的 `api_key=…` 必须在**进缓冲之前**就被抹掉。

    缓冲是另一条读路径（接口会把它交给浏览器），把清洗留到接口层做的话，
    明文会一直躺在进程内存里，任何一个新读缓冲的地方都会把它带走。
    """
    _emit(logging.ERROR, "调用失败 api_key=sk-secret 请检查")
    _emit(logging.INFO, "已保存", extra={"attributes": {"access_token": "tok-plaintext"}})

    entries = recent_logs(limit=10, logger=TEST_LOGGER)
    joined = "\n".join(entry.message for entry in entries)
    assert "sk-secret" not in joined
    assert REDACTED in joined
    assert entries[1].attributes["access_token"] == REDACTED


def test_canary_body_never_reaches_the_buffer() -> None:
    """正文类字段（attributes 里的 content）照旧整值打码。"""
    canary = "她今天说了一句只该存在于对话里的话"
    _emit(logging.INFO, "轮次开始", extra={"attributes": {"content": canary, "turn": 3}})

    entries = recent_logs(limit=10, logger=TEST_LOGGER)
    assert canary not in repr([entry.to_dict() for entry in entries])
    assert entries[0].attributes["turn"] == 3


def test_buffer_exclude_keeps_plaintext_credentials_out_of_the_buffer() -> None:
    """标了 `buffer_exclude` 的记录不进缓冲（首次安装的随机管理员口令）。

    它不是"又一条敏感日志"，而是**唯一一处故意写明文凭据的地方**：stdout
    是安装时人手抄口令的通道，而缓冲会由 `/v1/logs` 交给浏览器。所以断言
    两件事：缓冲里没有它，而且**其它日志照常进缓冲**（豁免不能扩散）。
    """
    secret = "install-time-plaintext-password"
    _emit(
        logging.WARNING,
        f"首次安装：已创建管理员账号，密码 {secret}",
        extra={"buffer_exclude": True},
    )
    _emit(logging.INFO, "紧接着的一行普通日志")

    joined = "\n".join(entry.message for entry in recent_logs(limit=50, logger=TEST_LOGGER))
    assert secret not in joined
    # 豁免只对那一条生效：后面的普通日志必须在
    assert "紧接着的一行普通日志" in joined


def test_buffer_exclude_only_affects_the_ring_buffer_not_stdout() -> None:
    """豁免只挡缓冲，不挡 stdout：`docker logs` 里必须还能看到那条口令。

    这是首次安装唯一的口令交付通道。把它一起挡掉的话，装完的人会拿着一个
    没有密码、又强制改密的服务——比泄漏更糟。
    """
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(TEST_LOGGER)
    grab = _Grab()
    logger.addHandler(grab)
    logger.setLevel(logging.DEBUG)
    try:
        _emit(logging.WARNING, "密码 hunter2", extra={"buffer_exclude": True})
    finally:
        logger.removeHandler(grab)

    assert [record.getMessage() for record in records] == ["密码 hunter2"]
    assert recent_logs(limit=50, logger=TEST_LOGGER) == []


# --------------------------------------------------------- 容量来源与幂等


def test_buffer_size_env_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LING_LOG_BUFFER_SIZE", "17")
    assert _buffer_capacity_from_env() == 17


@pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-5"])
def test_bad_buffer_size_falls_back_to_default_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    """读不懂就**说出来**再回默认值：不能悄悄用默认，让运维以为自己调大了。"""
    monkeypatch.setenv("LING_LOG_BUFFER_SIZE", raw)
    with caplog.at_level(logging.WARNING, logger="ling.logging"):
        capacity = _buffer_capacity_from_env()
    if raw.strip():
        assert capacity == DEFAULT_LOG_BUFFER_SIZE
        assert any("LING_LOG_BUFFER_SIZE" in record.getMessage() for record in caplog.records)
    else:
        # 空串 = 没配，不是错误输入
        assert capacity == DEFAULT_LOG_BUFFER_SIZE
        assert caplog.records == []


def test_buffer_size_env_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限是硬约束：手写 .env 多打一个零不该变成几百 MB 常驻内存。"""
    monkeypatch.setenv("LING_LOG_BUFFER_SIZE", "999999999")
    assert _buffer_capacity_from_env() == MAX_LOG_BUFFER_SIZE


def test_setup_logging_is_idempotent_for_the_ring_buffer() -> None:
    """`setup_logging()` 会被三个入口各调一次，缓冲 handler 只能有一个。

    多装一个的症状是每条日志在界面里出现两次——不报错、也没人会对数字起疑。
    """
    setup_logging(role="api")
    setup_logging(role="api")

    root = logging.getLogger()
    ring_handlers = [handler for handler in root.handlers if isinstance(handler, RingBufferHandler)]
    assert len(ring_handlers) == 1

    clear_log_buffer()
    _emit(logging.INFO, "只该出现一次")
    assert [entry.message for entry in recent_logs(limit=10, logger=TEST_LOGGER)] == ["只该出现一次"]


def test_install_log_buffer_does_not_duplicate_after_handlers_were_removed() -> None:
    """root 上没有 stdout handler 时也要能单独装缓冲（测试/嵌入场景）。"""
    root = logging.getLogger()
    original = list(root.handlers)
    for handler in original:
        root.removeHandler(handler)
    try:
        install_log_buffer()
        install_log_buffer()
        assert len([h for h in root.handlers if isinstance(h, RingBufferHandler)]) == 1
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original:
            root.addHandler(handler)


def test_stats_report_buffered_and_capacity() -> None:
    _emit(logging.INFO, "一条")
    stats = log_buffer_stats()
    assert stats.buffered >= 1
    assert stats.capacity == DEFAULT_LOG_BUFFER_SIZE
