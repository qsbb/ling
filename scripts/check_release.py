#!/usr/bin/env python3
"""发布前版本一致性检查（release.yml 的 verify job 用）。

只做静态解析 + git 祖先检查，**不导入、不执行仓库里的任何包**：

- pyproject.toml 用 tomllib 读 ``project.version``
- src/ling/__init__.py 用 ast 找 ``__version__`` 常量（不 exec）
- clients/web-console/package.json 用 json 读 ``version``
- tag 只接受稳定版 ``vX.Y.Z``（预发布/构建后缀一律拒绝）
- 可选：检查 tag 指向的提交是 main 的祖先（CI 里 checkout 必须 fetch-depth: 0）

用法::

    python scripts/check_release.py --tag v0.1.0
    python scripts/check_release.py --tag v0.1.0 --check-ancestor

``--tag`` 不传时依次读 ``RELEASE_TAG``、``GITHUB_REF_NAME``（只有 tag 推送
才有意义）。所有 tag 值只作为 argv 列表里的参数传给 git，不经过 shell，
也不拼进文件路径——避免 ``$(...)`` / 引号之类的注入。

退出码：0 通过；1 版本或祖先检查失败；2 用法/格式错误。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# 稳定版 tag：严格 v + 三段非负整数，不接受 0 前导（v01.2.3）、预发布、构建后缀。
STABLE_TAG = re.compile(r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$")


def fail(message: str, code: int = 1) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return code


def read_pyproject_version(path: Path) -> str:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法解析 {path}：{exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{path} 缺少 [project]")
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{path} 的 project.version 不是非空字符串")
    return version.strip()


def read_source_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError(f"无法解析 {path}：{exc}") from exc
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.append(value.value)
                    else:
                        raise ValueError(f"{path} 的 __version__ 不是字符串常量")
    if not found:
        raise ValueError(f"{path} 里找不到 __version__ 字符串常量")
    if len(set(found)) != 1:
        raise ValueError(f"{path} 里 __version__ 有多个不同取值：{found}")
    return found[0]


def read_package_version(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"无法解析 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 JSON 对象")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{path} 的 version 不是非空字符串")
    return version.strip()


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def check_ancestor(root: Path, tag: str, main_ref: str) -> str | None:
    """tag 指向的提交必须是 main 的祖先。不是则返回错误信息。"""
    tag_ref = f"refs/tags/{tag}^{{commit}}"
    resolved = run_git(root, "rev-parse", "--verify", tag_ref)
    if resolved.returncode != 0:
        return f"本地找不到 tag {tag}（CI 里请确认 checkout fetch-depth: 0）"
    tag_commit = resolved.stdout.strip()
    main = run_git(root, "rev-parse", "--verify", main_ref)
    if main.returncode != 0:
        return f"找不到主分支引用 {main_ref}（CI 里请确认已 fetch main）"
    main_commit = main.stdout.strip()
    ancestor = run_git(root, "merge-base", "--is-ancestor", tag_commit, main_commit)
    if ancestor.returncode == 0:
        return None
    if ancestor.returncode == 1:
        return (
            f"tag {tag} 指向的提交 {tag_commit} 不是 {main_ref} 的祖先；"
            "只能从已合并进 main 的提交打发布 tag"
        )
    detail = (ancestor.stderr or ancestor.stdout).strip()
    return f"git merge-base --is-ancestor 执行失败：{detail or ancestor.returncode}"


def parsed_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查发布 tag 与三处版本号是否一致")
    parser.add_argument("--tag", default="", help="发布 tag，例如 v0.1.0")
    parser.add_argument(
        "--root",
        default="",
        help="仓库根目录（默认脚本所在仓库）",
    )
    parser.add_argument(
        "--check-ancestor",
        action="store_true",
        help="同时要求 tag 指向的提交是主分支的祖先",
    )
    parser.add_argument(
        "--main-ref",
        default="origin/main",
        help="祖先检查用的主分支引用（默认 origin/main）",
    )
    return parser.parse_args(argv)



# 需要扫的源码目录：只扫 Python 源码，不扫测试（测试里的版本号是 fixture，
# 故意写死成某个值来验比较逻辑，改它没有意义）。
# **只扫这两个文件**，不扫整个源码树。原因：全树扫会把 `capabilities/tools.py`
# 里 `ToolSpec.version="1.0.0"` 这种**工具规格自身的版本号**当成应用版本误报。
# 那三处是每个工具独立的 ABI 版本，与应用版本无关，永远不该跟着 pyproject 走。
# 真正容易不同步的是"应用版本在别处又被硬编码一份"的地方，历史上就是这两个：
#   - app.py 的 FastAPI(version=...)  → 决定 /api/openapi.json 的 info.version
#   - logging_setup.py 的日志服务版本 → 决定启动日志里的 version 字段
_VERSION_SCAN_FILES = (
    "src/ling/app.py",
    "src/ling/logging_setup.py",
)
# 版本字面量形如 "1.2.3" / '1.2.3'；只认这种完整三段式，避免误伤
# 0.1 之类的浮点或注释里的半截版本。
_VERSION_LITERAL_RE = re.compile(r"""["'](\d+\.\d+\.\d+)["']""")


def find_stale_version_literals(root: Path, expected: str) -> list[tuple[str, int]]:
    """找出源码里残留的、与 expected 不同的三段式版本字面量。

    为什么需要它：`app.py` 的 FastAPI `version=` 和 `logging_setup.py` 的
    服务版本曾经各自写死一份字面量，`check_release.py` 只看 pyproject /
    `__init__.py` / `package.json` 三处，于是改版本时这两份会静默不同步——
    `/v1/version` 说一个数、`/api/openapi.json` 说另一个数。
    现在两处都改成引用 `__version__`，这个检查是**防止有人再写回去**。

    返回 [(相对路径, 行号)]，空列表表示干净。
    """
    hits: list[tuple[str, int]] = []
    for rel_path in _VERSION_SCAN_FILES:
        path = root / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _VERSION_LITERAL_RE.finditer(line):
                if match.group(1) != expected:
                    hits.append((str(path.relative_to(root)), lineno))
    return hits


def main(argv: list[str] | None = None) -> int:
    try:
        args = parsed_args(argv)
    except SystemExit as exc:  # argparse 的用法错误统一成返回码 2
        return int(exc.code) if isinstance(exc.code, int) else 2
    tag = (
        args.tag or os.environ.get("RELEASE_TAG") or os.environ.get("GITHUB_REF_NAME") or ""
    ).strip()
    if not tag:
        return fail("缺少 --tag（或 RELEASE_TAG / GITHUB_REF_NAME）", 2)
    if tag.startswith("-"):
        return fail("tag 不能以 - 开头", 2)
    match = STABLE_TAG.fullmatch(tag)
    if match is None:
        return fail(f"只接受稳定版 tag vX.Y.Z，收到：{tag!r}", 2)
    expected = match.group("version")

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    paths = {
        "pyproject.toml": root / "pyproject.toml",
        "src/ling/__init__.py": root / "src" / "ling" / "__init__.py",
        "clients/web-console/package.json": root / "clients" / "web-console" / "package.json",
    }
    try:
        versions = {
            "pyproject.toml": read_pyproject_version(paths["pyproject.toml"]),
            "src/ling/__init__.py": read_source_version(paths["src/ling/__init__.py"]),
            "clients/web-console/package.json": read_package_version(
                paths["clients/web-console/package.json"]
            ),
        }
    except ValueError as exc:
        return fail(str(exc))

    # 另外两处是**同一版本号的硬编码副本**：FastAPI 的 `version=` 与日志里的
    # 服务版本。它们不在 tag 一致性检查的历史范围内（历史上是写死的字面量），
    # 一旦与 pyproject 不同步，`/v1/version` 与 `/api/openapi.json` 会报两个
    # 不同的版本号，而 CI 的 tag 检查**发现不了**。所以这里也扫一遍源码：
    # 只要某文件里还残留与 expected 不同的旧版本字面量，就判失败。
    stale = find_stale_version_literals(root, expected)
    if stale:
        detail = "；".join(f"{path}:{line}" for path, line in stale)
        return fail(f"源码里还有与 {expected} 不一致的版本字面量：{detail}")

    mismatched = {name: value for name, value in versions.items() if value != expected}
    if mismatched:
        detail = "；".join(f"{name}={value}" for name, value in sorted(versions.items()))
        return fail(f"tag {tag}（应为 {expected}）与版本号不一致：{detail}")

    if args.check_ancestor:
        problem = check_ancestor(root, tag, args.main_ref)
        if problem is not None:
            return fail(problem)

    for name, value in versions.items():
        print(f"version {name} = {value}")
    print(f"tag {tag} 与 pyproject.toml / src/ling/__init__.py / package.json 一致")
    if args.check_ancestor:
        print(f"tag {tag} 指向的提交是 {args.main_ref} 的祖先")
    return 0


if __name__ == "__main__":
    sys.exit(main())
