# 共同背景、共同理解与指代：人因证据与对 Ling 的规则建议

子智能体：human-grounding　基线：HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`，main，开始时 `git status --short` 空。本文件为唯一写入，未改业务代码，未 git add/commit。

## 1. 取证方法与可靠性

优先级：①可直接下载的原文 PDF（作者自存档 / 机构仓储 / 会议论文集）；②DOI 元数据（Crossref / OpenAlex / Semantic Scholar）只用于核对书目，不用于代替正文；③二手与未取得项明确标注。

本次实际拿到并抽取正文的原文两份：

- Clark & Brennan, *Grounding in Communication*（1991），作者旧站自存档 PDF，12 页，内容为该章的删节版。书目元数据经 Crossref 核实：DOI `10.1037/10096-006`，载于 *Perspectives on socially shared cognition*，pp. 127–149。存档版印刷页码被重排（PDF 第 5 页标「134」、第 6 页标「137」、第 7 页标「138」），与原章页码不一一对应，**故本文只标 PDF 页，不虚构原章页码**。
- Grosz & Sidner, *Attention, Intention, and the Structure of Discourse*（1986），Illinois IDEALS 技术报告 No. 380 原文 PDF，56 页。正文页码以「Structure of Discourse – N」标注，PDF 页与报告页可对应（如 PDF 29 = 报告 48，PDF 30 = 报告 50，PDF 22 = 报告 34）。

另取得一份可核验的实证二手：Sarkar et al., *Understanding Common Ground Misalignment in Goal-Oriented Dialog*，ACL 2025，pp. 3200–3215，DOI `10.18653/v1/2025.acl-long.161`（ACL Anthology PDF 原文）。它本身是二手研究，但其对「摩擦」的观察数据来自真实 Ubuntu IRC 对话，可作为「共同背景失配如何被观察」的可核验证据。

未取得原文的条目（Clark 的 joint action、joint commitment、three floors）只核实了书目元数据，正文主张一律标注「未核实」。Wikipedia、DuckDuckGo、Google、ScienceDirect、Cambridge Core 页面在本次环境中超时或 403/404，未作为证据来源。

可靠性分档：下文证据卡片的「出处/情境/行为」来自上述已抽取原文，属实测引述；「对 Ling 的建议」属推理，按 AGENTS.md 要求用普通段落 + 推测措辞，不进加粗结论表。

## 2. 证据卡片

### 卡片 A：共同背景不是静态存量，而是要在过程中累积与更新

- 出处：Clark & Brennan (1991)，PDF 第 1 页。原文：*"They cannot even begin to coordinate on content without assuming a vast amount of shared information or common ground—that is, mutual knowledge, mutual beliefs, and mutual assumptions… And to coordinate on process, they need to update their common ground moment by moment. All collective actions are built on common ground and its accumulation."*
- 研究情境：开篇以钢琴二重奏、握手、下棋、教学等协同活动说明沟通是集体行动。
- 可观察行为：说话者持续维护共同背景的「此刻状态」；沟通一旦跨轮次，就要为已说内容建立「已被理解」的证据，而不是说完即默认成立。
- 边界：这是对协调过程的理论陈述，不是对「多久过期」的量化结论；它给出的方向是「随过程更新」，不含具体时效数值。
- URL：http://www.psychology.sunysb.edu/sbrennan-/papers/old_clarkbrennan.pdf （作者自存档，需忽略证书校验）

### 卡片 B： grounding criterion —— 理解只要「对当前目的足够」，且必须双方互信

- 出处：Clark & Brennan (1991)，PDF 第 2 页。原文：*"The contributor and his or her partners mutually believe that the partners have understood what the contributor meant to a criterion sufficient for current purposes. This is called the grounding criterion."*
- 研究情境：Alan 问 Barbara「你和丈夫有车吗」，Barbara 复述「have a car?」表示尚未听清，Alan 答「Yeah」后才继续。
- 可观察行为：完成一轮贡献不等于说完一句话；要看到对方给出足够证据，且双方都认为证据足够。标准随「当前目的」变化——不是绝对理解。
- 边界：「足够」由当前目的决定，因此同一句话在不同任务里的 grounding 深度不同；不能推出「所有内容都要逐字确认」。
- URL：同上。

### 卡片 C：贡献分呈现与接受两阶段；接受态有 0–3 级

- 出处：Clark & Brennan (1991)，PDF 第 3 页。原文列出呈现阶段（A presents utterance u for B to consider）、接受阶段（B accepts u by giving evidence e that she believes she understands），并列出 B 的状态：State 0 没注意到、State 1 注意到、State 2 正确听到、State 3 理解所指。
- 研究情境：同一段对话，Barbara 对前半句处于 State 3，对末尾「j- car」处于 State 2，因此发起插入序列要求澄清。
- 可观察行为：听者复述疑问、要求修正，是接受阶段在起作用的可见信号；说话者要等到接受阶段完成才算贡献完成。
- 边界：状态是理论抽象，真实对话中说话者只能从证据推断，不能直接读取对方状态。
- URL：同上。

### 卡片 D：人寻找的是积极证据，不只是等负面证据

- 出处：Clark & Brennan (1991)，PDF 第 3–5 页。原文：*"people ultimately seek positive evidence of understanding"*；三种常见积极证据依次为 acknowledgments（如 uh huh、yeah、点头）、initiation of the relevant next turn（相邻对第二部分的恰当接续）、continued attention（持续注视、未被打断的注意力）。原文还明确：*"Whenever she turns to listen to someone else, looks puzzled, or hangs up the telephone, Alan has reason to believe that he has lost her."*
- 研究情境：作者反驳「没听到反对就算默认理解」的简化假设（文中点名 Grosz & Sidner 1986 等属此类）。
- 可观察行为：点头、接续回答、持续注视表示理解成立；转头看别人、困惑表情、挂断表示理解失效，会触发修正动作。
- 边界：积极证据是「理解的证据」，不等于理解正确——原文强调 B 的下一轮必须是恰当的回答，A 才会把它当作理解证据。
- URL：同上。

### 卡片 E：含糊指代如何确认 —— 四种专门技术（直接回答「那个」）

- 出处：Clark & Brennan (1991)，PDF 第 6–7 页，小节目的就是建立 referential identity（*"the mutual belief that the addressees have correctly identified a referent"*）。四技术：①alternative descriptions（B 用「Joe Joe Wright you mean?」带疑问调核对 A 的「that young gentleman from the park」，A 接受）；②indicative gestures（指、看、触，例「You mean this one [pointing]?」）；③referential installments（先把指称单独 grounding 再说内容，例「Take the spout—the little one that looks like the end of an oil can—」后停顿等「Okay」；Lindgren 式左移位「the small blue cap we talked about before?」后等对方「Yeah」）；④trial references（试探性指称，例「a man called Annegra?」对方回「Yeah, Allegra.」纠正，说话者复述接受）。
- 研究情境：专家教新手装泵、律师与证人确认身份、Krauss & Weinheimer 式指称沟通任务。
- 可观察行为：说话者用一个候选描述+疑问语气、手势、切分停顿、try marker 来主动邀请确认；听话者用替代名称、指认、「Yeah」加纠正来回应。原文给出语料统计：伦敦–隆德语料库中 29% 左移位名词短语后有对方插入动作，另有 52% 后接可点头接受的停顿。
- 边界：这些技术用于「指称身份成为当下任务焦点」的场景；不是所有指代都要走完整确认。第③项的关键是先确认指称再推进命题，否则后续内容难以理解。
- URL：同上。

### 卡片 F：话语结构三成分，焦点栈与「刚才/更早」的区分

- 出处：Grosz & Sidner (1986)，PDF 第 8–9 页（报告 7–8），原文：话语结构由 linguistic structure、intentional structure、attentional state 三成分构成；attentional state 含「objects, properties, relations, and discourse intentions that are most salient at any given point」，作用是「summarize information from previous utterances crucial for processing subsequent ones thus obviating the need for keeping a complete history of the discourse」。
- 研究情境：任务对话、叙事、说明等多种语篇的分段与焦点现象。
- 可观察行为：系统只保留「对后续处理关键的 salient 信息」，不保留完整历史；段落边界处说话方式变化（停顿长度、语速、时态/体变化、线索词如「in the first place」「incidentally」）。
- 边界：原文自己指出段落边界的一致性缺乏心理学研究，分歧集中在边界附近的话段（Mann 的未发表观察，属 personal communication，属转述级别）。不能把焦点栈当成精确可判的算法。
- URL：https://www.ideals.illinois.edu/items/17595/bitstreams/63256/data.pdf

### 卡片 G：跨段与段内的指代约束不同，焦点栈决定搜索顺序

- 出处：Grosz & Sidner (1986)，PDF 第 11–12 页（报告 12–13）与第 29–30 页（报告 48–50）。原文：*"there are different constraints on the use of pronouns and reduced definite-noun phrases within a segment than across segment boundaries"*；焦点栈第二作用是约束定指名词与代词的指称搜索，栈中 FS5 先于 FS4 被考虑，「the screw in the center」因在 FS5 中隐性在焦点而直接确定，不必考虑 FS4 的两个紧定螺钉。代词机制是 centering：backward-looking center 约束后续代词指称，它会随段内推进而转移，而 DSP 在整段内恒定。
- 研究情境：专家/学徒拆飞轮任务；片语中「their」用代词、随后改用完整定指名词的差异。
- 可观察行为：段内倾向代词与简化定指描述；跨段更可能用完整描述；焦点空间中更上层的实体先被当作候选；段内焦点会转移（backward-looking center 可变）。
- 边界：这是约束而不是决定论——原文说分段「显然不是唯一因素」。centering 是更局部的现象，不等于整段话题。
- URL：同上。

### 卡片 H：承诺/待兑现事项——采纳意图即承诺其必要子动作

- 出处：Grosz & Sidner (1986)，PDF 第 22 页（报告 34）。原文：*"it is a shared belief that, unless he states otherwise, the OCP will adopt the intention to perform an action that the ICP intended him to. Second, in adopting the intention to carry out that action, the OCP also intends to perform subactions that are necessary. Thus, once the apprentice intends to remove the flywheel, he also commits himself to the collateral intentions of loosening the setscrews and pulling the wheel off."* 并指出子动作不必都显式引入话语。
- 研究情境：专家让学徒拆飞轮；「松开紧定螺钉」与「拉下飞轮」的满足优先关系部分来自共享任务知识、部分由话语直接说出（「First loosen…, then pull」）。
- 可观察行为：接受一个目标即携带其未言明的附带子目标；「后续/后来呢」的推进依赖这类意图层级与满足优先关系，而不是只依赖字面句子。
- 边界：原文强调这种推断建立在参与者共享任务知识之上；换任务或换领域，附带意图不可照搬。部分关系并未在对话中显式传达。
- URL：同上。

### 卡片 I：中断与恢复——注意力结构会弹出/压栈，意图结构留存

- 出处：Grosz & Sidner (1986)，PDF 第 30–33 页（报告 51 起，第 5 节 Interruptions）。原文把中断作为理论的检验场，指出必须区分 intentional structure 与 attentional state，并区分「语义返回」等现象；焦点空间在被中断段结束时弹出，被中断段的焦点重新回到栈顶。
- 研究情境：对话被无关、部分相关、高度相关的插入打断，以及随后回到原话题。
- 可观察行为：回到旧话题时，焦点恢复到被中断处；「后来呢」之所以能接续，靠的是意图结构仍在、焦点空间重新压回。
- 边界：原文明确说这些例子不穷尽中断类型，只覆盖相关性谱系上的若干点。不要当成穷举分类。
- URL：同上。

### 卡片 J：共同背景失配是可观察的，且摩擦与任务成功相关

- 出处：Sarkar et al. (2025)，ACL 2025，pp. 3200–3215，PDF 第 1–2、11 页。原文：*"conversational friction"* 定义为因说话者对共同背景内容的信念失配造成的交际流中断；摩擦需要「re-negotiation of content」与「conversational detour」；实测发现成功对话摩擦更少，含摩擦的对话里若双方做出 grounding 努力（RequestRepair / Repair），更可能完成任务；LLM 能识别显性摩擦（77.22% 被检测到的摩擦含显式 RequestRepair），但难以识别需要语用或领域推理的隐性摩擦。
- 研究情境：200 段 Ubuntu IRC 技术支持对话（7590 轮），其中 70 段标注 grounding act。例中 B 以为「cd 语法」已进入共同背景，A 在第 9 轮证明该假设错误。
- 可观察行为：失配通常先表现为对话流中断或修复请求；修复是双方协作重建共同背景的过程；隐性失配（如把「try」误读为命令一部分）无显式标记。
- 边界：这是文本 IRC 场景的相关性研究，不能推出因果或普适时效；作者自述 LLM 对隐性案例能力不足，且数据来自单一领域（Ubuntu 技术支持）。
- URL：https://aclanthology.org/2025.acl-long.161.pdf

### 卡片 K（元数据已核实，正文未核实）：joint action 与 joint commitment

- 出处：Herbert H. Clark, *Using Language*, Cambridge University Press, 1996，DOI `10.1017/cbo9780511620539`。Crossref 核实章节：第 4 章 "Joint actions"，pp. 59–91，DOI `10.1017/cbo9780511620539.004`；第 11 章 "Joint commitment"，pp. 289–317，DOI `10.1017/cbo9780511620539.011`。
- 状态：本次仅核实书目，**未取得章节正文**。因此「three floors」的具体表述、joint action 的层级划分、joint commitment 的解除条件，均**未核实**，不作为证据使用。
- URL：https://doi.org/10.1017/cbo9780511620539

### 卡片 L（二手转述，未核实）：three floors 类框架

- 状态：任务点名「H. Clark 的 three floors 或相邻相关研究」。本次检索没有拿到可核验的原文定义，也没有拿到可信的权威二手转述。仅能确认「Using Language 里有关 joint actions / joint commitment 的章节」这一相邻线索存在（见卡片 K）。任何关于「三层具体是哪三层」的说法此处**不提供**，标记为未取得。
- URL：无可用链接。

## 3. 对 Ling 的规则建议：常驻 / 随话题带出 / 明确需要时找回

以下属基于上述证据的推理，不是文献原文结论，按置信度用普通段落表述。

**常驻（对应共同背景的稳定底盘）**：参与者的稳定身份与长期偏好、用户与助手已经互认的协作方式、当前任务的总体目的（Grosz & Sidner 的 discourse purpose）。Clark & Brennan 卡片 A 说明共同背景是所有协同的基础，因此这部分应当在任何话题切换后仍可用。但「常驻」不等于「无条件正确」：卡片 J 显示失配常发生在对「什么已进入共同背景」的假设上，所以常驻项应当带有来源与确认时间，而不是只存一个裸值。

**随当前话题带出（对应注意力状态/焦点空间）**：当前段正在谈论的对象、当前段的 segment purpose、最近一轮的 backward-looking center、被中断前的话题。卡片 F、G、I 支持这种做法：人只保留对后续关键的 salient 信息，焦点按段组织且会弹出/压栈。具体到用户问句，「那个」优先在栈顶焦点空间里找；「后来呢」优先接当前段的意图层级与满足优先关系。段边界出现线索词或话题切换信号时，应主动换栈而不是把旧实体继续当默认候选。

**只在明确需要时找回（对应重新对齐而非猜测）**：用户用替代描述询问、要求「你说的 X 是哪个」、或出现线索显示理解失效时，才触发更深的检索与显式确认。卡片 E 的人类做法是：不确定就给候选描述 + 疑问语气、试探性指称、或先单独 grounding 指称再推进；卡片 D 说明负向证据不是充分条件，但也不是要每次都做完整确认——grounding criterion 是「对当前目的足够」。因此建议把找回动作分级：默认用栈顶；出现显式修复请求才全域检索；含糊但没有修复请求时，先给带候选的确认问句，而不是猜一个答案继续说。

**对「前天那个方案」「后来呢」「那个」的分别处理（推理）**：「那个」依赖的是当前注意力焦点（卡片 G），属随话题带出；「后来呢」依赖的是意图结构与中断恢复（卡片 H、I），需要知道当前在追哪个目标、被什么打断；「前天那个方案」依赖的是跨会话的个人共同背景与临时共同背景的交集（卡片 A 的 mutual knowledge/beliefs/assumptions），需要外部记忆找回，且因为时间跨度大而更可能出现失配（卡片 J），所以应优先走显式确认而不是直接断言。三者的检索代价与确认需求依次升高，这与 grounding criterion 随目的变化一致。

**共同背景何时过期**：文献没有给出统一时限。可核验的说法只有两条：共同背景需要 moment-by-moment 更新（卡片 A）；失配会因双方假设分叉而产生、并需要重新协商（卡片 J）。因此建议把「过期」实现为「出现失配证据或超过该目的的grounding标准」的事件驱动判断，而不是固定时间阈值。此处是设计推理，不是文献给出的阈值。

## 4. 对反例线索的解释边界

lead 在本次研究中实测更正：反例里「？触发召回」不成立；真正未跳过自动召回的是「早上好」。据此，本节只给出解释边界，不把「？」当作人类指代确认行为的反例。

- 「早上好」未跳过自动召回，只能说明系统在该输入上的召回策略未按预期生效；它不能直接证明「人类在问候语后会期待召回」，也不能反过来证明「人类不需要共同背景」。要把系统行为当作人因证据，需要先排除路由/配置/意图识别层面的原因。
- 根据卡片 D、E，人类在含糊时主动制造确认机会（替代描述、试探性指称、指称分批）；这描述的是「有指称任务且身份未定时」的行为。问候语不构成这类任务，因此不能用它检验指代 grounding 规则。
- 根据卡片 B，grounding 标准是「对当前目的足够」。若当前目的不需要确定指称，不发起确认是合理行为，不构成对本文规则的反证。
- 「？」作为单独输入在文献卡片里没有专门结论。卡片 D 的 acknowledgments（uh huh、yeah、点头）是理解证据，但「？」在不同语境可能是疑问、困惑或等待，不能从本文证据推出它必须触发或不触发召回。lead 的实测结论优先。
- 任何从「系统未跳过」推到「人脑机制」的归因都属于跨层推断，置信度低，应保留为待验证问题而不是结论。

## 5. 未取得数据与二手转述

- 未取得 Clark & Brennan (1991) 原章完整 23 页（pp. 127–149）。本次使用的是作者旧站删节存档 12 页，缺少书中「共同背景的来源」相关章节（个人共同背景/社区共同背景/物理共场三类来源）。因此本文没有引用三来源分类，也不虚构其页码。
- 未取得 Clark (1996) *Using Language* 第 4、11 章正文；joint action、joint commitment 只有元数据。
- 未取得「three floors」的可核验原文或权威二手定义；卡片 L 明确空缺。
- 未取得 Grosz & Sidner 理论在中文指代（如「前天」「后来」这类时间/叙事索引）上的直接实验数据。本文对此只有推理，没有实证支持。
- Wikipedia、DuckDuckGo、Google 搜索在本次环境超时；ScienceDirect 返回 403；Cambridge Core 相关章节链接 404；Semantic Scholar 搜索接口 429（按 DOI 单篇查询可用）。这些失败项未产生证据。
- Crossref、OpenAlex、Semantic Scholar 的返回结果只用于核对 DOI、页码、作者、开放获取状态；对 OpenAlex 给出的一个疑似错配 OA 链接（把 Clark & Brennan 指向 Caltech 的 Simon 论文），已弃用，未作为证据。
- 卡片 I 中段落边界一致性所引用的 Mann 观察，在原文里就是 personal communication，属转述级别，已按转述标注。

## 实际 URL

1. Clark & Brennan (1991) 作者自存档 PDF：http://www.psychology.sunysb.edu/sbrennan-/papers/old_clarkbrennan.pdf （下载需忽略证书校验）
2. Clark & Brennan (1991) 书目页：https://doi.org/10.1037/10096-006
3. Grosz & Sidner (1986) 原文 PDF：https://www.ideals.illinois.edu/items/17595/bitstreams/63256/data.pdf
4. Sarkar et al. (2025) ACL 原文 PDF：https://aclanthology.org/2025.acl-long.161.pdf
5. Sarkar et al. (2025) DOI：https://doi.org/10.18653/v1/2025.acl-long.161
6. Clark (1996) *Using Language* DOI：https://doi.org/10.1017/cbo9780511620539
7. Clark (1996) "Joint actions" 章节 DOI：https://doi.org/10.1017/cbo9780511620539.004
8. Clark (1996) "Joint commitment" 章节 DOI：https://doi.org/10.1017/cbo9780511620539.011
9. SEP Common Knowledge（仅作背景对照，非本文证据）：https://plato.stanford.edu/entries/common-knowledge/

## 失败项

- Wikipedia「Common ground (communication theory)」「Centering theory」「Herbert H. Clark」：超时。
- DuckDuckGo html/lite 搜索、Google 搜索：超时。
- ScienceDirect 文章页：403。
- Cambridge Core 章节页：404。
- Semantic Scholar 搜索接口：429（单篇 DOI 查询成功）。
- OpenAlex 返回的 Clark & Brennan OA 链接（Caltech Simon 论文）：错配，弃用。
- Clark & Brennan 完整章、Clark 1996 章节正文、three floors 定义：未取得。
