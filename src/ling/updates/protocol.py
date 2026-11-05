"""Web 更新通道的共享协议（**纯标准库**）。

宿主执行器（``deploy/update/executor.py``）在宿主上以独立进程运行，不能依赖
API 进程的 venv 或在线服务；API 容器也不接触 Docker。两边唯一的契约就是本
文件描述的**固定目录 + 固定 JSON**：

- ``requests_dir/pending.json``：API 原子写入的一次待执行请求；
- ``status_dir/heartbeat.json``：执行器心跳（含实际部署提交/版本）；
- ``status_dir/current.json``：执行器最近/当前任务（running 或终态）。

安全约束（都在这里集中实现，避免调用方各写一份）：

- 目录/文件不接受符号链接；读取有字节上限；写入用同目录临时文件 + 原子替换；
- 跨进程单飞用 ``fcntl.flock``（API 与执行器可以同时打开同一个锁文件）；
- 状态里的 ``message`` 一律由**固定码表**生成，绝不回传宿主日志、路径或命令。

这里不 import 任何第三方库，也不 import ``ling`` 的其他子模块。
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX；项目只支持 Linux 宿主
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台仅用于文档/静态检查
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = "v1"

# 固定文件名
PENDING_FILE = "pending.json"
CURRENT_FILE = "current.json"
HEARTBEAT_FILE = "heartbeat.json"
LOCK_FILE = ".lock"

# 读取上限：请求/状态文件都很小，超出即视为不可信。
MAX_JSON_BYTES = 16 * 1024
MAX_TEXT_FIELD = 128

REQUEST_ID_PREFIX = "upd_"
REQUEST_ID_RE = re.compile(r"^upd_[0-9a-f]{32}$")
# 只接受严格的稳定发布 tag：v1.2.3（不允许预发布/构建后缀/其它命名）。
STABLE_TAG_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

JOB_STATES = ("queued", "running", "succeeded", "failed", "needs_manual")
ACTIVE_JOB_STATES = ("queued", "running")
TERMINAL_JOB_STATES = ("succeeded", "failed", "needs_manual")

# 备份告警（``current.json`` 里的**可选**字段 ``backup_warnings``）。
#
# 为什么有：媒体归档不完整（``media_state=partial``）时执行器选择「判通过 +
# 告警」，告警原先只落 ``verify-warnings.txt`` 与备份日志，Web 完全看不到。
# 这里把它同时放进任务视图，让升级成功但备份不完整的任务在 API/UI 上显式可见。
#
# 兼容性：纯加法。字段可选、读取侧宽松（缺失/类型错/单条不合法都只丢告警，
# 绝不让整个任务变成不可解析），``SCHEMA_VERSION`` 保持 ``v1``——已装机的
# 执行器固定在 /srv 且不随 Release 更新，升版本会让它直接拒绝加载协议。
MAX_BACKUP_WARNINGS = 8
MAX_BACKUP_WARNING_LEN = 200
# 告警是给 Web 显示的单行摘要：换行/控制字符一律丢弃（防注入多行文本）。
_WARNING_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_backup_warnings(value: object) -> tuple[str, ...]:
    """把任意值收敛成合法告警元组（非字符串/超长/含控制字符的条目**丢弃**）。

    读取侧（``UpdateJob.from_dict``）与写入侧（执行器）共用这一个函数：

    - 读取侧必须宽松：字段缺失 = 旧执行器 = 没有告警，不能报错；单条不合法也
      只丢那一条——把整个任务判为不可解析会让一次成功的升级显示成协议错误。
    - 写入侧由 ``UpdateJob.__post_init__`` 严格校验，执行器先用本函数过一遍就不会
      造出非法 job（写状态失败会连带丢掉真正的升级结果）。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return ()
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > MAX_BACKUP_WARNING_LEN:
            continue
        if _WARNING_CONTROL_RE.search(text):
            continue
        items.append(text)
        if len(items) >= MAX_BACKUP_WARNINGS:
            break
    return tuple(items)


# 心跳里的执行器状态。**必须显式**写进 heartbeat.json：缺失或未知一律按
# ``protocol_unsupported`` fail closed，绝不能让“旧执行器 + 新协议”看起来在线。
EXECUTOR_STATUS_OK = "ok"
EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED = "protocol_unsupported"
EXECUTOR_STATUS_MANUAL = "manual"
EXECUTOR_STATUSES = (
    EXECUTOR_STATUS_OK,
    EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED,
    EXECUTOR_STATUS_MANUAL,
)
# 心跳里报告的 pending 状态（``unknown`` 只用于“没有心跳/解析失败”的合成值）。
PENDING_STATE_ABSENT = "absent"
PENDING_STATE_OK = "ok"
PENDING_STATE_INVALID = "invalid"
PENDING_STATE_UNKNOWN = "unknown"
PENDING_STATES = (
    PENDING_STATE_ABSENT,
    PENDING_STATE_OK,
    PENDING_STATE_INVALID,
    PENDING_STATE_UNKNOWN,
)

# pending.json 不可信时的固定原因码（可观测，但不含路径/原文/命令）。
PENDING_REASON_SCHEMA = "schema_unsupported"
PENDING_REASON_FIELDS = "invalid_fields"
PENDING_REASON_FORMAT = "invalid_format"
PENDING_REASON_FILE = "invalid_file"
PENDING_REASON_READ = "unreadable"

JOB_STAGES = (
    "queued",
    "preflight",
    "backup",
    "fetch",
    "verify_source",
    "build",
    "deploy",
    "verify",
    "complete",
)

# 请求超过这个时间仍未被执行器领取就会被执行器判为 failed（只在 claim 时检查）。
REQUEST_TTL_SECONDS = 15 * 60

# 固定错误码 → 固定文案。API 只按码表生成 message，宿主写什么文本都不转发。
ERROR_MESSAGES: dict[str, str] = {
    # 通道/权限门控
    "disabled": "Web 更新通道未启用",
    "not_owner": "只有所有者可以发起更新",
    "executor_offline": "更新执行器离线",
    "channel_unavailable": "更新通道目录不可用",
    "installed_commit_unknown": "无法确定当前运行提交，暂不允许更新",
    "installed_image_unknown": "无法记录当前运行镜像 ID，未进行任何部署变更",
    "version_drift": "运行版本与执行器记录不一致，请人工核对",
    "update_in_progress": "已有更新正在进行",
    "protocol_unsupported": "更新通道协议不兼容或请求文件无法识别，需要人工处理",
    "manual_required": "更新执行器需要人工恢复（adopt-current）后才能继续",
    "release_unverifiable": "无法核验远端发布目标",
    "not_newer": "当前版本不低于远端发布",
    "release_changed": "远端发布已变化，请重新检查后再提交",
    "installed_version_changed": "当前运行版本已变化，请重新检查后再提交",
    # 执行阶段
    "invalid_request": "更新请求内容无效",
    "config_invalid": "更新执行器配置无效",
    "source_dirty": "已安装源码有未提交改动，拒绝更新",
    "backup_failed": "更新前备份失败，未进行任何部署变更",
    "fetch_failed": "获取目标源码失败，未进行部署变更",
    "target_mismatch": "目标 tag 与提交号不一致，未进行部署变更",
    "source_invalid": "目标发布源码校验失败，未进行部署变更",
    "compose_bind_changed": "目标发布改动了不受支持的挂载配置，未进行部署变更",
    "installed_commit_changed": "运行中的提交与请求不一致，未进行部署变更",
    "build_failed": "构建阶段失败，旧服务未切换",
    "deploy_failed": "部署阶段失败，服务可能已切换，需要人工确认",
    "installer_timeout": "安装超时，服务可能已切换，需要人工确认",
    "deploy_state_unknown": "无法确认服务是否已切换，需要人工确认",
    "verify_failed": "部署后校验失败，需要人工确认",
    "interrupted": "上次执行被中断，需要人工确认",
    "expired": "更新请求已过期，未执行",
}

STAGE_MESSAGES: dict[str, str] = {
    "queued": "已排队，等待更新执行器领取",
    "preflight": "正在检查当前部署",
    "backup": "正在备份数据库与环境文件",
    "fetch": "正在获取目标源码",
    "verify_source": "正在校验目标源码",
    "build": "正在构建新版本",
    "deploy": "正在切换服务",
    "verify": "正在验证新版本",
    "complete": "更新完成",
}


class ProtocolError(Exception):
    """协议数据不可信/不可写。调用方负责翻译成固定码。"""


# ---------------------------------------------------------------- 文本工具


def iso_now() -> str:
    """UTC ISO-8601（秒精度），不用本地时区，避免宿主/容器时区差。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_FIELD:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def seconds_since(value: object, *, now: float | None = None) -> float | None:
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return None
    current = time.time() if now is None else now
    return current - parsed.timestamp()


def new_request_id() -> str:
    """``upd_`` + 32 位十六进制（uuid4），足以做目录内唯一键也便于肉眼分辨。"""
    return REQUEST_ID_PREFIX + uuid.uuid4().hex


def is_request_id(value: object) -> bool:
    return isinstance(value, str) and bool(REQUEST_ID_RE.fullmatch(value))


def is_full_commit(value: object) -> bool:
    return isinstance(value, str) and bool(COMMIT_RE.fullmatch(value))


def parse_stable_tag(value: object) -> tuple[int, int, int] | None:
    """严格 ``vX.Y.Z``；其它命名（``main``/``v1.2``/``v1.2.3-rc.1``）返回 None。"""
    if not isinstance(value, str):
        return None
    match = STABLE_TAG_RE.fullmatch(value)
    if match is None:
        return None
    major, minor, patch = value[1:].split(".")
    return int(major), int(minor), int(patch)


def tag_version(tag: str) -> str | None:
    """``v1.2.3`` → ``1.2.3``；不是严格稳定 tag 时返回 None。"""
    parsed = parse_stable_tag(tag)
    if parsed is None:
        return None
    return f"{parsed[0]}.{parsed[1]}.{parsed[2]}"


def _parse_installed_version(value: object) -> tuple[int, int, int] | None:
    """当前安装版本：允许 ``v`` 前缀与预发布后缀，只取核心三元组。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    core = re.split(r"[-+]", text, maxsplit=1)[0]
    parts = core.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def release_is_newer(current_version: object, tag: object) -> bool | None:
    """稳定 tag 是否比当前运行版本新；任一侧不可识别时返回 None。"""
    left = _parse_installed_version(current_version)
    right = parse_stable_tag(tag)
    if left is None or right is None:
        return None
    return right > left


# ---------------------------------------------------------------- 固定文案


def message_for(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return ERROR_MESSAGES.get(error_code)


def job_message(state: str, stage: str, error_code: str | None) -> str:
    """状态文案只来自固定码表：宿主日志/路径/命令绝不进入 Web。"""
    if state in ("failed", "needs_manual"):
        return message_for(error_code) or ERROR_MESSAGES["deploy_state_unknown"]
    if state == "succeeded":
        return STAGE_MESSAGES["complete"]
    return STAGE_MESSAGES.get(stage, STAGE_MESSAGES["queued"])


# ---------------------------------------------------------------- 文件工具


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _o_cloexec() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def ensure_directory(path: Path, *, mode: int = 0o750, parents: bool = False) -> bool:
    """创建（或确认）普通目录；符号链接一律拒绝。返回是否已存在。"""
    if parents and not directory_ready(path.parent):
        ensure_directory(path.parent, mode=0o755, parents=True)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, mode)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise ProtocolError("无法创建目录") from exc
        os.chmod(path, mode)
        return False
    if stat.S_ISLNK(info.st_mode):
        raise ProtocolError("目录不能是符号链接")
    if not stat.S_ISDIR(info.st_mode):
        raise ProtocolError("路径不是目录")
    return True


def directory_ready(path: Path) -> bool:
    """目录存在、是普通目录、不是符号链接。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def read_json_file(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any] | None:
    """有上限、无跟随符号链接的 JSON 对象读取；任何异常都返回 None。"""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size <= 0 or info.st_size > max_bytes:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _no_follow_flag() | _o_cloexec())
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o640) -> None:
    """同目录临时文件 + fsync + 原子替换；父目录必须是普通目录。"""
    parent = path.parent
    if not directory_ready(parent):
        raise ProtocolError("目标目录不可用")
    data = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    if len(data) > MAX_JSON_BYTES:
        raise ProtocolError("写入内容过大")
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    except OSError as exc:
        raise ProtocolError("无法创建临时文件") from exc
    replaced = False
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            os.close(fd)
            raise
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        replaced = True
        _fsync_directory(parent)
    finally:
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def remove_file(path: Path) -> bool:
    """删除普通文件；符号链接/目录不删。返回是否真的删掉了。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _o_cloexec()
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class FileLock:
    """``fcntl.flock`` 跨进程锁。锁文件必须是普通文件（拒绝符号链接）。"""

    def __init__(self, path: Path, *, blocking: bool = True, timeout: float = 0.0) -> None:
        self._path = path
        self._blocking = blocking
        self._timeout = timeout
        self._fd: int | None = None
        self._locked = False

    def acquire(self) -> bool:
        if fcntl is None:  # pragma: no cover - 非 POSIX
            raise ProtocolError("当前平台不支持文件锁")
        flags = os.O_RDWR | os.O_CREAT | _o_cloexec() | _no_follow_flag()
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ProtocolError("无法打开锁文件") from exc
        non_blocking = fcntl.LOCK_EX | fcntl.LOCK_NB
        if not self._blocking:
            if self._try_flock(fd, non_blocking):
                return True
            os.close(fd)
            return False
        if self._timeout <= 0:
            # 不设超时：真正的阻塞调用，由内核排队。
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                os.close(fd)
                raise ProtocolError("无法获取文件锁") from exc
            self._fd = fd
            self._locked = True
            return True
        deadline = time.monotonic() + self._timeout
        while True:
            if self._try_flock(fd, non_blocking):
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.05)

    def _try_flock(self, fd: int, operation: int) -> bool:
        if fcntl is None:  # pragma: no cover - 非 POSIX
            raise ProtocolError("当前平台不支持文件锁")
        try:
            fcntl.flock(fd, operation)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise ProtocolError("无法获取文件锁") from exc
        self._fd = fd
        self._locked = True
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if self._locked and fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise ProtocolError("锁被占用")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def try_lock(path: Path) -> FileLock | None:
    """非阻塞取锁：拿到返回锁对象，拿不到返回 None。"""
    lock = FileLock(path, blocking=False)
    try:
        return lock if lock.acquire() else None
    except ProtocolError:
        return None


# ---------------------------------------------------------------- 数据模型


def _checked_text(value: object, name: str, *, max_length: int = MAX_TEXT_FIELD) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} 必须是字符串")
    text = value.strip()
    if not text or len(text) > max_length:
        raise ProtocolError(f"{name} 长度不合法")
    return text


@dataclass(frozen=True)
class UpdateJob:
    """给 Web 看的任务视图：只有固定字段，没有路径/命令/日志。

    ``backup_warnings`` 是**可选**的可加字段（协议仍是 ``v1``）：执行器把
    ``verify-warnings.txt`` 里的那几条告警同时写进任务视图，让「升级成功但备份
    不完整」在 API/UI 上可见。旧执行器不写时它是空元组；读取侧永远不会因为它
    报错（见 :func:`sanitize_backup_warnings`）。
    """

    request_id: str
    state: str
    stage: str
    target_tag: str
    target_commit: str
    created_at: str
    updated_at: str
    error_code: str | None = None
    message: str | None = None
    backup_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_request_id(self.request_id):
            raise ProtocolError("request_id 非法")
        if self.state not in JOB_STATES:
            raise ProtocolError("state 非法")
        if self.stage not in JOB_STAGES:
            raise ProtocolError("stage 非法")
        if parse_stable_tag(self.target_tag) is None:
            raise ProtocolError("target_tag 非法")
        if not is_full_commit(self.target_commit):
            raise ProtocolError("target_commit 非法")
        if parse_iso_timestamp(self.created_at) is None:
            raise ProtocolError("created_at 非法")
        if parse_iso_timestamp(self.updated_at) is None:
            raise ProtocolError("updated_at 非法")
        if self.error_code is not None and self.error_code not in ERROR_MESSAGES:
            raise ProtocolError("error_code 非法")
        # 写入侧严格：条数/长度/字符都必须在界内（读取侧已由 sanitize 兜底）。
        if not isinstance(self.backup_warnings, tuple):
            raise ProtocolError("backup_warnings 必须是元组")
        if len(self.backup_warnings) > MAX_BACKUP_WARNINGS:
            raise ProtocolError("backup_warnings 条数过多")
        for note in self.backup_warnings:
            if not isinstance(note, str) or not note.strip():
                raise ProtocolError("backup_warnings 条目非法")
            if len(note) > MAX_BACKUP_WARNING_LEN or _WARNING_CONTROL_RE.search(note):
                raise ProtocolError("backup_warnings 条目非法")

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_JOB_STATES

    def to_dict(self) -> dict[str, Any]:
        # message 永远由固定码表决定，不信任调用方传入的文本。
        return {
            "request_id": self.request_id,
            "state": self.state,
            "stage": self.stage,
            "target_tag": self.target_tag,
            "target_commit": self.target_commit,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "message": job_message(self.state, self.stage, self.error_code),
            # 纯加法：旧 API 读新 current.json 时忽略这个键；旧执行器不写时
            # 新版 from_dict 得到空元组。两种组合都不影响状态机语义。
            "backup_warnings": list(self.backup_warnings),
        }

    @classmethod
    def from_dict(cls, payload: object) -> UpdateJob | None:
        """解析宿主写的状态；不合法字段返回 None（调用方回到安全态）。

        ``backup_warnings`` 例外：它是**可选**字段，缺失/类型不对/单条不合法
        都只收敛成空元组或丢弃那一条，绝不把整个任务判为不可解析。
        """
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                request_id=payload["request_id"],
                state=payload["state"],
                stage=payload["stage"],
                target_tag=payload["target_tag"],
                target_commit=payload["target_commit"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                error_code=payload.get("error_code"),
                backup_warnings=sanitize_backup_warnings(payload.get("backup_warnings")),
            )
        except (KeyError, ProtocolError):
            return None

    def with_progress(self, *, state: str | None = None, stage: str | None = None,
                      error_code: str | None = None,
                      backup_warnings: Sequence[str] | None = None) -> UpdateJob:
        return UpdateJob(
            request_id=self.request_id,
            state=state or self.state,
            stage=stage or self.stage,
            target_tag=self.target_tag,
            target_commit=self.target_commit,
            created_at=self.created_at,
            updated_at=iso_now(),
            error_code=error_code,
            backup_warnings=(
                self.backup_warnings
                if backup_warnings is None
                else sanitize_backup_warnings(list(backup_warnings))
            ),
        )


@dataclass(frozen=True)
class UpdateRequest:
    """API 写入 ``pending.json`` 的请求；宿主负责独立核验后执行。"""

    request_id: str
    target_tag: str
    target_commit: str
    current_version: str
    current_commit: str
    created_at: str

    def __post_init__(self) -> None:
        if not is_request_id(self.request_id):
            raise ProtocolError("request_id 非法")
        if parse_stable_tag(self.target_tag) is None:
            raise ProtocolError("target_tag 非法")
        if not is_full_commit(self.target_commit):
            raise ProtocolError("target_commit 非法")
        _checked_text(self.current_version, "current_version", max_length=64)
        if not is_full_commit(self.current_commit):
            raise ProtocolError("current_commit 非法")
        if parse_iso_timestamp(self.created_at) is None:
            raise ProtocolError("created_at 非法")

    @property
    def expires_at(self) -> str:
        parsed = parse_iso_timestamp(self.created_at)
        assert parsed is not None
        return datetime.fromtimestamp(
            parsed.timestamp() + REQUEST_TTL_SECONDS, tz=UTC
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def expired(self, *, now: float | None = None) -> bool:
        age = seconds_since(self.created_at, now=now)
        return age is not None and age > REQUEST_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": self.request_id,
            "target_tag": self.target_tag,
            "target_commit": self.target_commit,
            "current_version": self.current_version,
            "current_commit": self.current_commit,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> UpdateRequest:
        """严格解析 ``pending.json``；不可信内容抛 ``ProtocolError``。

        这里**不再**用 ``None`` 表示“解析失败”：``None`` 只代表文件不存在。
        文件存在但 schema 不认识、字段非法、带未知字段（schema 漂移）时都必须
        让调用方 fail closed 并留下可观测状态，不能静默当 ``idle``。
        """
        if not isinstance(payload, dict):
            raise ProtocolError(PENDING_REASON_FORMAT)
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ProtocolError(PENDING_REASON_SCHEMA)
        allowed = {
            "schema_version",
            "request_id",
            "target_tag",
            "target_commit",
            "current_version",
            "current_commit",
            "created_at",
            "expires_at",
        }
        if set(payload) - allowed:
            # 多出来的字段说明写入方用了更新的 schema；绝不能猜着执行。
            raise ProtocolError(PENDING_REASON_SCHEMA)
        try:
            return cls(
                request_id=payload["request_id"],
                target_tag=payload["target_tag"],
                target_commit=payload["target_commit"],
                current_version=payload["current_version"],
                current_commit=payload["current_commit"],
                created_at=payload["created_at"],
            )
        except KeyError as exc:
            raise ProtocolError(PENDING_REASON_FIELDS) from exc
        except ProtocolError as exc:
            # __post_init__ 抛出的中文原因不直接进 Web：统一成固定原因码。
            raise ProtocolError(PENDING_REASON_FIELDS) from exc

    def to_job(self) -> UpdateJob:
        return UpdateJob(
            request_id=self.request_id,
            state="queued",
            stage="queued",
            target_tag=self.target_tag,
            target_commit=self.target_commit,
            created_at=self.created_at,
            updated_at=self.created_at,
        )


def build_request(
    *,
    target_tag: str,
    target_commit: str,
    current_version: str,
    current_commit: str,
    request_id: str | None = None,
    created_at: str | None = None,
) -> UpdateRequest:
    return UpdateRequest(
        request_id=request_id or new_request_id(),
        target_tag=target_tag,
        target_commit=target_commit,
        current_version=current_version,
        current_commit=current_commit,
        created_at=created_at or iso_now(),
    )


# ---------------------------------------------------------------- pending 读取


@dataclass(frozen=True)
class PendingFile:
    """``pending.json`` 的读取结果：**区分“没有”与“有但不可信”**。

    ``state`` 只取 ``absent`` / ``ok`` / ``invalid`` / ``unknown``：

    - ``absent``：文件不存在（正常空闲）；
    - ``ok``：``request`` 一定是合法请求；
    - ``invalid``：文件存在但不能当请求用（符号链接/非法 JSON/未知 schema/
      字段非法），``reason`` 是固定原因码；
    - ``unknown``：调用方还没读（例如挂载不可用），调用方必须按 fail closed 处理。
    """

    state: str
    request: UpdateRequest | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in PENDING_STATES:
            raise ProtocolError("pending state 非法")
        if self.state == PENDING_STATE_OK and self.request is None:
            raise ProtocolError("pending ok 但缺少请求")
        if self.state != PENDING_STATE_OK and self.request is not None:
            raise ProtocolError("pending 非 ok 却带请求")


def read_pending_file(path: Path) -> PendingFile:
    """读取请求文件；任何不可信形态都返回 ``invalid``（绝不返回 ``None``）。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return PendingFile(PENDING_STATE_ABSENT)
    except OSError:
        return PendingFile(PENDING_STATE_INVALID, reason=PENDING_REASON_READ)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return PendingFile(PENDING_STATE_INVALID, reason=PENDING_REASON_FILE)
    payload = read_json_file(path)
    if payload is None:
        return PendingFile(PENDING_STATE_INVALID, reason=PENDING_REASON_FORMAT)
    try:
        request = UpdateRequest.from_dict(payload)
    except ProtocolError as exc:
        return PendingFile(PENDING_STATE_INVALID, reason=str(exc) or PENDING_REASON_FIELDS)
    return PendingFile(PENDING_STATE_OK, request)


# ---------------------------------------------------------------- 心跳


@dataclass(frozen=True)
class Heartbeat:
    """心跳的规范化视图（只保留固定字段，不信任任何自由文本）。"""

    status: str
    pending_state: str
    last_seen_at: str | None
    deployed_commit: str | None
    current_version: str | None

    @property
    def protocol_ok(self) -> bool:
        return self.status == EXECUTOR_STATUS_OK

    @property
    def manual_required(self) -> bool:
        return self.status == EXECUTOR_STATUS_MANUAL


def heartbeat_payload(
    *,
    last_seen_at: str,
    deployed_commit: str | None,
    current_version: str | None,
    status: str,
    pending_state: str,
) -> dict[str, Any]:
    """构造 ``heartbeat.json``：状态必须显式，缺失即不可信。"""
    if status not in EXECUTOR_STATUSES:
        raise ProtocolError("心跳状态非法")
    if pending_state not in PENDING_STATES:
        raise ProtocolError("心跳 pending 状态非法")
    commit = deployed_commit if is_full_commit(deployed_commit) else None
    version = current_version if isinstance(current_version, str) and current_version else None
    return {
        "schema_version": SCHEMA_VERSION,
        "last_seen_at": last_seen_at,
        "deployed_commit": commit,
        "current_version": version,
        "status": status,
        "pending_state": pending_state,
    }


def parse_heartbeat(payload: object) -> Heartbeat | None:
    """解析心跳；``None`` 只表示文件不可读/不是 JSON 对象。

    文件可读但 schema 不认识或 ``status`` 缺失/未知时，返回
    ``status=protocol_unsupported``：调用方据此拒绝执行 pending，而不是把它
    当成“在线且空闲”。``status=manual`` 同理用于保留人工门禁。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return Heartbeat(
            status=EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED,
            pending_state=PENDING_STATE_UNKNOWN,
            last_seen_at=None,
            deployed_commit=None,
            current_version=None,
        )
    last_seen_raw = payload.get("last_seen_at")
    last_seen = last_seen_raw if parse_iso_timestamp(last_seen_raw) is not None else None
    deployed = payload.get("deployed_commit")
    deployed_commit = deployed if is_full_commit(deployed) else None
    version_raw = payload.get("current_version")
    current_version = version_raw if isinstance(version_raw, str) and version_raw else None
    raw_status = payload.get("status")
    if raw_status == EXECUTOR_STATUS_OK:
        status = EXECUTOR_STATUS_OK
    elif raw_status == EXECUTOR_STATUS_MANUAL:
        status = EXECUTOR_STATUS_MANUAL
    else:
        status = EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED
    raw_pending = payload.get("pending_state")
    pending_state = raw_pending if raw_pending in PENDING_STATES else PENDING_STATE_UNKNOWN
    return Heartbeat(
        status=status,
        pending_state=pending_state,
        last_seen_at=last_seen,
        deployed_commit=deployed_commit,
        current_version=current_version,
    )


__all__ = [
    "ACTIVE_JOB_STATES",
    "COMMIT_RE",
    "CURRENT_FILE",
    "ERROR_MESSAGES",
    "EXECUTOR_STATUSES",
    "EXECUTOR_STATUS_MANUAL",
    "EXECUTOR_STATUS_OK",
    "EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED",
    "FileLock",
    "HEARTBEAT_FILE",
    "Heartbeat",
    "JOB_STAGES",
    "JOB_STATES",
    "MAX_BACKUP_WARNING_LEN",
    "MAX_BACKUP_WARNINGS",
    "MAX_JSON_BYTES",
    "PENDING_FILE",
    "PENDING_REASON_FIELDS",
    "PENDING_REASON_FILE",
    "PENDING_REASON_FORMAT",
    "PENDING_REASON_READ",
    "PENDING_REASON_SCHEMA",
    "PENDING_STATES",
    "PENDING_STATE_ABSENT",
    "PENDING_STATE_INVALID",
    "PENDING_STATE_OK",
    "PENDING_STATE_UNKNOWN",
    "PROJECT_RE",
    "PendingFile",
    "ProtocolError",
    "REQUEST_TTL_SECONDS",
    "SCHEMA_VERSION",
    "STABLE_TAG_RE",
    "STAGE_MESSAGES",
    "TERMINAL_JOB_STATES",
    "UpdateJob",
    "UpdateRequest",
    "atomic_write_json",
    "build_request",
    "directory_ready",
    "ensure_directory",
    "heartbeat_payload",
    "is_full_commit",
    "is_request_id",
    "iso_now",
    "job_message",
    "message_for",
    "new_request_id",
    "parse_heartbeat",
    "parse_iso_timestamp",
    "parse_stable_tag",
    "read_json_file",
    "read_pending_file",
    "release_is_newer",
    "remove_file",
    "sanitize_backup_warnings",
    "seconds_since",
    "tag_version",
    "try_lock",
]
