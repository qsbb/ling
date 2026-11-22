# Ling 上下文研究 · 主控汇总（Lead 版）：第一期改什么

> 基线：`/Users/lingxi/project/ling`，`HEAD = de43cbe79a78e0c01f7821470500eaa04ea79497`（2026-11-22 09:17:29 +0800）。
> 起始 `git status --short` 空；`bridge.py` SHA-256 `334534fe…a94a70`、`context.py` SHA-256 `ffb573b4…718ba`，取证期间未改业务代码与测试。
> 证据档：**[实测]**＝运行/纯函数/真实编译器输出可复现；**[代码路径]**＝读源码未跑运行时；**[推理]**＝可被回放推翻；**[未核实]**＝转述或未取得。
> 另一并行版本见 `docs/research/FINAL-REPORT.md`（168 行，论文原文核实更细，但误记"未取得 P2 真实编译器输出"）；本文件以 P2 已取得的**真实编译器输出**为准，并补入 S1/S2 的开源对照。两份结论方向一致：常驻/随话题带出/明确找回的分区、B 优先、C 不做第一期、不改会话管理。所有样本合成（QQ `100200300`、IP `192.0.2.x`）。

---

## 0. 先回答分区问题

| 层 | 内容 | 现状 | 第一期 |
|---|---|---|---|
| **常驻** | 人设/约束、世界与场景、关系、能力、核心记忆 | 每轮注入，受 `prompt_zones.py` 预算与保护集 | 不动 |
| **随当前话题带出** | 最近未摘要原文、未过期 `topic/open_questions`（TTL 120 分钟）、群聊署名与最近 12 条 `[msg_id:]` | 按 sequence 往回取到 20k token；短记忆按 TTL | **改取数**：最近完整语段优先；无锚点短回复只接眼前上一句；摘要/短记忆按"当前指向"降权 |
| **只在明确需要时找回** | 滚动摘要、长期记忆证据、跨端胶囊、被压缩旧事 | 摘要**每轮无条件注入**（`context.py:1673-1685`）；记忆受门控 | **摘要改为按需注入**（唯一有确定代码路径 + 真实编译器输出支撑的改动）；记忆补「早上好」门控 |

**结论：第一期不改会话管理、不新建窗口；做方案 B（当前轮取数）的最小版本，但先补一组真实编译 + 真实回复对照实验。**

---

## 1. 最小反例：已跑真实编译器（P2）

合成历史写入本地 `ling_test` 私聊窗口，`ContextCompiler.compile()` 直接取 `compiled.annotated/messages/meta`，未调外部模型（脚本 `/tmp/p2_baseline_probe*.py`、原始证据 `/tmp/p2_baseline_evidence*.json`，详见 `docs/research/P2-baseline.md §2`）：

```
user:  产品方案四项需求：A 登录、B 导出、C 通知、D 报表
actor: 好，我记下了，之后接着聊。
user:  早上好
actor: 早呀。
user:  ？      ← 当前轮
```

| 场景 | 旧需求所在段 | 形态 | 记忆召回 | token_total |
|---|---|---|---|---|
| A 短历史 | `lane=history` 第 3 段 user 原文 | 逐字原文 | **未触发**：`meta.memory_recall_skipped="low_information"` | 186 |
| B 长历史（40 条 filler，rollup=30） | `lane=summary` 第 3 段 system 摘要 | `对方说过「产品方案四项需求…」`，确定性抽取 | 未触发 | 20156 |

两条通道输出相同但 `lane` 不同，**判据必须读快照 lane**。B 的时序细节：`window_ctx` 在编译开头已缓存，rollup 当轮看不到新摘要，**下一轮**才注入。

**纠正错误归因**：早期推测「？触发宽泛召回捞出旧需求」被实测否证——`normalize("NFKC","？")→"?"→rstrip→""`，空串命中 `_STANDALONE_REPLIES`，`should_skip_automatic_recall("？")==True`；`should_skip_automatic_recall("早上好")==False`；`needs_recent_topic("后来呢？")==True`（`recall_query.py:6-27`，P2/P3/P4/S4 各自独立验证）。真实编译器交叉验证：`？`轮的 `meta.memory_recall_skipped="low_information"`。因此候选来源收敛为：**同窗原文按时间取数** 与 **摘要无条件常驻**；「早上好」触发宽泛召回的命中与排序**未验证**。

---

## 2. 人类与 IM 证据：旧话题为什么容易被续接

- **H1 轮次与修复**：Sacks/Schegloff/Jefferson (1974) *Language* 50(4):696–735（DOI 10.1353/lan.1974.0010）；Schegloff/Jefferson/Sacks (1977) *Language* 53(2):361–382（DOI 10.1353/lan.1977.0002）。「？」是问候相邻对第三位置的 other-initiated self-repair，偏好自我修复预测 Ling 应重述**自己上一句**，而非把修复扩到旧需求。1974 靠语调判 TCU/TRP，纯文本不可外推。
- **H2 话题边界**：Schegloff & Sacks (1973)、Button & Casey 系列、Yang (2019) 话题管理论文（另一 FINAL-REPORT 已逐段核读）。区分「系统复现」与「会话复入」；反例第 3 轮无锚点，属"焦点仍在"，但机制是被动带出而非显式复入。
- **H3 共同背景与指代**：Clark & Brennan (1991)、Grosz & Sidner (1986)、中心理论。**焦点是随语段维护的注意状态，不是关键词相似度**；"仍在焦点"需未被标记退出且有可及先行词。
- **H4 事件边界**：事件分段（Zacks、Newtson 等；未命中 DOI 者已标未核实）。旧事被重新打开常由边界标记触发，而非固定时长。不得把人类工作记忆容量或论文数字直接当 Ling 参数。
- **M0–M4 IM 综合**：信号优先级 **[推理]** 显式锚点（引用/@/找回词）> 时间位置 > 指代距离 > 在线状态。四类必列信号：「？」澄清刚才那句、「后来呢？」承接、「接着说前天那个方案」显式找回、群聊引用回复。IM 缺线下共同在场，异步把"打断"退化成"追加"；默认**无锚点不续旧话**。看见后延迟回复 vs 隔天晚看的 Δt 同形，不可单凭时间戳判定。

### 2.1 Ling 在 IM 信号上的两个硬缺口

1. **模型看不到时间**：历史正文不带时间戳，`CompiledMessage.at` 只进快照，`ChatMessageIn`（`ports/model.py:418-432`）无时间字段。正文时间只在媒体块（如 ` 12:21:07 `）。"隔天 vs 几秒"只能由**系统侧**判定并体现在取数/标注里。
2. **引用内容不入提示词**：入站只有 `reply_to: IdStr`（`contracts/v1.py:362`）；OneBot 只记 `reply_to_message_id`，不把被引文本拼进 input（`connectors/onebot/message.py:142-145`）；`bridge.py:532-538` 的 `text` 只来自 text parts，`reply_to` 仅作 addressed 布尔；`compile()` 无引用文本参数。模型只看到最近 12 条 `[msg_id:…]`。故"异步引用被当独立新句"不是模型选错，而是**入站链路丢信息**。

---

## 3. 源码取证

### 3.1 Ling 入站（S3，`docs/research/S3-bridge.md`）

窗口复用键 `(actor_id, channel, connector_window_ref/title)`，私聊加 `subject_id`，群聊按群共享；有受信实例时优先 `origin_status=known + origin_scope/origin_instance_id`，否则只接受唯一 `origin_status=unknown` 旧候选（`bridge.py:1556-1635`）。**与话题无关**，无按时间/换题开新窗的代码路径。停顿不参与窗口选择（5 分钟 actor 计数防刷屏 `:1729-1748`；短记忆 TTL 120 分钟）。**bridge.py 无显式换题检测钩子**。潜在 Gap（未核实）：窗口查询未过滤 `archived`。

### 3.2 Ling 出站（S4，`docs/research/S4-context.md`）

顺序 `[system 车道] → [summary] → [history 旧→新] → [当前 input]`；默认 `history_limit=200`、`HISTORY_FETCH_CAP=2000`、`HISTORY_TOKEN_BUDGET=20000`、单条上限 50%=10000 token（超长截头部）；`LING_CONTEXT_BUDGET_SCALE` 默认 1.0（0.1–5.0）。摘要由 `_rollup_summary` + `render_rollup` **确定性压缩、不调模型**（每条前 120 字，800 字符头尾保留），不进 `contributions` 故不受总预算裁剪，**每轮无条件注入、无 TTL/话题门控**。自动记忆 shortlist 12、最多 3 条、预算 420 token（RRF+MMR+可选 rerank，`memory/service.py:624-967`）。群聊历史按 `sender_subject_id` 署名、占位名过滤；当前句群名片+昵称合并；私聊无前缀。

### 3.3 开源对照（S1/S2，本轮新增）

- **AstrBot**（官方 `master` HEAD `feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea`，2026-11-19；codeload tarball sha256 `d29e2714…2a00`；隔离副本 `/tmp/astrbot-s1/feb3e82/repo`，入口 `main.py:131-176`）：
  - **窗口与对话分离**：UMO = `platform_id:message_type:session_id`，OneBot 群消息 session_id=群号、私聊=好友号（`aiocqhttp_platform_adapter.py:215-226`，`astr_message_event.py:175-178`）。UMO 是窗口，`conversation_id`（uuid4）是对话，当前选中存内存 `session_conversations` + `data/shared_preferences.json` 的 `sel_conv_id`（`conversation_mgr.py:40,267-312,349-363`）。
  - **上下文**：主链 `ProcessStage → InternalAgentSubStage → runtime.open_session(umo) → session.run_turn`；原生 runtime 每轮 `read_for_execution` **全量读** `conversation.content` 到 `req.contexts`，无服务端分页（`main_session.py:74-138`、`request.py:376-395`）；`get_human_readable_context` 的 `page=1/page_size=10` 只给人看。发送前 `ContextManager.process` 做轮次截断/LLM 摘要压缩（`tool_loop_agent_runner.py:1204-1221`），默认 `max_context_tokens=0`、`max_turns=-1` ⇒ **默认不压缩**，阈值 0.82、keep_recent_ratio=0.15。**引用消息拼成 `<Quoted Message>` 文本块**（`agent_media.py:190-284`），`<system_reminder>` 含 User ID/群名/时间、`prompt_prefix` 前后置（`agent_input.py:58-120,332,351`）。
  - **自动记忆无独立模块**：真实形态是 `group_icl_enable`（默认 False）的进程内群聊 ICL，经 `OnLLMRequestEvent` 注入（`builtin_stars/astrbot/main.py:324-333`）；以及 `group_message_history_enable`（默认 False）的持久化群历史工具、按需查询（`builder.py:171-182`）。
  - **显式换题真接线**：`/reset`（`builtin_commands/main.py:44-48 → commands/conversation.py:112-141 → update_conversation(history=[]) → db/sqlite.py:690-714`）与 `/new`（`main.py:55-59 → conversation.py:164-197` 新建并切换），两者都设 `_clean_group_context_session` 清群 ICL 缓存；内置 star，非可选插件。
  - **对照启示**：AstrBot 把"窗口（UMO）"和"对话（conversation UUID）"分开，且引用内容真进提示词——这两点恰好是 Ling 缺的。但 Ling 的窗口身份键仍绑定平台 `window_ref`（S3），简单照搬 `/new` 不会自动清摘要/短记忆；而 `<Quoted Message>` 的做法可直接补 §6 B3。端到端运行期行为本轮未实测（按约定未连生产实例），只证源码调用链完整。
- **MaiBot**（官方现址 `github.com/Mai-with-u/MaiBot`；任务书给的 `MaiMwithLove/MaiBot` 返回 404；v1.4.8 / commit `edf42446f123149c57fc79765dd5a8b004b8524c`，2026-11-20；直连 GitHub 超时改走 API tarball 到 `/tmp/maibot-s2`）：
  - 会话 id `SessionUtils.calculate_session_id`（`utils_session.py:8-42`）= MD5(platform + account/scope + group_id 或 user_id+"private")；`chat_manager.py:193-271` 内存→DB→新建复用；account/scope 是**路由隔离**不是换题。
  - 上下文：群 `max_context_size=50` / 私聊 75（`official_configs.py:850-896`），达上限裁到 20%、至少留 5 条真实消息（`post_processor.py:15-17,57-68`，接线 `reasoning_engine.py:1422-1425`）。摘要 `ContextSummaryMessage` 5 类 JSON（topics/background/progress/pending/agreements，`compaction.py:253-277`，接线 `reasoning_engine.py:1435-1475`）。长期记忆 `heuristic_injector.py` 经 `reasoning_engine.py:280-370` → `chat_loop_service.py:1113` 作为 User 消息注入，**已接线**。引用 `_build_reply_references:483-514` 按 target_message_id 从上下文找，找不到用组件字段，不查 DB；目标消息 300 字截断。
  - **显式换题**：`/clear` 是**已接线**（`clear_context.py:7-42` 定义 → `bot.py:57,60-70` 注册开关 core.clear，`disabled_commands` 默认空 → `bot.py:1010-1012` 入站拦截 → `bot.py:471-574` 写 DB marker → `heartflow_manager.py:177-189` 清运行时 → `planner_state.py:88-96` reset → `runtime.py:317/347/427` 恢复历史按 marker 截断）。清的是短期上下文/运行时，不改 session_id、不删长期记忆。未发现独立"换话题"命令。
  - **对照启示**：MaiBot 的换题是"在同一会话里按 marker 截断历史"，而非开新窗口——与 B 的"不新建窗口"方向一致，但 Ling 若要支持需同时清摘要/短记忆，不能只清 history。

---

## 4. 三方案与量化取舍

| 方案 | 内容 | 收益 | 风险 | 判定 |
|---|---|---|---|---|
| **A 对照** | 现状：同窗复用 + sequence 取数 + 摘要常驻 + 门控召回 | 不改代码 | 反例两通道复现（P2）；「早上好」可能宽泛召回；引用内容丢失 | 基线 |
| **B 轻量** | 显式引用/找回锚点 > 承接词 > 时间位置+短回复 > 最近完整语段；摘要按指向降权；补「早上好」门控 | 不动数据模型，直接针对 P2 两通道 | 权重需校准；锚点误判续错题；时间须系统侧判定 | **推荐，但先做对照实验** |
| **C 分段** | 明确上下文/话题分段 | 理论减少异话题混线 | 无真实模型回放、无话题边界标注、无误续/漏续人工标签、无 A/B/C 对照；按区分力判据不可区分 | **不建议第一期**（`docs/evidence/ling-context-plan-c.md`） |

B 优先级（P3）：显式引用/被点名 → 显式找回锚点（时间词+名词）→ 承接词（「后来呢/然后呢/接着」，`needs_recent_topic` 部分覆盖）→ 时间位置+短回复（无锚点「嗯/？」紧接上一句=澄清；隔天首条=新开场）→ 纯时间邻接兜底。现状只实现第 3 条的一部分与第 5 条；`reply_to` 存在 envelope 但只用于"回不回"，未用于"取哪段"。

**主动换题**：现实关联、自然结束、有来源的承诺（`intents`，`autonomy.py:129-158`）分别足以开启；"对方感兴趣"是延续信号。恰当主动/过度主动/错误续聊分开判，不合成单一"主动度"。

**量化（条件式）**：误提旧事看 `messages[].lane` + 回复主语；主动自然度要双评分 + A8 反例（「先别说那个」vs「那个不错」）；漏续聊必须有"接着说前天那个方案/后来呢"正向；指错人/消息看 `reply_ids` 的**消息 id 级**断言（id=被引那条 vs 时间上一条）；输入长度读 `payload.token_total`、`compiled.summary()["chars"]`；额外调用读 `ScriptedProvider.requests`、`behavior_model_calls`、`AttentionPollLogRow.llm_calls`（rollup 不调模型）。

---

## 5. 独立对抗验收（P4，判据先于方案）

- 时序：判据落盘 `2026-11-22T09:28:29+0800`、完成 09:31，抄录 `git status` 之后才读 P3/LEAD。
- 覆盖矩阵：**0 条覆盖 / 1 条部分（D3）/ 5 条无法证明**——当时无真实编译快照，方案自标 `[推理]`/`[代码路径]`/`[不足]`，按"写一句话不算覆盖"口径只能判无法证明。
- 最严重缺陷 **D3 摘要无条件常驻**（`context.py:1673-1685`；P2 场景 B 已见其进入真实编译）；现有测试无法证明。
- 半成品红：拆「锚点/时间位置判定」与「召回 query 调整」两半；D1「？」与 D3「无关轮不含摘要」在两半下都绿、无区分力，须配正向输入。**变异未实际执行**（本轮禁改 `src/tests`），属预测。
- **E-min 最值得补的实验**：同一会话让旧话题进摘要，发「早上好」「？」及带锚点「蓝绿部署那项后来怎样？」，同一次真实编译里分开看：① `lane=summary` 是否含旧事；② `evidence_selected>0` 否；③ 相关轮是否出现旧事。真实输出须附 HEAD 与文件 sha256；纯函数直调标 `[脚本模拟]`，不进覆盖矩阵。P2 已完成 E-min 的前半（真实编译器 + 两通道 + 跳过标记交叉验证），缺的是**真实模型回复与人工标注**。

---

## 6. 推荐最小改动（依依赖顺序）

1. **先补对照实验（非代码）**：用 P1 的 7 类样本（尤其「同样『早上好』：隔天续聊 vs 8 秒换题」配对、多个相似旧话题、群聊交错），隔离环境跑真实 `compile()` + 真实模型回复，记录 `messages/meta/reply_ids` 与人工标注。拿不到 API 就停在"真实编译器输出"并写明"回复未取得"。
2. **B1 摘要按需**：`context.py:1673-1685`，只在有显式找回/引用锚点时注入 `summary`，或无锚点短回复轮降权/后置。
3. **B2 补寒暄门控**：「早上好」加入 `_STANDALONE_REPLIES`（`recall_query.py:6-11`），且「早上好，接着说昨天那个方案」仍走找回（P4 D2 的区分力要求）。
4. **B3 引用目标入提示词**：入站把 `reply_to` 对应**被引文本与说话人**拼进当前输入（`onebot/message.py:142-145`、`bridge.py:532-538`），`reply_ids` 保持 id 级映射。
5. **B4 最近完整语段取数**：`context.py:1486-1529`，替代机械按条截断；无锚点短回复只接眼前上一句；时间判定留系统侧。
6. **不做**：新窗口/会话分段（C）、改窗口身份键、按固定时长自动切题。若 E-min 后出现"同一目标长期多话题簇、混合取数误续率显著高于分窗"的证据，再重评 C。

**新窗口是否必要：不必要。** 窗口按聊天目标复用、与话题无关（S3）；同一窗口里既有隔天续聊也有几秒换题才是常态，新建窗口会丢掉判断所需的时间位置与上一句锚点。`HandoffService` 胶囊只保留短时私聊话题、TTL 后失效、群聊不注入；归档窗口还可能被 `_find_or_create_window` 复用（未核实）。AstrBot `/reset` 与 MaiBot `/clear` 都证明"清短期历史"可行，但 Ling 清 history 不会清摘要/短记忆，不能照搬。

---

## 7. 未取得 / 未核实

- 真实用户会话 PromptSnapshot 与真实模型回复：未取得（禁连生产/NAS）。P2 取得的是真实编译器输出 + 脚本合成历史，模型回复未接 provider。
- 「早上好」是否真召回旧事：触发条件实测（`should_skip=False`），命中与排序未验证。
- H 组论文原文逐页核对：元数据经 Crossref/ACL 核实，理论内容标「转述未核实」；另一 FINAL-REPORT 对部分条目做了更细的逐段核读。
- 真实 IM 行为数据：全部合成或公开文献。
- 变异实验（半成品红）：P4 只有设计预测，未实际改代码跑红。
- `git status`：`docs/research/`、`docs/evidence/ling-context-*` 为本轮新增未跟踪文档；`src/`、`tests/` 未改；各产物 U+FFFD 与敏感模式自检零命中。

## 8. 关键来源

- 反例基线：`docs/research/P2-baseline.md`（真实编译器两场景）；`src/ling/kernel/context.py:548`、`:1486-1529`、`:1673-1685`、`:1933-1977`。
- 召回门控：`src/ling/domains/memory/recall_query.py:6-27`；更正见 `docs/research/P3-lightweight.md §0`、`S4-context.md §3`、`LEAD-notes.md §3`。
- 窗口复用：`src/ling/kernel/bridge.py:1479`、`:1556`；S3 报告。引用丢失：`contracts/v1.py:362`、`connectors/onebot/message.py:142-145`、`bridge.py:532-538`；S4 §4。
- 样本/判据/方案：`docs/research/P1-samples.md`、`P4-adversarial.md`、`P3-lightweight.md`、`docs/evidence/ling-context-plan-c.md`、`ling-context-adversarial.md`。
- 人类/IM：`docs/research/H1`–`H4`、`H-SUMMARY`、`M0`–`M4`。
- 开源：`docs/research/S1-astrbot.md`、`S2-maibot.md`（S1 尚未最终落盘时已收到关键发现，以最终文件为准）。
- 真实输入权威：`turn.py:1897-1958` 的 PromptSnapshot，**不是**行为日志摘要或截图。
