# 开源记忆系统对标：拟人化 AI 的写入、召回、睡眠整合与遗忘

调研日期：2026-11-09。范围是可核验的官方仓库、源码、文档和论文；“未核实”表示没有找到可证明该能力的实现。这里的“她”指本项目角色，不把模型生成的自述当作用户事实。

## §1 结论摘要

1. **Q1 该注入却没注入**：Generative Agents/MemoryOS 会把访问或热度纳入可用性；本项目应把高价值旧记忆独立召入候选池，而非只在最近 500 条内给 `access_count` 加分。[S1][S6]
2. **Q2 不该注入却注入**：Mem0 OSS v3 的检索有绝对阈值，Graphiti 有可配置重排下限；本项目应在融合排名后用“绝对相关性门”，允许零命中、零注入。[S2][S4]
3. **Q3 注入错事实**：Graphiti 的事实有效时间与入库时间分离、旧边失效；本项目应在写入时关闭冲突事实，召回时再做现行性与来源核验，睡眠整合不得复活旧值。[S3]
4. **Q4 睡眠压缩**：Letta 的后台记忆编辑和 Memobase 的缓冲区提炼最贴近角色生活节奏；本项目应把“梦”与“记忆整合”分开，让睡眠窗口调度可追溯、可拒绝发布的整合任务。[S5][S7]
5. **Q5 真的忘掉**：公开仓库展示了墓碑/删除和派生关系清理，却未给本项目 outbox 竞态的现成保证；本项目需同步撤回并取消待处理写入，以代次栅栏拦住已经认领的任务，再后台清理原文及派生物。[S3][S8]

这些是工程建议，不是“业界统一标准”；阈值、热度权重和保留期必须用本项目中文对话回放集校准。

调查口径与决策边界：
- “写入正确”至少包含主体、槽位、事实时间、来源、受众五个维度；同句语义相近并不等于同一事实。
- “压缩”至少有去重、实体归并、事件摘要、画像提炼四种；越靠后越容易改变证据等级，应分别设置发布条件。
- “召回”分候选生成、相关性裁决、注入表达三段；排名算法只解决第一、二段的顺序，不回答“该不该说”。
- “遗忘”有自然衰退、过期归档、用户删除三种语义；LFU/保留期与删除请求不可混同。
- “拟人化”要维护角色自我、对用户的了解、共同经历三个命名空间；角色的梦与感受不能成为用户画像的权威输入。

## §2 对比表

表内“无证据”仅指本次所读官方资料未证实，不能推断仓库绝无此能力。当前版本与历史设计分开记。[S2][S5]

| 仓库 | 写入：决定/去重/冲突 | 压缩：离线、证据 | 召回：路由、去噪 | 遗忘：删除、传播 | 拟人化价值 |
|---|---|---|---|---|---|
| Mem0 OSS v3（旧 v2） | v3 单次提取、精确哈希去重、ADD-only；v2 提取后由模型裁决 ADD/UPDATE/DELETE/NONE | 无明确睡眠阶段；原消息与提取事实的关系需另建证据表 | BM25+向量+实体，`threshold` 默认 0.1、可 rerank | API 可 update/delete；v3 旧事实并存，不能仅靠排名解决改口 | 用户事实抽取可借，当前 OSS 图记忆已移至 Platform [S2] |
| Letta / MemGPT | 代理/工具编辑 core blocks；会话进入 recall，长尾入 archival | compaction 摘要；sleep-time 代理后台编辑共享块；默认资料未证明每个块都有原文反链 | core 常驻，recall 历史/archival 按需查询 | 块/归档有删除 API；跨层派生级联未核实 | persona 与 human 块分开，稳定自我和用户画像 [S5] |
| Zep / Graphiti OSS | episode 抽实体/边；事实冲突使旧边失效，保留来源 | 社区摘要按实体连通聚合；不是人格事实的真值裁决 | 向量+BM25+图距离，RRF/MMR/cross-encoder，可设 `reranker_min_score` | `remove_episode` 可清仅由该来源支持的边/实体；“失效”不同于删除 | 改口、关系时间线与“以前怎样”最适合 [S3] |
| Cognee | `cognify` 分阶段抽取、建图、建向量；并非默认单值冲突裁决器 | 任务管道生成摘要、可记录 provenance；旧 `memify()` 已弃用，现用 `improve()`/自定义管道 | 图/向量混合检索；具体相关性门由调用方选 | 数据集更新/删可做；派生传播需按管道验证 | 适合后台加工框架，不宜整库替换 [S9] |
| A-MEM | 新经历变带上下文、关键词、标签的卡片；新卡动态连旧卡并演化描述 | 增量演化而非固定睡眠批；论文未证明默认原始证据链 | 卡片索引+语义关联，适合跨事件联想 | 删除级联/在途取消未核实 | 能保留“她经历过什么”的关联脉络 [S10] |
| MemoryOS | 短期 QA→中期 segment/page→长期 profile/knowledge；相似主题合并 | 容量和热度触发提炼；中期 page 留对话，长期画像证据反链未核实 | 短/中/长多层；segment、page 分级检索 | 中期容量按 LFU 淘汰；不等于用户请求的删除 | 访问热度与长期画像特别贴近陪伴 [S6] |
| Memobase | 每用户 blob 进入 buffer，按 topic/sub_topic 提炼 profile+事件 | buffer 过大或空闲后 flush；默认可移除原 blob，故画像可能失去原文 | profile 与事件时间线，可按 topic/预算组上下文 | 有 blob/profile/user 删除接口；在途 flush 竞态未核实 | 画像与情景分层直接可借 [S7] |
| Memary | KG 实体流+引用频率/最近度；显式冲突裁决未核实 | 无可核实睡眠整理与证据链 | 实体知识程度/频率辅助召回 | 传播删除未核实 | 适合作主题兴趣，不适合当真实经历 [S11] |
| LangMem | 热路径工具写入；后台 manager 提取、更新、合并/删过时项 | `ReflectionExecutor` 异步整理；源证据反链需应用自建 | 依赖 LangGraph store 检索候选；维护只覆盖选中的候选 | 可删所选记忆，未检索到的旧项可能永远不清 | 适合把提炼从对话延迟中拆出 [S12] |
| Basic Memory | 人/模型共编 Markdown observation+relation；人工可改 | 无默认睡眠整合；原文文件可审计 | 语义/混合检索、可选 rerank、沿链接构上下文 | 文件/笔记可删；衍生影响需另管 | 人可检查“她记得我什么”，但不是自动事实裁决 [S13] |
| ENGRAM（论文） | episodic/semantic/procedural 分型；同名开源仓库众多，无法唯一对照实现 | 论文有轻量编排，具体删除传播未核实 | 类型路由召回；没有可照搬的角色阈值 | 未核实 | 分类法可参考，仓库级实现暂不列为依据 [S14] |
| Generative Agents（研究原型） | observation 写入 memory stream；reflection 生成高层记忆 | reflection 按重要性累积触发，生成抽象推断；并非可信事实 | recency+importance+relevance 三因子 | 删除/竞态未核实 | 连续经历与情绪显著性解释“像一个人” [S1] |
| HippoRAG（研究原型） | 文档抽三元组建图；非用户画像写入器 | 离线索引知识图，非睡眠情节压缩 | 向量实体定位 + Personalized PageRank 多跳扩展 | 用户遗忘与派生删除未核实 | 可借多跳线索，不适合作全栈陪伴记忆 [S15] |

表格里的“可追溯”需要区分：能看到源文本、能定位源事件 ID、能检查源现在是否有效，是三个强度不同的能力。
本项目已有 `source_event_ids`，但 `compact_low_value()` 只把前 40 个事件 ID 放进新摘要，无法证明遗漏的来源也仍有效。
若摘要源集合超过可保存上限，应拆分摘要或改存关联表，不应截断来源后仍以最高置信度发布。
对 persona，建议只在少量稳定块存“她是谁/怎样相处”；用户画像中的偏好和限制仍需各自的来源与适用受众。

## §3 逐仓库要点：只保留本项目能用的部分

**Mem0。** 旧算法的 `update_memory` 是“提取新事实→找相近旧事实→模型给每项动作→应用”，`NONE` 相当于幂等无操作；但这不是当前 OSS 默认。v3 官方迁移文档明确是 ADD-only、精确哈希去重、相近旧事实作提取上下文，图功能移至 Platform。对本项目，动作分类可以做辅助建议，单值槽位仍应由代码原子关闭旧值；不能用 v3 的“新事实排在旧事实前”代替现行性过滤。[S2]
旧 OSS 图记忆通过外部图数据库存实体/关系；v3 OSS 删除图存储，保留实体匹配作为混合检索一路；Platform 的原生图记忆不是本次“开源库可直接抄”的实现。[S2]
它的阈值可说明“绝对门”存在，不能移植其数值：Mem0 的检索分与本项目 RRF、BM25、cosine 不是同一尺度。

**Letta / MemGPT。** 旧版三层是每轮在上下文的 core blocks、可搜索的归档、会话 recall；当前文档也强调块的编辑语义（`memory_replace`/`memory_insert`/`memory_rethink`）。sleep-time agent 确实存在：后台读近期上下文并修改共享块；它是计算时机与职责分离，不是系统自动保证“摘要正确”。本项目的 `CoreMemoryService` 已有常驻块；可借后台审阅流程，但不要让睡眠代理直接覆盖带来源的用户事实。[S5]
块编辑的优点是可定点修订：如“称呼”变化，只替换称呼块；缺点是块一旦承载多项混合事实，单项撤回便难以级联。

**Zep / Graphiti。** 边的 `valid_at/invalid_at` 记录事实何时成立，`created_at/expired_at` 记录系统何时创建/失效；episode IDs 保留出处。新居住地出现时，旧边失效而非抹去，因此历史问答仍可查。社区摘要是高层导航信息，不能绕过边的有效期和来源；删 episode 要清只靠该 episode 支撑的边/实体。本项目已有 `valid_until/superseded_by` 的雏形，补“系统时间”和派生反向索引比引入图库更直接。[S3]
双时间轴允许回答两个不同问题：“当时事实是什么？”与“系统在当时相信什么？”；它不会自动识别讽刺、引用或主体混淆。

**Cognee。** `cognify` 将分块、抽实体关系、建图、嵌入和摘要拆成可观察任务；源码还可按配置记录 provenance。旧 `memify()` 的无任务默认行为已变成空操作且被标记弃用。适合借“可重跑、可观测的整合管道”，不适合把 `memify()` 当成今日可用的一键睡眠压缩 API。[S9]
本项目可把每次睡眠整合的输入集合、规则版本、拒绝原因作为作业元数据，失败后重跑同一批而不新增第二份画像。

**A-MEM。** Zettelkasten 式卡片拥有上下文、关键词、标签和动态链接；新事件也可促使旧卡片描述演化。对角色有用的是把“用户提过妈妈”与“妈妈的忌日”连起来，不用词面相同才召回。但卡片的衍生描述需与原文证据分离，不能自动升级成“用户确认过的事实”。[S10]
动态链接可用于“想起相关事件”的候选扩展，链接本身不应提高事实置信度或拓宽私聊受众。

**MemoryOS。** 短期 QA 溢出形成中期主题 segment/page；访问次数、互动长度、时间衰减影响热度，热 segment 可促进长期 profile/knowledge 更新。源码容量超限调用 `evict_lfu()`，因此“热度提升”和“LFU 淘汰”是两件事，不能写成低热度直接物理删除。对本项目可借热度候选路与分层提炼，不借其无证据门的画像推广。[S6]
访问强化应只由“实际注入并被使用”驱动；检索到却被预算截断不应加热，否则旧噪声可能反复占位。

**Memobase。** 每用户 buffer 按大小/空闲 flush，输出 topic/sub_topic/profile 和事件；其文档说处理后默认可移除 blob。陪伴系统需要画像，但本项目应反过来保留可审计源 ID，至少在保留期内使画像能降级或重建。`context()` 默认会打包画像，必须先做本项目的受众、时效和相关性门。[S7]
画像适合“长期喜欢清淡口味”，情节适合“上周一起讨论了做饭”；把两者混为一句，会让角色在当前不相关时主动提旧事。

**Memary、LangMem、Basic Memory、ENGRAM。** Memary 的实体频度可补“共同话题熟悉度”，不能替代情节事实；LangMem 的后台 manager/ReflectionExecutor 可借调度边界，但候选只取到一部分时，未命中的陈旧项不会自动被清理；Basic Memory 的可读 Markdown 示范了人工核对与关系链接。`enGRAM` 名称不唯一：本次只核实 ENGRAM 对话记忆论文，未核实与之对应的唯一代码仓库，不对其删除/阈值能力下结论。[S11][S12][S13][S14]

**Generative Agents、HippoRAG。** 前者以近期性、重要性、相关性挑选经历，并用 reflection 产生抽象想法；reflection 不是“事实提升”。后者用图连接与 Personalized PageRank 找多跳证据，可在本项目实体图成熟后借局部扩展，不必现在引入整套索引。[S1][S15]
情绪显著性可提升“候选优先级”，例如用户主动提到重要日期；但高情绪强度不能把角色自己猜的事实转成用户陈述。

## §4 五个问题的答案：业界怎么做 → 本项目规则

### Q1 该注入不注入：高价值旧记忆进不了候选池

业界：Generative Agents 按“最近**访问**”而非只按创建时间算近期性；MemoryOS 的访问会提高 segment 热度；Graphiti 和 HippoRAG 可借实体/图路发现词面不重合的候选。[S1][S6][S3][S15]
本项目：`recall()` 先按 `updated_at DESC LIMIT 500` 圈定断言，再算 `access_count`；`reinforce()` 更新 `last_injected_at/access_count`，不改 `updated_at`，所以排序加权救不了池外旧记忆。不要为了召回而篡改 `updated_at`（它是内容修改时间）。
规则 1：各路独立限额并 `UNION` 去重：最近更新 200、最近实际注入/高热度 150、BM25/向量前 150、槽位/实体精确命中至多 100；总预算再裁剪，所有路先套主体/受众/有效期硬门。
规则 2：热度用 `log1p(access_count) × exp(-距 last_injected_at 的天数/半衰期)`；单次被提起不永久保送，显式纠正与重要边界可单独保留，但每轮是否注入仍过相关性门。
规则 3：情节查询在数据库侧分路 `LIMIT`，再对情节和断言**共同**做近重复抑制；保留独立事件 ID，避免同一事件的多份摘要占满证据车道。
规则 4：`last_recalled_at` 与 `last_injected_at` 分开记；只用后者计算“这条真的帮她接上话”，并给热度封顶/衰减。
规则 5：单值槽位的当前值可有一条精确候选通道，但不意味着每轮常驻；用户问“你记得我住哪吗”才可高优先触发。
验收：构造第 501 条之后仍被实际使用的旧偏好、词面不匹配但语义相关的禁忌、同事件重复情节，分别测候选入池率、最终注入率与重复率。
诊断日志分别记“因池外漏掉”“池内相关性门拒绝”“预算截断”数量，避免把三种漏召都归咎于 embedding。

### Q2 不该注入注入了：排序并不等于相关

业界：Mem0 v3 明确检索 `threshold` 默认 0.1（仅其分数尺度）；Graphiti 低层 `reranker_min_score`、MMR/cross-encoder 支持相关性过滤；相对名次只是排序。[S2][S4]
本项目：目前 `relative_score_floor × 第一名` 且 `kept or scored[:1]`，第一名再不相关也会留下；RRF 值约在 `1/(60+rank)` 附近，不是语义置信度，不可拿 Mem0 的 0.1 直接套它。
规则 1：独立保留原始 BM25、向量 cosine、实体命中、可选 reranker 分；RRF 只作融合排序，绝对门只看同一尺度的原始相关性或经校准的重排分。
规则 2：设 `eligible = 强字面命中 OR 已校准向量门 OR 已校准重排门 OR 明确槽位/实体关系`；全不满足直接 `hits=[]`，不要“至少保一条”。阈值先通过带人工标签的中文正/负例选择，按断言与情节分别校准；不写拍脑袋常数。
规则 3：硬门顺序是主体/受众→有效期/撤回→相关性→去重→预算→渲染；`core` 常驻只装少量经确认的称呼、边界、稳定角色信息，不能成为绕过相关性门的画像垃圾桶。
规则 4：对用户明确问及记忆的询问，可采用较高召回预算，但相关性与来源硬门不松；没可靠证据时说“不确定”，不凭候选列表凑一个答案。
规则 5：重排器缺席、失败或只返回部分分数时，未评分项要重新过原始信号门，不能因 RRF 兜底进入提示词。
验收：寒暄、无关话题、过期住址、群 A 私聊敏感事、完全零相关对话都应零证据注入；“你还记得我妈妈忌日吗”应准确命中并标来源。
校准时分别统计误注入率与漏注入率，再选门槛；不要只优化单一“命中率”，否则把每轮都塞满也会得到好看的数字。

### Q3 注入错了：改口、时间和证据等级

业界：Graphiti 对边记录事实有效区间及系统创建/失效时间，更新时让冲突边失效；Mem0 v2 会在写入时裁决动作，v3 ADD-only 则把正确性负担更多放在检索侧。[S3][S2]
本项目：`propose()` 对 `lives_in/name/...` 已关闭旧单值，`recall()` 默认排过期，历史问题才开放；但情节全文仍可能含旧值，压缩摘要也可能再写入“现行事实”。双时间轴本身只是表示法，不能自动识别“改口”、也不能自动核实冲突。
规则 1：写入时按 `(subject, slot, source scope)` 比较新旧；显式用户改口优先，单值关闭旧值并指向新 ID；多值只做去重/冲突标记，模糊断言不要擅删。
规则 2：新增 `recorded_at/superseded_at` 或系统修订日志，保留 `valid_from/valid_until` 作为事实时间；迟到补录必须以事实时间和来源判定，不能简单按入库时间“最新覆盖”。
规则 3：召回时查询当前值要求 `valid_from<=now<valid_until` 且来源仍有效；“以前”查询须输出当时区间，历史段不得作为当前状态；情节要标为经历/引语，不直接升级为断言。
规则 4：睡眠任务只对现行、未撤回、有可核验 `source_event_ids` 的事实生成衍生项；出稿前与发布事务内各验证一次，任何来源失效则不发布。
规则 5：不同来源对同一槽位冲突且无法判断改口时，记为“待确认”，不要靠入库时间或模型自信度自动任选一个。
验收：先北京、后上海、再广州，当前只说广州；问“以前住哪”按时序答；旧情节/旧社区摘要不能把北京说成现居。
再验收“用户说朋友住广州”“角色曾建议住广州”“用户后来否认住广州”三例，确保主体、来源和否定语义都正确。

### Q4 睡眠压缩：压什么、何时压、如何防复活

业界：Letta 后台 sleep-time agent 编辑共享块；Memobase 空闲 flush 为画像；MemoryOS 热 segment 促进长期提炼；Cognee 管道可分步骤、观察状态。没有一套现成方案能替本项目完成来源与有效期判定。[S5][S7][S6][S9]
本项目：已有每日 `memory.archive` 及 `compact_low_value()`，梦境 `dream.generate` 是模拟叙事、`sleep.settle` 是情绪结算，二者都不应作为事实写入器；可把记忆整合挂到睡眠/空闲维护，但作业独立、幂等、可迟到补做。
规则 1：先去重情节、合并同一事件的重复表达；再做主题/关系导航摘要；最后才提炼少量稳定 profile。角色梦中的片段不产生用户事实，也不反向改写 persona。
规则 2：按 `(subject, audience, source_window, origin, slot, 有效期区间)` 分桶；单值 `lives_in/name/works_at/...` 不拼多值摘要。过期或撤回来源不得参与当前事实摘要。
规则 3：每个摘要保存 `derived_from_memory_ids`、源事件 IDs、源修订/代次、`source_validity_intersection`、生成策略版本；只有所有支持源仍现行且受众兼容才可进入当前证据。模型摘要只能是“主题导航”或“可能的概括”，不能冒充原话。
规则 4：压缩时不物理删原始证据；若保留期到期、用户删除或源事实失效，反向索引级联失效摘要/画像/图边/向量，后台重建。原始证据已清除而摘要无法证明独立来源时，摘要也退出召回。
规则 5：sleep 作业写入草稿摘要与来源清单，发布事务里核对每个来源 ID、修订和受众；任一失败则整批不发布，避免半新半旧画像。
规则 6：社区/主题摘要只用于“去哪里找”，提到住址、健康、生日等易变或敏感槽位时必须回查当前断言，不能直接把摘要塞进提示词。
事故回放：三条过期居住地“北京、上海、广州”必须被拒绝合并；即便已有该摘要，读侧也要因来源无效而排除。
睡眠窗口只是调度时机，不是压缩资格；白天积压或夜间漏跑时可幂等补做，同一来源修订不能生成两份当前摘要。

### Q5 真的忘掉：用户不用说“忘了”，系统也不能复活

业界：Graphiti 的 `remove_episode` 处理仅由该 episode 支持的边/实体；Memobase 有删除 blob/profile/user 的接口；这些不是“撤回请求与异步任务原子一致”的公开承诺。**同步墓碑+代次栅栏+后台清理是本项目需要设计的规则，不声称是上述仓库现成功能。**[S3][S7]
本项目：`forget_subject()` 撤回断言、归档情节、停用 core/awareness，但不处理 outbox；只读真库显示 `memory.extract` 仍有 24 条 pending 且 24 条 payload 带 `user_text`，足以在清除后重新写入情节或事实。
规则 1：遗忘事务内先锁主体状态、递增 `memory_generation` 并写持久墓碑；同事务标记匹配的 pending/in_flight `memory.extract` 为取消，并擦除其 `user_text/actor_text` payload，提交后才报告“已停止记忆”。
规则 2：仅改 outbox 状态不能停住已认领 handler；生产者入队携带代次，消费者在读来源前、模型调用后和写库提交前检查代次/墓碑。最终写入须与遗忘共用主体锁或原子条件插入，保证先后顺序；旧代次重试一律 no-op。
规则 3：后台按来源反向索引清画像、摘要、图边、embedding、检索缓存/快照和历史 outbox 原文；清理失败可重试，读侧始终先查墓碑。仅保留无正文、最小化的取消审计和恢复防复活标记，并定义备份恢复后的墓碑重放策略。
规则 4：日常热度衰退/归档完全后台进行，不要求用户说“忘了”；用户若主动请求删除则 API 在同步屏障提交后回应，后台物理清理进度单独可查，不假称立刻从所有副本物理消失。
规则 5：主体级遗忘后若用户继续聊天，新代次的新对话可正常生成新记忆；旧代次原文、摘要和模型回包不得混入新代次。
规则 6：若只删除某一主题，将墓碑作用域缩到源事件/槽位并维护派生反链；只靠文本关键词搜索无法证明已清尽所有改写后的画像。
验收：pending、in_flight、模型调用中、提交前、重试、备份恢复六个交错场景均不得再出现可召回内容；清理后的派生物与原文扫描应为零。
前端表达应把遗忘描述为后台完成的清理与当前会话边界，不要诱导用户必须说“忘了”才能保持数据库卫生。

## §5 对本项目的具体建议（按优先级）

**P0 · 能直接抄：** 把 Graphiti 式“当前事实/历史事实”与现有硬门贯通到情节、摘要和 core；读侧不以 RRF 第一名保底，零相关即零证据。入口在 `service.py:recall/render_evidence` 与 `context.py`；先做人工标注的中文负例集。[S3][S4]

**P0 · 需要改造：** `forget_subject` 同步加持久墓碑、代次及 outbox 取消/脱敏，worker 做提交前栅栏；扫全派生物。这里不能只抄某个库的 `delete()`，本项目最现实的缺口是异步重放。[S3][S7]

**P1 · 能直接抄思路、需要本地 SQL 改造：** MemoryOS/Generative Agents 的访问强化只作用在候选池时没有用；独立热度/实体/全文候选路入池，再融合。不要更新 `updated_at` 伪装内容变新。[S6][S1]

**P1 · 需要改造：** 情节入库按来源 `turn_id` 幂等之外加内容指纹或事件链去重，历史存量做可回滚的重复标记。2026-11-09 只读统计：43 条 live episode 中 25 条落在完全相同的 `(subject_id, summary)` 重复组（多出的重复行 18 条），与用户提供的“25 条重复”口径一致；当前断言 live 数为 0。

**P1 · 能直接抄管道思想、需要来源模型：** 用独立睡眠整合作业串联“去重→冲突复核→导航摘要→画像提炼→验证→发布”；借 Letta 的后台编辑时机、Cognee 的可观测任务和 Memobase 的 topic profile，但将结果限定为可追溯派生视图。[S5][S9][S7]

**P2 · 需要改造：** 在既有实体关联上做一跳扩展，确有多跳中文回放收益再试 HippoRAG 式传播；图索引只是候选生成器，不给它绕过受众/墓碑门的资格。[S15]

衡量口径：按场景记录“该注入而漏掉/不该注入却注入/现行事实答错/删除后复活/来源不可追溯”，同时记录候选各路贡献、绝对门拒绝原因、最终被预算截断的数量；不能只看平均检索分或 LoCoMo 总分。

## §6 明确不建议照搬的部分

1. 不照搬 Mem0 v3 的“冲突事实并存、检索时让新事实靠前”：住址、称呼、边界改口是信任问题；现行性必须是硬门，旧值仅供历史问答。[S2]
2. 不把 MemoryOS 的“热”当“真”：高频提及的误解/玩笑同样会变热；热度只能影响候选与整合优先级，不能提升来源等级。[S6]
3. 不让 Letta 式 sleep-time agent 直接重写用户画像/core：共享块编辑方便，但未经证据校验的推断会每轮常驻；先在草稿区审验，再发布。[S5]
4. 不把 Graphiti 社区摘要、A-MEM 卡片演化或 Generative Agents reflection 当原始事实：聚合越深，越需要反链及失效传播；“她的感受”与“用户说过”必须分栏。[S3][S10][S1]
5. 不为 43 条情节先上 Neo4j/全套 HippoRAG，也不直接引入已弃用 `memify()`；先修候选、阈值、去重和删除竞态，现有 PostgreSQL 足以表达来源与双时间轴。[S9][S15]
6. 不把墓碑当永久保存用户原文的借口：墓碑只存最少的主体/代次/时间/状态；真正遗忘还需后台清理、备份恢复防复活和清理失败告警。

## §7 参考资料

均于 **2026-11-09** 访问；`main` 是当日可见版本，后续实现前需固定 commit 复核。仓库地址与关键路径一并列出，便于复查。

- [S1] [Stanford Generative Agents 仓库](https://github.com/joonspk-research/generative_agents)；[论文](https://arxiv.org/abs/2304.03442)，memory stream / recency / importance / relevance / reflection。
- [S2] [mem0ai/mem0](https://github.com/mem0ai/mem0)；[`mem0/memory/main.py`](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)；[`docs/migration/oss-v2-to-v3.mdx`](https://github.com/mem0ai/mem0/blob/main/docs/migration/oss-v2-to-v3.mdx)；[`mem0/configs/prompts.py` 的旧四动作提示词](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py)；[Platform 图记忆说明](https://github.com/mem0ai/mem0/blob/main/docs/platform/features/graph-memory.mdx)。
- [S3] [getzep/graphiti](https://github.com/getzep/graphiti)；[`graphiti_core/edges.py`](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)；[官方总览](https://help.getzep.com/graphiti/getting-started/overview)；[社区](https://help.getzep.com/graphiti/core-concepts/communities)；[`remove_episode` 的 MCP 调用](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)。
- [S4] [Graphiti 搜索文档](https://help.getzep.com/graphiti/working-with-data/searching)，RRF/MMR/cross-encoder 与 `reranker_min_score`。
- [S5] [letta-ai/letta](https://github.com/letta-ai/letta)；[旧版 context hierarchy](https://docs.letta.com/v1-sdk/memory/context-hierarchy)；[memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks)；[sleep-time 示例源码](https://github.com/letta-ai/sleep-time-compute/blob/main/run_stateful_aime_stream.py)；[API `enable_sleeptime`](https://docs.letta.com/api/python/resources/agents)。
- [S6] [BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS)；[`memoryos-mcp/memoryos/mid_term.py`](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-mcp/memoryos/mid_term.py)；[官方文档页](https://github.com/BAI-LAB/MemoryOS/blob/main/docs/docs.html)；[论文](https://arxiv.org/abs/2506.06326)。
- [S7] [memodb-io/memobase](https://github.com/memodb-io/memobase)，README 的 buffer/flush/profile/context 与 delete 示例；[服务端说明](https://github.com/memodb-io/memobase/blob/main/src/server/readme.md)。
- [S8] 本项目 `src/ling/domains/memory/service.py:1431`、`src/ling/worker.py:388`、`src/ling/kernel/turn.py:1791`；真库只读 `outbox_events` 统计（2026-11-09）。
- [S9] [topoteretes/cognee](https://github.com/topoteretes/cognee)；[`cognee/api/v1/cognify/cognify.py`](https://github.com/topoteretes/cognee/blob/main/cognee/api/v1/cognify/cognify.py)；[`cognee/api/v1/memify/memify.py`](https://github.com/topoteretes/cognee/blob/main/cognee/api/v1/memify/memify.py)。
- [S10] [WujiangXu/A-mem](https://github.com/WujiangXu/A-mem)；[A-MEM 论文](https://arxiv.org/abs/2502.12110)。
- [S11] [kingjulio8238/Memary](https://github.com/kingjulio8238/memary)，README 的 Memory Stream / Entity Knowledge Store。
- [S12] [langchain-ai/langmem](https://github.com/langchain-ai/langmem)；[`docs/docs/background_quickstart.md`](https://github.com/langchain-ai/langmem/blob/main/docs/docs/background_quickstart.md)；[维护候选范围讨论](https://github.com/langchain-ai/langmem/discussions/193)。
- [S13] [basicmachines-co/basic-memory](https://github.com/basicmachines-co/basic-memory)；[`NOTE-FORMAT.md`](https://github.com/basicmachines-co/basic-memory/blob/main/NOTE-FORMAT.md)。
- [S14] [ENGRAM 论文](https://arxiv.org/abs/2511.12960)；同名项目无法唯一对应，仓库实现与删除传播**未核实**。
- [S15] [OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG)；[`src/hipporag/HippoRAG.py`](https://github.com/OSU-NLP-Group/HippoRAG/blob/main/src/hipporag/HippoRAG.py)。
