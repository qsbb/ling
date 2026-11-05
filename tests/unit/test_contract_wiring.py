"""契约字段闸：声明了就必须有人填，或者是**标明的**保留位。

## 为什么需要这个文件

这个项目出现过两次「字段存在、看着完整、实际没接通」：

1. `OutboundMessage.media_refs`——存在，但 OneBot 连接器完全忽略它
2. `turn.completed.chunks`——每次都算，但只是装饰性数字，分段从未发生

两次都不是崩溃，而是**假的完整感**：读 schema 的人会以为能力在那儿。

## 闸的规则

对每个导出的契约字段：

- 要么在源码里**真的被赋值**（`field=` 或 `"field":`）
- 要么在 schema 里明确写着「保留位」（`description` 以"保留位"开头）

两者都不满足 → 红。

这样新加字段时只有两个诚实的选择：接上它，或者承认它是保留位。
不存在第三条路——"加进 schema 然后什么都不做"会被挡住。
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "v1"
SOURCE_ROOTS = [ROOT / "src" / "ling", ROOT / "connectors", ROOT / "scripts"]

# schema_version 是导出器统一注入的，不需要每个模型自己赋值
IGNORED = {"schema_version"}


def _source_blob() -> str:
    return "\n".join(
        f.read_text(encoding="utf-8")
        for root in SOURCE_ROOTS
        for f in root.rglob("*.py")
    )


def _is_assigned(name: str, blob: str) -> bool:
    return bool(
        re.search(rf"\b{re.escape(name)}\s*=", blob)
        or re.search(rf'"{re.escape(name)}"\s*:', blob)
    )


def _schema_fields() -> list[tuple[str, str, str]]:
    """(模型名, 字段名, description) —— 只取对象型契约。"""
    out: list[tuple[str, str, str]] = []
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, spec in (data.get("properties") or {}).items():
            if name in IGNORED:
                continue
            out.append((path.stem.replace(".schema", ""), name, spec.get("description") or ""))
    return out


def test_every_contract_field_is_wired_or_declared_reserved() -> None:
    blob = _source_blob()
    problems: list[str] = []
    for model, field, description in _schema_fields():
        if _is_assigned(field, blob):
            continue
        if description.startswith("保留位"):
            continue
        problems.append(
            f"{model}.{field}：既没有源码赋值，也没标成保留位。"
            "读 schema 的人会以为它有用——这正是本项目反复出现的假完整感。"
        )
    assert not problems, "\n".join(problems)


def test_received_at_is_actually_populated() -> None:
    """`received_at` 必须真的填——它是"消息在队列里等了多久"的唯一来源。

    这条单独钉住：它曾是 8 个"声明但从不设置"的字段之一。
    """
    connector = (ROOT / "connectors" / "onebot" / "connector.py").read_text(
        encoding="utf-8"
    )
    assert "received_at=datetime.now(UTC)" in connector, (
        "入站信封没有记录到达时间；occurred_at 是平台事件时间，不能替代它"
    )


@pytest.mark.parametrize(
    "field",
    ["device_ref", "scene_ref", "turn_ref", "audience_ref", "target_ref", "receipt_ref"],
)
def test_reserved_fields_say_so_in_the_schema(field: str) -> None:
    """保留位必须在**导出的 schema** 里说明。

    只在 dataclass 注释里写没用——契约的使用者看的是 schema。
    """
    found = [d for _m, f, d in _schema_fields() if f == field]
    assert found, f"{field} 不在任何契约里"
    assert any(d.startswith("保留位") for d in found), (
        f"{field} 没有在 schema 里标成保留位：{found}"
    )
