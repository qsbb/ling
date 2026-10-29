# 群聊轮询注意力设计（v1）

> 触发：用户 2026-10-28——「群聊可以以一定频率轮询（模拟人），如果"设定"她
> 目前在看这个群聊，轮询频率加快（实时聊天但又不是每条都触发 llm 节省成本），
> 当被 @ 时看当前的群聊消息上下文以及是否等待消息补充」。
>
> 本文结论来自实测与设计复核，两者结论高度一致，取重合部分。
> **所有时间都是经验初值，需实测。**

---

## 0. 总体判断

把两件事**分开但耦合**：

```
注意力调度（何时看）  ←→  批量决策（看什么、调用几次）
```

现在只有"看什么"（`AutonomyTurnService`，只接一条 text），没有"何时看"。

关键洞见：
> 延迟的是**补全文本**，不是 `@` 事件本身。

以及：
> 「@ 不延后」指立即入队/可回执，**不等于**每条立即调 LLM。

---

## 1. 已实现的基础（本轮之前）

- 普通群消息（未被 @、非紧急）落库 `attention_state='unseen'`，返回
  `decision="deferred_attention"`，**不进决策、不发消息**
- 被 @ / 叫名字 / 引用 / 紧急内容仍立刻处理
- `ConversationService.unseen_messages()` / `mark_seen(显式 id 列表)`
- 偏索引 `ix_messages_unseen (window_id, sequence) WHERE attention_state='unseen' AND role='user'`
  ——**已用 EXPLAIN 验证**走 `Index Scan`，不再是"扫窗口再过滤"

**但没有调度器**去刷这些未读。本轮补它。

---

## 2. 架构

```
                ┌─ 全局 tick（单一定时器）──────────────────┐
                │  取 next_due_at 最小堆里到期的窗口        │
                │  （不是每群一个定时器——群一多就爆炸）      │
                └──────────────┬───────────────────────────┘
                               ▼
                    租约到期窗口（SKIP LOCKED）
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
        @ 即时高优先路径                 普通未读批次
        mention_pending                   unseen ≤20 条
        等 8s / 硬截止 12s                     │
                │                              │
                └──────────────┬───────────────┘
                               ▼
                  拼成一段有上限的文本
                  （≤20 条 或 ≤4k token）
                               │
                               ▼
              **一次** choose（每窗口每轮最多 1 次 LLM）
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
          不回：标 seen+ignored          回：标 seen+replied
          （游标推进）                    最多发一条（群恒 1 段）
```

**失败不推进游标、不标已处理**——未读留在队列里下次再试。

---

## 3. 数据模型

```sql
-- 调度游标 + 聚焦租约（一张表即可）
CREATE TABLE attention_cursor (
  window_id       TEXT PRIMARY KEY,
  next_due_at     TIMESTAMPTZ NOT NULL,
  last_sequence   BIGINT NOT NULL DEFAULT 0,   -- 已处理到的消息序
  lease_until     TIMESTAMPTZ,                 -- 防多 worker 重复
  focused_until   TIMESTAMPTZ,                 -- 聚焦租约到期
  mention_deadline TIMESTAMPTZ,                -- @ 等待补正文的硬截止
  updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_attention_cursor_due ON attention_cursor (next_due_at);
```

**为什么单独一张表**：`messages.attention_state` 是**消息状态**，
`attention_cursor` 是**调度状态**。混在一起要么每次轮询扫全部未读来推时间，
要么把"下次什么时候看"写进消息行——两者都会把简单查询变成全表更新。

**游标只优化调度，不替代消息状态**：`seen` 仍然逐条写在 `messages` 上，
因为"她看见了哪几条"必须是可审计的事实。

---

## 4. 轮询节律

| 参数 | 值（经验初值，需实测） |
| --- | --- |
| 基础间隔 | **30 秒** |
| 聚焦间隔 | **8 秒** |
| 抖动 | ±20%（防止多群同拍） |
| 全局并发 | **1–2** 个窗口 |

**不是每群一个定时器**：一个全局 tick + 按 `next_due_at` 的最小堆。
多群只增加队列长度，不增加定时器数量。

`resting` / 深夜：**停轮询**，未读留在队列里。恢复后按"是否已过时"过滤
（见下），不补发。

---

## 5. 「她正在看这个群」= 带 TTL 的聚焦租约

**是状态，不是事件。** 一次轮询只是"看了一眼"，聚焦是"这段时间在盯着"。

| 项 | 规则 |
| --- | --- |
| 进入 | 显式设定 / 刚回复过 / 被 @ |
| 退出 | 手动 / TTL **15 分钟** / 进入 `resting` / 连续 **5 分钟**无新消息 |
| 同时聚焦 | **最多 1 个主 + 1 个次** |
| 私聊 | 不占群聚焦槽，走独立即时链路 |
| 存储 | DB（`focused_until`），内存只做调度缓存 |

### 聚焦加速为什么不会退化成「每条都调 LLM」

**关键机制：聚焦时仍有每窗口最小 LLM 间隔（15–30 秒，取 15 秒）**。

也就是说聚焦只让**轮询**变快（8 秒一次），但**决策**仍受冷却约束。
20 秒内来了 10 条消息 → 可能轮询 2 次，但只有 1 次真正调模型。

这条是成本控制的核心，不能省。

---

## 6. @ 之后等补充正文（**已实现**）

`@` **不进普通 `unseen` 队列**，走即时高优先路径写 `mention_pending`。

| 项 | 规则 |
| --- | --- |
| 等待窗口 | **8 秒** |
| 硬截止 | **12 秒**（不可再延） |
| 正文判定 | 同群 + 同发送者 + 相隔 ≤8s；**或** reply/quote 指向该 `@` |
| 多次 @ | 合并成一个 pending；**只允许延长到硬截止一次** |
| 超时无正文 | 就按"只有 @"决策一次（不是无限等） |
| 明确命令/问句 | **立即决策**，不等 |

**为什么 8/12 秒**：足够接住"先 @ 再打字"的常见节奏，又不至于让 @ 变成
半分钟的沉默。**经验初值，需实测。**

---

## 7. 成本控制

| 约束 | 值 |
| --- | --- |
| 每窗口每轮 LLM 调用 | **最多 1 次** |
| 全局并发 | **≤2** |
| 群发言预算 | 仍受现有 3 次/5 分钟约束，**模型无权推翻** |
| 拼进 prompt 的量 | ≤20 条 或 ≤4k token |
| 超出时 | 保留**首 2 条 + 末 12 条**，其余只给计数 |

**为什么保留首尾**：最早的常是"第一次说的重要事实"，最后的接当前话题；
只留尾部会丢掉最初的约定（这是本项目 `_rollup_summary` 已经踩过的坑）。

### 如何证明省了调用（必须可观测）

```
baseline_calls  同一条消息轨迹下"每条一次调用"的影子基线
actual_calls    实际调用数
saving = 1 - actual_calls / baseline_calls
```

再加：`polls`（轮询次数）、`empty_polls`（无未读的轮询）、`batch_size`、
`merged_mentions`、`decision_latency`。

**验收方式**：同一条轨迹做两次回放（一次逐条基线、一次批量），比 `actual_calls`。
不靠"感觉省了"。

### "不值得说话"记什么

- 整批无回应 → 全部 `seen + ignored`
- 有回应 → 被回的那条 `replied`，其余 `seen + ignored`

**先决策成功再标 `seen`**。顺序反了会在失败时凭空"看过"。

---

## 8. 性能

### 8.1 轮询本身

- 先**批量**租约到期窗口（一条 SQL + `SKIP LOCKED`）
- 再用**一条** SQL 按窗口取未读（`LATERAL` / `row_number()` + 每窗上限）
- **禁止**每轮 `count(*)`、全表扫描、逐行提交
- 最后批量更新状态（一次事务）

### 8.2 偏索引够不够

够当前检索。**再加** `attention_cursor(next_due_at)` + `SKIP LOCKED` 做租约。
不需要 Redis——2 个群、188 个窗口的规模上用 Redis 是纯粹的复杂度。

### 8.3 向量检索升级路径（本条经实测）

**现状**：`memory_embeddings.vector` 存 **JSON**，未装 pgvector，
余弦在 Python 里对候选集算。代码注释自己写着「个人规模够用；上万条时应改用 pgvector」。

**当前规模**：messages 915 / memory_embeddings 63 / memories 21 —— 没有慢查询。

**升级顺序**（重要：不是直接换）：
1. 先抽象 `VectorStore` 接口，启动时**探测** pgvector 是否可用
2. 未装扩展 → 走现有 JSON 路径，并**限制候选集**
3. 装扩展后：加 nullable `vector` 列 → 分批回填 → **双写** → 抽样比较两者结果一致 → 开关切换
4. 保留 JSON fallback，出问题能切回

**触发迁移的判断标准**（不是"以后再说"）：
- 基准 `k=20、并发 4` 时**连续两天 p95 > 100ms**，或
- Python 余弦计算 CPU 占比 **> 20%**，或
- 向量行数 **> 1 万且日增 > 500**

**现在不做**：63 条向量时上 pgvector 是纯粹的过早优化。

### 8.4 悬崖排序（由近到远）

| # | 悬崖 | 现状 | 何时爆发 |
| --- | --- | --- | --- |
| 1 | **请求/事务内同步生成 embedding** | 存在 | 慢 provider 会阻塞写事务 |
| 2 | **无上限的 prompt 拼接** | 已部分有 token 预算 | 历史一长就线性涨 |
| 3 | **JSON + Python 余弦** | 63 行 | ~5k 行开始明显 |
| 4 | **ORM N+1 与逐条提交** | 部分 | 批量路径变多后放大 |
| 5 | **无 TTL/归档导致表与索引膨胀** | 有 archived_at | 累积 |

### 8.5 现在就该做 vs 现在不该做

**现在做**：调度状态表、游标/租约、批量 SQL、prompt 上限、调用与延迟指标、
`EXPLAIN` 检查、**embedding 改异步 outbox**（把 #1 悬崖挪远）。

**现在不做**：Redis / Kafka、分库分表、未达阈值上 pgvector、
每群独立定时器、重写整套记忆层。

---

## 9. 最小可行第一步：`GroupAttentionScheduler`（**已实现**）

只做这一条纵向切片：

```
取到期窗口 → 加租约 → 批量读 unseen（≤20）
  → 拼一段文本 → **调用一次** choose
  → 成功后一次事务写 seen / handled_as
  → 最多发一条（群恒 1 段）
```

### 验收标准（已达成）

1. **无新消息连续 100 轮 → LLM 调用数 = 0**
2. **同窗口 20 条待处理消息 → 调用 1 次**（不是 20 次）
3. 双 worker 不重复处理同一窗口（租约 + `SKIP LOCKED` 验证）
4. 失败不推进游标、不标 `seen`——未读仍在队列
5. **调用下降率可复核**：同一轨迹回放两次，构造 20 条都需要模型判断的消息，
   `actual_calls` 从 20 降到 1；网关 stub 与指标都能对账

第 5 条是这轮的关键——**没有它就无法证明"省了成本"，只能声称**。

---

## 10. 不该做

- 不要把 `scene` 当成队列或唯一持久状态
- 不要全群固定扫描、每条消息或每个 @ 调 LLM
- 不要让 @ 无限等待正文
- 不要在 63 条向量时上 pgvector / Redis
- 不要在**决策成功之前**把未读标为已处理
- 不要每群一个定时器
- 不要让模型绕过现有的群发言预算
- 不要为了"看起来高性能"引入没测过的组件

---

## 11. 两版设计的差异

| 项 | 第一份 | 第二份 | 本文取值 |
| --- | --- | --- | --- |
| 聚焦间隔 | 5–10 秒 | 8 秒 | **8 秒** |
| 聚焦时 LLM 冷却 | 20–30 秒 | 15 秒 | **15 秒** |
| @ 等待 | 8–12 秒 | 8 秒 / 硬截止 12 秒 | **8 / 12 秒** |
| 聚焦槽位 | 1 群，上限 2 | 1 主 + 1 次 | **1 主 + 1 次** |

差异都在经验参数上，不影响架构。**全部需真实补测。**


### 实现结果

| 组件 | 落点 |
| --- | --- |
| 调度游标 | `attention_cursor`（迁移 0035）：`next_due_at` / `last_sequence` / `lease_until` / `focused_until` / `last_call_at` |
| 调度服务 | `domains/attention/service.py::GroupAttentionScheduler` |
| worker | `attention.tick`（影子模式：只决策不发送），接进主循环与收集器同节奏 |
| 参数 | 基础 30s / 聚焦 8s / 最小 LLM 间隔 15s / 批 ≤20 / 租约 60s / 聚焦 TTL 15min |

**验收逐条**：

| 验收项 | 证据 |
| --- | --- |
| 1. 无未读 → 0 调用 | `test_empty_window_costs_zero_calls` |
| 2. 20 条 → 1 次调用 | `test_twenty_messages_cost_one_call`（另断言 `saved_calls == 19`） |
| 3. 双 worker 不重复 | `test_lease_prevents_double_processing` + `test_lease_expires_so_window_can_be_retried` |
| 4. 失败不标 seen、不推进游标 | `test_failure_does_not_mark_seen` |
| 5. 下降率可| 见下方真实库实测 |

**真实库实测**（非测试环境）：

```
种 20 条未读 → 轮询结果：未读=20 决策调用=1 逐条基线=20 省=19 决定=ignored
chooser 实际被调用次数 = 1
再刷一次 → 未读=0 调用=0
```

**一处必须说清的成本事实**：`llm_calls` 只统计**判断要不要说**的调用。
"说什么"的生成是另一次调用，发生在决定要回之后。
所以省下来的是**决策次数**，不是总调用数——把两者混成一个数字，
"省了成本"就变成一句无法核对的空话。这一点写进了 `PollResult` 的注释。

**一个实现中踩到的坑**：冷却最初用 `updated_at` 判断，而游标刚创建时它等于现在，
于是**第一次轮询被自己的冷却挡住**。加了独立的 `last_call_at`（NULL = 从没调过）。

**负例实测**：去掉失败保护 → 红；去掉冷却 → 红；不限制批量 → 红；去掉租约 → 红。


---

## 12. @ 等补充正文实现结果

### 复用轮询器，不另造机制

「等待」= **把首次轮询推迟 8 秒**。补充的正文是同一个窗口的又一条 `unseen`，
自然落进同一批，被一次决策一起看到。不需要另建 pending 队列。

| 项 | 实现 |
| --- | --- |
| 存储 | `attention_cursor.mention_deadline`（已有）+ `next_due_at` + `focused_until` |
| 调度 | `GroupAttentionScheduler.await_supplement(window_id)` |
| 等待 | `MENTION_WAIT_SECONDS = 8`，硬截止 `MENTION_HARD_DEADLINE_SECONDS = 12` |
| 多次 @ | **只在第一次设硬截止**，之后最多把 `next_due_at` 推到该截止 |
| 只有 @ 无正文 | 决策时给一句 `MENTION_ONLY_HINT`，而不是空字符串 |

### 途中挖到一个真 bug

`connectors/onebot/connector.py` 里：

```python
parts=([InputPart(text=parsed.text)] if parsed.text else [])
```

只有 `[CQ:at]` 没有正文时 `parts=[]`，而契约要求 `parts` 至少一个片段——
**这条消息在校验层就被拒了，根本到不了 bridge**。

也就是说「先 @ 再发正文」这个场景在线上**压根收不到**：用户看到的是
"@ 了她却毫无反应"。改成永远带一个 text 段（哪怕是空串）。

负例 3 复现了这个错误：把连接器改回 `if parsed.text else []`，
测试直接抛 `ValidationError`。

### 顺序上的一个坑

`addressed`（是不是冲她说的）必须**在"有没有可读文本"之前**算。
原来的顺序是"没文本 → 直接拒"，而只有 @ 的消息正是"没文本"。
先按没文本拒掉，就永远等不到补充。

**负例实测**：不等补充 → 红；多次 @ 无限延长 → 红；
连接器改回不给空 text 段 → 红（`ValidationError`）。


---

## 13. 向量预计算实现结果

对应第 8.4 节悬崖 #1「请求/事务内同步生成 embedding」。

### 悬崖的确切位置

`MemoryService._ensure_embeddings_for` 在**召回路径**里补向量，而它要调外部
embedding API。代码注释原本写着"懒补齐而不是写入时同步向量化：写入路径不该被
外部 API 拖住"——但它把阻塞从**写**挪到了**读**，而读发生在用户等回复的时候。
积压 500 条 = 一次召回 500 次串行 HTTP。

### 两道防线

| 层 | 机制 |
| --- | --- |
| 止血 | `MAX_LAZY_EMBEDS = 8`：一次召回最多同步补 8 条，超出留后台 |
| 治本 | worker `embedding.backfill`：后台预计算，让积压根本不存在 |

backfill 的细节：
- 断言用 `text`、情节用 `summary`——与召回侧保持一致，否则补出来的向量和
  查询向量不在同一语义空间
- 幂等：已存在 `(memory_id, model_id)` 跳过；DB 复合主键是第二道保险
- `batch` 参数控制每次补多少，默认 32

**负例实测**：去掉懒补齐上限 → 红；不跳过已存在向量 → 红
（被复合主键以 `IntegrityError` 兜住，属于纵深防御）。

### 一个诚实的前提

**这台机器当前没有配置 embedding 凭证**（`LING_EMBEDDING_*` 全未设置），
所以召回一直走 BM25+RRF 字面路径，`build_embedding()` 返回 None，
悬崖**目前是潜在的而非正在发生**。库里那 63 条向量是以前配置过时留下的。

在真实库上跑 backfill 会如实输出「向量补算跳过：未配置 embedding 提供方」
——不报错、不写空向量。

改动在启用 embedding 后才起作用。现在做的理由是：**等配了才发现慢就晚了**，
而且上限这道阀必须在悬崖激活之前就在。


---

## 14. 影子数据

轮询目前是**影子模式**：只决策、不发送。为了让"她的判断像不像人"可查，
加了 `attention_poll_log`（迁移 0036）+ `GET /v1/attention/stats`。

**只记每次轮询的结论与成本，不存消息正文**——统计不需要正文，
存了等于把私聊内容多复制一份到审计表。

### 用真实模型（真实供应商）跑的四组

| 场景 | 条数 | 调用 | 决定 | 她给的理由 |
| --- | --- | --- | --- | --- |
| 群里闲聊没指向她 | 3 | 1 | ignored | 群聊未点名且非问句，他人对话无需插话 |
| 有人提到她的名字 | 2 | 1 | ignored | 在聊别人的思路且没点名我，不必插话 |
| 有人在吐槽 | 2 | 1 | ignored | 群聊里别人抱怨没点名，懒得插话 |
| 有人问了具体问题 | 1 | 1 | ignored | 群聊未点名且非本人相关，无需插话 |

```
polls 4 / unseen 8 / llm_calls 4 / baseline 8 / saved 4
by_decision: {ignored: 4}
```

### 这个数据的价值：它暴露了真问题

**4/4 全部 ignored，理由全是"没点名我"。**

这说明群聊里她把"没 @ 我"直接等同于"不该说话"。作为省钱的默认值这是对的
（用户原话："人不是整天都看着群"），但作为**活人**就不对了——真人会在群里
主动接话、吐槽、凑热闹。

值得注意：第 2 组「ling 上次那个思路挺有意思」也判成"没点名我"，
是因为该批消息是直接种进 `unseen` 的；走 bridge 时这类消息
`addressed=True`，根本不会进 `unseen`。所以这是**测试数据构造的偏差**，
不是线上行为。真实线上不会出现"点了名却被当成没点名"。

### 顺带修掉的一个真 bug

worker 里构造 `DecisionContext` 时**硬编码** `recent_actor_turns=0`，
于是 L0 防刷屏上限（`recent_actor_turns >= group_turn_budget`）在轮询路径上
**永远不触发**——热门群她能每 15 秒回一次，正是这条上限要拦的事。

修法：把 `DecisionContext` 的构造收进 `domains/attention/service.py::
make_group_chooser`，由 `recent_actor_turns()` 从库里真算。收进领域层就不会
再被某个调用点漏掉。

负例：把 `recent_actor_turns` 改回写死 0 → `test_polling_respects_group_budget` 红。

### 下一步该调的

群聊"全 ignored"不是 bug，是**倾向太保守**。两个方向：

1. **调提示词/人设**（推荐）：让她知道"在群里可以主动接话、可以只回半句、
   可以只对某一条感兴趣"，而不是把"没点名"当成一票否决
2. 调参数：给群聊一个非零的主动概率

方向 1 属于**人设**，不是代码写死的规则——符合本项目"行为归人设"的原则。


---

## 15. 群聊倾向：行为归人设，且人设真的生效了

上一节的影子数据暴露「4/4 全 ignored，理由全是没点名我」。查下去发现两个问题。

### 问题一：提示词里写死了行为规定

`CHOICE_PROMPT` 末尾原来有：

> 如果消息是别人之间的对话、广告、或已经有其他人回答了，**通常不该插话**。

这把一种性格（谨慎、不抢话）写死进了代码。它还把三件不同的事混成一条：
广告（该忽略）／别人已有回答（**这是选择**）／别人之间的对话（**爱说话的人恰恰会插嘴**）。

已删除。并加了一条闸测试 `test_choice_prompt_has_no_behavioural_rules`：
提示词里出现「通常不该 / 不该插话 / 必须回复」这类词就红。

### 问题二：决策时根本没用人设

`AutonomyTurnService` 决定"要不要开口"时只有 `CHOICE_PROMPT` + 处境，
**不知道她是谁**。已加 `persona_reader`，把人设（`system_prompt` + `boundaries`）
拼进**同一段** system prompt——模型对单条 system 的遵循度明显高于"再来一条"。

读不到人设就返回空串，**不编默认性格**：编了没人会想到去改，而它会一直生效。

### 关键：删规则 + 注入人设，行为**没有变**

同一批四组消息重跑，仍然全 ignored。说明不是代码在拦她，是**人设里压根没写
她在群里什么样**。

### 判别实验（决定性）

同一批消息、同一模型，只换人设：

| 人设 | 决定 | 理由 |
| --- | --- | --- |
| 现有人设（没提群聊） | 不回 | 没点名我，也不清楚哪个版本，群聊不必每条都接 |
| 假设「群聊里话多」 | **回** | 在群里看到开会顺手接话，凑个热闹 |
| 假设「群聊里安静」 | 不回 | 群聊没点名，我基本只看不说话 |

**结论**：代码不再强制她沉默，行为已经完全由人设决定——
这正是设计意图：这类判断交给人格，而不是写死在代码里。

### 已知限制：默认人设没有群聊自我描述

`DEFAULT_SYSTEM_PROMPT` 里没有任何关于"她在群里是什么样"的内容，
因此默认行为是保守沉默。补这一段属于人设内容，由使用者按自己的角色定义：

- 爱搭话／只看不说／偶尔冒一句
- 会不会主动吐槽
- 对不感兴趣的话题会不会直接无视


---

## 16. 调度间隔：设计是 8 秒，实际是 5 分钟

注意这个容易踩的坑：`attention.tick` 如果被放进 **300 秒**的收集器定时块里，
实际轮询间隔就会变成 5 分钟，而不是轮询器自己声明的**基础 30 秒 / 聚焦 8 秒**。

后果：`due_windows` 每次被调用时所有游标都已逾期 4 分半，
**「聚焦 8 秒」永远不会发生**——那些数字只是纸面参数，功能形同不存在。

这类错误的特点是：代码全对、测试全绿、日志无异常，只有实际跑起来才暴露。

### 修法

- `ATTENTION_TICK_SECONDS = 5.0`：独立定时器，必须小于轮询器自己的间隔
- 顺手把四件共用 `try` 的事拆开：原来收集器一炸，后面的意图队列、
  注意力轮询、向量补算全跳过，而它们彼此无关。加 `_guarded` 逐个隔离

### 真实运行验证

种 1 条未读 → 跑 worker **55 秒**：

```
轮询记录条数 = 1
  ignored | 1 条 | 调用 1 次 | 群聊没点名，只是通知，不必特意回应。
消息状态: attention=seen handled=ignored
```

修之前这要等最多 5 分钟。

### 钉成不变式的测试

`test_attention_tick_is_faster_than_poll_intervals` 断言
`ATTENTION_TICK_SECONDS < BASE_INTERVAL_SECONDS` 且 `< FOCUS_INTERVAL_SECONDS`。

这是个很容易再犯的错：**加一个新 handler 时顺手塞进已有的定时块**。
测试就是防这个。

另加 `test_guarded_isolates_failures`：前一个后台任务失败后，
后面的必须继续执行——后台任务之间不该有隐式依赖。

负例：把间隔改回 300 → 红；`_guarded` 不隔离异常 → 红。


---

## 17. 跨群调度：先看谁

之前 `due_windows` 只按 `next_due_at` 排序。问题是所有窗口的 `next_due_at`
会在同一时刻被设成"现在 + 间隔"，于是它们**永远同拍到期**，
每轮一起挤进来，先看谁全看数据库返回顺序——那不是策略，是噪音。

### 优先级（高到低）

1. **正在看的群**（`focused_until` 未过期）——她此刻就盯着这个群
2. **有 @ 待处理的**（`mention_deadline` 未过期）——有人在等回应
3. 其余按 `next_due_at` 先后（最久没看的先看）

SQL 用 `.desc().nulls_last()`：Postgres 里 `DESC` 默认把 NULL 排最前，
不显式写会把"没聚焦"的排到"聚焦中"前面——正好相反。

### 抖动：确定性，不是随机

`JITTER_SPAN_SECONDS = 6`，每个窗口一个固定偏移：

```python
zlib.crc32(window_id) % (JITTER_SPAN_SECONDS + 1)
```

**用窗口 id 的固定偏移而不是随机数**：同一窗口每次偏移一致，行为可预测、
可测试。随机数会让"为什么先看这个群"变成无法复现的事。

### 单次 tick 上限

`MAX_WINDOWS_PER_TICK = 5`。单次 tick 里串行打太多次模型调用会拖慢整个循环，
而下一次 tick 5 秒后就到。

### 一个测试隔离的坑

这些用例都在同一批窗口上建游标，而 `due_windows` 是全局查询——
不清游标的话，上一个用例留下的聚焦窗口会排到前面，
`order[0] == 期望窗口` 就会指向错误的窗口。加了 autouse fixture 清空游标，
并把断言从 `order[0] == x` 改成比较**相对顺序** `index(a) < index(b)`。

另一个：`await_supplement` 会把 `next_due_at` 推到 8 秒后（等补充），
所以"@ 优先"的测试必须查 `now + 10s`，查 `now` 时它本来就不该到期。

**负例实测**：去掉聚焦排序 → 红；抖动改随机 → 红；不封顶单 tick 窗口数 → 红。


---

## 18. 「错过了」：第三个注意力状态

### 为什么必须有第三态

之前只有 `unseen` / `seen`。于是这两件事在数据上**完全一样**：

- 她忙了一下午，压根没机会看
- 她看到了，想了想，决定不回

而这正是用户反复强调要能分开的两件事（"有时候会在忙/休息，没空回消息"）。

现在三态：

| 状态 | 含义 | 对应 `handled_as` |
| --- | --- | --- |
| `unseen` | 还没轮到看 | `NULL` |
| `seen` | 看过（进过决策） | `replied` / `ignored` |
| `missed` | **错过了**：一直没机会看，已过时效 | `missed` |

### 顺带解决一个更实际的问题

原来的行为：她忙一下午，轮询最终还是会把那条 5 小时前的「下午三点开会」
捞出来回一句。**晚到的回复比没有回复更像故障。**

现在：`RELEVANCE_TTL_MINUTES = 360`。超过 6 小时的群聊普通消息：
- 不进模型（**省一次调用**）
- 标 `missed` + `handled_as=missed`
- 轮询记录里 `decision="missed"`

混合批次只丢弃过期的那部分，新鲜的照常判断——不能因为混着就整批放弃。

私聊不走这条（私聊的沉默由主链路决定）。

### 为什么不建 ticket 表

设计文档里写过 `attention_ticket`。实际实现时发现不需要额外表：
**时效可以从消息自身的 `created_at` 推导**，而"她有过几次机会看"这件事
在只有 2 个群、5 秒一次 tick 的规模上没有决策价值。

建一张表去记录"可能看一眼"，然后每次轮询都要维护它，收益抵不上复杂度。
这是有意的简化，不是漏做。

### 一条保守的默认

读不到 `created_at` 时**当作新鲜**，不当作过期。

宁可多看一眼，也不要因为读不到时间戳就把消息判成错过——那等于静默吞掉
她的消息，而"重要内容不能被静默吞掉"是这个项目的硬约束。

**负例实测**：不判时效 → 红；无时间戳当过期 → 红。


---

## 19. 两个躺了很久的 handler

审计 `HANDLERS` 里每个 topic 的**派发源**，查出两个注册了但没人调用的：

| handler | 后果 |
| --- | --- |
| `media.describe` | `MediaDescriber` **从未被实例化**，整条媒体描述链路是死的 |
| `self.timeline` | `self_view` 会**读**她的时间线，但没有任何东西**写**它，读到的永远是空 |

后者特别隐蔽：读路径工作正常、测试全绿，只是数据永远是空的。

### 修法：不是补两个 if，而是把定时任务收进注册表

```python
PERIODIC_JOBS: tuple[PeriodicJob, ...] = (
    PeriodicJob("attention.tick",       handle_attention_tick,       {"shadow": True}, ATTENTION_TICK_SECONDS),
    PeriodicJob("embedding.backfill",   handle_embedding_backfill,   {},               EMBEDDING_BACKFILL_SECONDS),
    PeriodicJob("intent.tick",          handle_intent_tick,          {},               PROACTIVE_COLLECT_SECONDS),
    PeriodicJob("life.tick",            handle_life_tick,            {},               LIFE_TICK_SECONDS),
    PeriodicJob("media.describe",       handle_media_describe,       {},               MEDIA_DESCRIBE_SECONDS),
    PeriodicJob("proactive.collect",    handle_proactive_collect,    {"shadow": True}, PROACTIVE_COLLECT_SECONDS),
    PeriodicJob("self.timeline",        handle_self_timeline,        {},               SELF_TIMELINE_SECONDS),
)
OUTBOX_TOPICS = frozenset({...})   # 事件驱动的那批
```

主循环遍历注册表，每个 job 各自 `_guarded`。
**加一个 handler 就必须在两边之一登记**，否则闸红——"注册了但没人派发"在结构上就不可能。

### 闸（`tests/unit/test_worker_dispatch.py`）

1. 每个 `HANDLERS` topic 必须在 `PERIODIC_JOBS` 或 `OUTBOX_TOPICS` 里
2. 反过来：声明的 topic 必须真的注册了 handler
3. `PeriodicJob.run` 必须是对应 topic **真正注册的那个函数**（写错成别的函数时，
   调度器会勤快地调用一个不相干的东西——表面"跑了"，实际什么都没做）
4. `OUTBOX_TOPICS` 每个都必须真的有人在入队
5. 周期必须是正数且 ≤24 小时
6. `_should_run_maintenance(0, ...)` 必须首次立即触发（不然新任务要等一个完整周期）

### 真实运行验证

跑 `run_forever` 12 秒，记录实际派发：

```
attention.tick        3 次   （5 秒间隔，正确）
embedding.backfill    1 次
intent.tick           1 次
life.tick             1 次
media.describe        1 次   ← 原本从不执行
proactive.collect     1 次
self.timeline         1 次   ← 原本从不执行
未触发：（无，全部触发）
```

**负例实测**：删掉 `media.describe` 登记 → 红；`run` 指向错误函数 → 红；
加一个没人入队的 outbox topic → 红。

### 一个审计方法的教训

第一版审计脚本用 `handle_x(` 判断"有没有被调用"，结果把 `def handle_x(` 的
定义也算成调用——12 个 handler 全部假绿。第二版又漏掉了
`_guarded("名字", handle_x, ...)` 这种**传函数引用**的派发方式，
于是刚修好的 `attention.tick` 被误报成孤儿。

**审计脚本本身要能被审计。** 所以最后没停在脚本上，而是把规则变成
显式的注册表 + 断言——结构比正则可靠。


---

## 20. 设置页里 5 个绑定项设了不生效

审计 `ModelRole` 的每个成员到底有没有被请求，查出**13 个角色里 5 个是幽灵**：

| 角色 | 情况 |
| --- | --- |
| `reflection` / `planning` / `moderation.advisory` | 至今**没有任何代码请求** |
| `memory.embed` / `memory.rerank` | 能力**存在**，但走环境变量（`LING_EMBEDDING_*` / `LING_RERANK_*`），完全不经过模型绑定 |

用户看到的是一份"可选职责"清单，其中五分之二绑上去什么都不会发生。
**这不是崩溃，是承诺了不存在的能力**——和契约里的幻影字段同一类。

### 两类原因必须分开写

- 「**尚无实现**」：能力压根没有 → 后续动作是写实现
- 「**由环境变量配置**」：能力有，但配置入口不在这儿 → 后续动作是统一配置入口

混成一句"暂未支持"会让后来人分不清该做哪件事。所以
`RESERVED_PROFILES` 的值必须说明属于哪一类（有测试钉住）。

### 修法

- `known_profiles` 只列**绑定后真的会生效**的 8 个
- 新增 `reserved_profiles`，把 5 个保留角色和原因一起返回——**不静默删除**，
  用户能看到"为什么这里没有它"
- `KNOWN_PROFILES`（校验用）仍接受它们，兼容已存在的草稿

真实接口验证：

```
可绑定角色: ['dialogue.fast', 'dialogue.deep', 'dialogue.group',
             'perception.vision', 'speech.asr', 'speech.tts',
             'realtime.voice', 'memory.extract']
保留角色:
  reflection         尚无实现：目前没有任何代码请求这个角色
  planning           尚无实现：目前没有任何代码请求这个角色
  moderation.advisory 尚无实现：目前没有任何代码请求这个角色
  memory.embed       由环境变量配置（LING_EMBEDDING_*），不走模型绑定
  memory.rerank      由环境变量配置（LING_RERANK_*），不走模型绑定
```

### 顺带删掉一个死枚举

`TurnDecision.defer` —— 声明了，但**没有任何代码产出或消费它**。
而"延后"已经有活的实现：`deferred_by_floor`（发言权仲裁）和
`deferred_attention`（注意力层）。三个名字表示同一个概念，迟早有人用错
那个不生效的。

### 闸（`tests/unit/test_model_roles.py`）

1. 每个 `ModelRole` 要么真的被请求，要么在 `RESERVED_PROFILES` 里
2. 保留原因必须说清是"没实现"还是"配置在别处"
3. 保留名单里不能有拼错的角色名（拼错就永远匹配不上 = 静默失效）
4. **已在代码里被请求的角色不能还挂在保留名单**（功能做了但用户看不到）
5. 展示清单与保留名单不能重叠
6. `TurnDecision` 不能有死成员

**负例实测**：删掉 `planning` 的保留声明 → 红；把已实现的 `dialogue.fast`
误标为保留 → 红；把 `defer` 加回枚举 → 红。
