# Ling 上下文基线 A · 判据与静态预测

> 性质：**判据先行 + 当前实现的静态预测**。本文不调用模型、不连生产库、不跑回放脚本。
> 凡涉及当前代码会怎样处理的句子，一律是 **SCRIPTED / 静态预测**，不是 REAL_MODEL 输出。
> 观测对象钉住：仓库 `/Users/lingxi/project/ling`，HEAD / 分支 / 工作树 sha256 见 §1.3。
> 研究起始 HEAD=`de43cbe79a78e0c01f7821470500eaa04ea79497`（main）。
> 样本来源：`docs/evidence/ling-context-replay-samples.md`（全部 synthetic）。
> 本文**不读候选方案实现文档**；候选方案由他人撰写，本文只描述现状基线 A。

## (1) 判据先行声明与观测对象

### 1.1 写作顺序声明（可追溯）

本文按下列顺序产出，顺序本身是判据有效性的一部分：

1. 读 `AGENTS.md`，特别是「验收判据要在看到实现之前写完」「格式是置信度声明」「观测对象钉住」三条。
2. 读 `docs/evidence/ling-context-replay-samples.md`（A1–A8、C1–C8）。
3. **先写 §1 判据声明 + §2 C1–C8 判据表（不含任何当前代码结论），落盘。**
4. 再读当前代码作为静态证据（bridge.py / context.py / recall_query.py / service.py）。
5. 追加 §3 反例线索静态预测链、§4 prompt snapshot 字段清单、§5 未取得数据。

§2 的每一条判据在写入时尚未读过实现；§3–§5 才包含对当前代码的静态预测。这个顺序使「判据被实现形状框住」的风险降到最低——但不能降到零，因为判据的选题本身来自样本文件。

### 1.2 格式是置信度声明

| 内容类型 | 本文采用的格式 |
|---|---|
| 已观测事实（HEAD、sha256、文件是否存在） | 命令输出原文，代码块 |
| 先于实现写下的判据 | 普通表格，标注「应然，待回放」 |
| 基于代码的静态预测（未跑真实模型） | 普通段落，每条带「静态预测」字样，不用加粗结论句 |
| 无法从代码确定的环节 | 明写「待回放」，不推测 |

严禁把 SCRIPTED 结论写成表格里的事实。§2 表格里「正确行为」列全部是应然判据，不是「当前已通过」的意思。

### 1.3 观测对象钉住（原始输出）

开始时（2026-11-22T09:32:48+08:00 采样窗口）执行：

```text
$ git log -1 --format='%H %s'
de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制
$ git rev-parse --abbrev-ref HEAD
main
$ git status --short
?? docs/evidence/ling-context-human-turns.md
?? docs/evidence/ling-context-im-async.md
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-offline.md
?? docs/evidence/ling-context-im-private.md
?? docs/evidence/ling-context-replay-samples.md
?? docs/evidence/ling-context-research/
?? docs/research/
$ shasum -a 256 src/ling/kernel/bridge.py src/ling/kernel/context.py src/ling/kernel/recall_query.py src/ling/kernel/service.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
shasum: src/ling/kernel/recall_query.py: No such file or directory
shasum: src/ling/kernel/service.py: No such file or directory
```

工作树**并非全干净**：上述 8 个 untracked 项在开始时已存在，均非本文作者创建。任务描述中的「开始时工作树干净」与观测不符，以此处原始输出为准。

路径修正：`recall_query.py` / `service.py` 实际位于 `src/ling/domains/memory/`，不在 `src/ling/kernel/`。另存在 `.agents/worktrees/*/src/ling/domains/memory/` 同名副本，本文只读主工作树 `src/`，不碰 worktree。修正后的 sha256 在 §3.0 记录。

### 1.4 判据的效力边界（先写，防事后归因）

按 AGENTS.md 的三条要求逐条声明：

- **半成品必须红**：本文是静态基线文档，不构造半成品实现，因此**不声称任何判据已被变异验证**。§2 每条判据的「区分力」列回答的是「如果按该错误实现，回放结果会怎样」，属于推理，不是实测。
- **反例输入**：每条判据都写明「哪个错误实现会让它失败」。答不出来的，明确标 `无法证明`。
- **区分力**：若两条路径在观测字段上相同（例如 `None` 与 `flag=False`），必须指出需要额外观测字段才能区分；否则判据只是注释里的话。

### 1.5 条件式判据，不抄实测数字

C8 特别要求：不能用「固定秒数阈值」做判据。任何具体时长（2 天、3 天等）都只是样本的 Δ，不是 Ling 的参数。判据写成「同一输入在不同位置/历史下解读不同」，而不是「预期 N 秒」。

## (2) C1–C8 判据表（先于实现写下）

> 表中「正确行为」= 应然判据，待 REAL_MODEL 回放验证；「会红的错误实现」= 能区分对错的反例实现。
> 每条都附「观测字段」：回放时必须从 prompt snapshot 抄出的字段，否则判据无法落地。

### C1 显式找回

- 输入：A1。关键配对：与 A2 对照（同一旧历史，一个有时间/话题锚点，一个没有）。
- 正确行为（应然，待回放）：编译输入含旧方案的原文或摘要；回复接方案，而不是泛泛问候。
- 会红的错误实现：只看最近一轮 → 答不出方案；或召回策略无条件关闭 → 旧内容根本不进编译输入。
- 观测字段：compiled messages 是否含旧方案原文/摘要片段、召回来源标记、回复是否提到「四条/提醒偏好」。
- 区分力：能区分「最近一轮截断」与「正确召回」；**不能单独区分**「召回到但没进 prompt」和「没召回」——需要召回阶段与编译阶段两个快照点。

### C2 问候不误召回

- 输入：A2 与 A4（A4 单独发「早上好」，或接在 A1 历史后只发「早上好」）。
- 正确行为（应然，待回放）：编译输入不因问候触发旧事大幅召回；回复只接问候。
- 会红的错误实现：宽泛语义召回 → 列四项旧需求（正是反例线索的形态）。
- 观测字段：召回条目数与来源、compiled messages 中旧方案占比、回复是否出现四项需求。
- 区分力：强——「列四项」是正向可见差异。但要注意负向断言的陷阱：只断言「不列四项」不足以证明「接了问候」，必须同时断言回复确实回应了问候。

### C3 短问号定位

- 输入：A5 位置一（紧跟「早上好」后 3 秒发「？」）。
- 正确行为（应然，待回放）：澄清刚才那句「早上好」。
- 会红的错误实现：宽泛召回 → 回旧事；只看字面 → 无所指。
- 观测字段：「？」是否被跳过/进入模型、最近一条消息是否随「？」一起进入编译输入、回复是否指向「早上好」。
- 区分力：需要区分「低信息跳过」与「送入模型但模型没接住」——观测字段必须包含 skip/flag，不能只看回复。

### C4 异步引用

- 输入：A3（回复→T+0 那条旧消息，问「这个后来怎么处理的？」）。
- 正确行为（应然，待回放）：展开被引用那条，回复针对「第三条」。
- 会红的错误实现：按气泡相邻硬接 → 回最近一句（T+1d 的「好，我记得是群聊引用那条」）。
- 观测字段：reply_to 解析结果、被引消息是否进入编译输入、回复提到哪一条需求。
- 区分力：强，前提是快照必须记录 reply_to 的解析结果，否则「展开对了但回复错」会被误判成「引用没解析」。

### C5 多候选

- 输入：A6（三个方案 A/B/C，问「上次那个方案，你觉得哪个最急？」）。
- 正确行为（应然，待回放）：识别歧义或指向最明确锚点。
- 会红的错误实现：固定取最显著/最早 → 指错方案。
- 观测字段：召回候选列表及其排序、回复是否表达不确定或给出理由。
- 区分力：中——「表达不确定」与「随便选一个但说对了」在回复文本上可能相同，需候选列表才能区分。

### C6 群聊说话人

- 输入：A7（@Ling 的老周问评审材料，前面夹着午饭话题）。
- 正确行为（应然，待回放）：跟对 @Ling 的老周，不被午饭话题带走。
- 会红的错误实现：按最近气泡 → 回午饭。
- 观测字段：mentions 解析结果、当前说话人字段、进入编译输入的候选消息及说话人。
- 区分力：强，但必须用「@ 的不是最近发言者」这个形态（样本中老周确实是最后一条，所以还要确认午饭话题的干扰确实存在）。

### C7 换题抑制

- 输入：A8（「接着说前天那个方案」后紧跟「先别说那个，说今天的天气」）。
- 正确行为（应然，待回放）：停止旧话题续接。
- 会红的错误实现：仍接方案 → 误续。
- 观测字段：「先别说那个」是否进入编译输入、回复是否转向天气。
- 区分力：强，但要注意——两条消息相隔 5 秒，若系统按批合并可能只有一条进入模型，需要快照确认两条都在。

### C8 条件式时间

- 输入：A5 两位置（同一句「？」，位置一紧跟问候，位置二隔天无锚点）。
- 正确行为（应然，待回放）：位置不同解读不同；不能给固定秒数阈值。
- 会红的错误实现：固定时长阈值 → 一种位置必错。
- 观测字段：两位置分别的 skip/flag、最近消息是否随「？」入编译输入、回复分别指向什么。
- 区分力：这是唯一能区分「按间隔判断」与「按上下文锚点判断」的判据。若两位置行为相同，无论相同成什么样，都说明缺失上下文敏感性。

### 2.9 判据总表（应然，待回放）

| 判据 | 样本 | 应然结果（条件式，非期望值） | 会红的错误实现 | 观测字段 | 区分力 |
|---|---|---|---|---|---|
| C1 | A1 vs A2 | 有锚点时旧方案进编译输入并接方案；无锚点时不续 | 最近一轮截断 / 召回全关 | 旧方案片段、召回来源、回复内容 | 中，需两阶段快照 |
| C2 | A2/A4 | 问候只触发问候回应，不列四项旧需求 | 宽泛语义召回 | 召回条目数、旧方案占比、回复 | 强（需正反双断言） |
| C3 | A5 位置一 | 「？」澄清刚才那句 | 宽泛召回 / 字面无所指 | skip flag、最近消息、回复指向 | 中，需 skip 字段 |
| C4 | A3 | 针对被引的第三条 | 按气泡相邻 | reply_to 解析、被引消息、回复 | 强 |
| C5 | A6 | 识别歧义或最明确锚点 | 固定取最早/最显著 | 候选排序、回复确定性 | 中 |
| C6 | A7 | 跟 @Ling 的老周 | 按最近气泡 | mentions、说话人、候选消息 | 强 |
| C7 | A8 | 停止旧话题续接 | 仍续方案 | 换题句是否入输入、回复主题 | 强 |
| C8 | A5 两位置 | 位置不同解读不同 | 固定时长阈值 | 两位置 skip/flag、回复 | 核心区分力判据 |

### 3.0 静态证据文件与 sha256

读代码前/后 sha256 一致（同一时刻采集，未改动任何被测文件）：

```text
b03a4dcb40467e7ac926948f887ca067de8a8dce4860e7f9bc5737d1874886fe  src/ling/domains/memory/recall_query.py
303a1b4609843ee4afc23345706dbf0292388ef45731b9447aea6a93bfc293a7  src/ling/domains/memory/service.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
```

### 3.1 反例线索的三条消息是三次独立编译

静态预测（未跑真实模型）：bridge.py 对每条入站消息各建一次 turn，各调一次 `_run_turn`（bridge.py:863-877），后者把 `text` 原样作为 `user_text` 交给 `coordinator.run_turn`（bridge.py:1818-1833）。compile 签名收 `user_text: str`（context.py:548-566）。所以「你还记得前天聊了什么吗」「早上好」「？」是**三次独立的 compile 调用**，不是一次拼三条。

这意味着：不能把「早上好」的回复表现当成「？」的输入事实，也不能把前一条的召回状态继承给后一条——每条都重新走 context.py:1549-1575 的准入三分支。样本文件 A1 的第 40 行已经提醒过这一点。

### 3.2 逐消息的静态预测链

以下均为**静态预测**，依据是代码常量和分支，不是模型输出。行号是本时刻快照（sha256 见 §3.0），若被测文件被协作方改动，行号会漂。

#### 消息 1：「你还记得前天聊了什么吗」

静态预测链：

1. bridge.py:863-877 → `_run_turn(text=...)` → bridge.py:1818-1833 → `compile(user_text=...)`。
2. context.py:1550 `snapshot.allows_long_term_memory()` 为真时进入召回判断。
3. context.py:1552 `should_skip_automatic_recall("你还记得前天聊了什么吗")`：recall_query.py:21 剥标点后 `casefold`，整条不在 `_STANDALONE_REPLIES`（recall_query.py:6-11）→ **不跳过**。
4. context.py:1557 `needs_recent_topic`：recall_query.py:13-16 的 `_TOPIC_FOLLOWUP` 不含此句 → False，`query` 保持原文。
5. context.py:1567-1575 → `self._memory.recall(query=...)`。service.py:665 的 `_EXPLICIT_RECALL`（service.py:116，含「记得」）命中 → `explicit_recall=True` → `_query_topics(explicit=True)`；service.py:697 `wants_history(query)` 为 False（「前天」不在 service.py:1814 的 marker 表）→ `include_history=False`。
6. 召回到什么：service.py:675-679 先按 `updated_at` 取最近候选，service.py:681-704 在候选满且 topic_tokens 非空时做关键词补捞，service.py:748 起 BM25 + episode 路 + RRF/MMR（service.py:966）。**具体命中哪些记忆行取决于库内容与是否有 embedding，代码无法确定 → 待回放**。
7. context.py:126-137 同窗口去重：若命中项的 `turn_id` 属于当前窗口且已在 `usable` 原文里，则跳过进 evidence。context.py:145-147 受 `AUTOMATIC_MEMORY_TOKEN_BUDGET`（context.py:270，按 `_SCALE` 缩放）和 `_MAX_AUTOMATIC_MEMORIES` 限制。
8. context.py:1588-1601 有 evidence_text 时插入 `lane="evidence"` 车道。

静态预测结论：这条消息**会触发自动记忆召回**（`explicit_recall=True`），旧方案有机会进编译输入；但能否真的列出四项，取决于记忆行内容与窗口去重，不能从代码断言。

#### 消息 2：「早上好」

静态预测链：

1. 同样三次独立编译中的第二次，`user_text="早上好"`。
2. context.py:1552 → recall_query.py:21 剥标点后「早上好」不在 `_STANDALONE_REPLIES`（集合里有「你好」但**没有**「早上好」）→ **不跳过**。这一点是关键：问候不在跳过名单里。
3. `needs_recent_topic("早上好")` = False；`wants_history("早上好")` = False；`_EXPLICIT_RECALL.search("早上好")` = False。所以走普通召回，`explicit_recall=False`（service.py:665-668）。
4. 静态预测：`_query_topics(query, explicit=False)` 下没有显式锚点，BM25 用「早上好」这类词几乎不命中旧方案文本；但 service.py:675-679 仍按 `updated_at.desc()` 无条件取最近候选（RECALL_CANDIDATE_LIMIT 条），RRF 的新近度路可能把最近的旧方案记忆排进来。是否足以让 evidence 出现四项，**代码无法确定 → 待回放**。
5. 编译后的 `compiled_meta` 不会有 `memory_recall_skipped`（只有跳过时写 context.py:1554）；若有命中会有 `evidence_selected` / `evidence_dropped`（context.py:1654-1655）。

静态预测结论：这条消息**会触发自动记忆召回**（与「早上好」被当作低信息跳过的直觉相反），但是否列旧事要看检索结果。C2 的「宽泛召回 → 列四项旧需求」错误实现，在这条链上是有可能发生的路径，不是被代码堵死的。

#### 消息 3：「？」

静态预测链：

1. 第三次独立编译，`user_text="？"`。
2. context.py:1552 → recall_query.py:21：`normalize("NFKC","？").strip().casefold().rstrip(" \t\r\n.!?。~")`。NFKC 把全角「？」归一为 ASCII `?`，随后被 `rstrip` 剥掉 → `plain = ""`。
3. `""` 在 `_STANDALONE_REPLIES`（recall_query.py:7）→ `should_skip_automatic_recall("？") = True`。
4. context.py:1553 `recall = RecallResult()`（空），**不调用 `self._memory.recall`**；context.py:1554 写 `compiled_meta["memory_recall_skipped"] = "low_information"`。
5. 因为召回结果为空，context.py:1585 不 mark_recalled，context.py:1588 不插 evidence，context.py:1653-1655 不写 evidence_selected/evidence_dropped。

静态预测结论：**「？」确定命中 low-information 跳过，零自动召回**。这是从代码常量可推的，不依赖模型。但要区分两件事：跳过「自动记忆召回」不等于跳过「这次 turn」——「？」仍会继续编译，最近原文窗口（context.py:1460-1529 的 `usable`）和摘要仍可能把它前面的「早上好」带进提示词。所以 C3「澄清刚才那句」有可能成立，靠的是**历史窗口**而不是记忆召回。

### 3.3 摘要是否常驻

静态预测（context.py:1673-1685）：摘要注入有一个硬条件 `window_ctx is not None and window_ctx.summary`。所以摘要**不是无条件常驻**——它在滚动摘要已经生成并写回 WindowContext 之后才进编译输入（写入点在 context.py:1979-2021 的 `_rollup_summary`，触发点在 context.py:1791-1802 的溢出压缩）。「四项需求」若还都在最近原文窗口里，摘要可能还是空的；此时它们靠原文窗口而非摘要进提示词。

与摘要不同，**核心记忆是每轮常驻**的（context.py:773-799，`CoreMemoryService.render`），但只在 sealed 模式不注入（context.py:781-789）。核心记忆和「四项需求」不是一回事，不要混为一谈。

静态预测总结（反例线索）：先看最近的原文里有什么、摘要是否已生成，再看消息 1/2 的召回结果；消息 3 的「？」不会从记忆系统拿到任何东西。若实际回放里「？后列四项产品需求」，按静态预测最可能的来源是**消息 1/2 已注入的 evidence 或最近原文/摘要在后续轮次仍可见**，而不是「？」这一轮触发了新召回。这是推理，不是实测，需 prompt snapshot 证实。

### 3.4 C1–C8 的当前代码静态预测（逐条）

每条都先说「输入与当前路径」，再说「验证字段」，再说「会红的错误实现」与「区分力」。全部是静态预测。

C1（A1 显式找回）：输入「接着说前天那个方案」不在 `_STANDALONE_REPLIES`，`needs_recent_topic` 也不命中；`_EXPLICIT_RECALL` 不含「前天/接着说」，但「方案」可能经 BM25 命中旧记忆。静态预测：会走正常召回，旧方案有机会进 evidence；不保证一定进（取决于 BM25/向量命中）。验证字段：`compiled messages` 是否含旧方案片段、`evidence_selected`、`memory_recall_skipped` 是否缺失。会红的实现：最近一轮截断、无召回。**区分力**：当前判据无法区分「召回失败」与「召回成功但被预算裁掉」——需 §4 的两个阶段快照。

C2（A2/A4 问候不误召回）：「早上好」不在跳过集合，静态预测会走普通召回；是否列四项取决于 service.py:675-704 的候选与排序。**这是当前基线最可疑的一条**：代码没有把问候排除在召回之外。验证字段：三次编译的 `evidence_selected` 与回复。会红的实现：宽泛召回列四项。**区分力**：强，但必须用「回复只接问候」做正向断言配对，否则「没列四项」也可能只是召回为空。

C3（A5 位置一）：见 §3.2 消息 3。「？」确定跳过自动召回；是否澄清「早上好」靠历史窗口。验证字段：`memory_recall_skipped == "low_information"`、最近原文里是否有「早上好」、回复指向。会红的实现：宽泛召回旧事、字面无所指。**区分力**：中，必须抓 skip 字段才能区分「跳过召回」与「召回但模型没接住」。

C4（A3 异步引用）：静态预测——reply_to 在 bridge 入站解析（bridge.py:844 记录 platform id），但 `compile` 只收 `user_text` 一个字符串（context.py:553）；被引消息是否进入上下文，取决于入站层是否已把引用展开进 `text`。**从代码无法确定展开规则 → 待回放**。验证字段：compile 的 `user_text` 原文（是否含被引内容）、`current_message_platform_id`、回复提到哪条。会红的实现：按气泡相邻。**区分力**：强，但必须先确认 `user_text` 是否已被展开，否则无法把失败归因到召回层。

C5（A6 多候选）：「上次那个方案」含「上次」→ `_EXPLICIT_RECALL` 命中（service.py:116）→ explicit_recall=True。静态预测：会召回，候选排序按 RRF/MMR（service.py:966），非固定取最早。验证字段：召回候选列表与排序、回复的确定性。会红的实现：固定取最早/最显著。**区分力**：中——回复文本可能掩盖排序差异，需候选列表快照。

C6（A7 群聊说话人）：`compile` 有 `is_group` / `user_sender_name` 参数（context.py:554,561），bridge.py:875 从 envelope 透传 sender_name；context.py:1687-1699 给群聊历史消息标说话人。静态预测：说话人信息会进提示词；但「@Ling」的 mentions 是否影响召回查询，**从当前读到的代码段无法确定 → 待回放**。验证字段：`is_group`、`user_sender_name`、编译消息里的署名、回复指向。会红的实现：按最近气泡。**区分力**：样本里老周正好是最后一条，需确认午饭话题确实构成干扰。

C7（A8 换题抑制）：两条消息相隔 5 秒，是两次独立编译（§3.1）。静态预测：「先别说那个」不含跳过词、不含显式召回词，走普通召回；是否续旧话题取决于最近原文和 evidence。验证字段：两次编译各自的 `user_text` 与回复。会红的实现：仍续方案。**区分力**：强，但必须确认两条消息都各自建了 turn。

C8（A5 两位置）：位置一「？」跳过召回（§3.2）；位置二隔天的「？」同样跳过——`should_skip_automatic_recall` 只看整条文本，不看间隔。静态预测：**两位置在「自动召回」这一层行为相同（都跳过），差异只能来自历史窗口里最近一条是否为「早上好」**。验证字段：两位置的 `memory_recall_skipped`、最近原文、回复。会红的实现：固定时长阈值决定跳过。**区分力**：这是关键——当前代码的 skip 判定完全不读时间，C8 能区分「按锚点」与「按时长」，但当前实现两者都不是，它按「文本形态」跳过。回放时若两位置回复相同，说明连历史窗口的差异也没起作用，需单独记录。

## (4) 需要抓的 prompt snapshot 字段清单

> 目的：让 §2 每条判据都能落地，并区分「没召回」「召回到但被裁掉」「模型没接住」三种失败。
> 抓不到这些字段时，判据只能写「无法证明」，不得写成通过。

编译阶段（每次 compile 一份，三条消息各一份）：

- `user_text` 原文（compile 的入参，context.py:553）——区分「消息未展开引用」与「召回没做对」。
- `compiled_meta["memory_recall_skipped"]`（context.py:1554），取值缺失或 `"low_information"`。
- 是否调用了 `self._memory.recall`（context.py:1567）——可用 spy/日志；这是区分「跳过」与「召回为空」的唯一直接证据。
- `compiled_meta["evidence_selected"]` / `["evidence_dropped"]`（context.py:1654-1655）。
- `compiled_meta["audience"]`（context.py:770），用于确认可见性门。
- `compiled_meta["core_memory_items"]`（context.py:804），确认核心块是否注入。
- `compiled_meta["history_cap_hit"]`（context.py:1547），确认最近原文是否被读取上限截断。
- `compiled_meta["rolled_up"]`（context.py:1805），确认摘要是否在本轮更新。
- `window_ctx.summary` 是否非空、`summary_message_count`、`summary_through_sequence`（context.py:1674-1684）。

消息与编译结果：

- 完整 `messages`（每条的 role/key/lane/content），尤其是 `lane="evidence"`、`lane="summary"`、`lane="history"`、`lane="input"`。
- 最近原文窗口的条目数与每条的 `sender_subject_id` / `turn_id` / `message_id`（context.py:1460-1529 的 `usable`）。
- `reply_to` 解析结果、`current_message_platform_id`（bridge.py:1797,1832）。
- `is_group` 与 `user_sender_name`（context.py:554,561）。

生成阶段：

- 实际发给模型的完整 messages（不能只用 `compiled` 对象代替）。
- 模型原文回复、`source` 标记 `REAL_MODEL` 或 `SCRIPTED`。
- 样本要求的 `platform_message_id`（假值）、`sender_subject_id`、`text`、`reply_to`、`mentions`、`created_at`。

## (5) 明确未取得数据

- **真实模型输入与回复**：本文未调用任何模型，没有一条 A1–A8 的 REAL_MODEL 回放记录。§2 表格里的「应然结果」全部是判据，不是通过记录。
- **脚本模拟边界**：本文的「静态预测」只覆盖代码常量与分支可直接推出的部分——`should_skip_automatic_recall` 对「？」的命中、`_STANDALONE_REPLIES` 不含「早上好」、`_EXPLICIT_RECALL` 含「记得」、`wants_history` 不含「前天」、摘要注入条件、核心记忆常驻。凡涉及「具体召回到哪些记忆」「回复会写什么」「RRF/MMR 的实际排序」「引用是否被展开」的结论，都标了待回放，本文没有能力从代码单独确定。
- **半成品变异验证**：未构造「只做 A / 只做 B」的半成品实现，未跑任何测试，因此不能声称任何判据已被变异证明有效。§2 的「会红的错误实现」是推理出的反例，不是实测的红。
- **工作树状态**：开始时观测到的 8 个 untracked 项非本文创建；本文只新增 `docs/evidence/ling-context-baseline.md`，未改任何业务代码、未 `git add`、未提交。
