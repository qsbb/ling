"""deploy/install.sh 的隔离回归测试。

这些测试在临时复制的仓库里真实执行 shell 分支，只用 stub 替换
pnpm / docker / curl / sleep / python3，不会真的构建、部署或碰现有容器。
docker stub 的 `compose config --format json` 会按 Compose 语义生成
api/worker 的 build args（shell 环境 > --env-file，重复键后者胜），
以及 `compose build` 会继承到的 PIP_INDEX_URL；curl stub 区分 pypi.org
探测与 /readyz，且所有分支都只返回预设退出码，不发起真实网络请求。

**这是 stub-only 验证**：stub 自己也在近似解析 .env，所以这些用例不能
证明真实 Docker Compose 的解释（尤其 `KEY: value` 不是 Docker `.env`
的文档语法；stub 接受它只是为了覆盖 install.sh 的“不自己解析”原则）。
真实 Compose 行为仍必须在部署机单独验证。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
WEB_FILES = ["package.json", "pnpm-lock.yaml", "index.html", "tsconfig.json", "vite.config.ts"]
BACKEND_FILES = [
    "pyproject.toml",
    "README.md",
    "alembic.ini",
    "deploy/Dockerfile.api",
    "deploy/entrypoint.sh",
]
BACKEND_DIRS = ["src", "connectors", "migrations"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")


@pytest.fixture()
def fake_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy2(INSTALL_SH, repo / "deploy" / "install.sh")
    shutil.copy2(REPO_ROOT / "deploy" / "compose.yaml", repo / "deploy" / "compose.yaml")

    # web_manifest() 的清单包含后端发布输入；这里复制真实的小文件/目录，
    # 让清单校验跑在可运行的仓库结构上，而不是只靠 mock 返回。
    for rel in BACKEND_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    for rel in BACKEND_DIRS:
        shutil.copytree(REPO_ROOT / rel, repo / rel, ignore=IGNORE)

    web = repo / "clients" / "web-console"
    web.mkdir(parents=True)
    for name in WEB_FILES:
        shutil.copy2(REPO_ROOT / "clients" / "web-console" / name, web / name)
    shutil.copytree(REPO_ROOT / "clients" / "web-console" / "src", web / "src", ignore=IGNORE)
    (web / "public").mkdir()
    (web / "public" / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    (web / "public" / "nested").mkdir()
    (web / "public" / "nested" / "static.txt").write_text("static\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "pnpm", _PNPM)
    _write_stub(bin_dir / "docker", _DOCKER)
    _write_stub(bin_dir / "curl", _CURL)
    _write_stub(bin_dir / "sleep", _SLEEP)
    _write_stub(
        bin_dir / "python3",
        f'#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} "$@"\n',
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CALL_LOG"] = str(tmp_path / "calls.log")
    for key in (
        "PIP_INDEX_URL",
        "LING_WEB_DIST_HOST",
        "LING_PYTHON_BASE",
        "LING_POSTGRES_IMAGE",
        "LING_NGINX_IMAGE",
        "LING_EDGE_PORT",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
    ):
        env.pop(key, None)
    return repo, env


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


_PNPM = """#!/usr/bin/env bash
set -u
printf 'pnpm %s\\n' "$*" >> "${CALL_LOG:?}"
if [ "${1:-}" = "--version" ]; then
  printf '9.9.9\\n'
  exit 0
fi
if [ "${1:-}" = "install" ]; then
  exit "${PNPM_INSTALL_EXIT:-0}"
fi
if [ "${1:-}" = "build" ]; then
  out="dist"
  prev=""
  for arg in "$@"; do
    if [ "$prev" = "--outDir" ]; then
      out="$arg"
    fi
    prev="$arg"
  done
  if [ "${PNPM_IGNORE_OUTDIR:-0}" = "1" ]; then
    out="dist"
  fi
  mkdir -p "$out/assets"
  printf '<!doctype html><title>fresh</title>\\n' > "$out/index.html"
  printf 'console.log("fresh");\\n' > "$out/assets/app.js"
  if [ -d public ]; then
    cp -R public/. "$out/"
  fi
  if [ "${PNPM_SYMLINK:-0}" = "1" ]; then
    ln -s index.html "$out/linked.html"
  fi
  code="${PNPM_BUILD_EXIT:-0}"
  if [ "$code" -ne 0 ]; then
    exit "$code"
  fi
  exit 0
fi
exit 0
"""


_DOCKER = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys


def log(message):
    with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
        stream.write(message + "\\n")


def compose_options(raw_args):
    env_file = None
    compose_file = pathlib.Path("deploy/compose.yaml")
    index = 0
    while index < len(raw_args):
        if raw_args[index] == "--env-file" and index + 1 < len(raw_args):
            env_file = pathlib.Path(raw_args[index + 1])
            index += 2
            continue
        if raw_args[index] == "-f" and index + 1 < len(raw_args):
            compose_file = pathlib.Path(raw_args[index + 1])
            index += 2
            continue
        index += 1
    return env_file, compose_file


def env_file_values(env_file):
    values = {}
    if env_file is None or not env_file.is_file():
        return values
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        positions = [p for p in (line.find("="), line.find(":")) if p >= 0]
        if not positions:
            continue
        position = min(positions)
        if position <= 0:
            continue
        key = line[:position].strip()
        value = line[position + 1 :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\\\"":
            value = value[1:-1]
        values[key] = value
    return values


def pip_index(values):
    # Compose 的优先级：shell 环境变量（含空串）> --env-file。
    if "PIP_INDEX_URL" in os.environ:
        return "shell", os.environ["PIP_INDEX_URL"]
    if "PIP_INDEX_URL" in values:
        return "env-file", values["PIP_INDEX_URL"]
    return "unset", None


args = sys.argv[1:]
log("docker " + " ".join(args))
if not args:
    sys.exit(0)
if args[0] == "info":
    sys.exit(0)
if args[0] == "version":
    print("27.0.0" if "--format" in args else "Docker version 27.0.0")
    sys.exit(0)
if args[0] == "image":
    sys.exit(int(os.environ.get("STUB_IMAGE_INSPECT_EXIT", "1")))
if args[0] == "pull":
    sys.exit(int(os.environ.get("STUB_PULL_EXIT", "0")))
if args[0] != "compose":
    # 严格模式：未知 docker argv 一律非 0，避免“任何命令都成功”的假阳性。
    log("docker-unknown " + " ".join(args))
    sys.exit(64)
if "version" in args:
    sys.exit(0)
if "config" in args:
    code = int(os.environ.get("STUB_CONFIG_EXIT", "0"))
    fail_on = int(os.environ.get("STUB_CONFIG_FAIL_ON_CALL", "0"))
    if fail_on:
        # 第几次 compose config 调用失败（1 = 挂载校验，2 = 构建前 PIP 判定）。
        try:
            logged = pathlib.Path(os.environ["CALL_LOG"]).read_text(encoding="utf-8")
        except OSError:
            logged = ""
        config_count = sum(
            1
            for line in logged.splitlines()
            if line.startswith("docker ") and " config " in line
        )
        if config_count == fail_on:
            code = int(os.environ.get("STUB_CONFIG_FAIL_ON_CALL_EXIT", "1"))
    if code:
        sys.stderr.write(os.environ.get("STUB_CONFIG_STDERR", "compose config failed\\n"))
        sys.exit(code)
    override = os.environ.get("STUB_CONFIG_JSON")
    if override is not None:
        sys.stdout.write(override)
        sys.exit(0)
    env_file, compose_file = compose_options(args)
    values = env_file_values(env_file)
    pip_source, pip_value = pip_index(values)
    # 只记录来源，不把值写进 call log；config 必须真的把 build args 解析出来，
    # 否则 install.sh 的 config 判定就会落空（而不是 stub 与脚本互相印证）。
    log("config-pip " + json.dumps({"source": pip_source}, sort_keys=True))
    # Compose 的优先级：shell 环境变量 > --env-file；`${VAR:-default}` 在
    # 变量为空时也取默认值。
    if "LING_WEB_DIST_HOST" in os.environ:
        raw_source = os.environ["LING_WEB_DIST_HOST"]
    else:
        raw_source = values.get("LING_WEB_DIST_HOST", "")
    raw_source = raw_source or "../clients/web-console/dist"
    base = compose_file.resolve().parent
    source = raw_source if os.path.isabs(raw_source) else str((base / raw_source).resolve())
    worker_pip = os.environ.get("STUB_WORKER_PIP_INDEX", pip_value or "")
    # Compose 的端口语义：edge 发布 `${LING_EDGE_PORT:-8080}`，shell 优先于
    # --env-file。install.sh 的就绪/状态端口必须来自这里的解析结果。
    if "LING_EDGE_PORT" in os.environ:
        edge_port = os.environ["LING_EDGE_PORT"]
    else:
        edge_port = values.get("LING_EDGE_PORT", "") or "8080"
    log("config-edge-port " + edge_port)
    config = {
        "services": {
            "api": {
                "build": {"args": {"PIP_INDEX_URL": pip_value or ""}},
                "environment": {"LING_WEB_DIST": "/app/web"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": source,
                        "target": "/app/web",
                        "read_only": True,
                    }
                ],
            },
            "edge": {
                "image": "nginx:alpine",
                "ports": [
                    {
                        "mode": "ingress",
                        "target": 80,
                        "published": edge_port,
                        "protocol": "tcp",
                    }
                ],
            },
            "worker": {"build": {"args": {"PIP_INDEX_URL": worker_pip}}},
        }
    }
    print(json.dumps(config))
    sys.exit(0)
if "build" in args:
    env_file, _ = compose_options(args)
    pip_source, pip_value = pip_index(env_file_values(env_file))
    log(
        "build-pip-index "
        + json.dumps({"source": pip_source, "value": pip_value}, sort_keys=True)
    )
    sys.exit(int(os.environ.get("STUB_BUILD_EXIT", "0")))
if "up" in args:
    if os.environ.get("STUB_UP_REQUIRE_DIST") == "1" and not pathlib.Path(
        "clients/web-console/dist"
    ).is_dir():
        sys.stderr.write("up: clients/web-console/dist is missing")
        sys.exit(9)
    chmod_mode = os.environ.get("STUB_UP_CHMOD_DIST")
    if chmod_mode:
        os.chmod("clients/web-console/dist", int(chmod_mode, 8))
    sys.exit(int(os.environ.get("STUB_UP_EXIT", "0")))
if any(flag in args for flag in ("down", "restart", "ps", "logs")):
    sys.exit(0)
# 严格模式：没实现的 compose 子命令/未知 argv 一律非 0。
log("compose-unknown " + " ".join(args))
sys.exit(64)
"""


_CURL = """#!/usr/bin/env bash
set -u
printf 'curl %s\\n' "$*" >> "${CALL_LOG:?}"
status="${CURL_EXIT:-0}"
case "$*" in
  *pypi.org/simple*) status="${CURL_PYPI_EXIT:-$status}" ;;
  *readyz*) status="${CURL_READYZ_EXIT:-$status}" ;;
  *)
    # 严格模式：install.sh 只该请求这三类 URL；别的 URL 视为未知 argv。
    printf 'curl-unknown %s\\n' "$*" >> "${CALL_LOG:?}"
    exit 64 ;;
esac
exit "$status"
"""


_SLEEP = """#!/usr/bin/env bash
set -u
printf 'sleep %s\\n' "$*" >> "${CALL_LOG:?}"
exit 0
"""


def run_install(
    repo: Path,
    env: dict[str, str],
    *args: str,
    **extra_env: str | None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = Path(env["CALL_LOG"])
    log.write_text("", encoding="utf-8")
    run_env = env.copy()
    for key, value in extra_env.items():
        if value is None:
            run_env.pop(key, None)
        else:
            run_env[key] = value
    proc = subprocess.run(
        ["bash", "deploy/install.sh", *args],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, log.read_text(encoding="utf-8")


def dist_dir(repo: Path) -> Path:
    return repo / "clients" / "web-console" / "dist"


def manifest_path(repo: Path) -> Path:
    return dist_dir(repo) / ".ling-web-manifest.json"


def env_file_values(path: Path) -> dict[str, str]:
    """测试断言用：按 Compose 的宽松 .env 语法取值（不用于产品行为判断）。"""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def tree_hash(root: Path) -> dict[str, str]:
    root = Path(root)
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = "link:" + os.readlink(path)
        elif path.is_file():
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[rel] = "dir"
    return result


def seed_old_dist(repo: Path, *, collisions: bool = False) -> None:
    dist = dist_dir(repo)
    if dist.is_symlink() or dist.is_file():
        dist.unlink()
    elif dist.exists():
        shutil.rmtree(dist)
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "old.js").write_text("old asset\n", encoding="utf-8")
    (dist / "index.html").write_text("old index\n", encoding="utf-8")
    (dist / "old-only.js").write_text("old only\n", encoding="utf-8")
    if collisions:
        shutil.rmtree(dist / "assets")
        (dist / "assets").write_text("assets is a file\n", encoding="utf-8")
        (dist / "robots.txt").mkdir()
        (dist / "robots.txt" / "child.txt").write_text("old robots dir\n", encoding="utf-8")


def make_prebuilt(repo: Path, env: dict[str, str]) -> None:
    proc, calls = run_install(repo, env, "--build-web-only")
    assert proc.returncode == 0, proc.stderr
    assert "docker " not in calls
    assert manifest_path(repo).is_file()


def assert_no_launch(calls: str) -> None:
    assert "docker compose" not in calls or "up -d" not in calls


def build_pip_index(calls: str) -> dict[str, str | None]:
    """返回 docker stub 记录的 compose build 实际继承到的 pip 索引。"""
    lines = [line for line in calls.splitlines() if line.startswith("build-pip-index ")]
    assert len(lines) == 1, calls
    data = json.loads(lines[0].split(" ", 1)[1])
    assert isinstance(data, dict)
    return data


def docker_build_line(calls: str) -> str:
    return next(
        line
        for line in calls.splitlines()
        if line.startswith("docker compose") and line.endswith(" build")
    )


# ---------------------------------------------------------------------------
# 默认安装 / 构建失败
# ---------------------------------------------------------------------------


def test_default_install_builds_then_starts_and_prints_help_without_secrets(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    proc, calls = run_install(repo, env, STUB_UP_REQUIRE_DIST="1")

    assert proc.returncode == 0, proc.stderr
    assert "pnpm install --frozen-lockfile" in calls
    assert "pnpm build --outDir" in calls
    assert "--emptyOutDir" in calls
    assert "docker compose" in calls
    assert "config --format json" in calls
    assert "--env-file deploy/.env" in calls
    assert "up -d --no-build" in calls
    assert "up -d --build" not in calls
    docker_build = next(
        line
        for line in calls.splitlines()
        if line.startswith("docker compose") and line.endswith(" build")
    )
    assert calls.index("pnpm build") < calls.index(docker_build)
    assert calls.index("config --format json") < calls.index(docker_build)
    assert calls.index(docker_build) < calls.index("up -d --no-build")
    assert "compose 实际挂载的 /app/web 与本次校验产物一致" in proc.stdout

    data = json.loads(manifest_path(repo).read_text(encoding="utf-8"))
    assert data["schema"] == 1
    for name in ("index.html", "assets/app.js", "robots.txt", "nested/static.txt"):
        assert name in data["dist_files"]
    assert "src/App.tsx" in data["release_inputs"]
    assert "public/robots.txt" in data["release_inputs"]
    assert "backend/src/ling/__init__.py" in data["release_inputs"]

    env_text = (repo / "deploy" / ".env").read_text(encoding="utf-8")
    # 只断言**生效行**：绝对路径只允许出现在注释里当人工核对提示。旧断言直接
    # 在全文找绝对路径，命中的其实是注释，无法证明“有效值不是绝对路径”。
    effective = env_file_values(repo / "deploy" / ".env")
    assert effective["LING_WEB_DIST_HOST"] == "../clients/web-console/dist"
    custom_lines = [
        line for line in env_text.splitlines() if line.startswith("LING_WEB_DIST_HOST=")
    ]
    assert custom_lines == ["LING_WEB_DIST_HOST=../clients/web-console/dist"]
    assert "http://127.0.0.1:18080" in proc.stdout
    assert "用户名      ling" in proc.stdout
    assert "首次安装" in proc.stdout
    assert "--offline" not in calls

    for key in ("POSTGRES_PASSWORD", "LING_ROOT_KEY"):
        value = env_text.split(f"{key}=", 1)[1].splitlines()[0]
        assert value
        assert value not in proc.stdout
        assert value not in proc.stderr

    assert (dist_dir(repo) / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")


# ---------------------------------------------------------------------------
# 对外端口：就绪等待 / 摘要 / --status 都取 Compose 解析出的 edge 发布端口
# ---------------------------------------------------------------------------


def readyz_curls(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if line.startswith("curl ") and "/readyz" in line]


def console_url(stdout: str) -> str:
    line = next(item for item in stdout.splitlines() if "控制台" in item)
    return line.split()[-1]


@pytest.mark.parametrize("edge_port", ["8080", "9090"])
def test_existing_env_edge_port_drives_readiness_and_summary(
    fake_repo: tuple[Path, dict[str, str]], edge_port: str
) -> None:
    """已有 .env 的 LING_EDGE_PORT 才是真实发布端口；install 不能等 18080。"""
    repo, env = fake_repo
    env_file = repo / "deploy" / ".env"
    env_file.write_text(
        f"POSTGRES_PASSWORD=db-password\nLING_ROOT_KEY=root-key\nLING_EDGE_PORT={edge_port}\n",
        encoding="utf-8",
    )
    before = env_file.read_bytes()

    proc, calls = run_install(repo, env)

    assert proc.returncode == 0, proc.stderr
    curls = readyz_curls(calls)
    assert curls, calls
    assert all(f"127.0.0.1:{edge_port}/readyz" in line for line in curls), curls
    assert all("18080" not in line for line in curls), curls
    assert console_url(proc.stdout) == f"http://127.0.0.1:{edge_port}"
    # 安装器不写已有 env；也不把 compose 配置（含口令）打到输出里。
    assert env_file.read_bytes() == before
    assert '"services"' not in proc.stdout + proc.stderr


@pytest.mark.parametrize("edge_port", ["8080", "9090"])
def test_status_uses_existing_env_edge_port(
    fake_repo: tuple[Path, dict[str, str]], edge_port: str
) -> None:
    repo, env = fake_repo
    (repo / "deploy" / ".env").write_text(f"LING_EDGE_PORT={edge_port}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--status")

    assert proc.returncode == 0, proc.stderr
    curls = readyz_curls(calls)
    assert curls, calls
    assert all(f"127.0.0.1:{edge_port}/readyz" in line for line in curls), curls
    assert all("18080" not in line for line in curls), curls


def test_restart_waits_on_compose_resolved_port(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    (repo / "deploy" / ".env").write_text("LING_EDGE_PORT=8080\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--restart")

    assert proc.returncode == 0, proc.stderr
    assert " restart" in calls
    curls = readyz_curls(calls)
    assert curls and all("127.0.0.1:8080/readyz" in line for line in curls), curls
    assert all("18080" not in line for line in curls), curls


def test_fresh_install_keeps_default_port_and_documented_replicas(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env)

    assert proc.returncode == 0, proc.stderr
    values = env_file_values(repo / "deploy" / ".env")
    assert values["LING_EDGE_PORT"] == "18080"
    # 与 compose 默认（${LING_API_REPLICAS:-2}）、env.example、文档一致。
    assert values["LING_API_REPLICAS"] == "2"
    curls = readyz_curls(calls)
    assert curls and all("127.0.0.1:18080/readyz" in line for line in curls), curls
    assert console_url(proc.stdout) == "http://127.0.0.1:18080"


def test_port_option_only_affects_fresh_env(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    """没有 .env 时 --port 仍决定新生成 env 与就绪端口（显式语义不变）。"""
    repo, env = fake_repo

    proc, calls = run_install(repo, env, "--port", "18081")

    assert proc.returncode == 0, proc.stderr
    values = env_file_values(repo / "deploy" / ".env")
    assert values["LING_EDGE_PORT"] == "18081"
    curls = readyz_curls(calls)
    assert curls and all("127.0.0.1:18081/readyz" in line for line in curls), curls
    assert console_url(proc.stdout) == "http://127.0.0.1:18081"


def test_port_option_conflicting_with_existing_env_fails_fast(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    """安装器不写已有 .env，所以 --port 与真实发布端口冲突时必须 fail closed。"""
    repo, env = fake_repo
    env_file = repo / "deploy" / ".env"
    env_file.write_text("LING_EDGE_PORT=8080\n", encoding="utf-8")
    before = env_file.read_bytes()

    proc, calls = run_install(repo, env, "--port", "18080")

    assert proc.returncode != 0
    assert "--port 18080" in proc.stderr
    assert "LING_EDGE_PORT" in proc.stderr
    assert "8080" in proc.stderr
    assert "up -d" not in calls
    assert readyz_curls(calls) == []
    assert env_file.read_bytes() == before


def test_unresolvable_edge_port_fails_closed_without_guessing(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    config = json.dumps(
        {
            "services": {
                "api": {
                    "build": {"args": {"PIP_INDEX_URL": ""}},
                    "environment": {"LING_WEB_DIST": "/app/web"},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(dist_dir(repo)),
                            "target": "/app/web",
                            "read_only": True,
                        }
                    ],
                },
                "worker": {"build": {"args": {"PIP_INDEX_URL": ""}}},
            }
        }
    )

    proc, calls = run_install(repo, env, "--prebuilt-web", STUB_CONFIG_JSON=config)

    assert proc.returncode != 0
    assert "对外端口" in proc.stderr
    assert readyz_curls(calls) == []
    assert "up -d" not in calls


def test_status_without_resolvable_port_does_not_guess(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env, "--status", STUB_CONFIG_JSON='{"services": {}}')

    assert proc.returncode == 0, proc.stderr
    assert "无法从 compose 配置确定对外端口" in proc.stdout
    assert readyz_curls(calls) == []


def test_offline_web_is_explicit(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    proc, calls = run_install(repo, env, "--offline-web")

    assert proc.returncode == 0, proc.stderr
    assert "pnpm install --frozen-lockfile --offline" in calls
    assert "显式离线模式" in proc.stdout


def test_build_failure_never_starts_even_with_old_dist(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, PNPM_BUILD_EXIT="9")

    assert proc.returncode != 0
    assert "前端依赖安装/构建失败" in proc.stderr
    assert "up -d" not in calls
    assert "docker " not in calls
    assert not (repo / "deploy" / ".env").exists()
    assert tree_hash(dist_dir(repo)) == before


def test_build_ignoring_outdir_that_fails_restores_old_dist(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, PNPM_IGNORE_OUTDIR="1", PNPM_BUILD_EXIT="9")

    assert proc.returncode != 0
    assert "在线 dist 被改动" in proc.stderr
    assert "up -d" not in calls
    assert "docker " not in calls
    assert tree_hash(dist_dir(repo)) == before


def test_build_ignoring_outdir_that_succeeds_is_rejected_and_restored(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, PNPM_IGNORE_OUTDIR="1")

    assert proc.returncode != 0
    assert "构建改动了在线 dist" in proc.stderr
    assert "up -d" not in calls
    assert "docker " not in calls
    assert tree_hash(dist_dir(repo)) == before


# ---------------------------------------------------------------------------
# PyPI 源探测（必须在 compose build 之前）
# ---------------------------------------------------------------------------

TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"


def test_pypi_reachable_keeps_default_index(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env)

    assert proc.returncode == 0, proc.stderr
    probe = "curl -sf -m 8 -o /dev/null https://pypi.org/simple/"
    assert probe in calls
    assert calls.index(probe) < calls.index(docker_build_line(calls))
    assert build_pip_index(calls) == {"source": "unset", "value": None}
    assert "pypi.tuna" not in proc.stdout
    assert "pypi.tuna" not in proc.stderr


def test_pypi_unreachable_exports_mirror_before_build(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    probe = "curl -sf -m 8 -o /dev/null https://pypi.org/simple/"
    assert probe in calls
    assert calls.index(probe) < calls.index(docker_build_line(calls))
    assert build_pip_index(calls) == {"source": "shell", "value": TUNA}
    assert "pypi.org 探测失败" in proc.stdout
    assert TUNA in proc.stdout
    # 探测结果只用于本次 shell 环境，不写进 deploy/.env。
    env_text = (repo / "deploy" / ".env").read_text(encoding="utf-8")
    assert "PIP_INDEX_URL" not in env_text


def test_shell_pip_index_is_not_overridden_and_reaches_build(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    pinned = "https://user:shell-secret@mirror.example.invalid/simple"

    # 即使官方源探测会失败，也不能覆盖 shell 的显式选择。
    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1", PIP_INDEX_URL=pinned)

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "shell", "value": pinned}
    assert "PIP_INDEX_URL 已由环境显式设置" in proc.stdout
    assert TUNA not in proc.stdout
    assert "shell-secret" not in proc.stdout
    assert "shell-secret" not in proc.stderr


def test_shell_pip_index_takes_precedence_over_env_file(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    shell_pinned = "https://shell.example.invalid/simple"
    env_pinned = "https://env.example.invalid/simple"
    (repo / "deploy" / ".env").write_text(f"PIP_INDEX_URL={env_pinned}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1", PIP_INDEX_URL=shell_pinned)

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "shell", "value": shell_pinned}
    assert "PIP_INDEX_URL 已由环境显式设置" in proc.stdout
    assert "deploy/.env 已显式设置" not in proc.stdout
    assert env_pinned not in proc.stdout
    assert env_pinned not in proc.stderr


def test_env_file_pip_index_is_not_overridden_and_reaches_build(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    pinned = "https://private.example.invalid/simple?token=env-file-secret"
    (repo / "deploy" / ".env").write_text(f"PIP_INDEX_URL={pinned}\n", encoding="utf-8")

    # 即使官方源探测会失败，也不能覆盖 deploy/.env 的显式选择。
    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "env-file", "value": pinned}
    assert "deploy/.env 已显式设置 PIP_INDEX_URL" in proc.stdout
    assert TUNA not in proc.stdout
    assert "env-file-secret" not in proc.stdout
    assert "env-file-secret" not in proc.stderr


@pytest.mark.parametrize(
    "line",
    [
        "PIP_INDEX_URL={url}\n",
        "export PIP_INDEX_URL={url}\n",
        "PIP_INDEX_URL = {url}\n",
        "PIP_INDEX_URL: {url}\n",
        'PIP_INDEX_URL="{url}"\n',
    ],
)
def test_env_file_pip_index_syntaxes_are_honored(
    fake_repo: tuple[Path, dict[str, str]], line: str
) -> None:
    # stub-only：docker stub 接受 `KEY: value`，但 Docker Compose 的 --env-file
    # 文档语法是 KEY=VALUE。这里只验证 install.sh 把解释权交给 compose config，
    # 不证明真实 Docker 会接受冒号写法。
    repo, env = fake_repo
    pinned = "https://private.example.invalid/simple"
    (repo / "deploy" / ".env").write_text(line.format(url=pinned), encoding="utf-8")

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "env-file", "value": pinned}
    assert TUNA not in proc.stdout


def test_empty_shell_pip_index_lets_env_file_win(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    """shell 导出空串时必须在 dc 调用前 unset，否则会遮住 .env 的私有源。"""
    repo, env = fake_repo
    pinned = "https://private.example.invalid/simple?token=env-only-secret"
    (repo / "deploy" / ".env").write_text(f"PIP_INDEX_URL={pinned}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1", PIP_INDEX_URL="")

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "env-file", "value": pinned}
    assert "deploy/.env 已显式设置 PIP_INDEX_URL" in proc.stdout
    assert "PIP_INDEX_URL 已由环境显式设置" not in proc.stdout
    assert "env-only-secret" not in proc.stdout
    assert "env-only-secret" not in proc.stderr
    assert calls.index("config --format json") < calls.index(docker_build_line(calls))


def test_empty_shell_pip_index_without_env_file_still_probes(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1", PIP_INDEX_URL="")

    assert proc.returncode == 0, proc.stderr
    probe = "curl -sf -m 8 -o /dev/null https://pypi.org/simple/"
    assert probe in calls
    assert calls.index(probe) < calls.index(docker_build_line(calls))
    assert build_pip_index(calls) == {"source": "shell", "value": TUNA}
    assert "pypi.org 探测失败" in proc.stdout


def test_duplicate_env_file_key_last_empty_falls_back_to_mirror(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    """重复键由 Compose 决定后者胜；最后一行是空值时必须重新探测。"""
    repo, env = fake_repo
    (repo / "deploy" / ".env").write_text(
        "PIP_INDEX_URL=https://first.example.invalid/simple\nPIP_INDEX_URL=\n",
        encoding="utf-8",
    )

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    probe = "curl -sf -m 8 -o /dev/null https://pypi.org/simple/"
    assert probe in calls
    assert calls.index("config --format json") < calls.index(probe)
    assert calls.index(probe) < calls.index(docker_build_line(calls))
    assert build_pip_index(calls) == {"source": "shell", "value": TUNA}
    assert "pypi.org 探测失败" in proc.stdout
    assert "first.example.invalid" not in proc.stdout
    assert "first.example.invalid" not in proc.stderr


def test_duplicate_env_file_key_last_nonempty_is_explicit(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    pinned = "https://private.example.invalid/simple"
    (repo / "deploy" / ".env").write_text(
        f"PIP_INDEX_URL=\nPIP_INDEX_URL={pinned}\n", encoding="utf-8"
    )

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "env-file", "value": pinned}
    assert "deploy/.env 已显式设置 PIP_INDEX_URL" in proc.stdout
    assert pinned not in proc.stdout
    assert pinned not in proc.stderr


def test_env_file_colon_value_with_equals_is_not_truncated_stub_only(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    """stub-only：冒号写法不是 Docker .env 的文档语法，这里只锁住“不自解析”。

    值以 `?token=` 结尾时，旧的 `[[ == *=* ]]` 手工解析会把它截成空串并
    误判为“未配置”，从而用镜像源覆盖私有源。新实现只看 compose config。
    """
    repo, env = fake_repo
    pinned = "https://private.example.invalid/simple?token="
    (repo / "deploy" / ".env").write_text(f"PIP_INDEX_URL: {pinned}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, CURL_PYPI_EXIT="1")

    assert proc.returncode == 0, proc.stderr
    assert "pypi.org/simple" not in calls
    assert build_pip_index(calls) == {"source": "env-file", "value": pinned}
    assert "deploy/.env 已显式设置 PIP_INDEX_URL" in proc.stdout
    assert TUNA not in proc.stdout
    assert TUNA not in proc.stderr


def test_compose_config_failure_during_pip_resolution_fails_closed(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    secret = "pip-config-do-not-leak"
    (repo / "deploy" / ".env").write_text(
        f"POSTGRES_PASSWORD=super-db-password\nLING_ROOT_KEY={secret}\n",
        encoding="utf-8",
    )

    proc, calls = run_install(
        repo,
        env,
        STUB_CONFIG_FAIL_ON_CALL="2",
        STUB_CONFIG_STDERR=f"compose config failed LING_ROOT_KEY={secret}\n",
    )

    assert proc.returncode != 0
    assert "无法从 compose 配置确认 PIP_INDEX_URL" in proc.stderr
    assert "docker compose config 执行失败" in proc.stderr
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert "super-db-password" not in proc.stdout
    assert "super-db-password" not in proc.stderr
    assert "build-pip-index" not in calls
    assert "up -d" not in calls
    assert not any(
        line.startswith("docker compose") and line.endswith(" build") for line in calls.splitlines()
    )


def test_pip_index_api_worker_mismatch_fails_closed_without_values(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    secret = "worker-private-secret"
    worker_url = f"https://worker.example.invalid/simple?token={secret}"

    proc, calls = run_install(repo, env, STUB_WORKER_PIP_INDEX=worker_url)

    assert proc.returncode != 0
    assert "api 与 worker 的 PIP_INDEX_URL 构建参数不一致" in proc.stderr
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert worker_url not in proc.stdout
    assert worker_url not in proc.stderr
    assert "build-pip-index" not in calls
    assert "up -d" not in calls
    assert not any(
        line.startswith("docker compose") and line.endswith(" build") for line in calls.splitlines()
    )


# ---------------------------------------------------------------------------
# 预构建清单
# ---------------------------------------------------------------------------


def test_prebuilt_matching_dist_installs_without_rebuild(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode == 0, proc.stderr
    assert "pnpm " not in calls
    assert "up -d --no-build" in calls
    assert "http://127.0.0.1:18080" in proc.stdout
    assert tree_hash(dist_dir(repo)) == before


@pytest.mark.parametrize(
    "changed",
    [
        "clients/web-console/src/App.tsx",
        "clients/web-console/public/robots.txt",
        "clients/web-console/pnpm-lock.yaml",
        "pyproject.toml",
        "src/ling/__init__.py",
    ],
)
def test_prebuilt_rejects_changed_release_input(
    fake_repo: tuple[Path, dict[str, str]], changed: str
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    target = repo / changed
    target.write_text(target.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "不是同一发布版本" in proc.stderr
    assert "up -d" not in calls


def test_prebuilt_rejects_changed_asset_hash(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    (dist_dir(repo) / "assets" / "app.js").write_text("tampered\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "产物哈希不匹配" in proc.stderr
    assert "up -d" not in calls


def test_prebuilt_rejects_missing_asset(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    (dist_dir(repo) / "assets" / "app.js").unlink()

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "路径越界或文件不存在" in proc.stderr
    assert "up -d" not in calls


def test_prebuilt_rejects_extra_dist_file(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    (dist_dir(repo) / "assets" / "stale.js").write_text("stale\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "文件集合与清单不一致" in proc.stderr
    assert "up -d" not in calls


def test_prebuilt_rejects_missing_manifest(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    manifest_path(repo).unlink()

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "缺少前端清单" in proc.stderr
    assert "up -d" not in calls


@pytest.mark.parametrize(
    "manifest",
    [
        '{"schema": 0, "release_inputs": {}, "dist_files": {}}\n',
        '{"schema": 1}\n',
        "not json\n",
    ],
)
def test_prebuilt_rejects_old_or_invalid_manifest(
    fake_repo: tuple[Path, dict[str, str]], manifest: str
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    manifest_path(repo).write_text(manifest, encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "前端校验失败" in proc.stderr
    assert "up -d" not in calls


# ---------------------------------------------------------------------------
# Compose 挂载 / env 优先级
# ---------------------------------------------------------------------------


def test_prebuilt_rejects_mount_source_other_than_verified_dist(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    other = repo / "old-dist"
    other.mkdir()
    (other / "index.html").write_text("old\n", encoding="utf-8")
    (repo / "deploy" / ".env").write_text(f"LING_WEB_DIST_HOST={other}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode != 0
    assert "不是本次校验产物" in proc.stderr
    assert "up -d" not in calls


def test_prebuilt_accepts_relative_mount_matching_verified_dist(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    # env.example 里的默认写法就是相对 compose 文件目录的路径。
    (repo / "deploy" / ".env").write_text(
        "LING_WEB_DIST_HOST=../clients/web-console/dist\n", encoding="utf-8"
    )

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode == 0, proc.stderr
    assert "up -d --no-build" in calls


@pytest.mark.parametrize(
    "line",
    [
        "LING_WEB_DIST_HOST={dist}\n",
        "LING_WEB_DIST_HOST: {dist}\n",
        "LING_WEB_DIST_HOST = {dist}\n",
        "export LING_WEB_DIST_HOST={dist}\n",
        'LING_WEB_DIST_HOST="{dist}"\n',
    ],
)
def test_compose_env_file_syntaxes_are_delegated_to_compose(
    fake_repo: tuple[Path, dict[str, str]], line: str
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    (repo / "deploy" / ".env").write_text(line.format(dist=dist_dir(repo)), encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode == 0, proc.stderr
    assert "up -d --no-build" in calls
    assert "--env-file deploy/.env" in calls
    assert "compose 实际挂载的 /app/web 与本次校验产物一致" in proc.stdout


def test_shell_env_overrides_env_file(fake_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    other = repo / "old-dist"
    other.mkdir()
    (other / "index.html").write_text("old\n", encoding="utf-8")
    (repo / "deploy" / ".env").write_text(f"LING_WEB_DIST_HOST={other}\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--prebuilt-web", LING_WEB_DIST_HOST=str(dist_dir(repo)))

    assert proc.returncode == 0, proc.stderr
    assert "up -d --no-build" in calls


def test_shell_env_can_point_compose_at_other_dist_and_fail_closed(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    other = repo / "old-dist"
    other.mkdir()
    (other / "index.html").write_text("old\n", encoding="utf-8")
    (repo / "deploy" / ".env").write_text(
        f"LING_WEB_DIST_HOST={dist_dir(repo)}\n", encoding="utf-8"
    )

    proc, calls = run_install(repo, env, "--prebuilt-web", LING_WEB_DIST_HOST=str(other))

    assert proc.returncode != 0
    assert "不是本次校验产物" in proc.stderr
    assert "up -d" not in calls


def test_empty_shell_env_uses_compose_default(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    other = repo / "old-dist"
    other.mkdir()
    (other / "index.html").write_text("old\n", encoding="utf-8")
    (repo / "deploy" / ".env").write_text(f"LING_WEB_DIST_HOST={other}\n", encoding="utf-8")

    # Compose 的 ${VAR:-default} 在变量为空时使用默认值；
    # 这里默认值 ../clients/web-console/dist 正好是本次产物。
    proc, calls = run_install(repo, env, "--prebuilt-web", LING_WEB_DIST_HOST="")

    assert proc.returncode == 0, proc.stderr
    assert "up -d --no-build" in calls


def test_compose_config_failure_does_not_leak_secrets(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    secret = "do-not-leak-this-secret"
    (repo / "deploy" / ".env").write_text(
        "POSTGRES_PASSWORD=super-db-password\n"
        f"LING_ROOT_KEY={secret}\n"
        f"LING_WEB_DIST_HOST={dist_dir(repo)}\n",
        encoding="utf-8",
    )

    proc, calls = run_install(
        repo,
        env,
        "--prebuilt-web",
        STUB_CONFIG_EXIT="1",
        STUB_CONFIG_STDERR=f"compose config failed LING_ROOT_KEY={secret}\n",
    )

    assert proc.returncode != 0
    assert "docker compose config 执行失败" in proc.stderr
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert "super-db-password" not in proc.stdout
    assert "super-db-password" not in proc.stderr
    assert "up -d" not in calls


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("{bad json", "无法解析"),
        ('{"services": {}}', "没有任何服务挂载"),
        (
            '{"services": {"api": {"environment": {"LING_WEB_DIST": "/app/web"}, '
            '"volumes": [{"type": "bind", "source": "/x", "target": "/app/web", "read_only": true}, '
            '{"type": "bind", "source": "/y", "target": "/app/web", "read_only": true}]}}}',
            "挂载不唯一",
        ),
        (
            '{"services": {"web": {"environment": {"LING_WEB_DIST": "/app/web"}, '
            '"volumes": [{"type": "bind", "source": "/x", "target": "/app/web", "read_only": true}]}}}',
            "应挂在 api 上",
        ),
        (
            '{"services": {"api": {"environment": {"LING_WEB_DIST": "/app/web"}, '
            '"volumes": [{"type": "volume", "source": "x", "target": "/app/web", "read_only": true}]}}}',
            "不是 bind mount",
        ),
        (
            '{"services": {"api": {"environment": {"LING_WEB_DIST": "/app/web"}, '
            '"volumes": [{"type": "bind", "source": "/x", "target": "/app/web", "read_only": false}]}}}',
            "必须是只读挂载",
        ),
        (
            '{"services": {"api": {"environment": {}, '
            '"volumes": [{"type": "bind", "source": "/x", "target": "/app/web", "read_only": true}]}}}',
            "LING_WEB_DIST",
        ),
    ],
)
def test_compose_config_shape_is_fail_closed(
    fake_repo: tuple[Path, dict[str, str]], config: str, expected: str
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)

    proc, calls = run_install(repo, env, "--prebuilt-web", STUB_CONFIG_JSON=config)

    assert proc.returncode != 0
    assert expected in proc.stderr
    assert "up -d" not in calls
    assert "build" not in calls


def test_summary_never_prints_configured_secrets(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    (repo / "deploy" / ".env").write_text(
        "LING_BOOTSTRAP_ADMIN=ops\n"
        "LING_BOOTSTRAP_ADMIN_PASSWORD=do-not-print-me\n"
        "POSTGRES_PASSWORD=db-secret\n"
        "LING_ROOT_KEY=root-secret\n"
        f"LING_WEB_DIST_HOST={dist_dir(repo)}\n",
        encoding="utf-8",
    )

    proc, calls = run_install(repo, env, "--prebuilt-web")

    assert proc.returncode == 0, proc.stderr
    assert "用户名      ops" in proc.stdout
    assert "不在此显示" in proc.stdout
    assert "--logs api | grep -A4 '首次安装'" in proc.stdout
    for secret in ("do-not-print-me", "db-secret", "root-secret"):
        assert secret not in proc.stdout
        assert secret not in proc.stderr
    assert "up -d --no-build" in calls


def test_compose_config_secrets_are_not_printed(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    make_prebuilt(repo, env)
    secret = "config-output-secret"
    config = json.dumps(
        {
            "services": {
                "api": {
                    "build": {"args": {"PIP_INDEX_URL": ""}},
                    "environment": {
                        "LING_WEB_DIST": "/app/web",
                        "POSTGRES_PASSWORD": secret,
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(dist_dir(repo)),
                            "target": "/app/web",
                            "read_only": True,
                        }
                    ],
                },
                "worker": {"build": {"args": {"PIP_INDEX_URL": ""}}},
                "edge": {
                    "image": "nginx:alpine",
                    "ports": [
                        {"mode": "ingress", "target": 80, "published": "18080", "protocol": "tcp"}
                    ],
                },
            }
        }
    )

    proc, calls = run_install(repo, env, "--prebuilt-web", STUB_CONFIG_JSON=config)

    assert proc.returncode == 0, proc.stderr
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert "compose 实际挂载的 /app/web 与本次校验产物一致" in proc.stdout


# ---------------------------------------------------------------------------
# 发布时序 / 原地替换
# ---------------------------------------------------------------------------


def test_conflicting_web_modes_are_rejected(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env, "--prebuilt-web", "--build-web-only")

    assert proc.returncode == 2
    assert "不能同时使用" in proc.stderr
    assert calls == ""


def test_build_web_only_does_not_touch_runtime_config_or_home(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    old_inode = os.stat(dist_dir(repo)).st_ino
    real_home = repo.parent / "real-home"
    real_home.mkdir()
    env = dict(env, HOME=str(real_home))

    proc, calls = run_install(repo, env, "--build-web-only")

    assert proc.returncode == 0, proc.stderr
    assert "docker " not in calls
    assert not (repo / "deploy" / ".env").exists()
    assert not (repo / ".home").exists()
    assert manifest_path(repo).is_file()
    assert "会更新" in proc.stdout
    assert os.stat(dist_dir(repo)).st_ino == old_inode
    assert (dist_dir(repo) / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")


def test_publish_preserves_dist_inode_and_handles_collisions(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo, collisions=True)
    old_inode = os.stat(dist_dir(repo)).st_ino

    proc, calls = run_install(repo, env)

    assert proc.returncode == 0, proc.stderr
    assert os.stat(dist_dir(repo)).st_ino == old_inode
    assert (dist_dir(repo) / "assets").is_dir()
    assert (dist_dir(repo) / "assets" / "app.js").is_file()
    assert (dist_dir(repo) / "robots.txt").is_file()
    assert not (dist_dir(repo) / "old-only.js").exists()
    assert (dist_dir(repo) / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "保留目录 inode" in proc.stdout or "inode" in proc.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录写权限限制")
def test_publish_failure_keeps_old_index_and_reports_manual_recovery(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    old_inode = os.stat(dist_dir(repo)).st_ino
    temp_match = None

    proc, calls = run_install(repo, env, STUB_UP_CHMOD_DIST="555")

    try:
        assert proc.returncode != 0
        assert "发布前端失败" in proc.stderr
        assert "恢复失败" in proc.stdout
        assert "保留安装临时目录" in proc.stderr
        assert "安装完成" not in proc.stdout
        assert "up -d --no-build" in calls
        assert os.stat(dist_dir(repo)).st_ino == old_inode
        assert (dist_dir(repo) / "index.html").read_text(encoding="utf-8") == "old index\n"
        assert (dist_dir(repo) / "assets" / "old.js").is_file()
        assert (dist_dir(repo) / "old-only.js").is_file()
        assert not (dist_dir(repo) / "assets" / "app.js").exists()
        temp_match = re.search(r"保留安装临时目录供人工恢复：(\S+)", proc.stderr)
        assert temp_match is not None
    finally:
        os.chmod(dist_dir(repo), 0o755)
        if temp_match is not None:
            shutil.rmtree(temp_match.group(1), ignore_errors=True)


def test_compose_up_failure_keeps_old_frontend(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, STUB_UP_EXIT="7")

    assert proc.returncode != 0
    assert "启动失败" in proc.stderr
    assert "退出码 7" in proc.stdout
    assert "前端已发布" not in proc.stdout
    assert tree_hash(dist_dir(repo)) == before


def test_image_build_failure_keeps_old_frontend(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, STUB_BUILD_EXIT="1")

    assert proc.returncode != 0
    assert "镜像构建失败" in proc.stderr
    assert "up -d" not in calls
    assert tree_hash(dist_dir(repo)) == before


def test_readyz_failure_keeps_old_frontend(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    seed_old_dist(repo)
    before = tree_hash(dist_dir(repo))

    proc, calls = run_install(repo, env, CURL_EXIT="1")

    assert proc.returncode != 0
    assert "等了 120 秒还没就绪" in proc.stderr
    assert "前端尚未发布" in proc.stdout
    assert "前端已发布" not in proc.stdout
    assert tree_hash(dist_dir(repo)) == before


def test_source_symlink_is_rejected_before_publish(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo

    proc, calls = run_install(repo, env, PNPM_SYMLINK="1")

    assert proc.returncode != 0
    assert "不接受符号链接" in proc.stderr
    assert "up -d" not in calls
    assert not dist_dir(repo).exists() or not any(dist_dir(repo).iterdir())


def test_live_dist_symlink_is_rejected(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    real = repo.parent / "real-dist"
    real.mkdir()
    (real / "index.html").write_text("old\n", encoding="utf-8")
    dist_dir(repo).symlink_to(real)

    proc, calls = run_install(repo, env)

    assert proc.returncode != 0
    assert "在线前端目录是符号链接" in proc.stderr
    assert "docker " not in calls
    assert dist_dir(repo).is_symlink()


def test_help_does_not_print_shell_implementation(
    fake_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = fake_repo
    proc, calls = run_install(repo, env, "--help")

    assert proc.returncode == 0
    assert "set -uo pipefail" not in proc.stdout
    assert "不是发布签名或安全边界" in proc.stdout
    assert "docker " not in calls


def test_compose_default_mount_is_the_dist_checked_by_installer() -> None:
    compose = (REPO_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert "${LING_WEB_DIST_HOST:-../clients/web-console/dist}:/app/web:ro" in compose
    assert "LING_WEB_DIST: /app/web" in compose
