# 注意力与事件边界：旧话题为什么容易被继续接下去

> 本文只做证据梳理与可检验规则提议，不改业务代码。凡涉及 Ling 的描述，均指本次研究时观测到的代码事实（见 `(1)`），不是对人类机制的直接实现。人类时长和容量数字只用于描述**原实验情境**，不用于推导 Ling 窗口大小。

## (1) 范围与方法

- 观测对象：`/Users/lingxi/project/ling`，`main`，HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`。开始时 `git status --short` 为空。
- 问题：为什么人有时会把「很久前的未完成事项」重新带回当前对话？什么条件下旧内容更容易被继续接下去？哪些来自认知机制，哪些来自界面/文本可见性？
- 方法：以可访问原文为准，只用 **PMC 开放全文 / Crossref 书目 / OpenAlex 摘要或书目** 三类来源；每条证据卡片写清出处、研究情境、可观察行为、适用边界，并标注**原文 / 二手 / 未核实**。不虚构页码——只有从 Crossref/PMC 页眉读到的卷期页码才写出，其余只写 DOI。
- 分层：认知层（人）与界面/文本可见性层（机器/产品）严格分开；两者都再映射到 Ling 的「模型输入」这一具体接口。
- 明确否定：不用人类的秒/分钟级事件时长、工作记忆容量（如 4±1 项）去设置或论证 `HISTORY_TOKEN_BUDGET`、窗口截断或召回条数。实验里的影片、叙事、实验室任务与即时通讯文本窗口在刺激形态、时间尺度、任务要求、可见性控制上都不可直接类比。

## (2) 证据卡片

### 卡片 A：事件分割理论（EST）——边界是「预测出错→工作记忆更新」

- 出处：**原文（PMC 开放全文）**：Zacks, Speer, Swallow, Braver, & Reynolds (2007). Event perception: A mind–brain perspective. *Psychological Bulletin*, 133(2), 273–293. DOI 10.1037/0033-2909.133.2.273（PMC2852534）；Kurby & Zacks (2008). Segmentation in the perception and memory of events. *Trends in Cognitive Sciences*, 12(2), 72–79. DOI 10.1016/j.tics.2007.11.004（PMC2263140）。
- 原文要点：人把连续活动分割成事件；感知系统持续对「接下来会发生什么」做预测，当预测误差短暂上升时出现主观的事件边界（Zacks 2007 摘要）。**事件模型（event models）是工作记忆表征**，用神经活动的短暂变化实现，整合当前感知与语义记忆里的事件图式（event schemata，永久突触变化）。多数时候模型稳定、对即时输入不敏感；当预测误差上升，门控机制打开，事件模型被重置并更多接受当前输入，随后进入新的稳定态。原文明确说：「event segmentation does not itself require attention; rather, it implements a mechanism of attention」（Zacks 2007 正文），且分割在不同时间粒度上同时发生（数秒到数十分钟是原实验情境的粒度，不是产品窗口尺寸）。
- 研究情境：被动观看日常活动影片、叙事阅读理解、fMRI/EEG/瞳孔测量；参与者先观看/阅读，事后才被告知去做事件分割，因此关键的神经与阅读时数据是在没有分割任务要求时采集的（Kurby & Zacks 2008）。
- 可观察行为：在叙事里出现时间或因果变化时阅读变慢；「一小时后」比「片刻后」更容易被标为边界，且随后对先前提到的物体再认更差、代词指代读得更慢；在影片边界处脑区活动与瞳孔直径短暂增加；穿过门进入相邻房间后对刚才物体的记忆下降（Kurby & Zacks 2008 综述的实验）。
- 适用边界：这是**知觉/理解连续活动**的理论，不是对聊天窗口或文本上下文直接成立的定律。原文的时间粒度指现实活动，不能拿来推 Ling 的条数或 token。事件模型是工作记忆表征，与长期记忆里的事件图式区分开。

### 卡片 B：边界的记忆效应——边界处编码更好，跨边界的旧信息更难接近

- 出处：**原文（PMC 开放全文）**：Swallow, Zacks, & Abrams (2009). Event boundaries in perception affect memory encoding and updating. *Journal of Experimental Psychology: General*, 138(2), 236–257. DOI 10.1037/a0015631（PMC2819197；同文有 2011 更正，见 PMC 页眉）。Ezzyat & Davachi (2011). What constitutes an episode in episodic memory? *Psychological Science*, 22(2), 243–252. DOI 10.1177/0956797610393742（PMC4451827）。
- 原文要点：EST 的推论是——边界处门控打开，边界信息被更充分编码进长期记忆；同时当前事件的工作记忆表征被更新/冲掉，使边界前信息的可接近性下降。原文区分 `presentation-boundary`（物体呈现时发生边界，预测更好编码）与 `delay-boundary`（呈现与测试之间发生边界，决定要从「当前事件」还是「上一事件」提取）。Ezzyat & Davachi 用「A while later…」与「A moment later…」操纵边界，发现**跨边界的下一句线索回忆更低**（边界前线索的下一句回忆低于无边界的控制条件），说明边界把长期记忆里的信息组织成更分离的情节，事件内部结合更紧。
- 研究情境：Swallow 等让参与者看日常活动电影片段，物体出现后约 5 秒做二选一再认，独立被试事先对影片做事件分割；Ezzyat & Davachi 让参与者逐句读 6 篇日常叙事（每篇 38 句），10 分钟后做线索回忆，实验 2 在 fMRI 内编码。
- 可观察行为：边界处呈现的物体更好被再认；若呈现与测试之间隔了边界，物体需要从长期记忆提取，非边界物体的再认成绩下降；叙事中跨边界的句子对更难联动回忆；边界信息本身在长期记忆中有特权地位。
- 适用边界：这是**短延迟（秒级）到十分钟级**的实验，材料是影片和句子，结局是「可接近性下降 / 编码增强」，不是「旧内容自动回到当前对话」。原文还提示一个反向因素：当前事件的表征也可能对当前提取造成干扰，相似信息越多干扰越大——所以「旧内容更容易被继续」与「旧内容更难被接近」可以同时成立，取决于它属于当前事件还是上一事件、是否被编码进长期记忆。**原文没有证明**文本窗口里旧话题会以同样方式被冲掉。

### 卡片 C：情境模型与事件索引——「时间/空间/人物/目标/因果变化」触发新模型

- 出处：**原文（Kurby & Zacks 2008 正文转述具体模型，属原文中的二手综述）**：同卡片 A 的 PMC 全文，正文写到 Structure Building Framework（Gernsbacher）与 Event Indexing Model（Zwaan 等）：当文本指涉新的人物、地点或事物，动作的时间/空间位置改变，或新的因果序列开始，读者建立新的心理模型；Event Indexing Model 列出**空间、时间、主人公、物体、目标、因果**六类变化。Swallow 2009 原文也列出「actors' positions or movement characteristics、locations、goals or intentions」的变化是人们分割事件的主要线索。Zacks 2001（*Psychological Bulletin*, 127(1), 3–21，DOI 10.1037/0033-2909.127.1.3，Crossref 书目，**全文未取得**）是这一取向的理论源头。
- 原文要点：模型更新有加工代价，所以读者在变化处读得更慢；时间变化与因果变化稳定地增加阅读时间，空间变化取决于读者已有知识和任务目标。
- 研究情境：逐句自定步速阅读文学叙事；也有比较「事件分割」与「阅读时间」的相关研究。
- 可观察行为：同一批句子中，被更多读者标为事件的从句读得更久；显式插入「一小时后」这类时间不连续短语既提高被判为边界的概率，也拖慢阅读。
- 适用边界：这些维度描述的是**被感知/被叙述的情境变化**，不是聊天消息的元数据字段。把「换话题」等同于「时间/空间/目标变化」只是类比，需谨慎标注为推理。原文说读者「slow down at event boundaries」，并不等于系统应在边界处注入旧内容。

### 卡片 D：工作记忆与注意——可借鉴的架构语汇，不可直接类比

- 出处：**原文 / 二手混合**：Baddeley (2003). Working memory: looking back and looking forward. *Nature Reviews Neuroscience*, 4, 829–839. DOI 10.1038/nrn1201（Crossref 书目核实，**全文未取得**）；Baddeley & Hitch (1974). Working Memory. In *Psychology of Learning and Motivation*, DOI 10.1016/S0079-7421(08)60452-1（Crossref 书目，**全文未取得**）；Cowan (1999). An embedded-processes model of working memory. In *Models of Working Memory*（DOI 10.1017/cbo9781139174909.006，OpenAlex 摘要核实）。Zacks 2007 与 Kurby 2008 的**原文**里都实际讨论了与 Baddeley 的关系。
- 原文要点：Cowan 的嵌入加工模型强调记忆与注意的联系，把工作记忆视为长期记忆中被注意激活的那部分（OpenAlex 摘要仅给出「relies upon five principles, which emphasize links between memory and attention」，详细原文未取得）。Zacks 2007 原文说事件模型在若干方面类似 Baddeley 后来提出的 episodic buffer，但事件模型是激活态而非长期突触改变。Baddeley 的多成分模型区分中央执行、语音环路、视觉空间模板和情节缓冲（此句为通行教科书转述，**本次未取得原文全文，标为二手/未核实**）。
- 研究情境：实验室工作记忆任务、认知控制与神经影像；Baddeley 与 Cowan 的原始材料不是即时通讯场景。
- 可观察行为：注意聚焦会影响哪些表征处于活跃状态；容量有限意味着并非所有信息都可同时保持（具体「4±1」等数字本次未取得原文核实，故不写死，也不用于推 Ling）。
- 适用边界：**不能把人类工作记忆容量或保持时长直接映射到 Ling 的 token 预算、历史条数或摘要层**。二者的差异不是程度而是类别：人类的激活/衰减是生物过程，Ling 的窗口是显式的字节/token 截断与选段逻辑。Zacks 2007 已明确提醒事件模型「not necessarily accessible to consciousness」，所以「模型看得到=人意识到」也不成立。

### 卡片 E：前瞻记忆与未完成意图的再激活——「当前情境像不像当初设想的执行条件」

- 出处：**OpenAlex 摘要核实**：Seifert & Patalano (2001). Opportunism in Memory: Preparing for Chance Encounters. *Current Directions in Psychological Science*, 10(6), 198–201. DOI 10.1111/1467-8721.00148（开放摘要）。**原文（PMC 开放全文）支撑的相邻事实**：Zacks 2007 关于事件图式编码目标与计划。Crossref 书目核实：Einstein & McDaniel (2005). Prospective Memory: Multiple Retrieval Processes. *Current Directions in Psychological Science*, 14(6), 286–290. DOI 10.1111/j.0963-7214.2005.00382.x（摘要经 OpenAlex 核实：反对单一容量消耗的监控，主张包括自发过程在内的多重提取）。Sellen, Louie, Harris, & Wilkins (1997). What Brings Intentions to Mind? An In Situ Study of Prospective Memory. *Memory*, 5(4), 483–507. DOI 10.1080/741941433（摘要经 OpenAlex 核实：自然场景、电子徽章、两周、时间任务与地点任务）。
- 原文要点：Seifert & Patalano 的**预测编码模型（predictive encoding model）**直接回答本文主问题——未完成目标会被编码进记忆并**与预期会出现的环境特征绑定**；这些特征最好是（a）达成目标所必需、（b）判断计划该启动时足够独特、（c）在环境中容易被识别。后来在环境里普通地感知到这些特征，就会自动识别「机会到了」，于是旧目标回到当前意识。Einstein & McDaniel 主张前瞻提取不只有耗资源的持续监控，也有对目标事件的自发/反射式反应。Sellen 等的自然观察显示意图的回想与实际所处情境（时间、地点）有关。
- 研究情境：实验室问题求解/机会识别、自然工作场景中的电子徽章采样、前瞻记忆实验范式。
- 可观察行为：在具备「当初设想的执行条件」的情境里，未完成事项更容易被想起来；单纯经过时间未必足够。
- 适用边界：前瞻记忆研究的是**人要在未来某时做某事**，不是模型在对话里检索记忆。把「被引用的旧话题」当作「目标线索」是工程类比，是否成立必须实测。

### 卡片 F：Zeigarnik 效应——经典结果、现代综合与局限

- 出处：经典原文题名与出处经 Crossref/OpenAlex 书目核实：Lewin (1927). Untersuchungen zur Handlungs- und Affektpsychologie. *Psychologische Forschung*, 9(1), 1–85. DOI 10.1007/BF02409755。常见英译题名 *On finished and unfinished tasks* 见 Zeigarnik, in Ellis (Ed.), *A Source Book of Gestalt Psychology*（DOI 10.1037/11496-025，Crossref 核实；**原书全文未取得**）。现代综合：**OpenAlex 摘要核实**：Wendsche, Weigelt, & Syrek (2026). Unfinished work tasks and work-related thoughts during off-job time: meta-analysis of the Zeigarnik effect in a work-recovery context. DOI 10.1080/10615806.2026.2616302（PubMed 41554526）。**二手/未核实**：Seifert & Patalano (1991). Memory for interrupted problems: The Zeigarnik effect revisited. DOI 10.1037/e665402011-235（仅 Crossref 书目，无摘要全文）。
- 原文要点：经典表述是被中断/未完成的任务比已完成的任务更容易被记住（源自 1927 年实验）。2026 元分析将「未完成任务」与「下班后的工作相关想法」综合：between-person k=17, N=2,473，ρ=.382；within-person k=14, N=12,129，ρ=.247；关联在（情感性）反刍上最强；在纳入的研究特征里只有女性比例是显著调节变量，定性综述还指出目标效价、行动调节与目标相关策略、特质时间焦点、主管绩效期望等调节因素。这为「未完成事项会侵入当前思绪」提供了现代、可量化的支持，但**限定在工作—恢复情境**。
- 研究情境：1927 年是实验室任务中断后对任务的记忆；2026 元分析是工作组织心理学问卷与经验取样，关注下班后的侵入性工作想法。
- 可观察行为：未完成任务与更多工作相关反刍相关；关联为相关而非因果，且个体间强于个体内。
- 适用边界与局限（必须写明）：①「未完成⇒记得更好」的经典结果有**复制与调节争议**，不能当成无条件定律；②Zeigarnik 1927 的样本与任务类型有限，跨语言/跨文化的现代元分析显示效应受性别比例、目标效价等调节；③元分析测的是**反刍/侵入性想法**，不是对话系统里的召回；④反向效应（reversed Zeigarnik effect）在文献中存在（Claeys, 1969，DOI 10.1037/h0027765，Crossref 书目核实，**未核实原文**）；⑤不能由此推出「系统的 open_questions 必须一直挂在提示词里」——那是设计选择，需要实验验证。

### 卡片 G：Ling 侧可观测事实（代码，不是人类认知证据）

- 出处：**原文（本地源码，HEAD 如上）**：`src/ling/kernel/context.py`、`src/ling/kernel/turn.py`、`src/ling/domains/memory/recall_query.py`、`docs/evidence/memory-context-recall.md`。
- 事实：①上下文编译把历史「从最新一条往回装填，直到 token 预算用尽」，页大小由调用方给、真正决定用多少的是预算；源码特别注明过去 `history_limit=20` 会让第 21 条之后的旧消息在提示词里整块消失且不被摘要覆盖，说明这一段是显式工程决策。`HISTORY_FETCH_CAP = 2000`。②存在摘要层：`window_ctx.summary` / `summary_through_sequence`，旧消息可被压入摘要而非原文。③窗口短记忆里有 `recent_topic` 与 `open_questions`，会作为「话题：…」注入。④自动长期记忆召回使用本轮原话查询，BM25 + 可选向量 + 重排；低信息整句在 `should_skip_automatic_recall` 命中时跳过召回。该函数用 `NFKC → strip → casefold → rstrip(" \t\r\n.!?。~")` 后与固定集合比较。⑤`needs_recent_topic` 用一组整句正则（如「后来呢」「然后呢」「具体是什么」）识别续接式提问。
- 边界：这些是**当前实现**的取样，不保证其他提交或运行期配置相同；「open_questions 会被注入」不等于「它会正确影响回复」。

## (3) 对「旧话题为什么容易被继续接下去」的分层解释

### 认知层（人；推理，置信度中）

一个重要区分：EST 说的是**新事件开始时，旧事件的工作记忆表征被更新/冲掉，旧信息可接近性下降**；而前瞻记忆与 Seifert & Patalano 的预测编码模型说的是**当环境特征匹配当初绑定的执行条件时，未完成目标会自动回到当前意识**。两者不矛盾：前者讲「离开旧情境后旧细节难接近」，后者讲「回到相似情境时旧意图被重新激活」。所以「很久前的未完成事项回到当前对话」最贴合第二类机制，而不是被 EST 直接推出。

具体可观察的条件：

- **未完成**：元分析显示未完成任务与更多反刍相关（卡片 F），但相关不等于因果，且有复制争议。
- **被明确引用**：在人类的事件索引框架里，人物/目标/因果等变化会更新情境模型（卡片 C）；反过来，当前话语里重新指涉某个旧目标会重新构造它的情境线索（预测编码模型，卡片 E）。注意这只是机制类比，原文没有「聊天引用会怎样」的实验。
- **被周期性提醒**：Zacks 2007 的多时间粒度观点指出同一活动可在细/粗粒度同时分割；周期性提醒相当于人为制造重复的、粗粒度的「机会线索」（卡片 E）。但「提醒频率越高越容易继续」在本次证据里**没有直接实验**。
- **与当前情境强关联**：Seifert & Patalano 明确要求线索要能被当前环境识别、且是启动计划的独特前提（卡片 E）；这与 EST 里「自上而下的事件图式影响边界判定」（卡片 A）方向一致。

### 界面/文本可见性层（机器/产品；置信度高，因为是源码事实）

这一层与人类的注意/工作记忆**不可直接类比**，但会产生相似的表面行为：同样一句旧话，如果在当前可见文本里，就容易被「继续接下去」；如果不在，就只能靠召回。Ling 的具体接口有四个：

1. 原文层：最新往回装到预算用尽 → 最近消息天然更容易被继续。
2. 摘要层：更早消息被压成摘要 → 旧内容以概括形式存在，细节丢失但仍可能被提及。
3. 短记忆层：`recent_topic` / `open_questions` 作为「话题：…」直接注入 → 未完成事项被显式挂在提示词里。
4. 召回层：本轮原话查询长期记忆；低信息整句跳过；`needs_recent_topic` 对「后来呢」等续接式提问保留查询 → 「继续接下去」的问法会改变输入。

### 模型输入层（实际进入提示词的东西）

认知机制**不能直接说明**模型为何继续旧话题；能说明的只有进入提示词的内容与顺序。可检验的命题是：旧话题的「可继续性」与它在提示词里的**呈现形态**（原文 / 摘要 / 短记忆话题 / 召回证据）有关，而不是与它「在人类记忆里多牢固」有关。任何把 EST 的预测误差或工作记忆容量当成提示词排序公式的依据，都缺乏本文证据支持。

## (4) 对 Ling 的可检验规则与反例

以下都是**待验证假设**，不是已证实的行为。判据写成条件式，避免抄一个期望值当阈值。

| 编号 | 可检验规则（条件式） | 若成立说明什么 | 反例 / 会失败的情形 |
|---|---|---|---|
| R1 | 同一旧事实放在原文层 vs 只进摘要层：若回复里对细节的引用率在原文层明显更高，说明「可见形态」比「事实新旧」更关键 | 支持界面层解释；与卡片 B 的「可接近性」方向一致 | 两形态无差异 → 说明模型主要靠语义而非位置；不能推出人脑机制 |
| R2 | 旧事实出现在 `open_questions` 且当前消息与其中条件匹配：若继续接话的比例高于不匹配条件，支持预测编码类比（卡片 E） | 类比得到弱支持 | 无论如何都继续提 → 可能只是短记忆常驻注入的强制效果，不能归因于「匹配」 |
| R3 | 当前消息是「后来呢 / 然后呢」（`needs_recent_topic`）且旧话题在摘要里：若续接成功率上升，说明续接式问法恢复了对旧情境的访问 | 支持「明确引用→继续」的路径 | 成功率不变 → 说明查询改写没起作用，不要归因为模型「记得牢」 |
| R4 | 低信息整句与带话题短句对比：`？` 经 NFKC+rstrip 后为空，走 `should_skip_automatic_recall=True`；`早上好` 不在固定集合里，**不跳过** | 决定「旧内容是否被召回」的是准入规则，不是人类式注意 | 若把 `？` 写成「一定触发召回」，该规则直接被推翻；lead 已实测确认此反例 |
| R5 | 在提示词里反转原文/摘要顺序：若回复里「最近一轮」的占比随顺序显著变化，说明位置效应存在 | 位置（可见性）效应可被实验分离 | 无变化 → 顺序不是有效杠杆，别再调排序权重 |
| R6 | 对同一旧事项，一次标为「已完成」、一次标为「未完成」：若未完成形态更容易被继续，支持 Zeigarnik 类比（卡片 F） | 提供产品侧弱证据 | 无差异 → 不要据此在提示词里强化未完成事项；经典效应有争议 |

明确的反例与禁令：

- **不要把 `？` 当成「触发长期记忆召回」的例子**：按 `should_skip_automatic_recall`，NFKC 去标点后为空，该轮跳过自动召回。真正未跳过的是 `早上好`（不在固定寒暄集合内）。这是 lead 实测更正，本文采纳。
- **不要用「4±1 项」「7±2 项」或「事件 5 秒/20 分钟」去设置 `HISTORY_TOKEN_BUDGET` / 召回条数**：二者不可类比。
- **不要写「事件边界让模型更新工作记忆，所以窗口应该在边界处截断」**：EST 讲的是人的知觉系统，不是提示词装配。
- **不要把 Zeigarnik 效应写成无条件定律**：有反向效应与调节因素；产品侧规则必须实测。
- **不要把「被周期性提醒」当成已验证因子**：本次证据只有多粒度分割和机会线索的理论支持，没有提醒频率的实验。

## (5) 未取得证据与来源 URL

未取得 / 未核实清单：

- Baddeley (2003)、Baddeley & Hitch (1974) 的**全文**（只有 Crossref 书目；正文中多成分模型的细节标为二手/未核实）。
- Cowan (1999) 的**全文**（只有 OpenAlex 摘要）；因此未使用任何具体容量数字。
- Zeigarnik (1927) 原文全文、英译原书全文；只核实书目与 DOI。
- Seifert & Patalano (2001) 的**全文 PDF**（Wesleyan/bepress 链接均无法直接取得）；预测编码模型要点来自 OpenAlex 开放摘要。
- Seifert & Patalano (1991)、Claeys (1969) 的全文与效应方向细节；只核实书目。
- Wendsche 等 (2026) 元分析的全文（H-BRS 仓库有 Anubis 验证墙）；数值来自 OpenAlex 开放摘要。
- Einstein & McDaniel (2005)、Sellen 等 (1997) 的全文（Wiley/Taylor & Francis 403 或付费）；要点来自 OpenAlex 开放摘要。
- Zacks, Tversky, & Iyer (2001) 全文；只核实 Crossref 书目。
- 「周期性提醒使旧内容更容易继续」「引用旧目标必然提高续接率」等具体产品假设：**本次无直接实验证据**。

实际访问并成功的 URL：

- https://api.crossref.org/works?query.title=event%20segmentation%20Kurby%20Zacks&rows=2
- https://api.crossref.org/works（多次 `query.bibliographic` / DOI 查询，具体 DOI 见各卡片）
- https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi（PubMed/PMC 检索）
- https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi（书目/PMC ID）
- https://pmc.ncbi.nlm.nih.gov/articles/PMC2263140/?report=xml （Kurby & Zacks 2008 全文）
- https://pmc.ncbi.nlm.nih.gov/articles/PMC2852534/?report=xml （Zacks et al. 2007 全文）
- https://pmc.ncbi.nlm.nih.gov/articles/PMC2819197/?report=xml （Swallow et al. 2009 全文）
- https://pmc.ncbi.nlm.nih.gov/articles/PMC4451827/?report=xml （Ezzyat & Davachi 2011 全文）
- https://api.openalex.org/works/...（按 DOI 查询摘要与开放获取状态，涉及的 DOI 见卡片 E/F）
- https://psycnet.apa.org/record/2001-06665-008 （仅返回 JS 加载壳，无正文）

访问失败 / 被拦截的 URL：

- https://html.duckduckgo.com/html/?q=... （fetch failed）
- https://www.google.com/search?q=... （fetch failed）
- https://en.wikipedia.org/wiki/Zeigarnik_effect （fetch failed）
- https://onlinelibrary.wiley.com/doi/10.1111/1467-8721.00148 （HTTP 403）
- https://wesscholar.wesleyan.edu/cgi/viewcontent.cgi?article=1287&context=div3facpubs （跨域重定向到 webapps.wesleyan.edu）
- https://webapps.wesleyan.edu/cgi/viewcontent.cgi?article=1287&context=div3facpubs （HTTP 404）
- https://works.bepress.com/andrea_patalano/11/ （跨域重定向到 digitalcommons.elsevier.com）
- https://pub.h-brs.de/frontdoor/index/index/docId/9416 （Anubis 验证墙，无全文）
