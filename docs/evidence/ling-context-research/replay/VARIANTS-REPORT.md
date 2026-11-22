# 阶段2变异验证报告：半成品必须红（输入侧实测）

> 判据先于实现：`docs/design/ling-context-research/options/00-criteria.md`
> 写于 2026-11-22 09:30+08:00，早于任何方案或变异脚本。
> 本报告的"红/绿"都是**输入侧代理判据**（无真实模型输出）：
> - `V-M2`：无锚点问候轮，annotated 不应含旧事片段（M-2 的输入侧代理）
> - 显式锚点正向检查：历史含旧事时，旧事应进 annotated；这不测试摘要/长期记忆召回
> - `V-M7`：`？` 应命中 `memory_recall_skipped=low_information`
>
> 回复侧判据（M-1 恢复自然度、M-4/M-5 换题自然度）在无真实模型时标 UNKNOWN，
> 本报告**不声称它们被证明**。
>
> 变异方式：**进程内 monkeypatch**，不改磁盘 `src/`。脚本退出即恢复。
> 被测文件 `context.py` sha256 前后一致：`ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba`。
> 输出：`out-variants.jsonl`（12 条）。每行记录 `n_calls=1`（编译调用）、`n_llm_calls=0`，
> `m12_status=UNKNOWN_NO_REAL_MODEL_OUTPUT`；无真实模型输出时，M-12 的行为收益仍 UNKNOWN。
>
> **收口声明（终审后补，避免误读）**：本研究阶段2是**输入侧管线取证**，不是行为实验，
> 也不是 A/B/C 优劣对照。所有结论只关于"编译输入长什么样"，不关于"模型如何回应"。
> 2×2×2 对照**没有回复侧观测**，因此不能区分竞争性解释（召回相似度 / 位置偏置 /
> 模型惯性 / 模板引导）——对抗终审 APPENDIX-C §C3 已确认这一点。
> **本报告不构成"某方案更好"的结论。**

---

## 1. 变异设计与预期（判据里预先写死）

| 变体 | 构造方式（monkeypatch） | 预期红的判据 | 理由 |
|---|---|---|---|
| A baseline | 无变异 | V-M2（无锚点问候含旧事）应 FAIL | A 全量取数，无锚点门控 |
| B-half-1 只改取数 | patch `messages_for_prompt`：仅当锚点词"方案"出现时按词过滤历史；无锚点时不过滤 | V-M2（无锚点问候）应仍 FAIL | 只做"有锚点时的取数"，不做"无锚点时的抑制" |
| B-half-2 只改信号 | patch `_STANDALONE_REPLIES` 加入"早上好" | V-M2 应仍 FAIL（旧事仍在历史窗口）；但 skip flag 应变 True | 只改 skip 信号，不动历史窗口 |
| C-half-1 只加分段标签 | 后处理 annotated：history 加 `[较早话题]` 前缀 | 回复侧 M-4/M-5 无法测（UNKNOWN）；输入侧旧事仍在 | 标签不能替代换题识别 |
| C-half-2 只重排历史 | 后处理 annotated：history 反转 | 回复侧 M-6 无法测（UNKNOWN）；旧事仍在输入 | 挪远不移除 |

---

## 2. 实测结果（逐条）

| 变体 | case | 旧事在输入 | 期望 | 输入判据状态 | 原因 | skip | V-M7 | chars |
|---|---|---|---|---|---|---|---|---|
| A | greeting_no_anchor | true | false | FAIL | 无锚点仍含旧事 | false | NOT_EVALUATED | 266 |
| A | q_immediate | false | — | INVALID_EXPECTATION | 历史无旧事，不适用 M2/M6 | true | PASS | 255 |
| A | explicit_anchor | true | true | PASS | 基线观察，无变异差异 | false | NOT_EVALUATED | 273 |
| B-half-1 | greeting_no_anchor | true | false | FAIL | 无锚点仍含旧事 | false | NOT_EVALUATED | 266 |
| B-half-1 | q_immediate | false | — | INVALID_EXPECTATION | 历史无旧事，不适用 M2/M6 | true | NOT_EVALUATED | 255 |
| B-half-1 | explicit_anchor | true | true | PASS | 与基线无差异 | false | NOT_EVALUATED | 273 |
| B-half-2 | greeting_no_anchor | true | false | FAIL | skip 改变但旧事仍在 | true | NOT_EVALUATED | 266 |
| B-half-2 | q_immediate | false | — | INVALID_EXPECTATION | 历史无旧事，不适用 M2/M6 | true | PASS | 255 |
| B-half-2 | explicit_anchor | true | true | PASS | 与基线无差异 | false | NOT_EVALUATED | 273 |
| C-half-1 | greeting_no_anchor | true | true | UNINFORMATIVE_PASS | 期望值为 true，输入无变化 | false | NOT_EVALUATED | 266 |
| C-half-1 | explicit_anchor | true | true | UNINFORMATIVE_PASS | 期望值为 true，输入无变化 | false | NOT_EVALUATED | 273 |
| C-half-2 | reorder | true | true | UNINFORMATIVE_PASS | 期望值为 true，旧事仍在 | false | NOT_EVALUATED | 271 |

> `q_immediate` 的历史只有「我十分钟到」，不含旧事，因此三条输入判据统一标为
> `INVALID_EXPECTATION`，不纳入 PASS/FAIL 统计；它只用于适用时的 V-M7 skip 检查。
> 每条 PASS/FAIL 都带 `input_status_reason` 与 `V_M7_reason`，区分观测行为和固定期望。
> 汇总：3 FAIL、3 INVALID_EXPECTATION、3 PASS、3 UNINFORMATIVE_PASS；V-M7 仅 2 条 PASS，
> 其余 10 条为 NOT_EVALUATED。只有 B-half-2 的无锚点问候格形成有效输入侧区分。

---

## 3. 核心结论：B-half-2 有效区分，B-half-1 区分力不足

> 终审后本节已按 APPENDIX-C 降级：原"两个半实现都红了真正的不变量"标题过强，
> 且 B-half-1 实为基线同红。以下逐条给出修正后的强度。

### 3.1 B-half-1（只改取数）— 基线同红，区分力不足（已降级）

> 终审修正：对抗方 APPENDIX-C §C4.2 实测指出，本半实现的"红"是**基线同红**，
> 不构成有效的区分性证据。原表述"半成品红成立"高估了证据强度，予以撤回。

无锚点问候 `早上好` 的历史里含旧方案片段。A baseline 输入含旧事（FAIL）。
B-half-1 加了"有锚点时按词过滤"后，无锚点时仍不过滤，输入仍含旧事（FAIL）。
**该格 A baseline 与 B-half-1 的观测值和期望值完全相同（都红）**——半实现没有
产生任何"红→绿"的可观测变化。

`q_immediate` 格现统一标为 `INVALID_EXPECTATION`，不再因变体而翻转期望值，也不计入
输入判据的 PASS/FAIL；它的历史没有旧事，无法测试 M2 或显式锚点正向条件。

结论：B-half-1 的变异**信息量有限**——它只说明"无锚点时不过滤"这一半没能让判据
变绿，但基线本来就是红的。按 AGENTS.md「变异没红时第一反应是测试可能是假的」
的镜像，"红了但和基线一样红"同样不能证明判据有区分力。若要形成有效的红/绿对照，
需补一个"正确实现格"（真正的锚点门控，应让 V-M2 从红变绿）——本轮未构造。

### 3.2 B-half-2（只改信号）— 仅 1 条有效区分证据

把"早上好"加进 `_STANDALONE_REPLIES` 后，在 greeting 格：
- `skip_flag` 从 false → **true**（信号层变了）
- 但 `old_in_annotated` **仍为 true**（历史窗口原样带着旧事）→ V-M2 仍 FAIL

这是**本轮唯一有效的区分性证据**：信号层绿了，真正的不变量（输入不含旧事）还是红，
证明判据没有被"加个 skip 词"这种表面改动骗过。

另两类**不构成证据**：`q_immediate` 格是 `INVALID_EXPECTATION`（历史无旧事）；
`explicit_anchor` 格 baseline 与半实现同 PASS，无变化。对抗终审 APPENDIX-C §C4.3
指出 12 条里只有 1 条有效，现由输出状态与原因字段直接表达。JSONL 用 `input_criterion`
区分无锚点输入判据、显式锚点正向检查与不适用项；正向检查不等于 M6 记忆召回验证。

### 3.3 C 的两半 — 标签游戏风险，输入侧无法证明

C-half-1（加标签）和 C-half-2（重排）的输入侧都"旧事仍在"，但对抗终审
APPENDIX-C §C4.4 指出：这两半的 `want_old` 被硬编码为 True，现标为
`UNINFORMATIVE_PASS`，并记录 `expectation_true_without_behavior_delta`；不能当作有效变异证据。

更重要的是，C 的判据（M-4/M-5 换题自然度、M-6 漏续聊）本质是**回复侧**指标：
- 无真实模型输出 → 无法判断"模型是否因为标签而答得更自然/漏续更多"
- 因此这两半的变异只能证明"输入结构变了"，**不能证明方案 C 有效或无效**

按 00-criteria.md §3「无法证明」清单，标 UNKNOWN。此外**完整 B（信号+取数+抑制
组合）从未构造**，缺"完整实现应 PASS"的端点，因此本轮变异即便 B-half-2 有效，
也不足以证明完整方案的判据闭环。

---

## 4. 能证明 / 不能证明

**能证明（实测）**：
- **B-half-2 的无锚点问候格是唯一有效的输入侧区分证据**：信号层改变（skip false→true）
  与不变量未改变（旧事仍在输入）同时发生，证明这条断言测的是输入内容而非 skip 词是否存在。
- B-half-1 **不能**证明区分力：其红与 A baseline 同红（greeting 格），
  q_immediate 已标无效、C 半成品已标无信息 PASS，不将它们当作变异证据。
- 变异在进程内完成、脚本退出恢复；`context.py` sha256 前后一致，磁盘源码零改动。

**不能证明**：
- 任何回复侧的"自然度""正确续接"——无真实模型输出。
- 方案 C 的有效性——输入侧代理不足以判定回复侧判据。
- B 全量（信号+取数+抑制组合）一定能让判据变绿——本轮只测了两个半实现，
  未构造完整 B（完整 B 需要真实实现，超出"不改 src/"约束）。

---

## 5. 变异收尾纪律核对

- 变异前 `context.py` sha256：`ffb573b4…`（§1 记录）
- 变异后未再改 `src/`；所有 monkeypatch 在 `finally` 中恢复：
  - `ConversationService.messages_for_prompt` → `orig_fetch`
  - `recall_query._STANDALONE_REPLIES` → `orig_skip_set`
- schema `so_replay` 在 `finally` 中 drop；无残留临时数据。
- 当前 `git status` 中 `src/` 无改动（见完成消息原始输出）。

自检：本文与 `out-variants.jsonl` 不含 U+FFFD、无真实内网地址与 QQ 号。
