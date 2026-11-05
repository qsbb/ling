"""deploy/release/publish.py 的隔离回归测试（stub-only）。

这些用例在临时目录里真实执行发布脚本，只用 stub 替换 ``docker``，并用一个
假的 ``backup.sh`` 产物来覆盖 manifest 校验分支；不会真的拉镜像、起容器、
碰生产环境或执行真实备份。

**stub-only 的局限**：stub 自己也在近似解释 docker/compose 的 JSON 输出，
所以这里证明的是发布脚本的门禁顺序与失败语义，不是真实 Docker 的行为。
真实 Compose 的 ``config --format json``、``pull`` 平台解析、``run`` 迁移日志、
``up`` 后的容器 image ID 仍必须在隔离实例真机验证。
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
import yaml

from ling import __version__ as LING_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_PY = REPO_ROOT / "deploy" / "release" / "publish.py"

TARGET_HEX = "a" * 64
TARGET = f"ghcr.io/qsbb/ling-api@sha256:{TARGET_HEX}"
OLD_HEX = "c" * 64
OLD_REF = f"ghcr.io/qsbb/ling-api@sha256:{OLD_HEX}"
COMMIT = "b" * 40
OLD_COMMIT = "d" * 40
HOST_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().strip().lower(), platform.machine().strip().lower()
)
OTHER_ARCH = "arm64" if HOST_ARCH == "amd64" else "amd64"
HEAD_OLD = "0044_media_describe_failed"
HEAD_NEW = "0045_turn_control_attempt"
PLATFORM_DIGEST = "e" * 64
# 受限 env 里的根密钥（与真实值同形的可搜索哨兵；测试只断言"传到了"与"没回显"）。
ROOT_KEY = "rootkey-canary-4f9a2c7e-do-not-print"
ROTATED_ROOT_KEY = "rootkey-rotated-91bd0e55-do-not-print"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- docker stub

_DOCKER = r'''#!/usr/bin/env python3
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile


STATE_PATH = pathlib.Path(os.environ["STUB_STATE"])
CALL_LOG = pathlib.Path(os.environ["CALL_LOG"])


def log(message):
    with CALL_LOG.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def load_state():
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def image_for(ref, state):
    target = os.environ["STUB_TARGET"]
    old_ref = os.environ["STUB_OLD_REF"]
    if ref == target or ref == "sha256:target-image":
        return {
            "Id": "sha256:target-image",
            "RepoDigests": [target],
            "Architecture": os.environ.get("STUB_IMAGE_ARCH", "amd64"),
            "Os": "linux",
            "Size": 100 * 1024 * 1024,
            "Config": {
                "Env": ["LING_COMMIT=" + os.environ["STUB_COMMIT"]],
                "Labels": {
                    "org.opencontainers.image.revision": os.environ["STUB_COMMIT"],
                    "org.opencontainers.image.version": "9.9.9",
                },
            },
        }
    if ref == old_ref or ref == "sha256:old-image":
        return {
            "Id": "sha256:old-image",
            "RepoDigests": [old_ref],
            "Architecture": os.environ.get("STUB_HOST_ARCH", "amd64"),
            "Os": "linux",
            "Size": 90 * 1024 * 1024,
            "Config": {
                "Env": ["LING_COMMIT=" + os.environ["STUB_OLD_COMMIT"]],
                "Labels": {
                    "org.opencontainers.image.revision": os.environ["STUB_OLD_COMMIT"],
                },
            },
        }
    return None


def api_health_spec():
    """api 的健康状态脚本：``starting,starting,healthy`` 这类逗号序列。

    每做一次 ``docker inspect --type container c-api`` 前进一格，最后一格保持；
    计数落在 stub 状态文件里（每次 docker 调用都是新进程，不能放内存）。
    """
    return [item for item in os.environ.get("STUB_API_HEALTH", "healthy").split(",") if item]


def api_health_now(state):
    """不推进序列，只回答"api 现在是什么健康状态"（exec 探活用它模拟真实 Docker）。"""
    spec = api_health_spec()
    if spec == ["none"]:
        return None
    index = int(state.get("api_health_polls", 0))
    return spec[min(index, len(spec) - 1)]


def health_for(service, state, ref):
    """worker 沿用 STUB_WORKER_HEALTH；api 用可推进的 STUB_API_HEALTH 序列。

    只有**目标镜像**的 api 容器才推进序列：预检阶段 inspect 的是旧镜像，
    不能替新容器的 starting 轮次"吃掉"格子（否则真机场景会被压缩成一次）。
    """
    if service == "api":
        spec = api_health_spec()
        if spec == ["none"]:
            return None
        index = int(state.get("api_health_polls", 0))
        if index < len(spec) - 1 and ref == os.environ["STUB_TARGET"]:
            state["api_health_polls"] = index + 1
        return spec[min(index, len(spec) - 1)]
    health = os.environ.get("STUB_WORKER_HEALTH", "healthy")
    return None if health == "none" else health


def container_for(container_id, service, state):
    ref = state["container_image_ref"][service]
    image = image_for(ref, state)
    if image is None:
        return None
    container_state = {
        "Running": bool(state["running"][service]),
        "Status": "running" if state["running"][service] else "exited",
    }
    if service == "api" and not state["running"][service]:
        # 已退出的 api 没有可读的健康状态（不再喂给发布脚本一个假的 healthy）。
        health = None
        container_state["ExitCode"] = 1
    else:
        health = health_for(service, state, ref)
    if health is not None:
        container_state["Health"] = {"Status": health}
    return {
        "Id": container_id,
        "Image": state["container_image_id"][service],
        "Config": {"Image": ref},
        "State": container_state,
    }


def emit_json(value):
    print(json.dumps(value))


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


args = sys.argv[1:]
log("docker " + " ".join(args))

if not args:
    sys.exit(64)

if args[0] == "info":
    print(os.environ["STUB_DOCKER_ROOT"])
    sys.exit(0)

if args[0] == "manifest":
    if os.environ.get("STUB_MANIFEST_MODE") == "fail":
        sys.stderr.write("manifest unknown: denied\n")
        sys.exit(1)
    if os.environ.get("STUB_MANIFEST_MODE") == "no_host":
        print(json.dumps({"manifests": [
            {"digest": "sha256:" + "f" * 64,
             "platform": {"os": "linux",
                          "architecture": os.environ["STUB_OTHER_ARCH"]}},
        ]}))
        sys.exit(0)
    print(json.dumps({"manifests": [
        {"digest": "sha256:" + os.environ["STUB_PLATFORM_DIGEST"],
         "platform": {"os": "linux",
                      "architecture": os.environ["STUB_HOST_ARCH"]}},
        {"digest": "sha256:" + "f" * 64,
         "platform": {"os": "linux", "architecture": "arm64"}},
    ]}))
    sys.exit(0)

def host_readyz_probe(args):
    """模拟 `docker run --network <mode> ... --entrypoint python -c <readyz 探针>`。

    stub 的**关键语义**就是真 Docker 的关键语义：只有 ``--network host`` 的容器
    能看见宿主的 127.0.0.1:<端口>；``--network none`` / 默认 bridge 的容器有
    自己的网络命名空间，探容器自己的环回只会 Connection refused。所以 stub 在
    非 host 模式下**真的**把探针代码里的 socket.connect 弄成拒绝连接（而不是
    凭空断言），旧实现（进程自己 urlopen）与 ``--network none`` 变异都会红。
    探针代码本身照原样执行：http 请求真的会打到测试的 HTTP stub 上。
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
        # 非 host 网络：容器自己的环回上没有宿主端口。如实模拟成拒绝连接。
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
    if os.environ.get("STUB_HEAD_EXIT", "0") != "0":
        sys.stderr.write("alembic missing\n")
        sys.exit(int(os.environ["STUB_HEAD_EXIT"]))
    if os.environ.get("STUB_HEAD_MULTI") == "1":
        print("HEAD=one")
        print("HEAD=two")
    else:
        print("HEAD=" + os.environ["STUB_HEAD_NEW"])
    print("REV=" + os.environ["STUB_HEAD_OLD"])
    print("REV=" + os.environ["STUB_HEAD_NEW"])
    if os.environ.get("STUB_CHAIN_BROKEN") == "1":
        print("PARENT=" + os.environ["STUB_HEAD_NEW"] + ":9999_other_branch")
    else:
        print("PARENT=" + os.environ["STUB_HEAD_NEW"] + ":" + os.environ["STUB_HEAD_OLD"])
    sys.exit(0)

if args[0] == "tag":
    sys.exit(int(os.environ.get("STUB_TAG_EXIT", "0")))

if args[0] == "save":
    output = None
    index = 1
    while index < len(args):
        if args[index] == "--output" and index + 1 < len(args):
            output = pathlib.Path(args[index + 1])
            index += 2
            continue
        index += 1
    if output is None:
        sys.exit(64)
    if os.environ.get("STUB_SAVE_EXIT", "0") != "0":
        sys.exit(int(os.environ["STUB_SAVE_EXIT"]))
    if os.environ.get("STUB_SAVE_EMPTY") == "1":
        output.write_bytes(b"")
        sys.exit(0)
    if os.environ.get("STUB_SAVE_CORRUPT") == "1":
        output.write_bytes(b"not-a-tar")
        sys.exit(0)
    payload = b"[]"
    with tarfile.open(output, "w") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    sys.exit(0)

if args[0] == "ps":
    all_states = "-a" in args
    filters = parse_filters(args)
    service = None
    label = filters.get("label", "")
    if "com.docker.compose.service=" in label:
        service = label.split("com.docker.compose.service=", 1)[1]
    if "name" in filters:
        if os.environ.get("STUB_EDGE") == "1":
            print("edge-id|ling-edge-1")
        sys.exit(0)
    if "publish" in filters:
        owner = os.environ.get("STUB_PORT_OWNER")
        if owner:
            data = json.loads(owner)
            print("%s|%s" % (data["id"], data["project"]))
        elif load_state()["running"]["api"]:
            print("c-api|ling")
        sys.exit(0)
    if service == "edge":
        if os.environ.get("STUB_EDGE") == "1":
            print("edge-id|ling-edge-1|exited")
        sys.exit(0)
    if service in ("api", "worker"):
        state = load_state()
        if service == "api" and state.get("api_missing"):
            # 容器根本没被创建/已经消失：连 `ps -a` 也找不到。
            sys.exit(0)
        if state["running"][service]:
            print("c-%s|c-%s-name|running" % (service, service))
        elif all_states:
            print("c-%s|c-%s-name|exited" % (service, service))
        sys.exit(0)
    sys.exit(0)

if args[0] == "image" and len(args) > 1 and args[1] == "inspect":
    image = image_for(args[2], load_state()) if len(args) > 2 else None
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
        # 更新器/发布进程自己的镜像（Docker 把容器 hostname 设成容器 ID）。
        self_image = os.environ.get("STUB_SELF_IMAGE", "")
        if not self_image:
            sys.exit(1)
        print(self_image)
        sys.exit(0)
    if fmt is not None and "NetworkSettings.Ports" in fmt:
        # 宿主端口是否**真的**发布在运行中的 api 容器上（缺陷 7 的预检事实）。
        published = os.environ.get("STUB_HOST_PORT_PUBLISHED", "1") == "1"
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
                container = container_for("c-" + service, service, state)
                if container is None:
                    sys.exit(1)
                # 健康状态序列会推进计数，必须落盘（每次 docker 调用是新进程）。
                save_state(state)
                emit_json([container])
                sys.exit(0)
        sys.exit(1)
    if kind == "image":
        image = image_for(ref, state)
        if image is None:
            sys.exit(1)
        emit_json([image])
        sys.exit(0)
    sys.exit(64)

if args[0] == "volume" and len(args) > 1 and args[1] == "inspect":
    names = args[2:]
    project = os.environ.get("STUB_VOLUME_PROJECT", "ling")
    driver = os.environ.get("STUB_VOLUME_DRIVER", "local")
    emit_json([
        {"Name": name, "Driver": driver,
         "Labels": {"com.docker.compose.project": project}}
        for name in names
    ])
    sys.exit(0)

if args[0] != "compose":
    sys.exit(64)

# docker compose ...
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

state = load_state()
log("docker-env %s LING_APP_IMAGE=%s" % (subcommand, os.environ.get("LING_APP_IMAGE", "")))

if subcommand == "config":
    if os.environ.get("STUB_CONFIG_EXIT", "0") != "0":
        sys.stderr.write("compose config failed\n")
        sys.exit(int(os.environ["STUB_CONFIG_EXIT"]))
    sys.stdout.write(pathlib.Path(os.environ["STUB_CONFIG_JSON"]).read_text(encoding="utf-8"))
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
        if os.environ.get("STUB_WORKER_IMAGE_ID") and service == "worker":
            image_id = os.environ["STUB_WORKER_IMAGE_ID"]
        elif ref == os.environ["STUB_TARGET"]:
            image_id = "sha256:target-image"
        else:
            image_id = "sha256:old-image"
        state["container_image_ref"][service] = ref
        state["container_image_id"][service] = image_id
        if service == "api" and os.environ.get("STUB_HEAD_AFTER_API_START"):
            state["alembic_version"] = os.environ["STUB_HEAD_AFTER_API_START"]
        if service == "api" and os.environ.get("STUB_API_EXITED") == "1":
            # 真实 Docker：进程启动即退出，容器停在 exited（Health 不再前进）。
            state["running"]["api"] = False
        if service == "api" and os.environ.get("STUB_API_MISSING") == "1":
            # 容器根本没被创建出来（compose 报成功但对象不存在/已消失）。
            state["running"]["api"] = False
            state["api_missing"] = True
        save_state(state)
    sys.exit(int(os.environ.get("STUB_UP_EXIT", "0")))

if subcommand == "exec":
    service = None
    for item in subargs:
        if item in ("db", "api", "worker"):
            service = item
            break
    if service == "db":
        print(state["alembic_version"])
        sys.exit(0)
    if service == "api":
        if any("web_manifest.py" in item for item in subargs):
            sys.exit(int(os.environ.get("STUB_WEB_MANIFEST_EXIT", "0")))
        if not state["running"]["api"]:
            # 真实 compose：容器不在 running 时 exec 直接报错。
            sys.stderr.write('service "api" is not running\n')
            sys.exit(1)
        if any("__version__" in str(item) for item in subargs):
            # 容器内**进程证据**探活（publish.CONTAINER_VERSION_PROBE）。
            # 提交号来自**镜像** ENV（真实 Dockerfile 的 LING_COMMIT 就是构建时
            # 写进镜像的，与 OCI 标注 revision 同源），不是发布脚本传进去的
            # 变量：脚本若改成"自己传 commit 再读回来"，这里就对不上。
            image = image_for(state["container_image_ref"]["api"], state) or {}
            image_env = {}
            for entry in (image.get("Config") or {}).get("Env") or []:
                if "=" in entry:
                    key, value = entry.split("=", 1)
                    image_env[key] = value
            code = int(os.environ.get("STUB_CONTAINER_VERSION_EXIT", "0"))
            if code != 0:
                sys.stderr.write("ModuleNotFoundError: No module named 'ling'\n")
                sys.exit(code)
            print(json.dumps({
                "version": "9.9.9",
                "commit": os.environ.get("STUB_CONTAINER_COMMIT", image_env.get("LING_COMMIT", "")),
            }))
            sys.exit(0)
        # 真实 Docker 里"应用还没 bind 端口"的表现：容器仍 starting，容器内
        # 127.0.0.1:6520 拒绝连接。发布脚本若不等 healthy 就探活，会看到它。
        if api_health_now(state) == "starting":
            sys.stderr.write(
                'urllib.error.URLError: <urlopen error [Errno 111] Connection refused>\n'
            )
            sys.exit(1)
        script = [item for item in os.environ.get("STUB_CONTAINER_READYZ_SCRIPT", "").split(",") if item]
        if script:
            calls = int(state.get("readyz_exec_calls", 0))
            state["readyz_exec_calls"] = calls + 1
            save_state(state)
            code = int(script[min(calls, len(script) - 1)])
        else:
            code = int(os.environ.get("STUB_CONTAINER_READYZ_EXIT", "0"))
        if code != 0:
            sys.stderr.write(
                'urllib.error.URLError: <urlopen error [Errno 111] Connection refused>\n'
            )
        sys.exit(code)
    sys.exit(1)

if subcommand == "run":
    log("migration-run " + " ".join(subargs))
    if os.environ.get("STUB_MIGRATION_LOG_BOTH") == "1":
        print("[entrypoint] 执行迁移")
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
        state["alembic_version"] = os.environ["STUB_HEAD_NEW"]
        save_state(state)
    elif os.environ.get("STUB_MIGRATION_LOG_BOTH_STALE") == "1":
        # 同时出现"执行迁移"和"跳过迁移"，但数据库版本没前进：这样只有
        # skip 门禁能判失败；删掉 skip 门禁会落到随后 executed/head 门禁，
        # failure.step 就不是 migrate_skipped 了。
        print("[entrypoint] 执行迁移")
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
    elif os.environ.get("STUB_MIGRATION_SKIP") == "1":
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
    else:
        print("[entrypoint] 执行迁移")
        print("[migrate] alembic upgrade head 完成")
        if "STUB_MIGRATE_SET_VERSION" in os.environ:
            new_version = os.environ["STUB_MIGRATE_SET_VERSION"]
        else:
            new_version = os.environ["STUB_HEAD_NEW"]
        if new_version:
            state["alembic_version"] = new_version
            save_state(state)
    sys.exit(int(os.environ.get("STUB_MIGRATE_EXIT", "0")))

sys.exit(64)
'''

# ---------------------------------------------------------------- HTTP stub


class _HTTPState:
    def __init__(self) -> None:
        self.readyz_status = 200
        # 打开后 /readyz 返回 302 → /readyz-ok（那里是 200）。用来证明发布脚本
        # 不跟随重定向、也不把 302→200 洗成"就绪"。
        self.readyz_redirect = False
        # 发布脚本打过的 HTTP 路径（按到达顺序，含状态码），用来证明它只探
        # **免鉴权**端点。这里以前有 `version_status/version_commit`：stub 对
        # `/v1/version` 无条件回 200，而真实应用对该端点挂了 `CurrentAuth`，
        # 未鉴权永远 401 —— 那正是 P5 第 6 个缺陷（测试一直绿、真机必红）。
        self.requests: list[str] = []
        # 每个请求的 User-Agent（按到达顺序）。宿主 /readyz 必须由**一次性
        # host 网络容器**里的探针发出（缺陷 7）；发布进程自己 urlopen 会带
        # `ling-release-publish/1`，这里据此区分"谁在碰宿主 HTTP"。
        self.user_agents: list[str] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _HTTPServer

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.http_state
        state.requests.append(self.path)
        state.user_agents.append(str(self.headers.get("User-Agent") or ""))
        if self.path == "/readyz" and state.readyz_redirect:
            self.send_response(302)
            self.send_header("Location", "/readyz-ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/readyz", "/readyz-ok"):
            status = state.readyz_status
            body = b"ok" if status == 200 else b"not ready"
        elif self.path.startswith("/v1/"):
            # **如实模仿真实应用**：`/v1/version`（以及其它 /v1 只读端点）挂了
            # `CurrentAuth`，没有会话就是 401、body 为空。stub 以前在这里回 200，
            # 于是"发布脚本拿 401"这件事在单元测试里永远抓不到。
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


# ---------------------------------------------------------------- workspace


@dataclass
class Workspace:
    root: Path
    compose_file: Path
    env_file: Path
    backup_dir: Path
    backup_script: Path
    record_path: Path
    call_log: Path
    state_path: Path
    config_path: Path
    env: dict[str, str]
    http: Any

    def reset(self) -> None:
        self.call_log.write_text("", encoding="utf-8")

    def calls(self) -> list[str]:
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def record(self) -> dict[str, Any]:
        return json.loads(self.record_path.read_text(encoding="utf-8"))


_FAKE_BACKUP = r'''#!/usr/bin/env bash
set -euo pipefail
# **备份前必须已经没有写者**：stub 直接读 docker 替身的状态文件（容器实况），
# 而不是只看调用顺序。旧顺序「停 worker → 备份 → 停 api」会让这里以 9 退出，
# 于是"备份看起来成功、实际带着运行中的 api"不可能通过。
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
printf 'backup-env LING_COMPOSE_PROJECT=%s LING_COMPOSE_FILE=%s LING_COMPOSE_OVERRIDE=%s LING_COMPOSE_ENV_FILE=%s LING_APP_IMAGE=%s LING_BACKUP_APP_IMAGE=%s\n' \
  "${LING_COMPOSE_PROJECT:-}" "${LING_COMPOSE_FILE:-}" "${LING_COMPOSE_OVERRIDE:-}" \
  "${LING_COMPOSE_ENV_FILE:-}" "${LING_APP_IMAGE:-}" "${LING_BACKUP_APP_IMAGE:-}" >> "${CALL_LOG:?}"
# 根密钥：只记"有没有设置"与值哈希。测试要证明值确实传进来了，但**不**把明文
# 写进任何日志——与生产代码"绝不回显"的口径一致。
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
if [ "$mode" = "tamper_env" ]; then
  # 在预检之后、update_env_file 之前篡改受限 env：发布脚本必须用预检时的
  # env_sha256_before 抓住它，并以"已迁移后失败"退出，而不是把篡改后的配置
  # 换成目标 digest 继续启动。
  printf 'TAMPERED_AFTER_PREFLIGHT=1\n' >> "${LING_COMPOSE_ENV_FILE:?}"
  printf 'backup-env-tampered\n' >> "${CALL_LOG:?}"
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
if [ "$mode" = "bad_dump_sha" ]; then dump_sha="$(printf '0%.0s' $(seq 1 64))"; fi
keys_root="present"
keys_root_sha="$key_sha"
keys_env_set="false"
media_state="present"
if [ "$mode" = "keys_absent" ]; then
  keys_root="absent"
  keys_root_sha="-"
fi
if [ "$mode" = "media_partial" ]; then media_state="partial"; fi
{
  echo "created_at=2026-01-01T00:00:00Z"
  echo "mode=compose"
  echo "dump=$dump"
  if [ "$mode" != "missing_dump_sha" ]; then echo "dump_sha256=$dump_sha"; fi
  echo "tombstones=tombstones-$stamp.txt"
  echo "tombstones_state=ok"
  echo "tombstones_sha256=$tomb_sha"
  echo "keys_root=$keys_root"
  echo "keys_root_file=keys/root.key"
  echo "keys_root_source=file"
  echo "keys_root_sha256=$keys_root_sha"
  echo "keys_env_set=$keys_env_set"
  echo "media=media-$stamp.tar"
  echo "media_state=$media_state"
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
    api_env = {
        "LING_ENV": "production",
        "LING_RUN_MIGRATIONS": "0",
        "LING_UPDATE_ENABLED": "0",
        "LING_PORT": "6520",
    }
    worker_env = {"LING_ENV": "production"}
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
                "environment": api_env,
                "ports": [
                    {
                        "mode": "ingress",
                        "target": 6520,
                        "published": str(port),
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {"type": "volume", "source": "ling_app-data", "target": "/data"}
                ],
            },
            "worker": {
                "image": TARGET,
                "environment": worker_env,
                # 镜像模式要求 worker 必须声明 healthcheck：switch 阶段用 Docker
                # 的 Health 状态等待 worker，而不是只看容器进程起来。
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import asyncpg",
                    ],
                    "interval": "30s",
                    "timeout": "8s",
                    "retries": 3,
                },
                "volumes": [
                    {"type": "volume", "source": "ling_app-data", "target": "/data"}
                ],
            },
        },
        "volumes": {
            "db-data": {"name": "ling_db-data"},
            "app-data": {"name": "ling_app-data"},
        },
    }


# ------------------------------------------- 真实 compose 文件解析出的预检模型
#
# 为什么这些用例**必须**读仓库里真实的 `deploy/compose.image.yaml`：
# P5 接线（9334c6f）后 api 多出两条 `/run/ling-update*` 宿主目录挂载，而
# `_config_json`（自造 stub 模型）里没有它们——「api/worker 的每一个挂载都必须
# 是命名卷」的旧规则因此在单测里永远绿、在真机上必红
# （`failed_preflight` / `compose_mounts`，exit 10）。
#
# 这里按 `docker compose config --format json` 的规则从真实文件解析：短语法 →
# 长语法、`${VAR:?}` / `${VAR:-默认}` → 实际值、命名卷加项目前缀。于是 compose
# 文件里改一条挂载（目标、模式、少挂一条），这些用例就跟着动。
REAL_IMAGE_COMPOSE = REPO_ROOT / "deploy" / "compose.image.yaml"

# 模拟宿主受限 `.env`（`ling install` 写入的那几项）。只用于解析真实 compose。
REAL_COMPOSE_ENV = {
    "POSTGRES_PASSWORD": "pw",
    "LING_UPDATE_REQUESTS_HOST": "/srv/ling/update/requests",
    "LING_UPDATE_STATUS_HOST": "/srv/ling/update/status",
    "LING_UPDATE_SHARED_GID": "10002",
}

_INTERPOLATION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-|:\?)([^}]*))?\}")


def _interpolate(text: str, values: dict[str, str]) -> str:
    """Compose 插值的最小实现（只覆盖本仓库用到的 `:?` / `:-` / 裸 `${VAR}`）。"""

    def replace(match: re.Match[str]) -> str:
        name, operator, default = match.group(1), match.group(2), match.group(3)
        if operator in (":?", "?") and not values.get(name):
            raise AssertionError(f"测试 env 缺少必填变量 {name}（compose 会直接失败）")
        if operator in (":-", "-"):
            return values.get(name) or default
        return values[name]

    return _INTERPOLATION_RE.sub(replace, text)


def _split_outside_braces(text: str) -> list[str]:
    """按 `:` 切分，但**不**切入 `${...}`（占位里的错误信息可能带冒号/中文）。"""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        if text.startswith("${", index):
            depth += 1
            buffer.append("${")
            index += 2
            continue
        char = text[index]
        if char == "}" and depth:
            depth -= 1
        if char == ":" and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def _long_syntax_mount(entry: str, values: dict[str, str], project: str) -> dict[str, Any]:
    """短语法 `SOURCE:TARGET[:MODE]` → `docker compose config` 给的长语法。"""
    parts = _split_outside_braces(entry.strip())
    assert len(parts) >= 2, entry
    source, target = parts[0].strip(), parts[1].strip()
    options = (
        {item.strip().lower() for item in parts[2].split(",")} if len(parts) > 2 else set()
    )
    resolved = _interpolate(source, values)
    if resolved.startswith(("/", ".", "~")):
        return {
            "type": "bind",
            "source": resolved,
            "target": target,
            "read_only": "ro" in options,
            "bind": {"create_host_path": False},
        }
    return {
        "type": "volume",
        "source": f"{project}_{resolved}",
        "target": target,
        "read_only": "ro" in options,
    }


def _long_syntax_port(entry: str, values: dict[str, str]) -> dict[str, Any]:
    """短语法 `[HOST:]TARGET` → 长语法（`docker compose config` 的形态）。"""
    parts = _split_outside_braces(_interpolate(entry.strip(), values))
    if len(parts) == 1:
        return {"mode": "ingress", "target": int(parts[0])}
    return {
        "mode": "ingress",
        "target": int(parts[1]),
        "published": parts[0],
        "protocol": "tcp",
    }


def _real_image_compose_model(published_port: int) -> dict[str, Any]:
    """把**真实的** `deploy/compose.image.yaml` 解析成预检吃的那份模型。

    `published_port` 替换 `${LING_PORT}`（宿主发布端口；e2e 用例里必须是假 API
    占着的那个端口）。
    """
    data = yaml.safe_load(REAL_IMAGE_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    project = str(data.get("name") or "ling")
    values = dict(REAL_COMPOSE_ENV)
    values["LING_APP_IMAGE"] = TARGET
    values["LING_PORT"] = str(published_port)
    services: dict[str, Any] = {}
    for name, service in (data.get("services") or {}).items():
        built: dict[str, Any] = {}
        for key, value in service.items():
            if key == "volumes":
                built[key] = [_long_syntax_mount(entry, values, project) for entry in value]
            elif key == "ports":
                built[key] = [_long_syntax_port(entry, values) for entry in value]
            elif key == "environment":
                built[key] = {
                    str(env_key): _interpolate(str(env_value), values)
                    for env_key, env_value in value.items()
                }
            elif key == "image":
                built[key] = _interpolate(str(value), values)
            else:
                built[key] = value
        services[name] = built
    volumes = {key: {"name": f"{project}_{key}"} for key in (data.get("volumes") or {})}
    return {"name": project, "services": services, "volumes": volumes}


def _run_compose_mounts_preflight(publish: Any, model: dict[str, Any]) -> None:
    """只跑预检里的 compose 模型门禁（`Context.check_compose_model`）。

    不经 `run_publish`：这条用例要钉的就是挂载规则本身，直接调门禁函数才能让
    变异（删掉某一条判定）立刻变成 FAIL，而不被别的门禁或后续步骤掩盖。
    """
    options = publish.Options(
        target=TARGET,
        target_hex=TARGET_HEX,
        compose_file=REAL_IMAGE_COMPOSE,
        compose_override=None,
        env_file=REPO_ROOT / "deploy" / ".env",
        project=str(model.get("name") or "ling"),
        backup_dir=Path("/nonexistent/ling-backups"),
        backup_script=Path("/nonexistent/backup.sh"),
        record_path=Path("/nonexistent/release.json"),
        snapshot_dir=Path("/nonexistent/release-test"),
        source_commit=None,
        ready_timeout=2.0,
        health_timeout=2.0,
        migrate_timeout=10.0,
        backup_timeout=10.0,
    )
    context = publish.Context(options)
    context.config = model
    context.check_compose_model()


def _mount_with_target(model: dict[str, Any], service: str, target: str) -> dict[str, Any]:
    for mount in model["services"][service]["volumes"]:
        if str(mount.get("target")) == target:
            return mount
    raise AssertionError(
        f"{service} 没有挂载 {target}：{model['services'][service]['volumes']}"
    )


@pytest.fixture()
def ws(tmp_path: Path, http_state: Any) -> Workspace:
    root = tmp_path / "repo"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    compose_file = deploy / "compose.image.yaml"
    compose_file.write_text("name: ling\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    env_file = deploy / ".env"
    env_file.write_text(
        f"POSTGRES_PASSWORD=pw\nLING_APP_IMAGE={OLD_REF}\nLING_ROOT_KEY={ROOT_KEY}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    backup_script = tmp_path / "fake-backup.sh"
    _write_executable(backup_script, _FAKE_BACKUP, python=False)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", _DOCKER, python=True)

    call_log = tmp_path / "calls.log"
    call_log.write_text("", encoding="utf-8")
    state_path = tmp_path / "state.json"
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
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config_json(http_state.port)), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifests": [
                    {
                        "digest": "sha256:" + PLATFORM_DIGEST,
                        "platform": {"os": "linux", "architecture": HOST_ARCH},
                    },
                    {
                        "digest": "sha256:" + "f" * 64,
                        "platform": {"os": "linux", "architecture": OTHER_ARCH},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir()

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("LING_", "COMPOSE_", "DOCKER_")):
            env.pop(key, None)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    # 宿主上直接跑 publish.py：hostname 不是容器 ID，探针镜像回退到目标镜像。
    env.pop("HOSTNAME", None)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CALL_LOG": str(call_log),
            "STUB_STATE": str(state_path),
            "STUB_CONFIG_JSON": str(config_path),
            "STUB_MANIFEST_JSON": str(manifest_path),
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
            "STUB_IMAGE_ARCH": HOST_ARCH,
            "STUB_HOST_PORT": str(http_state.port),
        }
    )
    return Workspace(
        root=root,
        compose_file=compose_file,
        env_file=env_file,
        backup_dir=backup_dir,
        backup_script=backup_script,
        record_path=backup_dir / "records" / "release.json",
        call_log=call_log,
        state_path=state_path,
        config_path=config_path,
        env=env,
        http=http_state,
    )


def run_publish(
    ws: Workspace,
    *,
    digest: str = TARGET,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    ws.reset()
    ws.http.requests.clear()
    ws.http.user_agents.clear()
    argv = [
        sys.executable,
        str(PUBLISH_PY),
        "--digest",
        digest,
        "--compose-file",
        str(ws.compose_file),
        "--env-file",
        str(ws.env_file),
        "--backup-dir",
        str(ws.backup_dir),
        "--backup-script",
        str(ws.backup_script),
        "--record",
        str(ws.record_path),
        "--source-commit",
        COMMIT,
        "--ready-timeout",
        "2",
        "--health-timeout",
        "2",
        "--migrate-timeout",
        "10",
        "--backup-timeout",
        "10",
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


def call_lines(ws: Workspace) -> list[str]:
    return ws.calls()


def compose_calls(ws: Workspace) -> list[str]:
    return [line for line in call_lines(ws) if line.startswith("docker compose")]


def assert_no_changes(ws: Workspace) -> None:
    calls = compose_calls(ws)
    assert not [line for line in calls if " pull " in line], calls
    assert not [line for line in calls if " run " in line], calls
    assert not [line for line in calls if " up " in line], calls
    assert not [line for line in calls if " stop " in line], calls
    assert not [line for line in call_lines(ws) if line.startswith("backup ")], calls


# ---------------------------------------------------------------- 单元：宿主端口解析


def _publish_module() -> Any:
    spec = importlib.util.spec_from_file_location("ling_publish_under_test", PUBLISH_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_service_published_ports_covers_every_service_of_merged_model() -> None:
    """预检取宿主端口必须看**合并后模型的每个服务**，不是只看 api 的 6520。

    Compose 的 override 是**追加**语义：主文件里 api 只有 ``${LING_PORT}:6520``，
    WS 端口只出现在 ``compose.image.ws.example.yaml`` 合并后的结果里。只遍历
    api、或只读主文件，这里就会漏掉 6799——那正是缺口 1 的回退形态。
    """
    publish = _publish_module()
    services = {
        "db": {"ports": [{"target": 5432, "published": "15432", "protocol": "tcp"}]},
        "api": {
            "ports": [
                {"mode": "ingress", "target": 6520, "published": "16520", "protocol": "tcp"},
                # override 追加进来的 WS 端口（追加语义，6520 仍在）。
                {"mode": "ingress", "target": 6799, "published": "6799", "protocol": "tcp"},
            ]
        },
        "worker": {"ports": [{"target": 9100, "published": 19100, "protocol": "tcp"}]},
    }
    assert [
        (item["service"], item["target"], item["published"])
        for item in publish.service_published_ports(services)
    ] == [
        ("db", 5432, 15432),
        ("api", 6520, 16520),
        ("api", 6799, 6799),
        ("worker", 9100, 19100),
    ]
    # 只写容器侧（没有 published）不是宿主发布；由 Docker 随机分配也不核对。
    assert publish.service_published_ports({"api": {"ports": [{"target": 6520}]}}) == []
    # 范围展开：逐个核对，不把 8000-8002 当成一个条目跳过。
    assert [
        item["published"]
        for item in publish.service_published_ports(
            {"api": {"ports": [{"target": 8000, "published": "8000-8002"}]}}
        )
    ] == [8000, 8001, 8002]
    # 解析不了必须报错（预检失败），不能静默当成"没有端口"。
    with pytest.raises(publish.PublishError) as excinfo:
        publish.service_published_ports(
            {"api": {"ports": [{"target": 9000, "published": "not-a-port"}]}}
        )
    assert excinfo.value.exit_code == publish.EXIT_PREFLIGHT
    assert excinfo.value.step == "compose_ports"


# ---------------------------------------------------------------- 参数门禁


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
def test_only_digest_is_accepted_without_docker(ws: Workspace, digest: str) -> None:
    result = run_publish(ws, digest=digest)
    assert result.returncode == 2, result.stderr
    assert "digest" in result.stderr.lower()
    assert compose_calls(ws) == []
    assert not (ws.backup_dir / ".backup.lock").exists()
    assert not ws.record_path.exists()


# ---------------------------------------------------------------- 预检门禁


@pytest.mark.parametrize(
    ("case", "env", "needle"),
    [
        (
            "disk",
            {"LING_RELEASE_BACKUP_BUDGET_BYTES": str(10**15)},
            "磁盘余量不足",
        ),
        (
            "port",
            {
                "STUB_PORT_OWNER": json.dumps(
                    {"id": "other-id", "project": "other", "name": "other-api"}
                )
            },
            "被其他进程占用",
        ),
        ("volume", {"STUB_VOLUME_PROJECT": "other"}, "卷身份不一致"),
        ("edge", {"STUB_EDGE": "1"}, "旧 edge 容器残留"),
        ("registry", {"STUB_MANIFEST_MODE": "fail"}, "GHCR"),
    ],
)
def test_preflight_failures_happen_before_backup(
    ws: Workspace, case: str, env: dict[str, str], needle: str
) -> None:
    result = run_publish(ws, env=env)
    assert result.returncode == 10, (case, result.stdout, result.stderr)
    assert needle in result.stderr, (case, result.stderr)
    assert_no_changes(ws)
    record = ws.record()
    assert record["status"] == "failed_preflight"
    assert record["failure"]["step"] in {
        "disk",
        "port",
        "volumes",
        "edge",
        "registry",
        "registry_platform",
    }
    assert record["rollback"]["attempted"] is False
    # 端口被别的项目占用时，127.0.0.1 的 200 不能算“旧服务正常”；
    # 其余预检失败都应在失败处理里补做实测并确认旧服务未变。
    assert record["rollback"]["verified"] is (case != "port")


def test_platform_mismatch_fails_preflight(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_MANIFEST_MODE": "no_host"})
    assert result.returncode == 10, result.stderr
    assert f"没有 linux/{HOST_ARCH}" in result.stderr
    assert_no_changes(ws)


def test_api_worker_digest_mismatch_fails_before_backup(ws: Workspace) -> None:
    config = _config_json(ws.http.port)
    config["services"]["worker"]["image"] = OLD_REF
    ws.config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_publish(ws)
    assert result.returncode == 10, result.stderr
    assert "api/worker 镜像不是同一个目标 digest" in result.stderr
    assert_no_changes(ws)


@pytest.mark.parametrize(
    ("case", "healthcheck"),
    [
        ("missing", None),
        ("disabled", {"disable": True}),
        ("empty_test", {"test": []}),
    ],
)
def test_worker_without_healthcheck_fails_preflight(
    ws: Workspace, case: str, healthcheck: dict[str, Any] | None
) -> None:
    # worker 的 healthcheck 是 switch 阶段唯一的"可用"证据。删掉它时脚本必须
    # 在预检失败，而不是让 wait_worker_health 静默 return 把"进程起来"当健康。
    config = _config_json(ws.http.port)
    if healthcheck is None:
        config["services"]["worker"].pop("healthcheck", None)
    else:
        config["services"]["worker"]["healthcheck"] = healthcheck
    ws.config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_publish(ws)
    assert result.returncode == 10, (case, result.stdout, result.stderr)
    assert "worker 没有可用的 healthcheck" in result.stderr, (case, result.stderr)
    assert_no_changes(ws)
    record = ws.record()
    assert record["status"] == "failed_preflight"
    assert record["failure"]["step"] == "compose_healthcheck"


# ------------------------------------------ 预检：api 开关（compose_env 门禁）
#
# 历史：`check_compose_model` 曾要求 `LING_UPDATE_ENABLED` 必须是 `"0"`（P2 人工
# 发布脚本的"本阶段不允许 Web 一键更新"），而网页更新又要求它是 `"1"` 才收请求。
# 两边互锁，功能从来走不通（生产实测：201 之后 failed_not_started / compose_env）。
# 并发已由「updater.lock 单飞 + 跨工具 .ling-install.lock」两把锁解决，所以那条
# 断言删掉；`LING_RUN_MIGRATIONS == "0"` **保留**（迁移必须是发布流程里的一次显式
# 动作），观察到的两个开关值写进发布记录供事后审计。


def _compose_env_preflight(publish: Any, model: dict[str, Any]) -> Any:
    """只跑 compose 模型门禁并返回 context（为了读它写下的发布记录）。

    不走 `run_publish`：开关门禁要能独立于其它预检失败被观察，直接调门禁函数才能
    让变异（把断言加回去 / 删掉迁移断言 / 不写记录）各自只红对应用例。
    """
    options = publish.Options(
        target=TARGET,
        target_hex=TARGET_HEX,
        compose_file=REAL_IMAGE_COMPOSE,
        compose_override=None,
        env_file=REPO_ROOT / "deploy" / ".env",
        project=str(model.get("name") or "ling"),
        backup_dir=Path("/nonexistent/ling-backups"),
        backup_script=Path("/nonexistent/backup.sh"),
        record_path=Path("/nonexistent/release.json"),
        snapshot_dir=Path("/nonexistent/release-test"),
        source_commit=None,
        ready_timeout=2.0,
        health_timeout=2.0,
        migrate_timeout=10.0,
        backup_timeout=10.0,
    )
    context = publish.Context(options)
    context.config = model
    context.check_compose_model()
    return context


def _model_with_api_env(**overrides: Any) -> dict[str, Any]:
    """真实 compose 模型 + 覆盖 api 的开关值（`None` = 删掉该键）。"""
    model = _real_image_compose_model(16520)
    env = model["services"]["api"]["environment"]
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return model


@pytest.mark.parametrize(
    ("switch", "expected"),
    [
        ("1", "1"),  # 网页一键更新开着：更新器接手后必须能过预检（原来这里必红）
        ("0", "0"),  # 关着：老路径不能因为删断言一起坏掉
        (None, None),  # 模型里干脆没有这个键：不是失败条件，记录为 null
    ],
)
def test_update_enabled_switch_is_not_a_preflight_gate(
    switch: str | None, expected: str | None
) -> None:
    publish = _publish_module()
    context = _compose_env_preflight(
        publish, _model_with_api_env(LING_UPDATE_ENABLED=switch)
    )
    # 门禁跑完了（没抛）：开关只是被观察到，不是失败条件。
    assert context.record["compose_env"]["LING_UPDATE_ENABLED"] == expected
    assert context.record["compose_env"]["LING_RUN_MIGRATIONS"] == "0"


def test_run_migrations_must_stay_zero_even_when_switch_is_on() -> None:
    """`LING_RUN_MIGRATIONS` 的断言不能跟着开关那条一起被删掉。

    api 启动时隐式迁移会让"迁移有没有发生"失去证据；stage 必须是 `compose_env`。
    """
    publish = _publish_module()
    with pytest.raises(publish.PublishError) as excinfo:
        _compose_env_preflight(
            publish,
            _model_with_api_env(LING_UPDATE_ENABLED="1", LING_RUN_MIGRATIONS="1"),
        )
    assert excinfo.value.exit_code == publish.EXIT_PREFLIGHT
    assert excinfo.value.status == "failed_preflight"
    assert excinfo.value.step == "compose_env"
    assert "LING_RUN_MIGRATIONS" in excinfo.value.message


@pytest.mark.parametrize("switch", ["0", "1"])
def test_publish_record_keeps_observed_update_switch(ws: Workspace, switch: str) -> None:
    """发布记录里必须有**观察到的**开关值（切到 0 或 1 都要能事后审计）。

    走真实入口 `run_publish`：记录是脚本自己写的 JSON，不是内存里的字典。
    """
    config = _config_json(ws.http.port)
    config["services"]["api"]["environment"]["LING_UPDATE_ENABLED"] = switch
    ws.config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["compose_env"] == {
        "LING_UPDATE_ENABLED": switch,
        "LING_RUN_MIGRATIONS": "0",
    }


def test_update_switch_on_survives_the_whole_publish_run(ws: Workspace) -> None:
    """开关=1 时**整条发布链**都要走通，不能只在预检里放行。

    原来这条会停在 exit 10 / `failed_preflight` / `compose_env`——这正是生产里
    「网页点更新 → 201 → 更新器 failed_not_started」的最后一环。
    """
    config = _config_json(ws.http.port)
    config["services"]["api"]["environment"]["LING_UPDATE_ENABLED"] = "1"
    ws.config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "success"
    assert record["failure"] is None
    assert record["compose_env"]["LING_UPDATE_ENABLED"] == "1"
    # 迁移仍然只有那一次显式动作（开关=1 没有让 api 变成"启动即迁移"）。
    migrate_calls = [line for line in compose_calls(ws) if " run " in line]
    assert len(migrate_calls) == 1, migrate_calls
    assert "LING_RUN_MIGRATIONS=1" in migrate_calls[0]


# ------------------------------- 预检：更新通道挂载白名单（真实 compose 文件）


def test_real_image_compose_passes_compose_mounts_gate() -> None:
    """直接回归：仓库里**真实的** `deploy/compose.image.yaml` 必须过预检挂载门禁。

    真机演练的失败形态是 `preflight_failed` / `compose_mounts`（exit 10）：发布
    脚本拒绝了它自己要部署的那份 compose。stub 模型（`_config_json`）里没有
    P5 接线加上的两条 `/run/ling-update*` 挂载，所以这条只能用真实文件。
    """
    publish = _publish_module()
    model = _real_image_compose_model(16520)

    # 先证明模型里**确实**有那两条 bind：否则"解析器把挂载丢了"会让本用例退化成
    # "什么都没检查也绿"，正是这个 bug 的原始成因。
    api_mounts = {
        str(mount.get("target")): mount for mount in model["services"]["api"]["volumes"]
    }
    requests_mount = api_mounts["/run/ling-update/requests"]
    status_mount = api_mounts["/run/ling-update/status"]
    assert requests_mount["type"] == "bind"
    assert requests_mount["source"] == REAL_COMPOSE_ENV["LING_UPDATE_REQUESTS_HOST"]
    assert requests_mount["read_only"] is False
    assert status_mount["type"] == "bind"
    assert status_mount["source"] == REAL_COMPOSE_ENV["LING_UPDATE_STATUS_HOST"]
    assert status_mount["read_only"] is True
    assert not [
        mount
        for mount in model["services"]["worker"]["volumes"]
        if str(mount.get("target", "")).startswith("/run/ling-update")
    ]

    _run_compose_mounts_preflight(publish, model)


def _case_requests_read_only(model: dict[str, Any]) -> None:
    _mount_with_target(model, "api", "/run/ling-update/requests")["read_only"] = True


def _case_status_writable(model: dict[str, Any]) -> None:
    _mount_with_target(model, "api", "/run/ling-update/status")["read_only"] = False


def _case_worker_update_mount(model: dict[str, Any]) -> None:
    model["services"]["worker"]["volumes"].append(
        {
            "type": "bind",
            "source": REAL_COMPOSE_ENV["LING_UPDATE_REQUESTS_HOST"],
            "target": "/run/ling-update/requests",
            "read_only": False,
        }
    )


def _case_api_web_bind(model: dict[str, Any]) -> None:
    model["services"]["api"]["volumes"].append(
        {
            "type": "bind",
            "source": "/srv/ling/web",
            "target": "/app/web",
            "read_only": True,
        }
    )


def _case_api_docker_socket(model: dict[str, Any]) -> None:
    model["services"]["api"]["volumes"].append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
            "read_only": True,
        }
    )


def _case_api_update_extra_dir(model: dict[str, Any]) -> None:
    """白名单是**精确**的：多挂一个 `/run/ling-update/archive` 也不行。"""
    model["services"]["api"]["volumes"].append(
        {
            "type": "bind",
            "source": "/srv/ling/update/archive",
            "target": "/run/ling-update/archive",
            "read_only": False,
        }
    )


def _case_api_update_named_volume(model: dict[str, Any]) -> None:
    """更新通道必须挂宿主目录（bind）：换成命名卷等于换一份数据面，不许。"""
    model["services"]["api"]["volumes"].append(
        {
            "type": "volume",
            "source": "ling_update-status",
            "target": "/run/ling-update/status",
            "read_only": True,
        }
    )


@pytest.mark.parametrize(
    ("case", "mutate", "needle"),
    [
        (
            "requests_ro",
            _case_requests_read_only,
            "/run/ling-update/requests 必须以 rw 挂载",
        ),
        (
            "status_rw",
            _case_status_writable,
            "/run/ling-update/status 必须以 ro 挂载",
        ),
        (
            "worker_update_mount",
            _case_worker_update_mount,
            "更新通道路径只允许 api 挂",
        ),
        ("api_web_bind", _case_api_web_bind, "Web 构建产物已烤进镜像"),
        ("api_docker_socket", _case_api_docker_socket, "Docker socket"),
        ("api_update_extra_dir", _case_api_update_extra_dir, "api 只允许挂"),
        (
            "api_update_named_volume",
            _case_api_update_named_volume,
            "必须挂宿主目录（bind）",
        ),
    ],
)
def test_real_image_compose_mount_violations_fail_preflight(
    case: str, mutate: Any, needle: str
) -> None:
    """真实模型上每一条越权挂载都必须被预检拒绝，并指名"哪条、为什么不许"。

    白名单是**精确**允许：requests 只能 rw、status 只能 ro、worker 一条都不许挂、
    `/app/web` 与 `docker.sock` 照旧拒绝（没把原有防护改松）。
    """
    publish = _publish_module()
    model = _real_image_compose_model(16520)
    mutate(model)
    with pytest.raises(publish.PublishError) as excinfo:
        _run_compose_mounts_preflight(publish, model)
    error = excinfo.value
    assert error.exit_code == publish.EXIT_PREFLIGHT, (case, error.message)
    assert error.step == "compose_mounts", (case, error.step)
    assert needle in error.message, (case, error.message)


def test_real_image_compose_passes_full_preflight(ws: Workspace) -> None:
    """同一个真实模型走**完整预检 + 发布流程**也必须成功（回归真机那条路径）。

    `run_publish` 走的是真实入口：compose config → 预检 → 归档 → 备份 → 迁移 →
    切换。旧规则下这里会以 exit 10 / `compose_mounts` 结束；修复后必须一路到
    成功，且记录的 published_ports 就是 compose 里的宿主端口。
    """
    ws.config_path.write_text(
        json.dumps(_real_image_compose_model(ws.http.port)), encoding="utf-8"
    )
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "success"
    assert record["failure"] is None
    assert [item["published"] for item in record["published_ports"]] == [ws.http.port]


def test_update_mount_whitelist_matches_check_compose_rule() -> None:
    """白名单必须与 `scripts/check_compose.py` 的规则逐条相同。

    两份规则分处两个文件（`publish.py` 是纯标准库、宿主上没有 PyYAML，不能 import
    静态校验脚本），一致性只能靠常量 + 这条测试钉住。一边改了另一边没改时的表现
    不是"两道检查都拦"，而是"静态校验 PASS、发布预检 FAIL"（或反过来）——真机
    演练踩的就是这一类。
    """
    publish = _publish_module()
    spec = importlib.util.spec_from_file_location(
        "ling_check_compose_under_test", REPO_ROOT / "scripts" / "check_compose.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert publish.UPDATE_MOUNTS == module.UPDATE_MOUNTS
    assert publish.UPDATE_TARGET_PREFIX == module.UPDATE_TARGET_PREFIX


def test_env_file_too_open_is_rejected(ws: Workspace) -> None:
    ws.env_file.chmod(0o644)
    result = run_publish(ws)
    assert result.returncode == 10
    assert "权限过宽" in result.stderr
    assert_no_changes(ws)


# ---------------------------------------------------------------- 锁门禁


def test_backup_lock_unavailable_fails_immediately(ws: Workspace) -> None:
    lock_dir = ws.backup_dir / ".backup.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (lock_dir / "started_at").write_text("2026-01-01 00:00:00\n", encoding="utf-8")
    try:
        result = run_publish(ws)
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)
    assert result.returncode == 11, result.stderr
    assert "备份锁已被占用" in result.stderr
    assert call_lines(ws) == []


_HOLD_LOCK = r'''
import fcntl
import sys

handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
sys.stdin.readline()
'''


def test_install_lock_unavailable_fails_immediately(ws: Workspace) -> None:
    lock_path = Path(os.path.realpath(ws.env_file)).parent / ".ling-install.lock"
    helper = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "locked"
        result = run_publish(ws)
    finally:
        if helper.stdin is not None:
            helper.stdin.close()
        helper.wait(timeout=10)
    assert result.returncode == 11, result.stderr
    assert "安装锁" in result.stderr
    assert call_lines(ws) == []


# ---------------------------------------------------------------- 备份门禁


@pytest.mark.parametrize(
    "mode",
    ["missing_dump_sha", "bad_dump_sha", "media_partial", "keys_absent", "fail"],
)
def test_incomplete_backup_never_migrates(ws: Workspace, mode: str) -> None:
    result = run_publish(ws, env={"FAKE_BACKUP_MODE": mode})
    assert result.returncode == 13, (mode, result.stdout, result.stderr)
    assert not [line for line in compose_calls(ws) if " run " in line]
    # 备份前 api + worker 都已停，所以未迁移的失败必须把**两个**都恢复（用旧镜像），
    # 且只有实测通过才写回滚成功（见 record["rollback"]["verified"]）。
    up_lines = [line for line in compose_calls(ws) if " up " in line]
    assert [line for line in up_lines if line.endswith(" api")], up_lines
    assert [line for line in up_lines if line.endswith(" worker")], up_lines
    record = ws.record()
    assert record["status"] == "failed_backup"
    assert record["backup"]["complete"] is False
    assert record["migration"]["attempted"] is False
    # 未迁移：必须尝试恢复旧服务，并实测旧服务。
    assert record["rollback"]["attempted"] is True
    assert record["rollback"]["verified"] is True
    assert any("LING_APP_IMAGE=" + OLD_REF in line for line in call_lines(ws))


def test_archive_empty_save_fails_before_backup(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_SAVE_EMPTY": "1"})
    assert result.returncode == 12, result.stderr
    assert "docker save" in result.stderr
    assert not [line for line in compose_calls(ws) if " pull " in line]
    assert not [line for line in call_lines(ws) if line.startswith("backup ")]
    record = ws.record()
    assert record["status"] == "failed_archive"
    assert record["archive"]["complete"] is False
    assert record["rollback"]["attempted"] is False
    assert record["rollback"]["verified"] is True


def test_current_revision_not_on_target_ancestry_fails(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_CHAIN_BROKEN": "1"})
    assert result.returncode == 14, result.stderr
    assert "不是目标 head" in result.stderr
    assert not [line for line in call_lines(ws) if line.startswith("backup ")]
    assert not [line for line in compose_calls(ws) if " stop " in line]


def test_corrupt_saved_archive_fails_before_backup(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_SAVE_CORRUPT": "1"})
    assert result.returncode == 12, result.stderr
    assert "manifest.json" in result.stderr or "tar 归档" in result.stderr
    assert not [line for line in call_lines(ws) if line.startswith("backup ")]
    record = ws.record()
    assert record["status"] == "failed_archive"
    assert record["archive"]["complete"] is False


def test_unknown_current_revision_fails_before_maintenance(ws: Workspace) -> None:
    state = json.loads(ws.state_path.read_text(encoding="utf-8"))
    state["alembic_version"] = "9999_unknown_revision"
    ws.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = run_publish(ws)
    assert result.returncode == 14, result.stderr
    assert "不在目标镜像的修订集合里" in result.stderr
    assert not [line for line in call_lines(ws) if line.startswith("backup ")]
    assert not [line for line in compose_calls(ws) if " stop " in line]
    record = ws.record()
    assert record["status"] == "failed_pull"
    assert record["rollback"]["verified"] is True


def test_backup_failure_does_not_claim_old_version_when_unverified(ws: Workspace) -> None:
    ws.http.readyz_status = 503
    result = run_publish(ws, env={"FAKE_BACKUP_MODE": "fail"})
    assert result.returncode == 13, result.stderr
    record = ws.record()
    assert record["status"] == "failed_backup"
    assert record["rollback"]["attempted"] is True
    assert record["rollback"]["verified"] is False
    assert "未能实测确认旧服务正常" in result.stderr


# ---------------------------------------------------------------- 迁移门禁


def test_migration_log_showing_skip_fails(ws: Workspace) -> None:
    # 日志同时有"执行迁移"和"跳过迁移"（所以 executed 门禁不会兜住），
    # 但数据库版本没前进：只有 skip 门禁能产出 failure.step=migrate_skipped。
    # 删掉 skip 检查后，这里会落到 migrate_head（仍是 15，但 step 不同），
    # 于是本用例独立失败——不再依赖"执行迁移"标记缺失来间接捕获。
    result = run_publish(ws, env={"STUB_MIGRATION_LOG_BOTH_STALE": "1"})
    assert result.returncode == 15, result.stderr
    assert "跳过迁移" in result.stderr
    assert not [line for line in compose_calls(ws) if " up " in line]
    record = ws.record()
    assert record["status"] == "failed_migration"
    assert record["failure"]["step"] == "migrate_skipped"
    assert record["migration"]["skip_marker"] is True
    assert record["migration"]["executed_marker"] is True
    assert record["migration"]["current_after"] == HEAD_OLD
    assert record["rollback"]["attempted"] is False


def test_skip_marker_rejected_even_if_head_matches(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_MIGRATION_LOG_BOTH": "1"})
    assert result.returncode == 15, result.stderr
    assert not [line for line in compose_calls(ws) if " up " in line]
    record = ws.record()
    assert record["migration"]["skip_marker"] is True
    assert record["migration"]["executed_marker"] is True
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False


def test_migration_head_mismatch_does_not_start_api(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_MIGRATE_SET_VERSION": ""})
    assert result.returncode == 15, result.stderr
    assert not [line for line in compose_calls(ws) if " up " in line]
    record = ws.record()
    assert record["status"] == "failed_migration"
    assert record["migration"]["executed_marker"] is True
    assert record["migration"]["current_after"] == HEAD_OLD
    assert record["migration"]["target_head"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False


def test_migration_command_failure_does_not_start_api(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_MIGRATE_EXIT": "7"})
    assert result.returncode == 15, result.stderr
    assert not [line for line in compose_calls(ws) if " up " in line]
    record = ws.record()
    assert record["status"] == "failed_migration"
    assert record["migration"]["exit_code"] == 7
    assert record["rollback"]["attempted"] is False


# ---------------------------------------------------------------- 迁移后失败


def test_runtime_api_worker_digest_mismatch_fails_without_rollback(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_WORKER_IMAGE_ID": "sha256:other-image"})
    assert result.returncode == 16, result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["rollback"]["attempted"] is False
    assert record["result"]["api"]["image_id"] == "sha256:target-image"
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


def test_post_migration_verification_failure_never_rolls_back(ws: Workspace) -> None:
    # 容器内进程自报的提交与发布记录的源提交不符（模拟"跑着的不是目标构建"）。
    result = run_publish(ws, env={"STUB_CONTAINER_COMMIT": "f" * 40})
    assert result.returncode == 16, result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["after_migration"] is True
    assert record["rollback"]["attempted"] is False
    assert record["target"]["digest"] == TARGET
    assert record["previous"]["api"]["image_ref"] == OLD_REF
    assert record["migration"]["current_after"] == HEAD_NEW
    # 不自动切旧镜像/恢复旧 dump：不得出现旧镜像 env 的 up，也不得 load/tag/save。
    assert not [line for line in call_lines(ws) if "docker load" in line]
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


def test_env_tampered_after_preflight_fails_after_migration(ws: Workspace) -> None:
    # 备份脚本在预检之后、update_env_file 之前往受限 env 追加一行。脚本必须用
    # 预检时记下的 env_sha256_before 抓到篡改：退出码 16、状态
    # failed_after_migration，并且不执行任何回滚命令（已迁移）。
    original = ws.env_file.read_text(encoding="utf-8")
    result = run_publish(ws, env={"FAKE_BACKUP_MODE": "tamper_env"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "被改动" in result.stderr
    assert ws.env_file.read_text(encoding="utf-8") != original
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "env_update"
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False
    # 已迁移后失败：不得用旧镜像 up 回滚，也不得启动新 api/worker。
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]
    assert not [line for line in compose_calls(ws) if " up " in line]


def test_web_manifest_failure_after_migration_never_rolls_back(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_WEB_MANIFEST_EXIT": "1"})
    assert result.returncode == 16, result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["rollback"]["attempted"] is False
    assert record["result"]["web_manifest_verified"] is False


def test_schema_drift_after_start_never_rolls_back(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_HEAD_AFTER_API_START": "9999_schema_drift"})
    assert result.returncode == 16, result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["rollback"]["attempted"] is False
    assert record["result"]["alembic_version_after_start"] == "9999_schema_drift"
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


def test_readyz_failure_after_migration_never_rolls_back(ws: Workspace) -> None:
    ws.http.readyz_status = 503
    result = run_publish(ws)
    assert result.returncode == 16, result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["rollback"]["attempted"] is False


def test_readyz_redirect_is_not_accepted_as_200(ws: Workspace) -> None:
    # /readyz 返回 302 → /readyz-ok（那里确实是 200）。默认 urllib 会跟随
    # 重定向并把结果读成 200；发布脚本必须不跟随，并在拿到 3xx 时明确失败。
    ws.http.readyz_redirect = True
    result = run_publish(ws)
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "重定向" in result.stderr
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "readyz_redirect"
    assert record["result"]["readyz_http"] is None
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False
    # 失败发生在启动 worker 之前，且不得出现任何回滚 up。
    assert not [line for line in compose_calls(ws) if " up " in line and "worker" in line]
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


# ------------------------------------------- 容器内探活前先等 api healthy（缺陷 5）
#
# 真机演练抓到：更新器走维护门禁（宿主端口不发布），`up -d api` 之后直接
# `compose exec ... /readyz`。容器当时只是 running，应用还没 bind 端口 →
# Connection refused → 已迁移后失败（switch_failed/readyz_container）。实况是
# 容器稍后 `Up 41 seconds (healthy)`、日志里 `GET /readyz 200 OK`。
#
# 下面的用例用 stub 的 `STUB_API_HEALTH` 脚本化 `.State.Health.Status`
# （`starting,starting,healthy` 表示轮询三次才 healthy），并让容器内 exec
# 在 api 仍 starting 时真的返回 Connection refused —— 与真实 Docker 同形。


def _api_polls_after_up(ws: Workspace) -> list[int]:
    """`up ... api` 之后每次 `docker inspect --type container c-api` 的行号。"""
    lines = call_lines(ws)
    up_api = next(
        index
        for index, line in enumerate(lines)
        if " up -d " in line and line.endswith(" api")
    )
    return [
        index
        for index, line in enumerate(lines)
        if index > up_api and line == "docker inspect --type container c-api"
    ]


def _readyz_execs(ws: Workspace) -> list[int]:
    """容器内 /readyz 探活（`compose exec ... 6520/readyz`）的行号。"""
    return [
        index
        for index, line in enumerate(call_lines(ws))
        if " exec " in line and "6520/readyz" in line
    ]


def test_api_health_wait_polls_until_healthy_before_container_readyz(
    ws: Workspace,
) -> None:
    """容器 starting 两轮才 healthy：必须等到 healthy 再 exec 探活。

    旧实现只跑一次 exec，且把 running 当就绪：第一次探活就会拿到
    Connection refused，于是把"启动慢"误报成"目标版本启动失败"。
    """
    result = run_publish(
        ws,
        env={"STUB_API_HEALTH": "starting,starting,healthy"},
        extra=["--health-timeout", "15"],
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "success"
    assert record["result"]["api_health"]["polls"] == 3
    assert record["result"]["readyz_container_attempts"] == 1

    polls = _api_polls_after_up(ws)
    execs = _readyz_execs(ws)
    assert len(polls) >= 3, (polls, call_lines(ws))
    assert execs, "成功路径必须真的做过容器内探活"
    # 探活只能发生在"第 3 次检查拿到 healthy"之后，不能抢在 healthy 之前。
    assert polls[2] < execs[0], (polls, execs, call_lines(ws))


def test_api_health_starting_until_timeout_fails_without_probe_or_rollback(
    ws: Workspace,
) -> None:
    """一直 starting 到超时：失败、指明"没 healthy"、不进入 exec 探活、不回滚。"""
    result = run_publish(ws, env={"STUB_API_HEALTH": "starting"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "api_health"
    assert "没变 healthy" in record["failure"]["message"]
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False
    assert _readyz_execs(ws) == []
    assert not [line for line in compose_calls(ws) if " up " in line and "worker" in line]
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


def test_api_container_exited_fails_immediately_without_waiting_timeout(
    ws: Workspace,
) -> None:
    """容器 exited → 立即失败，不等满 --health-timeout（15s 只检查一次）。"""
    result = run_publish(ws, env={"STUB_API_EXITED": "1"}, extra=["--health-timeout", "15"])
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "api_container_exited"
    assert "没起来" in record["failure"]["message"]
    assert "已退出" in record["failure"]["message"]
    assert record["rollback"]["attempted"] is False
    # 一次检查就收手；傻等超时会是 ~8 次（2s 一轮 × 15s）。
    assert len(_api_polls_after_up(ws)) == 1, call_lines(ws)
    assert _readyz_execs(ws) == []


def test_api_container_missing_fails_immediately(ws: Workspace) -> None:
    """容器对象不存在（compose 报成功但 docker ps -a 找不到）→ 立即失败。"""
    result = run_publish(ws, env={"STUB_API_MISSING": "1"}, extra=["--health-timeout", "15"])
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "api_container_missing"
    assert "没起来" in record["failure"]["message"]
    assert "0 个 api 容器" in record["failure"]["message"]
    assert record["rollback"]["attempted"] is False
    assert _api_polls_after_up(ws) == []
    assert _readyz_execs(ws) == []


def test_container_readyz_exec_retries_after_one_failure(ws: Workspace) -> None:
    """healthy 之后 exec 探活第一次失败、第二次成功 → 整体通过（重试生效）。"""
    result = run_publish(ws, env={"STUB_CONTAINER_READYZ_SCRIPT": "1,0"})
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "success"
    assert record["result"]["readyz_container_attempts"] == 2
    assert len(_readyz_execs(ws)) == 2, call_lines(ws)


def test_container_readyz_exec_keeps_failing_reports_exec_probe(ws: Workspace) -> None:
    """healthy 但 exec 探活一直失败 → 失败信息明确指到"exec 探活失败"。"""
    result = run_publish(ws, env={"STUB_CONTAINER_READYZ_EXIT": "1"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "readyz_container"
    message = record["failure"]["message"]
    assert "已 healthy" in message
    assert "exec 探活" in message and "3 次都失败" in message
    assert "Connection refused" in message
    assert record["result"]["readyz_container_attempts"] == 3
    assert len(_readyz_execs(ws)) == 3, call_lines(ws)
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False
    # 已迁移后失败：不得用旧镜像 up 回滚，也不得启动 worker。
    assert not [line for line in compose_calls(ws) if " up " in line and "worker" in line]
    assert not [
        line
        for line in call_lines(ws)
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]


def test_api_without_healthcheck_falls_back_to_bounded_exec_probe(
    ws: Workspace,
) -> None:
    """compose 没声明 api healthcheck 时：没有健康信号可等，但仍不把 running 当就绪。

    这条路径是更新器隔离用例的形态（它的 compose 模型只给 worker 声明
    healthcheck）：不能因此把"容器进程起来了"当成就绪，而是靠有限次 exec 探活
    证明端口真的在服务。
    """
    result = run_publish(ws, env={"STUB_API_HEALTH": "none"})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "没有健康信号可等" in result.stderr
    record = ws.record()
    assert record["status"] == "success"
    assert record["result"]["api_health"] == {
        "polls": 1,
        "state": "status=running health=None",
    }
    assert record["result"]["readyz_container_attempts"] == 1


def test_api_healthcheck_declared_but_health_missing_fails(ws: Workspace) -> None:
    """compose 声明了 api healthcheck 但运行时读不到 State.Health → 失败（不猜）。"""
    model = _config_json(ws.http.port)
    model["services"]["api"]["healthcheck"] = {
        "test": ["CMD", "python", "-c", "import urllib.request"],
        "interval": "10s",
        "timeout": "5s",
        "retries": 3,
    }
    ws.config_path.write_text(json.dumps(model), encoding="utf-8")
    result = run_publish(ws, env={"STUB_API_HEALTH": "none"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "api_health"
    assert "State.Health 缺失" in record["failure"]["message"]
    assert record["rollback"]["attempted"] is False
    assert _readyz_execs(ws) == []


# ---------------------------------------------------------------- 成功路径


def test_success_path_records_identity_and_orders_gates(ws: Workspace) -> None:
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "success"
    assert record["exit_code"] == 0
    assert record["target"]["digest"] == TARGET
    assert record["target"]["digest_hex"] == TARGET_HEX
    assert record["target"]["source_commit"] == COMMIT
    assert record["previous"]["alembic_version"] == HEAD_OLD
    assert record["volumes"]["app-data"] == {
        "name": "ling_app-data",
        "driver": "local",
        "compose_name": "ling_app-data",
        "project_label": "ling",
    }
    assert record["volumes"]["db-data"]["name"] == "ling_db-data"
    assert record["archive"]["complete"] is True
    archives = record["archive"]["images"]
    assert [item["service"] for item in archives] == ["api", "worker"]
    tar_path = Path(archives[0]["tar"])
    assert tar_path.is_file() and tar_path.stat().st_size > 0
    assert archives[0]["tar_sha256"] == hashlib.sha256(tar_path.read_bytes()).hexdigest()
    # 日期化 tag 必须带时间，避免同一天第二次发布覆盖上一份回退材料。
    assert re.fullmatch(r"ling-rollback-\d{8}-\d{6}-api", archives[0]["tag"])
    assert archives[1]["service"] == "worker"
    assert archives[1]["tar"] == archives[0]["tar"]
    assert record["backup"]["complete"] is True
    assert record["backup"]["dump_sha256"] == hashlib.sha256(b"dump-bytes").hexdigest()
    assert record["backup"]["media_state"] == "present"
    assert record["backup"]["keys_root"] == "present"
    assert record["migration"]["target_head"] == HEAD_NEW
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["migration"]["executed_marker"] is True
    assert record["migration"]["skip_marker"] is False
    assert record["result"]["readyz_http"] == 200
    assert record["result"]["version_commit"] == COMMIT
    assert record["result"]["api"]["image_id"] == "sha256:target-image"
    assert record["result"]["worker"]["image_id"] == "sha256:target-image"
    assert record["result"]["api"]["repo_digests"] == [TARGET]
    env_text = ws.env_file.read_text(encoding="utf-8")
    assert f"LING_APP_IMAGE={TARGET}" in env_text
    assert OLD_REF not in env_text
    assert stat.S_IMODE(ws.env_file.stat().st_mode) & 0o077 == 0
    assert not (ws.backup_dir / ".backup.lock").exists()

    lines = call_lines(ws)
    assert not [line for line in lines if " compose " in line and " build" in line]
    def index_of(predicate: str) -> int:
        for index, line in enumerate(lines):
            if predicate in line:
                return index
        raise AssertionError(f"未找到调用：{predicate}\n" + "\n".join(lines))

    pull = index_of("docker compose -p ling -f " + str(ws.compose_file) + " --env-file " + str(ws.env_file) + " pull api worker")
    stop_api = index_of(" stop api")
    stop_worker = index_of(" stop worker")
    backup_state = index_of("backup-state ")
    backup = next(
        index for index, line in enumerate(lines) if line.startswith("backup ")
    )
    migrate = index_of(" run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate")
    up_api = index_of(" up -d --no-deps --no-build --pull=never api")
    up_worker = index_of(" up -d --no-deps --no-build --pull=never worker")
    # 先停写（api + worker）→ 备份 → 迁移 → 起新。**api 在备份前必须已停**：
    # 旧的「停 worker → 备份 → 停 api」会让 backup.sh 在 api 仍在服务时读库/读卷。
    assert pull < stop_api < stop_worker < backup_state < backup < migrate < up_api < up_worker


def test_backup_runs_after_api_and_worker_are_stopped(ws: Workspace) -> None:
    """一致备份的**独立**门禁：备份启动那一刻，api/worker 的容器实况都必须是停的。

    stub 的 fake backup.sh 直接读 docker 替身的状态文件并以 9 拒绝（见
    ``_FAKE_BACKUP``），所以就算调用顺序被改回去，这条也会单独红。
    """
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = call_lines(ws)
    state_lines = [line for line in lines if line.startswith("backup-state ")]
    assert state_lines == ["backup-state api=0 worker=0"], lines
    stop_api = next(
        index for index, line in enumerate(lines) if " stop api" in line
    )
    backup = next(
        index for index, line in enumerate(lines) if line.startswith("backup ")
    )
    assert stop_api < backup
    # 备份之后到迁移之前**不得**再出现 `stop api`（那不是"先停写再备份"）。
    stop_api_indexes = [
        index for index, line in enumerate(lines) if " stop api" in line
    ]
    assert max(stop_api_indexes) < backup, (stop_api_indexes, backup)


def test_backup_gets_running_api_image_id_for_oneshot_reader(ws: Workspace) -> None:
    """停 api 之前先捕获它的 image ID，并通过 ``LING_BACKUP_APP_IMAGE`` 传给 backup.sh。

    api 停了以后 backup.sh 要用**本地已有**的镜像起一次性只读取数容器；镜像不存在
    时它必须 fail closed（不 pull），所以这个值必须来自"停之前"的运行中容器。
    """
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    backup_env = [line for line in call_lines(ws) if line.startswith("backup-env ")]
    assert len(backup_env) == 1
    assert "LING_BACKUP_APP_IMAGE=sha256:old-image" in backup_env[0]
    # 捕获发生在停 api 之前（docker ps/inspect 在 stop api 之前）。
    lines = call_lines(ws)
    stop_api = next(index for index, line in enumerate(lines) if " stop api" in line)
    inspect_api = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("docker inspect") and "c-api" in line
    )
    assert inspect_api < stop_api


def test_backup_receives_same_compose_group_and_project(ws: Workspace) -> None:
    extra = ["--compose-override", str(ws.compose_file)]
    result = run_publish(ws, extra=extra)
    assert result.returncode == 0, result.stderr
    backup_env = [
        line for line in call_lines(ws) if line.startswith("backup-env ")
    ]
    assert len(backup_env) == 1
    line = backup_env[0]
    assert "LING_COMPOSE_PROJECT=ling" in line
    assert f"LING_COMPOSE_FILE={ws.compose_file}" in line
    assert f"LING_COMPOSE_OVERRIDE={ws.compose_file}" in line
    assert f"LING_COMPOSE_ENV_FILE={ws.env_file}" in line
    assert f"LING_APP_IMAGE={OLD_REF}" in line
    assert "LING_BACKUP_APP_IMAGE=sha256:old-image" in line
    # 同一文件组也用于 config/pull/run/up。
    compose_lines = compose_calls(ws)
    for needle in (" config ", " pull ", " run ", " stop worker", " stop api", " up "):
        matches = [line for line in compose_lines if needle in line]
        assert matches, needle
        for match in matches:
            assert f"-f {ws.compose_file}" in match
            assert f"-f {ws.compose_file} -f {ws.compose_file}" in match
            assert f"--env-file {ws.env_file}" in match


# ---------------------------------------------------------------- 根密钥注入
#
# P5 真机演练第 4 个阻断缺陷：更新器容器里没有 LING_ROOT_KEY（镜像模式也不把
# 根密钥落到 app-data 卷），根密钥只在受限 env 里。backup() 若只 copy 进程环境，
# backup.sh 会记 keys_env_set=false，manifest 门禁随后拒绝迁移。下面这些用例
# 钉住"从 .env 只读这一项、注入子进程、缺了就备份前失败、且绝不回显"。


def _write_env_text(ws: Workspace, text: str) -> None:
    ws.env_file.write_text(text, encoding="utf-8")
    ws.env_file.chmod(0o600)


def _root_key_probe_lines(ws: Workspace) -> list[str]:
    return [line for line in call_lines(ws) if line.startswith("backup-root-key-")]


def test_backup_receives_root_key_from_env_file(ws: Workspace) -> None:
    """值只来自受限 env：夹具已把子进程环境里的 LING_* 全部剔除。"""
    assert "LING_ROOT_KEY" not in ws.env
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    lines = _root_key_probe_lines(ws)
    assert "backup-root-key-set 1" in lines, lines
    expected = hashlib.sha256(ROOT_KEY.encode("utf-8")).hexdigest()
    assert f"backup-root-key-sha {expected}" in lines, lines
    # 测试桩只记哈希：调用记录里也不该出现明文。
    assert ROOT_KEY not in ws.call_log.read_text(encoding="utf-8")


def test_missing_root_key_fails_before_backup_without_running_backup_script(
    ws: Workspace,
) -> None:
    """没有根密钥时**在停写和备份之前**失败，绝不产出一份恢复后解不开的备份。"""
    _write_env_text(ws, f"POSTGRES_PASSWORD=pw\nLING_APP_IMAGE={OLD_REF}\n")
    result = run_publish(ws)
    assert result.returncode == 13, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_backup"
    assert record["failure"]["step"] == "backup_root_key"
    assert "LING_ROOT_KEY" in record["failure"]["message"]
    assert record["next_actions"]
    assert any("只备份 dump" in action for action in record["next_actions"])
    calls = call_lines(ws)
    assert not [line for line in calls if line.startswith("backup")], calls
    assert not [
        line for line in calls if " stop api" in line or " stop worker" in line
    ], calls
    assert not list(ws.backup_dir.rglob("manifest-*.txt"))
    assert record["backup"]["complete"] is False
    assert ROOT_KEY not in result.stderr


def test_multiline_root_key_is_rejected_before_backup(ws: Workspace) -> None:
    """值跨行（引号未闭合）→ 失败且不调 backup.sh，而不是截断成半把密钥。"""
    _write_env_text(
        ws,
        f'POSTGRES_PASSWORD=pw\nLING_APP_IMAGE={OLD_REF}\n'
        f'LING_ROOT_KEY="{ROOT_KEY}\nNEXT=1\n',
    )
    result = run_publish(ws)
    assert result.returncode == 13, (result.stdout, result.stderr)
    record = ws.record()
    assert record["failure"]["step"] == "backup_root_key"
    assert "引号未闭合" in record["failure"]["message"]
    assert not [line for line in call_lines(ws) if line.startswith("backup")]
    assert ROOT_KEY not in result.stderr
    assert ROOT_KEY not in json.dumps(record, ensure_ascii=False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"LING_ROOT_KEY={ROOT_KEY}\n", ROOT_KEY),
        (f"export LING_ROOT_KEY={ROOT_KEY}\n", ROOT_KEY),
        (f'export  LING_ROOT_KEY="{ROOT_KEY}"\n', ROOT_KEY),
        (f"\tLING_ROOT_KEY={ROOT_KEY}\n", ROOT_KEY),
        (f"LING_ROOT_KEY= {ROOT_KEY} \n", ROOT_KEY),
        (f"  LING_ROOT_KEY={ROOT_KEY}\r\n", ROOT_KEY),  # 前导空白 + CRLF
        (
            f"# LING_ROOT_KEY=commented-out\r\nPOSTGRES_PASSWORD=pw\r\n"
            f"LING_ROOT_KEY={ROTATED_ROOT_KEY}\r\n",
            ROTATED_ROOT_KEY,
        ),
        (f'LING_ROOT_KEY="{ROOT_KEY}"\n', ROOT_KEY),  # 双引号
        (f"LING_ROOT_KEY='{ROOT_KEY}'\n", ROOT_KEY),  # 单引号
        (
            f"LING_ROOT_KEY=stale-value\nPOSTGRES_PASSWORD=pw\n"
            f"LING_ROOT_KEY={ROTATED_ROOT_KEY}\n",
            ROTATED_ROOT_KEY,  # 重复键：最后一行生效
        ),
        (f"LING_ROOT_KEY={ROOT_KEY}\nLING_ROOT_KEY=\n", None),  # 最后为空 = 没给
        ("POSTGRES_PASSWORD=pw\n", None),
        (f"LING_ROOT_KEY ={ROOT_KEY}\n", None),  # 键必须紧贴 `=`
        ("# LING_ROOT_KEY=only-a-comment\n", None),
    ],
)
def test_parse_env_file_value_matches_install_cron_semantics(
    text: str, expected: str | None
) -> None:
    publish = _publish_module()
    assert publish.parse_env_file_value(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f'LING_ROOT_KEY="{ROOT_KEY}\nNEXT=1\n',  # 引号未闭合：值跨行
        f"LING_ROOT_KEY={ROOT_KEY}\rmiddle\n",  # 嵌入 CR
        f"LING_ROOT_KEY={ROOT_KEY}\x01\n",  # C0 控制字符
        f'LING_ROOT_KEY="{ROOT_KEY}\\n"\n',  # 双引号里的换行转义（compose 会展开）
        f"LING_ROOT_KEY={ROOT_KEY}\x7f\n",  # DEL
    ],
)
def test_parse_env_file_value_rejects_multiline_and_control_chars(text: str) -> None:
    publish = _publish_module()
    with pytest.raises(ValueError) as excinfo:
        publish.parse_env_file_value(text)
    # 拒绝也要守住"不回显"：异常里不能带值。
    assert ROOT_KEY not in str(excinfo.value)


def test_root_key_never_appears_in_record_logs_or_output(ws: Workspace) -> None:
    """明文只允许存在受限 env（及其 0600 归档副本）；记录/日志/输出一律不出现。"""
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert ROOT_KEY not in result.stdout
    assert ROOT_KEY not in result.stderr
    artifacts = [ws.record_path]
    artifacts += sorted(ws.backup_dir.rglob("*.log"))
    artifacts += sorted(ws.backup_dir.rglob("*.json"))
    texts = {
        str(path): path.read_text(encoding="utf-8")
        for path in artifacts
        if path.is_file()
    }
    assert texts
    leaked = [name for name, payload in texts.items() if ROOT_KEY in payload]
    assert leaked == []
    assert ROOT_KEY not in ws.call_log.read_text(encoding="utf-8")

# ------------------------------- 缺陷 6：发布/更新的 HTTP 探活必须免鉴权
#
# 真机演练 P5 第 6 个缺陷：发布脚本经宿主端口打 ``/v1/version``、更新器在容器内
# exec 打同一个端点来核提交，而该端点挂 ``CurrentAuth``，未鉴权永远 401 →
# ``failed_after_migration / version_container``。成因是 stub 对 ``/v1/version``
# 无条件回 200：单元测试一直绿，真实应用一直红。所以这里的 HTTP stub **如实**
# 对 ``/v1/*`` 回 401。
#
# 现在的约定：发布/更新流程**不持有用户凭据**，HTTP 探活只允许打
# ``publish.HTTP_PROBE_PATHS``（当前只有免鉴权的 ``/readyz``）；"跑着的进程是不是
# 目标构建"改用容器内进程证据（``compose exec api python -c``，零 HTTP）。
# 真实应用上的逐条核对在 ``tests/integration/test_version_image_update.py``。


def _version_probe_execs(ws: Workspace) -> list[str]:
    """容器内进程证据探活（``compose exec ... python -c`` 里带 ``__version__``）的行。"""
    return [line for line in call_lines(ws) if " exec " in line and "__version__" in line]


def test_stub_http_v1_version_is_unauthorized(ws: Workspace) -> None:
    """stub 必须如实反映真实应用：``/v1/version`` 未鉴权是 401，不是 200。

    这条用例是给"成因"本身把关的：stub 一旦又回 200，下面那条"成功路径没打
    ``/v1/version``"就失去意义（旧实现正是靠这个绿灯活到了真机）。
    """
    publish = _publish_module()
    code, body = publish.Context.http_get(
        f"http://127.0.0.1:{ws.http.port}/v1/version", timeout=3.0
    )
    assert code == 401, (code, body)
    assert body == b""


def test_success_path_only_requests_unauthenticated_paths(ws: Workspace) -> None:
    """成功路径打过的宿主 HTTP 路径必须都在 ``HTTP_PROBE_PATHS`` 里。"""
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    publish = _publish_module()
    requests = list(ws.http.requests)
    assert requests, "成功路径必须真的打过往宿主的 HTTP 探活"
    assert "/v1/version" not in requests, requests
    assert set(requests) <= set(publish.HTTP_PROBE_PATHS), requests


def test_version_evidence_comes_from_container_process(ws: Workspace) -> None:
    """提交证据来自容器内进程：argv 里没有任何 HTTP，且与镜像标注一致。

    docker stub 不会真的执行 `python -c`（它只模仿 Docker 的输出），所以探针
    脚本**本身**对真实 `ling` 包是否可用由下一条用例执行验证；这里管的是
    "发布脚本用了这条证据、并且拿读到的值去比对"。
    """
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    execs = _version_probe_execs(ws)
    assert execs, call_lines(ws)
    argv = execs[-1]
    for forbidden in ("http://", "https://", "urllib", "/v1/"):
        assert forbidden not in argv, argv
    record = ws.record()
    # 进程自报的提交 == 发布记录源提交 == 容器镜像的 OCI 标注 revision。
    # stub 的提交来自**镜像** Config.Env（真实 Dockerfile 的构建期 ENV），
    # 脚本若改成"自己传 commit 再读回来"，这里就对不上。
    assert record["result"]["version_commit"] == COMMIT
    assert record["result"]["api"]["commit"] == COMMIT
    assert record["result"]["version_reported"] == "9.9.9"


def test_container_version_probe_script_runs_against_real_package(tmp_path: Path) -> None:
    """探针脚本本身必须真的能跑：读 ``ling.__version__`` 与 ``LING_COMMIT``。"""
    publish = _publish_module()
    script = publish.CONTAINER_VERSION_PROBE
    for forbidden in ("http", "urllib", "/v1/"):
        assert forbidden not in script, script
    env = dict(os.environ)
    env["LING_COMMIT"] = COMMIT
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = publish.parse_container_version_probe(completed.stdout)
    assert payload == {"version": LING_VERSION, "commit": COMMIT}


def test_image_commit_env_and_oci_revision_share_one_build_arg() -> None:
    """进程证据的前提：镜像 ENV 的 LING_COMMIT 与 OCI 标注 revision 同源。

    探活不再走 HTTP，改读容器内 `LING_COMMIT`；它必须与发布脚本已核对的
    `org.opencontainers.image.revision` 来自**同一个构建参数**，否则"进程自报
    提交"与"镜像标注提交"可以各说一套，这条证据就失去意义。
    """
    text = (REPO_ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    assert "ARG LING_COMMIT=" in text
    assert "ENV LING_COMMIT=${LING_COMMIT}" in text
    assert 'org.opencontainers.image.revision="${LING_COMMIT}"' in text


def test_parse_container_version_probe_skips_noise() -> None:
    publish = _publish_module()
    noisy = "booting…\nnot json\n" + json.dumps({"version": "1.2.3", "commit": COMMIT})
    assert publish.parse_container_version_probe(noisy) == {
        "version": "1.2.3",
        "commit": COMMIT,
    }
    assert publish.parse_container_version_probe("") == {}
    assert publish.parse_container_version_probe("{not json}") == {}


def test_container_commit_mismatch_fails_after_migration(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_CONTAINER_COMMIT": "f" * 40})
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "version"
    assert "容器内进程报告的提交" in record["failure"]["message"]
    assert record["rollback"]["attempted"] is False


def test_container_version_probe_exec_failure_fails_after_migration(ws: Workspace) -> None:
    result = run_publish(ws, env={"STUB_CONTAINER_VERSION_EXIT": "1"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "version"
    assert "ModuleNotFoundError" in record["failure"]["message"]
    assert record["rollback"]["attempted"] is False


# ------------------------------- 缺陷 7：宿主端口探活必须借 host 网络的一次性容器
#
# 真机演练 P5 第 7 个缺陷：容器内验证全过、正式 compose 发布端口也成功，最后却
# `failed_after_migration / readyz`（宿主 6521 在 180s 内没有返回 200，最后 0）。
# 因为那条探测是更新器**容器进程自己**打的 `127.0.0.1:<宿主端口>`：容器里的环回
# 不是宿主的。修法：publish/updater 进程不再自己发这个 HTTP，改成一次性
# `--network host` 容器（镜像用更新器自己的/已核验的），并且先 `docker inspect`
# 证明端口真的发布出去了（没发布就立即失败，不等 180s）。

HOST_PROBE_LOG = "host-probe-args "


def _flag_value(argv: list[str], flag: str) -> str:
    assert flag in argv, argv
    return argv[argv.index(flag) + 1]


def _host_probe_argv(ws: Workspace) -> list[str]:
    lines = [line for line in call_lines(ws) if line.startswith(HOST_PROBE_LOG)]
    assert lines, "没有出现宿主端口探针容器：\n" + "\n".join(call_lines(ws))
    return json.loads(lines[0][len(HOST_PROBE_LOG) :])


def _context_for_host_probe(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> Any:
    """在测试进程里构造一个 Context，并让它的 docker 走 stub（同 ws 的 PATH）。"""
    publish = _publish_module()
    for key, value in ws.env.items():
        monkeypatch.setenv(key, value)
    options = publish.Options(
        target=TARGET,
        target_hex=TARGET_HEX,
        compose_file=ws.compose_file,
        compose_override=None,
        env_file=ws.env_file,
        project="ling",
        backup_dir=ws.backup_dir,
        backup_script=ws.backup_script,
        record_path=ws.record_path,
        snapshot_dir=ws.backup_dir / "release-test",
        source_commit=None,
        ready_timeout=2.0,
        health_timeout=2.0,
        migrate_timeout=10.0,
        backup_timeout=10.0,
    )
    context = publish.Context(options)
    context.published_port = ws.http.port
    context.ready_host = "127.0.0.1"
    context.published_ports = [
        {
            "service": "api",
            "target": 6520,
            "published": ws.http.port,
            "protocol": "tcp",
        }
    ]
    return publish, context


def test_host_readyz_probe_is_a_host_network_one_shot_container(ws: Workspace) -> None:
    """宿主 /readyz 探针的 argv 逐项：host 网络 + 只读 + 不拉镜像 + 无特权。

    旧实现是进程自己 `urlopen`（容器里永远连不上宿主端口），本用例钉住 argv：
    删掉 ``--network host``、``--pull=never``、``--entrypoint python`` 中任何一项，
    这里立刻红（变异验证见交付说明）。
    """
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    argv = _host_probe_argv(ws)
    # stub 记录的是 argv[1:]（"docker" 由 PATH 解析），首项即子命令。
    assert argv[:1] == ["run"], argv
    assert "--rm" in argv, argv
    assert _flag_value(argv, "--network") == "host", argv
    assert "--read-only" in argv, argv
    assert _flag_value(argv, "--tmpfs") == "/tmp", argv
    assert "--pull=never" in argv, argv
    assert _flag_value(argv, "--entrypoint") == "python", argv
    # 权限最小：不加 privileged、不挂 docker socket（这个探针只需要 TCP）。
    assert "--privileged" not in argv, argv
    assert not any("/var/run/docker.sock" in item for item in argv), argv
    # 镜像 = 已核验的目标镜像（宿主上直接跑 publish.py 时的回退；容器里是更新器
    # 自己的镜像）。必须是本机已有镜像：探针带 --pull=never，不会为它再拉一次。
    image = argv[argv.index("-c") - 1]
    assert image == TARGET, argv
    code = argv[argv.index("-c") + 1]
    assert f"127.0.0.1:{ws.http.port}/readyz" in code, code
    # 不跟随 3xx（沿用 _NoRedirectHandler 的思路），状态码打到 stdout。
    assert "HTTPRedirectHandler" in code, code
    record = ws.record()
    assert record["status"] == "success"
    assert record["result"]["readyz_http"] == 200
    assert record["result"]["readyz_probe_image"] == TARGET
    assert record["result"]["readyz_probe_attempts"] == 1


def test_host_readyz_is_requested_by_the_probe_container_not_by_the_process(
    ws: Workspace,
) -> None:
    """"谁在碰宿主 HTTP"必须是探针容器，不是 publish 进程自己。

    HTTP stub 按 User-Agent 记录调用方：探针带
    ``ling-host-readyz-probe/1``，而进程自己的 ``http_get`` 带
    ``ling-release-publish/1``。把实现退回进程内 urlopen，这条立刻红。
    """
    result = run_publish(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    publish = _publish_module()
    pairs = list(zip(ws.http.requests, ws.http.user_agents, strict=True))
    readyz_agents = [agent for path, agent in pairs if path == "/readyz"]
    assert readyz_agents, ws.http.requests
    assert readyz_agents == [publish.HOST_READYZ_PROBE_USER_AGENT], pairs
    assert "ling-release-publish/1" not in ws.http.user_agents, pairs


def test_host_probe_image_can_be_injected(ws: Workspace) -> None:
    """镜像可注入：运维显式给一个本机已有镜像时，探针必须用它。"""
    updater_image = f"ghcr.io/qsbb/ling-updater@sha256:{'7' * 64}"
    result = run_publish(ws, extra=["--host-probe-image", updater_image])
    assert result.returncode == 0, (result.stdout, result.stderr)
    argv = _host_probe_argv(ws)
    assert argv[argv.index("-c") - 1] == updater_image, argv
    assert ws.record()["result"]["readyz_probe_image"] == updater_image


def test_unpublished_host_port_fails_immediately_without_waiting(ws: Workspace) -> None:
    """端口没发布 → 立即失败并点明"维护门禁没解除"，不进入 180s 等待。"""
    started = time.monotonic()
    result = run_publish(
        ws,
        env={"STUB_HOST_PORT_PUBLISHED": "0"},
        extra=["--ready-timeout", "180"],
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "host_port", record["failure"]
    assert record["failure"]["after_migration"] is True
    assert "维护门禁没解除" in record["failure"]["message"]
    assert record["result"]["readyz_http"] is None
    # 一次探针都不许起：端口没发布时探活没有意义。
    assert not [line for line in call_lines(ws) if line.startswith(HOST_PROBE_LOG)]
    # --ready-timeout=180 却立刻返回（<30s），证明没有傻等。
    assert elapsed < 30, elapsed
    assert record["migration"]["current_after"] == HEAD_NEW
    assert record["rollback"]["attempted"] is False


def test_host_readyz_probe_nonzero_fails_after_migration_without_rollback(
    ws: Workspace,
) -> None:
    """探针容器返回非 0（应用真的没就绪）→ 已迁移后失败语义不变：不回滚。"""
    ws.http.readyz_status = 503
    result = run_publish(ws)
    assert result.returncode == 16, (result.stdout, result.stderr)
    record = ws.record()
    assert record["status"] == "failed_after_migration"
    assert record["failure"]["step"] == "readyz", record["failure"]
    assert "最后 503" in record["failure"]["message"]
    assert record["result"]["readyz_http"] is None
    assert record["result"]["readyz_probe_attempts"] >= 1
    assert record["rollback"]["attempted"] is False
    assert not [line for line in compose_calls(ws) if " up " in line and "worker" in line]
    # 探针确实是 host 网络容器（失败语义变了，但跑法没变）。
    argv = _host_probe_argv(ws)
    assert _flag_value(argv, "--network") == "host", argv


def test_probe_without_host_network_cannot_see_the_host_port(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反证：**同一段代码**去掉 ``--network host`` 就永远拿不到 200。

    stub 的 docker run 如实模拟网络命名空间：非 host 模式真的拒绝连接。所以
    "``--network none`` 的旧实现"在这里不是靠注释断言，而是真的失败。
    """
    publish, context = _context_for_host_probe(ws, monkeypatch)
    # 先证明同一套代码在 --network host 下拿得到 200。
    assert context.wait_readyz() == 200
    good = context.host_readyz_probe_argv()
    assert _flag_value(good, "--network") == "host", good

    original = context.host_readyz_probe_argv

    def no_network_argv(image: str | None = None) -> list[str]:
        argv = original(image)
        argv[argv.index("--network") + 1] = "none"
        return argv

    monkeypatch.setattr(context, "host_readyz_probe_argv", no_network_argv)
    with pytest.raises(publish.PublishError) as excinfo:
        context.wait_readyz()
    assert excinfo.value.step == "readyz"
    assert "最后 0" in excinfo.value.message
