# 线下同步对话 vs 在线 IM：哪些机制能搬进 Ling 的文本上下文窗口

> 基线：HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`，`main`；开始时工作树只有一处非本任务造成的未跟踪目录 `docs/research/`。本文只读研究，不改业务代码，不连生产库或 NAS。
> 本任务是**证据与边界研究**，不是行为验收。文中所有对话均为合成；不虚构页码、不把转述冒充原文、不把人的工作记忆/固定时长当 Ling 参数。
> 置信度标记：**[原文]**＝本轮实际打开并读到正文/摘要；**[元数据]**＝只从 Crossref/OpenAlex 等登记信息读到题录或摘要，未读全文；**[转述未核实]**＝理论主张来自通行转述或本仓既有笔记，页码与细节未逐页核对；**[推理]**＝基于前三类做的机制推断；**[本地代码]**＝只读查看仓库代码得到的事实，不是人类行为数据。

## 1. 对照框架

### 1.1 三种媒介，不是两种

| 维度 | 面对面同步对话 | 同步文本聊天（参照） | QQ 异步文本 IM |
| --- | --- | --- | --- |
| 轮的组织 | 语音 TCU 在 TRP 处交接，可重叠、可打断 **[转述未核实]** | 打字并发本身可成为临场感来源 **[原文]** | 每次按下发送即一条不可撤销消息；没有真正的 overlap **[推理]** |
| 修复时机 | 话还没说完就能自我修复；他人可在第三位置发起修复 **[转述未核实]** | 可边打边改，但发出后同样固化 | 发出后只能追加更正、引用指出、@ 点名或撤回；撤回受真实回执与时间窗约束 **[本地代码]** |
| 共在证据 | 视线、朝向、手势、共同注意对象、停顿、语气 | 打字中、实时文本流 | 时间戳、`@`、引用、已读、输入中；都是**弱近似**，不是在场等价物 **[原文+推理]** |
| 历史的可及性 | 说过的话随场景共在，靠「回到同一场景」找回 | 可滚动，但通常仍在同一次会话 | 可滚动、可引用、可跨天找回；同时也会被动留在窗口里 **[推理+本地代码]** |
| 默认语境 | 「现在共同看着/听着的东西」 | 当前会话流 | 最近若干条气泡；更早内容需要锚点才合法进入 **[推理]** |

同步文本聊天这一列不能省：它正好证明「打字中」不是万能替代品。Kojima 等用 TypeTrace 记录双方按键并回放打字过程，发现**只增加信息丰富度（呈现打字过程）并不显著提升社会临场感**；引入实时文本传输使双方信息并发度提高后，转移熵、社会临场感、情绪唤起与亲密感才上升；电话数据集也出现高转移熵。也就是说，输入中是「丰富度」线索，共在更依赖「并发」 **[原文]**。

### 1.2 判定问题的顺序

把线下机制往文本搬时，按以下顺序问，而不是见到一个线索就映射一个：

1. **这个信号在文本里真的存在吗？** 线下目光、指向、停顿没有原生文本等价物；「输入中」只是按钮状态，不携带目光对象。
2. **它能区分两种解释吗？** 若同一信号在「续旧话题」与「开新话题」两种情形下取值相同，它就没有区分力，不能当判据（项目约定中的区分力规则）。
3. **它是显式锚点还是弱先验？** 引用、`@`、带时间和话题锚点的找回词是显式锚点；时间邻接、在线状态、输入中只是弱先验。显式锚点优先于时间位置，时间位置优先于在线状态 **[推理，与既有研究笔记一致]**。
4. **它进入的是哪一层上下文？** 常驻、随当前话题带出、显式找回，三者的正确性条件不同。

### 1.3 三层上下文的媒介差异

| 层 | 线下对应物 | QQ 文本对应物 | Ling 的可观察位置 **[本地代码]** |
| --- | --- | --- | --- |
| 常驻 | 同一场景里始终可见的人、身份、共同目标、明确约定 | 置顶、群公告、稳定关系事实 | `core_memory`、部分 `knowledge`、`persona`、`constraints` 等按车道编译；核心记忆每轮常驻入口存在，但不是所有车道都无条件出现 |
| 随当前话题带出 | 当前共同注意对象、刚说过的话 | 最近气泡、当前窗口、话题词 | `history` 最近窗口、`short_memory`（`topic` / `open_questions`，带 TTL）、`summary` 块（按既有笔记为常驻且不做话题过滤，需要警惕） |
| 只在明确需要时找回 | 回头看同一张纸、问「刚才那个」 | 滚动、引用回复、`@`、搜聊天记录、「前天那个方案」 | `MemoryService.recall(query=current_text)`、引用真 id `[quote:…]`、`needs_recent_topic` 把上一句拼进 query、`should_skip_automatic_recall` 对寒暄整句跳过自动召回 |

关键差异：线下靠**场景连续性**默认续接，IM 没有场景，只能靠**显式锚点 + 时间位置 + 引用结构**重建；没有锚点时，默认应是「新开场」而不是「续旧话」 **[推理]**。Ling 里「摘要常驻」会把旧事件持续以文本形式摆在窗口里，因此在没有强边界的情况下，旧话题在注意力上并未真正退出——这是同批研究笔记提出的解释，尚须回放验证 **[转述+推理]**。

## 2. 来源与证据卡片

### 卡片 E1 · 会话轮次与修复的经典框架（理论来源）

- **出处**：Sacks, Schegloff & Jefferson (1974), *A simplest systematics for the organization of turn-taking for conversation*, Language 50(4): 696–735，<https://doi.org/10.1353/lan.1974.0010>；Schegloff, Jefferson & Sacks (1977), *The preference for self-correction in the organization of repair in conversation*, Language 53(2): 361–382，<https://doi.org/10.1353/lan.1977.0002>。本轮**未逐页核对原文**，页码照录自本仓笔记并标为未核实。
- **情境 [转述未核实]**：北美英语自然会话录音，熟人与家人朋友同事，面对面与电话；分析单位是 TCU、TRP、相邻对、插入序列、修复序列。
- **可观察行为 [转述未核实]**：一个 TCU 在 TRP 处提供交接机会；修复按「谁发起 / 谁执行」分为自我发起自我修复、他人发起自我修复等；偏好自我修复是**结构偏好**，不是心理倾向。
- **适用边界**：1974 系统靠语调、重音、停顿判定 TCU 与 TRP，纯文本没有这些；IM 每条发送都是一个不可撤销 TCU，不存在真正的重叠。把「系统偏好」直接当机器人的社会义务是范畴错误 **[推理]**。Ling 能借鉴的是**标注结构**（相邻对是否闭合、修复发起者与目标），不是把「？」当「Huh?」的无条件等价。

### 卡片 E2 · 线上聊天与首次见面的会话质量（原文摘要）

- **出处 [原文]**：Lipinski-Harten & Tafarodi (2012), *A Comparison of Conversational Quality in Online and Face-to-Face First Encounters*, Journal of Language and Social Psychology，<https://doi.org/10.1177/0261927x12446601>；摘要经 Crossref API 读取，<https://api.crossref.org/works?query.title=conversational%20repair%20face-to-face%20online&rows=5&select=DOI,title,author,published,container-title,URL,abstract>。
- **情境 [原文]**：学生配对进行 30 分钟初次会面，条件为面对面或在线聊天。
- **可观察行为 [原文摘要]**：面对面中出现的性别差异在线上消失；线上聊天整体产生**更弱的序列连续性（less sequential connectivity）、更强的自我聚焦、更弱的他人聚焦**。
- **适用边界**：对象是**同步在线聊天**且是陌生人初识任务，不是 QQ 异步私聊；不能据此断言「所有线上文本都连续性更弱」，只能说明媒介变化与序列组织变化相关。对 Ling 的含义是：不要假设「文本里相邻两条天然构成相邻对」。

### 卡片 E3 · 打字中 ≠ 共在，并发才是关键（原文摘要）

- **出处 [原文]**：Kojima, Chen, Oka & Ikegami (2021), *Analysis and Design of Social Presence in a Computer-Mediated Communication System*, Frontiers in Psychology，<https://doi.org/10.3389/fpsyg.2021.641927>；摘要经 Crossref API 读取，URL 同 E2 的 Crossref 查询（关键词 `Computer-mediated communication social presence`）。
- **情境 [原文]**：自研 TypeTrace 记录在线聊天参与者的全部按键并回放打字动作；比较不同聊天条件与电话数据集。
- **可观察行为 [原文摘要]**：仅呈现打字过程（提高信息丰富度）不显著提升他人临场感；引入实时文本传输（提高信息并发度）后，双方转移熵明显升高，社会临场感、情绪唤起与亲密感上升；电话数据集也观察到高转移熵。
- **适用边界**：同步双人聊天与电话，非异步 IM；「输入中」在 QQ 里不是持续文本流。对 Ling 的含义：**不要把「输入中」当作对方在场且正在接上一句的信号**；它只是弱状态提示，权重应低于显式引用与找回词。

### 卡片 E4 · 视频中介里「他人关照」需要额外做出来（原文摘要）

- **出处 [原文]**：Stommel, van Goor & Stommel (2019), *Other-Attentiveness in Video Consultation Openings: A Conversation Analysis of Video-Mediated Versus Face-to-Face Consultations*, Journal of Computer-Mediated Communication，<https://doi.org/10.1093/jcmc/zmz015>；摘要经 Crossref API 读取。
- **情境 [原文]**：39 段术后肿瘤复诊记录，面对面与视频中介各若干，会话分析。
- **可观察行为 [原文摘要]**：医生通常以报告病理结果开场；在视频复诊中，技术测试之后**常规插入「how are you?」**，用于展示外显的他人关照；但随后医生又可能单方面结束这个自述、转回病理报告，使这份额外工作被削弱。
- **适用边界**：医疗复诊、视频中介、机构性谈话，不是朋友闲聊或文本 IM。对 Ling 的含义：媒介削弱身体共在后，关系性动作往往需要**显式做出来**；但做出来后是否真正接续，还要看后续是否让它影响话题，而不是只说一句「在听」就转回默认上下文。

### 卡片 E5 · 视觉线索缺失会改变群体过程（原文摘要）

- **出处 [原文]**：Sia, Tan & Wei (2002), *Group Polarization and Computer-Mediated Communication: Effects of Communication Cues, Social Presence, and Anonymity*, Information Systems Research，<https://doi.org/10.1287/isre.13.1.70.92>；摘要经 Crossref API 读取。
- **情境 [原文]**：两个实验室实验，比较有无言语线索、视觉线索、匿名条件下的群体讨论。
- **可观察行为 [原文摘要]**：去除言语线索未必足以降低社会临场感；去除视觉线索则可能降低社会临场感并提高群体极化；匿名也会降低社会临场感并提高极化。过程数据表明，社会临场感降低可能导致更多新颖论点与竞相表现（one-upmanship）。
- **适用边界**：群体讨论与极化，不是一对一私聊；不能直接外推到 Ling 的回复风格。对 Ling 的含义仅是：视觉/身份线索的减少**会改变**互动组织，而不是只少一点「温度」。

### 卡片 E6 · 状态反馈的可访问性规范（原文）

- **出处 [原文]**：W3C, *Web Content Accessibility Guidelines (WCAG) 2.2*, SC 4.1.3 Status Messages，<https://www.w3.org/TR/WCAG22/#status-messages>；Understanding SC 4.1.3，<https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html>。
- **情境 [原文]**：辅助技术与网页状态变化的规范；不是人类面对面对话研究。
- **可观察行为 [原文]**：状态消息是「不改变上下文、但告知动作结果、等待状态、进度或错误」的内容变化；应可通过角色或属性被程序判定并由辅助技术呈现，且**不不必要地打断用户**。规范明确区分「状态消息」与「改变焦点/上下文的中断」。
- **适用边界**：这是**界面规范**，不是认知科学数据；不能拿来证明用户在 QQ 里如何解读「已读」。对 Ling 的含义：「输入中 / 已读」作为状态显示应与正文分离，不能冒充"对方已接受上一句"的语义事实。

### 卡片 E7 · 人对响应时间的三档阈值（原文，限定使用）

- **出处 [原文]**：Jakob Nielsen, *Response Times: The 3 Important Limits*（1993，网页版含 2014 补注），<https://www.nngroup.com/articles/response-times-3-important-limits/>；同站十原则中的 `Visibility of system status` 条，<https://www.nngroup.com/articles/ten-usability-heuristics/>。
- **情境 [原文]**：人与界面交互的感知阈值，来源为 Miller 1968、Card 等 1991 的人因文献。
- **可观察行为 [原文]**：0.1 秒近似「即时响应」上限；1 秒近似「思路不被打断」上限；10 秒近似「注意力仍集中在对话上」上限，超过则需要进度反馈与可中断方式。
- **适用边界**：**这是人对界面反馈的感知阈值，不是模型推理时延、token 预算、窗口大小或记忆容量参数。** 也不能把 0.1/1/10 秒直接写成 Ling 的 SLA。它只能说明「状态变化需要被看见」这一原则。

### 卡片 E8 · 撤回的语义不是"删除"：本地代码事实（非行为来源）

- **出处 [本地代码，只读]**：`src/ling/kernel/context.py` 中引用 id 与撤回 id 的说明；`docs/RECALL-TOOL.md`、`docs/BEHAVIOR-LOG-CONTRACT.md`。
- **情境 [本地代码]**：线上曾出现模型编造 `[quote:3]` 被平台静默丢弃；撤回需区分平台消息 id 与本地短号，并要求真实送达回执、时间窗、频次、对象可撤性等闸门；只有 `recalled_at` 非空才能说"撤回了"。
- **可观察行为 [本地代码]**：撤回消息在上下文里保留原文并加注记，而不是删掉；引用目标对不上真 id 时降级为普通消息并留痕。
- **适用边界**：这是**实现事实**，不能证明人类如何解读撤回。对 Ling 的含义：文本里的「我收回刚才那句」不等于平台真撤回了；应把两类事实分开呈现。

### 卡片 E9 · 工作记忆容量：只用于否定，不用于赋值（元数据/推理）

- **出处 [元数据]**：Miller 1956 *The magical number seven, plus or minus two* 的经典主张；Crossref 可检索到后续评论文献如 *Magical numbers: the seven-plus-or-minus-two myth*（<https://doi.org/10.1109/tpc.2002.1003695>）与 *The magical "wave" seven, plus or minus two?*（<https://doi.org/10.1121/1.423296>，摘要指出该数字更接近连续区间而非固定值）。本轮未取得 Miller 原文，标为元数据/转述。
- **情境**：人类短时记忆容量的实验传统，后续研究对其固定性与解释持续修正。
- **适用边界**：**明确不把 7±2、或任何人类工作记忆/固定时长，映射为 Ling 的历史条数、车道预算、摘要长度或 TTL。** Ling 的预算来自工程约束与配置，不来自人的认知常数。若要给窗口大小设值，依据应是任务需求、测试证据与配置，并写成可配置量，而不是"人只能记 7 件事"。

## 3. 合成对照样本

记号：`A`＝用户（假 QQ `100200300`），`B`＝对方（假 QQ `100200301`）。所有对话合成；时间用相对时间，不用真实数据。每个样本先给最小对话，再给解读差异与对 Ling 的判定要点。

### S1 · 指代：「这个」靠什么落地

**线下版本**
> 两人同时看一张打印稿。A 指着第二行：**「这个要改。」**
> B：**「哪个？」**
> A（手指没动）：**「就这个。」**

解读：手指与共同注意让「这个」有唯一指代对象；B 的「哪个」是请求确认指向，不是质疑 A 的意图 **【线下共在线索】**。

**QQ 文本版本**
> A：`这个要改`
> B：`哪个？`
> A：`就这个`

解读差异：最后一句没有图片、引用或共享锚点，「这个」在文本里**指代落空**；读者只能猜最近提到的对象，而这次最近提到的是「哪个」这个问题本身。判定要点：只有出现引用、图片或明确名词时，「这个」才可解；否则应请求澄清 **[推理]**。 Ling 侧对应的不是"猜得更聪明"，而是提示词快照里是否存在可引用的真实消息 id 与可见锚点 **[本地代码]**。

### S2 · 打断 vs 追加：换题信号的消失

**线下版本**
> A：`那个方案我列了四项……`
> B（插话、转向）：`先别说那个，看这个。`
> A：`好，看这个。`

解读：插话与身体转向把旧话题**当场挂起**；A 的回应确认了新焦点 **【即时打断】**。

**QQ 文本版本**
> A：`那个方案我列了四项……`
> B：`先别说那个，看这个。`

解读差异：B 的消息只是排在时间线末尾的**新一条**，不是对 A 话语物理进程的打断；A 那条仍完整留在窗口里。判定要点：`先别说那个 / 不聊这个了 / 换一个` 应作为显式换题指令，抑制对旧话题的续接；反例是把「先别说那个」换成「那个不错」，若同样抑制续接，说明测的是词而不是换题意图 **[推理，与 M4 笔记一致]**。线下「沉默几秒」不等于 IM「隔天断档」：前者同场不断档，后者默认新开场。

### S3 · 即时修复 vs 延迟更正

**线下版本**
> A：`我们周三——不，周四下午见。`
> B：`好，周四。`

解读：A 在话没说完时自我修复，B 听到的最终提案只有「周四下午」；「周三」没有进入交付内容 **【自我发起自我修复】**。

**QQ 文本版本**
> A（T）：`我们周三下午见`
> A（T+40s）：`打错了，周四`
> B（T+3min）：`好，那周三还是周四？`

解读差异：第一条已经作为独立消息存在，第二条是**追加更正**而不是语音流内的覆盖。判定要点：模型需要知道"最后一条文本是更正，覆盖前一条的提案"，但**不能**把这条规则泛化为"后一条永远覆盖前一条"——那会破坏「我先说 A，再补充 B」的正常累积 **[推理]**。Ling 里撤回也不是"假装没说过"：保留原文并加注记，且只有拿到真实回执才说已撤回 **[本地代码]**。这就是延迟修复与即时修复最本质的差别：文本可以追加，但历史不会自动重写。

### S4 · 共在替代：输入中 / 已读 / @ / 引用

**线下版本**
> A 看着 B：`在吗？`
> B（抬头、点头，但没说话）：……
> A：`那我先说结论：方案过了。`

解读：目光与点头确认「B 在场且听到」，A 才继续；停顿表示等待回应，不是话题结束 **【视觉共在】**。

**QQ 文本版本**
> A：`在吗？`
> 系统：`对方正在输入…`（3 秒后消失）
> A：`那我先说结论：方案过了。`
> B：已读，无回复

解读差异：「正在输入」只说明对方有按键动作，不说明他在看 A 的「在吗」；「已读」只说明消息被查看，不说明同意或承接。Kojima 等的实验更严格地显示，仅呈现打字过程并不显著增加临场感 **[原文 E3]**。判定要点：这四种状态都只能当弱先验；真正改变语境的是 `@某人`、引用具体消息或带锚点的找回词 **[推理]**。

### S5 · 找回：隔天开场 vs 显式锚点

**线下版本**
> 第二天，两人回到同一张桌子前。A：`接着昨天那个方案说。`
> B：`好，第二项。`

解读：物理场景 + 「昨天那个方案」让旧话题合法恢复；不需要复述昨天全部内容 **【场景连续 + 显式回指】**。

**QQ 文本版本（a）无锚点**
> 次日 09:12，A：`早上好`

解读：默认新开场，不接昨天的方案；「早上好」是问候 FPP，不是续旧标记 **[转述未核实+推理]**。

**QQ 文本版本（b）有锚点**
> 次日 09:12，A：`接着前天那个方案说，第二项`

解读：时间锚点（前天）+ 话题锚点（方案）+ 承接词（接着说）构成显式找回，旧话题合法进入当前语境；若只有「接着说」而无锚点，不足以判为找回 **[推理]**。判定要点：两种 (a)/(b) 在「隔天」这一维度取值相同，所以时间本身没有区分力，必须看锚点。Ling 侧：`needs_recent_topic` 只处理「后来呢/然后呢」这类**接上一句**，不处理时间锚点；摘要块常驻且无话题过滤，可能让 (a) 也带出旧话题——这是需要回放验证的风险，不是已测事实 **[本地代码+推理]**。

### S6 · 群聊的引用与 @：指向谁、接哪句

**线下版本**
> 三人围桌。C 对 B 说：`你说那个截止日期，是周五吗？`
> B（看向 C）：`周五。`

解读：称呼 + 目光让接收者与所指对象都明确；A 虽然在场但不是被问对象 **【共同注意】**。

**QQ 文本版本**
> C：`@B 你说的截止日期是周五吗？`
> B：`[quote: C 的上面那条] 周五。`

解读差异：群聊里 `@` 指定"对谁说"，引用指定"接哪句"；两者指向不同维度。判定要点：`@` 不能替代引用，`引用` 也不能替代 `@`；引用必须是**真实存在且本轮可见的消息 id**，编造的 id 会被平台静默丢弃、正文照发 **[本地代码 E8]**。判定两类错误：只 @ 不引用时接错句，只引用不 @ 时群聊里不知道对谁说；两者同时成立才证明按渠道区分 **[项目负向断言纪律]**。

## 4. 对 Ling 的可迁移 / 不可迁移结论

### 4.1 可迁移（作为信号与判据，不作为人类常数）

1. **相邻对闭合状态、修复发起者与目标**：可作为提示词快照的标注维度——上一问是否已答、「？」紧邻哪条消息、更正是否来自原说话人。来源是会话分析框架 **[转述未核实]**，不是 QQ 实测。
2. **显式锚点优先**：引用、`@`、时间+话题找回词是强信号；时间邻接、在线状态、输入中是弱先验。样本 S2/S4/S5/S6 都指向同一排序 **[推理]**。
3. **状态与正文分离**：`输入中/已读` 属于状态变化，应像 WCAG 的状态消息那样可感知但不夺取焦点，不冒充语义承接 **[原文 E6 + 推理]**。
4. **关系性动作要显式做出来**：视觉共在被削弱后，「我在」「我看到了」需要文本化，但说完之后是否真正影响接续，要看后续选择 **[原文 E4 + 推理]**。
5. **撤回是注记不是删除**：保留原文 + 注记，且只有真实回执才能说"已撤回"；这与线下"话已说出不可收回"相容 **[本地代码 E8]**。

### 4.2 不可迁移（明确拒绝的映射）

1. **不把 TCU/TRP、停顿、语调直接映射到文本**：纯文本没有韵律，IM 以发送键定轮；「？」不是「Huh?」的无条件等价 **[转述未核实+推理]**。
2. **不把「输入中」当有在场**：TypeTrace 的证据表明丰富度不足以提升临场感，并发才关键；QQ 的输入中既非持续文本流，也非目光 **[原文 E3]**。
3. **不把人类工作记忆/固定时长当 Ling 参数**：7±2、0.1/1/10 秒、30 分钟实验时长都不是窗口大小、预算或 TTL 的依据 **[原文 E7 + 元数据 E9]**。Nielsen 的阈值只说明"反馈要可见"，Miller 的数字只用于说明"不要这么赋值"。
4. **不把线下场景连续性当文本默认**：没有锚点的隔天文本默认新开场；摘要常驻带来的旧话题被动带出是风险，不是"像人一样记得" **[推理+本地代码]**。
5. **不把群体 CMC 结论外推到私聊**：E5 的极化效应来自群体讨论；只能说明线索减少会改变互动组织，不能推出 Ling 的风格应更谨慎或更激进。
6. **不把人类会话参与的义务强加给系统**：Ling 回问候是策略，不是社会义务；模型"重新生成"不是人的自我修复 **[推理]**。

### 4.3 建议的验证形态（不是本次结论）

- 同一合成输入跑两次，只改一个锚点（有/无引用、有/无时间+话题词），看提示词快照是否把旧话题纳入；
- 对每种弱先验（输入中、已读）配一个"结论相反"的强锚点，验证强锚点确实压过弱先验；
- 对每一条"降级"行为（引用对不上、撤回失败）检查是否留有日志，而不是只看正文是否流畅；
- 变异验证：删掉锚点解析/召回接线后，相关判据必须变红；全绿而无变异，不足以证明测到了东西。

## 5. 未取得数据

- 本轮**没有**真实 QQ 聊天记录、真实用户、真实模型回放或提示词快照。所有样本合成，所有"解读差异"是机制推理，不是行为实测。
- 本轮没有取得 Miller 1956、Sacks 1974、Schegloff 1977、Clark & Brennan 的全文；相关页码/主张均按既有笔记转述，逐条标为未核实，未虚构页码。
- E2/E3/E4/E5 只读到 Crossref 登记的摘要，未读全文；摘要可能省略条件与效应量。
- 未取得 QQ 官方对"输入中/已读"呈现语义的产品文档；腾讯客服入口只作弱证据，不用于本文论证。
- 未验证摘要常驻导致旧话题带出、引用目标展开失败等具体缺陷是否在当前版本发生；这些是同批研究笔记的代码路径观察 + 推理，须用合法快照/回放确认。
- 未进行任何变异实验或回归测试；本文不提供通过/不通过判定。

---

## 实际 URL

| 用途 | URL | 结果 |
| --- | --- | --- |
| Crossref 查询：线上/线面对话与修复 | <https://api.crossref.org/works?query.title=conversational%20repair%20face-to-face%20online&rows=5&select=DOI,title,author,published,container-title,URL,abstract> | 200，取得 E2 摘要 |
| Crossref 查询：CMC 社会临场感 | <https://api.crossref.org/works?query.title=Computer-mediated%20communication%20social%20presence&rows=5&select=DOI,title,author,published,container-title,URL,abstract> | 200，取得 E3、E5 摘要 |
| Crossref 查询：视频会诊开场 | <https://api.crossref.org/works?query.title=Anywhere%20and%20everywhere%20The%20use%20of%20gesture%20and%20pointing%20to%20coordinate%20attention%20in%20face-to-face%20and%20video-mediated%20conversation&rows=3&select=DOI,title,author,published,container-title,URL,abstract> | 200，取得 E4 摘要（标题与查询不同，按返回题录用） |
| Crossref 查询：社会线索缺失与极化 | <https://api.crossref.org/works?query.title=Computer-mediated%20communication%20lack%20of%20social%20context%20cues&rows=5&select=DOI,title,author,published,container-title,URL,abstract> | 200，复核 E5 摘要 |
| Crossref 查询：工作记忆数字批判 | <https://api.crossref.org/works?query.title=The%20magical%20number%20seven%20plus%20or%20minus%20two&rows=3&select=DOI,title,author,published,container-title,URL,abstract> | 200，取得 E9 元数据 |
| Nielsen 响应时间阈值 | <https://www.nngroup.com/articles/response-times-3-important-limits/> | 200，取得原文 |
| Nielsen 十原则 | <https://www.nngroup.com/articles/ten-usability-heuristics/> | 200，取得原文 |
| WCAG 2.2 规范正文 | <https://www.w3.org/TR/WCAG22/#status-messages> | 200，取得规范 |
| WCAG 4.1.3 Understanding | <https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html> | 200，取得原文 |
| Lipinski-Harten & Tafarodi DOI | <https://doi.org/10.1177/0261927x12446601> | 未单独打开；经 Crossref 摘要引用 |
| Kojima 等 DOI | <https://doi.org/10.3389/fpsyg.2021.641927> | 未单独打开；经 Crossref 摘要引用 |
| Stommel 等 DOI | <https://doi.org/10.1093/jcmc/zmz015> | 未单独打开；经 Crossref 摘要引用 |
| Sia 等 DOI | <https://doi.org/10.1287/isre.13.1.70.92> | 未单独打开；经 Crossref 摘要引用 |
| Sacks 等 DOI | <https://doi.org/10.1353/lan.1974.0010> | 未取得原文；转述未核实 |
| Schegloff 等 DOI | <https://doi.org/10.1353/lan.1977.0002> | 未取得原文；转述未核实 |
| Doumont 2002 DOI | <https://doi.org/10.1109/tpc.2002.1003695> | 仅题录；未取原文 |
| Wong & Mori 1998 DOI | <https://doi.org/10.1121/1.423296> | 仅题录与摘要；未取原文 |

## 失败项（如实记录）

- <https://www.cambridge.org/core/journals/language-in-context/article/conversational-repair-in-face-to-face-and-online-interaction/E3D42B0C2EC2F4B1EAF45BD5F7E7B98F>：HTTP 500 / 仅返回登录壳；未取得正文。
- <https://dl.acm.org/doi/10.1145/2493384>：HTTP 403（Cloudflare "Just a moment..."）。后经 OpenAlex/Semantic Scholar 核实该 DOI 实为芯片加速器论文，**与本研究无关，已弃用**；这正好说明不能按 DOI 猜测主题。
- <https://academic.oup.com/joplin/article/39/2/259/2884643>：HTTP 403；未取得正文。
- <https://journals.sagepub.com/doi/10.1177/0956797613497023>：fetch failed；未取得正文。
- <https://plato.stanford.edu/entries/conversation/>：HTTP 404（SEP 无此条目）；未取得正文。
- <https://www.researchgate.net/publication/313396597_Computer-Mediated_Communication>：HTTP 403（安全检查）；未取得正文。
- <https://en.wikipedia.org/wiki/Computer-mediated_communication>、<https://en.wikipedia.org/api/rest_v1/page/summary/Computer-mediated_communication>：fetch failed；未取得正文。
- <https://www.psychologyoflanguage.com/wp-content/uploads/2021/03/Clark_1996_Using_Language.pdf>：fetch failed；未取得 Clark 原文。
- <https://www.eric.ed.gov/ERICWebPortal/recordDetail?accno=EJ541348>、<https://labs.google.com/research/pubs/>：fetch failed；未取得内容。
- <https://pmc.ncbi.nlm.nih.gov/articles/PMC3764524/>：HTTP 200 但内容为伊朗蜡蝉生物地理学论文，**误命中，已弃用**。
- <https://api.crossref.org/works/10.1037/0033-295X.100.2.341>：404；<https://api.crossref.org/works/10.1037/1089-2680.2.2.139> 等若干 Crossref 查询：`unsupported content type "unknown"`；未取得对应原文。
- <https://api.openalex.org/works/https://doi.org/10.1145/2493384> 与 Semantic Scholar 同 DOI：200，但核实为无关论文，**未作为证据使用**。
