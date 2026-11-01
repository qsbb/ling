"""统一日志配置：结构化输出、关联 ID 贯穿、**机制上**的脱敏。

## 为什么单独一个模块

API 有三种启动方式：

- 开发：``python -m ling`` → 走 ``ling/__main__.py``
- 容器：``python -m uvicorn ling.app:app`` → 走 ``deploy/entrypoint.sh``
- worker：``python -m ling.worker``

`uvicorn.run(log_level=…)` 只调 **uvicorn 自己**的 logger，不碰 root logger。
只在一处配的话，其它路径会**一条应用日志都没有**（落到 lastResort，只输出
WARNING 以上）。实测踩过：容器部署后 qqbot 网关到底启没启动、为什么没启动，
日志里一个字都看不到。所以三个入口都调这里。

## 三个设计约束

1. **正文永不进日志**。对话内容、记忆内容、画像细节一旦写进日志，就等于
   把它们复制到了另一个长期保存的地方，而且不受记忆的撤回/遗忘机制管辖。
   这件事不能靠"每个调用点自觉"——`SENSITIVE_KEYS` + `JsonFormatter` 里
   的最终拦截，加上测试，才是机制。

2. **关联 ID 要能跨进程**。一次主动联系可能是 worker 决策、api 投递。
   两边各自生成 ID 的话，一条链路会被看成两件无关的事。用 `contextvars`
   承载，`worker` 处理任务时显式恢复上下文。

3. **空字段不输出**。日志是给人看的；一屏 20 个 `null` 会淹掉真正有用的信息。

## 诚实的边界

Formatter 只能拦**字段名**和**已知模式**（Bearer、带密码的连接串等）。
它无法判断一段中文自然语言是不是敏感内容。所以"机制上保证"是三件事合力：
事件字段白名单 + 这里的最终拦截 + 测试里塞 canary 字符串断言不出现。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from ling import __version__

# 字段名命中这些就整值替换。**宁可多拦**：漏一个的成本远大于多打一个 [REDACTED]。
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "content",
        "text",
        "body",
        "prompt",
        "completion",
        "response_body",
        "user_message",
        "actor_message",
        "memory",
        "memories",
        "profile",
        "persona_detail",
        "raw_payload",
        "payload",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "bot_secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "cookie",
        "set-cookie",
        "private_key",
        "database_url",
        "dsn",
        "embedding",
    }
)

REDACTED = "[REDACTED]"

# 值层面的已知模式：字符串里夹带的凭据也要拦
_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"\b(?:sk|pk|ghp|gho|ghs)_[A-Za-z0-9]{12,}"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # postgres://user:pass@host
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # 长 hex（openid/token 常见形态）
)

# 太长的一律截断：日志里出现大段文本通常意味着把正文带进来了
MAX_VALUE_LEN = 300

NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "websockets.client",
    "websockets.server",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "asyncpg",
)

# 默认值用 None：dict 是可变对象，做 ContextVar 默认值会被多个上下文共享，
# 一个请求往里写字段，另一个请求就读得到——那是最难查的一类串味。
_log_context: ContextVar[dict[str, str] | None] = ContextVar(
    "ling_log_context", default=None
)


def _context() -> dict[str, str]:
    return _log_context.get() or {}


def bind_context(**fields: str | None) -> Any:
    """把关联字段绑到当前上下文中，返回可传回 `reset_context` 的 token。"""
    merged = dict(_context())
    for key, value in fields.items():
        if value:
            merged[key] = str(value)
    return _log_context.set(merged)


def reset_context(token: Any) -> None:
    _log_context.reset(token)


def current_context() -> dict[str, str]:
    return dict(_context())


@contextlib.contextmanager
def logging_context(**fields: str | None) -> Iterator[None]:
    token = bind_context(**fields)
    try:
        yield
    finally:
        reset_context(token)


def new_trace_id() -> str:
    """32 位小写十六进制，和 W3C traceparent 的宽度一致。"""
    return os.urandom(16).hex()


def redact_value(value: Any) -> Any:
    """按已知模式清洗一个**值**。"""
    if not isinstance(value, str):
        return value
    cleaned = value
    for pattern in _VALUE_PATTERNS:
        cleaned = pattern.sub(REDACTED, cleaned)
    if len(cleaned) > MAX_VALUE_LEN:
        cleaned = cleaned[:MAX_VALUE_LEN] + "…"
    return cleaned


def redact_mapping(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按字段名清洗一个字典，返回 (清洗后, 被拦下的字段名)。

    第二项用于在日志里留痕：命中时输出 `redaction_applied` 与字段名，
    让人知道"这里本该有内容但被策略拦了"，而不是以为字段本来就空。
    """
    out: dict[str, Any] = {}
    hit: list[str] = []
    for key, value in values.items():
        if key.lower() in SENSITIVE_KEYS:
            out[key] = REDACTED
            hit.append(key)
        elif isinstance(value, dict):
            nested, nested_hit = redact_mapping(value)
            out[key] = nested
            hit.extend(f"{key}.{n}" for n in nested_hit)
        else:
            out[key] = redact_value(value)
    return out, hit


class ContextFilter(logging.Filter):
    """把 contextvars 里的关联字段搬进 LogRecord。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。字段名对齐 OpenTelemetry Logs 的常见约定。"""

    def __init__(self, *, service: str, version: str, role: str) -> None:
        super().__init__()
        self.service = service
        self.version = version
        self.role = role

    def format(self, record: logging.LogRecord) -> str:
        attributes = getattr(record, "attributes", None)
        if not isinstance(attributes, dict):
            attributes = {}
        safe_attributes, hit = redact_mapping(attributes)

        payload: dict[str, Any] = {
            "schema_version": "1",
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity_text": record.levelname,
            "service": self.service,
            "service_version": self.version,
            "process_role": self.role,
            "pid": record.process,
            "logger": record.name,
            "event_name": getattr(record, "event_name", None) or "",
            "message": redact_value(record.getMessage()),
        }
        if hit:
            payload["redaction_applied"] = True
            payload["redaction_fields"] = hit

        # 关联字段**不做值清洗**：trace_id 本身就是 32 位 hex，套用"长 hex
        # 一律打码"的规则会把它抹掉——那等于把用于排查的线索亲手删了。
        # 这几个字段的值要么由我们生成，要么经过格式校验后才写入。
        for key in ("trace_id", "turn_id", "session_id", "account_ref_hash"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value

        if safe_attributes:
            payload["attributes"] = safe_attributes

        if record.exc_info:
            # 只留类型与脱敏后的消息，不输出完整 traceback 里的局部变量
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["error_type"] = exc_type
            payload["error_message"] = redact_value(str(record.exc_info[1] or ""))

        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """开发用的人类可读格式。敏感字段同样被拦。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        attributes = getattr(record, "attributes", None)
        if isinstance(attributes, dict) and attributes:
            safe, hit = redact_mapping(attributes)
            suffix = " ".join(f"{k}={v}" for k, v in safe.items())
            if hit:
                suffix += f" redaction_applied=true fields={','.join(hit)}"
            base = f"{base} {suffix}"
        event = getattr(record, "event_name", None)
        if event:
            base = f"{base} event={event}"
        for key in ("trace_id", "turn_id", "session_id"):
            value = getattr(record, key, None)
            if value:
                base = f"{base} {key}={value}"
        return base


def _service_meta() -> tuple[str, str]:
    service = os.environ.get("LING_LOG_SERVICE", "ling")
    version = os.environ.get("LING_VERSION") or __version__
    return service, version


def setup_logging(role: str | None = None) -> None:
    """装 root handler。**幂等**：已有 handler 就不重复加。

    `role` 区分 api / worker，方便在多进程部署里筛日志。
    """
    level_name = os.environ.get("LING_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("LING_LOG_FORMAT", "").lower()
    if not fmt:
        # 容器里默认 JSON（便于采集），本地默认可读文本
        fmt = "json" if os.environ.get("LING_ENV") == "production" else "text"
    process_role = role or os.environ.get("LING_PROCESS_ROLE", "api")

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(ContextFilter())
        service, version = _service_meta()
        if fmt == "json":
            handler.setFormatter(
                JsonFormatter(service=service, version=version, role=process_role)
            )
        else:
            handler.setFormatter(
                TextFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
        root.addHandler(handler)

    root.setLevel(level)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def emit_event(
    logger: logging.Logger,
    level: int,
    *,
    event_name: str,
    message: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    """发一条结构化事件。

    事件名用稳定的 `snake_case`（`connector_connected`），动态信息全部放
    attributes。事件名是拿来检索聚合的，所以不能把变量拼进去——
    否则每个 trace_id 都会变成一个新事件名，聚合就废了。
    """
    logger.log(
        level,
        message,
        extra={"event_name": event_name, "attributes": attributes or {}},
    )


class Timer:
    """量一段代码耗时，用于 duration_ms 字段。"""

    __slots__ = ("_start",)

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 1)


__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "NOISY_LOGGERS",
    "REDACTED",
    "SENSITIVE_KEYS",
    "TextFormatter",
    "Timer",
    "bind_context",
    "current_context",
    "emit_event",
    "logging_context",
    "new_trace_id",
    "redact_mapping",
    "redact_value",
    "reset_context",
    "setup_logging",
]
