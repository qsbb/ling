"""``deploy/cli/ling.py`` 的隔离回归测试（stub-only）。

这些用例在临时目录里真实执行 ``ling install/status/logs``，只用 stub 替换
``docker``，并用本地 HTTP stub 提供 ``/readyz``；``/v1/*`` 一律**如实**回
401（真实应用对它们挂了 ``CurrentAuth``，见 ``src/ling/api/routes/version.py``）。
不会真的拉镜像、起容器、碰生产或执行真实迁移。

版本/提交的核对走**容器内进程证据**（``compose exec api python -c``），stub
里对应 ``exec`` 分支打印应用自报的 JSON——与发布/更新流程同一口径。

stub-only 的局限（必须说清楚）
============================
1. stub 自己也在近似解释 docker/compose 的 JSON 输出，所以这里证明的是
   安装器的门禁顺序与失败语义，**不是真实 Docker 的行为**。真实 Compose 的
   ``config --format json``、``pull`` 平台解析、``run`` 迁移日志、``up`` 后的
   容器 image ID 仍必须在隔离实例真机验证。
2. 安装器的端口占用检查是**真实 socket bind**（能发现宿主进程，而不只是
   容器）。因此成功路径的 ``/readyz`` 由测试在 ``compose pull`` 之后才启动
   （见 ``late_readyz``）：预检时端口确实空闲，验证时才有人应答——这与真机
   上"容器在 up 之后开始监听"的时序一致。
3. 安装器生成的 compose 副本来自**仓库真实的** ``deploy/compose.image.yaml``，
   所以"宿主端口行"的锚点如果被改坏，这些用例会直接失败（不会静默产出一份
   绑 0.0.0.0 的文件）。
"""

from __future__ import annotations

import ast
import atexit
import contextlib
import fcntl
import hashlib
import http.server
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LING_PY = REPO_ROOT / "deploy" / "cli" / "ling.py"
PUBLISH_PY = REPO_ROOT / "deploy" / "release" / "publish.py"
UPDATER_PY = REPO_ROOT / "deploy" / "updater" / "updater.py"
REAL_COMPOSE = REPO_ROOT / "deploy" / "compose.image.yaml"

TARGET_HEX = "a" * 64
TARGET = f"ghcr.io/qsbb/ling-api@sha256:{TARGET_HEX}"
OTHER_HEX = "d" * 64
OTHER_REF = f"ghcr.io/qsbb/ling-api@sha256:{OTHER_HEX}"
UPDATER_TARGET = f"ghcr.io/qsbb/ling-updater@sha256:{'c' * 64}"
UPDATER_COMPOSE = REPO_ROOT / "deploy" / "updater" / "compose.yaml"
COMMIT = "b" * 40
OTHER_COMMIT = "f" * 40
HOST_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().strip().lower(), platform.machine().strip().lower()
)
OTHER_ARCH = "arm64" if HOST_ARCH == "amd64" else "amd64"
PLATFORM_DIGEST = "e" * 64
HEAD = "0045_turn_control_attempt"
OLD_HEAD = "0044_media_describe_failed"
POSTGRES_IMAGE = "postgres:16-alpine"
# 真机现场的两条 registry 报错（逐字取自 `docker compose pull` 的 stderr 形态）。
DOCKER_HUB_TIMEOUT = (
    'Image postgres:16-alpine Error Get "https://registry-1.docker.io/v2/": '
    "context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
)
GHCR_UNREACHABLE = (
    'Image ghcr.io/qsbb/ling-api Error Get "https://ghcr.io/v2/": '
    "net/http: TLS handshake timeout"
)
# 容器内固定端口 / api 在 compose 里的默认宿主端口。
DEFAULT_PORT = 6520
# 模型里的插值占位符：由 docker stub 按 `--env-file` + 进程环境解析（Compose 口径）。
LING_PORT_TOKEN = "${LING_PORT:-6520}"
LING_HOST_TOKEN = "${LING_HOST:-0.0.0.0}"


# ---------------------------------------------------------------- docker stub

_DOCKER = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import sys

STATE = pathlib.Path(os.environ["STUB_STATE"])
CALL_LOG = pathlib.Path(os.environ["CALL_LOG"])
TARGET = os.environ["STUB_TARGET"]
OTHER_REF = os.environ["STUB_OTHER_REF"]
COMMIT = os.environ["STUB_COMMIT"]
OTHER_COMMIT = os.environ["STUB_OTHER_COMMIT"]
HOST_ARCH = os.environ["STUB_HOST_ARCH"]
OTHER_ARCH = os.environ["STUB_OTHER_ARCH"]
PLATFORM_DIGEST = os.environ["STUB_PLATFORM_DIGEST"]
POSTGRES = os.environ["STUB_POSTGRES_IMAGE"]


def log(message):
    with CALL_LOG.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def read_env_file(path):
    """按 Compose 的 ``--env-file`` 语法读 KEY=VALUE（注释/空行忽略）。"""
    values = {}
    if not path:
        return values
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def interpolate(text, env_file):
    """``${VAR}`` / ``${VAR:-default}`` 的精简复刻（Compose 插值口径）。

    优先级照 Compose：宿主进程环境 > ``--env-file`` > 模板默认值。**故意**做这
    一层，是为了让"预检 env 里有没有 LING_PORT/LING_HOST"真的改变解析出的模型
    ——空 env 会像真机一样落回 6520/0.0.0.0，而不是被一份写死端口的 JSON 掩盖。
    """
    values = read_env_file(env_file)

    def replace(match):
        name = match.group(1)
        default = match.group(2)
        if name in os.environ:
            return os.environ[name]
        if name in values:
            return values[name]
        if default is not None:
            return default
        return ""

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replace, text)


def load():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


MISSING = pathlib.Path(os.environ["STUB_MISSING_IMAGES_FILE"])
WRONG_APP_DIGEST = os.environ.get("STUB_APP_IMAGE_WRONG_DIGEST") == "1"
PARTIAL_PULL = {
    item for item in os.environ.get("STUB_PULL_PARTIAL", "").split(",") if item
}


def missing_images():
    """模拟"本地还没有这些镜像"：inspect 看不到它们，直到某次 pull 成功。"""
    try:
        text = MISSING.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def save_missing(refs):
    MISSING.write_text("\n".join(sorted(refs)) + ("\n" if refs else ""), encoding="utf-8")


def image_ref_for_service(service):
    return POSTGRES if service == "db" else TARGET


def image_for(ref, state):
    if ref in missing_images():
        return None
    if ref == TARGET and WRONG_APP_DIGEST:
        # 本地的同 tag 镜像摘要是**另一个**：inspect 查得到，但 RepoDigests 对不上
        # 目标 digest。这正是"不能把 pull 失败一忽略了之"的安全属性现场。
        return {
            "Id": "sha256:other-image",
            "RepoDigests": [OTHER_REF],
            "Architecture": HOST_ARCH,
            "Os": "linux",
            "Size": 100 * 1024 * 1024,
            "Config": {
                "Env": ["LING_COMMIT=" + OTHER_COMMIT],
                "Labels": {
                    "org.opencontainers.image.revision": OTHER_COMMIT,
                    "org.opencontainers.image.version": "9.9.10",
                },
            },
        }
    if ref in (TARGET, "sha256:target-image"):
        return {
            "Id": "sha256:target-image",
            "RepoDigests": [TARGET],
            "Architecture": HOST_ARCH,
            "Os": "linux",
            "Size": 100 * 1024 * 1024,
            "Config": {
                "Env": ["LING_COMMIT=" + COMMIT],
                "Labels": {
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.version": "9.9.9",
                },
            },
        }
    if ref == "sha256:other-image":
        return {
            "Id": "sha256:other-image",
            "RepoDigests": [OTHER_REF],
            "Architecture": HOST_ARCH,
            "Os": "linux",
            "Size": 100 * 1024 * 1024,
            "Config": {
                "Env": ["LING_COMMIT=" + OTHER_COMMIT],
                "Labels": {
                    "org.opencontainers.image.revision": OTHER_COMMIT,
                    "org.opencontainers.image.version": "9.9.10",
                },
            },
        }
    if ref in (POSTGRES, "sha256:pg-image"):
        return {
            "Id": "sha256:pg-image",
            "RepoDigests": ["postgres@sha256:" + "9" * 64],
            "Architecture": HOST_ARCH,
            "Os": "linux",
            "Size": 80 * 1024 * 1024,
            "Config": {"Env": [], "Labels": {}},
        }
    return None


def emit(value):
    print(json.dumps(value))


def parse_filters(args):
    values = []
    index = 0
    while index < len(args):
        if args[index].startswith("--filter="):
            values.append(args[index].split("=", 1)[1])
            index += 1
            continue
        if args[index] == "--filter" and index + 1 < len(args):
            values.append(args[index + 1])
            index += 2
            continue
        index += 1
    filters = {"label": {}, "publish": None, "name": None}
    for item in values:
        if item.startswith("label="):
            key, value = item[len("label="):].split("=", 1)
            filters["label"][key] = value
        elif item.startswith("publish="):
            filters["publish"] = item[len("publish="):]
        elif item.startswith("name="):
            filters["name"] = item[len("name="):]
    return filters


args = sys.argv[1:]
log("docker " + " ".join(args))

if not args:
    sys.exit(64)

if args[0] == "info":
    fmt = ""
    if "--format" in args:
        fmt = args[args.index("--format") + 1]
    if "ServerVersion" in fmt:
        print(os.environ.get("STUB_SERVER_VERSION", "27.1.1"))
    elif "DockerRootDir" in fmt:
        print(os.environ["STUB_DOCKER_ROOT"])
    sys.exit(0)

if args[0] == "compose" and len(args) > 1 and args[1] == "version":
    print(os.environ.get("STUB_COMPOSE_VERSION", "2.29.1"))
    sys.exit(0)

if args[0] == "manifest":
    mode = os.environ.get("STUB_MANIFEST_MODE", "ok")
    if mode == "fail":
        sys.stderr.write("manifest unknown: denied\n")
        sys.exit(1)
    if mode == "no_host":
        emit({"manifests": [
            {"digest": "sha256:" + "f" * 64,
             "platform": {"os": "linux", "architecture": OTHER_ARCH}},
        ]})
        sys.exit(0)
    emit({"manifests": [
        {"digest": "sha256:" + PLATFORM_DIGEST,
         "platform": {"os": "linux", "architecture": HOST_ARCH}},
        {"digest": "sha256:" + "f" * 64,
         "platform": {"os": "linux", "architecture": OTHER_ARCH}},
    ]})
    sys.exit(0)

if args[0] == "run":
    # 只用于从镜像里读 Alembic head（--entrypoint python ... -c <script>）。
    if os.environ.get("STUB_HEAD_EXIT", "0") != "0":
        sys.stderr.write("alembic missing\n")
        sys.exit(int(os.environ["STUB_HEAD_EXIT"]))
    print("HEAD=" + os.environ["STUB_HEAD"])
    print("REV=" + os.environ["STUB_HEAD_OLD"])
    print("REV=" + os.environ["STUB_HEAD"])
    print("PARENT=" + os.environ["STUB_HEAD"] + ":" + os.environ["STUB_HEAD_OLD"])
    sys.exit(0)

if args[0] == "ps":
    all_states = "-a" in args
    filters = parse_filters(args)
    state = load()
    if filters["publish"] is not None:
        # 单端口（历史用法）或按端口给的映射（{"<port>": owner}）——后者用来
        # 模拟"只有 WS override 端口被别人占着、API 端口空闲"。
        owners = os.environ.get("STUB_PORT_OWNERS")
        owner = None
        if owners:
            port = filters["publish"].split("/", 1)[0]
            entry = json.loads(owners).get(port)
            owner = json.dumps(entry) if entry else None
        else:
            owner = os.environ.get("STUB_PORT_OWNER")
        if owner:
            data = json.loads(owner)
            print("%s|%s|%s" % (data["id"], data["project"], data.get("name", "")))
        sys.exit(0)
    service = filters["label"].get("com.docker.compose.service")
    project = filters["label"].get("com.docker.compose.project")
    rows = []
    for key, entry in state["containers"].items():
        if service is not None and key != service:
            continue
        if project is not None and entry["project"] != project:
            continue
        if not all_states and not entry["running"]:
            continue
        rows.append(entry)
    for entry in rows:
        print("%s|%s|%s" % (entry["id"], entry["name"],
                            "running" if entry["running"] else "exited"))
    sys.exit(0)

if args[0] == "inspect":
    kind = None
    ref = None
    index = 1
    while index < len(args):
        if args[index] == "--type" and index + 1 < len(args):
            kind = args[index + 1]
            index += 2
            continue
        if not args[index].startswith("-"):
            ref = args[index]
        index += 1
    state = load()
    if kind == "container":
        for entry in state["containers"].values():
            if ref == entry["id"]:
                emit([{
                    "Id": entry["id"],
                    "Image": entry["image_id"],
                    # 真 Docker 的 `Config.Labels` / `Mounts` 一定存在；安装器的
                    # 更新器项目归属判据（working_dir + 挂载源）就靠它们。缺省
                    # 空值 = "没有证据指向本安装"，按别人的容器处理。
                    "Config": {
                        "Image": entry["image_ref"],
                        "Labels": entry.get("labels", {}),
                    },
                    "Mounts": entry.get("mounts", []),
                    "State": {
                        "Running": entry["running"],
                        "Status": "running" if entry["running"] else "exited",
                        "Health": {"Status": entry.get("health", "healthy")},
                    },
                }])
                sys.exit(0)
        sys.exit(1)
    if kind == "volume":
        entry = state["volumes"].get(ref)
        if entry is None:
            sys.exit(1)
        emit([entry])
        sys.exit(0)
    sys.exit(64)

if args[0] == "image" and len(args) > 1 and args[1] == "inspect":
    image = image_for(args[2], load()) if len(args) > 2 else None
    if image is None:
        sys.exit(1)
    emit([image])
    sys.exit(0)

if args[0] == "volume" and len(args) > 1 and args[1] == "inspect":
    state = load()
    names = args[2:]
    # 模拟"预检之后才有同名卷"：只为指定名字计数，第 N 次 inspect 时创建它。
    # 安装器修 L2 后会在抢锁后再查一次，从而看见这个刚出现的卷并拒绝。
    appear = os.environ.get("STUB_VOLUME_APPEAR_NAME", "")
    appear_on = int(os.environ.get("STUB_VOLUME_APPEAR_ON_CALL", "0"))
    if appear and appear in names and appear_on > 0:
        counter = STATE.with_name(STATE.name + ".volume-calls")
        count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
        count += 1
        counter.write_text(str(count), encoding="utf-8")
        if count == appear_on and appear not in state["volumes"]:
            state["volumes"][appear] = {
                "Name": appear,
                "Driver": "local",
                "Labels": {"com.docker.compose.project": "ling"},
                "Mountpoint": os.environ["STUB_VOLUME_MOUNTPOINT"],
                "CreatedAt": "2026-01-01T00:00:00Z",
            }
            save(state)
    found = [state["volumes"][name] for name in names if name in state["volumes"]]
    if len(found) != len(names):
        sys.stderr.write("no such volume\n")
        sys.exit(1)
    emit(found)
    sys.exit(0)

if args[0] != "compose":
    sys.exit(64)

subargs = args[1:]
index = 0
subcommand = None
env_file = None
files = []
project = None
while index < len(subargs):
    if subargs[index] in ("-p", "-f", "--env-file") and index + 1 < len(subargs):
        if subargs[index] == "--env-file":
            env_file = subargs[index + 1]
        if subargs[index] == "-f":
            files.append(subargs[index + 1])
        if subargs[index] == "-p":
            project = subargs[index + 1]
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
# `-p` 缺省时 Compose 用目录名；stub 的模型/卷名默认就按 ling 写。
project = project or "ling"

state = load()
log("docker-env %s LING_APP_IMAGE=%s" % (subcommand, os.environ.get("LING_APP_IMAGE", "")))

if subcommand == "config":
    if os.environ.get("STUB_CONFIG_EXIT", "0") != "0":
        sys.stderr.write("compose config failed\n")
        sys.exit(int(os.environ["STUB_CONFIG_EXIT"]))
    if "-q" in subargs:
        sys.exit(0)
    if "--format" in subargs:
        template = pathlib.Path(os.environ["STUB_CONFIG_JSON"]).read_text(encoding="utf-8")
        values = read_env_file(env_file)
        # 记录每次**模型解析**用的 -f 文件（真 compose 会解析它）：测试据此核对
        # 预检解析的是渲染后的副本、且与最终落盘的那份逐字节相同。
        for path in files:
            try:
                body = pathlib.Path(path).read_bytes()
            except OSError:
                body = b""
            log(
                "compose-file %s sha256=%s host_anchor=%s"
                % (
                    path,
                    hashlib.sha256(body).hexdigest(),
                    "${LING_HOST:-127.0.0.1}" in body.decode("utf-8", "replace"),
                )
            )
        # 把 env-file 的内容记进调用日志：测试据此核对**预检** env 到底写了什么
        # （安装器还会把同一批值打到 stderr，两边可互相印证）。
        log("compose-envfile " + " ".join("%s=%s" % item for item in sorted(values.items())))
        # 照 Compose 的 `-p` 规则重写**默认命名**：项目名 + 没有显式 name: 的卷
        # 名 = ``<项目>_<键>``。没有 -p 时就是 ling，与既有用例逐字一致；带
        # ``-p ling-verify`` 时模型/卷名跟着走，才不会让第二个实例去查第一个
        # 实例的卷（真 compose 的行为正是这样）。
        model = json.loads(interpolate(template, env_file))
        default_project = model.get("name") or "ling"
        if project != default_project:
            model["name"] = project
            for entry in (model.get("volumes") or {}).values():
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                prefix = default_project + "_"
                if name.startswith(prefix):
                    entry["name"] = project + "_" + name[len(prefix):]
        sys.stdout.write(json.dumps(model))
    sys.exit(0)

if subcommand == "pull":
    # 逐个服务的 pull：每个服务单独一条（安装器现在只拉"本地没有的"那个）。
    # 失败的**具体服务**由 STUB_PULL_FAIL 指定（JSON: {"db": "<docker 的报错>"}），
    # 用来复刻真机"Docker Hub 连不上、但 GHCR 正常"的现场。
    services = [item for item in subargs if not item.startswith("-")]
    log("compose-pull " + " ".join(services))
    forced = os.environ.get("STUB_PULL_EXIT", "0")
    if forced != "0":
        sys.stderr.write("pull failed\n")
        sys.exit(int(forced))
    failures = json.loads(os.environ.get("STUB_PULL_FAIL", "{}") or "{}")
    failing = [service for service in services if service in failures]
    if failing:
        for service in failing:
            sys.stderr.write(failures[service].rstrip() + "\n")
            if service in PARTIAL_PULL:
                # 模拟"镜像其实已经写完、随后才报错"：现在真的能在本地 inspect 到。
                save_missing(missing_images() - {image_ref_for_service(service)})
        sys.exit(1)
    for service in services:
        save_missing(missing_images() - {image_ref_for_service(service)})
    sys.exit(0)

if subcommand == "up":
    service = subargs[-1] if subargs else ""
    if service in ("db", "api", "worker"):
        if service == "db":
            for key in ("app-data", "db-data"):
                name = project + "_" + key
                state["volumes"].setdefault(name, {
                    "Name": name,
                    "Driver": "local",
                    "Labels": {"com.docker.compose.project": project},
                    "Mountpoint": os.environ["STUB_VOLUME_MOUNTPOINT"],
                    "CreatedAt": "2026-01-01T00:00:00Z",
                })
        image_id = os.environ.get("STUB_APP_IMAGE_ID", "sha256:target-image")
        if service == "db":
            image_id = "sha256:pg-image"
        state["containers"][service] = {
            "id": "c-" + project + "-" + service,
            "name": project + "-" + service + "-1",
            "project": project,
            "running": True,
            "image_id": image_id,
            "image_ref": TARGET if service != "db" else POSTGRES,
            "health": os.environ.get("STUB_" + service.upper() + "_HEALTH", "healthy"),
        }
        save(state)
    sys.exit(int(os.environ.get("STUB_UP_EXIT", "0")))

if subcommand == "run":
    log("migration-run " + " ".join(subargs))
    mode = os.environ.get("STUB_MIGRATION_MODE", "ok")
    if mode == "skip":
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
    elif mode == "both":
        print("[entrypoint] 执行迁移")
        print("[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）")
        state["alembic_version"] = os.environ["STUB_HEAD"]
        save(state)
    elif mode == "stale":
        print("[entrypoint] 执行迁移")
        print("[migrate] 没有前进")
    else:
        print("[entrypoint] 执行迁移")
        print("[migrate] alembic upgrade head 完成")
        state["alembic_version"] = os.environ.get("STUB_MIGRATE_SET_HEAD", os.environ["STUB_HEAD"])
        save(state)
    sys.exit(int(os.environ.get("STUB_MIGRATE_EXIT", "0")))

if subcommand == "exec":
    # 三种只读 exec：
    # - `exec api python -c <ALEMBIC_HEAD_SCRIPT>` 算镜像内 head（doctor）；
    # - `exec api python -c <CONTAINER_VERSION_PROBE>` 让应用自报版本/提交
    #   （安装 verify / status / doctor 的"运行容器是不是目标构建"）；
    # - `exec db psql ...` 读 alembic_version（迁移/状态）。
    script = " ".join(subargs)
    if "python" in subargs:
        if "__version__" in script:
            # 容器内进程证据。stub 只是照搬**容器里会发生什么**：由应用自己的
            # Python 打印一行 JSON；失败/输出不可解析也如实模拟（见 STUB_PROBE_*）。
            exit_code = int(os.environ.get("STUB_PROBE_EXIT", "0"))
            if exit_code != 0:
                sys.stderr.write("ModuleNotFoundError: No module named 'ling'\n")
                sys.exit(exit_code)
            if os.environ.get("STUB_PROBE_GARBAGE") == "1":
                print("warming up; not json")
                sys.exit(0)
            print(json.dumps({
                "version": os.environ.get("STUB_PROBE_VERSION", "9.9.9"),
                "commit": os.environ.get("STUB_PROBE_COMMIT", COMMIT),
            }))
            sys.exit(0)
        if os.environ.get("STUB_HEAD_EXIT", "0") != "0":
            sys.stderr.write("alembic missing\n")
            sys.exit(int(os.environ["STUB_HEAD_EXIT"]))
        print("HEAD=" + os.environ["STUB_HEAD"])
        print("REV=" + os.environ["STUB_HEAD_OLD"])
        print("REV=" + os.environ["STUB_HEAD"])
        print("PARENT=" + os.environ["STUB_HEAD"] + ":" + os.environ["STUB_HEAD_OLD"])
        sys.exit(0)
    # psql -tAc ...：安装器会跑两种查询——
    #   * 就绪探针 `select 1`（真机缺陷：pg_isready healthy 之后才跑这道门禁）；
    #   * `select version_num from alembic_version`（迁移/状态）。
    # STUB_PSQL_FAILURES 按顺序注入失败（JSON 列表，每项一组 stderr 行），
    # 模拟真机现场"空卷首装时 db 还在 entrypoint 临时服务器阶段"：
    #   ["psql: error: … FATAL:  the database system is shutting down", …]
    # 注入项用完之后恢复正常。`STUB_PSQL_ALWAYS_FAIL` 则让每次 psql 都失败，
    # 用来钉住"重试窗口耗尽"的语义。
    failures = json.loads(os.environ.get("STUB_PSQL_FAILURES", "[]") or "[]")
    counter = STATE.with_name(STATE.name + ".psql-calls")
    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    count += 1
    counter.write_text(str(count), encoding="utf-8")
    always = os.environ.get("STUB_PSQL_ALWAYS_FAIL", "")
    fail_after = int(os.environ.get("STUB_PSQL_FAIL_AFTER", "0") or "0")
    if always and count > fail_after:
        message = always
    else:
        message = failures[count - 1] if count <= len(failures) else ""
    if message:
        sys.stderr.write(message + "\n")
        sys.exit(int(os.environ.get("STUB_PSQL_FAIL_EXIT", "2")))
    print(state.get("alembic_version") or "")
    sys.exit(int(os.environ.get("STUB_EXEC_EXIT", "0")))

if subcommand == "logs":
    print("fake-logs " + " ".join(subargs))
    sys.exit(int(os.environ.get("STUB_LOGS_EXIT", "0")))

sys.exit(64)
'''


# ---------------------------------------------------------------- HTTP stub


class _HTTPState:
    def __init__(self) -> None:
        self.readyz_status = 200
        self.readyz_redirect = False
        # 安装/状态/诊断打过的 HTTP 路径（按到达顺序）。用来证明它们只探
        # **免鉴权**端点。这里以前有 `version_status/version_commit/version`：
        # stub 对 `/v1/version` 无条件回 200，而真实应用对它挂了 `CurrentAuth`，
        # 未鉴权永远 401 —— 那正是这个 bug 一直没被发现的原因（假绿）。
        self.requests: list[str] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _HTTPServer

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.http_state
        state.requests.append(self.path)
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
            # `CurrentAuth`，没有会话就是 401。stub 以前在这里回 200，于是
            # "安装器最后一步拿 401"这件事在单元测试里永远抓不到。
            status = 401
            body = json.dumps(
                {"schema_version": "v1", "error": {"code": "unauthorized", "message": "需要登录"}}
            ).encode("utf-8")
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_range(size: int = 2) -> tuple[int, int]:
    """找一段本机连续空闲的 TCP 端口（``published`` 范围的用例要用）。"""
    for _ in range(200):
        start = _free_port()
        end = start + size - 1
        if end > 65535:
            continue
        sockets = []
        try:
            for offset in range(size):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", start + offset))
                sockets.append(sock)
            return start, end
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise AssertionError("找不到连续的空闲 TCP 端口")


class LateReadyz:
    """在安装器越过"拉取阶段"之后才在安装端口上起 HTTP stub。

    安装器的端口检查是真实 bind，预检时端口必须空闲；真机上容器也是 ``up``
    之后才监听。stub 环境没有容器，所以由测试按调用日志在恰当时刻开始监听。

    触发点必须容得下"**一次 pull 都不发**"：现在本地已有镜像时安装器不会
    调用 ``compose pull``（那是"网络受限就装不上"缺陷的修法），只等
    ``compose-pull`` 会永远等不到。``config -q`` 是紧随其后的固定步骤
    （落盘之后、``up db`` 之前），且只在预检之后出现，仍早于任何容器动作。
    """

    def __init__(self, port: int, call_log: Path, state: _HTTPState) -> None:
        self.port = port
        self.call_log = call_log
        self.state = state
        self._server: _HTTPServer | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                text = self.call_log.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if "compose-pull" in text or " config -q" in text:
                break
            self._stop.wait(0.02)
        if self._stop.is_set():
            return
        server = _HTTPServer(("127.0.0.1", self.port), _Handler)
        server.http_state = self.state
        self._server = server
        self._ready.set()
        server.serve_forever()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@contextlib.contextmanager
def late_readyz(ws: Workspace, *, port: int | None = None) -> Any:
    """在（默认安装端口的）``port`` 上，等安装器越过拉取阶段后再提供 /readyz。

    给第二个实例用时要先 ``ws.reset()``：``LateReadyz`` 以调用日志里的
    ``config -q`` 为触发点，日志里若还留着上一个实例的记录，它会**立刻**绑定
    端口，预检的真实 bind 就会撞上自己。
    """
    server = LateReadyz(ws.port if port is None else port, ws.call_log, ws.http)
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------- workspace


@dataclass
class Workspace:
    root: Path
    compose_source: Path
    install_dir: Path
    backup_dir: Path
    port: int
    call_log: Path
    docker_state: Path
    config_path: Path
    env: dict[str, str]
    http: _HTTPState

    def reset(self) -> None:
        self.call_log.write_text("", encoding="utf-8")
        # stub 的 psql 失败注入按调用次数计数；每次 run_ling 都是新的一次运行。
        counter = self.docker_state.with_name(self.docker_state.name + ".psql-calls")
        with contextlib.suppress(FileNotFoundError):
            counter.unlink()

    def calls(self) -> list[str]:
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def state(self) -> dict[str, Any]:
        return json.loads(self.docker_state.read_text(encoding="utf-8"))

    def write_state(self, state: dict[str, Any]) -> None:
        self.docker_state.write_text(json.dumps(state), encoding="utf-8")

    def install_state(self) -> dict[str, Any]:
        return json.loads(
            (self.install_dir / "install-state.json").read_text(encoding="utf-8")
        )


def _write_executable(path: Path, body: str, *, python: bool) -> None:
    if python:
        body = f"#!{sys.executable}\n" + body.split("\n", 1)[1]
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _config_json(
    port: int | str | None = None,
    host: str | None = None,
    extra_ports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``docker compose config --format json`` 的合并后模型（stub 模板）。

    api 的宿主端口/绑定默认写成 ``${LING_PORT:-6520}`` / ``${LING_HOST:-0.0.0.0}``
    占位符，由 docker stub 按 ``--env-file`` + 进程环境**照 Compose 规则插值**
    （见 ``_DOCKER`` 的 config 分支）。这样"预检 env 漏写 LING_PORT"会像真机一样
    落回默认 6520，而不是被一份写死端口的 JSON 掩盖掉——正是本缺陷的现场。
    显式传 ``port``/``host`` 时写死（用来模拟"env 没生效/模型与命令不一致"）。
    """
    return {
        "name": "ling",
        "services": {
            "db": {
                "image": POSTGRES_IMAGE,
                "environment": {},
                "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U ling -d ling"]},
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
                    "LING_PORT": str(DEFAULT_PORT),
                },
                "ports": [
                    {
                        "mode": "ingress",
                        "host_ip": LING_HOST_TOKEN if host is None else host,
                        "target": DEFAULT_PORT,
                        "published": LING_PORT_TOKEN if port is None else str(port),
                        "protocol": "tcp",
                    },
                    *(extra_ports or []),
                ],
                "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request"]},
                "volumes": [{"type": "volume", "source": "ling_app-data", "target": "/data"}],
            },
            "worker": {
                "image": TARGET,
                "environment": {"LING_ENV": "production"},
                "healthcheck": {"test": ["CMD", "python", "-c", "import asyncpg"]},
                "volumes": [{"type": "volume", "source": "ling_app-data", "target": "/data"}],
            },
        },
        "volumes": {
            "db-data": {"name": "ling_db-data"},
            "app-data": {"name": "ling_app-data"},
        },
    }


def write_config(
    ws: Workspace,
    *,
    port: int | str | None = None,
    host: str | None = None,
    extra_ports: list[dict[str, Any]] | None = None,
) -> None:
    """把 stub 的 ``compose config --format json`` 换成指定的合并后模型。

    ``port``/``host`` 省略时保留占位符（照 env-file 插值）；显式传值则写死，
    用来模拟"env 没生效"或"模型与命令不一致"。
    """
    ws.config_path.write_text(
        json.dumps(_config_json(port, host, extra_ports=extra_ports)),
        encoding="utf-8",
    )


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "release"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    compose_source = deploy / "compose.image.yaml"
    shutil.copyfile(REAL_COMPOSE, compose_source)

    install_dir = tmp_path / "opt" / "ling"
    backup_dir = tmp_path / "backups"
    port = _free_port()

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", _DOCKER, python=True)

    call_log = tmp_path / "calls.log"
    call_log.write_text("", encoding="utf-8")
    docker_state = tmp_path / "docker-state.json"
    docker_state.write_text(
        json.dumps(
            {
                "containers": {},
                "volumes": {},
                "alembic_version": None,
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config_json()), encoding="utf-8")
    missing_images = docker_state.with_name(docker_state.name + ".missing-images")
    missing_images.write_text("", encoding="utf-8")
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir()
    volume_mountpoint = tmp_path / "volume-mount"
    volume_mountpoint.mkdir()

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("LING_", "COMPOSE_", "DOCKER_")):
            env.pop(key, None)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CALL_LOG": str(call_log),
            "STUB_STATE": str(docker_state),
            "STUB_CONFIG_JSON": str(config_path),
            "STUB_MISSING_IMAGES_FILE": str(missing_images),
            "STUB_DOCKER_ROOT": str(docker_root),
            "STUB_VOLUME_MOUNTPOINT": str(volume_mountpoint),
            "STUB_TARGET": TARGET,
            "STUB_OTHER_REF": OTHER_REF,
            "STUB_COMMIT": COMMIT,
            "STUB_OTHER_COMMIT": OTHER_COMMIT,
            "STUB_HEAD": HEAD,
            "STUB_HEAD_OLD": OLD_HEAD,
            "STUB_PLATFORM_DIGEST": PLATFORM_DIGEST,
            "STUB_HOST_ARCH": HOST_ARCH,
            "STUB_OTHER_ARCH": OTHER_ARCH,
            "STUB_POSTGRES_IMAGE": POSTGRES_IMAGE,
        }
    )
    http_state = _HTTPState()
    return Workspace(
        root=root,
        compose_source=compose_source,
        install_dir=install_dir,
        backup_dir=backup_dir,
        port=port,
        call_log=call_log,
        docker_state=docker_state,
        config_path=config_path,
        env=env,
        http=http_state,
    )


def build_install_argv(ws: Workspace, extra: list[str] | None = None) -> list[str]:
    argv = [
        sys.executable,
        str(LING_PY),
        "install",
        "--non-interactive",
        "--digest",
        TARGET,
        "--dir",
        str(ws.install_dir),
        "--backup-dir",
        str(ws.backup_dir),
        "--compose-file",
        str(ws.compose_source),
        "--port",
        str(ws.port),
        "--ready-timeout",
        "10",
        "--db-timeout",
        "10",
        "--worker-timeout",
        "10",
        "--migrate-timeout",
        "20",
        "--min-free-bytes",
        "1",
    ]
    argv.extend(extra or [])
    return argv


def run_ling(
    ws: Workspace,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    ws.reset()
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


def run_install(
    ws: Workspace,
    *,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
    readyz: bool = False,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """跑一次 ``ling install``。

    子进程里的 db 重试等待被换成虚拟时间（见 ``no_db_retry_sleep``）：重试的
    **次数/顺序/结论**仍由真实实现决定，只是"秒"不再真的流逝。这样 db 就绪
    用例的断言可以钉在结构事实上，而不受机器快慢影响。
    """
    argv = build_install_argv(ws, extra)
    merged = no_db_retry_sleep(env)
    if not readyz:
        return run_ling(ws, argv, env=merged, timeout=timeout)
    with late_readyz(ws):
        return run_ling(ws, argv, env=merged, timeout=timeout)


def run_status(
    ws: Workspace,
    extra: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(LING_PY),
        "status",
        "--dir",
        str(ws.install_dir),
        "--ready-timeout",
        "5",
    ]
    argv.extend(extra or [])
    return run_ling(ws, argv, env=env)


def compose_calls(ws: Workspace) -> list[str]:
    return [line for line in ws.calls() if line.startswith("docker compose")]


def compose_subcommand(line: str) -> str:
    tokens = line.split()
    index = 2
    while index < len(tokens):
        if tokens[index] in ("-p", "-f", "--env-file"):
            index += 2
            continue
        return tokens[index]
    return ""


def find_call(ws: Workspace, needle: str) -> int:
    for index, line in enumerate(ws.calls()):
        if needle in line:
            return index
    raise AssertionError(f"未找到调用：{needle}\n" + "\n".join(ws.calls()))


def missing_images_path(ws: Workspace) -> Path:
    return ws.docker_state.with_name(ws.docker_state.name + ".missing-images")


def set_missing_images(ws: Workspace, *refs: str) -> None:
    """让 stub 认为这些镜像**本地还没有**（inspect 失败），直到某次 pull 成功。

    这是本批用例的"真机现场"开关：宿主上有没有镜像，只能由
    ``docker image inspect`` 的成败表达——安装器也是这么判定的。
    """
    missing_images_path(ws).write_text(
        "\n".join(refs) + ("\n" if refs else ""), encoding="utf-8"
    )


def pull_services(ws: Workspace) -> list[str]:
    """安装器**真正发起的** pull，按到达顺序返回服务名（一次一条）。

    现在要求逐个服务拉；``pull api worker db`` 一把梭会让受限网络里的
    Docker Hub 超时连坐整个安装——这里就是钉住"只拉缺的那个"的断言点。
    """
    services: list[str] = []
    for line in ws.calls():
        if not line.startswith("docker compose") or " pull " not in line:
            continue
        services.append(line.split(" pull ", 1)[1].strip())
    return services


def assert_no_mutation(ws: Workspace) -> None:
    calls = compose_calls(ws)
    assert not [line for line in calls if compose_subcommand(line) == "pull"], calls
    assert not [line for line in calls if compose_subcommand(line) == "up"], calls
    assert not [line for line in calls if compose_subcommand(line) == "run"], calls


# ---------------------------------------------------------------- 成功路径


def test_noninteractive_install_success_records_identity(ws: Workspace) -> None:
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    env_file = ws.install_dir / ".env"
    state_file = ws.install_dir / "install-state.json"
    compose_file = ws.install_dir / "compose" / "compose.image.yaml"
    assert env_file.is_file() and compose_file.is_file() and state_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(state_file.stat().st_mode) & 0o077 == 0

    env_text = env_file.read_text(encoding="utf-8")
    assert f"LING_APP_IMAGE={TARGET}" in env_text
    assert "LING_HOST=127.0.0.1" in env_text
    assert f"LING_PORT={ws.port}" in env_text
    assert "POSTGRES_PASSWORD=" in env_text
    assert "LING_ROOT_KEY=" in env_text
    assert "LING_BOOTSTRAP_ADMIN=ling" in env_text
    assert "LING_BOOTSTRAP_ADMIN_PASSWORD=" in env_text
    assert "LING_UPDATE_ENABLED=1" not in env_text

    # 生成的 compose 文件组：宿主绑定显式写进 ports，不能沿用 0.0.0.0。
    compose_text = compose_file.read_text(encoding="utf-8")
    assert "${LING_HOST:-127.0.0.1}:${LING_PORT:-6520}:6520" in compose_text
    assert '- "${LING_PORT:-6520}:6520"' not in compose_text
    assert hashlib.sha256(compose_file.read_bytes()).hexdigest() == ws.install_state()[
        "layout"
    ]["compose"]["sha256"]

    state = ws.install_state()
    assert state["status"] == "complete"
    assert state["exit_code"] == 0
    assert state["installer"]["protocol"] == 1
    assert state["installer"]["updater_protocol"] == 1
    assert state["image"]["requested_digest"] == TARGET
    assert state["image"]["index_digest_hex"] == TARGET_HEX
    assert state["image"]["platform_digest"] == PLATFORM_DIGEST
    assert state["image"]["source_commit"] == COMMIT
    assert state["image"]["image_id"] == "sha256:target-image"
    assert state["image"]["architecture"] == HOST_ARCH
    assert state["database"]["target_head"] == HEAD
    assert state["database"]["alembic_version"] == HEAD
    assert state["volumes"]["app-data"] == {
        "name": "ling_app-data",
        "driver": "local",
        "compose_name": "ling_app-data",
        "project_label": "ling",
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert state["volumes"]["db-data"]["name"] == "ling_db-data"
    assert state["containers"]["api"]["image_id"] == "sha256:target-image"
    assert state["containers"]["worker"]["image_id"] == "sha256:target-image"
    assert state["listen"]["host"] == "127.0.0.1"
    assert state["listen"]["port"] == ws.port
    assert state["backup"]["dir"] == str(ws.backup_dir)
    assert state["backup"]["restore_point_ready"] is False
    assert state["update"]["enabled"] is False
    assert state["layout"]["compose"]["files"] == [str(compose_file)]
    assert state["migration"]["executed_marker"] is True
    assert state["migration"]["skip_marker"] is False

    # 调用顺序：预检 config →（没有 pull，三个镜像都在本地）→ config -q → up db
    # → 单次迁移 → up api → up worker。
    #
    # **成功路径不许对已存在的镜像发起 pull**：旧实现无条件
    # `docker compose pull api worker db`，Docker Hub 不可达时整个安装在这里
    # 中断（真机 exit=14），哪怕三个镜像本地全都有。
    lines = ws.calls()
    preflight_config = find_call(ws, "config --format json")
    config_q = find_call(ws, "config -q")
    up_db = find_call(ws, "up -d --no-deps --no-build --pull=never db")
    migrate = find_call(ws, "run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate")
    up_api = find_call(ws, "up -d --no-deps --no-build --pull=never api")
    up_worker = find_call(ws, "up -d --no-deps --no-build --pull=never worker")
    assert pull_services(ws) == []
    assert preflight_config < config_q < up_db < migrate < up_api < up_worker
    migration_calls = [
        line
        for line in lines
        if line.startswith("docker compose") and "LING_RUN_MIGRATIONS=1" in line
    ]
    assert len(migration_calls) == 1
    combined = result.stdout + result.stderr
    assert "跳过拉取 api：本地已能 inspect 到" in combined
    assert f"跳过拉取 db：本地已能 inspect 到 {POSTGRES_IMAGE}" in combined
    assert "本次未发起任何 pull" in combined
    # 落盘之后的 compose 调用一律用安装目录里的文件组与受限 env。
    assert "-f " + str(compose_file) in lines[config_q]
    assert "--env-file " + str(env_file) in lines[config_q]
    for needle in ("up -d --no-deps --no-build --pull=never db", "run --rm --no-deps"):
        line = lines[find_call(ws, needle)]
        assert "-f " + str(compose_file) in line
        assert "--env-file " + str(env_file) in line

    assert not [line for line in lines if " build" in line]
    assert not (ws.backup_dir / ".backup.lock").exists()

    # 摘要：必须说“不可一键更新”，且绝不能出现“更新已启用”。
    assert f"http://127.0.0.1:{ws.port}" in result.stdout
    assert "不可一键更新" in result.stdout
    assert "更新已启用" not in combined
    assert "安装全部完成" not in result.stdout
    # 非交互终端不显示初始口令（也不会写进 stdout/stderr）。
    password = re.search(r"^LING_BOOTSTRAP_ADMIN_PASSWORD=(.+)$", env_text, re.M)
    assert password is not None and password.group(1)
    assert password.group(1) not in combined


# ---------------------------------------------------------------- 按需拉取
#
# 真机现场（改前）：`docker compose pull api worker db` 会去 registry 核对**每个**
# 服务，哪怕镜像就在本地。Docker Hub 连不上时（国内 NAS / 受限网络），只因为
# db 的 `postgres:16-alpine` 就整体 rc=1，把一次本来可以完成的安装阻断：
#
#   Image ghcr.io/qsbb/ling-api@sha256:… Pulled
#   Image postgres:16-alpine Error Get "https://registry-1.docker.io/v2/":
#     context deadline exceeded …                      → exit=14
#
# 修法：逐个镜像判定"本地能不能 inspect 到"，缺哪个才拉哪个；拉取报错时按本地
# 实况复核；进入下一步前统一复核。**应用镜像的身份门禁一条都没放宽**。


def test_db_image_already_local_does_not_block_install(ws: Workspace) -> None:
    """关键回归：db 镜像本地已有 + Docker Hub 不可达 → 安装继续（改前中断）。

    宿主上 `postgres:16-alpine` 就在本地（生产实例的 db 容器就是它），但
    `compose pull` 仍会去 registry-1.docker.io 查一次并超时——改前这一条就把
    整个安装打断（exit=14）。现在 db 的 inspect 成功 ⇒ 压根不发 pull。
    """
    result = run_install(
        ws,
        env={"STUB_PULL_FAIL": json.dumps({"db": DOCKER_HUB_TIMEOUT})},
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == []
    assert "跳过拉取 api" in combined
    assert "跳过拉取 db" in combined
    assert "本次未发起任何 pull" in combined
    assert ws.install_state()["status"] == "complete"
    assert ws.state()["containers"]["db"]["image_ref"] == POSTGRES_IMAGE


def test_app_image_already_local_does_not_block_install(ws: Workspace) -> None:
    """应用镜像本地已有 + registry 不可达 → 安装继续。"""
    result = run_install(
        ws,
        env={"STUB_PULL_FAIL": json.dumps({"api": GHCR_UNREACHABLE, "worker": GHCR_UNREACHABLE})},
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == []
    assert "跳过拉取 api：本地已能 inspect 到" in combined
    assert "跳过拉取 worker" in combined
    assert "与 api 是同一个镜像引用" in combined


def test_missing_app_image_fails_and_names_ghcr(ws: Workspace) -> None:
    """应用镜像本地没有 + registry 不可达 → 失败，点名 api 镜像与 GHCR。"""
    set_missing_images(ws, TARGET)
    result = run_install(
        ws, env={"STUB_PULL_FAIL": json.dumps({"api": GHCR_UNREACHABLE})}
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == ["api"]
    assert "failed_pull/pull_api" in combined
    assert TARGET in combined
    assert "registry ghcr.io" in combined
    assert "docker login ghcr.io" in combined
    assert "同一个 digest" in combined
    # 文案必须如实：pull 阶段已经建了目录与锁，不能说"未做任何变更"。
    assert "未做任何变更" not in combined
    assert "已创建：安装目录" in combined
    assert ".ling-install.lock" in combined
    assert not (ws.install_dir / ".env").exists()
    assert not (ws.install_dir / "install-state.json").exists()


def test_missing_db_image_fails_and_names_docker_hub(ws: Workspace) -> None:
    """db 镜像本地没有 + Docker Hub 不可达 → 失败，点名 postgres 与 Docker Hub。

    并且必须给可执行的下一步：镜像加速 / 预加载 / 换 `LING_POSTGRES_IMAGE`。
    """
    set_missing_images(ws, POSTGRES_IMAGE)
    result = run_install(
        ws, env={"STUB_PULL_FAIL": json.dumps({"db": DOCKER_HUB_TIMEOUT})}
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    # api/worker 本地有 ⇒ 只拉 db（不拉一把梭）。
    assert pull_services(ws) == ["db"]
    assert "failed_pull/pull_db" in combined
    assert POSTGRES_IMAGE in combined
    assert "registry-1.docker.io（Docker Hub）" in combined
    assert "镜像加速" in combined
    assert "LING_POSTGRES_IMAGE" in combined
    assert "未做任何变更" not in combined
    assert not (ws.install_dir / ".env").exists()
    assert not [line for line in compose_calls(ws) if compose_subcommand(line) == "up"]


def test_wrong_digest_local_app_image_is_still_rejected(ws: Workspace) -> None:
    """安全属性不放宽：本地那份应用镜像是**另一个摘要** → 仍然失败。

    这条钉住"不要改成忽略 pull 失败"：跳过了 pull 不等于跳过了身份核验，
    RepoDigests 对不上目标 digest 时安装器必须停在这里，绝不进入部署。
    """
    result = run_install(ws, env={"STUB_APP_IMAGE_WRONG_DIGEST": "1"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == []
    assert "failed_pull/image_digest" in combined
    assert "RepoDigests" in combined
    assert OTHER_REF in combined
    # 没有迁移、没有起容器、没有落 .env。
    assert not [line for line in compose_calls(ws) if compose_subcommand(line) == "up"]
    assert not [line for line in compose_calls(ws) if compose_subcommand(line) == "run"]
    assert not (ws.install_dir / ".env").exists()


def test_only_the_missing_image_is_pulled(ws: Workspace) -> None:
    """只拉缺的那个：db 缺失 → 仅 `pull db`；api/worker 本地有 → 一次都不拉。"""
    set_missing_images(ws, POSTGRES_IMAGE)
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == ["db"]
    assert "已拉取 db" in combined
    assert "跳过拉取 api" in combined
    assert "跳过拉取 worker" in combined
    assert ws.install_state()["status"] == "complete"


def test_missing_app_image_is_pulled_once_and_worker_reuses_it(ws: Workspace) -> None:
    """api/worker 同一个 digest 引用：拉一次 api，worker 不重复拉。"""
    set_missing_images(ws, TARGET)
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == ["api"]
    assert "已拉取 api" in combined
    assert "与 api 是同一个镜像引用" in combined
    assert ws.install_state()["status"] == "complete"


def test_pull_error_with_image_now_present_continues(ws: Workspace) -> None:
    """拉取报错、但镜像现在确实能在本地 inspect 到 → 按本地实况继续。

    不改"忽略 pull 失败"：这条只在**本地已经有这份镜像**时放行；拿不到时
    `test_missing_db_image_fails_and_names_docker_hub` 会失败退出。
    """
    set_missing_images(ws, POSTGRES_IMAGE)
    result = run_install(
        ws,
        env={
            "STUB_PULL_FAIL": json.dumps({"db": DOCKER_HUB_TIMEOUT}),
            "STUB_PULL_PARTIAL": "db",
        },
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert pull_services(ws) == ["db"]
    assert "拉取 db 报错（rc=1）" in combined
    assert "按本地实况继续" in combined
    assert "拉取报错但本地可用" in combined
    assert ws.install_state()["status"] == "complete"


def test_install_does_not_call_compose_5_old(ws: Workspace) -> None:
    """预检的 Compose 版本提示与 doctor 同口径：v5.x 不是"较旧"。

    旧实现只看 minor（5.3.1 的 3 < 20）⇒ 在**更新的** Compose 上打一条"建议升级
    到 2.20+"的误导提示。门禁本身不拦（major≥2），但诊断文字不能说反。
    """
    result = run_install(ws, readyz=True, env={"STUB_COMPOSE_VERSION": "5.3.1"})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "较旧" not in (result.stdout + result.stderr), result.stdout


def test_install_creates_install_lock_and_records_updater_mounts(ws: Workspace) -> None:
    """更新器要单文件挂载这两个文件：安装器必须先把锁预建成 0600 普通文件。"""
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    lock = ws.install_dir / ".ling-install.lock"
    assert lock.is_file()
    assert not lock.is_symlink()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    state = ws.install_state()
    assert state["layout"]["install_lock"] == str(lock)
    mounts = state["layout"]["updater"]["mounts"]
    by_target = {item["target"]: item for item in mounts}
    assert {target: item["mode"] for target, item in by_target.items()} == {
        "/srv/ling/.env": "rw",
        "/srv/ling/.ling-install.lock": "rw",
        "/srv/ling/compose": "ro",
    }
    assert by_target["/srv/ling/.env"]["source"] == str(ws.install_dir / ".env")
    assert by_target["/srv/ling/.ling-install.lock"]["source"] == str(lock)
    assert by_target["/srv/ling/compose"]["source"] == str(ws.install_dir / "compose")
    assert {item["kind"] for item in mounts} == {"file", "dir"}
    assert state["layout"]["updater"]["project"] == "ling-updater"
    assert state["layout"]["updater"]["compose_yaml"] == "deploy/updater/compose.yaml"
    # 只记录挂载，**不**自动拉起更新器。
    assert state["update"]["enabled"] is False
    assert not [line for line in ws.calls() if "ling-updater" in line]


def test_install_fixes_a_too_wide_install_lock(ws: Workspace) -> None:
    """预建的锁必须被校正成 0600（`ling install` 自己创建时不会顺手 chmod）。"""
    ws.install_dir.mkdir(parents=True)
    lock = ws.install_dir / ".ling-install.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)

    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def _shared_group_gid() -> int:
    """安装者能 chgrp 到的非 root 组（非 root 只允许设成自己所属的组）。

    优先挑一个**不等于 egid** 的补充组：否则文件的属组本来就是它，断言就看不出
    `chgrp` 到底有没有发生（变异验证会漏掉"删掉 fchown"这种改动）。
    """
    supplementary = [gid for gid in os.getgroups() if gid not in (0, os.getegid())]
    if supplementary:
        return supplementary[0]
    if os.getegid() != 0:
        return os.getegid()
    pytest.skip("当前用户没有可用的非 root 组，无法模拟共享组")


def _other_updater_uid() -> int:
    return 10003 if os.geteuid() == 10002 else 10002


def _readable_by(info: os.stat_result, uid: int, gid: int, *, need: str = "read") -> bool:
    """按 POSIX 位核算"uid:gid 这个身份"能否读（``need="enter"`` 时能否进入目录）。

    测试机通常不是 root，不能把文件 chown 到 10002；所以断言必须按**实际
    stat** 的属主/属组/权限位核算，而不是假设"安装者 == 更新器"。这正好同时
    覆盖两条路线：属主路线（uid 匹配 + 属主读位）与共享组路线（gid 匹配 +
    组读位）。
    """
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == uid:
        return bool(mode & (0o400 if need == "read" else 0o100))
    if info.st_gid == gid:
        return bool(mode & (0o040 if need == "read" else 0o010))
    return bool(mode & (0o004 if need == "read" else 0o001))


def _open_and_flock(path: Path) -> None:
    """更新器真正要做的事：``O_RDWR`` 打开锁 + flock 非阻塞独占。"""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_install_lock_is_shared_with_updater_group(ws: Workspace) -> None:
    """缺陷 1 回归：更新器是另一个 uid 时，锁必须是 0660 + 属组=共享组。

    0600（连组读都没有）会让更新器的 ``os.open(O_RDWR)`` 直接 EACCES，随后被
    updater.py 归类成 ``install_lock_busy``（"另一个任务正持有安装锁"）——实际
    没有任何进程持锁。
    """
    gid = _shared_group_gid()
    result = run_install(
        ws,
        env={"LING_UPDATER_UID": str(_other_updater_uid()), "LING_UPDATER_GID": str(gid)},
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    lock = ws.install_dir / ".ling-install.lock"
    info = lock.stat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o660
    assert info.st_gid == gid
    # 属主仍是**安装者**：宿主 install.sh / publish.py / ling 以属主身份打开它。
    assert info.st_uid == os.geteuid()
    # 关键回归：组位必须可读写（flock 要 O_RDWR），否则另一个 uid 依旧打不开。
    assert info.st_mode & 0o060 == 0o060
    _open_and_flock(lock)

    state = ws.install_state()
    assert state["layout"]["install_lock_mode"] == "0660"
    assert state["layout"]["install_lock_gid"] == gid
    # 与 compose 的 LING_UPDATE_SHARED_GID 是同一个 gid（不是另编一个）。
    assert state["layout"]["updater"]["update_channel"]["shared_gid"] == gid
    # 锁的属主不再被 chown 给更新器（update 通道的属主表里没有它）。
    ownership = state["updater"]["ownership"]
    assert str(lock) not in ownership["applied"]
    assert str(lock) not in ownership["unchanged"]


def test_install_makes_compose_files_readable_by_updater_uid(ws: Workspace) -> None:
    """缺陷 3 回归：更新器（另一个 uid）必须读得到 compose 文件。

    更新器在容器里对 ``/srv/ling/compose/compose.image.yaml`` 跑
    ``docker compose config``。旧实现只 chown 了 ``compose/`` **目录**，文件仍是
    ``write_file(..., 0o600)`` 的属主安装者 ⇒ 另一个 uid 的更新器 EACCES。
    真实 ``ling install --with-updater`` 因此会装出一个跑不起来的更新器。

    断言按**更新器身份**核算可读性（属主路线或共享组路线都算），并要求：
    不能出现世界权限位（只有安装者 + 更新器可读）；安装记录里的 stat 必须与
    磁盘一致（不是"计划值"）。
    """
    gid = _shared_group_gid()
    updater_uid = _other_updater_uid()
    result = run_install(
        ws,
        env={"LING_UPDATER_UID": str(updater_uid), "LING_UPDATER_GID": str(gid)},
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    compose_dir = ws.install_dir / "compose"
    compose_file = compose_dir / "compose.image.yaml"
    file_info = compose_file.stat()
    file_mode = stat.S_IMODE(file_info.st_mode)
    assert file_mode & 0o007 == 0, f"compose 文件出现世界权限位：{file_mode:04o}"
    assert _readable_by(file_info, updater_uid, gid), (
        f"更新器 {updater_uid}:{gid} 读不了 {compose_file}："
        f"mode={file_mode:04o} uid={file_info.st_uid} gid={file_info.st_gid}"
    )
    dir_info = compose_dir.stat()
    dir_mode = stat.S_IMODE(dir_info.st_mode)
    assert dir_mode & 0o007 == 0, f"compose 目录出现世界权限位：{dir_mode:04o}"
    assert _readable_by(dir_info, updater_uid, gid, need="enter"), (
        f"更新器 {updater_uid}:{gid} 进不了 {compose_dir}："
        f"mode={dir_mode:04o} uid={dir_info.st_uid} gid={dir_info.st_gid}"
    )
    # 两条合法路线之一：属主就是更新器（属主读位），或属组是共享组（组读位）。
    assert (file_info.st_uid == updater_uid and file_mode & 0o400) or (
        file_info.st_gid == gid and file_mode & 0o040
    ), (
        f"{compose_file} 既不归更新器所有、属组也不是共享组："
        f"mode={file_mode:04o} uid={file_info.st_uid} gid={file_info.st_gid}"
    )

    state = ws.install_state()
    record = state["updater"]["ownership"]["compose"]
    assert record["route"] in ("owner", "shared_group"), record
    assert record["stat"][str(compose_file)] == {
        "mode": f"{file_mode:04o}",
        "uid": file_info.st_uid,
        "gid": file_info.st_gid,
    }, (record["stat"].get(str(compose_file)), file_info)
    assert str(compose_file) in record["applied"] + record["unchanged"], record


def test_install_lock_stays_owner_only_without_updater_identity(ws: Workspace) -> None:
    """没配更新器（普通用户安装，更新器与安装者同一 uid）：锁保持 0600。"""
    if os.geteuid() == 0:
        pytest.skip("root 安装会解析出 10002:10002，不属于「同一 uid」场景")
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    lock = ws.install_dir / ".ling-install.lock"
    info = lock.stat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_mode & 0o060 == 0
    assert info.st_uid == os.geteuid()
    _open_and_flock(lock)

    state = ws.install_state()
    assert state["layout"]["install_lock_mode"] == "0600"
    assert state["layout"]["install_lock_gid"] is None
    assert "同一 uid" in state["layout"]["install_lock_share"]


def test_install_precreates_bind_sources_with_the_right_types(ws: Workspace) -> None:
    """缺陷 2：Docker 会把**缺失**的 bind 源建成目录，安装器必须先建对类型。

    ``.env`` / ``.ling-install.lock`` 必须是普通文件（compose 的 --env-file 与
    flock 都读不了目录），``compose/`` 必须是目录且含主文件（更新器只读挂载）。
    """
    ws.install_dir.mkdir(parents=True)  # 安装根在，三者都缺（正是不存在时 Docker 会建目录的时刻）
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    env_file = ws.install_dir / ".env"
    lock = ws.install_dir / ".ling-install.lock"
    compose_dir = ws.install_dir / "compose"
    compose_file = compose_dir / "compose.image.yaml"

    env_info = env_file.stat()
    assert stat.S_ISREG(env_info.st_mode), ".env 是普通文件，不是 Docker 建的空目录"
    assert stat.S_IMODE(env_info.st_mode) == 0o600
    assert stat.S_ISREG(lock.stat().st_mode)
    compose_info = compose_dir.stat()
    assert stat.S_ISDIR(compose_info.st_mode)
    assert stat.S_IMODE(compose_info.st_mode) == 0o700
    assert stat.S_ISREG(compose_file.stat().st_mode), "compose/ 里必须有主文件"
    assert ws.install_state()["layout"]["compose"]["files"] == [str(compose_file)]


def test_install_refuses_env_that_is_a_directory(ws: Workspace) -> None:
    """``.env`` 已被 Docker 建成目录：失败 + 可执行的修复指引，不静默继续。"""
    ws.install_dir.mkdir(parents=True)
    env_dir = ws.install_dir / ".env"
    env_dir.mkdir(mode=0o700)

    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert str(env_dir) in combined
    assert "Docker" in combined
    assert f"rmdir {env_dir}" in combined
    # 没有"静默继续"：不建 compose 文件组、不碰 docker、也不替用户删目录。
    assert env_dir.is_dir()
    assert not (ws.install_dir / "compose").exists()
    assert ws.calls() == []


def test_install_refuses_lock_that_is_a_directory(ws: Workspace) -> None:
    """``.ling-install.lock`` 被 Docker 建成目录：同样失败并给出 rmdir 指引。"""
    ws.install_dir.mkdir(parents=True)
    lock_dir = ws.install_dir / ".ling-install.lock"
    lock_dir.mkdir(mode=0o700)

    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert str(lock_dir) in combined
    assert f"rmdir {lock_dir}" in combined
    assert lock_dir.is_dir()
    assert ws.calls() == []


def test_install_adopts_empty_docker_created_compose_dir(ws: Workspace) -> None:
    """``compose/`` 被 Docker 建**空**：安装器直接接管并写入文件组。"""
    ws.install_dir.mkdir(parents=True)
    compose_dir = ws.install_dir / "compose"
    compose_dir.mkdir(mode=0o700)

    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "接管 Docker 预建的空 compose 目录" in result.stderr
    assert compose_dir.is_dir()
    assert (compose_dir / "compose.image.yaml").is_file()
    assert ws.install_state()["layout"]["compose"]["files"] == [
        str(compose_dir / "compose.image.yaml")
    ]


def test_install_still_refuses_non_empty_compose_dir(ws: Workspace) -> None:
    """空目录可以接管；**有内容**的 compose 目录仍然拒绝（绝不覆盖别人的部署）。"""
    compose_dir = ws.install_dir / "compose"
    compose_dir.mkdir(parents=True)
    marker = compose_dir / "compose.image.yaml"
    marker.write_text("# 别的实例\n", encoding="utf-8")

    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "拒绝覆盖" in result.stderr
    assert marker.read_text(encoding="utf-8") == "# 别的实例\n"
    assert ws.calls() == []


def test_install_state_never_enables_web_update(ws: Workspace) -> None:
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    state_text = (ws.install_dir / "install-state.json").read_text(encoding="utf-8")
    env_text = (ws.install_dir / ".env").read_text(encoding="utf-8")
    compose_text = (ws.install_dir / "compose" / "compose.image.yaml").read_text(
        encoding="utf-8"
    )
    for text in (state_text, env_text, compose_text):
        assert "LING_UPDATE_ENABLED=1" not in text
        assert '"enabled": true' not in text
    state = json.loads(state_text)
    assert state["update"] == {
        "enabled": False,
        "protocol": 1,
        "reason": "网页更新执行器尚未实现（设计 §7 第 6 项）；本次安装未启用",
    }
    combined = result.stdout + result.stderr
    assert "更新已启用" not in combined
    assert "不可一键更新" in result.stdout


# ---------------------------------------------------------------- 更新器准备


def _updater_env_lines(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_install_prepares_updater_dirs_and_env_without_digest(ws: Workspace) -> None:
    """缺 --updater-digest：目录/env 照建，但**不写假摘要**，网页更新不可用。

    特别注意：**缺更新器镜像时仍然要建更新通道的两个宿主目录**——它们是
    `compose.image.yaml` 的必填挂载源，之后装好镜像再挂载时缺目录会直接起不来。
    """
    socket_path = ws.root / "run" / "docker.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.write_bytes(b"")
    socket_gid = os.stat(socket_path).st_gid
    result = run_install(ws, extra=["--docker-socket", str(socket_path)], readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    updater_dir = ws.install_dir / "updater"
    requests_dir = updater_dir / "requests"
    archive_dir = requests_dir / "archive"
    state_dir = updater_dir / "state"
    env_file = updater_dir / ".env"
    # 权限方案（P5 接线）：父目录 0700；requests 2770（共享组可写 + setgid）；
    # archive 2750（只有更新器写，API 只 stat）；state 0750（API 只读 status.json）。
    for path, expected in (
        (updater_dir, 0o700),
        (requests_dir, 0o2770),
        (archive_dir, 0o2750),
        (state_dir, 0o0750),
    ):
        assert path.is_dir(), (path, "缺更新器镜像时也必须建目录")
        actual = stat.S_IMODE(path.stat().st_mode)
        assert actual == expected, (path, oct(actual), oct(expected))
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    lock = ws.install_dir / ".ling-install.lock"
    assert lock.is_file() and not lock.is_symlink()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    values = _updater_env_lines(env_file)
    for key in (
        "LING_INSTALL_DIR",
        "LING_DOCKER_SOCKET",
        "LING_DOCKER_GID",
        "LING_UPDATER_REQUESTS_DIR",
        "LING_UPDATER_STATE_DIR",
        "LING_BACKUP_DIR",
        "LING_UPDATER_CODE_DIR",
        "LING_UPDATER_UID",
        "LING_UPDATER_GID",
    ):
        assert key in values, (key, values)
    assert values["LING_INSTALL_DIR"] == str(ws.install_dir)
    assert values["LING_UPDATER_REQUESTS_DIR"] == str(requests_dir)
    assert values["LING_UPDATER_STATE_DIR"] == str(state_dir)
    assert values["LING_BACKUP_DIR"] == str(ws.backup_dir)
    assert values["LING_UPDATER_CODE_DIR"] == str(REPO_ROOT / "deploy")
    assert values["LING_DOCKER_SOCKET"] == str(socket_path)
    assert values["LING_DOCKER_GID"] == str(socket_gid)
    # 缺 digest：不写假的 digest、也不写空值；只留注释说明。
    assert "LING_UPDATER_IMAGE" not in values
    env_text = env_file.read_text(encoding="utf-8")
    # 注释里可以出现仓库名，但**不能**出现任何赋值或 64 位 hex 的“看起来像真的”摘要。
    assert not [
        line for line in env_text.splitlines() if line.startswith("LING_UPDATER_IMAGE=")
    ]
    assert not re.search(r"@sha256:[0-9a-f]{64}", env_text)
    assert "LING_UPDATE_ENABLED=1" not in env_text

    state = ws.install_state()
    updater = state["updater"]
    assert updater["image"] is None
    assert updater["image_configured"] is False
    assert updater["requested"] is False
    assert updater["started"] is False
    assert updater["protocol"] == 1
    assert "网页更新不可用" in updater["reason"]
    assert state["update"]["enabled"] is False
    layout_updater = state["layout"]["updater"]
    assert layout_updater["project"] == "ling-updater"
    assert layout_updater["requests_dir"] == str(requests_dir)
    assert layout_updater["state_dir"] == str(state_dir)
    assert layout_updater["env_file"] == str(env_file)
    assert layout_updater["docker"]["socket"] == str(socket_path)
    assert layout_updater["docker"]["socket_source"] == "argument"
    assert layout_updater["docker"]["gid"] == socket_gid
    assert layout_updater["docker"]["gid_source"] == "stat"
    assert layout_updater["user"]["uid"] >= 1
    assert layout_updater["user"]["gid"] >= 1
    # 缺摘要 / 未用 --with-updater：**绝不**拉起 ling-updater 项目。
    assert not [line for line in ws.calls() if "-p ling-updater" in line]

    combined = result.stdout + result.stderr
    assert "网页更新不可用" in result.stdout
    assert "更新已启用" not in combined


def test_install_wires_update_channel_host_dirs_and_shared_group(ws: Workspace) -> None:
    """P5 接线：安装器算出两个宿主目录写进 .env，并把权限设成共享组方案。

    `compose.image.yaml` 的两条更新通道挂载是**必填**插值，所以 `.env` 必须给出
    宿主路径；api 的 `group_add` 用 `LING_UPDATE_SHARED_GID`。

    共享组选**更新器 gid**（而不是 API 镜像的 10001）：创建目录的人（安装器）
    天然属于这个组，非 root 安装也能设上——`chown` 到 10001 会 EPERM（本用例
    在非 root 下运行，requests 目录的真实 gid 就是安装者的组）。requests 带
    setgid，API 写出的 0640 请求文件因此继承共享组，更新器按组读得到。
    """
    result = run_install(
        ws,
        extra=["--updater-digest", UPDATER_TARGET, "--docker-gid", "999"],
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    requests_dir = ws.install_dir / "updater" / "requests"
    archive_dir = requests_dir / "archive"
    state_dir = ws.install_dir / "updater" / "state"
    state = ws.install_state()
    channel = state["layout"]["updater"]["update_channel"]
    updater_uid = state["layout"]["updater"]["user"]["uid"]
    updater_gid = state["layout"]["updater"]["user"]["gid"]
    shared_gid = channel["shared_gid"]

    # 共享组就是更新器的 gid（两种安装方式都成立：root 10002 / 安装者 gid）。
    assert shared_gid == updater_gid, (shared_gid, updater_gid)
    assert channel["requests_host"] == str(requests_dir)
    assert channel["status_host"] == str(state_dir)
    assert channel["modes"] == {"requests": "2770", "archive": "2750", "status": "0750"}

    app_env = _updater_env_lines(ws.install_dir / ".env")
    assert app_env["LING_UPDATE_REQUESTS_HOST"] == str(requests_dir)
    assert app_env["LING_UPDATE_STATUS_HOST"] == str(state_dir)
    assert app_env["LING_UPDATE_SHARED_GID"] == str(shared_gid)
    # 必填插值要的是**宿主绝对路径**，相对路径会按 compose 项目目录解析到别处。
    assert app_env["LING_UPDATE_REQUESTS_HOST"].startswith("/")
    assert app_env["LING_UPDATE_STATUS_HOST"].startswith("/")
    # 开关仍未打开：接线 ≠ 启用。
    assert "LING_UPDATE_ENABLED" not in app_env
    compose_text = (ws.install_dir / "compose" / "compose.image.yaml").read_text(
        encoding="utf-8"
    )
    for needle in (
        "${LING_UPDATE_REQUESTS_HOST:",
        "${LING_UPDATE_STATUS_HOST:",
        "${LING_UPDATE_SHARED_GID:",
    ):
        assert needle in compose_text, (needle, "生成的 compose 副本丢了必填插值")

    # 真实 stat：模式位 + 属主/属组（失败时把实际值打出来，别只说"不等"）。
    for path, expected_mode in (
        (requests_dir, 0o2770),
        (archive_dir, 0o2750),
        (state_dir, 0o0750),
    ):
        info = path.stat()
        actual = stat.S_IMODE(info.st_mode)
        assert actual == expected_mode, (
            f"{path} stat: mode={oct(actual)} uid={info.st_uid} gid={info.st_gid}；"
            f"期望 mode={oct(expected_mode)}"
        )
        assert (info.st_uid, info.st_gid) == (updater_uid, updater_gid), (
            f"{path} 属主应为更新器 uid/gid {updater_uid}:{updater_gid}，"
            f"实际 {info.st_uid}:{info.st_gid}"
        )
        assert info.st_gid == shared_gid
    requests_info = requests_dir.stat()
    assert requests_info.st_mode & stat.S_ISGID, (
        "requests 必须带 setgid：否则 API 写出的请求文件不会继承共享组"
    )
    assert not (state_dir.stat().st_mode & stat.S_ISGID)
    # 记录下来的 stat 必须与磁盘一致（状态文件是排查依据，不能是"计划值"）。
    assert channel["stat"]["requests"] == {
        "mode": "2770",
        "uid": requests_info.st_uid,
        "gid": requests_info.st_gid,
    }
    assert channel["stat"]["state"]["mode"] == "0750"
    assert channel["stat"]["archive"]["mode"] == "2750"
    assert channel["setgid"] is True
    assert "共享组" in channel["note"]


def test_install_env_provides_every_required_interpolation(ws: Workspace) -> None:
    """生成的 `.env` 必须为 compose 里每条 `${VAR:?...}` 提供值。

    stub 的 `docker compose config` 返回固定 JSON、**不做真实插值**，所以"安装器
    少写一个必填变量"在这里只能靠这条静态核对发现（真机上 `config` 会直接失败）。
    更新通道的三个变量（两个宿主路径 + 共享组）正是本批新增的必填项。
    """
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    compose_text = (ws.install_dir / "compose" / "compose.image.yaml").read_text(
        encoding="utf-8"
    )
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", compose_text))
    for key in (
        "LING_UPDATE_REQUESTS_HOST",
        "LING_UPDATE_STATUS_HOST",
        "LING_UPDATE_SHARED_GID",
    ):
        assert key in required, (key, "compose 里应保持必填插值")
    env_values = _updater_env_lines(ws.install_dir / ".env")
    missing = sorted(required - set(env_values))
    assert not missing, (missing, "安装器没给这些必填插值写值；真机 config 会失败")


def test_install_updater_env_records_digest_but_does_not_start_by_default(
    ws: Workspace,
) -> None:
    result = run_install(ws, extra=["--updater-digest", UPDATER_TARGET], readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    values = _updater_env_lines(ws.install_dir / "updater" / ".env")
    assert values["LING_UPDATER_IMAGE"] == UPDATER_TARGET
    state = ws.install_state()
    assert state["updater"]["image"] == UPDATER_TARGET
    assert state["updater"]["image_configured"] is True
    assert state["updater"]["requested"] is False
    assert state["updater"]["started"] is False
    assert state["update"]["enabled"] is False
    # 默认不自动拉起：更新器存在是网页更新的前提，但网页更新尚未正式支持。
    assert not [line for line in ws.calls() if "-p ling-updater" in line]
    assert "未拉起" in result.stdout
    assert "更新已启用" not in (result.stdout + result.stderr)


def test_install_with_updater_starts_the_separate_compose_project(ws: Workspace) -> None:
    result = run_install(
        ws,
        extra=[
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-gid",
            "999",
        ],
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    updater_env = ws.install_dir / "updater" / ".env"
    calls = [line for line in ws.calls() if "docker compose" in line and "-p ling-updater" in line]
    assert len(calls) == 1, calls
    line = calls[0]
    assert "-f " + str(UPDATER_COMPOSE) in line
    assert "--env-file " + str(updater_env) in line
    assert line.rstrip().endswith("up -d")
    # 顺序：先应用起来（worker up），最后才拉更新器。
    assert find_call(ws, "up -d --no-deps --no-build --pull=never worker") < find_call(
        ws, "-p ling-updater"
    )

    values = _updater_env_lines(updater_env)
    assert values["LING_UPDATER_IMAGE"] == UPDATER_TARGET
    assert values["LING_DOCKER_GID"] == "999"
    state = ws.install_state()
    assert state["status"] == "complete"
    assert state["updater"]["requested"] is True
    assert state["updater"]["started"] is True
    assert state["updater"]["started_at"]
    assert state["updater"]["compose_argv"][-2:] == ["up", "-d"]
    assert state["layout"]["updater"]["docker"]["gid"] == 999
    assert state["layout"]["updater"]["docker"]["gid_source"] == "explicit"
    assert state["update"]["enabled"] is False
    assert "更新已启用" not in (result.stdout + result.stderr)


def test_install_with_updater_requires_a_pinned_digest(ws: Workspace) -> None:
    result = run_install(ws, extra=["--with-updater", "--docker-gid", "999"])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "--updater-digest" in result.stderr
    assert not ws.install_dir.exists()
    assert not [line for line in ws.calls() if "docker" in line]


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "ghcr.io/qsbb/ling-updater:latest",
        f"ghcr.io/qsbb/ling-updater@sha256:{'c' * 63}",
        f"ghcr.io/qsbb/ling-api@sha256:{'a' * 64}",
    ],
)
def test_updater_digest_must_be_pinned(ws: Workspace, digest: str) -> None:
    result = run_install(ws, extra=["--updater-digest", digest])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "更新器" in result.stderr


def test_install_never_invents_a_docker_gid_when_socket_is_missing(ws: Workspace) -> None:
    """socket 不存在：gid 记 None、env 里只留注释——**绝不**写成 0/root 兜底。"""
    missing = ws.root / "no-such-docker.sock"
    result = run_install(ws, extra=["--docker-socket", str(missing)], readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    state = ws.install_state()
    assert state["layout"]["updater"]["docker"]["socket"] == str(missing)
    assert state["layout"]["updater"]["docker"]["gid"] is None
    assert state["layout"]["updater"]["docker"]["gid_source"] == "unavailable"
    values = _updater_env_lines(ws.install_dir / "updater" / ".env")
    assert "LING_DOCKER_GID" not in values
    assert "回退 root" in (ws.install_dir / "updater" / ".env").read_text(encoding="utf-8")


def test_install_with_updater_refuses_unknown_socket_gid(ws: Workspace) -> None:
    missing = ws.root / "no-such-docker.sock"
    result = run_install(
        ws,
        extra=[
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-socket",
            str(missing),
        ],
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "gid" in result.stderr
    assert not ws.install_dir.exists()


def test_install_rejects_root_as_updater_user(ws: Workspace) -> None:
    result = run_install(ws, env={"LING_UPDATER_UID": "0"})
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "root" in result.stderr
    assert not ws.install_dir.exists()


def test_install_with_updater_never_enables_web_update(ws: Workspace) -> None:
    result = run_install(
        ws,
        extra=["--updater-digest", UPDATER_TARGET, "--with-updater", "--docker-gid", "1"],
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    state_text = (ws.install_dir / "install-state.json").read_text(encoding="utf-8")
    app_env = (ws.install_dir / ".env").read_text(encoding="utf-8")
    updater_env = (ws.install_dir / "updater" / ".env").read_text(encoding="utf-8")
    for text in (combined, state_text, app_env, updater_env):
        assert "LING_UPDATE_ENABLED=1" not in text
        assert "更新已启用" not in text
    state = json.loads(state_text)
    assert state["update"]["enabled"] is False
    assert state["update"]["protocol"] == 1
    # 更新器起了，但摘要里只能出现「仍未启用」这类诚实表述。
    assert "网页更新仍未启用" in result.stdout


# ---------------------------------------------------------------- 更新器项目名派生 / 冲突守卫


def updater_up_calls(ws: Workspace) -> list[tuple[str, str]]:
    """更新器项目的 ``up -d`` 调用：(项目名, 完整命令行)。

    只认 ``--env-file`` 指向 ``<安装根>/updater/.env`` 的 compose 调用——应用
    项目的 ``up`` 也用 ``-p``，不能混在一起数。
    """
    found: list[tuple[str, str]] = []
    for line in compose_calls(ws):
        if compose_subcommand(line) != "up":
            continue
        tokens = line.split()
        if not any(token.endswith("/updater/.env") for token in tokens):
            continue
        found.append((tokens[tokens.index("-p") + 1], line))
    return found


def foreign_updater_container(owner_dir: Path) -> dict[str, Any]:
    """`docker inspect` 形态的**另一个安装**的更新器容器（挂载/工作目录指向它）。"""
    return {
        "id": "c-updater-foreign",
        "name": "ling-updater-updater-1",
        "project": "ling-updater",
        "running": True,
        "image_id": "sha256:updater-image",
        "image_ref": UPDATER_TARGET,
        "health": "healthy",
        "labels": {
            "com.docker.compose.project.working_dir": str(owner_dir / "deploy" / "updater")
        },
        "mounts": [
            {
                "Type": "bind",
                "Source": str(owner_dir / "updater" / ".env"),
                "Destination": "/srv/ling/.env",
            }
        ],
    }


def test_updater_project_is_derived_from_the_app_project() -> None:
    """派生函数与记录优先级（纯函数层，不跑 docker）。"""
    ling = _load_module("ling_cli_updater_project", LING_PY)
    # 默认项目必须与旧常量逐字相同：现有部署仍叫 ling-updater。
    assert ling.updater_project_for("ling") == "ling-updater"
    assert ling.updater_project_for("ling-verify") == "ling-verify-updater"
    assert not hasattr(ling, "UPDATER_PROJECT"), "写死的项目名常量必须去掉"
    # 记录优先：有 updater.project 就照它，不重新派生。
    assert (
        ling.updater_project_of(
            {"project": "ling", "updater": {"project": "custom-updater"}}
        )
        == "custom-updater"
    )
    assert ling.updater_project_of({"project": "ling-verify"}) == "ling-verify-updater"
    assert ling.updater_project_of(None) == "ling-updater"
    layout = ling.Layout(Path("/tmp/ling-verify"), "ling-verify")
    assert ling.updater_mounts(layout)["project"] == "ling-verify-updater"


def test_two_updater_installs_use_distinct_compose_projects(ws: Workspace) -> None:
    """核心回归：第二个 --with-updater 安装必须用自己的项目名。

    改前两套安装都是 ``-p ling-updater``：Compose 看到同一项目名、不同
    ``--env-file``，会**重建**第一个实例的更新器容器（挂载改成第二个实例的
    路径），A 的网页更新于是静默操作 B 的数据。这里断言两次 ``up`` 用的
    ``-p`` 实参集合**没有重复**。
    """
    first = run_install(
        ws,
        extra=[
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-gid",
            "999",
        ],
        readyz=True,
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_ups = updater_up_calls(ws)
    assert [project for project, _line in first_ups] == ["ling-updater"], first_ups
    assert ws.install_state()["updater"]["project"] == "ling-updater"

    # 第二个实例是**空白库**（自己的卷），stub 的 db 记录要跟着重置。
    docker_state = ws.state()
    docker_state["alembic_version"] = None
    ws.write_state(docker_state)

    second_dir = ws.root / "opt" / "ling-verify"
    second_port = _free_port()
    second_argv = build_install_argv(
        ws,
        [
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-gid",
            "999",
            "--project",
            "ling-verify",
            "--dir",
            str(second_dir),
            "--backup-dir",
            str(ws.root / "backups-verify"),
            "--port",
            str(second_port),
        ],
    )
    ws.reset()
    with late_readyz(ws, port=second_port) as server:
        second = run_ling(ws, second_argv)
        assert server.wait_ready(10.0)
    assert second.returncode == 0, (second.stdout, second.stderr)

    second_ups = updater_up_calls(ws)
    assert [project for project, _line in second_ups] == [
        "ling-verify-updater"
    ], second_ups
    assert "ling-verify-updater" in second.stdout
    state = json.loads((second_dir / "install-state.json").read_text(encoding="utf-8"))
    assert state["project"] == "ling-verify"
    assert state["updater"]["project"] == "ling-verify-updater"
    assert state["layout"]["updater"]["project"] == "ling-verify-updater"
    # 两套安装用过的更新器项目名没有任何重复：不会互相重建对方容器。
    used = [project for project, _line in first_ups + second_ups]
    assert len(used) == len(set(used)) == 2, used


def test_install_refuses_updater_project_owned_by_another_install(
    ws: Workspace,
) -> None:
    """冲突守卫：派生项目已有**别人**的容器 → 预检就拒绝（rc 非 0）。"""
    other_dir = ws.root / "another-install"
    docker_state = ws.state()
    docker_state["containers"]["updater"] = foreign_updater_container(other_dir)
    ws.write_state(docker_state)

    result = run_install(
        ws,
        extra=[
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-gid",
            "999",
        ],
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "已属于另一个安装" in result.stderr, result.stderr
    assert str(other_dir) in result.stderr, result.stderr
    assert "换 --project" in result.stderr or "ling-verify" in result.stderr, result.stderr
    # 在预检就停：不建安装目录，也不发任何 compose 调用（更不会 up 别人的项目）。
    assert not ws.install_dir.exists()
    assert not compose_calls(ws), compose_calls(ws)


def test_install_allows_updater_project_of_the_same_install(ws: Workspace) -> None:
    """同一安装目录的残留容器不算冲突（判据看挂载，不看共享的 working_dir）。"""
    leftover = foreign_updater_container(ws.install_dir)
    # 真 Compose 的 working_dir 是**共享代码目录**（所有实例一样），单看它会把
    # 自己的容器误判成别人的；这里刻意保持这一点，只让挂载指向本安装目录。
    leftover["labels"] = {
        "com.docker.compose.project.working_dir": "/srv/ling-code/deploy/updater"
    }
    docker_state = ws.state()
    docker_state["containers"]["updater"] = leftover
    ws.write_state(docker_state)

    result = run_install(
        ws,
        extra=[
            "--updater-digest",
            UPDATER_TARGET,
            "--with-updater",
            "--docker-gid",
            "999",
        ],
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert [project for project, _line in updater_up_calls(ws)] == ["ling-updater"]


# ---------------------------------------------------------------- 预检门禁


@pytest.mark.parametrize("case", ["project", "volume"])
def test_existing_project_or_volume_is_refused(ws: Workspace, case: str) -> None:
    state = ws.state()
    if case == "project":
        state["containers"]["api"] = {
            "id": "c-old",
            "name": "ling-api-1",
            "project": "ling",
            "running": True,
            "image_id": "sha256:target-image",
            "image_ref": TARGET,
            "health": "healthy",
        }
    else:
        state["volumes"]["ling_app-data"] = {
            "Name": "ling_app-data",
            "Driver": "local",
            "Labels": {"com.docker.compose.project": "ling"},
            "Mountpoint": str(ws.root),
            "CreatedAt": "2026-01-01T00:00:00Z",
        }
    before = json.dumps(state, sort_keys=True)
    ws.write_state(state)

    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "现有实例" in result.stderr
    assert "迁移" in result.stderr
    assert_no_mutation(ws)
    # 没有创建任何容器/卷：stub 状态未被安装器改动。
    assert json.dumps(ws.state(), sort_keys=True) == before
    assert not (ws.install_dir / ".env").exists()
    assert not (ws.install_dir / "install-state.json").exists()


def test_instance_appearing_after_lock_is_refused(ws: Workspace) -> None:
    """预检时卷不存在、抢锁后才发现同名卷：必须失败并说明是"预检之后"。

    L2 的并发窗口：``check_no_existing_instance`` 只在预检里查一次，抢锁后只复核
    ``.env``/状态/compose 目录。stub 让 ``ling_app-data`` 在**第二次** inspect 时
    才出现（第一次是预检，第二次是抢锁后的复核）；不修则安装器看不见它，会继续
    往下走（rc != 10），修了则必须停在预检失败并给出"预检之后出现了同名资源"。
    """
    result = run_install(
        ws,
        env={
            "STUB_VOLUME_APPEAR_NAME": "ling_app-data",
            "STUB_VOLUME_APPEAR_ON_CALL": "2",
        },
    )
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "预检之后出现了同名资源" in result.stderr, result.stderr
    assert "ling_app-data" in result.stderr
    assert "迁移" in result.stderr
    assert_no_mutation(ws)
    assert "ling_app-data" in ws.state()["volumes"]  # 由 stub 模拟"别人刚建"
    assert not (ws.install_dir / ".env").exists()
    assert not (ws.install_dir / "install-state.json").exists()
    assert not (ws.install_dir / "compose").exists()


def test_existing_env_is_never_overwritten(ws: Workspace) -> None:
    ws.install_dir.mkdir(parents=True)
    env_file = ws.install_dir / ".env"
    env_file.write_text("MARKER=keep-me\n", encoding="utf-8")
    env_file.chmod(0o600)
    before = hashlib.sha256(env_file.read_bytes()).hexdigest()

    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "绝不覆盖" in result.stderr or "已有受限配置" in result.stderr
    assert hashlib.sha256(env_file.read_bytes()).hexdigest() == before
    assert ws.calls() == []
    assert not (ws.install_dir / "install-state.json").exists()
    assert not (ws.install_dir / "compose").exists()


def test_port_in_use_fails_preflight(ws: Workspace) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        result = run_install(ws, extra=["--port", str(port)])
    finally:
        listener.close()
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "端口" in result.stderr and "占用" in result.stderr
    assert_no_mutation(ws)
    assert not (ws.install_dir / ".env").exists()


def test_ws_override_port_in_use_fails_preflight(ws: Workspace) -> None:
    """WS override 追加的宿主端口被占，预检必须失败——且在创建任何目录之前。

    ``compose.image.ws.example.yaml`` 只往 api.ports **追加**一条 WS 端口；只看
    api 的 6520（或只看 ``--port``）会把它漏掉，冲突要到迁移后的 ``up -d api``
    才由 Docker 报错。这里让 stub 的合并后模型带一条 6799，并只让 6799 被别的
    项目容器占着（API 端口空闲）。
    """
    ws_port = _free_port()
    while ws_port == ws.port:
        ws_port = _free_port()
    write_config(
        ws,
        extra_ports=[
            {"mode": "ingress", "target": 6799, "published": str(ws_port), "protocol": "tcp"}
        ],
    )
    result = run_install(
        ws,
        env={
            "STUB_PORT_OWNERS": json.dumps(
                {str(ws_port): {"id": "c-foreign", "project": "other", "name": "other-ws-1"}}
            )
        },
    )
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert f"宿主端口 {ws_port}/tcp 已被容器占用" in result.stderr, result.stderr
    assert "api target=6799" in result.stderr, result.stderr
    # 预检失败于建目录之前：没有安装目录、没有 .env、没有 pull/up/run。
    assert not ws.install_dir.exists(), list(ws.install_dir.rglob("*"))
    assert_no_mutation(ws)


def test_unparseable_published_port_fails_preflight(ws: Workspace) -> None:
    """published 解析不了必须 fail closed，不能当成"没有端口"跳过。"""
    write_config(
        ws,
        extra_ports=[
            {"mode": "ingress", "target": 6799, "published": "not-a-port", "protocol": "tcp"}
        ],
    )
    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "无法解析" in result.stderr and "compose_ports" in result.stderr, result.stderr
    assert not ws.install_dir.exists()
    assert_no_mutation(ws)


def test_port_range_is_expanded_and_checked(ws: Workspace) -> None:
    """``published`` 是范围时逐个核对：中间一个被占就要预检失败。"""
    start, end = _free_range(2)
    write_config(
        ws,
        extra_ports=[
            {
                "mode": "ingress",
                "target": 9000,
                "published": f"{start}-{end}",
                "protocol": "tcp",
            }
        ],
    )
    result = run_install(
        ws,
        env={
            "STUB_PORT_OWNERS": json.dumps(
                {str(end): {"id": "c-foreign", "project": "other", "name": "other-range-1"}}
            )
        },
    )
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert f"宿主端口 {end}/tcp 已被容器占用" in result.stderr, result.stderr
    assert not ws.install_dir.exists()
    assert_no_mutation(ws)


# ------------------------------------------------- 预检模型 == --port / --host
#
# 这一组钉住"预检看到的模型必须是**将要部署的**那份"。缺陷现场：预检临时 env
# 是空文件，`compose.image.yaml` 的 `${LING_PORT:-6520}` 于是落回 6520，
# `--port 6522` 根本没进模型——安装器拿 6520 去查容器占用、命中同机另一个
# 实例，于是"不同项目名/不同端口/空白卷并存安装"（设计 P3）永远做不到。


def _port_owners(port: int, *, project: str, name: str) -> str:
    """stub 的 ``docker ps --filter publish=...`` 占用者（只对指定端口回答）。"""
    return json.dumps({str(port): {"id": "c-" + project, "project": project, "name": name}})


def _preflight_env(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """读回安装器打印的**预检 env**（临时 ``--env-file`` 的真实内容）。"""
    for line in result.stderr.splitlines():
        if "预检 env（临时 0600）" not in line:
            continue
        values: dict[str, str] = {}
        for item in line.split("：", 1)[1].split("、"):
            key, _, value = item.partition("=")
            values[key.strip()] = value
        return values
    raise AssertionError("安装器没有打印预检 env：\n" + result.stderr)


def _compose_file_records(ws: Workspace) -> list[dict[str, str]]:
    """docker stub 记下的每次**模型解析**用到的 ``-f`` 文件（path/sha256/host_anchor）。"""
    records: list[dict[str, str]] = []
    for line in ws.calls():
        if not line.startswith("compose-file "):
            continue
        _, path, sha, anchor = line.split(" ", 3)
        records.append(
            {
                "path": path,
                "sha256": sha.split("=", 1)[1],
                "host_anchor": anchor.split("=", 1)[1],
            }
        )
    return records


def test_preflight_parses_the_same_rendered_compose_copy(ws: Workspace) -> None:
    """预检解析的必须是**渲染后的副本**，且与最终落盘的那份逐字节相同。

    源 ``deploy/compose.image.yaml`` 的 ports 是 ``"${LING_PORT:-6520}:6520"``
    （= 0.0.0.0）；``${LING_HOST}`` 只出现在 ``render_compose_copy`` 生成的副本里。
    预检若解析源文件，模型里的宿主绑定永远不可能等于 ``--host``——那正是旧实现
    只能 ``require_host_bind=False`` 跳过绑定核对的原因。
    """
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    records = _compose_file_records(ws)
    assert records, ws.calls()
    first = records[0]
    assert first["path"] != str(ws.compose_source), records
    assert first["host_anchor"] == "True", records
    installed = ws.install_dir / "compose" / "compose.image.yaml"
    installed_sha = hashlib.sha256(installed.read_bytes()).hexdigest()
    assert first["sha256"] == installed_sha, (first, installed_sha)


def test_install_coexists_with_instance_on_default_port(ws: Workspace) -> None:
    """核心回归：``--port`` 指定的端口干净时，别的实例占着 6520 也不该拦。

    改前：预检 env 是空文件 → 模型解析出 6520 → ``docker ps --filter
    publish=6520/tcp`` 命中生产实例 → 失败（``failed_preflight/port``）。
    改后：预检 env 带 ``LING_PORT=<--port>`` → 模型解析出 --port → 通过。
    """
    result = run_install(
        ws,
        env={
            "STUB_PORT_OWNERS": _port_owners(
                DEFAULT_PORT, project="ling-verify", name="ling-verify-api-1"
            )
        },
        readyz=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    preflight_env = _preflight_env(result)
    assert preflight_env["LING_PORT"] == str(ws.port)
    assert preflight_env["LING_HOST"] == "127.0.0.1"
    # 模型解析出的就是 --port：核对的是 ws.port，绝不去碰 6520。
    published_checks = [line for line in ws.calls() if "publish=" in line]
    assert any(f"publish={ws.port}/tcp" in line for line in published_checks), published_checks
    assert not [line for line in published_checks if f"publish={DEFAULT_PORT}/tcp" in line], (
        published_checks
    )
    # 落盘的受限 env 与预检 env 的端口/绑定逐字符一致（预检模型不是另造一份）。
    env_text = (ws.install_dir / ".env").read_text(encoding="utf-8")
    assert f"LING_PORT={ws.port}" in env_text
    assert "LING_HOST=127.0.0.1" in env_text
    assert ws.install_state()["listen"]["port"] == ws.port


def test_preflight_refuses_model_port_that_ignores_command(ws: Workspace) -> None:
    """模型解析出 6520（模拟"env 没生效"）→ 预检必须失败并**指明不一致**。

    旧顺序是"先按错模型查占用、再核对模型"：错的那一步先抛错，真正的不一致
    永远看不到（报的是"6520 被别人占着"）。现在模型与命令不一致就先失败。
    """
    write_config(ws, port=DEFAULT_PORT)  # 写死 6520：不照 LING_PORT 插值
    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "预检模型与 --port 不一致" in result.stderr, result.stderr
    assert f"宿主端口是 {DEFAULT_PORT}" in result.stderr, result.stderr
    assert f"命令给的是 {ws.port}" in result.stderr, result.stderr
    # 失败在建目录/查占用之前：不创建安装目录，也不拿错端口去查容器。
    assert not ws.install_dir.exists()
    assert not [line for line in ws.calls() if "publish=" in line]
    assert_no_mutation(ws)


def test_preflight_refuses_model_host_that_ignores_command(ws: Workspace) -> None:
    """模型绑定 0.0.0.0（模拟 LING_HOST 没生效）→ 预检失败，不许悄悄放宽。"""
    write_config(ws, host="0.0.0.0")
    result = run_install(ws)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert "预检模型与 --host 不一致" in result.stderr, result.stderr
    assert "0.0.0.0" in result.stderr and "127.0.0.1" in result.stderr, result.stderr
    assert not ws.install_dir.exists()
    assert_no_mutation(ws)


def test_port_owner_on_requested_port_still_fails(ws: Workspace) -> None:
    """真正被占用的是 ``--port`` 指定的端口：仍然必须失败（检查不许改松）。"""
    result = run_install(
        ws,
        env={"STUB_PORT_OWNERS": _port_owners(ws.port, project="other", name="other-api-1")},
    )
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert f"宿主端口 {ws.port}/tcp 已被容器占用" in result.stderr, result.stderr
    assert "other-api-1" in result.stderr, result.stderr
    assert not ws.install_dir.exists()
    assert_no_mutation(ws)


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_host_choice_reaches_the_preflight_model(ws: Workspace, host: str) -> None:
    """``--host`` 两种取值都必须进预检 env / 模型（不能只写在 .env 里）。"""
    result = run_install(ws, extra=["--host", host], readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _preflight_env(result)["LING_HOST"] == host
    assert ws.install_state()["listen"]["host"] == host
    env_text = (ws.install_dir / ".env").read_text(encoding="utf-8")
    assert f"LING_HOST={host}" in env_text


def test_preflight_env_covers_every_required_interpolation(ws: Workspace) -> None:
    """预检 env 必须给 compose 里每条 ``${VAR:?...}`` 提供值（否则真机 config 直接失败）。

    stub 的 ``config`` 现在照样做插值，但"必填变量"这条契约仍要静态核对：漏一条
    时真机会在 ``config`` 阶段报缺值，预检同样走不过去。
    """
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    compose_text = (ws.install_dir / "compose" / "compose.image.yaml").read_text(
        encoding="utf-8"
    )
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", compose_text))
    values = _preflight_env(result)
    missing = sorted(required - set(values))
    assert not missing, (missing, "预检 env 缺必填插值；真机 docker compose config 会失败")
    # 端口/绑定这两个"渲染期"值正是本缺陷的根因，必须在里面。
    assert {"LING_PORT", "LING_HOST"} <= set(values)


def test_ws_override_port_is_still_checked_alongside_api_port(ws: Workspace) -> None:
    """override 追加的 WS 端口仍逐个核对（原有能力不许退化）。"""
    ws_port = ws.port
    extra = _free_port()
    while extra == ws_port:
        extra = _free_port()
    write_config(
        ws,
        extra_ports=[
            {"mode": "ingress", "target": 6799, "published": str(extra), "protocol": "tcp"}
        ],
    )
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    published_checks = [line for line in ws.calls() if "publish=" in line]
    assert any(f"publish={ws_port}/tcp" in line for line in published_checks), published_checks
    assert any(f"publish={extra}/tcp" in line for line in published_checks), published_checks


def test_arch_mismatch_fails_preflight(ws: Workspace) -> None:
    result = run_install(ws, env={"STUB_MANIFEST_MODE": "no_host"})
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert f"没有 linux/{HOST_ARCH}" in result.stderr
    assert_no_mutation(ws)


def test_digest_must_be_pinned_without_docker(ws: Workspace) -> None:
    result = run_install(ws, extra=["--digest", "latest"])
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "digest" in result.stderr.lower()
    assert ws.calls() == []


# ------------------------------------------------ db 就绪门禁（真机首装缺陷）
#
# 真机现场：全新空卷首装时 postgres 官方镜像的 entrypoint 是两段式启动——
# 先起一个**临时服务器**做 initdb/CREATE DATABASE，此时 compose 的
# `pg_isready -U ling -d ling` healthcheck 已经报 healthy（它只探"接受连接"，
# 不看库是否存在），随后临时服务器关闭、才起正式服务器。安装器曾经的
# "db healthy → psql 读 alembic_version" 正好撞在关闭窗口里：
#
#     [ling] db healthy
#     psql: error: … FATAL:  the database system is shutting down
#
# 结果一次本会成功的安装被记成 failed_after_migration 并留下半成品。
# 下面这些用例用 stub 的 `STUB_PSQL_FAILURES` / `STUB_PSQL_ALWAYS_FAIL`
# 逐次注入 psql 错误，钉住"有界重试 + 门禁不放宽 + 阶段标签准确"。

PSQL_SHUTTING_DOWN = (
    'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" '
    "failed: FATAL:  the database system is shutting down"
)
PSQL_STARTING_UP = (
    'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" '
    "failed: FATAL:  the database system is starting up"
)
PSQL_MISSING_DB = (
    'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" '
    'failed: FATAL:  database "ling" does not exist'
)
PSQL_AUTH_FAILED = (
    'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" '
    'failed: FATAL:  password authentication failed for user "ling"'
)


def psql_calls(ws: Workspace) -> list[str]:
    return [line for line in ws.calls() if " exec -T db psql " in line]


# 重试窗口的长度**不**决定被测语义，只需要"有限"。但"窗口里到底能试几次"若
# 由**真实**耗时决定，就会随机器负载漂移——那正是这批用例 flaky 的来源，所以
# 重试节奏必须可控。
#
# 做法（只动测试）：`db_query_with_retry` 里唯一的等待点是
# ``time.sleep(min(DB_READY_POLL_INTERVAL, ...))``，把它 patch 成 no-op。但
# ``ling.py`` 是 ``run_install`` 用 ``sys.executable`` 起的**独立子进程**，进程内
# ``monkeypatch`` 够不到它；因此用一个一次性的 ``sitecustomize`` 经 ``PYTHONPATH``
# **只注入给这一次子进程**。补丁按 ``co_filename`` + 函数名严格限定在
# ``db_query_with_retry`` 内（别的函数、别的模块——包括 docker stub——行为不变）。
#
# 只把 sleep 变 no-op 还不够：循环的终止条件是 ``deadline - time.monotonic()``，
# 不睡就会**空转**到真实窗口结束（既慢又不确定）。所以循环内的 ``monotonic``
# 一并换成"进入循环时的真实时刻 + 已睡掉的虚拟时长"：逻辑节奏与生产完全一致
# （仍是 min(2s, 剩余)），只是等待不花真实时间。重试**次数/顺序/结论**全部由
# 真实代码决定，被替换的只有"秒"这个单位。
_DB_CLOCK_SITECUSTOMIZE = r'''"""仅测试用：db 重试循环的 sleep 不真睡（虚拟时钟）。"""
import os
import sys
import time

_target = os.environ["LING_TEST_DB_CLOCK_TARGET"]
_real_monotonic = time.monotonic
_real_sleep = time.sleep
_base: list[float] = []          # 进入循环时的真实时刻（首次问时间时锁定）
_elapsed = [0.0]                 # 循环内已"睡掉"的虚拟时长


def _in_db_retry() -> bool:
    """只看**紧邻的调用方**：必须是 ling.py 里的 ``db_query_with_retry``。

    按 ``co_filename`` 精确匹配：docker stub（同一个解释器、同样的
    PYTHONPATH）以及 ling.py 的其它函数都不受影响。
    """
    try:
        frame = sys._getframe(2)
    except ValueError:
        return False
    code = frame.f_code
    return code.co_name == "db_query_with_retry" and code.co_filename == _target


def _monotonic() -> float:
    if not _in_db_retry():
        return _real_monotonic()
    if not _base:
        _base.append(_real_monotonic())
    return _base[0] + _elapsed[0]


def _sleep(seconds: float) -> None:
    if _in_db_retry():
        _elapsed[0] += max(0.0, float(seconds))
        return None
    return _real_sleep(seconds)


time.monotonic = _monotonic
time.sleep = _sleep
'''

# sitecustomize 放**源码树之外**（临时目录）：测试不在仓库里留生成物。
_DB_CLOCK_DIR: list[Path] = []

# 供 sitecustomize 精确匹配"哪一个文件里的 db_query_with_retry"。
LING_PY_STR = str(LING_PY.resolve())


def no_db_retry_sleep(env: dict[str, str] | None = None) -> dict[str, str]:
    """把 db 重试循环的等待换成虚拟时间（只作用于本次 ``run_install`` 的子进程）。"""
    if not _DB_CLOCK_DIR:
        directory = Path(tempfile.mkdtemp(prefix="ling-db-clock-"))
        (directory / "sitecustomize.py").write_text(_DB_CLOCK_SITECUSTOMIZE, encoding="utf-8")
        # 临时目录出了源码树，且用完即删：测试不在仓库或 /tmp 里留垃圾。
        atexit.register(shutil.rmtree, directory, True)
        _DB_CLOCK_DIR.append(directory)
    merged = dict(env or {})
    merged["PYTHONPATH"] = f"{_DB_CLOCK_DIR[0]}{os.pathsep}{merged.get('PYTHONPATH', '')}"
    merged["LING_TEST_DB_CLOCK_TARGET"] = LING_PY_STR
    return merged


# 窗口只需"有限"：虚拟时钟让等待不花真实时间，取多大都一样快。
DB_READY_WINDOW = "30"


def psql_queries(ws: Workspace) -> list[str]:
    """``psql_calls`` 的 SQL 部分（``-tAc`` 之后的整段），按到达顺序。"""
    queries: list[str] = []
    for line in psql_calls(ws):
        marker = " -tAc "
        assert marker in line, line
        queries.append(line.split(marker, 1)[1])
    return queries


def assert_no_wait_exhaustion_log(result: subprocess.CompletedProcess[str]) -> None:
    """"等满整个窗口"才有的那些日志行，一条都不许出现。

    - ``始终没有就绪``：窗口被真的用完；
    - ``窗口还剩``：进入过"重试并等待"分支（认证失败/立刻失败不该有）。

    这是"没白等一个窗口"的**结构**等价物，替掉了原来的墙钟断言。
    """
    assert "始终没有就绪" not in result.stderr, result.stderr
    assert "窗口还剩" not in result.stderr, result.stderr


def test_db_shutting_down_on_first_psql_is_retried_and_install_succeeds(
    ws: Workspace,
) -> None:
    """真机缺陷的回归：首次 psql 撞上"临时服务器正在关闭" → 重试后安装成功。"""
    result = run_install(
        ws,
        readyz=True,
        env={"STUB_PSQL_FAILURES": json.dumps([PSQL_SHUTTING_DOWN])},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 重试证据：第 1 次失败的原因、第 2 次成功，都要能在日志里看到。
    assert "db 还没就绪" in result.stderr
    assert "the database system is shutting down" in result.stderr
    assert "第 2 次尝试成功" in result.stderr
    state = ws.install_state()
    assert state["status"] == "complete"
    assert state["migration"]["attempted"] is True
    assert state["database"]["alembic_version"] == HEAD


def test_db_missing_database_is_retried_until_entrypoint_creates_it(
    ws: Workspace,
) -> None:
    """临时服务器阶段库还没建出来（database "ling" does not exist）也要重试。"""
    result = run_install(
        ws,
        readyz=True,
        env={"STUB_PSQL_FAILURES": json.dumps([PSQL_MISSING_DB] * 3)},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stderr.count("db 还没就绪") == 3
    assert "第 4 次尝试成功" in result.stderr
    assert ws.install_state()["status"] == "complete"


def test_db_never_ready_exhausts_window_and_says_so(ws: Workspace) -> None:
    """一直 starting up 到窗口耗尽 → 失败，且说明"始终没有就绪""不猜状态"。

    不量墙钟：这里证明的是**窗口真的被用完**（日志说"始终没有就绪"、psql 被
    反复打过、状态记在 db 阶段）与**门禁没被绕过**，不是"这花了多久"。
    """
    result = run_install(
        ws,
        env={"STUB_PSQL_ALWAYS_FAIL": PSQL_STARTING_UP},
        extra=["--db-timeout", DB_READY_WINDOW],
    )
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "始终没有就绪" in result.stderr
    assert "the database system is starting up" in result.stderr
    # 窗口有限，且报错里点名用的是 --db-timeout 的值。
    assert f"{DB_READY_WINDOW}s" in result.stderr
    # 有界重试真的发生过：同一查询被反复打过（不是"打一次就放弃"）。
    # 虚拟时钟让它与机器快慢无关，故这里可以直接钉住次数。
    queries = psql_queries(ws)
    assert set(queries) == {"select 1"}, queries
    assert len(queries) == 16, queries
    # 每次失败都有一次"还没就绪 + 重试"，最后那条是"窗口耗尽"的结论。
    assert result.stderr.count("db 还没就绪") == len(queries) - 1, result.stderr
    assert f"共尝试 {len(queries)} 次" in result.stderr, result.stderr
    state = ws.install_state()
    assert state["status"] == "failed_db_ready"
    assert state["failure"]["step"] == "db_ready"
    assert state["failure"]["after_migration"] is False
    assert state["migration"]["attempted"] is False
    # 没就绪就绝不迁移、绝不启动 api/worker。
    assert not [line for line in compose_calls(ws) if compose_subcommand(line) == "run"]
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "api" in line
    ]


def test_db_auth_error_fails_immediately_without_retry(ws: Workspace) -> None:
    """口令错误是配置问题：立刻失败、只打一次 psql，绝不重试掩盖。

    "立刻"用**结构**证明：psql 只被调用 1 次、日志没有"还没就绪/窗口还剩"，
    并且错误文本自己说明"共尝试 1 次"。**不量耗时**：窗口给 30s，就算实现退化
    成"先等满一个窗口再失败"，psql 调用次数这条断言仍然抓得到。
    """
    result = run_install(
        ws,
        env={"STUB_PSQL_ALWAYS_FAIL": PSQL_AUTH_FAILED},
        extra=["--db-timeout", DB_READY_WINDOW],
    )
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "不重试" in result.stderr
    assert "password authentication failed" in result.stderr
    assert "共尝试 1 次" in result.stderr, result.stderr
    # 必须是"配置错误"这一类（fatal），而不是被当成"未知错误"顺手放过：
    # 两者都不重试，只有这条断言能把它们分开。
    assert "凭据/角色/权限配置错误" in result.stderr, result.stderr
    # 核心：只打一次 psql（认证失败不重试）。
    assert len(psql_calls(ws)) == 1, psql_calls(ws)
    assert psql_queries(ws) == ["select 1"], psql_queries(ws)
    # 没有进入重试等待：这些行只有"再试一次"才会有。
    assert_no_wait_exhaustion_log(result)
    assert "db 还没就绪" not in result.stderr, result.stderr
    state = ws.install_state()
    assert state["status"] == "failed_db_ready"
    assert state["failure"]["step"] == "db_ready"
    assert state["failure"]["after_migration"] is False
    assert state["migration"]["attempted"] is False


def test_db_ready_failure_is_never_labelled_after_migration(ws: Workspace) -> None:
    """探针已过、读版本读不出 → 状态仍是 failed_db_ready，且区分两种失败语义。"""
    result = run_install(
        ws,
        env={
            "STUB_PSQL_FAIL_AFTER": "1",
            "STUB_PSQL_ALWAYS_FAIL": PSQL_SHUTTING_DOWN,
        },
        extra=["--db-timeout", DB_READY_WINDOW],
    )
    assert result.returncode == 16, (result.stdout, result.stderr)
    # "就绪了但读不出"与"超时内始终没就绪"必须能区分开：
    # 探针那条 `select 1` 必须真的先成功过（否则后面这句结论就是假的）。
    assert "db 就绪探针通过" in result.stderr
    assert "db 已就绪（select 1 就绪探针通过），但" in result.stderr
    assert "读不出" in result.stderr
    assert "始终没有就绪" not in result.stderr, result.stderr
    # 执行顺序：先 `select 1` 探针成功，之后才是反复读 alembic_version。
    queries = psql_queries(ws)
    assert queries[0] == "select 1", queries
    assert set(queries[1:]) == {"select version_num from alembic_version"}, queries
    # 读版本确实被重试过（不止一次），最后才判定"读不出"。
    assert len(queries) > 2, queries
    state = ws.install_state()
    assert state["status"] == "failed_db_ready"
    assert state["status"] != "failed_after_migration"
    assert state["failure"]["step"] == "alembic_version"
    assert state["failure"]["after_migration"] is False
    assert state["migration"]["attempted"] is False
    # 不是"打错了再被兜底纠正"：分类在源头就是对的——纠正日志一次都没有出现。
    assert "本次失败发生在迁移执行之前" not in result.stderr, result.stderr


def test_db_ready_success_path_does_not_wait_extra(ws: Workspace) -> None:
    """成功路径：首个成功即退出重试循环，只打 4 次 psql（1 探针 + 3 读版本）。

    "没白等"用**调用次数**钉住：成功路径上没有一次多余的 psql、也没有任何
    "还没就绪/窗口还剩"的等待日志。量墙钟只会测出机器快慢。
    """
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = psql_calls(ws)
    assert len(calls) == 4, calls
    # 第 1 次必须是真实就绪探针，不能只信 pg_isready。
    assert calls[0].endswith("-tAc select 1"), calls[0]
    assert all("select version_num from alembic_version" in item for item in calls[1:])
    # 成功路径不重试：没有一次"还没就绪"，也不出现任何重试等待。
    assert "db 还没就绪" not in result.stderr, result.stderr
    assert_no_wait_exhaustion_log(result)
    assert "db 就绪探针通过" in result.stderr
    state = ws.install_state()
    assert state["status"] == "complete"
    assert state["migration"]["attempted"] is True


def test_failure_handler_never_labels_after_migration_without_an_attempt(
    tmp_path: Path,
) -> None:
    """不变式：没执行过迁移，就不许出现 after_migration（连兜底也要钉住）。

    安装路径上的 db 阶段错误已经直接写 ``failed_db_ready``；这条额外证明
    ``handle_failure`` 的**兜底纠正**真的生效——即使上游给了一个
    ``after_migration=True`` 的错误，只要 ``migration.attempted`` 是 False，
    记录里的 ``failure.after_migration`` 就必须是 False、状态名也要被纠正。
    （没有它，将来新加一处 `LingError(..., after_migration=True)` 就会悄悄
    把"迁移根本没跑"记成"已迁移后失败"。）
    """
    ling = _load_module("ling_cli_module_invariant", LING_PY)

    class _Log:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def emit(self, message: str) -> None:
            self.lines.append(message)

    class _Layout:
        state_file = tmp_path / "install-state.json"

    class _Shim:
        """只带 handle_failure 真正会碰到的属性（不起容器、不跑 docker）。"""

        def __init__(self, *, attempted: bool, phase: str) -> None:
            self.state = {
                "status": "whatever",
                "migration": {"attempted": attempted},
            }
            self.phase = phase
            self.log = _Log()
            self.layout = _Layout()
            self.layout_created = False
            self.container_started = True
            self.state_written: dict[str, Any] | None = None

        migration_attempted = ling.Installer.migration_attempted

        def write_state(self) -> None:
            self.state_written = dict(self.state)

    def run(**kwargs: Any) -> _Shim:
        shim = _Shim(**kwargs)
        error = ling.LingError(
            16,
            "failed_after_migration",
            "alembic_version",
            "错误的标签：迁移根本没跑",
            after_migration=True,
        )
        ling.Installer.handle_failure(shim, error)  # type: ignore[arg-type]
        return shim

    # 迁移没跑过（attempted=False）：纠正成 db 阶段标签，after_migration=False。
    shim = run(attempted=False, phase="database")
    assert shim.state["status"] == "failed_db_ready"
    assert shim.state["failure"]["after_migration"] is False
    assert any("不按“已迁移后失败”处理" in line for line in shim.log.lines)

    # 迁移真跑过：原标签与 after_migration 都必须保留（不许反过来吞掉真实告警）。
    shim = run(attempted=True, phase="start")
    assert shim.state["status"] == "failed_after_migration"
    assert shim.state["failure"]["after_migration"] is True


def test_db_error_classification_never_treats_missing_db_as_blank_instance() -> None:
    """分类顺序：库不存在 = 还没就绪（retry），表不存在 = 空白实例（blank）。

    顺序反过来会把"db 还在初始化"当成"空白库，直接迁移"。
    """
    ling = _load_module("ling_cli_module_db_kind", LING_PY)
    assert ling.db_error_kind(PSQL_SHUTTING_DOWN) == "retry"
    assert ling.db_error_kind(PSQL_STARTING_UP) == "retry"
    assert ling.db_error_kind(PSQL_MISSING_DB) == "retry"
    assert (
        ling.db_error_kind('ERROR:  relation "alembic_version" does not exist')
        == "blank"
    )
    assert ling.db_error_kind(PSQL_AUTH_FAILED) == "fatal"
    assert ling.db_error_kind("psql: error: something unexpected") == "unknown"


# ---------------------------------------------------------------- 迁移门禁


def test_migration_log_showing_skip_fails(ws: Workspace) -> None:
    # 日志同时有"执行迁移"和"跳过迁移"，且数据库已到 head：只有 skip 门禁能
    # 判失败（删掉它就会落到 executed/head 门禁，step 不再是 migrate_skipped）。
    result = run_install(ws, env={"STUB_MIGRATION_MODE": "both"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    assert "跳过迁移" in result.stderr
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "api" in line
    ]
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "worker" in line
    ]
    state = ws.install_state()
    assert state["status"] == "failed_migration"
    assert state["failure"]["step"] == "migrate_skipped"
    assert state["migration"]["skip_marker"] is True
    assert state["migration"]["executed_marker"] is True


def test_migration_head_mismatch_fails_and_never_starts_api(ws: Workspace) -> None:
    result = run_install(ws, env={"STUB_MIGRATION_MODE": "stale"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    assert "≠ 目标镜像 head" in result.stderr
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "api" in line
    ]
    state = ws.install_state()
    assert state["status"] == "failed_migration"
    assert state["migration"]["executed_marker"] is True
    assert state["migration"]["current_after"] is None
    assert state["database"]["target_head"] == HEAD


# ---------------------------------------------------------------- 迁移后失败


def test_readyz_redirect_is_not_accepted(ws: Workspace) -> None:
    ws.http.readyz_redirect = True
    result = run_install(ws, readyz=True)
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "重定向" in result.stderr
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "worker" in line
    ]
    state = ws.install_state()
    assert state["status"] == "failed_after_migration"
    assert state["failure"]["step"] == "readyz_redirect"
    assert state["database"]["alembic_version"] == HEAD
    # 已迁移：不得出现“重建 db / 回滚 api”的 up，也不得回退镜像引用。
    up_services = [
        line.split()[-1]
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up"
    ]
    assert up_services == ["db", "api"]


# ---------------------------------------------------------------- 版本/提交探活
#
# 真机演练 P5 第 6 个缺陷的同一类问题：`/v1/version` 挂 `CurrentAuth`，未鉴权
# 恒 401。安装器的 verify 曾打它，于是**真机上安装最后一步必然失败**；而 HTTP
# stub 以前对它无条件回 200，所以单元测试一直是绿的。
#
# 现在改成容器内**进程证据**（`compose exec api python -c`，零凭据）。
# 下面的 stub 对 `/v1/*` 如实回 401：成功路径一旦退回 HTTP 探活，立刻红。


def test_install_verify_reads_version_from_container_process(ws: Workspace) -> None:
    """安装成功路径的版本/提交来自容器内进程，且 HTTP 只探过 `/readyz`。"""
    result = run_install(ws, readyz=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    state = ws.install_state()
    assert state["status"] == "complete"
    # stub 的 exec 分支打印的就是应用自报的 JSON（version + commit）。
    assert state["result"]["version"] == "9.9.9"
    probe_call = ws.calls()[find_call(ws, " exec -T api python -c ")]
    assert "LING_COMMIT" in probe_call and "__version__" in probe_call
    assert "-f " + str(ws.install_dir / "compose" / "compose.image.yaml") in probe_call
    assert "--env-file " + str(ws.install_dir / ".env") in probe_call
    # 关键：一次 `/v1/*` 都没打（stub 会回 401；打了就不可能成功）。
    assert ws.http.requests, "安装成功路径必须真的等过 /readyz"
    assert set(ws.http.requests) <= {"/readyz"}, ws.http.requests


def test_install_reports_commit_mismatch_from_running_process(ws: Workspace) -> None:
    """容器里跑的进程自报的提交 ≠ 镜像标注源提交 → 迁移后失败，不启 worker。"""
    result = run_install(ws, readyz=True, env={"STUB_PROBE_COMMIT": OTHER_COMMIT})
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "容器内进程报告的提交" in result.stderr
    assert COMMIT in result.stderr and OTHER_COMMIT in result.stderr
    state = ws.install_state()
    assert state["status"] == "failed_after_migration"
    assert state["failure"]["step"] == "version"
    assert not [
        line
        for line in compose_calls(ws)
        if compose_subcommand(line) == "up" and "worker" in line
    ]


def test_install_fails_when_container_probe_cannot_run(ws: Workspace) -> None:
    """exec 本身失败（镜像里没有 ling / 容器没起来）也要如实失败。"""
    result = run_install(ws, readyz=True, env={"STUB_PROBE_EXIT": "1"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "exec 失败" in result.stderr
    state = ws.install_state()
    assert state["status"] == "failed_after_migration"
    assert state["failure"]["step"] == "version"


def test_install_fails_when_container_probe_output_is_not_json(ws: Workspace) -> None:
    """探针输出不可解析时不能把缺失的 version/commit 当成功。"""
    result = run_install(ws, readyz=True, env={"STUB_PROBE_GARBAGE": "1"})
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert "没有打印可解析的 JSON" in result.stderr
    state = ws.install_state()
    assert state["status"] == "failed_after_migration"
    assert state["failure"]["step"] == "version"


# ---------------------------------------------------------------- status / logs


def test_status_detects_recorded_vs_live_mismatch(ws: Workspace) -> None:
    with late_readyz(ws) as server:
        installed = run_ling(ws, build_install_argv(ws))
        assert installed.returncode == 0, (installed.stdout, installed.stderr)
        assert server.wait_ready(10.0)
        state = ws.state()
        state["containers"]["api"]["image_id"] = "sha256:other-image"
        state["containers"]["worker"]["image_id"] = "sha256:other-image"
        ws.write_state(state)
        # 实况来自容器内进程证据（compose exec），不再来自 `/v1/version`：
        # 让那个"跑着的容器"自报另一个提交，才算真的模拟了"记录 vs 实况"。
        result = run_status(ws, env={"STUB_PROBE_COMMIT": OTHER_COMMIT})
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "差异" in result.stdout
    assert "以实况为准" in result.stdout
    assert COMMIT in result.stdout and OTHER_COMMIT in result.stdout
    assert TARGET_HEX in result.stdout and OTHER_HEX in result.stdout
    assert "api 运行 image ID" in result.stdout
    assert "sha256:target-image" in result.stdout
    assert "sha256:other-image" in result.stdout
    assert set(ws.http.requests) <= {"/readyz"}, ws.http.requests


def test_status_consistent_after_install(ws: Workspace) -> None:
    with late_readyz(ws) as server:
        installed = run_ling(ws, build_install_argv(ws))
        assert installed.returncode == 0, (installed.stdout, installed.stderr)
        assert server.wait_ready(10.0)
        result = run_status(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "一致" in result.stdout
    assert "更新已启用" not in result.stdout + result.stderr


def test_status_reads_version_from_container_process(ws: Workspace) -> None:
    """`ling status` 的版本/提交也走容器内进程证据，HTTP 只探 `/readyz`。"""
    with late_readyz(ws) as server:
        installed = run_ling(ws, build_install_argv(ws))
        assert installed.returncode == 0, (installed.stdout, installed.stderr)
        assert server.wait_ready(10.0)
        ws.http.requests.clear()
        result = run_status(ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "运行容器自报版本/提交" in result.stdout, result.stdout
    assert "9.9.9" in result.stdout and COMMIT in result.stdout
    assert set(ws.http.requests) <= {"/readyz"}, ws.http.requests
    assert [
        line for line in compose_calls(ws) if " exec -T api python -c " in line
    ], ws.calls()


def test_status_reports_probe_failure_as_mismatch(ws: Workspace) -> None:
    """读不到进程证据时不能静默显示"一致"。"""
    installed = run_install(ws, readyz=True)
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    result = run_status(ws, env={"STUB_PROBE_EXIT": "1"})
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "运行容器自报版本/提交" in result.stdout
    assert "读不到" in result.stdout
    assert "差异" in result.stdout


def test_logs_proxies_same_compose_group(ws: Workspace) -> None:
    installed = run_install(ws, readyz=True)
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    compose_file = ws.install_dir / "compose" / "compose.image.yaml"
    env_file = ws.install_dir / ".env"

    result = run_ling(
        ws,
        [
            sys.executable,
            str(LING_PY),
            "logs",
            "--dir",
            str(ws.install_dir),
            "--service",
            "worker",
            "--tail",
            "17",
        ],
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "fake-logs" in result.stdout
    logs_calls = [
        line for line in compose_calls(ws) if compose_subcommand(line) == "logs"
    ]
    assert len(logs_calls) == 1
    line = logs_calls[0]
    assert "-p ling" in line
    assert "-f " + str(compose_file) in line
    assert "--env-file " + str(env_file) in line
    assert "logs --tail 17 worker" in line

    bad = run_ling(
        ws,
        [sys.executable, str(LING_PY), "logs", "--dir", str(ws.install_dir), "--service", "nope"],
    )
    assert bad.returncode == 2


# ---------------------------------------------------------------- 与 publish.py 的共享约定


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses 用 sys.modules[cls.__module__] 解析字符串注解；必须先登记。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_lock_and_redirect_invariants() -> None:
    """复制到 ling.py 的基础约定必须与 publish.py 逐字节等价。

    两处都不 import 对方（安装包要能独立交付），所以用测试钉住：
    ``.ling-install.lock`` 的路径公式、以及"3xx 绝不跟随"。
    """
    ling = _load_module("ling_cli_module", LING_PY)
    publish = _load_module("ling_publish_module", PUBLISH_PY)

    env_file = Path("/tmp/ling-lock-check/.env")
    assert ling.InstallLock(env_file).path == publish.InstallLock(env_file).path
    assert ling.InstallLock(env_file).path.name == ".ling-install.lock"

    server = _HTTPServer(("127.0.0.1", 0), _Handler)
    state = _HTTPState()
    state.readyz_redirect = True
    server.http_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/readyz"
    try:
        assert ling.http_get(url, timeout=5.0)[0] == 302
        with pytest.raises(Exception) as excinfo:
            publish._NO_REDIRECT_OPENER.open(url, timeout=5.0)
        assert getattr(excinfo.value, "code", None) == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_published_ports_match_publish_script() -> None:
    """同一份合并后 compose 模型，两个脚本必须解析出**同一组**宿主端口。

    ``ling.py`` 与 ``publish.py`` 刻意不互相 import（安装包与发布脚本各自独立
    交付），端口解析是复制出来的，所以用这条用例钉住：服务取值、顺序、范围
    展开、空值/随机端口、以及"解析不了就拒绝"都必须逐项等价。
    """
    ling = _load_module("ling_cli_ports", LING_PY)
    publish = _load_module("ling_publish_ports", PUBLISH_PY)

    services = {
        "db": {"ports": [{"target": 5432, "published": "15432", "protocol": "tcp"}]},
        "api": {
            "ports": [
                {"target": 6520, "published": "16520", "protocol": "tcp"},
                # compose.image.ws.example.yaml 追加进来的 WS 端口（追加语义）。
                {"target": 6799, "published": "6799", "protocol": "tcp"},
                # 范围：逐个展开，不把 8000-8002 当成一个条目跳过。
                {"target": 8000, "published": "8000-8002", "protocol": "tcp"},
                # 非 tcp 条目按自己的 protocol 走，不一律当 tcp。
                {"target": 9000, "published": 19000, "protocol": "udp"},
                # 没有 published / 空字符串：Docker 随机分配，没有宿主机端口要核对。
                {"target": 6521},
                {"target": 6522, "published": ""},
            ]
        },
        "worker": {"ports": [{"target": 9100, "published": "19100", "protocol": "tcp"}]},
    }
    ling_ports = [
        (item["service"], item["target"], item["published"], item["protocol"])
        for item in ling.service_published_ports(services)
    ]
    publish_ports = [
        (item["service"], item["target"], item["published"], item["protocol"])
        for item in publish.service_published_ports(services)
    ]
    assert ling_ports == publish_ports
    assert ("api", 6799, 6799, "tcp") in ling_ports
    assert [
        published for _service, target, published, _protocol in ling_ports if target == 8000
    ] == [8000, 8001, 8002]
    assert ("api", 9000, 19000, "udp") in ling_ports

    # 单值解析逐项等价（含范围上限、越界与非法值）。
    for raw in ("6520", 6520, "8000-8002", "1-64", "1-65", "0", "65536", "8000-",
                "-8000", "not-a-port", "", None, True, 1.5, [6520]):
        assert ling.parse_published_port(raw) == publish.parse_published_port(raw), raw
    assert ling.parse_published_port("1-65") is None
    assert ling.parse_published_port("1-64") == list(range(1, 65))
    assert ling.MAX_PUBLISHED_PORT_RANGE == publish.MAX_PUBLISHED_PORT_RANGE

    # 解析失败两边都必须拒绝（fail closed），且报同一个 step / 退出码。
    bad = {"api": {"ports": [{"target": 9000, "published": "not-a-port"}]}}
    with pytest.raises(ling.LingError) as ling_error:
        ling.service_published_ports(bad)
    with pytest.raises(publish.PublishError) as publish_error:
        publish.service_published_ports(bad)
    assert ling_error.value.exit_code == publish_error.value.exit_code == 10
    assert ling_error.value.step == publish_error.value.step == "compose_ports"
    assert ling_error.value.status == publish_error.value.status == "failed_preflight"


# ------------------------------------------- 探活免鉴权（P4 阻断缺陷的防回归）
#
# 缺陷：`deploy/cli/ling.py` 的安装 verify、`ling status`、`ling doctor` 都拿
# `GET /v1/version` 当探活，而该端点挂 `CurrentAuth`，未鉴权永远 401。真机上
# `ling install` 最后一步必失败、status/doctor 永远报错；单元测试的 HTTP stub
# 对它无条件回 200，所以一直是绿的。
#
# 修法（与 `deploy/release/publish.py` / `deploy/updater/updater.py` 同一口径）：
# HTTP 探活只允许 `HTTP_PROBE_PATHS` 里登记的**免鉴权**端点（当前只有 `/readyz`）；
# "运行容器是不是目标构建"改用容器内**进程证据**（`compose exec api python -c`）。
#
# 下面几条是门禁：
#   1. AST 扫描：所有 HTTP 探活必须走 `http_probe(base, "<字面量路径>")`，且路径
#      ⊆ 声明表。绕过 helper 直接 `http_get(f"{base}/v1/version")`、把路径写成
#      变量、或新增未登记端点 → 红。
#   2. 声明表里每一条在**真实 FastAPI 应用**上未鉴权都不是 401/403。
#   3. `/v1/version` 仍然要鉴权（没有为了给探活让路而把它改开）。
#   4. 探针字符串由**真实应用自己的包**执行，报出的 version/commit 与
#      `/v1/version` 的实现同源。

_RAW_HTTP_HELPERS = frozenset({"http_get", "http_get_json"})
_HTTP_URL_LITERAL_RE = re.compile(r"https?://[^\s'\"\\]+")
_LOCAL_URL_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})


def _string_literal_values(node: ast.AST) -> list[str]:
    """收集一个字符串字面量/ f-string 的**可静态核对**文本。

    普通的 ``Constant`` 直接取；``JoinedStr``（f-string）把格式化字段换成 ``{}``
    后拼起来，这样 ``f"http://{host}:{port}/readyz"`` 里的 ``/readyz`` 不会因为
    它是 f-string 就漏出扫描。``Constant`` 子节点会被 ``ast.walk`` 单独再访问
    一次，但只产生重复的集合项，不影响判定。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            else:
                parts.append("{}")
        return ["".join(parts)]
    return []


def _scan_probe_source(
    source: str, *, label: str
) -> tuple[set[str], set[str], list[str]]:
    """扫描一份源码：``(http_probe 的路径, URL 字面量里的路径, 违规描述)``。

    与 ``ling.py`` 同一套规则，**publish.py / updater.py 也要扫**：它们有自己的
    ``HTTP_PROBE_PATHS`` 声明，但只靠"三张表相等"是间接约束——有人在这两个文件
    里绕过表直接 ``http_get``/``self.http_get`` 时，表仍然相等。

    违规包括：
    - 绕过 ``http_probe`` 直接调 ``http_get``/``http_get_json``（含
      ``self.http_get`` 这类属性调用）；
    - ``http_probe`` 的路径实参不是字符串字面量；
    - 源码里出现本机/占位主机名的 ``http(s)://`` 字面量，其路径不在声明表里。
    """
    tree = ast.parse(source, filename=label)
    wrappers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "http_probe"
    ]
    inside_wrapper = {
        id(node) for wrapper in wrappers for node in ast.walk(wrapper) if isinstance(node, ast.Call)
    }
    used: set[str] = set()
    literals: set[str] = set()
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            raw_name: str | None = None
            if isinstance(func, ast.Name):
                raw_name = func.id
            elif isinstance(func, ast.Attribute):
                raw_name = func.attr
            if raw_name in _RAW_HTTP_HELPERS and id(node) not in inside_wrapper:
                violations.append(
                    f"{label}:{node.lineno} 绕过 http_probe 直接调用 {raw_name}"
                )
            elif isinstance(func, ast.Name) and func.id == "http_probe":
                path = node.args[1] if len(node.args) > 1 else None
                if not isinstance(path, ast.Constant) or not isinstance(path.value, str):
                    violations.append(
                        f"{label}:{node.lineno} http_probe 的路径不是字符串字面量"
                    )
                else:
                    used.add(path.value)
        else:
            for text in _string_literal_values(node):
                for raw in _HTTP_URL_LITERAL_RE.findall(text):
                    try:
                        parsed = urllib.parse.urlsplit(raw)
                        hostname = parsed.hostname or ""
                    except ValueError:
                        violations.append(f"{label}:{node.lineno} 无法解析的 URL 字面量")
                        continue
                    is_placeholder = "{" in hostname or "<" in hostname
                    if not is_placeholder and hostname not in _LOCAL_URL_HOSTNAMES:
                        continue
                    if parsed.path and parsed.path != "/":
                        literals.add(parsed.path)
    return used, literals, violations


def _scan_probe_file(path: Path) -> tuple[set[str], set[str], list[str]]:
    return _scan_probe_source(path.read_text(encoding="utf-8"), label=str(path))


def _ling_probe_paths() -> tuple[set[str], set[str], list[str]]:
    """扫描 ling.py（保留原入口名，其他用例/文档引用它）。"""
    return _scan_probe_file(LING_PY)


def test_ling_http_probes_are_declared_and_go_through_the_helper() -> None:
    """所有本机 HTTP 探活 ⊆ 声明表；且唯一出口是 ``http_probe``。"""
    ling = _load_module("ling_cli_probe_scan", LING_PY)
    assert ling.HTTP_PROBE_PATHS, "探活路径表不能是空的"
    assert "/v1/version" not in ling.HTTP_PROBE_PATHS, (
        "/v1/version 挂 CurrentAuth（未鉴权 401），不能登记成探活路径"
    )

    used, literal_paths, violations = _ling_probe_paths()
    assert not violations, (
        f"{LING_PY} 里绕开 http_probe / 用非字面量路径的 HTTP 探活：{violations}"
    )
    assert used, "安装/状态/诊断必须真的探过（否则这条门禁没有覆盖对象）"
    missing = used - set(ling.HTTP_PROBE_PATHS)
    assert not missing, f"{LING_PY} 里有未登记的 HTTP 探活：{sorted(missing)}"
    undeclared_literals = literal_paths - set(ling.HTTP_PROBE_PATHS)
    assert not undeclared_literals, (
        f"{LING_PY} 的 URL 字面量里有未登记的探活：{sorted(undeclared_literals)}"
    )


def test_http_probe_guards_cover_all_three_entrypoints() -> None:
    """L4：AST 护栏扫 ``ling.py`` / ``publish.py`` / ``updater.py`` 三份入口。

    三份文件各自声明 ``HTTP_PROBE_PATHS``（安装包与发布包互不 import，只能复制），
    这里同时断言：
    1. 三张表**逐项相等**；
    2. 每份文件都没有绕过 helper 的 ``http_get``/``self.http_get``；
    3. 每份文件里的本机 URL 字面量都 ⊆ 声明表；
    4. ``/v1/version`` 不在任何一张表里。
    """
    ling = _load_module("ling_cli_probe_all", LING_PY)
    publish = _load_module("ling_publish_probe_all", PUBLISH_PY)
    updater = _load_module("ling_updater_probe_all", UPDATER_PY)
    declared = set(ling.HTTP_PROBE_PATHS)
    assert declared == set(publish.HTTP_PROBE_PATHS), (
        f"ling={ling.HTTP_PROBE_PATHS} publish={publish.HTTP_PROBE_PATHS}"
    )
    assert declared == set(updater.HTTP_PROBE_PATHS), (
        f"ling={ling.HTTP_PROBE_PATHS} updater={updater.HTTP_PROBE_PATHS}"
    )
    assert declared == {"/readyz"}, declared
    assert "/v1/version" not in declared

    for path in (LING_PY, PUBLISH_PY, UPDATER_PY):
        used, literals, violations = _scan_probe_file(path)
        assert not violations, violations
        assert literals <= declared, (path, sorted(literals - declared))
        assert used <= declared, (path, sorted(used - declared))
    # publish.py / updater.py 目前没有自己的 http_probe（探活走一次性容器），
    # 所以 used 允许为空；但 ling.py 必须真的走过 helper，否则门禁没覆盖对象。
    assert _scan_probe_file(LING_PY)[0]


def test_probe_scanner_catches_bypasses_in_all_three_shapes(tmp_path: Path) -> None:
    """元测试：扫描器本身要能抓到绕过 helper / 非字面量 / 未登记 URL。"""
    examples = {
        "direct.py": (
            'def f(base):\n'
            '    return http_get(base + "/v1/version", timeout=5)\n',
            "绕过 http_probe",
        ),
        "method.py": (
            'class C:\n'
            '    def f(self):\n'
            '        return self.http_get("http://127.0.0.1:6520/v1/version", 5)\n',
            "绕过 http_probe",
        ),
        "literal.py": (
            'URL = "http://127.0.0.1:6520/v1/version"\n',
            "未登记的 URL 字面量",
        ),
    }
    for name, (source, expected) in examples.items():
        _used, literals, violations = _scan_probe_source(source, label=name)
        assert violations or literals - {"/readyz"}, (name, source)
        if expected == "未登记的 URL 字面量":
            # URL 字面量由调用方拿 literals 与声明表比对；保持与文件级测试
            # 同一口径（未登记 → literals 多出来）。
            assert literals == {"/v1/version"}, (name, literals)
        else:
            assert any(expected in item for item in violations), (name, violations)
    # 合法写法：http_probe(base, "/readyz") 不报违规。
    used, literals, violations = _scan_probe_source(
        'def http_probe(base, path, *, timeout):\n'
        '    return http_get(base + path, timeout=timeout)\n'
        '\ndef f(base):\n'
        '    return http_probe(base, "/readyz", timeout=5)\n',
        label="ok.py",
    )
    assert not violations, violations
    assert used == {"/readyz"} and literals == set()


def test_container_version_probe_matches_publish_script() -> None:
    """探针字符串与发布脚本逐字节一致（两边复制，必须同源）。"""
    ling = _load_module("ling_cli_probe_sync", LING_PY)
    publish = _load_module("ling_publish_probe_sync", PUBLISH_PY)
    assert ling.CONTAINER_VERSION_PROBE == publish.CONTAINER_VERSION_PROBE
    assert ling.CONTAINER_VERSION_EXEC_TIMEOUT == publish.CONTAINER_VERSION_EXEC_TIMEOUT
    assert set(ling.HTTP_PROBE_PATHS) == set(publish.HTTP_PROBE_PATHS)


def test_container_version_probe_uses_process_evidence_without_http() -> None:
    """探针字符串里没有任何 HTTP/端点字样：它是进程证据，不是一次请求。"""
    ling = _load_module("ling_cli_probe_script", LING_PY)
    script = ling.CONTAINER_VERSION_PROBE
    for forbidden in ("http", "urllib", "/v1/", "socket", "request"):
        assert forbidden not in script, (forbidden, script)
    assert "LING_COMMIT" in script
    assert "__version__" in script


def test_container_version_probe_agrees_with_real_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """把探针交给**真实应用自己的包**执行：version/commit 与 `/v1/version` 同源。

    版本取 ``ling.__version__``（探针自己 import 的就是它）。不取
    ``current_version()``：它优先读**已安装发行版**的元数据，而开发 venv 里
    装着的那个 ``ling`` 与 ``src/`` 不是同一份；容器里两者来自同一个 wheel，
    所以那边自然一致。commit 与 ``build_commit()``（`/v1/version` 用的同一个
    函数）比较，证明读的是同一个 ``LING_COMMIT``。
    """
    from ling import __version__ as app_version
    from ling.api.routes.version import build_commit, current_version

    ling = _load_module("ling_cli_probe_exec", LING_PY)
    monkeypatch.setenv("LING_COMMIT", COMMIT)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    proc = subprocess.run(
        [sys.executable, "-c", ling.CONTAINER_VERSION_PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = ling.parse_container_version_probe(proc.stdout)
    assert payload == {"version": app_version, "commit": build_commit()}
    assert payload["commit"] == COMMIT
    assert current_version(), "/v1/version 的版本实现必须仍能给出非空版本"


@contextlib.asynccontextmanager
async def _real_app_client(tmp_path: Path):
    """起**真实 FastAPI 应用**（ASGI，不绑端口、不需要 PostgreSQL）。

    sqlite + `create_all_for_dev` 足够让 `/readyz` 真的 200、`/v1/version` 真的
    401 —— 这正是要核的鉴权事实；用例不依赖 127.0.0.1:55432 的测试库。
    """
    from httpx import ASGITransport, AsyncClient

    from ling.app import create_app
    from ling.config import Settings
    from ling.infra.db import create_all_for_dev, dispose_engine, init_engine

    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path / "data",
        bootstrap_admin_user="tester",
        bootstrap_admin_password="tester-password",
        root_key="probe-root-key",
        test_mode=True,
        cookie_secure=False,
        actor_id="actor_ling",
        persona_id="persona_ling",
        access_mode="off",
        access_list=frozenset(),
        onebot_ws_port=None,
        onebot_ws_url=None,
        qqbot_enabled=False,
    )
    await dispose_engine()
    engine = init_engine(settings)
    await create_all_for_dev(engine)
    app = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:6520"
        ) as client:
            yield client
    finally:
        await dispose_engine()


async def test_declared_http_probe_paths_are_unauthenticated_on_real_app(
    tmp_path: Path,
) -> None:
    """每一条登记过的探活路径，在真实应用上未鉴权都必须**不是** 401/403。"""
    ling = _load_module("ling_cli_probe_auth", LING_PY)
    assert ling.HTTP_PROBE_PATHS, "探活路径表不能是空的"
    async with _real_app_client(tmp_path) as client:
        for probe in ling.HTTP_PROBE_PATHS:
            response = await client.get(probe)
            assert response.status_code not in (401, 403), (
                f"探活 {probe} 需要鉴权（{response.status_code}）：安装/状态/诊断"
                f"不持有用户凭据，这样的探活永远失败。响应：{response.text[:200]}"
            )
            assert response.status_code == 200, (probe, response.status_code, response.text[:200])
        # 这一条是根治点：`/v1/version` 未鉴权就是 401，不能用它当探活。
        version = await client.get("/v1/version")
        assert version.status_code == 401, version.text
        assert version.json()["error"]["code"] == "unauthorized"


async def test_v1_version_still_requires_auth(tmp_path: Path) -> None:
    """`/v1/version` 仍然要鉴权：没有为了给探活让路而把它改开。"""
    async with _real_app_client(tmp_path) as client:
        for path in (
            "/v1/version",
            "/v1/version/check-release",
            "/v1/version/update",
            "/v1/version/image-update",
            "/v1/health/model",
        ):
            response = await client.get(path)
            assert response.status_code == 401, (path, response.status_code, response.text[:200])
        assert (await client.get("/healthz")).status_code == 200
