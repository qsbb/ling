# 日程 / 状态 / 梦境子系统：学术依据与工程参考

> 面向 ling 的架构设计者。先结论，后依据。**所有"建议权重"都是工程先验，不是论文给出的数值**——
> 文献只告诉我们"哪些变量有影响、方向如何"，从不给系数。
> 每条引用都带链接；打不开/查不到的写进 §6，不猜。

---

## §1 结论摘要（给设计者的 7 条硬结论）

1. **梦不该复述昨天的日程。** 只有 0.5% 的梦含完整情节记忆，但 >80% 含"自传体记忆碎片"（[Malinowski & Horton 2014](https://pubmed.ncbi.nlm.nih.gov/24635722/)）。正确做法是**拆成特征再重组**（人/地点/情绪/未了事），不是把日程原文喂进 prompt。
2. **情绪连续性是最稳的一条。** 日间压力与睡眠质量预测当晚梦的负性情绪（[Williams et al. 2026, N=130 多层模型](https://pubmed.ncbi.nlm.nih.gov/41986553/)）。情绪 -> 梦 这条边要显式建模，别只靠"人设语气"。
3. **威胁要出现，但要是少数、且大多失败。** 生物学威胁梦约 20%，其中成功逃脱约 1/5（[Malcolm-Smith et al. 2012](https://doi.org/10.1016/j.concog.2011.11.004)）。"梦里总被追且总逃掉"是错的；"追不上、醒不过来"才对。
4. **"REM = 做梦"是流行说法。** 药物抑制 81% REM 后，梦的报告长度/怪异度/愉悦度无实质变化（[Oudiette et al. 2012](https://pubmed.ncbi.nlm.nih.gov/22647346/)）。别把梦绑在睡眠分期上。
5. **日程用"三层 + 局部修复"，不要每晚重排全天。** 大纲 5–8 块 -> 小时级 -> 5–15 分钟级（[Park et al. 2023](https://arxiv.org/abs/2304.03442)）；临时改动只重排 `[now, 下一次锚点]`。多轮约束变更会让模型性能显著下降且对顺序敏感（[Flex-TravelPlanner, Oh et al. 2025](https://arxiv.org/abs/2506.04649)）。
6. **情绪状态用"OCC 做评估规则 + Russell 二维做状态 + 指数衰减"最省事**；三维 PAD 只在需要"主动权/掌控感"时才值得多加一维。
7. **饥饿用单一 `satiety 0–100` 连续变量 + 每小时递减**，判定"这顿饭满足了没有"交给模型即可——Humanoid Agents 就是这么做的，且已验证会改变活动分布（[Wang et al. 2023](https://arxiv.org/abs/2310.05418)）。

---

## §2 梦境

### 2.1 三套理论，各自能用在哪

| 理论 | 代表文献 | 对实现的直接含义 |
|---|---|---|
| **连续性假设**（梦反映清醒生活/担忧） | Domhoff 1996《Finding Meaning in Dreams: A Quantitative Approach》（Plenum；书目见 [Bulkeley & Kahan 2008](https://pubmed.ncbi.nlm.nih.gov/18801664/) 的引文，作者站点 <http://dreamresearch.net/>）；[Schredl & Hofmann 2003](https://pubmed.ncbi.nlm.nih.gov/12763010/)；[Schredl & Erlacher 2008](https://pubmed.ncbi.nlm.nih.gov/18589937/) | 梦的原料 = 最近 1–3 天的生活；**投入时间越多、情感卷入越深的主题，入梦率越高**（运动学生梦到运动、心理学生梦到阅读，且与清醒时长正相关） |
| **威胁模拟 TST** | [Revonsuo 2000, BBS 23(6)](https://doi.org/10.1017/s0140525x00004015)；[Valli & Revonsuo 2009 综述](https://pubmed.ncbi.nlm.nih.gov/19353929/)；**反证**见 [Malcolm-Smith 2012](https://doi.org/10.1016/j.concog.2011.11.004) | 可以给梦加"威胁演练"，但只能占少数场景，且**多数以失败/僵住收场**；"接近/趋近"梦比"回避"梦更常见 |
| **情绪调节 / 隔夜加工** | [Walker & van der Helm 2009, Psych Bull](https://pubmed.ncbi.nlm.nih.gov/19702380/) | 睡一觉应让高唤醒负性情绪的**强度下降**、不被删除。实现上：跨夜对 `affect_states.intensity` 打折，而不是清零 |

**记忆巩固 / TMR 该怎么用**：睡眠会按"重要性"筛选并重组记忆（[Stickgold & Walker 2013, Nat Neurosci](https://pubmed.ncbi.nlm.nih.gov/23354387/)）；在慢波睡眠给线索能定向增强特定记忆（[Rasch et al. 2007, Science](https://pubmed.ncbi.nlm.nih.gov/17347444/)；[Rudoy et al. 2009, Science](https://pubmed.ncbi.nlm.nih.gov/19965421/)）。
→ 工程含义：**"哪段记忆进入梦"应当由一个显式的打分函数决定**（类似 TMR 的线索选择），而不是随机抽一条。
→ 但 TMR 只在**慢波睡眠**有效，REM 或清醒时给线索无效（Rasch 2007 原文结论）——所以别把"梦境生成"和"记忆巩固"写成同一个动作。

### 2.2 影响因素：哪些有实证，哪些只是流行说法

| 因素 | 证据状态 | 说明 |
|---|---|---|
| 日间压力 / 当日情绪 | **有**（中等强度） | 多层模型：日间压力与睡眠质量的**个体内波动**预测梦的负性情绪（Williams et al. 2026） |
| 睡前经历 / 日间投入时长 | **有** | 连贯性假设的核心证据（Schredl & Erlacher 2008） |
| 身体刺激（肌肉/前庭/触觉） | **弱**（对照实验为阴性） | 24 人、三种刺激各配安慰对照、120 份报告：梦里普遍出现身体感觉，但**刺激组与安慰组无显著差异**，更像是情境与期待在起作用（[Peters et al. 2026, PLoS ONE](https://doi.org/10.1371/journal.pone.0358438)）→ 别做"饿了就梦到吃"的硬耦合，只能当低权重氛围色 |
| 外部线索（睡前/睡中提示词） | **有**（工程验证） | Dormio 在入睡期反复播词可直接改写梦内容（[Haar Horowitz et al. 2020](https://pubmed.ncbi.nlm.nih.gov/32480292/)） |
| 创伤/重大公共事件 | **有**（自然实验，但"部分支持"） | 9/11 后的梦报告确实与公共事件交织；同文也检验并部分削弱了"中央意象""媒体暴露"等流行解释（[Bulkeley & Kahan 2008](https://pubmed.ncbi.nlm.nih.gov/18801664/)） |
| 威胁密度 | **有争议** | TST 支持者称"强支持"，反对者实测约 20%；**别把威胁当默认基调** |
| 咖啡因 | **未证实** | 检索未找到针对梦内容/回忆的高质量对照研究（见 §6）；不要写进 prompt 当事实 |
| 酒精 | **未证实** | 同上 |
| 药物 | **只证实了"抑制 REM 不等于消除梦"** | Oudiette 2012（氯米帕明） |
| 睡眠分期（REM/非 REM） | **被削弱** | 同上：梦的报告在 REM 被抑制后仍在 |
| "梦能预演/解决问题" | **未证实** | 没有找到可复现的对照证据 |

### 2.3 可落地的影响变量表（建议初始权重，需用日志回放调）

入梦候选池 = 每条记忆/经历算一个 `salience`，取 top-k（k≈6–8）再加**强制重组算子**。权重合计 1.0：

| 变量 | 学术支持强度 | 建议权重 | 怎么从项目数据里取 |
|---|---|---|---|
| 最近 0–24h 的自我经历 | **强** | 0.30 | `self_experiences`，按 `occurred_at` 指数衰减（半衰期 ≈ 18h） |
| 当日情绪强度（尤其未消解的负面） | **强** | 0.25 | `affect_states`：`scope=window` 的当日峰值 × `intensity`；负性 ×1.3 |
| 未完成 / 被打断 / 未兑现的事 | **中**（TST 的核心预测，实测部分支持） | 0.20 | `activities.state ∈ {paused, failed}`；`commitments` 逾期未完成 |
| 人设核心（价值观、重要关系、口癖） | **中** | 0.15 | `personas` + `core_memories` + `relationship_states` 里 familiarity 最高的 1–2 人 |
| 身体状态（累/饿/冷） | **弱**（对照实验阴性，只当氛围） | 0.05 | `presence_states.energy` 低、`satiety` 低 → 只染"跑不动/很冷"这类底色，不直接生成"在吃东西" |
| 随机扰动 + 重组算子 | **弱**（梦的怪异来自重组，不是纯随机） | 0.05 | 固定种子；算子：换地点、换身份、时间倒错、物品拟人 |
| 前 2–3 天残余 | **弱** | —（不单列，含在上面的衰减里） | `self_experiences` 48–72h，salience 再乘 0.3 |

**三条来自论文的硬约束（比权重更重要）**：
1. **不复述**：每条入梦经历先抽成 `(人, 地点, 动作, 情绪)` 四元组，再跨条目重混（Malinowski & Horton 2014）。
2. **威胁占比封顶**：约 20–25% 的梦含威胁，其中"成功脱身"≤ 1/3，其余是"逃不掉/醒不过来"（Malcolm-Smith 2012；Valli & Revonsuo 2009）。
3. **跨夜情绪收敛**：生成梦的同时对当日 `affect_states` 做一次"睡眠结算"——强度 ×0.6，arousal 向中位回归（Walker & van der Helm 2009）。这一步让"昨天很气、今天还气但没那么气"成立。

**生成时机**：04:00 落在睡眠窗口内，此时生成的是**当天的大纲**而不是梦。建议把梦拆成独立任务，在 `wake_minute` 前 30–60 分钟跑；叙事必须落库为 `origin=simulated`、`visibility=actor_private`，**禁止**被当作 `self_experiences` 的事实来源（与项目已有的"计划不是事实"同一条纪律）。

### 2.4 近年"用 LLM 生成梦/叙事"的工程工作（少，别高估）

- **唯一直接对口的**：[Dreaming Is Not a Bug: A Jung-Inspired Dream Layer for Multi-Agent LLM Companions（V. Cheung, 2026, arXiv 预印本）](https://arxiv.org/abs/2601.06115)。做法是"离线放宽约束 + 高温采样"产生怪异叙事，配治理栈（抽象化、延迟、临时记忆）。**只有行为仿真，无人类被试评估**，当作设计灵感而非证据。
- 相邻但不做"生成"：[DreamLLM-3D（Liu et al. 2025）](https://arxiv.org/abs/2503.16439) 用 LLM 分析口述梦报告再转 3D 场景；[Bertolini et al. 2023](https://arxiv.org/abs/2302.14828) 用 LLM 给梦报告的情绪内容打分。
- **没有找到**："日程 -> 梦"的对照实验或基准。这一块是空白，权重只能靠自建评测（人工盲评"像不像同一个人的梦"）。

---

## §3 日程

### 3.1 作息先验：可直接当默认值的数

| 项 | 默认值 | 依据 / 状态 |
|---|---|---|
| 成人睡眠时长 | **7–9 小时**（中心 8h） | [National Sleep Foundation 建议（Hirshkowitz et al. 2015, Sleep Health）](https://doi.org/10.1016/j.sleh.2014.12.010) |
| 入睡 / 起床 | 23:00–24:00 / 07:00–08:00 | **工程默认值**，非统计推断；用 `actor_routines.wake_minute/rest_minute` 覆盖 |
| 三段主餐 | 早 08:00±1h / 午 12:00±1h / 晚 18:30±1h | **工程默认值**（时间日记类研究的常用中心，本文件未核到原文数字） |
| 各活动时长占比（睡/吃/通勤/休闲/工作） | 睡 8h / 吃 1.2h / 通勤 0.8h / 休闲 3–5h / 余下为工作 | **工程默认值，未经 ATUS/统计公报核对**（见 §6）；只当兜底，第一先验仍是 `actor_routines` |
| 时间轴形态 | 不固定、区间而非槽位 | 用户需求；`agenda_plans` 已有 `start_at/end_at`，天然支持 |

> 关键提醒：**这个角色的`actor_routines`才是第一先验**，人群统计只是兜底。
> 04:00 生成时必须把"04:00–起床"这一段固定为"睡觉"，不要让模型自己编。

### 3.2 Generative Agents 的三层结构，逐层对上你的需求

原文 §4.3（[Park et al. 2023, UIST](https://arxiv.org/abs/2304.03442)，DOI 10.1145/3586183.3606763）：

1. **日大纲**：给"人设 + 最近经历 + 昨天的小结"，让模型输出 **5–8 个块**（"8:00 起床洗漱 / 10:00 去学校上课 / 13:00–17:00 写曲子 / 17:30 晚饭 / 23:00 睡"）。
   → 对应你的**凌晨 4 点生成**。产出要写进 memory stream（项目里对应 `agenda_plans`）。
2. **小时级分解**：把每个块递归拆成整点条目（"13:00 先头脑风暴 / 14:00 记谱 / 16:00 休息"）。
3. **5–15 分钟级分解**：再递归一层（"16:00 抓点零食 / 16:05 绕工作区走一圈 / 16:50 收拾桌子"）。
   原文明确写 "This process can be adjusted to match the desired granularity"。
4. **反应与更新**（§4.3.1）：每个时间步"感知 -> 存记忆 -> 问模型：继续原计划还是反应？"。计划是**可被观测打破的对象**，不是脚本。
5. **反思**（§4.2）：把零散记忆合成高层结论，再喂回计划。

**和你需求的差异点（应该改进的地方）**：GA 是**一次性递归拆到底**，你今天就把明天的 5 分钟级细节全生成好，token 与"临场感"都吃亏。你的"8 点细化 9/10/11 点"是**按需提前 1 小时细化**——更省、也更像人（人不会提前 24h 想清楚 16:05 干什么）。建议：**只保留未来 2–3 小时已细化**，其余保持块级。

### 3.3 临时改动后的重规划：局部修复优于全量重生成

- **证据**：多轮"顺序引入约束"的任务里，模型单轮表现无法预测多轮适应能力，且**约束引入顺序显著影响结果**（[Flex-TravelPlanner, Oh et al. 2025](https://arxiv.org/abs/2506.04649)）；长程约束满足本身就不稳（[TravelPlanner, Xie et al. 2024](https://arxiv.org/abs/2402.01622)）。
- **所以：全量重生成 = 每次都赌一次长程约束**。工程上应做**局部重规划**：

```
输入事件（"去看电影吧"）
1) 硬门（代码判定，不问模型）：是否在睡眠窗口？是否与 confirmed commitment 冲突？
   是否超出当天 rest_minute？能量/饥饿是否够？（presence_states / satiety）
2) 模型只决定"去不去 + 去的话怎么改"，并只允许改 [now, 下一个不可移动锚点]
3) 保留原 plan_id，写 plan_revision+1 与 reason；旧条目置 superseded，不物理删除
4) 不可变项：已过去的段、已完成的 activity、commitment_level=confirmed、rest_minute 之后
5) 改完跑一遍程序化校验（不重叠、不越界、吃饭/睡觉窗口仍在），失败就退回局部版
```

- **生成 -> 自检 -> 修补**这两步有成熟做法可抄：Self-Refine（[Madaan et al. 2023](https://arxiv.org/abs/2303.17651)）做"同模型自我批评再改"；Reflexion（[Shinn et al. 2023](https://arxiv.org/abs/2303.11366)）把失败原因写进情景记忆供下轮用；机器人侧的在线重规划 RePLan（[Skreta et al. 2024](https://arxiv.org/abs/2401.04157)）用"感知反馈 -> 触发重规划"的开关，正好对应"临时事件 -> 触发重规划"。

---

## §4 状态建模

### 4.1 情绪：OCC 定"为什么"，Russell 二维定"是多少"

| 候选 | 文献 | 取舍 |
|---|---|---|
| **Russell 环形（valence × arousal）** | [Russell 1980, JPSP](https://doi.org/10.1037/h0077714) | **推荐做状态层**：两维连续、易衰减、易映射到"话多话少/语气冷热"，实现成本最低 |
| **PAD 三维** | Mehrabian & Russell 1974《An Approach to Environmental Psychology》（MIT Press 专著，无 DOI）；[Mehrabian 1996, Current Psychology](https://doi.org/10.1007/bf02686918) | 多出的 **dominance（掌控感）** 对"她敢不敢主动找你/敢不敢拒绝"有用；不需要就别加 |
| **OCC 认知评估** | [Ortony, Clore & Collins 1988](https://doi.org/10.1017/cbo9780511571299) | **推荐做事件映射层**：按"事件对目标有利/不利 + 谁造成 + 是否确定"决定产生哪种情绪。规则可枚举，不必让模型自由发挥 |

落地组合：`事件 -> OCC 规则（好处/坏处、归因、确定性）-> 目标情绪标签 -> 写入 PAD/Russell 两维 + 强度 -> 每小时衰减`。
项目里已有 `affect_states(emotion, intensity, decay_per_hour=0.15)`，**扩展成两维比换表更省**：`emotion` 标签保留（可解释、可审计），另加 `valence/arousal` 两个浮点作为连续状态。跨夜按 §2.3 第 3 条打折。

### 4.2 饥饿：单变量 + 离散步进，别上 Maslow

- 理论根：内稳态 / 驱力削减（Cannon 1932《The Wisdom of the Body》；Hull 1943《Principles of Behavior》——两本专著，无 DOI）——本质就是"偏离设定点 -> 产生驱力 -> 行为消除驱力"。
- **工程最优解**（有现成验证）：`satiety: 0–100`，每 `Δh` 递减（睡眠时减速），吃饭 `+30~40`；**"这顿饭算不算满足"交给模型判一句**。Humanoid Agents 正是如此：每个基本需求"有固定概率每步 -1"，模型判定某活动是否满足则 +1；把 health 初始化为 0 时，智能体花在"满足该需求"上的时间增加 156%，energy +56%，fullness +35%（[Wang et al. 2023](https://arxiv.org/abs/2310.05418)）——说明这套粗糙模型已经能驱动出可见的行为差异。
- **Maslow 只用来排序，不用来算值**：[Maslow 1943](https://doi.org/10.1037/h0054346) 的层级本身实证支持弱；Humanoid Agents 也只是引用它做"低层需求优先"的解释。
- 注意：`presence_states.energy/social_load` 已经是 0–100 的同构变量，**饥饿应并进同一套"每 Δh 结算一次"的机制**，不要另起一套定时器。

### 4.3 位置与天气

- **位置不要单独存**。当前位置 = 当前 `activities.place_id`（running 的那条），计划里的位置 = `agenda_plans.place_id`。两处真相会打架；项目已经因为"计划不是事实"踩过这类坑。
- **天气**：项目目前**没有天气实体**（`proactive/sources.py` 明确把"天气变化"排除在候选源外）。
  实证上天气对心情的效应**小且高度个体差异**（[Denissen et al. 2008, Emotion](https://doi.org/10.1037/a0013497)；[Klimstra et al. 2011, Emotion](https://doi.org/10.1037/a0024649)）。
  → 因此：**天气不要直接改情绪**；它只做两件事：①过滤/替换可选活动（下雨不散步）；②当对话素材。若一定要影响心情，系数压到 ±0.05 量级，并按人设给"怕冷/喜欢雨"之类的个体差异开关。

---

## §5 参考实现清单（读哪几个文件）

1. **Generative Agents** — <https://github.com/joonspk-research/generative_agents>
   读 `reverie/backend_server/persona/cognitive_modules/`：
   `plan.py`（41 KB，最重要：日计划 -> 小时 -> 5–15 分钟 + `_update_plan`/react 逻辑）、
   `retrieve.py`（recency/importance/relevance 三因子加权的检索）、
   `reflect.py`（反思何时触发）、`execute.py`、`perceive.py`、`converse.py`。
2. **Humanoid Agents** — <https://github.com/HumanoidAgents/HumanoidAgents>
   读"basic needs"更新与"活动是否满足需求"的判定：这是"饥饿感"最省事的参照实现；同时看它的 analytics dashboard（`needs` 随时间曲线正是你要的调试视图）。
3. **TravelPlanner / Flex-TravelPlanner** — 约束满足与多轮变更的评测集，借它的**校验器思路**（程序化检查可行性）而不是借它的 prompt。
4. **Sleep-time Compute**（[arXiv 2504.13171](https://arxiv.org/abs/2504.13171)）——"把重活放到空闲/夜间预计算"的工程范式，正好解释"凌晨 4 点批量生成"为什么合理。
5. **Dormio**（[论文](https://pubmed.ncbi.nlm.nih.gov/32480292/)；MIT Media Lab）——不是代码库，是"用线索影响梦内容"的实验协议，对你调"线索 -> 梦"的映射最有参考价值。
6. 项目内既有实现：`src/ling/domains/agenda/service.py`（软作息 + `MAX_LIVE_PLANS=4` 的克制）、`activity/service.py`（状态机 + 完成证据）、`world/service.py`（地点）、`affect/service.py`（情绪衰减）、`presence/load.py`（可恢复的负荷值）。

---

## §6 未证实 / 没找到（别当成已有结论）

1. **ATUS（美国时间利用调查）具体数字**：`bls.gov` 在本环境返回 403（Akamai 拒绝），未能核对 2023/2024 公报的睡眠/工作/用餐时长。引用入口：<https://www.bls.gov/tus/>。
2. **《全国时间利用调查公报》（国家统计局）**：`stats.gov.cn` 可访问但目标页 404（站点改版后路径失效），**未取到 2008/2018 的任何数字**。
3. **连续性假设的具体相关系数**：Schredl & Hofmann 2003 为付费全文，摘要只给"正相关、非零"，**未取到 r 值**。
4. **咖啡因 -> 梦**：PubMed/Crossref 检索未见针对梦内容或梦回忆的高质量对照研究。**按"无证据"处理**，不要写进 prompt 当事实。
5. **酒精 -> 梦**：同上，未取到可信实验。
6. **梦境各变量权重**：没有任何论文给出系数；§2.3 的权重是工程先验，必须用日志回放 + 人工盲评校准。
7. **"用日程 + 人设 + 情绪生成梦"的工程工作**：只找到 1 篇 arXiv 预印本（Cheung 2026）与 2 篇相邻工作，**没有对照实验、没有基准、没有开源实现**。
8. **"梦能解决问题/预演未来"**：流行的科普说法，未找到可复现证据。
9. **天气 -> 心情的强效应**：两次独立研究都指向"效应小、个体差异大"，"下雨就低落"属夸张。
10. **Maslow 层级的实证效度**：作为动机理论争议很大，本文件只把它当"优先级排序的俗名"使用。
