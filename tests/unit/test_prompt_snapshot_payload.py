"""提示词快照的纯函数部分：块标签映射、payload 组装与 256KB 截断。

这几条不需要数据库，所以单独放在 unit 里跑得快——真正的端到端断言（快照
== provider 收到的内容、三态、越权）在
``tests/integration/test_prompt_snapshot.py``。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.kernel.context import (
    KEY_LABELS,
    KEY_ORDER,
    CompiledMessage,
    lane_key,
)
from ling.kernel.turn import (
    PROMPT_SNAPSHOT_MAX_BYTES,
    PROMPT_SNAPSHOT_RETENTION_DAYS,
    PROMPT_SNAPSHOT_SCHEMA_VERSION,
    build_prompt_snapshot_payload,
)


def _msg(
    content: str,
    *,
    role: str = "system",
    key: str = "system",
    lane: str | None = None,
    sender: str | None = None,
) -> CompiledMessage:
    return CompiledMessage(
        role=role, content=content, key=key, lane=lane or key, sender=sender
    )


def test_lane_mapping_is_explicit_and_covers_every_lane() -> None:
    """车道 → 分组的映射必须显式列出每个真实车道，不能靠前缀猜。

    新增车道忘了登记时落到 ``other``（界面上仍然看得见），
    而不是被静默算进某个不相干的组里。
    """
    expected = {
        "persona": "system",
        "constraints": "system",
        "scenario": "system",
        "capabilities": "system",
        "evidence": "memory",
        "core_memory": "memory",
        "knowledge": "memory",
        "short_memory": "memory",
        "relationship": "memory",
        "self_state": "memory",
        "goals": "plan",
        "media": "media",
    }
    for lane, key in expected.items():
        assert lane_key(lane) == key, f"车道 {lane} 应归到 {key}"
    # 没登记的新车道 → other（不是 KeyError，也不会串到别组）
    assert lane_key("brand_new_lane") == "other"
    # 所有 key 都有中文标签、都在固定顺序里
    for key in set(expected.values()) | {"other"}:
        assert key in KEY_LABELS, f"{key} 没有 label"
        assert key in KEY_ORDER, f"{key} 不在展示顺序里"


def test_payload_keeps_order_roles_and_content() -> None:
    """payload 的 messages 与输入逐条同序同内容——快照的唯一价值就在这。"""
    annotated = [
        _msg("你是阿岚。", key="system", lane="persona"),
        _msg("你记得：她喜欢下雨天。", key="memory", lane="evidence"),
        _msg("阿岚：昨天说的那件事", role="user", key="history", sender="阿岚"),
        _msg("嗯，我记得。", role="assistant", key="history"),
        _msg("今天呢？", role="user", key="input"),
    ]
    payload, truncated = build_prompt_snapshot_payload(
        annotated, model={"provider_id": "p", "model_id": "m", "profile": "dialogue.fast"}
    )
    assert truncated is False
    entries = payload["messages"]
    assert [entry["index"] for entry in entries] == [0, 1, 2, 3, 4]
    assert [(entry["role"], entry["content"]) for entry in entries] == [
        (item.role, item.content) for item in annotated
    ]
    assert [entry["key"] for entry in entries] == [
        "system",
        "memory",
        "history",
        "history",
        "input",
    ]
    # 群聊历史带署名；她自己的话与 system 段没有
    assert entries[2]["sender"] == "阿岚"
    assert entries[3]["sender"] is None
    assert entries[0]["sender"] is None
    # token 估算逐条存在，总数是它们的和（未截断时）
    assert all(entry["tokens"] >= 0 for entry in entries)
    assert payload["token_total"] == sum(entry["tokens"] for entry in entries)
    assert payload["schema_version"] == PROMPT_SNAPSHOT_SCHEMA_VERSION
    assert payload["model"]["profile"] == "dialogue.fast"
    assert payload["truncated"] is False
    assert payload["dropped_items"] == 0


def test_payload_records_history_timestamps() -> None:
    at = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    annotated = [
        CompiledMessage(
            role="user",
            content="阿岚：昨天说的那件事",
            key="history",
            lane="history",
            sender="阿岚",
            at=at,
        )
    ]
    payload, _ = build_prompt_snapshot_payload(annotated)
    assert payload["messages"][0]["at"] == at.isoformat()


def test_oversized_payload_keeps_system_and_tail() -> None:
    """超过 256KB：保留 system 与**最后 N 条**，middle 丢掉并标 truncated。"""
    annotated = [_msg("你是阿岚。" * 50, key="system", lane="persona")]
    # 每条 4000 字符 ASCII（≈1000 token），80 条 ≈ 320KB > 256KB
    for index in range(80):
        annotated.append(
            _msg(f"历史{index}：" + "a" * 4000, role="user", key="history")
        )
    annotated.append(_msg("这一次她收到的", role="user", key="input"))

    payload, truncated = build_prompt_snapshot_payload(annotated)
    assert truncated is True
    assert payload["truncated"] is True
    assert payload["dropped_items"] > 0

    entries = payload["messages"]
    # system 段一条都不能少
    assert entries[0]["key"] == "system"
    assert entries[0]["content"] == annotated[0].content
    # 最后一条（这一轮的输入）一定还在
    assert entries[-1]["key"] == "input"
    # 保留下来的是**连续尾部**（历史从中间某条开始，不是随机挖洞）
    indexes = [entry["index"] for entry in entries]
    assert indexes == sorted(indexes)
    assert indexes[0] == 0  # system
    assert indexes[1] > 1, "应当从中间截，而不是把最早的历史留下"
    assert indexes[1:] == list(range(indexes[1], indexes[-1] + 1))

    # 落库体积确实在上限内
    import json

    size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert size <= PROMPT_SNAPSHOT_MAX_BYTES, f"{size} > {PROMPT_SNAPSHOT_MAX_BYTES}"


def test_single_huge_section_is_cut_not_dropped() -> None:
    """锁定段自己就超：从最长那条砍半并留痕，而不是把 system 整条丢掉。"""
    annotated = [
        _msg("你是阿岚。" + "a" * 300_000, key="system", lane="persona"),
        _msg("这一句", role="user", key="input"),
    ]
    payload, truncated = build_prompt_snapshot_payload(annotated)
    assert truncated is True
    entries = payload["messages"]
    assert len(entries) == 2, "锁定段只该被截断，不该被整条丢掉"
    assert entries[0]["truncated"] is True
    assert "已截断" in entries[0]["content"]
    assert entries[1]["content"] == "这一句"

    import json

    size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert size <= PROMPT_SNAPSHOT_MAX_BYTES, f"{size} > {PROMPT_SNAPSHOT_MAX_BYTES}"


def test_retention_is_seven_days() -> None:
    """保留期是产品约定（7 天），改了要能一眼看出来。"""
    assert PROMPT_SNAPSHOT_RETENTION_DAYS == 7


def test_empty_annotated_yields_empty_payload() -> None:
    payload, truncated = build_prompt_snapshot_payload([])
    assert payload["messages"] == []
    assert payload["token_total"] == 0
    assert truncated is False


@pytest.mark.parametrize("key", ["system", "persona", "memory", "summary", "history",
                                "input", "media", "plan", "other"])
def test_every_declared_key_has_a_label(key: str) -> None:
    """闭集里的每个 key 都必须有人话标签（前端直接用，不再翻译）。"""
    assert KEY_LABELS[key]
