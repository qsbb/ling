"""deploy/updater/updater.py 的隔离回归测试（stub-only）。

这些用例在临时目录里真实执行更新器，只用 stub 替换 ``docker``、``backup.sh``
与宿主 HTTP；不会真的拉镜像、起容器、碰生产环境。

**stub-only 的局限**：stub 自己也在近似解释 ``docker compose config`` /
``docker inspect`` / ``compose exec`` 的输出，所以这里证明的是更新器的门禁顺序、
维护门禁的生成与"什么时候才发布宿主端口"、以及失败语义，**不是**真实 Docker
的行为。真实 Compose 的 ``config --format json``、``up --force-recreate`` 是否会
按预期重建容器、容器内 ``/readyz`` 与 ``/v1/version``、宿主机端口占用者，仍必须
在隔离实例真机演练（本机没有 docker）。
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATER_PY = REPO_ROOT / "deploy" / "updater" / "updater.py"
UPDATER_COMPOSE = REPO_ROOT / "deploy" / "updater" / "compose.yaml"
PUBLISH_PY = REPO_ROOT / "deploy" / "release" / "publish.py"
REAL_COMPOSE = REPO_ROOT / "deploy" / "compose.image.yaml"
REAL_WS_OVERRIDE = REPO_ROOT / "deploy" / "compose.image.ws.example.yaml"

TARGET_HEX = "a" * 64
TARGET = f"ghcr.io/qsbb/ling-api@sha256:{TARGET_HEX}"
OLD_HEX = "c" * 64
OLD_REF = f"ghcr.io/qsbb/ling-api@sha256:{OLD_HEX}"
COMMIT = "b" * 40
OLD_COMMIT = "d" * 40
OTHER_COMMIT = "e" * 40
PLATFORM_DIGEST = "9" * 64
HOST_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}.get(platform.machine().strip().lower(), platform.machine().strip().lower())
OTHER_ARCH = "arm64" if HOST_ARCH == "amd64" else "amd64"
HEAD_OLD = "0044_media_describe_failed"
HEAD_NEW = "0045_turn_control_attempt"
REQUEST_ID = "1" * 32
REQUEST_ID_B = "2" * 32
T0 = "2026-11-04T00:00:00Z"
# 更新器**自己的**镜像（固定 digest，与 deploy/updater/compose.yaml 的
# `LING_UPDATER_IMAGE` 同形）。宿主端口探针必须用它跑一次性容器（缺陷 7），
# 不能引入新镜像。
UPDATER_IMAGE = f"ghcr.io/qsbb/ling-updater@sha256:{'7' * 64}"

# 可信发布目录（deploy/releases.json）里的版本 ↔ 摘要映射：
# 当前安装的版本 = `.env` 里 LING_APP_IMAGE（OLD_REF）在目录里反查到的版本。
CURRENT_VERSION = "0.0.1"
TARGET_VERSION = "0.0.2"
RELEASED_AT = "2026-11-04T00:00:00Z"

# 受限 env 里的根密钥（与 publish 测试同一套可搜索哨兵；只断言"传到/不回显"）。
ROOT_KEY = "rootkey-canary-4f9a2c7e-do-not-print"

STATUS_KEYS = {
    "schema",
    "protocol",
    "updated_at",
    "updater",
    "task",
    "backup_gate",
    "release_catalog",
    "available_targets",
    "recommended_target",
}
# 恢复点门禁的固定结构：与 task 无关，由扫描备份目录得到（新增字段，见
# updater.py 的「恢复点门禁为什么必须是独立顶层字段」）。
BACKUP_GATE_KEYS = {
    "restore_point",
    "backup_id",
    "complete",
    "tombstones_state",
    "media_state",
    "keys_root",
    "keys_env_set",
    "reason",
    "checked_at",
}
TASK_VIEW_KEYS = {
    "request_id",
    "state",
    "stage",
    "started_at",
    "updated_at",
    "finished_at",
    "target",
    "previous",
    "migration",
    "backup",
    "maintenance",
    "rollback",
    "still_on_old_version",
    "failure",
    "warnings",
    "next_actions",
}


# ---------------------------------------------------------------- docker stub

_DOCKER = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys

STATE = pathlib.Path(os.environ["STUB_STATE"])
CALL_LOG = pathlib.Path(os.environ["CALL_LOG"])


def log(message):
    with CALL_LOG.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


def target_image():
    return {
        "Id": "sha256:target-image",
        "RepoDigests": [os.environ["STUB_TARGET"]],
        "Architecture": os.environ.get("STUB_IMAGE_ARCH", os.environ["STUB_HOST_ARCH"]),
        "Os": "linux",
        "Size": 100 * 1024 * 1024,
        "Config": {
            "Env": ["LING_COMMIT=" + os.environ["STUB_COMMIT"]],
            "Labels": {
                "org.opencontainers.image.revision": os.environ.get(
                    "STUB_LABEL_COMMIT", os.environ["STUB_COMMIT"]
                ),
                "org.opencontainers.image.version": "9.9.9",
            },
        },
    }


def old_image():
    return {
        "Id": "sha256:old-image",
        "RepoDigests": [os.environ["STUB_OLD_REF"]],
        "Architecture": os.environ["STUB_HOST_ARCH"],
        "Os": "linux",
        "Size": 90 * 1024 * 1024,
        "Config": {
            "Env": ["LING_COMMIT=" + os.environ["STUB_OLD_COMMIT"]],
            "Labels": {"org.opencontainers.image.revision": os.environ["STUB_OLD_COMMIT"]},
        },
    }


def image_for(ref):
    if ref in (os.environ["STUB_TARGET"], "sha256:target-image"):
        return target_image()
    if ref in (os.environ["STUB_OLD_REF"], "sha256:old-image"):
        return old_image()
    return None


def container_for(container_id, service, state):
    image = image_for(state["container_image_ref"][service])
    if image is None:
        return None
    running = bool(state["running"][service])
    payload = {
        "Id": container_id,
        "Image": state["container_image_id"][service],
        "Config": {"Image": state["container_image_ref"][service]},
        "State": {
            "Running": running,
            "Status": "running" if running else "exited",
        },
    }
    health = os.environ.get("STUB_WORKER_HEALTH", "healthy")
    if health != "none" and service == "worker":
        payload["State"]["Health"] = {"Status": health}
    return payload


def parse_filters(args):
    filters = {}
    index = 0
    while index < len(args):
        if args[index] == "--filter" and index + 1 < len(args):
            raw = args[index + 1]
            if "=" in raw:
                key, value = raw.split("=", 1)
                filters[key] = value
            index += 2
            continue
        index += 1
    return filters


def compose_files(args):
    files = []
    index = 0
    while index < len(args):
        if args[index] == "-f" and index + 1 < len(args):
            files.append(args[index + 1])
            index += 2
            continue
        index += 1
    return files


def ports_in_file(path):
    """极简块扫描：只要服务的 ports 下还有条目就算"端口还在"。"""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return True
    in_block = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key = stripped.split(":", 1)[0].strip()
        rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        if key == "ports":
            in_block = indent
            if rest.startswith("[") and rest != "[]":
                return True
            continue
        if in_block is not None:
            if indent > in_block:
                if stripped.startswith("-"):
                    return True
                continue
            in_block = None
    return False


def env_file_value(argv, key):
    """极简 ``--env-file`` 解析（Compose 的优先级：进程环境 > env 文件）。"""
    path = None
    for index, item in enumerate(argv):
        if item == "--env-file" and index + 1 < len(argv):
            path = argv[index + 1]
    if path is None:
        return None
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    value = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name == key:
            value = raw.strip().strip('"').strip("'")
    return value


def image_from_env(argv):
    """必须**真的**按 env 文件解析。

    更新器最后会用「不带 shell 覆盖」的 ``compose config`` 证明持久化的 env
    自己能解析出目标摘要（publish 的 ``verify_config_points_to_target``）；
    stub 若永远返回固定镜像，这个门禁就是空的。
    """
    return os.environ.get("LING_APP_IMAGE") or env_file_value(argv, "LING_APP_IMAGE")


def config_model(files, argv):
    base = json.loads(pathlib.Path(os.environ["STUB_CONFIG_JSON"]).read_text(encoding="utf-8"))
    image = image_from_env(argv)
    if image:
        for service in ("api", "worker"):
            base["services"][service]["image"] = image
    maintenance = [path for path in files if "/maintenance/" in str(path)]
    if not maintenance:
        return base
    leaked = any(ports_in_file(path) for path in maintenance)
    if leaked or os.environ.get("STUB_CONFIG_MAINT_PORTS") == "1":
        return base
    base["services"]["api"].pop("ports", None)
    return base


def emit_json(value):
    print(json.dumps(value))


args = sys.argv[1:]
log("docker " + " ".join(args))
if not args:
    sys.exit(64)

if args[0] == "info":
    fmt = ""
    if "--format" in args:
        fmt = args[args.index("--format") + 1]
    if "Architecture" in fmt:
        print(os.environ.get("STUB_INFO_ARCH", os.environ["STUB_HOST_ARCH"]))
    else:
        print(os.environ["STUB_DOCKER_ROOT"])
    sys.exit(0)

if args[0] == "manifest":
    manifest_arch = os.environ.get("STUB_MANIFEST_ARCH", os.environ["STUB_HOST_ARCH"])
    emit_json({
        "manifests": [
            {
                "digest": "sha256:" + os.environ["STUB_PLATFORM_DIGEST"],
                "platform": {"os": "linux", "architecture": manifest_arch},
            },
            {
                "digest": "sha256:" + "f" * 64,
                "platform": {"os": "linux", "architecture": os.environ["STUB_OTHER_ARCH"]},
            },
        ]
    })
    sys.exit(0)

def host_readyz_probe(args):
    """模拟 `docker run --network <mode> ... --entrypoint python -c <readyz 探针>`。

    与真 Docker 同语义：只有 ``--network host`` 的容器能看见宿主的
    127.0.0.1:<端口>；``--network none``/默认 bridge 有自己的网络命名空间，
    探自己的环回只会 Connection refused。非 host 模式真的把探针里的
    socket.connect 弄成拒绝连接，探针代码本身照原样执行（真的打到测试 HTTP stub）。
    """
    network = ""
    image = None
    code = None
    index = 1
    while index < len(args):
        item = args[index]
        if item == "--network" and index + 1 < len(args):
            network = args[index + 1]
            index += 2
            continue
        if item.startswith("--network="):
            network = item.split("=", 1)[1]
            index += 1
            continue
        if item in ("--pull=never", "--rm", "--read-only", "-i", "-t"):
            index += 1
            continue
        if item in ("--tmpfs", "--entrypoint", "--name", "-e", "--env") and index + 1 < len(args):
            index += 2
            continue
        if item == "-c" and index + 1 < len(args):
            code = args[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        image = item
        index += 1
    log("host-probe-args " + json.dumps(args))
    if code is None:
        sys.exit(64)
    if network != "host":
        code = (
            "import socket\n"
            "class _Blocked(socket.socket):\n"
            "    def connect(self, address):\n"
            "        raise ConnectionRefusedError(111, 'Connection refused')\n"
            "socket.socket = _Blocked\n"
        ) + code
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)


if args[0] == "run":
    if any("readyz" in str(item) for item in args):
        host_readyz_probe(args)
    print("HEAD=" + os.environ["STUB_HEAD_NEW"])
    print("REV=" + os.environ["STUB_HEAD_OLD"])
    print("REV=" + os.environ["STUB_HEAD_NEW"])
    print("PARENT=" + os.environ["STUB_HEAD_NEW"] + ":" + os.environ["STUB_HEAD_OLD"])
    sys.exit(0)

if args[0] == "ps":
    all_states = "-a" in args
    filters = parse_filters(args)
    state = load_state()
    if "name" in filters:
        sys.exit(0)
    if "publish" in filters:
        if state["running"]["api"] and state.get("api_port_published", True):
            print("c-api|ling")
        sys.exit(0)
    label = filters.get("label", "")
    service = None
    if "com.docker.compose.service=" in label:
        service = label.split("com.docker.compose.service=", 1)[1]
    if service in ("api", "worker"):
        if state["running"][service]:
            print("c-%s|c-%s-name|running" % (service, service))
        elif all_states:
            print("c-%s|c-%s-name|exited" % (service, service))
    sys.exit(0)

if args[0] == "image" and len(args) > 1 and args[1] == "inspect":
    image = image_for(args[2]) if len(args) > 2 else None
    if image is None:
        sys.exit(1)
    emit_json([image])
    sys.exit(0)

if args[0] == "inspect":
    kind = None
    ref = None
    fmt = None
    index = 1
    while index < len(args):
        if args[index] == "--type" and index + 1 < len(args):
            kind = args[index + 1]
            index += 2
            continue
        if args[index] == "--format" and index + 1 < len(args):
            fmt = args[index + 1]
            index += 2
            continue
        if not args[index].startswith("-"):
            ref = args[index]
        index += 1
    state = load_state()
    if fmt is not None and "Config.Image" in fmt:
        # 更新器容器自己的镜像（Docker 把 hostname 设成容器 ID）。
        self_image = os.environ.get("STUB_SELF_IMAGE", "")
        if not self_image:
            sys.exit(1)
        print(self_image)
        sys.exit(0)
    if fmt is not None and "NetworkSettings.Ports" in fmt:
        # 宿主端口是否真的发布在运行中的 api 容器上（缺陷 7 的预检事实）。
        published = (
            os.environ.get("STUB_HOST_PORT_PUBLISHED", "1") == "1"
            and state.get("api_port_published", True)
        )
        if ref == "c-api" and state["running"]["api"] and published:
            print(json.dumps({
                "6520/tcp": [
                    {"HostIp": "0.0.0.0", "HostPort": os.environ["STUB_HOST_PORT"]}
                ]
            }))
        else:
            print("null")
        sys.exit(0)
    if kind == "container":
        for service in ("api", "worker"):
            if ref == "c-" + service:
                payload = container_for("c-" + service, service, state)
                if payload is None:
                    sys.exit(1)
                emit_json([payload])
                sys.exit(0)
        sys.exit(1)
    if kind == "image":
        image = image_for(ref)
        if image is None:
            sys.exit(1)
        emit_json([image])
        sys.exit(0)
    sys.exit(64)

if args[0] == "volume" and len(args) > 1 and args[1] == "inspect":
    emit_json([
        {
            "Name": name,
            "Driver": os.environ.get("STUB_VOLUME_DRIVER", "local"),
            "Labels": {"com.docker.compose.project": os.environ.get("STUB_VOLUME_PROJECT", "ling")},
        }
        for name in args[2:]
    ])
    sys.exit(0)

if args[0] != "compose":
    # tag/save/load 等归档类命令：更新器**不**做镜像归档，出现即视为回归。
    sys.exit(64)

subargs = args[1:]
index = 0
subcommand = None
while index < len(subargs):
    if subargs[index] in ("-p", "-f", "--env-file") and index + 1 < len(subargs):
        index += 2
        continue
    if subargs[index].startswith("-"):
        index += 1
        continue
    subcommand = subargs[index]
    subargs = subargs[index + 1:]
    break
if subcommand is None:
    sys.exit(64)

files = compose_files(args)
maintenance = any("/maintenance/" in str(path) for path in files)
state = load_state()
log("docker-env %s LING_APP_IMAGE=%s" % (subcommand, os.environ.get("LING_APP_IMAGE", "")))

if subcommand == "config":
    emit_json(config_model(files, args))
    sys.exit(0)

if subcommand == "pull":
    sys.exit(int(os.environ.get("STUB_PULL_EXIT", "0")))

if subcommand == "stop":
    for service in subargs:
        if service in state["running"]:
            state["running"][service] = False
    save_state(state)
    sys.exit(0)

if subcommand == "up":
    service = subargs[-1] if subargs else ""
    if service in state["running"]:
        state["running"][service] = True
        ref = os.environ.get("LING_APP_IMAGE", state["container_image_ref"][service])
        if ref == os.environ["STUB_TARGET"]:
            image_id = "sha256:target-image"
        else:
            image_id = "sha256:old-image"
        state["container_image_ref"][service] = ref
        state["container_image_id"][service] = image_id
        if service == "api":
            state["api_port_published"] = not maintenance
    save_state(state)
    sys.exit(int(os.environ.get("STUB_UP_EXIT", "0")))

if subcommand == "exec":
    service = None
    for item in subargs:
        if item in ("db", "api", "worker"):
            service = item
            break
    if service == "db":
        if os.environ.get("STUB_DB_EXEC_EXIT", "0") != "0":
            sys.stderr.write("psql: connection refused\n")
            sys.exit(int(os.environ["STUB_DB_EXEC_EXIT"]))
        print(state["alembic_version"])
        sys.exit(0)
    if service == "api":
        if any("web_manifest.py" in str(item) for item in subargs):
            sys.exit(int(os.environ.get("STUB_MANIFEST_EXIT", "0")))
        if any("__version__" in str(item) for item in subargs):
            # 容器内**进程证据**探活（publish.CONTAINER_VERSION_PROBE）：提交号来自
            # **镜像** ENV（真实 Dockerfile 的构建期 LING_COMMIT，与 OCI 标注
            # revision 同源）。旧实现是容器内 urllib 打 /v1/version —— 那端点要
            # 鉴权、未鉴权恒 401，真机演练 P5 第 6 个缺陷就卡在这里。
            image = image_for(state["container_image_ref"]["api"]) or {}
            image_env = {}
            for entry in (image.get("Config") or {}).get("Env") or []:
                if "=" in entry:
                    key, value = entry.split("=", 1)
                    image_env[key] = value
            code = int(os.environ.get("STUB_CONTAINER_VERSION_EXIT", "0"))
            if code != 0:
                sys.stderr.write("ModuleNotFoundError: No module named 'ling'\n")
                sys.exit(code)
            commit = image_env.get("LING_COMMIT", "")
            if state.get("api_port_published"):
                # 只在正式 compose 重启、宿主端口已发布之后成立的覆盖：模拟
                # "重新发布出来的容器不是目标构建"——此时失败必须重新关上维护门禁。
                commit = os.environ.get("STUB_CONTAINER_COMMIT_PUBLISHED", commit)
            commit = os.environ.get("STUB_CONTAINER_COMMIT", commit)
            print(json.dumps({"version": "9.9.9", "commit": commit}))
            sys.exit(0)
        sys.exit(int(os.environ.get("STUB_INTERNAL_READYZ_EXIT", "0")))
    sys.exit(1)

if subcommand == "run":
    log("migration-run " + " ".join(subargs))
    if os.environ.get("STUB_MIGRATION_SKIP") == "1":
        # 「跳过迁移」= 没有真的迁移：DB 版本不前进，发布门禁必须判失败。
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
        sys.exit(int(os.environ.get("STUB_MIGRATE_EXIT", "0")))
    print("[entrypoint] 执行迁移")
    print("[migrate] alembic upgrade head 完成")
    if os.environ.get("STUB_MIGRATE_EXIT", "0") == "0":
        state["alembic_version"] = os.environ["STUB_HEAD_NEW"]
        save_state(state)
    sys.exit(int(os.environ.get("STUB_MIGRATE_EXIT", "0")))

sys.exit(64)
'''


# ---------------------------------------------------------------- HTTP stub


class _HTTPState:
    def __init__(self) -> None:
        self.readyz_status = 200
        self.port = 0
        self.call_log: Path | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _HTTPServer

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.http_state
        if state.call_log is not None:
            agent = self.headers.get("User-Agent") or ""
            with state.call_log.open("a", encoding="utf-8") as stream:
                # UA 用来区分"谁在碰宿主 HTTP"：探针容器（缺陷 7）带
                # ling-host-readyz-probe/1；进程自己 urlopen 会带
                # ling-release-publish/1。旧断言按子串匹配，不受影响。
                stream.write(f"http GET {self.path} ua={agent}\n")
        if self.path == "/readyz":
            status = state.readyz_status
            body = b"ok" if status == 200 else b"not ready"
        elif self.path.startswith("/v1/"):
            # **如实模仿真实应用**：`/v1/version` 等只读端点挂了 `CurrentAuth`，
            # 没有会话就是 401、body 为空。stub 以前在这里无条件回 200，于是
            # "更新器拿 401"在单元测试里永远抓不到（真机演练 P5 第 6 个缺陷）。
            status = 401
            body = b""
        else:
            status = 404
            body = b"not found"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


class _HTTPServer(http.server.ThreadingHTTPServer):
    http_state: _HTTPState


@pytest.fixture()
def http_state() -> Any:
    server = _HTTPServer(("127.0.0.1", 0), _Handler)
    state = _HTTPState()
    server.http_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_address[1]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------- 工作目录


_FAKE_BACKUP = r'''#!/usr/bin/env bash
set -euo pipefail
# **备份前必须已经没有写者**：更新器复用 publish.Context.backup()，顺序必须是
# 「停 api + worker → 备份」。这里直接读 docker 替身的状态文件，顺序回退就 9 退出。
states="$(python3 -c 'import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
print(int(bool(state["running"]["api"])), int(bool(state["running"]["worker"])))' "${STUB_STATE:?}")"
api_state="${states% *}"
worker_state="${states#* }"
printf 'backup-state api=%s worker=%s\n' "$api_state" "$worker_state" >> "${CALL_LOG:?}"
if [ "$api_state" != "0" ] || [ "$worker_state" != "0" ]; then
  echo "fake backup: 拒绝在写者未停时备份（api=${api_state} worker=${worker_state}）" >&2
  exit 9
fi
printf 'backup %s\n' "$*" >> "${CALL_LOG:?}"
printf 'backup-env LING_BACKUP_APP_IMAGE=%s\n' \
  "${LING_BACKUP_APP_IMAGE:-}" >> "${CALL_LOG:?}"
# 根密钥：只记"有没有设置"与值哈希，不把明文写进任何日志。
printf 'backup-root-key-set %s\n' "${LING_ROOT_KEY+1}" >> "${CALL_LOG:?}"
printf 'backup-root-key-sha %s\n' \
  "$(python3 -c 'import hashlib, os; print(hashlib.sha256(os.environ.get("LING_ROOT_KEY", "").encode()).hexdigest())')" \
  >> "${CALL_LOG:?}"
out="$1"
mode="${FAKE_BACKUP_MODE:-ok}"
if [ "$mode" = "fail" ]; then
  echo "fake backup failed" >&2
  exit 5
fi
mkdir -p "$out"
stamp="20260101-000000"
dump="ling-$stamp.dump"
printf 'dump-bytes' > "$out/$dump"
: > "$out/tombstones-$stamp.txt"
mkdir -p "$out/keys"
( umask 077; printf 'root-key-bytes' > "$out/keys/root.key" )
chmod 600 "$out/keys/root.key"
printf 'media-bytes' > "$out/media-$stamp.tar"
chmod 600 "$out/media-$stamp.tar"
sha() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
dump_sha="$(sha "$out/$dump")"
tomb_sha="$(sha "$out/tombstones-$stamp.txt")"
key_sha="$(sha "$out/keys/root.key")"
media_sha="$(sha "$out/media-$stamp.tar")"
{
  echo "created_at=2026-01-01T00:00:00Z"
  echo "mode=compose"
  echo "dump=$dump"
  echo "dump_sha256=$dump_sha"
  echo "tombstones=tombstones-$stamp.txt"
  echo "tombstones_state=ok"
  echo "tombstones_sha256=$tomb_sha"
  echo "keys_root=present"
  echo "keys_root_file=keys/root.key"
  echo "keys_root_source=file"
  echo "keys_root_sha256=$key_sha"
  echo "keys_env_set=false"
  echo "media=media-$stamp.tar"
  echo "media_state=present"
  echo "media_sha256=$media_sha"
} > "$out/manifest-$stamp.txt"
printf 'backup-complete %s\n' "$out" >> "${CALL_LOG:?}"
'''


def _write_executable(path: Path, body: str, *, python: bool) -> None:
    if python:
        body = f"#!{sys.executable}\n" + body.split("\n", 1)[1]
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _config_json(port: int) -> dict[str, Any]:
    return {
        "name": "ling",
        "services": {
            "db": {
                "image": "postgres:16-alpine",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "ling_db-data",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
            },
            "api": {
                "image": TARGET,
                "environment": {
                    "LING_ENV": "production",
                    "LING_RUN_MIGRATIONS": "0",
                    "LING_UPDATE_ENABLED": "0",
                    "LING_PORT": "6520",
                },
                "ports": [
                    {
                        "mode": "ingress",
                        "target": 6520,
                        "published": str(port),
                        "protocol": "tcp",
                    }
                ],
                "volumes": [{"type": "volume", "source": "ling_app-data", "target": "/data"}],
            },
            "worker": {
                "image": TARGET,
                "environment": {"LING_ENV": "production"},
                "healthcheck": {
                    "test": ["CMD", "python", "-c", "import asyncpg"],
                    "interval": "30s",
                    "timeout": "8s",
                    "retries": 3,
                },
                "volumes": [{"type": "volume", "source": "ling_app-data", "target": "/data"}],
            },
        },
        "volumes": {
            "db-data": {"name": "ling_db-data"},
            "app-data": {"name": "ling_app-data"},
        },
    }


def catalog_entry(
    version: str = TARGET_VERSION,
    *,
    digest: str = TARGET,
    commit: str = COMMIT,
    min_from: list[str] | None = None,
    has_migration: bool = True,
    released_at: str = RELEASED_AT,
) -> dict[str, Any]:
    """目录里的一条发布记录（结构必须与 deploy/release/catalog.py 的 schema 一致）。"""
    return {
        "version": version,
        "digest": digest,
        "commit": commit,
        "min_from": [CURRENT_VERSION] if min_from is None else min_from,
        "has_migration": has_migration,
        "released_at": released_at,
    }


def catalog_releases() -> list[dict[str, Any]]:
    """默认目录：当前装的 0.0.1 + 目标 0.0.2（可用目标=0.0.2）。

    当前版本是**反查**出来的：`.env` 里 LING_APP_IMAGE=OLD_REF，目录里 OLD_REF
    对应 0.0.1，所以当前版本=0.0.1，0.0.2 的 min_from 含 0.0.1 → 兼容。
    """
    return [
        catalog_entry(
            CURRENT_VERSION,
            digest=OLD_REF,
            commit=OLD_COMMIT,
            min_from=["0.0.0"],
            has_migration=False,
        ),
        catalog_entry(TARGET_VERSION),
    ]


@dataclass
class Workspace:
    root: Path
    compose_file: Path
    env_file: Path
    lock_file: Path
    backup_dir: Path
    backup_script: Path
    requests_dir: Path
    state_dir: Path
    release_catalog: Path
    call_log: Path
    state_path: Path
    config_path: Path
    env: dict[str, str]
    http: Any

    def reset(self) -> None:
        self.call_log.write_text("", encoding="utf-8")

    def calls(self) -> list[str]:
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def compose_calls(self) -> list[str]:
        return [line for line in self.calls() if line.startswith("docker compose")]

    def status(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))

    def status_text(self) -> str:
        return (self.state_dir / "status.json").read_text(encoding="utf-8")

    def task(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "task.json").read_text(encoding="utf-8"))

    def docker_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_docker_state(self, **fields: Any) -> None:
        state = self.docker_state()
        state.update(fields)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def write_catalog(self, releases: list[dict[str, Any]] | None = None) -> Path:
        """写一份可信发布目录（默认：当前 0.0.1 → 目标 0.0.2，可升级）。"""
        payload = {
            "schema": 1,
            "releases": catalog_releases() if releases is None else releases,
        }
        self.release_catalog.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.release_catalog.chmod(0o644)
        return self.release_catalog

    def catalog_entry(self, version: str = TARGET_VERSION, **overrides: Any) -> dict[str, Any]:
        return catalog_entry(version, **overrides)


@pytest.fixture()
def ws(tmp_path: Path, http_state: Any) -> Workspace:
    root = tmp_path / "repo"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    # 用**仓库真实的**镜像模式 compose 当输入：维护门禁的 ports 处理必须能在
    # 真实文件上工作，而不是只在测试夹具上工作。
    compose_file = deploy / "compose.image.yaml"
    shutil.copyfile(REAL_COMPOSE, compose_file)
    env_file = deploy / ".env"
    env_file.write_text(
        f"POSTGRES_PASSWORD=super-secret-password\nLING_APP_IMAGE={OLD_REF}\n"
        f"LING_ROOT_KEY={ROOT_KEY}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    # 安装器预建的跨工具安装锁（容器里是单文件挂载的源，Docker 不会替你创建）。
    lock_file = deploy / ".ling-install.lock"
    lock_file.write_bytes(b"")
    lock_file.chmod(0o600)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_script = tmp_path / "fake-backup.sh"
    _write_executable(backup_script, _FAKE_BACKUP, python=False)
    requests_dir = tmp_path / "requests"
    requests_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    release_catalog = deploy / "releases.json"
    release_catalog.write_text(
        json.dumps({"schema": 1, "releases": catalog_releases()}, indent=2) + "\n",
        encoding="utf-8",
    )
    release_catalog.chmod(0o644)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", _DOCKER, python=True)

    call_log = tmp_path / "calls.log"
    call_log.write_text("", encoding="utf-8")
    http_state.call_log = call_log
    state_path = tmp_path / "docker-state.json"
    state_path.write_text(
        json.dumps(
            {
                "running": {"api": True, "worker": True},
                "container_image_ref": {"api": OLD_REF, "worker": OLD_REF},
                "container_image_id": {
                    "api": "sha256:old-image",
                    "worker": "sha256:old-image",
                },
                "alembic_version": HEAD_OLD,
                "api_port_published": True,
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config_json(http_state.port)), encoding="utf-8")
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir()

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("LING_", "COMPOSE_", "DOCKER_")):
            env.pop(key, None)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CALL_LOG": str(call_log),
            "STUB_STATE": str(state_path),
            "STUB_CONFIG_JSON": str(config_path),
            "STUB_DOCKER_ROOT": str(docker_root),
            "STUB_TARGET": TARGET,
            "STUB_OLD_REF": OLD_REF,
            "STUB_COMMIT": COMMIT,
            "STUB_OLD_COMMIT": OLD_COMMIT,
            "STUB_HEAD_OLD": HEAD_OLD,
            "STUB_HEAD_NEW": HEAD_NEW,
            "STUB_PLATFORM_DIGEST": PLATFORM_DIGEST,
            "STUB_HOST_ARCH": HOST_ARCH,
            "STUB_OTHER_ARCH": OTHER_ARCH,
            # 更新器跑在容器里：Docker 把 hostname 设成容器 ID（hex），
            # `docker inspect --format {{.Config.Image}}` 因此能取到自己的镜像。
            "HOSTNAME": "f" * 64,
            "STUB_SELF_IMAGE": UPDATER_IMAGE,
            "STUB_HOST_PORT": str(http_state.port),
        }
    )
    return Workspace(
        root=root,
        compose_file=compose_file,
        env_file=env_file,
        lock_file=lock_file,
        backup_dir=backup_dir,
        backup_script=backup_script,
        requests_dir=requests_dir,
        state_dir=state_dir,
        release_catalog=release_catalog,
        call_log=call_log,
        state_path=state_path,
        config_path=config_path,
        env=env,
        http=http_state,
    )


# ---------------------------------------------------------------- 驱动


def write_request(
    ws: Workspace,
    request_id: str = REQUEST_ID,
    *,
    target: str = TARGET,
    expected: str = OLD_REF,
    commit: str = OLD_COMMIT,
    operator: Any = True,
    extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    payload: dict[str, Any] = {
        "schema": 1,
        "request_id": request_id,
        "target_digest": target,
        "expected_current_digest": expected,
        "expected_current_commit": commit,
        "operator_confirmed": operator,
        "created_at": T0,
    }
    payload.update(extra or {})
    for key in drop:
        payload.pop(key, None)
    tmp = ws.requests_dir / f".request-{request_id}.json.tmp"
    final = ws.requests_dir / f"request-{request_id}.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, final)
    return final


def write_manifest(
    ws: Workspace,
    stamp: str = "20260101-000000",
    *,
    directory: Path | None = None,
    mode: str = "compose",
    tombstones: str = "ok",
    media: str = "present",
    keys_root: str = "present",
    keys_env: str = "true",
    mtime: float | None = None,
) -> Path:
    """在备份目录里写一份**结构合法**的 manifest（cron/手动备份的样子）。

    只造恢复点门禁关心的字段与产物（dump 必须存在且非空）；哈希写成 64 位 hex
    但**不**要求正确——门禁是廉价的静态核对，真正核哈希的是 publish 的备份门禁。
    """
    out = ws.backup_dir if directory is None else directory
    out.mkdir(parents=True, exist_ok=True)
    dump = f"ling-{stamp}.dump"
    (out / dump).write_bytes(b"dump-bytes")
    fields = {
        "created_at": "2026-01-01T00:00:00Z",
        "mode": mode,
        "dump": dump,
        "dump_sha256": "0" * 64,
        "tombstones": f"tombstones-{stamp}.txt",
        "tombstones_state": tombstones,
        "tombstones_sha256": "0" * 64,
        "keys_root": keys_root,
        "keys_root_file": "keys/root.key",
        "keys_root_source": "file",
        "keys_root_sha256": "0" * 64 if keys_root == "present" else "-",
        "keys_env_set": keys_env,
        "media": f"media-{stamp}.tar" if media != "absent" else "absent",
        "media_state": media,
        "media_sha256": "0" * 64 if media != "absent" else "-",
    }
    manifest = out / f"manifest-{stamp}.txt"
    manifest.write_text("".join(f"{key}={value}\n" for key, value in fields.items()), encoding="utf-8")
    if mtime is not None:
        os.utime(manifest, (mtime, mtime))
    return manifest


def run_updater(
    ws: Workspace,
    *,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(UPDATER_PY),
        "--requests-dir",
        str(ws.requests_dir),
        "--state-dir",
        str(ws.state_dir),
        "--compose-file",
        str(ws.compose_file),
        "--env-file",
        str(ws.env_file),
        "--backup-dir",
        str(ws.backup_dir),
        "--backup-script",
        str(ws.backup_script),
        "--publish-py",
        str(PUBLISH_PY),
        "--release-catalog",
        str(ws.release_catalog),
        "--once",
        "--ready-timeout",
        "3",
        "--health-timeout",
        "3",
        "--migrate-timeout",
        "30",
        "--backup-timeout",
        "30",
    ]
    argv.extend(extra or [])
    merged = dict(ws.env)
    merged.update(env or {})
    return subprocess.run(
        argv,
        cwd=str(ws.root),
        env=merged,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def seed_task(
    ws: Workspace,
    *,
    state: str = "running",
    stage: str = "migrate",
    attempted: bool = True,
    previous_head: str | None = HEAD_OLD,
    maintenance: str = "active",
) -> dict[str, Any]:
    # 真实运行里"prepare"阶段就会生成维护门禁副本；对账时要按它重新关门。
    gate_dir = ws.state_dir / "maintenance" / REQUEST_ID_B
    gate_dir.mkdir(parents=True, exist_ok=True)
    gate_file = gate_dir / "0-compose.image.yaml"
    gate_file.write_text(
        _updater_module().strip_service_ports(REAL_COMPOSE.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    task = {
        "schema": 1,
        "protocol": 1,
        "request_id": REQUEST_ID_B,
        "state": state,
        "stage": stage,
        "started_at": T0,
        "updated_at": T0,
        "finished_at": None,
        "target": {
            "digest": TARGET,
            "digest_hex": TARGET_HEX,
            "commit": COMMIT,
            "image_id": "sha256:target-image",
        },
        "previous": {
            "digest": OLD_REF,
            "commit": OLD_COMMIT,
            "head": previous_head,
            "services": {
                "api": {
                    "container_id": "c-api",
                    "container_name": "c-api-name",
                    "image_ref": OLD_REF,
                    "image_id": "sha256:old-image",
                    "repo_digests": [OLD_REF],
                    "commit": OLD_COMMIT,
                    "architecture": HOST_ARCH,
                    "os": "linux",
                    "size": 90 * 1024 * 1024,
                },
                "worker": {
                    "container_id": "c-worker",
                    "container_name": "c-worker-name",
                    "image_ref": OLD_REF,
                    "image_id": "sha256:old-image",
                    "repo_digests": [OLD_REF],
                    "commit": OLD_COMMIT,
                    "architecture": HOST_ARCH,
                    "os": "linux",
                    "size": 90 * 1024 * 1024,
                },
            },
        },
        "migration": {
            "attempted": attempted,
            "current_before": previous_head,
            "current_after": HEAD_NEW if attempted else None,
            "target_head": HEAD_NEW,
        },
        "backup": {
            "id": f"backup-{REQUEST_ID_B[:12]}",
            "complete": True,
            "tombstones_state": "ok",
            "media_state": "present",
            "keys_root": "present",
            "keys_env_set": False,
        },
        "maintenance": {
            "state": maintenance,
            "verified": attempted,
            "files": [str(gate_file)],
        },
        "rollback": {"attempted": False, "verified": False},
        "failure": None,
        "still_on_old_version": None,
        "warnings": [],
        "next_actions": [],
    }
    (ws.state_dir / "task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return task


_MUTATING = (" up ", " down ", " stop ", " start ", " restart ", " run ", " pull ")


def mutating_compose_calls(ws: Workspace) -> list[str]:
    """只读探测（config / exec db psql）不算变更。"""
    return [line for line in ws.compose_calls() if any(word in line for word in _MUTATING)]


def calls_with(ws: Workspace, needle: str) -> list[str]:
    return [line for line in ws.calls() if needle in line]


def index_of(ws: Workspace, needle: str) -> int:
    for index, line in enumerate(ws.calls()):
        if needle in line:
            return index
    raise AssertionError(f"未找到调用：{needle}\n" + "\n".join(ws.calls()))


# ---------------------------------------------------------------- 单元：ports 处理


def _updater_module() -> Any:
    spec = importlib.util.spec_from_file_location("ling_updater_under_test", UPDATER_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_strip_service_ports_on_real_compose() -> None:
    updater = _updater_module()
    text = REAL_COMPOSE.read_text(encoding="utf-8")
    stripped = updater.strip_service_ports(text)
    assert stripped != text
    # api 的宿主端口条目被整块替换成空列表，别的地方逐字符保留。
    assert "- \"${LING_PORT:-6520}:6520\"" not in stripped
    assert "    ports: []\n" in stripped
    assert stripped.count("ports: []") == 1
    for needle in (
        "LING_RUN_MIGRATIONS: \"0\"",
        "LING_UPDATE_ENABLED: ${LING_UPDATE_ENABLED:-0}",
        "- app-data:/data",
        "      - db-data:/var/lib/postgresql/data",
        "      - \"6520\"",
    ):
        assert needle in stripped, needle
    # 幂等：再处理一次不变化（避免"第二次运行悄悄改更多东西"）。
    assert updater.strip_service_ports(stripped) == stripped


def test_strip_service_ports_also_handles_installer_copy_and_ws_override() -> None:
    updater = _updater_module()
    installer_style = REAL_COMPOSE.read_text(encoding="utf-8").replace(
        '      - "${LING_PORT:-6520}:6520"\n',
        '      - "${LING_HOST:-127.0.0.1}:${LING_PORT:-6520}:6520"\n',
    )
    assert "LING_HOST" in installer_style
    stripped = updater.strip_service_ports(installer_style)
    assert "LING_HOST" not in stripped
    assert stripped.count("ports: []") == 1

    override = REAL_WS_OVERRIDE.read_text(encoding="utf-8")
    stripped_override = updater.strip_service_ports(override)
    assert "services:\n  api:\n    ports: []\n" in stripped_override
    assert "LING_ONEBOT_WS_PORT" not in stripped_override.split("services:")[-1]


def test_compare_compose_models_rejects_leaked_and_missing_ports() -> None:
    updater = _updater_module()
    real = _config_json(6520)
    clean = _config_json(6520)
    clean["services"]["api"].pop("ports")
    updater.compare_compose_models(real, clean)

    leaked = _config_json(6520)
    with pytest.raises(updater.UpdaterError) as excinfo:
        updater.compare_compose_models(real, leaked)
    assert excinfo.value.code == "maintenance_gate_failed"

    no_ports = _config_json(6520)
    no_ports["services"]["api"].pop("ports")
    with pytest.raises(updater.UpdaterError) as excinfo:
        updater.compare_compose_models(no_ports, no_ports)
    assert "没有可阻断的宿主端口" in excinfo.value.detail

    drifted = _config_json(6520)
    drifted["services"]["api"].pop("ports")
    drifted["services"]["worker"]["image"] = OLD_REF
    with pytest.raises(updater.UpdaterError) as excinfo:
        updater.compare_compose_models(real, drifted)
    assert "除端口外" in excinfo.value.detail


# ---------------------------------------------------------------- 单元：失败码表


class _Failure:
    """只带 ``failure_code`` 读取的三个属性（真实对象是 publish.PublishError）。"""

    def __init__(self, status: str, step: str, *, after_migration: bool = False) -> None:
        self.status = status
        self.step = step
        self.after_migration = after_migration


@pytest.mark.parametrize(
    ("status", "step", "after_migration", "expected"),
    [
        # 迁移阶段：迁移命令失败、跳过迁移、迁移后 head 不符、迁移链被别人改过。
        ("failed_migration", "migrate", True, "migration_failed"),
        ("failed_migration", "migrate_skipped", True, "migration_failed"),
        ("failed_migration", "migrate_head", True, "migration_failed"),
        ("failed_migration", "migration_chain", True, "migration_failed"),
        ("failed_migration", "target_head", False, "migration_failed"),
        # 真正「已迁移、启动/验证失败」仍然是 switch_failed（分类不能被放宽）。
        ("failed_after_migration", "up_api", True, "switch_failed"),
        ("failed_after_migration", "version_container", True, "switch_failed"),
        ("failed_after_migration", "head_after_start", True, "switch_failed"),
        # env 写入有自己的码（处置方式不同），优先于 after_migration。
        ("failed_after_migration", "env_update", True, "env_write_failed"),
        # 迁移之前的失败照旧。
        ("failed_preflight", "compose_ports", False, "preflight_failed"),
        ("failed_pull", "pull", False, "pull_failed"),
        ("failed_backup", "backup_manifest", False, "backup_failed"),
    ],
)
def test_failure_code_separates_migration_from_switch(
    status: str, step: str, after_migration: bool, expected: str
) -> None:
    """码表对照：迁移阶段失败不能被 ``after_migration`` 吞成 ``switch_failed``。

    ``failed_migration`` 必须可达（网页据此提示「迁移失败/核对迁移日志」，而不是
    「目标版本启动或验证失败」）；同时 ``failed_after_migration`` 仍归
    ``switch_failed``，避免修一处把分类整体放宽。
    """
    updater = _updater_module()
    error = _Failure(status, step, after_migration=after_migration)
    assert updater.failure_code(error) == expected


def test_failure_code_tables_match_api_side() -> None:
    """两边的码表**逐字一致**（跨信任边界刻意重复，靠测试防漂移）。

    API 只按自己那份码表翻译状态文件里的失败码/下一步/告警，绝不回显原文；
    两份码表一旦漂移，网页就会把 ``migration_failed`` 之类显示成兜底文案。
    """
    from ling.api.routes import version as version_routes

    updater = _updater_module()
    assert updater.FAILURE_TEXT == version_routes.IMAGE_FAILURE_MESSAGES
    assert updater.NEXT_ACTION_TEXT == version_routes.IMAGE_NEXT_ACTION_MESSAGES
    assert updater.WARNING_TEXT == version_routes.IMAGE_WARNING_MESSAGES
    # 迁移与切换必须是**两个**码（这次修复的核心）。
    assert {"migration_failed", "switch_failed"} <= set(updater.FAILURE_TEXT)


def test_stage_table_matches_updater_stages() -> None:
    """阶段码表与更新器的 ``STAGES`` 一致；但 ``failure.stage`` 的来源**更大**。

    ``task.stage`` 只会是 ``STAGES`` 里的五个值；``failure.stage`` 不是——它复用
    ``PublishError.step`` / ``UpdaterError.stage``，于是可能是 ``pull``、
    ``preflight``、``up_api``、``reconcile``、``bootstrap`` 等等。这份差异是**有意**
    的（故障定位要细到哪一步），所以 API 侧必须白名单投影，不能因为"更新器看起来
    只会写五个阶段"就放宽校验（否则网页显示「处理中（阶段不可识别）」）。
    """
    from ling.api.routes import version as version_routes

    updater = _updater_module()
    # 顺序按 ruff 的要求：变量在左，常量表达式在右（反过来会被判 SIM300
    # Yoda 条件——它本身无害，但会让 `ruff check .` 这个门禁变红）。
    assert tuple(version_routes.IMAGE_STAGE_MESSAGES) == updater.STAGES

    # 反证"来源更大"：更新器源码里确实有 STAGES 之外的 stage 值。
    source = UPDATER_PY.read_text(encoding="utf-8")
    assert '"stage": "reconcile"' in source
    assert '"bootstrap"' in source


def updater_volume_lines(text: str) -> list[str]:
    """抽出 service 的 ``volumes:`` 条目（不引入 YAML 依赖，只按缩进扫块）。"""
    mounts: list[str] = []
    inside = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped == "volumes:":
            inside = True
            continue
        if not inside:
            continue
        if not raw.startswith("      - "):
            break
        mounts.append(raw.split("- ", 1)[1].strip())
    return mounts


INSTALL_DIR_VAR = r"\$\{LING_INSTALL_DIR:\?[^}]*\}"


def test_updater_compose_file_contract() -> None:
    text = UPDATER_COMPOSE.read_text(encoding="utf-8")
    assert "\nname: ling-updater\n" in text
    assert "${LING_UPDATER_IMAGE:?" in text
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
    assert "restart: unless-stopped" in text
    assert "/var/run/docker.sock" in text
    assert ":/srv/ling-code:ro" in text
    assert ":/run/ling-updater/requests" in text
    assert ":/var/lib/ling-updater" in text
    assert ":/var/lib/ling-backups" in text
    # 独立项目：不定义应用服务，也不发布端口。
    services_block = text.split("services:", 1)[1].split("\nnetworks:", 1)[0]
    assert "api:" not in services_block
    assert "worker:" not in services_block
    assert "db:" not in services_block
    assert "ports:" not in services_block


def test_updater_compose_mounts_env_and_lock_as_files_and_compose_as_ro() -> None:
    """缺陷①/③的挂载前提：`.env` 与锁必须是**读写单文件**，compose/ 只读。"""
    text = UPDATER_COMPOSE.read_text(encoding="utf-8")
    mounts = updater_volume_lines(text)
    assert mounts, "没有解析到 volumes 条目"
    # .env：单文件 rw（更新成功后必须能写回宿主）。
    assert [m for m in mounts if re.fullmatch(rf"{INSTALL_DIR_VAR}/\.env:/srv/ling/\.env:rw", m)]
    # 跨工具安装锁：单文件 rw（flock 绑在宿主同一个 inode 上）。
    assert [
        m
        for m in mounts
        if re.fullmatch(
            rf"{INSTALL_DIR_VAR}/\.ling-install\.lock:/srv/ling/\.ling-install\.lock:rw", m
        )
    ]
    # compose 文件组：目录 ro。
    assert [
        m for m in mounts if re.fullmatch(rf"{INSTALL_DIR_VAR}/compose:/srv/ling/compose:ro", m)
    ]
    # 不再有整目录挂载：只读会写不了 env/锁，读写会放开整个安装根。
    assert not [m for m in mounts if re.search(r":/srv/ling(?::(?:ro|rw))?$", m)], mounts
    assert not [m for m in mounts if m.endswith(":/srv/ling")], mounts


def test_updater_compose_enforces_non_root_user_and_socket_group() -> None:
    """compose **必须**强制非 root：uid/gid 显式（gid 必填）、socket gid 走 group_add。

    回归的是上一批自己承认的不一致：镜像 `USER 10002:10002`，但 compose 的
    `user:` 默认 `0:0` 且没有 `group_add`，用现成 compose 跑起来实际是 root。
    """
    text = UPDATER_COMPOSE.read_text(encoding="utf-8")
    user_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("user:")]
    assert len(user_lines) == 1, user_lines
    user_line = user_lines[0]
    # uid 缺省 10002（与镜像一致）；gid 必须显式给（`:-` 都不行，用 `:?`）。
    assert "${LING_UPDATER_UID:-10002}" in user_line, user_line
    assert re.search(r"\$\{LING_UPDATER_GID:\?[^}]*\}", user_line), user_line
    # 没有任何 root 默认值残留。
    for needle in ("LING_UPDATER_UID:-0", "LING_UPDATER_GID:-0", ":-0}", '"0:0"'):
        assert needle not in text, needle
    assert "user: root" not in text
    # socket 的 gid 各宿主不同：必须是必填插值 + group_add（补充组）。
    assert re.search(r"^    group_add:$", text, re.MULTILINE), text
    group_lines = [
        line.strip()
        for line in text.splitlines()
        if "LING_DOCKER_GID" in line and line.strip().startswith("- ")
    ]
    assert len(group_lines) == 1, group_lines
    assert re.search(r"\$\{LING_DOCKER_GID:\?[^}]*\}", group_lines[0]), group_lines[0]
    # 加固不能被“为了让非 root 写得进去”而放开。
    assert "privileged" not in re.sub(r"(?m)^\s*#.*$", "", text), "不该出现 privileged"
    assert not re.search(r"(?m)^\s*privileged\s*:", text)
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
    assert 'cap_drop: ["ALL"]' in text


def test_updater_compose_documents_installer_owned_permissions() -> None:
    """注释必须说清谁 chown/chmod，避免“文档与实现两张皮”。"""
    text = UPDATER_COMPOSE.read_text(encoding="utf-8")
    assert "0700" in text and "0600" in text
    assert "chown" in text
    # 安装器实际准备的宿主路径都出现在注释里。
    for needle in (
        "updater/state",
        "updater/requests",
        "updater/.env",
        ".ling-install.lock",
        "备份",
    ):
        assert needle in text, needle


# ---------------------------------------------------------------- 请求门禁


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "",
        "ghcr.io/qsbb/ling-api:latest",
        "ghcr.io/qsbb/ling-api:v0.0.1",
        f"ghcr.io/qsbb/ling-api@sha256:{'a' * 63}",
        f"ghcr.io/qsbb/ling-api@sha256:{'a' * 65}",
        f"ghcr.io/qsbb/ling-api@sha256:{'A' * 64}",
        f"ghcr.io/qsbb/ling-api@sha256:{'a' * 64}extra",
        f"ghcr.io/other/ling-api@sha256:{'a' * 64}",
        f"ghcr.io/qsbb/ling-api/extra@sha256:{'a' * 64}",
        f"docker.io/qsbb/ling-api@sha256:{'a' * 64}",
    ],
)
def test_only_valid_digest_is_accepted_without_docker(ws: Workspace, digest: str) -> None:
    write_request(ws, target=digest)
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []
    assert not (ws.state_dir / "task.json").exists()
    # 拒绝的请求会被认领后归档（否则下一轮会无限重试同一个坏请求）。
    assert not (ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert (ws.requests_dir / "archive" / f"claimed-{REQUEST_ID}.json").exists()
    assert ws.status()["task"] is None


def test_no_request_writes_idle_status(ws: Workspace) -> None:
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ws.calls() == []
    status = ws.status()
    assert status["updater"]["state"] == "idle"
    assert status["task"] is None
    # idle 也必须写恢复点门禁（否则"第一次更新之前"API 只能看 task，永久不可安装）。
    assert set(status["backup_gate"]) == BACKUP_GATE_KEYS


# ------------------------------------------------- 恢复点门禁（与 task 无关）


def test_idle_status_backup_gate_matches_backup_dir(ws: Workspace) -> None:
    """idle 时 status.json 里**有** backup_gate，内容与备份目录事实一致。"""
    manifest = write_manifest(ws, "20260101-000000")
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ws.calls() == []
    status = ws.status()
    gate = status["backup_gate"]
    assert set(gate) == BACKUP_GATE_KEYS
    assert status["task"] is None
    assert gate["restore_point"] is True
    assert gate["complete"] is True
    # backup_id 来自 manifest 文件名（不含路径，页面可直接显示）。
    assert gate["backup_id"] == "20260101-000000"
    assert gate["tombstones_state"] == "ok"
    assert gate["media_state"] == "present"
    assert gate["keys_root"] == "present"
    assert gate["keys_env_set"] == "true"
    assert gate["reason"] == "ok"
    assert isinstance(gate["checked_at"], str) and gate["checked_at"].endswith("Z")
    # 与目录事实一致：manifest 真的在，且状态文件里没有备份路径。
    assert manifest.is_file()
    assert str(ws.backup_dir) not in ws.status_text()


def test_empty_backup_dir_means_no_restore_point(ws: Workspace) -> None:
    """备份目录为空 → restore_point=false + 码表原因（不是"更新器离线"）。"""
    assert not list(ws.backup_dir.glob("manifest-*.txt"))
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    gate = ws.status()["backup_gate"]
    assert gate["restore_point"] is False
    assert gate["complete"] is False
    assert gate["backup_id"] is None
    assert gate["reason"] == "no_manifest"


def test_unreadable_backup_dir_reports_backup_dir_reason(ws: Workspace) -> None:
    """备份目录不可读 → 明确的码表原因（与"没有 manifest"区分开）。"""
    ws.backup_dir.rmdir()
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    gate = ws.status()["backup_gate"]
    assert gate["restore_point"] is False
    assert gate["reason"] == "backup_dir_unreadable"


def test_backup_gate_is_refreshed_after_a_task(ws: Workspace) -> None:
    """每处理完一个任务重算一次：新备份落地后门禁指向**新的** manifest。"""
    write_manifest(ws, "20250101-000000", mtime=1_600_000_000.0)
    first = run_updater(ws)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert ws.status()["backup_gate"]["backup_id"] == "20250101-000000"

    write_request(ws)
    second = run_updater(ws)
    assert second.returncode == 0, (second.stdout, second.stderr)
    status = ws.status()
    assert status["task"]["state"] == "succeeded"
    gate = status["backup_gate"]
    # 假备份脚本写到 <backup>/release-<id>/manifest-20260101-000000.txt（mtime 更新）。
    assert gate["backup_id"] == "20260101-000000", gate
    assert gate["restore_point"] is True
    assert gate["reason"] == "ok"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"media": "partial"}, "media_partial"),
        ({"tombstones": "FAILED"}, "tombstones_not_ok"),
        ({"keys_root": "absent", "keys_env": "false"}, "keys_missing"),
        ({"mode": "direct"}, "manifest_incomplete"),
    ],
)
def test_backup_gate_rejects_unusable_manifest(
    ws: Workspace, fields: dict[str, str], reason: str
) -> None:
    write_manifest(ws, "20260101-000000", **fields)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    gate = ws.status()["backup_gate"]
    assert gate["restore_point"] is False
    assert gate["reason"] == reason


def test_invalid_expected_current_values_are_rejected(ws: Workspace) -> None:
    write_request(ws, expected="latest")
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []

    write_request(ws, request_id=REQUEST_ID_B, commit="f" * 39)
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []


def test_already_on_target_is_rejected(ws: Workspace) -> None:
    write_request(ws, expected=TARGET)
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []


def test_unknown_fields_are_rejected(ws: Workspace) -> None:
    # 请求方不能借字段夹带镜像 URL / shell / compose 片段 / 路径。
    write_request(ws, extra={"image": "ghcr.io/attacker/evil:latest", "shell": "rm -rf /"})
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert "未定义字段" in result.stderr
    assert ws.calls() == []


def test_missing_or_false_operator_confirmation_is_rejected(ws: Workspace) -> None:
    write_request(ws, operator="true")
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []

    write_request(ws, request_id=REQUEST_ID_B, drop=("operator_confirmed",))
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []


def test_duplicate_request_id_is_rejected(ws: Workspace) -> None:
    archive = ws.requests_dir / "archive"
    archive.mkdir()
    (archive / f"claimed-{REQUEST_ID}.json").write_text("{}", encoding="utf-8")
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 2, result.stderr
    assert ws.calls() == []
    assert not (ws.state_dir / "task.json").exists()


# ---------------------------------------------------------------- 当前版本复核


def test_expected_current_digest_mismatch_is_rejected(ws: Workspace) -> None:
    write_request(ws, expected=f"ghcr.io/qsbb/ling-api@sha256:{'e' * 64}")
    result = run_updater(ws)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "期望当前值不一致" in result.stderr
    # 只做只读的 docker inspect/ps；绝不跑任何 compose 变更。
    assert ws.compose_calls() == []
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["still_on_old_version"] is None
    assert ws.status()["task"]["failure"]["code"] == "expected_current_mismatch"


def test_expected_current_commit_mismatch_is_rejected(ws: Workspace) -> None:
    write_request(ws, commit="f" * 40)
    result = run_updater(ws)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "期望当前值不一致" in result.stderr
    assert ws.compose_calls() == []
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["migration"]["attempted"] is False


def test_image_commit_not_matching_oci_revision_is_rejected(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"STUB_LABEL_COMMIT": OTHER_COMMIT})
    assert result.returncode == 14, (result.stdout, result.stderr)
    assert "更新器复核" in result.stderr
    assert "不一致" in result.stderr
    # 拉取本身不影响在线服务（设计 §5.1），但绝不能进维护窗口：不备份、
    # 不迁移、不启停容器。
    assert [line for line in ws.compose_calls() if " pull " in line]
    assert not [line for line in ws.compose_calls() if " run " in line]
    assert not [line for line in ws.compose_calls() if " up " in line]
    assert not [line for line in ws.compose_calls() if " stop " in line]
    assert not [line for line in ws.calls() if line.startswith("backup ")]
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["still_on_old_version"] is True


def test_platform_check_uses_docker_daemon_architecture(ws: Workspace) -> None:
    # 容器/本进程架构与宿主不一致（多架构模拟）时，必须按 daemon 报告的宿主架构
    # 判断：镜像、manifest 与 daemon 都是 arm64 → 应当接受。
    env = {
        "STUB_INFO_ARCH": OTHER_ARCH,
        "STUB_MANIFEST_ARCH": OTHER_ARCH,
        "STUB_IMAGE_ARCH": OTHER_ARCH,
    }
    write_request(ws)
    result = run_updater(ws, env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_platform_mismatch_is_rejected(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"STUB_IMAGE_ARCH": OTHER_ARCH})
    assert result.returncode == 14, (result.stdout, result.stderr)
    assert "更新器复核" in result.stderr
    assert "镜像平台" in result.stderr
    assert not [line for line in ws.compose_calls() if " up " in line]
    assert not [line for line in ws.calls() if line.startswith("backup ")]


# ---------------------------------------------------------------- 未迁移失败


def test_not_migrated_failure_claims_old_version_only_when_measured(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"STUB_PULL_EXIT": "1"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["still_on_old_version"] is True
    assert task["migration"]["attempted"] is False
    assert task["maintenance"]["state"] == "released"
    # 实测证据：旧容器 image ID 未变 + 宿主端口占用者是旧 api + /readyz 200。
    assert calls_with(ws, "http GET /readyz")
    assert calls_with(ws, "docker ps --filter publish=")
    status = ws.status()
    assert status["task"]["still_on_old_version"] is True
    assert status["task"]["failure"]["code"] == "pull_failed"


def test_not_migrated_failure_without_measured_old_service_is_not_claimed(
    ws: Workspace,
) -> None:
    # 维护窗口里停过 worker，旧服务又实测不健康 → 必须停在人工门禁。
    ws.http.readyz_status = 503
    write_request(ws)
    result = run_updater(ws, env={"FAKE_BACKUP_MODE": "fail"})
    assert result.returncode == 13, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["still_on_old_version"] is False
    status = ws.status()
    assert status["updater"]["state"] == "needs_manual"
    assert status["task"]["still_on_old_version"] is False
    assert any("人工确认旧服务" in item for item in status["task"]["next_actions"])


def test_unmeasured_old_service_never_reports_still_on_old_version(ws: Workspace) -> None:
    # 拉取失败且没动过任何服务：状态可以写"未开始"，但不得写 still_on_old_version=true。
    ws.http.readyz_status = 503
    write_request(ws)
    result = run_updater(ws, env={"STUB_PULL_EXIT": "1"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["still_on_old_version"] is False
    assert ws.status()["task"]["still_on_old_version"] is False
    assert ws.status()["task"]["failure"]["code"] == "pull_failed"


def test_updater_stops_api_and_worker_before_backup(ws: Workspace) -> None:
    """更新器**复用** ``publish.Context.backup()``：顺序必须是先停写、再备份。

    它不是自己写顺序（没有自己的 backstop），所以这条同时钉住两件事：
    1. 备份启动那一刻 api/worker 的容器实况都是停的（fake backup 直接读状态文件，
       顺序回退就以 9 失败）；
    2. 停之前捕获的 image ID 通过 ``LING_BACKUP_APP_IMAGE`` 传给 backup.sh，
       供它起一次性只读取数容器。
    """
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = ws.calls()
    stop_api = index_of(ws, " stop api")
    stop_worker = index_of(ws, " stop worker")
    backup_state = next(
        index for index, line in enumerate(lines) if line.startswith("backup-state ")
    )
    backup = index_of(ws, "backup ")
    assert stop_api < backup_state < backup
    assert stop_worker < backup_state < backup
    assert lines[backup_state] == "backup-state api=0 worker=0", lines
    env_lines = [line for line in lines if line.startswith("backup-env ")]
    assert len(env_lines) == 1
    assert "LING_BACKUP_APP_IMAGE=sha256:old-image" in env_lines[0], env_lines


def test_updater_injects_root_key_into_backup_env_without_echoing_it(
    ws: Workspace,
) -> None:
    """更新器自动受益于 publish 的修复：受限 env 的根密钥必须到 backup.sh 手里。

    更新器容器没有 LING_ROOT_KEY，夹具的子进程环境也剔除了所有 LING_*，所以
    这里能证明值来自 ``.env``（经 ``publish.Context.backup()`` 注入），而不是
    从更新器进程环境碰巧继承。
    """
    assert "LING_ROOT_KEY" not in ws.env
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = [line for line in ws.calls() if line.startswith("backup-root-key-")]
    assert "backup-root-key-set 1" in lines, lines
    expected = hashlib.sha256(ROOT_KEY.encode("utf-8")).hexdigest()
    assert f"backup-root-key-sha {expected}" in lines, lines
    # 不回显：任务/状态/私有日志/发布记录/输出里都不出现明文。
    assert ROOT_KEY not in result.stdout
    assert ROOT_KEY not in result.stderr
    artifacts = [
        ws.state_dir / "status.json",
        ws.state_dir / "task.json",
        ws.state_dir / "journal.log",
    ]
    artifacts += sorted(ws.state_dir.glob("release-*.json"))
    for path in artifacts:
        if path.is_file():
            assert ROOT_KEY not in path.read_text(encoding="utf-8"), path
    assert ROOT_KEY not in ws.call_log.read_text(encoding="utf-8")


def test_updater_missing_root_key_fails_before_backup(ws: Workspace) -> None:
    """更新器路径也 fail closed：没有根密钥就停写/备份都不做。"""
    env_text = ws.env_file.read_text(encoding="utf-8")
    assert f"LING_ROOT_KEY={ROOT_KEY}" in env_text
    ws.env_file.write_text(
        env_text.replace(f"LING_ROOT_KEY={ROOT_KEY}\n", ""), encoding="utf-8"
    )
    ws.env_file.chmod(0o600)
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 13, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["backup"]["complete"] is False
    assert task["migration"]["attempted"] is False
    assert task["failure"]["code"] == "backup_failed"
    lines = ws.calls()
    assert not [line for line in lines if line.startswith("backup")], lines
    assert not [
        line for line in lines if " stop api" in line or " stop worker" in line
    ], lines
    assert not [line for line in ws.compose_calls() if " run " in line]


def test_backup_failure_never_migrates(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"FAKE_BACKUP_MODE": "fail"})
    assert result.returncode == 13, (result.stdout, result.stderr)
    assert not [line for line in ws.compose_calls() if " run " in line]
    task = ws.task()
    assert task["migration"]["attempted"] is False
    assert task["backup"]["complete"] is False
    assert task["state"] == "failed_not_started"
    # 未迁移：恢复旧 worker/api 并实测（allow 回到旧镜像）。
    assert calls_with(ws, f"docker-env up LING_APP_IMAGE={OLD_REF}")


# ---------------------------------------------------------------- 已迁移失败


def test_migrated_failure_never_rolls_back(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"STUB_INTERNAL_READYZ_EXIT": "1"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["migration"]["attempted"] is True
    assert task["still_on_old_version"] is False
    assert task["maintenance"]["state"] == "active"
    assert ws.status()["task"]["failure"]["code"] == "switch_failed"

    # 已迁移：不得出现任何"切回旧镜像/恢复 dump"的动作。
    assert not [
        line
        for line in ws.calls()
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]
    assert not calls_with(ws, "docker load")
    assert not calls_with(ws, "docker tag")
    assert not calls_with(ws, "docker save")
    assert not calls_with(ws, "pg_restore")
    assert not calls_with(ws, "psql -f")
    # 维护门禁在失败时仍然生效：宿主端口从未发布（stub 记录）。
    assert ws.docker_state()["api_port_published"] is False


def test_post_republish_failure_recloses_gate_instead_of_rolling_back(ws: Workspace) -> None:
    write_request(ws)
    # 正式 compose 重启、宿主端口已发布之后，容器内进程自报的提交与发布记录
    # 不符（模拟"重新发布出来的不是目标构建"）：失败必须重新关上维护门禁。
    result = run_updater(ws, env={"STUB_CONTAINER_COMMIT_PUBLISHED": "f" * 40})
    assert result.returncode == 16, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert "maintenance_reenforced" in task["warnings"]
    assert ws.docker_state()["api_port_published"] is False
    # 重新关门用的是**维护副本 + 目标镜像**，不是旧镜像回滚。
    reclosing = [
        line
        for line in ws.compose_calls()
        if " up " in line and "/maintenance/" in line
    ]
    assert reclosing
    assert all(f"LING_APP_IMAGE={OLD_REF}" not in line for line in ws.calls())
    assert not [line for line in ws.calls() if "docker load" in line]


# ---------------------------------------------------------------- 维护门禁


def test_maintenance_gate_keeps_host_port_closed_until_verified(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)

    lines = ws.calls()
    maintenance_up = index_of(ws, "up -d --no-deps --no-build --pull=never api")
    # 第一次启动 api 用的必须是临时副本，且副本里 api 的 ports 为空。
    assert "/maintenance/" in lines[maintenance_up]
    gate_files = sorted((ws.state_dir / "maintenance" / REQUEST_ID).glob("*.yaml"))
    assert gate_files, "维护门禁副本必须落在状态目录里"
    gate_text = gate_files[0].read_text(encoding="utf-8")
    assert "    ports: []\n" in gate_text
    assert '${LING_PORT:-6520}:6520' not in gate_text
    assert "LING_RUN_MIGRATIONS" in gate_text

    # 容器网络内验证在"发布宿主端口"之前。
    real_up_indexes = [
        index
        for index, line in enumerate(lines)
        if line.startswith("docker compose") and "up " in line and "/maintenance/" not in line
    ]
    assert real_up_indexes, "验证通过后必须用正式 compose 重启 api"
    real_up = real_up_indexes[0]
    for needle in (
        "exec -T api python -c",
        "web_manifest.py",
        "__version__",
        "psql -U ling",
    ):
        internal = index_of(ws, needle)
        assert internal < real_up, (needle, internal, real_up)
    # 宿主 HTTP 只在正式 compose 重启之后才被打；且**只**打免鉴权的 /readyz
    # （`/v1/version` 要鉴权，更新器不持有凭据）。
    assert index_of(ws, "http GET /readyz") > real_up
    assert not calls_with(ws, "http GET /v1/")

    # 维护期（api 已停、尚未发布端口）宿主端口没有占用者。
    publish_probes = [
        index for index, line in enumerate(lines) if "ps --filter publish=" in line
    ]
    assert publish_probes
    assert make_index_between(ws, "stop api", "up -d --no-deps --no-build --pull=never api")
    status = ws.status()
    assert status["task"]["state"] == "succeeded"
    assert status["task"]["maintenance"] == {"state": "released", "verified": True}
    assert status["task"]["migration"]["current_after"] == HEAD_NEW
    assert status["task"]["backup"]["complete"] is True


def make_index_between(ws: Workspace, first: str, second: str) -> bool:
    start = index_of(ws, first)
    end = index_of(ws, second)
    return start < end


def test_maintenance_gate_rejects_model_drift_before_touching_anything(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws, env={"STUB_CONFIG_MAINT_PORTS": "1"})
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "维护门禁" in result.stderr
    assert not [line for line in ws.compose_calls() if " pull " in line]
    assert not [line for line in ws.compose_calls() if " stop " in line]
    assert not [line for line in ws.calls() if line.startswith("backup ")]
    task = ws.task()
    assert task["maintenance"]["state"] == "released"
    assert task["failure"]["code"] == "maintenance_gate_failed"
    assert task["still_on_old_version"] is True


def test_success_path_uses_target_digest_and_records_identity(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "succeeded"
    assert task["target"]["digest"] == TARGET
    assert task["target"]["commit"] == COMMIT
    assert task["previous"]["digest"] == OLD_REF
    assert task["previous"]["commit"] == OLD_COMMIT
    assert task["backup"]["id"] == f"backup-{REQUEST_ID[:12]}"
    assert task["migration"]["target_head"] == HEAD_NEW
    # 挂载形态变了（env 是单文件 rw）：不再有「宿主 env 未更新」的 warning/待办，
    # 更新成功的定义变成「宿主 .env 真的指向目标」（见下一个用例）。
    assert "host_env_image_not_updated" not in task["warnings"]
    assert "update_host_env_image" not in task["next_actions"]
    # 同一组 -f 与 --env-file 贯穿（迁移 run 用正式组，切换用临时副本组）。
    run_calls = [line for line in ws.compose_calls() if " run " in line]
    assert len(run_calls) == 1
    assert f"-f {ws.compose_file}" in run_calls[0]
    assert f"--env-file {ws.env_file}" in run_calls[0]
    assert ws.docker_state()["api_port_published"] is True


def test_success_persists_target_digest_into_app_env_file(ws: Workspace) -> None:
    """缺陷①：更新成功后**宿主** `.env` 必须真的指向目标摘要（真实文件断言）。"""
    before = ws.env_file.read_bytes()
    assert f"LING_APP_IMAGE={OLD_REF}\n".encode() in before
    env_inode = ws.env_file.stat().st_ino
    lock_inode = ws.lock_file.stat().st_ino

    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)

    text = ws.env_file.read_text(encoding="utf-8")
    image_lines = [line for line in text.splitlines() if line.startswith("LING_APP_IMAGE=")]
    assert image_lines == [f"LING_APP_IMAGE={TARGET}"], text
    # 其它字段逐字保留（不是整份重写、不是只剩一行）。
    assert "POSTGRES_PASSWORD=super-secret-password" in text
    # 原地重写不换 inode（换了等于挂载点上的文件"脱钩"）。
    assert ws.env_file.stat().st_ino == env_inode
    assert stat.S_IMODE(ws.env_file.stat().st_mode) == 0o600
    # 不需要人工恢复：私有回滚副本不应残留。
    assert list(ws.state_dir.glob("env-before-*.env")) == []
    # 锁文件只被 open/flock：inode 不变（换了另一进程能在同一路径上再拿一把锁）。
    assert ws.lock_file.stat().st_ino == lock_inode

    # 持久化的 env 自己就能解析出目标摘要：publish 的最后一步是不带 shell 覆盖
    # 的 `compose config`（stub 把这次调用记成 `LING_APP_IMAGE=` 空值）。
    no_override = [line for line in ws.calls() if line.endswith("LING_APP_IMAGE=")]
    assert any(" config " in line for line in no_override), ws.calls()


def test_env_write_survives_a_read_only_mount_parent(ws: Workspace) -> None:
    """容器里 `/srv/ling` 是只读根：只有 `.env` 是单文件 rw 挂载。

    把 env 所在目录设成 0o500（不能新建/改名任何文件）来模拟单文件挂载：
    原地重写必须仍然成功。任何「同目录建临时文件再 rename」（publish 原来的
    ``write_atomic``）在这里都会 EACCES——这就是缺陷①的回归门禁。
    """
    ws.env_file.parent.chmod(0o500)
    try:
        write_request(ws)
        result = run_updater(ws)
    finally:
        ws.env_file.parent.chmod(0o700)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"LING_APP_IMAGE={TARGET}" in ws.env_file.read_text(encoding="utf-8")
    # 单文件挂载的目录里不能多出临时文件；`releases.json` 是**随安装包一起装的**
    # 可信发布目录（与 compose 文件同级），不是这次写入产生的。
    assert sorted(path.name for path in ws.env_file.parent.iterdir()) == [
        ".env",
        ".ling-install.lock",
        "compose.image.yaml",
        "releases.json",
    ]


def test_env_write_failure_leaves_old_content_intact(ws: Workspace) -> None:
    """写不进 env 时：旧内容逐字节完好、容器不动、转人工，不留半写。"""
    before = ws.env_file.read_bytes()
    ws.env_file.chmod(0o400)  # 属主也不可写：模拟只读/权限错误
    try:
        write_request(ws)
        result = run_updater(ws)
    finally:
        ws.env_file.chmod(0o600)

    assert result.returncode == 16, (result.stdout, result.stderr)
    assert ws.env_file.read_bytes() == before
    assert f"LING_APP_IMAGE={OLD_REF}" in ws.env_file.read_text(encoding="utf-8")
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "env_write_failed"
    assert task["still_on_old_version"] is False
    assert ws.status()["updater"]["state"] == "needs_manual"
    # 已迁移后失败：不许切旧镜像、不许（重新）启动任何容器。
    assert not [line for line in ws.calls() if f"LING_APP_IMAGE={OLD_REF}" in line]
    assert [line for line in ws.compose_calls() if " up " in line] == []
    assert ws.docker_state()["running"] == {"api": False, "worker": False}
    # 内容没变 → 不需要人工恢复，回滚副本不残留。
    assert list(ws.state_dir.glob("env-before-*.env")) == []


def test_env_write_rolls_back_a_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原地重写写了一半就失败：必须把旧字节写回（不许留半写状态）。"""
    updater = _updater_module()
    env = tmp_path / "app.env"
    env.write_text("POSTGRES_PASSWORD=x\nLING_APP_IMAGE=old\n", encoding="utf-8")
    env.chmod(0o600)
    before = env.read_bytes()
    backup = tmp_path / "env-before-abc.env"
    calls = {"n": 0}
    real = updater.truncate_rewrite

    def flaky(path: Path, data: bytes, mode: int = 0o600) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            path.write_bytes(data[: len(data) // 2])  # 真的写了一半
            raise OSError("模拟内核写满")
        real(path, data, mode)

    monkeypatch.setattr(updater, "truncate_rewrite", flaky)
    with pytest.raises(OSError) as excinfo:
        updater.write_env_in_place(
            env,
            b"POSTGRES_PASSWORD=x\nLING_APP_IMAGE=new\n",
            backup_path=backup,
            save_backup=_save_copy,
        )
    assert not isinstance(excinfo.value, updater.EnvWriteError)
    assert env.read_bytes() == before
    assert not backup.exists()


def _save_copy(target: Path, payload: bytes, mode: int) -> None:
    target.write_bytes(payload)
    target.chmod(mode)


def test_env_write_keeps_private_copy_when_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连回滚都失败时：保留私有副本（旧内容）并显式报「未恢复」。"""
    updater = _updater_module()
    env = tmp_path / "app.env"
    env.write_text("POSTGRES_PASSWORD=x\nLING_APP_IMAGE=old\n", encoding="utf-8")
    env.chmod(0o600)
    before = env.read_bytes()
    backup = tmp_path / "env-before-abc.env"

    def always_partial(path: Path, data: bytes, mode: int = 0o600) -> None:
        path.write_bytes(data[:4])
        raise OSError("模拟设备只读")

    monkeypatch.setattr(updater, "truncate_rewrite", always_partial)
    with pytest.raises(updater.EnvWriteError) as excinfo:
        updater.write_env_in_place(
            env,
            b"POSTGRES_PASSWORD=x\nLING_APP_IMAGE=new\n",
            backup_path=backup,
            save_backup=_save_copy,
        )
    assert excinfo.value.restored is False
    assert backup.read_bytes() == before  # 旧内容在私有副本里，可人工恢复
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


# ---------------------------------------------------------------- 状态脱敏


def test_status_file_is_sanitized_and_fixed_shape(ws: Workspace) -> None:
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    text = ws.status_text()
    status = ws.status()
    assert set(status) == STATUS_KEYS
    assert set(status["task"]) == TASK_VIEW_KEYS
    assert set(status["updater"]) == {"state", "version"}
    # 密码、备份路径、私有日志、发布记录路径一律不出现。
    assert "super-secret-password" not in text
    assert "POSTGRES_PASSWORD" not in text
    assert str(ws.backup_dir) not in text
    assert str(ws.state_dir) not in text
    assert str(ws.requests_dir) not in text
    assert "journal.log" not in text
    assert "backup.log" not in text
    assert "release-" not in text
    assert "Traceback" not in text
    # 私有文件仍然存在（诊断用），但权限是 0600。
    journal = ws.state_dir / "journal.log"
    assert journal.is_file()
    assert stat.S_IMODE(journal.stat().st_mode) & 0o077 == 0


# ---------------------------------------------------------------- 重启对账


def test_restart_after_migration_stops_at_manual_gate(ws: Workspace) -> None:
    seed_task(ws, stage="switch", attempted=True)
    result = run_updater(ws)
    assert result.returncode == 17, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "switch_failed"
    assert task["still_on_old_version"] is False
    # 不继续原来的阶段：不迁移、不切镜像、不启动已停的服务。
    assert not [line for line in ws.compose_calls() if " run " in line]
    assert not [
        line
        for line in ws.calls()
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]
    ups = [line for line in ws.compose_calls() if " up " in line]
    # 允许（且应当）把维护门禁重新关上：用维护副本 + 目标镜像，不是回滚。
    for line in ups:
        assert "/maintenance/" in line
    assert ws.docker_state()["api_port_published"] is False


def test_restart_before_migration_recovers_and_measures_old_version(ws: Workspace) -> None:
    seed_task(ws, stage="backup", attempted=False, maintenance="active")
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "failed_not_started"
    assert task["still_on_old_version"] is True
    assert task["maintenance"]["state"] == "released"
    assert calls_with(ws, "http GET /readyz")
    assert not [line for line in ws.compose_calls() if " run " in line]
    # 没有新建第二个任务：request_id 仍然是持久记录里的那个。
    assert task["request_id"] == REQUEST_ID_B
    assert list(ws.requests_dir.glob("request-*.json")) == []


def test_restart_with_unknown_state_does_not_mutate(ws: Workspace) -> None:
    seed_task(ws, stage="migrate", attempted=False, maintenance="active")
    result = run_updater(ws, env={"STUB_DB_EXEC_EXIT": "1"})
    assert result.returncode == 17, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "reconcile_needs_manual"
    assert task["still_on_old_version"] is None
    # 不能确认 → 只读探测，不做任何变更。
    assert mutating_compose_calls(ws) == []
    assert calls_with(ws, "docker-env up") == []
    assert calls_with(ws, "http GET") == []


def test_needs_manual_blocks_new_tasks_until_acknowledged(ws: Workspace) -> None:
    seed_task(ws, stage="migrate", attempted=True)
    assert run_updater(ws).returncode == 17
    ws.reset()
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 17, (result.stdout, result.stderr)
    # 上一个任务没有人工收口前，新请求不被认领，也不新建第二次任务。
    assert (ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert ws.calls() == []
    assert ws.task()["request_id"] == REQUEST_ID_B

    ack = run_updater(ws, extra=["--acknowledge-manual", REQUEST_ID_B])
    assert ack.returncode == 0, (ack.stdout, ack.stderr)
    assert ws.task()["state"] == "resolved"
    assert ws.status()["updater"]["state"] == "idle"


# ---------------------------------------------------------------- 单飞


def test_single_flight_processes_one_of_two_requests(ws: Workspace) -> None:
    write_request(ws, REQUEST_ID)
    write_request(ws, REQUEST_ID_B)
    result = run_updater(ws)
    assert result.returncode in {0, 14, 16}, (result.stdout, result.stderr)
    pulls = [line for line in ws.compose_calls() if " pull " in line]
    assert len(pulls) == 1
    task = ws.task()
    assert task["request_id"] == REQUEST_ID
    remaining = sorted(path.name for path in ws.requests_dir.glob("request-*.json"))
    assert remaining == [f"request-{REQUEST_ID_B}.json"]
    archived = sorted(path.name for path in (ws.requests_dir / "archive").iterdir())
    assert archived == [f"claimed-{REQUEST_ID}.json"]


_HOLD_LOCK = r'''
import fcntl
import sys

handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
sys.stdin.readline()
'''


def test_single_flight_second_instance_is_busy(ws: Workspace) -> None:
    write_request(ws)
    helper = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(ws.state_dir / "updater.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "locked"
        result = run_updater(ws)
    finally:
        if helper.stdin is not None:
            helper.stdin.close()
        helper.wait(timeout=10)
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert ws.calls() == []
    assert (ws.requests_dir / f"request-{REQUEST_ID}.json").exists()


# ---------------------------------------------------------------- 跨工具安装锁


def test_install_lock_busy_needs_manual_and_touches_nothing(ws: Workspace) -> None:
    """缺陷③：`install.sh`/`ling install`/`publish.py` 持锁时，更新器必须停手。"""
    write_request(ws)
    inode = ws.lock_file.stat().st_ino
    helper = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(ws.lock_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "locked"
        result = run_updater(ws)
    finally:
        if helper.stdin is not None:
            helper.stdin.close()
        helper.wait(timeout=10)

    assert result.returncode == 17, (result.stdout, result.stderr)
    # 一个 compose 调用都没有：不 pull、不 stop、不 run、不 up。
    assert ws.compose_calls() == []
    assert [line for line in ws.calls() if line.startswith("docker-env")] == []
    assert not [line for line in ws.calls() if line.startswith("backup ")]
    # 只有一次只读的宿主架构探测（在拿锁之前发生，用于构造 Options）。
    assert all(line.startswith("docker info") for line in ws.calls()), ws.calls()

    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"] == {"code": "install_lock_busy", "stage": "prepare"}
    assert task["next_actions"] == ["acknowledge_manual"]
    assert ws.docker_state()["running"] == {"api": True, "worker": True}
    assert ws.docker_state()["alembic_version"] == HEAD_OLD
    status = ws.status()
    assert status["updater"]["state"] == "needs_manual"
    assert status["task"]["failure"]["message"] == (
        "另一个安装/发布/更新任务正持有安装锁，本次未执行任何操作"
    )
    # 请求被认领归档（不会无限重试同一个请求），但锁文件本身没被动过。
    assert not (ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert ws.lock_file.stat().st_ino == inode


def test_install_lock_is_released_after_success(ws: Workspace) -> None:
    """验证结束后必须释放锁，否则下一次安装/发布永远拿不到。"""
    write_request(ws)
    assert run_updater(ws).returncode == 0

    lock = _updater_module()
    pub = lock.load_publish(PUBLISH_PY)
    probe = pub.InstallLock(ws.env_file)
    probe.acquire()
    try:
        assert probe.path == Path(os.path.realpath(ws.lock_file))
    finally:
        probe.release()


# ---------------------------------------------------------------- 开关不再互锁
#
# 网页一键更新的真实顺序：用户点「更新」→ `POST /v1/version/image-update-requests`
# （要求 `LING_UPDATE_ENABLED=1` 才收）→ 更新器接手 → 调 `publish.py`。而
# `publish.py` 的预检**曾经**硬要求 `LING_UPDATE_ENABLED=0`，于是两边互锁，功能
# 从来走不通（生产实测：201 之后 failed_not_started / preflight_failed /
# compose_env）。下面这条走**真实入口**（真跑 `updater.py`，它自己按路径加载
# `publish.py`），在 compose 模型里把开关设成 1，证明整条链能走完。
#
# 并发不靠开关值保证：`updater.lock` 单飞 + 跨工具 `.ling-install.lock`，
# 见上面「跨工具安装锁」那组用例（锁被占用时**立即** needs_manual，不等待）。


def test_web_update_chain_passes_preflight_with_switch_on(ws: Workspace) -> None:
    """开关=1 时更新器 → publish 预检必须放行，且记录里留下开关实况。"""
    config = _config_json(ws.http.port)
    config["services"]["api"]["environment"]["LING_UPDATE_ENABLED"] = "1"
    ws.config_path.write_text(json.dumps(config), encoding="utf-8")

    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 失败码里不能再出现 compose_env / preflight_failed（那正是互锁的症状）。
    assert "compose_env" not in result.stderr, result.stderr
    task = ws.task()
    assert task["state"] == "succeeded"
    assert task["failure"] is None

    # 发布记录是更新器跑 publish.Context 时写的私有 JSON：开关的**观察值**在里面。
    records = sorted(ws.state_dir.glob(f"release-{REQUEST_ID}.json"))
    assert len(records) == 1, sorted(ws.state_dir.iterdir())
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "success"
    assert record["compose_env"] == {
        "LING_UPDATE_ENABLED": "1",
        "LING_RUN_MIGRATIONS": "0",
    }
    # 迁移仍然只有那一次显式动作（开关=1 没有让 api 变成"启动即迁移"）。
    migrate_calls = [line for line in ws.compose_calls() if " run " in line]
    assert len(migrate_calls) == 1, migrate_calls
    assert "LING_RUN_MIGRATIONS=1" in migrate_calls[0]


def test_install_lock_is_checked_immediately_not_waited_on(ws: Workspace) -> None:
    """锁被占用时必须**立即** needs_manual：等待会让网页一直转圈到超时。

    与 `test_install_lock_busy_needs_manual_and_touches_nothing` 的分工：那条证明
    "什么都不做"，这条证明"没有等"。变异（把非阻塞 flock 改成阻塞等待）会让这里
    以超时/耗时断言失败，而不是含糊地挂在 CI 里。
    """
    write_request(ws)
    helper = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(ws.lock_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "locked"
        started = time.monotonic()
        result = run_updater(ws, timeout=30)
        elapsed = time.monotonic() - started
        # 持锁者仍然活着（stdin 还没关）——所以这不是"等到了别人释放"。
        assert helper.poll() is None
    finally:
        if helper.stdin is not None:
            helper.stdin.close()
        helper.wait(timeout=10)

    assert result.returncode == 17, (result.stdout, result.stderr)
    assert elapsed < 15, f"锁被占用时等待了 {elapsed:.1f}s，应当立即返回"
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"] == {"code": "install_lock_busy", "stage": "prepare"}
    assert ws.docker_state()["running"] == {"api": True, "worker": True}
    assert ws.docker_state()["alembic_version"] == HEAD_OLD


# ---------------------------------------------------------------- 可信发布目录
#
# 设计依据：docs/EASY-INSTALL-UPDATE-DESIGN.md「可信发布目录只列已通过门禁、
# 可从已知版本升级的稳定目标」+「可编辑的 Release 正文不是摘要可信来源」。
#
# 这里锁死四条硬规则：
# 1. 当前版本由 `.env` 的摘要**反查**目录得到（不猜、不问 Docker）；
# 2. 只允许升级：目标 version 不高于当前版本 → **不出现**在 available_targets；
# 3. 版本跨度：当前版本不在目标 min_from 里 → compatible=false + 原因；
# 4. 目录缺失/坏/工具缺失 → available_targets=[] + 明确 reason（不静默、不假装）。


def test_idle_status_exposes_compatible_recommended_target(ws: Workspace) -> None:
    """有兼容目标时：available_targets 只列目标，recommended_target 指向它。"""
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 读目录是本地只读文件操作：idle **不产生任何 Docker 调用**。
    assert ws.calls() == []
    status = ws.status()
    assert status["release_catalog"] == {
        "state": "ok",
        "reason": None,
        "entries": 2,
        "current_version": CURRENT_VERSION,
    }
    assert status["available_targets"] == [
        {
            "version": TARGET_VERSION,
            "digest": TARGET,
            "commit": COMMIT,
            "has_migration": True,
            "compatible": True,
            "incompatible_reason": None,
        }
    ]
    assert status["recommended_target"] == {
        "version": TARGET_VERSION,
        "digest": TARGET,
        "commit": COMMIT,
        "has_migration": True,
    }


def test_recommended_target_is_the_greatest_compatible_version(ws: Workspace) -> None:
    """多目标时推荐**版本最大**的兼容目标（不依赖文件顺序）。"""
    newest = "ghcr.io/qsbb/ling-api@sha256:" + "7" * 64
    ws.write_catalog(
        [
            catalog_entry(
                CURRENT_VERSION,
                digest=OLD_REF,
                commit=OLD_COMMIT,
                min_from=["0.0.0"],
                has_migration=False,
            ),
            # 故意把新版本写在前面：使用方不能依赖文件顺序。
            catalog_entry("0.0.10", digest=newest, commit=COMMIT, min_from=[CURRENT_VERSION]),
            catalog_entry(TARGET_VERSION, min_from=[CURRENT_VERSION]),
        ]
    )
    assert run_updater(ws).returncode == 0
    status = ws.status()
    assert [item["version"] for item in status["available_targets"]] == [
        TARGET_VERSION,
        "0.0.10",
    ]
    assert status["recommended_target"]["version"] == "0.0.10"
    assert status["recommended_target"]["digest"] == newest


def test_target_outside_min_from_is_incompatible_with_reason(ws: Workspace) -> None:
    """当前版本不在 min_from 里：不兼容 + 原因，且没有推荐目标。"""
    ws.write_catalog(
        [
            catalog_entry(
                CURRENT_VERSION,
                digest=OLD_REF,
                commit=OLD_COMMIT,
                min_from=["0.0.0"],
                has_migration=False,
            ),
            catalog_entry(TARGET_VERSION, min_from=["0.0.9"]),
        ]
    )
    assert run_updater(ws).returncode == 0
    status = ws.status()
    assert status["release_catalog"]["current_version"] == CURRENT_VERSION
    assert status["available_targets"] == [
        {
            "version": TARGET_VERSION,
            "digest": TARGET,
            "commit": COMMIT,
            "has_migration": True,
            "compatible": False,
            "incompatible_reason": "not_in_min_from",
        }
    ]
    assert status["recommended_target"] is None


def test_target_not_newer_is_not_offered_at_all(ws: Workspace) -> None:
    """不许降级：目标版本不高于当前版本时**不出现**，也就没有推荐目标。"""
    ws.write_catalog(
        [
            # 当前装的是 0.0.2（摘要 OLD_REF），目录里 0.0.1 比它旧。
            catalog_entry(
                TARGET_VERSION,
                digest=OLD_REF,
                commit=OLD_COMMIT,
                min_from=[CURRENT_VERSION],
                has_migration=False,
            ),
            catalog_entry(CURRENT_VERSION, digest=TARGET, commit=COMMIT, min_from=["0.0.0"]),
        ]
    )
    assert run_updater(ws).returncode == 0
    status = ws.status()
    assert status["release_catalog"]["current_version"] == TARGET_VERSION
    assert status["available_targets"] == []
    assert status["recommended_target"] is None


def test_missing_catalog_yields_no_targets_and_reason(ws: Workspace) -> None:
    ws.release_catalog.unlink()
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    status = ws.status()
    assert status["release_catalog"] == {
        "state": "missing",
        "reason": "catalog_missing",
        "entries": 0,
        "current_version": None,
    }
    assert status["available_targets"] == []
    assert status["recommended_target"] is None
    # 状态文件里不出现目录路径（跨信任边界只放码）。
    assert str(ws.release_catalog) not in ws.status_text()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"schema": 1, "releases": [{"version": "0.0.2"}]}, "catalog_invalid"),
        ({"schema": 2, "releases": []}, "catalog_schema_unsupported"),
        ([], "catalog_invalid"),
    ],
)
def test_invalid_catalog_yields_no_targets_and_reason(
    ws: Workspace, payload: object, reason: str
) -> None:
    ws.release_catalog.write_text(json.dumps(payload), encoding="utf-8")
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    status = ws.status()
    assert status["release_catalog"]["state"] == "invalid"
    assert status["release_catalog"]["reason"] == reason
    assert status["available_targets"] == []
    assert status["recommended_target"] is None


def test_world_writable_catalog_is_treated_as_unreadable(ws: Workspace) -> None:
    ws.release_catalog.chmod(0o666)
    assert run_updater(ws).returncode == 0
    status = ws.status()
    assert status["release_catalog"]["state"] == "invalid"
    assert status["release_catalog"]["reason"] == "catalog_unreadable"
    assert status["available_targets"] == []


def test_missing_catalog_tool_is_reported_not_fatal(ws: Workspace, tmp_path: Path) -> None:
    """发行包缺 catalog.py：不退出、不假装有目标，状态里给 catalog_tool_missing。"""
    release_dir = tmp_path / "release-without-tool"
    release_dir.mkdir()
    copy = release_dir / "publish.py"
    shutil.copyfile(PUBLISH_PY, copy)
    result = run_updater(ws, extra=["--publish-py", str(copy)])
    assert result.returncode == 0, (result.stdout, result.stderr)
    status = ws.status()
    assert status["release_catalog"]["state"] == "invalid"
    assert status["release_catalog"]["reason"] == "catalog_tool_missing"
    assert status["available_targets"] == []
    assert status["recommended_target"] is None


def test_unknown_current_version_blocks_all_targets(ws: Workspace) -> None:
    """当前摘要不在目录里：当前版本未知 → 所有目标不兼容，绝不猜当前版本。"""
    ws.write_catalog([catalog_entry(TARGET_VERSION)])
    assert run_updater(ws).returncode == 0
    status = ws.status()
    assert status["release_catalog"]["state"] == "ok"
    assert status["release_catalog"]["current_version"] is None
    assert status["available_targets"] == [
        {
            "version": TARGET_VERSION,
            "digest": TARGET,
            "commit": COMMIT,
            "has_migration": True,
            "compatible": False,
            "incompatible_reason": "current_version_unknown",
        }
    ]
    assert status["recommended_target"] is None


def test_target_outside_catalog_is_rejected_before_any_docker_call(ws: Workspace) -> None:
    """请求里的 digest 格式合格但不在目录里 → 拒绝，一个 Docker 调用都不发。"""
    outside = "ghcr.io/qsbb/ling-api@sha256:" + "e" * 64
    write_request(ws, target=outside)
    result = run_updater(ws)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert ws.calls() == []
    assert not (ws.state_dir / "task.json").exists()
    assert ws.status()["updater"]["state"] == "idle"
    assert ws.status()["task"] is None
    journal = (ws.state_dir / "journal.log").read_text(encoding="utf-8")
    assert "可信发布目录" in journal
    # 请求被认领归档（不会无限重试），没有被执行。
    assert not (ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert (ws.requests_dir / "archive" / f"claimed-{REQUEST_ID}.json").exists()


def test_catalog_broken_rejects_request_without_touching_anything(ws: Workspace) -> None:
    """目录坏掉时请求被拒（catalog_unavailable），不拉取/不备份/不切换。"""
    ws.release_catalog.write_text('{"schema": 1, "releases": [{}]}', encoding="utf-8")
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert ws.calls() == []
    assert not (ws.state_dir / "task.json").exists()
    journal = (ws.state_dir / "journal.log").read_text(encoding="utf-8")
    assert "可信发布目录不可用" in journal


def test_catalog_entry_with_invalid_digest_blocks_the_whole_catalog(ws: Workspace) -> None:
    """目标来自目录，但目录里那条 digest 格式非法 → 整份目录不可信，请求被拒。"""
    ws.write_catalog(
        [
            catalog_entry(
                CURRENT_VERSION,
                digest=OLD_REF,
                commit=OLD_COMMIT,
                min_from=["0.0.0"],
                has_migration=False,
            ),
            catalog_entry(TARGET_VERSION, digest="ghcr.io/qsbb/ling-api:latest"),
        ]
    )
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert ws.calls() == []
    assert not (ws.state_dir / "task.json").exists()
    status = ws.status()
    assert status["release_catalog"]["state"] == "invalid"
    assert status["release_catalog"]["reason"] == "catalog_invalid"
    assert status["available_targets"] == []


def test_successful_update_moves_the_current_version_in_the_catalog(ws: Workspace) -> None:
    """更新成功后受限 env 指向目标摘要 → 当前版本变成 0.0.2，0.0.2 不再是目标。"""
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"LING_APP_IMAGE={TARGET}" in ws.env_file.read_text(encoding="utf-8")
    status = ws.status()
    assert status["release_catalog"]["current_version"] == TARGET_VERSION
    assert status["available_targets"] == []
    assert status["recommended_target"] is None


# ------------------------------------------- 联网取目录（--catalog-url / LING_UPDATER_CATALOG_URL）


def _catalog_payload(version: str = "0.0.3", **overrides: Any) -> bytes:
    """一份合法的目录字节（结构与 deploy/release/catalog.py 的 schema 一致）。"""
    entry = {
        "version": version,
        "digest": "ghcr.io/qsbb/ling-api@sha256:" + "a" * 64,
        "commit": "b" * 40,
        "min_from": ["0.0.1"],
        "has_migration": True,
        "released_at": "2026-11-06T00:00:00Z",
    }
    entry.update(overrides)
    return (json.dumps({"schema": 1, "releases": [entry]}, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def test_catalog_url_defaults_to_none(tmp_path: Path) -> None:
    """不配就**不联网**：已部署实例的行为必须与改动前完全一样。"""
    module = _updater_module()
    cfg = module.parse_config(
        [
            "--requests-dir", str(tmp_path / "requests"),
            "--state-dir", str(tmp_path / "state"),
            "--compose-file", str(REPO_ROOT / "deploy" / "compose.image.yaml"),
            "--env-file", str(tmp_path / ".env"),
            "--backup-dir", str(tmp_path / "backups"),
            "--backup-script", str(tmp_path / "backup.sh"),
            "--publish-py", str(PUBLISH_PY),
        ]
    )
    assert cfg.catalog_url is None


def test_catalog_url_can_be_given_on_cli(tmp_path: Path) -> None:
    module = _updater_module()
    url = "https://raw.githubusercontent.com/qsbb/ling/main/deploy/releases.json"
    cfg = module.parse_config(
        [
            "--requests-dir", str(tmp_path / "requests"),
            "--state-dir", str(tmp_path / "state"),
            "--compose-file", str(REPO_ROOT / "deploy" / "compose.image.yaml"),
            "--env-file", str(tmp_path / ".env"),
            "--backup-dir", str(tmp_path / "backups"),
            "--backup-script", str(tmp_path / "backup.sh"),
            "--publish-py", str(PUBLISH_PY),
            "--catalog-url", url,
        ]
    )
    assert cfg.catalog_url == url


def test_fetch_rejects_plain_http() -> None:
    """http 会让链路上任何人替换目录内容：直接拒绝（返回 None，退回本地）。"""
    module = _updater_module()
    assert module._fetch_catalog_bytes("http://example.com/releases.json", 1024) is None


def test_fetch_returns_none_on_connection_error() -> None:
    """连不上不是错误：返回 None，让调用方退回本地那份。"""
    module = _updater_module()
    # 127.0.0.1 上一个几乎不可能在听的端口。
    assert module._fetch_catalog_bytes("https://127.0.0.1:1/x.json", 1024) is None


def test_fetch_rejects_oversized_response() -> None:
    """超过上限直接放弃：不把任意大的响应读进内存。"""
    module = _updater_module()
    # 用真实 https 不好造，这里直接验证"读到 max+1 就判超"的边界：
    # 请求必然失败（连不上），但函数不该因此抛异常。
    assert module._fetch_catalog_bytes("https://127.0.0.1:1/x.json", 8) is None


def test_load_catalog_bytes_uses_remote_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """联网取回合法内容 -> 用远程那份，**不**读本地文件。"""
    module = _updater_module()
    tool = module.load_catalog_tool(REPO_ROOT / "deploy" / "release" / "catalog.py")

    called: dict[str, Any] = {"local": False}

    def fake_load(path: Path) -> dict[str, Any]:
        called["local"] = True
        raise AssertionError("远程成功时不该读本地")

    monkeypatch.setattr(module, "_fetch_catalog_bytes", lambda url, n: _catalog_payload())
    monkeypatch.setattr(tool, "load_catalog", fake_load)

    cfg = module.Config.__new__(module.Config)
    object.__setattr__(cfg, "catalog_url", "https://example.com/releases.json")
    object.__setattr__(cfg, "release_catalog", Path("/nonexistent/releases.json"))

    catalog = module._load_catalog_bytes(cfg, tool)
    assert [entry["version"] for entry in catalog["releases"]] == ["0.0.3"]
    assert called["local"] is False


def test_load_catalog_bytes_falls_back_to_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """联网失败 -> 退回本地那份，且不抛异常。"""
    module = _updater_module()
    tool = module.load_catalog_tool(REPO_ROOT / "deploy" / "release" / "catalog.py")

    local = tmp_path / "releases.json"
    local.write_bytes(_catalog_payload(version="0.0.2"))
    local.chmod(0o644)

    monkeypatch.setattr(module, "_fetch_catalog_bytes", lambda url, n: None)

    cfg = module.Config.__new__(module.Config)
    object.__setattr__(cfg, "catalog_url", "https://example.com/releases.json")
    object.__setattr__(cfg, "release_catalog", local)

    catalog = module._load_catalog_bytes(cfg, tool)
    assert [entry["version"] for entry in catalog["releases"]] == ["0.0.2"]


def test_load_catalog_bytes_rejects_invalid_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """远程内容非法 -> 交给 parse_catalog 抛错（调用方按既有方式收敛成原因码）。"""
    module = _updater_module()
    tool = module.load_catalog_tool(REPO_ROOT / "deploy" / "release" / "catalog.py")

    monkeypatch.setattr(
        module, "_fetch_catalog_bytes", lambda url, n: b'{"schema": 1, "releases": [{"version": "bad"}]}'
    )

    cfg = module.Config.__new__(module.Config)
    object.__setattr__(cfg, "catalog_url", "https://example.com/releases.json")
    object.__setattr__(cfg, "release_catalog", tmp_path / "releases.json")

    with pytest.raises(Exception) as exc:
        module._load_catalog_bytes(cfg, tool)
    # catalog.py 的固定错误码，不是我们另造一套。
    assert getattr(exc.value, "code", "") in {
        "release_field_missing",
        "digest_invalid",
        "commit_invalid",
        "min_from_invalid",
        "semver_invalid",
    }


def test_parse_config_derives_catalog_paths_from_the_distribution(tmp_path: Path) -> None:
    """默认值来自**发行包**：目录数据在 deploy/releases.json，校验工具在
    deploy/release/catalog.py（容器里就是 /srv/ling-code 那两条）。"""
    module = _updater_module()
    cfg = module.parse_config(
        [
            "--requests-dir",
            str(tmp_path / "requests"),
            "--state-dir",
            str(tmp_path / "state"),
            "--compose-file",
            str(ws_compose := REPO_ROOT / "deploy" / "compose.image.yaml"),
            "--env-file",
            str(tmp_path / ".env"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--backup-script",
            str(tmp_path / "backup.sh"),
            "--publish-py",
            str(PUBLISH_PY),
        ]
    )
    assert ws_compose.is_file()
    assert cfg.release_catalog == REPO_ROOT / "deploy" / "releases.json"
    assert cfg.catalog_tool == REPO_ROOT / "deploy" / "release" / "catalog.py"


# ------------------------------- 缺陷 7：宿主端口探活借 host 网络的一次性容器
#
# 更新器按安全设计跑在容器里（compose 只给 egress 网络；--network none 的演练里
# 连它都没有）。它自己打 `127.0.0.1:<宿主端口>/readyz` 永远拿不到 200（容器里的
# 环回不是宿主的）。所以宿主端口必须由**一次性 --network host 容器**探，镜像用
# 更新器自己的镜像；并且先 `docker inspect` 证明端口真的发布出去了。

HOST_PROBE_LOG = "host-probe-args "


def _flag_value(argv: list[str], flag: str) -> str:
    assert flag in argv, argv
    return argv[argv.index(flag) + 1]


def _host_probe_argv(ws: Workspace) -> list[str]:
    lines = [line for line in ws.calls() if line.startswith(HOST_PROBE_LOG)]
    assert lines, "没有出现宿主端口探针容器：\n" + "\n".join(ws.calls())
    return json.loads(lines[0][len(HOST_PROBE_LOG) :])


def test_host_readyz_probe_uses_updater_image_and_host_network(ws: Workspace) -> None:
    """容器里的更新器必须借 host 网络容器探宿主端口，镜像=更新器自己的镜像。"""
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    argv = _host_probe_argv(ws)
    # stub 记录的是 argv[1:]（"docker" 由 PATH 解析），首项即子命令。
    assert argv[:1] == ["run"], argv
    assert _flag_value(argv, "--network") == "host", argv
    assert "--pull=never" in argv, argv
    assert "--read-only" in argv, argv
    assert _flag_value(argv, "--tmpfs") == "/tmp", argv
    assert _flag_value(argv, "--entrypoint") == "python", argv
    # 镜像 = 更新器**自己的**镜像（从容器 ID 反查），不是新引入的镜像。
    assert argv[argv.index("-c") - 1] == UPDATER_IMAGE, argv
    # 最小权限：不加 privileged、不挂 docker socket。
    assert "--privileged" not in argv, argv
    assert not any("/var/run/docker.sock" in item for item in argv), argv
    code = argv[argv.index("-c") + 1]
    assert f"127.0.0.1:{ws.http.port}/readyz" in code, code
    assert "HTTPRedirectHandler" in code, code
    # 宿主 HTTP 只由探针容器发（UA 可区分）；更新器进程自己不建这个连接。
    readyz_lines = [line for line in ws.calls() if "http GET /readyz" in line]
    assert readyz_lines, ws.calls()
    assert all("ua=ling-host-readyz-probe/1" in line for line in readyz_lines), readyz_lines
    assert not [line for line in ws.calls() if "ua=ling-release-publish/1" in line]


def test_unpublished_host_port_in_updater_fails_immediately(ws: Workspace) -> None:
    """端口没发布 → 立即失败（维护门禁没解除），不进入 --ready-timeout 等待。"""
    write_request(ws)
    started = time.monotonic()
    result = run_updater(
        ws,
        env={"STUB_HOST_PORT_PUBLISHED": "0"},
        extra=["--ready-timeout", "180"],
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "维护门禁没解除" in result.stderr, result.stderr[-3000:]
    assert elapsed < 30, elapsed
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "switch_failed"
    assert task["failure"]["stage"] == "host_port"
    assert task["still_on_old_version"] is False
    assert task["maintenance"]["state"] == "active"
    # 一次探针都不许起。
    assert not [line for line in ws.calls() if line.startswith(HOST_PROBE_LOG)]


def test_host_readyz_probe_nonzero_keeps_maintenance_and_never_rolls_back(
    ws: Workspace,
) -> None:
    """探针容器返回非 0 → 已迁移后失败：保持维护门禁，不切回旧镜像。"""
    ws.http.readyz_status = 503
    write_request(ws)
    result = run_updater(ws)
    assert result.returncode == 16, (result.stdout, result.stderr)
    task = ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "switch_failed"
    assert task["failure"]["stage"] == "readyz"
    assert task["still_on_old_version"] is False
    assert task["maintenance"]["state"] == "active"
    assert task["rollback"]["attempted"] is False
    # 探针确实跑过（host 网络），且没有"切回旧镜像/恢复 dump"。
    argv = _host_probe_argv(ws)
    assert _flag_value(argv, "--network") == "host", argv
    assert not [
        line
        for line in ws.calls()
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]
    assert not calls_with(ws, "pg_restore")
    assert "最后 503" in result.stderr, result.stderr[-3000:]
