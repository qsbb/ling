"""Tests for one-line source install/upgrade and the Debian mirror helper."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml
from deploy.configure_apt_mirror import configure_apt_mirror

REPO_ROOT = Path(__file__).resolve().parents[1]
UPGRADE = REPO_ROOT / "deploy" / "upgrade.sh"
BOOTSTRAP_UPGRADE = (
    "test -z \"$(git status --porcelain --untracked-files=normal)\""
    " && git fetch --no-tags origin main"
    " && git merge-base --is-ancestor HEAD FETCH_HEAD"
    " && { ! git show-ref --verify --quiet refs/heads/main"
    " || git merge-base --is-ancestor refs/heads/main FETCH_HEAD; }"
    " && git switch -C main FETCH_HEAD"
    " && bash deploy/upgrade.sh"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    install_root = tmp_path / "installed" / "ling"
    log_path = tmp_path / "install.log"

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        capture_output=True,
    )
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "remote", "add", "origin", str(remote))

    (source / "deploy").mkdir()
    (source / ".gitignore").write_text("deploy/.env\n", encoding="utf-8")
    (source / "deploy" / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        "cd \"$(dirname \"$0\")/..\"\n"
        "printf '%s|%s\\n' \"$PWD\" \"$*\" >> \"${INSTALL_CALL_LOG:?}\"\n"
        "exit \"${INSTALL_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    (source / "version.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "initial source")
    _git(source, "push", "--set-upstream", "origin", "main")
    return remote, source, install_root, log_path


def _test_env(
    tmp_path: Path, install_root: Path, log_path: Path
) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("LING_INSTALL_DIR", "LING_INSTALL_REF", "LING_INSTALL_REMOTE"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "LING_INSTALL_DIR": str(install_root),
            "INSTALL_CALL_LOG": str(log_path),
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _clone_install(remote: Path, install_root: Path) -> subprocess.CompletedProcess[str]:
    install_root.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(install_root)],
        check=False,
        text=True,
        capture_output=True,
    )


def _run_upgrade(
    tmp_path: Path,
    install_root: Path,
    log_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _test_env(tmp_path, install_root, log_path)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(UPGRADE), *args],
        cwd=tmp_path,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_one_line_install_clones_and_runs_installer_once(tmp_path: Path) -> None:
    remote, _, install_root, log_path = _source_repo(tmp_path)
    env = _test_env(tmp_path, install_root, log_path)
    env["INSTALL_EXIT"] = "0"
    command = (
        f"git clone --quiet --depth 1 --single-branch --branch main "
        f"{shlex.quote(str(remote))} {shlex.quote(str(install_root))} "
        f"&& bash {shlex.quote(str(install_root / 'deploy/install.sh'))} --port 6520"
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (install_root / "version.txt").read_text(encoding="utf-8") == "first\n"
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"{install_root}|--port 6520"
    ]


def test_one_line_upgrade_fast_forwards_and_preserves_configuration(tmp_path: Path) -> None:
    remote, source, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    first = _run_upgrade(tmp_path, install_root, log_path)
    assert first.returncode == 0, first.stderr

    env_file = install_root / "deploy" / ".env"
    env_file.write_text("LING_ROOT_KEY=test-secret\n", encoding="utf-8")
    (source / "version.txt").write_text("second\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "second source")
    _git(source, "push")

    result = _run_upgrade(tmp_path, install_root, log_path)

    assert result.returncode == 0, result.stderr
    assert (install_root / "version.txt").read_text(encoding="utf-8") == "second\n"
    assert env_file.read_text(encoding="utf-8") == "LING_ROOT_KEY=test-secret\n"
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 2


def test_v008_checkout_bootstraps_missing_upgrade_script_in_one_line(
    tmp_path: Path,
) -> None:
    remote, source, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    old_commit = _git(install_root, "rev-parse", "HEAD")

    (source / "version.txt").write_text("checkpoint\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "main checkpoint")
    _git(source, "push")
    fast_forward = subprocess.run(
        ["git", "-C", str(install_root), "pull", "--ff-only", "origin", "main"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert fast_forward.returncode == 0, fast_forward.stderr
    checkout = subprocess.run(
        ["git", "-C", str(install_root), "checkout", "--detach", old_commit],
        check=False,
        text=True,
        capture_output=True,
    )
    assert checkout.returncode == 0, checkout.stderr

    env_file = install_root / "deploy" / ".env"
    env_file.write_text("LING_ROOT_KEY=preserve-me\n", encoding="utf-8")
    (source / "deploy" / "upgrade.sh").write_text(
        UPGRADE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (source / "version.txt").write_text("second\n", encoding="utf-8")
    _git(source, "add", "deploy/upgrade.sh", "version.txt")
    _git(source, "commit", "-m", "add upgrade entry point")
    _git(source, "push")

    result = subprocess.run(
        ["bash", "-c", BOOTSTRAP_UPGRADE],
        cwd=install_root,
        env=_test_env(tmp_path, install_root, log_path),
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert _git(install_root, "branch", "--show-current") == "main"
    assert (install_root / "version.txt").read_text(encoding="utf-8") == "second\n"
    assert env_file.read_text(encoding="utf-8") == "LING_ROOT_KEY=preserve-me\n"
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"{install_root}|"
    ]


def test_v008_bootstrap_refuses_to_reset_diverged_local_main(tmp_path: Path) -> None:
    remote, source, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    old_commit = _git(install_root, "rev-parse", "HEAD")

    (source / "deploy" / "upgrade.sh").write_text(
        UPGRADE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (source / "version.txt").write_text("remote update\n", encoding="utf-8")
    _git(source, "add", "deploy/upgrade.sh", "version.txt")
    _git(source, "commit", "-m", "remote upgrade")
    _git(source, "push")

    (install_root / "version.txt").write_text("local-only commit\n", encoding="utf-8")
    _git(install_root, "add", "version.txt")
    _git(install_root, "commit", "-m", "local-only work")
    local_main = _git(install_root, "rev-parse", "main")
    checkout = subprocess.run(
        ["git", "-C", str(install_root), "checkout", "--detach", old_commit],
        check=False,
        text=True,
        capture_output=True,
    )
    assert checkout.returncode == 0, checkout.stderr

    result = subprocess.run(
        ["bash", "-c", BOOTSTRAP_UPGRADE],
        cwd=install_root,
        env=_test_env(tmp_path, install_root, log_path),
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert _git(install_root, "rev-parse", "HEAD") == old_commit
    assert _git(install_root, "rev-parse", "main") == local_main
    assert not log_path.exists()


def test_one_line_upgrade_can_repeat_current_version_without_losing_configuration(
    tmp_path: Path,
) -> None:
    remote, _, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    first = _run_upgrade(tmp_path, install_root, log_path)
    assert first.returncode == 0, first.stderr
    env_file = install_root / "deploy" / ".env"
    env_file.write_text("LING_TEST_MODE=0\n", encoding="utf-8")

    result = _run_upgrade(tmp_path, install_root, log_path)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "LING_TEST_MODE=0\n"
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 2


def test_one_line_upgrade_rejects_dirty_install_before_running_installer(tmp_path: Path) -> None:
    remote, _, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    first = _run_upgrade(tmp_path, install_root, log_path)
    assert first.returncode == 0, first.stderr
    (install_root / "version.txt").write_text("local edit\n", encoding="utf-8")

    result = _run_upgrade(tmp_path, install_root, log_path)

    assert result.returncode != 0
    assert "未提交或未跟踪文件" in result.stderr
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_one_line_upgrade_refuses_to_rebuild_image_mode_install(tmp_path: Path) -> None:
    remote, _, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    first = _run_upgrade(tmp_path, install_root, log_path)
    assert first.returncode == 0, first.stderr
    (install_root / "deploy" / ".env").write_text(
        "LING_APP_IMAGE=ghcr.io/qsbb/ling-api@sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )

    result = _run_upgrade(tmp_path, install_root, log_path)

    assert result.returncode != 0
    assert "镜像模式发布流程" in result.stderr
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_one_line_upgrade_propagates_installer_failure(tmp_path: Path) -> None:
    remote, _, install_root, log_path = _source_repo(tmp_path)
    clone = _clone_install(remote, install_root)
    assert clone.returncode == 0, clone.stderr
    result = _run_upgrade(
        tmp_path,
        install_root,
        log_path,
        env_overrides={"INSTALL_EXIT": "9"},
    )

    assert result.returncode == 9


def test_configure_apt_mirror_rewrites_only_active_debian_uris(tmp_path: Path) -> None:
    apt_root = tmp_path / "apt"
    source_dir = apt_root / "sources.list.d"
    source_dir.mkdir(parents=True)
    list_file = apt_root / "sources.list"
    list_file.write_text(
        "# deb http://deb.debian.org/debian stable main\n"
        "deb http://deb.debian.org/debian stable main\n"
        "deb http://mirror.example/debian stable main\n",
        encoding="utf-8",
    )
    deb822_file = source_dir / "debian.sources"
    deb822_file.write_text(
        "Types: deb\n"
        "URIs: https://deb.debian.org/debian https://deb.debian.org/debian-security\n"
        "Suites: stable stable-updates\n",
        encoding="utf-8",
    )

    count = configure_apt_mirror(apt_root, "mirrors.aliyun.com")

    assert count == 3
    assert list_file.read_text(encoding="utf-8") == (
        "# deb http://deb.debian.org/debian stable main\n"
        "deb http://mirrors.aliyun.com/debian stable main\n"
        "deb http://mirror.example/debian stable main\n"
    )
    assert "https://mirrors.aliyun.com/debian-security" in deb822_file.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "host",
    ["https://mirror.example", "mirror.example:8080", "mirror/path", "-invalid.example"],
)
def test_configure_apt_mirror_rejects_non_host_values(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError, match="hostname"):
        configure_apt_mirror(tmp_path, host)


def test_configure_apt_mirror_fails_when_no_official_source_exists(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no deb.debian.org source"):
        configure_apt_mirror(tmp_path, "mirrors.aliyun.com")


def _assert_apt_mirror_wiring(dockerfile: str, compose: dict, env_example: str) -> None:
    assert 'ARG APT_MIRROR_HOST=""' in dockerfile
    assert "COPY deploy/configure_apt_mirror.py /tmp/configure_apt_mirror.py" in dockerfile
    assert 'python3 /tmp/configure_apt_mirror.py "$APT_MIRROR_HOST"' in dockerfile
    assert "deploy/configure_apt_mirror.py ./deploy/" in dockerfile
    assert "APT_MIRROR_HOST=" in env_example
    for service_name in ("api", "worker"):
        build_args = compose["services"][service_name]["build"]["args"]
        assert "APT_MIRROR_HOST" in build_args


def test_apt_mirror_build_argument_reaches_both_services() -> None:
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    compose = yaml.safe_load((REPO_ROOT / "deploy" / "compose.yaml").read_text())
    env_example = (REPO_ROOT / "deploy" / "env.example").read_text(encoding="utf-8")

    _assert_apt_mirror_wiring(dockerfile, compose, env_example)


def test_apt_mirror_wiring_guard_catches_missing_worker_argument() -> None:
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    compose = yaml.safe_load((REPO_ROOT / "deploy" / "compose.yaml").read_text())
    compose["services"]["worker"]["build"]["args"].pop("APT_MIRROR_HOST")
    env_example = (REPO_ROOT / "deploy" / "env.example").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_apt_mirror_wiring(dockerfile, compose, env_example)
