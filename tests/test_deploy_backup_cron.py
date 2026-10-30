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
``ling``）、``LING_COMPOSE_*`` 既有覆盖不被破坏、不含明文密码、重复安装幂等、
缺参数/项目名不可解析/取值非法时失败退出且**不写** crontab。
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


@dataclass
class CronRepo:
    """临时仓库 + 严格 fake 的运行环境。"""

    repo: Path
    backup_dir: Path
    state: Path
    env: dict[str, str]

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        run_env = self.env.copy()
        run_env.update(extra_env)
        return subprocess.run(
            ["bash", "deploy/backup/install-cron.sh", *args],
            cwd=self.repo,
            env=run_env,
            capture_output=True,
            text=True,
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
        "LING_BACKUP_DIR",
        "LING_DATABASE_URL",
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


def test_unresolvable_project_fails_without_writing_crontab(cron_repo: CronRepo) -> None:
    (cron_repo.repo / "deploy" / "install.sh").unlink()

    proc = cron_repo.run(str(cron_repo.backup_dir), "04:30")
    assert proc.returncode != 0
    assert "LING_COMPOSE_PROJECT" in proc.stderr
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
