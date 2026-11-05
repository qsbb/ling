"""设置分类法闸门：新增设置项必须先在分类法里找到归属。

为什么需要：分类法最容易的死法不是写错，是**写完就没人再看**。
某天有人加了一个 ``ModelRole``，顺手绑进设置页，文档里没有它——
不报错，只是文档慢慢变成假话。等到发现时，已经有二十项设置在文档外游荡。

权威只有一份：``ling.settings.taxonomy.CATEGORIES``。文档与接口都由它派生。
本文件检查三件事：

1. 文档树的类名与顺序必须与代码一致（否则文档在撒谎）
2. 每个 ``editable_route`` / ``status_route`` 必须真的存在于 FastAPI 路由表
   （否则分类法声称自己有设置面，而实际没实现）
3. 声明 ``available`` 的类必须有 ``editable_route``（不允许空壳入口）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ling.contracts.v1 import ModelRole
from ling.settings.taxonomy import CATEGORIES, editable_categories, summary

DOC = Path(__file__).resolve().parents[2] / "docs" / "SETTINGS-TAXONOMY.md"

CIRCLED = "①②③④⑤⑥⑦⑧⑨"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.exists(), f"分类法文档缺失：{DOC}"
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def openapi_paths() -> set[str]:
    """真实路由表。用来验证分类法没有声称不存在的入口。"""
    from ling.app import create_app

    return set(create_app().openapi().get("paths", {}))


# ---------------------------------------------------------------- 文档 ↔ 代码


def test_doc_top_level_matches_code(doc_text: str) -> None:
    """文档树里的一级类必须与 CATEGORIES 完全一致，顺序也一致。

    序号顺序本身是归属判定序列的视觉提示，错序会让人按错的顺序归类。
    """
    pattern = re.compile(r"[├└]─[①②③④⑤⑥⑦⑧⑨]\s*(\S+?)\s*─+")
    found = pattern.findall(doc_text)
    expected = [c.label for c in CATEGORIES]
    assert len(found) == len(expected), (
        f"文档树里找到 {len(found)} 个一级类，期望 {len(expected)}：{found}"
    )
    assert found == expected, f"一级类名称或顺序不一致：\n  文档：{found}\n  代码：{expected}"


def test_doc_verbs_match_code(doc_text: str) -> None:
    """每个一级类后面那句「改『…』」必须与代码一致。

    这句话是用户唯一会记住的东西，漂了比类名漂更隐蔽。
    """
    pattern = re.compile(r"[├└]─[①②③④⑤⑥⑦⑧⑨]\s*(\S+?)\s*─+\s*(改「[^」]+」)")
    pairs = {label: verb for label, verb in pattern.findall(doc_text)}
    for c in CATEGORIES:
        assert c.label in pairs, f"文档树缺少一级类「{c.label}」"
        assert pairs[c.label] == c.verb, (
            f"「{c.label}」的动词不一致：文档 {pairs[c.label]}，代码 {c.verb}"
        )


def test_every_model_role_is_documented(doc_text: str) -> None:
    """新增 ModelRole 必须同步进分类法，否则 ③ 职责绑定的清单就是假话。"""
    missing = [role.value for role in ModelRole if role.value not in doc_text]
    assert not missing, (
        "以下 ModelRole 未出现在 docs/SETTINGS-TAXONOMY.md §6："
        f"{missing}。请先把它们写入分类法，再接入设置面。"
    )


def test_no_empty_top_level_category(doc_text: str) -> None:
    """每个一级类下面至少有一个二级分组，不留空壳。"""
    lines = doc_text.splitlines()
    marks = [
        i for i, line in enumerate(lines) if re.search(r"[├└]─[①②③④⑤⑥⑦⑧⑨]", line)
    ]
    assert marks, "文档树未找到任何一级类"

    for idx, start in enumerate(marks):
        end = marks[idx + 1] if idx + 1 < len(marks) else len(lines)
        body = lines[start + 1 : end]
        # 最后一个一级类用 └─ 收尾，其子项是纯空格缩进而非 "│"，两种画法都合法
        subgroups = [
            line
            for line in body
            if re.match(r"^\s*[│\s]", line)
            and re.search(r"[├└]─", line)
            and not re.search(r"[①②③④⑤⑥⑦⑧⑨]", line)
        ]
        name = re.search(r"[①-⑨]\s*(\S+)", lines[start])
        label = name.group(1) if name else f"#{idx + 1}"
        assert subgroups, f"一级类「{label}」没有任何二级分组"


# ---------------------------------------------------------------- 代码 ↔ 运行时


def test_editable_route_exists_in_app(openapi_paths: set[str]) -> None:
    """分类法说这类可编辑，就必须真有那个端点。

    这条是诚实性锚：防止有人把 ``editable_route`` 填上却没有实现，
    或者路由改名后忘了同步——两种情况都会让前端拿到 404。
    """
    broken: list[str] = []
    for c in CATEGORIES:
        if c.editable_route is not None and c.editable_route not in openapi_paths:
            broken.append(f"{c.label} → {c.editable_route}")
    assert not broken, f"以下 editable_route 在真实路由表里不存在：{broken}"


def test_status_route_exists_in_app(openapi_paths: set[str]) -> None:
    broken: list[str] = []
    for c in CATEGORIES:
        if c.status_route is not None and c.status_route not in openapi_paths:
            broken.append(f"{c.label} → {c.status_route}")
    assert not broken, f"以下 status_route 在真实路由表里不存在：{broken}"


def test_taxonomy_endpoint_is_registered(openapi_paths: set[str]) -> None:
    """分类法接口本身必须可用，否则前端无从渲染导航。"""
    assert "/v1/settings/taxonomy" in openapi_paths


def test_available_implies_editable_route() -> None:
    """不允许"声称可用但没有编辑入口"的空壳分类。"""
    shells = [c.label for c in CATEGORIES if c.available and c.editable_route is None]
    assert not shells, f"以下类声称可用但没有 editable_route：{shells}"


def test_no_duplicate_keys_or_indexes() -> None:
    keys = [c.key for c in CATEGORIES]
    indexes = [c.index for c in CATEGORIES]
    assert len(set(keys)) == len(keys), f"一级类 key 重复：{keys}"
    assert len(set(indexes)) == len(indexes), f"一级类序号重复：{indexes}"
    assert indexes == list(range(1, len(CATEGORIES) + 1)), "序号必须从 1 连续递增"


def test_every_category_has_groups() -> None:
    for c in CATEGORIES:
        assert c.groups, f"一级类「{c.label}」没有二级分组"


def test_summary_is_consistent() -> None:
    s = summary()
    assert s["total"] == len(CATEGORIES)
    assert s["editable"] == len(editable_categories())
    assert s["editable"] + s["pending"] == s["total"]


def test_editable_count_is_exactly_six_today() -> None:
    """当前有 ①身份与人格、③模型与生成、④记忆与学习、⑤对话与主动性、
    ⑦权限与隐私、⑧生活与活动 有设置面。

    这个数字变化时应当**主动**更新本测试，而不是让它悄悄跟着变——
    每次增加都意味着一个新的设置面真的落地了。
    """
    labels = sorted(c.label for c in editable_categories())
    assert labels == [
        "对话与主动性",
        "权限与隐私",
        "模型与生成",
        "生活与活动",
        "记忆与学习",
        "身份与人格",
    ], (
        f"可编辑分类集合变了：{labels}。"
        "如果这是有意的实现进展，请同时更新本测试与文档 §7。"
    )
