# 阶段2基线报告：现状 A 的提示词装配回放（输入侧取证）

> 观测对象：HEAD=`de43cbe79a78e0c01f7821470500eaa04ea79497`。
> 钉住时刻：2026-11-22 09:41+08:00。
> 命令原始输出：`git log -1` → `de43cbe… feat(live): 合入直播工作台与舞台控制`；
> `shasum -a 256 src/ling/kernel/context.py` → `ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba`。
>
> **真实模型输出：未取得。** 环境无 `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY`
> 等（实测 `env | grep -i` 无命中），且本脚本只调用 `ContextCompiler.compile()` 导出
> 编译输入，**不调用任何模型**。所有 `out-*.jsonl/txt` 的 `source` 字段均为
> `COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT`。凡涉及"模型会怎么回"的判断一律标
> SIMULATED / UNKNOWN，不得当作真实输出。
>
> **隔离措施**：在测试库 `ling_test` 的独立 schema `so_replay` 里用
> `Base.metadata.create_all` 建当前模型对应的表，跑前 drop 重建、跑后 drop；
> 不碰测试库现有 schema 的数据，不连生产库/NAS，不改 `src/`。
> 测试库原 schema 缺 `messages.output_modality` 等新列（实测报错），故不能直接复用，
> 这是采用独立 schema 的原因。

---

## 1. 纯函数实测（不依赖 DB）

直接调用 `ling.domains.memory.recall_query` 的函数，2026-11-22 实测：

| 输入 | `should_skip_automatic_recall` | `needs_recent_topic` |
|---|---|---|
| `？` | **True** | False |
| `早上好` | **False** | False |
| `你好` | True | False |
| `后来呢？` | False | **True** |
| `嗯` / `hi` / `在吗` | True | False |
| `那证书呢` | False | False |
| `先别说那个` | False | False |

结论（实测）：`？` 经 NFKC + `rstrip` 后为空串，命中跳过集合 → **零自动召回**；
这推翻了 lead-notes 早期"？不跳过、触发宽泛召回"的推测。真正不跳过的是`早上好`。

---

## 1.5 样本 ID 映射（快照 ↔ 样本文件）

快照 case 名与 `01-samples.md` 的样本 ID 对应关系（**显式声明**，不是隐式一一对应）：

| 快照 case | 样本文件 ID | 说明 |
|---|---|---|
| S01_explicit_resume | S-01 | 明确找回；快照用简化历史（2 条） |
| S05_greeting | S-05 | 隔天问候；快照历史为同窗口旧方案（2 条） |
| S06_q_immediate | S-06 | 紧接问号；快照历史 1 条 |
| S07_q_stale | S-07 | 隔天问号；快照历史 1 条 |
| S08_multi_similar | S-08 | 多相似旧话题；快照历史 4 条 |
| S13_shift_then_q | S-13 | 换题后问号；快照历史 2 条 |

> 快照只取了样本文件 13 条开放样本中的 6 条，且历史被简化为最小可复现形态。
> 这不是"13 条全跑"，报告不声称全覆盖。命名差异已在此显式映射，避免
> "id 命名空间不同却声称一一对应"的假绿。

## 2. 基线 A · `compiled.annotated` 快照（6 条样本）

脚本：`replay_01.py`（基线）、输出 `out-baseline.jsonl`。每条记录含完整 annotated
（lane/role/key/chars/text 摘要）与 meta 精选字段。摘要表：

| 样本 | user_text | meta.skip | lanes | chars | 旧事片段在输入 |
|---|---|---|---|---|---|
| S01 | 明确找回"上次那个产品方案" | — | persona1/constraints1/world1/relationship1/capabilities1/history2/input1 | 288 | 是（history） |
| S05 | "早上好" | — | 同上，history2 | 274 | 是（history） |
| S06 | "？"（紧接"我十分钟到"） | low_information | history1/input1 | 255 | 是（最近一句） |
| S07 | "？"（上一句"明天见"） | low_information | history1/input1 | 253 | 是（最近一句） |
| S08 | "B 方案成本表" | — | history4/input1 | 293 | 是（4 条全进） |
| S13 | "？"（历史含"先别说那个"） | low_information | history2/input1 | 262 | 是 |

观测（实测）：
1. 所有样本 **evidence=0、summary=0**——因为隔离库里没有 MemoryRow，且摘要还没生成。
   **在本回放环境和这 6 个样本中**，旧事是否进输入只由最近原文窗口决定；这不证明存在
   MemoryRow 或已生成摘要时的系统行为。
2. `？` 样本一致带 `memory_recall_skipped=low_information`，零 evidence。
3. 历史窗口按 sequence **全量**进 annotated（S08 四条都在），无 topic 相关性过滤。
4. 输入长度：全量 `annotated` 字符和，实测落在 253–293（基线 6 条）。这是**单次运行的
   观测值，不是判据阈值**——按 M-11 写成条件式才有信息量：若 B 方案字符和上升但回复
   无改善→说明增量是噪声；若字符和不变而区分测试变好→说明改进在选数而非堆数。
   口径是字符数，**不是 token 数**；两者不可混用（AGENTS.md M-11）。

**不能证明**：以上只是"模型看到什么"，不是"模型如何回应"。无真实模型输出，
S01 是否真的续接旧事、S05 是否误提旧事、S06 是否澄清上一句——全部 UNKNOWN。

---

## 3. 2×2×2 输入侧对照（8 格）

脚本：`02-contrast.py`，输出 `out-contrast.txt`（含 RAW_JSONL）。

因子：A=旧事片段在历史（有/无）；B=本轮带显式锚点（有/无）；C=引用正文注入 user_text（有/无）。
观测列：`old_in_annotated` / history 数 / evidence 数 / summary 数 / skip / chars。

| case | A旧事 | B锚点 | C引用 | 旧事进输入 | history | evidence | skip | chars |
|---|---|---|---|---|---|---|---|---|
| A1B1C1 | 有 | 有 | 有 | 是 | 2 | 0 | — | 316 |
| A1B1C0 | 有 | 有 | 无 | 是 | 2 | 0 | — | 280 |
| A1B0C1 | 有 | 无 | 有 | 是 | 2 | 0 | — | 303 |
| A1B0C0 | 有 | 无 | 无 | 是 | 2 | 0 | — | 267 |
| A0B1C1 | 无 | 有 | 有 | 是 | 1 | 0 | — | 307 |
| A0B1C0 | 无 | 有 | 无 | 否 | 1 | 0 | — | 271 |
| A0B0C1 | 无 | 无 | 有 | 是 | 1 | 0 | — | 294 |
| A0B0C0 | 无 | 无 | 无 | 否 | 1 | 0 | — | 258 |

条件式读法（不写期望值）：
- **因子 A 主导**：A=1 时四格旧事全进输入；A=0 时仅 C=1（引用注入）让旧事以
  user_text 一部分进入。说明历史窗口"全量取数"，不做锚点过滤。
- **因子 B 对输入侧无差异**：A=1 时 B=有/无都进旧事；A=0+C=0 时都不进。
  这是 A 方案的结构性事实——用户有没有锚点，取数都一样。它不能证明模型行为，
  但证明"输入侧没有锚点门控"。
- **因子 C 的作用**：A=0 时 C=1 → 旧事进输入（引用正文被拼进 user_text），
  C=0 → 不进。符合 D 组"入站 reply_to 无独立 prompt 块，引用文本靠 connector 拼进 user_text"的发现。
- 所有格子 evidence=0、summary=0、skip 空（本轮 user_text 都不是 `？`）。
- **字符数差（实测，供对照，不是期望值）**：A0B0C0=258 → A0B0C1=294（+36，
  来自**因子 C**引用注入）；A0B0C0=258 → A0B1C0=271（+13，来自**因子 B**锚点句
  比"早上好"长）。两因子增量不同，恰说明它们各自独立影响输入长度。
  口径：这是 `annotated` 字符和，不是 token 数；按 M-11 需注明口径，不能当 token 用。

**重叠披露（终审修正，原"不是因子混淆"措辞偏强）**：因子 C 注入的引用文本恰好包含
旧事片段，因此 A=0+C=1 的"旧事进输入"来自 user_text 而非历史窗口。这是**已披露的
字符串重叠**——`old_in_annotated` 观测字段由 A 与 C 共同决定，C 注入的 OLD 与 A 定义
旧事的是同一字符串；历史侧只随 A 变（`n_history` 只由 A 控制），已用 n_history 反证。
这不影响"历史取数只由 A 控制"的结论，但`old_in_annotated`不能单独归因给 A。

**不能证明**：有锚点是否让模型"正确续接"、无锚点问候是否"误提旧事"——回复侧全 UNKNOWN。

---

## 4. 真实 / 模拟严格分开（纪律核对）

| 项 | 状态 | 证据 |
|---|---|---|
| 真实模型 API | 未取得 | 无 API env；脚本不调模型 |
| 真实模型输入 | **已取得（编译输入快照）** | out-baseline.jsonl / out-contrast.txt 的 annotated |
| 真实模型回复 | **未取得** | 不存在任何 REAL_MODEL 回复字段 |
| 脚本模拟回复 | 本轮未生成 | 变异脚本只改输入侧，不生成回复文本 |
| 输入长度实测 | 已取得（**字符数口径，非 token**） | chars 列（253–316 / 258–316）；按 M-11 为条件式观测，非阈值 |
| 调用数记录 | 已落盘；M-12 仍为 UNKNOWN | 每条 out-* 记录 `n_calls=1`（`ContextCompiler.compile`）与 `n_llm_calls=0`；因没有真实模型输出，无法判断额外调用是否带来行为收益 |

> 按对抗报告 §3 变异1：无真实输出时，只能报管线属性（输入长度、车道、skip、
> 引用降级），不得报"模型行为"。因此调用数虽已记录，M-12 的条件式结果仍无法判定。

---

## 5. 未取得 / 无法证明

1. 真实模型回复（全部样本）——未取得，不得声称 A/B/C 谁"更好"。
2. 旧话题恢复的"自然度"、换题"自然度"——无真人评测，UNKNOWN。
3. 群聊跟对说话人在真实 QQ 群的表现——只有合成样本，无真实群聊许可数据。
4. "新窗口是否必要"——D 组取证已给代码层结论（话题切换自然发生、不需新窗口），
   但仍无行为层对照；标为"代码路径实测 + 推理"，非行为实测。
5. 论文数字当 Ling 参数——禁止；所有阈值来自代码常量或本回放条件式观测。

---

## 6. 复现命令

```bash
cd /Users/lingxi/project/ling
source /tmp/ling_test_lock.sh && acquire_test_lock
export LING_TEST_DATABASE_URL=postgresql+asyncpg://ling@127.0.0.1:55432/ling_test
export LING_DATABASE_URL=postgresql+asyncpg://ling@127.0.0.1:55432/ling_test
.venv/bin/python docs/evidence/ling-context-research/replay/replay_01.py \
  > docs/evidence/ling-context-research/replay/out-baseline.jsonl
.venv/bin/python docs/evidence/ling-context-research/replay/02-contrast.py \
  > docs/evidence/ling-context-research/replay/out-contrast.txt
.venv/bin/python docs/evidence/ling-context-research/replay/03-variants.py \
  > docs/evidence/ling-context-research/replay/out-variants.jsonl
release_test_lock
```

自检：本文与三份 out-* 文件均不含 U+FFFD、无真实内网地址与真实 QQ 号。
