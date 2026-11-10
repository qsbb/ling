# 记忆系统重构设计：按人类记忆的工作方式重做召回、分层与睡眠压缩

调研日期：2026-11-11。本文回答用户三个问题：**「睡眠的时候应该要对记忆进行压缩」**、**「一轮对话不会每次都召回这么多条吧」**、**「看过的视频完整解析留存一小段时间，经过筛选走记忆入库流程」**。

方法：先查人类记忆的认知科学论文，再对照本项目实测数据（见 `docs/SLEEP-MEMORY-CONSOLIDATION.md`），最后给出可落地的改造方案。**每条设计决策都标注「人是这样的 → 所以我们这样做」**。论文出处见 §8，未核实的写「未证实」。

本文不重复 `docs/SLEEP-MEMORY-CONSOLIDATION.md` 已完成的缺陷清单与验收标准，而是**在其基础上补充认知科学依据**，并把「一轮召回几条」这个用户直接提问的问题给出定量答案。

---

## §0 结论先讲

1. **「一轮对话不会每次都召回 6 条」——用户说得对，而且现状问题比"条数多"更严重：不管用户说什么，都无条件跑一次检索，并且强制至少回 1 条。** `kernel/context.py:636` 写死 `limit=6`，`service.py` 里 `scored = kept or scored[:1]` 保证即使全部低于门槛也留一条。这跟人类记忆完全相反：人只在**线索对上**的时候才想起来（Tulving & Thomson 1973）。
2. **每轮该召回几条？建议平时 0–2 条，上限 4 条（不是 6）。** 依据是 Cowan (2001) 的「工作记忆容量约 4 个组块（chunk）」，而他明确反对 Miller (1956) 的 7±2 —— 因为 7±2 是**复述策略**下的容量，去掉复述后是 4。对话里真正能用上的记忆还得跟当前话题竞争注意力，所以实际可用只有 1–2 条。6 条超出人类瞬时容量。
3. **大部分轮次应该召回 0 条。** 寒暄、表情、闲扯、「在吗」不需要任何长期记忆。现在的实现保证至少 1 条，等于每轮都硬塞给她一段回忆，这正是"不像人"的根源之一。
4. **睡眠压缩该做什么：把「情节（verbatim，原话）」沉淀成「图式（gist，要点）」，而不是加摘要。** 依据 Brainerd & Reyna 的模糊痕迹理论（Fuzzy-Trace Theory）：人脑同时存 gist 和 verbatim，**verbatim 快速丢失、gist 长期保留**。所以压缩的产物必须是 gist，且不能顶替 verbatim 的证据地位。
5. **压缩可以调模型，但产物不能获得事实地位。** 用户已同意睡眠期调用模型。关键约束：模型生成的是 `gist`，只能作为**导航线索**，绝不能变成「用户事实」注入。这与本项目「`model_inference` 永不计为事实」的既有铁律一致。
6. **遗忘该是分级的，不是删除。** Ebbinghaus 遗忘曲线 + Wixted & Ebbesen (1991) 指出遗忘更接近**幂律**而非指数。落到工程上：**先降低回忆概率（软遗忘）→ 再归档（不可召回但仍可追溯）→ 最后才物理清除**。`livingmemory` 的三段式（expire → forget → purge）正是这个结构。
7. **候选池 bug 的根因是「用修改时间当唯一入口」。** `service.py:588-599` 的 `ORDER BY updated_at DESC LIMIT 500` 意味着一年没动过的重要记忆永远进不了池子。实测：500 条时能召回，501 条时 `hits=[]`。人的记忆不按"最后修改时间"检索，而是**多路并行 + 线索匹配**。

---

## §1 现状问题清单（带行号与实测）

| # | 问题 | 位置 | 实测/证据 |
|---|---|---|---|
| 1 | 每轮无条件召回，写死 6 条 | `kernel/context.py:636` | `limit=6` 是常量，与用户说了什么无关 |
| 2 | 强制至少回 1 条 | `memory/service.py`（`scored = kept or scored[:1]`） | 相对门槛形同虚设，永远不为空 |
| 3 | 门槛是「相对分」不是「绝对相关」 | 同上，`relative_score_floor=0.35` | RRF 分值约 `1/(60+rank)`≈0.016，乘 0.35 后几乎必然通过 |
| 4 | 候选池按 `updated_at DESC LIMIT 500` | `memory/service.py:298`、`:588-599` | **实测：500 条时召回成功；501 条时 `hits=[]`** |
| 5 | 被预算截断的不强化（已做对） | `context.py:648-665` | 只有 `selected` 才 `reinforce`，方向正确，可保留 |
| 6 | 只按字面 BM25 匹配（无 embedder 时） | `service.py:736-748` | 换个说法就召不回，"我妈妈忌日" vs "母亲去世那天" |
| 7 | 情节查询无 `ORDER BY` / 无 `LIMIT` | `service.py`（episode 查询） | 全表扫描，数据量大后性能与相关性都失控 |
| 8 | 情节不去重 | `docs/SLEEP-MEMORY-CONSOLIDATION.md` §2 第 4 条 | 真实库：258 条情节、43 条未归档、7 组重复、18 条冗余、最大组 8 条 |
| 9 | 睡眠整合零模型调用 | `memory/consolidate.py` | 只做确定性去重，无法生成 gist |
| 10 | 核心记忆真实库为空 | 同文档 §2 第 9 条 | `core_memories=0`、`knowledge_items=0` |

---

## §2 人是如何记忆的：论文依据

### 2.1 工作记忆容量：4 个组块，不是 7

**Cowan, N. (2001). The magical number 4 in short-term memory: A reconsideration of mental storage capacity. *Behavioral and Brain Sciences*, 24(1), 87–114.** DOI: [10.1017/S0140525X01003922](https://doi.org/10.1017/S0140525X01003922)

核心论点：Miller (1956) 的「7±2」测量的是**复述与组块后的**成绩；一旦用双任务等方式阻止复述，成年人的容量稳定在**约 4 个组块**。Cowan 因此提出"纯容量"约为 4。

> **人是这样的**：同一时刻能"放在心里"的东西大约 4 件，而且这 4 件还要跟当前正在做的事竞争。
>
> **所以我们这样做**：一轮对话注入的长期记忆**默认 0–2 条，硬上限 4 条**（现状 6 条）。这不是"少给点"的保守估计，而是对齐人类瞬时容量。

**重要限定（未证实部分）**：Cowan 的 4 是**短时记忆容量**，不是"对话中该注入几条记忆"的直接换算。从 4 到 2 需要一步推论：注入的记忆必须与当前话题竞争这 4 个位置，因此可用量应低于 4。这一步是工程判断，**不是论文的直接结论**，需要在真实回放集上校准。

### 2.2 提取是线索依赖的：想不起来是因为线索不对

**Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373.** DOI: [10.1037/h0020071](https://doi.org/10.1037/h0020071)

核心论点：记忆的可提取性取决于**编码时的情境与提取时的线索是否匹配**。同一段记忆，换了线索就可能完全想不起来——这不是"忘了"，是"没对上钥匙"。经典的编码特异性现象：给被试看词表时附带弱关联词，事后用那些弱关联词当线索，提取效果反而比用强关联词更好。

> **人是这样的**：不是"有记忆就会想起"，而是"线索对上才想起"。线索不对，再重要的记忆也沉在下面。
>
> **所以我们这样做**：
> 1. 放弃"每轮必须召回"的思路，允许 `hits=[]`（对应问题 1、2）。
> 2. 检索必须**多线索并行**：字面、语义、实体、时间、地点，任何一路对上就能进来（对应问题 6）。单一 BM25 就是"只有一把钥匙"。
> 3. 用户明确问"你还记得…吗"时，是**强线索**，可以放宽召回预算——但相关性与来源硬门不能松。

### 2.3 遗忘曲线是幂律，不是指数

**Ebbinghaus, H. (1885/1913). *Memory: A contribution to experimental psychology*.**（英译本 DOI: [10.1037/11304-034](https://doi.org/10.1037/11304-034)）

**Wixted, J. T., & Ebbesen, E. B. (1991). On the form of forgetting. *Psychological Science*, 2(6), 409–415.** DOI: [10.1111/j.1467-9280.1991.tb00175.x](https://doi.org/10.1111/j.1467-9280.1991.tb00175.x)

Ebbinghaus 发现遗忘先快后慢；Wixted & Ebbesen 进一步指出，用**幂函数**拟合遗忘数据比指数函数更好——意味着遗忘在长时间尺度上有"长尾"，不会指数级归零。

> **人是这样的**：刚发生的事忘得快，但很久以前的事不会"归零"，只是变得难想起来。
>
> **所以我们这样做**：遗忘做成**软衰减**——只降低回忆概率，**不删除、不归档**。真正的删除只由「用户要求」或「保留期到期」触发，与衰减完全分开。
>
> 佐证：`astrbot_plugin_memory_companion/core/memory_lifecycle.py:139-152` 的注释明确写着 `Soft decay may lower rank but never silently delete an otherwise active fact`，并给出分级半衰期（`HALF_LIFE_DAYS`，`:24-30`）：ephemeral 1 天、short 14 天、normal 120 天、durable 730 天、pinned 永不。

### 2.4 间隔重复优于集中重复

**Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380.** DOI: [10.1037/0033-2909.132.3.354](https://doi.org/10.1037/0033-2909.132.3.354)

核心论点：把同样的学习总量分散到多次、有间隔的复习，长期保持效果显著优于集中一次（间隔效应）。

> **人是这样的**：反复在**不同时间点**被想起的事记得牢；一次被强调很多遍没用。
>
> **所以我们这样做**：强化分必须**按"被想起的次数 × 时间分散度"算**，而不是按"这一轮被塞进去几次"算。本项目已有 `access_count` 与 `last_injected_at`，方向对；要补的是：**同一轮重复引用同一条不重复加分**，且强化要随时间衰减（避免"一次性爆红"永久保送）。

### 2.5 睡眠的作用：把情节从海马体转移到新皮层

**McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457.** DOI: [10.1037/0033-295X.102.3.419](https://doi.org/10.1037/0033-295X.102.3.419)

**Walker, M. P., & Stickgold, R. (2004). Sleep-dependent learning and memory consolidation. *Neuron*, 44(1), 121–133.** DOI: [10.1016/j.neuron.2004.08.031](https://doi.org/10.1016/j.neuron.2004.08.031)

核心论点（互补学习系统理论）：海马体快速学**具体情节**，新皮层慢速学**统计规律**。睡眠期海马体把情节"重放"给新皮层，新皮层据此提取共性、形成图式。

> **人是这样的**：白天存具体的事，睡着后把事"过一遍"，提炼出规律，具体细节慢慢淡掉但要点留下。
>
> **所以我们这样做**：睡眠压缩的产物是**图式（gist）**，不是"更短的原文摘要"。情节（verbatim）保留但降低召回优先级，gist 承担日常召回。这与 §2.6 的模糊痕迹理论一致。

### 2.6 压缩要压成 gist，不是压成更短的 verbatim

**Brainerd, C. J., & Reyna, V. F. (1990). Gist is the grist: Fuzzy-trace theory and the new intuitionism. *Developmental Review*, 10(1), 3–47.** DOI: [10.1016/0273-2297(90)90003-M](https://doi.org/10.1016/0273-2297(90)90003-M)

**Reyna, V. F., & Brainerd, C. J. (1995). Fuzzy-trace theory: An interim synthesis. *Learning and Individual Differences*, 7(1), 1–75.** DOI: [10.1016/1041-6080(95)90031-4](https://doi.org/10.1016/1041-6080(95)90031-4)

核心论点：人记忆时**并行存储两条痕迹**——verbatim（精确细节）与 gist（要点/意义）。两者**独立遗忘**：verbatim 丢失快，gist 保留久。人做判断时主要依赖 gist。

> **人是这样的**：记得"他妈去世了，他很难过"（gist），但记不清那天说了哪几句话（verbatim 丢了）。
>
> **所以我们这样做**：
> - 压缩**不是**把原文改短，而是**生成一条不同层级的记录**：gist 记录（新行），原情节保留。
> - gist 可以模糊（"用户对母亲去世这件事很在意"），verbatim 必须精确（"2026-03-15 他说明天是母亲忌日"）。
> - **回答具体事实（日期、姓名、地址）必须用 verbatim，不能用 gist。** 用 gist 回答细节就是编造。

**过度概括的风险**：gist 的副作用是把个案误升为通则。必须约束：gist 里不能出现**未在原情节中出现过的具体值**（日期、人名、地名）。

### 2.7 线索与提取的边界

编码特异性（§2.2）还有一个推论：**相似的线索会互相干扰**。工程对应就是 MMR 去重——同一事件的多份摘要不该同时占位。本项目已有 `max_similarity=0.7` 的 MMR，但**只作用于断言，情节是追加的**（`service.py` 中 `_mmr_filter` 只处理 assertions）。这是 §4 要修的。

---

## §3 「一轮召回几条」——给用户的正面回答

### 3.1 答案

| 场景 | 建议条数 | 依据 |
|---|---|---|
| 寒暄、表情、闲扯、贴图 | **0** | 无线索 → 无提取（Tulving & Thomson 1973） |
| 普通闲聊、情绪陪伴 | **0–1** | 工作记忆 4 组块中大部分被当前话题占用（Cowan 2001） |
| 话题引用了共同经历 | **1–2** | 线索匹配 + 容量约束 |
| 用户明确问「你还记得…」 | **最多 4** | 强线索可放宽，但不超过容量上限（Cowan 2001） |
| 用户问「最近一周都聊了什么」 | **4–6，且走时间线而非相关性检索** | 这类查询要的是**汇总**，不是**联想** |

**硬上限 4**（现状 6）：再多的记忆也无法被同时"放在心里"，只会稀释当前话题的注意力。

### 3.2 为什么不是「每次都召回一点更安全」

一种直觉是"多给一点总没坏处"。这不成立，有三个具体代价：

1. **注意力稀释**：有限的上下文预算被旧事占用，当前话题的处理质量下降。
2. **错误注入概率上升**：每条注入都是一次"她可能会说出来"的机会；召回 6 条里 1 条不相关，就有编造风险。`MEMORY-OSS-RESEARCH.md` §4-Q2 已指出这点。
3. **强化马太效应**：被注入越多，得分越高，越容易被再次注入。虽然 `context.py:648-665` 已把强化限制在 `selected` 上，但只要池子倾向固定，长尾仍出不来。

### 3.3 「按需召回」的判定规则（不调模型）

`docs/ATTENTION-DESIGN.md` §8 已定下铁律：**不为判断"看不看"而每条消息调模型**。本设计遵守该约束，用确定性信号判定：

**必须先过（任一不满足 → 召回 0 条）**
- 本轮允许长期记忆（现有 `snapshot.allows_long_term_memory()`）
- 用户文本长度 ≥ 4 个字符（纯表情/单字不检索）

**加分信号（累计达到阈值才检索）**
- 含第一人称/第二人称代词（我、你、咱）+1
- 含时间词（上次、以前、那天、还记得）+2
- 含疑问结构（吗、还记得、哪、什么时候）+1
- 与历史话题有实词重叠 ≥ 2 个且非常用词 +2
- 文本长度 ≥ 20 字 +1

**阈值 **：得分 ≥ 2 才检索。这是一组**待校准的超参**，不是论文结论；应由中文对话回放集上的误注入率/漏注入率共同确定（见 §7 验收）。

**注意反例**：不要用"疑问词"做唯一判据。参考项目 `astrbot_plugin_memory_companion/core/injection.py:91` 的提示语明确写着 `不要依赖固定疑问词判断用户意图`。

---

## §4 候选池 bug：多路并行取候选

### 4.1 根因

`service.py:588-599`：

```python
select(MemoryRow).where(
    MemoryRow.subject_id.in_(list(subjects)),
    MemoryRow.retracted.is_(False),
    MemoryRow.archived_at.is_(None),
).order_by(MemoryRow.updated_at.desc(), MemoryRow.id.asc()).limit(RECALL_CANDIDATE_LIMIT)
```

`RECALL_CANDIDATE_LIMIT = 500`。**唯一入口是最新更新的 500 条**。一条一年前写入、之后再没动过、但对当前对话极其重要的记忆，只要头上有 500 条更新的记录，就永远进不了候选池——**不是排序不好，是根本没被看见**。

实测已确认：500 条时召回成功，加到 501 条时 `hits=[]`。

### 4.2 方案：多路并行 + 固定配额 + 并集去重

```
候选 = UNION(
    A. 最近更新          最多 200  （保持现状的"新鲜"倾向）
    B. 最近实际注入/高热度 最多 150  （按 last_injected_at + access_count，解决"老而重要"）
    C. 字面/语义命中       最多 150  （BM25 / 向量，解决"语义相关"）
    D. 槽位/实体精确命中   最多 100  （结构化事实，解决"名字/地址"）
)
再对并集做去重、硬门过滤、排序、MMR
```

**为什么必须"有界但有固定配额"而不是"直接不限量"**：
- 不限量 → 每次检索全表扫，延迟随数据量线性增长，不可接受。
- 只提高总上限（比如 500→5000）→ 只是把断崖推后，问题依旧。
- 固定配额分路 → 每一路都保证自己的代表能进池，**没有任何一路会被其它路挤掉**。

这就是 SLEEP-MEMORY-CONSOLIDATION.md §5 第 39 条要求的方案，本文补充其**认知科学依据**：人的提取是多线索并行的（Tulving & Thomson 1973），不是"只在最近的记忆里找"。

**B 路的正确性前提（必须注意）**：`TimestampMixin.onupdate` 会让 `reinforce` 顺带更新 `updated_at`，所以"强化不改更新时间"这个前提**是假的**（SLEEP-MEMORY-CONSOLIDATION.md §5 第 38 条已指出）。要么给 `MemoryRow` 加一个不被 `onupdate` 影响的 `last_injected_at` 专用索引路，要么显式把 `updated_at` 排除在 `onupdate` 之外。**推荐前者**——`updated_at` 的语义就是"内容最后修改时间"，不该被读操作污染。

### 4.3 绝对相关性门（允许零命中）

现状：`scored = kept or scored[:1]` —— 强制至少 1 条。

改为：

```python
eligible = (
    强字面命中            # BM25 超过绝对阈值
    or 向量相似度 >= 绝对阈值
    or 重排分 >= 绝对阈值
    or 槽位/实体精确命中
)
hits = [h for h in ranked if eligible(h)][:limit]   # 可以返回 []
```

**关键**：RRF 分数**不能**直接当绝对相关度用（它只是名次融合，值域 ~0.016），必须保留原始信号分。这与 `MEMORY-OSS-RESEARCH.md` §4-Q2 规则 1、规则 2 一致。

**阈值怎么定**：不能拍脑袋写常数，要用**带人工标注的中文正/负例**分别对断言和情节校准，同时看误注入率和漏注入率。

---

## §5 分层：短期 / 中期 / 长期

### 5.1 三层设计

| 层 | 存什么 | 何时用 | 生命周期 | 依据 |
|---|---|---|---|---|
| **工作记忆**（当前窗口） | 最近若干轮原文 | 每轮必带 | 窗口内 | 人是这样的：正在说的话当然记得（Cowan 2001 的"焦点"） |
| **短期情节**（verbatim） | 具体事件、原话 | 线索匹配时才召回 | 数天~数周，软衰减 | Ebbinghaus 遗忘曲线：细节先淡 |
| **长期图式**（gist）+ **稳定事实** | 要点、偏好、关系 | 日常召回的主力 | 长期 | Fuzzy-trace theory：gist 保留久 |
| **核心记忆**（常驻） | 称呼、禁忌、边界 | **每轮常驻，不靠检索** | 直到被修正 | 现有 `memory/core.py` 的设计理由：边界性事实不能靠概率检索 |

### 5.2 何时用哪层

- **日常对话**：核心记忆（常驻）+ 长期图式（按需 0–2 条）。
- **用户问细节**（"我上次说的那个日期是几号"）：必须下探到**短期情节**的 verbatim，且要标来源；不要用 gist 回答。
- **用户问长期状态**（"我喜欢吃什么"）：优先长期图式/稳定事实；gist 与事实冲突时以**有来源的事实**为准。
- **用户问"最近"**：走时间线查询，不是相关性检索。

### 5.3 与现有代码的对应

- 工作记忆 = `HISTORY_TOKEN_BUDGET = 20000` 的历史车道（`context.py`）。
- 短期情节 = `MemoryEpisode`。
- 长期图式 = **新增**（`consolidate.py` 目前不产生 gist）。
- 稳定事实 = `MemoryRow` 的 assertion。
- 核心记忆 = `CoreMemoryRow`（`MAX_PER_SUBJECT = 20`，`DEFAULT_CHAR_BUDGET = 800`）。

**注意实际状态**：真实库里 `core_memories=0`、`knowledge_items=0`（SLEEP-MEMORY-CONSOLIDATION.md §2 第 9 条）。也就是说"核心记忆常驻"这条**目前没有数据支撑**，设计上要保证它为空时系统正常工作。

---

## §6 睡眠期压缩：压什么、压成什么、怎么不丢关键信息

### 6.1 用户已同意调用模型

现状 `consolidate.py` 刻意零模型调用，理由是"压缩产物会顶替原始记忆参与召回"。用户已明确同意睡眠期可以调模型，因此本设计**允许调用**，但用严格边界消除当初的担忧：

### 6.2 压什么

**只压这些**（其余不动）：
- 同一事件的多份重复情节（真实库有 7 组重复、18 条冗余，最大组 8 条）
- 已在保留期外的低价值情节（与 `MEMORY-OSS-RESEARCH.md` 的旧 `compact_low_value` 条件一致：`value` 非空、槽位非空、龄 ≥60 天、importance <0.25、≥3 行且 ≥2 个值）
- 一段时间内同一主题的零散情节 → 一条主题 gist

**绝不压这些**：
- 用户明确纠正过的事实
- 有未决冲突的槽位（标"待确认"，不合并）
- 边界/禁忌类核心记忆
- 仍有 `valid_until` 未到期的现行事实

### 6.3 压成什么

一条 **gist 记录**，字段要求：

| 字段 | 要求 |
|---|---|
| `origin` | `derived_summary`（现有约定） |
| `fact_eligible` | **`false`** —— 永远不能当用户事实 |
| `source_event_ids` | 必须完整指向原情节；**不得截断**（现有 `compact_low_value` 只保留前 40 个，这是缺陷） |
| `valid_until` | 与源集合中最早的到期时间一致 |
| `audience`/`visibility` | 必须与所有源一致；**跨受众的源不得合并**（见 §6.5） |
| 内容 | 只允许 gist，**不得引入源中不存在的具体值**（日期、人名、地名、数字） |

**产物不能顶替 verbatim**：原情节保留，只是降低召回优先级。这样"gist 说错了"永远可以回溯原文。

### 6.4 怎么调模型而不失控

| 约束 | 值 | 理由 |
|---|---|---|
| 角色 | `memory.extract` | 复用既有 role，不新增权限 |
| 频次 | ≤ 1 次/主体/天 | 配合 `sleep_date` 唯一约束，天然幂等 |
| 输入上限 | ≤ 2000 token | 控制成本与延迟 |
| 输出上限 | ≤ 300 token | gist 就该短 |
| 失败处理 | 保持原状，**不写入半成品** | 与"拿不到就留空，绝不编造"一致 |
| 校验 | 出稿前 + 发布事务内**各验证一次** | 任一来源失效则不发布 |

**prompt 必须禁止的事**：不许推断用户未说过的偏好；不许把角色的猜测写成用户的陈述；不许合并不同人的信息；不许输出原文中不存在的具体值。

### 6.5 受众边界（最容易出事的地方）

`livingmemory` 的 `consolidation_manager.py:124-138` 有一段注释值得直接引用：

> 语义聚类可能跨会话：合并前必须按 (session_id, persona_id) 拆分，否则合并记忆要么无作用域（任何会话都召回不到），要么把 A 会话的内容写进 B 会话的作用域。

`livingmemory` 的做法是 `_split_by_scope()`（`:146-152`），按 `(session, persona, adapter)` 分桶后再合并。

**本项目对应规则**：合并前必须按**全部受众边界**分桶（私聊/群、可见性、主体），跨桶绝不合并。这与 `consolidate.py` 现有"按所有 audience 边界分组"一致，必须保留。

### 6.6 幂等与可重跑

- `sleep_date` 唯一约束保证同一天不重复压缩（现有设计）。
- 增加**规则版本号**：压缩算法变更后，旧产物标记为旧版本，可重跑。
- 失败后重跑**同一批输入**，不得产生第二份 gist（参考 Cognee 的管道可重跑理念，见 `MEMORY-OSS-RESEARCH.md` §3）。

### 6.7 不复活旧值

压缩**绝不能**把已 supersede 的旧值写回 gist。规则：
- 只用 `valid_until > now` 且未被 `superseded_by` 指向的现行事实。
- 若源集合里有已撤回项 → 该组跳过合并，记拒绝原因。
- 这与 `MEMORY-OSS-RESEARCH.md` §4-Q4 规则 4 一致。

---

## §7 可复用点：参考项目具体做了什么

### 7.1 `astrbot_plugin_memory_companion`（本地：`/Users/lingxi/project/forks/astrbot_plugin_memory_companion`）

| 可复用点 | 出处 | 为什么值得学 | 本项目怎么用 |
|---|---|---|---|
| **注入条数默认 4，不是 6** | `_conf_schema.json:713-718`，`top_k` 默认 `4` | 与 Cowan 4 组块一致；本项目现状 6 超了 | 硬上限降到 4 |
| **按字符预算动态决定条数** | `core/injection.py:66-71`，`max_chars=1800` 时进精简模式（`compact_for_budget`） | 不是"取 N 条再裁"，而是"预算决定形态" | 预算不足时先降级单条表达，再减少条数 |
| **单条内容上限** | `_conf_schema.json:720-726`，`max_item_chars` 默认 `320` | 单条过长会挤掉其它条 | 给 `evidence` 车道加单条上限 |
| **时间聚合查询放宽预算** | `_conf_schema.json:727-733`，`temporal_aggregate_max_chars` 默认 `3600` | 对应本文 §3.1「问最近一周」场景 | 时间线查询走独立预算 |
| **默认不注入原始事件** | `_conf_schema.json:734-740`，`include_raw_events` 默认 `false` | 避免流水账污染 | 情节默认不进提示词，只进 gist |
| **三级表达状态过滤** | `core/injection.py:698-712`，`uncertain` 一律不注入；`candidate` 且规则画像不注入 | 把"不确定的"挡在门外 | 对应现有 `confidence` 分组，保留并加强 |
| **分层半衰期** | `core/memory_lifecycle.py:24-30`，`HALF_LIFE_DAYS` 五档 | 不同耐久度不同衰减 | 引入到 `recency` 计算 |
| **软衰减不删数据** | `core/memory_lifecycle.py:139-142` 注释 | 衰减≠删除 | 本文 §2.3 的核心原则 |
| **失败不放宽 = 硬门在前** | `core/injection.py:637-646` docstring：`Retrieval is not the only producer of injection candidates` | 最后一道同步闸门覆盖快路径 | 注入前统一过一遍门 |

**它没有的东西（不要误以为有）**：在 `core/` 下搜 `睡眠|sleep|consolidat|夜间` 只命中 `compact`（注入精简）与"睡眠中查岗"文案，**没有睡眠期记忆整合**。

### 7.2 `livingmemory` = `lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory`

已核实仓库：<https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory>（master，438★，描述「一个拥有动态生命周期的智能长期记忆插件」）。**注意**：GitHub 上有多个同名项目（`mitmedialab/livingmemory` 等），从本项目插件的 `core/migration_livingmemory.py` 引用 `documents` 表可确认对应的是 AstrBot 生态这个。

| 可复用点 | 出处 | 为什么值得学 | 本项目怎么用 |
|---|---|---|---|
| **三段式遗忘** | `core/managers/atom_lifecycle_manager.py:71-79`：`expire_stale_atoms` → `forget_expired_atoms`（软删除，移出 FTS 保留元数据）→ `cleanup_forgotten`（物理清除） | 完全对齐 Ebbinghaus/幂律遗忘：先失联、再模糊、最后消失 | 记忆分三级状态，别一步删 |
| **压缩有明确候选条件** | `core/managers/consolidation_manager.py:100-103`：`max_importance` 默认 0.5 + `min_age_days` 默认 7 | 只压又老又不重要的 | 与现有 `compact_low_value` 条件对齐 |
| **每次最多处理 N 组** | `core/managers/consolidation_manager.py:71`，`max_groups_per_run` 默认 5 | 防止一次睡眠跑爆 | 睡眠任务加批次上限 |
| **原记忆保留策略可配** | `core/managers/consolidation_manager.py:72,86`，`keep_original` 默认 `archive` | 压缩不等于删除 | 默认归档不删 |
| **受众分桶后再合并** | `core/managers/consolidation_manager.py:117-127,135-152`，`_split_by_scope` + 注释 | 防跨会话泄漏 | 必须照做（§6.5） |
| **按需召回有预算** | `core/tools/agentic_recall_budget.py:17-19`：`max_calls=4`、20 秒、12000 字符 | 一次请求内多次检索要限总量 | 我们不主动多次调，但预算思路可用于"放宽召回"场景 |
| **后台反思并发上限** | `core/event_handler_modules/memory_reflection.py:27`，`_MAX_CONCURRENT_REFLECTIONS = 2` | 防多会话同时爆发调模型 | 睡眠任务并发限 2 |
| **长空闲补做** | 同文件 `:31`，`_IDLE_SUMMARY_GAP_SECONDS = 60*60` | 漏掉的总结靠空闲补 | 睡眠任务错过要能补跑 |
| **历史回滚取消在途任务** | 同文件 `cancel_session_storage_task` docstring | 任务持有的范围已失效就该取消 | 现有 purge 网关类似，可加强 |

---

## §8 论文出处汇总

| 主题 | 文献 | DOI/链接 | 核实方式 |
|---|---|---|---|
| 工作记忆容量 4 | Cowan (2001), *Behavioral and Brain Sciences* 24(1), 87–114 | [10.1017/S0140525X01003922](https://doi.org/10.1017/S0140525X01003922) | Crossref |
| 7±2（被质疑的旧说） | Miller (1956), *Psychological Review* 63(2), 81–97 | [10.1037/h0043158](https://doi.org/10.1037/h0043158) | Crossref |
| 编码特异性 | Tulving & Thomson (1973), *Psychological Review* 80(5), 352–373 | [10.1037/h0020071](https://doi.org/10.1037/h0020071) | Crossref |
| 遗忘曲线 | Ebbinghaus (1885/1913) | [10.1037/11304-034](https://doi.org/10.1037/11304-034) | Crossref |
| 遗忘是幂律 | Wixted & Ebbesen (1991), *Psychological Science* 2(6), 409–415 | [10.1111/j.1467-9280.1991.tb00175.x](https://doi.org/10.1111/j.1467-9280.1991.tb00175.x) | Crossref |
| 间隔重复 | Cepeda et al. (2006), *Psychological Bulletin* 132(3), 354–380 | [10.1037/0033-2909.132.3.354](https://doi.org/10.1037/0033-2909.132.3.354) | Crossref |
| 互补学习系统 | McClelland, McNaughton & O'Reilly (1995), *Psychological Review* 102(3), 419–457 | [10.1037/0033-295X.102.3.419](https://doi.org/10.1037/0033-295X.102.3.419) | Crossref |
| 睡眠依赖巩固 | Walker & Stickgold (2004), *Neuron* 44(1), 121–133 | [10.1016/j.neuron.2004.08.031](https://doi.org/10.1016/j.neuron.2004.08.031) | Crossref |
| gist vs verbatim | Brainerd & Reyna (1990), *Developmental Review* 10(1), 3–47 | [10.1016/0273-2297(90)90003-M](https://doi.org/10.1016/0273-2297(90)90003-M) | Crossref |
| 模糊痕迹理论综述 | Reyna & Brainerd (1995), *Learning and Individual Differences* 7(1), 1–75 | [10.1016/1041-6080(95)90031-4](https://doi.org/10.1016/1041-6080(95)90031-4) | Crossref |

**说明**：以上均通过 Crossref API 核对了标题、第一作者、年份、期刊、卷期页码与 DOI。**未读原文全文**——本文引用的是这些文献的**核心结论**（在认知心理学界有共识地位），不是对具体实验数据的复述。需要精确实验参数（如 Cowan 的具体容量估计区间）时应查原文，本文不代为断言。

**未证实的部分**：
- Cowan 的"4"是短时记忆容量，**不等于**"对话应注入 4 条"。从 4 到"每轮 0–2 条"是工程推论，需实测校准。
- 睡眠期 LLM 生成 gist 的**质量**没有任何论文保证；模糊痕迹理论描述的是人脑机制，不是"用 LLM 压缩就能得到 gist"。必须人工抽检。

---

## §9 分阶段落地

| 阶段 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **P0** | 去掉"强制至少 1 条"；加绝对相关性门，允许 `hits=[]` | 低 | 寒暄、无关话题、过期话题均零注入 |
| **P1** | 召回条数 6 → 4，加"按需召回"确定性判定 | 低 | 线上抽样：普通闲聊注入率 <30% |
| **P2** | 候选池多路并行 + 固定配额 | 中 | 构造第 501 条之后的旧偏好，验证能召回 |
| **P3** | 睡眠 gist 压缩（调模型），带全部安全约束 | 中高 | 人工抽检 gist 不含源中没有的具体值 |
| **P4** | 分级遗忘（软衰减 → 归档 → 清除） | 中 | 衰减后仍可被强线索召回 |

**必须先做 P0**：它是所有后续工作的前提——如果"永远至少回 1 条"还在，P2 的多路候选池只会让更多不相关内容进来。

---

## §10 风险与不该做的事

1. **不要为了"能召回"去改 `updated_at`。** 它是内容修改时间，被读操作污染后语义就废了。要用就新加字段。
2. **不要让 gist 获得事实地位。** 一旦 gist 能回答具体日期/姓名，编造就发生了。`fact_eligible=false` 是硬约束。
3. **不要截断 `source_event_ids`。** 现有实现截到 40 个；截断后无法证明遗漏的来源仍有效。超限应拆分或多存关联表。
4. **不要跨受众合并。** 群聊与私聊的记忆合并是泄漏事故，不是优化。
5. **不要用固定疑问词判意图。** 参考项目已踩过这个坑。
6. **不要为"看不看"每条消息调模型。** `ATTENTION-DESIGN.md:332-345` 已明令禁止。
7. **不要踩领域边界白名单。** `tests/unit/test_domain_boundaries.py` 有白名单；memory 域不得直接 import media/affect/conversation 域。睡眠任务需要媒体或情绪数据时，走 ports 或事件，不要直接依赖。
8. **不要指望压缩一定成功。** 零模型调用的确定性整合必须**永远保留**作为降级路径；模型压缩失败时回落到它。
9. **不要一次改完。** 见 §9，P0 单独上线观察。

---

## §11 待办与未决

- 绝对相关性门的**具体阈值**未定：需要中文正/负例标注集，本文不给拍脑袋常数。
- "按需召回"的**信号权重**未定：同上。
- Cowan 4 组块 → 每轮 0–2 条 的**映射**是推论，需回放集验证。
- 睡眠 gist 压缩的**prompt** 需要单独设计并做人工抽检。
- 本文未覆盖：视频解析留存与记忆入库流程（见 `docs/VIDEO-UNDERSTANDING-RESEARCH.md`）；晚回兜底文案（见 `docs/design/DEFERRED-FALLBACK-DESIGN.md`）。
