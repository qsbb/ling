"""Stable JSON snapshots. Normal runs never write fixtures."""

import json
import os
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).with_name("golden")


def snapshot(name: str, actual: Any) -> None:
    path = GOLDEN / f"{name}.json"
    serialized = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.environ.get("LING_RECORD_WP00") == "1":
        path.write_text(serialized, encoding="utf-8")
    else:
        assert serialized == path.read_text(encoding="utf-8"), f"golden snapshot changed: {name}"


def shape(
    value: Any,
    *,
    maps: frozenset[str] = frozenset(),
    opaque_maps: frozenset[str] = frozenset(),
) -> Any:
    """Preserve field names and JSON types but never record scalar values.

    ``maps`` have dynamic keys but a declared value shape. In ``opaque_maps``,
    every descendant's dictionary keys are data, not schema. Arrays record the
    set of observed item shapes.
    """

    def visit(item: Any, path: str, opaque: bool) -> Any:
        opaque = opaque or path in opaque_maps
        if isinstance(item, dict):
            if opaque or path in maps:
                return {
                    "$map": _distinct(visit(child, f"{path}.*", opaque) for child in item.values())
                }
            return {key: visit(child, f"{path}.{key}", opaque) for key, child in item.items()}
        if isinstance(item, list):
            return _distinct(visit(child, f"{path}[]", opaque) for child in item)
        return {
            type(None): "null",
            bool: "boolean",
            str: "string",
            int: "integer",
            float: "number",
        }[type(item)]

    return visit(value, "", False)


def _distinct(shapes):
    return [json.loads(item) for item in sorted({json.dumps(s, sort_keys=True) for s in shapes})]
