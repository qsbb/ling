"""``deploy/release/catalog.py`` 的回归测试（纯标准库，不碰网络/Docker）。

保护的是「可信发布目录」的硬约束，而不是实现细节：

- 目录文件（``deploy/releases.json``）是**信任锚**：校验必须严格到"多一个字段、
  少一个字段、类型不对、格式不对、重复 version"全部拒绝，且绝不"顺手修一下"。
  特别是**旧目录里的 ``digest`` 字段必须被拒绝**（``release_field_unknown``）：
  目录已经不负责目标摘要，静默忽略会留下"到底有没有 digest 在生效"的两种事实；
- ``add`` 必须**幂等**：同 version 且 commit/min_from/has_migration 全同重跑不改
  文件；同 version 但字段不同 → ``version_conflict``（已发布的条目只能人工评审后
  改）。同版本**重新打包镜像**（digest 变了）不再是冲突——目录里没有 digest 了；
- ``validate`` **不看顺序**：排错序的目录文件仍然合法；排序只是使用方的事，
  ``add`` 写出时按 version 排序只为让 diff 稳定。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PY = REPO_ROOT / "deploy" / "release" / "catalog.py"
REPO_CATALOG = REPO_ROOT / "deploy" / "releases.json"

# 旧目录里那条 digest（0.0.3 的 index digest）：只用来证明它现在**非法**。
LEGACY_DIGEST = "ghcr.io/qsbb/ling-api@sha256:" + "a" * 64
COMMIT = "1" * 40
COMMIT_B = "2" * 40
RELEASED_AT = "2026-11-04T00:00:00Z"


def _catalog_module() -> Any:
    spec = importlib.util.spec_from_file_location("ling_release_catalog_under_test", CATALOG_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


catalog = _catalog_module()


def release(
    version: str = "0.0.2",
    *,
    commit: str = COMMIT,
    min_from: list[str] | None = None,
    has_migration: bool = True,
    released_at: str = RELEASED_AT,
) -> dict[str, Any]:
    return {
        "version": version,
        "commit": commit,
        "min_from": ["0.0.1"] if min_from is None else min_from,
        "has_migration": has_migration,
        "released_at": released_at,
    }



def write(path: Path, payload: object, *, mode: int = 0o644) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(mode)
    return path


def good(*releases: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "releases": list(releases) or [release()]}


def error_code(payload: object) -> str:
    with pytest.raises(catalog.CatalogError) as info:
        catalog.parse_catalog(json.dumps(payload).encode("utf-8"))
    return info.value.code


# ---------------------------------------------------------------- 仓库里的目录


def test_repository_catalog_is_valid_and_lists_real_releases() -> None:
    """仓库里的目录文件必须能过门禁，且只列**真实发布过**的版本。

    这条原来断言"条目必须为空"——那是 0.0.2 时代的事实（当时还没有任何
    镜像模式正式发布，空目录是诚实状态）。v0.0.3 发布后事实变了：目录里
    该有 0.0.3 这一条，否则部署机上的网页更新永远显示 no_available_target。

    所以断言改成"非空且每条都自洽"，而不是继续钉死"必须为空"：
    钉死空列表会让"发布后填目录"这件正确的事变成 CI 红灯，
    而那正是这次 0.0.3 发布要解决的问题。
    """
    parsed = catalog.load_catalog(REPO_CATALOG)
    assert parsed["schema"] == 1
    releases = parsed["releases"]
    assert releases, "仓库目录里至少要有一条真实发布记录（否则网页更新没有可用目标）"
    for release in releases:
        # 目录只负责**发布决策**（版本/源提交/跨度/迁移），不再负责目标摘要：
        # 摘要由更新器刷新时从 registry 解析。所以这里断言的字段集合必须是
        # catalog.py 的 RELEASE_KEYS，多一个（尤其是 `digest`）都会被 parse 拒绝。
        assert set(release) == set(catalog.RELEASE_KEYS)
        assert "digest" not in release
        assert len(release["commit"]) == 40
        assert release["min_from"]
    # 权限位仍然不许组/世界可写：目录被改等于更新目标被改。
    assert stat.S_IMODE(REPO_CATALOG.stat().st_mode) & 0o022 == 0


def test_repository_catalog_is_only_group_or_world_non_writable() -> None:
    assert stat.S_IMODE(REPO_CATALOG.stat().st_mode) == 0o644


# ---------------------------------------------------------------- 合法输入


def test_valid_catalog_roundtrips_and_normalizes() -> None:
    parsed = catalog.parse_catalog(json.dumps(good()).encode("utf-8"))
    assert parsed == {"schema": 1, "releases": [release()]}
    # 渲染 → 再解析，语义不变（写出的文件必须还能通过校验）。
    again = catalog.parse_catalog(catalog.render_catalog(parsed).encode("utf-8"))
    assert again == parsed
    assert catalog.render_catalog(parsed).endswith("\n")


def test_multiple_releases_are_accepted_out_of_order() -> None:
    """`validate` **不按发布时间/版本排序**：排错序的文件仍然合法。"""
    payload = good(
        release("0.0.10", commit=COMMIT_B, min_from=["0.0.9"]),
        release("0.0.2", commit=COMMIT, min_from=["0.0.1"]),
    )
    parsed = catalog.parse_catalog(json.dumps(payload).encode("utf-8"))
    assert [item["version"] for item in parsed["releases"]] == ["0.0.10", "0.0.2"]


def test_sort_releases_uses_semver_not_lexicographic_or_file_order() -> None:
    entries = [
        release("0.0.10", commit=COMMIT_B, min_from=["0.0.9"]),
        release("0.0.9", commit=COMMIT_B, min_from=["0.0.1"]),
        release("0.0.2", commit=COMMIT, min_from=["0.0.1"]),
    ]
    assert [item["version"] for item in catalog.sort_releases(entries)] == [
        "0.0.2",
        "0.0.9",
        "0.0.10",
    ]


def test_prerelease_sorts_before_release_of_same_core() -> None:
    assert catalog.version_key("1.0.0-rc.1") < catalog.version_key("1.0.0")
    assert catalog.version_key("1.0.0-alpha") < catalog.version_key("1.0.0-beta")
    assert catalog.version_key("1.0.0-beta.2") < catalog.version_key("1.0.0-beta.10")


# ---------------------------------------------------------------- 非法输入


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "not_object"),
        ({"schema": 1}, "root_field_missing"),
        ({"releases": []}, "root_field_missing"),
        ({"schema": 1, "releases": [], "extra": 1}, "root_field_unknown"),
        ({"schema": 2, "releases": []}, "schema_unsupported"),
        ({"schema": True, "releases": []}, "schema_invalid"),
        ({"schema": "1", "releases": []}, "schema_invalid"),
        ({"schema": 1, "releases": {}}, "releases_not_list"),
        ({"schema": 1, "releases": ["x"]}, "release_not_object"),
        ({"schema": 1, "releases": [{"version": "0.0.2"}]}, "release_field_missing"),
        (
            {"schema": 1, "releases": [{**release(), "extra": "x"}]},
            "release_field_unknown",
        ),
        (good(release("0.0")), "version_invalid"),
        (good(release("v0.0.2")), "version_invalid"),
        (good(release("0.00.2")), "version_invalid"),
        (good(release("latest")), "version_invalid"),
        (good(release(commit="1" * 39)), "commit_invalid"),
        (good(release(commit="A" * 40)), "commit_invalid"),
        (good(release(min_from=[])), "min_from_empty"),
        (good(release(min_from=["0.0.1", "0.0.1"])), "min_from_duplicate"),
        (good(release(min_from=["0.0.2"])), "min_from_self"),
        (good(release(min_from=["v0.0.1"])), "min_from_invalid"),
        (good(release(min_from="0.0.1")), "min_from_invalid"),
        (good(release(has_migration="true")), "has_migration_invalid"),
        (good(release(has_migration=1)), "has_migration_invalid"),
        (good(release(released_at="2026-11-04 00:00:00")), "released_at_invalid"),
        (good(release(released_at="2026-11-04T00:00:00+08:00")), "released_at_invalid"),
        (
            good(
                release("0.0.2"),
                release("0.0.2", commit=COMMIT_B),
            ),
            "version_duplicate",
        ),
        # 旧目录（带 digest）必须被**显式拒绝**，不能读的时候顺手忽略：
        # 否则"目录里到底还有没有 digest 在生效"又会变成两种事实。
        (
            good({**release("0.0.2"), "digest": LEGACY_DIGEST}),
            "release_field_unknown",
        ),
    ],
)
def test_invalid_catalogs_are_rejected(payload: object, code: str) -> None:
    assert error_code(payload) == code


def test_non_json_and_non_utf8_are_rejected() -> None:
    with pytest.raises(catalog.CatalogError) as info:
        catalog.parse_catalog(b"{")
    assert info.value.code == "not_json"
    with pytest.raises(catalog.CatalogError) as info:
        catalog.parse_catalog(b"\xff\xfe")
    assert info.value.code == "not_utf8"


def test_oversized_catalog_is_rejected() -> None:
    raw = b" " * (catalog.MAX_CATALOG_BYTES + 1)
    with pytest.raises(catalog.CatalogError) as info:
        catalog.parse_catalog(raw)
    assert info.value.code == "too_large"


def test_too_many_releases_is_rejected() -> None:
    releases = [
        release(
            f"0.0.{index + 2}",
            commit=f"{index + 2:040x}",
        )
        for index in range(catalog.MAX_RELEASES + 1)
    ]
    payload = {"schema": 1, "releases": releases}
    with pytest.raises(catalog.CatalogError) as info:
        catalog.parse_catalog(json.dumps(payload).encode("utf-8"))
    assert info.value.code == "too_many_releases"


# ---------------------------------------------------------------- 文件层


def test_missing_file_reports_not_found(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError) as info:
        catalog.load_catalog(tmp_path / "releases.json")
    assert info.value.code == "not_found"


def test_symlink_is_not_accepted(tmp_path: Path) -> None:
    real = write(tmp_path / "real.json", good())
    link = tmp_path / "releases.json"
    link.symlink_to(real)
    with pytest.raises(catalog.CatalogError) as info:
        catalog.load_catalog(link)
    assert info.value.code == "not_a_file"


def test_world_writable_file_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path / "releases.json", good(), mode=0o666)
    with pytest.raises(catalog.CatalogError) as info:
        catalog.load_catalog(path)
    assert info.value.code == "unsafe_permissions"


def test_directory_instead_of_file_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "releases.json"
    directory.mkdir()
    with pytest.raises(catalog.CatalogError) as info:
        catalog.load_catalog(directory)
    assert info.value.code == "not_a_file"


# ---------------------------------------------------------------- add


def test_add_appends_and_writes_sorted_file(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, good(release("0.0.2")))
    merged, state = catalog.add_release(
        path,
        version="0.0.10",
        commit=COMMIT_B,
        min_from=["0.0.2"],
        has_migration=False,
        released_at=RELEASED_AT,
    )
    assert state == "added"
    assert [item["version"] for item in merged["releases"]] == ["0.0.2", "0.0.10"]
    on_disk = catalog.load_catalog(path)
    assert [item["version"] for item in on_disk["releases"]] == ["0.0.2", "0.0.10"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert on_disk["releases"][1]["has_migration"] is False


def test_add_is_idempotent_and_does_not_touch_the_file(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, good(release("0.0.2")))
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    merged, state = catalog.add_release(
        path,
        version="0.0.2",
        commit=COMMIT,
        min_from=["0.0.1"],
        has_migration=True,
        released_at="2030-01-01T00:00:00Z",
    )
    assert state == "unchanged"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert merged["releases"] == [release()]


def test_add_same_version_with_different_fields_fails(tmp_path: Path) -> None:
    """同 version 但 commit/min_from/has_migration 不同 → version_conflict，不改文件。

    已发布的条目是信任锚：换源提交/换跨度必须人工评审后改目录，不能一条 `add`
    就静默改写。（同版本**重新打包镜像**不是冲突：目录里已经没有 digest 了。）
    """
    path = tmp_path / "releases.json"
    write(path, good(release("0.0.2")))
    before = path.read_bytes()
    for overrides in (
        {"commit": COMMIT_B},
        {"min_from": ["0.0.0"]},
        {"has_migration": False},
    ):
        values = {
            "version": "0.0.2",
            "commit": COMMIT,
            "min_from": ["0.0.1"],
            "has_migration": True,
            **overrides,
        }
        with pytest.raises(catalog.CatalogError) as info:
            catalog.add_release(path, **values)
        assert info.value.code == "version_conflict", overrides
        assert path.read_bytes() == before


def test_add_allows_another_version_with_the_same_commit(tmp_path: Path) -> None:
    """不同版本共用同一个源提交是合法的（重新打包/纯元数据发版都会这样）。

    旧的 ``digest_duplicate`` 规则随 digest 一起去掉了：目录不再记录摘要，
    「同一个提交产出两个版本条目」不该被工具挡住。
    """
    path = tmp_path / "releases.json"
    write(path, good(release("0.0.2")))
    merged, state = catalog.add_release(
        path,
        version="0.0.3",
        commit=COMMIT,
        min_from=["0.0.2"],
        has_migration=True,
    )
    assert state == "added"
    assert [item["version"] for item in merged["releases"]] == ["0.0.2", "0.0.3"]


@pytest.mark.parametrize(
    ("version", "commit", "min_from", "code"),
    [
        ("0.0", COMMIT_B, ["0.0.2"], "version_invalid"),
        ("0.0.3", "z" * 40, ["0.0.2"], "commit_invalid"),
        ("0.0.3", COMMIT_B, [], "min_from_empty"),
        ("0.0.3", COMMIT_B, ["0.0.3"], "min_from_self"),
    ],
)
def test_add_validates_the_new_entry(
    tmp_path: Path,
    version: str,
    commit: str,
    min_from: list[str],
    code: str,
) -> None:
    path = tmp_path / "releases.json"
    write(path, good(release("0.0.2")))
    before = path.read_bytes()
    with pytest.raises(catalog.CatalogError) as info:
        catalog.add_release(
            path,
            version=version,
            commit=commit,
            min_from=min_from,
            has_migration=True,
        )
    assert info.value.code == code
    assert path.read_bytes() == before


def test_add_creates_the_file_when_the_directory_exists(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    merged, state = catalog.add_release(
        path,
        version="0.0.2",
        commit=COMMIT,
        min_from=["0.0.1"],
        has_migration=True,
        released_at=RELEASED_AT,
    )
    assert state == "added"
    assert catalog.load_catalog(path) == merged
    assert path.read_text(encoding="utf-8").startswith("{\n  \"schema\": 1,")
    # 写出来的文件里**没有** digest：目录只放发布决策。
    assert "digest" not in path.read_text(encoding="utf-8")


def test_add_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError) as info:
        catalog.add_release(
            tmp_path / "nope" / "releases.json",
            version="0.0.2",
            commit=COMMIT,
            min_from=["0.0.1"],
            has_migration=True,
        )
    assert info.value.code == "not_found"


def test_add_refuses_when_existing_catalog_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    path.write_text('{"schema": 1, "releases": [{}]}', encoding="utf-8")
    with pytest.raises(catalog.CatalogError) as info:
        catalog.add_release(
            path,
            version="0.0.2",
            commit=COMMIT,
            min_from=["0.0.1"],
            has_migration=True,
        )
    assert info.value.code == "release_field_missing"


# ---------------------------------------------------------------- CLI


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CATALOG_PY), *args],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=120,
    )


def test_cli_validate_accepts_repository_catalog() -> None:
    result = run_cli("validate")
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 条数不写死：发布一次就多一条，写死数字会让每次发版都要改测试。
    assert "OK schema=1 releases=" in result.stdout


def test_cli_validate_rejects_broken_catalog(tmp_path: Path) -> None:
    path = write(tmp_path / "releases.json", {"schema": 1, "releases": [{"version": "0.0.2"}]})
    result = run_cli("validate", "--catalog", str(path))
    assert result.returncode == 2
    assert "release_field_missing" in result.stderr
    assert result.stdout == ""


def test_cli_add_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, {"schema": 1, "releases": []})
    args = (
        "add",
        "--catalog",
        str(path),
        "--version",
        "0.0.2",
        "--commit",
        COMMIT,
        "--min-from",
        "0.0.1",
        "--has-migration",
        "--released-at",
        RELEASED_AT,
    )
    first = run_cli(*args)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert "ADDED 0.0.2" in first.stdout
    before = path.read_bytes()
    second = run_cli(*args)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "UNCHANGED 0.0.2" in second.stdout
    assert path.read_bytes() == before


def test_cli_add_accepts_comma_separated_min_from(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, {"schema": 1, "releases": []})
    result = run_cli(
        "add",
        "--catalog",
        str(path),
        "--version",
        "0.0.3",
        "--commit",
        COMMIT,
        "--min-from",
        "0.0.1,0.0.2",
        "--no-migration",
        "--released-at",
        RELEASED_AT,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = catalog.load_catalog(path)
    assert parsed["releases"][0]["min_from"] == ["0.0.1", "0.0.2"]
    assert parsed["releases"][0]["has_migration"] is False


def test_cli_add_requires_a_migration_choice(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, {"schema": 1, "releases": []})
    result = run_cli(
        "add",
        "--catalog",
        str(path),
        "--version",
        "0.0.2",
        "--commit",
        COMMIT,
        "--min-from",
        "0.0.1",
    )
    assert result.returncode != 0
    assert "--has-migration" in result.stderr or "--has-migration" in result.stdout


def test_cli_reports_conflict_with_exit_code_two(tmp_path: Path) -> None:
    """同版本换源提交 → version_conflict + 退出码 2（不静默改写已发布条目）。"""
    path = write(tmp_path / "releases.json", good(release("0.0.2")))
    result = run_cli(
        "add",
        "--catalog",
        str(path),
        "--version",
        "0.0.2",
        "--commit",
        COMMIT_B,
        "--min-from",
        "0.0.1",
        "--has-migration",
    )
    assert result.returncode == 2
    assert "version_conflict" in result.stderr


def test_cli_written_file_is_valid_and_world_readable_only(tmp_path: Path) -> None:
    path = tmp_path / "releases.json"
    write(path, {"schema": 1, "releases": []})
    result = run_cli(
        "add",
        "--catalog",
        str(path),
        "--version",
        "0.0.2",
        "--commit",
        COMMIT,
        "--min-from",
        "0.0.1",
        "--has-migration",
        "--released-at",
        RELEASED_AT,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o644
    assert catalog.load_catalog(path)["releases"][0]["released_at"] == RELEASED_AT


# ---------------------------------------------------------------- CLI 帮助文本
#
# 背景（2026-11 CI 的 updater-image job 实跑失败）：`--released-at` 的 help 曾经是
#   f"UTC 发布时间 {RELEASED_AT_FORMAT}Z（默认现在）"
# 展开成字面量 "UTC 发布时间 %Y-%m-%dT%H:%M:%SZZ（默认现在）"。argparse 会把
# help/description 当 `%` 模板格式化（`HelpFormatter._expand_help` 里的
# `help % params`）：
#   - Python 3.12/3.13：`catalog.py add --help` 抛
#     `ValueError: unsupported format character 'Y'`——但 `validate` 还能跑，所以
#     本机 3.13 的日常测试一直是绿的；
#   - Python 3.14 起：argparse 把这个校验提前到**构造 parser 时**，于是连
#     `catalog.py validate`（CI 门禁那一步）都直接炸。
# 本机 venv 是 3.13，看不到 3.14 的校验时机，所以下面**不**去模拟 3.14，而是直接
# 断言"交给 argparse 的字符串里没有 `%`"：坏字符串在任何版本上都是同一个坏字符串，
# 这条断言在 3.13 上同样会红。另外 `format_help()` 走的是真实的 `%` 格式化路径
# （3.12/3.13 就会抛），所以"扫描 + 真渲染"两条都留着，不依赖解释器版本。


def _subparsers(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser]]:
    """`add_subparsers()` 挂上的子 parser（按注册顺序）。"""
    found: list[tuple[str, argparse.ArgumentParser]] = []
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        for name, child in choices.items():
            if isinstance(child, argparse.ArgumentParser):
                found.append((name, child))
    return found


_DISPLAY_ATTRS = ("description", "epilog", "usage", "help", "metavar")


def _displayed_strings(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """递归收集 argparse 会展示（并做 `%` 格式化）的全部字符串。

    覆盖 parser 的 ``description`` / ``epilog`` / ``usage``、每个 action 的
    ``help`` / ``metavar`` / ``choices``，以及父级 ``_choices_actions`` 上挂的
    子命令 help（它不在 ``_actions`` 里，只有写 ``add_parser(..., help=...)``
    时才出现）。
    """
    found: list[tuple[str, str]] = []

    def add(where: str, value: object) -> None:
        values = value if isinstance(value, list | tuple) else [value]
        for text in values:
            if isinstance(text, str) and text:
                found.append((where, text))

    def walk(node: argparse.ArgumentParser, name: str) -> None:
        for attr in ("description", "epilog", "usage"):
            add(f"{name}.{attr}", getattr(node, attr, None))
        for action in node._actions:
            label = f"{name}.{getattr(action, 'dest', '?')}"
            for attr in _DISPLAY_ATTRS:
                add(f"{label}.{attr}", getattr(action, attr, None))
            choices = getattr(action, "choices", None)
            if isinstance(choices, list | tuple):
                add(f"{label}.choices", choices)
            for pseudo in getattr(action, "_choices_actions", ()):
                add(f"{label}.subcommand_help", getattr(pseudo, "help", None))
        for child_name, child in _subparsers(node):
            walk(child, f"{name} {child_name}")

    walk(parser, "catalog.py")
    return found


def test_no_argparse_display_string_contains_a_percent_sign() -> None:
    """parser 里任何会被展示/格式化的字符串都不许出现 `%`（含"看起来像格式符"的）。

    这是本次缺陷的**版本无关**护栏：`%` 一旦出现，3.13 的 `--help` 与 3.14 的
    任何子命令都会炸；反过来，字符串里没有 `%`，哪个版本都安全。
    """
    strings = _displayed_strings(catalog.build_parser())
    # 防"扫描为空"的假绿：build_parser 至少有一个 prog/description、两个子命令、
    # 十来个参数，收集到的字符串远多于 12 条。
    assert len(strings) >= 12, strings
    offenders = [f"{where} = {text!r}" for where, text in strings if "%" in text]
    assert offenders == [], (
        "argparse 会把 help/description 当 % 模板格式化（3.14 起连构造 parser 都校验），"
        "这些字符串里不能出现 `%`：" + "；".join(offenders)
    )


def test_released_at_help_documents_the_format_literally() -> None:
    """`--released-at` 的格式必须**字面**写出来，不能插值 strftime 的 `%` 模板。"""
    add_parser = dict(_subparsers(catalog.build_parser()))["add"]
    help_text = next(
        str(action.help) for action in add_parser._actions if action.dest == "released_at"
    )
    assert "%" not in help_text
    assert "YYYY-MM-DDTHH:MM:SSZ" in help_text
    # 文档里的字面格式必须与真正用于 `datetime.strptime`/`strftime` 的模板同值
    # （旧的 `...{RELEASED_AT_FORMAT}Z` 还多拼了一个 Z，会显示成 `...SSZZ`）。
    documented = catalog.RELEASED_AT_FORMAT
    for code, word in (
        ("%Y", "YYYY"),
        ("%m", "MM"),
        ("%d", "DD"),
        ("%H", "HH"),
        ("%M", "MM"),
        ("%S", "SS"),
    ):
        documented = documented.replace(code, word)
    assert documented == "YYYY-MM-DDTHH:MM:SSZ"


def test_parser_parses_and_every_help_renders() -> None:
    """`parse_args` 与每个 `format_help()` 都要能跑。

    3.14 在构造 parser 时就校验 help，本机（3.13）构造得了、但 `format_help()`
    会抛——所以这条用例在本机也能抓住同类回归，而不是只在 3.14 上才红。
    """
    parser = catalog.build_parser()
    assert parser.parse_args(["validate"]).command == "validate"
    assert "usage:" in parser.format_help()
    for name, child in _subparsers(parser):
        assert "usage:" in child.format_help(), name
    add_help = dict(_subparsers(parser))["add"].format_help()
    assert "--released-at" in add_help
    assert "%Y" not in add_help


@pytest.mark.parametrize("argv", [("--help",), ("validate", "--help"), ("add", "--help")])
def test_cli_help_exits_zero(argv: tuple[str, ...]) -> None:
    """真实进程里的 `--help`：坏 help 会以非 0 退出（3.12/3.13 是 ValueError）。"""
    result = run_cli(*argv)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout.startswith("usage:"), result.stdout[:200]
