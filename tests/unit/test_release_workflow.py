"""发布流程的单元测试：版本一致性、tag 安全传参、CI/Release 触发结构。

不构建、不打包、不访问网络、不创建真实 tag/Release。git 用例只在临时仓库里跑。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_release.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def load_check_release() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ling_check_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = load_check_release()


def write_versions(root: Path, version: str = "0.1.0") -> None:
    (root / "src" / "ling").mkdir(parents=True, exist_ok=True)
    (root / "clients" / "web-console").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "ling"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / "ling" / "__init__.py").write_text(
        f'"""stub"""\n\n__all__ = ["__version__"]\n__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "clients" / "web-console" / "package.json").write_text(
        json.dumps({"name": "ling-web-console", "private": True, "version": version}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 版本一致性
# ---------------------------------------------------------------------------


def test_repository_versions_are_consistent() -> None:
    version = check_release.read_pyproject_version(REPO_ROOT / "pyproject.toml")
    assert check_release.main(["--tag", f"v{version}", "--root", str(REPO_ROOT)]) == 0


def test_tag_must_match_all_three_versions(tmp_path: Path) -> None:
    write_versions(tmp_path, "0.1.0")
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 0
    assert check_release.main(["--tag", "v0.1.1", "--root", str(tmp_path)]) == 1


def test_mismatch_reports_every_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_versions(tmp_path, "0.1.0")
    (tmp_path / "clients" / "web-console" / "package.json").write_text(
        json.dumps({"version": "0.2.0"}), encoding="utf-8"
    )
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "pyproject.toml=0.1.0" in err
    assert "package.json=0.2.0" in err


@pytest.mark.parametrize(
    "tag",
    [
        "0.1.0",
        "v0.1",
        "v0.1.0.1",
        "v0.1.0-rc.1",
        "v0.1.0+build",
        "v01.2.3",
        "v0.1.0; touch pwned",
        "--upload-pack=touch",
    ],
)
def test_unstable_or_unsafe_tags_are_rejected(
    tag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_versions(tmp_path)
    assert check_release.main(["--tag", tag, "--root", str(tmp_path)]) == 2
    # 拒绝方式可能是“不是稳定版 tag”，也可能是 argparse 认为以 - 开头的值
    # 本身像另一个选项——两种都以退出码 2 结束，不会进 git 或文件路径。
    assert capsys.readouterr().err.strip()


def test_version_check_never_executes_python_source(tmp_path: Path) -> None:
    write_versions(tmp_path)
    marker = tmp_path / "executed"
    (tmp_path / "src" / "ling" / "__init__.py").write_text(
        f'"""stub"""\n\n__version__ = "0.1.0"\n\nopen({str(marker)!r}, "w").write("boom")\n',
        encoding="utf-8",
    )
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 0
    assert not marker.exists()


def test_script_does_not_use_shell_or_eval() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "os.system" not in text
    assert "eval(" not in text
    assert "import ling" not in text


def test_malicious_tag_env_never_reaches_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    env = os.environ.copy()
    env["RELEASE_TAG"] = f"v0.1.0; touch {marker}"
    env.pop("GITHUB_REF_NAME", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "只接受稳定版 tag" in proc.stderr
    assert not marker.exists()

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", f"$(touch {marker})"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert not marker.exists()


# ---------------------------------------------------------------------------
# git 祖先检查（临时仓库）
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def _make_git_repo(tmp_path: Path, *, tag_on_feature: bool) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir(parents=True)
    init = _run_git(repo, "init", "-b", "main")
    if init.returncode != 0:
        pytest.skip(f"git init -b 不可用：{init.stderr.strip()}")
    write_versions(repo)
    assert _run_git(repo, "add", "-A").returncode == 0
    commit = _run_git(repo, "commit", "-m", "init")
    assert commit.returncode == 0, commit.stderr
    if tag_on_feature:
        assert _run_git(repo, "checkout", "-b", "feature").returncode == 0
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        assert _run_git(repo, "add", "-A").returncode == 0
        assert _run_git(repo, "commit", "-m", "feature").returncode == 0
    assert _run_git(repo, "tag", "v0.1.0").returncode == 0
    return repo


def test_tag_must_be_ancestor_of_main(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path, tag_on_feature=False)
    assert (
        check_release.main(
            ["--tag", "v0.1.0", "--root", str(repo), "--check-ancestor", "--main-ref", "main"]
        )
        == 0
    )

    feature_repo = _make_git_repo(tmp_path / "second", tag_on_feature=True)
    assert (
        check_release.main(
            [
                "--tag",
                "v0.1.0",
                "--root",
                str(feature_repo),
                "--check-ancestor",
                "--main-ref",
                "main",
            ]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# CI / Release 工作流结构
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict | None:
    if importlib.util.find_spec("yaml") is None:
        return None
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _trigger(data: dict) -> dict:
    # PyYAML 按 YAML 1.1 把裸 on 解析成布尔 True。
    value = data.get("on")
    if value is None:
        value = data.get(True)
    assert isinstance(value, dict)
    return value


def test_ci_is_reusable_by_release() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_call:" in text
    data = _load_yaml(CI_WORKFLOW)
    if data is None:
        return
    triggers = _trigger(data)
    assert "workflow_call" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers


def test_release_workflow_only_triggers_on_stable_tags() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # 注释可以提，但 trigger 里不能真的出现 pull_request/分支触发。
    assert "\n  pull_request:" not in text
    data = _load_yaml(RELEASE_WORKFLOW)
    if data is None:
        assert "tags:" in text
        assert "v[0-9]+.[0-9]+.[0-9]+" in text
        return
    triggers = _trigger(data)
    assert set(triggers) == {"push"}
    push = triggers["push"]
    assert "branches" not in push
    assert push["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    # GitHub 的 tag 过滤里 [0-9]+ 是“一位或多位数字”；该模式不接受预发布后缀。
    stable = re.compile(r"v\d+\.\d+\.\d+")
    assert stable.fullmatch("v1.2.3")
    assert not stable.fullmatch("v1.2.3-rc.1")
    assert not stable.fullmatch("v1.2")


def test_release_workflow_verifies_then_reuses_ci_then_publishes() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    data = _load_yaml(RELEASE_WORKFLOW)
    assert "fetch-depth: 0" in text
    assert "scripts/check_release.py" in text
    assert "--check-ancestor" in text
    assert "uses: ./.github/workflows/ci.yml" in text
    assert "gh release create" in text
    assert "--verify-tag" in text
    assert "--generate-notes" in text
    assert "contents: write" in text
    assert "RELEASE_TAG: ${{ github.ref_name }}" in text
    if data is None:
        return
    assert data["permissions"] == {"contents": "read"}
    jobs = data["jobs"]
    assert set(jobs) == {"verify", "ci", "release"}
    assert jobs["verify"].get("permissions") is None
    assert jobs["ci"].get("permissions") is None
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert jobs["ci"]["needs"] == "verify"
    assert jobs["release"]["needs"] == "ci"
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    verify_step = jobs["verify"]["steps"][0]
    assert verify_step["uses"].startswith("actions/checkout@")
    assert verify_step["with"]["fetch-depth"] == 0
    run = " ".join(
        step.get("run", "") for step in jobs["verify"]["steps"] if isinstance(step, dict)
    )
    assert "--tag" in run and '"$RELEASE_TAG"' in run


# ---------------------------------------------------------------------------
# 版本字面量扫描（src/ling/app.py / src/ling/logging_setup.py）
# ---------------------------------------------------------------------------


def _write_scan_file(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_SCAN_FILE_PREFIX = (
    '"""stub — 只被静态扫描，不导入也不执行"""\n\n'
    "from ling import __version__\n\n"
    "SERVICE_VERSION = __version__\n"
)


def test_stale_literal_in_app_py_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """app.py 里又写回死版本 → 判失败（防 /api/openapi.json 报另一个版本号）。"""
    write_versions(tmp_path, "0.0.1")
    _write_scan_file(
        tmp_path,
        "src/ling/app.py",
        'from ling import __version__\n\napp = FastAPI(version="0.1.0")\n',
    )
    assert check_release.main(["--tag", "v0.0.1", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    # 失败信息是 path:line，行号精确到放字面量的那一行
    assert "src/ling/app.py:3" in err


def test_stale_literal_in_logging_setup_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """logging_setup.py 里又写回死版本 → 判失败（防启动日志报另一个版本号）。"""
    write_versions(tmp_path, "0.0.1")
    _write_scan_file(
        tmp_path,
        "src/ling/logging_setup.py",
        'from ling import __version__\n\nSERVICE_VERSION = "0.1.0"\n',
    )
    assert check_release.main(["--tag", "v0.0.1", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "src/ling/logging_setup.py:3" in err


def test_scan_files_referencing_version_constant_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """两个被扫文件都引用 __version__（不含字面量）→ 通过，且没有"未找到"提示。"""
    write_versions(tmp_path, "0.1.0")
    _write_scan_file(
        tmp_path,
        "src/ling/app.py",
        "from ling import __version__\n\napp = FastAPI(version=__version__)\n",
    )
    _write_scan_file(tmp_path, "src/ling/logging_setup.py", _SCAN_FILE_PREFIX)
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "未找到" not in captured.out
    assert captured.err == ""


def test_tool_spec_version_outside_scan_scope_is_ignored(tmp_path: Path) -> None:
    """capabilities/tools.py 的 ToolSpec.version="1.0.0" 是工具 ABI 版本，不扫 → 通过。

    这是防误报回归：如果把 _VERSION_SCAN_FILES 扩成整棵源码树，这条会红。
    """
    write_versions(tmp_path, "0.1.0")
    _write_scan_file(
        tmp_path,
        "src/ling/app.py",
        "from ling import __version__\n\napp = FastAPI(version=__version__)\n",
    )
    _write_scan_file(tmp_path, "src/ling/logging_setup.py", _SCAN_FILE_PREFIX)
    _write_scan_file(
        tmp_path,
        "src/ling/capabilities/tools.py",
        'TOOL_SPEC_VERSION = "1.0.0"\n',
    )
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 0


def test_missing_scan_files_are_reported_but_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """夹具里没有 app.py / logging_setup.py：放行（exit 0）但必须打印"未检查"。"""
    write_versions(tmp_path, "0.1.0")
    assert check_release.main(["--tag", "v0.1.0", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "注意：未找到 src/ling/app.py，本次未检查（临时夹具下正常）" in out
    assert "注意：未找到 src/ling/logging_setup.py，本次未检查（临时夹具下正常）" in out


def test_find_stale_version_literals_contract_on_repository() -> None:
    """真实仓库：返回值是 (hits, skipped) 二元组，两个被扫文件都在、且没有旧字面量。"""
    version = check_release.read_pyproject_version(REPO_ROOT / "pyproject.toml")
    hits, skipped = check_release.find_stale_version_literals(REPO_ROOT, version)
    assert hits == []
    assert skipped == []
