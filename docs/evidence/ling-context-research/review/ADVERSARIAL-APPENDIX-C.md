# 对抗验收报告 · 附录 C：阶段 2 回放/变异产物审查

> 本文是 `ADVERSARIAL-REPORT.md` 的附录 C。
> **判据来源**：我用自己于 09:28 写的 `ADVERSARIAL-CRITERIA.md`（sha `783f829d…`）审查；
> `ADVERSARIAL-PHASE2-CRITERIA.md`（09:52，samples-options 的 subagent 写）是本次被审产物之一，
> **不作为我的判据来源**，避免"用 designer 的判据验 designer 的实现"。
> 写于 2026-11-22 09:50+08:00；HEAD 复核 `de43cbe...`；只写 review/。
> 审查对象：replay_01.py、out-baseline.jsonl、02-contrast.py、out-contrast.txt、
> 03-variants.py、out-variants.jsonl、BASELINE-REPORT.md、VARIANTS-REPORT.md、
> options/02-options.md（D 组闭合段）。

---

## C1. 总裁决

| 核对项（lead 指定） | 裁决 |
|---|---|
| 真实输出 vs SIMULATED 分栏 | **到位（实测）**：所有记录 `source=COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT`；报告明写零模型调用、零回复文本；无一处把输入快照当模型输出 |
| C1–C10 口径 | **部分到位**：C5（输入长度=annotated 字符和 253–316）、C6（调用数=每格 1 compile、0 LLM）有数；C1/C2/C3/C4（误提/自然度/漏续/指错人）全部 UNKNOWN，报告诚实标注；C7–C10 的区分力/反例结构在判据层有，但**结果层无** |
| 2×2×2 四格 | **执行了 8 格且输出可核对**，但**它测的不是"旧话题为何被继续接下去"**——只测"旧事字符串是否进 annotated"，没有任何回复侧观测（详见 C3） |
| 半成品红 + sha 恢复 + git status | **形式到位，实质有一处可疑**：B-half-2 信号层与不变量分离是强证据；B-half-1 的"红"是我见到的最弱一种（判据期望值随变体翻转）；sha 与 src/ 状态我复核干净（详见 C4） |

**一句话**：这是一份**输入侧管线取证**，不是"方案 A/B/C 谁更好"的行为实验；它在自己的声明范围内诚实合规，但**不可用于回答 lead 原任务中"旧话题为何容易被继续接下去"的竞争性解释排序**。

---

## C2. 逐项实测核对

### C2.1 `？` 的 skip 行为：设计者自报被自己的原始数据推翻，修正正确（实测）

- `BASELINE-REPORT.md` §1 声称"`？` skip=True 零自动召回，早上好 skip=False"——我用
  `.venv/bin/python` 直接调 `should_skip_automatic_recall` 复核：`'？'→True`、`'早上好'→False`、`'后来呢？'→(False, needs_recent_topic=True)`，与报告逐字一致。
- 机制我实算：`NFKC('？')` → `?`，再 `rstrip('.!?。~')` → 空串，命中 `_STANDALONE_REPLIES` 的空串条目。
- 这**推翻了我在附录 A §A1.2 与 im/cross/private 原稿中的推测**（"？触发宽泛召回"）。按 S5/A5，这是"实测推翻推理"的正确处理，报告明写"推翻 lead-notes 早期推测"，值得肯定。
- **但**：这同时使 im/cross L94 那句"这条路径最容易召回显著旧事"若仍存在会更错——owner 已改为"当前未测"的中性措辞（我 B1 验证过），没有残留冲突。**结论：这条链自洽。**

### C2.2 快照与 01-samples 的映射：诚实（实测）

`BASELINE-REPORT.md` §1.5 显式声明 6 条 case ↔ S-xx 的映射及"历史被简化"，并明写"只取 13 条开放样本中的 6 条，不声称全覆盖"。我比对了 `replay_01.py:148-156` 的 cases 与 `out-baseline.jsonl` 的 6 条记录，case 名、user_text、history 都一致。**没有"id 命名空间不同却声称一一对应"的假绿**。

### C2.3 输入长度与调用数：有可复查的数（实测）

- `out-baseline.jsonl` 六条 chars = 288/274/255/253/293/262；`out-contrast.txt` 八格 chars = 258–316；`out-variants.jsonl` 12 条 chars = 255–273。
- 脚本结构：`replay_01.py:94-113` 每次 `compile()` 一次，无模型调用；`source` 字段全为 `COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT`。C5/C6 口径成立。
- **限制**：chars 是 `annotated` 字符和（脚本 `:109`），**不是 token 数**；C5 原判据说"token 数（含 prompt 全量）"。报告用字符数代 token 并在 §2 明说"M-11 的实测口径：全量 annotated 字符和"——口径替换是显式的，可接受，但下游若做 C5 跨方案比较须统一用同一口径。

### C2.4 2×2×2 输出核对（实测，与报告表格逐格一致）

八格 `old_in_annotated` = T/T/T/T/T/F/T/F（按 A1B1C1…A0B0C0 顺序），`n_history` 2/2/2/2/1/1/1/1，`evidence=0 summary=0`。
- 因子 B（锚点）对输入侧零差异：A=1 四格全 T；A=0+C=0 两格全 F。**证实"A 方案取数不看锚点"**（这是输入侧结构事实）。
- 因子 C（引用注入）在 A=0 时决定旧事是否以 user_text 形式出现：C=1→T，C=0→F。与 D 组"入站 reply_to 无独立 prompt 块、引用靠 connector 拼进 user_text"一致。
- **混淆检查**：报告 §3 说 A=0+C=1 的旧事来自 user_text 而非历史窗口，n_history=1 佐证——我核对 `02-contrast.py:42` 确实把 OLD 拼进 `text`，判定正确。

---

## C3. 关键方法学缺陷：2×2×2 不能回答"旧话题为何被继续接下去"

这是本报告最重要的对抗发现。

**判据依据**：我 09:28 写的 §10 与附录 A §A3 要求四格观测的是"**模型是否续接旧事、续接是否针对锚点**"，因为：
- E1（召回/片段驱动）与 E6（模型惯性）都会在"旧片段在输入"时续接，光看输入无法区分；
- E5（用户真延续）要求看"有锚点格是否续接"；
- E3（未完成意图）要求看"无锚点但旧事在输入时是否续接"。

**实际做法**：`02-contrast.py:22-23` 的 `has_old()` 只检查 `OLD in m["text"]`——纯粹的输入字符串存在性。`out-contrast.txt` 里没有任何"模型回复"字段；`BASELINE-REPORT.md` §3 自己也写"回复侧全 UNKNOWN"。

**后果**：
1. 八格中 A=1 的四格 `old_in_annotated=True` 是**脚本构造的必然结果**（`hist` 里就放了 OLD，`replay_01.py:74-79` 原样入 history），不是发现；
2. "因子 B 对输入侧零差异"证明的是"编译器不按锚点过滤历史"，**不是**"模型不按锚点决定续接"；
3. 用这份数据**无法**给我在主报告 §4 排的 E1/E3/E5/E6 任何一格打分——所有格都停在"输入侧"，竞争解释排序**未被推进**。

**裁决**：按 A3（区分力）与 C1-C4（回复侧指标），**这组实验不能替代我建议的 2×2×2 行为实验**。它不是假实验（管线属性真实），但它是**另一个问题**的实验。lead 若用它来"区分竞争性解释"，会犯"用输入侧证据回答输出侧问题"的范畴错误。

**补救**（最小成本）：在同样 8 格上接一次真实模型（或至少一次按规则明确标注的 SIMULATED 对照），观测"回复是否提及旧事/是否针对锚点"，才能真正对 E1/E3/E5/E6 打分。无 API 时可先跑 SIMULATED 并**只**报"规则下输出"，不得外推到模型。

---

## C4. 半成品红与变异纪律：一处强、一处弱、一处诚实

### C4.1 B-half-2：强证据（实测，我认同）

`03-variants.py:98-103` 把 `recall_query._STANDALONE_REPLIES` 加入"早上好"；我复核 `context.py:29` 是 `from ... import should_skip_automatic_recall`（函数对象），而函数体 `:22` 读的是模块全局 `_STANDALONE_REPLIES`——**patch 能到达代码路径**。
输出显示：greeting 格 `skip_flag false→true`，但 `old_in_annotated` 仍 true、`V_M2` 仍 FAIL。**信号层绿、不变量红同时发生**——这正是 A1"半成品必须红"要的实证，判据没被"加个 skip 词"骗过。

### C4.2 B-half-1：红得可疑，期望值随变体翻转（实测，需标注）

`03-variants.py:52-56`：`want_old = want_old_when_filter if label=="B_half1_fetch" else want_old_expect`。
- A_baseline 的 q_immediate：`want_old=True`，实际 false → FAIL；报告 §2 注自己承认"期望值写错，历史里根本没有旧事，A FAIL 是正确的红……已标注，不拿它当变异证据"。
- B_half1 的 q_immediate：`want_old=False`（翻转），实际 false → PASS。
- B-half-1 的 greeting 格 `want_old=False`、实际 true → FAIL，与 A baseline 同格**完全相同的观测值与期望值**。

**对抗判定**：
- "B-half-1 红"的证据只来自 greeting 格，而该格 A baseline 也红、B-half-1 也红——**说明 B-half-1 没有产生任何"从红变绿"或"从绿变红"的可观测变化**（除了 q_immediate 的期望值被改写）。按 AGENTS.md「变异没红时第一反应是测试可能是假的」的镜像，这里"红了但红得和基线一样"同样信息量有限。
- 报告 §3.1 说"这证明判据 V-M2 测的是无锚点时不带旧事这个不变量，半实现无法让它变绿"——这个结论**成立但弱**：它只证明"什么都没做时红"，没证明"做对了会绿"（缺完整 B 的正对照）。报告 §4 自己也承认"未构造完整 B"。
- **建议**：把 B-half-1 的红标为"基线同红，非区分性证据"，或补一个"正确实现"格（真正的锚点门控）看是否变绿，才能形成红/绿对照。

### C4.3 C 的两半：诚实标 UNKNOWN（实测，认可）

`03-variants.py:105-129` 的 C-half-1（加 `[较早话题]` 前缀）与 C-half-2（反转 history）都只改输入结构；报告 §3.3 明说回复侧 M-4/M-5/M-6 无法测、不冒充验证通过。**符合 A5/S8**，无需修正。

### C4.4 收尾纪律核对（实测）

- `git status --short src/` 空；`shasum -a 256 src/ling/kernel/context.py` = `ffb573b4...9718ba`，与报告 §1/§5 一致（**实测复核**）。
- 但注意：我看到 `git status --short` 中 `docs/evidence/...` 大量 `A`/`AM`（含我的 review/ 文件）——**有人做了批量 `git add`**，把 phase-2 产物与我的未提交报告一起暂存了。按项目纪律我报告这一点：`AM docs/.../BASELINE-REPORT.md` 表示它暂存后又被改过；我的 review/ 四份也被 `A` 暂存。我不撤回（不是我的作用域），但 lead 应知道提交历史的归属会失真。
- schema `so_replay` 的 drop 在 `finally`（`replay_01.py:161-162`、`02-contrast.py:55-56`、`03-variants.py:130-131`），代码层确认。

---

## C5. 编码自检（实测，澄清一个假阳性）

`grep -rn $'\ufffd'` 会命中 `01-samples.md:308` 和 `ADVERSARIAL-PHASE2-CRITERIA.md` 里的**字面转义序列**（文档在写自检命令），不是真实损坏。我用 Python `read_text(encoding='utf-8')` 逐文件扫描 `docs/`，**真实的 U+FFFD 字符零命中**。报告的自检说法成立。

---

## C6. 对 options/02-options.md D 组闭合段的核对（实测）

§3 现写"不需要新窗口来切断；跨窗口旧内容通过 awareness/handoff/recall 进入且各有 gate；sealed 主动切断；'新窗口不是现状也不是必需'——代码路径实测+推理，非行为实测"。与我在附录 B §B5 的口径一致（sealed=隔离档位，非新窗口；入站无新窗入口），且明确降级为"代码路径实测+推理"。**通过**。

---

## C7. 汇总与待办

| 项 | 裁决 |
|---|---|
| 诚实性（真实/模拟/未取得） | **优秀**：`COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT` 贯穿所有原始输出，无一处越界 |
| 管线属性（skip、输入长度、车道、调用数） | **实测通过**，我复核了函数、脚本、原始输出 |
| 2×2×2 行为解释力 | **未达标**：只测输入存在性，不能排序 E1/E3/E5/E6；不可当作"旧话题为何被续接"的证据 |
| 半成品红 | B-half-2 强；B-half-1 弱（基线同红+期望翻转），应降级表述；C 诚实 UNKNOWN |
| 收尾纪律 | src/ 干净、sha 一致、schema finally drop 均确认；但批量 git add 污染提交归属，已报告 |
| 编码 | 真实 U+FFFD 零命中（grep 命中是字面转义，假阳性） |

**仍待**:若 lead 要求真正回答"旧话题为何容易被继续接下去"，必须补一组**带回复侧观测**的对照（真实模型优先；无 API 则标 SIMULATED 且只报规则输出）。现有 8 格输入快照可复用为因子设定，但不能替代回复侧数据。
