# 行为日志口径（`ling.behavior`）

本文是 `ling.audit.behavior_event` 的**口径契约**：什么算行为日志、哪些事能进、
哪些事绝对不进、动作名怎么起。代码在 `src/ling/audit.py:174`。

一句话：行为日志回答「**她经历了什么、怎么想的、做了什么**」，读起来像日记。
它不是事实源、不是审计证据、不落库（进程内环形缓冲，500 条上限）。

---

## 1. 与另外三条日志的分工

| 管道 | 回答的问题 | 留存 | 能放正文吗 |
| --- | --- | --- | --- |
| `audit` | 到底**谁改了什么**（可追责的事实） | 长期 | **不能**（只放计数/枚举/ID） |
| `security` | 谁被**拒绝**了、为什么（安全边界） | 长期 | 不能 |
| `behavior` | 她收到谁的话、看了哪个群、接不接、为什么 | 环形缓冲 500 条 | **能**（没有正文无法还原） |
| 运行日志（`logger.*`） | 运维排障 | 文件/控制台 | 尽量不放 |

选错管道不是"风格问题"：把可追责的事实写进环形缓冲，等于**过几小时就消失**；
把叙事写进审计，等于把用户聊天内容长期留存。

---

## 2. 硬规矩

### 2.1 `message` 必须是**人话**

写「看了下 XX 群，他们在聊游戏，插不上嘴，没说话」，不要写
「poll_window result=silent」。事件名放 `action`，正文说人话。

### 2.2 `action` 是**稳定的 `snake_case`**，动态信息进 `attributes`

事件名拿来检索聚合，所以不能把变量拼进去——否则每个 trace_id 都会变成
一个新事件名，聚合就废了。

### 2.3 动作与理由**必须来自不同字段**

`reason` 只放理由（「群里的普通消息」），动作（「先放着，等她刷到」）来自
`BridgeResult.action`。两处各写一份必然重复，这正是历史上那句
「先放着，等她刷到（群里的普通消息，先放着，等她刷到）」的成因。

### 2.4 不写事实表

行为日志是**观测**，不是写入路径。一条行为日志**不得**伴随
`activities` / `self_experiences` / `memories` / `scene_commits` 的写入——
除非那条写入本来就有自己的业务理由（那就不是"为了记日志而写"）。

判据：**把这条行为日志整行删掉，业务行为必须完全不变。**
做不到就说明有逻辑藏进了日志函数里。

### 2.5 降噪要合并，不要静默丢弃

同一窗口同类"没开口"的处置在 120 秒内只完整记一次
（`_is_repeated_handled`）；她**开了口**（回了 / 想回但没发出去）每次都记——
合并掉等于隐瞒她说过话。

### 2.6 不记录敏感原文

- 不放 prompt、密钥、token、完整人设；
- 正文只放**摘要级**片段（`inbound_received` 截 60 字）；
- 撤回事件里本来就没有原文，只放 id。

---

## 3. 已知动作名（按域）

新加动作名请追加到本表，并遵守 §2。

| 域 | 动作名 | 一句话 |
| --- | --- | --- |
| 入站 | `inbound_received` | 她收到了谁在哪说的话（截断） |
| 入站 | `inbound_handled` | 她怎么处理的（回了 / 先放着 / 没接话） |
| 入站 | `platform_message_id_record_failed` | 没记下平台 id（她没法引用这条） |
| 撤回 | `message_retracted_after_seen` | **她看手机之后才撤回**：见过、照常处理，之后引用时注明已被撤回 |
| 撤回 | `message_retracted_unseen` | **她还没看到就被撤回**：等同没看见，不进上下文 |
| 撤回 | `recall_unmatched` | 撤回的 id 在库里找不到（正常情况，不是告警） |
| 撤回 | `recall_self` | 她自己撤回了自己的消息（不是"对方撤回"） |
| 撤回（出站） | `message_recalled` | **她**把发出去的那条收回来了（含五态回执、不含正文） |
| 撤回（出站） | `message_recall_refused` | 想撤但被拒（归属/时间窗/频次/回执），**零平台 action** |
| 媒体 | `video_watched` / `video_watch_skipped` | 看了 / 没看成那个视频，为什么 |
| 媒体 | `video_quota_exhausted` | 看视频的 **token 额度**用完了（含 used/limit 与供应商，便于排障） |
| 上游回退 | `upstream_fallback` | 这次用了领域默认参数（上游不可用），附原因 |

> 模型侧失败**不是**行为日志：它走运行日志（`ling.models.router` 的
> 「模型调用失败 provider=… code=…」）。行为日志只说"她这一侧发生了什么"，
> 供应商 5xx 不是她的经历。

---

## 3A. 看视频的额度口径（2026-11-09 改）

### 从"次数"改成"token 账本"

原来是"每窗口每小时 ≤2、每天 ≤20"，两份**进程内**计数器（注释自己写着
"重启清零可以接受"）。已删除。理由三条：

1. **次数与成本脱钩**：5 秒的短片和 3 分钟的长片都记"1 次"，而后者抽帧更多、
   token 多一个量级。按次数限等于把成本管错了对象。
2. **进程内计数不是额度**，是限流——重启即清零，多副本各算各的。
3. **真正该管的那份账已经存在**：`models/quota.py::TokenQuotaStore` 按
   (供应商, 本地日) 累计真实 token 到 `provider_token_usage` 表，`router`
   每次调用前都会查。看视频走同一个 router、同一家供应商，所以额度**天然共用**
   ——再维护第二套计数器必然对不上，而对不上的时候用户看到的是
   "额度页说还有、视频却看不了"。

### 判据（与 router 完全一致，不另立一套）

```
limit = providers[perception.video 绑定的供应商].daily_limit_effective()
limit is None        → 放行（没设限额）
used >= limit        → 拒绝
```

`used` 来自 `TokenQuotaStore.used(provider_id)`。**不自己维护任何计数器**。

### 没看成之后：把事实交给主回复链路

用户原话：「看视频额度如果没了的话，直接把事实告诉主回复链路，让他进行按人设
自然的回复，例如懒得看或者什么样」。

`WatchOutcome` 除了 `observed`（看到了什么）还有结构化的：

| 字段 | 含义 |
| --- | --- |
| `observed` | 她看到了什么。**失败时必为空**——走了二次生成不等于"看到了" |
| `skipped` | 没看成的**人话**原因 |
| `denied_kind` | `quota` / `missing` / `error`（`None` = 没失败） |

`kernel/turn.py` 在 `observed` 为空但 `skipped` 非空时，**同轮再生成一次**，
把下面这条作为 `user` 角色的**事实**喂进去：

- **`quota`**：「你本来想看一眼这个视频，但今天的看视频额度用完了（已用 X /
  上限 Y tokens）。这不是视频坏了，是你今天已经没有余力再去看了。用你自己的
  方式回应——比如表示今天懒得看、有点累、改天再看。」
- **`missing` / `error`**：「你本来想看一眼这个视频，但取不到视频内容。用你
  自己的方式回应——比如表示打不开、看不到、让对方说说里面有什么。」

两类的共同硬边界：**不要说你看过了，也不要说视频里是什么**。

### 为什么两类必须说不同的话

- 额度用完**不是故障**，是她今天真的没有余力。说成"打不开"就是谎报技术故障，
  用户会去查网络和视频文件。
- 取不到内容**是真的没读到**。说成"不想看"会让用户以为她敷衍。

### 为什么断言要看提示词

用户看到的是她最终说的那句话。"她有没有假装看过"必须从**喂进去的提示词**与
**生成的内容**两头看——只看 `WatchOutcome` 的字段会漏掉"字段正确、提示词里
却写着观察内容"的情况（那样她照样会说"我看到一只猫"）。

---

## 4. 撤回（2026-11 新增）的专门口径

撤回**分两种**，判据是**事实**而不是猜测：撤回那一刻她是否已经看过这条消息
（`messages.attention_state` / `seen_at` / `handled_as` 三档，见
`domains/conversation/service.py::saw_it_before_recall`）。判定结果冻结在
`messages.retracted_after_seen`（迁移 0056）——之后 `attention_state` 还会变，
读取时再推就是用今天的状态解释撤回那一刻的事实。

| 情况 | 上下文 | 已发出的回复 | 行为日志 |
| --- | --- | --- | --- |
| **看手机之后才撤回**（`after_seen=true`） | 原文**保留**，末尾加「（这条消息后来被撤回了）」；压进摘要时也带「（后来被撤回了）」 | 不回滚、不修改 | `message_retracted_after_seen` |
| **还没看到就被撤回**（`false` / 旧数据 NULL） | **整条不进**任何模型上下文（连 `dropped` 都不进，不会被摘要捞回来） | 不适用（没有回复） | `message_retracted_unseen` |
| 库里没有这条 | —— | —— | `recall_unmatched` |

三条推论，都属于本口径：

1. **撤回不是事实**：不写 `activities` / `self_experiences`，不调模型
   （`behavior_model_calls` 计数不变）；
2. **不物理删除**：`retracted_at` 非空、行还在——"她当时为什么没接那句话"
   要能追溯；
3. **已沉淀的记忆不主动回改**：撤回是"以后别再引用"，不是"过去没发生"。
   用户要清除记忆走 `retract` / `forget`，那是另一件事（`forget` 现在是
   真删，见 §4B）。

---

## 4B. `POST /v1/memory/forget`：真删与审计（2026-11 变更）

`forget` 从"软清除"（打撤回/归档标记，原文仍在库里）改成了**真删**：
请求阶段同步封住读写，清理阶段由后台 worker 物理删除原文与全部派生数据。
完整契约见 `docs/API-WEB-V1.md` §10.2.1/§10.2.2，这里只记**审计口径**。

### 4B.1 为什么审计要分三段

「用户点了删除」和「数据真的没了」在审计里必须长得不一样。混成一条时，
出事查不出它停在哪个阶段——是请求没落库、还是清理一直失败。

| 动作 | 何时写 | `attributes` |
| --- | --- | --- |
| `memory_forgotten` | 请求阶段（`POST /v1/memory/forget` 提交时） | `{tombstone_id, generation, events_cancelled, mode:"purge"}` |
| `memory_purged` | 清理阶段完成（`state=done`） | `{tombstone_id, memories_deleted, episodes_deleted, core_deleted, awareness_deleted, embeddings_deleted, entities_deleted, retrieval_logs_deleted, events_scrubbed, media_evidence_deleted, knowledge_items_deleted, prompt_snapshots_deleted, relationship_notes_scrubbed}` |
| `memory_purge_failed` | 清理失败（`state=failed`） | `{tombstone_id}`；原因码放 `reason_code` |

**字段口径变更**：`memory_forgotten` 原来带 `retracted_count`（软清除的影响
行数）。真删的请求阶段**还不知道**真实删除数（物理删除在后台），返回它只能
是假的——所以换成 `tombstone_id` + `generation` + `events_cancelled`。真实行数
在 `memory_purged` 里，那时才知道。

`memory_purged` 的后四个计数（媒体描述 / 私有知识 / 提示词快照 / 关系事件
`note`）**只出现在审计与 worker 日志里**，墓碑表没有对应的列，所以
`GET /v1/memory/purge/status` 不报它们。这不是遗漏：墓碑只存"能撑起状态
机的那几个数"，其余属观测细节。

### 4B.2 审计里**不能**出现被删内容

三条都只带 id、计数、状态码。`attributes` 里出现正文（哪怕一句摘要）就把
审计管道变成了"不存在"的副本——而审计是长期保留的。

同理，墓碑表（`memory_purge_tombstones`）不存正文：备份恢复后能凭
`subject_id + generation` 阻止旧数据复活，却读不出她被删了什么。

### 4B.3 相关不变式

- **对话里不能删**：工具注册表里没有任何遗忘/删除类工具
  （`tests/unit/test_memory_purge_no_chat_path.py` 用封闭集合钉住）；唯一入口
  是后台管理页的这个端点。
- **投递失败也必须收敛**：`memory.purge` 事件与墓碑同一事务落库；另有
  `memory.purge.sweep` 周期任务扫描 `pending|running|failed` 墓碑兜底。
- **失败不放行读**：`state=failed` 期间读侧仍然返回空——用一次清理失败换一次
  泄漏是不可接受的。

---

## 4A. 主动撤回（出站）的授权边界（2026-11 新增）

上面 §4 讲的是**对方**撤回；这里讲**她自己**撤回自己发出去的话（用户拍板
「说错了想收回」）。两者都让原文不能再被当成有效发言，但**撤回方不同**，
所以列、日志动作、判据全部独立（`messages.recalled_at` 等，迁移 0057）。

### 4A.1 五道闸，全部在发平台 action **之前**

| # | 闸 | 判据（**事实**，不猜） | 拒绝码 |
| --- | --- | --- | --- |
| 1 | 归属 | 这条本地消息 `role="actor"`，且能经 `operations.payload.reply_plan` → `delivery_receipts` 找到"我们真的发出去过" | `not_actor_message` / `not_recallable_origin` |
| 2 | 已送达 | 回执 `state ∈ {sent, delivered}` 且带 `platform_message_id` | `no_receipt` / `not_delivered` |
| 3 | 时间窗 | 距回执时刻 ≤ 120 秒（QQ 群/私聊通常 2 分钟） | `expired` |
| 4 | 频次 | 同一会话每分钟 ≤ 3 次 | `rate_limited` |
| 5 | 对象 | 非外部观测/资料、非模拟消息、正文非空 | `not_recallable_origin` |

**不按内容匹配、不按时间猜**：正文相同不等于同一条，而撤回不可逆——
撤错一次就是把别人的话从群里抹掉。平台 id 是**唯一**可用的定位凭据。

### 4A.2 回执五态（**只有 `recalled` 能说"撤回了"**）

| 状态 | 含义 | 本地写什么 |
| --- | --- | --- |
| `recalled` | 平台确认撤回 | `recalled_at` + `recall_state` + `recall_receipt_id` |
| `not_found` | 平台上已经没这条 | `recall_state`（**不写** `recalled_at`） |
| `expired` | 平台说超时/没权限 | 同上 |
| `unsupported` | 这个接入方式不支持（QQ 官方机器人） | 同上 |
| `uncertain` | 没拿到回执（超时/断连/非 JSON） | `recall_attempted_at` + `recall_state`，**不写** `recalled_at` |

`recalled_at` 与 `recall_attempted_at` 分开是刻意的：只有前者能说"撤回了"。
合成一个"试过"的时间戳，下一次点击就会看到"已撤回"而跳过——一次超时会被
永久记成成功。

### 4A.2a `uncertain` 允许重试，但**不幂等**（2026-11 明确）

`uncertain` 时用户再点一次确认，会**真的再发一次 `delete_msg`**，对着一条
平台上可能已经不存在的消息。我们没有可靠的核对手段（`get_msg` 对"已删除"与
"id 过期"的返回一模一样，分不开），所以**如实承认**：

- 重试可能对已删除的消息再撤一次 → 平台回"消息不存在" → 记 `not_found`；
- 平台侧无副作用（重复 delete 一条已删消息是空操作）；
- 最坏结果是用户看到"这条已经不在平台上了"，而不是"撤回了"。

**不许**把它做成假幂等（例如 `uncertain` 也写 `recalled_at`）：那会把一次超时
永久记成成功，而它可能根本没撤掉。

### 4A.2b 并发确认只能执行一次

确认端点先做**条件更新**（`WHERE state='proposed'`）抢占这条提议，抢到的才
执行；第二个并发请求 rowcount=0 → 409，**平台零调用**。这是"确认只有一次"
在数据库层的保证，不靠"先读后写"的检查（那在并发下必然漏）。

### 4A.2c 标记永远不进任何出口（含流式）

`<<撤回 rX>>` 是给模型的语法，用户在**任何**出口都不该看到它：

| 出口 | 怎么挡 |
| --- | --- |
| 流式 delta（浏览器） | `kernel/marker_stream.py::stream_safe_all` + `strip_all_markers` |
| 落库正文 | `finalize_recall_text` |
| 外发文本 | `bridge` 的出口清洗 |
| 摘要/历史渲染 | 同上（剥标记幂等） |

流式这一层必须**同时**挡两种标记（`<<看视频 …>>` 与 `<<撤回 …>>`）：漏一种
就等于用户先看到说明书、再看到它消失。两条标记的"半截暂扣"共用**同一个**正则
（`domains/media/video.py::PENDING_MARKER_RE`），因为它只认已知动词前缀
（`撤` / `看`）——`1<<2 就是 4` 这种正常正文不会被当成半截标记吞掉。

### 4A.3 上下文与事实

- **上下文保留原文 + 第一人称注记**「（我后来撤回了这条）」：她说过这句话是
  既成事实，抹掉会让下一轮的她不理解自己上一条回复在说什么；但也不能装作
  没撤回（否则她会继续引用一条平台上已经没有的消息）。
  **措辞与入站的「（这条消息后来被撤回了）」刻意不同**——谁撤回的不能混。
- **不写事实表**：`activities` / `self_experiences` / `memories` 一律不动，
  `behavior_model_calls` 计数不变。撤回是"以后别再引用"，不是"过去没发生"。
- **行不删**：只标记。删行之后"这条从来不存在"与"它被撤回了"在库里一样，
  而记忆的 `source_event_ids` 会变成悬空引用。

### 4A.4 谁能触发

**本轮只有用户手动**（`POST /v1/conversations/{id}/messages/{mid}/recall`，
owner only）。**不开放给模型自主调用**：撤回是对已发生交互的补救，让模型
判断"这句说错了"意味着它要评估自己的输出，而它对自己的错误没有可靠判据——
错判一次就是替用户删掉一条正常回复。下一阶段若开放，需单独授权（模型只能
提议、由 owner 确认），并复用同一套五道闸。

### 4A.5 审计不含正文

`message_recalled` / `message_recall_refused` 只放：谁触发、哪个窗口、哪条
**本地** message id、平台回执 id 是否存在、结果状态、原因码、消息年龄（秒）。
**不放消息正文**——撤回本身往往正是因为那条内容不该再被引用，把它抄进审计
等于给内容开第二份长期副本。

---

## 5. 不在本口径内的事

- 梦、日程、意向的生成日志：归 `docs/DREAM-SCHEDULE-DESIGN.md` §214 的清单；
- 投递对账的终态：归 `docs/OUTBOUND-DELIVERY-RESEARCH.md`；
- 访问拒绝的计数与逐条：归 `security` 管道 + `denied_total`。
