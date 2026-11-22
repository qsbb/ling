# Ling 上下文研究汇总：第一期该改什么

观测对象：`HEAD=de43cbe79a78e0c01f7821470500eaa04ea79497`，工作树初始干净。
`bridge.py` sha256 `334534fe1ea…a94a70`、`context.py` sha256 `ffb573b49e8…9718ba`，全程未变。
研究阶段实际派发 5 名 teammate（人类对话、即时通讯、源码取证、样本方案、独立对抗），每名在内部并行派生子智能体，均显式 `provider=nasfree, model=hy4flash`（本次用户授权的破例；全局 DeepSeek 白名单在本会话不可用）。

置信度约定：本文用「实测」=本轮有命令/日志/代码/API 核对；「推理」=基于证据的推导；「转述」=引用他人说法但未独立核实。表格只承载实测或明确规则，推测用普通段落。

---

## 0. 先回答三个问题

**什么应常驻**：身份/人设/关系/约束、当前窗口的滚动摘要（`window_contexts.summary`）、最近几轮原文（`history` 车道）。这三样在 Ling 里是「同一个 `window_id` 下按 token 预算回装」的，不是按话题组织的。源码实测：入站复用键 `(actor_id, channel, envelope.window_ref, trusted_instance_key)`，命中后 history/summary 全按 `windows.id` 取（`C-bridge-window-reuse.md`）；发送块顺序是 system 车道 → summary → history(旧→新) → input 当前消息，无后置块（`D-context-assembly.md`）。

**什么随当前话题带出**：最近 1–2 泡、且与当前话语相邻的原文。人类对话与 IM 两组共同给出：默认接眼前上一句（相邻对/修复邻接待证），`后来呢？` 是承接上一话题的续接提示但只在最近话题焦点内有效，群聊里先由引用结构定目标消息、再由署名定说话人。

**什么只在明确需要时找回**：很久以前的旧话题。恢复通道只有三类：①显式时间/话题锚点（`接着说前天那个方案`）②引用/@ 锚点（被引用正文必须在输入可见）③承诺兑现等情境自然重提。无锚点的 `？`/`早上好`/`嗯` 默认不恢复旧话题。这条是行为规则建议（推理/转述），不是当前 Ling 的实测行为——当前实现里**没有**话题级门控，见下。

**要不要改变会话管理（新窗口）**：不需要。三个代码库（AstrBot、MaiBot、Ling）的源码静态观察都显示没有话题级自动切窗，旧话题靠自然漂移；Ling 的 sealed 才是唯一切断语义的方式。结论置信度：代码层实测 + 推理，非行为实测。

---

## 1. 反例线索的裁决

反例：用户先问「你还记得前天聊了什么吗」，后发「早上好」，再对刚才问候回「？」；Ling 先解释问候，又继续列很久以前四项产品需求。

先厘清「实际输入」而不是截图：截图只能说明回复表现，真实输入必须以提示词快照/回放为准。本轮没有取得该次真实回放，因此**不能断定 Ling 当时到底从哪一类来源拿到了那四项需求**。可排除与不可排除的，分开写：

- 实测（输入侧管线）：`？` 经 NFKC + `rstrip` 后为空串，命中跳过集合 → `should_skip_automatic_recall=True`，**零自动召回**；`早上好` 为 `False`；`后来呢？` 触发 `needs_recent_topic`。所以「短问号触发宽泛记忆召回」这条早期推测被推翻。旧事若进输入，在回放环境里只由**同窗口最近原文**决定（隔离库 `evidence=0, summary=0`），不是由摘要或召回决定；但真实生产环境里摘要常驻、长期记忆可召回，这条输入侧实验覆盖不到。
- 实测（源码）：同实例 + 同 `window_ref` 命中 → 旧消息/摘要挂同一 `window_id` → 编译器按 token 预算回装，无话题相关性过滤。所以「同窗口旧话被一起塞进当前 prompt」在实现上是可能的，不需要任何话题切换逻辑。
- 推理（人类对话/IM 证据）：`？` 默认是 repair 当前上一句的信号，不是指代；只有「未完成相邻对 + 显式回指 + 回复内容回答更早提问」同时成立才应改判为话题续接。`早上好` 把旧事件退为背景，是可插入的 greeting。按此，Ling 续列旧需求属于「误把背景焦点当当前活跃意图」这一类错误——但这是机制假说，**须快照/回放判定**，不能由文本分析单独定案。

因此反例的最小可复现形式不是「Ling 一定错了」，而是：**在 `？` 零召回的前提下，同窗口历史仍可无门控地整段进入输入，使「把旧事当当前话题续接」成为一个无需额外触发就能发生的失败模式。** 这是输入侧实测，不是回复侧归因。

---

## 2. 证据来源与可复查路径

### 2.1 人类对话（推理/转述为主，书目标注为实测复核）

四份文档各含原文出处、研究情境、可观察行为、适用边界、反例映射：
- `docs/design/ling-context-research/human/01-turn-taking-repair.md` — Sacks/Schegloff/Jefferson 轮次与 repair；`？` 默认当前上一句 OIR。
- `docs/design/ling-context-research/human/02-topic-management.md` — 话题靠序列共同关闭；持久化 IM 里话题只会失去注意力、不会消失；`早上好` 是 greeting、`后来呢` 是续接、`接着说前天那个方案` 是 resumption。
- `docs/design/ling-context-research/human/03-common-ground-reference.md` — Clark & Brennan 共同背景、指代与修复启动；经对抗修正后统一为「两种解读皆可能，须快照/回放判定」。
- `docs/design/ling-context-research/human/04-attention-event-boundaries.md` — 事件边界靠感知到的情境变化点，不靠固定时长。

书目经 Crossref/ACL 联网核实，并纠正三处题面误差：Clark & Brennan DOI 实为 `10.1037/10096-006`；Event Segmentation 只有 Zacks & Swallow 两位作者；Grosz & Sidner 1986 为 CL 12(3):175–204。对抗方复核了 Crossref/ACL 条目一致。

### 2.2 即时通讯（推理/转述，外部来源部分实测抓取）

`docs/design/ling-context-research/im/{private-chat,group-parallel-topics,cross-session-async,offline-contrast}.md`。
实际抓取核对的只有 SEP Implicature 与 QQ 机器人官方 API 文档（均 HTTP 200）；其余文献标「转述未核实」。全部样本合成，QQ 号 `1002003xx`、地址 `192.0.2.x`。经对抗修正后，所有「这条路径最容易召回旧事」的结果断言已改为「代码路径事实 + 机制推测/当前未测/须 annotated 快照判定」。

### 2.3 源码取证（本轮最硬的实测证据）

- `docs/evidence/ling-context-research/source/A-astrbot-upstream.md` — 官方 AstrBotDevs/AstrBot，tag `v4.30.0-beta.1` / commit `90ba3270f09b857063da68908d80f742042067cd`（GitHub API 复核一致；clone 403 如实申报）。会话键 `UMO=platform_id:message_type:session_id`，LLM 收到整段 `conversation.content`，真正的「窗口」是 token 触发压缩（默认 `max_turns=-1`、`keep_recent_ratio=0.15`）。`provider_ltm_settings.*` 默认全关且走 hook；无自动话题切换检测，靠 `/reset`、`/new`。
- `docs/evidence/ling-context-research/source/B-maibot-upstream.md` — 官方 Mai-with-u/MaiBot，v1.4.8 / commit `edf42446`（API 复核一致）。`session_id=MD5(platform+scope+group_id/user_id+private)` 跨天复用；历史窗口 `max_context_size=50` / `max_private_context_size=75`，选窗 2× 放大、轮末裁到 ~20%；旧内容主因是**每轮无条件注入且裁切永不移除的常驻摘要**；`/clear` 不换 session_id；A-Memorix 默认关，配置存在≠主链接线。
- `docs/evidence/ling-context-research/source/C-bridge-window-reuse.md` — 复用键、群/私聊判定唯一实现 `is_group_window_ref`、入站无 TTL/无归档过滤/无话题切换或强制新建。`group_mention_on_reply` 在 `bridge.py` 仅 2 处（361、390）**只存不用**。
- `docs/evidence/ling-context-research/source/D-context-assembly.md` — `turn.py:1033` 唯一编译入口；块顺序、无 pause 块、入站 `reply_to` 无结构化 prompt 块；群聊署名用 `subjects.display_name` + 群名片消歧，与被引 `[msg_id:]` token 正交；自动记忆 12 候选→3 条精筛、420 token 预算。

对抗方对 A/B 的版本 pin 做了独立 GitHub API 复核，对 C/D 的 sha、常量、行号、否定结论做了 grep/代码复核，均一致。但 A/B/C/D 是四个代码库的静态观察（C/D 同库），**不是四次独立实验，也不能替代某次 Ling 回放因摘要常驻误接旧事的快照证据**。

### 2.4 样本与回放（输入侧实测，回复侧未取得）

- `docs/evidence/ling-context-research/replay/01-samples.md` — 13 开放 + 4 盲评，覆盖明确续聊/刚换话题/异步引用/简单问候/短问号/多相似旧话题/群聊交错。
- `docs/evidence/ling-context-research/replay/BASELINE-REPORT.md` 等 — 在测试库 `ling_test` 的独立 schema `so_replay` 上调用 `ContextCompiler.compile()`，产出 6 条基线快照、2×2×2（8 格）、半成品变异（12 条）。环境无 API，脚本不调模型，所有输出标 `COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT`。变异数据中 3 条 q 输入标 `INVALID_EXPECTATION`，C 变体 3 条标 `UNINFORMATIVE_PASS`；仅 B-half-2 无锚点问候格提供有效输入侧区分，回复侧判据仍 UNKNOWN。

---

## 3. A / B / C 方案裁决

| 方案 | 定义 | 本轮能证明什么 | 不能证明什么 | 裁决 |
|---|---|---|---|---|
| A 对照现有行为 | 同窗口 history 按 sequence 全量入输入，无锚点门控 | 实测：2×2×2 里锚点因子对输入侧零差异；`？` 零自动召回；history 无 topic 过滤 | 回复侧误提/自然度全部 UNKNOWN | 作为基线保留；不要据输入侧说它「行为更差」 |
| B 轻量取数改进 | 从最近几轮 + 问候/引用信号改进当前轮取数 | 实测：B-half-2（信号层绿但旧事仍在输入）是唯一有效区分性证据；`later`/`？`/引用信号可操作 | 完整 B 从未构造，缺完整实现 PASS 端点；B-half-1 基线同红、区分力不足 | 第一期的最小改动方向，但须先补回复侧观测再定参数 |
| C 上下文分段 | 更明确的主题/事件分段 | 推理：能解释「自然结束/重新提起」 | 实测无；需要数据模型/协议层改动；对抗复核认为证据不足 | 暂缓，不进第一期 |

量化指标现状（诚实写空白）：C1–C4（误提旧事/主动换题自然度/真正需要续聊却漏掉/指错人或消息）在真实模型输出缺失时**全部 UNKNOWN**；C5 输入长度有数（annotated 字符和 253–316，字符非 token）；调用数已在每条 out-* 记录 `n_calls=1`（compile）、`n_llm_calls=0`。M-11 没有回复侧条件结果；M-12 虽有调用数，仍缺行为结果，均未达判定门槛。所以**不能从本轮数据得出 A/B/C 谁在行为上更好**。

主动换题的四种触发（当前有现实关联、自然结束、对方感兴趣、待兑现承诺）在人类/IM 证据里只有「待兑现承诺」有较强可观察判据，其余多为推测；本轮没有真实回复数据评估「恰当主动/过度主动/错误续聊」的频率。

---

## 4. 第一期推荐的最小改动

按证据强度排序，只做有输入侧实测支撑、且不改会话管理的一步：

1. **给旧事恢复加显式锚点门控（B 的最小形态）**：当前轮无时间/话题锚点、无可见引用、无承诺兑现线索时，不从 history 里把很久以前的旧事当当前话题带出；`？`/`早上好`/`嗯` 默认接最近一句或视为新开场。理由：`？` 零召回已实测；history 无过滤已实测；锚点是人类/IM 两组共同的可观察判据。注意这只是输入侧规则，行为效果待补实验验证。
2. **把入站引用正文接进 prompt（修复真实失败模式）**：`D-context-assembly.md` 证明入站 `reply_to` 没有结构化 prompt 块，只作 bool 被点名；「引用看起来在但正文未进 prompt」是当前真实失败模式。让被引正文可见，群聊里按引用消息→消息、`sender_subject_id` 逐条绑说话人，`@` 不改变说话人。
3. **不改 window 划分、不引入新窗口**：三个代码库静态观察均不需要；sealed 是唯一切断语义，按话题分窗要动数据模型/协议层，证据不足。

关于旧话题如何恢复：用户重新提起时走「显式锚点 → 可见引用 → 承诺兑现」三条通道，任一命中才把旧事重新置为当前焦点；无锚点时不因「它还在同一窗口里」而续。

---

## 5. 证据不足时最值得补的一组实验

对抗方两次独立指出同一个缺口：现有 2×2×2 只查字符串是否进 `annotated`，**零回复侧观测**，无法区分「旧话题为何容易被继续接下去」的竞争解释（recency、共同背景、未完成计划、事件边界）。

最小补实验：复用现有 8 个因子设置（旧事片段在/不在 × 用户本轮有/无锚点 × 引用正文注入/不注入），但**加回复侧观测**——同一模型、同一份历史，记录真实回复并标注它是否：接眼前上一句 / 续旧事 / 误提旧事 / 自然换题 / 指错人或消息。若环境仍无 API，可用脚本模拟规则输出，但必须整栏标 SIMULATED，且只能作为管线正确性检查，不能回答「模型为什么续旧事」。这一组实验能同时给 C1–C4 填数、验证 B 的完整实现 PASS 端点、并对四个竞争解释排序。

---

## 6. 纪律与运维记录

- 全程未改 `src/`；HEAD、两个关键文件 sha 与初始基准逐字一致。
- 子智能体一律显式 provider+model；本轮按用户授权使用 `nasfree/hy4flash`，已在开头标注。
- 对抗验收先写判据后看实现，发现并推动修正 3 类问题：human/03「？」写成事实断言、options/02 悬空 M0、IM 两篇把路径推理写成结果断言；终审又降级了 B-half-1 的区分力。全部修正经 owner 在本写作用域内完成并回归验证。
- 写作时的一次 `git status` 观测曾显示多项 `A`/`AM`，报告作者未执行 `git add`；该条记录的是当时提交归属告警，不代表后续工作树状态。
- 真实 U+FFFD 字节零命中；grep 对 `$'\ufffd'` 字面量的命中是自检命令自引用造成的假阳性，已用 Python 逐字节扫描排除。

## 7. 关键交付路径

- 汇总与对抗：`docs/evidence/ling-context-research/review/ADVERSARIAL-REPORT.md`、`ADVERSARIAL-APPENDIX-{A,B,C}.md`、`ADVERSARIAL-CRITERIA.md`
- 方案与判据：`docs/design/ling-context-research/options/00-criteria.md`、`02-options.md`
- 回放样本与报告：`docs/evidence/ling-context-research/replay/01-samples.md`、`BASELINE-REPORT.md`、`VARIANTS-REPORT.md`
- 源码取证：`docs/evidence/ling-context-research/source/{A-astrbot-upstream,B-maibot-upstream,C-bridge-window-reuse,D-context-assembly}.md`
- 人类/IM 研究：`docs/design/ling-context-research/human/*.md`、`docs/design/ling-context-research/im/*.md`
