# 话题的建立、延续、结束与主动开启：人类话语研究的证据整理

> 观测对象：本文件为只读调研，基准 HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`，分支 `main`，开始时工作树干净。只新增本证据文档，不改业务代码。
>
> 读法：凡标「原文引文」的段落，都来自本次实际下载/打开的全文或官方摘要页；凡标「未核实」的，只作为假设或对照，不作为 Ling 的参数依据。

## 1. 方法与来源可靠性

### 1.1 方法

1. 先钉观测对象（`git status --short` 为空、`git log -1` 为上述 HEAD、`git branch --show-current` 为 `main`）。
2. 用 OpenAlex / Crossref / arXiv API 找论文与开放版本，避免凭记忆猜 DOI 或 ACL ID；本轮有两次 ACL ID 猜错，已丢弃，不进入证据。
3. 能取到 Wikipedia/机构仓库全文的，下载到 `/tmp` 后用 pypdf 提取正文，逐字核对引文与页码；取不到的，只用官方摘要页与出版商元数据，并明确写「全文未取得」。
4. LLM 长上下文研究只作对照：它证明的是「模型使用上下文的方式」，不是「人类工作记忆」「固定时长」或某篇论文的数值可直接搬进 Ling。

### 1.2 来源可靠性分级

| 级别 | 来源 | 本轮状态 | 能用来做什么 |
|---|---|---|---|
| A：原文全文已核对 | Drew & Holt (1998) 机构仓库 PDF，29 页，正文逐字提取 | 已下载 `/tmp/drewp1.pdf`，提取 86,934 字符 | 给可观察行为、顺序结构、反例的直接引文与页码 |
| B：官方摘要/元数据已核对 | Grosz & Sidner (1986) OpenAlex 记录 + 摘要倒排索引；Drew & Holt 的 Crossref/White Rose 摘要页 | 已核对标题、作者、年份、摘要文本 | 给概念定义与研究情境；不能替代全文细节 |
| C：LLM/NLP 全文或摘要已核对 | Liu et al. (2023) arXiv HTML 全文；中文话题转换与切分论文 arXiv 摘要 | 已核对摘要/部分正文 | 只作工程对照，不作为人类参数 |
| D：仅书目线索 | Sacks/Schegloff/Jefferson、Star & Griesemer、Grosz & Sidner 1990 等 | 本轮未取得全文 | 只列「未核实」，不写具体结论 |

本轮实际访问的完整 URL 列表见第 7 节；访问失败项见第 6 节。

## 2. 研究证据卡片

### 卡片 A：话题关闭与转换是一个协作序列，不是单点「切题」

- **来源**：Paul Drew & Elizabeth Holt, *Figures of speech: Figurative expressions and the management of topic transition in conversation*, **Language in Society 27:4 (1998), 495–522**。DOI `10.1017/S0047404500020200`（Crossref 别名 `10.1017/S0047404598004035`）。开放版：White Rose Research Online `eprints/1545/1/drewp1.pdf`。
- **可靠性**：A。全文已下载并逐字核对；以下引文页码按期刊页码（PDF 提取中可见 502/505/506/507 等页眉）。
- **研究情境**：自然发生的电话/日常谈话语料；作者研究「比喻性表达出现在对话的什么位置」，不是实验室判断任务。自我限定：这不是所有话题终止的通用理论，而是其中一类有序实践（见 p. 502 "Hence the account we are developing here is not a general account for the termination of topics"）。
- **原文引文（p. 505 附近）**：

  > "These cases illustrate clearly a sequential pattern in which a figurative expression is produced in a turn that results in the termination of a current topic. That termination is managed by the participants in a brief exchange of agreements, followed by a transition to a new /next topic."

- **原文引文（p. 505–506，标准序列）**：

  > "1 Speaker A: Figurative summary / 2 Speaker B: Agreement (or other expression of contiguity) / 3 Speaker A: Agreement /confirmation / 4 Speaker A /B: Introduces next topic"

- **可观察行为**：
  - 说话人 A 用一句**总结性、评价性**的话把前面铺陈收束（例 "you can't win really"、"he had a good innings"）。作者称其为 "summary assessments"，并指出它们 "move away from, or step out of, their report of empirical details"（p. 502）。
  - 接收者 B 的下一步是**极简同意**（"No::."、"Oh no."、"Yes"），并且**不展开**上一话题。原文："The recipient had the opportunity ... to continue talking about the topic ... Instead, she produces a near-minimal unelaborated agreement; in doing so, the recipient declines the opportunity to develop that topic any further."（p. 505）
  - A 再用一个对称的极简确认，或**直接跳到第 4 步**开启新话题；B 也可以自己直接开新话题。原文说第 3 步可省略，"This is evidence that the recipient's topically disengaged minimal concurrence ... is sufficient to give either speaker the opportunity to embark on a new topic."（p. 506）
  - 双方**同时**开启新话题被视为强证据，说明这个位置是共享可识别的换题点："simultaneously performing the same action ... provides evidence for the shared intersubjective 'reality' of this position"（p. 507）。
- **适用边界**：
  - 只证明「有一类话题关闭是这样做的」，不证明所有话题都必须经过比喻总结；作者明确点名 repetition、其他 assessment 也能关闭话题（p. 502）。
  - 语料是英语电话谈话；中文文本对话是否同构，本轮没有中文 CA 语料证据。
  - 「极简同意 + 不展开」必须放在序列里看；单独一句「嗯/好」不是关闭信号。

### 卡片 B：旧话题如何被重新接上——「换了话题再回来」也算新话题

- **来源**：同上，Drew & Holt (1998), p. 518 注 9。
- **可靠性**：A（原文已核对）。
- **原文引文**：

  > "By 'new,' we mean a next topic, different from that which has been concluded following the production of a figurative expression. Hence, speakers may return to a topic previously talked about in the conversation, e.g. in exx. 5–6. The point is, though, that they change topic."

- **研究情境**：作者在界定什么叫 "new topic" 时发现，例 5、6 中谈话人**回到了前面已经谈过的事**。
- **可观察行为**：判断「新话题」的判据不是「这个内容从未出现过」，而是「相对于刚被关闭的那个话题，它现在是一个不同的下一步」。因此，回到旧事只要中间发生过话题转换，就被参与者当作换题处理。
- **对「沉下去的旧话题凭什么被继续接下去」的直接含义（推理，置信度中-高）**：人类接续旧话题靠的不是「记忆未被清空」这一内部状态，而是**当前这一步可识别地重新指向一个先前话题**，并且在它之前有可识别的退出/转换。换句话说，桥梁是「可重识别的旧内容 + 明确的再进入动作」，不是「它还在工作记忆里」。
- **适用边界**：这是注脚里的观察，不是专门研究话题回返的统计结果；例 5–6 的具体回返机制未在本文展开。

### 卡片 C：「自然结束」靠什么识别——看对方是否放弃展开，而不是看沉默多久

- **来源**：同上，Drew & Holt (1998), p. 505–506、p. 515、p. 517。
- **可靠性**：A。
- **原文引文（成功关闭）**：

  > "The significance of this sequence is that it reflects ... the fact that co-participants regularly and systematically orient to the summary and closing implications of the figurative expression ... This is displayed in the way that both speakers systematically withhold any further topical development or elaboration."（p. 506）

- **原文引文（失败关闭）**：

  > "when the production of a figurative summary of a topic fails to lead to the termination of that topic and the transition to a next, then the failure and its attendant topic continuation are associated with a lack of accord, and perhaps with incipient disagreement, between the participants."（p. 515）

  > "This occurs when there is some conflict or non-alignment between participants, manifest in part through the recipient's unwillingness to agree to – or perhaps more aptly, in 14–15, to concede to – the prior speaker's figurative assessment."（p. 517）

- **可观察行为**：
  - 成功识别：总结之后，接收者给出**话题适配但不提供新素材**的最小回应；随后任一方开启新话题；双方都不再展开旧话题。
  - 失败识别：接收者**不同意/不 concede**，旧话题被继续，且伴随冲突或不结盟。作者还观察到，一次关闭失败后说话人可能换更强的关闭手段（例 4 里先用重复、失败后再用比喻总结，p. 502）。
  - 因此「自然结束」的可观测判据是**序列性的**：总结→最小不展开回应→新话题；以及反例中的「总结→不同意/继续展开」。
- **适用边界**：论文研究的是「比喻总结」这一类关闭技术；「沉默时长」在转写里只是 (.)/(0.2)/(0.3) 这样的微停顿，作者并未把它当作独立结束信号，也未给出可用于产品的秒数。

### 卡片 D：话语结构有三个可分离成分（linguistic / intentional / attentional）

- **来源**：Barbara J. Grosz & Candace L. Sidner, *Attention, intention, and the structure of discourse*, 1986。OpenAlex 工作 `W2492584954`；机构开放版登记为 University of Illinois IDEALS `hdl.handle.net/2142/17510`，PDF `items/17595/bitstreams/63256/data.pdf`（本轮下载被 403 阻断）。
- **可靠性**：B。官方书目与摘要已核对；**全文未取得**。
- **原文摘要（按 OpenAlex 倒排索引复原，逐词来自该记录）**：

  > "This paper presents the basic elements of a computational theory of discourse structure that simplifies and expands upon previous work. By specifying the units that comprises a discourse and the ways in which they can relate, an account provides a basis for meaning. The paper distinguishes three components of discourse structure: one linguistic, one intentional, one attentional. The theory, although still incomplete, is investigating meaning in discourse, as well as constructing 'discourse processing systems.'"

- **研究情境**：计算话语结构理论，面向话语加工系统；不是人类对话的时序实验。
- **可观察行为 / 对 Ling 的含义**：把「说了什么（linguistic）」「想达成什么（intentional）」「当前共同聚焦什么（attentional/focus）」分开建模，是**理论分工**层面的证据；它支持「话题焦点不是普通记忆表的一项」，但不支持任何固定 focus 容量或时长参数。
- **适用边界**：摘要自称理论 "still incomplete"；本轮未读全文，不能引用其 focus stack 的具体进入/弹出规则，也不能把其计算模型当成人类工作记忆模型。

### 卡片 E：话题转换的检测本身是困难且可标注的（工程对照）

- **来源**：Jiangyi Lin et al., *Topic Shift Detection in Chinese Dialogues: Corpus and Benchmark*, arXiv:2305.01195v1（2023）。官方摘要页已核对，全文未下载。
- **可靠性**：C（摘要已核对）。
- **原文摘要引文**：

  > "Dialogue topic shift detection is to detect whether an ongoing topic has shifted or should shift in a dialogue, which can be divided into two categories, i.e., response-known task and response-unknown task. ... it is still a challenge to predict the topic shift without the response information. ... we first annotate a Chinese Natural Topic Dialogue (CNTD) corpus consisting of 1308 dialogues ..."

- **研究情境**：中文自然对话标注语料；任务分「已知回应」和「未知回应」两类。
- **可观察行为**：论文把「话题已转移」与「应当转移」都作为待检测对象，并明确指出**不看回应就预测转移很难**。这对 Ling 的含义是：主动换题的判断若只看自己上一条和内部状态，信息不足；对对方回应的观察是关键输入。
- **适用边界**：这是计算标注任务，不是人类会话机制的直接证据；1308 是对话条数，不是「人能记住多少个话题」，不能转成 Ling 参数。

### 卡片 F：话题切分依赖多种线索，边界标注本身有歧义（工程对照）

- **来源**：Seungmin Lee et al., *Def-DTS: Deductive Reasoning for Open-domain Dialogue Topic Segmentation*, arXiv:2505.21033v1 / Findings of ACL 2025。官方摘要已核对。
- **可靠性**：C。
- **原文摘要引文**：

  > "Dialogue Topic Segmentation (DTS) aims to divide dialogues into coherent segments. DTS plays a crucial role in various NLP downstream tasks, but suffers from chronic problems: data shortage, labeling ambiguity, and incremental complexity of recently proposed solutions. Our method employs a structured prompting approach for bidirectional context summarization, utterance intent classification, and deductive topic shift detection."

- **可观察行为**：该方法把「双向上下文摘要 + 话语意图分类 + 话题转移检测」组合使用；摘要明确指出**标注歧义**是长期问题。
- **对 Ling 的含义**：换题/续题的判定在人类语料里也不是单一句子特征能定死的，产品上应保留「不确定」而非强判。
- **适用边界**：LLM 方法的有效性不等于人类机制；不提供固定阈值。

### 卡片 G：LLM 长上下文的「中部信息丢失」只能作对照

- **来源**：Nelson F. Liu et al., *Lost in the Middle: How Language Models Use Long Contexts*, arXiv:2307.03172v3 / TACL 2023。arXiv HTML 全文已核对。
- **可靠性**：C（全文已核对）。
- **原文引文（Introduction）**：

  > "we observe a distinctive U-shaped performance curve ... language model performance is highest when relevant information occurs at the very beginning (primacy bias) or end of its input context (recency bias), and performance significantly degrades when models must access and use information in the middle of its input context"

  > "the U-shaped curve we observe in this work has a connection in psychology known as the serial-position effect ... that states that in free-association recall of elements from a list, humans tend to best remember the first and last elements of the list."

- **研究情境**：多文档问答与键值检索，相关文档/键的位置被人为移动；模型包括 MPT、LongChat、GPT-3.5-Turbo、Claude 等。
- **可观察行为**：同等语义内容放在开头/结尾比放在中间更容易被模型使用；论文自己把它**类比**到人类 serial-position effect，但这是在讨论相关工作，不是用人类记忆实验证明 LLM 机制。
- **适用边界（用户要求重点）**：
  - 这证明的是**模型使用输入上下文的位置偏置**，不是 Ling 应采用的「人类旧话题沉下去」机制。
  - 不能把论文中的 10/20/30 篇文档、75/140/300 键值对、56.1%、~1.5% 等数值当成人类工作记忆容量或 Ling 的话题 TTL/预算。
  - 「人类也有首因/近因」是论文援引的心理学相关关系，不能反过来给 Ling 规定固定秒数或固定话题数。

### 卡片 H：Knowledge graph 可做「自然话题流」的生成骨架（弱对照）

- **来源**：Yerin Hwang et al., *MP2D: An Automated Topic Shift Dialogue Generation Framework Leveraging Knowledge Graphs*, arXiv:2403.05814v1（2024）。摘要已核对。
- **可靠性**：C。
- **原文摘要引文**：

  > "By leveraging the relationships between entities in a knowledge graph, MP2D maps the flow of topics within a dialogue, effectively mirroring the dynamics of human conversation. ... we demonstrate that even Large Language Models (LLMs) struggle to handle topic shifts in dialogue effectively"

- **可观察行为**：用实体关系把话题串成流，可生成「自然过渡」的对话；同时指出 LLM 本身处理话题转换仍吃力。
- **适用边界**：这是生成框架与 benchmark，不是人类社交规则的证据；「entity relation」不能等同于「用户兴趣」。

## 3. 面向 Ling 的行为规则假设（均需回放验证，注释里写的语义要有区分力）

以下都是**假设**，不是文献直接给出的产品参数。置信度标注按：高=有 A 级原文直接支持且边界清楚；中=有 A 级行为描述但需映射到文本陪伴场景；低=主要由工程对照或推理支撑。

1. **[中-高] 用「序列位置」而非「时间间隔」判断话题是否自然结束。**
   可观察条件：上一轮出现**总结/评价性收束** + 对方给出**话题适配但不展开**的最小回应 + 任一方开启不同下一步。满足则可把旧话题标为「已关闭」；缺任一环，尤其对方不同意或继续展开时，标为「未关闭/冲突中」，不要假装结束。
   依据：卡片 A、C。边界：Drew & Holt 的比喻总结只是其中一种技术；不要把「对方只回了一个嗯」单独当关闭。

2. **[中] 「回到旧话题」与「开启新话题」在状态上应可区分，但都允许被当作换题。**
   规则假设：若当前输入明显重指一个先前话题，且此前存在可识别的转换/关闭，则记为 `resume`；若重指不依赖先前话题、只是当前新内容，则记为 `new`。两者对「是否可主动接」可以走同一门槛，但对「是否算重复话题」不能混为一谈。
   依据：卡片 B 的注 9。边界：论文没有给「明显重指」的自动判据；工程上要有不确定态。

3. **[中] 主动接旧话题需要「可引用锚点」，不能靠模糊相似度。**
   规则假设：只有当旧话题在当前上下文中能被**具体引用**（复述其未竟处、上次结论、对方说过的词）时才恢复它；仅凭向量/词面相似命中而当前输入其实在别处，不恢复。
   依据：卡片 B 的「可重识别 + 再进入」推理；卡片 E 的「无回应难判」；置信度中，因为这是对 CA 观察的产品化映射。

4. **[中] 「重新带回来」与「情境自然重提」应分成不同触发，而不是同一个相似度阈值。**
   - `引用`：当前输入显式指回旧话题（如「你上次说的那件事」）→ 直接可接，且应让对方知道接的是哪件事。
   - `带回`：当前轮没有明说，但旧话题有**未完成的承诺/待办**或明确未答问题 → 只有在承诺仍有效、受众合适时才可主动带回。
   - `自然重提`：当前现实情境本身带来同一对象（如对方又提到同一个地点/人/日程）→ 当作当前话题处理，**不要包装成「我记得你以前说过」**。
   依据：卡片 A（新话题由当前一步构成）、卡片 B（回返也算换题）；「承诺/待办」部分文献未直接证明，置信度低-中。

5. **[中] 主动换题前先确认「当前话题是否已被对方接受为结束」。**
   若上一话题处于「未关闭/冲突中」，主动换题会被读成回避。可观察门槛：对方已有不展开回应，或对方自己已发起新话题；二者都没有时，宁可停在原话题或显式征询。
   依据：卡片 C 的成功/失败对照。边界：这是从成人电话谈话外推到文本陪伴，需回放验证。

6. **[低-中] 不要给「沉下去」设固定时长或固定条数。**
   LLM 的 Middle 丢失是**模型访问偏置**，不是人类旧话题的自然遗忘曲线；Sacks 类 TRP 与 Stivers 类轮次间隔在本轮未核实，更不能转成毫秒参数。若需要时效，只能来自 Ling 自己的真实会话回放或产品决策，并显式标为工程初值。
   依据：卡片 G 的适用边界；全局约定「参数唯一正当来源是真实会话回放」。

7. **[中] 把「不确定」作为一等状态。**
   话题切分论文明确指出标注歧义；中文话题转换论文指出无回应时预测困难。因此 Ling 的内部状态应允许 `topic_unknown`，并在不确定时不主动带旧话题、不宣称「我们聊完了」。
   依据：卡片 E、F。

## 4. 四种主动换题条件的比较

用户列出的四种条件：**相关性、自然结束、对方兴趣、承诺/待办**。下表把「文献支持程度」与「可观察信号」分开写；文献直接支持的是前两种的**序列结构**，后两种多为推理或产品语境，不要当成已证事实。

| 条件 | 文献支持 | 可观察信号（人类） | 失效/反例 | 对 Ling 的建议 |
|---|---|---|---|---|
| **相关性** | 弱-中：Drew & Holt 显示新话题必须与刚结束的话题「不同」且被双方定向；MP2D 用实体关系做自然过渡，但那是生成方法 | 当前输入与候选旧话题有可说明的共同对象，且说话人把它构造成「下一步」而不是继续展开旧话题 | 只有词面/向量相似，但当前输入实际在推进别的事；被当成「复读机」 | 相关性是**必要不充分**门；必须再过「当前是否已换题」 |
| **自然结束** | **强（A 级）**：总结→最小不展开同意→新话题；失败时是不结盟/继续旧话题 | 总结性收束、对方极简且不给新素材的回应、任一方开新话题；双方同时开新话题是强信号 | 总结后对方不同意或不 concede，旧话题继续（卡片 C p. 515/517） | 作为主动换题的**主门**；不要把沉默或字数当结束 |
| **对方兴趣** | 弱：本轮没有直接测量「兴趣」的人类 CA 证据；Drew & Holt 只观察到接收者**有机会展开而选择不展开** | 对方追问、补充细节、表达同意并继续展开 → 未结束；对方只做最小回应 → 可退出 | 「对方礼貌附和」可能是 disaffiliation 的包装，不一定是兴趣；论文明确把冲突/不结盟列为关闭失败原因 | 只能作为**负向证据**：不展开=可退出；不要把「极简同意」正向解释成「她感兴趣」 |
| **承诺/待办** | 弱：Drew & Holt p. 507 的例子里双方同时转向「她为什么打电话」，说明**当下相关性**可以成为新话题的理由；但「承诺」本身未被该文研究 | 明确的未完成约定、未答问题、对方或自己留下的下一步 | 旧承诺已撤回、过期、受众不对；把「候选意向」当「已确认承诺」 | 只在有结构化来源与有效期时带回；无来源时标不确定，不编造「你答应过」 |

综合判断（推理，置信度中）：**自然结束是最硬的门；相关性是必要的语义条件；对方兴趣主要用来否决换题；承诺/待办是允许的例外通道，但必须有可审计来源。**

## 5. 反例线索解释力边界

| 反例线索 | 能解释什么 | 不能解释什么 |
|---|---|---|
| 「她只是沉默/很久没回」 | 可能表示话题暂停；在通话转写中微停顿 (.)/(0.2) 确实出现在总结与新话题之间 | **不能**单独判定结束；Drew & Holt 的结束判据是「不展开的回应 + 新话题」，不是时长；更不能给秒数 |
| 「她回了个嗯/好」 | 若紧跟总结性收束且之后开新话题，是标准关闭序列的一环 | 单独出现时**不是**关闭信号；可能是继续中的 backchannel，也可能是礼貌附和；论文强调它 "does not offer any further resources to talk more about that topic"，要在序列里看 |
| 「又聊回上次那件事」 | 可以是「换了话题再回来」，按注 9 算新话题 | **不能**证明「她一直记着」或「旧话题从未下沉」；恰恰相反，论文说 "they change topic" |
| 「模型对中间信息答不出来」 | 说明该模型在该任务上有位置偏置（卡片 G） | **不能**推出人类旧话题因为「在上下文中间」而被遗忘；不能转成 Ling 的话题容量、TTL 或固定时长 |
| 「中文话题切分论文的 1308 对话 / 3.3% 提升」 | 说明中文自然对话可标注、该任务有标注歧义 | **不是**人类工作记忆容量，也不是产品 KPI；不能拿来当 Ling 参数 |
| 「Grosz & Sidner 有 attentional focus」 | 支持把「当前焦点」与「话语内容」分开建模 | 本轮未取得全文，**不能**引用其 focus stack 进出规则，不能把计算 focus 当人类记忆 |

## 6. 未取得 / 未核实证据

1. **Grosz & Sidner (1986) 全文未取得**：IDEALS PDF 返回 403，OpenAlex content endpoint 需 API key。只核对了 OpenAlex 书目与摘要。因此关于 focus stack、 attentional state 的具体规则一律未写。
2. **Grosz & Sidner (1990) *Plans for Discourse*（收录于 *Intentions in Communication*, MIT Press）全文未取得**：OpenAlex 记为 closed，无 OA 版本。
3. **Sacks, Schegloff & Jefferson (1974) 全文未取得**：OpenAlex 找到的俄译/重印 PDF 链接已 404；本轮不引用其 TRP 具体定义，本项目 `docs/ATTENTION-DESIGN.md` 里那条也标注为未核实。
4. **Schegloff, *Sequence Organization in Interaction* 全文未取得**：Cambridge 书章页面 404；OpenAlex 只找到一篇 2022 年书评（开放），未用于结论。
5. **Star & Griesemer boundary object 原文未取得**：只核实了相关书目/综述条目，未引用其定义；因此本文件没有把 boundary object 写进行为规则。
6. **Drew & Holt 的具体转写例（ex. 5、6 的回返）只用了注 9 的概括**，未逐条核对那两段回返的话轮；「回返靠什么具体词触发」未核实。
7. **中文自然会话的 CA 语料未取得**：卡片 E 只是计算标注任务，不等于中文人类的换题实践。
8. **「对方兴趣」「承诺/待办」两项的人类 CA 直接证据不足**：表 4 中它们是推理/产品语境，不是已证的人类机制。
9. **LLM 侧只核对了摘要/部分正文**：中文话题转换、DTS、DyCP 未下载全文；只作对照，不从中提参数。

## 7. 实际访问 URL 与失败项

### 7.1 实际访问成功（用于证据）

- `https://api.openalex.org/works?search=Discourse%20attention%20an%20explicit%20theory%20of%20focus%20in%20discourse&per-page=5`（发现 Grosz & Sidner 1986）
- `https://api.openalex.org/works/W2492584954?select=id,doi,title,publication_year,authorships,open_access,primary_location,abstract_inverted_index`（摘要原文）
- `https://eprints.whiterose.ac.uk/id/eprint/1545/`（Drew & Holt 摘要页、页码、下载链接）
- `https://eprints.whiterose.ac.uk/id/eprint/1545/1/drewp1.pdf`（**全文下载成功**，311,710 字节，29 页）
- `https://api.crossref.org/works/10.1017/s0047404500020200`（Drew & Holt 出版元数据、摘要、页码）
- `https://arxiv.org/abs/2305.01195v1`（中文话题转换摘要）
- `https://arxiv.org/abs/2403.05814v1`（MP2D 摘要）
- `https://arxiv.org/abs/2505.21033v1`（Def-DTS 摘要）
- `https://arxiv.org/abs/2601.07994v5`（DyCP 摘要）
- `https://arxiv.org/abs/2307.03172v3`（Lost in the Middle 摘要页）
- `https://arxiv.org/html/2307.03172v3`（Lost in the Middle 全文，逐字核对 Introduction/§6）
- `https://api.openalex.org/works?search=Plans%20for%20discourse%20Grosz%20Sidner&per-page=5&select=...`（1990 书的 closed 状态）
- `https://api.openalex.org/works?search=On%20the%20structure%20of%20topic%20organization%20in%20conversation%20Schegloff&per-page=5&select=...`（发现 Drew & Holt 与 Sacks 重印）
- `https://api.openalex.org/works?search=boundary%20object%20Star%20Griesemer&per-page=5&select=...`（boundary object 书目状态）
- `https://api.openalex.org/works?search=adjacency%20pair%20conversation%20analysis%20Schegloff%20Sacks&per-page=6&select=...`（adjacency pair 书目状态）
- `https://export.arxiv.org/api/query?search_query=all:%22topic%20shift%22%20AND%20all:%22dialogue%22&start=0&max_results=8`（话题转换论文发现）
- `https://export.arxiv.org/api/query?search_query=ti:%22Lost%20in%20the%20Middle%22&start=0&max_results=3`（LitM 论文发现）

### 7.2 访问失败 / 未用于证据

- `https://onlinelibrary.wiley.com/doi/pdf/10.1002/9780470754603.ch4`：HTTP 403（Cloudflare）
- `https://www.sciencedirect.com/science/article/pii/S0022278X09800138`：HTTP 403
- `https://www.ideals.illinois.edu/items/17595/bitstreams/63256/data.pdf`：web_fetch 传输失败；curl 带浏览器 UA 仍 403
- `https://content.openalex.org/works/W2492584954.grobid-xml`：HTTP 401，需 API key
- `https://sociologica.hse.ru/data/2015/03/31/1095843245/SocOboz_14_1_06_Sacks.pdf`：HTTP 404
- `https://people.csail.mit.edu/jgrossz/papers/grosz-sidner-86.pdf`：HTTP 403
- `https://www.cs.cornell.edu/~diana/publications/GroszSidner.pdf`：HTTP 404
- `https://www.cambridge.org/core/.../figures-of-speech...`：HTTP 404
- `https://www.cambridge.org/core/books/primer-in-conversation-analysis/sequence-organization-in-interaction`：HTTP 404
- `https://www.google.com/search?q=...`、`https://html.duckduckgo.com/html/?q=...`、`https://cn.bing.com/search?q=...`：fetch 失败或跨域重定向，未用
- `https://aclanthology.org/P86-1023/`、`https://aclanthology.org/P88-1035/`、`https://aclanthology.org/J86-1001/`、`https://aclanthology.org/P90-1025/`、`https://aclanthology.org/N18-1121/`：均为**猜错 ID 后误命中的其他论文**，已丢弃，不作为证据
