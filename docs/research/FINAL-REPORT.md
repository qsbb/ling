# Ling 上下文研究汇总：第一期应改什么

日期：2026-11-22（Asia/Shanghai）。仓库：`/Users/lingxi/project/ling`，分支 `main`，基线 `de43cbe79a78e0c01f7821470500eaa04ea79497`。研究阶段只读源码、使用合成/脱敏材料；未改业务代码、未连生产或 NAS。

## 0. 一页结论

**先回答分区问题：什么常驻、什么随当前话题带出、什么只在明确需要时找回。**

- **常驻**：人设与角色约束、当前可见的环境事实（平台/群私/会话/账号/实例/本地时间）、受众允许的核心记忆、能力边界。它们定义“她是谁、在哪、能做什么”，不是某个旧话题。
- **随当前话题带出**：最近几轮原文、与该话题绑定的窗口短记忆（`topic/open_questions`，TTL 120 分钟）、必要的引用/说话人标注。用于接住“嗯、？、后来呢”这类短承接，但它必须**跟着当前话题**走，而不是按消息数无限拖着旧事。
- **只在明确需要时找回**：滚动摘要里的旧事、长期记忆证据、跨端交接、被压缩掉的旧事。触发条件应是**用户显式线索**——引用目标、`@`、时间+话题锚点（“接着说前天那个方案”）、元话语（“刚看到，回到……”）、有来源的承诺，而不是“文本相关”或“同窗口可见”。

**因此第一期不改会话管理，不新建窗口。** 推荐方案：**B（当前轮取数改进）的最小版本**——①补低信息门槛；②把“接上一句/显式找回/换题/引用目标”做成纯函数分路；③让分路决定本轮的召回 query、原文取数与摘要标注；④把 `reply_to`/被引用目标作为取数信号透传。A（现状）作为对照，C（明确分段）列为第二期候选：现在没有真实回放和话题边界标注，无法证明“分段是解法”。

**反例的正确解释**：用户问「你还记得前天聊了什么吗」→「早上好」→「？」后，Ling 续列四项需求。关键实测修正：**「？」不会触发长期记忆召回**。`.venv/bin/python` 直接调用 `should_skip_automatic_recall("？")` 返回 `True`（NFKC 归一成 `?`，`rstrip` 后为空串，落入 `_STANDALONE_REPLIES`）。真正未跳过的是「早上好」：它不在 `_STANDALONE_REPLIES`，会以 `query="早上好"` 调 `_memory.recall()`。与此同时，四项需求还有两条不依赖召回的通道：摘要无条件常驻（`context.py:1673-1685`）和最近原文按 token 预算倒序取（`context.py:1460-1529`）。**「旧事来自哪条通道」必须用同一份提示词快照区分，截图和回复文本不够。** P2 已取得合成历史回放的真实编译器输出，但未取得原始真实用户会话的提示词快照；因此线上反例的主因仍只能作条件式预测，不写成定论。

---

## 1. 派发与取证概况

实际并行派发 16 组：人类对话 4、即时通讯 4、源码取证 4、样本与方案 4。运行时并发受限，多数组被回收后重派；AstrBot、MaiBot 两组最终都通过 raw/codeload/`git clone` 取得源码，全部 16 组均有结果回收。可用子模型路由经 `list_subagent_models` 核实为 `nasfree/hy4flash`；白名单中的 `ds/*`、`cpa/*` 在本会话被拒（控制台错误：`child LLM route ... is not allowed`）。

观测对象钉住：HEAD 与基线一致；`bridge.py` SHA-256 `334534fe…94a70`，`context.py` SHA-256 `ffb573b4…718ba`，研究期间业务文件无修改。各组新增文件均为未跟踪文档（切勿当业务改动提交）。

---

## 2. 论文与行为证据

### 2.1 轮次、修复、话题边界（证据强度：原文已核实/全文逐段核对）

- Sacks, Schegloff & Jefferson (1974), *A Simplest Systematics for the Organization of Turn-Taking for Conversation*, Language 50(4):696–735, https://doi.org/10.2307/412243 。逐字摘要：“The organization of taking turns to talk is fundamental to conversation… locally managed, party-administered, interactionally controlled, and sensitive to recipient design.” 自然录音语料（电话/面对面），提出 TCU、TRP、相邻对与偏好组织。边界：依赖语调/重音/停顿；IM 无真实 TRP。
- Schegloff, Jefferson & Sacks (1977), *The preference for self-correction…*, Language 53(2):361–382, https://doi.org/10.2307/413107 。逐字摘要确认“strong empirical skewing … a preference for self-repair”。修复四分法与“偏好自我修复”是统计倾向，不是硬规则。
- Kendrick (2015), *Other-initiated repair in English*, Open Linguistics 1(1), https://doi.org/10.2478/opli-2014-0009 。227 例：相当比例的 OIR 形态实际做玩笑、质疑、惊讶、不偏好应答前置等，甚至形似但不修复。对「？」最关键：孤立「？」默认可当 OIR，但必须留反证通道。
- Schegloff & Sacks (1973), *Opening up Closings*, Semiotica 8(4):289–327, https://doi.org/10.1515/semi.1973.8.4.289 。结束是“预闭合+终末交换”的可展开序列，不是一句“再见”。对 IM：收束一个话题不等于收束整场会话。
- Button & Casey (1984/1985/1988)。**更正**：任务中“Button & Lee 的 topic transition/boundary”在 Crossref/OpenAlex 查无此文；可核实的是 *Generating topic: the use of topic initial elicitors*（DOI 10.1017/CBO9780511665868.013）、*Topic nomination and topic pursuit*（DOI 10.1007/BF00143022）、*Topic initiation: Business-at-hand*（DOI 10.1080/08351818809389298）。
- Yang (2019), *The Management of Topics in Ordinary Conversation*, White Rose eTheses, https://etheses.whiterose.ac.uk/id/eprint/26984/ 。全文逐段核读：话题自然结束靠“收尾动作 + 对方配合 + 话题不再被推进”；判断信号按强度包括最小确认、总结性评价/格言、穷举、贡献萎缩（dwindling contributions）、pre-closing（“Okay then”）、对方不接球。显式重开旧话题靠 `so/anyway` + 重复旧内容 + attention getter，或 stepwise 过渡五步。这解释了为什么“问候后无标记续列几天前的需求”刺耳：它单方面恢复了一个已被双方 ratify 为 closed 的话题。

**「？」与「后来呢？」的区别**：孤立「？」在口语里默认是“他人启动修复”，目标是让原说话人重说刚才那句；判据是对方复述/改写上一句。带新指称的完整问句或话题收束后的新序列起点则推翻该默认。「后来呢？」不是修复，而是叙事续讲请求（story-continuation request），预设存在未讲完的 telling，期待更多故事而不是重说某词。

### 2.2 注意力、事件边界与指代（全文逐段核对，可靠度最高）

- Grosz & Sidner (1986), *Attention, Intentions, and the Structure of Discourse*, CL 12(3):175–204, https://aclanthology.org/J86-3001/ 。原文：attentional state 是话语本身的属性，不是参与者认知状态；focus spaces 组成栈，由话语目的（DSP）的 dominance 驱动 push/pop。
  - **延续/子段**：新 DSP 被当前 DSP 支配 → push 新空间，旧空间仍在栈里。
  - **真返回**：插入段完成 → pop，未完成的前段空间自然回到栈顶；不新增 DSP 与支配关系。线索 `anyway / in any case / now back to`。
  - **语义返回**：如隔天重提，对应 focus space 已不在栈里 → **不 pop 而是 push 新空间，实体必须显式重引入，不能直接用代词**。
  - 硬判据不是线索词（`now/next` 只表示注意变化），而是指代词可解性 + 栈操作方向 + 是否新增 DSP。
- Grosz, Joshi & Weinstein (1995), *Centering*, CL 21(2):203–225, https://aclanthology.org/J95-2003/ 。每句 Cb/Cf、CONTINUE/RETAIN/SHIFT；明确 **shift ≠ 段落边界**；全局返回需要额外推理，线索与负荷效应作者自承未解决。
- Hearst (1997), *TextTiling*, CL 23(1):33–64, https://aclanthology.org/J97-1003/ 。书面说明文的线性子话题切分；作者自述刻意不用话语标记、代词、时态、韵律，样本仅 12 篇。不能外推为 IM 分段参数。
- Clark & Brennan (1991) grounding 与 Clark & Wilkes-Gibbs (1986) 指代协作（书目/元数据核实，正文未逐字取得）：跨很久恢复靠先前协作固化的 shorthand，不是当前句的词汇共现。Stanford Encyclopedia of Philosophy *Common Knowledge* 逐字区分 mutual knowledge 与 common knowledge：“mutual knowledge by itself implies nothing about what … knowledge anyone attributes to anyone else.”——**同窗可见 ≠ 共同背景**的哲学版本。

**面向 Ling 的可操作规则**：判断“接上一句还是回看更早”，按显式引用/`@` > 显式锚点（时间+话题名词、元话语） > 承接词（“后来呢/接着”，仅在最近 1–2 条有效） > 时间位置 + 短回复 > 纯邻接。时间只作弱信号；同一句“？”在“隔天仍在续聊”和“几秒内已换题”两种上下文里文本相同，差别必须在取数侧体现。

### 2.3 即时通讯（摘要/元数据核实 + 合成演示）

- QQ 机器人官方 C2C 文档（A 级，https://bot.q.qq.com/wiki/develop/api-v2/ ）：单聊用 `author.user_openid` 标识，消息 `id` 用于回复/撤回；引用是结构化字段 `message_type=103` + `msg_elements` + `ref_msg_idx`，不是文本约定；`msg_id` 可能重复推送，回复用 `msg_id + msg_seq`；单聊被动回复 60 分钟/4 次。边界：这是**用户↔机器人**协议，不等于人与人私聊客户端行为。
- Herring (1999), *Interactional Coherence in CMC*, DOI 10.1111/j.1083-6101.1999.tb00106.x ：IM 天然有 disrupted adjacency、overlapping exchanges、topic decay。
- Woerner, Yates & Orlikowski (2007), HICSS, DOI 10.1109/hicss.2007.152 ：一个月团队 IM 研究，四类连贯挑战：缺乏同步反馈、邻接破坏、多任务、权威。
- Chou et al. (2022), CHI, DOI 10.1145/3491102.3517496 ：已读回执=轮次转换感知；发送者猜测与接收者自述原因系统性错配。**不要用“已读未回”判断续聊状态**。
- Chen et al. (2018), JCMC 23(4), DOI 10.1093/jcmc/zmy009 ：实验证明 non-contingency（回复与上文不相干）损害人际评价，contingency 调节 latency 效应。
- Nardi et al. (2000), CSCW, DOI 10.1145/358916.358975 ：IM 的 outeraction——为连接而连接本身是主要用途；「早上好」是**关系通道重启**，不是内容命题。
- Chen & Chen (2023), *The naturalness of Chinese online chatting*, Frontiers in Communication, DOI 10.3389/fcomm.2023.994192 ：真实微信/QQ 打字聊天语料。引用（citing function）把被插队旧句“marked 地插入当前上下文”，**恢复 adjacency**；@ 用于定人；同人连续短气泡 + 时间邻近读成一个 turn；人会“read back and forth”重建配对。边界：微信 5 分钟阈值不可外推到 QQ；腾讯官方 QQ 引用 UI 专页本次未检出。

**结论**：续聊由“话题锚点能否被对方解析”定义，不由时间间隔定义。判断顺序：显式元话语 > 指称/锚点可解析性 > 相干性 > 结束序列 >（最后才看）时间。QQ/微信关于离线批量推送、已读回执的具体条文未取得，不写成事实。

---

## 3. Ling 源码取证（均为 HEAD de43cbe 的实测代码路径）

### 3.1 入站窗口复用（`bridge.py`）

- 入口 `InboundBridge.handle()` `bridge.py:399`；窗口确定 `:493-524`。`_ensure_window()` `bridge.py:1479-1509` → `_find_or_create_window()` `bridge.py:1556-1635`。
- 复用键：`(actor_id, channel, connector_window_ref/title)`，私聊再加 `subject_id`；有受信实例时优先 `origin_status='known' + origin_scope/instance_id` 精确匹配，冲突抛 `WindowOriginConflictError`（`:1578-1606`）；找不到才新建（`:1620-1635`）。群聊不按发言人分窗口（`:493-501,1575-1576`）。
- **没有“用户显式换题 → 新窗口/清空历史”的分支**。全仓 grep `换题|新话题|topic_shift|new_topic|reset_window|clear_history` 仅命中延迟补话与防刷屏注释。窗口复用与话题无关，这是反例的结构性前提。
- 说话人：群聊历史按 `sender_subject_id` 查 display_name 并过滤“群友/用户”占位名（`context.py:1687-1717`）；当前句用本次群名片 + 稳定昵称合并（`:1807-1835`）。
- 引用：`envelope.reply_to` 目前只用于 `_is_addressed()` 决定“回不回”（`bridge.py:558,721,1652-1670`），**没有进入 `compile()` 的取数参数**。

### 3.2 发给模型的上下文（`context.py`）

唯一装配点 `ContextCompiler.compile()` `context.py:548-1861`；生产调用点 `turn.py:1033-1070`，刻意不传 `history_limit=20`。

最终 `messages[]` 顺序：system 车道（人设/约束/场景/环境事实/evidence/core_memory/knowledge/media/self_state/relationship/短记忆/意识/交接/能力说明）→ 旧摘要（`1673-1685`）→ 历史原文（`1732-1789`）→ 当前输入（`1836-1847`，最后一条）。

- 历史取数：按 `sequence` 从新往旧分页（`1463-1529`），不是按时间窗、也不是固定 N 条。预算 `HISTORY_TOKEN_BUDGET=20000`（`:81`）、`HISTORY_FETCH_CAP=2000`（`:93`）、单条上限 10000 token（`:100,1478`），`LING_CONTEXT_BUDGET_SCALE` 默认 1.0（`:247-275`）。溢出部分用确定性 `render_rollup()` 压进摘要（`:1791-1805,1979-2024,2074-2098`），不调模型。
- 摘要：`if window_ctx.summary` 就无条件注入（`:1673-1685`），**不看 TTL、不看话题相关性**；TTL 只管同一行的 `topic/open_questions` 短记忆（`:230-244,1137-1156`）。
- 长期记忆三道门（`:1549-1575`）：受众不允许 → 不召回；低信息整条消息 → 跳过并记 `memory_recall_skipped=low_information`；否则用当前文本做 query。「后来呢」类整条匹配时拼最近一条合格用户消息前 180 字（`:1557-1566`）。`wants_history()` 命中“以前/之前/原来/过去/上次说过/曾经”等则 `include_history=True`（`service.py:1814-1818`）。候选短表 12、最多注入 3 条证据（`:101-102`）。
- 低信息表 `recall_query.py:6-27`：含 `嗯/哦/好/在吗/你好/hi/ok/thanks/...？` 等；**「早上好」不在表内**。直接调用实测：`？`→跳过 `True`，`早上好`→`False`，`后来呢？`→`needs_recent_topic True`。
- 引用标注：只对最近 12 条 user 消息标 `[msg_id:…]`（`HISTORY_MESSAGE_ID_LIMIT=12`，`:384,1965`），长平台 id 缩成 `mXXXXXX`（`:407-418`）；当前句单独标（`:1837-1838`）。模型写出的 `[quote:X]` 必须在 `reply_ids` 白名单内，否则降级为普通消息（`turn.py:2586-2596`）。**引用目标在 12 条窗口外会悬空**，不能静默补成“最近一条”。
- **没有给历史消息渲染“距上条多久/停顿”**。发给模型的 `ChatMessageIn` 只有 `role/content`（`:1852`），`created_at` 只保存在快照用的 `CompiledMessage.at`（`:1787`）。时间精度只在媒体车道（`:168-202`）。

### 3.3 开源对照（部分未取得，如实标注）

- **AstrBot**：官方仓库 https://github.com/AstrBotDevs/AstrBot ，master HEAD `feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea`（2026-11-19），最新 release `v4.30.0-beta.1`。源码经 codeload tarball + raw 文件核对，关键文件行号（相对仓库根）：
  - 平台标识 UMO = `platform:message_type:session_id`（`astrbot/core/platform/message_session.py:6-27`）；内部 UUID `conversation_id` 另存（`astrbot/core/db/po.py:152,158`、`conversation_mgr.py:267-299,349-363`）。**平台会话标识与内部会话 ID 分离**，值得 Ling 保持类似的边界意识。
  - 最近历史按对话持久化，每轮重读（`astr_agent_runtime/main_agent/request.py:311-395`、`main_session.py:75-143`、`history.py:22-139`）。压缩摘要作为两条普通消息注入并回写历史（`agent/context/compressor.py:350-364`），`context.updated/rebased` 事件落库（`db/conversation.py:28-35,72-109`）。配置链 schema→runner→每步 `process()` 已接线（`agent/runners/tool_loop_agent_runner.py:1204-1224`），阈值 0.82、默认 `llm_compress`、最近保留比 0.15。
  - `/reset` 清空历史但保留会话 ID，`/new` 建新对话并切换（`builtin_stars/builtin_commands/commands/conversation.py:112-197`），二者都真接线且都清理群聊临时上下文。**这印证“清空”与“新建/切换”是两种语义**；Ling 目前显式换题无专门入口，但第一期也不必照搬；若要补，先区分语义而非只做关键词。
  - 群聊历史是另一套按 UMO 分区的旁路（默认关闭），不是当前对话历史（`config/default.py:154-160`、`builtin_stars/astrbot/group_chat_context.py:142-197`）。未发现 `/memory` 命令；dashboard 的 LongTermMemory 页面是独立功能，不是每轮自动注入的会话状态。
  - 边界：AstrBot 的“压缩摘要”是派生历史片段，不是独立的长期记忆对象；其配置默认值/阈值不可直接当 Ling 参数。

- **MaiBot**：官方仓库 https://github.com/MaiM-with-u/MaiBot （README badge 另用 org 名 `Mai-with-u/MaiBot`，同一项目）。经 `git clone --depth 1` 取得源码，HEAD `edf42446f123149c57fc79765dd5a8b004b8524c`（2026-11-20，"feat: 发布 1.4.8 与 WebUI 1.10.8"），`pyproject.toml:7` version=1.4.8（README 顶部残留旧文案 1.3.10，以 pyproject 为准）。关键路径：
  - 平台聊天标识→内部 session_id：`utils_session.py:8-42` 把 platform + account/scope + group/user 拼串取 MD5；平台原始 message_id 与内部 id 分离（`mai_message_data_model.py:33-65`、`platform_io/types.py:170-176`）。
  - 最近历史按 session_id 从库恢复（`maisaka/runtime.py:330-388`），`build_reply_history` 另取较小回复窗口（`:304-328`），并以最后一条 `/clear` 边界过滤（`context/clear_context.py:34-42`）。
  - 摘要两层：Planner 滚动摘要写回历史（`context/compaction.py:48-277`、`reasoning_engine.py:1442-1470`）与长期记忆的聊天摘要写回（`memory_flow_service.py:552-625`，按消息阈值默认 36 触发）。滚动摘要被明确构造成**独立内部状态**——`display_prefix="【上下文状态摘要-内部参考】"`、`source="context_compaction"`，**不伪装成用户消息、不占近期条数**（`compaction.py:73-82`）；注入的长期记忆也带「内部参考、勿逐字引用」标记（`memory/heuristic_injector.py:23,367-387`）。这与本报告 §5 建议给 Ling 摘要加「较早对话，不代表当前话题」标注是同一类做法。
  - 轮后裁切硬保护最近 5 条真实消息、最近用户回合与未完成工具调用（`context/post_processor.py:113-177`）；压缩失败不阻断对话，保留上一版有效摘要（`reasoning_engine.py:1453-1474`）。长期记忆 A_memorix 总开关默认 `False`；启发式召回与跨聊天召回默认 `False`，画像/查询工具默认开（`official_configs.py:1304-1402,1832-1844`）。
  - `/clear` 真接主链路（`bot.py:1010-1013` → `heartflow_manager.py:177-189`），但**只清短期上下文、写边界标记，不清 A_memorix 长期记忆/摘要**——所以显式清上下文后旧事仍可能被启发式或工具拉回。未发现自然语言“换题”的专门检测。
  - 对 Ling 的旁证：内部会话 id 与平台 id 分离、摘要作为派生对象回写、以及“清短期上下文≠清长期记忆”的区分，都支持报告 §0 的分层原则；但 `/clear` 的语义（清历史）不等于“换话题”。

这两项对照对第一期决策不是必需：Ling 自身的行为路径已有源码证据。AstrBot 提供“标识分层/压缩回写/reset 与 new 分离”的工程旁证；MaiBot 提供“MD5 会话 id/两层摘要/清上下文不碰长期记忆”的旁证。两者共同说明：**显式清空与新建/切换是不同语义，摘要是派生对象，长期记忆不应随短期上下文一起被“话题切换”隐式清除**——这与本报告的分区结论一致。

---

## 4. 可复现最小反例与样本

样本已落盘，全部合成、假 QQ 号 `100200300` 形态、地址 `192.0.2.x`，不含真实数据/凭据：

- [最小回放样本（JSON）](.data/ling-context-replay-samples.json) 与 [可读版](.data/ling-context-replay-samples.md)：7 条（S01 很久后续聊、S02 刚换题、S03 异步引用、S04 简单问候、S05 短问号、S06 多个相似旧话题、S07 群聊交错）+ 4 判据。
- [可复用中文回放样本](docs/evidence/ling-context-research/replay/01-samples.md)：13 条开放样本 + 4 条盲评样本，统一 ID，判据/基线/方案/对抗共用。
- [对抗验收判据](docs/evidence/ling-context-research/review/ADVERSARIAL-CRITERIA.md) 与 [P4 判据稿](docs/research/P4-adversarial.md)：先于实现写定，条件式写法，正负配对、半成品红、`[脚本]`/`[模型]` 分开。

核心反例的条件式回放判据：

1. 同一段历史，输入 `？` 与输入 `接着说前天那个方案`，`compiled.meta` 的召回分支、`sections`/`messages` 应不同；若相同 → 没有锚点区分力。
2. `？` 轮应出现 `memory_recall_skipped=low_information`；若不出现但 memory 段有旧事 → 宽泛召回；不出现且无旧事 → 召回未命中，**不能判已修复**。
3. `早上好` 轮当前会走召回；补入低信息表后应跳过，但 `早上好，接着说前天那个方案` 必须仍能找回——否则“所有问候都跳过”是另一种错误。
4. 摘要块若在无锚点轮仍出现 → 常驻；带锚点轮才出现 → 有门控。两种结果都要记录，不预设哪个一定对。

**真实模型输出当前未取得**：无模型 API 环境变量，无 prompt_snapshots 回放结果。所有“回复会怎样”只能标脚本模拟/人工期望，不冒充模型观测。

---

## 5. A/B/C 取舍与第一期最小改动

| 方案 | 内容 | 收益方向（推理） | 代价/风险 | 本期裁决 |
|---|---|---|---|---|
| A | 现状：窗口按聊天目标复用；原文按 token 倒序；摘要常驻；低信息表跳过召回；`reply_to` 只决定回不回 | 零改动，行为可预期 | 同窗但换题时旧事仍在原文/摘要里；问候触发宽泛召回；引用目标可能悬空 | 仅作对照基线，不单独采用 |
| B | 当前轮取数改进：最近几轮/短承接/显式锚点/引用目标分路；低信息表补问候；摘要标注“较早，不代表当前”；`reply_to` 透传取数 | 直接命中反例三类输入分岔；不改数据模型；可用现有 `prompt_snapshots` 验收 | 关键词表覆盖有限；锚点误判会续错题；阈值需回放校准 | **推荐，且取最小版本** |
| C | 更明确的上下文分段（话题段/事件边界） | 若 B 暴露“信号有但落点错”，分段可能进一步降低误续 | 无真实回放、无话题边界标注、无 A/B/C 对照；分段不能延长引用有效期；新窗口会丢时间位置信号 | **不做第一期**，第二期候选 |

**推荐的最小改动（只定位，不改代码）**：

1. `src/ling/domains/memory/recall_query.py`：扩展 `_STANDALONE_REPLIES` 补“早上好/早/早呀/早上好呀”；新增纯函数 `classify_turn(user_text, recent_messages, reply_to)`，三态 `recent_only / anchored_recall / explicit_new_topic`，无 DB、可单测。判据：纯问候跳过、带锚点的问候不跳过。
2. `src/ling/kernel/context.py:1549-1575`：用分类结果决定 query 来源与 `include_history`；`meta` 记 `recall_mode`。`？` 走 recent_only，不额外召回；`后来呢` 拼上一句；`接着说前天那个方案` 用“时间+话题名词”做锚点召回。
3. `src/ling/kernel/context.py:1673-1685`：摘要块加“这是较早对话，不代表当前话题”的表达层标注；不做相关性硬门控（先观察 B 的效果）。
4. `src/ling/kernel/bridge.py:863-877` / `turn.py:1033-1070`：把 `envelope.reply_to` 与被引用目标 id 透传到 `compile()`；引用目标在 12 条窗口外时标注“锚点不可见”，不静默取最近一条。
5. 不改：`bridge.py:1479/1565` 的窗口复用、摘要生成链路、MemoryService 排序。**新窗口不必要**：它把“隔天续聊”和“几秒内换题”拆到不同窗口，反而丢掉判断所需的时间位置信号。

**旧话题如何恢复**：用户显式重提时，用时间+话题锚点定位 evidence/仍可取的原文；已被压缩进摘要的消息不能按 id 重放（`messages_for_prompt` 受 `summary_through_sequence` 限制），产品预期是“摘要+记忆命中”，不是逐字原文回放。窗口仍只作可见性过滤（群 A 内容不进群 B），不作话题边界。

**群聊怎么跟对**：`sender_subject_id` 逐条绑定，不继承上一条；`@` 是目标字段不是说话人；引用是消息→消息，不推出说话人；引用 id 白名单按 `window_id` 限定。优先级：引用目标 > @ > 文本近因。

---

## 6. 量化指标与最值得补的一组实验

指标（全部写成条件式，不用某次运行的数字当期望值）：误提旧事、换题自然度、漏续聊、指错人/指错消息、输入长度、额外调用。定义与条件式判据见 `docs/research/P3-lightweight.md` §6、`docs/research/P4-adversarial.md` §4、`docs/evidence/ling-context-plan-c.md` §5。其中“自然度”只能人工盲评，不能用单一分数；脚本只能证明取数/调用/快照，不能证明模型理解。

**最值得补的一组实验（2×2 对照回放）**：同一模型、同一份合成历史，只改当前输入与锚点：

| 维度 | 隔天仍在续聊（S-keep） | 几秒内已换题（S-switch） |
|---|---|---|
| 孤立「？」 | 承接昨夜未完事项（有 `接着说前天那个方案` 锚点在前） | 澄清刚发的“早上好” |
| 显式找回 | `接着说前天那个方案` → 应取回四项需求 | 同句若出现在刚换题后 → 应判歧义/追问，不强行续 |
| 问候 | `早上好` 单独出现 → 跳过召回 | `早上好，接着说……` → 必须召回 |

每条都跑 A（现状）与 B（最小改动），保存**实际 prompt_snapshot 的 messages/meta**与真实模型回复（有 API 时）或明确标“脚本模拟”；再用 P4 的变异法：只做门控(A 半边)/只做取数(B 半边)各跑一次，对应断言必须红。没有这组数据，A/B/C 的比较只是偏好陈述。

---

## 7. 交付物与未取得数据

- 分组研究稿：[docs/research/](docs/research/)（H1/H2/H3/H4、M0–M4、P1/P3/P4、S3、LEAD-notes）。
- 证据稿：[docs/evidence/ling-context-im-private.md](docs/evidence/ling-context-im-private.md)、`ling-context-im-group.md`、`ling-context-im-async.md`、`ling-context-im-offline.md`、`ling-context-baseline.md`、`ling-context-plan-b.md`、`ling-context-plan-c.md`、`ling-context-adversarial.md`、`ling-context-src-ling-outbound.md`。
- 回放与判据：[01-samples.md](docs/evidence/ling-context-research/replay/01-samples.md)、[ADVERSARIAL-CRITERIA.md](docs/evidence/ling-context-research/review/ADVERSARIAL-CRITERIA.md)、[结构化样本](.data/ling-context-replay-samples.json)。

未取得数据：①反例的真实 prompt_snapshot（旧事 lane 来源未定）；②真实模型输出与盲评自然度；③话题边界/误续漏续人工标签；④QQ/微信官方关于批量离线推送与已读回执的条文。AstrBot 与 MaiBot 的仓库版本、提交与关键源码路径已取得（见 §3.3）。以上未取事项均不写成结论。
