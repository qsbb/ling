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
    # 上游依赖不可用时的回退标记（docs/UX-FIXES-2026-11-09.md §4）：
    # 消费点（看手机节奏、当前场景）在响应里说清"这次的值是不是默认值"。
    # 枚举 `FallbackState` / `FallbackSource` 不单独导出（导出器只处理 Object
    # 契约），取值已内联在 `FallbackNotice.state` / `.source` 的 schema enum 里。
    "FallbackNotice",
    # 「查看本次提示词」：某条回复当时真正发给模型的上下文（只读快照）。
    # 枚举 `PromptBlock` / `PromptSnapshotState` **不单独导出**（导出器只处理
    # Object 契约）；它们的取值已内联在 `PromptSnapshotSection.key` 与
    # `PromptSnapshotOut.state` 的 schema enum 里，前端从那里取即可。
    "PromptSnapshotItem",
    "PromptSnapshotModel",
    "PromptSnapshotOut",
    "PromptSnapshotSection",
    # 「关系」页的分页列表（docs/API-WEB-V1.md §13）。`subjects` 默认 50 条，
    # `total` / `has_more` / `q` 与 `SubjectRow` 一起导出：前端只按 schema 里的
    # 字段名读，不照文档手抄。
    "SubjectAccountOut",
    "SubjectRow",
    "SubjectsPage",
    # 接入器可配置（docs/CONNECTOR-CONFIG-DESIGN.md §6）：字段只带 source /
    # configured / preview，**没有**解密后的值；保存结果带 generation 与终态。
    "ConnectorsOut",
    "ConnectorView",
    "ConnectorFieldOut",
    "ConnectorActivationOut",
    "ConnectorAccessGroupOut",
    "ConnectorChangeRequest",
    "ConnectorChangeAccepted",
    "ConnectorOperationOut",
    "ConnectorResetResult",
    # 「检查更新」四态卡片（docs/UPDATE-UX-DESIGN.md）。嵌套片段对象（current /
    # newer / target / task / details.*）也各自导出：它们是 API 契约的一部分，
    # 单独写 schema 才能让前端按字段名/类型对齐，而不是对着文档抄。
    "ImageUpdateView",
    "ImageUpdateCurrent",
    "ImageNewerRelease",
    "ImageUpdateTarget",
    "ImageUpdateTaskRow",
    "ImageUpdateDetails",
    "ImageUpdateCatalogDetails",
    "ImageUpdateUpdaterDetails",
    "ImageUpdateBackupGateDetails",
    # 发现事实（唯一来源：更新器的 `release_check`，见
    # docs/UPDATE-SIMPLIFICATION-DESIGN.md §3）。
    "ImageReleaseCheckDetails",
    "ImageReleaseCheckCurrent",
    "ImageReleaseCheckLatest",
    # 按需检查的固定请求体与回执。
    "ImageUpdateCheckRequest",
    "ImageUpdateCheckAccepted",
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
