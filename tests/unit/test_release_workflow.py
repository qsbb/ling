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
UPDATER_DOCKERFILE = REPO_ROOT / "deploy" / "updater" / "Dockerfile"


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


def _needs(job: dict) -> list[str]:
    """把 job 的 `needs` 归一化成列表（单值/缺省都能处理）。"""
    deps = job.get("needs")
    if deps is None:
        return []
    return [deps] if isinstance(deps, str) else list(deps)


def _needs_closure(jobs: dict, job_name: str) -> set[str]:
    """`needs` 的传递闭包：直接或间接依赖到的全部 job 名。"""
    stack = [job_name]
    seen: set[str] = set()
    while stack:
        for dep in _needs(jobs.get(stack.pop()) or {}):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


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
    # `workflow_dispatch` 是发版前的流水线验证入口：它只构建并推送
    # `sha-<commit>` 镜像、不建 Release（release job 有 event_name 门禁），
    # 所以 tag 触发仍然只有稳定版 vX.Y.Z 这一条。
    assert set(triggers) == {"push", "workflow_dispatch"}
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
    # 原来这里断言 `--generate-notes`；现在正文还**必须**带上两个 index digest，
    # 而 `--generate-notes` 无法与自定义正文拼在一起。改成先调
    # `releases/generate-notes` 取自动正文、拼上 digest 段、再 `--notes-file`
    # 一次创建（顺序见上：生成失败时 Release 还没被建出来）。见
    # test_release_body_carries_both_digests_and_says_they_are_not_trusted。
    assert "releases/generate-notes" in text
    assert "--notes-file" in text
    assert "contents: write" in text
    assert "RELEASE_TAG: ${{ github.ref_name }}" in text
    if data is None:
        return
    assert data["permissions"] == {"contents": "read"}
    jobs = data["jobs"]
    # 五个门禁 + 发布本身（`verify` / `ci` / `mobile` / `image` / `updater-image`）。
    # `mobile` 是后加的：移动端编译不过时不该发版（lib/api/ling_api.dart 曾经有
    # 类型错误让整个 App 编译不了，而那时没有任何门禁会红）。`image` 与
    # `updater-image` 也是门禁：镜像没推送成功就不能宣称"可镜像部署"，也更不能
    # 让更新器拿不到一个可按 digest 固定部署的镜像，两者都必须在 release 的
    # 依赖闭包里（见下面的闭包断言）。
    assert set(jobs) == {"verify", "ci", "mobile", "image", "updater-image", "release"}
    assert jobs["verify"].get("permissions") is None
    assert jobs["ci"].get("permissions") is None
    assert jobs["mobile"].get("permissions") is None
    # 推镜像的 job 权限必须**恰为**"读源码 + 推 GHCR"：多一个 contents: write
    # 就等于让一个会执行仓库代码（Dockerfile）的 job 也能改 Release。
    assert jobs["image"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["updater-image"]["permissions"] == {"contents": "read", "packages": "write"}
    # PR/fork 不得持有推镜像的 token（release.yml 本身也没有 pull_request 触发，
    # 这里再加事件/仓库门禁是纵深防御）。
    assert "github.event_name != 'pull_request'" in text
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert jobs["ci"]["needs"] == "verify"
    assert jobs["mobile"]["needs"] == "verify"
    # 镜像失败不能让 Release 先成功：release 必须直接依赖两个镜像 job。
    release_needs = _needs(jobs["release"])
    assert "image" in release_needs
    assert "updater-image" in release_needs
    # 手动触发只验证流水线，不建 Release。
    assert "github.event_name == 'push'" in text
    # 发布要等**所有**门禁。用传递闭包而不是逐项列举：`release` 直接依赖
    # `[ci, mobile, image, updater-image]`，而它们各自 `needs: verify`，所以
    # verify 虽不在 release.needs 里也已被覆盖。这条断言的作用是让"以后再加一个
    # 门禁却忘了接进发布链"必然变红——漏接一个就等于那个门禁形同虚设。
    gates = set(jobs) - {"release"}
    assert _needs_closure(jobs, "release") == gates, (
        f"release 的依赖闭包 {_needs_closure(jobs, 'release')} 没有覆盖全部门禁 {gates}"
    )
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["mobile"]["uses"] == "./.github/workflows/mobile.yml"
    verify_step = jobs["verify"]["steps"][0]
    assert verify_step["uses"].startswith("actions/checkout@")
    assert verify_step["with"]["fetch-depth"] == 0
    run = " ".join(
        step.get("run", "") for step in jobs["verify"]["steps"] if isinstance(step, dict)
    )
    assert "--tag" in run and '"$RELEASE_TAG"' in run


# ---------------------------------------------------------------------------
# 更新器镜像（Dockerfile + release.yml 的 updater-image job）与发布目录门禁
# ---------------------------------------------------------------------------


def test_updater_image_job_is_a_gate_with_exact_permissions() -> None:
    """更新器镜像是一个**门禁**：排在 image 之后、权限恰好两项、按 digest 记录。"""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    data = _load_yaml(RELEASE_WORKFLOW)
    if data is None:
        assert "updater-image:" in text
        return
    jobs = data["jobs"]
    assert "updater-image" in jobs
    job = jobs["updater-image"]
    # 门禁顺序：verify → ci + mobile → image → updater-image。
    assert sorted(_needs(job)) == ["ci", "image", "mobile", "verify"]
    # 权限**恰好**"读源码 + 推 GHCR"：没有 contents: write（不建 Release），
    # 也没有 id-token（不用原生构建证明）。
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    # 与 image job 一样挡住 PR/fork（本 job 也持有 packages: write）。
    assert "github.event_name != 'pull_request'" in str(job["if"])
    assert job["env"]["IMAGE"] == "ghcr.io/qsbb/ling-updater"
    build = [step for step in job["steps"] if step.get("id") == "build"]
    assert len(build) == 1, "更新器镜像只应有唯一一次 build+push"
    with_ = build[0]["with"]
    assert with_["context"] == "."
    assert with_["file"] == "deploy/updater/Dockerfile"
    assert with_["platforms"] == "linux/amd64,linux/arm64"
    assert with_["push"] is True
    # index digest 既是 job output（release 正文要用），也必须进 job summary
    # （更新器按 digest 固定部署，summary 才是可信来源）。
    assert job["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    run = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))
    assert "$GITHUB_STEP_SUMMARY" in run
    assert "index digest" in run
    # tag 规则：sha-<commit> 必推、vX.Y.Z 只在 tag 事件，**不**推 latest。
    assert "$IMAGE:sha-$GITHUB_SHA" in run
    assert "$IMAGE:$release_tag" in run
    assert ":latest" not in run


def test_release_body_carries_both_digests_and_says_they_are_not_trusted() -> None:
    """两个 digest 都进 Release 正文，但正文本身必须被声明为**不可信**来源。"""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    data = _load_yaml(RELEASE_WORKFLOW)
    if data is None:
        assert "needs.updater-image.outputs.digest" in text
        assert "needs.image.outputs.digest" in text
        return
    jobs = data["jobs"]
    release = jobs["release"]
    assert "updater-image" in _needs(release)
    steps = release["steps"]
    assert len(steps) == 1
    env = steps[0]["env"]
    assert env["API_DIGEST"] == "${{ needs.image.outputs.digest }}"
    assert env["UPDATER_DIGEST"] == "${{ needs.updater-image.outputs.digest }}"
    run = steps[0]["run"]
    assert "$API_DIGEST" in run
    assert "$UPDATER_DIGEST" in run
    # 两个 digest 在创建 Release **之前**就要验格式：缺值/畸形一律不发。
    assert "sha256:[0-9a-f]{64}" in run
    # 正文可编辑 ≠ 可信：这句话必须在模板里（与设计 §3 一致）。
    assert "不是摘要的可信来源" in run
    # 用 --notes-file 一次创建（生成 notes 失败时不会留下"已发布但缺摘要"的窗口）。
    assert "--notes-file" in run
    assert "--generate-notes" not in run


def test_ci_has_a_named_catalog_validate_step() -> None:
    """`catalog.py validate` 是独立具名 step，不埋在别的多行 run 里。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    data = _load_yaml(CI_WORKFLOW)
    if data is None:
        assert "deploy/release/catalog.py validate" in text
        return
    steps = data["jobs"]["test"]["steps"]
    matches = [
        step for step in steps if "deploy/release/catalog.py validate" in str(step.get("run", ""))
    ]
    assert len(matches) == 1, "发布目录校验应当只有一条（且是独立门禁）"
    step = matches[0]
    # 必须有名字：job 列表/失败行要一眼看出是哪条门禁红了。
    assert "发布目录" in step.get("name", "")
    assert step["run"].strip() == "python deploy/release/catalog.py validate"
    static = [item for item in steps if item.get("name") == "静态检查"]
    assert static, "静态检查 step 不该被改名/合并"
    assert "catalog.py validate" not in static[0]["run"]


def test_release_workflow_pins_every_external_action_to_full_sha() -> None:
    """新增 job 不得引入 `@vN` 之类可移动引用；同一 action 只用同一个 SHA。"""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # 行尾允许有 `# v4` 之类的注释；只取 `uses:` 后面的第一个 token。
    refs = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.MULTILINE)
    external = [ref for ref in refs if not ref.startswith("./")]
    assert external
    for ref in external:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", ref), ref
    actions = {ref.split("@", 1)[0] for ref in external}
    for action in actions:
        shas = {ref.split("@", 1)[1] for ref in external if ref.startswith(f"{action}@")}
        assert len(shas) == 1, f"{action} 在 release.yml 里用了多个 SHA：{shas}"


def test_updater_dockerfile_copies_only_the_release_package() -> None:
    """镜像内容契约：只拷发行包代码、非 root、固定入口、只读根可跑。"""
    text = UPDATER_DOCKERFILE.read_text(encoding="utf-8")
    # 两段式且**精确版本** tag（不是 cli/latest、也不是 3-alpine）：
    #   - 阶段 1 `docker:29.9.3-cli` 只作 docker CLI 三件套的 COPY 来源；
    #   - 阶段 2 `python:3.13-alpine3.24` 才是运行时（Python 必须被 CI 覆盖，
    #     见 test_updater_image_python_is_the_ci_covered_version）。
    # 平台覆盖由 CI 的 platforms 决定（`COPY --from` 按目标平台解析）。
    froms = re.findall(r"^FROM (\S+)(?: AS (\w+))?$", text, re.MULTILINE)
    assert froms == [
        ("docker:29.9.3-cli", "dockercli"),
        ("python:3.13-alpine3.24", ""),
    ], froms
    allowed = {
        "deploy/updater/updater.py",
        "deploy/release/publish.py",
        "deploy/release/catalog.py",
        "deploy/backup/backup.sh",
        "deploy/backup/restore_drill.sh",
        "deploy/releases.json",
    }
    copied = {
        token
        for line in text.splitlines()
        if line.startswith("COPY ")
        for token in line.split()
        if token.startswith("deploy/")
    }
    assert copied == allowed, f"镜像里拷了不该拷/漏了该拷的文件：{copied ^ allowed}"
    # .env / 私钥 / socket / 整个仓库目录都不能被 COPY 进来。
    assert not re.search(
        r"^COPY .*(\.env|\.pem|\.key|\.crt|docker\.sock|/var/run)", text, re.MULTILINE
    )
    # 非 root + 固定入口；`CMD []` 清掉基础镜像继承的 `CMD ["sh"]`（否则 argparse
    # 会收到一个多余的 `sh` 参数）。
    assert "USER 10002:10002" in text
    assert re.search(r'^ENTRYPOINT \["python3", "/app/updater.py"\]$', text, re.MULTILINE)
    assert re.search(r"^CMD \[\]$", text, re.MULTILINE)
    # 只读根的两个前提：不写 .pyc、运行时才挂 socket。
    assert "PYTHONDONTWRITEBYTECODE=1" in text


def _dockerfile_code(text: str) -> str:
    """去掉注释行后的 Dockerfile 指令，避免断言命中说明文字里的示例。"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_updater_image_python_is_the_ci_covered_version() -> None:
    """更新器镜像的 Python 必须落在 **CI 覆盖的版本**里，且在构建期就断言。

    2026-11 的实跑缺陷：运行时基镜像是 `docker:29.9.3-cli`（alpine 3.24），
    `apk add python3` 给出 **3.14**，而 `ci.yml` 的 matrix 只跑 3.12/3.13。
    一个没被 CI 跑过的解释器就是"没验证过的代码"，而设计 §4 要的正是"更新器只
    保留最小可信代码"——所以钉到覆盖范围内的版本，并用构建期断言把这件事变成
    硬约束（基镜像 tag 被上游重推成 3.14 时，构建立刻红）。
    """
    text = UPDATER_DOCKERFILE.read_text(encoding="utf-8")
    code = _dockerfile_code(text)
    base = re.search(r"^FROM python:(\d+\.\d+)-alpine", text, re.MULTILINE)
    assert base, "运行时基镜像必须是精确的 python:X.Y-alpine（不是 latest/3-alpine）"
    data = _load_yaml(CI_WORKFLOW)
    if data is None:
        pytest.skip("需要 PyYAML 才能读 ci.yml 的 matrix")
    matrix = data["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    covered = {str(item) for item in matrix}
    assert base.group(1) in covered, (
        f"镜像 Python {base.group(1)} 不在 CI matrix {sorted(covered)} 里："
        "先用 CI 覆盖这个版本，再改基镜像"
    )
    # 构建期版本闸门（不是"能 import"的烟测）：越界就让构建失败。
    assert re.search(
        r"\(3, ?12\) ?<= ?sys\.version_info\[:2\] ?<= ?\(3, ?13\)", code
    ), "Dockerfile 缺少构建期 Python 版本区间断言 (3,12) <= sys.version_info[:2] <= (3,13)"
    # 镜像里只能有一个解释器：alpine 的 `apk add python3`（3.14）会落在 /usr/bin，
    # 而 PATH 里 /usr/local 优先——只看版本号仍会绿，所以显式禁止它。
    assert "test ! -e /usr/bin/python3" in code, "没有禁止第二个（未被 CI 覆盖的）Python"
    assert not re.search(r"\bapk add\b[^\n]*\bpython3\b", code), "不要再 apk add python3"


def test_updater_image_runtime_python_check_matches_ci_coverage() -> None:
    """运行期自检也必须只接受 CI 覆盖的版本。

    旧断言 `^Python 3\\.(1[2-9]|[2-9][0-9])` 把 3.14 当成合格，正是"CI 实跑抓不到
    版本漂移"的那一环：镜像里的 `catalog.py validate` 炸了，版本检查却是绿的。
    """
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # 只看指令行（注释里会引用旧模式，不能算数）。
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    runtime = [line for line in code_lines if "Python 3" in line]
    assert runtime, "release.yml 里找不到运行期 python 版本自检"
    assert any(r"^Python 3\.1[23]\." in line for line in runtime), (
        "运行期 python 版本自检没有限定在 3.12/3.13"
    )
    assert not any("1[2-9]|[2-9][0-9]" in line for line in runtime), "运行期自检仍放行 3.14+"


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
