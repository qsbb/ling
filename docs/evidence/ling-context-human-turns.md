# 人类对话中的轮次组织与澄清：给 Ling 上下文策略的行为证据

> 取证日期：2026-11-22。仓库状态：HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`，`main`，开始时 `git status` 干净。
>
> 置信度标记：**[原文]**＝我这次实际打开并读到的原文文字（含逐字引文）；**[元数据]**＝OpenAlex/Crossref/Semantic Scholar 的权威书目信息或原摘要，未拿到全文；**[转述]**＝通行二手转述，我未逐页核对；**[推理]**＝我从证据到 Ling 的推论，不是文献结论。排版纪律：加粗与表格只给 [原文]/[元数据]，推测一律用普通段落并带标记。

## 1. 范围与取证方法

问题范围：在线即时文本对话里，「？」「嗯」「后来呢？」分别处于什么轮次位置、承担什么功能；人何时接眼前上一句、何时回头修复更早的话；第三人称/指代与答非所问如何做 next-turn repair；停顿（typing/gap）如何改变解释。

方法：先读项目 `AGENTS.md` 与已有 `docs/research/H1-turn-taking-repair.md`（后者把全部文献标为「转述未核实」，本文的任务是把其中可被核实的部分核实）。随后用 `web_fetch` 访问公开来源。本机 `curl` 直连公共互联网普遍超时（Wikipedia、Google、archive.org、Google Scholar 均失败），因此改用 `web_fetch`；它能到达 PMC、Frontiers、PLOS、ACL Anthology、OpenAlex API、Semantic Scholar API 与部分大学站点。文献定位主要靠 OpenAlex API 检索，再取开放获取版本。

取证结果概览：Sacks/Schegloff/Jefferson 三篇经典均**未取到可逐字核对的全文**（DOI 落地页多为 403/404；1974 俄译重印 PDF 链接已失效；OpenAlex 全文端点需 API key）。因此本文对经典理论的表述保持 [转述]/[元数据]，绝不伪造页码或引文。可逐字引用的支撑来自三篇开放获取实证/综述：Stivers et al. 2009（PNAS，PMC 全文）、Meyer 2023（Journal of Cognition 全文）、Goddard & Gillespie 2025（PLOS ONE 全文），以及 Balaraman et al. 2023（ACL 摘要页）与 Mertens & de Ruiter 2021（摘要）。

适用边界先行：这些是**人类口语/异步论坛**行为。Ling 是 LLM 系统，异步 IM 的「发送键」、typing 指示器、生成延迟都不是口语 TRP 的等价物。任何时长数字**不得**直接成为 Ling 的等待阈值或工作记忆参数。

## 2. 逐研究证据卡片

### 卡片 A — Sacks, Schegloff & Jefferson (1974), *A Simplest Systematics for the Organization of Turn-Taking for Conversation*

- **原文出处 [元数据]**：*Language* 50(4): 696–735，DOI `10.1353/lan.1974.0010`。OpenAlex 记录了一篇 2015 年俄译重印（`10.17323/1728-192x-2015-1-142-202`，CC-BY，声称有全文），但该 PDF 链接在 Sociologica HSE 站已 404，OpenAlex 全文端点返回 401（需 API key）。我没有读到全文。
- **研究情境 [转述]**：北美英语自然会话录音，手工转写，逐轮标注；口语面对面/电话，非文本 IM。
- **可观察行为 [转述，未核实]**：提出 TCU（turn construction unit）、TRP（transition relevance place）、adjacency pair（FPP→SPP）、insertion sequence；turn 分配混合「当前说话人选择下一人」「下一人自选」「当前说话人继续」三种机制。1974 系统的核心主张是：转交以 TCU 可能完成之处为候选位置，听者可预测该点并交接。
- **适用边界**：该模型依赖语调、重音、可预测的句法完成点。纯文本没有韵律，「发送一条消息」不等于到达 TRP。它是**结构描述**，不是可复制的调度算法。
- **本文据此提出的推论 [推理]**：把「一句已完成的消息」当作一个 TCU 候选边界，比按字符数或固定秒切分更接近人类对轮次的感觉；但这只是设计类比，不是文献对 IM 的结论。

### 卡片 B — Schegloff, Jefferson & Sacks (1977), *The Preference for Self-Correction in the Organization of Repair in Conversation*

- **原文出处 [元数据]**：*Language* 53(2): 361–382，DOI `10.1353/lan.1977.0041`。我通过 OpenAlex 拿到了**原文摘要全文**（`abstract_inverted_index` 重建）：
  > "An 'organization of repair' operates in conversation, addressed to recurrent problems in speaking, hearing, and understanding. Several features of that organization are introduced to explicate the mechanism which produces a strong empirical skewing: self-repair predominates over other-repair, show operation of preference for repair. Consequences for conversational interaction are sketched."
- 术语 [元数据]：OpenAlex 关键词含 `conversational-repair`、`self-healing`；被引 6683 次（查询当日）。
- **研究情境 [元数据+转述]**：自然会话录音；分类维度是「谁发起修复（self/other）× 谁执行修复（self/other）」。[转述未核实] 偏好结构是结构性的经验偏斜，不是说话人的心理偏好。
- **可观察行为 [转述，未核实]**：self-initiated self-repair 最多；other-initiated other-repair 最少且带有社交代价；「Huh?/What?」类 third-position repair 出现在理解受阻的下一轮。
- **适用边界**：「偏好」是分布性结果，不是对每个个体的规范命令；更不是让 Ling 永远自我纠正。口语里的 third-position 靠语调，文本「？」没有升调信息。
- **推论 [推理]**：若用户只发「？」而没有替 Ling 写答案，可把它标为「他人发起、待执行」的修复，优先让原说话人（Ling）重说自己上一句，而不是扩展到无关旧话题。

### 卡片 C — Schegloff (1968), *Sequencing in Conversational Openings*

- **原文出处 [转述]**：*American Anthropologist* 70(6): 1075–1095，DOI `10.1525/aa.1968.70.6.02a00060`；1999 重印于 *Sequence and Organization in Conversation*（Cambridge UP）。我未取到全文。
- **研究情境 [转述]**：电话开场序列。
- **可观察行为 [转述，未核实]**：「Hello」类问候是相邻对的 FPP，其 SPP 典型是返回问候，而非对问候内容的字面回答；开场可含多个可识别的序列槽。
- **适用边界**：电话开场的互惠义务来自电话礼仪与双方都是会话参与者；IM 里机器人回问候是人设策略，不是社会义务。
- **推论 [推理]**：「早上好 → ？」可被建模为问候相邻对未闭合时的催 SPP；但若 Ling 已回过问候，「？」落到第三位置，功能改变。

### 卡片 D — Stivers et al. (2009), *Universals and cultural variation in turn-taking in conversation*

- **原文出处 [原文]**：PNAS 106(26): 10587–10592，DOI `10.1073/pnas.0903616106`；PMC 全文已读：https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/ 。
- **研究情境 [原文]**：10 种语言、5 大洲、101 段自然会话视频；每段采集 350 个连续问句，只分析极性（yes/no）问句及其回答；ELAN 以 10 ms 编码、四舍五入到 100 ms。不是 IM，不是人机对话。
- **可观察行为 [原文，逐字引文]**：
  > "we find that the response timings for each language, although slightly skewed to the right, have a unimodal distribution with a mode offset for each language between 0 and +200 ms, and an overall mode of 0 ms"
  >
  > "The means display somewhat more variation... Danish has the slowest response time on average (+469 ms) and Japanese has the fastest (+7 ms). The mean response offset for the full dataset is +208 ms, and the language-specific means fall within ≈250 ms either side of this cross-language mean"
  >
  > "Speakers of all of the languages provide answers significantly faster than nonanswer responses to questions"
  >
  > "Within the set of answers, those that are confirmations are delivered faster than disconfirmations in all languages, between 100 and 500 ms faster on average"
  >
  > "Information requests are slower than questions with other functions such as those initiating repair."
  >
  > "If a community of speakers shows a highly regular target for the timing of turn transition, deviations will come to have a natural communicative significance (e.g., delays implying problems with the prior utterance)"
- **多变量结果 [原文]**：非回答 +131.78 ms（p≤.001）、确认 −206.87 ms（p≤.001）、信息请求 +129.38 ms（p≤.001）。跨语言相同方向。
- **适用边界（强）**：这是**口头轮次起始延迟**，单位毫秒，条件是双方同处一段连续会话且能预测对方话语结束；作者自己说「highly structured institutional interaction」另有规则。IM 的上一条消息和这一条之间可能隔天、可能离线，Δt 不是 FTO。**这些数字不能当 Ling 的时间窗口、记忆 TTL 或 typing 等待阈值**。可以外推的只有「答非所问/不回答会更慢」「确认比否认更快」「修复发起类问句比信息请求更快」这三类**相对方向**，且需 IM 语料重新测。
- **对「停顿改变解释」的直接支撑 [原文]**：偏离本地节奏会被读作对前一句有问题——这是人类会话的普遍符号学，但「本地节奏」由该社区自己校准（丹麦 200 ms 仍算 on time，多数语言算 delay），不存在一个跨场景的固定秒数。

### 卡片 E — Meyer (2023), *Timing in Conversation*（综述）

- **原文出处 [原文]**：*Journal of Cognition* 6(1): 20，DOI `10.5334/joc.268`；全文已读：https://journalofcognition.org/articles/10.5334/joc.268 。
- **研究情境 [原文]**：同行评议综述，整合会话分析与实验心理学；讨论的是面对面会话与实验室言语产生。
- **可观察行为 [原文，逐字引文]**：
  > "Turn-taking in everyday conversation is fast, with median latencies in corpora of conversational speech often reported to be under 300 ms."
  >
  > "there are backchannels, such as 'uhu' or 'yeah', which listeners use to encourage their partners to continue their turns and which are often not classified as turns themselves"
  >
  > "successive turns are pragmatically linked, that is, they fit in the context of the conversation. Questions need relevant answers, requests need to be accepted or rejected, stories need relevant comments"
  >
  > "an unexpectedly long gap may express reluctance to accept a request"
  >
  > "speakers can choose to begin with an easy-to-plan particle, such as 'Well…'... conversational speech is riddled with disfluencies, i.e. silent and filled pause, repetitions, errors and repairs"
- **机制 [原文]**：听者识别 speech act 与 gist 后可提前规划回答，预测话语结尾并在末尾前 launch；但 [原文] "upcoming speakers do not necessarily begin to plan utterances as early as possible... listeners may postpone response planning and so reduce the mental load arising from keeping a planned utterance in working memory."
- **适用边界（强）**：这是人类同时做「听＋规划言语」的双任务机制；LLM 的上下文窗口与人类工作记忆不是同一对象。**不得**用「人类工作记忆有限」推断 Ling 的上下文策略；[原文] 反而说明人会**主动推迟**规划以减轻工作记忆负担——这是关于人的事实，不是对模型设计的命令。
- **与「嗯」直接相关 [原文]**：`uhu`/`yeah` 被描述为 backchannel，功能是鼓励对方继续，且常常**不算作一个 turn**。这是本文对「嗯」最强的原文证据。文本「嗯」是否算 turn 仍需 IM 语料判定。

### 卡片 F — Goddard & Gillespie (2025), *Conversational repairs on Reddit: Widely initiated but often uncompleted*

- **原文出处 [原文]**：PLOS ONE 20(1): e0316618，DOI `10.1371/journal.pone.0316618`；全文摘要与引言已读：https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0316618 。
- **研究情境 [原文]**：25 个英文 subreddit，异步公开文本讨论；分析 other-initiated repairs（OIR）。这是目前最接近 IM 的开放获取实证。
- **可观察行为 [原文，逐字引文]**：
  > "Conversational repairs, defined as any sequence of interaction aimed at addressing misunderstanding"
  >
  > "finding them to be widespread (in every subreddit) and frequent (58.48% of interactions experience a repair initiation)"
  >
  > "a repair initiation becomes increasingly likely the longer a comments thread progresses (Median time-to-repair = 6 turns)"
  >
  > "we also find 44.80% of initiations receive no reply, precluding the possibility of a repair completion. Thus, conservatively, nearly half of the repair initiations in our data went uncompleted."
  >
  > "This suggests that the online medium alters the way initiations are completed compared to offline interactions."
- **适用边界**：Reddit 是公开多人异步帖，不是一对一私聊；「44.80% 无人回复」不能搬到 Ling（Ling 每条必回）。可迁移的只是：**在线文本里修复发起很常见，但常常不被完成**——所以「发起一次修复就要一次性解决全部旧话题」不是人类做法。
- **推论 [推理]**：Ling 面对「？」应做一次小而闭合的修复（重说上一句/问清目标），而不是顺便把长期记忆里的旧需求全列出来。

### 卡片 G — Mertens & de Ruiter (2021), *Cognitive and social delays in the initiation of conversational repair*

- **原文出处 [元数据+摘要原文]**：*Dialogue & Discourse* 12(1)，DOI `10.5210/dad.2021.102`；页面：https://journals.uic.edu/ojs/index.php/dad/article/view/11388 。摘要逐字：
  > "Most turns are initiated within 250ms after the previous turn. However, interlocutors take longer to initiate certain types of turns: those that either require more cognitive processing or are socially dispreferred."
  >
  > "We focus on a type of turn that is socially dispreferred, cognitively demanding, and generally delayed: other-initiations of repair (OIRs). OIRs occur when a listener notices and decides to signal a comprehension problem (e.g., 'What?'). We analyzed the Floor Transfer Offsets of 456 OIRs, and found that interlocutors initiated OIRs later when trouble sources had weaker discourse context or were shorter, and when the OIR was more face-threatening."
- **研究情境 [元数据]**：456 个 OIR，口语对话，测 FTO。
- **可观察行为 [原文]**：trouble source 语境更弱或更短时、OIR 更威胁面子时，发起更晚。
- **适用边界**：这是**人发起** OIR 的延迟；Ling 回复用户 OIR 的延迟没有对应人类数据。数字 250 ms 属口语 FTO，不是 Ling 阈值。
- **推论 [推理]**：当用户用极短形式（「？」）且前语境薄弱时，人对同类信号的判断也会变慢/更犹豫；Ling 应显式标注目标歧义，而不是凭短形式猜一个大话题。

### 卡片 H — Balaraman et al. (2023), *No that's not what I meant: Handling Third Position Repair in Conversational Question Answering*

- **原文出处 [原文：ACL 摘要页]**：SIGDIAL 2023, pp. 562–571，DOI `10.18653/v1/2023.sigdial-1.52`；https://aclanthology.org/2023.sigdial-1.52/ 。PDF 存在但 `web_fetch` 拒绝 `application/pdf`，正文未读。
- **研究情境 [原文摘要]**：构建 REPAIR-QA 数据集，任务是对话式 QA 中的 third position repair（TPR）——说话人先被误解，在对方给出错误回应后纠正该误解。
- **可观察行为 [原文摘要，逐字引文]**：
  > "People usually deal with miscommunication immediately as they detect it, using highly systematic interactional mechanisms called repair."
  >
  > "One important type of repair is Third Position Repair (TPR) whereby a speaker is initially misunderstood but then corrects the misunderstanding as it becomes apparent after the addressee's erroneous response."
  >
  > "The results indicate poor out-of-the-box performance on TPR's by the GPT-3 models, which then significantly improves when exposed to REPAIR-QA."
- **适用边界**：这是**任务型问答数据集**，不是开放域陪伴对话；且是机器性能研究，不是人类行为常模。它证明 TPR 是可计算、可训练的任务，并且 LLM 开箱表现差。
- **推论 [推理]**：Ling 遇到「用户答非所问/指出理解错」时，应把「纠正前一个误解」当作独立任务，而不是把它当新一轮正常问答。

### 卡片 I — Hoey (2017), *Sequence recompletion: A practice for managing lapses in conversation*

- **原文出处 [元数据]**：*Journal of Pragmatics*，DOI `10.1016/j.pragma.2016.12.008`；OpenAlex 记为 bronze OA。我未取到全文。
- **可观察行为 [转述，未核实]**：会话中的 lapse（长时间无回应）后，人会用「重做/补全先前序列」的方式恢复，而不是把停顿当作全新话题。
- **适用边界**：口语 lapse 的恢复依赖在场感与共享节奏，IM 的隔夜消息常是正常状态。
- **推论 [推理]**：这是「隔天回来先恢复上一序列」的一类人类证据，但证据强度低；不足以支撑 Ling 在隔天首条时就强势续旧话题。

## 3. 对 Ling 的可观察规则建议

以下每条都写成**快照/回放可核对**的形式。凡带 [推理] 的都不当成已验证的人类常数。

1. **「？」的位置判定优先于内容判定 [推理]**。看「？」前面那一条是不是 Ling 的消息、那一条是否构成可确认断言或相邻对 SPP。若是问候相邻对未闭合 → 催 SPP；若已闭合 → 第三位置修复；若前面是 Ling 的长答 → 对该答的理解修复。判据：`user_text="？"` 的上一条 `role=assistant` 内容与是否回过问候。
2. **「嗯」默认 backchannel，不默认新 FPP [原文支持 + 推理]**。卡片 E 原文把 `uhu/yeah` 归为鼓励对方继续的 backchannel 且常不算 turn。迁移到文本是 [推理]：紧跟 Ling 长回复后的「嗯」优先解释为「收到/请继续」，不触发宽泛记忆召回；隔天孤立的「嗯」缺少可续对象，才考虑新开场。
3. **「后来呢？」是续讲请求，不是记忆检索指令 [推理]**。它指向上一条未完成叙事的续接位置；若上一条是 Ling 的旧需求列表，才可能续旧话题。判据：上一条消息是否明显未完（叙事/列表中断）。
4. **修复目标限制在眼前一轮 [原文方向 + 推理]**。卡片 B 的偏好结构是修复自我偏斜；卡片 F 说明在线修复常未完成。两者合起来支持：一次「？」只做一次小修复，**不**同时展开长期记忆里的最显著旧事。
5. **答非所问/第三人称指代做 next-turn repair [原文方向 + 推理]**。卡片 D：非回答更慢、信息请求更慢但 repair 发起更快；卡片 H：TPR 是独立任务且 LLM 开箱差。可观察规则：用户用「他/那个/刚才那个」或给出与问题不匹配的回答时，Ling 下一轮应先确认指代对象/说明误解，再答内容。判据：快照中是否出现显式的「你说的 X 是指……吗」类修复句。
6. **停顿只当调节信号，不当开关 [原文支持]**。卡片 D/E：偏离本地节奏会被赋予意义，且本地节奏因社区而异。对 IM 的可观察形式只有两类：[原文] 层面是「是否存在 typing/已读/回复间隔」；[推理] 层面是「同一对话中该用户惯常间隔」。不得写死 200/250/300 ms 阈值。
7. **偏好自我修复不等于 Ling 自我辩护 [推理]**。1977 的「self-repair predominates」是人类的经验分布；Ling 重生成一句是技术动作，不是自我认知。可观察目标是「原说话人重说上一句」，不是「模型坚持自己是对的」。

## 4. 反例线索：「早上好」+ 一个「？」后不该列四产品需求

链路（来自 `docs/research/LEAD-notes.md`、`M0-synthesis.md`，本文未独立验证快照）：用户先问「你还记得前天聊了什么吗」→ 发「早上好」→ 对刚才问候回「？」；Ling 先解释问候，又续列很久以前的四项产品需求。

**能解释什么**

- 相邻对视角 [推理]：若「早上好」的 SPP 未闭合，「？」优先催回问候；若已闭合，「？」是第三位置，指向上一句 Ling 消息。两种位置都**不**指向前天的四需求。卡片 C 支持这一位置区分（转述层）。
- 修复目标视角 [推理+卡片 F 原文方向]：修复是局部工作；「？」一次只应修眼前一轮。把修复扩展成旧需求清单，违反局部性。
- 时序视角 [原文支持，仅方向]：卡片 D 显示信息请求比 repair 类问句慢；卡片 G 显示薄弱语境的 OIR 更晚发起。这些只说明「短形式＋弱语境」本身会让人类判断更犹豫，支持 Ling 显式澄清，不支持自动召回旧事。
- 在线medium视角 [原文]：卡片 F 发现近半 OIR 未获回复——在线修复本就常不完整；Ling 一次回答里塞完整旧清单，反而比人类更「过度完成」。

**不能解释什么（必须说清）**

- 不能证明旧需求是 `lane="summary"` 常驻块带出来的，还是「？」触发的自动记忆召回捞出来的。两者在「最终列了旧需求」这个输出上相同；仓库里 `docs/research/P3-lightweight.md` 还给出 [实测] 反证：`？` 经 NFKC + 去标点后 plain 为空，属于 `_STANDALONE_REPLIES`，会跳过自动召回。本文没有独立跑过该代码，**不采信也不反驳**，只指出「？触发召回」这一归因在仓库内已有争议。
- 不能证明「解释问候」在人类看来就是未完成 SPP。这是 [推理]；卡片 C 的转述只覆盖电话开场，不覆盖中文 IM。
- 不能把英文 `uhu/yeah` 的 backchannel 结论直接等同于中文「嗯」——中文「嗯」在疑问上调后可能是质疑，文本中无韵律可判。
- 不能给出「隔多久算隔天/算新话题」的门槛。人类节奏是社区校准的（卡片 D 原文），不存在可搬用的秒数。
- 不能证明 LLM 的「重新生成」等同于人类的 self-repair（卡片 H 只证明 TPR 是可训练任务）。

## 5. 关键来源链接与访问日期

全部访问日期 2026-11-22。

- [Sacks, Schegloff & Jefferson 1974, DOI](https://doi.org/10.1353/lan.1974.0010) — 403/重定向，全文未取。
- [OpenAlex 记录：1974 俄译重印](https://api.openalex.org/works/W4241891521) — 元数据与摘要可读；PDF 链接 404、全文端点 401。
- [Schegloff, Jefferson & Sacks 1977, DOI](https://doi.org/10.1353/lan.1977.0041) — 落地页不可读；[OpenAlex 记录](https://api.openalex.org/works/W1980298376) 提供原摘要与卷期页码。
- [Stivers et al. 2009, PMC 全文](https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/) — **原文已读**，核心逐字引文来自此页。
- [Meyer 2023, Journal of Cognition 全文](https://journalofcognition.org/articles/10.5334/joc.268) — **原文已读**。
- [Goddard & Gillespie 2025, PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0316618) — **摘要与引言原文已读**。
- [Balaraman et al. 2023, ACL Anthology](https://aclanthology.org/2023.sigdial-1.52/) — **摘要页原文已读**；PDF 因内容类型被 `web_fetch` 拒绝。
- [Mertens & de Ruiter 2021, Dialogue & Discourse](https://journals.uic.edu/ojs/index.php/dad/article/view/11388) — **摘要原文已读**。
- [Schegloff 1968, DOI](https://doi.org/10.1525/aa.1968.70.6.02a00060) — 未取到全文，仅转述。
- [Hoey 2017, OpenAlex 记录](https://api.openalex.org/works/W2574537012) — 元数据可读，全文未取。

## 6. 证据不足处

- **经典三篇没有逐字原文**。1974 的 TCU/TRP、1977 的四类修复与偏好结构、1968 的问候相邻对，全部是 [转述]。本文不提供这些概念的页码，也不声称核对过纸本。
- **「后来呢？」没有直接文献**。它是我从「叙事需相关回应」「续讲请求」[原文方向] 推出的 [推理]，没有找到专门研究该汉语形式的文献。
- **「嗯」的中文证据不足**。backchannel 结论来自英语 `uhu/yeah` [原文]，且原文明确说这类成分常不算 turn；中文「嗯」的疑问调、拖长、连续三个「嗯」等差异未覆盖。
- **typing/gap 在 IM 中的作用没有直接实证**。卡片 D/E 只给出口语延迟的符号学方向与社区校准事实，不能推出 typing 指示器如何改变解释。
- **Reddit 论文与 Ling 场景差距大**：公开异步多人群帖 vs 私聊一对一；44.80% 未回复在 Ling 不适用。
- **反例机制未在本文核实**。我未跑任何回放/提示词快照，也未独立验证 `P3-lightweight.md` 的 NFKC 结论；「四需求来自哪条通道」保持未决。
- **未能访问**：Wikipedia 全部页面、archive.org、Google、Google Scholar、PNAS DOI 站、TandF、Project Muse、Cambridge、Frontiers PDF、ACL PDF、HSE PDF。失败详情见下节。

## 本次实际访问的 URL 与失败记录

成功（读到内容）：

- `https://www.purdue.edu/gig/05_schegloff.htm` — 返回 404 页面（Purdue 的 404 模板），非目标内容。
- `https://pmc.ncbi.nlm.nih.gov/articles/PMC2871215/` — 200，但内容为 CMAJ 短文，与主题无关，未采用。
- `https://api.semanticscholar.org/graph/v1/paper/DOI:10.1080/01638537709544497` — 404（DOI 不存在于该库）。
- `https://api.openalex.org/works?search=simplest systematics...` — 200，定位 1974 俄译重印。
- `https://api.openalex.org/works/W1980298376` — 200，1977 原摘要。
- `https://api.openalex.org/works?search=preference organization of conversational repair` — 200。
- `https://api.openalex.org/works?filter=title.search:third position repair` — 200，定位 ACL 2023。
- `https://api.openalex.org/works?search=Jefferson gap lapse conversational timing` — 200，定位 Hoey 2017、Meyer 2023。
- `https://api.openalex.org/works?search=Stivers response latency question conversation timing` — 200，定位 Stivers 2009。
- `https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/` — 200，全文已读。
- `https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2015.00731/full` — 200，但页面导航占满输出，正文被截断，未逐字引用；仅用书目信息。
- `https://journalofcognition.org/articles/10.5334/joc.268` — 200，全文已读。
- `https://aclanthology.org/2023.sigdial-1.52/` — 200，摘要页已读。
- `https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0316618` — 200，摘要与引言已读。
- `https://journals.uic.edu/ojs/index.php/dad/article/view/11388` — 200，摘要已读。
- `https://api.semanticscholar.org/graph/v1/paper/DOI:10.1353/lan.1977.0041` — 200，书目元数据。
- `https://www.conversationanalysis.org/` — 200，仅站点首页，无可用正文。
- `https://api.openalex.org/works?search=conversational repair other-initiated...` — 200，定位 Reddit 论文与 Mertens。

失败（明确记录，未当作证据）：

- Wikipedia `Turn-taking_in_conversation`、`Repair_(linguistics)`、`Conversation_analysis`、移动版与中文版：`web_fetch` TypeError / 直连超时。
- `https://r.jina.ai/...` 两次：TypeError（不可达）。
- `https://www.pnas.org/doi/10.1073/pnas.0903376106`：403 Cloudflare（且非目标论文）。
- `https://www.tandfonline.com/doi/abs/10.1080/01638537709544497`：403。
- `https://pmc.ncbi.nlm.nih.gov/articles/PMC2871215/`：200 但内容无关，未引用。
- `https://sociologica.hse.ru/...Sacks.pdf`（含 `?download=1`）：404。
- `https://content.openalex.org/works/W4241891521.grobid-xml` 与 `.pdf`：401（需 API key）。
- `https://muse.jhu.edu/article/453740`：503。
- `https://api.crossref.org/works/10.1080/01638537709544497`：404（DOI 号在本文任务卡中可能是旧号；OpenAlex 给出的 1977 DOI 是 `10.1353/lan.1977.0041`）。
- `https://doi.org/10.1353/lan.1974.0010`：跨域重定向未跟随；Cambridge 落地页未直接打开。
- `https://aclanthology.org/2023.sigdial-1.52.pdf`：web_fetch 拒绝 `application/pdf`。
- `https://api.semanticscholar.org/graph/v1/paper/search?...` 三次：429 限流。
- `https://api.core.ac.uk/v3/search/works`：429。
- `https://web.archive.org`、`https://www.google.com`、`https://www.lingsoc.ucsc.edu`、`https://www.ling.upenn.edu/~dengler/courses/ling001/turn-taking.html`、`https://www.ling.helsinki.fi/...`、`https://plato.stanford.edu/entries/conversation/`、`/entries/conversation-analysis/`、`https://www.cs.cmu.edu/~dbryant/files/CA-tutorial.pdf`：超时或 404/403。
- `https://www.degruyterbrill.com/document/doi/10.1515/opli-2016-0001/html`：HTTP 202 空内容。
- `https://www.research.ed.ac.uk/files/16403781/repair_revised.pdf`：403。
- `https://sci-hub.se/...`：不可达（未使用）。
