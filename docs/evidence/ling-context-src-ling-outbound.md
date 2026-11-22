# Ling 发给模型的完整上下文：`src/ling/kernel/context.py` 源码取证

> 子智能体 `src-ling-outbound` 证据文档。HEAD=`de43cbe79a78e0c01f7821470500eaa04ea79497`，branch=`main`。
> 观测时刻：2026-11-22 09:31+08:00 起（Asia/Shanghai）。
> 只读研究：未改任何业务代码；本报告是唯一新增文件，未 `git add` / `git commit`。
> 原则：**真实输入以提示词快照／回放为准**。本文全部结论来自源码路径与结构定位，不把截图或回复表现当作真实输入；回复表现只能当待验证现象，不能反推提示词内容。

## (1) 观测对象：git status/log/sha256

开始时按 AGENTS.md「先钉住观测对象」跑了 `git status --short` + `git log -1` + `git rev-parse HEAD` + `git branch --show-current`，原样输出：

```
=== git status --short ===
?? docs/evidence/ling-context-im-async.md
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-offline.md
?? docs/evidence/ling-context-im-private.md
?? docs/evidence/ling-context-research/
?? docs/research/
=== git log -1 ===
de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制
=== git rev-parse HEAD ===
de43cbe79a78e0c01f7821470500eaa04ea79497
=== branch ===
main
```

**注意：任务书说"开始时工作树干净"，但实测并不干净**——有 6 项未跟踪路径（兄弟子智能体的 `docs/evidence/ling-context-im-*.md`、`docs/evidence/ling-context-research/`、`docs/research/`）。这些不是本子智能体的文件，且 `git status` 里没有任何 `M`（业务代码与被测文件都未被改动）。记录差异而不假设干净。

被测文件 sha256（开始与收尾各测一次，两次一致）：

```
=== 开始时 ===
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
b03a4dcb40467e7ac926948f887ca067de8a8dce4860e7f9bc5737d1874886fe  src/ling/domains/memory/recall_query.py
0e7fc539c5754404e474c9d9a65d18a513431a0d7ae3318aba89b3dd9e3a18b9  src/ling/config.py
=== 收尾复测 ===
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
```

`wc -l`：`context.py` 2287 行、`bridge.py` 2175 行、`recall_query.py` 27 行、`config.py` 341 行。**本报告所有行号都基于上述 sha256 对应的当前文件，用 AST 结构定位取得，不是行号开窗的快照。**

方法：用 `python3 - <<'PY'` 跑 `ast.parse(path.read_text())`，遍历 `ast.Assign` / `ast.ClassDef` / `ast.FunctionDef` / `ast.AsyncFunctionDef` 取 `lineno`/`end_lineno`，再对目标行区间用 read 工具读正文；最后用 `grep -rn $'\ufffd'` 自检编码。不依赖记忆中的行号。

---

## (2) 上下文车道与预算

### 2.1 车道清单来自哪里：单一数据源 `prompt_zones.py`

`context.py:40–57` 的注释明写「提示词分区注册表：车道清单、预算、牺牲次序、保护集合、展示分组的**唯一**来源。本文件不再手写任何一份车道表」，`context.py:42–57` 从 `ling.kernel.prompt_zones` 导入 `_DEFAULT_BUDGETS` / `_LANE_TO_KEY` / `_PROTECTED_LANES` / `_SACRIFICE_ORDER` / `budget_overruns` / `lane_zone`；`context.py:165` 把 `_DEFAULT_BUDGETS` 别名为本模块的 `DEFAULT_BUDGETS`，`context.py:330–332` 从 `_LANE_TO_KEY` 过滤出结构车道后别名 `LANE_TO_KEY`，`context.py:2058–2059` 对 `SACRIFICE_ORDER` / `PROTECTED_LANES` 同样做了别名。

权威表在 `prompt_zones.py:183–310 ZONES`：10 个区域（identity / scene / situation / memory / plan / media / capabilities / recap / dialogue / input），三类 `Kind`（`SYSTEM` / `SUMMARY` / `HISTORY` / `INPUT`，定义于 `:101–115`）。每条车道的预算、消息位置、牺牲顺序、保护标志都登记在 `LaneSpec`（`:136–155`）。派生表：`LANES`（`:317–319`）、`DEFAULT_BUDGETS`（`:321–325`，**只取 SYSTEM 车道**）、`PROTECTED_LANES`（`:327–329`）、`LANE_TO_KEY`（`:331–333`）。`validate_registry()`（`:458–571`）在导入期 `raise ValueError`（不用 `assert`，`:461`），表写错启动即崩。

**关键区分**：`summary` / `history` / `input` 是**结构车道**，`LaneSpec.budget_tokens=None`（`prompt_zones.py:142`、`288`、`298`、`308`），不受 `SACRIFICE_ORDER` 裁剪；它们各自有独立窗口策略。`budget_overruns()` 也明确跳过它们（`:619–623`）。所以"车道预算表"里没有"历史 20000"这一项——历史预算在 `context.py` 里另立常量。

### 2.2 历史/摘要预算常量（`context.py`）

| 常量 | 行号 | 默认值 | 含义 |
|---|---|---|---|
| `HISTORY_TOKEN_BUDGET` | `:81` | 20000 | 最近原文层的 token 上限（注释 `:77–80`：原来 1600，约 20~30 条就开始滚动摘要） |
| `SUMMARY_TOKEN_BUDGET` | `:83` | 400 | 滚动摘要预算；实际截断阈值是 `×2` 字符（`:2007`） |
| `HISTORY_FETCH_CAP` | `:93` | 2000 | 一轮最多从库里读回多少条**尚未摘要**的消息；注释 `:85–92` 明写它是"别吃满内存"的保护，**不是**取数边界 |
| `_HISTORY_SINGLE_MESSAGE_SHARE` | `:100` | 0.5 | 单条消息最多占历史预算的比例 → `single_cap = 10000`（`:1478`） |
| `_MAX_AUTOMATIC_MEMORIES` | `:101` | 3 | `_memory_evidence()` 最多选 3 条（`:153`） |
| `_MEMORY_SHORTLIST` | `:102` | 12 | 传给 `MemoryService.recall()` 的 `limit`（`:1572`） |
| `HISTORY_MESSAGE_ID_LIMIT` | `:384` | 12 | 只给最近 12 条用户消息标 `[msg_id:…]`（`:1965`） |
| `AUTOMATIC_MEMORY_TOKEN_BUDGET` | `:270` | `max(1, int(420 * min(1.0, _SCALE)))` | 自动记忆 evidedence 预算，`evidence_budget = min(DEFAULT_BUDGETS["evidence"], 该值)`（`:1576`） |

`estimate_tokens()`（`:105–115`）：中文 1 字≈1 token，其他 4 字符≈1 token；用于所有预算裁剪，不是精确分词器。

### 2.3 默认配置来自哪里，是否真接到主链路

**配置来源**：
- 预算常量是**代码内硬编码默认值**；`LING_CONTEXT_BUDGET_SCALE` 可整体缩放（`:247–266`，`0.1~5.0` 夹紧，非法值警告后按 1.0）。`_SCALE`（`:269`）在**导入期**算一次，若非 1.0 则同时缩 `DEFAULT_BUDGETS`（`:272`）、`HISTORY_TOKEN_BUDGET`（`:275`，下限 400）。部署样例见 `docs/DEPLOYMENT.md:984`（`LING_CONTEXT_BUDGET_SCALE=1.0`）。
- 车道预算**权威来源**是 `prompt_zones.py::ZONES`（经 `context.py:165` 别名），不是 `src/ling/config.py`。总预算软/硬上限由 `_total_soft_hard()`（`:2062–2071`）从 `sum(DEFAULT_BUDGETS.values())*2` 推导，可用 `LING_CONTEXT_SOFT_CHARS` / `LING_CONTEXT_HARD_CHARS` 环境变量覆盖。
- 人格 id 默认 `"persona_ling"` 在 `ports/config.py:533 EffectiveConfig.persona_id`，`memory: MemoryPolicy` 在 `:534`；`ContextCompiler.__init__`（`:455–504`）从调用方接收 `persona_id` / `memory_policy` / `model_config` / `secret_reader`，自己**不读库取策略**（`:489–497` 注释：不传才退回代码默认，那是"设置页能改、运行时从不读"的断链）。

**是否真接到主链路：是（实测代码路径）。** `turn.py:93` 导入 `ContextCompiler`；`turn.py:1033–1045` 在 `run_turn()` 内构造 `ContextCompiler(...)` 并 `.compile(...)`，传入 `subject_id` / `window_id` / `user_text` / `is_group` / `current_turn_id=turn_id` / `user_sender_name` / `current_message_platform_id`（`:1046–1070`），并显式**不传** `history_limit`（`:1050–1056` 注释：原来传 20 是永久缺口的根因）。`messages = list(compiled.messages)`（`:1071`），`reply_ids = dict(compiled.reply_ids or {})`（`:1075`）供收尾校验引用。这是生产回复路径上唯一的上下文来源（`turn.py:986` 注释：「上下文只有一个来源：ContextCompiler」）。

**真实输入的落盘处**：`turn.py:1897–1958 _capture_prompt_snapshot()` 保存 `compiled.annotated`（与 `messages` 同一份数据 + 语义块标签）；若 `len(annotated) != len(messages)` 则退回未标注版并标 `other`（`:1922–1941`），注释明写「宁可丢掉块标签，也不能把没发出去的东西记成她看到的」。表 `prompt_snapshots` 定义在 `infra/models.py:620–630`。**因此，要判定"模型实际看到了什么"，应读 prompt_snapshots 的 payload／回放，而不是读 UI 截图或模型回复。** 本任务为只读源码研究，未连生产库/NAS，故未取得真实快照（见第 (6) 节）。

---

## (3) 历史分页 / 摘要 / 自动记忆 / 跨窗来源逐条定位

### 3.1 最近对话分页（同一窗口原文）

装配入口 `compile()`（`:548–1861`）。原文层逻辑在 `:1460–1546`：

- `:1476`：`_through = int(window_ctx.summary_through_sequence or 0)` —— **只取尚未摘要的消息**（`sequence > _through`）。
- `:1477`：`page_size = max(1, min(int(history_limit or 1), HISTORY_FETCH_CAP))`。`turn.py` 不传 `history_limit`，故取 `compile` 默认值 `200`（`:556`）→ `page_size=200`，即每页 200 条、最多 2000 条。
- `:1486–1520`：`while True` 按页往回取。`messages_for_prompt(window_id, limit=page_size, after_sequence=_through, before_sequence=cursor)`（`:1487–1492`）。返回**旧→新**（`service.py:1354–1356`），这里 `list(reversed(page))` 变新→旧，`fetched = newest_first + fetched`（`:1497–1498`）。游标 `cursor = page[0].sequence`（最旧那条，`:1520`）。
- 过滤/截断：跳过本轮消息 `current_turn_id`（`:1500–1501`）；单条 `cost > single_cap(10000)` 时 `_fit_message_text()` 截断并 `truncated += 1`（`:1503–1507`，截断函数 `:205–227`，留痕 marker `…（这条太长，后文被截掉了）`）；`used_tokens + cost > HISTORY_TOKEN_BUDGET` 时 `budget_full=True; break`（`:1508–1510`）。
- 退出条件三选一：预算满（`:1513–1514`）、取完（`exhausted = len(page) < page_size`，`:1495`、`:1515–1516`）、触顶 `HISTORY_FETCH_CAP`（`:1517–1519`，记 `compiled_meta["history_cap_hit"]=True`，`:1547`）。
- `:1529` `usable.reverse()` 回旧→新；`:1530–1536` 由 `fetched` 减去 `kept_ids` 算 `dropped`。
- **消息顺序**：`annotated` 里历史块按 `for msg in usable`（旧→新）逐条 append（`:1732–1789`），所以模型读到的历史是**从旧到新**，当前消息永远最后一条（`:1839–1847`）。
- 取数 SQL 在 `conversation/service.py:1302–1356`：`state=completed`、`role in {user, actor}`、`visible_to_model()`（`:1327–1343`），`sequence > after_sequence` / `< before_sequence`（`:1344–1347`），`order_by(sequence.desc()).limit(limit)` 再 `reverse()`。**这保证撤回未看过的不进上下文也不进 `dropped`。** 时间/说话人/消息 id 是取数视图 `MessageView` 的字段（`:1787 at=msg.created_at`）。

**停顿**：`CompiledMessage.at` 只挂在 annotated 的历史块上（`:1787`），而 `ChatMessageIn`（`ports/model.py:418–432`）只有 `role/content/images/videos/...`，**没有时间字段**。真正发给模型的 `messages` 在 `:1852` 由 `ChatMessageIn(role=m.role, content=m.content)` 构造——**原生时间戳/停顿没有进入模型输入正文**，只进了快照标注。正文里的时间只出现在媒体块的 `_as_local_hint_seconds()`（`:930`、`:996`，格式 ` 12:21:07 `，定义 `:184–202`）。结论：模型看不到"隔了多久才回"，只能看到消息先后顺序与媒体块里的钟点。（实测代码路径。）

### 3.2 滚动摘要（旧摘要 `summary` / `summary_through_sequence`）

- 读：`ctx = await self._conversation.read_window_context(window_id, expired_ok=True)`（`:1135`），缓存到 `self._window_ctx_cache`（`:1136`），`:1461` 复用。
- **TTL 只管短记忆，不管摘要**：`_window_topic_live()`（`:230–244`）只判 `expires_at`，注释 `:231–237` 记载旧实现把摘要一起判过期导致「她完全不记得聊过什么」。短记忆注入条件是 `_window_topic_live(ctx) and (ctx.topic or ctx.open_questions)`（`:1137–1141`），**不包含 summary**。
- 注入：`:1673–1685`，只要 `window_ctx.summary` 非空就无条件 append 一条 `role=system, key="summary", lane="summary"` 的块，文案「本窗口较早的对话摘要（截至第 {summary_through_sequence} 条，共 {summary_message_count} 条）：\n{summary}」。**没有话题相关性判断，也没有"当前是否还在那个话题"判断**（实测代码路径）。位置在 system 贡献之后、历史之前，且经 `annotated` 直接进入 `messages`（`:1852`），**不受 `_apply_total_budget` 裁剪**（它是 annotated.append，不是 contributions）。
- 写：`dropped` 非空时 `:1791–1805` 调 `_rollup_summary(window_id, dropped, window_ctx, through=new_through)`。`new_through = min(usable 的 sequence) - 1`（`:1797–1801`），保证摘要覆盖与原文窗口严丝合缝。`_rollup_summary()`（`:1979–2024`）用 `render_rollup(dropped, prev_summary=...)` 确定性压缩（`:2001`，**不调模型**），截断时保留头 45% + 尾 55%、中间省略（`:2007–2015`，避免最早约定被丢），再 `save_summary(summary=merged, through_sequence=through, message_count=prev_count + len(dropped))`（`:2019–2024`）。
- `render_rollup()`（`:2074–2098`）：渲染成第一人称回顾「对方说过「…」/ 我说过「…」」，**刻意不写"名字：内容"对话记录**（`:2077–2082` 注释：避免模型模仿加「她：」前缀）；单条截取前 120 字（`:2097`）；撤回消息在摘要里也留「（后来被撤回了）」（`:2096`）。

结论（实测代码路径）：旧话题一旦压进摘要，会每轮无条件随上下文带出，与当前话题无关。这是"隔天问旧事她还能接上"的结构性来源，也是"话题已换却仍在带旧背景"的来源。**但摘要是逐条截取要点、非逐字**，细节会丢。

### 3.3 自动记忆与 evidence 装配

- 门控（`context.py:1549–1575`）：
  - 不允许长期记忆 → `RecallResult()`（`:1550–1551`）。
  - `should_skip_automatic_recall(user_text)` 为真 → 空召回 + `compiled_meta["memory_recall_skipped"]="low_information"`（`:1552–1554`）。
  - 否则用**当前消息原文**做 query（`:1556`）；只有 `needs_recent_topic(user_text)` 为真才回扫 `usable`，拼最近一条符合条件的用户消息（`role=user`、未撤回、群聊里须是同一 speaker、本身不是追问也不是寒暄）成 `f"{message.text[:180]}\n{user_text}"`（`:1557–1566`）。
- 门控实现 `recall_query.py:6–27`：`_STANDALONE_REPLIES` 含 `""`/嗯/哦/好/你好/在吗/hi/hello 等（`:6–11`），**不含"早上好"**；`should_skip_automatic_recall()` 用 NFKC + strip + casefold + `rstrip(" \t\r\n.!?。~")` 后精确匹配（`:19–22`）——所以全角「？」归一成 `?` 再被吃掉 → plain 为空串 → 在集合里 → **跳过**；「早上好」不在集合 → **会触发**宽泛查询。`needs_recent_topic()`（`:25–27`）匹配 `后来呢/然后呢/哪家/几点/原话…`（`_TOPIC_FOLLOWUP` `:13–16`）。
- 召回调用：`self._memory.recall(subject_id, query, include_history=wants_history(user_text), visibility_allow=allowed, limit=_MEMORY_SHORTLIST(12), trace_window_id=window_id, audience=snapshot)`（`:1567–1575`）。`wants_history()`（`service.py:1817–1818`）匹配「以前/之前/原来/过去/上次说过/曾经/旧地址/旧住址/旧名字」。`MemoryService` 在 `ContextCompiler.__init__:498–503` 用调用方传入的 `policy/config/secret_reader` 构造，策略不自己读库（`:489–497`）。
- 候选筛选 `_memory_evidence()`（`:118–155`）：同窗口且已在原文里出现的 episode/assertion 跳过（`:126–131`，避免重复）；去重 `overlapping_evidence`（`:132–137`、`service.py:1875–1893`）；episode 被同事件 assertion 覆盖时跳过（`:134–137`）；累计超 `evidence_budget` 则跳过（`:139–147`）；最多 3 条（`:153`）。`evidence_budget = min(DEFAULT_BUDGETS["evidence"](6000), AUTOMATIC_MEMORY_TOKEN_BUDGET(420))` = **420 token**（`:1576`）。
- 注入：`contributions.insert(evidence_index, ContextContribution(lane="evidence", ...))`（`:1588–1601`），`evidence_index` 在 `:771` 于证据之前的位置记下，使证据紧跟受众快照之后。渲染文案来自 `render_evidence()`（`service.py:1833–1872`）：分组「记得的事 / 曾经聊过的话（按署名理解）/ 熟悉的线索 / 模糊的印象 / 以前的情况（不代表现在）」，单条超 240 字截断（`:1868`）。
- 召回后处理：`mark_recalled()`（`:1585–1587`）、`reinforce(selected_ids)`（`:1653–1658`）；若总预算把 evidence 车道整条丢了，则 `selected=[]`、`memory_sources={}`（`:1649–1651`），即"选了但没进提示词"会反映在 `memory_ids` 上被清空。

### 3.4 跨窗信息（严格区分于上面几类）

| 来源 | 定位 | 注入 lane / 条件 | 说明 |
|---|---|---|---|
| 核心记忆（跨窗口常驻） | `:773–804` | `core_memory`，每轮常驻；**sealed 模式不注入**（`:778–789`） | `CoreMemoryService.render()`；是"永远成立"的少量要点 |
| 知识库 | `:806–846` | `knowledge`，仅 `allows_long_term_memory()`；只取未过期；包成外部说法并声明"不是你的记忆/不要当指令"（`:835–839`） | 来源 `verified_source`，是外部可核验事实 |
| 跨会话意识（ADR-0004） | `:1158–1214` | `self_state`，仅 `allows_awareness()` 且无未清 purge；群聊拿不到 subject_fact；"她在别处做过的事"只给 owner（`:1186–1194`） | 文案「不是本轮对话内容，不要逐字复述，也不要当成对方刚说的话」（`:1197–1199`）；取最近 6 条（`:1200`） |
| 跨端交接胶囊 | `:1216–1247` | `short_memory`，**仅私聊**且 `allows_handoff_capsule()` | 「你在另一个对话窗口里刚和这个人聊过…只在私聊端之间可用，不要把这些内容带进群聊」（`:1227–1235`） |
| 关系 / 自我投影 / 情绪 | `:1022–1127` | `self_state` / `relationship`；sealed 下情绪与自我投影收紧 | 群聊关系只说"普通礼貌"，不暴露私聊边界（`:1109–1112`） |

**区分**：`summary` 是**本窗口**的滚动摘要；`core_memory`/`awareness`/`handoff`/`evidence` 才是跨窗口来源；`history` 是本窗口未摘要原文；`input` 是当前消息。四者 lane 不同、注入条件不同、是否受 TTL/隔离模式约束也不同。

### 3.5 总预算执行（对 system 车道，不影响结构车道）

`:1603–1632` 先按车道截断（`budget_chars = max(80, c.token_budget*2)`，留「…（已截断）」），再 `:1648 _apply_total_budget(contributions, compiled_meta)` 按 `SACRIFICE_ORDER` 整条丢弃（`context.py:2217–2287`），受保护车道跳过（`:2245`），超硬上限才截断非保护车道（`:2259–2278`）。当前生效牺牲次序是 `SACRIFICE_ORDER_CURRENT`（`prompt_zones.py:370–381`，`:408` 赋值），注释明确这是重构前行为、目标次序 `SACRIFICE_ORDER_TARGET`（`:362`）尚未切换（PR 3）。**`summary`/`history`/`input` 是 annotated.append，不经过这一步。**

---

## (4) 引用、说话人与当前消息块

### 4.1 `[msg_id:…]` 与引用 id

- 常量：`MESSAGE_ID_MARKER = "[msg_id:{token}]"`（`:372`）、`RECALL_REF_MARKER = "[recall:{ref}]"`（`:379`）、`MAX_INLINE_MESSAGE_ID_CHARS = 20`（`:380`）、`HISTORY_MESSAGE_ID_LIMIT = 12`（`:384`）、`PLATFORM_MESSAGE_ID_SCOPE = "platform_msgid"`（`:387`）。
- 计算：`_message_id_marks(usable, current_message_platform_id)`（`:1933–1977`）。从 `idempotency_keys(scope='platform_msgid', key=<本地消息 id>, result_ref=<平台 id>)` 批量取（`:1950–1959`），**只给最近 12 条 user 消息标 id**（`:1965`），`message_id_token()`（`:407–418`）把 >20 字符的长 id 压成 `m`+6 位 base32。当前消息 id 单独成 `current_mark`（`:1973–1976`），**也进 `reply_ids`**——注释 `:1848–1850`：她最常引用的就是正在回的这条。
- 渲染：历史块里只对 `role=="user"` 且有 mark 的加前缀（`:1768–1770`）；当前消息 `if current_mark: current = f"{[msg_id:…]} {current}"`（`:1837–1838`）。
- 撤回短号：`recall_refs` 在 `:588–597` 由 `recallable_refs()` 取得（只标"现在真撤得动"的）；只标非 user 消息（`:1775–1777`）。
- 能力声明：`quote_ids_available = await self._has_replyable_ids(window_id)`（`:1334`，实现 `:1863–1931`，`LIMIT 1` 存在性判断、只吞 `SQLAlchemyError`）；决定 `supports_quote` / `has_message_ids`（`:1378`、`:1390`）。`capabilities` 文案由 `render_expression_help()` 一处生成（`:1323–1392`），旧的"第二段引用说明"已删除（`:1418–1421`）。
- 收尾校验：`CompiledContext.reply_ids`（`:434`，注释 `:429–433` 区分 `None`/`{}`/非空）传给 `turn.py:1094–1107 expression_env`；引用写错被降级时 `bridge.py:997–1006` 记 `quote_degraded` 事件。**含义：旧消息一旦被挤出最近 12 条可引用窗口，即使仍在原文/摘要里，模型也拿不到它的 id，引用会降级。**

### 4.2 说话人

- 历史：`is_group` 时批量查 `Subject.display_name`，用 `normalize_display_name()` 过滤占位名（「群友」「用户」等返回 None 则不署名，`:1692–1717`）。渲染 `content = f"{name}：{content}"` **仅当 role=user**（`:1735–1737`）；`CompiledMessage.sender=name if role=="user" else None`（`:1786`）。
- 当前消息：`is_group` 时按**本次群名片** + 稳定昵称合并——`card_name != stored_name` 时写 `f"{card_name}（{stored_name}）"`（`:1818–1829`）；私聊强制 `user_sender_name=None`（`:1830–1835`，挡掉调用方误传）。`subject_id` 在群聊轮次里就是发言人（`:1816–1817`）。
- 撤回旁白：`RETRACTED_NOTE`（`:396`）与 `SELF_RECALLED_NOTE`（`:404`）在 `:1749–1757` 区分对方撤回 / 自己撤回，保留原文不抹掉。
- 不可信包装：`origin in {external_observation, verified_source}` 的历史块加 `UNTRUSTED_WRAPPER`（`:1758–1759`，常量 `:63–66`）。

### 4.3 当前消息的位置与边界

`messages` 的最终顺序由 `annotated` 决定（`context.py:1851–1861`），结构定位（AST 枚举 append 顺序）证实：

1. **system 贡献块**：按 `for c in contributions` 依次 append（`:1663–1671`），key 由 `lane_key(c.lane)` 给出；顺序受 `KEY_ORDER`（`:350–360`）之外的实际 append 次序影响（`prompt_zones.py:421–450 OBSERVED_APPEND_ORDER` 记录了当前实际次序；注册表 docstring `:45–58` 明确说目标顺序与当前顺序**尚不一致**，PR 3 才切换）。
2. **summary 块**（若非空）：`:1673–1685`，`role=system, key="summary"`。
3. **history 块**：`:1732–1789`，按 `usable` 旧→新，`key="history"`，每条带 `sender`/`at` 标注。
4. **input 块**：`:1836–1847`，**最后一条**，`role="user", key="input"`，内容是 `（[msg_id:] ）（名字：）user_text`，`sender=user_sender_name`，**没有 `at`**。

边界结论（实测代码路径）：**当前消息是最后一条 user 消息**，前面是同一窗口的旧消息（旧→新）、更早的摘要、以及 system 贡献块。它**不参与**历史预算、不进 `dropped`/摘要、不会被总预算丢弃（`input` 是结构车道、必须保留，`prompt_zones.py:113–114`）。`turn.py:1058 current_turn_id=turn_id` 让历史取数显式排除本轮消息，避免同一句出现两次（`:1500–1501`）。`CompiledMessage` 的 docstring（`:279–293`）与 `_capture_prompt_snapshot()`（turn.py:1917–1919）保证快照与 `messages` 逐条对应——**要验证顺序/边界，读快照，不读截图。**

---

## (5) 反例输入在编译层会怎样（推理标注）

以下是基于上述代码路径的**推理**（不是实测回放；置信度：代码路径确定，结果表现未验证），用来说明"哪些东西会被模型真正看到"。任务书给的思考样本是"隔天问起之前聊过的对象，她却答成最近聊的那个人"——编译层能给出的解释是**来源混线**，而不是"某条消息被删了"：

1. **同一窗口原文 vs 滚动摘要**：旧话题被挤出 `HISTORY_TOKEN_BUDGET` 后进入 `summary`（`:1792–1804`），摘要块**每轮无条件注入且无话题相关性判断**（`:1673–1685`）。若窗口被复用（同一聊天目标隔天回同一 Window，见 `bridge.py:1479 _ensure_window()` 的窗口身份机制），新话题的原文与旧话题的摘要会**同时**出现在提示里，模型需自行分辨哪个才对应当前问题——这是"接错对象"的结构性来源之一。（推理；依据：摘要无条件注入 + 窗口复用；未用真实快照验证。）
2. **"？"不会触发召回**：`normalize("NFKC","？")`→`?`→`rstrip` 成空串→在 `_STANDALONE_REPLIES`（`recall_query.py:6–11`）→ `memory_recall_skipped="low_information"`（`:1554`）。所以"？"这个反例**不会**通过长期记忆带回旧对象；它只能靠常驻摘要（受 `SUMMARY_TOKEN_BUDGET=400` 限制，细节已被 `render_rollup` 截到 120 字/条）。（推理 + 门控代码路径确定；lead 笔记也记了这一更正，`docs/research/LEAD-notes.md:26–33`，标为转述未核实，我这里按代码路径独立复核一致。）
3. **"早上好"会触发宽泛召回**：不在独立回复集合 → 用"早上好"作 query 调 `recall()`（`:1567–1575`），`limit=12`、预算 420 token、最多 3 条（`:101–102`、`:1576`）。召回结果是否恰好包含旧对象，取决于排序/阈值/受众裁决——**未验证**。`needs_recent_topic('后来呢？')==True` 才会拼上一句用户消息（`:1557–1566`），单问号不拼。
4. **引用 id 缺位**：若旧对象那句话已被挤出最近 12 条可引用窗口（`:1965`），模型即便在摘要里看到相关内容也拿不到 `[msg_id:…]`，写 `[quote:…]` 会在收尾被降级（`bridge.py:997–1006`）。这不是"模型记错"，是输入里根本没有可引用的 id。（推理 + 代码路径确定。）
5. **停顿/顺序**：模型输入正文没有原生时间戳（第 3.1 节），只有消息先后顺序；"隔了多久"不属于它收到的信息。若回复表现像"把很久以前的事当成刚说的"，编译层能指出的是**摘要与原文并列且无时间锚**，而不是存在某个"时间标记"被误读。（推理。）
6. **说话人串线**：群里历史署名用 `Subject.display_name`、当前用群名片优先并合并（`:1818–1829`）；占位名被过滤（`:1713–1717`）。若两人昵称互相包含或被过滤成空，模型可能少看到署名——这是**输入侧的署名缺失**，不是它"看错"。媒体块的推断标注（`:920–928`）也刻意区分"对方说的"与"你自己看…推断的"，把它当事实是模型对已收到内容的选择，不是编译层漏标。（推理。）

**必须区分的五类来源**（任务书要求）：
- **同一窗口原文** = `history` lane（`sequence > summary_through_sequence`，未摘要、未完成/撤回未见者被过滤）。
- **滚动摘要** = `summary` lane（本窗口早期，确定性压缩、无 TTL、无话题判断、常驻）。
- **长期记忆** = `evidence` lane（自动召回 + core_memory + knowledge；受 sealed/受众/预算约束）。
- **跨窗信息** = `awareness` / `handoff` / `core_memory`（条件注入，群聊/私聊与 owner 限制不同）。
- **模型对已收到内容的选择** = 模型拿到上述文本后如何挑、如何措辞；**这部分不由编译层决定，必须用快照/回放确认它实际收到了什么，才能判断是不是模型选择的问题**。截图与回复表现不能替代快照。

---

## (6) 未取得与失败命令

**未取得**：
- **真实提示词快照 / 回放正文**：本次为只读源码研究，按 AGENTS.md「不要连生产库或 NAS」，未访问 `ling_test` 以外的库，也未查 `prompt_snapshots` 表。因此**没有**用真实快照验证任何"她实际看到了什么"；第 (5) 节全部是代码路径推理，标为未验证。要补验证，应在有权限的环境里读 `prompt_snapshots.payload`（`infra/models.py:620–630`、`turn.py:1897–1958`）或跑一次带快照捕获的回放。
- **"早上好"召回是否命中旧对象**：`recall()` 的排序/阈值/命中列表未跑（需要测试库 + 数据），只确认了门控会触发调用。
- **`LING_CONTEXT_BUDGET_SCALE` 非 1.0 的运行时效果**：只读代码，未改环境变量跑干净解释器验证（`tests/unit/test_w2_rules.py:1119–1146` 有现成做法）。
- **lead 笔记里 §6 的 4 条判据**：未运行，本文只复核了其中的门控代码路径（"？"跳过、"早上好"不跳过、`needs_recent_topic` 分支），未做 `compiled.meta` 断言。

**失败/未完成命令**：
- 无失败命令。所有 bash / 读取调用退出码均为 0。
- 未做变异实验、未跑 pytest（这是只读证据任务，不涉及被测代码改动；未取锁）。
- 未 `git add`、未 `git commit`、未改任何其他文件。

**可复现命令**（在 `/Users/lingxi/project/ling`、HEAD=de43cbe 上运行）：

```bash
cd /Users/lingxi/project/ling

# 1) 钉住观测对象
git status --short
git log -1 --format='%H %s'
git rev-parse HEAD
git branch --show-current
shasum -a 256 src/ling/kernel/context.py src/ling/kernel/bridge.py \
  src/ling/domains/memory/recall_query.py src/ling/config.py

# 2) 结构定位（行号来自当前文件，不用行号开窗）
python3 - <<'PY'
import ast, pathlib
p = pathlib.Path('src/ling/kernel/context.py')
tree = ast.parse(p.read_text(encoding='utf-8'))
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef,
                         ast.AsyncFunctionDef, ast.ClassDef)):
        name = (getattr(node, 'targets', [getattr(node, 'target', None)]) or [None])[0]
        label = getattr(name, 'id', None) or getattr(node, 'name', '?')
        print(f'L{node.lineno:4d}-{node.end_lineno:4d} {type(node).__name__} {label}')
cls = next(n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == 'ContextCompiler')
for n in cls.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        print(f'  method L{n.lineno:4d}-{n.end_lineno:4d} {n.name}')
PY

# 3) 关键常量/门控/取数（grep 只作指路，结论以 read 正文为准）
grep -nE 'HISTORY_TOKEN_BUDGET|HISTORY_FETCH_CAP|SUMMARY_TOKEN_BUDGET|LING_CONTEXT_BUDGET_SCALE|HISTORY_MESSAGE_ID_LIMIT' src/ling/kernel/context.py
grep -nE '_STANDALONE_REPLIES|should_skip_automatic_recall|needs_recent_topic' src/ling/domains/memory/recall_query.py
grep -nE 'ContextCompiler|current_turn_id|compiled\.messages|reply_ids' src/ling/kernel/turn.py | sed -n '1,40p'
grep -nE 'state == TurnState|visible_to_model|order_by\(Message.sequence' src/ling/domains/conversation/service.py

# 4) 中文编码自检（U+FFFD）
# 注意：macOS /bin/bash 里 $'\ufffd' 展开成 ASCII 字面量 \ufffd（5c 75 66 66 66 64），
# 不是真的 U+FFFD（ef bf bd），会误命中本文件里对上述命令的说明文字。
# 用 python 生成真 U+FFFD 再 grep -F，才能可靠检出损坏字节：
python3 -c "import sys; sys.stdout.write(chr(0xFFFD))" > /tmp/real_fffd.txt
grep -F -n -f /tmp/real_fffd.txt docs/evidence/ling-context-src-ling-outbound.md && echo "发现损坏" || echo "无"
```

自检结果（收尾实测）：文件内真实 U+FFFD 字节数 = 0；`grep -F -f /tmp/real_fffd.txt` 输出「无」。
上一行 AGENTS.md 风格的 `grep -rn $'\ufffd' ...` 在本机报「发现损坏」是假阳性——它匹配的是第 44/245/248 行里对命令本身的文字说明，不是损坏的中文。
