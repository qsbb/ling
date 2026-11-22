# 阶段 2 对抗验收报告（独立验收方）

> **写作时刻**：2026-11-22T10:01+08:00（Asia/Shanghai）
> **判据先行**：本报告的所有判据写于 `ADVERSARIAL-PHASE2-CRITERIA.md`（写作时刻 09:52），
> 且在该文件落盘**之后**才读取实现/结果。审查期间未改 replay/、options/、src/、任何业务代码。
> **模型渠道**：provider=nasfree, model=hy4flash（按任务硬性要求）。

## 0. 观测对象钉住（原始输出抄录）

**写作时刻基线确认**：`git log -1` → `de43cbe79a78e0c01f7821470500eaa04ea79497 2026-11-22T09:17:29+08:00 feat(live): 合入直播工作台与舞台控制`；`git rev-parse HEAD` 同值。与任务声明的 HEAD 一致。（实测，一次观测）

**写作时刻 `git status --short`**（抄录，节选）：`?? docs/evidence/ling-context-*.md`（多个）、`?? docs/evidence/ling-context-research/`、`?? docs/research/`。无跟踪文件 M 状态。（实测）

**被测文件 sha256（写作时刻 09:50，审查结束 09:58 复查一致）**：

| 文件 | sha256 | 复查 |
|---|---|---|
| replay/out-baseline.jsonl | `9dc9ea3a514d6811e55387c2bb65a7f9f56d730f2356b8b6580112784082d39c` | 一致 |
| replay/out-contrast.txt | `bad11c6fadf4e42597cffe79952929abf17501ccc1436a607f0d216ba1ef60de` | 一致 |
| replay/out-variants.jsonl | `1634fdcebee89e97d3c1bac943c2624b44c3d9a364a77c325afa2487163f77ad` | 一致 |
| replay/replay_01.py | `4c01807e1196178484b9fac9bb2ac5ed61ca723d58d5a328acd669a3598be419` | 一致 |
| replay/02-contrast.py | `57b8a493037667c81ebec0872330b7ea6d084f09923a5c7985907f16d250f092` | 一致 |
| replay/03-variants.py | `7fcb852f0f1bf59fed170bbef0198db5ba056eadbe1661f1512c2fea2da96ecf` | 一致 |
| options/00-criteria.md | `bab8c321819afc41f88b944589ac3c270fe1894a0b8bd55a3fb73f0c0264af93` | 一致 |
| replay/01-samples.md | `ec0aa1201f0fdb7ad830fe67b38d7c2cdf8cfd3f36ef76d4aa3ea979ac0bd3be` | 一致 |
| options/02-options.md | `3a997335eda8b2f3ce9996cc9f6d8dadf56f7c375efa27595b15de109658c72d` | **复查时变为 `234e795a…`** |

**冻结结论**：8/9 文件字节级冻结成立。options/02 在我审查期间被另一个 agent 改动（追加了一行「独立对抗验收：派生 subagent（nasfree/hy4flash），产物在 review/ADVERSARIAL-PHASE2-*.md」，即对我的引用）。改动区域已定位，不涉及判据/变异逻辑，不影响本报告结论；但按 AGENTS.md「观测期间文件冻结」须如实记录——本次审查对象在 options/02 上不是同一个字节版本。（实测）

**报告阅读约定**：置信度三档——**实测**（我核对了文件字节/字段，可复查）、**推理**（有依据、可被反驳，但未做实验）、**转述**（引用他人文档，未独立核实）。加粗与表格只用于实测；推理与转述一律普通段落并显式标注。

---

## 1. 总裁决（先给结论）

- 「无真实模型输出」纪律：out-* 三份产物的 `source` 字段全部是 `COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT`，脚本无 API 调用，只调 `ContextCompiler.compile()`。**实测遵守**，这一点不属于假绿。但是「遵守纪律」本身不等于「结论被验证」——B 方案与 C 方案的有效性依然全部 UNKNOWN。
- 半成品变异「两半各自红」的核心声称：**实测不成立/部分不成立**。B-half-1 的红是「期望值设置自相矛盾」的产物；B-half-2 的红混合了一条正确证据和一条被设计者自己承认「期望值写错」的哨兵 FAIL。C 的两半是标签 PASS，不能作为变异证据。详见 §4。
- 2×2×2 对照：因子 A 与 C 在**测量层面**重叠（C 注入的文本就是 A 用来定义「旧事」的同一字符串），但设计者已在 BASELINE-REPORT.md §3 显式声明并给出反证（history 计数不变），因此**这是可接受的测量重叠而非隐蔽混淆**；报告原文的「不是因子混淆」结论偏强，改为「测量指标重叠已显式披露、其解释有 n_history 支撑」更准确。详见 §5。
- M-11 条件式记录：长度有实测数值但**写法是复述式**（「253–293 字符」是抄自运行结果，不是条件式）；M-12 调用数没有进入任何 out-* 产物字段，只在报告里以「脚本结构」文字声称。**两项都未达到 AGENTS.md 的判据门槛**。详见 §6。
- 假绿/悬空/推测写成事实：发现 1 处命名过强（`V_M2_or_M6_input_proxy` 塞入与 M6 无关的 case，使 M6 语义被稀释）、1 处结论过强（§3 标题「两个半实现都红了真正的不变量」），以及 2 处口径细节待统一（见 §7）。

---

## 2. P2-0 观测对象与冻结：能证明什么/不能证明什么

**实测**：8/9 文件哈希前后一致，HEAD 匹配声明。options/02 在审查期间被改动（见 §0），已定位改动内容。

**不能证明**：无法从一次观测证明「所有被测文件在整个阶段2执行期间都没被改过」——我只在审查开始和结束时各取一次哈希，中间时刻没有连续监控。报告里的「8/9 一致」是两次观测的结论，不是全程保证（实测的边界）。

---

## 3. P2-1 / P2-2 真实输出纪律与基线快照

### 3.1 无真实模型输出（实测，判据通过）

**能证明什么**：
- out-baseline.jsonl 6 条、out-contrast.txt 8 条 JSONL、out-variants.jsonl 12 条，每一条都带 `"source": "COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT"`（实测，逐条核对）。
- 三个脚本中 `replay_01.py` 只调 `ContextCompiler.compile()` 并导出 annotated；`02-contrast.py` 与 `03-variants.py` 都 import `replay_01.run_case`，没有任何 HTTP 客户端、模型 SDK 或 API key 读取（实测，全文扫描 `openai|anthropic|api_key|httpx|aiohttp|requests.` 等模式，零命中）。
- annotated 中的文本全部来自样本输入（user_text / history），没有可辨认的「模型回复」字段（实测）。
- BASELINE-REPORT.md 明确写「真实模型输出：未取得」「脚本模拟回复：本轮未生成」，VARIANTS-REPORT.md 写「回复侧 UNKNOWN」（转述，未独立核实环境是否真的无 API key；我只核实了文件本身的自述与脚本行为）。

**不能证明什么**：
- 无法从文件证明「环境真的没有 API key」——这是 BASELINE-REPORT 的转述，我未执行 `env | grep -i`。即便有 key，脚本也没调用，因此「未取得」在脚本层面成立；但「无法取得」这个更强的说法没有证据。
- 纪律遵守不等于验证：annotated 里有旧事片段 ≠ 模型会误提旧事。所有「A 方案基线 X 条红/绿」只描述输入侧，不描述行为。

**置信度**：纪律本身——实测；环境无 key——转述（来自 BASELINE-REPORT，未核实）；「无 API 故无法取得」——推理（可被反驳：若装了 key 仍可取，只是本轮未取）。

### 3.2 compiled.annotated 快照可追溯性（实测，判据通过）

**能证明什么**：
- out-baseline.jsonl 确有 6 条，case 名为 S01/S05/S06/S07/S08/S13，与 BASELINE-REPORT §1.5 的显式映射表一一对应（实测）。
- 每条 annotated 含 persona/constraints/world/relationship/capabilities 5 个 system lane + history/input；lane 分布与 replay_01.py 的 `lane_counts()` 及样本输入一致，可由脚本+样本重算（实测，逐条比对字符数与 text）。
- 报告没有声称「13 条全跑」，明确写了只取 6 条简化历史（转述 + 实测交叉：报告文字与文件条数一致）。

**不能证明什么**：
- 这 6 条只是 13 条开放样本的子集，且历史被简化（如 S05 的隔夜语义、S06 的「十分钟到」时间信息在 annotated 里都丢失了「几秒/隔夜」标记）。因此快照**不能证明**样本文件里那些依赖时间距离的判据（M-2 隔天 vs 紧邻、M-7 时间相邻）被真正覆盖——被喂给编译器的只是文本，时间信息在 `seed()` 里只作为 sequence 序号存在，不进 annotated。
- 快照里所有样本 `evidence=0, summary=0`（实测）。这证明隔离库无 MemoryRow/摘要，因此 M-1/M-6 中「从摘要/记忆拉回旧事」这条错误实现路径**在本回放中根本无法触发**——即这些判据在本次回放里没有区分力（推理，依据：annotated 字段 + 报告 §2 观测1）。

---

## 4. P2-3 半成品变异：核心裁决

这一节是本次验收最重要的部分。先复述判据 P2-3 的条件式：只有「只做 A 半」与「只做 B 半」在**同一条判定逻辑**下各自触红、完整实现 PASS，才算「两半各自红」；若 FAIL/PASS 由手写的 `want_old` 期望值决定而非被测行为，则是标签游戏。

### 4.1 B-half-1：「红」是期望值自相矛盾的产物，不是区分力证据（实测）

**观察**：out-variants.jsonl 第 4 行 `B_half1_greeting_no_anchor`：`old_in_annotated=true, want_old=false → FAIL`。第 5 行 `B_half1_q_immediate`：`old_in_annotated=false, want_old=false → PASS`。第 6 行 `explicit_anchor`：`true==true → PASS`。

**能证明什么**：B-half-1 的 monkeypatch（`fetch_anchor_only`）在 `anchor=""` 时返回原始 rows，无锚点时不过滤——这是代码事实（实测，03-variants.py:79）。所以「无锚点问候仍含旧事」是这个半实现的**预期行为**，判据把它标红只是因为 `want_old` 被手动设为 false。

**不能证明什么 / 问题**：这条 FAIL 与 A baseline 第 1 行（`old=true, want=false → FAIL`）**完全同值**。也就是说，B-half-1 与「什么都没改」在 V-M2 上给出相同结果——按 AGENTS.md「正确实现和错误实现在该输入上给出相同结果，那个输入就证明不了任何事」，B-half-1 的红证明的是「这个半实现没修好 V-M2」，而不是「判据 V-M2 能区分半实现与完整实现」。两者本来就该都红，因为**完整 B 从未被构造**（VARIANTS-REPORT §4 明说「未构造完整 B」）。没有「完整实现 PASS」这一端点，就不满足 P2-3 的条件式。

**置信度**：实测（字段与代码路径直接比对）。

### 4.2 B-half-2：混合证据——一半有效，一半是设计者自认的期望值错误（实测）

**观察**：第 7 行 `B_half2_signal_greeting_no_anchor`：`old=true, want=false → FAIL`，同时 `skip_flag=true`（A baseline 第 1 行同 case 为 `skip_flag=false`）。第 8 行 `B_half2_signal_q_immediate`：`old=false, want=true → FAIL`（与 A baseline 第 2 行同值）。

**能证明什么**：第 7 行是真实有效的区分力证据——同一 case 下，skip 信号从 false 变 true，而旧事仍在输入、V-M2 仍 FAIL。这满足 AGENTS.md「同一次变异里看新旧测试的相反反应」的精神：信号层变化与不变量未变化同时被观测到（实测）。这条支持「判据没有被『加个 skip 词』骗过」。

**不能证明什么 / 问题**：
- 第 8 行的 FAIL 是设计者自己在 VARIANTS-REPORT §2 注里承认的「期望值写错」——该 case 历史只有「我十分钟到」，根本没有旧事片段，`want_old=true` 是刻意设的哨兵，与信号变异无关。报告说「不拿它当变异证据」，但**它仍然留在 out-variants.jsonl 的 12 条里**，且 §3.2 的论证把它和有效证据并列陈述。结论「B-half-2 红」在逐条数据上是 1 条有效 + 1 条自认无效。
- B-half-2 的 `want_skip` 在 run_matrix 的 tuple 里对 q_immediate 设为 True，但 judge() 中 B-half-2 走的是 `run_matrix("B_half2_signal")`，即 `want_skip=True`，导致 V_M7 全 PASS——这是正确行为，但意味着 M7 判据在这个变异里**无法提供区分力**（信号变化不影响它）。

**置信度**：实测（数据字段 + 代码 tuple）。

### 4.3 C-half-1 / C-half-2：标签 PASS，不是变异证据（实测）

**观察**：第 10–12 行，C 的三个 case 全部 `V_M2_or_M6_input_proxy=PASS`，`want_old=true`（硬编码在 03-variants.py:107-108,129），`old_in_annotated=true` 是变异前就成立的既有事实。C-half-1 只给 history 文本加 `[较早话题]` 前缀，C-half-2 只反转 annotated 顺序——两者都不改变「旧事在输入」这个被测量的量。

**能证明什么**：证明了「加标签/重排不改变旧事片段是否在输入里」这一输入侧事实（实测）。

**不能证明什么**：`PASS` 这个颜色完全由 `want_old=true`（手动设定）决定；去掉变异代码，A baseline 的 explicit_anchor case 也是同样的 `true==true → PASS`。按 P2-3 的条件式，这两半**没有红**，因此不满足「半成品必须红」的硬要求。VARIANTS-REPORT §3.3 诚实地承认 C 的输入侧无法证明有效性（转述，与实测数据一致），但 out-variants.jsonl 里的 `PASS` 标签本身会被不知情的读者误读成「C 半实现通过验收」——这是**格式即置信度声明**的反例：一个 `PASS` 字符串承载了它证明不了的含义。

**置信度**：实测。

### 4.4 命名过强：`V_M2_or_M6_input_proxy` 稀释了 M6（实测）

`judge()`（03-variants.py:29-42）把三种不同 case（greeting_no_anchor / q_immediate / explicit_anchor）的结果都写进同一个字段 `V_M2_or_M6_input_proxy`。其中 q_immediate 的历史是「我十分钟到」，与 M2（无锚点误提旧事）和 M6（异步引用找回）都不对应；把它算进「M2 或 M6」使这两个判据的语义被稀释。VARIANTS-REPORT §1 把 V-M6 定义为「显式找回锚点存在时，旧事应进 annotated」，这实际上只有 explicit_anchor 一条 case 覆盖，而它恰好在 A/B-half-1/B-half-2 下都 PASS（即没有变异让它红）。**结论：M6 的输入侧代理从未被任何变异触红**。（实测，字段级核对）

### 4.5 变异收尾纪律（转述 + 实测边界）

VARIANTS-REPORT §5 声称 monkeypatch 在 finally 恢复、schema 在 finally drop、`context.py` sha256 前后一致 `ffb573b4…`、磁盘源码零改动。（转述，未独立核实：我没有重跑变异，也没在变异前后取 context.py 哈希；只核实了磁盘当前 `git status` 里 src/ 无改动——实测，一次观测。）按 AGENTS.md「「我恢复干净了」是一句有保质期的话」，这份声明只对报告写作时刻有效。

---

## 5. P2-4 2×2×2 因子独立性

**能证明什么（实测）**：
- 8 格齐全，A/B/C 标签与 `02-contrast.py` 的三重循环一致（实测）。
- 历史条数只随 A 变（A=1 → n_history=2；A=0 → n_history=1），与 B/C 无关；这证明**历史窗口侧**确实只被 A 因子控制（实测，对照 out-contrast.txt 的 history 列）。
- 长度随 C（引用注入）增加约 +36~45 字符、随 B（锚点句比「早上好」长）增加约 +36 字符（实测的算术：A0B0C0=258 → A0B0C1=294 为 +36；A0B0C0=258 → A0B1C0=271 为 +13，注意原文 §3 说「锚点句 +36」与实际 +13 不符——见 §7）。

**不能证明什么 / 混淆分析**：
- **测量层面的 A/C 重叠确实存在**：`02-contrast.py:19` 的 `OLD = "上次那个产品方案你再发我一份"`，既是 A 因子塞进 history 的文本，也是 C 因子注入 user_text 的文本，还是 `has_old()` 用来判定 `old_fragment_in_annotated` 的子串。因此 A=0,C=1 格子的 `old_in_annotated=true` 来自 user_text——**它让「旧事进输入」这个观测字段无法区分旧事来自历史还是引用**。这与我在 P2-4 里写的「改 C 时 A 字段联动变化」形状一致，只是这里 A 字段没被改，而是**测量字段被两个因子共同决定**。
- 设计者已在 BASELINE-REPORT §3「混淆检查」显式披露这一点，并用 `n_history=1` 论证历史侧没变——这个反证是有效的（实测：A0B1C1 / A0B0C1 的 history 确为 1）。因此我判为**已披露的重叠，不是隐蔽混淆**；但原文「这不是因子混淆」的措辞过强，应改为「该观测字段由 A 与 C 共同决定；历史侧独立性由 n_history 单独支撑」（推理，依据：脚本常量 + 数据列）。
- B 因子在输入侧对 `old_in_annotated` 无差异（A=1 时 B 有/无都 true；A=0,C=0 时都 false），这证明「用户有没有锚点，取数都一样」——是 A 方案的结构性事实，不是模型行为（实测 + 推理）。
- 所有格子 `skip` 为空、`evidence=0`、`summary=0`——本轮 user_text 都不是 `？`，所以 M7 门控在这 8 格里从未被触发（实测）。

**置信度**：因子重叠——实测；「可接受」的裁决——推理（可被反驳：若读者只拿 `old_in_annotated` 单列做因果推断就会错，所以报告必须附 n_history 一起读）。

---

## 6. P2-5 M-11 / M-12 条件式记录

**M-11（长度）——实测未达判据门槛**：
- out-* 产物里有 `annotated_chars` 字段，数值 253–316（实测）。
- 但 BASELINE-REPORT §2 把它写成「输入长度 253–293 字符（判据 M-11 的实测口径）」，这是从运行结果抄来的**复述式数字**，不是条件式结论；AGENTS.md 明确禁止「预期 8.7 秒」式写法，而条件式写法应是「若长度增加而回复无改善 → 噪声；若长度不变而区分测试变好 → 改进在选数」。报告 §3 做了部分条件式读法（「C 注入约 +30~45 字符」），但 M-11 本身没有被改写成条件式。
- 口径问题：`annotated_chars` 是 `sum(len(m.content))`（字符数，replay_01.py:109），不是 token 数；00-criteria.md M-11 说的是「实际模型输入 token 数」和「annotated 总字符数」两个口径混用。报告没有说明这个差异（实测）。
- **结论**：能证明「长度被记录了」；不能证明「M-11 被满足」——它只是复述了一个区间，没有条件式分支，也没有 token 口径。

**M-12（调用数）——实测未落地到产物**：
- 三份 out-* 的字段里没有任何 `n_calls` / `call_count` / `num_calls`（实测，grep 零命中）。
- BASELINE-REPORT §4 表格写「调用次数：每格 1 次 compile，0 次额外 LLM 调用」，证据栏是「脚本结构；M-12 口径」——即推理自代码结构，不是记录字段（转述 + 推理）。
- 00-criteria.md M-12 要求的是条件式（「若调用数 +1 但 M-1..M-10 没从红变绿 → 无收益」）。由于没有真实模型输出，M-1..M-10 的绿/红本就 UNKNOWN，M-12 的条件式无法被判定——这一点报告没有明说。
- **结论**：能证明「脚本只调用 compile 一次」这一结构事实；不能证明「M-12 被按条件式记录」——产物里没有调用数字段，且缺少判据所需的回复侧端点。

---

## 7. P2-6 悬空引用 / 假绿 / 推测写成事实

**悬空引用（实测）**：
- options/02 §6 引用的 `replay/BASELINE-REPORT.md`、`replay/VARIANTS-REPORT.md`、`source/D-context-assembly.md`、`docs/evidence/ling-context-im-private.md` 全部存在（实测，逐文件检查）。审查期间 BASELINE-REPORT.md 和 VARIANTS-REPORT.md 被创建（mtime 09:50），在我写判据（09:50:19）时它们尚不可读——即**判据确实先于这两份报告**，符合纪律。
- options/02 §6 引用 `ADVERSARIAL-PHASE2-*.md`（我的产物），在我审查期间由别人追加（见 §0）——这是「引用尚未完成的产物」的时序细节，最终文件会存在，不算悬空，但写作时应标「待产出」。

**假绿 / 格式过强（实测 + 推理）**：
1. VARIANTS-REPORT §3 标题「两个半实现都红了真正的不变量」——实测不成立：B-half-1 的红与 baseline 同值（§4.1），B-half-2 只有一条有效证据（§4.2），C 两半全 PASS（§4.3）。这个标题用**加粗小节标题**承载了过强结论，违反「格式是一种置信度声明」。
2. out-variants.jsonl 的 `PASS` 字符串（C 两半、B-half-1 q_immediate）承载的是「期望值被满足」，而不是「方案有效」；不知情的读者会把 `PASS` 读成验收通过（实测字段 + 推理）。
3. BASELINE-REPORT §3「锚点句比『早上好』长约 +36 字符」与实际数据不符：A0B0C0=258 → A0B1C0=271 是 +13 字符；+36 是 C 引用注入的增量（258→294）。这是**把两个因子的增量错配**（实测，算术核对）。
4. VARIANTS-REPORT §2 注承认 q_immediate 的 `want_old=true` 是「期望值写错」，但仍把该 FAIL 保留在产物里，且 §3.2 的论证没有明确把它剔除——属于「推测/错误写进数据，再靠报告文字打补丁」（实测）。
5. 00-criteria.md §3「无法证明」清单第 1 条「B 方案在真实模型上比 A 好」——VARIANTS-REPORT §4 正确保留为不能证明（转述 + 实测一致）。这一点做得对，不算假绿。

**推测写成事实（实测）**：
- BASELINE-REPORT §2 观测 1「旧事是否进输入只由最近原文窗口决定，不由摘要/召回决定」——证据是 evidence=0/summary=0（实测）。但这是**环境导致的空集合**（隔离库无 MemoryRow），不是 Ling 在真实数据下的行为；应标注「在本回放环境下」。原文用「这说明」的强措辞，属于把环境事实写成系统事实（推理，可被反驳：有 MemoryRow 时召回可能非空）。
- VARIANTS-REPORT §3.2「这是最强的一条证据……正是 AGENTS.md 要的『两个半成品都能绿 = 假绿』的反面实证」——定性偏强；它只证明了「一个信号变化没有骗过一条输入侧断言」，不涉及「两个半成品」的完整比较（§4）。

---

## 8. P2-7 变异式反驳（取反关键前提）

以下四条按判据 P2-7 执行，全部为只读推演，依据已在上文给出：

1. **取反「两半各自红」→ 假设 FAIL/PASS 由手写 `want_old` 查表决定**。
   原结论「B 两半各自红」部分成立（B-half-2 的一条有效证据），部分不成立（B-half-1 与 baseline 同值、B-half-2 另一条自认期望错、C 全 PASS）。取反后原结论**不稳健**（实测）。
2. **取反「因子独立」→ 假设 C 注入的 OLD 就是 A 的定义文本**。
   假设成立（脚本常量同一）。原结论「2×2×2 因子独立」在历史侧成立（n_history 只随 A 变），在 `old_in_annotated` 观测侧不成立（A、C 共同决定）。设计者已披露，故降级为「部分独立」（实测 + 推理）。
3. **取反「无真实模型输出」→ 假设输出含真实 API 文本**。
   假设被否：三份产物 `source` 字段一致、脚本无 API 调用（实测）。原纪律结论**稳健**。
4. **取反「annotated 可复查」→ 假设它来自静态复制而非编译**。
   假设被否：annotated 内容可由 replay_01.py + 样本重算，case/lane/字符数自洽（实测）。原结论**稳健**。

---

## 9. 对 lead / samples-options 的行动建议（按优先级）

1. **必改（samples-options）**：VARIANTS-REPORT §3 标题与结论降级——B 只证明「一条输入侧断言未被信号层骗过」；B-half-1 的红不是区分力证据；C 两半的 PASS 不是验收通过。产物里应给每条 FAIL/PASS 加 `reason` 字段，区分「行为触红」与「期望值自设」。
2. **必改（samples-options）**：把 q_immediate 那条 `want_old=true` 的哨兵从 out-variants.jsonl 剔除或改标 `INVALID_EXPECTATION`，不要混在 12 条 PASS/FAIL 统计里。
3. **必改（samples-options）**：M-11/M-12 改成条件式；M-12 至少在 out-* 产物里记一个 `n_calls` 字段（即使值为 1），并说明「无真实输出时 M-12 条件式无法判定」。
4. **建议（samples-options）**：BASELINE-REPORT §3 的「+36 字符」改 +13（B 因子）并区分 C 因子的 +36；§2 观测 1 加「在本隔离环境下」限定。
5. **建议（lead）**：若后续要证明 B/C 有效，必须构造完整 B（信号+取数+抑制）并取得真实模型输出，否则所有回复侧判据维持 UNKNOWN。
6. **无需改**：无真实模型输出纪律、基线 6 条快照的可复查性、变异进程内恢复机制——这三项实测过关。

---

## 10. 自检（实测，2026-11-22T10:00+08:00 一次观测）

- `git status --short`：我的写作用域内只新增了 `docs/evidence/ling-context-research/review/ADVERSARIAL-PHASE2-CRITERIA.md` 与本报告。其他未跟踪文件（研究文档、replay 产出）均非我写；`src/` 无改动。
- U+FFFD：用 Python 按码点扫描两个新文件，count=0（实测，输出见 §过程记录；判据文件曾含一处 shell 示例里的 U+FFFD 字面量，已于 09:58 改为文字描述后重查为 0）。
- 敏感串：用 `git grep -nIE` 扫敏感串模式（内网 IP / 真实 QQ 号 / 密钥前缀，此处不抄完整正则以免自检自命中），我新增的两个文件零命中（实测）。本文引用的 QQ 号均为样本文件里的 `1002003xx` 假号，地址为文档专用段 `192.0.2.x`。
- 提交纪律告警（实测）：检查 `git status` 时发现我的 `ADVERSARIAL-PHASE2-CRITERIA.md` 已显示为 `A`（已暂存）——这是别人的批量 `git add` 卷入的，不是我执行的 `git add`。按 AGENTS.md 必须报告而非当作「已提交好」。本报告（`ADVERSARIAL-PHASE2-REPORT.md`）仍是 `??` 未跟踪。我未执行任何 `git add` / `git stash`，也未触碰其他 agent 的文件。
- 变异期间文件冻结：我只读被测文件，未改 replay/、options/、src/（实测，git status 佐证）；options/02 被别人改动已如实记录于 §0。
- 未标 task 完成——按任务要求，task 状态由 samples-options 管理。
