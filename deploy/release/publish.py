#!/usr/bin/env python3
"""宿主人工发布脚本（镜像模式，按 OCI 摘要切换 api/worker）。

这是 ``docs/IMAGE-DEPLOYMENT-DESIGN.md`` §5「人工发布协议（首版）」的宿主执行
入口：操作员只给一个**不可变 digest**，脚本按固定顺序做门禁、归档、备份、
拉取、显式迁移、切换与验证；任何一步失败都留下机器可读记录，且**不会**把
"Compose 退出 0"当成成功。

为什么是 Python（纯标准库）
==========================
宿主已经有 ``python3``：``deploy/install.sh`` 的锁与 Web 清单、以及
``deploy/update/executor.py`` 都依赖它。本脚本需要 JSON、flock、HTTP 与精确的
子进程参数，用 bash 3.2 写会增加转义/解析风险；纯标准库 Python 不需要宿主
安装任何依赖。**不要**给本脚本加第三方 import（宿主不装依赖）。需要 Python
3.9+（``str.removeprefix`` 与 ``dict[str, ...]`` 注解）。

可覆盖的容量/超时（全部有默认值，覆盖值仍会与真实余量比较）
------------------------------------------------------------
- ``LING_RELEASE_IMAGE_BUDGET_BYTES``：Docker 侧需要的最小余量（默认
  ``2×旧镜像 + 512MiB``）。
- ``LING_RELEASE_BACKUP_BUDGET_BYTES``：备份目录需要的最小余量（默认
  ``旧镜像 save + 最近一份 dump/媒体 + 256MiB``）。
- ``LING_RELEASE_READY_TIMEOUT_SECONDS`` / ``..._HEALTH_TIMEOUT_SECONDS`` /
  ``..._MIGRATE_TIMEOUT_SECONDS`` / ``..._BACKUP_TIMEOUT_SECONDS``：等待/命令超时。

用法
====
::

    python3 deploy/release/publish.py \
      --digest 'ghcr.io/qsbb/ling-api@sha256:<64 位小写十六进制>' \
      --backup-dir /vol1/1000/ling-backups \
      --env-file deploy/.env \
      --compose-file deploy/compose.image.yaml

    # 可选：显式给出发布记录/日志所在位置与源提交（用于独立核对）
    ... --record /vol1/1000/ling-backups/release-records/release.json \
        --source-commit <40 位源提交>

    # 可选：镜像模式 + 反向 WS 端口 override（必须与 backup/cron 用同一组）
    ... --compose-override deploy/compose.image.ws.example.yaml

必填参数是 ``--digest``。其余路径的默认值按脚本所在位置推导：
``deploy/compose.image.yaml``、主文件同目录的 ``.env``、``deploy/backup/backup.sh``。
备份目录必须显式给出（``--backup-dir`` 或 ``LING_BACKUP_DIR``），不猜历史路径。

**只接受 digest**：``latest``、可变 tag（``:v0.0.1``）、空值、非 64 位 hex、
非 ``ghcr.io/qsbb/ling-api`` 的仓库、带额外路径的引用一律在**执行任何 docker
命令之前**拒绝。脚本不代替人选择版本，也不做 ``latest`` 降级。

执行顺序（与设计 §5 一致）
==========================
1. 参数校验（不跑 docker）
2. 抢锁：``.ling-install.lock``（与 ``install.sh``/宿主机执行器同一把）+
   ``<backup-dir>/.backup.lock``（与 ``backup.sh``/cron 同一个锁目录，全程持有）
3. 预检（只读）：compose 解析、镜像/卷/端口/edge 残留、GHCR manifest、
   架构、磁盘、当前容器/提交、数据库 ``alembic_version``、目标镜像已知时的
   Alembic head
4. 归档回退材料：旧镜像日期化 tag + ``docker save``、旧 Compose 文件组、旧
   env（0600）、当前 digest/commit、卷身份
5. ``pull api worker`` 并核验身份（同一摘要、提交、平台、迁移链）；**此步不
   影响在线服务**
6. 维护窗口（**先停写再备份**）：捕获运行中 api 的 image ID（供 backup.sh 的一次性
   只读取数容器用）→ 停 **api + worker**（阻断 HTTP 写入与机器人入站）→ 确认两者
   真的进入 exited/created → 复用 ``deploy/backup/backup.sh`` 做一致备份并逐项
   校验 manifest
7. 单次显式迁移：``run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate``，
   核对"确实执行"、退出码 0、``alembic_version`` == 目标镜像内 head
8. 原子更新受限 env 里的 ``LING_APP_IMAGE``，按目标 digest 起 api → 验证
   宿主与容器内 ``/readyz``、镜像内 Web 清单、容器内**进程自报**的提交
   （``exec python -c``，**不**经 HTTP、不带凭据）、容器实际 image ID/digest
   → 起 worker 并验证两者是同一摘要与最终 ``alembic_version``
9. 失败处理见下方退出码表；已迁移后失败**不**自动切旧镜像、**不**自动恢复
   旧 dump

退出码（可区分失败阶段）
========================
====  ==========================================================
0     成功（仅当运行容器实况、版本接口与迁移 head 全部核对通过）
2     参数/用法错误（含拒绝非 digest）——未执行任何 docker 命令
10    预检失败——未做任何变更
11    拿不到跨工具锁——立即失败，不等待、不跳过
12    归档回退材料失败——未做任何变更
13    备份失败（含维护窗口内读不到取数镜像/停 api/停 worker 失败）——未迁移
14    拉取/镜像身份/迁移链核验失败——未迁移
15    迁移失败或迁移后 head 不匹配——不启动新 API，不自动恢复旧服务
16    迁移后的切换/验证失败——不自动切回旧镜像或恢复旧 dump
====  ==========================================================

失败时脚本会写明：当前**明确可知**的状态、已经做过的动作、下一步；未迁移的
失败会尝试恢复并**实测**旧服务（image ID + ``/readyz``）后才宣称"仍在旧版本"。

本脚本**不**做、也不能证明的事
============================
- 不验证 digest 的发布证明/签名。摘要到源提交的可信绑定来自 CI 与 Release
  证据（设计 §3/§5.1），本脚本只核对镜像内 ``LING_COMMIT`` 与 OCI 标注
  ``org.opencontainers.image.revision`` 一致；若给 ``--source-commit`` 则额外
  要求两者都等于它。没有发布证明时，摘要只能证明"取到了这组字节"。
- 不提供外部维护门禁（防火墙/反向代理/机器人入口）。脚本只能停容器；运行前
  必须由宿主/网络层阻断外部写入，否则验证期仍可能有流量。
- 不做数据库降级或自动恢复；已迁移后的恢复是人工决策（设计 §6）。
- 不做 ``down -v`` / ``prune`` / ``volume rm``，不碰命名卷身份。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE_REPOSITORY = "ghcr.io/qsbb/ling-api"
DIGEST_RE = re.compile(r"^ghcr\.io/qsbb/ling-api@sha256:([0-9a-f]{64})$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# 镜像模式的根密钥**只**存在于受限 env（容器里没有 /data/keys/root.key），而
# publish.py / updater.py 的进程环境里没有它。备份时必须把这一项单独注进
# backup.sh 的环境，否则 manifest 会记 keys_env_set=false。
ROOT_KEY_ENV_NAME = "LING_ROOT_KEY"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PREFLIGHT = 10
EXIT_LOCK = 11
EXIT_ARCHIVE = 12
EXIT_BACKUP = 13
EXIT_PULL = 14
EXIT_MIGRATE = 15
EXIT_POST_MIGRATION = 16

ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

DEFAULT_READY_TIMEOUT = 180.0
DEFAULT_HEALTH_TIMEOUT = 90.0
DEFAULT_MIGRATE_TIMEOUT = 600.0
DEFAULT_BACKUP_TIMEOUT = 3600.0
DEFAULT_COMMAND_TIMEOUT = 120.0

# 容器内探活前等 api 自身 healthy 的节奏，以及 exec 探活自身的有限重试。
# 前三个值不进 CLI：运维只需要用 --health-timeout 改"最多等多久"，轮询间隔与
# 重试次数属于实现细节，写死才能保证每一份发布走同样的等待语义。
API_HEALTH_POLL_SECONDS = 2.0
CONTAINER_READYZ_ATTEMPTS = 3
CONTAINER_READYZ_RETRY_SECONDS = 2.0
CONTAINER_READYZ_EXEC_TIMEOUT = 30.0
CONTAINER_VERSION_EXEC_TIMEOUT = 60.0

# --- HTTP 探活的唯一出口（**这张表必须与真实应用的鉴权一致**）-------------
#
# 发布/更新流程**不持有用户凭据**：一条能改宿主状态的通道不应该同时是凭据的
# 另一个持有者（攻击面只会变大）。所以这里只允许打**免鉴权**的端点。
#
# `/readyz` 是唯一被允许的那个：它的鉴权是被**刻意降级**的——容器 healthcheck
# 必须能探它，未登录时它只回 `{"status": "ready"}`，不含任何部署指纹（见
# `src/ling/api/routes/health.py` 的模块 docstring）。
#
# `/v1/version` **不在这张表里**：它挂 `CurrentAuth`，未鉴权永远 401。真机演练
# P5 第 6 个缺陷就是拿它当探活（`failed_after_migration / version_container`）。
# "运行容器是不是目标构建"改用容器内进程证据，见 `verify_running_commit`。
#
# 防回归：`tests/integration/test_version_image_update.py` 会
#   1) 对着**真实 FastAPI 应用**逐条核这张表（未鉴权不是 401/403）；
#   2) 扫描本文件与 `deploy/updater/updater.py` 里的 `http://` 字面量，
#      新增探活却忘了登记 → 测试红。
HTTP_PROBE_PATHS: tuple[str, ...] = ("/readyz",)

# --- 宿主端口探活：**必须借一个 --network host 的一次性容器**（缺陷 7）-------
#
# 真机演练 P5 第 7 个缺陷：容器内验证全过、正式 compose 发布端口也成功，最后却
# 失败在 ``failed_after_migration / readyz``——宿主 6521 在 180s 内没有返回 200
# （最后 0）。原因是那条探测是**更新器容器进程自己**打的
# http://127.0.0.1:<宿主端口>/readyz （更新器按安全设计跑在容器里：compose
# 只给它一条 egress 网络；``--network none`` 的演练里连 egress 都没有），容器里
# 的 127.0.0.1 是**容器自己**的环回，宿主端口上什么都没监听，等多久都是 0。
#
# 所以：publish/updater 进程**不再自己发这个 HTTP 请求**，改为起一个一次性的
# ``--network host`` 容器去打它：
#   - 镜像用更新器**自己的镜像**（容器里一定已存在、已核验、带 python），宿主上
#     直接跑 publish.py 时回退到已核验的目标镜像；都可用 --host-probe-image 覆盖；
#   - ``--network host`` 是硬要求：否则又回到"看不见宿主端口"；
#   - ``--read-only --tmpfs /tmp --pull=never`` 与更新器同样的最小权限；
#   - **不**加 ``--privileged``、**不**挂 docker socket：这个探针只需要 TCP。
# 探针不跟随 3xx（沿用仓库里 ``_NoRedirectHandler`` 的思路），并把真实状态码
# 打到 stdout：进程只读退出码/状态码，3xx 仍然按 ``readyz_redirect`` 失败。
HOST_READYZ_PROBE_USER_AGENT = "ling-host-readyz-probe/1"
HOST_READYZ_PROBE_TIMEOUT = 30.0
HOST_PROBE_IMAGE_ENV = "LING_HOST_PROBE_IMAGE"
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
HOST_READYZ_PROBE = (
    "import sys, urllib.request\n"
    "class _NoRedirect(urllib.request.HTTPRedirectHandler):\n"
    "    def redirect_request(self, req, fp, code, msg, headers, newurl):\n"
    "        return None\n"
    "status = 0\n"
    "try:\n"
    "    opener = urllib.request.build_opener(_NoRedirect)\n"
    "    request = urllib.request.Request('http://{host}:{port}/readyz')\n"
    "    request.add_header('User-Agent', '" + HOST_READYZ_PROBE_USER_AGENT + "')\n"
    "    status = opener.open(request, timeout=3).status\n"
    "except urllib.error.HTTPError as exc:\n"
    "    status = exc.code\n"
    "except Exception:\n"
    "    status = 0\n"
    "print(status)\n"
    "sys.exit(0 if status == 200 else 1)\n"
)

# 容器内**进程证据**探针：让应用自己报出版本与构建提交，不发任何 HTTP、不读
# 任何配置/秘密。`LING_COMMIT` 由 `deploy/Dockerfile.api` 在构建时写进镜像 ENV，
# 与 OCI 标注 `org.opencontainers.image.revision` 同源；`ling.__version__` 来自
# 镜像里装好的 wheel。两者都不需要凭据。
CONTAINER_VERSION_PROBE = (
    "import json, os; from ling import __version__; "
    "print(json.dumps({'version': __version__, "
    "'commit': os.environ.get('LING_COMMIT', '').strip()}))"
)
MAX_CAPTURED_BODY = 2 * 1024 * 1024
MAX_ERROR_TAIL = 2000

# --- 更新通道挂载白名单（**必须**与 scripts/check_compose.py 的 UPDATE_MOUNTS 一致）---
#
# P5 接线（提交 9334c6f）后 `deploy/compose.image.yaml` 的 api **必须**挂两条宿主
# 目录（bind）：
#
#   ${LING_UPDATE_REQUESTS_HOST:?...}:/run/ling-update/requests:rw
#   ${LING_UPDATE_STATUS_HOST:?...}:/run/ling-update/status:ro
#
# 旧规则「api/worker 的每一个挂载都必须是命名卷」会让预检拒绝脚本自己该部署的
# 那份 compose（真机演练实测：exit 10 / failure.step=compose_mounts），镜像模式下
# 网页更新与发布都走不通。所以规则改成**精确白名单**（不是删掉检查）：
#
# - 只有 api 能挂，且只能是下面这两个**精确**目标（多一个都不行）；
# - requests 必须 rw（原子写请求文件）、status 必须 ro（更新器是唯一写者，
#   API 写不了才伪造不了状态）；
# - 挂载源必须是宿主目录（bind），不能是命名卷——这是
#   `scripts/check_compose.py` 那条「源必须是 ${VAR:?...} 必填插值」在**已解析**
#   模型（`docker compose config --format json`）上的等价形式：插值解析后宿主
#   路径就长成一条 bind；
# - worker/db 仍然一条 `/run/ling-update*` 都不许挂：后台任务不能拿到"触发部署"
#   的写面。
#
# **改一边要同步另一边**：两边不一致不是"两道检查"，而是一边放行一边拒绝——真机
# 演练就是这么发现的（单元测试用自造 stub 模型，里面没有这两条挂载，所以没抓到）。
UPDATE_TARGET_PREFIX = "/run/ling-update"
UPDATE_MOUNTS: dict[str, str] = {
    f"{UPDATE_TARGET_PREFIX}/requests": "rw",
    f"{UPDATE_TARGET_PREFIX}/status": "ro",
}

ALEMBIC_HEAD_SCRIPT = """\
import os
from alembic.config import Config
from alembic.script import ScriptDirectory

root = os.environ.get("LING_PROJECT_ROOT", "/app")
cfg = Config(os.path.join(root, "alembic.ini"))
cfg.set_main_option("script_location", os.path.join(root, "migrations"))
script = ScriptDirectory.from_config(cfg)
for head in script.get_heads():
    print("HEAD=" + head)
for revision in script.walk_revisions():
    print("REV=" + revision.revision)
    parents = revision.down_revision
    if isinstance(parents, str):
        parents = (parents,)
    if parents:
        for parent in parents:
            print("PARENT=" + revision.revision + ":" + str(parent))
"""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def host_arch() -> str:
    machine = platform.machine().strip().lower()
    return ARCH_ALIASES.get(machine, machine)


def tail(text: str, limit: int = MAX_ERROR_TAIL) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def parse_container_version_probe(stdout: str) -> dict[str, Any]:
    """取容器内探针打印的 JSON（从后往前找第一条可解析的对象）。

    容器里跑的是应用自己的 Python，镜像启动脚本可能先打印别的东西；所以从
    输出**末尾**往前找第一条合法 JSON，绝不把整段 stdout 当 JSON 解析。
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """把 3xx 当错误抛出，绝不跟随 Location。

    ``urllib.request.urlopen`` 默认跟随 301/302/303/307/308：一个 302→200 的
    响应会被读成 200，``/readyz`` 的"恰好 200"门禁就被洗掉了。这里覆盖所有
    重定向分支，让 :meth:`Context.http_get` 拿到真实状态码。
    """

    def _reject(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + rename；创建瞬间就带目标权限位。"""
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._file: Path | None = None

    def emit(self, message: str) -> None:
        line = f"[publish] {message}"
        print(line, file=sys.stderr, flush=True)
        self.lines.append(line)
        if self._file is not None:
            try:
                with self._file.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass

    def command(self, argv: Sequence[str]) -> None:
        # 只记录 argv；绝不打印 env（可能含密钥）。
        self.emit("$ " + " ".join(argv))

    def attach(self, path: Path) -> None:
        self._file = path
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                for line in self.lines:
                    stream.write(line + "\n")
            os.chmod(path, 0o600)
        except OSError:
            self._file = None


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        cwd: Path | None = None,
    ) -> Result:
        self.logger.command(argv)
        try:
            proc = subprocess.run(
                list(argv),
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return Result(tuple(argv), 127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return Result(tuple(argv), 124, stdout, stderr + f"\n超时（{timeout}s）")
        if proc.returncode != 0:
            detail = proc.stderr or proc.stdout
            if detail.strip():
                self.logger.emit(f"（rc={proc.returncode}）{tail(detail)}")
        return Result(tuple(argv), proc.returncode, proc.stdout, proc.stderr)


class PublishError(Exception):
    def __init__(
        self,
        exit_code: int,
        status: str,
        step: str,
        message: str,
        next_actions: Sequence[str] = (),
        *,
        after_migration: bool = False,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status
        self.step = step
        self.message = message
        self.next_actions = list(next_actions)
        self.after_migration = after_migration


@dataclass
class Options:
    target: str
    target_hex: str
    compose_file: Path
    compose_override: Path | None
    env_file: Path
    project: str
    backup_dir: Path
    backup_script: Path
    record_path: Path
    snapshot_dir: Path
    source_commit: str | None
    ready_timeout: float
    health_timeout: float
    migrate_timeout: float
    backup_timeout: float
    host_arch: str = field(default_factory=host_arch)
    # 宿主端口探针用哪个镜像（None = 自动：更新器自己的镜像 → 目标镜像）。
    # 必须是本机**已有**的镜像：探针带 --pull=never，不会为它再拉一次。
    host_probe_image: str | None = None


def build_parser() -> argparse.ArgumentParser:
    deploy_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        prog="publish.py",
        description="按 OCI digest 发布 ling 镜像（人工、有门禁、失败不误报）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 成功；2 参数错误；10 预检；11 锁；12 归档；"
            "13 备份；14 拉取/身份；15 迁移；16 迁移后失败。"
        ),
    )
    parser.add_argument(
        "--digest",
        required=True,
        help="目标摘要，必须形如 ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
    )
    parser.add_argument(
        "--compose-file",
        default=str(deploy_dir / "compose.image.yaml"),
        help="镜像模式主 Compose 文件（默认 deploy/compose.image.yaml）",
    )
    parser.add_argument(
        "--compose-override",
        default=os.environ.get("LING_COMPOSE_OVERRIDE", ""),
        help="可选 override；必须与 backup/cron 同一组 -f（主文件在前）",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("LING_COMPOSE_ENV_FILE", ""),
        help="受限 env（默认主文件同目录的 .env）",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("LING_COMPOSE_PROJECT", "ling"),
        help="Compose 项目名（默认 ling）",
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("LING_BACKUP_DIR", ""),
        help="备份根目录（必填；本脚本在其中创建 release-<id>/ 快照）",
    )
    parser.add_argument(
        "--backup-script",
        default=str(deploy_dir / "backup" / "backup.sh"),
        help="复用 deploy/backup/backup.sh（默认按脚本位置推导）",
    )
    parser.add_argument(
        "--record",
        default="",
        help="发布记录 JSON 路径（默认 <backup-dir>/release-records/release-<id>.json）",
    )
    parser.add_argument(
        "--source-commit",
        default="",
        help="可选：期望的完整源提交（40 位 hex），用于独立核对镜像标注",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=float(os.environ.get("LING_RELEASE_READY_TIMEOUT_SECONDS", DEFAULT_READY_TIMEOUT)),
        help="等待 /readyz 的秒数（默认 180）",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=float(os.environ.get("LING_RELEASE_HEALTH_TIMEOUT_SECONDS", DEFAULT_HEALTH_TIMEOUT)),
        help="等待 worker health 的秒数（默认 90）",
    )
    parser.add_argument(
        "--migrate-timeout",
        type=float,
        default=float(os.environ.get("LING_RELEASE_MIGRATE_TIMEOUT_SECONDS", DEFAULT_MIGRATE_TIMEOUT)),
        help="一次性迁移命令的超时秒数（默认 600）",
    )
    parser.add_argument(
        "--backup-timeout",
        type=float,
        default=float(os.environ.get("LING_RELEASE_BACKUP_TIMEOUT_SECONDS", DEFAULT_BACKUP_TIMEOUT)),
        help="backup.sh 的超时秒数（默认 3600）",
    )
    parser.add_argument(
        "--host-probe-image",
        default=os.environ.get(HOST_PROBE_IMAGE_ENV, ""),
        help=(
            "宿主 /readyz 探针容器用的镜像（默认自动：更新器自己的镜像 → 目标镜像）。"
            "必须是本机已有镜像，探针带 --pull=never。"
        ),
    )
    return parser


def parse_options(argv: Sequence[str] | None) -> Options:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = args.digest
    match = DIGEST_RE.fullmatch(target)
    if match is None:
        raise PublishError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                "拒绝非 digest 目标：只接受 ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>；"
                f"拿到 {target!r}。latest、可变 tag、空值、其他 registry/仓库、"
                "带额外路径或非 64 位 hex 都会被拒绝。"
            ),
            [
                "从 CI 发布证据/Release 的不可变记录中取得 index digest，"
                "不要用 latest 或 tag 代替。",
            ],
        )
    if not PROJECT_RE.fullmatch(args.project):
        raise PublishError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"项目名不合法：{args.project!r}（只允许小写字母/数字/_-，且以字母数字开头）",
        )
    if not args.backup_dir:
        raise PublishError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            "缺少 --backup-dir（或环境变量 LING_BACKUP_DIR）；脚本不猜历史备份路径",
            ["指定与现有 cron 相同的备份根目录。"],
        )
    source_commit = args.source_commit.strip()
    if source_commit and COMMIT_RE.fullmatch(source_commit) is None:
        raise PublishError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--source-commit 必须是 40 位小写 hex 的完整提交号；拿到 {source_commit!r}",
        )
    for name, value in (
        ("--ready-timeout", args.ready_timeout),
        ("--health-timeout", args.health_timeout),
        ("--migrate-timeout", args.migrate_timeout),
        ("--backup-timeout", args.backup_timeout),
    ):
        if value <= 0:
            raise PublishError(
                EXIT_USAGE, "failed_usage", "arguments", f"{name} 必须 > 0（拿到 {value}）"
            )
    host_probe_image = args.host_probe_image.strip()
    if host_probe_image.startswith("-"):
        # 不能以 - 开头：否则它会变成 docker run 的一个新旗标（argv 注入面）。
        raise PublishError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--host-probe-image 不能以 '-' 开头（拿到 {host_probe_image!r}）",
            ["写成本机已有的镜像引用；探针带 --pull=never，不会替它拉取。"],
        )

    compose_file = Path(args.compose_file).expanduser().resolve()
    override_raw = args.compose_override.strip()
    compose_override = (
        Path(override_raw).expanduser().resolve() if override_raw else None
    )
    env_raw = args.env_file.strip()
    env_file = (
        Path(env_raw).expanduser().resolve()
        if env_raw
        else compose_file.parent / ".env"
    )
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    backup_script = Path(args.backup_script).expanduser().resolve()
    record_id = f"{local_stamp()}-{match.group(1)[:12]}"
    snapshot_dir = backup_dir / f"release-{record_id}"
    record_path = (
        Path(args.record).expanduser().resolve()
        if args.record.strip()
        else backup_dir / "release-records" / f"release-{record_id}.json"
    )
    return Options(
        target=target,
        target_hex=match.group(1),
        compose_file=compose_file,
        compose_override=compose_override,
        env_file=env_file,
        project=args.project,
        backup_dir=backup_dir,
        backup_script=backup_script,
        record_path=record_path,
        snapshot_dir=snapshot_dir,
        source_commit=source_commit or None,
        ready_timeout=args.ready_timeout,
        health_timeout=args.health_timeout,
        migrate_timeout=args.migrate_timeout,
        backup_timeout=args.backup_timeout,
        host_probe_image=host_probe_image or None,
    )


def new_record(options: Options) -> dict[str, Any]:
    return {
        "schema": 1,
        "record_id": options.snapshot_dir.name.removeprefix("release-"),
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "target": {
            "digest": options.target,
            "repository": IMAGE_REPOSITORY,
            "digest_hex": options.target_hex,
            "source_commit": options.source_commit,
            "source_commit_source": "operator" if options.source_commit else None,
            "platform_digest": None,
            "image_id": None,
            "architecture": None,
            "os": None,
        },
        "project": options.project,
        "host_arch": options.host_arch,
        "published_port": None,
        "published_ports": [],
        "ready_host": None,
        "disk": None,
        "compose": {
            "file": str(options.compose_file),
            "override": str(options.compose_override) if options.compose_override else None,
            "file_group": [
                item
                for item in (str(options.compose_file), str(options.compose_override))
                if item
            ],
            "env_file": str(options.env_file),
            "env_file_realpath": None,
            "env_sha256_before": None,
            "env_sha256_after": None,
        },
        "locks": {
            "install": None,
            "backup": None,
        },
        # 预检时从**合并后模型**观察到的 api 开关值（只审计，不判定）。
        # `LING_UPDATE_ENABLED` 不再是门禁：并发由 updater.lock + InstallLock
        # 两把锁保证，开关只表示"网页可以发起更新"。见 check_compose_model 的注释。
        "compose_env": {
            "LING_UPDATE_ENABLED": None,
            "LING_RUN_MIGRATIONS": None,
        },
        "volumes": {},
        "previous": {
            "api": None,
            "worker": None,
            "alembic_version": None,
        },
        "archive": {
            "dir": str(options.snapshot_dir),
            "complete": False,
            "images": [],
            "files": [],
        },
        "backup": {
            "dir": str(options.snapshot_dir),
            "script": str(options.backup_script),
            "manifest": None,
            "dump": None,
            "dump_sha256": None,
            "tombstones_state": None,
            "keys_root": None,
            "keys_root_sha256": None,
            "keys_env_set": None,
            "media": None,
            "media_state": None,
            "media_sha256": None,
            "complete": False,
        },
        "migration": {
            "target_head": None,
            "target_revisions_known": False,
            "current_before": None,
            "current_at_migrate": None,
            "current_after": None,
            "attempted": False,
            "executed_marker": False,
            "skip_marker": False,
            "exit_code": None,
            "log": None,
        },
        "result": {
            "api": None,
            "worker": None,
            "readyz_http": None,
            "readyz_probe_image": None,
            "readyz_probe_attempts": 0,
            "version_commit": None,
            "web_manifest_verified": False,
            "alembic_version_after_start": None,
        },
        "rollback": {
            "attempted": False,
            "verified": False,
            "detail": None,
        },
        "failure": None,
        "next_actions": [],
    }


class InstallLock:
    """与 install.sh / 宿主更新执行器同一把锁（``.ling-install.lock``）。

    路径口径与 install.sh 完全一致：``deploy/.env`` **解析后真实目标**所在目录
    下的 ``.ling-install.lock``。用 fcntl.flock 非阻塞独占；拿不到立即失败。

    权限位由**安装器**决定（``ling.py::ensure_install_lock``）：属主是安装者，
    更新器是另一个 uid 时属组=共享组、模式 0660（flock 用 ``O_RDWR``，需要组写
    位）；同一 uid 或未配更新器时是 0600。这里的 ``0o600`` 只是本进程**首次创建**
    时的 umask，不是权限策略：**不要**在这里 chmod/chown——
    1. 宿主的 ``publish.py`` 以属主身份跑，属主位够用；
    2. 更新器容器里复用本类时（``/srv/ling/.ling-install.lock`` 是单文件挂载，
       文件已存在）走的是共享组位，把模式改回 0600 会让下一次更新 EACCES，
       并被 updater.py 误归类成 ``install_lock_busy``。
    """

    def __init__(self, env_file: Path) -> None:
        self.path = Path(os.path.realpath(env_file)).parent / ".ling-install.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                "当前平台不支持 O_NOFOLLOW，拒绝打开安装锁（避免跟随符号链接）",
            )
        try:
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | nofollow,
                0o600,
            )
        except OSError as exc:
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                f"无法打开安装锁 {self.path}：{exc}",
                ["检查该路径的权限/类型（锁必须是普通文件，不能是符号链接）。"],
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PublishError(
                    EXIT_LOCK,
                    "failed_lock",
                    "lock",
                    f"安装锁必须是普通文件：{self.path}",
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PublishError(
                    EXIT_LOCK,
                    "failed_lock",
                    "lock",
                    (
                        f"另一个安装/发布/更新任务正持有安装锁：{self.path}。"
                        "本脚本不等待、不跳过。"
                    ),
                    [
                        "等待 install.sh/宿主更新执行器结束；确认没有残留进程后重跑。",
                        "不要手工删除锁文件来绕过互斥（flock 随进程释放）。",
                    ],
                ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

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


class BackupDirLock:
    """与 backup.sh / cron 同一个锁目录：``<OUT_DIR>/.backup.lock``。

    backup.sh 用 ``mkdir`` 原子抢锁，目录里写 pid/started_at。本脚本用同一格式
    抢同一个目录，并在整个「备份→切换→验证」期间持有；因此 cron 在发布期间
    启动会像遇到另一个备份实例一样 exit 6。本脚本**不**接管残留锁：拿不到就
    失败，把 pid/启动时间留给操作员判断。
    """

    def __init__(self, backup_dir: Path) -> None:
        self.path = backup_dir / ".backup.lock"
        self._acquired = False
        self._identity: tuple[int, int] | None = None

    def acquire(self) -> None:
        try:
            self.path.mkdir(mode=0o700)
        except FileExistsError as exc:
            pid = ""
            started = ""
            try:
                pid = (self.path / "pid").read_text(encoding="utf-8").strip()
                started = (self.path / "started_at").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                (
                    f"备份锁已被占用：{self.path}"
                    f"（pid={pid or '未知'}，启动时间={started or '未知'}）"
                ),
                [
                    "确认 backup.sh/cron 是否正在运行；等它结束后重跑。",
                    "不要用 --force-unlock 之外的猜测方式删锁；本脚本不会强开。",
                ],
            ) from exc
        except OSError as exc:
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                f"无法创建备份锁 {self.path}：{exc}",
            ) from exc
        try:
            (self.path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            (self.path / "started_at").write_text(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            shutil.rmtree(self.path, ignore_errors=True)
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                f"写入备份锁失败 {self.path}：{exc}",
            ) from exc
        try:
            info = self.path.stat()
        except OSError:
            shutil.rmtree(self.path, ignore_errors=True)
            raise PublishError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                f"备份锁在创建后消失了：{self.path}",
            ) from None
        self._identity = (info.st_dev, info.st_ino)
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        if self._identity is not None:
            try:
                current = self.path.stat()
            except OSError:
                return
            if (current.st_dev, current.st_ino) != self._identity:
                # 锁目录被替换：绝不删除别人的目录。
                return
        shutil.rmtree(self.path, ignore_errors=True)


def parse_manifest(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not path.is_file():
        raise ValueError(f"manifest 不存在：{path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key in fields:
            raise ValueError(f"manifest 字段重复：{key}")
        fields[key] = value
    return fields


def parse_env_file_value(text: str, name: str = ROOT_KEY_ENV_NAME) -> str | None:
    """从受限 env 文本里取一个键的值；只解析，**不执行**、不回显。

    语义与 ``deploy/backup/install-cron.sh`` 里那套 ``sed | tail | tr`` 等价，
    并显式支持 ``export KEY=VALUE``：

    - 同一键出现多次以**最后一行**为准（env-file/compose 语义）；
    - 忽略空行与注释行（前导空白后的 ``#``）；行尾 CR（CRLF）不算内容；
    - 键必须紧贴 ``=``（``LING_ROOT_KEY=``，不是 ``LING_ROOT_KEY =``）；
    - 去掉值两端空白，再去掉一层成对引号（不 source、不解释变量）；
    - 最后一行是 ``LING_ROOT_KEY=`` 等于没给（与 install-cron 的判空一致）。

    宁可拒绝也不静默截断：引号没闭合（值可能跨行）、值里含换行/回车/其他不可
    打印字符、双引号里出现 ``\\n`` / ``\\r`` 转义（compose 会把转义变成真正的
    换行，backup.sh 拿到的却是字面量，两边会静默分歧）都抛 ``ValueError``。
    异常信息只给行号与原因，**绝不含值本身**。
    """
    found = False
    value = ""
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        line = line.lstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and len(line) > 6 and line[6] in " \t":
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key != name:
            continue
        candidate = raw_value.strip()
        if candidate[:1] in {"'", '"'}:
            quote = candidate[0]
            if len(candidate) < 2 or not candidate.endswith(quote):
                raise ValueError(
                    f"第 {number} 行的 {name} 引号未闭合（值可能跨行）；拒绝猜测"
                )
            candidate = candidate[1:-1]
            if quote == '"' and ("\\n" in candidate or "\\r" in candidate):
                raise ValueError(
                    f"第 {number} 行的 {name} 含换行转义；拒绝把可能带换行的值注入备份"
                )
        if not candidate.isprintable():
            raise ValueError(
                f"第 {number} 行的 {name} 含换行/不可打印字符；拒绝注入备份"
            )
        value = candidate
        found = True
    if not found or not value:
        return None
    return value


def service_environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                result[key] = value
        return result
    if isinstance(raw, dict):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    return {}


def service_volumes(service: dict[str, Any]) -> list[dict[str, Any]]:
    raw = service.get("volumes") or []
    return [item for item in raw if isinstance(item, dict)]


def is_update_mount_target(target: str) -> bool:
    """这条挂载点是否落在更新通道的目录树里（``/run/ling-update`` 及其子目录）。"""
    return target == UPDATE_TARGET_PREFIX or target.startswith(f"{UPDATE_TARGET_PREFIX}/")


def mount_mode(mount: dict[str, Any]) -> str:
    """挂载模式，归一到 ``"rw"`` / ``"ro"``。

    ``docker compose config --format json`` 给的是长语法：只读时 ``read_only`` 为
    ``true``，读写时该键省略（或为 false）。所以缺省按 rw 处理，与 Compose 的
    默认语义一致——只查 target 会把"requests 挂成 ro"这种缺陷放过去：目录在，
    但 API 写不进去，网页更新 POST 必然失败。
    """
    return "ro" if mount.get("read_only") else "rw"


def mount_is_host_bind(mount: dict[str, Any]) -> bool:
    """这条挂载是否挂宿主目录（bind）。

    已解析的模型里 ``type`` 直接给出（``bind`` / ``volume``）；``type`` 缺失时退回
    看源是不是宿主路径（绝对/相对/家目录），命名卷一律不算。
    """
    if mount.get("type") == "volume":
        return False
    source = str(mount.get("source") or "")
    return source.startswith(("/", ".", "~"))


def update_mount_violation(service_name: str, mount: dict[str, Any]) -> str | None:
    """更新通道挂载的白名单判定：允许返回 ``None``，拒绝返回**具体原因**。

    规则与 ``scripts/check_compose.py`` 的 ``UPDATE_MOUNTS`` 逐条对应（见文件头
    常量处的说明）。非更新通道的挂载不在这里判定，由调用方按"只许命名卷"处理。

    错误信息必须指名"哪条挂载、为什么不许"：只说"存在非命名卷挂载"时，真机上
    分不清是允许的 requests/status，还是漏进来的 ``/app/web`` / ``docker.sock``。
    """
    target = str(mount.get("target") or "")
    if not is_update_mount_target(target):
        return None
    source = str(mount.get("source") or "")
    mode = mount_mode(mount)
    what = (
        f"{service_name} 的挂载 source={source!r} target={target!r} "
        f"type={mount.get('type')!r} mode={mode}"
    )
    if service_name != "api":
        return (
            f"{what}：更新通道路径只允许 api 挂（API 仅获得「写请求/读状态」的最小"
            f"权限），{service_name} 挂上等于多一个能触发部署的写面；"
            f"允许的挂载只有 api 的 {sorted(UPDATE_MOUNTS)}。"
        )
    expected = UPDATE_MOUNTS.get(target)
    if expected is None:
        return (
            f"{what}：api 只允许挂 {sorted(UPDATE_MOUNTS)} 这两个更新通道目录，"
            "多给一个目录就是多一份写面。"
        )
    if not mount_is_host_bind(mount):
        return (
            f"{what}：{target} 必须挂宿主目录（bind），不允许命名卷——"
            "更新器的请求/状态目录属组在宿主侧（安装器设置），换一份数据面等于 "
            "API 与更新器看不到同一个目录；scripts/check_compose.py 也要求这条源"
            "必须是 ${VAR:?...} 必填插值。"
        )
    if mode != expected:
        why = (
            "requests 只读等于 API 写不了请求文件，网页更新 POST 必然失败"
            if expected == "rw"
            else "status 可写等于 API 能伪造更新器状态（更新器必须是唯一写者）"
        )
        return f"{what}：{target} 必须以 {expected} 挂载，实际 {mode}——{why}。"
    return None


def non_volume_mount_reason(service_name: str, mount: dict[str, Any]) -> str:
    """非命名卷挂载的拒绝理由（按危险类型给出具体原因，而不是一句笼统的话）。"""
    source = str(mount.get("source") or "")
    target = str(mount.get("target") or "")
    if target == "/app/web" or target.startswith("/app/web/"):
        why = "Web 构建产物已烤进镜像，这条 bind 会让镜像里的 /app/web 被宿主目录遮蔽"
    elif "docker.sock" in source:
        why = "挂 Docker socket 等于把宿主 Docker 控制权交给容器（等价 root）"
    else:
        why = "镜像模式只允许命名卷；源码/前端 bind 与宿主目录一律不允许"
    return (
        f"{service_name} 存在非命名卷挂载：type={mount.get('type')!r} "
        f"source={source!r} target={target!r}；{why}。"
    )


# 一条 ``published`` 值最多展开多少个端口。Compose 允许端口范围，预检是逐个
# ``docker ps --filter publish=N/tcp`` 核对的，范围大到没法逐个核对时宁可拒绝，
# 也不静默跳过（跳过等于把"端口冲突"放回切换阶段才暴露）。
MAX_PUBLISHED_PORT_RANGE = 64


def parse_published_port(raw: Any) -> list[int] | None:
    """解析一条 compose ``ports`` 条目的**宿主侧**值（``published``）。

    ``docker compose config --format json`` 给的是长语法，``published`` 通常是
    数字字符串（如 ``"6520"``）；Compose 也允许 ``"8000-8005"`` 这样的范围。
    返回展开后的宿主端口列表；**空值**表示由 Docker 随机分配（没有固定宿主机
    端口要核对），返回空列表；**无法解析返回 None**——调用方必须拒绝，不能把
    解析不了的端口当成"没有端口"。
    """
    if raw is None:
        return []
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return [raw] if 1 <= raw <= 65535 else None
    text = str(raw).strip()
    if not text:
        return []
    start_text, sep, end_text = text.partition("-")
    if sep:
        try:
            start, end = int(start_text), int(end_text)
        except ValueError:
            return None
        if not (1 <= start <= end <= 65535):
            return None
        if end - start + 1 > MAX_PUBLISHED_PORT_RANGE:
            return None
        return list(range(start, end + 1))
    try:
        port = int(text)
    except ValueError:
        return None
    return [port] if 1 <= port <= 65535 else None


def service_published_ports(services: dict[str, Any]) -> list[dict[str, Any]]:
    """从**合并后**的 compose 模型取出所有服务发布到宿主的端口。

    ``docker compose config --format json`` 已经应用了 ``-f`` override，而 Compose
    的 ``ports`` 是**追加**语义（override 只加条目，不覆盖主文件）。所以必须按
    合并后的模型对**每个服务**取并集：只盯 api 的 6520 会漏掉
    ``compose.image.ws.example.yaml`` 追加的 WS 端口——那类冲突要到切换阶段的
    ``up -d api`` 才由 Docker 报错，那时已经迁移过了，违反"预检拒绝、不改动"。

    返回 ``[{"service", "target", "published", "protocol"}]``（范围已展开）。
    """
    result: list[dict[str, Any]] = []
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        for entry in service.get("ports") or []:
            if not isinstance(entry, dict):
                continue
            published = parse_published_port(entry.get("published"))
            if published is None:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_ports",
                    (
                        f"{name} 服务的 ports 条目宿主端口无法解析：{entry!r}；"
                        f"端口范围也最多展开 {MAX_PUBLISHED_PORT_RANGE} 个。"
                        "预检要逐个核对宿主端口，解析不了就不能放行。"
                    ),
                    ["检查 compose 的 ports 段（长格式 published）。"],
                )
            for port in published:
                result.append(
                    {
                        "service": str(name),
                        "target": entry.get("target"),
                        "published": port,
                        "protocol": str(entry.get("protocol") or "tcp"),
                    }
                )
    return result


class Context:
    def __init__(self, options: Options) -> None:
        self.o = options
        self.log = Logger()
        self.runner = Runner(self.log)
        self.record = new_record(options)
        self.install_locks: list[InstallLock] = []
        self.backup_lock: BackupDirLock | None = None
        self.config: dict[str, Any] = {}
        self.compose_env: dict[str, str] = {}
        self.previous: dict[str, dict[str, Any]] = {}
        self.previous_ref: str | None = None
        self.previous_commit: str | None = None
        self.target_image: dict[str, Any] = {}
        self.target_head: str | None = None
        self.target_revisions: set[str] = set()
        self.target_parents: dict[str, list[str]] = {}
        self.target_platform_digest: str | None = None
        self.published_port: int | None = None
        # 合并后模型里**所有**服务发布到宿主的端口（含 override 追加的 WS 端口）。
        self.published_ports: list[dict[str, Any]] = []
        self.ready_host = "127.0.0.1"
        self.stopped_worker = False
        self.stopped_api = False
        self.migration_attempted = False
        self.migrated = False
        self.env_updated = False
        self.phase = "init"

    # ---------------------------------------------------------------- 基础
    def write_record(self) -> None:
        self.record["updated_at"] = utc_now()
        payload = json.dumps(self.record, ensure_ascii=False, indent=2, sort_keys=True)
        write_atomic(self.o.record_path, (payload + "\n").encode("utf-8"), 0o600)

    def compose_argv(self) -> list[str]:
        argv = ["docker", "compose", "-p", self.o.project]
        argv += ["-f", str(self.o.compose_file)]
        if self.o.compose_override is not None:
            argv += ["-f", str(self.o.compose_override)]
        argv += ["--env-file", str(self.o.env_file)]
        return argv

    def docker_env(self, image: str | None) -> dict[str, str]:
        env = os.environ.copy()
        if image is None:
            env.pop("LING_APP_IMAGE", None)
        else:
            env["LING_APP_IMAGE"] = image
        return env

    def target_env(self) -> dict[str, str]:
        return self.docker_env(self.o.target)

    def previous_env(self) -> dict[str, str]:
        if self.previous_ref is None:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "rollback",
                "没有记录到旧镜像引用，无法按旧镜像恢复",
            )
        return self.docker_env(self.previous_ref)

    def run_checked(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        exit_code: int,
        status: str,
        step: str,
        message: str,
        next_actions: Sequence[str] = (),
        after_migration: bool = False,
    ) -> Result:
        result = self.runner.run(argv, env=env, timeout=timeout)
        if result.returncode != 0:
            detail = tail(result.stderr or result.stdout)
            raise PublishError(
                exit_code,
                status,
                step,
                f"{message}（rc={result.returncode}）" + (f"：{detail}" if detail else ""),
                next_actions,
                after_migration=after_migration,
            )
        return result

    # ---------------------------------------------------------------- 锁
    def acquire_locks(self) -> None:
        if not self.o.backup_dir.is_dir():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "lock",
                f"备份目录不存在或不是目录：{self.o.backup_dir}",
                ["创建/修正备份目录（与现有 cron 的 LING_BACKUP_DIR 一致）后重跑。"],
            )
        # 与 install.sh / 宿主执行器互斥的安装锁。两条路径都拿：
        #   1) 受限 env 的真实目标所在目录（install.sh 在有 deploy/.env 时用这条；
        #      也是 executor 的 install_lock_path 口径）；
        #   2) compose 文件所在目录的 .ling-install.lock（install.sh 在没有
        #      deploy/.env 时的口径；当 compose 就放在 deploy/ 时与 1 相同，去重）。
        candidates = [
            InstallLock(self.o.env_file),
            InstallLock(self.o.compose_file.parent / ".env"),
        ]
        seen: set[str] = set()
        for lock in candidates:
            key = str(lock.path)
            if key in seen:
                continue
            seen.add(key)
            self.install_locks.append(lock)
        self.log.emit(
            "抢锁：安装锁 "
            + "、".join(str(lock.path) for lock in self.install_locks)
            + f"；备份锁 {self.o.backup_dir / '.backup.lock'}"
        )
        try:
            for lock in self.install_locks:
                lock.acquire()
            self.record["locks"]["install"] = [str(lock.path) for lock in self.install_locks]
            self.backup_lock = BackupDirLock(self.o.backup_dir)
            self.backup_lock.acquire()
        except Exception:
            if self.backup_lock is not None:
                self.backup_lock.release()
                self.backup_lock = None
            self.release_locks()
            raise
        self.record["locks"]["backup"] = str(self.backup_lock.path)

    def release_locks(self) -> None:
        if self.backup_lock is not None:
            self.backup_lock.release()
            self.backup_lock = None
        for lock in reversed(self.install_locks):
            lock.release()
        self.install_locks = []

    # ---------------------------------------------------------------- 预检
    def preflight(self) -> None:
        self.log.emit("预检：只读检查（不拉镜像、不动容器/卷/数据库）")
        self.check_input_files()
        self.config = self.compose_config(self.o.target, step="preflight")
        self.check_compose_model()
        self.check_volumes()
        self.inspect_previous_containers()
        self.check_port()
        self.check_edge_containers()
        self.probe_registry_manifest()
        self.check_disk()
        self.read_current_alembic_version()
        self.try_extract_target_head_from_local_image()
        self.record["status"] = "preflight_passed"
        self.write_record()
        self.log.emit("预检通过")

    def check_input_files(self) -> None:
        if not self.o.compose_file.is_file():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_file",
                f"Compose 主文件不存在：{self.o.compose_file}",
                ["确认 --compose-file 指向镜像模式的 compose.image.yaml。"],
            )
        if self.o.compose_override is not None and not self.o.compose_override.is_file():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_override",
                f"Compose override 不存在：{self.o.compose_override}",
                [
                    "override 必须与 config/pull/run/up/备份使用同一组 -f；"
                    "不存在时宁可失败，也不静默退化成只读主文件。"
                ],
            )
        if not self.o.env_file.is_file():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "env_file",
                f"受限 env 不存在：{self.o.env_file}",
                ["镜像模式需要 LING_APP_IMAGE / POSTGRES_PASSWORD 等插值变量。"],
            )
        real = Path(os.path.realpath(self.o.env_file))
        info = real.stat()
        if info.st_mode & 0o077:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "env_file",
                (
                    f"受限 env 权限过宽：{real} mode={stat.S_IMODE(info.st_mode):04o}；"
                    "group/other 位必须为 0（chmod 600）。"
                ),
                [f"chmod 600 {real} 后重跑。"],
            )
        if not self.o.backup_script.is_file():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "backup_script",
                f"备份脚本不存在：{self.o.backup_script}",
                ["确认部署包包含 deploy/backup/backup.sh（不要重新实现备份）。"],
            )
        self.record["compose"]["env_file_realpath"] = str(real)
        self.record["compose"]["env_sha256_before"] = sha256_file(real)
        if self.o.snapshot_dir.exists():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "snapshot",
                f"本次快照目录已存在：{self.o.snapshot_dir}",
                ["换一个发布记录 ID（稍后重跑）或人工确认后移走旧目录。"],
            )

    def compose_config(
        self,
        image: str | None,
        *,
        step: str,
        exit_code: int | None = None,
        status: str | None = None,
        after_migration: bool = False,
    ) -> dict[str, Any]:
        code = (
            exit_code
            if exit_code is not None
            else (EXIT_PREFLIGHT if step == "preflight" else EXIT_PULL)
        )
        state = status or ("failed_preflight" if step == "preflight" else "failed_pull")
        result = self.runner.run(
            [*self.compose_argv(), "config", "--format", "json"],
            env=self.docker_env(image),
            timeout=DEFAULT_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            raise PublishError(
                code,
                state,
                "compose_config",
                (
                    "docker compose config 解析失败"
                    f"（rc={result.returncode}）：{tail(result.stderr or result.stdout)}"
                ),
                [
                    "检查 compose 文件组、--env-file、项目名与 LING_APP_IMAGE 插值；"
                    "不要用另一份 compose 文件替代。",
                ],
                after_migration=after_migration,
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PublishError(
                code,
                state,
                "compose_config",
                f"docker compose config 输出不是 JSON：{exc}",
                ["需要 Compose v2（支持 config --format json）。"],
                after_migration=after_migration,
            ) from exc
        if not isinstance(data, dict):
            raise PublishError(
                code,
                state,
                "compose_config",
                "docker compose config 输出不是对象",
                after_migration=after_migration,
            )
        return data

    def check_compose_model(self) -> None:
        services = self.config.get("services")
        if not isinstance(services, dict):
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_model",
                "compose 配置里没有 services",
            )
        for name in ("db", "api", "worker"):
            if name not in services:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_model",
                    f"compose 缺少服务 {name}",
                )
        api = services["api"]
        worker = services["worker"]
        api_image = api.get("image")
        worker_image = worker.get("image")
        if api_image != self.o.target or worker_image != self.o.target:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_image",
                (
                    "compose 解析出的 api/worker 镜像不是同一个目标 digest："
                    f"api={api_image!r} worker={worker_image!r} target={self.o.target!r}。"
                    "镜像模式下两者必须逐字符相同（同一镜像，靠 entrypoint 区分角色）。"
                ),
                [
                    "检查 LING_APP_IMAGE 插值与 compose 文件；"
                    "api/worker 摘要不同时拒绝发布。",
                ],
            )
        for name in ("api", "worker"):
            if "build" in services[name]:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_model",
                    f"{name} 服务仍有 build:；这不是镜像模式的主文件",
                    ["使用 compose.image.yaml，不要与源码模式 compose.yaml 叠加。"],
                )
        worker_healthcheck = worker.get("healthcheck")
        if (
            not isinstance(worker_healthcheck, dict)
            or worker_healthcheck.get("disable") is True
            or not worker_healthcheck.get("test")
        ):
            # 显式门禁：switch 阶段靠 Docker 的 health 状态等待 worker。
            # healthcheck 被删掉时 wait_worker_health 只能静默跳过，等于把
            # "worker 可用"降级成"容器进程起来了"——必须在预检就拒绝。
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_healthcheck",
                (
                    "compose 解析出的 worker 没有可用的 healthcheck"
                    "（test 为空、缺失或被 disable）；"
                    "没有健康信号就无法证明 worker 可用，不能静默跳过等待。"
                ),
                [
                    "在 compose 文件里给 worker 定义 healthcheck"
                    "（连接数据库并查询 outbox），然后重跑；"
                    "不要用 Compose 退出 0 代替 worker 健康证据。",
                ],
            )
        expected_mounts = {
            "db": ("db-data", "/var/lib/postgresql/data"),
            "api": ("app-data", "/data"),
            "worker": ("app-data", "/data"),
        }
        volumes = self.config.get("volumes") or {}
        if not isinstance(volumes, dict):
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_volumes",
                "compose volumes 段不是对象",
            )
        for key, expected_name in (
            ("db-data", f"{self.o.project}_db-data"),
            ("app-data", f"{self.o.project}_app-data"),
        ):
            volume = volumes.get(key)
            if not isinstance(volume, dict):
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_volumes",
                    f"compose 缺少命名卷键 {key}",
                )
            if volume.get("external"):
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_volumes",
                    f"命名卷 {key} 被声明为 external；不能确认旧数据是否在同一卷",
                )
            actual = volume.get("name")
            if actual != expected_name:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_volumes",
                    (
                        f"命名卷 {key} 的解析名是 {actual!r}，期望 {expected_name!r}；"
                        "换卷等于换数据，拒绝继续。"
                    ),
                )
        for service_name, (volume_key, target) in expected_mounts.items():
            mounts = service_volumes(services[service_name])
            for mount in mounts:
                # 更新通道先按**精确白名单**判定（只有 api 的 requests=rw /
                # status=ro 两条宿主目录；worker/db 一条都不许挂）。见文件头
                # UPDATE_MOUNTS 与 scripts/check_compose.py 的对应规则。
                #
                # 这里只做**否定**判定（不许挂的必须拒绝）；"api 必须挂上这两条"
                # 由 scripts/check_compose.py 断言。发布脚本面对的是合并后的解析
                # 模型（可能叠了 override），缺挂载的表现是网页更新不可用，不是
                # 这次发布不安全——两边的分工不要在这里重复。
                violation = update_mount_violation(service_name, mount)
                if violation is not None:
                    raise PublishError(
                        EXIT_PREFLIGHT,
                        "failed_preflight",
                        "compose_mounts",
                        violation,
                    )
                if is_update_mount_target(str(mount.get("target") or "")):
                    # 走到这里说明这条更新通道挂载已按白名单放行（否则上面就
                    # raise 了）：它是本规则里**唯一**允许的 bind，不能再被下面
                    # 那条"只许命名卷"拒绝——旧实现缺的正是这一步。
                    continue
                # 更新通道之外仍然只许命名卷：/app/web、源码 bind、docker.sock
                # 与任何其它宿主目录一律拒绝（允许清单之外没有放宽）。
                if mount.get("type") != "volume":
                    raise PublishError(
                        EXIT_PREFLIGHT,
                        "failed_preflight",
                        "compose_mounts",
                        non_volume_mount_reason(service_name, mount),
                    )
            matched = [
                mount
                for mount in mounts
                if mount.get("target") == target
                and mount.get("source") in {volume_key, f"{self.o.project}_{volume_key}"}
            ]
            if not matched:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_mounts",
                    f"{service_name} 没有把 {volume_key} 挂到 {target}",
                )
        api_env = service_environment(api)
        # 审计：把**观察到的**两个开关值写进发布记录。它们只记录、不判定，事后要能
        # 回答"这次发布时网页更新开关是什么状态"。放在下面的门禁**之前**：预检在
        # 后面失败时，记录里也留得下当时观察到的值。
        self.record["compose_env"] = {
            "LING_UPDATE_ENABLED": api_env.get("LING_UPDATE_ENABLED"),
            "LING_RUN_MIGRATIONS": api_env.get("LING_RUN_MIGRATIONS"),
        }
        # 这条**保留**：迁移必须是发布流程里的一次显式动作
        # （`run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate`）。api 启动时
        # 隐式迁移会让"迁移有没有发生"失去证据，也让失败发生在维护窗口之外。
        if api_env.get("LING_RUN_MIGRATIONS") != "0":
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_env",
                "api 的 LING_RUN_MIGRATIONS 不是 0；镜像模式必须由发布流程单次显式迁移",
            )
        # 这里**曾经**有一条硬断言：`LING_UPDATE_ENABLED` 必须是 `"0"`，否则
        # `failed_preflight`/`compose_env`（"本阶段不允许 Web 一键更新"）。
        # 它来自 P2 的人工发布脚本，写在网页一键更新功能之前，后来一直没人清理，
        # 于是和更新通道**互锁**：用户点更新 → API 要求开关=1 才收请求 → 更新器
        # 接手 → 调本脚本 → 这里要求开关=0 → 必然失败
        # （实测 201 之后 stop 在 failed_not_started / compose_env）。
        #
        # 为什么现在可以安全去掉：**并发不由开关值保证，由锁保证**，共两把
        # （见 deploy/updater/updater.py 设计文档「两把锁」）：
        #   1. 单飞：`<state-dir>/updater.lock` 的 flock 包住「读任务、认领请求、
        #      处理」整段，请求文件用 rename 认领（并发只有一个成功），同一
        #      `request_id` 不会被处理两次；
        #   2. 跨工具安装锁：复用本文件的 `InstallLock`，与 install.sh /
        #      `ling install` / 手动 publish.py / 宿主更新执行器**同一把锁**，
        #      从处理请求前持有到验证结束，拿不到就立即 `needs_manual`。
        # 所以"开关=1 会并发升级"这个担心不成立；开关只表示"网页可以发起更新"，
        # 实际执行仍然单飞、仍然与人工发布互斥。删除后开关的观察值照常进
        # `record["compose_env"]`，事后可审计。
        if api_env.get("LING_PORT") != "6520":
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_env",
                f"容器内 API 端口不是固定的 6520（拿到 {api_env.get('LING_PORT')!r}）",
            )
        # 预检必须覆盖**所有**服务发布到宿主的端口，而不只是 api 的 6520：
        # 这里的 ``services`` 来自 ``docker compose config`` 的合并后模型，
        # override 追加的 ports 已经在里面（Compose 的 ports 是追加语义）。
        self.published_ports = service_published_ports(services)
        self.record["published_ports"] = [
            {
                "service": item["service"],
                "target": item["target"],
                "published": item["published"],
                "protocol": item["protocol"],
            }
            for item in self.published_ports
        ]
        ports = api.get("ports") or []
        published: int | None = None
        host_ip: Any = None
        for port in ports:
            if not isinstance(port, dict):
                continue
            try:
                target_port = int(port.get("target"))
            except (TypeError, ValueError):
                continue
            if target_port == 6520:
                try:
                    published = int(port.get("published"))
                except (TypeError, ValueError):
                    published = None
                host_ip = port.get("host_ip")
                break
        if published is None or not (1 <= published <= 65535):
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_ports",
                f"无法从 compose 解析 api 发布到宿主的 6520 端口（拿到 {published!r}）",
                ["检查 ports 段；不要按文档猜端口。"],
            )
        self.published_port = published
        if isinstance(host_ip, str) and host_ip.strip() not in {"", "0.0.0.0", "::"}:
            self.ready_host = host_ip.strip()
        else:
            self.ready_host = "127.0.0.1"
        self.record["published_port"] = published
        self.record["ready_host"] = self.ready_host

    def docker_inspect(
        self,
        kind: str,
        ref: str,
        *,
        exit_code: int = EXIT_PREFLIGHT,
        status: str = "failed_preflight",
        after_migration: bool = False,
    ) -> dict[str, Any]:
        result = self.runner.run(["docker", "inspect", "--type", kind, ref])
        if result.returncode != 0:
            raise PublishError(
                exit_code,
                status,
                "docker_inspect",
                f"docker inspect {kind} {ref} 失败：{tail(result.stderr or result.stdout)}",
                after_migration=after_migration,
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PublishError(
                exit_code,
                status,
                "docker_inspect",
                f"docker inspect {kind} {ref} 输出不是 JSON：{exc}",
                after_migration=after_migration,
            ) from exc
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise PublishError(
                exit_code,
                status,
                "docker_inspect",
                f"docker inspect {kind} {ref} 返回空/非对象",
                after_migration=after_migration,
            )
        return data[0]

    def docker_image_inspect(
        self,
        ref: str,
        *,
        exit_code: int = EXIT_PREFLIGHT,
        status: str = "failed_preflight",
        after_migration: bool = False,
    ) -> dict[str, Any]:
        result = self.runner.run(["docker", "image", "inspect", ref])
        if result.returncode != 0:
            raise PublishError(
                exit_code,
                status,
                "image_inspect",
                f"docker image inspect {ref} 失败：{tail(result.stderr or result.stdout)}",
                after_migration=after_migration,
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PublishError(
                exit_code,
                status,
                "image_inspect",
                f"docker image inspect {ref} 输出不是 JSON：{exc}",
                after_migration=after_migration,
            ) from exc
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise PublishError(
                exit_code,
                status,
                "image_inspect",
                f"docker image inspect {ref} 返回空/非对象",
                after_migration=after_migration,
            )
        return data[0]

    def list_containers(
        self,
        *,
        service: str,
        all_states: bool,
        exit_code: int = EXIT_PREFLIGHT,
        status: str = "failed_preflight",
        after_migration: bool = False,
    ) -> list[dict[str, str]]:
        argv = ["docker", "ps"]
        if all_states:
            argv.append("-a")
        argv += [
            "--filter",
            f"label=com.docker.compose.project={self.o.project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.State}}",
        ]
        result = self.runner.run(argv)
        if result.returncode != 0:
            raise PublishError(
                exit_code,
                status,
                "docker_ps",
                f"docker ps 失败：{tail(result.stderr or result.stdout)}",
                after_migration=after_migration,
            )
        containers: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            containers.append(
                {"id": parts[0], "name": parts[1], "state": parts[2]}
            )
        return containers

    def inspect_previous_containers(self) -> None:
        for service in ("api", "worker"):
            containers = self.list_containers(service=service, all_states=False)
            if len(containers) != 1:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    f"previous_{service}",
                    (
                        f"项目 {self.o.project} 下正在运行的 {service} 容器数量是 "
                        f"{len(containers)}，期望恰好 1 个；无法取得可靠的回退材料。"
                    ),
                    [
                        "确认当前栈确实由同一个 Compose 项目运行；"
                        "本脚本只服务镜像模式的日常发布，不处理首次源码迁移。",
                    ],
                )
            container = containers[0]
            if container["state"] != "running":
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    f"previous_{service}",
                    f"{service} 容器不在 running 状态：{container}",
                )
            info = self.docker_inspect("container", container["id"])
            image_id = info.get("Image")
            image_ref = (info.get("Config") or {}).get("Image")
            running = (info.get("State") or {}).get("Running")
            if not image_id or not image_ref or running is not True:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    f"previous_{service}",
                    f"无法从容器 {container['name']} 读取 image/运行状态",
                )
            image = self.docker_image_inspect(str(image_id))
            labels = (image.get("Config") or {}).get("Labels") or {}
            env_values = self.parse_env_list((image.get("Config") or {}).get("Env") or [])
            label_commit = str(labels.get("org.opencontainers.image.revision") or "")
            env_commit = env_values.get("LING_COMMIT", "")
            if label_commit and env_commit and label_commit != env_commit:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    f"previous_{service}",
                    (
                        f"旧镜像 {image_id} 的 LING_COMMIT={env_commit!r} 与 OCI 标注 "
                        f"revision={label_commit!r} 不一致；回退材料不可信。"
                    ),
                )
            commit = label_commit or env_commit or None
            digests = [str(item) for item in image.get("RepoDigests") or []]
            self.previous[service] = {
                "container_id": container["id"],
                "container_name": container["name"],
                "image_ref": str(image_ref),
                "image_id": str(image_id),
                "repo_digests": digests,
                "commit": commit,
                "architecture": image.get("Architecture"),
                "os": image.get("Os"),
                "size": image.get("Size"),
            }
            self.log.emit(
                f"当前 {service}：image={image_ref} id={image_id} commit={commit or '未知'}"
            )
        api_ref = self.previous["api"]["image_ref"]
        worker_ref = self.previous["worker"]["image_ref"]
        if api_ref != worker_ref:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "previous_image",
                (
                    f"当前 api/worker 镜像引用不同（api={api_ref!r} "
                    f"worker={worker_ref!r}）；本脚本只处理镜像模式（两者必须同一摘要）。"
                ),
                ["首次从源码模式迁移请按设计 §6 单独作业，不要用本脚本。"],
            )
        if self.previous["api"]["image_id"] != self.previous["worker"]["image_id"]:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "previous_image",
                "当前 api/worker 容器实际 image ID 不同；无法做一致回退。",
            )
        self.previous_ref = str(api_ref)
        self.previous_commit = self.previous["api"]["commit"]
        self.record["previous"]["api"] = self.previous["api"]
        self.record["previous"]["worker"] = self.previous["worker"]

    @staticmethod
    def parse_env_list(items: Iterable[Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in items:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                values[key] = value
        return values

    def check_volumes(self) -> None:
        names = [
            f"{self.o.project}_app-data",
            f"{self.o.project}_db-data",
        ]
        result = self.runner.run(["docker", "volume", "inspect", *names])
        if result.returncode != 0:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "volumes",
                f"docker volume inspect 失败：{tail(result.stderr or result.stdout)}",
                ["确认命名卷存在；不要创建同名空卷代替旧卷。"],
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "volumes",
                f"docker volume inspect 输出不是 JSON：{exc}",
            ) from exc
        if not isinstance(data, list):
            raise PublishError(
                EXIT_PREFLIGHT, "failed_preflight", "volumes", "docker volume inspect 输出不是数组"
            )
        found: dict[str, dict[str, Any]] = {}
        for item in data:
            if isinstance(item, dict) and item.get("Name"):
                found[str(item["Name"])] = item
        compose_volumes = self.config.get("volumes") or {}
        for key, name in (
            ("app-data", f"{self.o.project}_app-data"),
            ("db-data", f"{self.o.project}_db-data"),
        ):
            volume = found.get(name)
            if volume is None:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "volumes",
                    f"命名卷 {name} 不存在；拒绝让 Compose 创建同名空卷。",
                )
            driver = volume.get("Driver")
            if driver != "local":
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "volumes",
                    f"命名卷 {name} 的 driver 是 {driver!r}，期望 local；卷身份不一致。",
                )
            labels = volume.get("Labels") or {}
            if labels.get("com.docker.compose.project") != self.o.project:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "volumes",
                    (
                        f"命名卷 {name} 的 Compose 项目标签是 "
                        f"{labels.get('com.docker.compose.project')!r}，不是 "
                        f"{self.o.project!r}；卷身份不一致。"
                    ),
                )
            config_name = (compose_volumes.get(key) or {}).get("name")
            if config_name != name:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "volumes",
                    f"卷 {key} 的 compose 解析名 {config_name!r} 与实际卷 {name!r} 不一致",
                )
            self.record["volumes"][key] = {
                "name": name,
                "driver": driver,
                "compose_name": config_name,
                "project_label": labels.get("com.docker.compose.project"),
            }

    def port_holders(self, port: int, protocol: str = "tcp") -> tuple[list[str], str]:
        """宿主端口的占用者（``ID|project|name``）与查询失败详情。

        返回 ``(lines, detail)``。``detail`` 非空表示 ``docker ps`` 失败：读不到
        占用者就无法证明端口空闲，调用方必须按失败处理（不能当成"没有占用"）。
        ``protocol`` 与 compose 条目一致（``tcp``/``udp``），不能一律按 tcp 查。
        """
        result = self.runner.run(
            [
                "docker",
                "ps",
                "--filter",
                f"publish={port}/{protocol}",
                "--format",
                '{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Names}}',
            ]
        )
        if result.returncode != 0:
            return [], tail(result.stderr or result.stdout)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()], ""

    def check_port(self) -> None:
        """逐个核对**所有**宿主发布端口（api 的 6520 与 override 追加的 WS 端口）。

        只盯 6520 会漏掉 ``compose.image.ws.example.yaml`` 追加的 WS 端口：该冲突
        要到迁移后的 ``up -d api`` 才由 Docker 报错，那时已经迁移过了。占用者判定
        按端口做（:meth:`port_holders` / :meth:`port_owned_by`），只有本栈当前的
        api/worker 容器可以占着目标端口；其余一律预检失败、不改动任何东西。
        """
        assert self.published_port is not None
        current_ids = {self.previous[name]["container_id"] for name in self.previous}
        for item in self.published_ports:
            port = int(item["published"])
            protocol = str(item["protocol"])
            lines, detail = self.port_holders(port, protocol)
            if detail:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    f"检查端口 {port}/{protocol} 占用失败：{detail}",
                )
            for line in lines:
                parts = line.split("|", 2)
                if len(parts) < 2:
                    continue
                owner_id = parts[0]
                owner_project = parts[1]
                if owner_id in current_ids and owner_project == self.o.project:
                    continue
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    (
                        f"宿主端口 {port}/{protocol} 被其他进程占用："
                        f"container={owner_id} project={owner_project!r} "
                        f"name={parts[2] if len(parts) > 2 else ''}"
                        f"（{item['service']} target={item['target']}）"
                    ),
                    [
                        "确认占用者；只有本栈当前 api 才允许占用目标端口。"
                        "不要用 kill/prune 猜测处理。",
                    ],
                )
        self.log.emit(
            "宿主端口全部可用："
            + "、".join(
                f"{item['service']}:{item['published']}/{item['protocol']}"
                for item in self.published_ports
            )
        )

    def check_edge_containers(self) -> None:
        by_service = self.list_containers(service="edge", all_states=True)
        by_name = self.runner.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name={self.o.project}-edge-1",
                "--format",
                "{{.ID}}|{{.Names}}",
            ]
        )
        leftovers = [item["name"] for item in by_service]
        if by_name.returncode == 0:
            for line in by_name.stdout.splitlines():
                if line.strip():
                    leftovers.append(line.replace("|", " "))
        if leftovers:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "edge",
                f"发现旧 edge 容器残留：{', '.join(sorted(set(leftovers)))}",
                [
                    "先按设计 §5.4 确认并单独停止/移除旧 ling-edge-1，"
                    "确认目标端口不再被占用后再发布。",
                    "本脚本不会自动 down/prune 任何容器或卷。",
                ],
            )

    def probe_registry_manifest(self) -> None:
        result = self.runner.run(
            ["docker", "manifest", "inspect", self.o.target], timeout=60.0
        )
        if result.returncode != 0:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "registry",
                (
                    f"无法读取目标摘要的 manifest（GHCR 不可达或无权限）："
                    f"{tail(result.stderr or result.stdout)}"
                ),
                [
                    "确认网络可达 ghcr.io；私有包需要宿主受限凭据（docker login ghcr.io）。",
                    "核对 digest 确实由 CI 推送；不要改用 latest 或 tag 试探。",
                ],
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "registry",
                f"docker manifest inspect 输出不是 JSON：{exc}",
            ) from exc
        platforms = self.manifest_platforms(data)
        if platforms:
            want = f"linux/{self.o.host_arch}"
            for entry in platforms:
                if entry.get("platform") == want:
                    digest = entry.get("digest")
                    if isinstance(digest, str) and HEX64_RE.fullmatch(digest):
                        self.target_platform_digest = digest
                    break
            else:
                raise PublishError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "registry_platform",
                    (
                        f"目标摘要没有 linux/{self.o.host_arch} 平台；"
                        f"镜像平台：{sorted(entry.get('platform', '?') for entry in platforms)}"
                    ),
                    ["确认宿主架构（uname -m）与 CI 构建的平台。"],
                )
            self.record["target"]["platform_digest"] = self.target_platform_digest
            self.log.emit(
                f"GHCR manifest 可达；匹配平台 linux/{self.o.host_arch}"
                + (f"（manifest digest {self.target_platform_digest}）" if self.target_platform_digest else "")
            )
        else:
            self.log.emit(
                "GHCR manifest 可达；该摘要不是多架构 index（平台将在 pull 后按 "
                "docker image inspect 的 Architecture/Os 核对）"
            )

    @staticmethod
    def manifest_platforms(data: Any) -> list[dict[str, Any]]:
        raw: Any = data
        if isinstance(data, dict) and isinstance(data.get("manifests"), list):
            raw = data["manifests"]
        elif isinstance(data, dict) and isinstance(data.get("platform"), dict):
            raw = [data]
        if not isinstance(raw, list):
            return []
        platforms: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            platform = item.get("platform")
            if platform is None and isinstance(item.get("Descriptor"), dict):
                platform = item["Descriptor"].get("platform")
            if not isinstance(platform, dict):
                continue
            os_name = platform.get("os") or platform.get("OS")
            arch = platform.get("architecture") or platform.get("Architecture")
            if not os_name or not arch:
                continue
            digest = item.get("digest")
            if digest is None and isinstance(item.get("Descriptor"), dict):
                digest = item["Descriptor"].get("digest")
            platforms.append(
                {
                    "platform": f"{str(os_name).lower()}/{str(arch).lower()}",
                    "digest": str(digest).removeprefix("sha256:") if digest else None,
                }
            )
        return platforms

    def check_disk(self) -> None:
        docker_root_raw = self.runner.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"]
        )
        if docker_root_raw.returncode != 0:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                f"无法读取 Docker 根目录：{tail(docker_root_raw.stderr or docker_root_raw.stdout)}",
            )
        docker_root = Path(docker_root_raw.stdout.strip() or "/var/lib/docker")
        if not docker_root.is_dir():
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                f"Docker 根目录不存在：{docker_root}",
            )
        old_size = max(
            int(self.previous.get(name, {}).get("size") or 0)
            for name in ("api", "worker")
        )
        previous_backup = self.previous_backup_bytes()
        image_need = self.disk_budget(
            "LING_RELEASE_IMAGE_BUDGET_BYTES",
            2 * old_size + 512 * 1024 * 1024,
        )
        backup_need = self.disk_budget(
            "LING_RELEASE_BACKUP_BUDGET_BYTES",
            old_size + previous_backup + 256 * 1024 * 1024,
        )
        docker_usage = shutil.disk_usage(docker_root)
        backup_usage = shutil.disk_usage(self.o.backup_dir)
        self.record["disk"] = {
            "docker_root": str(docker_root),
            "docker_free": docker_usage.free,
            "docker_need": image_need,
            "backup_dir": str(self.o.backup_dir),
            "backup_free": backup_usage.free,
            "backup_need": backup_need,
            "old_image_size": old_size,
            "previous_backup_bytes": previous_backup,
        }
        problems: list[str] = []
        if docker_usage.free < image_need:
            problems.append(
                f"Docker 根目录 {docker_root} 余量 {docker_usage.free} B < 需要 {image_need} B"
            )
        if backup_usage.free < backup_need:
            problems.append(
                f"备份目录 {self.o.backup_dir} 余量 {backup_usage.free} B < 需要 {backup_need} B"
            )
        if problems:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                "磁盘余量不足：" + "；".join(problems),
                [
                    "先清理/轮转旧备份或扩容；不要为了腾空间 prune 正在使用的卷/镜像。",
                    "预算算法：Docker 侧 = 2×旧镜像 + 512MiB；备份侧 = 旧镜像 save "
                    "+ 最近一份媒体/dump + 256MiB；可用 LING_RELEASE_*_BUDGET_BYTES "
                    "按现场实测覆盖（覆盖值仍会与真实余量比较）。",
                ],
            )
        self.log.emit(
            f"磁盘检查通过：docker 余量 {docker_usage.free} B（需 {image_need} B），"
            f"备份余量 {backup_usage.free} B（需 {backup_need} B）"
        )

    @staticmethod
    def disk_budget(env_name: str, default: int) -> int:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                f"{env_name} 不是整数：{raw!r}",
            ) from exc
        if value <= 0:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                f"{env_name} 必须 > 0（拿到 {value}）",
            )
        return value

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def previous_backup_bytes(self) -> int:
        manifests = sorted(
            self.o.backup_dir.glob("manifest-*.txt"),
            key=self._safe_mtime,
            reverse=True,
        )
        if not manifests:
            return 0
        try:
            fields = parse_manifest(manifests[0])
        except (OSError, ValueError):
            return 0
        total = 0
        for key in ("dump", "media"):
            name = fields.get(key)
            if not name or name in {"absent", "-"}:
                continue
            path = self.o.backup_dir / name
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def read_current_alembic_version(
        self,
        *,
        record_previous: bool = True,
        exit_code: int = EXIT_PREFLIGHT,
        status: str = "failed_preflight",
        after_migration: bool = False,
    ) -> str | None:
        result = self.runner.run(
            [
                *self.compose_argv(),
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "ling",
                "-d",
                "ling",
                "-tAc",
                "select version_num from alembic_version",
            ],
            env=self.target_env(),
        )
        if result.returncode != 0:
            raise PublishError(
                exit_code,
                status,
                "alembic_version",
                (
                    "无法读取当前 alembic_version（db 容器/项目名/schema 异常）："
                    f"{tail(result.stderr or result.stdout)}"
                ),
                [
                    "确认 db 容器健康、项目名与 compose 文件组正确；"
                    "本脚本不猜 schema 版本。",
                ],
                after_migration=after_migration,
            )
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        version = values[-1] if values else None
        if record_previous:
            self.record["previous"]["alembic_version"] = version
        self.log.emit(f"当前 alembic_version={version or '（空）'}")
        return version

    def try_extract_target_head_from_local_image(self) -> None:
        exists = self.runner.run(["docker", "image", "inspect", self.o.target])
        if exists.returncode != 0:
            self.log.emit(
                "目标镜像尚未在本地；Alembic head 将在 pull 之后、进入迁移之前读取"
            )
            return
        self.extract_target_head()
        self.verify_current_revision_in_target(self.record["previous"]["alembic_version"])
        self.log.emit(
            f"目标镜像内 Alembic head={self.target_head}"
            f"（已知修订 {len(self.target_revisions)} 个）"
        )

    def extract_target_head(self) -> None:
        result = self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "LING_PROJECT_ROOT=/app",
                "--entrypoint",
                "python",
                self.o.target,
                "-c",
                ALEMBIC_HEAD_SCRIPT,
            ],
            timeout=DEFAULT_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                (
                    "无法从目标镜像读取 Alembic head（镜像内缺少 alembic/migrations "
                    f"或运行失败）：{tail(result.stderr or result.stdout)}"
                ),
                [
                    "不要跳过 head 比对；确认 CI 构建的镜像包含 migrations 与 alembic.ini。",
                ],
            )
        heads = [
            line.split("=", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("HEAD=") and "=" in line
        ]
        revisions = {
            line.split("=", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("REV=") and "=" in line
        }
        parents: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            if not line.startswith("PARENT=") or ":" not in line:
                continue
            child, parent = line.split("=", 1)[1].split(":", 1)
            parents.setdefault(child.strip(), []).append(parent.strip())
        if len(heads) != 1 or not heads[0]:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                f"目标镜像的 Alembic head 不唯一或为空：{heads!r}",
                ["多 head 需要先合并迁移；不要带着歧义发布。"],
            )
        if not revisions:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                "目标镜像没有可读取的 Alembic 修订；无法核对迁移链",
            )
        self.target_head = heads[0]
        self.target_revisions = revisions
        self.target_parents = parents
        self.record["migration"]["target_head"] = self.target_head
        self.record["migration"]["target_revisions_known"] = True

    def verify_current_revision_in_target(self, current: Any) -> None:
        if not isinstance(current, str) or not current:
            return
        if self.target_revisions and current not in self.target_revisions:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "migration_chain",
                (
                    f"当前 alembic_version={current!r} 不在目标镜像的修订集合里；"
                    "迁移链不连续（可能跨版本太大或数据库来自别的分支）。"
                ),
                [
                    "先在隔离实例演练该跨度；不要直接对测试实例跳版本发布。",
                ],
            )
        if not self.target_head or not self.target_revisions:
            return
        # 只检查"修订存在"不够：当前版本必须能从目标 head 沿 down_revision
        # 走到（即它是 head 的祖先）。否则 Alembic 可能把旧库升级到另一个
        # 分支的 head，或者直接报 multiple heads。
        reachable: set[str] = set()
        stack = [self.target_head]
        while stack:
            revision = stack.pop()
            if revision in reachable:
                continue
            reachable.add(revision)
            stack.extend(self.target_parents.get(revision, []))
        if current not in reachable:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "migration_chain",
                (
                    f"当前 alembic_version={current!r} 不是目标 head="
                    f"{self.target_head!r} 的祖先；迁移链不连续。"
                ),
                [
                    "核对数据库来自哪条发布分支；先在隔离实例演练，"
                    "不要直接对测试实例跳版本或合并迁移。",
                ],
            )

    # ---------------------------------------------------------------- 归档
    def archive(self) -> None:
        self.log.emit("归档回退材料（旧镜像/Compose/env/提交/卷身份）")
        try:
            self.o.snapshot_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise PublishError(
                EXIT_ARCHIVE,
                "failed_archive",
                "snapshot",
                f"快照目录已存在：{self.o.snapshot_dir}",
            ) from exc
        os.chmod(self.o.snapshot_dir, 0o700)
        self.log.attach(self.o.snapshot_dir / "publish.log")
        story = {
            "api": self.previous["api"],
            "worker": self.previous["worker"],
            "volumes": self.record["volumes"],
            "target": self.record["target"],
            "created_at": utc_now(),
        }
        write_atomic(
            self.o.snapshot_dir / "previous-state.json",
            (json.dumps(story, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
            0o600,
        )
        # record_id = YYYYMMDD-HHMMSS-<digest 前 12 位>；保留日期+时间，
        # 否则同一天第二次发布会覆盖 rollback tag / tar 文件名。
        stamp = self.record["record_id"].rsplit("-", 1)[0]
        images: list[dict[str, Any]] = []
        tagged: dict[str, str] = {}
        saved: dict[str, dict[str, Any]] = {}
        for service in ("api", "worker"):
            info = self.previous[service]
            image_id = info["image_id"]
            if image_id in tagged:
                base = saved[image_id]
                images.append(
                    {
                        "service": service,
                        "image_id": image_id,
                        "image_ref": info["image_ref"],
                        "tag": tagged[image_id],
                        "tar": base.get("tar"),
                        "tar_sha256": base.get("tar_sha256"),
                        "tar_bytes": base.get("tar_bytes"),
                        "shared_with": base["service"],
                    }
                )
                continue
            tag = f"ling-rollback-{stamp}-{service}"
            self.run_checked(
                ["docker", "tag", image_id, tag],
                exit_code=EXIT_ARCHIVE,
                status="failed_archive",
                step="archive_tag",
                message=f"给旧 {service} 镜像打日期化保留 tag 失败",
            )
            tagged[image_id] = tag
            tar_path = self.o.snapshot_dir / f"old-{service}-{stamp}.tar"
            self.run_checked(
                ["docker", "save", "--output", str(tar_path), tag],
                exit_code=EXIT_ARCHIVE,
                status="failed_archive",
                step="archive_save",
                message=f"docker save 旧 {service} 镜像失败",
            )
            if not tar_path.is_file() or tar_path.stat().st_size <= 0:
                raise PublishError(
                    EXIT_ARCHIVE,
                    "failed_archive",
                    "archive_save",
                    f"docker save 产物为空或不存在：{tar_path}",
                    ["检查磁盘/权限；空归档不等于回退材料。"],
                )
            self.validate_saved_image_archive(tar_path)
            os.chmod(tar_path, 0o600)
            tar_sha = sha256_file(tar_path)
            entry = {
                "service": service,
                "image_id": image_id,
                "image_ref": info["image_ref"],
                "tag": tag,
                "tar": str(tar_path),
                "tar_sha256": tar_sha,
                "tar_bytes": tar_path.stat().st_size,
            }
            images.append(entry)
            saved[image_id] = entry
            self.log.emit(f"已归档旧 {service} 镜像：{tag} → {tar_path.name}（{tar_sha[:12]}）")
        files: list[dict[str, Any]] = []
        for label, source in (
            ("compose", self.o.compose_file),
            ("override", self.o.compose_override),
        ):
            if source is None:
                continue
            data = source.read_bytes()
            dest = self.o.snapshot_dir / f"{label}-{source.name}"
            write_atomic(dest, data, 0o600)
            copied_sha = sha256_file(dest)
            if copied_sha != sha256_bytes(data):
                raise PublishError(
                    EXIT_ARCHIVE,
                    "failed_archive",
                    "archive_copy",
                    f"归档副本与源文件哈希不一致：{dest}",
                    ["检查磁盘/文件系统；不一致的恢复材料不能使用。"],
                )
            files.append(
                {
                    "kind": label,
                    "source": str(source),
                    "copy": str(dest),
                    "sha256": copied_sha,
                }
            )
        env_real = Path(os.path.realpath(self.o.env_file))
        env_data = env_real.read_bytes()
        if sha256_bytes(env_data) != self.record["compose"]["env_sha256_before"]:
            raise PublishError(
                EXIT_ARCHIVE,
                "failed_archive",
                "archive_env",
                "受限 env 在预检之后被改动，归档的将不是预检时核对过的那份",
                ["确认没有其他进程改 env；重跑发布（不要继续用不一致的恢复材料）。"],
            )
        env_dest = self.o.snapshot_dir / f"env-{stamp}.env"
        write_atomic(env_dest, env_data, 0o600)
        env_copy_sha = sha256_file(env_dest)
        if env_copy_sha != sha256_bytes(env_data):
            raise PublishError(
                EXIT_ARCHIVE,
                "failed_archive",
                "archive_copy",
                f"env 归档副本与源文件哈希不一致：{env_dest}",
                ["检查磁盘/文件系统；不一致的恢复材料不能使用。"],
            )
        files.append(
            {
                "kind": "env",
                "source": str(env_real),
                "copy": str(env_dest),
                "sha256": env_copy_sha,
            }
        )
        self.record["archive"]["images"] = images
        self.record["archive"]["files"] = files
        self.record["archive"]["complete"] = True
        self.record["status"] = "archived"
        self.write_record()
        self.log.emit("回退材料归档完成")

    @staticmethod
    def validate_saved_image_archive(path: Path) -> None:
        """docker save 的归档必须能读出 manifest.json，不能只看"文件非空"。"""
        try:
            with tarfile.open(path, "r:") as archive:
                names = archive.getnames()
        except (tarfile.TarError, OSError) as exc:
            raise PublishError(
                EXIT_ARCHIVE,
                "failed_archive",
                "archive_save",
                f"docker save 产物不是可读的 tar 归档：{path}（{exc}）",
                ["重新执行 docker save；截断的归档不能当作回退材料。"],
            ) from exc
        if "manifest.json" not in names:
            raise PublishError(
                EXIT_ARCHIVE,
                "failed_archive",
                "archive_save",
                f"docker save 归档里没有 manifest.json：{path}",
                ["确认 docker save 完成且未被截断。"],
            )

    # ---------------------------------------------------------------- 拉取
    def pull_and_validate(self) -> None:
        self.log.emit("拉取目标镜像（此步不影响在线服务）")
        self.run_checked(
            [*self.compose_argv(), "pull", "api", "worker"],
            env=self.target_env(),
            timeout=DEFAULT_BACKUP_TIMEOUT,
            exit_code=EXIT_PULL,
            status="failed_pull",
            step="pull",
            message="docker compose pull api worker 失败",
            next_actions=[
                "检查 GHCR 凭据/网络与 digest；旧服务未进入维护窗口，保持运行。",
            ],
        )
        image = self.docker_image_inspect(
            self.o.target, exit_code=EXIT_PULL, status="failed_pull"
        )
        self.target_image = image
        self.verify_target_image_identity(image)
        self.extract_target_head()
        self.verify_current_revision_in_target(self.record["previous"]["alembic_version"])
        self.record["status"] = "pulled"
        self.write_record()
        self.log.emit(
            f"目标镜像身份核验通过：id={image.get('Id')} "
            f"commit={self.target_commit(image)} head={self.target_head}"
        )

    def verify_target_image_identity(self, image: dict[str, Any]) -> None:
        arch = image.get("Architecture")
        os_name = image.get("Os")
        if os_name != "linux" or arch != self.o.host_arch:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_platform",
                (
                    f"目标镜像平台是 {os_name}/{arch}，宿主是 linux/{self.o.host_arch}；"
                    "平台不匹配。"
                ),
                ["确认拉取的是对应平台；拒绝运行错误架构的镜像。"],
            )
        labels = (image.get("Config") or {}).get("Labels") or {}
        env_values = self.parse_env_list((image.get("Config") or {}).get("Env") or [])
        env_commit = env_values.get("LING_COMMIT", "")
        label_commit = str(labels.get("org.opencontainers.image.revision") or "")
        if not env_commit or not label_commit:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    "目标镜像缺少 LING_COMMIT 或 OCI 标注 "
                    f"org.opencontainers.image.revision（env={env_commit!r} "
                    f"label={label_commit!r}）；不能证明部署的是哪个源提交。"
                ),
                ["由 CI 用完整源 SHA 构建并注入两者；不要部署无身份的镜像。"],
            )
        if env_commit != label_commit:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    f"镜像内 LING_COMMIT={env_commit!r} 与 OCI 标注 "
                    f"revision={label_commit!r} 不一致。"
                ),
            )
        if COMMIT_RE.fullmatch(label_commit) is None:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                f"镜像标注的提交不是 40 位小写 hex：{label_commit!r}",
            )
        if self.o.source_commit and label_commit != self.o.source_commit:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    f"镜像标注提交 {label_commit!r} ≠ --source-commit "
                    f"{self.o.source_commit!r}；摘要与期望源提交不符。"
                ),
                ["核对 CI 发布证据；不要用错误的 digest 发布。"],
            )
        self.verify_repo_digests(image)
        self.record["target"]["source_commit"] = label_commit
        self.record["target"]["source_commit_source"] = (
            "operator+image" if self.o.source_commit else "image"
        )
        self.record["target"]["image_id"] = str(image.get("Id"))
        self.record["target"]["architecture"] = arch
        self.record["target"]["os"] = os_name

    def verify_repo_digests(self, image: dict[str, Any]) -> None:
        digests = [str(item) for item in image.get("RepoDigests") or []]
        suffixes = set()
        for item in digests:
            if "@sha256:" in item:
                suffixes.add(item.rsplit("@sha256:", 1)[1])
        accepted = {self.o.target_hex}
        if self.target_platform_digest:
            accepted.add(self.target_platform_digest)
        if not suffixes & accepted:
            raise PublishError(
                EXIT_PULL,
                "failed_pull",
                "image_digest",
                (
                    "本地镜像的 RepoDigests 与目标摘要对不上："
                    f"期望 {sorted(accepted)}，实际 {digests}。"
                ),
                [
                    "确认 pull 的是同一 digest；不要用同名 tag 或缓存镜像冒充。",
                ],
            )

    @staticmethod
    def target_commit(image: dict[str, Any]) -> str | None:
        labels = (image.get("Config") or {}).get("Labels") or {}
        return str(labels.get("org.opencontainers.image.revision") or "") or None

    # ---------------------------------------------------------------- 备份
    def capture_backup_image(self) -> str:
        """在**停 api 之前**捕获它的 image ID。

        维护窗口里 api 会先停掉，backup.sh 只能用一次性只读容器读 ``app-data``；
        那个容器需要一个**本地已有**的镜像（它绝不 pull）。这里读的是停之前
        运行中容器的实况，而不是预检缓存：预检到维护窗口之间可能有人手动改过。
        """
        containers = self.list_containers(
            service="api",
            all_states=False,
            exit_code=EXIT_BACKUP,
            status="failed_backup",
        )
        if len(containers) != 1 or containers[0]["state"] != "running":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_image",
                f"备份前读不到唯一的运行中 api 容器：{containers}",
                [
                    "确认 api 正在运行；没有运行中的 api 就无法确定一次性取数镜像。",
                    "不要在有写者时继续备份。",
                ],
            )
        info = self.docker_inspect(
            "container",
            containers[0]["id"],
            exit_code=EXIT_BACKUP,
            status="failed_backup",
        )
        image_id = str(info.get("Image") or "")
        if not image_id:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_image",
                f"无法从运行中的 api 容器读取 image ID：{info.get('Id')!r}",
                ["确认容器/镜像实况；备份需要一个本地已有的取数镜像。"],
            )
        return image_id

    def read_root_key(self) -> str:
        """从受限 env 读出生效中的根密钥，用来**只注入这一项**给 backup.sh。

        镜像模式没有 ``/data/keys/root.key``：根密钥只存在于 ``.env`` 的
        ``LING_ROOT_KEY``，而发布脚本/更新器的进程环境里根本没有它。backup.sh
        只看进程环境（manifest 的 ``keys_env_set``），所以不注入就会产出一份
        "看起来成功、恢复后 provider 凭据解不开"的备份。

        只读 ``self.o.env_file`` 的 ``LING_ROOT_KEY`` 一项，**不**把整个 env
        灌进子进程；解析见 :func:`parse_env_file_value`。读不到/不可用一律
        fail closed（不做备份），错误信息与记录里都不出现值。
        """
        path = self.o.env_file
        try:
            text = path.read_bytes().decode("utf-8")
        except OSError as exc:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_root_key",
                f"读取受限 env 失败（{type(exc).__name__}）：{path}；无法确认根密钥。",
                [
                    "确认受限 env 存在、0600、属主是运行发布/更新器的用户；修好后重跑。",
                    "不要在没有根密钥时继续备份：恢复后 provider 凭据解不开。",
                ],
            ) from exc
        except UnicodeDecodeError as exc:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_root_key",
                f"受限 env 不是 UTF-8 文本：{path}；无法安全读取根密钥。",
                [
                    "把受限 env 保存为 UTF-8（LING_ROOT_KEY 为单行、无不可打印字符）后重跑。",
                    "不要只备份 dump。",
                ],
            ) from exc
        try:
            key = parse_env_file_value(text)
        except ValueError as exc:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_root_key",
                f"受限 env 里的 LING_ROOT_KEY 不可用：{exc}。",
                [
                    "确认 LING_ROOT_KEY 是单行、单层成对引号、无换行/不可打印字符；修好后重跑。",
                    "不要静默截断或跳过：那会让备份归档不到生效中的根密钥。",
                ],
            ) from exc
        if key is None:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_root_key",
                (
                    "受限 env 里没有 LING_ROOT_KEY：镜像模式没有 /data/keys/root.key，"
                    "备份无法归档生效中的根密钥；拒绝在备份之前继续"
                    "（恢复后 provider 凭据将无法解密）。"
                ),
                [
                    "确认 LING_ROOT_KEY 真的写进了受限 env（0600、属主正确、值为非空单行），再重跑。",
                    "不要只备份 dump；也不要只靠进程环境临时 export 绕过（应用读的是 compose 注入的 .env）。",
                ],
            )
        return key

    def backup(self) -> None:
        # 根密钥只存在于受限 env（镜像模式没有 /data/keys/root.key），而更新器/
        # 发布进程的环境里没有它：先读出来，**在停写之前**失败，不为了一个配置
        # 错误把 api/worker 停掉。值绝不打印、绝不写进记录/日志。
        root_key = self.read_root_key()
        process_key = os.environ.get(ROOT_KEY_ENV_NAME, "")
        if process_key and process_key != root_key:
            self.log.emit(
                "警告：进程环境里的 LING_ROOT_KEY 与受限 env 不一致；backup.sh 将按"
                "受限 env 的值注入（与 compose 注入应用的来源一致；不回显值）"
            )
        self.log.emit(
            "已从受限 env 读到 LING_ROOT_KEY（值不回显），只会把这一项注入 backup.sh"
        )
        # **先停写再备份**：备份期间 api/worker 都不在服务，dump 与媒体归档才是
        # 写者静止的快照。旧的「停 worker → 备份 → 停 api」会让 api 在整个备份
        # 期间继续写库/写 /data，产物看起来成功、实际可能是撕裂的。
        self.log.emit("进入维护窗口：先捕获取数镜像，再停 api + worker（阻断 HTTP 写入与机器人入站）")
        backup_image = self.capture_backup_image()
        self.log.emit(f"已捕获备份取数镜像（运行中 api 的 image ID）：{backup_image}")
        self.stopped_api = True
        self.run_checked(
            [*self.compose_argv(), "stop", "api"],
            env=self.target_env(),
            timeout=DEFAULT_COMMAND_TIMEOUT,
            exit_code=EXIT_BACKUP,
            status="failed_backup",
            step="stop_api",
            message="停止旧 api 失败",
            next_actions=["确认 api 状态；不要在仍有写者时继续备份。"],
        )
        self.stopped_worker = True
        self.run_checked(
            [*self.compose_argv(), "stop", "worker"],
            env=self.target_env(),
            timeout=DEFAULT_COMMAND_TIMEOUT,
            exit_code=EXIT_BACKUP,
            status="failed_backup",
            step="stop_worker",
            message="停止旧 worker 失败",
            next_actions=["确认 worker 状态；不要在仍有写者时继续备份。"],
        )
        # 停完两个**再一起确认**：某个服务可能在停另一个的间隙被重启策略拉起来。
        self.verify_service_stopped("api")
        self.verify_service_stopped("worker")
        self.log.emit(
            "已确认 api/worker 都进入 exited/created：备份期间没有 HTTP 写入、"
            "机器人入站与后台写者"
        )
        self.log.emit("调用现有 backup.sh 做一致备份（同一组 -f / --env-file / 项目名）")
        backup_env = os.environ.copy()
        backup_env.update(
            {
                "LING_BACKUP_VIA_COMPOSE": "1",
                "LING_COMPOSE_PROJECT": self.o.project,
                "LING_COMPOSE_FILE": str(self.o.compose_file),
                "LING_COMPOSE_ENV_FILE": str(self.o.env_file),
                "LING_APP_IMAGE": self.previous_ref or self.o.target,
                # api 已停：backup.sh 用这个本地 image ID 起一次性只读容器。
                # 它是在停之前从运行中容器读到的（见 capture_backup_image）。
                "LING_BACKUP_APP_IMAGE": backup_image,
                # 只注入根密钥这一项（值来自受限 env，绝不打印/落盘）。镜像模式
                # 容器内没有 /data/root.key，缺了它 backup.sh 会记
                # keys_env_set=false，备份门禁随后拒绝迁移。
                ROOT_KEY_ENV_NAME: root_key,
            }
        )
        backup_env.pop("LING_COMPOSE_OVERRIDE", None)
        if self.o.compose_override is not None:
            backup_env["LING_COMPOSE_OVERRIDE"] = str(self.o.compose_override)
        result = self.runner.run(
            ["bash", str(self.o.backup_script), str(self.o.snapshot_dir)],
            env=backup_env,
            timeout=self.o.backup_timeout,
            cwd=self.o.compose_file.parent,
        )
        backup_log = self.o.snapshot_dir / "backup.log"
        write_atomic(
            backup_log,
            (result.stdout + "\n" + result.stderr).encode("utf-8"),
            0o600,
        )
        if result.returncode != 0:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup",
                (
                    f"backup.sh 失败（rc={result.returncode}）；"
                    f"日志 {backup_log}；{tail(result.stderr or result.stdout)}"
                ),
                [
                    "备份不完整时绝不进入迁移；旧服务会尝试恢复并实测。",
                    "检查备份目录空间、db/api 状态与 backup.sh 退出码含义（2 配置、"
                    "3 dump、4 根密钥、5 媒体、6 锁、7 缺 sha256 工具）。",
                ],
            )
        self.validate_backup_manifest()
        self.record["status"] = "backed_up"
        self.write_record()
        self.log.emit("备份 manifest 校验通过（dump/墓碑/根密钥/媒体同一恢复点）")

    def verify_service_stopped(self, service: str) -> None:
        containers = self.list_containers(
            service=service,
            all_states=True,
            exit_code=EXIT_BACKUP,
            status="failed_backup",
        )
        # "restarting"/"paused" 也不是安全停写状态：只接受 exited/created
        # （或根本没有该服务的容器）。
        unsafe = [
            item for item in containers if item["state"] not in {"exited", "created"}
        ]
        if unsafe:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                f"stop_{service}",
                f"{service} 容器没有进入 exited/created 状态：{unsafe}",
                ["不要在有写者时继续；人工确认容器/项目名与重启策略。"],
            )

    def validate_backup_manifest(self) -> None:
        manifests = sorted(
            self.o.snapshot_dir.glob("manifest-*.txt"),
            key=self._safe_mtime,
            reverse=True,
        )
        if not manifests:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"备份目录里没有 manifest：{self.o.snapshot_dir}",
                ["backup.sh 退出 0 不代表可恢复；没有 manifest 一律视为不完整。"],
            )
        if len(manifests) > 1:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"本次快照目录出现多份 manifest，无法确定恢复点：{[p.name for p in manifests]}",
            )
        manifest_path = manifests[0]
        try:
            fields = parse_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"manifest 不可解析：{exc}",
            ) from exc
        required = (
            "created_at",
            "mode",
            "dump",
            "dump_sha256",
            "tombstones",
            "tombstones_state",
            "tombstones_sha256",
            "keys_root",
            "keys_root_file",
            "keys_root_source",
            "keys_root_sha256",
            "keys_env_set",
            "media",
            "media_state",
            "media_sha256",
        )
        missing = [key for key in required if key not in fields]
        if missing:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"manifest 缺字段：{missing}；备份不可核验。",
                ["不要进入迁移；检查 backup.sh 版本与产物。"],
            )
        if fields["mode"] != "compose":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"manifest mode={fields['mode']!r}，期望 compose",
            )
        dump_name = fields["dump"]
        if not dump_name or "/" in dump_name or dump_name in {".", ".."}:
            raise PublishError(
                EXIT_BACKUP, "failed_backup", "backup_manifest", f"dump 字段不合法：{dump_name!r}"
            )
        dump_path = self.o.snapshot_dir / dump_name
        if not dump_path.is_file() or dump_path.stat().st_size <= 0:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"dump 不存在或为空：{dump_path}",
            )
        if HEX64_RE.fullmatch(fields["dump_sha256"]) is None:
            raise PublishError(
                EXIT_BACKUP, "failed_backup", "backup_manifest", "dump_sha256 不是 64 位 hex"
            )
        actual_dump_sha = sha256_file(dump_path)
        if actual_dump_sha != fields["dump_sha256"]:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"dump sha256 不匹配（manifest={fields['dump_sha256']} 实际={actual_dump_sha}）",
            )
        tombstones_name = fields["tombstones"]
        if not tombstones_name or "/" in tombstones_name:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"tombstones 字段不合法：{tombstones_name!r}",
            )
        tombstones_path = self.o.snapshot_dir / tombstones_name
        if fields["tombstones_state"] != "ok":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"tombstones_state={fields['tombstones_state']!r}；墓碑不确定的备份不可用于恢复。",
            )
        if not tombstones_path.is_file():
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"墓碑文件不存在：{tombstones_path}",
            )
        if HEX64_RE.fullmatch(fields["tombstones_sha256"]) is None:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                "tombstones_state=ok 但 tombstones_sha256 不是 64 位 hex；无法核验产物",
            )
        if sha256_file(tombstones_path) != fields["tombstones_sha256"]:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                "tombstones sha256 不匹配",
            )
        media_state = fields["media_state"]
        if media_state == "partial":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                "media_state=partial：媒体只备份了一部分，不能作为默认可恢复点进入迁移。",
                [
                    "在写入静止时重跑备份；不要接受“部分媒体”作为更新放行条件。",
                ],
            )
        media_name = fields["media"]
        if media_name == "absent" and media_state != "absent":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"media=absent 但 media_state={media_state!r}；manifest 自相矛盾",
            )
        if media_name != "absent" and media_state == "absent":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"media={media_name!r} 但 media_state=absent；manifest 自相矛盾",
            )
        if media_name != "absent":
            if not media_name or "/" in media_name:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    f"media 字段不合法：{media_name!r}",
                )
            media_path = self.o.snapshot_dir / media_name
            if not media_path.is_file() or media_path.stat().st_size <= 0:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    f"媒体归档不存在或为空：{media_path}",
                )
            if HEX64_RE.fullmatch(fields["media_sha256"]) is None:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    "media_sha256 不是 64 位 hex",
                )
            if sha256_file(media_path) != fields["media_sha256"]:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    "媒体归档 sha256 不匹配",
                )
        self.validate_backup_keys(fields)
        self.record["backup"].update(
            {
                "manifest": str(manifest_path),
                "dump": str(dump_path),
                "dump_sha256": actual_dump_sha,
                "tombstones_state": fields["tombstones_state"],
                "keys_root": fields["keys_root"],
                "keys_root_sha256": fields["keys_root_sha256"],
                "keys_env_set": fields["keys_env_set"],
                "media": media_name,
                "media_state": media_state,
                "media_sha256": fields["media_sha256"],
                "complete": True,
            }
        )

    def validate_backup_keys(self, fields: dict[str, str]) -> None:
        env_set = fields["keys_env_set"]
        if env_set not in {"true", "false"}:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"keys_env_set={env_set!r}，期望 true/false",
            )
        keys_root = fields["keys_root"]
        if keys_root not in {"present", "absent"}:
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                f"keys_root={keys_root!r}，期望 present/absent",
            )
        if env_set == "false" and keys_root != "present":
            raise PublishError(
                EXIT_BACKUP,
                "failed_backup",
                "backup_manifest",
                (
                    "keys_env_set=false 且 keys_root=absent：数据库里的 provider 凭据"
                    "恢复后将无法解密；拒绝进入迁移。"
                ),
                ["确认 LING_ROOT_KEY 是否实际生效；不要只备份 dump。"],
            )
        if keys_root == "present":
            key_path = self.o.snapshot_dir / "keys" / "root.key"
            if not key_path.is_file():
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    f"manifest 说 keys_root=present，但 {key_path} 不存在",
                )
            mode = stat.S_IMODE(key_path.stat().st_mode)
            if mode & 0o077:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    f"归档根密钥权限过宽：{key_path} mode={mode:04o}",
                )
            expected = fields["keys_root_sha256"]
            if HEX64_RE.fullmatch(expected) is None:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    "keys_root_sha256 不是 64 位 hex",
                )
            if sha256_file(key_path) != expected:
                raise PublishError(
                    EXIT_BACKUP,
                    "failed_backup",
                    "backup_manifest",
                    "归档根密钥 sha256 不匹配",
                )

    # ---------------------------------------------------------------- 迁移
    def migrate(self) -> None:
        current = self.read_current_alembic_version(
            record_previous=False,
            exit_code=EXIT_MIGRATE,
            status="failed_migration",
        )
        self.record["migration"]["current_at_migrate"] = current
        if current != self.record["previous"]["alembic_version"]:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "migration_chain",
                (
                    "停写前后 alembic_version 发生变化："
                    f"preflight={self.record['previous']['alembic_version']!r} "
                    f"now={current!r}；可能有其他迁移者在运行。"
                ),
                [
                    "不要继续迁移；schema 可能已变，按“已迁移后失败”处理："
                    "不自动启动旧 API/恢复旧 dump，人工核对谁改了 schema。",
                ],
                after_migration=True,
            )
        if self.target_head is None:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "target_head",
                "目标 Alembic head 未知；拒绝执行迁移。",
            )
        self.log.emit(
            f"单次显式迁移：{self.record['migration']['current_at_migrate'] or '空'} → "
            f"{self.target_head}"
        )
        self.migration_attempted = True
        log_path = self.o.snapshot_dir / "migrate.log"
        result = self.runner.run(
            [
                *self.compose_argv(),
                "run",
                "--rm",
                "--no-deps",
                "-e",
                "LING_RUN_MIGRATIONS=1",
                "api",
                "migrate",
            ],
            env=self.target_env(),
            timeout=self.o.migrate_timeout,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()
        write_atomic(log_path, (combined + "\n").encode("utf-8"), 0o600)
        self.record["migration"].update(
            {
                "attempted": True,
                "exit_code": result.returncode,
                "log": str(log_path),
                "executed_marker": "执行迁移" in combined,
                "skip_marker": "跳过迁移" in combined,
            }
        )
        after = self.read_current_alembic_version(
            record_previous=False,
            exit_code=EXIT_MIGRATE,
            status="failed_migration",
            after_migration=True,
        )
        self.record["migration"]["current_after"] = after
        if result.returncode != 0:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate",
                (
                    f"迁移命令失败（rc={result.returncode}）；日志 {log_path}；"
                    f"{tail(combined)}"
                ),
                [
                    "已停止旧服务且迁移可能已部分执行：不要自动启动旧 API、"
                    "不要自动恢复旧 dump；按设计 §6 人工判断。",
                ],
                after_migration=True,
            )
        if self.record["migration"]["skip_marker"]:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_skipped",
                (
                    "迁移日志显示“跳过迁移”（LING_RUN_MIGRATIONS=0 生效），"
                    "这不是一次真实迁移。"
                ),
                [
                    "检查 compose api 服务是否覆盖了 run 的 -e LING_RUN_MIGRATIONS=1；"
                    "不要启动新 API。",
                ],
                after_migration=True,
            )
        if not self.record["migration"]["executed_marker"]:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_not_executed",
                f"迁移日志没有“执行迁移”标记；日志 {log_path}：{tail(combined)}",
                ["无法证明迁移实际执行；不要启动新 API。"],
                after_migration=True,
            )
        if after != self.target_head:
            raise PublishError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_head",
                (
                    f"迁移后 alembic_version={after!r} ≠ 目标镜像 head="
                    f"{self.target_head!r}；拒绝启动新 API。"
                ),
                [
                    "保持维护状态，人工核对迁移日志与 schema；不要启动新 API，"
                    "也不要自动恢复旧 dump。",
                ],
                after_migration=True,
            )
        self.migrated = True
        self.record["status"] = "migrated"
        self.write_record()
        self.log.emit(f"迁移核验通过：alembic_version={after}")

    # ---------------------------------------------------------------- 切换
    def update_env_file(self) -> None:
        real = Path(os.path.realpath(self.o.env_file))
        before = real.read_bytes()
        if sha256_bytes(before) != self.record["compose"]["env_sha256_before"]:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "env_update",
                "受限 env 在发布期间被改动；拒绝用未核对的配置启动新容器",
                [
                    "已迁移：不要自动回滚；人工核对 env 变更来源，"
                    "确认后用同一份记录与备份继续。",
                ],
                after_migration=True,
            )
        text = before.decode("utf-8")
        pattern = re.compile(r"^\s*(?:export\s+)?LING_APP_IMAGE\s*=")
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        found = False
        for line in lines:
            if pattern.match(line):
                if not found:
                    output.append(f"LING_APP_IMAGE={self.o.target}\n")
                    found = True
                continue
            output.append(line)
        if not found:
            if output and not output[-1].endswith("\n"):
                output[-1] += "\n"
            output.append(f"LING_APP_IMAGE={self.o.target}\n")
        new_text = "".join(output)
        write_atomic(real, new_text.encode("utf-8"), 0o600)
        after = sha256_file(real)
        if stat.S_IMODE(real.stat().st_mode) & 0o077:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "env_update",
                f"更新后的 env 权限过宽：{real}",
                ["人工 chmod 600 后继续；不要启动新容器。"],
                after_migration=True,
            )
        self.env_updated = True
        self.record["compose"]["env_sha256_after"] = after
        self.log.emit(f"受限 env 已原子更新为目标 digest（{real}，0600）")
        # 不传 shell 覆盖，证明持久化的 env 自己能解析出目标摘要。
        config = self.compose_config(
            None,
            step="post_migration",
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        self.verify_config_points_to_target(config)

    def verify_config_points_to_target(self, config: dict[str, Any]) -> None:
        services = config.get("services") or {}
        for name in ("api", "worker"):
            image = (services.get(name) or {}).get("image")
            if image != self.o.target:
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "env_update",
                    (
                        f"更新 env 后 compose 解析出的 {name} 镜像仍是 {image!r}，"
                        f"不是 {self.o.target!r}。"
                    ),
                    ["检查 env 文件是否被其他进程覆盖；不要启动新容器。"],
                    after_migration=True,
                )

    def switch_and_verify(self) -> None:
        self.update_env_file()
        self.log.emit("按目标 digest 启动新 api（--no-deps --no-build --pull never）")
        self.run_checked(
            [
                *self.compose_argv(),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull=never",
                "api",
            ],
            env=self.target_env(),
            timeout=DEFAULT_COMMAND_TIMEOUT,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="up_api",
            message="启动新 api 容器失败",
            next_actions=[
                "已迁移：不要自动切旧镜像/恢复旧 dump；保持维护并人工检查容器日志。",
            ],
            after_migration=True,
        )
        # 顺序：先证明**容器内**一切正常（healthy → exec /readyz → 清单 → 提交），
        # 再探**宿主端口**。宿主探针要求 api 容器正在运行且端口已发布；容器根本
        # 没起来/已退出时，应该报 api_container_* 的启动失败，而不是一个看不懂的
        # 端口错误（缺陷 7 的另一面：探活不能抢在能证明"容器在跑"的证据之前）。
        self.verify_container_readyz()
        ready_status = self.wait_readyz()
        self.record["result"]["readyz_http"] = ready_status
        self.verify_web_manifest()
        version_commit = self.verify_running_commit()
        self.record["result"]["version_commit"] = version_commit
        api_state = self.verify_container_identity("api")
        self.record["result"]["api"] = api_state
        self.log.emit("新 api 验证通过；启动 worker")
        self.run_checked(
            [
                *self.compose_argv(),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull=never",
                "worker",
            ],
            env=self.target_env(),
            timeout=DEFAULT_COMMAND_TIMEOUT,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="up_worker",
            message="启动新 worker 容器失败",
            next_actions=[
                "已迁移：不要自动切旧镜像；worker 未启动时保持维护并人工处理。",
            ],
            after_migration=True,
        )
        worker_state = self.verify_container_identity("worker")
        self.wait_worker_health(worker_state)
        self.record["result"]["worker"] = worker_state
        final_head = self.read_current_alembic_version(
            record_previous=False,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        self.record["result"]["alembic_version_after_start"] = final_head
        if final_head != self.target_head:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "head_after_start",
                (
                    f"新 API 启动后 alembic_version={final_head!r} ≠ 目标 head="
                    f"{self.target_head!r}；可能有隐式迁移或外部写入。"
                ),
                [
                    "已迁移：不要自动回滚；保持维护并人工核对 schema 与写者。",
                ],
                after_migration=True,
            )
        if api_state["image_id"] != worker_state["image_id"]:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "same_digest",
                (
                    f"api/worker 运行镜像 ID 不同：api={api_state['image_id']} "
                    f"worker={worker_state['image_id']}；不是同一摘要。"
                ),
                ["不要以 Compose 退出 0 为成功；人工核对两个容器的镜像。"],
                after_migration=True,
            )
        self.record["status"] = "success"
        self.record["finished_at"] = utc_now()
        self.record["exit_code"] = EXIT_OK
        self.write_record()
        self.log.emit(
            f"发布成功：digest={self.o.target} commit={version_commit} "
            f"image_id={api_state['image_id']} head={self.target_head}"
        )

    # -------------------------------------------------- 宿主端口探活（缺陷 7）
    def host_container_port(self) -> int:
        """宿主端口对应的**容器侧**端口（compose 里 api 的 6520）。"""
        for item in self.published_ports:
            if (
                item.get("service") == "api"
                and item.get("published") == self.published_port
                and str(item.get("protocol") or "tcp") == "tcp"
            ):
                return int(item["target"])
        return 6520

    def host_probe_image(self) -> str:
        """探针容器用哪个镜像：显式指定 → 更新器自己的镜像 → 目标镜像。

        三道都**只取本机已有镜像**（探针带 ``--pull=never``）：更新器容器里自己
        正跑着的镜像一定在、一定核验过、一定带 python；宿主上直接跑 publish.py
        时它可能不在，回退到本次已经 pull + 核验过的目标镜像。不引入新镜像。
        """
        for candidate in (
            self.o.host_probe_image,
            os.environ.get(HOST_PROBE_IMAGE_ENV, ""),
        ):
            value = (candidate or "").strip()
            if value:
                return value
        ref = self.self_container_image()
        if ref:
            return ref
        return self.o.target

    def self_container_image(self) -> str | None:
        """自己正跑在哪个镜像上（Docker 把容器 hostname 设成容器 ID）。

        取不到就返回 None（宿主上直接跑 publish.py、或 docker inspect 失败），
        由 :meth:`host_probe_image` 回退。``CONTAINER_ID_RE`` 限定成 hex：宿主
        hostname 通常不是 hex，不能拿它去 inspect 一个不存在的容器。
        """
        hostname = (os.environ.get("HOSTNAME") or "").strip()
        if CONTAINER_ID_RE.fullmatch(hostname) is None:
            return None
        result = self.runner.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.Config.Image}}",
                hostname,
            ]
        )
        if result.returncode != 0:
            return None
        ref = result.stdout.strip()
        return ref or None

    def host_readyz_probe_argv(self, image: str | None = None) -> list[str]:
        """一次性 ``--network host`` 探针的 argv（逐项固定，便于测试钉住）。"""
        assert self.published_port is not None
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--pull=never",
            "--entrypoint",
            "python",
            image or self.host_probe_image(),
            "-c",
            HOST_READYZ_PROBE.format(
                host=self.ready_host, port=self.published_port
            ),
        ]

    def probe_host_readyz(self, image: str | None = None) -> tuple[int, str]:
        """跑一次性 host-network 探针，返回 ``(状态码, 详情)``。

        只有**探针容器**碰宿主 HTTP；本进程不建任何到宿主的连接。状态码由探针
        打印（连接失败/没响应时打印 0），退出码 0 表示恰好 200。
        """
        result = self.runner.run(
            self.host_readyz_probe_argv(image), timeout=HOST_READYZ_PROBE_TIMEOUT
        )
        detail = tail(result.stderr or result.stdout)
        status = 0
        for line in reversed(result.stdout.splitlines()):
            raw = line.strip()
            if raw.isdigit():
                status = int(raw)
                break
        return status, detail

    def host_port_bindings(self, container_id: str) -> tuple[list[dict[str, Any]], str]:
        """api 容器的 ``.NetworkSettings.Ports`` 里该容器端口的宿主绑定。

        直接问 docker（``--format '{{json .NetworkSettings.Ports}}'``），不猜：
        "端口真的发布出去了"只能由运行时事实证明，compose 文件里写着 ports 不算。
        """
        result = self.runner.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                container_id,
            ]
        )
        if result.returncode != 0:
            return [], tail(result.stderr or result.stdout)
        raw = result.stdout.strip()
        try:
            ports = json.loads(raw) if raw else {}
        except ValueError as exc:
            return [], f"docker inspect 端口输出不是 JSON：{exc}"
        if ports is None:
            ports = {}
        if not isinstance(ports, dict):
            return [], f"docker inspect 端口输出不是对象：{raw!r}"
        entries = ports.get(f"{self.host_container_port()}/tcp") or []
        return [item for item in entries if isinstance(item, dict)], ""

    def host_port_published(self, container_id: str) -> tuple[bool, str]:
        """宿主端口是否**真的**发布在 ``container_id`` 上（绑定在预期端口）。"""
        bindings, detail = self.host_port_bindings(container_id)
        if detail:
            return False, detail
        key = f"{self.host_container_port()}/tcp"
        host_ports = [
            str(item.get("HostPort"))
            for item in bindings
            if item.get("HostPort") not in (None, "")
        ]
        if str(self.published_port) in host_ports:
            return True, key
        return False, f"{key} 的宿主绑定是 {bindings!r}（期望 HostPort={self.published_port}）"

    def require_api_container_id(self, step: str = "host_port") -> str:
        containers = self.list_containers(
            service="api",
            all_states=False,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        if len(containers) != 1:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                step,
                (
                    f"项目 {self.o.project} 下正在运行的 api 容器有 {len(containers)} 个"
                    "（期望恰好 1 个）；没有对象可以用来证明宿主端口已发布。"
                ),
                [
                    "已迁移：不要自动回滚；先确认正式 compose 是否真的把 api 重启起来了。",
                ],
                after_migration=True,
            )
        return containers[0]["id"]

    def wait_readyz(self) -> int:
        """宿主端口 ``/readyz``：先证明端口已发布，再用一次性 host 容器探活。

        顺序很关键（缺陷 7）：

        1. 先 ``docker inspect`` 证明 api 容器的 ``.NetworkSettings.Ports`` 里
           ``<容器端口>/tcp`` 有 ``HostPort=<宿主端口>``。没有就**立即**失败并
           点明"维护门禁没解除"——不要在这里傻等 180s，那是把"端口没发布"误报
           成"应用没就绪"。
        2. 再起一次性的 ``--network host`` 容器打 ``127.0.0.1:<宿主端口>/readyz``。
           更新器进程自己在容器里（没有宿主网络），它自己打是**永远拿不到**的。
        3. 有限重试直到 ``--ready-timeout``；3xx 仍然按 ``readyz_redirect`` 失败
           （探针不跟随重定向）。
        """
        assert self.published_port is not None
        container_id = self.require_api_container_id()
        published, detail = self.host_port_published(container_id)
        url = f"http://{self.ready_host}:{self.published_port}/readyz"
        if not published:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "host_port",
                (
                    f"维护门禁没解除：api 容器 {container_id} 没有把宿主端口 "
                    f"{self.published_port} 发布出来（{detail}）；端口没发布就不探 "
                    f"{url}，也不等 --ready-timeout。"
                ),
                [
                    "已迁移：不要自动回滚；核对正式 compose 的 ports 段是否被维护"
                    "副本覆盖，以及 `docker inspect --format "
                    f"'{{{{json .NetworkSettings.Ports}}}}' {container_id}` 的实况。",
                ],
                after_migration=True,
            )
        # 只解析一次镜像并复用：record 里记的就是 argv 里用的那个。
        probe_image = self.host_probe_image()
        self.record["result"]["readyz_probe_image"] = probe_image
        self.log.emit(
            f"宿主端口 {self.published_port} 已发布（{container_id}）；"
            f"用 --network host 一次性容器探 {url}"
        )
        deadline = time.monotonic() + self.o.ready_timeout
        last: str = "未尝试"
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            code, detail = self.probe_host_readyz(probe_image)
            last = str(code) if code else (detail or "0")
            if code == 200:
                self.record["result"]["readyz_probe_attempts"] = attempts
                self.log.emit(f"宿主 /readyz 200（{url}，第 {attempts} 次探测）")
                return code
            if 300 <= code < 400:
                # 探针不跟随重定向，这里拿到的是真实 3xx。设计 §5 要求
                # /readyz"恰好 200"；302 指到别处（反代/错误端口）不算就绪。
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "readyz_redirect",
                    (
                        f"宿主 {url} 返回 {code}（重定向）；设计要求 /readyz "
                        "恰好 200，不接受 3xx。"
                    ),
                    [
                        "已迁移：不要自动回滚；检查宿主端口/反向代理是否指到新 api，"
                        "以及 /readyz 是否被改写成重定向。",
                    ],
                    after_migration=True,
                )
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        self.record["result"]["readyz_probe_attempts"] = attempts
        raise PublishError(
            EXIT_POST_MIGRATION,
            "failed_after_migration",
            "readyz",
            f"宿主 {url} 在 {self.o.ready_timeout}s 内没有返回 200（最后 {last}）",
            [
                "已迁移：不要自动切旧镜像/恢复旧 dump；查看新 api 日志与端口/健康检查。",
            ],
            after_migration=True,
        )


    def api_healthcheck_declared(self) -> bool:
        """compose 合并模型里 api 是否声明了可用的 healthcheck。"""
        services = self.config.get("services")
        if not isinstance(services, dict):
            return False
        api = services.get("api")
        if not isinstance(api, dict):
            return False
        healthcheck = api.get("healthcheck")
        return (
            isinstance(healthcheck, dict)
            and healthcheck.get("disable") is not True
            and bool(healthcheck.get("test"))
        )

    def wait_api_healthy(self) -> None:
        """探活之前先等 api 容器**自身** healthy。

        compose 里 api 的 healthcheck 探的就是容器内 ``127.0.0.1:6520/readyz``：
        只有 ``.State.Health.Status == healthy`` 才证明应用已经 bind 端口。
        ``up -d`` 返回时容器只是 ``running``，进程可能还没监听；此时立刻
        ``compose exec`` 会拿到 Connection refused，被误判成"目标版本启动失败"
        （P5 真机演练缺陷 5：容器稍后 ``Up 41 seconds (healthy)``、日志里
        ``GET /readyz 200 OK``，失败却发生在探活这一步）。

        - 只看 ``.State.Status == exited/dead`` → 立即失败，不等满超时；
        - ``starting`` → 每 ``API_HEALTH_POLL_SECONDS`` 轮询一次，直到
          ``--health-timeout``（默认 90s）用完；
        - ``unhealthy`` → 立即失败（healthcheck 已经连续失败，等下去只会拖时间）；
        - 读不到 ``State.Health``：compose 声明了 healthcheck 却没生效 → 失败；
          没声明 → 没有健康信号可等，**不**把 running 当就绪，交给后面的有限次
          容器内 exec 探活证明。
        """
        declared = self.api_healthcheck_declared()
        deadline = time.monotonic() + self.o.health_timeout
        polls = 0
        ever_running = False
        last = "未检查"
        while True:
            polls += 1
            containers = self.list_containers(
                service="api",
                all_states=True,
                exit_code=EXIT_POST_MIGRATION,
                status="failed_after_migration",
                after_migration=True,
            )
            if len(containers) != 1:
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "api_container_missing",
                    (
                        f"api 容器没起来：项目 {self.o.project} 下找到 "
                        f"{len(containers)} 个 api 容器（期望恰好 1 个），没有可等待"
                        "健康状态的对象。"
                    ),
                    [
                        "已迁移：不要自动回滚；用 "
                        f"`docker compose -p {self.o.project} ps -a` 与 api 启动日志"
                        "核对容器是否被创建/是否立刻退出。",
                    ],
                    after_migration=True,
                )
            info = self.docker_inspect(
                "container",
                containers[0]["id"],
                exit_code=EXIT_POST_MIGRATION,
                status="failed_after_migration",
                after_migration=True,
            )
            state = info.get("State") or {}
            status = str(state.get("Status") or "unknown")
            health = (state.get("Health") or {}).get("Status")
            last = f"status={status} health={health!r}"
            if status in ("exited", "dead"):
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "api_container_exited",
                    (
                        f"api 容器没起来：已退出（{last}，"
                        f"exit_code={state.get('ExitCode')!r}），不再等待超时。"
                    ),
                    [
                        "已迁移：不要自动回滚；看 api 启动日志（入口脚本、配置、"
                        "数据库连接）确认为什么进程直接退出。",
                    ],
                    after_migration=True,
                )
            if status == "running":
                ever_running = True
            if health == "healthy":
                self.record["result"]["api_health"] = {"polls": polls, "state": last}
                self.log.emit(f"api 容器 healthy（第 {polls} 次检查：{last}）")
                return
            if health == "unhealthy":
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "api_health",
                    (
                        f"api 容器起来了但健康检查是 unhealthy（{last}）："
                        "目标版本自检连续失败，不再等待。"
                    ),
                    [
                        "已迁移：不要自动回滚；人工检查 api 日志与"
                        "容器内 127.0.0.1:6520/readyz 的失败原因。",
                    ],
                    after_migration=True,
                )
            if health is None:
                if declared:
                    raise PublishError(
                        EXIT_POST_MIGRATION,
                        "failed_after_migration",
                        "api_health",
                        (
                            "api 容器起来了但读不到 healthcheck 状态"
                            f"（State.Health 缺失：{last}）；compose 声明了 "
                            "healthcheck 却没有生效，无法证明容器就绪。"
                        ),
                        [
                            "已迁移：不要自动回滚；核对 compose 的 api healthcheck "
                            "是否被 override 覆盖/disable，以及容器实况。",
                        ],
                        after_migration=True,
                    )
                if status == "running":
                    # compose 没有声明 api healthcheck：没有健康信号可等。绝不能
                    # 把 running 当就绪；改由下面有限次容器内 exec 探活来证明。
                    self.record["result"]["api_health"] = {"polls": polls, "state": last}
                    self.log.emit(
                        "api 的 compose 没有声明 healthcheck：没有健康信号可等，"
                        "改用有限次容器内 exec 探活"
                    )
                    return
            if time.monotonic() >= deadline:
                if not ever_running:
                    raise PublishError(
                        EXIT_POST_MIGRATION,
                        "failed_after_migration",
                        "api_container_not_started",
                        (
                            f"api 容器没起来：{self.o.health_timeout}s 内从未进入 "
                            f"running（最后 {last}）。"
                        ),
                        [
                            "已迁移：不要自动回滚；检查 compose 启动是否被卡住"
                            "以及 api 启动日志。",
                        ],
                        after_migration=True,
                    )
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "api_health",
                    (
                        f"api 容器在 {self.o.health_timeout}s 内没变 healthy"
                        f"（最后 {last}）；目标版本的 healthcheck 没通过，"
                        "不进入容器内 exec 探活。"
                    ),
                    [
                        "已迁移：不要自动回滚；用 `docker inspect --format "
                        "'{{json .State.Health}}'` 看健康检查日志，必要时调大 "
                        "--health-timeout 后人工重试。",
                    ],
                    after_migration=True,
                )
            time.sleep(
                min(API_HEALTH_POLL_SECONDS, max(0.1, deadline - time.monotonic()))
            )

    def verify_container_readyz(self) -> None:
        # 顺序不能反：exec 探活是"容器已 healthy"之后的第二道证据。容器刚 up
        # 时应用可能还没 bind 端口，直接 exec 会把启动慢误报成启动失败。
        self.wait_api_healthy()
        argv = [
            *self.compose_argv(),
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            (
                "import sys, urllib.request; "
                "sys.exit(0 if urllib.request.urlopen("
                "'http://127.0.0.1:6520/readyz', timeout=3).status == 200 else 1)"
            ),
        ]
        last = "未执行"
        for attempt in range(1, CONTAINER_READYZ_ATTEMPTS + 1):
            result = self.runner.run(
                argv, env=self.target_env(), timeout=CONTAINER_READYZ_EXEC_TIMEOUT
            )
            if result.returncode == 0:
                self.record["result"]["readyz_container_attempts"] = attempt
                return
            last = tail(result.stderr or result.stdout)
            if attempt < CONTAINER_READYZ_ATTEMPTS:
                self.log.emit(
                    f"容器内 /readyz 探活第 {attempt}/{CONTAINER_READYZ_ATTEMPTS} 次失败"
                    f"（{last}）；{CONTAINER_READYZ_RETRY_SECONDS:g}s 后重试"
                )
                time.sleep(CONTAINER_READYZ_RETRY_SECONDS)
        self.record["result"]["readyz_container_attempts"] = CONTAINER_READYZ_ATTEMPTS
        raise PublishError(
            EXIT_POST_MIGRATION,
            "failed_after_migration",
            "readyz_container",
            (
                f"api 容器已 healthy，但容器内 exec 探活 {CONTAINER_READYZ_ATTEMPTS} "
                f"次都失败（最后：{last or '无输出'}）"
            ),
            [
                "已迁移：不要自动回滚；容器健康检查通过而进程内探活失败，"
                "人工核对容器日志、数据库连接与 LING_PORT/监听地址是否一致。",
            ],
            after_migration=True,
        )

    def verify_web_manifest(self) -> None:
        # 与 CI 的「核对镜像标注、LING_COMMIT 与镜像内 Web 清单」同一命令：
        # 清单必须能对着镜像内文件重新校验通过，而不是只看文件存在。
        self.run_checked(
            [
                *self.compose_argv(),
                "exec",
                "-T",
                "api",
                "python",
                "/app/deploy/web_manifest.py",
                "verify",
                "/app",
                "clients/web-console",
                "/app/web",
                ".ling-web-manifest.json",
            ],
            env=self.target_env(),
            timeout=60.0,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="web_manifest",
            message="镜像内 Web 清单校验失败（静态资源与清单不一致）",
            next_actions=[
                "已迁移：不要自动回滚；确认运行容器就是 CI 产出的目标摘要，"
                "并检查镜像内 /app/web 与清单。",
            ],
            after_migration=True,
        )
        self.record["result"]["web_manifest_verified"] = True

    def verify_running_commit(self) -> str:
        """运行容器内**进程证据**：应用自报的提交必须等于发布记录的源提交。

        为什么不再打 HTTP ``/v1/version``（旧实现就是因此永远失败）：
        ``GET /v1/version`` 挂 ``CurrentAuth``，未鉴权恒 401——真机演练 P5 抓到
        的 ``failed_after_migration / version_container`` 就是这条探活。

        为什么**不给探活加 token**：发布/更新流程一旦持有用户凭据，能改宿主
        状态的通道就多了一个凭据持有者，攻击面只增不减。这里改成在容器里问
        应用自己（``CONTAINER_VERSION_PROBE``），零凭据、零 HTTP。

        证据等价性：``wait_readyz``（宿主端口）与 ``verify_container_readyz``
        （容器内）已经在证明"HTTP 栈可用"，容器身份另由
        ``verify_container_identity`` 按 OCI 标注复核；这里补上最后一块
        "跑着的进程就是目标构建"。三者合起来与"HTTP 拿到 commit"等价。

        没选那条路（新增一个只回 version/commit 的免鉴权端点）：它与"只读端点
        一律鉴权"的既有决定冲突。``/readyz`` 能免鉴权只是因为容器 healthcheck
        必须探它，而且它已经降级成只回状态；为发布流程再开一个公开端点，等于
        让匿名调用方读到部署指纹（版本/提交＝精确的补丁级情报）。
        """
        result = self.run_checked(
            [
                *self.compose_argv(),
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                CONTAINER_VERSION_PROBE,
            ],
            env=self.target_env(),
            timeout=CONTAINER_VERSION_EXEC_TIMEOUT,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="version",
            message="容器内读取应用自报的版本/提交失败（exec 失败）",
            next_actions=[
                "已迁移：不要自动回滚；确认运行容器就是目标摘要，并检查容器日志与镜像内 Python。",
            ],
            after_migration=True,
        )
        probe = parse_container_version_probe(result.stdout)
        commit = probe.get("commit")
        expected = self.record["target"]["source_commit"]
        if not isinstance(commit, str) or commit != expected:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "version",
                (
                    f"容器内进程报告的提交 {commit!r} ≠ 发布记录源提交 {expected!r}"
                    f"（应用自报版本 {probe.get('version')!r}）"
                ),
                ["已迁移：不要自动回滚；确认运行容器确实是目标镜像。"],
                after_migration=True,
            )
        self.record["result"]["version_reported"] = probe.get("version")
        return commit

    def verify_container_identity(self, service: str) -> dict[str, Any]:
        containers = self.list_containers(
            service=service,
            all_states=False,
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        if len(containers) != 1:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 正在运行的容器数量是 {len(containers)}，期望 1",
                ["已迁移：不要自动回滚；人工检查 Compose 副本数/残留容器。"],
                after_migration=True,
            )
        container = containers[0]
        info = self.docker_inspect(
            "container",
            container["id"],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        image_id = info.get("Image")
        image_ref = (info.get("Config") or {}).get("Image")
        if (info.get("State") or {}).get("Running") is not True:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器 {container['name']} 不在 running 状态",
                after_migration=True,
            )
        if image_ref != self.o.target:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器镜像引用是 {image_ref!r}，不是目标 digest {self.o.target!r}",
                after_migration=True,
            )
        expected_id = self.record["target"].get("image_id")
        if expected_id and image_id != expected_id:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器 image ID={image_id!r} ≠ 拉取到的目标 image ID={expected_id!r}",
                after_migration=True,
            )
        image = self.docker_image_inspect(
            str(image_id),
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        try:
            self.verify_repo_digests(image)
        except PublishError as exc:
            raise PublishError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                exc.message,
                after_migration=True,
            ) from exc
        return {
            "container_id": container["id"],
            "container_name": container["name"],
            "image_id": str(image_id),
            "image_ref": str(image_ref),
            "repo_digests": [str(item) for item in image.get("RepoDigests") or []],
            "commit": self.target_commit(image),
        }

    def wait_worker_health(self, state: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.o.health_timeout
        last = "unknown"
        while time.monotonic() < deadline:
            info = self.docker_inspect(
                "container",
                state["container_id"],
                exit_code=EXIT_POST_MIGRATION,
                status="failed_after_migration",
                after_migration=True,
            )
            health = ((info.get("State") or {}).get("Health") or {}).get("Status")
            if health is None:
                # 预检已要求 compose 声明 worker healthcheck；运行时仍读不到
                # Health 说明它没有生效。绝不能把"没有健康信号"当成通过。
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "worker_health_missing",
                    (
                        "新 worker 没有可读的 healthcheck 状态（State.Health 缺失）；"
                        "无法证明 worker 健康，拒绝按成功处理。"
                    ),
                    [
                        "已迁移：不要自动回滚；核对 compose 的 worker healthcheck "
                        "是否被覆盖/disable，以及容器实况。",
                    ],
                    after_migration=True,
                )
            last = str(health)
            if health == "healthy":
                return
            if health == "unhealthy":
                raise PublishError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "worker_health",
                    "新 worker 的 healthcheck 是 unhealthy",
                    ["已迁移：不要自动回滚；人工检查 worker 日志与 outbox/数据库。"],
                    after_migration=True,
                )
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        raise PublishError(
            EXIT_POST_MIGRATION,
            "failed_after_migration",
            "worker_health",
            f"新 worker 在 {self.o.health_timeout}s 内未变为 healthy（最后 {last}）",
            ["已迁移：不要自动回滚；人工检查 worker 健康。"],
            after_migration=True,
        )

    @staticmethod
    def http_get(url: str, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url, headers={"User-Agent": "ling-release-publish/1"}
        )
        try:
            # 不跟随重定向：3xx 由 _NoRedirectHandler 抛 HTTPError，下方返回真实
            # 状态码。调用方只打 HTTP_PROBE_PATHS 里的免鉴权端点，且只接受恰好 200。
            with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                return response.getcode(), response.read(MAX_CAPTURED_BODY)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(MAX_CAPTURED_BODY)
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc).encode("utf-8")

    # ---------------------------------------------------------------- 失败
    def handle_failure(self, error: PublishError) -> int:
        self.record["finished_at"] = utc_now()
        self.record["exit_code"] = error.exit_code
        self.record["status"] = error.status
        self.record["failure"] = {
            "step": error.step,
            "message": error.message,
            "after_migration": bool(error.after_migration or self.migration_attempted),
        }
        self.record["next_actions"] = list(error.next_actions)
        self.log.emit(f"失败（{error.status}/{error.step}）：{error.message}")
        if error.exit_code == EXIT_LOCK:
            # 拿不到锁就立即失败：不跑任何只读探测，也不声称旧服务状态。
            self.record["rollback"] = {
                "attempted": False,
                "verified": False,
                "detail": "未获得跨工具锁；本脚本没有执行任何变更或检查。",
            }
        elif error.after_migration or self.migration_attempted:
            self.record["rollback"] = {
                "attempted": False,
                "verified": False,
                "detail": (
                    "迁移已开始/完成，按设计 §6 不自动切回旧镜像、不自动恢复旧 dump；"
                    "保持当前状态供人工判断。"
                ),
            }
            self.log.emit(
                "已迁移后失败：不执行任何回滚 compose 命令，不恢复旧 dump；"
                "记录中保留旧/新 digest 与恢复点。"
            )
        else:
            try:
                self.record["rollback"] = self.attempt_restore_previous()
            except Exception as exc:  # noqa: BLE001 - 恢复失败也不能掩盖原始错误
                self.record["rollback"] = {
                    "attempted": True,
                    "verified": False,
                    "detail": f"恢复旧服务时异常：{type(exc).__name__}: {exc}",
                }
                self.log.emit(
                    "警告：恢复旧服务时异常；不得宣称仍在旧版本，请人工检查。"
                )
        for action in error.next_actions:
            self.log.emit(f"下一步：{action}")
        try:
            self.write_record()
        except OSError as exc:
            self.log.emit(f"严重：失败记录也写不进去（{self.o.record_path}）：{exc}")
            print(
                f"[publish] 发布记录写入失败：{self.o.record_path}；"
                f"原始错误：{error.message}",
                file=sys.stderr,
                flush=True,
            )
        return error.exit_code

    def attempt_restore_previous(self) -> dict[str, Any]:
        if not self.stopped_worker and not self.stopped_api:
            detail = "未改动任何服务；校验旧服务实况"
            if not self.previous:
                # 预检在读到旧容器之前就失败时，这里补一次只读核对：
                # 只有实测旧 image ID 未变且 /readyz 200 才能宣称"仍在旧版本"。
                try:
                    self.inspect_previous_containers()
                except PublishError as exc:
                    return {
                        "attempted": False,
                        "verified": False,
                        "detail": f"未改动任何服务；但无法读取旧服务实况：{exc.message}",
                    }
            verified = self.verify_previous_running()
            return {
                "attempted": False,
                "verified": verified,
                "detail": detail + ("；旧服务实测正常" if verified else "；但旧服务实况未能验证"),
            }
        services = []
        if self.stopped_api:
            services.append("api")
        if self.stopped_worker:
            services.append("worker")
        self.log.emit(f"未进入迁移：尝试恢复旧服务 {services}（image={self.previous_ref}）")
        problems: list[str] = []
        for service in services:
            result = self.runner.run(
                [
                    *self.compose_argv(),
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--pull=never",
                    service,
                ],
                env=self.previous_env(),
                timeout=DEFAULT_COMMAND_TIMEOUT,
            )
            if result.returncode != 0:
                problems.append(
                    f"恢复 {service} 失败（rc={result.returncode}）："
                    f"{tail(result.stderr or result.stdout)}"
                )
        verified = self.verify_previous_running() and not problems
        detail = "; ".join(problems) if problems else "旧服务已恢复"
        return {
            "attempted": True,
            "verified": verified,
            "detail": detail + ("；实测正常" if verified else "；未能确认旧服务正常"),
        }

    def port_owned_by(self, container_id: str, port: int | None = None) -> bool:
        """``container_id`` 是不是该宿主端口（默认 api 的 6520）的占用者。

        预检按端口逐个判定占用（见 :meth:`check_port`），这里复用同一份
        ``docker ps`` 解析，避免两处对"占用者"的定义漂移。
        """
        target_port = self.published_port if port is None else port
        if target_port is None or not container_id:
            return False
        lines, detail = self.port_holders(target_port)
        if detail:
            return False
        for line in lines:
            parts = line.split("|", 2)
            if len(parts) >= 2 and parts[0] == container_id and parts[1] == self.o.project:
                return True
        return False

    def verify_previous_running(self) -> bool:
        if self.previous_ref is None:
            return False
        ok = True
        current_ids: dict[str, str] = {}
        for service in ("api", "worker"):
            expected = self.previous.get(service)
            if expected is None:
                ok = False
                continue
            containers = self.list_containers(service=service, all_states=False)
            if len(containers) != 1:
                ok = False
                continue
            current_ids[service] = containers[0]["id"]
            info = self.docker_inspect("container", containers[0]["id"])
            if (info.get("State") or {}).get("Running") is not True:
                ok = False
                continue
            image_ref = (info.get("Config") or {}).get("Image")
            if image_ref != expected["image_ref"] or info.get("Image") != expected["image_id"]:
                ok = False
        if ok and not self.port_owned_by(current_ids.get("api", "")):
            # 端口被别的进程/项目占用时，即使 127.0.0.1 返回 200，也不能把
            # 那个响应当成旧服务正常。
            ok = False
        if ok and self.published_port is not None:
            # 同一条缺陷 7：进程自己在容器里时看不见宿主端口。这里也走一次性
            # host 容器；镜像用**旧镜像**（此刻它一定还在本机，且就是被实测的
            # 那个构建），而不是本进程自己的镜像。
            api_container = current_ids.get("api", "")
            published, detail = self.host_port_published(api_container)
            if not published:
                self.log.emit(
                    f"旧服务未实测：宿主端口 {self.published_port} 没发布（{detail}）"
                )
                ok = False
            else:
                code, probe_detail = self.probe_host_readyz(self.previous_ref)
                if code != 200:
                    self.log.emit(
                        f"旧服务 /readyz 不是 200（最后 {code or probe_detail}）"
                    )
                    ok = False
        if ok:
            self.log.emit("旧服务实测正常（容器 image ID 未变，/readyz 200）")
        else:
            self.log.emit("警告：未能实测确认旧服务正常；不得宣称仍在旧版本")
        return ok


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_options(argv)
    except PublishError as exc:
        print(f"[publish] {exc.message}", file=sys.stderr)
        for action in exc.next_actions:
            print(f"[publish] 下一步：{action}", file=sys.stderr)
        return exc.exit_code
    context = Context(options)
    try:
        context.acquire_locks()
        try:
            context.write_record()
        except OSError as exc:
            raise PublishError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "record",
                f"无法写入发布记录 {options.record_path}：{exc}",
                ["修正记录路径/权限后重跑；没有记录就不发布。"],
            ) from exc
        context.phase = "preflight"
        context.preflight()
        context.phase = "archive"
        context.archive()
        context.phase = "pull"
        context.pull_and_validate()
        context.phase = "backup"
        context.backup()
        context.phase = "migrate"
        context.migrate()
        context.phase = "switch"
        context.switch_and_verify()
    except PublishError as exc:
        return context.handle_failure(exc)
    except OSError as exc:
        mapping = {
            "archive": (EXIT_ARCHIVE, "failed_archive"),
            "pull": (EXIT_PULL, "failed_pull"),
            "backup": (EXIT_BACKUP, "failed_backup"),
            "migrate": (EXIT_MIGRATE, "failed_migration"),
            "switch": (EXIT_POST_MIGRATION, "failed_after_migration"),
        }
        code, status = mapping.get(context.phase, (EXIT_PREFLIGHT, "failed_preflight"))
        io_error = PublishError(
            code,
            status,
            f"{context.phase}_io",
            f"文件/目录操作失败（{context.phase}）：{exc}",
            [
                "检查磁盘空间、权限与路径；不要根据退出码猜测容器状态，"
                "先看发布记录与容器实况。",
            ],
            after_migration=context.phase in {"migrate", "switch"}
            or context.migration_attempted,
        )
        return context.handle_failure(io_error)
    except Exception as exc:  # noqa: BLE001 - 未预期异常也必须留下记录
        unexpected = PublishError(
            EXIT_POST_MIGRATION if context.migration_attempted else EXIT_PREFLIGHT,
            "failed_after_migration" if context.migration_attempted else "failed_preflight",
            "unexpected",
            f"未预期异常：{type(exc).__name__}: {exc}",
            [
                "保留本记录与快照目录；不要根据 Compose 退出码猜测状态，"
                "先核对容器/数据库实况。",
            ],
            after_migration=context.migration_attempted,
        )
        return context.handle_failure(unexpected)
    else:
        # switch_and_verify() 已在全部核验通过后原子写入成功记录；
        # 这里不再写第二遍（写失败会把成功路径变成退出码 16）。
        return EXIT_OK
    finally:
        context.release_locks()


if __name__ == "__main__":
    raise SystemExit(main())
