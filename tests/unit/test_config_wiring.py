"""配置接线闸：加了配置项就必须有人读它。

## 为什么需要这个文件

这个项目反复出现同一类 bug：

1. `chat.py` 读 `app.state.config_service`——**该属性不存在**，群聊模型从未参与
2. `split_semantic` 只被用来算 SSE 里的 `chunks: N`——分段从未发生
3. 轮询器挂在 300 秒的定时块里——"聚焦 8 秒"从未发生

共同点：**组件都在、接线错了，而且测试全绿、日志无异常。**

前两条没法用静态检查抓，但"加了一个配置项却没人读"可以——
那就是本文件。

## 它检查什么

每个策略字段（或由它派生的 property）必须在**存储/回显层之外**
至少被引用一次。`ports/config.py`、`settings/service.py`、
`api/routes/settings.py` 只负责存取和显示，不算"有人用它做判断"。
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import re

import pytest

from ling.ports.config import DialoguePolicy, LifePolicy, MemoryPolicy

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"

# 只存不判的地方：字段在这里出现不算"被使用"
STORAGE_ONLY = {
    "ports/config.py",
    "settings/service.py",
    "api/routes/settings.py",
}

POLICIES = [DialoguePolicy, MemoryPolicy, LifePolicy]


def _derived_names(cls: type, field: str) -> list[str]:
    """字段名 + 由它派生的 property 名。

    注意：**必须包含派生属性**。`quiet_start_minute` 自己没被任何人读，
    但它的 property `quiet_start` 被 ProactiveService 读了——
    只 grep 字段名会把正常的接线误判成孤儿（我自己就误判过一次）。
    """
    names = [field]
    for name, member in inspect.getmembers(cls):
        if isinstance(member, property) and member.fget is not None:
            try:
                body = inspect.getsource(member.fget)
            except OSError:  # pragma: no cover - 交互式定义时取不到源码
                continue
            if re.search(rf"\b{re.escape(field)}\b", body):
                names.append(name)
    return names


def _references(names: list[str]) -> list[str]:
    """在存储层之外找引用。返回命中的位置，便于失败时定位。"""
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in STORAGE_ONLY:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            hits.append(f"{rel}:{line_no}")
    return hits


@pytest.mark.parametrize(
    ("policy", "field"),
    [(cls, f.name) for cls in POLICIES for f in dataclasses.fields(cls)],
)
def test_policy_field_is_actually_read(policy: type, field: str) -> None:
    """每个策略字段都要有人读——否则界面上设了也没有任何效果。"""
    names = _derived_names(policy, field)
    hits = _references(names)
    assert hits, (
        f"{policy.__name__}.{field} 没有任何地方读取。"
        f"界面上设了它不会有任何效果——这正是本项目反复出现的那类 bug。\n"
        f"要么把它接进真正的判断，要么删掉它。"
    )


def test_storage_only_list_is_not_stale() -> None:
    """`STORAGE_ONLY` 里的文件必须都存在。

    名单写错了会让这条闸静默失效：一个不存在的路径永远不会被跳过，
    于是测试仍然绿，但它检查的东西已经不是它以为的那个了。
    """
    for rel in STORAGE_ONLY:
        assert (SRC / rel).is_file(), f"STORAGE_ONLY 里的 {rel} 不存在"


def test_derived_property_is_detected() -> None:
    """自检：派生属性必须被识别出来。

    这条防的是"闸本身写错了"。`quiet_start_minute` 的真实用法藏在
    `quiet_start` 属性里，如果 `_derived_names` 退化回只返回字段名，
    上面的参数化测试会误报，然后有人会去"修"一个没坏的东西。
    """
    names = _derived_names(DialoguePolicy, "quiet_start_minute")
    assert "quiet_start" in names, f"没识别出派生属性：{names}"
    assert "quiet_end" not in names, "把不相干属性也算进来了"
