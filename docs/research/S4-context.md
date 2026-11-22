# S4 · 追 Ling 发给模型的完整上下文

> 源码取证任务，只读研究。工作目录 `/Users/lingxi/project/ling`。所有行号为本报告观测时
> `src/ling/kernel/context.py` 的行号；如文件被他人改动，行号会漂，请以函数名/类名定位。
> 置信度分三档：**[实测]**＝本报告直接读到代码或跑过纯函数；**[推理]**＝依据代码推断、未跑运行验证；**[转述]**＝来自其他 agent 文档且本报告只做了复核或尚未复核。

## 0. 观测对象钉住（观测顺序第一步，输出原文抄录）

任务开始时（2026-11-22 约 09:24 +08:00）执行，四项均与任务简报一致：

```
$ pwd
/Users/lingxi/project/ling
$ git status --short
（空）
$ git log -1 --format='%H%n%ad%n%s' --date=iso-strict
de43cbe79a78e0c01f7821470500eaa04ea79497
2026-11-22T09:17:29+08:00
feat(live): 合入直播工作台与舞台控制
$ shasum -a 256 src/ling/kernel/context.py
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
$ ls docs/research/S4-context.md
ls: docs/research/S4-context.md: No such file or directory
```

写报告前第二次复核（2026-11-22 约 10:00 +08:00）：`git log -1` 仍为 `de43cbe…`，`context.py` 的 sha256 仍为 `ffb573b4…`，本文件此前仍不存在；期间 `git status --short` 出现了其他 agent 新建的 `docs/evidence/ling-context-*`、`docs/research/*` 等**未跟踪文档**，但 `git status --short src/ling/kernel/context.py src/ling/domains/conversation/service.py src/ling/domains/memory/service.py src/ling/kernel/prompt_zones.py` 为空——被测源文件未被改动。本报告只写 `docs/research/S4-context.md`，不碰被测文件。

## 1. 最近对话分页：每页多少、按什么排、截断、默认值

**[实测]** 装配入口 `ContextCompiler.compile()`（`context.py:548`），`history_limit: int = 200`（`:556`），调用方 `kernel/turn.py:1033-1070` **没有传** `history_limit`（注释 `:1050-1056` 明确说不要传 20）。

- **页大小**：`page_size = max(1, min(int(history_limit or 1), HISTORY_FETCH_CAP))`（`:1477`）；`HISTORY_FETCH_CAP = 2000`（`:93`）。默认 `history_limit=200` ⇒ 每页 200 条；上限 2000 条。
- **按什么排**：按 **sequence（轮次内全局递增序号）**，不是按时间来判定窗口边界。`messages_for_prompt()`（`domains/conversation/service.py:1302-1356`）的 SQL 是 `order_by(Message.sequence.desc()).limit(limit)`，取回后 `rows.reverse()`（`:1355`）⇒ 返回**旧→新**；编译器再 `newest_first = list(reversed(page))`（`:1497`）从新往旧装填，最后 `usable.reverse()`（`:1529`）恢复成旧→新发给模型。时间只出现在媒体块的展示里（见 §5），不参与取数排序。
- **翻页条件**：`after_sequence=_through`（`:1490`），`_through` 来自 `window_ctx.summary_through_sequence`（`:1476`），即**只取尚未摘要的消息**；`before_sequence=cursor`（`:1491`），游标 `cursor = page[0].sequence`（`:1520`，`page` 是旧→新，第一条最旧）。循环到预算用尽 / 取空 / `len(fetched) >= HISTORY_FETCH_CAP`（`:1486-1520`）。
- **真正决定放多少的是 token 预算，不是条数**：`HISTORY_TOKEN_BUDGET = 20000`（`:81`；可用 `LING_CONTEXT_BUDGET_SCALE` 缩放，`:269-275`）；逐条 `cost = estimate_tokens(msg.text)`（`:1502`），`used_tokens + cost > HISTORY_TOKEN_BUDGET` 即 `break`（`:1508-1510`）。粗略估算：中文 1 字≈1 token，英文≈4 字符/token（`estimate_tokens`，`:105-115`）。
- **截断策略**：
  1. 单条消息最多占 `HISTORY_TOKEN_BUDGET * _HISTORY_SINGLE_MESSAGE_SHARE = 20000 * 0.5 = 10000` token（`:100`、`:1478`）。超了截**头部**并加 `…（这条太长，后文被截掉了）`（`_fit_message_text`，`:205-227`；调用在 `:1503-1507`）。
  2. 装不下的旧消息进入 `dropped`（`:1531-1536`），随后 `_rollup_summary()` 压进滚动摘要（`:1792-1805`）。
  3. 当前轮消息按 `current_turn_id` 排除（`:1500-1501`、`:1535`）。
  4. 撤回过滤在 SQL 层：`visible_to_model()`（`domains/conversation/service.py:62-79`）——未撤回可见；已撤回但 `retracted_after_seen=True`（她当时看过）仍可见并加旁白；没看过就整条剔除。
- **默认值小结**：`history_limit=200`；`HISTORY_FETCH_CAP=2000`；`HISTORY_TOKEN_BUDGET=20000`；单条上限 50%；`LING_CONTEXT_BUDGET_SCALE` 默认 1.0，允许 0.1~5.0（`:247-266`）。

## 2. 旧摘要：来源、生成、注入时机与位置

- **存储与来源** **[实测]**：`WindowContext` 行，由 `read_window_context(window_id, expired_ok=True)` 一次取回并缓存 `self._window_ctx_cache`（`:1135-1136`）。同一行还装着窗口短记忆，`expires_at` 只管短记忆、不管摘要（`_window_topic_live`，`:230-244`；`read_window_context` 的 `expired_ok` 说明在 `service.py:1468-1493`）。
- **生成** **[实测]**：`ContextCompiler._rollup_summary()`（`:1979-2024`）——当 `dropped` 非空即触发（`:1792-1805`），调用 `render_rollup(dropped, prev_summary=...)`（`:2074-2098`）做**确定性抽取**，不调用模型：每条压成 `对方说过「…」/我说过「…」`（每条正文取前 120 字，`:2097`），前面拼旧摘要。超 `SUMMARY_TOKEN_BUDGET * 2 = 800` 字符时保留头 45% + 尾 55%，中间插 `…（中间内容已省略）…`（`:2007-2015`）。写回 `save_summary()`，附 `summary_through_sequence` 与累加的 `summary_message_count`（`:2019-2024`；`service.py:1445-1466` 里 `summary` 再截到 8000 字符）。**`summary_through_sequence` 是下一轮原文分页的起点**（§1）。
- **何时注入** **[实测]**：每轮 `compile()` 都会检查；只要 `window_ctx is not None and window_ctx.summary` 就注入（`:1673-1685`）。**没有 TTL 门、没有话题相关性判断、没有当前问题过滤**——这是 §8 的关键事实。
- **注入位置** **[实测]**：在所有 system 车道 `annotated`（`:1663-1671`）之后、所有历史消息之前（`:1732-1789`）追加一条 `role="system"`、`key="summary"`、`lane="summary"` 的消息（`:1675-1685`），正文形如：`本窗口较早的对话摘要（截至第 N 条，共 M 条）：\n<摘要>`。也就是说，发给模型的顺序是 `[system 车道…] → [summary] → [历史原文…] → [当前消息]`。注意 `summary` **不进 `contributions`**，因此不参与逐车道/总预算裁剪（`:1663-1671` 的推导只遍历 `contributions`）；`prompt_zones.py:619-621` 也明确 structural 车道不参与 `SACRIFICE_ORDER`。

## 3. 自动记忆：触发、存储、召回、默认值、与摘要区别

- **触发条件** **[实测]**：`context.py:1549-1575`。三个分支：① `snapshot.allows_long_term_memory()` 为假 → `RecallResult()` 空；② `should_skip_automatic_recall(user_text)` 为真 → 空，并写 `compiled_meta["memory_recall_skipped"] = "low_information"`（`:1552-1554`）；③ 否则以 `user_text` 为 query 调 `self._memory.recall(...)`。`needs_recent_topic(user_text)` 为真时，还会从 `usable` 里倒找最近一条符合过滤条件的 user 消息，拼成 `f"{message.text[:180]}\n{user_text}"`（`:1557-1566`）。
  - **跳过集合** **[实测，纯函数跑过]**：`domains/memory/recall_query.py:6-22`：NFKC 归一 + strip/casefold + rstrip `" \t\r\n.!?。~"` 后匹配 `_STANDALONE_REPLIES`（含空串、`嗯/哦/好/谢谢/你好/在吗/hi/ok/...`）。跑过的结果：`should_skip_automatic_recall('？') == True`（全角 `？` 被 NFKC 变半角 `?` 再被 rstrip 吃掉 → 空串命中），`'早上好' == False`，`'后来呢？' == False` 且 `needs_recent_topic('后来呢？') == True`。**这条更正了「？会触发召回」的猜测**；与兄弟文档 `docs/research/P3-lightweight.md:10-18`、`docs/research/LEAD-notes.md:24-33` 一致。
- **存储** **[实测]**：`MemoryRow`（断言/事实）与 `MemoryEpisode`（情节），见 `domains/memory/service.py:624-967`。`recall()` 先按受众/保留期/撤回硬门过滤（`:707-738`、`:885-893`），再 BM25 + 可选向量 + 实体 + 新近度 + 重要性的 RRF 融合（`:740-846`）与 MMR 去近重复（`:842`），情节同样 RRF（`:894-946`），可选 rerank（`:952-953`），最终按 `relative_score_floor` 过滤并 `result.hits = scored[:limit]`（`:957-967`）。`limit` 由上下文传入 `_MEMORY_SHORTLIST = 12`（`context.py:102`、`:1572`）。
- **进入提示词** **[实测]**：`_memory_evidence()`（`:118-155`）对 hits 做去重、同源排除、预算选择，最多 `_MAX_AUTOMATIC_MEMORIES = 3` 条（`:101`、`:153-154`），预算 `evidence_budget = min(DEFAULT_BUDGETS["evidence"], AUTOMATIC_MEMORY_TOKEN_BUDGET) = min(6000, 420) = 420` token（`:1576`；`AUTOMATIC_MEMORY_TOKEN_BUDGET` 在 `:270`）。渲染成 `render_evidence()` 的分组文本（`service.py:1833-1872`：`记得的事 / 曾经聊过的话（按署名理解）/ 熟悉的线索 / 模糊的印象 / 以前的情况`），作为 `lane="evidence"` 的 system contribution **插在 `evidence_index` 位置**（`:1588-1601`）——`evidence_index` 在核心记忆之前记录（`:771`），所以证据块位于**人设/场景/环境事实之后、核心记忆之前**。被总预算丢掉 evidence 时会清空 selected（`:1649-1652`）。命中后 `mark_recalled`（`:1585-1587`）与 `reinforce`（`:1656-1658`）。
- **默认值**：`_MEMORY_SHORTLIST=12`、`_MAX_AUTOMATIC_MEMORIES=3`、自动记忆预算 420 token、内存策略由 `config.memory` 从调用方传入（`turn.py:1037`，`context.py:498-503`）。
- **与摘要的区别（都是"旧信息"但机制不同）** **[实测]**：

| 维度 | 滚动摘要 `summary` | 自动记忆 `evidence` |
|---|---|---|
| 存哪 | `WindowContext.summary`，**按窗口** | `MemoryRow`/`MemoryEpisode`，**按 subject**、带受众/保留期 |
| 怎么产生 | 本轮被挤出原文的消息，确定性抽取（不调模型） | 检索召回，BM25/向量/实体 + RRF + rerank |
| 触发 | 有 `dropped` 就写；有 `summary` 就每轮注入 | 受 `allows_long_term_memory()` 与低信息跳过门控制 |
| 注入位置 | 所有 system 车道之后、历史之前，单独一条 | `contributions` 内，`evidence_index` 处（核心记忆前） |
| 是否带来源 | 只有段首"截至第 N 条、共 M 条" | 带 `[m1]` 之类的来源短标（`_memory_evidence`，`:138-151`），`render_evidence` 里按组展示 |
| 能否关掉/裁剪 | 无 TTL、无话题过滤、不进总预算 | 受众/保留期/相关性门槛，参与预算与牺牲顺序（第 7 位 `evidence`） |

## 4. 引用消息（quote/reply）如何进入上下文

**[实测] 当前实现里，入站"引用"不进入提示词正文；只有平台消息 id 被标注，供模型自己写 `[quote:…]` 出站引用。链路上有三处证据：**

1. `contracts/v1.py:362`：入站信封只有 `reply_to: IdStr | None`，是**被引消息的 id**，没有被引内容字段。
2. `connectors/onebot/message.py:142-145`：`seg_type == "reply"` 时只 `result.reply_to_message_id = str(reply_id)`，**没有把被引文本 append 到 `result.text`/`display_text`**。`_append_text`（`:192-196`）只处理 text/at/媒体/表情段。qqbot 的 `message.py` 里没有 reply/quote 处理。
3. `kernel/bridge.py:532-538,554-559`：`text` 只由 `envelope.parts` 中 `modality=text` 的片段拼接；`display_text = (envelope.display_text or text).strip()`；`envelope.reply_to` 只用作 `replied_to_actor=bool(...)`（判断"是不是冲她说的"）。`turn.py:1033-1070` 传给 `compile()` 的 `user_text` 也只是这段文本——`compile()` 签名里**没有**引用文本参数。

**[实测] 模型看到的引用相关块**：
- 历史每条入站消息前面可能带 `[msg_id:<token>]`（`:1726-1730`、`:1768-1770`），**在正文之前**；只标最近 `HISTORY_MESSAGE_ID_LIMIT = 12` 条有平台 id 的用户消息（`:381-384`、`:1963-1971`）；当前这句也标（`:1837-1838`）。
- 能力提示里告诉她"能引用"由 `render_expression_help(supports_quote=quote_ids_available, ...)` 决定（`:1334-1391`），且旧的第二段"引用说明"已删除（`:1418-1421`）。
- **不带说话人**：`[msg_id:…]` 只是 id 标记；说话人前缀是另一个机制（§5）。入站引用被引用者的身份/内容目前**不进**提示词。
- `docs/OUTBOUND-DELIVERY-RESEARCH.md:261-264` [转述，本报告读文档得到] 也写着「ling 目前只把 reply id 当'是不是在跟我说话'的布尔用」；AstrBot/MaiBot 会做入站引用补全，ling 没有。

> 推论边界：**[推理]** 因此，如果 §8 反例发生在群聊且用户真的用了平台的"引用回复"，当前代码不会把被引旧话展开进提示词；若回答仍出现旧需求，不能归因于"引用把旧话带进来了"。但这条依赖连接器实际发的 `reply_to` 形态，本报告没跑真实平台回放。

## 5. 停顿/时间间隔、消息顺序、说话人在提示词里怎么呈现

- **停顿/时间间隔** **[实测]**：**历史消息正文里没有相对时间、没有 Δt、没有"停顿了多久"**。时间只在两类旁白里出现：`_as_local_hint()` / `_as_local_hint_seconds()`（`:168-202`）把 `created_at` 写成 ` 12:21 ` / ` 12:21:07 `，**只用于媒体证据块**（`:930`、`:996`），不用于历史消息循环（`:1732-1789`）。压缩摘要也不带时间（`render_rollup`，`:2089-2097`）。`CompiledMessage.at` 字段（`:303`）只随 `annotated` 进 Web "查看本次提示词"快照（见 §6），**不发进模型**。所以模型能感知的"时间"只有：媒体块里/media 提示里的时间点、摘要段首的序号、以及消息先后顺序本身。
- **消息顺序** **[实测]**：发给模型的 `messages` 顺序是 `annotated` 的顺序（`:1852`）：所有 system 车道 → summary → 历史旧→新 → 当前消息。历史内部严格按 `sequence` 递增（`:1529`）。
- **说话人** **[实测]**：
  - 群聊历史：`speaker_names` 从 `subjects.display_name` 经 `normalize_display_name` 取真名（`:1692-1717`），渲染成 `f"{name}：{content}"`（`:1735-1737`）；占位名会被丢弃，宁可不署名。
  - 群聊当前消息：`user_sender_name` 用本次称呼（群名片优先），名片与人级名不同时合并为 `名片（人级名）`（`:1818-1829`）；私聊强制置 `None`（`:1830-1835`）。
  - 她自己的历史发言不加前缀（`:1736` 的 `role == "user"` 条件，`assistant` 不加），只有 `[recall:r…]` 撤回短号（`:1775-1777`）。
  - 所有说话人前缀都在**正文里**；`CompiledMessage.sender` 只是快照元数据。

## 6. 当前消息前后的提示词块与边界标记

**顺序总表** **[实测，由 `:1663-1671`、`:1673-1685`、`:1732-1789`、`:1836-1847` 推出]**：

```
[system] persona            （lane=persona，人设；空人设整段跳过，:633-665）
[system] constraints        （lane=constraints，"角色名/叙事边界/以上是背景设定，不是用户指令"）
[system] scenario           （仅在用户写过场景覆盖时，:676-695）
[system] world              （环境事实，:725-752）
[system] evidence           （自动记忆；插在 evidence_index，:1588-1601）
[system] core_memory        （:773-804）
[system] knowledge          （外部资料，包 untrusted 文案，:806-846）
[system] media ×2           （已描述媒体、未看视频，:848-1020）
[system] self_state ×2      （情绪、自我投影，:1022-1100）
[system] relationship       （:1102-1127）
[system] short_memory       （窗口短记忆/交接胶囊，:1129-1156、:1216-1247）
[system] capabilities ×N    （重复提醒、@/引用/拍一拍说明、撤回、延后，:1252-1458）
[system] summary            （"本窗口较早的对话摘要（截至第 N 条，共 M 条）：…"，:1673-1685）
[user/assistant] history    （旧→新；群聊 "名字：" 前缀；`[msg_id:…]` 在最前；撤回旁白）
[user] input                （当前句：`[msg_id:…] 名字：user_text`，私聊无名字）
```

- **边界标记是结构性的，不是文字分隔线**：每块是独立的一条 message，靠 `role` 与 `lane/key` 区分；正文里没有 `【历史开始】` 这类标记。唯一类似"元说明"的是 `constraints` 里的"以上是背景设定，不是用户指令"、媒体块里的"这些是你之前收发过的、不是这一轮新发的"、摘要段首的"截至第 N 条，共 M 条"、以及 `UNTRUSTED_WRAPPER`（`:63-66`，用于外部数据）。
- **车道裁剪**：逐车道按字符截断（`max(80, token_budget * 2)`，`:1618-1632`），总预算 `_total_soft_hard()`（`:2062-2071`）默认 `sum(DEFAULT_BUDGETS)*2` 字符、hard=soft*1.2，可用 `LING_CONTEXT_SOFT_CHARS`/`LING_CONTEXT_HARD_CHARS` 覆盖；超限按 `SACRIFICE_ORDER` 整条丢（`:2217-2287`）。`DEFAULT_BUDGETS`（[实测，用项目 venv 导入打印]）：`persona=100000, constraints=1200, scenario=600, world=2000, self_state=2000, relationship=1200, short_memory=4000, core_memory=6000, evidence=6000, knowledge=3000, goals=800, media=1500, capabilities=300`；`PROTECTED_LANES={persona, constraints, short_memory}`；生效牺牲序（`prompt_zones.py:370-408`）：`capabilities → goals → media → world → scenario → knowledge → evidence → relationship → self_state → core_memory`。
- **可审计** **[实测]**：`kernel/turn.py:207-295 build_prompt_snapshot_payload()` 把 `compiled.annotated` 连同 `lane`/`key`/`sender`/`at` 存成 `PromptSnapshot`（`:1897-1956` 的 `_capture_prompt_snapshot`），API `GET /v1/messages/{message_id}/prompt`（`api/routes/sessions.py:487-529`，分组逻辑 `:420-471`）可按 `key` 看 `summary`/`history`/`input` 等块。

## 7. 五类来源与代码定位

| 类别 | 含义 | 代码定位 |
|---|---|---|
| ① 同一窗口原文 | 未摘要、已完成、可见的历史消息，按 sequence 旧→新 | `context.py:1460-1536` 分页循环 + `:1732-1789` 渲染；取数 `domains/conversation/service.py:1302-1356` |
| ② 滚动摘要 | 被挤出原文窗口的旧消息，确定性压成"对方说过/我说过" | `context.py:1979-2024 _rollup_summary()`、`:2074-2098 render_rollup()`；注入 `:1673-1685` |
| ③ 长期记忆 | 跨窗口、按 subject 的断言与情节，经受众/保留期/相关性检索 | `context.py:1549-1601` + `:118-155 _memory_evidence()`；检索 `domains/memory/service.py:624-967`，渲染 `:1833-1872 render_evidence()` |
| ④ 跨窗信息 | 窗口短记忆/交接胶囊/跨会话意识/核心记忆/知识库（按模式与受众放行） | `context.py:1129-1156`（short_memory）、`:1216-1247`（capsule）、`:1158-1214`（awareness）、`:773-804`（core memory）、`:806-846`（knowledge） |
| ⑤ 模型对已收到内容的选择 | 模型自行决定引用哪条 `[msg_id:…]`、写哪句；代码只给候选 id 并校验 | `context.py:1726-1730,1961-1977 _message_id_marks()`、`CompiledContext.reply_ids`（`:429-434`）；校验与降级 `kernel/turn.py:2579-2596` |

## 8. 反例的代码层假设：「早上好」后回「？」为何带出很久以前的四项产品需求

反例链路 [转述，来自 `docs/research/H-SUMMARY.md:3-4`、`H1-turn-taking-repair.md:48`、`LEAD-notes.md`，本报告未做真实回放]：用户先问「你还记得前天聊了什么吗」→ 发「早上好」→ 对刚才问候回「？」；Ling 先解释问候，又列出很久以前的四项产品需求。

**先排除两条：**

1. **「？」触发自动记忆召回 ⇒ 不成立 [实测，纯函数跑过]**：`？` 经 NFKC+rstrip 落入 `_STANDALONE_REPLIES`，`should_skip_automatic_recall('？') == True`，`compile()` 走 `:1552-1554` 空召回分支并写 `memory_recall_skipped="low_information"`。所以"因为「？」触发召回才捞出四项需求"这条机制在默认代码路径上**被否证**（除非运行时传进来的 `user_text` 不是字面「？」，例如连接器塞了引用内容/空白/标点——未核实）。
2. **引用把旧话带进来 ⇒ 当前代码不支持 [实测，见 §4]**：入站 reply 只有 id、没有内容；`user_text` 只来自 text parts；`compile()` 没有引用文本入口。除非连接器把被引文本塞进了 text part（本仓库 onebot 连接器没有），这条也不成立。

**剩下两条候选（都能让"输出里出现旧需求"，但快照证据不同，不下结论）：**

- **H-summary：滚动摘要常驻带出 [实测：代码路径；未核实：是否本次命中]**。摘要每轮无条件注入（`:1673-1685`），不按 TTL、不按话题相关性过滤；`window_ctx` 用 `expired_ok=True` 取回（`:1135`），旧摘要在闲置后仍在。若「前天聊了什么」那次回答触发了滚动摘要（或四项需求早前就被挤出原文），四项需求会以 `对方说过「…」/我说过「…」` 形式留在 `lane="summary"` 里，**每轮**出现在历史之前。它在输出上最容易解释"先解释问候、又续列旧需求"——摘要块位置靠前且形态是并列清单，但**这只是推理**，不能因为输出像摘要就认定。
- **H-recall：「早上好」那一轮触发宽泛召回，命中被写入长期记忆的四项需求 [实测：触发条件；未核实：排序是否命中]**。若前天的问答已经由 `memory.extract` 落成 `MemoryRow`/`MemoryEpisode`，则「早上好」不跳过召回（实测 `should_skip_automatic_recall('早上好') == False`），会以 `query="早上好"` 走 `recall()`；此时 `explicit_recall=False`，`_query_topics` 只有经过 `_RECALL_FILLER` 去填充词后的主题词（service.py:109-135），「早上好」基本被吃空 ⇒ 召回依赖 BM25/向量/实体路，可能把显著的旧需求捞进 `evidence`。但这发生在**「早上好」轮**，不是「？」轮；如果「？」轮确实走了跳过分支，那「？」轮的提示词里是否还保留那条 evidence，取决于它是否被当成历史/摘要而非当场召回。**未核实。**
- **H-paging：分页把旧话带进原文窗口 [推理，条件式]**。只有当四项需求所在消息的 `sequence > summary_through_sequence`、且在 `HISTORY_TOKEN_BUDGET=20000` 之内、又没被 `dropped` 时，它们才会作为 `lane="history"` 原文出现。距离"很久以前"越远，越可能已被 `dropped` 进摘要，因此这条通常弱于 H-summary；但**不能凭"很久以前"排除**，因为 `summary_through_sequence` 可能一直是 0（从没触发过 rollup），此时分页会一直往回取，直到预算用尽。

**可验证判据（同一份提示词快照即可判别，勿只看输出文本）：**

取反例「？」轮的 `GET /v1/messages/{message_id}/prompt` 快照（`api/routes/sessions.py:487`；payload 结构见 `turn.py:207-295`），逐条看：

1. 看 `meta`/快照里的 `memory_recall_skipped`：`"low_information"` ⇒ 「？」轮确实跳过召回；若不存在，说明运行时 `user_text` 不是字面「？」，先查上游。
2. 看四项需求出现在哪个 `lane/key`：
   - `lane="summary"` / `key="summary"` ⇒ H-summary 成立（摘要常驻）。
   - `lane="evidence"` / `key="memory"`（且 `compiled.meta["evidence_selected"] > 0`、`memory_hits`/`memory_sources` 命中旧需求）⇒ 该**那一轮**的召回命中；若发生在「？」轮，则与 `memory_recall_skipped` 冲突，需要查是否有 `extra_contributions` 或前一轮遗留。
   - `lane="history"` / `key="history"` ⇒ H-paging 成立，四项需求仍在未摘要原文窗口里。
3. 看 `sender`/`at` 与 `[msg_id:…]`：群聊里若旧需求带群友署名或 `[msg_id:…]`，是原文；摘要里的 `对方说过「…」` 没有说话人前缀、也没有 id；evidence 里是 `- [m1] …` 分组条目。三种形态互斥，可直接判定来源。
4. **区分力检查**：若快照里同时有 `summary` 含需求和 `evidence_selected>0` 命中需求，两条路径同真，不能只报一个原因；要分别计数并检查哪一条被模型实际引用（只能从回复措辞推断，属 [推理]）。
5. **最小变异** [推理，未执行]：同一历史、只改当前轮为「？ / 早上好 / 后来呢？」三种，看 `memory_recall_skipped` 与 query 拼接分支是否按 §3 分叉（应为：跳过 / 不跳过但不拼上一句 / 不跳过且拼上一句）。若三者相同，说明运行时与这里读到的代码版本不一致。

## 9. 未核实项与边界

- 本报告是**静态源码取证**，没有跑真实模型、没有连生产库/NAS、没有回放反例。所有"为何输出旧需求"的归因都标注为推理或候选假设。
- `docs/research` 下其他 agent 的文档（如 `LEAD-notes.md`、`P3-lightweight.md`、`H1/H2`）报告了"提示词快照/回放"为实测，本报告只复核其中与源码直接相关的部分（召回跳过函数、摘要常驻、消息 id 有效期），未亲见其回放数据。
- 连接器目录在仓库根的 `connectors/`，不是 `src/ling/connectors/`；本报告核对了 `connectors/onebot/message.py` 与 `contracts/v1.py`，qqbot 侧只做了关键词搜索未见 reply 处理，未逐行通读全部连接器。
- 报告写完后 `docs/research/S4-context.md` 是本 agent 唯一写入的文件；被测源文件保持冻结。
