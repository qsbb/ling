"""``deploy/backup/install-cron.sh`` 的隔离回归测试。

只验证**生成出来的 crontab 行**与安装脚本的失败路径，不安装任何真实 cron、
不碰 docker、不执行备份：

- 仓库结构复制到临时目录（``deploy/install.sh`` / ``deploy/backup/backup.sh`` /
  ``deploy/backup/install-cron.sh``），因此 ``install-cron.sh`` 解析项目名时读到
  的是这份副本，不会看到宿主机真实仓库；
- ``crontab`` 用**严格 fake**：只接受 ``-l`` 与 ``-``（stdin），任何未知命令/参数
  直接以非零退出并记录调用，所以“脚本偷偷用了别的 crontab 参数”会立刻失败；
- ``docker`` 也是严格 fake：``install-cron.sh`` 一旦调用它就算失败（安装定时任务
  不应该运行任何容器命令）。

覆盖：项目名/ compose 文件写进 cron 行、项目名跟随 ``install.sh`` 默认值（不写死
``ling``）、**缺失旧 ``install.sh`` 时回退到内置默认 ``ling``**（镜像模式部署包
不含旧安装器）、文件组（主文件 + override）与 ``--env-file`` 写进 cron 行、
``LING_COMPOSE_*`` 既有覆盖不被破坏、不含明文密码、重复安装幂等、
缺参数/取值非法/显式路径不存在时失败退出且**不写** crontab。

还覆盖**根密钥跨组件缺陷**（镜像模式 ``ling install`` 把根密钥放在受限 ``.env``
的 ``LING_ROOT_KEY`` 里，容器内没有 ``/data/root.key``，而 cron 环境没有可继承的
``LING_*``）：生成的 cron 行必须让 ``backup.sh`` 拿到这一项，否则 manifest 只能记
``keys_env_set=false`` + ``keys_root=absent``，``ling doctor`` 每天告警、
``ling backup`` 的完整性校验把定时备份判成"恢复到新卷后凭据解不开"。
验收方式是**真执行**生成的那一行（``backup.sh`` 换成记录器，``sh -c`` / ``bash -c``
各跑一次），而不是只匹配字符串；同时断言 crontab 全文里搜不到密钥值。
源码模式（``.env`` 里没有这一项，密钥在容器卷）必须与改动前行为一致：
行里不出现 ``LING_ROOT_KEY``、子进程环境里该项**未设置**（不是设成空串）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_CRON = REPO_ROOT / "deploy" / "backup" / "install-cron.sh"
BACKUP_SH = REPO_ROOT / "deploy" / "backup" / "backup.sh"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"

# 项目名必须以小写字母/数字开头，且只含小写字母、数字、下划线、连字符
# （与 install-cron.sh 的校验一致，也与 protocol.PROJECT_RE 同形）。
_PROJECT_LINE = re.compile(r'^PROJECT="([^"]*)"$', re.MULTILINE)

_CRONTAB_FAKE = """#!/usr/bin/env bash
# 严格 fake：只实现 `crontab -l` 与 `crontab -`（从 stdin 读新表）。
set -euo pipefail
printf 'crontab %s\\n' "$*" >> "${CRONTAB_CALLS:?}"
if [[ $# -ne 1 ]]; then
  echo "fake-crontab: 只接受 -l 或 -，收到 $# 个参数: $*" >&2
  exit 90
fi
case "$1" in
  -l)
    # CRONTAB_LIST_FAIL 用来模拟“读现有 crontab 失败”（权限/损坏），不是“没有条目”。
    if [[ -n "${CRONTAB_LIST_FAIL:-}" ]]; then
      echo "fake-crontab: 模拟 crontab -l 失败" >&2
      exit "${CRONTAB_LIST_FAIL}"
    fi
    if [[ ! -f "${CRONTAB_STATE:?}" ]]; then
      echo "no crontab for fakeuser" >&2
      exit 1
    fi
    cat "$CRONTAB_STATE"
    ;;
  -)
    tmp="${CRONTAB_STATE}.tmp.$$"
    cat > "$tmp"
    mv "$tmp" "$CRONTAB_STATE"
    ;;
  *)
    echo "fake-crontab: 未知参数 $1" >&2
    exit 91
    ;;
esac
"""

_DOCKER_FAKE = """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\\n' "$*" >> "${DOCKER_CALLS:?}"
echo "fake-docker: install-cron.sh 不应该调用 docker" >&2
exit 97
"""

# 这两个值故意带密码：它们只能出现在宿主环境里，绝不能进 crontab 行或输出。
SECRET_URL = "postgresql://ling:S3cr3t-Pa55w0rd@db:5432/ling"
SECRET_PASSWORD = "S3cr3t-Pa55w0rd"

# 镜像模式的根密钥：只应存在于受限 .env 与 backup.sh 的进程环境里。
# crontab 通常全局可读，所以它的**明文绝不能**出现在 crontab 文本或安装输出中。
ROOT_KEY = "ROOTKEY-fixture-7f3a9c2e-DO-NOT-LEAK"


@dataclass
class CronRepo:
    """临时仓库 + 严格 fake 的运行环境。"""

    repo: Path
    backup_dir: Path
    state: Path
    env: dict[str, str]

    def run(
        self, *args: str, xtrace: bool = False, **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        run_env = self.env.copy()
        run_env.update(extra_env)
        command = ["bash", "deploy/backup/install-cron.sh", *args]
        if xtrace:
            # `bash -x` 是调试安装脚本最常用的方式：trace 会原样打印变量的值。
            command.insert(1, "-x")
        return subprocess.run(
            command,
            cwd=self.repo,
            env=run_env,
            capture_output=True,
            # macOS 自带的 bash 3.2 在 xtrace 里对多字节字符会写出不完整的字节，
            # 严格 UTF-8 解码会失败；只有 -x 这条路径需要放宽（密钥是 ASCII，
            # 用 replace 不会让"搜不到密钥"的断言失真）。
            text=True,
            errors="replace" if xtrace else "strict",
            timeout=60,
            check=False,
        )

    def crontab_lines(self) -> list[str]:
        if not self.state.exists():
            return []
        return [line for line in self.state.read_text(encoding="utf-8").splitlines() if line]

    def seed_crontab(self, lines: list[str]) -> None:
        self.state.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def calls(self, name: str) -> list[str]:
        path = Path(self.env[name])
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def crontab_calls(self) -> list[str]:
        return self.calls("CRONTAB_CALLS")

    def docker_calls(self) -> list[str]:
        return self.calls("DOCKER_CALLS")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def install_default_project(install_sh: Path) -> str:
    """独立解析 ``deploy/install.sh`` 的 ``PROJECT=`` 默认值（不复用被测脚本的逻辑）。"""
    match = _PROJECT_LINE.search(install_sh.read_text(encoding="utf-8"))
    assert match is not None, "deploy/install.sh 里找不到 PROJECT= 默认值"
    return match.group(1)


@pytest.fixture()
def cron_repo(tmp_path: Path) -> CronRepo:
    repo = tmp_path / "repo"
    (repo / "deploy" / "backup").mkdir(parents=True)
    shutil.copy2(INSTALL_CRON, repo / "deploy" / "backup" / "install-cron.sh")
    shutil.copy2(BACKUP_SH, repo / "deploy" / "backup" / "backup.sh")
    (repo / "deploy" / "backup" / "backup.sh").chmod(0o755)
    shutil.copy2(INSTALL_SH, repo / "deploy" / "install.sh")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "crontab", _CRONTAB_FAKE)
    _write_executable(bin_dir / "docker", _DOCKER_FAKE)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CRONTAB_STATE"] = str(tmp_path / "crontab.txt")
    env["CRONTAB_CALLS"] = str(tmp_path / "crontab.calls")
    env["DOCKER_CALLS"] = str(tmp_path / "docker.calls")
    # 宿主环境里可能已有这些变量；测试要看到“脚本自己的默认推导”。
    for key in (
        "LING_COMPOSE_PROJECT",
        "LING_COMPOSE_FILE",
        "LING_COMPOSE_OVERRIDE",
        "LING_COMPOSE_ENV_FILE",
        "LING_BACKUP_DIR",
        "LING_DATABASE_URL",
        "LING_ROOT_KEY",
        "PGPASSWORD",
    ):
        env.pop(key, None)

    return CronRepo(
        repo=repo,
        backup_dir=tmp_path / "backup",
        state=tmp_path / "crontab.txt",
        env=env,
    )


def _only_backup_line(repo: CronRepo) -> str:
    backup_sh = str(repo.repo / "deploy" / "backup" / "backup.sh")
    lines = [line for line in repo.crontab_lines() if backup_sh in line]
    assert len(lines) == 1, repo.crontab_lines()
    return lines[0]


def test_cron_line_carries_project_and_compose_file(cron_repo: CronRepo) -> None:
    proc = cron_repo.run(
        str(cron_repo.backup_dir),
        "04:30",
        LING_DATABASE_URL=SECRET_URL,
        PGPASSWORD=SECRET_PASSWORD,
    )
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    expected_project = install_default_project(cron_repo.repo / "deploy" / "install.sh")

    # 时间与 cd 目标：cron 行本身要能自洽（cd 与默认 compose 文件同一个根）。
    assert line.startswith("30 04 * * * ")
    cd_match = re.search(r"cd '([^']*)'", line)
    assert cd_match is not None, line
    root = Path(cd_match.group(1))
    assert Path(os.path.realpath(root)) == Path(os.path.realpath(cron_repo.repo))

    # 项目名（显式、来自 install.sh 默认值）与 compose 文件都写进了这一行。
    assert f"LING_COMPOSE_PROJECT='{expected_project}'" in line
    assert expected_project == "ling"
    assert f"LING_COMPOSE_FILE='{root}/deploy/compose.yaml'" in line
    # 既有变量不被破坏。
    assert "LING_BACKUP_VIA_COMPOSE=1" in line
    assert f"LING_BACKUP_DIR='{cron_repo.backup_dir}'" in line
    assert f"'{cron_repo.repo}/deploy/backup/backup.sh' '{cron_repo.backup_dir}'" in line
    assert f">> '{cron_repo.backup_dir}/backup.log' 2>&1" in line

    # 不含明文密码/DSN，输出里也不能带出来。
    for secret in (SECRET_URL, SECRET_PASSWORD):
        assert secret not in line
        assert secret not in proc.stdout
        assert secret not in proc.stderr

    # 严格 fake 证明脚本只用 -l 与 -，且从不调用 docker。
    assert cron_repo.crontab_calls() == ["crontab -l", "crontab -"]
    assert cron_repo.docker_calls() == []


def test_project_follows_install_sh_default_not_hardcoded(cron_repo: CronRepo) -> None:
    install_sh = cron_repo.repo / "deploy" / "install.sh"
    text = install_sh.read_text(encoding="utf-8")
    patched = _PROJECT_LINE.sub('PROJECT="ling-custom"', text, count=1)
    assert patched != text
    install_sh.write_text(patched, encoding="utf-8")

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert "LING_COMPOSE_PROJECT='ling-custom'" in line
    # 没有写死字面 ling（避免 install.sh 改默认值后两处漂移）。
    assert "LING_COMPOSE_PROJECT='ling'" not in line


def test_explicit_env_overrides_are_kept(cron_repo: CronRepo, tmp_path: Path) -> None:
    compose = tmp_path / "host-override" / "compose.yaml"
    compose.parent.mkdir()
    compose.write_text("services: {}\n", encoding="utf-8")

    proc = cron_repo.run(
        str(cron_repo.backup_dir),
        "23:05",
        LING_COMPOSE_PROJECT="ling-verify",
        LING_COMPOSE_FILE=str(compose),
    )
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert line.startswith("05 23 * * * ")
    assert "LING_COMPOSE_PROJECT='ling-verify'" in line
    assert f"LING_COMPOSE_FILE='{compose}'" in line


def test_reinstall_is_idempotent(cron_repo: CronRepo) -> None:
    backup_sh = str(cron_repo.repo / "deploy" / "backup" / "backup.sh")
    foreign = "15 2 * * * /usr/local/bin/other-job.sh --quiet"
    # 旧版本装出来的行：没有 LING_COMPOSE_PROJECT（正是本次要替换掉的那种）。
    legacy = (
        f"30 04 * * * cd {cron_repo.repo} && LING_BACKUP_VIA_COMPOSE=1 "
        f"LING_COMPOSE_FILE='{cron_repo.repo}/deploy/compose.yaml' "
        f"'{backup_sh}' '{cron_repo.backup_dir}' >> "
        f"'{cron_repo.backup_dir}/backup.log' 2>&1"
    )
    cron_repo.seed_crontab([foreign, legacy])

    first = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert first.returncode == 0, first.stderr
    second = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert second.returncode == 0, second.stderr

    lines = cron_repo.crontab_lines()
    assert len(lines) == 2, lines
    assert foreign in lines
    line = _only_backup_line(cron_repo)
    assert "LING_COMPOSE_PROJECT=" in line
    assert legacy not in lines


def test_missing_backup_dir_fails_without_writing_crontab(cron_repo: CronRepo) -> None:
    proc = cron_repo.run()
    assert proc.returncode != 0
    assert "用法" in proc.stderr
    assert not cron_repo.state.exists()
    assert cron_repo.crontab_calls() == []


def test_missing_install_sh_falls_back_to_builtin_project(cron_repo: CronRepo) -> None:
    """镜像模式的部署包**不含旧 install.sh**：不能再因此装不上备份 cron。

    设计要求（§4）："缺失旧 install.sh 时必须显式指定 LING_COMPOSE_PROJECT"。
    因此默认项目名必须自带（``ling``），而不是从 install.sh 解析——旧实现在
    install.sh 缺失时直接 exit 1，独立部署包里这条路径等于"备份永远装不上"。
    """
    (cron_repo.repo / "deploy" / "install.sh").unlink()

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert "LING_COMPOSE_PROJECT='ling'" in line
    # 来源要如实写出来（回退到内置默认值，不是"跟着 install.sh"）。
    assert "内置默认值" in proc.stdout, proc.stdout
    assert cron_repo.crontab_calls() == ["crontab -l", "crontab -"]
    # 默认值同样能被显式环境变量覆盖。
    explicit = cron_repo.run(
        str(cron_repo.backup_dir), "04:30", LING_COMPOSE_PROJECT="ling-verify"
    )
    assert explicit.returncode == 0, explicit.stderr
    assert "LING_COMPOSE_PROJECT='ling-verify'" in _only_backup_line(cron_repo)


def test_cron_line_carries_env_file_and_override(
    cron_repo: CronRepo, tmp_path: Path
) -> None:
    """文件组与 `--env-file` 都要进 cron 行：cron 环境没有 LING_* 可继承。

    镜像模式下 `LING_APP_IMAGE` 只能来自受限 env；不写 `LING_COMPOSE_ENV_FILE`
    时 compose 的 `${LING_APP_IMAGE:?}` 插值会让每日备份以 exit 2 失败。
    """
    env_file = cron_repo.repo / "deploy" / ".env"
    env_file.write_text(
        "LING_APP_IMAGE=ghcr.io/qsbb/ling-api@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    override = tmp_path / "host-override" / "compose.ws.png.yaml"
    override.parent.mkdir()
    override.write_text("services: {}\n", encoding="utf-8")

    proc = cron_repo.run(
        str(cron_repo.backup_dir),
        "04:30",
        LING_COMPOSE_OVERRIDE=str(override),
    )
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    # 默认发现逻辑：主文件同目录的 .env（这里就是 deploy/.env）。
    assert f"LING_COMPOSE_ENV_FILE='{env_file}'" in line, line
    assert f"LING_COMPOSE_OVERRIDE='{override}'" in line, line
    # 顺序与 backup.sh 拼 -f 的顺序一致：项目 → 主文件 → override → env-file。
    assert (
        line.index("LING_COMPOSE_PROJECT=")
        < line.index("LING_COMPOSE_FILE=")
        < line.index("LING_COMPOSE_OVERRIDE=")
        < line.index("LING_COMPOSE_ENV_FILE=")
    ), line


def test_missing_env_file_warns_but_still_installs(cron_repo: CronRepo) -> None:
    """找不到 env 文件时不写空变量，但必须**提示后果**（否则镜像模式静默失败）。"""
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert "LING_COMPOSE_ENV_FILE=" not in line
    assert "LING_COMPOSE_ENV_FILE" in proc.stderr, proc.stderr
    assert "LING_APP_IMAGE" in proc.stderr, proc.stderr


@pytest.mark.parametrize("which", ["override", "env_file"])
def test_missing_explicit_group_path_fails_without_writing_crontab(
    cron_repo: CronRepo, tmp_path: Path, which: str
) -> None:
    """显式指错的 override / env 文件 → exit 1，且**不写** crontab。

    装一条每天注定失败的 cron 没有意义；而且 cron 里失败只会写日志，没人看。
    """
    missing = tmp_path / "nope" / ("override.yaml" if which == "override" else "ling.env")
    key = "LING_COMPOSE_OVERRIDE" if which == "override" else "LING_COMPOSE_ENV_FILE"
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30", **{key: str(missing)})

    assert proc.returncode != 0
    assert key in proc.stderr, proc.stderr
    assert not cron_repo.state.exists()
    assert cron_repo.crontab_calls() == []


@pytest.mark.parametrize("bad_project", ["ling verify", "Ling", "'ling'", "ling;rm", "-ling"])
def test_invalid_project_is_rejected(cron_repo: CronRepo, bad_project: str) -> None:
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30", LING_COMPOSE_PROJECT=bad_project)
    assert proc.returncode != 0
    assert "项目名" in proc.stderr
    assert not cron_repo.state.exists()
    assert cron_repo.crontab_calls() == []


@pytest.mark.parametrize("bad_time", ["4:30", "24:00", "04:60", "04:30; rm -rf /", "0430"])
def test_invalid_time_is_rejected(cron_repo: CronRepo, bad_time: str) -> None:
    proc = cron_repo.run(str(cron_repo.backup_dir), bad_time)
    assert proc.returncode != 0
    assert "时间" in proc.stderr
    assert not cron_repo.state.exists()
    assert cron_repo.crontab_calls() == []


def test_crontab_read_failure_aborts_without_overwriting(cron_repo: CronRepo) -> None:
    """`crontab -l` 因权限/损坏失败时不能当成“没有条目”而覆盖别人的定时任务。"""
    cron_repo.seed_crontab(["15 2 * * * /usr/local/bin/other-job.sh"])

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30", CRONTAB_LIST_FAIL="2")
    assert proc.returncode != 0
    assert "crontab -l" in proc.stderr
    assert cron_repo.crontab_lines() == ["15 2 * * * /usr/local/bin/other-job.sh"]
    assert cron_repo.crontab_calls() == ["crontab -l"]


def test_backup_script_must_be_executable(cron_repo: CronRepo) -> None:
    (cron_repo.repo / "deploy" / "backup" / "backup.sh").chmod(0o644)

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode != 0
    assert "找不到可执行的备份脚本" in proc.stderr
    assert not cron_repo.state.exists()
    assert cron_repo.crontab_calls() == []


def test_prune_stays_off_by_default_but_is_documented(cron_repo: CronRepo) -> None:
    """TASK75 #2(d)：安装时**不开启删除**，只在注释里给出推荐参数。

    每日全量媒体副本会线性累积，但"装一次就决定删历史备份"不该是安装脚本的
    默认行为——所以生成的 crontab 行里一个 `--prune-*` 都不能有；推荐值以注释
    形式写在 install-cron.sh 里，并在结尾注意事项里提示。
    """
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    # 默认行为必须与历史版本一致：永不删除。
    assert "--prune" not in line, line
    assert "--dry-run" not in line, line
    assert "--force-unlock" not in line, line

    # 推荐参数以注释形式存在（不在被安装的那一行里）。
    script = (cron_repo.repo / "deploy" / "backup" / "install-cron.sh").read_text(encoding="utf-8")
    assert "--prune-keep 14" in script
    assert "--prune-days 30" in script
    assert "--dry-run" in script
    assert "默认不开启删除" in script or "默认关闭" in script
    # 推荐参数不能混进真正被安装的那一行：`LINE=` 的定义里不许出现任何轮转开关。
    line_assignments = [raw for raw in script.splitlines() if raw.strip().startswith("LINE=")]
    assert line_assignments, "install-cron.sh 里找不到 LINE= 定义"
    for raw in line_assignments:
        assert "--prune" not in raw, f"轮转参数混进了 crontab 行: {raw}"

    # 结尾注意事项里要能看到怎么开、以及锁的退出码。
    assert "--prune-keep" in proc.stdout
    assert "--dry-run" in proc.stdout
    assert "exit 6" in proc.stdout


# ---------------------------------------------------------------- 根密钥导出
#
# 背景（真实跨组件缺陷）：镜像模式的 `ling install` 把根密钥放在受限 `.env` 的
# `LING_ROOT_KEY` 里（compose 以环境变量注入容器），容器里没有 `/data/root.key`。
# cron 环境里也没有任何 `LING_*` 可继承，所以改动前的 cron 行调起的 backup.sh
# 两头都拿不到密钥 → manifest 只能写 `keys_env_set=false` + `keys_root=absent`：
# `ling doctor` 每天告警，`ling backup` 的完整性校验把定时备份判成"恢复到新卷
# 后凭据解不开"。只有手动 `ling backup`（自己从 .env 读值注入子进程环境）合格。
#
# 下面的用例**真执行**生成的那一行：把 backup.sh 换成记录器，在**没有**
# `LING_ROOT_KEY` 的环境里按 cron 的方式跑（`sh -c` / `bash -c`），断言记录器
# 看到的子进程环境。同时断言 crontab 全文里搜不到密钥明文。

_ENV_IMAGE = "LING_APP_IMAGE=ghcr.io/qsbb/ling-api@sha256:" + "a" * 64 + "\n"

# 记录器：替代 backup.sh，只写下"它到底拿到了什么"。
_RECORDER = """#!/usr/bin/env bash
# 测试用记录器：替代 backup.sh，证明 cron 行把哪些环境变量交给了备份脚本。
set -euo pipefail
{
  printf 'argv=%s\\n' "$*"
  printf 'root_key_is_set=%s\\n' "${LING_ROOT_KEY+1}"
  printf 'root_key=%s\\n' "${LING_ROOT_KEY-<unset>}"
  printf 'via_compose=%s\\n' "${LING_BACKUP_VIA_COMPOSE-<unset>}"
  printf 'env_file=%s\\n' "${LING_COMPOSE_ENV_FILE-<unset>}"
} > "${RECORD:?}"
"""


def _write_env_file(repo: CronRepo, body: str) -> Path:
    """写受限 env（保留写入的换行样式，用来模拟 CRLF）。"""
    env_file = repo.repo / "deploy" / ".env"
    env_file.write_text(body, encoding="utf-8", newline="")
    env_file.chmod(0o600)
    return env_file


def _install_recorder(repo: CronRepo) -> None:
    """把临时仓库里的 backup.sh 换成记录器（cron 行里的路径不变）。"""
    _write_executable(repo.repo / "deploy" / "backup" / "backup.sh", _RECORDER)


def _cron_command(line: str) -> str:
    """去掉前 5 个时间字段，留下 cron 真正交给 shell 的命令部分。"""
    parts = line.split(" ", 5)
    assert len(parts) == 6, line
    return parts[5]


def _run_cron_command(
    repo: CronRepo, line: str, record: Path, shell: str
) -> subprocess.CompletedProcess[str]:
    """按 cron 的方式执行生成的那一行（环境里**没有** LING_ROOT_KEY）。"""
    run_env = repo.env.copy()
    run_env.pop("LING_ROOT_KEY", None)
    run_env["RECORD"] = str(record)
    return subprocess.run(
        [shell, "-c", _cron_command(line)],
        cwd=repo.repo,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _recorded(record: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in record.read_text(encoding="utf-8").splitlines():
        key, _, value = raw.partition("=")
        values[key] = value
    return values


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_image_mode_cron_line_exports_root_key_without_plaintext(
    cron_repo: CronRepo, shell: str
) -> None:
    """镜像模式：cron 行要让 backup.sh 拿到根密钥，但明文不能进 crontab。

    cron 的 SHELL 通常是 `/bin/sh`（Ubuntu 上是 dash），所以两种 shell 都跑一遍。
    """
    env_file = _write_env_file(
        cron_repo,
        _ENV_IMAGE + f"POSTGRES_PASSWORD={SECRET_PASSWORD}\nLING_ROOT_KEY={ROOT_KEY}\n",
    )
    proc = cron_repo.run(
        str(cron_repo.backup_dir),
        "04:30",
        LING_DATABASE_URL=SECRET_URL,
        PGPASSWORD=SECRET_PASSWORD,
    )
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    crontab_text = cron_repo.state.read_text(encoding="utf-8")

    # ① 行里只有"怎么读"（路径 + sed 表达式），没有"值"。
    assert ROOT_KEY not in line
    assert ROOT_KEY not in crontab_text
    assert ROOT_KEY not in proc.stdout
    assert ROOT_KEY not in proc.stderr
    assert 'LING_ROOT_KEY="$(sed' in line
    assert str(env_file) in line
    # 仍然直接调 backup.sh：`ling doctor` 用 "backup.sh" in crontab 判断"已安装"，
    # 换成包装脚本会让它报"crontab 里没有 backup.sh"。
    assert str(cron_repo.repo / "deploy" / "backup" / "backup.sh") in line
    # 安装输出要如实说明"会导出"，否则运维无从判断镜像模式是否被覆盖。
    assert "根密钥导出" in proc.stdout
    assert "LING_ROOT_KEY" in proc.stdout

    # ② 真执行：在没有 LING_ROOT_KEY 的环境里跑这一行，backup.sh 必须拿到值。
    _install_recorder(cron_repo)
    record = cron_repo.backup_dir.parent / f"record-{shell}.txt"
    run = _run_cron_command(cron_repo, line, record, shell)
    assert run.returncode == 0, (run.stdout, run.stderr)
    got = _recorded(record)
    assert got["root_key_is_set"] == "1", got
    assert got["root_key"] == ROOT_KEY, got
    assert got["via_compose"] == "1", got
    assert got["env_file"] == str(env_file), got
    assert got["argv"] == str(cron_repo.backup_dir), got


def test_root_key_is_read_at_runtime_not_baked_into_cron(cron_repo: CronRepo) -> None:
    """值不固化在 cron 行里：换掉 .env 的密钥，下一次 cron 用新值，不用重装。"""
    _write_env_file(cron_repo, _ENV_IMAGE + f"LING_ROOT_KEY={ROOT_KEY}\n")
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr
    line = _only_backup_line(cron_repo)

    rotated = ROOT_KEY + "-rotated"
    _write_env_file(cron_repo, _ENV_IMAGE + f"LING_ROOT_KEY={rotated}\n")
    crontab_text = cron_repo.state.read_text(encoding="utf-8")
    assert rotated not in line
    assert rotated not in crontab_text

    _install_recorder(cron_repo)
    record = cron_repo.backup_dir.parent / "record-rotated.txt"
    run = _run_cron_command(cron_repo, line, record, "sh")
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert _recorded(record)["root_key"] == rotated


@pytest.mark.parametrize(
    "env_body",
    [
        # 源码模式：.env 在位，但根密钥在容器卷 /data/root.key 里。
        _ENV_IMAGE + f"POSTGRES_PASSWORD={SECRET_PASSWORD}\n",
        # 连 .env 都没有：既有行为是不写 LING_COMPOSE_ENV_FILE 并告警，但仍要装成功。
        "",
    ],
    ids=["no_root_key_in_env", "no_env_file"],
)
def test_source_mode_without_root_key_keeps_old_behavior(
    cron_repo: CronRepo, env_body: str
) -> None:
    """env 里没有 LING_ROOT_KEY → 不导出、不报错，cron 行与改动前一致。"""
    if env_body:
        _write_env_file(cron_repo, env_body)

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    # 整段导出都不加：行里既没有变量名，也没有命令替换。
    assert "LING_ROOT_KEY" not in line
    assert "$(" not in line
    assert line.startswith("30 04 * * * cd ")
    assert "LING_BACKUP_VIA_COMPOSE=1" in line
    assert "根密钥导出" in proc.stdout

    # 真执行：该项**未设置**（不是设成空串）→ backup.sh 走 /data/root.key 那条路，
    # manifest 仍记 keys_env_set=false，与改动前逐字节同行为。
    _install_recorder(cron_repo)
    record = cron_repo.backup_dir.parent / "record-source.txt"
    run = _run_cron_command(cron_repo, line, record, "sh")
    assert run.returncode == 0, (run.stdout, run.stderr)
    got = _recorded(record)
    assert got["root_key_is_set"] == "", got
    assert got["root_key"] == "<unset>", got


def test_root_key_uses_last_assignment_and_strips_quotes_and_crlf(
    cron_repo: CronRepo,
) -> None:
    """与 compose 的 env-file 语义对齐：最后一条赋值生效、去引号、容忍 CRLF。

    否则会出现"应用用的是去引号后的值，cron 导出的是带引号的另一份"这种静默分歧。
    """
    _write_env_file(
        cron_repo,
        "POSTGRES_PASSWORD=" + SECRET_PASSWORD + "\r\n"
        "LING_ROOT_KEY=stale-value-must-lose\r\n"
        f'LING_ROOT_KEY="{ROOT_KEY}"\r\n',
    )
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert ROOT_KEY not in line
    assert "stale-value-must-lose" not in line

    _install_recorder(cron_repo)
    record = cron_repo.backup_dir.parent / "record-last.txt"
    run = _run_cron_command(cron_repo, line, record, "sh")
    assert run.returncode == 0, (run.stdout, run.stderr)
    got = _recorded(record)
    assert got["root_key_is_set"] == "1", got
    assert got["root_key"] == ROOT_KEY, got


def test_empty_last_assignment_means_no_key(cron_repo: CronRepo) -> None:
    """最后一次赋值是空的 = 应用那次插值为空 → 不导出（与 compose 语义一致）。"""
    _write_env_file(
        cron_repo, _ENV_IMAGE + "LING_ROOT_KEY=looks-real-but-overridden\nLING_ROOT_KEY=\n"
    )
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert "LING_ROOT_KEY" not in line
    assert "looks-real-but-overridden" not in line


@pytest.mark.skipif(os.geteuid() == 0, reason="root 无视 0000 权限，无法模拟“当前用户不可读”")
def test_unreadable_env_file_warns_and_does_not_export(cron_repo: CronRepo) -> None:
    """env 不可读时不能假装"没有密钥"就完事：要告警，且安装仍然成功。"""
    env_file = _write_env_file(cron_repo, _ENV_IMAGE + f"LING_ROOT_KEY={ROOT_KEY}\n")
    env_file.chmod(0o000)

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode == 0, proc.stderr

    line = _only_backup_line(cron_repo)
    assert "LING_ROOT_KEY" not in line
    assert "不可读" in proc.stderr, proc.stderr
    # 告警本身也不能把密钥带出来。
    assert ROOT_KEY not in proc.stderr
    assert ROOT_KEY not in proc.stdout


def test_xtrace_debug_does_not_print_root_key(cron_repo: CronRepo) -> None:
    """`bash -x` 调试安装脚本也不能把密钥打进终端/CI 日志。

    安装脚本要在内存里读一遍 env 才能判断"要不要导出"，所以判空期间必须关掉
    xtrace：`[[ -n <值> ]]` 的 trace 会把操作数原样打出来（这个用例就是为此加的）。
    """
    _write_env_file(cron_repo, _ENV_IMAGE + f"LING_ROOT_KEY={ROOT_KEY}\n")
    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30", xtrace=True)
    assert proc.returncode == 0, proc.stderr
    assert "env_file_root_key_present" in proc.stderr, "trace 没生效，用例失去意义"
    assert ROOT_KEY not in proc.stderr
    assert ROOT_KEY not in proc.stdout
    assert ROOT_KEY not in cron_repo.state.read_text(encoding="utf-8")
