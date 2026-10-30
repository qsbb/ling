"""deploy 更新通道接线、install.sh 锁/阶段标记/构建提交的回归测试。

**全部是 stub-only**：docker / pnpm / curl / sleep / python3 都是临时目录里的
替身，测试不会构建镜像、启动容器、访问网络或碰真实部署目录。真实 Docker /
Compose 的解释仍需在部署机单独验证。

覆盖：
- compose 只给 api 注入 LING_UPDATE_*，worker 不挂更新通道、不拿配置；
- api 的 requests 可写、status 只读，Web 侧没有 Docker socket / 宿主源码树；
- Dockerfile 的 LING_COMMIT 构建参数传递；
- install.sh 的跨 release 锁（默认路径、.env symlink 共享、伪 FD 拒绝、
  真实 flock 经 pass_fds 穿过两层 shell 的正向路径）；
- 继承锁 FD 的**重新校验**：别人持独占/共享锁时传同 inode 但未加锁的 FD 会被
  拒绝；共享锁 FD 可安全升级；FD 指向别的文件也拒绝；
- 锁文件本身只接受普通文件：symlink（含指向 deploy/.env 的）/目录/FIFO 被拒，
  目标文件字节不变（不跟随、不截断），已有内容也不会被写；
- 对外端口以 Compose 解析出的 edge 发布端口为准（这里只覆盖 wiring 层的夹具，
  端到端断言在 tests/test_deploy_install.py）；
- LING_INSTALL_PHASE_FILE 的阶段顺序 build → deploy → verify → complete；
- LING_COMMIT 格式错误在构建前中止，且不写阶段标记；
- canonical deploy/.env 只读：普通/只读/symlink 三种形态都字节不变，基础镜像
  只导出到进程环境、显式值不被覆盖、默认镜像回退只换同一 tag 且不跨主版本；
- 新生成的 .env 不写死本检出的绝对 dist；
- 宿主 override 的内容不变量：不能搬走 /app/web，也不能搬走/换身份持久命名卷
  （db-data/app-data 的 identity 与使用服务/target/只读），合法 edge 绝对 bind
  不受影响；FIFO override 被拒；
- HOME 回退到仓库内已忽略的 .run/home、.run/docker，且不删除/搬移旧目录。
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile.api"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "env.example"

VALID_COMMIT = "0123456789abcdef0123456789abcdef01234567"

# ---------------------------------------------------------------------------
# stub 工具
# ---------------------------------------------------------------------------

_PNPM = """#!/usr/bin/env bash
set -u
printf 'pnpm %s\\n' "$*" >> "${CALL_LOG:?}"
if [ "${1:-}" = "--version" ]; then
  printf '9.9.9\\n'
  exit 0
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
  mkdir -p "$out/assets"
  printf '<!doctype html><title>stub</title>\\n' > "$out/index.html"
  printf 'console.log("stub");\\n' > "$out/assets/app.js"
  exit 0
fi
exit 0
"""

_DOCKER = """#!/usr/bin/env bash
set -u
log() { printf '%s\\n' "$*" >> "${CALL_LOG:?}"; }
phase_now() { cat "${STUB_PHASE_FILE:-/nonexistent}" 2>/dev/null || true; }
log "docker $*"
if [ "${1:-}" != "compose" ]; then
  log "docker-home ${HOME-<unset>} ${DOCKER_CONFIG-<unset>}"
fi
case "${1:-}" in
  info) exit 0 ;;
  version) printf '27.0.0\\n'; exit 0 ;;
  image) exit "${STUB_IMAGE_INSPECT_EXIT:-1}" ;;
  pull) exit "${STUB_PULL_EXIT:-0}" ;;
  compose) ;;
  *)
    # 严格模式：未知 docker argv 不得静默成功（会让“什么都没验证”看起来通过）。
    log "docker-unknown $*"
    exit 64 ;;
esac
sub=""
for arg in "$@"; do
  case "$arg" in
    config|build|up|down|restart|ps|logs|version) sub="$arg"; break ;;
  esac
done
case "$sub" in
  version) exit 0 ;;
  config) printf '%s' "${STUB_CONFIG_JSON:-}" ; exit 0 ;;
  build)
    log "phase-at-build $(phase_now)"
    log "build-commit ${LING_COMMIT-<unset>}"
    exit 0 ;;
  up)
    log "phase-at-up $(phase_now)"
    exit "${STUB_UP_EXIT:-0}" ;;
  down|restart|ps|logs) exit 0 ;;
  *)
    log "compose-unknown $*"
    exit 64 ;;
esac
"""

_CURL = """#!/usr/bin/env bash
set -u
printf 'curl %s\\n' "$*" >> "${CALL_LOG:?}"
case "$*" in
  *readyz*)
    printf 'phase-at-readyz %s\\n' "$(cat "${STUB_PHASE_FILE:-/nonexistent}" 2>/dev/null || true)" >> "${CALL_LOG:?}"
    exit "${STUB_READYZ_EXIT:-0}" ;;
  *pypi.org/simple*) exit "${STUB_PYPI_EXIT:-0}" ;;
  *)
    printf 'curl-unknown %s\\n' "$*" >> "${CALL_LOG:?}"
    exit 64 ;;
esac
"""

_SLEEP = """#!/usr/bin/env bash
exit 0
"""


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _touch(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _compose_config(repo: Path) -> str:
    """docker compose config --format json 的替身输出（只含 install.sh 会读的字段）。"""
    dist = str((repo / "clients" / "web-console" / "dist").resolve())
    return json.dumps(
        {
            "services": {
                "api": {
                    "build": {"args": {"PIP_INDEX_URL": "", "LING_COMMIT": "${LING_COMMIT:-}"}},
                    "environment": {"LING_WEB_DIST": "/app/web"},
                    "volumes": [
                        {"type": "bind", "source": dist, "target": "/app/web", "read_only": True}
                    ],
                },
                "worker": {
                    "build": {"args": {"PIP_INDEX_URL": "", "LING_COMMIT": "${LING_COMMIT:-}"}}
                },
                "edge": {
                    "image": "nginx:alpine",
                    # 真实 Compose 的 config 输出是长语法；install.sh 的就绪/状态
                    # 端口只认这里的 target=80 发布端口。
                    "ports": [
                        {"mode": "ingress", "target": 80, "published": "18080", "protocol": "tcp"}
                    ],
                },
            }
        }
    )


@pytest.fixture()
def deploy_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    _touch(repo / "deploy" / "install.sh", INSTALL_SH.read_text(encoding="utf-8"))
    _touch(repo / "deploy" / "compose.yaml", COMPOSE.read_text(encoding="utf-8"))
    for rel in (
        "pyproject.toml",
        "README.md",
        "alembic.ini",
        "deploy/Dockerfile.api",
        "deploy/entrypoint.sh",
    ):
        _touch(repo / rel, f"# stub {rel}\n")
    for rel in ("src/ling/__init__.py", "connectors/__init__.py", "migrations/versions/.keep"):
        _touch(repo / rel, "" if rel.endswith(".keep") else '"""stub"""\n')

    web = repo / "clients" / "web-console"
    for name in ("package.json", "pnpm-lock.yaml", "index.html", "tsconfig.json", "vite.config.ts"):
        _touch(web / name, "{}\n")
    _touch(web / "src" / "main.ts", "export {};\n")
    _touch(web / "public" / "robots.txt", "User-agent: *\n")

    bin_dir = tmp_path / "bin"
    _write_exe(bin_dir / "pnpm", _PNPM)
    _write_exe(bin_dir / "docker", _DOCKER)
    _write_exe(bin_dir / "curl", _CURL)
    _write_exe(bin_dir / "sleep", _SLEEP)
    _write_exe(bin_dir / "python3", f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CALL_LOG"] = str(tmp_path / "calls.log")
    env["STUB_CONFIG_JSON"] = _compose_config(repo)
    for key in (
        "LING_COMMIT",
        "LING_INSTALL_PHASE_FILE",
        "LING_INSTALL_LOCK_FD",
        "LING_INSTALL_COMPOSE_OVERRIDE",
        "LING_WEB_DIST_HOST",
        "PIP_INDEX_URL",
        "STUB_PHASE_FILE",
    ):
        env.pop(key, None)
    return repo, env


def run_install(
    repo: Path,
    env: dict[str, str],
    *args: str,
    **extra_env: str | None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    run_env = copy.deepcopy(env)
    for key, value in extra_env.items():
        if value is None:
            run_env.pop(key, None)
        else:
            run_env[key] = value
    Path(env["CALL_LOG"]).write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "deploy/install.sh", *args],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc, Path(env["CALL_LOG"]).read_text(encoding="utf-8")


_LOCK_HOLDER = """
import fcntl, os, sys, time
mode = fcntl.LOCK_SH if (len(sys.argv) > 2 and sys.argv[2] == "shared") else fcntl.LOCK_EX
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, mode)
print("ready", flush=True)
time.sleep(120)
"""


def hold_lock(lock_path: Path, mode: str = "exclusive") -> subprocess.Popen[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path), mode],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    return holder


def stat_is_fifo(path: Path) -> bool:
    return stat.S_ISFIFO(os.lstat(path).st_mode)


def stop_holder(holder: subprocess.Popen[str]) -> None:
    holder.terminate()
    try:
        holder.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
        holder.kill()
        holder.wait(timeout=10)


# ---------------------------------------------------------------------------
# compose / Dockerfile / env.example 接线
# ---------------------------------------------------------------------------


def test_compose_only_api_gets_update_channel() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "docker.sock" not in text
    api = text.split("  api:\n", 1)[1].split("  edge:\n", 1)[0]
    worker = text.split("  worker:\n", 1)[1].split("\nvolumes:\n", 1)[0]

    assert "LING_UPDATE_ENABLED: ${LING_UPDATE_ENABLED:-0}" in api
    assert "LING_UPDATE_REQUESTS_DIR: /run/ling-update/requests" in api
    assert "LING_UPDATE_STATUS_DIR: /run/ling-update/status" in api
    # 注释里可以提，但 worker 的 environment 不能真的出现这些键。
    assert re.search(r"^\s+LING_UPDATE_[A-Z_]+: ", worker, re.MULTILINE) is None

    assert (
        "${LING_UPDATE_REQUESTS_HOST:-../.run/update/requests}:/run/ling-update/requests:rw" in api
    )
    assert "${LING_UPDATE_STATUS_HOST:-../.run/update/status}:/run/ling-update/status:ro" in api
    assert "/run/ling-update/" not in worker

    assert "LING_COMMIT: ${LING_COMMIT:-}" in api
    assert "LING_COMMIT: ${LING_COMMIT:-}" in worker
    # Web 只从只读 dist 挂载；api 根文件系统只读。
    assert "${LING_WEB_DIST_HOST:-../clients/web-console/dist}:/app/web:ro" in api
    assert "read_only: true" in api


def test_compose_semantics_with_yaml_parser_if_available() -> None:
    # 不为了测试额外安装依赖：有 PyYAML 就做语义检查，没有就依赖文本级检查。
    yaml = pytest.importorskip("yaml", reason="环境没有 YAML parser；仅保留文本级接线检查")
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = data["services"]
    api_env = services["api"]["environment"]
    worker_env = services["worker"]["environment"]
    assert api_env["LING_UPDATE_ENABLED"] == "${LING_UPDATE_ENABLED:-0}"
    assert api_env["LING_UPDATE_REQUESTS_DIR"] == "/run/ling-update/requests"
    assert api_env["LING_UPDATE_STATUS_DIR"] == "/run/ling-update/status"
    assert "LING_UPDATE_ENABLED" not in worker_env
    assert "LING_UPDATE_REQUESTS_DIR" not in worker_env
    assert "LING_UPDATE_STATUS_DIR" not in worker_env
    assert services["worker"]["volumes"] == ["app-data:/data"]
    assert services["api"]["build"]["args"]["LING_COMMIT"] == "${LING_COMMIT:-}"
    assert services["worker"]["build"]["args"]["LING_COMMIT"] == "${LING_COMMIT:-}"


def test_dockerfile_injects_commit_only_at_runtime() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    runtime = text.index("AS runtime")
    assert 'ARG LING_COMMIT=""' in text
    assert "ENV LING_COMMIT=${LING_COMMIT}" in text
    assert runtime < text.index('ARG LING_COMMIT=""')
    assert runtime < text.index("ENV LING_COMMIT=${LING_COMMIT}")


def test_env_example_keeps_update_disabled() -> None:
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    assert values["LING_UPDATE_ENABLED"] == "0"
    assert values["LING_UPDATE_REQUESTS_HOST"] == "../.run/update/requests"
    assert values["LING_UPDATE_STATUS_HOST"] == "../.run/update/status"
    assert "LING_UPDATE_ENABLED=1" not in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_env_example_has_no_duplicate_or_dead_keys() -> None:
    """同一个 .env 里重复定义会让“改了一处、另一处还是旧值”。"""
    keys: list[str] = []
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"env.example 重复定义：{duplicates}"
    # 端口只有一个发布来源；LING_API_PORT 从未被 compose 读取，删掉避免误导。
    assert "LING_EDGE_PORT" in keys
    assert "LING_API_PORT" not in keys
    # 构建期 PyPI 源与备份目录都要在模板里说明（check_compose 也要求）。
    assert "PIP_INDEX_URL" in keys
    assert "LING_BACKUP_DIR" in keys


# ---------------------------------------------------------------------------
# install.sh 静态契约
# ---------------------------------------------------------------------------


def test_installer_lock_contract_is_static_and_has_no_skip_flag() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ".ling-install.lock" in text
    assert "fcntl.flock" in text
    assert "LOCK_EX | fcntl.LOCK_NB" in text
    assert "install|restart|down|build-web-only" in text
    assert "command -v flock" not in text
    lowered = text.lower()
    for forbidden in ("skip_lock", "no_lock", "allow_unlocked", "unsafe_no_lock"):
        assert forbidden not in lowered


def test_installer_phase_markers_are_ordered() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("do_install() {")
    end = text.index("# 所有会改变运行状态")
    body = text[start:end]
    assert body.index("phase_write build") < body.index("ensure_web")
    assert body.index("phase_write deploy") < body.index("start_services")
    assert body.index("phase_write verify") < body.index("wait_ready")
    assert body.index("publish_web") < body.index("phase_write complete")
    assert body.rindex("phase_write complete") < body.index("install_summary")


# ---------------------------------------------------------------------------
# 安装流程行为（stub-only）
# ---------------------------------------------------------------------------


def test_install_writes_phase_sequence_and_commit_build_arg(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    phase = tmp_path / "phase"
    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        LING_INSTALL_PHASE_FILE=str(phase),
        STUB_PHASE_FILE=str(phase),
    )
    assert proc.returncode == 0, proc.stderr
    assert phase.read_text(encoding="utf-8") == "complete\n"
    assert "phase-at-build build" in calls
    assert "phase-at-up deploy" in calls
    assert "phase-at-readyz verify" in calls
    assert f"build-commit {VALID_COMMIT}" in calls
    assert (repo / "deploy" / ".ling-install.lock").is_file()


def test_install_rejects_bad_commit_before_building(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    phase = tmp_path / "phase"
    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT="not-a-commit",
        LING_INSTALL_PHASE_FILE=str(phase),
        STUB_PHASE_FILE=str(phase),
    )
    assert proc.returncode != 0
    assert "LING_COMMIT 格式错误" in proc.stderr
    # 已经在“构建阶段”，但没有真正开始构建（执行器会判 failed 而不是 needs_manual）。
    assert phase.read_text(encoding="utf-8") == "build\n"
    assert "phase-at-build" not in calls


def test_install_rejects_relative_or_symlink_phase_file(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, LING_INSTALL_PHASE_FILE="relative-phase"
    )
    assert proc.returncode != 0
    assert "LING_INSTALL_PHASE_FILE" in proc.stderr
    assert "phase-at-build" not in calls

    target = tmp_path / "real-phase"
    target.write_text("old\n", encoding="utf-8")
    link = tmp_path / "phase-link"
    link.symlink_to(target)
    proc, _ = run_install(repo, env, LING_COMMIT=VALID_COMMIT, LING_INSTALL_PHASE_FILE=str(link))
    assert proc.returncode != 0
    assert "符号链接" in proc.stderr
    assert target.read_text(encoding="utf-8") == "old\n"


def test_phase_stays_deploy_when_service_start_fails(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """compose up 失败：已经动过容器状态，标记必须停在 deploy（执行器判 needs_manual）。"""
    repo, env = deploy_repo
    phase = tmp_path / "phase"
    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        LING_INSTALL_PHASE_FILE=str(phase),
        STUB_PHASE_FILE=str(phase),
        STUB_UP_EXIT="7",
    )

    assert proc.returncode != 0
    assert "启动失败" in proc.stderr
    assert phase.read_text(encoding="utf-8") == "deploy\n"
    assert "phase-at-up deploy" in calls
    # 前端不能已经发布（发布在 readyz 之后）。
    assert "前端已发布" not in proc.stdout


def test_phase_stays_verify_when_readyz_fails(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """容器已起但没就绪：标记停在 verify，前端仍未发布。"""
    repo, env = deploy_repo
    phase = tmp_path / "phase"
    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        LING_INSTALL_PHASE_FILE=str(phase),
        STUB_PHASE_FILE=str(phase),
        STUB_READYZ_EXIT="1",
    )

    assert proc.returncode != 0
    assert "就绪" in proc.stderr
    assert phase.read_text(encoding="utf-8") == "verify\n"
    assert "phase-at-readyz verify" in calls
    assert "前端尚未发布" in proc.stdout


def test_installer_lock_blocks_concurrent_run(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = deploy_repo
    lock = repo / "deploy" / ".ling-install.lock"
    holder = hold_lock(lock)
    try:
        proc, _ = run_install(repo, env, "--down")
    finally:
        stop_holder(holder)
    assert proc.returncode != 0
    assert "另一个 install.sh 正在运行" in proc.stderr

    # 锁释放后可以正常运行（也证明没有“锁文件存在就算占用”）。
    proc, calls = run_install(repo, env, "--down")
    assert proc.returncode == 0, proc.stderr
    assert "docker compose" in calls


def test_installer_lock_follows_env_symlink(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    shared = tmp_path / "shared"
    shared.mkdir()
    shared_env = shared / "ling.env"
    shared_env.write_text("LING_TEST_MODE=0\n", encoding="utf-8")
    (repo / "deploy" / ".env").symlink_to(shared_env)

    lock = shared / ".ling-install.lock"
    holder = hold_lock(lock)
    try:
        proc, _ = run_install(repo, env, "--down")
    finally:
        stop_holder(holder)
    assert proc.returncode != 0, "共享 .env 的锁没有被遵守"
    assert "另一个 install.sh 正在运行" in proc.stderr
    assert not (repo / "deploy" / ".ling-install.lock").exists()


def test_installer_rejects_forged_lock_fd(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = deploy_repo
    proc, calls = run_install(repo, env, "--down", LING_INSTALL_LOCK_FD="3")
    assert proc.returncode != 0
    assert "LING_INSTALL_LOCK_FD" in proc.stderr
    assert "docker compose" not in calls


# ---------------------------------------------------------------------------
# 宿主 Compose override（LING_INSTALL_COMPOSE_OVERRIDE）
# ---------------------------------------------------------------------------

# 这些用例仍然全是 stub-only：docker stub 不解析 override 的内容，只记录命令行。
# 因此它们证明的是 install.sh 的传参/校验契约，**不是**真实 Compose 的合并语义，
# 真实行为仍必须在部署机上单独验证。


def _f_flags(line: str) -> list[str]:
    parts = line.split()
    return [parts[index + 1] for index, token in enumerate(parts[:-1]) if token == "-f"]


def dc_call_flags(calls: str) -> list[list[str]]:
    """返回每次 docker compose 调用的 -f 列表（跳过 `docker compose version` 探针）。"""
    flags: list[list[str]] = []
    for line in calls.splitlines():
        if not line.startswith("docker compose"):
            continue
        if line.strip() == "docker compose version":
            continue
        flags.append(_f_flags(line))
    return flags


def _write_override(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "host-override.json"
    path.write_text(body, encoding="utf-8")
    return path


def override_flag_calls(calls: str) -> tuple[list[list[str]], list[list[str]]]:
    """拆出“只有基础 compose 的只读基准解析”和“真正带 override 的调用”。

    安装流程在传了 override 时会多做一次 `-f deploy/compose.yaml` 的 config，
    用来和 base+override 的解析结果比对持久数据卷；它不该执行 build/up。
    """
    probes: list[list[str]] = []
    overridden: list[list[str]] = []
    for flags in dc_call_flags(calls):
        if flags == ["deploy/compose.yaml"]:
            probes.append(flags)
        else:
            overridden.append(flags)
    return probes, overridden


def assert_override_calls_keep_both_files(calls: str, override: Path) -> None:
    probes, overridden = override_flag_calls(calls)
    assert overridden, calls
    assert all(item == ["deploy/compose.yaml", str(override)] for item in overridden), overridden
    # 基准解析最多一次，而且只是 config（不会触发 build/up）。
    assert len(probes) <= 1, probes


def test_compose_calls_use_only_the_repo_file_by_default(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = deploy_repo
    proc, calls = run_install(repo, env, LING_COMMIT=VALID_COMMIT)

    assert proc.returncode == 0, proc.stderr
    flags = dc_call_flags(calls)
    assert flags, calls
    assert all(item == ["deploy/compose.yaml"] for item in flags), flags
    assert "--env-file deploy/.env" in calls


def test_host_override_is_appended_after_repo_file_and_content_is_untouched(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    secret = "override-file-secret"
    override = _write_override(
        tmp_path,
        json.dumps({"services": {"api": {"environment": {"LING_TEST_SECRET": secret}}}}),
    )
    before = override.read_bytes()

    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, LING_INSTALL_COMPOSE_OVERRIDE=str(override)
    )

    assert proc.returncode == 0, proc.stderr
    # 恒定顺序：先仓库文件，再宿主 override；--env-file deploy/.env 仍然保留。
    assert_override_calls_keep_both_files(calls, override)
    assert "--env-file deploy/.env" in calls
    # 只追加参数：内容原样、不打印、不复制进仓库、不写进 deploy/.env。
    assert override.read_bytes() == before
    assert secret not in proc.stdout
    assert secret not in proc.stderr
    assert str(override) in proc.stdout
    assert [path for path in repo.rglob(override.name)] == []


@pytest.mark.parametrize("args", [("--down",), ("--status",), ("--restart",)])
def test_host_override_applies_to_every_docker_action(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path, args: tuple[str, ...]
) -> None:
    repo, env = deploy_repo
    override = _write_override(tmp_path, "{}\n")

    proc, calls = run_install(repo, env, *args, LING_INSTALL_COMPOSE_OVERRIDE=str(override))

    assert proc.returncode == 0, proc.stderr
    assert_override_calls_keep_both_files(calls, override)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("relative", "必须是绝对路径"),
        ("missing", "文件不存在"),
        ("directory", "必须是普通文件"),
        ("symlink", "不能是符号链接"),
        ("fifo", "必须是普通文件"),
        ("oversized", "1 MiB"),
    ],
)
def test_host_override_is_validated_before_anything_else(
    deploy_repo: tuple[Path, dict[str, str]],
    tmp_path: Path,
    setup: str,
    expected: str,
) -> None:
    repo, env = deploy_repo
    target = tmp_path / "override.json"
    if setup == "relative":
        value = "deploy/override.json"
        (repo / "deploy" / "override.json").write_text("{}\n", encoding="utf-8")
    elif setup == "missing":
        value = str(target)
    elif setup == "directory":
        target.mkdir()
        value = str(target)
    elif setup == "symlink":
        real = tmp_path / "real-override.json"
        real.write_text("{}\n", encoding="utf-8")
        target.symlink_to(real)
        value = str(target)
    elif setup == "fifo":
        os.mkfifo(target)
        value = str(target)
    else:
        target.write_text("#" * (1024 * 1024 + 1), encoding="utf-8")
        value = str(target)

    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, LING_INSTALL_COMPOSE_OVERRIDE=value
    )

    assert proc.returncode != 0
    assert "LING_INSTALL_COMPOSE_OVERRIDE" in proc.stderr
    assert expected in proc.stderr
    # 在任何 docker / pnpm 动作之前就失败。
    assert calls == ""
    assert "pnpm" not in calls


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受文件读权限限制")
def test_host_override_must_be_readable(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    override = _write_override(tmp_path, "{}\n")
    os.chmod(override, 0o000)
    try:
        proc, calls = run_install(
            repo, env, LING_COMMIT=VALID_COMMIT, LING_INSTALL_COMPOSE_OVERRIDE=str(override)
        )
    finally:
        os.chmod(override, 0o600)

    assert proc.returncode != 0
    assert "LING_INSTALL_COMPOSE_OVERRIDE" in proc.stderr
    assert calls == ""


def test_build_web_only_ignores_host_override(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    bogus = tmp_path / "not-there.json"

    proc, calls = run_install(
        repo, env, "--build-web-only", LING_INSTALL_COMPOSE_OVERRIDE=str(bogus)
    )

    assert proc.returncode == 0, proc.stderr
    assert "忽略 LING_INSTALL_COMPOSE_OVERRIDE" in proc.stdout
    assert "docker " not in calls
    assert not bogus.exists()


# ---------------------------------------------------------------------------
# HOME / DOCKER_CONFIG：只在缺可写 HOME 时保守回退
# ---------------------------------------------------------------------------


def docker_home(calls: str) -> tuple[str, str]:
    line = next(item for item in calls.splitlines() if item.startswith("docker-home "))
    _, home, docker_config = line.split(" ", 2)
    return home, docker_config


def test_explicit_home_and_docker_config_are_preserved(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    home = tmp_path / "host-home"
    home.mkdir()
    docker_config = tmp_path / "host-docker"
    docker_config.mkdir()

    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        HOME=str(home),
        DOCKER_CONFIG=str(docker_config),
    )

    assert proc.returncode == 0, proc.stderr
    # 宿主的 docker context / registry 凭据跟着这两个变量走，不能被改写。
    assert docker_home(calls) == (str(home), str(docker_config))
    assert not (repo / ".home").exists()
    assert not (repo / ".docker").exists()
    assert not (repo / ".run").exists()
    assert "回退到" not in proc.stdout


def test_explicit_home_without_docker_config_keeps_docker_default(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    home = tmp_path / "host-home"
    home.mkdir()

    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, HOME=str(home), DOCKER_CONFIG=None
    )

    assert proc.returncode == 0, proc.stderr
    # 没设置 DOCKER_CONFIG 就不要替用户设：Docker 自己会用 $HOME/.docker。
    assert docker_home(calls) == (str(home), "<unset>")
    assert not (repo / ".docker").exists()


def test_missing_home_falls_back_to_repo_local_dirs(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    missing = tmp_path / "no-such-home" / "nested"

    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, HOME=str(missing), DOCKER_CONFIG=None
    )

    assert proc.returncode == 0, proc.stderr
    assert "回退到" in proc.stdout
    assert "没有宿主的 docker context / registry 凭据" in proc.stdout
    # 回退目录放在仓库已忽略的 .run/ 下：旧 checkout 不会被弄脏，registry 配置
    # 也不会进 docker build context。
    assert (repo / ".run" / "home").is_dir()
    assert (repo / ".run" / "docker").is_dir()
    assert docker_home(calls) == (str(repo / ".run" / "home"), str(repo / ".run" / "docker"))
    assert oct((repo / ".run" / "docker").stat().st_mode & 0o777) == "0o700"
    assert not (repo / ".home").exists()
    assert not (repo / ".docker").exists()
    # 不替用户去创建那个不存在的 HOME。
    assert not missing.exists()


def test_explicit_docker_config_survives_home_fallback(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    docker_config = tmp_path / "host-docker"
    docker_config.mkdir()

    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        HOME=str(tmp_path / "missing-home"),
        DOCKER_CONFIG=str(docker_config),
    )

    assert proc.returncode == 0, proc.stderr
    assert docker_home(calls) == (str(repo / ".run" / "home"), str(docker_config))
    assert not (repo / ".run" / "docker").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root 没有“不可写目录”这回事")
def test_unwritable_home_falls_back(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    home = tmp_path / "readonly-home"
    home.mkdir()
    os.chmod(home, 0o500)
    try:
        proc, calls = run_install(
            repo, env, LING_COMMIT=VALID_COMMIT, HOME=str(home), DOCKER_CONFIG=None
        )
    finally:
        os.chmod(home, 0o700)

    assert proc.returncode == 0, proc.stderr
    assert "回退到" in proc.stdout
    assert docker_home(calls)[0] == str(repo / ".run" / "home")


# ---------------------------------------------------------------------------
# 内容感知的 compose 替身：真的按 -f 顺序合并 override 并解释 env 优先级
# ---------------------------------------------------------------------------
# 仍然是 stub-only：它只实现 install.sh 会读的字段（/app/web 挂载、顶层命名卷、
# build args、db/edge image），不访问网络、不启动容器。它让“override 内容会不会
# 搬走 /app/web 或持久数据卷”这类用例跑在解析结果上，而不是只比对命令行参数；
# 真实 Docker Compose 的合并语义仍必须在部署机单独验证。

_DOCKER_EMULATOR = r'''#!/usr/bin/env python3
import copy
import fnmatch
import json
import os
import pathlib
import re
import sys

COMPOSE_DIR = pathlib.Path("deploy")


def log(message):
    with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def env_file_values(path):
    values = {}
    try:
        raw = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
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


_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate(value, env_file):
    if not isinstance(value, str):
        return value

    def replace(match):
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name)
        if resolved is None:
            resolved = env_file.get(name)
        if resolved is None or resolved == "":
            return default if default is not None else ""
        return resolved

    return _INTERPOLATION.sub(replace, value)


def resolve_values(node, env_file):
    if isinstance(node, dict):
        return {key: resolve_values(value, env_file) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve_values(value, env_file) for value in node]
    return interpolate(node, env_file)


def merge_dict(base, extra):
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_service(base, extra):
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in extra.items():
        if key == "volumes" and isinstance(value, list):
            current = [item for item in result.get("volumes", []) if isinstance(item, dict)]
            by_target = {item.get("target"): item for item in current}
            order = [item.get("target") for item in current]
            for item in value:
                if not isinstance(item, dict) or not item.get("target"):
                    continue
                if item["target"] not in by_target:
                    order.append(item["target"])
                by_target[item["target"]] = copy.deepcopy(item)
            result["volumes"] = [by_target[target] for target in order]
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


BASE_MODEL = {
    "db": {
        "image": "${LING_POSTGRES_IMAGE:-postgres:16-alpine}",
        "volumes": [
            {
                "type": "volume",
                "source": "db-data",
                "target": "/var/lib/postgresql/data",
                "read_only": False,
            }
        ],
    },
    "api": {
        "build": {
            "args": {
                "LING_PYTHON_BASE": "${LING_PYTHON_BASE:-python:3.13-slim}",
                "PIP_INDEX_URL": "${PIP_INDEX_URL:-}",
            }
        },
        "environment": {"LING_WEB_DIST": "/app/web"},
        "volumes": [
            {"type": "volume", "source": "app-data", "target": "/data", "read_only": False},
            {
                "type": "bind",
                "source": "${LING_WEB_DIST_HOST:-../clients/web-console/dist}",
                "target": "/app/web",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "${LING_UPDATE_REQUESTS_HOST:-../.run/update/requests}",
                "target": "/run/ling-update/requests",
                "read_only": False,
            },
            {
                "type": "bind",
                "source": "${LING_UPDATE_STATUS_HOST:-../.run/update/status}",
                "target": "/run/ling-update/status",
                "read_only": True,
            },
        ],
    },
    "edge": {
        "image": "${LING_NGINX_IMAGE:-nginx:alpine}",
        # 与 deploy/compose.yaml 的 edge ports 一致：install.sh 等待就绪/看状态
        # 时用的就是这个 target=80 的发布端口。
        "ports": [
            {
                "mode": "ingress",
                "target": 80,
                "published": "${LING_EDGE_PORT:-8080}",
                "protocol": "tcp",
            }
        ],
        "volumes": [
            {
                "type": "bind",
                "source": "${LING_EDGE_CONF:-./nginx.conf}",
                "target": "/etc/nginx/conf.d/default.conf",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "${LING_EDGE_CERTS:-./certs}",
                "target": "/etc/nginx/certs",
                "read_only": True,
            },
        ],
    },
    "worker": {
        "build": {
            "args": {
                "LING_PYTHON_BASE": "${LING_PYTHON_BASE:-python:3.13-slim}",
                "PIP_INDEX_URL": "${PIP_INDEX_URL:-}",
            }
        },
        "volumes": [
            {"type": "volume", "source": "app-data", "target": "/data", "read_only": False}
        ],
    },
}

BASE_VOLUMES = {
    "db-data": {"name": "ling_db-data", "driver": "local"},
    "app-data": {"name": "ling_app-data", "driver": "local"},
}


def normalize_bind_sources(model):
    """Compose config 会把相对 bind 源解析成绝对路径（相对 compose 文件目录）。"""
    for service in model.get("services", {}).values():
        if not isinstance(service, dict):
            continue
        for item in service.get("volumes") or []:
            if not isinstance(item, dict) or item.get("type") != "bind":
                continue
            source = item.get("source")
            if not isinstance(source, str) or source.startswith("/"):
                continue
            item["source"] = str((COMPOSE_DIR / source).resolve())


def matches(patterns, value):
    return any(
        pattern and fnmatch.fnmatchcase(value, pattern)
        for pattern in patterns.split(",")
    )


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
    name = args[-1] if len(args) > 2 else ""
    sys.exit(0 if matches(os.environ.get("EMU_IMAGE_OK", ""), name) else 1)
if args[0] == "pull":
    name = args[-1]
    sys.exit(0 if matches(os.environ.get("EMU_PULL_OK", "*"), name) else 1)
if args[0] != "compose":
    sys.exit(0)

files = []
env_file = None
sub = None
index = 0
while index < len(args):
    if args[index] == "-f" and index + 1 < len(args):
        files.append(pathlib.Path(args[index + 1]))
        index += 2
        continue
    if args[index] == "--env-file" and index + 1 < len(args):
        env_file = pathlib.Path(args[index + 1])
        index += 2
        continue
    if args[index] in (
        "config", "build", "up", "down", "restart", "ps", "logs", "images", "exec", "version"
    ):
        sub = args[index]
        break
    index += 1
if sub is None:
    sys.exit(0)
if sub == "version":
    sys.exit(0)
if sub == "build":
    log(
        "build-env "
        + json.dumps(
            {
                key: os.environ.get(key)
                for key in ("LING_PYTHON_BASE", "LING_POSTGRES_IMAGE", "LING_NGINX_IMAGE")
            },
            sort_keys=True,
        )
    )
    sys.exit(int(os.environ.get("EMU_BUILD_EXIT", "0")))
if sub == "up":
    log("compose-up")
    sys.exit(int(os.environ.get("EMU_UP_EXIT", "0")))
if sub != "config":
    if sub in ("down", "restart", "ps", "logs"):
        sys.exit(0)
    # 严格模式：没实现的子命令/未知 argv 不得静默成功。
    log("compose-unknown " + " ".join(args))
    sys.exit(64)

values = env_file_values(env_file) if env_file else {}
model = {"services": copy.deepcopy(BASE_MODEL), "volumes": copy.deepcopy(BASE_VOLUMES)}
for path in files[1:]:
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover - 用例都写合法 JSON
        log("emulator-error " + type(exc).__name__)
        sys.exit(3)
    for name, service in (override.get("services") or {}).items():
        if isinstance(service, dict):
            model["services"][name] = merge_service(model["services"].get(name, {}), service)
    extra_volumes = override.get("volumes")
    if isinstance(extra_volumes, dict):
        for key, spec in extra_volumes.items():
            if isinstance(spec, dict) and isinstance(model["volumes"].get(key), dict):
                model["volumes"][key] = merge_dict(model["volumes"][key], spec)
            else:
                model["volumes"][key] = copy.deepcopy(spec)

if os.environ.get("EMU_ANONYMOUS_DATA") == "1":
    # 模拟 compose 对匿名卷的输出：不写 source，也不声明顶层卷。
    model["volumes"].pop("app-data", None)
    for name in ("api", "worker"):
        for item in model["services"][name].get("volumes") or []:
            if item.get("target") == "/data":
                item.pop("source", None)

model = resolve_values(model, values)
normalize_bind_sources(model)

web = ""
for service in model["services"].values():
    for item in service.get("volumes") or []:
        if isinstance(item, dict) and item.get("target") == "/app/web":
            web = item.get("source") or ""
log(
    "config-images "
    + json.dumps(
        {
            "python": model["services"]["api"]["build"]["args"]["LING_PYTHON_BASE"],
            "postgres": model["services"]["db"]["image"],
            "nginx": model["services"]["edge"]["image"],
            "web": web,
        },
        sort_keys=True,
    )
)
print(json.dumps(model))
'''

_EMULATOR_DEFAULTS = {
    "EMU_PULL_OK": "*",
    "EMU_IMAGE_OK": "",
}


@pytest.fixture()
def emulated_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """deploy_repo + 内容感知的 docker compose 替身（可配置拉取成功集合）。"""
    repo, env = deploy_repo.__wrapped__(tmp_path)
    _write_exe(tmp_path / "bin" / "docker", _DOCKER_EMULATOR)
    env = copy.deepcopy(env)
    env.pop("STUB_CONFIG_JSON", None)
    env.update(_EMULATOR_DEFAULTS)
    return repo, env


def emulator_images_history(calls: str) -> list[dict[str, str]]:
    return [
        json.loads(item.split(" ", 1)[1])
        for item in calls.splitlines()
        if item.startswith("config-images ")
    ]


def emulator_images(calls: str) -> dict[str, str]:
    return emulator_images_history(calls)[0]


def build_env(calls: str) -> dict[str, str | None]:
    line = next(item for item in calls.splitlines() if item.startswith("build-env "))
    return json.loads(line.split(" ", 1)[1])


def env_bytes(repo: Path) -> bytes:
    return (repo / "deploy" / ".env").read_bytes()


def write_env(repo: Path, body: str) -> None:
    path = repo / "deploy" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


DEFAULT_ENV = (
    "POSTGRES_PASSWORD=db-password-do-not-print\n"
    "LING_ROOT_KEY=root-key-do-not-print\n"
    "LING_WEB_DIST_HOST=../clients/web-console/dist\n"
)


# ---------------------------------------------------------------------------
# canonical deploy/.env 只读 + 基础镜像只进进程环境
# ---------------------------------------------------------------------------


def test_generated_env_uses_relative_web_dist(emulated_repo: tuple[Path, dict[str, str]]) -> None:
    repo, env = emulated_repo
    proc, _ = run_install(repo, env, LING_COMMIT=VALID_COMMIT)

    assert proc.returncode == 0, proc.stderr
    text = (repo / "deploy" / ".env").read_text(encoding="utf-8")
    effective = [line for line in text.splitlines() if line.startswith("LING_WEB_DIST_HOST=")]
    assert effective == ["LING_WEB_DIST_HOST=../clients/web-console/dist"]
    # 生成时不再把本检出的绝对路径写成生效配置。
    assert not any(line.startswith("LING_WEB_DIST_HOST=/") for line in text.splitlines())
    assert "LING_PYTHON_BASE" not in text
    assert "LING_POSTGRES_IMAGE" not in text
    assert "LING_NGINX_IMAGE" not in text


@pytest.mark.parametrize("kind", ["regular", "readonly", "symlink"])
def test_existing_env_is_byte_identical(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path, kind: str
) -> None:
    repo, env = emulated_repo
    target = repo / "deploy" / ".env"
    if kind == "symlink":
        shared = tmp_path / "shared"
        shared.mkdir()
        target = shared / "ling.env"
        target.write_text(DEFAULT_ENV, encoding="utf-8")
        (repo / "deploy" / ".env").symlink_to(target)
    else:
        target.write_text(DEFAULT_ENV, encoding="utf-8")
    before = target.read_bytes()
    if kind == "readonly":
        os.chmod(target, 0o400)
    try:
        proc, _ = run_install(repo, env, LING_COMMIT=VALID_COMMIT)
    finally:
        os.chmod(target, 0o600)

    assert proc.returncode == 0, proc.stderr
    # 安装器只读 canonical env：普通/只读/symlink 都字节不变，也不复制密钥。
    assert target.read_bytes() == before
    if kind == "symlink":
        assert (repo / "deploy" / ".env").is_symlink()
        assert not (repo / "deploy" / ".env").resolve().is_relative_to(repo)
    for secret in ("db-password-do-not-print", "root-key-do-not-print"):
        assert secret not in proc.stdout
        assert secret not in proc.stderr


def test_explicit_env_file_images_are_preserved_without_probing(
    emulated_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = emulated_repo
    write_env(
        repo,
        DEFAULT_ENV
        + 'LING_PYTHON_BASE="python:3.12-slim"\n'
        + "LING_POSTGRES_IMAGE=postgres:17-alpine\n"
        + "LING_NGINX_IMAGE=nginx:1.27-alpine\n",
    )
    before = env_bytes(repo)

    proc, calls = run_install(repo, env, LING_COMMIT=VALID_COMMIT, EMU_PULL_OK="", EMU_IMAGE_OK="")

    assert proc.returncode == 0, proc.stderr
    # 显式选择（含带引号的写法）由 Compose 解析后原样保留，不探测也不替换。
    assert emulator_images(calls) == {
        "python": "python:3.12-slim",
        "postgres": "postgres:17-alpine",
        "nginx": "nginx:1.27-alpine",
        "web": str((repo / "clients" / "web-console" / "dist").resolve()),
    }
    assert "docker pull" not in calls
    assert "docker image inspect" not in calls
    assert env_bytes(repo) == before
    assert "python 基础镜像（deploy/.env 显式指定，保留）" in proc.stdout
    # 后续所有 compose 解析都还是这些显式值（没有被导出/替换）。
    assert all(item == emulator_images(calls) for item in emulator_images_history(calls))


def test_shell_environment_images_are_preserved(
    emulated_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        EMU_PULL_OK="postgres:16-alpine,nginx:alpine",
        LING_PYTHON_BASE="python:3.11-slim",
        LING_POSTGRES_IMAGE="postgres:17-alpine",
    )

    assert proc.returncode == 0, proc.stderr
    data = build_env(calls)
    assert data["LING_PYTHON_BASE"] == "python:3.11-slim"
    assert data["LING_POSTGRES_IMAGE"] == "postgres:17-alpine"
    assert "docker pull -q python:3.11-slim" not in calls
    assert "docker pull -q postgres:17-alpine" not in calls
    assert "python 基础镜像（环境显式设置，保留）" in proc.stdout
    assert "Postgres 基础镜像（环境显式设置，保留）" in proc.stdout
    # shell 的显式值不写进 canonical env。
    assert "LING_PYTHON_BASE" not in (repo / "deploy" / ".env").read_text(encoding="utf-8")


def test_python_base_flag_is_exported_only(
    emulated_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)
    before = env_bytes(repo)

    proc, calls = run_install(
        repo,
        env,
        "--python-base",
        "registry.local/library/python:3.13-slim",
        LING_COMMIT=VALID_COMMIT,
        EMU_PULL_OK="postgres:16-alpine,nginx:alpine",
    )

    assert proc.returncode == 0, proc.stderr
    assert build_env(calls)["LING_PYTHON_BASE"] == "registry.local/library/python:3.13-slim"
    assert "docker pull -q registry.local/library/python:3.13-slim" not in calls
    assert "--python-base 指定" in proc.stdout
    assert env_bytes(repo) == before


def test_implicit_images_fall_back_to_same_tag_mirror(
    emulated_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)
    before = env_bytes(repo)

    proc, calls = run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        EMU_PULL_OK="docker.1panel.live/*",
        EMU_IMAGE_OK="",
    )

    assert proc.returncode == 0, proc.stderr
    data = build_env(calls)
    assert data["LING_PYTHON_BASE"] == "docker.1panel.live/library/python:3.13-slim"
    # 必须保持同一个 tag，绝不跨 Postgres 主版本。
    assert data["LING_POSTGRES_IMAGE"] == "docker.1panel.live/library/postgres:16-alpine"
    assert data["LING_NGINX_IMAGE"] == "docker.1panel.live/library/nginx:alpine"
    assert "postgres:18" not in calls
    assert "postgres:18" not in proc.stdout + proc.stderr
    assert "同一 tag 的镜像源" in proc.stdout
    assert env_bytes(repo) == before
    # 回退值确实通过进程环境被后续 compose 解析读到（不只是打了一条日志）。
    last = emulator_images_history(calls)[-1]
    assert last["python"] == "docker.1panel.live/library/python:3.13-slim"
    assert last["postgres"] == "docker.1panel.live/library/postgres:16-alpine"
    assert last["nginx"] == "docker.1panel.live/library/nginx:alpine"


def test_image_fallback_fails_closed_without_touching_env(
    emulated_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)
    before = env_bytes(repo)

    proc, calls = run_install(repo, env, LING_COMMIT=VALID_COMMIT, EMU_PULL_OK="")

    assert proc.returncode != 0
    assert "拿不到" in proc.stderr
    assert "compose build" not in calls
    assert "build-env" not in calls
    assert "compose-up" not in calls
    assert env_bytes(repo) == before
    assert "root-key-do-not-print" not in proc.stdout + proc.stderr
    assert "db-password-do-not-print" not in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# override 内容不变量（内容感知 compose 替身）
# ---------------------------------------------------------------------------


def _run_with_override(
    repo: Path,
    env: dict[str, str],
    tmp_path: Path,
    body: dict,
    **extra: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    override = tmp_path / "host-override.json"
    override.write_text(json.dumps(body), encoding="utf-8")
    return run_install(
        repo,
        env,
        LING_COMMIT=VALID_COMMIT,
        LING_INSTALL_COMPOSE_OVERRIDE=str(override),
        **extra,
    )


def test_content_aware_override_cannot_move_app_web(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)
    other = tmp_path / "other-dist"
    other.mkdir()

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "api": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(other),
                            "target": "/app/web",
                            "read_only": True,
                        }
                    ]
                }
            }
        },
    )

    assert proc.returncode != 0
    assert "不是本次校验产物" in proc.stderr
    assert "build-env" not in calls
    assert "compose-up" not in calls


def test_override_cannot_replace_app_data_volume(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "api": {
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "other-data",
                            "target": "/data",
                            "read_only": False,
                        }
                    ]
                }
            }
        },
    )

    assert proc.returncode != 0
    assert "持久数据卷" in proc.stderr
    assert "api" in proc.stderr and "/data" in proc.stderr
    assert "build-env" not in calls
    assert "compose-up" not in calls


def test_override_cannot_change_db_data_mount(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "db": {
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "other-db",
                            "target": "/var/lib/postgresql/data",
                            "read_only": False,
                        }
                    ]
                }
            }
        },
    )

    assert proc.returncode != 0
    assert "持久数据卷" in proc.stderr
    assert "db-data" in proc.stderr
    assert "build-env" not in calls
    assert "compose-up" not in calls


@pytest.mark.parametrize(
    "volume_spec",
    [
        {"driver": "nfs", "driver_opts": {"o": "addr=10.0.0.9,password=driver-secret"}},
        {"external": True, "name": "external-app-data"},
    ],
)
def test_override_cannot_change_volume_identity_or_external(
    emulated_repo: tuple[Path, dict[str, str]],
    tmp_path: Path,
    volume_spec: dict,
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(repo, env, tmp_path, {"volumes": {"app-data": volume_spec}})

    assert proc.returncode != 0
    assert "持久数据卷定义：app-data" in proc.stderr
    assert "build-env" not in calls
    assert "compose-up" not in calls
    # 失败信息不能把 compose 配置/驱动参数（可能含凭据）带出来。
    assert "driver-secret" not in proc.stdout + proc.stderr
    assert "root-key-do-not-print" not in proc.stdout + proc.stderr
    assert "db-password-do-not-print" not in proc.stdout + proc.stderr
    assert "external-app-data" not in proc.stderr


def test_legit_override_for_edge_binds_still_starts(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "edge": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/srv/ling/nginx-tls.conf",
                            "target": "/etc/nginx/conf.d/default.conf",
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": "/srv/ling/certs",
                            "target": "/etc/nginx/certs",
                            "read_only": True,
                        },
                    ]
                },
                "api": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/run/ling-update/requests",
                            "target": "/run/ling-update/requests",
                            "read_only": False,
                        },
                        {
                            "type": "bind",
                            "source": "/run/ling-update/status",
                            "target": "/run/ling-update/status",
                            "read_only": True,
                        },
                    ]
                },
            }
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "compose-up" in calls
    assert "使用宿主 Compose override" in proc.stdout
    assert_override_calls_keep_both_files(calls, tmp_path / "host-override.json")


def test_anonymous_data_volume_does_not_cause_false_reject(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """匿名卷没有跨重启身份：不能因为随机名不同就把合法 override 判成换卷。"""
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "edge": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/srv/ling/certs",
                            "target": "/etc/nginx/certs",
                            "read_only": True,
                        }
                    ]
                }
            }
        },
        EMU_ANONYMOUS_DATA="1",
    )

    assert proc.returncode == 0, proc.stderr
    assert "compose-up" in calls


def test_override_cannot_delete_db_data_mount(
    emulated_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = emulated_repo
    write_env(repo, DEFAULT_ENV)

    proc, calls = _run_with_override(
        repo,
        env,
        tmp_path,
        {
            "services": {
                "db": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/srv/small",
                            "target": "/var/lib/postgresql/data",
                        }
                    ]
                }
            }
        },
    )

    assert proc.returncode != 0
    assert "持久数据卷挂载" in proc.stderr
    assert "compose-up" not in calls


# ---------------------------------------------------------------------------
# 锁 FD 的真实继承路径（真 flock / pass_fds / 两层 shell，无 Docker）
# ---------------------------------------------------------------------------

_LOCK_PASSER = """
import fcntl, json, os, subprocess, sys

lock, repo, mode = sys.argv[1], sys.argv[2], sys.argv[3]
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
if mode == "locked":
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
elif mode == "shared":
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
os.set_inheritable(fd, True)
env = dict(os.environ)
env["LING_INSTALL_LOCK_FD"] = str(fd)
proc = subprocess.run(
    ["bash", "-c", "exec bash deploy/install.sh --down"],
    cwd=repo,
    env=env,
    pass_fds=(fd,),
    capture_output=True,
    text=True,
)
print(json.dumps({"rc": proc.returncode, "out": proc.stdout, "err": proc.stderr}))
"""


def _run_lock_passer(
    repo: Path, env: dict[str, str], tmp_path: Path, mode: str
) -> tuple[int, str, str]:
    lock = repo / "deploy" / ".ling-install.lock"
    proc = subprocess.run(
        [sys.executable, "-c", _LOCK_PASSER, str(lock), str(repo), mode],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    log_path = Path(env["CALL_LOG"])
    calls = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return data["rc"], data["err"], calls


def test_real_flock_fd_passes_through_two_shells(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = deploy_repo
    rc, err, calls = _run_lock_passer(repo, env, repo.parent, "locked")

    assert rc == 0, err
    assert "另一个 install.sh" not in err
    assert "docker compose" in calls


def test_inherited_fd_without_real_lock_is_rejected(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = deploy_repo
    rc, err, calls = _run_lock_passer(repo, env, repo.parent, "unlocked")

    assert rc != 0
    assert "LING_INSTALL_LOCK_FD" in err
    assert "docker compose" not in calls


# 下面两条覆盖 F1 的漏网分支：**别人持锁**时传一个同 inode 但未加锁的 FD。
# 旧实现用独立 probe 判断“有人持锁”，于是只要别人持锁就放行；新实现在继承来的
# FD 本身上要求 flock(LOCK_EX|LOCK_NB) 成功，因此这两条都必须被拒绝。
@pytest.mark.parametrize("holder_mode", ["exclusive", "shared"])
def test_inherited_unlocked_fd_is_rejected_while_another_process_holds_lock(
    deploy_repo: tuple[Path, dict[str, str]], holder_mode: str
) -> None:
    repo, env = deploy_repo
    lock = repo / "deploy" / ".ling-install.lock"
    holder = hold_lock(lock, holder_mode)
    try:
        rc, err, calls = _run_lock_passer(repo, env, repo.parent, "unlocked")
    finally:
        stop_holder(holder)

    assert rc != 0, f"{holder_mode} holder 存在时，未加锁的同 inode FD 被当成了锁"
    assert "LING_INSTALL_LOCK_FD" in err
    assert "docker compose" not in calls


def test_inherited_shared_lock_fd_is_upgraded_and_accepted(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    """共享锁 FD 在无人竞争时可以安全升级成独占，因此接受（锁真的被持有）。"""
    repo, env = deploy_repo
    rc, err, calls = _run_lock_passer(repo, env, repo.parent, "shared")

    assert rc == 0, err
    assert "LING_INSTALL_LOCK_FD" not in err
    assert "docker compose" in calls


_OTHER_FD_PASSER = """
import json, os, subprocess, sys

other, repo = sys.argv[1], sys.argv[2]
fd = os.open(other, os.O_CREAT | os.O_RDWR, 0o600)
os.set_inheritable(fd, True)
env = dict(os.environ)
env["LING_INSTALL_LOCK_FD"] = str(fd)
proc = subprocess.run(
    ["bash", "-c", "exec bash deploy/install.sh --down"],
    cwd=repo,
    env=env,
    pass_fds=(fd,),
    capture_output=True,
    text=True,
)
print(json.dumps({"rc": proc.returncode, "err": proc.stderr}))
"""


def test_inherited_fd_of_different_file_is_rejected(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """FD 指向别的普通文件（inode 不同）不是这把锁，必须拒绝。"""
    repo, env = deploy_repo
    other = tmp_path / "other-file"
    other.write_text("not the lock\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-c", _OTHER_FD_PASSER, str(other), str(repo)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["rc"] != 0
    assert "LING_INSTALL_LOCK_FD" in data["err"]
    log_path = Path(env["CALL_LOG"])
    calls = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "docker compose" not in calls


# ---------------------------------------------------------------------------
# 锁文件本身：拒绝 symlink / 目录 / FIFO，且绝不截断内容
# ---------------------------------------------------------------------------


def test_lock_symlink_is_rejected_and_victim_is_untouched(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    victim = tmp_path / "victim.txt"
    victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
    lock = repo / "deploy" / ".ling-install.lock"
    lock.symlink_to(victim)

    proc, calls = run_install(repo, env, "--down")

    assert proc.returncode != 0
    assert "符号链接" in proc.stderr
    assert "docker compose" not in calls
    # 目标文件内容零改动，链接本身也没被删掉/替换。
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH\n"
    assert lock.is_symlink()
    assert os.readlink(lock) == str(victim)


def test_lock_symlink_to_env_does_not_truncate_it(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    """QA F2 的原始场景：锁文件被换成指向 deploy/.env 的 symlink。"""
    repo, env = deploy_repo
    env_file = repo / "deploy" / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=db-secret\nLING_ROOT_KEY=root-secret\n", encoding="utf-8"
    )
    before = env_file.read_bytes()
    (repo / "deploy" / ".ling-install.lock").symlink_to(env_file)

    proc, calls = run_install(repo, env, "--down")

    assert proc.returncode != 0
    assert "符号链接" in proc.stderr
    assert "docker compose" not in calls
    assert env_file.read_bytes() == before
    assert b"db-secret" in before  # 内容还在（没有被 truncate/覆盖）


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_lock_non_regular_file_is_rejected(
    deploy_repo: tuple[Path, dict[str, str]], kind: str
) -> None:
    repo, env = deploy_repo
    lock = repo / "deploy" / ".ling-install.lock"
    if kind == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock)

    proc, calls = run_install(repo, env, "--down")

    assert proc.returncode != 0
    assert "普通文件" in proc.stderr
    assert "docker compose" not in calls
    # 拒绝时不动这个路径（不删除、不 replace 成普通文件）。
    assert lock.is_dir() if kind == "directory" else stat_is_fifo(lock)


def test_lock_file_content_is_never_truncated(
    deploy_repo: tuple[Path, dict[str, str]],
) -> None:
    """锁文件不再写 PID：已有内容保持原样（诊断信息不值得冒截断风险）。"""
    repo, env = deploy_repo
    lock = repo / "deploy" / ".ling-install.lock"
    lock.write_text("keep me\n", encoding="utf-8")

    proc, calls = run_install(repo, env, "--down")

    assert proc.returncode == 0, proc.stderr
    assert "docker compose" in calls
    assert lock.read_text(encoding="utf-8") == "keep me\n"


# ---------------------------------------------------------------------------
# HOME 回退目录迁移
# ---------------------------------------------------------------------------


def test_legacy_home_dirs_are_not_moved_or_deleted(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = deploy_repo
    legacy_home = repo / ".home"
    legacy_docker = repo / ".docker"
    legacy_home.mkdir()
    (legacy_home / "keep.txt").write_text("legacy home\n", encoding="utf-8")
    legacy_docker.mkdir()
    (legacy_docker / "config.json").write_text('{"legacy": true}\n', encoding="utf-8")

    proc, calls = run_install(
        repo, env, LING_COMMIT=VALID_COMMIT, HOME=str(tmp_path / "missing-home"), DOCKER_CONFIG=None
    )

    assert proc.returncode == 0, proc.stderr
    assert "旧版本的回退目录" in proc.stdout
    # 新回退路径生效，旧目录原样保留（既不是“删掉重建”，也没被搬走）。
    assert docker_home(calls) == (str(repo / ".run" / "home"), str(repo / ".run" / "docker"))
    assert (legacy_home / "keep.txt").read_text(encoding="utf-8") == "legacy home\n"
    assert (legacy_docker / "config.json").read_text(encoding="utf-8") == '{"legacy": true}\n'


def test_ignore_files_exclude_fallback_and_update_runtime() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".run/", "/.home/", "/.docker/"):
        assert pattern in gitignore
    for pattern in (".run", ".home", ".docker", ".ling-install.lock", "deploy/compose.override.*"):
        assert pattern in dockerignore
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert ".run/home" in env_example
    assert ".run/docker" in env_example


@pytest.mark.parametrize("kind", ["regular_file", "missing_parent"])
def test_invalid_docker_config_is_warned_not_reset(
    deploy_repo: tuple[Path, dict[str, str]], tmp_path: Path, kind: str
) -> None:
    """显式 DOCKER_CONFIG 不重置：只告警，让 docker 自己报具体路径错误。"""
    repo, env = deploy_repo
    home = tmp_path / "home"
    home.mkdir()
    if kind == "regular_file":
        docker_config = tmp_path / "docker-config"
        docker_config.write_text("{}\n", encoding="utf-8")
    else:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x\n", encoding="utf-8")
        docker_config = blocker / "docker"

    proc, calls = run_install(repo, env, "--down", HOME=str(home), DOCKER_CONFIG=str(docker_config))

    assert proc.returncode == 0, proc.stderr
    assert "保留该路径，不替你换配置" in proc.stdout
    # 没有被替换成回退目录（HOME 有效，所以也不会创建 .run/），路径内容不改写。
    assert not (repo / ".run").exists()
    assert "docker compose" in calls
    if kind == "regular_file":
        assert docker_config.read_text(encoding="utf-8") == "{}\n"
    else:
        assert docker_config.parent.read_text(encoding="utf-8") == "x\n"
