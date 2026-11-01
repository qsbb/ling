#!/usr/bin/env python3
"""ling 宿主更新执行器（**纯标准库**，只跑在宿主机上）。

职责边界
========

API 容器没有 Docker socket，也不执行 git/备份/部署。它只做两件事：

- 把一条通过二次核验的请求原子写进 ``requests_dir/pending.json``；
- 读取 ``status_dir`` 里的心跳与任务状态给 Web 看。

本脚本运行在宿主机（建议 root 的 systemd 服务），独立完成：

1. 取宿主私有锁，保证同一时刻只有一个任务；
2. 校验当前实际运行提交/版本与请求一致，且已安装 checkout 没有未提交改动；
3. 备份数据库（pg_dump + 墓碑状态 + alembic 版本）与原 env 文件；
4. 从**固定仓库**取 tag 源码到宿主私有 release 树，独立核验 commit 与版本号；
5. 调 ``<release>/deploy/install.sh`` 构建/部署，并用安装阶段标记判断
   “服务到底有没有切换”；
6. 部署后验证 readyz、实际运行 commit 与包版本，写持久 installed 记录。

刻意不做的事：不 checkout/stash/reset/clean 本仓库，不删除旧部署树/镜像/数据，
不自动回滚数据库，不把宿主管道日志发给 Web，不执行候选 release 里的 Python。

使用方式见同目录 README.md；示例配置见 ``config.example.toml``。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

# 固定仓库：不接受配置/请求里的 URL。tag 由 API 按严格 vX.Y.Z 提供。
REPO_URL = "https://github.com/qsbb/ling.git"
REPO_SLUG = "qsbb/ling"
# 宿主自己核验 Release：固定 host，不接受请求里的 URL，不跟随重定向，
# 不使用环境代理（HTTP(S)_PROXY 只影响 urllib 的默认 opener）。
GITHUB_API_ROOT = "https://api.github.com"
CONFIG_SECTION = "update"
DEFAULT_CONFIG_PATH = "/etc/ling-update.toml"
DEFAULT_POLL_SECONDS = 5
DEFAULT_MAX_SECONDS = 3600
# 备份超时。注意这个上限现在包住的是**一次完整备份**，而不只是 ``pg_dump``：
# ``deploy/backup/backup.sh`` 会把 ``app-data:/data`` 里的 ``media`` 整目录
# 用 ``tar -cf -`` 流式打包（图片/语音，可能几十 GB、也可能在慢盘上）。
# 因此：
#   - 媒体卷很大时，这个 30 分钟可能不够 → 超时 = ``backup_failed`` → 升级被
#     阻断。这是**有意的 fail closed**（不丢数据、不半路进入构建），代价是运维
#     要重跑或调大这个值；调大是安全的，调小到"媒体还没流完就构建"才危险。
#     要缩短本次打包时间只能缩小 /data/media、调大本值或换更快存储——减少历史
#     备份份数不会让本次 tar 变快。
#   - 每日 cron 备份默认**不删**历史全量媒体副本。``backup.sh`` 提供默认关闭的
#     轮转（``--prune-keep`` / ``--prune-days`` / ``--dry-run``）；执行器调用它时
#     只传位置参数、**不传任何 ``--prune-*``**，要由 cron 行或人工显式开启，
#     空间由运维估算并轮转，见 docs/DEPLOYMENT.md §8.1。
BACKUP_TIMEOUT_SECONDS = 1800
FETCH_TIMEOUT_SECONDS = 600
QUICK_TIMEOUT_SECONDS = 120
VERIFY_TIMEOUT_SECONDS = 90
GITHUB_TIMEOUT_SECONDS = 15
MAX_GITHUB_BODY_BYTES = 256 * 1024
MAX_CONFIG_JSON_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
PHASE_FILE_NAME = "install-phase.json"
INSTALL_PHASES = ("build", "deploy", "verify", "complete")
# ``deploy/backup/backup.sh`` 写进 manifest 的封闭取值。执行器按它们做枚举/自洽
# 校验：脚本回归或被手工改过的 manifest 出现别的值一律 backup_failed（fail closed）。
BACKUP_KEYS_ROOT_COMBOS = (
    ("present", "file"),
    ("absent", "env"),
    ("absent", "none"),
)
BACKUP_MEDIA_STATES = ("present", "partial", "absent")
# 与 API 共享的 pending 所有权锁：host claim/clear 与 API submit 必须同一把锁。
REQUESTS_LOCK_TIMEOUT_SECONDS = 10.0
# api 容器内监听端口（deploy/compose.yaml 的 expose: 8080）。逐副本就绪探针在
# 容器内直接访问它，不经过 edge；edge 的 301 只代表“有东西在 80 上”。
API_READYZ_PORT = 8080
# 宿主 edge 探活：只接受 2xx；2s × 60 ≈ 2 分钟（与安装/滚动更新上限相当）。
EDGE_READY_ATTEMPTS = 60
EDGE_READY_INTERVAL_SECONDS = 2.0
API_READYZ_PATH = "/readyz"
# 需要记录旧镜像 ID 的服务（回退必须两个都有，缺一不可）。
IMAGE_SERVICES = ("api", "worker")
# 允许 release 改变的挂载目标：前端目录由本次 release 提供，更新通道目录由
# 配置给出绝对来源。证书/data/nginx 配置等其它 bind 一律固定为当前运行来源。
WEB_MOUNT_TARGET = "/app/web"
UPDATE_MOUNT_TARGETS = ("/run/ling-update/requests", "/run/ling-update/status")
EDGE_CONF_TARGET = "/etc/nginx/conf.d/default.conf"
EDGE_CERTS_TARGET = "/etc/nginx/certs"
# 这些 bind 的宿主来源若不是"运行数据"以外的绝对路径，就必须逐字节保持不变。
API_UID = 10001
API_GID = 10001
CAPTURE_LIMIT = 64 * 1024
MAX_REPLICAS = 32
# 逐副本就绪探针：在**容器内**访问 127.0.0.1:{port}/readyz，只接受 2xx。
# urllib 的默认 opener 会跟随 3xx（TLS edge 会把 80 上的 /readyz 301 到 https），
# 那只能证明 edge 在服务，不能证明副本 ready；这里显式拒绝重定向并禁用代理。
READYZ_SCRIPT = (
    "import json, urllib.error, urllib.request\n"
    "class _NoRedirect(urllib.request.HTTPRedirectHandler):\n"
    "    def redirect_request(self, req, fp, code, msg, headers, newurl):\n"
    "        raise urllib.error.HTTPError(req.full_url, code, 'redirect refused', headers, fp)\n"
    "opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}), _NoRedirect())\n"
    "url = 'http://127.0.0.1:{port}{path}'\n"
    "code = None\n"
    "try:\n"
    "    with opener.open(url, timeout=3) as response:\n"
    "        code = getattr(response, 'status', 0)\n"
    "except urllib.error.HTTPError as exc:\n"
    "    code = exc.code\n"
    "except Exception:\n"
    "    code = 0\n"
    "print(json.dumps({{'status': code}}))\n"
    "raise SystemExit(0 if isinstance(code, int) and 200 <= code < 300 else 1)\n"
)
# 运行容器核对：image / 挂载 / 网络别名都从 docker inspect 的结构化输出里取，
# 绝不把 stderr 当 stdout 解析。
INSPECT_IMAGE_FORMAT = "{{.Image}}"
INSPECT_MOUNTS_FORMAT = "{{json .Mounts}}"
INSPECT_NETWORKS_FORMAT = "{{json .NetworkSettings.Networks}}"
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
# 运行容器里核对前端清单：只读普通文件、限制大小、校验 schema 与 dist 内容，
# 输出 sha256 供宿主与本次 release 的 dist 清单逐个比对。绝不执行候选 release
# 的 Python，也不访问网络。
MANIFEST_SCRIPT = (
    "import hashlib, json, os, stat\n"
    "base = '/app/web'\n"
    "p = base + '/.ling-web-manifest.json'\n"
    "st = os.lstat(p)\n"
    "assert stat.S_ISREG(st.st_mode) and 0 < st.st_size <= 2 * 1024 * 1024\n"
    "raw = open(p, 'rb').read()\n"
    "data = json.loads(raw)\n"
    "files = data.get('dist_files') if isinstance(data, dict) else None\n"
    "assert data.get('schema') == 1\n"
    "assert isinstance(files, dict) and 'index.html' in files and len(files) >= 2\n"
    "for rel, expected in files.items():\n"
    "    assert isinstance(rel, str) and isinstance(expected, str) and len(expected) == 64\n"
    "    assert rel and not rel.startswith('/') and '..' not in rel.split('/')\n"
    "    path = os.path.join(base, rel)\n"
    "    info = os.lstat(path)\n"
    "    assert stat.S_ISREG(info.st_mode)\n"
    "    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()\n"
    "    assert digest == expected\n"
    "print(json.dumps({'sha256': hashlib.sha256(raw).hexdigest(), 'files': len(files)}))\n"
)
# 备份环境只从可信 env 文件挑这些键（不把整份 env 塞进子进程，避免 PATH 等被改写）。
BACKUP_ENV_KEYS = (
    "POSTGRES_PASSWORD",
    "LING_DB_NAME",
    "LING_DB_USER",
    "LING_DATABASE_URL",
    "LING_DATA_DIR",
    "LING_ROOT_KEY",
)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ExecutorError(Exception):
    """执行器内部错误（不直接进 Web）。"""


class ConfigError(ExecutorError):
    pass


class CommandTimeout(ExecutorError):
    """命令超时。``group_terminated`` 表示已确认整个进程组都不存在。"""

    def __init__(self, message: str, *, group_terminated: bool = False) -> None:
        self.group_terminated = group_terminated
        super().__init__(message)


class UpdateFailure(ExecutorError):
    """执行阶段失败。``manual`` 为真表示无法证明服务未切换，需要人工确认。"""

    def __init__(self, code: str, *, manual: bool = False, stage: str | None = None) -> None:
        self.code = code
        self.manual = manual
        self.stage = stage
        super().__init__(code)


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class UpdateConfig:
    source_root: Path
    state_dir: Path
    requests_dir: Path
    status_dir: Path
    project: str
    port: int
    env_file: Path
    poll_seconds: int = DEFAULT_POLL_SECONDS
    max_seconds: int = DEFAULT_MAX_SECONDS
    api_uid: int = API_UID
    api_gid: int = API_GID

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "host.lock"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def installed_path(self) -> Path:
        return self.state_dir / "installed.json"

    @property
    def phase_path(self) -> Path:
        return self.state_dir / PHASE_FILE_NAME

    @property
    def mirror_dir(self) -> Path:
        return self.state_dir / "mirror.git"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def overrides_dir(self) -> Path:
        """私有 compose override（只修运行 bind 来源）持久化目录。"""
        return self.state_dir / "compose-overrides"

    def install_lock_path(self) -> Path:
        """install.sh 的跨 release 锁：锚点与它完全一致。

        install.sh 用 ``deploy/.env`` **解析后**所在目录的 ``.ling-install.lock``；
        本执行器把 release 的 ``deploy/.env`` 做成指向原 env 的 symlink，所以
        锚点就是原 env 的真实父目录。宿主在执行前就拿这把锁，一直持到本任务
        结束，并把 FD 交给安装器继承（避免安装器自己再抢一次而失败）。
        """
        return Path(os.path.realpath(self.env_file)).parent / ".ling-install.lock"


def _absolute(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是绝对路径")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{field} 必须是绝对路径")
    return path


def _positive_int(value: object, field: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field} 必须是正整数")
    return value


def load_config(path: Path) -> UpdateConfig:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ConfigError(f"读不到配置文件：{path}") from exc
    if not os.path.isfile(path) or os.path.islink(path):
        raise ConfigError("配置文件必须是普通文件（不能是符号链接）")
    if info.st_size > 256 * 1024:
        raise ConfigError("配置文件过大")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("配置文件不是合法 TOML") from exc
    section = raw.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        raise ConfigError(f"配置缺少 [{CONFIG_SECTION}] 段")
    source_root = _absolute(section.get("source_root"), "source_root")
    state_dir = _absolute(section.get("state_dir"), "state_dir")
    requests_dir = _absolute(section.get("requests_dir"), "requests_dir")
    status_dir = _absolute(section.get("status_dir"), "status_dir")
    env_file = _absolute(section.get("env_file"), "env_file")
    project = section.get("project")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ConfigError("project 只允许小写字母/数字/-/_")
    port = _positive_int(section.get("port"), "port")
    if not 1 <= port <= 65535:
        raise ConfigError("port 超出范围")
    if not source_root.is_dir() or source_root.is_symlink():
        raise ConfigError("source_root 必须是已存在的普通目录")
    # env_file 允许是 symlink（install.sh 支持跨 release 共享同一份原始 env）；
    # 这里只要求解析后是普通文件，真正的权限/世界可写检查在 _protect_env_file。
    if not env_file.is_file():
        raise ConfigError("env_file 必须指向已存在的普通文件")
    if not Path(os.path.realpath(env_file)).is_file():
        raise ConfigError("env_file 解析后不是普通文件")
    for name, value in (
        ("state_dir", state_dir),
        ("requests_dir", requests_dir),
        ("status_dir", status_dir),
    ):
        if value == source_root or source_root in value.parents:
            raise ConfigError(f"{name} 不能位于源码 checkout 内")
    return UpdateConfig(
        source_root=source_root,
        state_dir=state_dir,
        requests_dir=requests_dir,
        status_dir=status_dir,
        project=project,
        port=port,
        env_file=env_file,
        poll_seconds=_positive_int(
            section.get("poll_seconds"), "poll_seconds", default=DEFAULT_POLL_SECONDS
        ),
        max_seconds=_positive_int(
            section.get("max_seconds"), "max_seconds", default=DEFAULT_MAX_SECONDS
        ),
        api_uid=_positive_int(section.get("api_uid"), "api_uid", default=API_UID),
        api_gid=_positive_int(section.get("api_gid"), "api_gid", default=API_GID),
    )


# 执行器依赖的协议符号：缺失说明 source_root 里的协议实现太旧（或太新）。
# 宁可在启动时明确拒绝，也不要在跑任务时 AttributeError 到一半。
REQUIRED_PROTOCOL_ATTRS = (
    "SCHEMA_VERSION",
    "UpdateJob",
    "UpdateRequest",
    "PendingFile",
    "read_pending_file",
    "heartbeat_payload",
    "parse_heartbeat",
    "PENDING_STATE_ABSENT",
    "PENDING_STATE_OK",
    "PENDING_STATE_INVALID",
    "PENDING_STATE_UNKNOWN",
    "EXECUTOR_STATUS_OK",
    "EXECUTOR_STATUS_MANUAL",
    "EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED",
)


def installed_release_root(state_dir: Path) -> Path | None:
    """只读 ``installed.json`` 取 live release 根（不依赖 protocol 模块）。

    用于在导入协议**之前**判断应该从哪棵树加载协议：优先本次实际运行的
    release，其次 source_root，最后脚本自身 checkout。任何不可信形态都返回
    ``None``（调用方继续用后备候选）。
    """
    path = state_dir / "installed.json"
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 64 * 1024:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("release_root")
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        return None
    return candidate


def load_protocol(*roots: Path) -> Any:
    """导入共享协议模块，**优先当前实际运行的 release**。

    顺序：``installed.release_root/src`` → ``source_root/src`` → 脚本所在
    checkout 的 ``src``。升级到新 release 后，旧 ``source_root`` 可能还停在旧
    代码；协议必须与真正在跑的版本一致，否则 API 写的新 schema 会被旧协议
    静默当成 ``idle``（见 QA M1）。找不到/版本不兼容都拒绝启动。
    """
    candidates = [*(root / "src" for root in roots), Path(__file__).resolve().parents[2] / "src"]
    for candidate in candidates:
        if not (candidate / "ling" / "updates" / "protocol.py").is_file():
            continue
        text = str(candidate)
        if text not in sys.path:
            sys.path.insert(0, text)
        from ling.updates import protocol as module

        if getattr(module, "SCHEMA_VERSION", None) != "v1":
            raise ConfigError("共享协议版本不兼容（需要 v1）")
        missing = [name for name in REQUIRED_PROTOCOL_ATTRS if not hasattr(module, name)]
        if missing:
            raise ConfigError(
                "共享协议实现过旧/不完整（缺少 " + ", ".join(missing) + "）；"
                "请先让 live release 与执行器来自同一提交"
            )
        return module
    raise ConfigError("找不到 ling.updates.protocol（检查 source_root）")


# ---------------------------------------------------------------- 进程


@dataclass
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    # 捕获内容超过上限被截断时为 True：调用方必须按失败处理，绝不能解析半截 JSON。
    truncated: bool = False


def _kill_process_group(proc: subprocess.Popen[Any]) -> bool:
    """杀掉整个进程组，并确认组内已没有进程（返回是否确认终止）。"""
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pgid = None
    if pgid is None:
        try:
            proc.terminate()
        except OSError:
            return proc.poll() is not None
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    if pgid is not None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    if pgid is None:
        return proc.poll() is not None
    try:
        os.killpg(pgid, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _open_log(log_path: Path) -> Any:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(log_path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab")


def run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    log_path: Path | None = None,
    capture: bool = False,
    capture_limit: int = CAPTURE_LIMIT,
    pass_fds: Sequence[int] = (),
    stdin_file: Path | None = None,
) -> ProcessResult:
    """在独立进程组里运行命令；超时杀掉整个组。

    stdout/stderr 只写宿主私有日志（0600）；``capture=True`` 时 stderr 仍**单独**
    收（合并进 stdout 会把 compose 的 warning 混进 JSON，静默破坏解析），返回的
    stdout 最多 ``capture_limit`` 字节，超出则标记 ``truncated`` 并交给调用方失败。

    ``pass_fds`` 用于把宿主已持有的安装锁 FD 继承给 install.sh；
    ``stdin_file`` 用于把备份 dump 喂给 ``pg_restore --list``（不落临时副本）。
    """
    log_handle = _open_log(log_path) if log_path is not None else None
    stdin_handle = None
    try:
        if stdin_file is not None:
            stdin_handle = stdin_file.open("rb")
            stdin_target: Any = stdin_handle
        else:
            stdin_target = subprocess.DEVNULL
        if capture:
            stdout_target: Any = subprocess.PIPE
            stderr_target: Any = subprocess.PIPE
        else:
            stdout_target = log_handle if log_handle is not None else subprocess.DEVNULL
            stderr_target = subprocess.STDOUT if log_handle is None else log_handle
        try:
            proc = subprocess.Popen(  # noqa: S603 - 参数由执行器固定拼装
                list(argv),
                cwd=str(cwd) if cwd is not None else None,
                env=dict(env) if env is not None else None,
                stdin=stdin_target,
                stdout=stdout_target,
                stderr=stderr_target,
                start_new_session=True,
                text=capture,
                encoding="utf-8" if capture else None,
                errors="replace" if capture else None,
                pass_fds=tuple(pass_fds),
            )
        except OSError as exc:
            raise ExecutorError(f"无法启动命令：{argv[0]}") from exc
        try:
            output, error_output = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            group_terminated = _kill_process_group(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=5)
            if log_handle is not None:
                log_handle.write(f"\n[executor] timeout after {timeout}s\n".encode())
                log_handle.flush()
            raise CommandTimeout(
                "命令执行超时", group_terminated=group_terminated
            ) from None
        text_output = output if isinstance(output, str) else ""
        text_error = error_output if isinstance(error_output, str) else ""
        truncated = len(text_output) > capture_limit
        if truncated:
            text_output = text_output[:capture_limit]
        if log_handle is not None:
            if text_output:
                log_handle.write(text_output.encode("utf-8", "replace"))
            if text_error:
                log_handle.write(b"\n[stderr]\n")
                log_handle.write(text_error.encode("utf-8", "replace"))
            log_handle.flush()
        return ProcessResult(
            returncode=proc.returncode,
            stdout=text_output,
            stderr=text_error,
            truncated=truncated,
        )
    finally:
        if stdin_handle is not None:
            stdin_handle.close()
        if log_handle is not None:
            log_handle.close()


def _read_limited_text(path: Path, *, max_bytes: int = 1024 * 1024) -> str | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or os.path.islink(path) or info.st_size > max_bytes:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _sha256_file(path: Path) -> str:
    """流式 sha256（媒体归档可能几十 GB，不整份读进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_env_file(text: str) -> dict[str, str]:
    """解析 Compose ``KEY=VALUE`` env 文件（不执行、不展开、只取字面值）。

    只接受无歧义的 ``KEY=VALUE`` 行（允许 ``export`` 前缀与成对引号）；注释、
    空行忽略。其它写法（``KEY: value``、含空格的键、未闭合引号）直接跳过
    而不是猜——备份只挑白名单键，跳过的键不会悄悄生效。
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _parse_last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def read_phase_stage(path: Path) -> str | None:
    """读安装阶段标记；接受 JSON 对象/字符串或纯文本行，取最后一个已知阶段。"""
    raw = _read_limited_text(path, max_bytes=16 * 1024)
    if raw is None:
        return None
    payload: Any = None
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        stage = payload.get("stage")
        return stage if stage in INSTALL_PHASES else None
    if isinstance(payload, str):
        return payload if payload in INSTALL_PHASES else None
    found = None
    for line in raw.splitlines():
        token = line.strip().strip('",')
        if token in INSTALL_PHASES:
            found = token
    return found


def _project_version(root: Path) -> str | None:
    raw = _read_limited_text(root / "pyproject.toml", max_bytes=256 * 1024)
    if raw is None:
        return None
    try:
        payload = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return None
    project = payload.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _package_json_version(root: Path) -> str | None:
    raw = _read_limited_text(root / "clients" / "web-console" / "package.json", max_bytes=256 * 1024)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) else None


def _module_version(root: Path) -> str | None:
    """用 AST 读 ``__version__``，绝不执行候选 release 的 Python。"""
    raw = _read_limited_text(root / "src" / "ling" / "__init__.py", max_bytes=64 * 1024)
    if raw is None:
        return None
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value_node = node.value
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, SyntaxError):
            return None
        return value if isinstance(value, str) else None
    return None


@dataclass(frozen=True)
class Mount:
    service: str
    target: str
    type: str
    source: str
    read_only: bool


def parse_compose_config(payload: object) -> dict[str, dict[str, Mount]] | None:
    """把 ``docker compose config --format json`` 解析成 service → target → 挂载。

    只用真实 Compose 解析器的输出：本执行器**不**自己解析 YAML（行级猜测会
    漏掉长写法、merge、profile、env 插值等），也不打印整份配置（里面有数据库
    口令与 root key）。任何字段不符合预期都返回 None（调用方保守失败）。
    """
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        return None
    mounts: dict[str, dict[str, Mount]] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            return None
        volumes = service.get("volumes")
        if volumes is None:
            volumes = []
        if not isinstance(volumes, list):
            return None
        entries: dict[str, Mount] = {}
        for entry in volumes:
            if not isinstance(entry, dict):
                return None
            mount_type = entry.get("type")
            target = entry.get("target")
            source = entry.get("source")
            if not isinstance(mount_type, str) or not isinstance(target, str):
                return None
            if not isinstance(source, str) or not source or not target.startswith("/"):
                return None
            entries[target] = Mount(
                service=name,
                target=target,
                type=mount_type,
                source=str(Path(source)) if mount_type == "bind" else source,
                read_only=entry.get("read_only") is True,
            )
        mounts[name] = entries
    return mounts


def compose_service_replicas(payload: object, service: str) -> int | None:
    """从 Compose 解析结果读某个服务的期望副本数（缺省 1）；不可识别返回 None。"""
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, dict):
        return None
    entry = services.get(service)
    if not isinstance(entry, dict):
        return None
    deploy = entry.get("deploy")
    raw = deploy.get("replicas") if isinstance(deploy, dict) else None
    if raw is None:
        return 1
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= MAX_REPLICAS:
        return None
    return raw


def compose_api_replicas(payload: object) -> int | None:
    """兼容入口：api 的期望副本数。"""
    return compose_service_replicas(payload, "api")


def compose_service_env(payload: object, service: str) -> dict[str, str] | None:
    """读取某个服务解析后的环境变量；只用于断言关键变量存在，不对外打印。"""
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, dict):
        return None
    entry = services.get(service)
    if not isinstance(entry, dict):
        return None
    env = entry.get("environment")
    if not isinstance(env, dict):
        return None
    result: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
        elif isinstance(key, str) and value is None:
            result[key] = ""
        else:
            return None
    return result


@dataclass(frozen=True)
class DatabaseDSN:
    """compose 解析出的 api → 数据库连接串（**不含**原文，避免口令外泄）。"""

    host: str
    port: int
    user: str
    password: str | None
    name: str


def parse_postgres_dsn(dsn: str) -> DatabaseDSN | None:
    """解析 PostgreSQL DSN；非 postgres / 回环地址 / 缺字段一律 None。

    显式拒绝 ``localhost`` / ``127.x`` / ``::1`` / ``0.0.0.0``：compose 网络里
    api 必须通过 db 服务名访问数据库，指向回环说明 DSN 被换成了别的库。
    """
    if not isinstance(dsn, str) or not dsn.strip():
        return None
    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return None
    if parsed.scheme not in ("postgresql", "postgresql+asyncpg", "postgresql+psycopg"):
        return None
    host = parsed.hostname
    if not host or not parsed.username:
        return None
    lowered = host.lower()
    if lowered in ("localhost", "0.0.0.0", "host.docker.internal") or lowered.endswith(
        ".localhost"
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        return None
    name = parsed.path.lstrip("/")
    if not name or "/" in name:
        return None
    try:
        port = parsed.port or 5432
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    password = unquote(parsed.password) if parsed.password is not None else None
    return DatabaseDSN(
        host=host,
        port=port,
        user=unquote(parsed.username),
        password=password,
        name=name,
    )


def compose_database_hosts(payload: object) -> set[str] | None:
    """db 服务在 compose 网络里的可用主机名（service 名 + aliases）。"""
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, dict):
        return None
    db = services.get("db")
    if not isinstance(db, dict):
        return None
    hosts = {"db"}
    networks = db.get("networks")
    if isinstance(networks, dict):
        for key, entry in networks.items():
            if isinstance(key, str) and key:
                hosts.add(key)
            if isinstance(entry, dict):
                for alias in entry.get("aliases") or []:
                    if isinstance(alias, str) and alias:
                        hosts.add(alias)
    return hosts


def compose_model_signature(payload: object) -> tuple[Any, ...] | None:
    """把 compose 解析结果压成可比较的签名（服务/镜像/环境/挂载）。

    用于证明“备份实际使用的 compose 模型”与“安装实际使用的模型”是同一份：
    只比较能改运行行为的部分（image、environment、每个 target 的挂载），
    不比较 build 上下文一类与运行无关的字段。
    """
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, dict):
        return None
    parsed = parse_compose_config(payload)
    if parsed is None:
        return None
    signature: list[Any] = []
    for name in sorted(services):
        entry = services[name]
        if not isinstance(entry, dict):
            return None
        env = entry.get("environment")
        if isinstance(env, dict):
            env_items = tuple(
                sorted((str(key), "" if value is None else str(value)) for key, value in env.items())
            )
        elif env is None:
            env_items = ()
        else:
            return None
        image = entry.get("image")
        mounts = tuple(
            sorted(
                (target, mount.type, mount.source, mount.read_only)
                for target, mount in (parsed.get(name) or {}).items()
            )
        )
        signature.append(
            (name, image if isinstance(image, str) else None, env_items, mounts)
        )
    return tuple(signature)


def runtime_mounts_match(expected: Mapping[str, Mount], actual: Mapping[str, Mount]) -> bool:
    """逐 target 比较 compose 模型与**运行容器真实挂载**（``docker inspect``）。

    与 ``_mounts_match``（compose 模型 vs compose 模型，来源严格相等）不同：
    命名卷的宿主路径由 Docker 生成（``/var/lib/docker/volumes/...``），只能比较
    target/类型/只读位；bind 必须来源逐字一致。
    """
    if set(expected) != set(actual):
        return False
    for target, want in expected.items():
        got = actual[target]
        if want.type != got.type or want.read_only != got.read_only:
            return False
        if want.type == "bind" and want.source != got.source:
            return False
    return True


@dataclass(frozen=True)
class MountPlan:
    """候选 release 的挂载计划：override 内容 + 应用后应有的挂载表。"""

    override: dict[str, Any]
    expected: dict[str, dict[str, Mount]]


def _under_root(source: str, root: Path) -> str | None:
    """source 是否位于 root 之下；是则返回相对路径（posix）。"""
    try:
        relative = Path(source).relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return relative.as_posix()


def _exists(path: str, *, want_dir: bool) -> bool:
    candidate = Path(path)
    try:
        info = os.lstat(candidate)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        try:
            info = os.stat(candidate)
        except OSError:
            return False
    return stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode)


def plan_runtime_override(
    *,
    current: dict[str, dict[str, Mount]],
    candidate: dict[str, dict[str, Mount]],
    live_root: Path,
    release_root: Path,
    requests_dir: Path,
    status_dir: Path,
) -> MountPlan | None:
    """规划只修运行 bind 来源的 compose override。

    规则（任何无法证明安全的变化都返回 None，调用方判 ``compose_bind_changed``）：

    - 服务集合必须完全一致（新增服务可能带任意挂载，一律拒绝）；
    - 每个服务必须保留当前所有挂载目标，且不能新增未知目标；
    - 命名卷（``type: volume``）来源必须一致；
    - ``/app/web`` 指向本次 release 的 dist（唯一允许改来源的挂载）；
    - 更新通道固定指向执行器配置的绝对目录；
    - nginx 配置：当前来自 live release 时允许换成新 release 的同名文件
      （明文/TLS 的选择因此保持不变），否则固定为当前绝对路径；
    - 证书目录：固定为当前绝对路径（证书是运行数据，不在 release 树里）；
    - 其它 bind 全部固定为当前运行来源。
    """
    if set(current) != set(candidate):
        return None
    web_dist = (release_root / "clients" / "web-console" / "dist").resolve()
    expected: dict[str, dict[str, Mount]] = {}
    override_services: dict[str, Any] = {}
    for service in sorted(current):
        current_mounts = current[service]
        candidate_mounts = candidate[service]
        if not set(current_mounts) <= set(candidate_mounts):
            return None
        added = set(candidate_mounts) - set(current_mounts)
        if any(target not in UPDATE_MOUNT_TARGETS for target in added):
            # 新增未知挂载目标 = 无法证明安全的变化，直接拒绝。
            return None
        expected_mounts: dict[str, Mount] = {}
        pinned: list[dict[str, Any]] = []
        for target in sorted(set(current_mounts) | set(candidate_mounts)):
            before = current_mounts.get(target)
            after = candidate_mounts[target]
            if before is None:
                # 只允许补上更新通道自己的两个挂载（首次接线），来源由配置决定。
                if target not in UPDATE_MOUNT_TARGETS or after.type != "bind":
                    return None
                channel_source = str(
                    requests_dir if target.endswith("/requests") else status_dir
                )
                channel_ro = target.endswith("/status")
                if not _exists(channel_source, want_dir=True):
                    return None
                expected_mounts[target] = Mount(
                    service=service,
                    target=target,
                    type="bind",
                    source=channel_source,
                    read_only=channel_ro,
                )
                pinned.append(
                    {
                        "type": "bind",
                        "source": channel_source,
                        "target": target,
                        "read_only": channel_ro,
                    }
                )
                continue
            if after.type != before.type:
                return None
            if before.type != "bind":
                # 命名卷/tmpfs：来源与读写属性都必须一致。
                if after.source != before.source or after.read_only != before.read_only:
                    return None
                expected_mounts[target] = after
                continue
            source: str
            read_only: bool
            if target == WEB_MOUNT_TARGET:
                # dist 是本次安装的构建产物，verify_source 时还不存在；
                # 这里只钉死"来源必须是本 release 的 dist"，安装后再校验清单。
                source = str(web_dist)
                read_only = True
            elif target in UPDATE_MOUNT_TARGETS:
                source = str(requests_dir if target.endswith("/requests") else status_dir)
                read_only = target.endswith("/status")
                if not _exists(source, want_dir=True):
                    return None
            elif target == EDGE_CONF_TARGET:
                relative = _under_root(before.source, live_root)
                source = str(release_root / relative) if relative else before.source
                read_only = True
                if not _exists(source, want_dir=False):
                    return None
            elif target == EDGE_CERTS_TARGET:
                source = before.source
                read_only = True
                if not _exists(source, want_dir=True):
                    return None
            else:
                source = before.source
                read_only = before.read_only
                if after.read_only != before.read_only:
                    return None
                if not _exists(source, want_dir=False) and not _exists(source, want_dir=True):
                    return None
            # 目标容器路径必须保持不变（不能借 override 换掉容器内位置）。
            expected_mounts[target] = Mount(
                service=service, target=target, type="bind", source=source, read_only=read_only
            )
            pinned.append(
                {
                    "type": "bind",
                    "source": source,
                    "target": target,
                    "read_only": read_only,
                }
            )
        expected[service] = expected_mounts
        if pinned:
            override_services[service] = {"volumes": pinned}
    return MountPlan(override={"services": override_services}, expected=expected)


def _mounts_match(
    actual: dict[str, dict[str, Mount]], expected: dict[str, dict[str, Mount]]
) -> bool:
    """逐服务、逐目标比对挂载（类型/来源/只读）；目标集合必须完全一致。"""
    if set(actual) != set(expected):
        return False
    for service, expected_mounts in expected.items():
        actual_mounts = actual.get(service)
        if actual_mounts is None or set(actual_mounts) != set(expected_mounts):
            return False
        for target, want in expected_mounts.items():
            got = actual_mounts[target]
            if (got.type, got.source, got.read_only) != (want.type, want.source, want.read_only):
                return False
    return True


# ---------------------------------------------------------------- 执行器


@dataclass(frozen=True)
class RuntimeInfo:
    commit: str
    version: str
    replicas: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseInfo:
    """宿主独立核验过的稳定 Release（不来自 API/请求）。"""

    release_id: int
    tag: str
    commit: str
    tag_version: str


RUNTIME_SCRIPT = (
    "import json, os\n"
    "from ling import __version__\n"
    "print(json.dumps({'commit': os.environ.get('LING_COMMIT') or None,"
    " 'version': __version__}))\n"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向：Release/ tag 解析只允许固定 URL 本身。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _github_json(url: str) -> dict[str, Any] | None:
    """固定 GitHub API 的只读 JSON GET；禁代理、禁重定向、限制大小。

    不使用 urllib 默认 opener（它会读环境里的 HTTP(S)_PROXY），也不跟随 302：
    否则一个被污染的代理/跳转就能把"远端发布"指到任意位置。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ling-update-executor",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 0) != 200:
                return None
            raw = response.read(MAX_GITHUB_BODY_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(raw) > MAX_GITHUB_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_release_tag_commit(tag: str) -> str | None:
    """用 GitHub git API 解析 annotated tag → commit（最多 4 层）。"""
    payload = _github_json(f"{GITHUB_API_ROOT}/repos/{REPO_SLUG}/git/ref/tags/{tag}")
    if payload is None:
        return None
    obj = payload.get("object")
    for _ in range(5):
        if not isinstance(obj, dict):
            return None
        sha = obj.get("sha")
        obj_type = obj.get("type")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        if obj_type == "commit":
            return sha
        if obj_type != "tag":
            return None
        nxt = _github_json(f"{GITHUB_API_ROOT}/repos/{REPO_SLUG}/git/tags/{sha}")
        if nxt is None:
            return None
        obj = nxt.get("object")
    return None


def lookup_release(tag: str) -> ReleaseInfo | None:
    """宿主独立核验 tag 对应的稳定 Release，并解析其 commit。

    校验：Release id 正整数、tag 完全一致、非 draft、非 prerelease、tag 是
    严格 ``vX.Y.Z``、``refs/tags/<tag>`` 解引用得到 40 位 commit 且与 git
    fetch 的结果一致（由调用方比对）。API/请求里的 target_commitish、分支名
    与客户端 URL 一律不采信。
    """
    if not isinstance(tag, str) or not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        return None
    payload = _github_json(f"{GITHUB_API_ROOT}/repos/{REPO_SLUG}/releases/tags/{tag}")
    if payload is None:
        return None
    release_id = payload.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        return None
    if payload.get("tag_name") != tag:
        return None
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        return None
    commit = _resolve_release_tag_commit(tag)
    if commit is None:
        return None
    return ReleaseInfo(release_id=release_id, tag=tag, commit=commit, tag_version=tag[1:])


class InstallLock:
    """install.sh 的跨 release 锁（宿主侧先持有，再把 FD 交给安装器）。

    install.sh 拿锁后会清掉 FD_CLOEXEC 再 ``exec``；本执行器在调用安装器前
    用 ``pass_fds`` 把同一个 FD 继承过去，安装器验证 inode 与真实 flock 后
    直接复用，不会出现"宿主持锁 → 安装器再抢同一把锁"的自锁。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def fd(self) -> int | None:
        return self._fd

    def acquire(self) -> bool:
        try:
            os.makedirs(self._path.parent, mode=0o700, exist_ok=True)
            fd = os.open(
                self._path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


def _with_backup_warnings(
    job: Any, warnings: Sequence[str], **progress: Any
) -> Any:
    """推进任务状态，并把这些备份告警一起写进任务视图（可选字段）。

    为什么要判能力而不是直接传：已装机器的执行器固定在 /srv 且**不随 Release
    更新**，它可能加载到还没有 ``backup_warnings`` 的旧协议模块。那种情况下这
    条告警只留在 ``verify-warnings.txt``/备份日志里——可以接受（缺展示），但
    **绝不能**因为多传一个关键字参数让写状态本身失败。

    用 ``__dataclass_fields__`` 判能力而不是判模块属性：字段存在就一定能接收，
    旧协议没有这个字段时退回原调用。
    """
    fields = getattr(job, "__dataclass_fields__", {})
    if warnings and "backup_warnings" in fields:
        return job.with_progress(backup_warnings=list(warnings), **progress)
    return job.with_progress(**progress)


class Executor:
    """一次更新 = 一串可单测的阶段函数，任何外部命令都可注入。"""

    def __init__(
        self,
        config: UpdateConfig,
        protocol: Any,
        *,
        process: Callable[..., ProcessResult] | None = None,
        now: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        release_lookup: Callable[[str], ReleaseInfo | None] | None = None,
    ) -> None:
        self.config = config
        self.protocol = protocol
        self._process = process or run_process
        self._now = now or protocol.iso_now
        self._clock = clock or time.time
        self._release_lookup = release_lookup or lookup_release
        self._job: Any = None
        # 当前任务累积的备份告警（媒体归档 partial、旧格式无法校验等）。只用于
        # 把 verify-warnings.txt 的内容同时透出到 Web；文件落点保持不变。
        self._backup_warnings: list[str] = []
        self._previous: dict[str, Any] = {}
        self._install_lock: InstallLock | None = None
        self._compose_override: Path | None = None
        self._expected_mounts: dict[str, dict[str, Mount]] = {}

    # ------------------------------------------------------------ 目录/文件

    def ensure_runtime_dirs(self) -> None:
        """创建宿主目录（安全权限），绝不使用 0777/世界可写。"""
        protocol = self.protocol
        protocol.ensure_directory(self.config.state_dir, mode=0o700, parents=True)
        os.chmod(self.config.state_dir, 0o700)
        for child in (
            self.config.staging_dir,
            self.config.backups_dir,
            self.config.logs_dir,
            self.config.overrides_dir,
        ):
            protocol.ensure_directory(child, mode=0o700, parents=True)
            os.chmod(child, 0o700)
        protocol.ensure_directory(self.config.status_dir, mode=0o755, parents=True)
        os.chmod(self.config.status_dir, 0o755)
        protocol.ensure_directory(self.config.requests_dir, mode=0o770, parents=True)
        self._fix_requests_permissions()

    def _fix_requests_permissions(self) -> None:
        """requests: owner=宿主用户、group=API gid、0770（无 world 权限）。

        容器里的 API（uid 10001）通过 group 位写请求；宿主执行器是 owner。
        没有 root/成员组时无法 chgrp，明确报错而不是退化成 0777。
        """
        owner = -1
        if os.geteuid() == 0:
            sudo_uid = os.environ.get("SUDO_UID", "").strip()
            if sudo_uid.isdigit():
                owner = int(sudo_uid)
        try:
            os.chmod(self.config.requests_dir, 0o770)
            os.chown(self.config.requests_dir, owner, self.config.api_gid)
        except PermissionError as exc:
            raise ConfigError(
                "无法把 requests_dir 的属组设为 API gid；请用 root（或与容器同组的"
                "账号）运行 init，否则 API 无法写入更新请求"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"设置 requests_dir 权限失败：{exc.strerror or exc}") from exc

    def _log_path(self, request_id: str) -> Path:
        return self.config.logs_dir / f"{request_id}.log"

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = QUICK_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        log_path: Path | None = None,
        capture: bool = False,
        pass_fds: Sequence[int] = (),
        stdin_file: Path | None = None,
        capture_limit: int | None = None,
    ) -> ProcessResult:
        return self._process(
            argv,
            timeout=timeout,
            env=env,
            cwd=cwd,
            log_path=log_path,
            capture=capture,
            pass_fds=tuple(pass_fds),
            stdin_file=stdin_file,
            capture_limit=capture_limit,
        )

    def _require_ok(self, result: ProcessResult, code: str) -> None:
        if result.returncode != 0:
            raise UpdateFailure(code)

    # ------------------------------------------------------------ 状态

    def read_installed(self) -> dict[str, Any]:
        payload = self.protocol.read_json_file(self.config.installed_path)
        return payload or {}

    def write_installed(self, payload: Mapping[str, Any]) -> None:
        self.protocol.atomic_write_json(self.config.installed_path, payload, mode=0o600)

    def read_current(self) -> Any:
        return self.protocol.UpdateJob.from_dict(
            self.protocol.read_json_file(self.config.status_dir / self.protocol.CURRENT_FILE)
        )

    def write_job(self, job: Any) -> None:
        self.protocol.atomic_write_json(
            self.config.status_dir / self.protocol.CURRENT_FILE,
            job.to_dict(),
            mode=0o644,
        )

    def _pending_path(self) -> Path:
        return self.config.requests_dir / self.protocol.PENDING_FILE

    def read_pending_file(self) -> Any:
        """读 pending：区分「不存在 / 合法 / 存在但不可信」（协议层保证）。"""
        return self.protocol.read_pending_file(self._pending_path())

    def _requests_lock(self) -> Any:
        """与 API 共享的 pending 所有权锁（``requests/.lock``）。"""
        return self.protocol.FileLock(
            self.config.requests_dir / self.protocol.LOCK_FILE,
            blocking=True,
            timeout=REQUESTS_LOCK_TIMEOUT_SECONDS,
        )

    def clear_pending(self, request_id: str) -> bool:
        """compare-and-unlink：只删除**自己 claim 的那个** request_id。

        API 与宿主共享 ``requests/.lock``。删除必须在锁内重新读取并比较 ID：
        否则“宿主读到 A → API 清理后写入 B → 宿主删除”会把 B 吞掉，用户看到
        201、心跳在线，但请求永远不执行。
        """
        if not self.protocol.is_request_id(request_id):
            return False
        lock = self._requests_lock()
        try:
            acquired = lock.acquire()
        except self.protocol.ProtocolError:
            return False
        if not acquired:
            return False
        try:
            pending = self.read_pending_file()
            if (
                pending.state != self.protocol.PENDING_STATE_OK
                or pending.request is None
                or pending.request.request_id != request_id
            ):
                return False
            return bool(self.protocol.remove_file(self._pending_path()))
        finally:
            lock.release()

    def claim_pending(self, request: Any) -> bool:
        """原子 claim：锁内复查同一 request → 写 running/journal start → CAS 删除。

        任何一步失败都不删除 pending（宁可下一轮重试，也不吃请求）。写 running
        与 journal start 也在锁内完成，API 在锁内读到的状态因此是串行化的：
        要么“还没被 claim（pending 在）”，要么“已 claim（current 是 running）”。
        """
        lock = self._requests_lock()
        try:
            acquired = lock.acquire()
        except self.protocol.ProtocolError:
            return False
        if not acquired:
            return False
        try:
            pending = self.read_pending_file()
            if (
                pending.state != self.protocol.PENDING_STATE_OK
                or pending.request is None
                or pending.request.request_id != request.request_id
            ):
                return False
            self.journal("start", request.request_id)
            self._job = request.to_job().with_progress(state="running", stage="preflight")
            self.write_job(self._job)
            return bool(self.protocol.remove_file(self._pending_path()))
        finally:
            lock.release()

    def write_heartbeat(
        self, *, status: str | None = None, pending_state: str | None = None
    ) -> None:
        """写心跳：状态**显式**声明，API 不再从“文件存在”推断在线/空闲。"""
        installed = self.read_installed()
        commit = installed.get("target_commit")
        version = installed.get("version")
        current = self.read_current()
        if status is None:
            status = self.protocol.EXECUTOR_STATUS_OK
            if current is not None and current.state == "needs_manual":
                status = self.protocol.EXECUTOR_STATUS_MANUAL
        if pending_state is None:
            pending_state = self.read_pending_file().state
        payload = self.protocol.heartbeat_payload(
            last_seen_at=self._now(),
            deployed_commit=commit,
            current_version=version if isinstance(version, str) else None,
            status=status,
            pending_state=pending_state,
        )
        self.protocol.atomic_write_json(
            self.config.status_dir / self.protocol.HEARTBEAT_FILE,
            payload,
            mode=0o644,
        )

    def journal(self, event: str, request_id: str, **fields: Any) -> None:
        record = {
            "schema_version": self.protocol.SCHEMA_VERSION,
            "event": event,
            "request_id": request_id,
            "at": self._now(),
            **fields,
        }
        path = self.config.journal_path
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def journal_started(self, request_id: str) -> bool:
        """该 id 是否“已 claim 但未结束”（上次执行中断），绝不重跑。"""
        return self.journal_state(request_id) == "interrupted"

    def journal_state(self, request_id: str) -> str:
        """持久消费账本：``fresh`` / ``interrupted`` / ``consumed`` / ``corrupt``。

        journal 是 request_id 的持久消费记录：只要出现过该 id 的 ``start``
        或 ``end``，这个请求就已经被消费过，重放一律拒绝（即使上次已成功结束，
        也不能再执行一次）。文件不可读、是符号链接、过大或含坏行时 fail closed
        返回 ``corrupt``，由调用方转人工确认。
        """
        text = self._read_journal()
        if text is None:
            return "corrupt"
        if not text:
            return "fresh"
        started = False
        finished = False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # 损坏的 journal 不能静默跳过：可能正是一个被吞掉的 start。
                return "corrupt"
            if not isinstance(record, dict):
                return "corrupt"
            if record.get("request_id") != request_id:
                continue
            if record.get("event") == "start":
                started = True
            elif record.get("event") == "end":
                finished = True
        if started and not finished:
            return "interrupted"
        if started or finished:
            return "consumed"
        return "fresh"

    def _read_journal(self) -> str | None:
        """读取 journal 原文；缺失/空文件返回 ""，任何不可信形态返回 None。"""
        path = self.config.journal_path
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return ""
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(MAX_JOURNAL_BYTES + 1)
        except OSError:
            return None
        if len(raw) > MAX_JOURNAL_BYTES:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _set_stage(self, stage: str) -> None:
        if self._job is None:
            return
        # 备份阶段之后的每次推进都带上已记录的告警：任务还在跑时就该看得见
        # "这份备份不完整"，而不是等终态。
        self._job = _with_backup_warnings(
            self._job, self._backup_warnings, state="running", stage=stage
        )
        self.write_job(self._job)

    # ------------------------------------------------------------ compose

    def _live_root(self) -> Path:
        """当前部署的 release 根（已核验的 live path）。

        ``installed.release_root`` 一旦存在就必须是**绝对、非符号链接的普通目录**
        且含普通文件 ``deploy/compose.yaml``；否则直接 fail closed，绝不悄悄退回
        ``source_root``（旧树）去跑 compose/备份——那正是 B5 的来源错误。
        只有首次启用（还没有 installed 基线）时才用 ``source_root``。
        """
        installed = self.read_installed()
        raw = installed.get("release_root")
        if raw is None:
            return self.config.source_root
        if not isinstance(raw, str) or not raw:
            raise UpdateFailure("installed_commit_unknown")
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise UpdateFailure("installed_commit_unknown")
        try:
            info = os.lstat(candidate)
        except OSError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UpdateFailure("installed_commit_unknown")
        try:
            compose_info = os.lstat(candidate / "deploy" / "compose.yaml")
        except OSError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        if not stat.S_ISREG(compose_info.st_mode):
            raise UpdateFailure("installed_commit_unknown")
        return candidate

    def _live_env_file(self) -> Path:
        installed = self.read_installed()
        raw = installed.get("env_file")
        if isinstance(raw, str):
            candidate = Path(raw)
            if candidate.is_absolute() and candidate.is_file():
                return candidate
        return self.config.env_file

    def _live_override(self) -> Path | None:
        """当前部署持久化的 compose override（只修运行 bind 来源）。

        本次更新计划出 override 后，后续所有 compose 调用（验证/运行检查）
        都必须带上它；否则检查用的配置与实际启动的配置不是同一份。
        """
        if self._compose_override is not None:
            try:
                if stat.S_ISREG(os.lstat(self._compose_override).st_mode):
                    return self._compose_override
            except OSError:
                pass
        installed = self.read_installed()
        raw = installed.get("compose_override")
        if not isinstance(raw, str) or not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            return None
        try:
            info = os.lstat(candidate)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        try:
            candidate.resolve().relative_to(self.config.overrides_dir.resolve())
        except (OSError, ValueError):
            return None
        return candidate

    def _compose_argv(
        self,
        *args: str,
        root: Path | None = None,
        env_file: Path | None = None,
        override: Path | None | object = ...,
    ) -> list[str]:
        """compose 参数；``override`` 省略时自动带上当前部署持久化的 override。"""
        base = root or self._live_root()
        env = env_file or self._live_env_file()
        extra = self._live_override() if override is ... else override
        argv = ["docker", "compose", "-f", str(base / "deploy" / "compose.yaml")]
        if isinstance(extra, Path):
            argv += ["-f", str(extra)]
        argv += [
            "--project-name",
            self.config.project,
            "--env-file",
            str(env),
            *args,
        ]
        return argv

    def _compose_env(self) -> dict[str, str]:
        """compose 子进程环境：只补更新通道的宿主绝对路径（模板插值用）。

        其余变量仍由 ``--env-file`` 提供；环境里已有这些键时不覆盖用户配置。
        """
        env = dict(os.environ)
        env.setdefault(
            "LING_UPDATE_REQUESTS_HOST", str(Path(os.path.realpath(self.config.requests_dir)))
        )
        env.setdefault(
            "LING_UPDATE_STATUS_HOST", str(Path(os.path.realpath(self.config.status_dir)))
        )
        return env

    def _compose_config_json(
        self,
        *,
        root: Path | None = None,
        override: Path | None | object = ...,
        env_file: Path | None = None,
    ) -> dict[str, Any] | None:
        """``docker compose config --format json``：结构化解析，不打印任何配置。

        输出里含解析后的口令与 root key，所以**绝不**写日志、绝不进 Web；
        只在内存里解析。超过上限、被截断或失败一律 None（调用方保守失败）。
        """
        try:
            result = self._run(
                self._compose_argv(
                    "config",
                    "--format",
                    "json",
                    root=root,
                    override=override,
                    env_file=env_file,
                ),
                timeout=QUICK_TIMEOUT_SECONDS,
                env=self._compose_env(),
                capture=True,
                capture_limit=MAX_CONFIG_JSON_BYTES,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated:
            return None
        text = result.stdout
        if not text or len(text.encode("utf-8", "replace")) > MAX_CONFIG_JSON_BYTES:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _compose_config_yaml(
        self,
        *,
        root: Path | None = None,
        override: Path | None | object = ...,
        env_file: Path | None = None,
    ) -> str | None:
        """``docker compose config``（YAML）：**规范化的合并结果**，用于备份。

        stderr 与 stdout 已分离；截断/失败返回 None。调用方必须把它写进宿主
        私有目录（0600），因为它含解析后的口令。
        """
        try:
            result = self._run(
                self._compose_argv("config", root=root, override=override, env_file=env_file),
                timeout=QUICK_TIMEOUT_SECONDS,
                env=self._compose_env(),
                capture=True,
                capture_limit=MAX_CONFIG_JSON_BYTES,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated or not result.stdout.strip():
            return None
        return result.stdout

    def _expected_replicas(self, payload: object, service: str = "api") -> int:
        replicas = compose_service_replicas(payload, service)
        if replicas is None:
            raise UpdateFailure("config_invalid")
        return replicas

    def _running_service_containers(self, service: str) -> list[str] | None:
        """``compose ps -q --status running <service>`` → 去重后的容器 ID 列表。"""
        try:
            result = self._run(
                self._compose_argv("ps", "-q", "--status", "running", service),
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated:
            return None
        ids: list[str] = []
        for line in result.stdout.splitlines():
            value = line.strip()
            if CONTAINER_ID_RE.fullmatch(value) and value not in ids:
                ids.append(value)
        return ids

    def _running_api_containers(self) -> list[str] | None:
        return self._running_service_containers("api")

    def _container_image_id(self, container_id: str) -> str | None:
        """读容器**实际运行的 image ID**（不是 compose 里写的 tag）。"""
        try:
            result = self._run(
                ["docker", "inspect", "--format", INSPECT_IMAGE_FORMAT, container_id],
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated:
            return None
        value = result.stdout.strip()
        return value if IMAGE_ID_RE.fullmatch(value) else None

    def _container_mounts(self, container_id: str) -> dict[str, Mount] | None:
        """从 ``docker inspect`` 读运行容器的真实 bind/volume 挂载。"""
        try:
            result = self._run(
                ["docker", "inspect", "--format", INSPECT_MOUNTS_FORMAT, container_id],
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated:
            return None
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return None
        if not isinstance(payload, list):
            return None
        mounts: dict[str, Mount] = {}
        for item in payload:
            if not isinstance(item, dict):
                return None
            mount_type = item.get("Type")
            target = item.get("Destination")
            source = item.get("Source")
            if not isinstance(mount_type, str) or not isinstance(target, str):
                return None
            if mount_type == "volume":
                if not isinstance(source, str) or not source:
                    return None
            elif mount_type == "bind":
                if not isinstance(source, str) or not source.startswith("/"):
                    return None
            else:
                continue
            mounts[target] = Mount(
                service="",
                target=target,
                type=mount_type,
                source=source,
                read_only=bool(item.get("RW") is False),
            )
        return mounts

    def _container_network_aliases(self, container_id: str) -> set[str] | None:
        try:
            result = self._run(
                ["docker", "inspect", "--format", INSPECT_NETWORKS_FORMAT, container_id],
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0 or result.truncated:
            return None
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        aliases: set[str] = set()
        for network in payload.values():
            if not isinstance(network, dict):
                return None
            for name in network.get("Aliases") or []:
                if isinstance(name, str) and name:
                    aliases.add(name)
        return aliases

    def _service_images(self, service: str, expected: int) -> dict[str, Any] | None:
        """记录某服务**全部**运行副本的容器 ID 与 image ID（完整、非空、唯一集合）。

        结构：``{"containers": [...], "image_ids": ["sha256:..."]}``。同一服务的
        所有副本必须跑同一个 image ID，否则无法为回退给出可信 pin，直接失败。
        """
        ids = self._running_service_containers(service)
        if ids is None or len(ids) != expected:
            return None
        images: list[str] = []
        for container_id in ids:
            image_id = self._container_image_id(container_id)
            if image_id is None:
                return None
            images.append(image_id)
        unique = sorted(set(images))
        if len(unique) != 1:
            return None
        return {"containers": list(ids), "image_ids": unique}

    def _container_ready(self, container_id: str) -> bool:
        """在**容器内**探 ``127.0.0.1:{port}/readyz``：只接受 2xx，拒绝重定向。"""
        script = READYZ_SCRIPT.format(port=API_READYZ_PORT, path=API_READYZ_PATH)
        try:
            result = self._run(
                ["docker", "exec", container_id, "python", "-c", script],
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return False
        if result.returncode != 0:
            return False
        payload = _parse_last_json_object(result.stdout)
        if payload is None:
            return False
        status = payload.get("status")
        return isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300

    def _container_runtime(self, container_id: str) -> tuple[str, str] | None:
        try:
            result = self._run(
                ["docker", "exec", container_id, "python", "-c", RUNTIME_SCRIPT],
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout):
            return None
        if result.returncode != 0:
            return None
        payload = _parse_last_json_object(result.stdout)
        if payload is None:
            return None
        commit = payload.get("commit")
        version = payload.get("version")
        if not self.protocol.is_full_commit(commit) or not isinstance(version, str) or not version:
            return None
        return commit, version

    def _runtime_info(self, *, release_root: Path | None = None) -> RuntimeInfo:
        """读取**全部**运行中的 api 副本的 commit/version，并核对副本数量。

        compose 默认 ``replicas: 2``；只 ``exec api`` 会随机命中一个副本，
        另一副本跑旧镜像时会被误判为成功。这里按 compose 解析出的期望副本数
        逐个核对 ``docker exec <container>``，数量不足/超编/任一不一致都失败。
        """
        config = self._compose_config_json(root=release_root)
        if config is None:
            raise UpdateFailure("installed_commit_unknown")
        expected = self._expected_replicas(config)
        ids = self._running_api_containers()
        if ids is None or len(ids) != expected:
            raise UpdateFailure("installed_commit_unknown")
        commits: set[str] = set()
        versions: set[str] = set()
        for container_id in ids:
            runtime = self._container_runtime(container_id)
            if runtime is None:
                raise UpdateFailure("installed_commit_unknown")
            commits.add(runtime[0])
            versions.add(runtime[1])
        if len(commits) != 1 or len(versions) != 1:
            # 副本之间不一致：无法证明"整个服务"都跑同一版本。
            raise UpdateFailure("installed_commit_unknown")
        return RuntimeInfo(
            commit=commits.pop(), version=versions.pop(), replicas=tuple(ids)
        )

    # ------------------------------------------------------------ 主循环

    def execute_once(self) -> str:
        # 目录（尤其 state_dir）要先存在，否则连锁文件都创建不了。
        try:
            self.ensure_runtime_dirs()
        except Exception:  # noqa: BLE001 - 权限/路径问题由 doctor 暴露
            return "config_error"
        lock = self.protocol.try_lock(self.config.lock_path)
        if lock is None:
            return "busy"
        try:
            current = self.read_current()
            if current is not None and current.active:
                # 上次进程在 running 状态死掉：真实状态未知，绝不盲重试。
                self._mark_manual(current, "interrupted")
                self.journal("end", current.request_id, state="needs_manual", code="interrupted")
                # 只清自己那个 id：窗口里新到达的请求绝不能被这里删掉。
                self.clear_pending(current.request_id)
                self.write_heartbeat()
                return "interrupted"
            pending = self.read_pending_file()
            if pending.state == self.protocol.PENDING_STATE_INVALID:
                # 文件存在但不可信（坏 JSON / 未来 schema / 符号链接）：
                # fail closed + 可观测，既不执行也不删除，等人工处理。
                self.write_heartbeat(status=self.protocol.EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED)
                return "pending_invalid"
            if pending.state != self.protocol.PENDING_STATE_OK or pending.request is None:
                self.write_heartbeat()
                return "idle"
            request = pending.request
            if current is not None and current.state == "needs_manual":
                # 人工恢复门禁：不执行，也**不删除**请求（它不是失败任务的一部分）；
                # adopt-current 解除门禁后按到达顺序处理。
                self.write_heartbeat(status=self.protocol.EXECUTOR_STATUS_MANUAL)
                return "manual_gate"
            journal_state = self.journal_state(request.request_id)
            if journal_state == "corrupt":
                # journal 损坏/过大：无法证明这个请求没被执行过，转人工。
                self._terminal(request, "needs_manual", "interrupted")
                self.clear_pending(request.request_id)
                self.write_heartbeat()
                return "manual_gate"
            if journal_state == "interrupted":
                self._terminal(request, "needs_manual", "interrupted")
                self.clear_pending(request.request_id)
                self.write_heartbeat()
                return "needs_manual"
            if journal_state == "consumed":
                # 已完成的请求被重放：持久消费 ID 拦住它，绝不二次部署。
                self._terminal(request, "failed", "invalid_request")
                self.clear_pending(request.request_id)
                self.write_heartbeat()
                return "duplicate"
            if request.expired(now=self._clock()):
                self._terminal(request, "failed", "expired")
                self.journal("end", request.request_id, state="failed", code="expired")
                self.clear_pending(request.request_id)
                self.write_heartbeat()
                return "expired"
            self._install_lock = InstallLock(self.config.install_lock_path())
            if not self._install_lock.acquire():
                self.write_heartbeat()
                return "busy"
            try:
                if not self.claim_pending(request):
                    # 锁内发现 pending 已被替换/删除：不执行、不覆盖，下一轮再看。
                    self.write_heartbeat()
                    return "pending_changed"
                self._execute(request)
            finally:
                self._install_lock.release()
                self._install_lock = None
            return "ran"
        finally:
            lock.release()

    def _terminal(self, request: Any, state: str, code: str) -> None:
        job = request.to_job().with_progress(state=state, stage="queued", error_code=code)
        self.write_job(job)

    def _mark_manual(self, current: Any, code: str) -> None:
        job = current.with_progress(state="needs_manual", error_code=code)
        self.write_job(job)
        self._write_recovery_note(job)

    def _execute(self, request: Any) -> None:
        # 每轮从干净状态开始：上一轮失败/中断留下的 override 不能沿用。
        self._compose_override = None
        self._expected_mounts = {}
        self._previous = {}
        self._backup_warnings = []
        # claim_pending 已在 requests 锁内写入 journal start + running 状态，
        # 并用 compare-and-unlink 删掉**本请求**的 pending（不会误删后来的请求）。
        self._job = request.to_job().with_progress(state="running", stage="preflight")
        self.write_job(self._job)
        stage = "preflight"
        try:
            self._preflight(request)
            stage = "backup"
            self._set_stage(stage)
            backup_dir = self._backup(request)
            stage = "fetch"
            self._set_stage(stage)
            release_root = self._fetch_and_stage(request)
            stage = "verify_source"
            self._set_stage(stage)
            self._verify_source(request, release_root)
            stage = "build"
            self._set_stage(stage)
            self._install(request, release_root, backup_dir)
            stage = "verify"
            self._set_stage(stage)
            runtime = self._verify_deployed(request, release_root)
            self._finish_success(request, release_root, backup_dir, runtime)
        except UpdateFailure as exc:
            self._finish_failure(request, exc, stage=exc.stage or stage)
        except Exception:  # noqa: BLE001 - 未预期异常也不能让状态卡在 running
            self._finish_failure(
                request, self._unexpected_failure(stage), stage=stage
            )

    # 未预期异常也按阶段判定：部署前（preflight/backup/fetch/verify_source）没有
    # 动过服务，可以安全判 ``failed`` 让用户重新确认；从 build 开始无法证明
    # 服务是否已切换，一律 ``needs_manual``。绝不在异常时声称成功。
    STAGE_FAILURE_CODES = {
        "preflight": "config_invalid",
        "backup": "backup_failed",
        "fetch": "fetch_failed",
        "verify_source": "source_invalid",
    }

    def _unexpected_failure(self, stage: str) -> UpdateFailure:
        code = self.STAGE_FAILURE_CODES.get(stage)
        if code is None:
            return UpdateFailure("deploy_state_unknown", manual=True)
        return UpdateFailure(code, manual=False, stage=stage)

    # ------------------------------------------------------------ 阶段

    def _preflight(self, request: Any) -> None:
        self._protect_env_file()
        runtime = self._runtime_info()
        if runtime.commit != request.current_commit:
            raise UpdateFailure("installed_commit_changed")
        if runtime.version != request.current_version:
            raise UpdateFailure("installed_commit_changed")
        self._verify_checkout_clean()
        config = self._compose_config_json()
        if config is None:
            raise UpdateFailure("config_invalid")
        images: dict[str, list[str]] = {}
        containers: dict[str, list[str]] = {}
        for service in IMAGE_SERVICES:
            expected = self._expected_replicas(config, service)
            identity = self._service_images(service, expected)
            if identity is None:
                # api 与 worker 是两个独立镜像，回退必须两个都有精确 ID；
                # 拿不到就 fail closed（此时还没动任何服务，可安全重试）。
                raise UpdateFailure("installed_image_unknown")
            images[service] = list(identity["image_ids"])
            containers[service] = list(identity["containers"])
        self._previous = {
            "commit": runtime.commit,
            "version": runtime.version,
            # 结构固定为 {service: [sha256:...]}，非空、去重、每服务唯一。
            "images": images,
            "containers": containers,
            "replicas": list(runtime.replicas),
        }

    def _protect_env_file(self) -> Path:
        """原 env 解析后必须是普通文件、存在且不世界可写。

        ``config.env_file`` 本身允许是 symlink（跨 release 共享一份原始 env），
        但链路终点必须是普通文件，避免被换成设备/目录。候选 release 绝不能
        自带 ``deploy/.env`` 或 symlink（``_export_release`` 会拒绝）；release
        里的 ``deploy/.env`` 由执行器创建为指向这里的受控 symlink，因此安装器
        与执行器永远锚在同一份原始 env 与同一把锁上。
        """
        path = Path(os.path.realpath(self.config.env_file))
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise UpdateFailure("config_invalid") from exc
        if not stat.S_ISREG(info.st_mode):
            raise UpdateFailure("config_invalid")
        if info.st_mode & 0o002:
            raise UpdateFailure("config_invalid")
        return path

    def _env_values_from(self, path: Path) -> dict[str, str]:
        """读取可信 env 文件里的白名单键（只用于备份子进程，不打印）。"""
        raw = _read_limited_text(path, max_bytes=256 * 1024)
        if raw is None:
            raise UpdateFailure("backup_failed")
        values = parse_env_file(raw)
        return {key: values[key] for key in BACKUP_ENV_KEYS if key in values}

    def _env_values(self) -> dict[str, str]:
        return self._env_values_from(self._protect_env_file())

    def _verify_checkout_clean(self) -> None:
        """拒绝 tracked/untracked 未提交改动；gitignored 的 runtime 不算。"""
        try:
            result = self._run(
                ["git", "-C", str(self.config.source_root), "status", "--porcelain"],
                timeout=QUICK_TIMEOUT_SECONDS,
                log_path=self._log_path("git-status"),
                capture=True,
            )
        except (ExecutorError, CommandTimeout) as exc:
            raise UpdateFailure("source_dirty") from exc
        if result.returncode != 0:
            raise UpdateFailure("source_dirty")
        if result.stdout.strip():
            raise UpdateFailure("source_dirty")

    def _backup_script(self, live_root: Path) -> Path:
        """备份脚本必须来自**已核验的 live release**（不是旧 source_root）。

        要求：普通文件、可执行、realpath 仍在 live release 内（拒绝被替换成
        symlink 指向别处）。这样“已安装的备份能力”与“正在运行的服务”同源。
        """
        script = live_root / "deploy" / "backup" / "backup.sh"
        try:
            info = os.lstat(script)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o100:
            raise UpdateFailure("backup_failed")
        try:
            real = script.resolve(strict=True)
            real.relative_to(live_root.resolve())
        except (OSError, ValueError) as exc:
            raise UpdateFailure("backup_failed") from exc
        return real

    def _write_backup_compose_model(
        self, out: Path, live_root: Path, env_file: Path, override: Path | None
    ) -> Path:
        """把 canonical compose 模型（base + live override + env）落到私有文件。

        备份脚本只接受 ``LING_COMPOSE_FILE`` + 项目名；为了让它和安装/运行使用
        完全相同的模型（而不是从源码树里的相对 bind 猜），这里把
        ``docker compose config`` 的合并结果写进备份目录（0600），并再解析回来
        与服务/镜像/环境/挂载逐项比对 canonical 模型；不一致就备份失败。
        """
        text = self._compose_config_yaml(env_file=env_file)
        if text is None:
            raise UpdateFailure("backup_failed")
        path = out / "compose-resolved.yaml"
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        canonical = self._compose_config_json(env_file=env_file)
        if canonical is None:
            raise UpdateFailure("backup_failed")
        if not self._verify_resolved_compose(path, env_file, canonical):
            raise UpdateFailure("backup_failed")
        return path

    def _verify_resolved_compose(
        self, path: Path, env_file: Path, canonical: dict[str, Any]
    ) -> bool:
        """重新解析落盘的合并模型，要求与 canonical 模型完全等价。"""
        try:
            result = self._run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(path),
                    "--project-name",
                    self.config.project,
                    "--env-file",
                    str(env_file),
                    "config",
                    "--format",
                    "json",
                ],
                timeout=QUICK_TIMEOUT_SECONDS,
                env=self._compose_env(),
                capture=True,
                capture_limit=MAX_CONFIG_JSON_BYTES,
            )
        except (ExecutorError, CommandTimeout):
            return False
        if result.returncode != 0 or result.truncated:
            return False
        try:
            resolved = json.loads(result.stdout)
        except ValueError:
            return False
        if not isinstance(resolved, dict):
            return False
        return compose_model_signature(resolved) == compose_model_signature(canonical)

    def _backup(self, request: Any) -> Path:
        out = self.config.backups_dir / request.request_id
        try:
            self.protocol.ensure_directory(out, mode=0o700)
            os.chmod(out, 0o700)
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailure("backup_failed") from exc
        # 1) 原 env 文件（含数据库口令/root key）：只进宿主私有备份目录，0600。
        try:
            shutil.copyfile(self._live_env_file(), out / "deploy.env.bak")
            os.chmod(out / "deploy.env.bak", 0o600)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        # 2) pg_dump + 墓碑清单（复用 deploy/backup/backup.sh；它墓碑失败仍 exit0，
        #    因此必须自己解析 manifest，不能只看退出码）。
        #
        # compose 模式需要 compose 自己的插值变量（POSTGRES_PASSWORD 等）。
        # 不能直接 copy os.environ（缺口令 → pg_dump 认证失败），也不能把整份
        # env 乱塞（PATH/LD_* 会被改写）。这里只从**可信 env 文件**取白名单键，
        # 且 env 只进子进程、绝不打印或落盘。
        live_root = self._live_root()
        # canonical env 是唯一真源：release/deploy/.env 只是指向它的受控 symlink。
        env_file = self._protect_env_file()
        override = self._live_override()
        # 2) 先证明 api 实际连的就是 compose 里那个 db 服务（不是 localhost/外部库），
        #    否则后面 dump/校验都可能是在“另一个库”上做的。
        self._verify_database_identity()
        # 3) 备份必须跑**live release 自己的**脚本，并使用与安装同一份 canonical
        #    compose 模型（base live compose + live override + env + project）。
        script = self._backup_script(live_root)
        resolved = self._write_backup_compose_model(out, live_root, env_file, override)
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "LANG", "LC_ALL")
            if key in os.environ
        }
        env.update(self._env_values_from(env_file))
        env.update(
            {
                "LING_BACKUP_VIA_COMPOSE": "1",
                "LING_COMPOSE_FILE": str(resolved),
                "LING_COMPOSE_PROJECT": self.config.project,
                # 前向兼容：脚本若支持这两个键，会用它们再拼一次 override/env-file；
                # 当前脚本只认 -f，但已由上面的合并模型等价覆盖（并有签名校验）。
                "LING_COMPOSE_ENV_FILE": str(env_file),
            }
        )
        if override is not None:
            env["LING_COMPOSE_OVERRIDE"] = str(override)
        # 备份日志：脚本的全部输出 + 校验阶段的"未能校验/不完整"告警都落这里。
        backup_log = self.config.logs_dir / f"{request.request_id}-backup.log"
        try:
            result = self._run(
                ["bash", str(script), str(out)],
                timeout=BACKUP_TIMEOUT_SECONDS,
                env=env,
                cwd=live_root,
                log_path=backup_log,
            )
        except (ExecutorError, CommandTimeout) as exc:
            raise UpdateFailure("backup_failed") from exc
        if result.returncode != 0:
            raise UpdateFailure("backup_failed")
        self._verify_backup(out, log_path=backup_log)
        # 4) 迁移版本（恢复时必须知道备份对应哪个 schema）。
        self._record_alembic_version(out)
        # 备份目录不挂进 API：它只在 state_dir（0700，宿主私有）下。
        return out

    def _verify_backup(self, out: Path, *, log_path: Path | None = None) -> None:
        manifests = sorted(out.glob("manifest-*.txt"))
        dumps = sorted(out.glob("*.dump"))
        tombstones = sorted(out.glob("tombstones-*.txt"))
        if not manifests or not dumps or not tombstones:
            raise UpdateFailure("backup_failed")
        manifest: dict[str, str] = {}
        for line in manifests[-1].read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                manifest[key.strip()] = value.strip()
        if manifest.get("tombstones_state") != "ok":
            # backup.sh 墓碑失败时仍 exit 0，这里必须视为失败。
            raise UpdateFailure("backup_failed")
        dump = dumps[-1]
        # manifest 指名的那份 dump 必须就是我们即将记录/恢复的那份（防"两份 dump"）。
        named = manifest.get("dump")
        if named is not None and named != dump.name:
            raise UpdateFailure("backup_failed")
        try:
            if dump.stat().st_size <= 0:
                raise UpdateFailure("backup_failed")
            with dump.open("rb") as handle:
                magic = handle.read(5)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        if magic != b"PGDMP":
            # 自定义格式 pg_dump 的头；不是合法 dump 就不能声称“有备份”。
            raise UpdateFailure("backup_failed")
        self._verify_backup_manifest(out, manifest, dump, log_path=log_path)
        self._verify_dump_toc(out, dump)

    def _verify_backup_manifest(
        self,
        out: Path,
        manifest: Mapping[str, str],
        dump: Path,
        *,
        log_path: Path | None = None,
    ) -> None:
        """校验 manifest 的 coverage 字段：根密钥、媒体、产物哈希。

        为什么需要（QA task60 Q2）：只验 ``tombstones_state`` + ``PGDMP`` 头 + TOC
        时，一份 ``media_state=partial``（归档时正在上传，可能漏文件）或
        ``keys_root=absent``（恢复后凭据解不开）的备份仍会被判"备份通过"并继续
        升级——而这个判定是整条数据保全链上唯一的自动关口。这里补上三类最小校验：

        - **枚举**：``keys_root``/``keys_root_source``/``media_state`` 只能取
          ``backup.sh`` 写过的封闭值组合，别的值一律 ``backup_failed``；
        - **自洽**：``media == "absent"`` 必须 ⟺ ``media_state == "absent"``；
        - **哈希/存在性**：``media_state=present`` 时归档必须存在且
          ``media_sha256`` 与实际字节一致；``keys_root=present`` 时
          ``keys/root.key`` 必须存在、非空且 ``keys_root_sha256`` 与实际字节一致
          （见 :meth:`_verify_backup_keys`，QA task62 F2 补的对称性）；
          manifest 记了 ``dump_sha256`` 也核。

        ``media_state=partial`` 有意**不**硬失败（取舍理由）：升级不替换
        ``app-data`` 卷，媒体字节不会因升级消失，partial 通常只是"恰好有文件在上传"
        这一瞬时状态；把它升级成硬失败会让每次"上传中提交升级"都失败，代价大于
        收益。但**绝不能**让它被当成"备份完整"：这里把它写成显式告警
        （``verify-warnings.txt`` + 备份日志），恢复时按"可能缺文件"处理。

        向后兼容：旧格式 manifest 没有这些键时**不失败**（那不是这次脚本的产物，
        且旧备份确实无法校验），但按"未能校验"记录告警，不静默。
        """
        notes: list[str] = []
        keys_root = manifest.get("keys_root")
        keys_source = manifest.get("keys_root_source")
        if keys_root is None and keys_source is None:
            # 两个键都没有 = 旧格式 manifest；只有一个 = 被截断/手改过，不可信。
            notes.append("manifest 缺少 keys_root/keys_root_source（旧格式）：未能校验根密钥")
        elif keys_root is None or keys_source is None:
            raise UpdateFailure("backup_failed")
        else:
            if (keys_root, keys_source) not in BACKUP_KEYS_ROOT_COMBOS:
                raise UpdateFailure("backup_failed")
            if (keys_root, keys_source) == ("absent", "none"):
                notes.append(
                    "manifest: keys_root=absent + keys_root_source=none"
                    "（该部署没有根密钥，恢复到新卷后 provider 凭据无法解密）"
                )
            if (keys_root, keys_source) == ("absent", "env"):
                notes.append(
                    "manifest: keys_root=absent + keys_root_source=env"
                    "（根密钥只在 deploy/.env 的 LING_ROOT_KEY 里，未落盘）："
                    "恢复时必须同时恢复那份 env，否则凭据解不开"
                )
            if keys_root == "present":
                # 与媒体侧对称：present 是"字节被归档了"的断言，必须由文件本身
                # 证明（存在、非空、哈希一致），不能只凭 manifest 里的一句话。
                self._verify_backup_keys(out, manifest)
            if manifest.get("keys_env_set") == "true":
                notes.append(
                    "manifest: keys_env_set=true（备份时 LING_ROOT_KEY 非空，"
                    "应用优先用环境变量而不是 keys/root.key）：恢复时必须同时恢复"
                    "deploy/.env，只回灌 keys/root.key 可能解不开这些密文"
                )
        media_name = manifest.get("media")
        media_state = manifest.get("media_state")
        if media_name is None and media_state is None:
            notes.append("manifest 缺少 media/media_state（旧格式）：未能校验媒体覆盖")
        elif media_name is None or media_state is None:
            # 只写了一半（例如有 media= 没有 media_state=）：无法判断这份备份的状态。
            raise UpdateFailure("backup_failed")
        else:
            if media_state not in BACKUP_MEDIA_STATES:
                raise UpdateFailure("backup_failed")
            if (media_name == "absent") != (media_state == "absent"):
                # 一个说"这次没有媒体"、一个说"归档过"，自相矛盾的 manifest 不可信。
                raise UpdateFailure("backup_failed")
            if media_name != "absent":
                media_sha = manifest.get("media_sha256")
                if media_state == "present" and (not media_sha or media_sha == "-"):
                    # present 却没有任何哈希：无法证明归档字节完整，fail closed。
                    raise UpdateFailure("backup_failed")
                self._verify_backup_file(out, media_name, media_sha)
                if media_state == "partial":
                    notes.append(
                        f"manifest: media_state=partial（{media_name} 归档时有文件正在"
                        "写入，可能漏掉刚上传的媒体）：升级不替换 app-data 卷，但这份"
                        "备份恢复后可能缺文件，恢复前必须核对 media_ref"
                    )
        dump_sha = manifest.get("dump_sha256")
        if dump_sha is None:
            notes.append("manifest 缺少 dump_sha256（旧格式）：未能校验 dump 字节")
        elif dump_sha != "-" and _sha256_file(dump) != dump_sha.lower():
            raise UpdateFailure("backup_failed")
        if notes:
            self._record_backup_warnings(out, notes, log_path=log_path)

    def _verify_backup_keys(self, out: Path, manifest: Mapping[str, str]) -> None:
        """``keys_root=present`` 必须由文件字节自证（与媒体侧对称，fail closed）。

        为什么需要（QA task62 F2）：媒体侧早已校验"归档存在、非空、
        ``media_sha256`` 与实际字节一致"，密钥侧原先只校验枚举组合——于是一份
        ``keys_root=present`` 但 ``keys/root.key`` 已被删掉、清空或被替换的备份
        仍会被判"备份通过"并继续升级，等到真要恢复时才发现库里的 provider 凭据
        全部解不开（那时已经没有第二个来源）。根密钥是整条链上唯一"丢了就不可逆"
        的东西，所以这里要求三件事同时成立：

        - ``keys_root_file`` 必须是 ``keys/root.key``（``present`` 却写 ``-``
          或别的路径，是自相矛盾的 manifest）；
        - 文件存在且非空（空文件等于没有密钥，却看起来像"已经备好了"）；
        - ``keys_root_sha256`` 与**实际字节**一致（``backup.sh`` 在 present 时
          永远写真实哈希，写 ``-`` 说明 manifest 被手改过或脚本回归）。
        """
        if manifest.get("keys_root_file") != "keys/root.key":
            raise UpdateFailure("backup_failed")
        expected = manifest.get("keys_root_sha256")
        if not expected or expected == "-":
            raise UpdateFailure("backup_failed")
        path = out / "keys" / "root.key"
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise UpdateFailure("backup_failed")
            actual = _sha256_file(path)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        if actual != expected.strip().lower():
            raise UpdateFailure("backup_failed")

    def _verify_backup_file(self, out: Path, name: str, expected: str | None) -> None:
        """核对备份目录里的单个产物：必须是本目录内的普通文件，哈希可选核对。"""
        if not name or name in (".", "..") or posixpath.basename(name) != name:
            # manifest 只该写文件名；带路径分隔符的值一律当不可信处理。
            raise UpdateFailure("backup_failed")
        path = out / name
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise UpdateFailure("backup_failed")
            if expected is None or expected == "-":
                return
            actual = _sha256_file(path)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        if actual != expected.strip().lower():
            raise UpdateFailure("backup_failed")

    def _record_backup_warnings(
        self, out: Path, notes: Sequence[str], *, log_path: Path | None
    ) -> None:
        """把"未能校验 / 备份不完整"写成可审计的告警（备份目录 + 备份日志）。

        任务状态里的 ``message`` 由固定码表生成（不能塞自由文本），所以告警必须
        落到宿主私有文件：``state_dir/backups/<id>/verify-warnings.txt``（0600）。
        写不下告警也算失败——否则"告警没写成功"会被读成"没有告警"。

        **同时**（task-76）把同样的几条摘要记到 ``self._backup_warnings``，由
        ``_finish_success`` / ``_finish_failure`` 写进任务视图的可选字段
        ``backup_warnings``：文件落点不变（运维仍可 tail），但 Web/API 也能看见
        「升级成功、备份不完整」。这里用协议层的 ``sanitize_backup_warnings``
        先把条目收敛到合法范围（单行、有长度上限），避免写状态失败连带丢掉真正的
        升级结果；旧协议没有这个字段/函数时静默跳过（只少一个展示）。
        """
        text = "".join(f"[backup-verify] {note}\n" for note in notes)
        try:
            path = out / "verify-warnings.txt"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        self._backup_warnings = self._remember_backup_warnings(notes)

    def _remember_backup_warnings(self, notes: Sequence[str]) -> list[str]:
        """把告警摘要收集进任务视图用的列表（去重、限界，绝不抛异常）。

        这里刻意**不**失败：文件已经写好了，告警本身是"已知且可控"的；把任务
        视图的展示字段弄炸只会让一次成功升级变成失败。协议能力探测在
        ``_with_backup_warnings`` 里做，这里只做纯数据收敛。
        """
        sanitize = getattr(self.protocol, "sanitize_backup_warnings", None)
        merged: list[str] = [*self._backup_warnings, *notes]
        if callable(sanitize):
            try:
                return list(sanitize(merged))
            except Exception:  # noqa: BLE001 - 展示字段绝不连累任务状态
                return list(self._backup_warnings)
        return list(self._backup_warnings)

    def _verify_dump_toc(self, out: Path, dump: Path) -> None:
        """用 ``pg_restore --list`` 真读一遍备份，并核对相关对象。

        只看 5 字节 magic 不够：截断/空壳文件也有 PGDMP 头。用 db 容器里的
        pg_restore（与生成 dump 的版本一致）列出 TOC，必须含 alembic_version
        与 memories 表——证明这份备份确实属于本项目的数据库。TOC 写进宿主
        私有备份目录（0600），不打印（可能含对象名）。
        """
        toc_path = out / "dump-toc.txt"
        try:
            result = self._run(
                self._compose_argv("exec", "-T", "db", "pg_restore", "--list"),
                timeout=QUICK_TIMEOUT_SECONDS,
                log_path=toc_path,
                stdin_file=dump,
            )
        except (ExecutorError, CommandTimeout) as exc:
            raise UpdateFailure("backup_failed") from exc
        if result.returncode != 0:
            raise UpdateFailure("backup_failed")
        try:
            os.chmod(toc_path, 0o600)
        except OSError as exc:
            raise UpdateFailure("backup_failed") from exc
        toc = _read_limited_text(toc_path, max_bytes=16 * 1024 * 1024)
        if toc is None:
            raise UpdateFailure("backup_failed")
        # 至少要有 alembic 版本表，并且是一份“有内容”的项目库 dump；
        # 不绑定具体业务表名（迁移可能改名），但空壳/错库仍会被挡住。
        if "TABLE public alembic_version" not in toc:
            raise UpdateFailure("backup_failed")
        table_lines = sum(
            1 for line in toc.splitlines() if re.match(r"^\d+; \d+ \d+ TABLE ", line)
        )
        if table_lines < 5:
            raise UpdateFailure("backup_failed")

    def _verify_database_identity(self) -> DatabaseDSN:
        """证明 api 的 DSN 指向 compose 的 db 服务，且**正在跑的那个库**一致。

        四步都要过（任一步失败都 ``backup_failed``，此时还没动服务）：

        1. 从 ``docker compose config --format json`` 取 api 服务解析后的
           ``LING_DATABASE_URL``（不是从 .env 或正则猜）；
        2. 与同一个模型里的 db 服务身份比对：``POSTGRES_USER`` /
           ``POSTGRES_DB`` / ``POSTGRES_PASSWORD`` 必须逐字一致，host 必须是
           db 服务名或其网络 alias，且不能是 localhost/127/::1（回环=换了库）；
        3. 运行中的 db 容器只有 1 个，且其网络 alias 里确实有该 host；
        4. 在 db 容器里查 ``current_database()/current_user``，必须与 DSN 一致
           （证明 DSN 指向的是真正在跑的那个库，而不是另一个同名库）。
        """
        config = self._compose_config_json()
        if config is None:
            raise UpdateFailure("backup_failed")
        api_env = compose_service_env(config, "api")
        db_env = compose_service_env(config, "db")
        if api_env is None or db_env is None:
            raise UpdateFailure("backup_failed")
        dsn = parse_postgres_dsn(api_env.get("LING_DATABASE_URL", ""))
        if dsn is None:
            raise UpdateFailure("backup_failed")
        db_user = db_env.get("POSTGRES_USER")
        db_name = db_env.get("POSTGRES_DB")
        db_password = db_env.get("POSTGRES_PASSWORD")
        if not db_user or not db_name or db_password is None:
            raise UpdateFailure("backup_failed")
        if dsn.user != db_user or dsn.name != db_name or dsn.password != db_password:
            raise UpdateFailure("backup_failed")
        hosts = compose_database_hosts(config)
        if hosts is None or dsn.host not in hosts:
            raise UpdateFailure("backup_failed")
        ids = self._running_service_containers("db")
        if ids is None or len(ids) != 1:
            raise UpdateFailure("backup_failed")
        aliases = self._container_network_aliases(ids[0])
        if aliases is None or dsn.host not in aliases:
            raise UpdateFailure("backup_failed")
        try:
            live = self._run(
                self._compose_argv(
                    "exec",
                    "-T",
                    "db",
                    "psql",
                    "-U",
                    dsn.user,
                    "-d",
                    dsn.name,
                    "-tAc",
                    "select current_database() || '|' || current_user",
                ),
                timeout=QUICK_TIMEOUT_SECONDS,
                capture=True,
            )
        except (ExecutorError, CommandTimeout) as exc:
            raise UpdateFailure("backup_failed") from exc
        if live.returncode != 0:
            raise UpdateFailure("backup_failed")
        if live.stdout.strip() != f"{dsn.name}|{dsn.user}":
            raise UpdateFailure("backup_failed")
        return dsn

    def _record_alembic_version(self, out: Path) -> None:
        """记录 alembic 版本。

        数据库身份已由 ``_verify_database_identity``（备份开始时）证明；这里在
        api 容器里执行 ``alembic current``，把版本落进备份目录，恢复时据此判断
        旧镜像能否认识新 schema。
        """
        try:
            alembic = self._run(
                self._compose_argv("exec", "-T", "api", "alembic", "current"),
                timeout=QUICK_TIMEOUT_SECONDS,
                log_path=out / "alembic.log",
                capture=True,
            )
        except (ExecutorError, CommandTimeout) as exc:
            raise UpdateFailure("backup_failed") from exc
        if alembic.returncode != 0 or not alembic.stdout.strip():
            raise UpdateFailure("backup_failed")
        if re.search(r"^[0-9a-f]{6,}\b", alembic.stdout, re.MULTILINE) is None:
            raise UpdateFailure("backup_failed")
        (out / "alembic-current.txt").write_text(alembic.stdout, encoding="utf-8")
        os.chmod(out / "alembic-current.txt", 0o600)

    def _fetch_and_stage(self, request: Any) -> Path:
        tag = request.target_tag
        mirror = self.config.mirror_dir
        if not (mirror / "HEAD").exists():
            self._require_ok(
                self._run(
                    ["git", "init", "--bare", str(mirror)],
                    timeout=QUICK_TIMEOUT_SECONDS,
                    log_path=self._log_path(f"{request.request_id}-fetch"),
                ),
                "fetch_failed",
            )
        remote = self._run(
            ["git", "-C", str(mirror), "remote", "get-url", "origin"],
            timeout=QUICK_TIMEOUT_SECONDS,
            capture=True,
            log_path=self._log_path(f"{request.request_id}-fetch"),
        )
        if remote.returncode != 0:
            self._require_ok(
                self._run(
                    ["git", "-C", str(mirror), "remote", "add", "origin", REPO_URL],
                    timeout=QUICK_TIMEOUT_SECONDS,
                    log_path=self._log_path(f"{request.request_id}-fetch"),
                ),
                "fetch_failed",
            )
        elif remote.stdout.strip() != REPO_URL:
            # 固定仓库：mirror 的 origin 被改成别的地址时拒绝更新。
            raise UpdateFailure("fetch_failed")
        fetch = self._run(
            [
                "git",
                "-C",
                str(mirror),
                "fetch",
                "--no-tags",
                "--prune",
                "origin",
                f"+refs/tags/{tag}:refs/tags/{tag}",
            ],
            timeout=FETCH_TIMEOUT_SECONDS,
            log_path=self._log_path(f"{request.request_id}-fetch"),
            capture=True,
        )
        if fetch.returncode != 0:
            raise UpdateFailure("fetch_failed")
        resolved = self._run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            timeout=QUICK_TIMEOUT_SECONDS,
            capture=True,
            log_path=self._log_path(f"{request.request_id}-fetch"),
        )
        commit = resolved.stdout.strip().splitlines()[-1] if resolved.stdout.strip() else ""
        if resolved.returncode != 0 or not self.protocol.is_full_commit(commit):
            raise UpdateFailure("target_mismatch")
        if commit != request.target_commit:
            # 宿主独立核验：不信任 API/请求里的提交号。
            raise UpdateFailure("target_mismatch")
        self._verify_release(request, commit)
        return self._export_release(request, commit)

    def _verify_release(self, request: Any, commit: str) -> None:
        """宿主独立查询固定 GitHub Release，不信任请求里的 release 信息。

        request 里没有 release_id（也不应该有）：宿主按 tag 查
        ``/repos/qsbb/ling/releases/tags/<tag>``，要求 id>0、tag 一致、非
        draft、非 prerelease、annotated tag 解引用后的 commit 与 git fetch 的
        结果一致，并且版本严格高于当前运行版本。仅"tag 存在"不足以发布：
        未发布 tag、预发布 release、降级重放都必须在这里被拒绝。
        """
        try:
            release = self._release_lookup(request.target_tag)
        except Exception:  # noqa: BLE001 - 网络/解析失败一律不可核验
            release = None
        if release is None:
            raise UpdateFailure("release_unverifiable")
        if (
            release.tag != request.target_tag
            or release.commit != commit
            or release.release_id <= 0
            or release.tag_version != self.protocol.tag_version(request.target_tag)
        ):
            raise UpdateFailure("release_unverifiable")
        newer = self.protocol.release_is_newer(request.current_version, request.target_tag)
        if newer is not True:
            # 禁止仅凭 tag 做任意降级（或无法比较时"猜"成升级）。
            raise UpdateFailure("not_newer")

    def _export_release(self, request: Any, commit: str) -> Path:
        stage_dir = self.config.staging_dir / request.request_id
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        self.protocol.ensure_directory(stage_dir, mode=0o700)
        archive = stage_dir / "source.tar"
        self._require_ok(
            self._run(
                [
                    "git",
                    "-C",
                    str(self.config.mirror_dir),
                    "archive",
                    "--format=tar",
                    f"--output={archive}",
                    commit,
                ],
                timeout=FETCH_TIMEOUT_SECONDS,
                log_path=self._log_path(f"{request.request_id}-archive"),
            ),
            "fetch_failed",
        )
        repo = stage_dir / "repo"
        try:
            self.protocol.ensure_directory(repo, mode=0o700)
            with tarfile.open(archive, "r:") as tar:
                for member in tar.getmembers():
                    normalized = posixpath.normpath(member.name)
                    if (
                        normalized.startswith("/")
                        or normalized == ".."
                        or normalized.startswith("../")
                    ):
                        raise UpdateFailure("source_invalid")
                    if normalized == "deploy/.env" or normalized.startswith("deploy/.env/"):
                        # 候选 release 不能自带 env 文件或 symlink：来源必须是
                        # 宿主配置的原 env（否则安装锁锚点与口令来源都可被换掉）。
                        raise UpdateFailure("source_invalid")
                tar.extractall(path=repo, filter="data")
        except UpdateFailure:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise UpdateFailure("source_invalid") from exc
        finally:
            with contextlib.suppress(OSError):
                archive.unlink()
        # 环境文件：不复制，建立受控 symlink 指向宿主配置的原 env。
        # install.sh 的锁锚点 = deploy/.env 解析后所在目录 + .ling-install.lock，
        # 因此 release 树、宿主执行器与安装器始终落在同一份 env / 同一把锁上。
        deploy_dir = repo / "deploy"
        try:
            self.protocol.ensure_directory(deploy_dir, mode=0o755)
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailure("source_invalid") from exc
        env_link = deploy_dir / ".env"
        if os.path.lexists(env_link):
            raise UpdateFailure("source_invalid")
        target = self._protect_env_file()
        try:
            os.symlink(str(target), env_link)
        except OSError as exc:
            raise UpdateFailure("source_invalid") from exc
        return repo

    def _verify_source(self, request: Any, release_root: Path) -> None:
        expected = self.protocol.tag_version(request.target_tag)
        if expected is None:
            raise UpdateFailure("source_invalid")
        versions = {
            "pyproject": _project_version(release_root),
            "ling": _module_version(release_root),
            "web": _package_json_version(release_root),
        }
        if any(value != expected for value in versions.values()):
            # 安全解析、不执行候选 Python；版本不一致就拒绝发布。
            raise UpdateFailure("source_invalid")
        self._plan_and_verify_override(request, release_root)

    def _plan_and_verify_override(self, request: Any, release_root: Path) -> None:
        """用真实 ``docker compose config`` 固定运行 bind 来源，并落盘私有 override。

        候选 release 换了根目录后，compose 里 ``./certs``、``./nginx-tls.conf``
        等相对路径会静默指向新 release 树（证书不存在 → TLS 起不来）。这里：
        证书固定为当前绝对路径；nginx 配置若当前来自 live release，则换成新
        release 的同名文件（明文/TLS 选择不变）；更新通道固定为配置目录；
        ``/app/web`` 指向新 dist；其它来源全部钉死为当前运行值。任何未知挂载
        变化都拒绝（``compose_bind_changed``）。不手写 YAML：override 是 JSON
        （合法 YAML 子集），最终再用 compose 自己解析一遍做逐项比对。
        """
        live_root = self._live_root()
        current_payload = self._compose_config_json()
        candidate_payload = self._compose_config_json(root=release_root, override=None)
        if current_payload is None or candidate_payload is None:
            raise UpdateFailure("source_invalid")
        current = parse_compose_config(current_payload)
        candidate = parse_compose_config(candidate_payload)
        if current is None or candidate is None:
            raise UpdateFailure("source_invalid")
        plan = plan_runtime_override(
            current=current,
            candidate=candidate,
            live_root=live_root,
            release_root=release_root,
            requests_dir=Path(os.path.realpath(self.config.requests_dir)),
            status_dir=Path(os.path.realpath(self.config.status_dir)),
        )
        if plan is None:
            raise UpdateFailure("compose_bind_changed")
        final = self.config.overrides_dir / f"{request.request_id}.json"
        pending = self.config.overrides_dir / f".{request.request_id}.json.tmp"
        try:
            self.protocol.atomic_write_json(pending, plan.override, mode=0o600)
            verified_payload = self._compose_config_json(root=release_root, override=pending)
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailure("compose_bind_changed") from exc
        verified = parse_compose_config(verified_payload) if verified_payload else None
        if verified is None or not _mounts_match(verified, plan.expected):
            with contextlib.suppress(OSError):
                pending.unlink()
            raise UpdateFailure("compose_bind_changed")
        try:
            os.replace(pending, final)
            os.chmod(final, 0o600)
        except OSError as exc:
            raise UpdateFailure("compose_bind_changed") from exc
        self._compose_override = final
        self._expected_mounts = plan.expected

    def _install(self, request: Any, release_root: Path, backup_dir: Path) -> None:
        installer = release_root / "deploy" / "install.sh"
        if not installer.is_file():
            raise UpdateFailure("source_invalid")
        if self._compose_override is None:
            # 没经过 verify_source 就不带 override 安装等于换掉宿主挂载，拒绝。
            raise UpdateFailure("compose_bind_changed")
        phase = self.config.phase_path
        with contextlib.suppress(OSError):
            phase.unlink()
        env = dict(os.environ)
        env.update(
            {
                "LING_COMMIT": request.target_commit,
                "LING_WEB_DIST_HOST": str(release_root / "clients" / "web-console" / "dist"),
                "LING_UPDATE_REQUESTS_HOST": os.path.realpath(self.config.requests_dir),
                "LING_UPDATE_STATUS_HOST": os.path.realpath(self.config.status_dir),
                "LING_UPDATE_ENABLED": "1",
                # 只有本次更新生成的私有 override：安装器的所有 dc 调用会追加
                # -f <path>，证书/更新通道/运行数据不会跟着 release 根目录漂移。
                "LING_INSTALL_COMPOSE_OVERRIDE": str(self._compose_override),
                # 安装器 owner 提供的阶段标记；本执行器只读，不改 install.sh。
                "LING_INSTALL_PHASE_FILE": str(phase),
                # 宿主已持有原 env 父目录的 .ling-install.lock；把 FD 交给安装器
                # 继承，安装器验证 inode/flock 后复用而不是再抢一次（会自锁）。
                "LING_INSTALL_LOCK_FD": str(self._install_fd()),
            }
        )
        # 安装器若尚未支持 LING_INSTALL_COMPOSE_OVERRIDE，至少用环境变量把
        # TLS 配置/证书这两个最关键的相对路径固定成绝对路径（compose 模板本来
        # 就由它们插值），避免换 release 后证书目录静默变成候选树里的空目录。
        for mounts in self._expected_mounts.values():
            conf = mounts.get(EDGE_CONF_TARGET)
            if conf is not None and conf.type == "bind":
                env["LING_EDGE_CONF"] = conf.source
            certs = mounts.get(EDGE_CERTS_TARGET)
            if certs is not None and certs.type == "bind":
                env["LING_EDGE_CERTS"] = certs.source
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_phase, args=(phase, stop), name="install-phase", daemon=True
        )
        watcher.start()
        result: ProcessResult | None = None
        timeout: CommandTimeout | None = None
        try:
            result = self._run(
                [
                    "bash",
                    str(installer),
                    "--project",
                    self.config.project,
                    "--port",
                    str(self.config.port),
                ],
                timeout=self.config.max_seconds,
                env=env,
                cwd=release_root,
                log_path=self.config.logs_dir / f"{request.request_id}-install.log",
                pass_fds=(self._install_fd(),),
            )
        except CommandTimeout as exc:
            timeout = exc
        finally:
            stop.set()
            watcher.join(timeout=5)

        if timeout is not None:
            manual, code = self._classify_install_timeout(phase, timeout)
            raise UpdateFailure(code, manual=manual)
        assert result is not None
        if result.returncode != 0:
            manual, code = self._classify_install_failure(phase)
            raise UpdateFailure(code, manual=manual)

    def _install_fd(self) -> int:
        lock = self._install_lock
        if lock is None or lock.fd is None:
            raise UpdateFailure("config_invalid")
        return lock.fd

    def _watch_phase(self, phase: Path, stop: threading.Event) -> None:
        mapping = {"build": "build", "deploy": "deploy", "verify": "verify", "complete": "verify"}
        last: str | None = None
        while not stop.wait(1.0):
            stage = read_phase_stage(phase)
            if stage is None or stage == last:
                continue
            last = stage
            mapped = mapping.get(stage)
            if mapped is None:
                continue
            try:
                self._set_stage(mapped)
            except Exception:  # noqa: BLE001 - 状态写失败不能让安装线程崩
                continue

    def _classify_install_failure(self, phase: Path) -> tuple[bool, str]:
        stage = read_phase_stage(phase)
        if stage == "build":
            # 有明确标记证明还没进入 deploy：旧服务仍在线，可安全判 failed。
            return False, "build_failed"
        if stage in ("deploy", "verify", "complete"):
            return True, "deploy_failed"
        # 没有阶段标记（或标记不可读）：无法证明服务未切换，必须人工确认。
        return True, "deploy_state_unknown"

    def _classify_install_timeout(self, phase: Path, timeout: CommandTimeout) -> tuple[bool, str]:
        """超时：只有「阶段标记=build」且「安装进程组确认全部终止」才判可重试。

        注意 ``docker compose build`` 由 daemon 执行；杀安装进程组不证明 daemon
        已经停止构建，但 daemon 无法切换容器（切换发生在 deploy 阶段）。因此
        可判 failed 的充分条件必须同时成立：

        1. 阶段标记可读且恰好是 ``build``（没有触发 deploy/verify/complete 命令）；
        2. 宿主确认安装进程组内已无存活进程（``group_terminated``）。

        任一不满足（无标记、标记已是 deploy+、进程组仍在、被外部 kill）都按
        ``installer_timeout`` / manual 保守处理。
        """
        if read_phase_stage(phase) != "build":
            return True, "installer_timeout"
        if not timeout.group_terminated:
            return True, "installer_timeout"
        return False, "build_failed"

    def _verify_deployed(self, request: Any, release_root: Path) -> RuntimeInfo:
        """逐副本验证 commit/version/就绪/manifest，最后再验 edge 只返回 2xx。

        只看 edge 或 compose health 都不够：``/readyz`` 可能被 TLS 配置 301，
        health 也可能停留在“容器活着”。这里要求：

        - compose 解析出的期望副本数全部在运行（``_runtime_info``）；
        - **每个** api 副本在容器内访问 ``127.0.0.1:{port}/readyz`` 得到 2xx；
        - 每个副本的 commit/version 与目标一致；
        - 每个容器的 ``/app/web`` 清单哈希与本次 release 的 dist 一致；
        - edge 对宿主 ``/readyz`` 严格返回 2xx（3xx 判失败，不跟随）。
        """
        expected_version = self.protocol.tag_version(request.target_tag)
        deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
        runtime: RuntimeInfo | None = None
        while True:
            try:
                candidate = self._runtime_info(release_root=release_root)
            except UpdateFailure:
                candidate = None
            if (
                candidate is not None
                and candidate.commit == request.target_commit
                and candidate.version == expected_version
                and self._all_replicas_ready(candidate)
                and self._verify_web_manifest(release_root, candidate)
            ):
                runtime = candidate
                break
            if time.monotonic() >= deadline:
                break
            # 滚动更新（start-first）期间可能短暂多一个副本或新副本未就绪。
            time.sleep(3)
        if runtime is None:
            raise UpdateFailure("verify_failed", manual=True)
        if not self._wait_edge_ready():
            raise UpdateFailure("verify_failed", manual=True)
        return runtime

    def _all_replicas_ready(self, runtime: RuntimeInfo) -> bool:
        """逐个 api 副本在容器内探 readyz；缺一个都不算就绪。"""
        if not runtime.replicas:
            return False
        return all(self._container_ready(container_id) for container_id in runtime.replicas)

    def _verify_web_manifest(self, release_root: Path, runtime: RuntimeInfo) -> bool:
        """确认**每个** api 副本实际挂载的 web 清单就是本次 release 的 dist。

        不假定"install.sh 退出码 0 就发布了前端"：宿主自己读 release 的
        ``.ling-web-manifest.json``（schema/dist 校验 + sha256），再进每个
        api 容器读 ``/app/web`` 里的真实文件比对。任何一个副本不一致都不算成功。
        """
        manifest = release_root / "clients" / "web-console" / "dist" / ".ling-web-manifest.json"
        try:
            info = os.lstat(manifest)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024 * 1024:
                return False
            raw = manifest.read_bytes()
        except OSError:
            return False
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("schema") != 1:
            return False
        files = data.get("dist_files")
        if not isinstance(files, dict) or "index.html" not in files or len(files) < 2:
            return False
        # 清单里的每个文件都必须真的在 release dist 里且哈希一致；只看清单
        # 本身无法证明构建产物没被替换。
        dist = manifest.parent
        if dist.is_symlink():
            return False
        dist_real = dist.resolve()
        for rel, expected in files.items():
            if not isinstance(rel, str) or not isinstance(expected, str) or len(expected) != 64:
                return False
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts:
                return False
            path = dist / rel
            try:
                if path.is_symlink():
                    return False
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                    return False
                if not path.resolve().is_relative_to(dist_real):
                    return False
                file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                return False
            if file_digest != expected:
                return False
        digest = hashlib.sha256(raw).hexdigest()
        for container_id in runtime.replicas:
            try:
                result = self._run(
                    ["docker", "exec", container_id, "python", "-c", MANIFEST_SCRIPT],
                    timeout=QUICK_TIMEOUT_SECONDS,
                    capture=True,
                )
            except (ExecutorError, CommandTimeout):
                return False
            if result.returncode != 0:
                return False
            payload = _parse_last_json_object(result.stdout)
            if payload is None or payload.get("sha256") != digest:
                return False
        return True

    def _wait_edge_ready(self) -> bool:
        """宿主 edge 的 ``/readyz`` **只接受 2xx**。

        不跟随重定向（带着 Origin/Host 去连外部域名不可控），也**不接受 3xx**：
        301 只能说明 80 端口上有东西在应答，可能是 TLS 跳转、可能是配置错。
        逐副本就绪已由容器内探针独立证明；edge 必须真的 2xx 才算部署成功。
        """
        url = f"http://127.0.0.1:{self.config.port}/readyz"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        for _ in range(EDGE_READY_ATTEMPTS):
            try:
                with opener.open(url, timeout=3) as response:
                    status = getattr(response, "status", 0)
                    if isinstance(status, int) and 200 <= status < 300:
                        return True
            except urllib.error.HTTPError:
                # 3xx/4xx/5xx 一律不算就绪（301 尤其不能当成成功）。
                pass
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(EDGE_READY_INTERVAL_SECONDS)
        return False

    def _finish_success(
        self, request: Any, release_root: Path, backup_dir: Path, runtime: RuntimeInfo
    ) -> None:
        expected_version = self.protocol.tag_version(request.target_tag) or runtime.version
        installed = {
            "schema_version": self.protocol.SCHEMA_VERSION,
            "target_tag": request.target_tag,
            "target_commit": request.target_commit,
            "version": expected_version,
            "release_root": str(release_root),
            "env_file": str(release_root / "deploy" / ".env"),
            "backup_dir": str(backup_dir),
            "previous": self._previous,
            "updated_at": self._now(),
            "compose_override": str(self._compose_override) if self._compose_override else None,
            "replicas": list(runtime.replicas),
        }
        self.write_installed(installed)
        # 升级成功但备份不完整时，告警必须跟着任务一起到 Web（不只是日志文件）。
        self._job = _with_backup_warnings(
            self._job, self._backup_warnings, state="succeeded", stage="complete"
        )
        self.write_job(self._job)
        self.write_heartbeat()
        self.journal("end", request.request_id, state="succeeded")
        images = self._previous.get("images") if isinstance(self._previous, dict) else None
        print(
            f"[executor] 更新完成：{request.target_tag} {request.target_commit[:12]} "
            f"（备份：{backup_dir}；旧镜像：{images or '未记录'}）"
        )

    def _finish_failure(self, request: Any, exc: UpdateFailure, *, stage: str) -> None:
        state = "needs_manual" if exc.manual else "failed"
        self._job = _with_backup_warnings(
            self._job,
            self._backup_warnings,
            state=state,
            stage=stage,
            error_code=exc.code,
        )
        self.write_job(self._job)
        self.journal("end", request.request_id, state=state, code=exc.code, stage=stage)
        self._write_recovery_note(self._job)
        print(f"[executor] 更新{'需要人工确认' if exc.manual else '失败'}：{exc.code}（stage={stage}）")

    def _write_recovery_note(self, job: Any) -> None:
        backup = self.config.backups_dir / job.request_id
        lines = [
            "ling 更新失败/中断记录（宿主私有，不含在 Web 响应里）",
            f"request_id={job.request_id}",
            f"state={job.state}",
            f"stage={job.stage}",
            f"error_code={job.error_code}",
            f"target_tag={job.target_tag}",
            f"target_commit={job.target_commit}",
            f"updated_at={job.updated_at}",
        ]
        if backup.is_dir():
            lines.append(f"backup_dir={backup}")
        if self._previous:
            lines.append(f"previous_commit={self._previous.get('commit')}")
            lines.append(f"previous_version={self._previous.get('version')}")
            lines.extend(self._previous_images_lines())
        lines.extend(self._recovery_context_lines())
        lines.extend(
            [
                "",
                "恢复前提与限制（逐步核对；这里不给可一键执行的命令）：",
                "1. 本执行器不会自动回滚数据库或镜像。needs_manual 的含义是"
                "“无法证明服务仍停在旧版本”，先人工确认当前实际运行的 commit/version。",
                "2. `docker compose ... up -d --no-build` **不是**回旧镜像：build 已经把新构建"
                "重新指向 Compose 使用的镜像名，--no-build 只是这次不构建。真正的回退必须把"
                "**更新前**核实保存的 api 与 worker **两个** image ID 重新 pin 回 Compose 用的"
                "镜像名；缺任一 ID（上面显示 unrecorded）就不能声称“回旧版本”，只能向前修复。",
                "3. 所有手工 compose 操作都必须带完整上下文：live 基础 compose + 持久 override"
                " + canonical env + 原 project。不要从旧源码 checkout 裸跑（相对 bind 会静默"
                "指向旧树的前端/证书）。",
                "4. 回旧镜像前先核对数据库 schema：alembic-current.txt 与旧镜像是否兼容；"
                "旧代码不认识新 schema 时“镜像回旧”不等于“服务回旧”，只能向前修或恢复备份。"
                "执行器不做自动 schema 回滚。",
                "5. 备份是 pg_dump 全量**逻辑** dump，不是 PITR：恢复会丢掉备份之后写入的全部"
                "数据。恢复后必须**重新应用**撤回墓碑（只核对清单不够），再校验 /readyz 与关键数据。",
                "6. 若阶段标记停在 build 且安装进程组已确认终止（旧服务仍在跑），可在 Web 重新"
                "发起更新；若标记已是 deploy/verify/complete 或读不到，必须先人工确认。",
                "7. 完整步骤与人工恢复的安全检查清单见 docs/UPDATES.md 第 8 节与"
                " deploy/update/README.md；恢复完成后由宿主执行 adopt-current 核验并解除门禁。",
                "8. backup_dir 里若存在 verify-warnings.txt（媒体归档为 partial、或旧格式"
                " manifest 的字段无法校验），恢复前先读它：那份备份可能缺媒体文件，"
                "恢复后必须按 media_ref 逐条核对。",
            ]
        )
        path = self.config.state_dir / f"recovery-{job.request_id}.txt"
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def _previous_images_lines(self) -> list[str]:
        """旧镜像记录：每个服务都必须单独列出；缺失就明确标 unrecorded。"""
        images = self._previous.get("images") if isinstance(self._previous, dict) else None
        containers = (
            self._previous.get("containers") if isinstance(self._previous, dict) else None
        )
        lines: list[str] = []
        for service in IMAGE_SERVICES:
            ids: list[str] = []
            if isinstance(images, dict):
                raw = images.get(service)
                if isinstance(raw, list):
                    ids = [item for item in raw if isinstance(item, str) and item]
            lines.append(
                f"previous_image_{service}=" + (",".join(ids) if ids else "unrecorded")
            )
            cids: list[str] = []
            if isinstance(containers, dict):
                raw_c = containers.get(service)
                if isinstance(raw_c, list):
                    cids = [item for item in raw_c if isinstance(item, str) and item]
            if cids:
                lines.append(f"previous_containers_{service}=" + ",".join(cids))
        return lines

    def _recovery_context_lines(self) -> list[str]:
        """给出恢复所需的**已捕获**上下文（路径都是绝对、宿主私有）。"""
        lines: list[str] = []
        try:
            live_root = self._live_root()
            lines.append(f"live_release_root={live_root}")
        except UpdateFailure:
            lines.append("live_release_root=unknown（installed 记录不可用，先核对真实运行树）")
        try:
            lines.append(f"canonical_env_file={self._protect_env_file()}")
        except UpdateFailure:
            lines.append("canonical_env_file=unknown（配置的 env 文件不可信）")
        override = self._live_override()
        lines.append(f"compose_override={override if override is not None else '（无）'}")
        lines.append(f"compose_project={self.config.project}")
        lines.append(
            "compose_note=手工命令必须组合 live 基础 compose + 上面的 override + 上面的"
            " env 文件 + 这个 project；备份目录里的 compose-resolved.yaml 是当次合并模型"
            "（0600，含口令，勿外传/勿提交）。"
        )
        return lines

    # ------------------------------------------------------------ CLI 辅助

    def _detect_live_release_root(self, runtime: RuntimeInfo) -> Path | None:
        """从运行容器的 ``/app/web`` 挂载反推 live release 根（不信任旧 installed）。"""
        roots: set[Path] = set()
        for container_id in runtime.replicas:
            mounts = self._container_mounts(container_id)
            if mounts is None:
                return None
            mount = mounts.get(WEB_MOUNT_TARGET)
            if mount is None or mount.type != "bind":
                return None
            source = Path(mount.source)
            # <release>/clients/web-console/dist
            if source.name != "dist":
                return None
            if source.parent.name != "web-console" or source.parent.parent.name != "clients":
                return None
            roots.add(source.parent.parent.parent)
        if len(roots) != 1:
            return None
        return self._validated_release_root(roots.pop())

    def _validated_release_root(self, root: Path) -> Path:
        """release 根必须是绝对、非符号链接的普通目录，且含普通 compose 文件。"""
        if not isinstance(root, Path) or not root.is_absolute():
            raise UpdateFailure("installed_commit_unknown")
        try:
            info = os.lstat(root)
        except OSError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UpdateFailure("installed_commit_unknown")
        try:
            compose_info = os.lstat(root / "deploy" / "compose.yaml")
        except OSError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        if not stat.S_ISREG(compose_info.st_mode):
            raise UpdateFailure("installed_commit_unknown")
        return root.resolve()

    def _validated_override(self, candidate: Path | None) -> Path | None:
        """人工显式提供的 override：必须在私有 override 目录内、且是普通文件。"""
        if candidate is None:
            return self._live_override()
        if not candidate.is_absolute():
            raise UpdateFailure("installed_commit_unknown")
        try:
            info = os.lstat(candidate)
        except OSError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        if not stat.S_ISREG(info.st_mode):
            raise UpdateFailure("installed_commit_unknown")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.config.overrides_dir.resolve())
        except ValueError as exc:
            raise UpdateFailure("installed_commit_unknown") from exc
        return resolved

    def _verify_live_release(self, root: Path, runtime: RuntimeInfo) -> bool:
        """live release 必须自证完整：dist 清单/版本文件/备份脚本都在且匹配运行版本。"""
        if not self._verify_web_manifest(root, runtime):
            return False
        versions = {
            _project_version(root),
            _module_version(root),
            _package_json_version(root),
        }
        if versions != {runtime.version}:
            return False
        try:
            self._backup_script(root)
        except UpdateFailure:
            return False
        return True

    def _verify_runtime_mounts(self, config: dict[str, Any], runtime: RuntimeInfo) -> bool:
        """运行容器的真实挂载必须与「该 release + 该 override」的 compose 模型一致。"""
        parsed = parse_compose_config(config)
        if parsed is None:
            return False
        api_ids = set(runtime.replicas)
        for service in sorted(parsed):
            ids = self._running_service_containers(service)
            if ids is None or not ids:
                return False
            if service == "api" and set(ids) != api_ids:
                return False
            expected = parsed.get(service) or {}
            for container_id in ids:
                actual = self._container_mounts(container_id)
                if actual is None or not runtime_mounts_match(expected, actual):
                    return False
        return True

    def adopt_current(
        self, *, release_root: Path | None = None, compose_override: Path | None = None
    ) -> RuntimeInfo:
        """核验真实部署并记为基线；只在**完全自证**时解除 needs_manual 门禁。

        规则（QA B4）：绝不把“旧 release_root / 旧 override + 新 runtime commit”
        盖章成基线。必须同时满足：

        1. 两把锁都在手（宿主锁 + install.sh 安装锁），证明没有并发安装/更新；
        2. 全部 api 副本 commit/version 一致，数量与 compose 期望一致；
        3. live release 根从**运行容器**的 ``/app/web`` 挂载反推（或人工显式
           ``--release-root`` 提供，同一标准核验）：compose 在、dist 清单与每个
           副本一致、版本文件与运行版本一致、live 备份脚本可执行；
        4. api 与 worker 的运行镜像 ID 都能完整读出（每服务唯一）；
        5. 运行容器的真实挂载与「该 release + 该 override」的 compose 模型逐项
           一致（挂载来源/只读位/服务集合），且 edge ``/readyz`` 严格 2xx。

        任一条不满足都会抛 ``installed_commit_unknown``：**不写 installed、不解除
        门禁**，人工需要先补齐完整来源（显式提供 release 根/override）或重新安装
        整个 release 后再来 adopt。
        """
        self.ensure_runtime_dirs()
        host_lock = self.protocol.try_lock(self.config.lock_path)
        if host_lock is None:
            raise UpdateFailure("update_in_progress")
        install = InstallLock(self.config.install_lock_path())
        if not install.acquire():
            host_lock.release()
            raise UpdateFailure("update_in_progress")
        try:
            runtime = self._runtime_info()
            root = (
                self._validated_release_root(release_root)
                if release_root is not None
                else self._detect_live_release_root(runtime)
            )
            if root is None:
                raise UpdateFailure("installed_commit_unknown")
            override = self._validated_override(compose_override)
            if not self._verify_live_release(root, runtime):
                raise UpdateFailure("installed_commit_unknown")
            config = self._compose_config_json(root=root, override=override)
            if config is None:
                raise UpdateFailure("installed_commit_unknown")
            if not self._verify_runtime_mounts(config, runtime):
                raise UpdateFailure("installed_commit_unknown")
            images: dict[str, list[str]] = {}
            containers: dict[str, list[str]] = {}
            for service in IMAGE_SERVICES:
                expected = self._expected_replicas(config, service)
                identity = self._service_images(service, expected)
                if identity is None:
                    raise UpdateFailure("installed_commit_unknown")
                images[service] = list(identity["image_ids"])
                containers[service] = list(identity["containers"])
            if not self._wait_edge_ready():
                raise UpdateFailure("installed_commit_unknown")
            previous = self.read_installed()
            # tag 只在“记录的 tag 与运行版本配对”时保留，否则置空：
            # 旧 tag + 新 commit 就是半切换状态，不能拿来盖章。
            keep_tag = (
                previous.get("target_commit") == runtime.commit
                and self.protocol.tag_version(str(previous.get("target_tag") or ""))
                == runtime.version
            )
            installed = {
                "schema_version": self.protocol.SCHEMA_VERSION,
                "target_tag": previous.get("target_tag") if keep_tag else None,
                "target_commit": runtime.commit,
                "version": runtime.version,
                "release_root": str(root),
                "env_file": str(self._protect_env_file()),
                "backup_dir": previous.get("backup_dir"),
                "previous": previous.get("previous") or {},
                "updated_at": self._now(),
                "adopted": True,
                "compose_override": str(override) if override is not None else None,
                "replicas": list(runtime.replicas),
                "images": images,
                "containers": containers,
            }
            self.write_installed(installed)
            current = self.read_current()
            if current is not None and current.state == "needs_manual":
                self.protocol.remove_file(self.config.status_dir / self.protocol.CURRENT_FILE)
                self.journal("manual-cleared", current.request_id, state="cleared")
            # 心跳在**解除门禁之后**再写：否则 write_heartbeat() 会把刚清掉的
            # needs_manual 当成 status=manual，API 继续报 manual_required。
            self.write_heartbeat()
            return runtime
        finally:
            install.release()
            host_lock.release()


# ---------------------------------------------------------------- 心跳


class HeartbeatThread(threading.Thread):
    def __init__(self, executor: Executor, interval: float) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self._executor = executor
        self._interval = max(interval, 1.0)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            with contextlib.suppress(Exception):
                self._executor.write_heartbeat()
            self.stop_event.wait(self._interval)

    def stop(self) -> None:
        self.stop_event.set()


# ---------------------------------------------------------------- 命令


def _doctor_line(ok: bool, label: str, detail: str = "") -> bool:
    prefix = "OK  " if ok else "FAIL"
    print(f"[{prefix}] {label}{('：' + detail) if detail else ''}")
    return ok


def cmd_init(config: UpdateConfig, protocol: Any) -> int:
    executor = Executor(config, protocol)
    executor.ensure_runtime_dirs()
    print("[executor] 目录已准备（安全权限）：")
    for label, path in (
        ("source_root", config.source_root),
        ("state_dir（仅宿主，0700）", config.state_dir),
        ("requests_dir（API gid 可写，0770）", config.requests_dir),
        ("status_dir（宿主写/容器只读，0755）", config.status_dir),
    ):
        try:
            mode = oct(os.stat(path).st_mode & 0o777)
        except OSError:
            mode = "?"
        print(f"  {label}: {path} ({mode})")
    print("[executor] 不会自动运行部署；下一步可执行 doctor / adopt-current / run。")
    return 0


def cmd_status(config: UpdateConfig, protocol: Any) -> int:
    executor = Executor(config, protocol)
    installed = executor.read_installed()
    current = executor.read_current()
    heartbeat = protocol.read_json_file(config.status_dir / protocol.HEARTBEAT_FILE) or {}
    print(f"source_root   {config.source_root}")
    print(f"state_dir     {config.state_dir}")
    print(f"installed     {installed.get('target_commit') or '（未 adopt）'} "
          f"{installed.get('version') or ''}")
    print(
        f"heartbeat     {heartbeat.get('last_seen_at') or '（无）'} "
        f"status={heartbeat.get('status') or '（缺失=协议不兼容）'} "
        f"pending={heartbeat.get('pending_state') or '-'}"
    )
    print(f"images        {installed.get('images') or '（未记录）'}")
    print(f"override      {installed.get('compose_override') or '（无）'}")
    print(f"journal       {'损坏' if executor._read_journal() is None else 'ok'}")
    if current is not None:
        print(f"current_job   {current.request_id} {current.state}/{current.stage} "
              f"{current.error_code or ''}")
    else:
        print("current_job   （无）")
    return 0


def cmd_doctor(config: UpdateConfig, protocol: Any) -> int:
    executor = Executor(config, protocol)
    failures = 0
    failures += not _doctor_line(config.source_root.is_dir(), "source_root 存在")
    clean = True
    try:
        result = executor._run(  # noqa: SLF001 - doctor 复用只读检查
            ["git", "-C", str(config.source_root), "status", "--porcelain"],
            timeout=QUICK_TIMEOUT_SECONDS,
            capture=True,
        )
        clean = result.returncode == 0 and not result.stdout.strip()
        if result.returncode != 0:
            clean = False
    except (ExecutorError, CommandTimeout):
        clean = False
    failures += not _doctor_line(clean, "已安装 checkout 干净（tracked/untracked）")
    for label, path, want in (
        ("state_dir", config.state_dir, 0o700),
        ("requests_dir", config.requests_dir, 0o770),
        ("status_dir", config.status_dir, 0o755),
    ):
        try:
            info = os.stat(path)
            mode = info.st_mode & 0o777
            good = os.path.isdir(path) and not os.path.islink(path) and (mode & 0o002) == 0
            if label == "requests_dir":
                good = good and info.st_gid == config.api_gid and (mode & 0o020) != 0
            if label == "status_dir":
                good = good and (mode & 0o022) == 0
            failures += not _doctor_line(good, f"{label} 权限", oct(mode))
        except OSError as exc:
            failures += not _doctor_line(False, f"{label} 权限", str(exc))
            del want
    try:
        env_mode = os.stat(config.env_file).st_mode & 0o777
        failures += not _doctor_line(
            (env_mode & 0o007) == 0, "env_file 不向 other 暴露", oct(env_mode)
        )
    except OSError:
        failures += not _doctor_line(False, "env_file 可读")
    try:
        result = executor._run(["docker", "version", "--format", "{{.Server.Version}}"],
                               timeout=QUICK_TIMEOUT_SECONDS, capture=True)
        failures += not _doctor_line(result.returncode == 0, "docker 可用")
    except (ExecutorError, CommandTimeout):
        failures += not _doctor_line(False, "docker 可用")
    installed = executor.read_installed()
    failures += not _doctor_line(
        bool(installed.get("target_commit")),
        "已记录 installed 提交（可先运行 adopt-current）",
        str(installed.get("target_commit") or ""),
    )
    journal_text = executor._read_journal()  # noqa: SLF001 - doctor 复用只读检查
    journal_ok = journal_text is not None
    if journal_text:
        for line in journal_text.splitlines():
            if not line.strip():
                continue
            try:
                if not isinstance(json.loads(line), dict):
                    journal_ok = False
                    break
            except ValueError:
                journal_ok = False
                break
    failures += not _doctor_line(journal_ok, "journal 可读且未损坏")
    override_raw = installed.get("compose_override")
    if override_raw:
        override_path = Path(str(override_raw))
        try:
            info = os.lstat(override_path)
            override_ok = (
                stat.S_ISREG(info.st_mode)
                and (info.st_mode & 0o777) == 0o600
                and override_path.parent == config.overrides_dir
            )
        except OSError:
            override_ok = False
        failures += not _doctor_line(
            override_ok, "已记录的 compose override 存在且 0600", str(override_path)
        )
    else:
        failures += not _doctor_line(True, "compose override（尚未生成）")
    try:
        lock_path = config.install_lock_path()
        install_lock = InstallLock(lock_path)
        acquired = install_lock.acquire()
        if acquired:
            install_lock.release()
        failures += not _doctor_line(acquired, "安装锁可用（当前无并发安装）", str(lock_path))
    except OSError as exc:
        failures += not _doctor_line(False, "安装锁可用", str(exc))
    return 0 if failures == 0 else 1


def cmd_run(config: UpdateConfig, protocol: Any, *, once: bool) -> int:
    executor = Executor(config, protocol)
    try:
        executor.ensure_runtime_dirs()
    except ConfigError as exc:
        print(f"[executor] {exc}", file=sys.stderr)
        return 2
    heartbeat = HeartbeatThread(executor, config.poll_seconds)
    heartbeat.start()
    stop = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        while True:
            try:
                outcome = executor.execute_once()
                if outcome not in ("idle", "busy"):
                    print(f"[executor] 本轮结果：{outcome}")
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能杀死常驻循环
                print(f"[executor] 本轮异常（已收敛，详情在宿主日志）：{type(exc).__name__}",
                      file=sys.stderr)
            if once:
                break
            if stop.wait(config.poll_seconds):
                break
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=5)
    return 0


def cmd_adopt_current(
    config: UpdateConfig,
    protocol: Any,
    *,
    release_root: Path | None = None,
    compose_override: Path | None = None,
) -> int:
    executor = Executor(config, protocol)
    try:
        runtime = executor.adopt_current(
            release_root=release_root, compose_override=compose_override
        )
    except (ConfigError, UpdateFailure) as exc:
        print(f"[executor] 无法采用当前部署：{exc}", file=sys.stderr)
        print(
            "[executor] 未写入基线、未解除 needs_manual。先补齐完整来源"
            "（例如用 --release-root 指定已核验的 release 根，或按 README 重新安装整个"
            " release）后重试；不要用旧配置给新运行版本盖章。",
            file=sys.stderr,
        )
        return 1
    print(f"[executor] 已记录当前部署：commit={runtime.commit} version={runtime.version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ling 宿主更新执行器（API 不接触 Docker；本脚本只由宿主显式运行）"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML 配置路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="创建 requests/status/state 目录并设置安全权限")
    sub.add_parser("doctor", help="检查目录权限、worktree 干净度、docker 与 installed 记录")
    run = sub.add_parser("run", help="常驻轮询（或 --once 只跑一轮）")
    run.add_argument("--once", action="store_true", help="只检查/执行一轮后退出")
    sub.add_parser("status", help="打印当前任务与心跳（宿主私有）")
    adopt = sub.add_parser(
        "adopt-current",
        help="核验并采用当前运行部署（需能自证 release 根/overrides/镜像，否则拒绝）",
    )
    adopt.add_argument(
        "--release-root",
        type=Path,
        default=None,
        help="显式提供 live release 根（仍会用同一标准与运行容器核验）",
    )
    adopt.add_argument(
        "--compose-override",
        type=Path,
        default=None,
        help="显式提供持久 compose override（必须位于私有 override 目录内）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    try:
        config = load_config(config_path)
        # 协议从**实际运行的 release** 加载（其次 source_root，最后脚本 checkout）：
        # source_root 可能还停在旧代码，旧协议会把新 schema 静默当 idle。
        roots = [config.source_root]
        live_root = installed_release_root(config.state_dir)
        if live_root is not None:
            roots.insert(0, live_root)
        protocol = load_protocol(*roots)
    except ConfigError as exc:
        print(f"[executor] 配置错误：{exc}", file=sys.stderr)
        return 2
    if args.command == "init":
        try:
            return cmd_init(config, protocol)
        except ConfigError as exc:
            print(f"[executor] init 失败：{exc}", file=sys.stderr)
            return 1
    if args.command == "doctor":
        return cmd_doctor(config, protocol)
    if args.command == "status":
        return cmd_status(config, protocol)
    if args.command == "adopt-current":
        return cmd_adopt_current(
            config,
            protocol,
            release_root=args.release_root,
            compose_override=args.compose_override,
        )
    if args.command == "run":
        return cmd_run(config, protocol, once=bool(args.once))
    return 2


if __name__ == "__main__":
    sys.exit(main())
