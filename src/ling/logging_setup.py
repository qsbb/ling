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

## 内存里的最近 N 条（Web 控制台的「日志」页）

容器里的日志走 stdout 交给 Docker 收集，进程自己看不到 `docker logs`。
出问题时要 SSH 到机器上翻容器日志很痛苦，所以这里额外留一份**固定容量**的
环形缓冲，由 `GET /v1/logs` 读给界面。

缓冲里存的是**已经脱敏过的**条目（写入时就清洗），不是原始 `LogRecord`：
一旦明文进了缓冲，任何读缓冲的路径（接口、调试打印、以后可能的导出）都会
把它带走。API 层的鉴权只决定"谁能读"，决定不了"读到什么"。

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
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
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
#
# **这里不猜厂商的密钥前缀。** 曾经有一条 `sk|pk|ghp|...` 的正则，只认
# 下划线不认连字符，于是 `sk-...` 原样进日志；补上连字符之后又漏了
# `sk-proj-...`。根因不是正则写得不全，而是"靠前缀猜密钥"这个方法本身
# 就漏——密钥的格式由厂商和用户决定，下一个服务商换个前缀就绕过去了，
# 自建/中转的 token 更是可以任意命名。
#
# 真正的防线是**按字段名**拦（`SENSITIVE_KEYS` 里的 `api_key` / `token` /
# `authorization` / `secret` …）：它不依赖值长什么样。这里的模式只保留
# 那些**有固定写法、不是某个厂商自定义**的形态——HTTP 鉴权头、URL 里的
# 口令、长 hex 令牌。
_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # postgres://user:pass@host
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # 长 hex（openid/token 常见形态）
)

# 消息里手写的 `key=value` / `"key": "value"`。
#
# 键名复用 `SENSITIVE_KEYS`：同一份名单，不另立一份。长键排前面（`access_token`
# 先于 `token`），否则交替匹配会先吃掉短的那截、在键名中间留下残余。
# 值只吃到空白/引号/分隔符为止：宁可少抹一点，也不要把整段中文说明连锅端掉。
#
# 前视用的是 **ASCII** 字符类而不是 `\w`：`\w` 在 Unicode 下包含汉字，于是
# 「保存token=xxx」这种中英混排会把 `token` 当成词中片段而漏掉——而中文日志
# 恰恰最常长这样。
_INLINE_SECRET = re.compile(
    "(?ix)"
    r"(?<![A-Za-z0-9_.\-])"
    r"(?P<prefix>[\"']?(?:" + "|".join(
        re.escape(key) for key in sorted(SENSITIVE_KEYS, key=len, reverse=True)
    ) + r")[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"',;&]+)"
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
    # **文本内联的 `key=value` 凭据**：日志消息常被拼成 `api_key=sk-xxx`
    # 这种样子，而按字段名脱敏（`redact_mapping`）只看得见 attributes 的键，
    # 看不见消息正文里手写的键。少了这一层，明文会原样进缓冲、被接口读走。
    #
    # 键名复用 `SENSITIVE_KEYS`（同一份名单，不另立一份）：两份名单迟早漂，
    # 而漂出去的那一份正好是没人测的那份。替换时保留键名（`api_key=[REDACTED]`）：
    # 只抹值才既挡得住明文、又保得住"这里本来有个键"的排查线索。
    cleaned = _INLINE_SECRET.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", cleaned
    )
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


# ------------------------------------------------------------------ 内存缓冲

DEFAULT_LOG_BUFFER_SIZE = 500

# 环境变量给个上界：这个值来自运维手写的 .env，写多一个零就是几百 MB 长期驻留。
# 上限本身可调，但必须显式改代码——不在运行时接受任意大数。
MAX_LOG_BUFFER_SIZE = 10_000

# 查询与条目里的级别名只用这四个。日志模块自己还有 CRITICAL 与自定义数值级别，
# 它们照样**会进缓冲**（只是不在这张过滤表里），按 `levelno` 比较即可。
LEVEL_ORDER = {"debug": 10, "info": 20, "warning": 30, "error": 40}


@dataclass(frozen=True)
class LogEntry:
    """缓冲里的一条。**存的是清洗后的副本**，不是 LogRecord 本身。

    持有 LogRecord 有两个问题：它带着 `args`（原始对象）与异常对象，等于把
    明文（以及它指向的整个对象图）留在内存里；而且记录对象的生命周期会因此
    被日志缓冲拖长。
    """

    timestamp: str
    level: str
    levelno: int
    logger: str
    message: str
    event_name: str
    attributes: dict[str, Any]
    trace_id: str | None = None
    turn_id: str | None = None

    @property
    def kind(self) -> str:
        """`runtime` / `behavior` / `audit` / `security`（默认 runtime）。"""
        return _log_kind_of(self)
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """接口形状。空字段省略，与 stdout 的 JSON 信封保持一致。"""
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            # 分类由后端算好下发，前端不要再从 attributes 里猜一遍——
            # 两处各判一次早晚会不一致。
            "kind": self.kind,
        }
        if self.event_name:
            payload["event_name"] = self.event_name
        if self.attributes:
            payload["attributes"] = self.attributes
        for key in ("trace_id", "turn_id", "session_id"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class BufferStats:
    buffered: int
    capacity: int


class LogRingBuffer:
    """固定容量的环形缓冲。

    用 `deque(maxlen=capacity)` 而不是 `list` + 手工裁剪：`maxlen` 的丢弃发生在
    C 层，满了就丢最旧的，容量是**结构上**的保证——不存在"忘了裁剪所以无限
    增长"这条路径。手工裁剪的版本一旦某个分支提前 return 就会漏裁，而 OOM
    是几小时后才发作的那种故障。
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("日志缓冲容量必须 >= 1")
        self._lock = threading.Lock()
        self._items: deque[LogEntry] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._items.maxlen or 0

    def append(self, entry: LogEntry) -> None:
        # 锁必须有：uvicorn 的线程池、worker 里的 to_thread、以及 pytest 的
        # 多线程收集都会并发写同一条 root 日志链路。deque 的 append 本身是
        # 原子的，但"改容量/清空/快照"与它不是一组原子操作。
        with self._lock:
            self._items.append(entry)

    def snapshot(self) -> list[LogEntry]:
        """按写入顺序（时间正序）拷贝一份。"""
        with self._lock:
            return list(self._items)

    def resize(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("日志缓冲容量必须 >= 1")
        with self._lock:
            # 保留最新的 capacity 条：扩容时不能把最旧的丢掉，缩容时才丢最旧的。
            self._items = deque(self._items, maxlen=capacity)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> BufferStats:
        with self._lock:
            return BufferStats(buffered=len(self._items), capacity=self.capacity)


def _buffer_capacity_from_env() -> int:
    """读 `LING_LOG_BUFFER_SIZE`；读不懂就**说**出来，然后回默认值。

    这里不能抛异常：`setup_logging()` 在模块导入期就被调用（`ling.app`），
    一个手写的环境变量打错字不该让整个服务起不来。但也不能悄悄用默认值——
    运维会以为自己把缓冲调大了。
    """
    raw = os.environ.get("LING_LOG_BUFFER_SIZE")
    if raw is None or not raw.strip():
        return DEFAULT_LOG_BUFFER_SIZE
    try:
        value = int(raw.strip())
    except ValueError:
        logging.getLogger("ling.logging").warning(
            "LING_LOG_BUFFER_SIZE=%r 不是整数，按默认值 %d 处理",
            raw,
            DEFAULT_LOG_BUFFER_SIZE,
        )
        return DEFAULT_LOG_BUFFER_SIZE
    if value < 1:
        logging.getLogger("ling.logging").warning(
            "LING_LOG_BUFFER_SIZE=%d 无效（必须 >= 1），按默认值 %d 处理",
            value,
            DEFAULT_LOG_BUFFER_SIZE,
        )
        return DEFAULT_LOG_BUFFER_SIZE
    if value > MAX_LOG_BUFFER_SIZE:
        logging.getLogger("ling.logging").warning(
            "LING_LOG_BUFFER_SIZE=%d 超过上限 %d，按上限处理",
            value,
            MAX_LOG_BUFFER_SIZE,
        )
        return MAX_LOG_BUFFER_SIZE
    return value


# 模块级单例：`setup_logging()` 只装 handler，缓冲本身在这里，测试可以直接写读。
_buffer = LogRingBuffer(_buffer_capacity_from_env())

# 装/重配 handler 的锁。与 `LogRingBuffer._lock` 是两把不同的锁：
# 一把管"缓冲内容"，一把管"root 上有哪些 handler"，混用会留下重入死锁的隐患。
_buffer_lock = threading.Lock()


def _entry_from_record(record: logging.LogRecord) -> LogEntry:
    """把一条 LogRecord 变成**已脱敏**的缓冲条目。

    这是缓冲与 stdout 之外的第二条读路径，所以清洗必须在这里**再做一遍**：
    缓冲里存明文的话，接口、调试打印、以后可能加的导出都会把它带走。
    """
    raw_attributes = getattr(record, "attributes", None)
    if not isinstance(raw_attributes, dict):
        raw_attributes = {}
    safe_attributes, _hit = redact_mapping(raw_attributes)
    return LogEntry(
        timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(),
        level=record.levelname.lower(),
        levelno=record.levelno,
        logger=record.name,
        # 消息正文同样过 `redact_value`：`api_key=sk-…` 这类凭据常常是直接
        # 拼进消息里的，按字段名脱敏看不见它。
        message=redact_value(record.getMessage()),
        event_name=getattr(record, "event_name", None) or "",
        attributes=safe_attributes,
        trace_id=getattr(record, "trace_id", None),
        turn_id=getattr(record, "turn_id", None),
        session_id=getattr(record, "session_id", None),
    )


class RingBufferHandler(logging.Handler):
    """把每条日志追加进内存环形缓冲。

    过滤器挂 `ContextFilter`：trace_id/turn_id/session_id 存在 contextvars 里，
    不经过它就取不到。**不依赖** stdout handler 先把字段写上 record——那会把
    "缓冲里有没有关联 ID"变成 handler 安装顺序的副作用。

    ## 为什么有 `buffer_exclude`

    整个仓库里只有一处**故意**把明文凭据写进日志：首次安装时随机生成的
    管理员口令（`app.py` 的 `_bootstrap`）。那条必须进 stdout——安装的人要
    从 `docker logs` 里抄它才能登进去，**没有别的通道**。

    但缓冲是通到 HTTP 的第二条路：进了缓冲的口令就能被 `/v1/logs` 读出来。
    值层面的脱敏（`_INLINE_SECRET`）认不出它（"密码    abc123" 既不是 `key=value`
    也没有已知前缀），而且把口令格式硬编码进脱敏规则里等于给凭据形状立规矩——
    换个生成方式就漏。

    所以这里给一个**显式**出口：记录上带 `buffer_exclude=True` 就不进缓冲。
    它不改变 stdout 的行为（安装流程照旧），只把"这条不该被 HTTP 读走"写进代码。
    默认值是 False：绝大多数日志都该被看到，豁免必须逐个显式声明。
    """

    def __init__(self, buffer: LogRingBuffer) -> None:
        super().__init__()
        self.buffer = buffer
        self.addFilter(ContextFilter())

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "buffer_exclude", False):
            return
        self.buffer.append(_entry_from_record(record))


def install_log_buffer(capacity: int | None = None) -> RingBufferHandler:
    """在 root 上装环形缓冲 handler。**幂等**：已装过就返回原来那个。

    `app.py` / `worker.py` / `__main__.py` 各自都会调 `setup_logging()`，
    多装一个 handler 会让每条日志在缓冲里出现两次。
    """
    root = logging.getLogger()
    with _buffer_lock:
        if capacity is not None:
            _buffer.resize(capacity)
        for handler in root.handlers:
            if isinstance(handler, RingBufferHandler):
                return handler
        handler = RingBufferHandler(_buffer)
        root.addHandler(handler)
        return handler


def configure_log_buffer(capacity: int) -> None:
    """重设缓冲容量（测试与运维开关用）；已写入的条目保留最新的那些。"""
    with _buffer_lock:
        _buffer.resize(capacity)
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, RingBufferHandler):
                handler.buffer = _buffer
                break


# 日志分流的取值。`runtime` 是**默认**：绝大多数日志没有显式 `log_kind`，
# 它们本来就属于"系统内部在干什么"。
LOG_KINDS: tuple[str, ...] = ("runtime", "behavior", "audit", "security")


def _log_kind_of(entry: LogEntry) -> str:
    """一条日志属于哪一类。没有标注的一律算 `runtime`。"""
    raw = entry.attributes.get("log_kind")
    value = str(raw) if raw else "runtime"
    return value if value in LOG_KINDS else "runtime"


def recent_logs(
    limit: int = 200,
    level: str | None = None,
    logger: str | None = None,
    query: str | None = None,
    kind: str | None = None,
) -> list[LogEntry]:
    """取缓冲里**时间正序**的最后 `limit` 条，按条件过滤。

    过滤在快照上做（不是在缓冲内部做），因为筛选条件来自请求参数：把过滤
    下推到环形缓冲会让"最近 N 条里的 last N"与"N 条里符合条件的 last N"
    混为一谈，而后者才是使用者要的。
    """
    threshold = None
    if level is not None:
        if level not in LEVEL_ORDER:
            # 不认识的级别**报错**，不当作"不过滤"——静默忽略筛选条件会让
            # 使用者以为自己看到的是 error，实际是全部。
            raise ValueError(f"未知日志级别：{level}")
        threshold = LEVEL_ORDER[level]

    needle = query.lower() if query else None
    entries = _buffer.snapshot()
    if threshold is not None:
        entries = [entry for entry in entries if entry.levelno >= threshold]
    if logger:
        # 普通前缀匹配（不是按 `.` 分段）：`logger=ling.api.ac` 也要能命中
        # `ling.api.access`，调用方少打一个字时不该得到空列表。
        entries = [entry for entry in entries if entry.logger.startswith(logger)]
    if kind:
        if kind not in LOG_KINDS:
            # 与 level 同一条原则：不认识的筛选值要报错，不能静默忽略——
            # 否则使用者以为在看"处理日志"，实际拿到的是全部。
            raise ValueError(f"未知日志分类：{kind}")
        entries = [entry for entry in entries if _log_kind_of(entry) == kind]
    if needle:
        entries = [entry for entry in entries if needle in entry.message.lower()]
    if limit < 0:
        raise ValueError("limit 不能为负数")
    return entries[-limit:] if limit else []


def clear_log_buffer() -> None:
    """清空缓冲（测试用；也留着给将来"清屏"按钮）。"""
    _buffer.clear()


def log_buffer_stats() -> BufferStats:
    return _buffer.stats()


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

    # `ContextFilter` 原来只加在"本函数自己新建的那个 stdout handler"上，
    # 于是有一个隐蔽缺口：root 已经有别的 handler 时（uvicorn 先配过、pytest
    # 的捕获器、宿主程序调过 basicConfig），这一支根本不执行，**filter 就没
    # 装上**——contextvars 里的 trace_id / turn_id / session_id 一个都不会进
    # LogRecord。表现是"日志看起来正常，但关联字段全是 null"，而排查恰恰靠
    # 这几个字段。
    #
    # 注意 logging 的语义：`Logger.filter()` 只在**记录被发出的那个 logger**
    # 上执行；记录向上冒泡时，祖先 logger 的 filters **不会**再跑。所以只把
    # filter 挂到 root logger 上是不够的——它只对"直接打在 root 上的记录"生效。
    #
    # 因此两处都要：root logger 一份（覆盖直接打 root 的调用），以及给 root 上
    # **已经存在的每个 handler** 补一份（覆盖子 logger 冒泡上来的记录，不管那个
    # handler 是谁装的）。两者都幂等，重复调用不会把字段塞多次。
    if not any(isinstance(f, ContextFilter) for f in root.filters):
        root.addFilter(ContextFilter())
    for existing_handler in list(root.handlers):
        if not any(isinstance(f, ContextFilter) for f in existing_handler.filters):
            existing_handler.addFilter(ContextFilter())

    # 内存缓冲独立于 stdout handler 判断：已经有了 stdout handler（比如
    # uvicorn 先配过、或测试自己装过）时，仍要确保缓冲存在，否则
    # `/v1/logs` 会安静地永远是空的。函数自身幂等，多调几次不会重复装。
    install_log_buffer()

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
    "DEFAULT_LOG_BUFFER_SIZE",
    "LEVEL_ORDER",
    "MAX_LOG_BUFFER_SIZE",
    "ContextFilter",
    "JsonFormatter",
    "LogEntry",
    "LogRingBuffer",
    "NOISY_LOGGERS",
    "REDACTED",
    "RingBufferHandler",
    "SENSITIVE_KEYS",
    "TextFormatter",
    "Timer",
    "bind_context",
    "clear_log_buffer",
    "configure_log_buffer",
    "current_context",
    "emit_event",
    "install_log_buffer",
    "log_buffer_stats",
    "logging_context",
    "new_trace_id",
    "recent_logs",
    "redact_mapping",
    "redact_value",
    "reset_context",
    "setup_logging",
]
