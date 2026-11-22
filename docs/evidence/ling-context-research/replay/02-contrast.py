"""2×2×2 输入侧对照回放（无真实模型，只测编译输入管线属性）。

因子 A：旧事片段是否存在于历史（有 / 无）
因子 B：本轮用户是否带显式锚点（有 / 无）
因子 C：引用正文是否注入 user_text（注入 / 不注入）

每格导入 01-replay 的 run_case，导出 annotated 快照摘要到 stdout JSONL。
真实模型输出：本脚本不产出，一律「未取得真实输出」。
脚本模拟回复：本脚本不生成；回复侧判定见报告，标 SIMULATED/UNKNOWN。
"""

from __future__ import annotations

import asyncio
import json

from replay_01 import run_case, setup_schema, teardown_schema  # type: ignore

OLD = "上次那个产品方案你再发我一份"


def has_old(rec: dict) -> bool:
    return any(OLD in m["text"] for m in rec["annotated"])


async def main() -> None:
    await setup_schema()
    rows = []
    try:
        for a in (True, False):          # A: 旧事片段有无
            for b in (True, False):      # B: 本轮锚点有无
                for c in (True, False):  # C: 引用正文注入与否
                    name = f"A{int(a)}B{int(b)}C{int(c)}"
                    hist = [(1, "user", OLD, None), (2, "actor", "好", None)] if a else [
                        (1, "user", "今天天气不错", None)
                    ]
                    text = "上次说的那个产品方案，整理好了吗" if b else "早上好"
                    if c:
                        text = f"[quote D-1 21:30 对方：{OLD}] " + text
                    rec = await run_case(case=name, user_text=text, history=hist, is_group=False)
                    rows.append({
                        "case": name,
                        "A_old_in_hist": a, "B_anchor_in_turn": b, "C_quote_injected": c,
                        "old_fragment_in_annotated": has_old(rec),
                        "n_history": rec["lanes"].get("history", 0),
                        "n_evidence": rec["lanes"].get("evidence", 0),
                        "n_summary": rec["lanes"].get("summary", 0),
                        "skip": rec["meta"].get("memory_recall_skipped", ""),
                        "annotated_chars": rec["annotated_chars"],
                        "n_calls": rec["n_calls"],
                        "n_calls_kind": rec["n_calls_kind"],
                        "n_llm_calls": rec["n_llm_calls"],
                        "m12_status": rec["m12_status"],
                        "source": "COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT",
                    })
    finally:
        await teardown_schema()

    print("case\tA_old\tB_anchor\tC_quote\told_in_annotated\thistory\tevidence\tsummary\tskip\tchars")
    for r in rows:
        print("\t".join(str(r[k]) for k in (
            "case", "A_old_in_hist", "B_anchor_in_turn", "C_quote_injected",
            "old_fragment_in_annotated", "n_history", "n_evidence", "n_summary",
            "skip", "annotated_chars",
        )))
    print("\nRAW_JSONL_START")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


asyncio.run(main())
