#!/usr/bin/env python3
"""从 Pydantic 契约导出 JSON Schema 到 contracts/v1/。

契约的唯一源码是 ``src/ling/contracts/v1.py``；本脚本只做导出，不手写 schema。
运行：``python scripts/export_contracts.py``（需要已安装依赖）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ling import contracts  # noqa: E402
from ling.contracts import v1  # noqa: E402

EXPORTED = [
    "InteractionEnvelope",
    "RuntimeScope",
    "AudienceRule",
    "SessionRecord",
    "ChatMessage",
    "HandoffCapsule",
    "ContextContribution",
    "CapabilityDescriptor",
    "ActionIntent",
    "OperationResult",
    "DeliveryReceipt",
    "MemoryProposal",
    "ErrorResponse",
    # 只读生活时间轴（设计 §7）：双时区、段/事实分层、未来意向与梦卡片。
    # 仪表盘两块摘要（today_outline / intent_teaser）也在这一组里。
    "LifeTimelineOut",
    "LifeTimelineSegment",
    "LifeTimelineFact",
    "DayIntentSummary",
    "DreamCard",
    "TodayOutlineOut",
    "TodayOutlineItem",
    "IntentTeaserOut",
    "IntentTeaserItem",
    # 「查看本次提示词」：某条回复当时真正发给模型的上下文（只读快照）。
    # 枚举 `PromptBlock` / `PromptSnapshotState` **不单独导出**（导出器只处理
    # Object 契约）；它们的取值已内联在 `PromptSnapshotSection.key` 与
    # `PromptSnapshotOut.state` 的 schema enum 里，前端从那里取即可。
    "PromptSnapshotItem",
    "PromptSnapshotModel",
    "PromptSnapshotOut",
    "PromptSnapshotSection",
]


def main() -> int:
    out_dir = ROOT / "contracts" / "v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    for name in EXPORTED:
        model = getattr(v1, name)
        schema = model.model_json_schema()
        schema["$id"] = f"https://ling.local/contracts/{v1.SCHEMA_VERSION}/{name}.schema.json"
        schema["x-ling-schema-version"] = v1.SCHEMA_VERSION
        filename = f"{name}.schema.json"
        (out_dir / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        index[name] = filename
    (out_dir / "index.json").write_text(
        json.dumps(
            {"schema_version": v1.SCHEMA_VERSION, "contracts": index},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"exported {len(index)} contracts to {out_dir} (ling {contracts.SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
