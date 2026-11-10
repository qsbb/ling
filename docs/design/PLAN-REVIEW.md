# ling 方案优化审核：前提修正、实施顺序与改动说明

日期：2026-11-11。状态：**评审结论，不是实施记录**。本次只新增本文，生产代码、既有设计稿、测试和版本均未改动。下文的「用户要求」指本次明确给出的两条前提及 `docs/AUTONOMY-DESIGN.md:356-374`、`/Users/lingxi/project/凝心溯溪-开发状态记录.md:99-118` 中的原话；其余标「评审建议」。路径/行号是本次工作树定位，实施前重查。

## 1. 先修正前提

### 1.1 人格立场由人设决定

**用户要求**：「不是必须承认是 AI，而是根据人设决定，不额外注入；有的人想要拟人，有的人想要 AI。」因此不得把“被问时必须承认”或“必须否认”当成全局默认。当前真正写死的是 `src/ling/domains/persona/service.py:25,31`；`system_prompt` 本来可由用户更新（同文件 `:96-113`，API 在 `src/ling/api/routes/actor.py:63-67,123-129`）。默认人设只在行不存在时创建，已有行不被更新（`persona/service.py:65-87`）：**只改常量不会改变既有数据库中的人设**。`PersonaView` 已承载完整 `system_prompt`，无须加数据库字段（`:46-57,132-143`）。

**评审建议：让既有 `system_prompt`/`traits` 拥有完整话语权**，从默认 `DEFAULT_SYSTEM_PROMPT` 删除 `:25` 中“不假装自己是人类”及 `:31` 整句“被问到 AI 时……”；保留与身份无关的“不编造事实/回执”原则（`:26,29-30`）。**不加 `is_ai`、`disclosure_mode` 等选项，不在每轮追加身份声明，也不在场景模板里另设默认身份句**。用户想让角色自称 AI 或拟人，直接写在人设里。`src/ling/kernel/context.py:534-558` 已把完整 `system_prompt` 和 traits 放入人设车道，`:565-583` 又放入默认 `boundaries`；需审查 `persona/service.py:39-43` 的“模拟生活”默认边界是否仍带隐含身份立场。建议将其改成不涉及身份的“不得把无回执动作当已完成”，而将来源真实性继续由代码保证（`src/ling/domains/activity/service.py:140,182`）。

**不能只删两行**：`src/ling/domains/presence/service.py:190-208` 会给模拟场景追加“你的模拟生活，不是真实身体经历”；`src/ling/kernel/self_view.py:205-220` 会给自我情节追加同类句，且后者进入本轮 self-state 车道（`src/ling/kernel/context.py:988-1017`）。这些是**来源保真**要求，并不等同于“必须自认 AI”，但固定措辞也会夺走人设的话语权。评审建议把来源类别留在结构化状态和审计中，模型可见部分用不带“真人/程序”判词的精确措辞，如“这是尚无外部回执的情节，不要说成已实际完成”；具体台词由人设决定。不能为拟人效果把 `simulated` 提升成真实回执。需要核对 `tests/integration/test_presence.py::test_simulated_scene_is_labelled`、`tests/integration/test_self_timeline.py::test_injected_in_private_chat`（`:194-209` 断言含“模拟”）、`tests/integration/test_self_view.py::test_normal_mode_includes_state` 及 `::test_group_does_not_repeat_private_scene`；只改措辞断言，不放松来源/受众断言。

**测试事实纠错**：`tests/integration/test_persona_injection.py:191-206` 中 `:194-195` 是 `test_long_trait_is_not_silently_cut` 的**用户自定义 trait 测试数据**，`assert long_trait in first` 只检验长 trait 原样进入模型，**没有断言默认角色必须承认 AI**。这段 trait 即使表达“承认 AI”也应原样保留，恰好证明人设自主权。真正需要更新/扩展的是 `::test_default_persona_reaches_the_model`（`:146-155`）和 `::test_group_turn_also_carries_persona`（`:304-312`）的默认文案检查，再新增「中性默认不含身份立场」「AI 人设原样进入 provider」「拟人人设原样进入 provider」「空人设不被补写」测试；现成空人设用例见 `:258-299`。`::test_persona_api_matches_database`（`:318-343`）应继续证明 API、数据库、模型输入一致。用户写入的旧人设应保留；不能靠启动时静默改写迁移。

### 1.2 凝心溯溪系列是需求输入

**用户要求**：系列插件是用户自己的设计，ling 应承接其中的需求，在独立运行架构中重新设计边界，而不是给“外部作品”打分。`docs/design/SERIES-REPORT.md` 因此以下结论须逐条改写：

| 原文位置 | 失效/改写结论 | 正确口径 |
|---|---|---|
| `:1-4,29-38` | “对 ling 的借鉴”“系列在框架内修 bot、ling 才是主体”的对立叙事没有需求证据。 | 改为“用户在系列中已经表达的能力需求，以及 ling 的承接差距”；是否“刻意留白人格/生活”不能由插件命名推出，标「没查到」。 |
| `:19-25` | 把 README/代码里的作者意图当外部作者观点，不足以表达用户当前选择。 | 保留“明确/实现/推断”证据等级，新增“用户当前要求”最高层；历史实现不自动成为 ling 的硬规格。 |
| `:59-79,95-99,113-130,137-147,151-185,188-205` | “最有价值”“最干净”“最容易误抄”“对 ling 最有用”等评价口吻。 | 保留已核实机制和边界，改列“用户需求—ling 现状—需重新设计的接口/权限/取数方式”，不要把 AstrBot 插件钩子原样搬进单体系统。 |
| `:210-227` | “两者不冲突”说得过满；身份表达与事实正确性的边界仍待拆。 | 用户原则是“行为风格归人设，正确性归代码”（`docs/AUTONOMY-DESIGN.md:356-374`）；角色如何描述自身归人设，回执、隐私、来源事实仍由代码约束。 |
| `:231-249` | “最值得优先跟进、改动面小”无排期与回放证据。 | 三层时间观是用户需求（`状态记录:99-118`）；具体模糊桶进入计划，但需先定义源时间、日界与日期推算，再与记忆注入一起验收。 |
| `:253-280` | “系列不承认 AI vs ling 诚实承认 AI”二选一、`:274-280`“ling 现行做法自洽、不需要改/改了就冲突”**全部失效**。 | 用户已选“由人设决定”。身份叙事不与事实来源正确性画等号；“承认 AI”不是诚实性硬门。删除 `:269-280` 的代价表和“不需要改”判断。 |
| `:284-313` | “值得抄/不该抄”整组分类失效；`:306`“三个成熟插件”、`:312`“临的 52.9k 行不值得迁移”也不应当作用户需求的裁决。 | 改成“已确认要承接的需求 / 独立架构下需变形 / 尚待用户确认”；召回条数、固定门控、主动发送等是待回放验证的实现选择，不能拿系列默认值替用户决策。临仍按 `:8-17` 的既定排除范围，不复活其协议。 |
| `:314-323` | “工程教训”仍有价值，但“跨插件 Python dict”不是 ling 单体的直接实现要求。 | 转写为领域契约与可观测失败的需求；是否要新跨域序列化接口需看 ling 的实际边界，不能类比即立项。 |
| `:326-338` | “言缺失发言判断/境完全缺失”的绝对结论与当前源码不符。 | `src/ling/kernel/autonomy.py:168,220-298` 已有 reply/silence 判定；`src/ling/domains/presence/service.py:190-208` 已有场景感知。应按能力缺口逐项核，而非写“完全缺失”。具体境的天气/日历 provider 覆盖范围本次没查到。 |
| `:342-358` | 44 探针纠错可保留；“以本文结论为准”不能覆盖新的用户前提。 | 归档五份仅溯源，合并报告按上述框架重写后再作为有效总览。 |

另有一处原文内部错误：`SERIES-REPORT.md:17` 写“六个插件”，却列出知、情、言、序、境、声、核**七个**；重写时应直接改正，不能沿用错误计数。

**失效的原有建议**：`SERIES-REPORT.md:278` 的“不需要改 AI 立场”、`:284-313` 的“抄/不抄”裁决及 `MEMORY-REDESIGN.md:395-401` 的“先 P0、再把 6 改 4、后修候选池”不能实施。`MEMORY-ROLLOUT-PLAN.md:5-9,35-54` 已纠正后一个顺序：先修有确定性 500/501 漏召回，再校准零召回和注入量。`MEMORY-REDESIGN.md:187-206` 固定 200/150/150/100 配额没有全域覆盖证明；`MEMORY-CANDIDATE-DESIGN.md:13-27,49-53` 的可证覆盖契约取代它。

## 2. 优化后的完整实施方案（评审建议）

顺序原则：人格前提纠错可独立先做；记忆先建证据基线，再候选覆盖与 BM25 **同批切流**，之后才决定零召回和条数；重试先于晚回兜底；时间模糊化随注入策略回放，不改变记忆真值。每步为未来实施批次，**本次均未执行**。

| 顺序 | 未来文件与具体动作 | 依赖、会撞的既有用例 | 验收、风险和回滚 |
|---|---|---|---|
| **P 人格立场** | `src/ling/domains/persona/service.py:20-31,39-43` 移除默认身份判词，保留事实/回执边界；`src/ling/kernel/context.py:534-583` 验证只传用户人设及既有边界，不增加 AI 选项/附加句；`src/ling/domains/presence/service.py:190-208`、`src/ling/kernel/self_view.py:205-220` 将“模拟生活”呈现与内部来源标签分开。现有 API `src/ling/api/routes/actor.py:63-67,123-129` 不扩字段。 | 独立批次；依赖用户已明确的两条前提。检查 `test_default_persona_reaches_the_model`、`test_group_turn_also_carries_persona`、`test_long_trait_is_not_silently_cut`、`test_empty_persona_sends_no_empty_system_message`（`tests/integration/test_persona_injection.py:146-206,258-312`）；后者的用户自定义文案**不应改掉**。还看 `test_simulated_scene_is_labelled`、`test_injected_in_private_chat`（`tests/integration/test_presence.py:183-191`、`tests/integration/test_self_timeline.py:194-209`）。 | 新增三类默认/AI/拟人 provider 输入测试，私聊和群聊各跑；验证原 `simulated` 仍不能自封现实事实（`activity/service.py:140,182`）。风险是旧库仍保存旧默认文案；只发布新默认，既有行用 UI 明示供用户编辑，不能静默覆盖。回滚默认模板/呈现措辞即可，用户人设数据不回滚。 |
| **E 评测基座** | `tests/eval/probe_baseline.py:475-495,631-659` 增候选 ID、扫描数与认证标记报告；`tests/eval/turn_replay.yaml:1`（新文件）建去标识整轮回放，记录是否应检索/应注入、受众、原始证据、实际消息与延迟。`docs/design/SERIES-REPORT.md:1-358` 按 §1.2 重写。 | P 可先/并行，C/M/R/F 共用。现有 44 条在 `tests/eval/probes.yaml:257-523`，旧文件 `:255-256` 仍写“42”，以 `tests/eval/baseline.json:14-25` 的 44 为准；不要重写基线洗掉退化。 | 先只读跑 `probe_baseline.py --compare`，要求现有 44 条的隐私/不可召回红线；整轮回放另外覆盖人格两端。风险是日志含正文，报告只存 ID/聚合，关诊断开关即回滚。 |
| **C1 影子候选与 oracle** | `src/ling/domains/memory/service.py:84-127,206-218,582-603` 实现全域小库 oracle、候选/分路名次影子；`src/ling/kernel/context.py:616-643` 将冻结的 `AudienceSnapshot` 编成查询作用域，仍以 `source_filter` 读时复验；不改变真实结果。 | E 后做，给 C2 证明标准。`tests/integration/test_memory_recall_scope.py::test_old_but_relevant_memory_survives_the_candidate_pool`（`:122-172`）先保留 xfail；`::test_merged_subject_episodes_are_recalled_too`（`:178`）保持。 | 44 条结果不变；新增 500/501、同分、合并/拆分、群 A/B/私聊、过期、缺向量的全域 oracle。影子高延迟/泄漏可关影子，线上仍走旧检索。 |
| **C2 候选覆盖正式切流** | `src/ling/domains/memory/service.py:65-127,206-218,582-835,1235-1283` 用倒排/实体/时间/重要性/可用向量全域取 ID，在同一个有权有效域 `D(q)` 计算 BM25 `N/df/avgdl` **及各路全域名次**，动态扩页/同分取全/无法认证则精确回退或标近似；情节也有界入口。`src/ling/infra/models.py:1161-1310` 与 `migrations/versions/<next>_memory_terms.py:1`（新文件）建事务内索引。保持 `context.py:636` 的 `k=6` 和 `service.py:860-869` 旧返回策略以隔离变量。 | C1 的 oracle 通过才切；**候选池与 BM25 必须同一发布批次**，因为 `_bm25_scores` 随传入语料重算统计（`:84-127`），`_rank_map` 只排传入行（`:206-218`）；只换候选 SQL 不能证明前六覆盖。现有 `test_bm25_prefers_rare_term_over_common`（`tests/integration/test_memory_retrieval.py:32`）、`test_rrf_prefers_multi_signal_match` / `test_mmr_suppresses_near_duplicates`（`tests/integration/test_memory_fusion.py:78,109`）、`test_recall_filters_visibility_before_ranking`（`tests/integration/test_memory_retrieval.py:94`）须保持；500/501 用例去 xfail。 | 44 条 `--compare`，旧第 501 条回到前六，小库 `certified=True` 与 oracle 同序；1000/百万情节另测扫描数与 p95/p99。ANN 或缺向量只能标近似。风险是索引失配、受众泄漏或慢查询；切回旧读入口，停读新索引，原行保留。`updated_at` 绝不可作为唯一入口（`infra/models.py:48-52`、`memory/service.py:1439-1476`）。 |
| **M1 按需与条数** | `src/ling/kernel/context.py:624-685` 加无需额外 LLM 的召回需求判定及注入预算；`src/ling/domains/memory/service.py:860-869,1613-1680` 用原始词法/实体/向量证据的**绝对相关门**决定可否 `hits=[]`，移除 `kept or scored[:1]` 保底；`src/ling/ports/config.py:106-122` 只在回放校准后保存策略版本。 | C2 后做，否则“没有想起”可能只是第 501 条被截断。`tests/integration/test_memory_fusion.py::test_floor_never_returns_empty_when_hit_exists`（`:253-258`）**必须改**为弱证据可空；`tests/integration/test_memory_policy.py::test_default_policy_keeps_recall_behavior`（`:234`）按新策略评估。 | **条数先不拍定**：把 0/1/2/4/6 与字符预算在真实整轮回放上盲评，以关键事实漏召、无关旧事误注入、隐私 0 泄漏、延迟与 token 成本选默认；Cowan 4 组块到 0–2 条只是工程假说（`MEMORY-REDESIGN.md:42-52`），不是论文给出的 LLM 条数。44 探针直接调 `recall(k=6)`，只能检验检索不能验证“每轮不检索”；须新增 provider 输入及回放测试。误拒绝时分别关闭 gate/绝对门/预算，保留 C2。 |
| **M1-T 记忆时间呈现** | `src/ling/domains/memory/service.py:1661-1705` 的 `render_evidence` 增可选的模糊时间标签，`src/ling/kernel/context.py:670-685` 传冻结的本轮时钟/时区；必要时 `MemoryHit` 数据结构补记录时间，但不改原行的 `created_at/occurred_at`（当前 `service.py:795-845`、`infra/models.py:48-52`）。 | 与 M1 的注入回放同验；“何时记下”用 `created_at`，事件“何时发生”另用 `occurred_at`，不能混用，更不能用会被强化更新的 `updated_at`。按用户已定桶：`<1h 刚刚`、当地同日 `今天`、前一日 `昨天`、`N天前（MM-DD）`、`N周前（MM-DD）`、久远 `很久以前（YYYY-MM）`（`状态记录:99-118`）；日级锚点只给日期计算，不作为角色应逐字念出的台词。 | 新增固定时钟/时区及跨午夜、周/月/年、夏令时、历史 `valid_until` 测试；回放确认事实日期仍可算对、回答不机械背诵“何时记下”。44 条只看 ID，无法验收文案。时间标签失真就关渲染开关，原始时间和来源仍保留。 |
| **R1 LLM 流前重试** | `src/ling/models/router.py:252-374` 消费 `ModelError.retriable`，同候选最多 **3 次含首次**，用总 deadline 截断指数退避，耗尽再 fallback；已有 `health.is_open()`（`:261-269`）熔断按候选耗尽计一次，不把内部重试算三次业务失败。`src/ling/models/openai_compatible.py:99-145` 保留 429 已有 `retriable=True`，只补安全解析 `Retry-After`/流完成标志；`src/ling/worker.py:48-52,5184-5204` 区分调度重放与同一 LLM 作业耗尽，防 5×3 叠乘。 | C2 后按 `MEMORY-ROLLOUT-PLAN.md:5-7,50,63` 排列；F1 必须等终态明确。`tests/contract/test_openai_compatible.py::test_http_errors_are_classified`（`:105`）、`tests/integration/test_model_error_classification.py::test_provider_401_raises_auth_kind`（`:60`）、`tests/integration/test_stop_retry_race.py::test_retry_preserves_delivered_text`（`:216`）、`tests/integration/test_worker_idempotency.py::test_failure_applies_backoff_not_immediate_retry`（`:320`）保持语义。 | **最终参数建议**：每候选 3 次含首次，退避基准 1 秒、上限 8 秒、0.5–1 倍抖动；429 尊重 `Retry-After`，超过剩余 deadline 就放弃该次；400/401/403/404、取消不重试；首个可见或不可重放输出后不重试/不换候选；交互式调用总预算暂定 **90 秒**，晚回调用总预算为 `min(60 秒, hard_deadline_at-当前时间)`（当前硬截止 60 秒见 `src/ling/domains/deferred/state.py:88`，provider 单次默认 60 秒见 `src/ling/ports/config.py:282`）；每次尝试的超时还须截于剩余预算。90 秒是工程初值，需用故障回放确认 p95 尾延迟，不能凭“等待最多 3 秒”忽略每次请求耗时（`LLM-RETRY-RESEARCH.md:358-364`）。主+一个备用在无输出且 deadline 允许时理论最多 6 个供应商请求。新增流前/首字后、429/5xx/超时、取消、半截工具参数、用量/回执测试；风险是成本和尾延迟，关内部重试回旧路由。 |
| **F1 晚回失败兜底** | `src/ling/worker.py:1255-1263,1578-1695` 保留确定性事实短句，按可见承诺/零正文失败/受众/抢占/回执决定是否发送；外部渠道先落库后发送的幽灵消息风险见 `:1653-1677`，改用稳定业务键、回执对账；`src/ling/domains/deferred/service.py:1` 如需状态字段再迁移。个性化模型改写仅以后置离线变体考虑。 | R1 后才能正确区分零正文失败、部分完成与可重试故障。`tests/integration/test_deferred_reply_flow.py::test_generation_failure_uses_deterministic_fallback`（`:465-502`）若改文案须按事实骨架改断言；`::test_new_input_preempts_before_generation`、`::test_group_window_is_refused_instead_of_delivering`（`:227,403`）继续保持。`tests/integration/test_deferred_reply_wiring.py::test_fallback_text_is_a_module_constant`、`::test_fallback_text_is_a_literal_not_computed`（`:154,169`）要求保留模块级字面常量。 | 当前群聊是拒绝路径（`test_deferred_reply_flow.py:403-464`），**不要把 `DEFERRED-FALLBACK-DESIGN.md:112-127` 的群聊模板当已支持**；同文 `:101-118` 的“等下/回头补”与其 `:76-80` 的不承诺相矛盾，弃用。44 探针不覆盖晚回；用投递故障、重复 sweep、撤权、抢占、部分流回放验收。误发不可撤回，先关新投递路径并按业务键对账。 |
| **M2 后续整理/遗忘** | 按 `docs/design/MEMORY-ROLLOUT-PLAN.md:55-57`，在 `src/ling/domains/memory/service.py:736-835` 做情节去重/冲突闸，再扩 `src/ling/domains/memory/consolidate.py:71-178` 的睡眠索引/可选 `fact_eligible=false` gist，最后基于回放做软降权；保留 `src/ling/worker.py:4933-4968` 现有调度。 | 依赖 C2、M1 的来源/相关性语义稳定，不能把模型摘要升格为用户事实。`test_episode_is_recallable_later`（`tests/integration/test_memory_pipeline.py:131`）、`test_model_inference_cannot_become_fact`（`tests/integration/test_memory_retrieval.py:153`）和睡眠幂等用例须保持。 | 44 探针及来源撤回/受众隔离/同晚重放/冲突反例；gist 人工抽检和真实回放另做。风险是错误合并、伪事实或误删；先影子索引，关 gist 读写及软分，原情节/断言不动。 |

## 3. “已经改动了哪里，为什么”：原方案修订对照

此表说的是**评审对原方案的修订**，不是已改代码；本次实际文件改动只有 `docs/design/PLAN-REVIEW.md`。

| 原方案位置 | 改成什么 | 为什么改 |
|---|---|---|
| `SERIES-REPORT.md:1-4,284-313` “借鉴/值得抄/不该抄” | 需求输入、ling 承接状态、独立架构重设计、待确认事项 | 系列是用户自己的需求，不是外部样板。 |
| `SERIES-REPORT.md:29-38,253-280` “系列沉浸 vs ling 诚实；不需要改” | 身份表述由人设决定；保留代码侧隐私/来源/回执正确性 | 用户明确否定二选一，原“不需要改”结论失效；`persona/service.py:25,31` 是默认硬编码。 |
| `persona/service.py:20-31,39-43` 在原方案中未列为实施项 | 增 P 批次：删除默认身份判词并审默认边界，不增身份配置项 | `:65-87,96-113` 已有可覆盖的人设；额外开关/每轮声明违背“不额外注入”。 |
| `SERIES-REPORT.md:326-338` “言缺失/境完全缺失” | 按具体功能核差距，先承认已存在 reply/silence 与场景状态 | `kernel/autonomy.py:168,220-298`、`presence/service.py:190-208` 与原绝对表述冲突。 |
| `MEMORY-REDESIGN.md:14,42-52,126-138,395-401` 先 0–2/4 条、P0 先行 | C2 保持 `k=6` 修覆盖；M1 用整轮回放选择 0/1/2/4/6 及字符预算 | Cowan 不直接规定 LLM 注入条数；500/501 漏召回会污染零召回评估（`memory/service.py:582-603`）。 |
| `MEMORY-REDESIGN.md:187-206` 固定多路配额 | `MEMORY-CANDIDATE-DESIGN.md:13-27,49-53` 的同域打分、动态扩页、认证/近似 | 固定份额不能保证第 501 条或全域前六。 |
| `MEMORY-ROLLOUT-PLAN.md:35-39,49` 候选与 BM25 同批 | **保留并强化**：影子可分批，正式切流必须同批 | `_bm25_scores` 与 `_rank_map` 对输入集合敏感（`memory/service.py:84-127,206-218`）。 |
| `SERIES-REPORT.md:231-249` 模糊时间“优先、改动小” | M1-T：沿既定桶增加固定时钟、时区和日期推算回放 | 用户桶已定（`状态记录:99-118`），但 ling 的来源时间/日界尚未验收；`updated_at` 混入强化。 |
| `LLM-RETRY-RESEARCH.md:349-355` “429 需改为 True” | 已是 True；修改路由消费 `retriable`，补 `Retry-After`/deadline | `openai_compatible.py:99-110` 已分类，`router.py:312-347` 当前不据此重试。 |
| `LLM-RETRY-RESEARCH.md:360-364,381-383` 3 次退避/可选软熔断 | 3 次含首次可作初值；总 deadline 必选，熔断按候选耗尽计；软熔断另测 | 单看 sleep 不等于最坏等待，`router.py:261-269,312-322` 的硬熔断与内部重试会交互。 |
| `DEFERRED-FALLBACK-DESIGN.md:9-13,101-118` “群私聊均用固定句/等下补” | 当前群聊不支持；仅可核实的可见晚回承诺和未答事实，不承诺补答时间 | `test_deferred_reply_flow.py:403-464`、`worker.py:1255-1263`；原稿示例自相矛盾。 |
| `MEMORY-ROLLOUT-PLAN.md:45-57` 未含人格和时间桶 | 在 E 前增加 P，在 M1 旁增加 M1-T；其余 C→R→F 主依赖保留 | 补齐用户新前提及三层时间观，同时不打乱已裁决的候选/BM25 同批。 |

## 4. 遗留问题与证据边界

**需用户决策**：既有数据库里存过旧默认 `system_prompt` 时，是让用户逐个人设在 UI 审核后保存，还是提供一次性、可预览且只命中精确旧默认文本的迁移工具；评审建议前者，因为 `persona/service.py:65-87` 明确不覆盖现有行。另需用户明确“拟人人设”允许怎样的自称；本文只保证系统不额外加身份判词，**不替用户写人格内容**。对外界面是否展示“模拟生活”标签的具体措辞也需按用户的人设目标确定，但来源/回执硬门不随措辞取消（`presence/service.py:206-208`、`activity/service.py:140,182`）。

**缺证据不能定**：M1 默认注入条数、绝对相关阈值、整轮召回门的误拒率、90 秒交互 deadline 是否合适、是否软化熔断、模型改写晚回句的收益、百万级精确向量 top-M 的时延都需要真实回放/故障注入/容量压测。`tests/eval/probes.yaml:29-30,255-257` 的 44 条只在 `k=6` 直接测检索，无法回答整轮注入、人设或投递质量；`baseline.json:14-25` 是旧策略基线，不能据其空结果率直接否定“应该为零”的新策略。`SERIES-REPORT.md:17,342-351` 里各模块覆盖范围和历史断言只在当时快照核过；本次未逐仓复验的插件细节标“没查到当前状态”，不作为新增 ling 功能的事实依据。
