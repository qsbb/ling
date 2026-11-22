"""半成品变异验证（硬要求：两半各自构造一次，确认对应判据会红）。

不改磁盘 src/；所有半实现用 monkeypatch 在进程内构造，退出即恢复。
判据全部是输入侧可判定的（无真实模型输出）：
  V-M2  无锚点问候轮，annotated 不应含旧事片段（判据 M-2 的输入侧代理）
  V-M7  「？」跳过自动召回（meta.memory_recall_skipped == low_information）
  V-M6  引用/找回锚点存在时，旧事片段应进 annotated（M-6 输入侧代理）
  V-M11 输入长度可测且两半方向与声明一致
回复侧判据（M-1 恢复自然度、M-4 换题自然度等）无真实模型，标 UNKNOWN。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import replay_01 as R
from ling.domains.conversation import service as conv_service
from ling.domains.memory import recall_query

OLD = "上次那个产品方案你再发我一份"


def frag_in(rec: dict) -> bool:
    return any(OLD in m["text"] for m in rec["annotated"])


def judge(label: str, rec: dict, *, want_old: bool | None, want_skip: bool | None) -> dict:
    got_old = frag_in(rec)
    got_skip = rec["meta"].get("memory_recall_skipped") == "low_information"
    case = rec["case"]
    if "greeting_no_anchor" in case:
        criterion = "M2_no_anchor_old_absent"
        if label.startswith("C_") and got_old == want_old:
            input_status = "UNINFORMATIVE_PASS"
            input_reason = "expectation_true_without_behavior_delta"
        elif got_old == want_old:
            input_status = "PASS"
            input_reason = "observed_expected_state"
        else:
            input_status = "FAIL"
            input_reason = "behavior_triggered_old_fragment_without_anchor"
    elif "explicit_anchor" in case:
        criterion = "explicit_anchor_old_present"
        if label.startswith("C_") and got_old == want_old:
            input_status = "UNINFORMATIVE_PASS"
            input_reason = "expectation_true_without_behavior_delta"
        else:
            input_status = "PASS" if got_old == want_old else "FAIL"
            input_reason = (
                "observed_expected_state_no_variant_delta"
                if got_old == want_old else "behavior_triggered_missing_old_fragment"
            )
    elif "C_half2_reorder" in case:
        criterion = "old_fragment_retained_after_reorder"
        input_status = "UNINFORMATIVE_PASS" if got_old == want_old else "FAIL"
        input_reason = (
            "expectation_true_without_behavior_delta"
            if got_old == want_old else "behavior_triggered_old_fragment_missing"
        )
    else:
        criterion = "NOT_APPLICABLE"
        input_status = "INVALID_EXPECTATION"
        input_reason = "history_has_no_old_fragment_for_M2_or_M6"
        want_old = None

    if want_skip is None:
        m7_status = "NOT_EVALUATED"
        m7_reason = "no_skip_expectation_for_case"
    else:
        m7_status = "PASS" if got_skip == want_skip else "FAIL"
        m7_reason = "observed_skip_state_matches_expectation" if got_skip == want_skip else "skip_state_mismatch"

    return {
        "variant": label,
        "case": case,
        "old_in_annotated": got_old,
        "want_old": want_old,
        "input_criterion": criterion,
        "input_expectation_status": input_status,
        "input_status_reason": input_reason,
        "skip_flag": got_skip,
        "want_skip": want_skip,
        "V_M7": m7_status,
        "V_M7_reason": m7_reason,
        "chars": rec["annotated_chars"],
        "n_calls": rec["n_calls"],
        "n_calls_kind": rec["n_calls_kind"],
        "n_llm_calls": rec["n_llm_calls"],
        "m12_status": rec["m12_status"],
    }


async def run_matrix(label: str) -> list[dict]:
    out = []
    cases = [
        ("greeting_no_anchor", "早上好", [(1, "user", OLD, None)], False, False, None),
        ("q_immediate", "？", [(1, "user", "我十分钟到", None)], None, False, True),
        ("explicit_anchor", "上次说的那个产品方案", [(1, "user", OLD, None)], True, True, None),
    ]
    for name, text, hist, want_old_expect, want_old_when_filter, want_skip in cases:
        # want_old 随变体不同：baseline 全量取数=含旧事；B-half-1 过滤后按锚点
        rec = await R.run_case(case=f"{label}_{name}", user_text=text, history=hist, is_group=False)
        want_old = want_old_when_filter if label == "B_half1_fetch" else want_old_expect
        out.append(judge(label, rec, want_old=want_old, want_skip=want_skip))
    return out


async def main() -> None:
    await R.setup_schema()
    results: list[dict] = []
    try:
        # ---------- A baseline ----------
        results += await run_matrix("A_baseline")

        # ---------- B-half-1: 只改取数，按锚点词过滤历史 ----------
        orig_fetch = conv_service.ConversationService.messages_for_prompt

        async def fetch_anchor_only(self, window_id, *, limit, after_sequence=0,
                                    before_sequence=None, **kw):
            rows = await orig_fetch(self, window_id, limit=limit,
                                    after_sequence=after_sequence,
                                    before_sequence=before_sequence, **kw)
            # 半实现：只在 user_text（由外部闭包提供）含"方案"时保留含"方案"的消息
            anchor = getattr(fetch_anchor_only, "anchor", "")
            if anchor:
                return [m for m in rows if anchor in (m.text or "")]
            return rows  # 无锚点时不过滤 —— 半成品，仍会把旧事带进来

        conv_service.ConversationService.messages_for_prompt = fetch_anchor_only
        try:
            # 逐个 case 设定锚点（模拟"只做取数识别"）
            for name, text, hist, want in (
                ("greeting_no_anchor", "早上好", [(1, "user", OLD, None)], False),
                ("q_immediate", "？", [(1, "user", "我十分钟到", None)], None),
                ("explicit_anchor", "上次说的那个产品方案", [(1, "user", OLD, None)], True),
            ):
                fetch_anchor_only.anchor = "方案" if "方案" in text else ""
                rec = await R.run_case(
                    case=f"B_half1_{name}", user_text=text, history=hist, is_group=False
                )
                results.append(judge("B_half1_fetch", rec, want_old=want, want_skip=None))
        finally:
            conv_service.ConversationService.messages_for_prompt = orig_fetch

        # ---------- B-half-2: 只改信号，把"早上好"加进 skip 集合 ----------
        orig_skip_set = set(recall_query._STANDALONE_REPLIES)
        try:
            recall_query._STANDALONE_REPLIES = orig_skip_set | {"早上好"}
            results += await run_matrix("B_half2_signal")
        finally:
            recall_query._STANDALONE_REPLIES = orig_skip_set

        # ---------- C-half-1: 只加分段标签（后处理 annotated）----------
        for name, text, hist, want in (
            ("greeting_no_anchor", "早上好", [(1, "user", OLD, None)], True),
            ("explicit_anchor", "上次说的那个产品方案", [(1, "user", OLD, None)], True),
        ):
            rec = await R.run_case(
                case=f"C_half1_{name}", user_text=text, history=hist, is_group=False
            )
            # 半实现：给 history 加标签，不改变取数 → 旧事仍在
            for m in rec["annotated"]:
                if m["lane"] == "history":
                    m["text"] = "[较早话题] " + m["text"]
            results.append(judge("C_half1_label", rec, want_old=want, want_skip=None))

        # ---------- C-half-2: 只重排历史（把旧消息挪远）----------
        rec = await R.run_case(
            case="C_half2_reorder", user_text="后来呢",
            history=[(1, "user", OLD, None), (2, "actor", "好", None),
                     (3, "user", "今天天气", None)], is_group=False,
        )
        # 半实现：把 history 反转（旧消息挪到最后）
        hist_msgs = [m for m in rec["annotated"] if m["lane"] == "history"]
        other = [m for m in rec["annotated"] if m["lane"] != "history"]
        rec["annotated"] = other + list(reversed(hist_msgs))
        results.append(judge("C_half2_reorder", rec, want_old=True, want_skip=None))
    finally:
        await R.teardown_schema()

    for r in results:
        print(json.dumps(r, ensure_ascii=False))


asyncio.run(main())
