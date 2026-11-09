# 撤回工具（`message.recall`）：标记语法与确认流程

用户拍板的原话是「说错了想收回」，后来又明确「撤回消息只做注册工具开放给 llm」。

这两句话合起来是一条**很窄**的授权：模型可以**提议**撤回，但**不能直接撤**。
本文写清它到底怎么工作、边界在哪、以及为什么是这么设计的。

---

## 1. 一句话概括

> 她在回复里写 `<<撤回 rXXXX>>` → 代码把它变成一条**待确认项**并提示到网页
> → 用户点「确认」→ 走既有五道闸 → 平台收到**一次** `delete_msg`。

不确认 = **平台一次都不碰**。

---

## 2. 标记语法

```
<<撤回 r3f2a1>>     撤上下文里标着 [recall:r3f2a1] 的那一条
<<撤回>>            撤她自己最近发出去的那一条
```

### 短号从哪来

上下文里，**她自己说的、而且现在还真撤得动**的那几条发言，前面会带一个
`[recall:r3f2a1]` 标记：

```
assistant: [recall:r3f2a1] 今天不想去了
```

短号 = 本地消息 id 末尾 6 位字母数字，前面加 `r`（recall）。它由 id
唯一决定，不需要额外存映射表——同一句话在相邻两轮里拿到同一个短号。

> **为什么不是 `[msg_id:…]`**：那个是**平台**消息 id（`[quote:…]` 引用用的），
> 而撤回要的是**本地**消息 id。两套 id 语义不同，混用会让模型把平台 id
> 写进 `<<撤回 …>>`，然后必然对不上、静默失效。

### 为什么只标"真撤得动的"

`recallable_refs()` 查过三件事才标：`role="actor"`（她的发言）、
有真实送达回执、未被撤回。

**不标做不到的**是这条设计里最重要的一环：如果给她一个写了也撤不成的短号，
她会照着写，然后用户看到"她要撤，但撤不了"——那比不提供这个能力更糟。

### 与看视频标记的关系

两套标记**互不吞**：

```
<<看视频 m3f2a1>>   看视频（m 前缀）
<<撤回 r9b2c3>>     撤回（r 前缀）
```

前缀不同、正则各认各的，`parse_watch_markers` 不会顺手把撤回标记吃掉，
反之亦然（有单测钉住）。

---

## 3. 完整流程

```
┌─ 上下文编译 ────────────────────────────────────────────┐
│  recallable_refs() 查出真撤得动的几条                    │
│  → 历史里给它们打 [recall:rXXXX]                         │
│  → capabilities 车道追加一段「你可以撤回哪几条」          │
└────────────────────────────────────────────────────────┘
                          ↓ 模型生成
┌─ 轮次收尾（kernel/turn.py::_finalize）──────────────────┐
│  1. parse_recall_markers() 取出短号                      │
│  2. resolve_recall_ref() 换成本地消息 id                 │
│  3. 落 operations(kind='message_recall_proposal',        │
│     state='proposed')  ← 待确认项                        │
│  4. finalize_recall_text() 把标记从正文里剥掉            │
│  5. turn.completed 事件带 recall_proposals               │
└────────────────────────────────────────────────────────┘
                          ↓ 事件推到网页
┌─ 网页 ─────────────────────────────────────────────────┐
│  「她要撤回这条，确认吗？」  [确认]  [不要]              │
└────────────────────────────────────────────────────────┘
          ↓ 确认                        ↓ 不要
┌────────────────────────────┐  ┌──────────────────────────┐
│ POST .../recall-proposals/ │  │ POST .../recall-proposals│
│      {pid}/confirm         │  │      /{pid}/decline      │
│ → registry.call(           │  │ → state='declined'       │
│      confirmed=True)       │  │ → 平台零调用             │
│ → 五道闸 → delete_msg ×1   │  └──────────────────────────┘
└────────────────────────────┘
```

---

## 4. 为什么不用 function-calling

`ModelRequest`（`ports/model.py`）没有 `tools` 字段，各 provider 适配器也没有
实现工具调用。而仓库里"她决定做某事"的既有范式就是**标记**
（`<<看视频 mXXXX>>`，见 `domains/media/video.py`）。

沿用同一种形状的好处是：解析、剥离、流式暂扣（`stream_safe`）那套代码路径
都能复用，不需要为撤回单独造一条通道，也不会出现"两套机制对同一件事各判一次"。

将来接上真正的 function-calling 时，**语义不用改**：`ToolSpec` +
`requires_confirmation` 这一层本来就和调用方式无关。

---

## 5. 五道闸（复用，不重写）

工具 handler（`capabilities/tools.py::_recall_tool`）**不自己判**任何东西，
它直接调 `domains/conversation/recall.py` 里的既有实现：

| # | 闸 | 判据 |
|---|---|---|
| 1 | 归属 | `role="actor"` + 能找到真实回执 |
| 2 | 已送达 | 回执 `sent`/`delivered` 且带 `platform_message_id` |
| 3 | 时间窗 | 发出后 ≤ 120 秒（本地保守预检） |
| 4 | 频次 | 同一会话每分钟 ≤ 3 |
| 5 | 不可撤回对象 | 外部观测 / 模拟 / 空正文 / 已撤过 |

**判据只有一份**：手端点 `POST /v1/sessions/{id}/messages/{mid}/recall` 与
模型提议的确认点，最终跑的是同一段代码。抄一份过来迟早会走散，而
"模型那条路判松了"正是最难发现的那种错。

拒绝时**一律人话**（`RecallRejected.note`），原因码只进日志与测试——
`not_actor_message` / `expired` 这些不该露给用户。

---

## 6. 「不确认就不执行」是怎么保证的

不是靠 handler 自觉，是靠三层：

1. **注册表层**：`ToolSpec.requires_confirmation=True` 时，
   `ToolRegistry.call(confirmed=False)` 在**进 handler 之前**就返回
   `needs_confirmation`。handler 连执行的机会都没有。
2. **提议层**：轮次收尾只落 `state='proposed'` 的行，不发任何平台 action。
3. **端点层**：`POST .../confirm` 是唯一把 `confirmed=True` 传下去的地方。

### 6.1 `POST /v1/tools/{id}/call` **不能**自证确认（2026-11 修正）

这个通用工具端点原来有一个 `confirmed: bool = False` 字段，直接透传给
`ToolRegistry.call(confirmed=...)`——也就是**请求体自己给自己发确认**。那等于
把上面第 1 层做成摆设：任何拿到 token 的调用方在 body 里写
`"confirmed": true` 就能执行撤回（这条路径还没有 owner / 窗口归属校验）。

现在：

- `ToolCallBody` **没有** `confirmed` 字段，且 `extra="forbid"`——带上就是 422，
  **连 handler 都不进**；
- 端点自己永远传 `confirmed=False`，没有例外；
- `/openapi.json` 里也不会出现这个字段（有测试钉住），否则前端会照着它写代码。

所以这个端点的身份是"**只能提议**"。不可逆动作的确认只有一条路：

```
POST /v1/sessions/{conversation_id}/recall-proposals/{proposal_id}/confirm
```

拒绝文案会直接指出这条路径（"确认必须走提议端点"），不是 pydantic 的
"字段多余"。

验收里"不确认 → 零平台调用"这条，断言的是 `fake.delete_calls() == 0`
——**handler 层返回什么都不能替代这个断言**。

---

## 7. 状态与审计

### 提议状态（`operations.state`）

| 值 | 含义 |
|---|---|
| `proposed` | 等用户确认（唯一会显示在网页上的状态） |
| `confirming` | **已被某个请求占有**，正在执行（并发第二个人看不到它） |
| `confirmed` | 用户确认，且**真的执行过**（成功或失败都算） |
| `declined` | 用户说"不要撤" |

`needs_confirmation` / `unsupported` / `timeout` 的结果**落回 `proposed`**，
让用户还能再点一次。

### 7.1 并发确认：只能撤一次（2026-11 修正）

原来的 confirm 是"先读 `state != proposed`、再执行"——**先读后写，没有锁**。
两个并发 confirm（双击、两个标签页、前端重试）都能通过那次检查，于是**两次
真实的 `delete_msg`** 打到平台。

现在用**条件更新**先"占有"这条提议，拿到执行权才动手：

```sql
UPDATE operations SET state='confirming'
 WHERE id=:pid AND kind='recall_proposal' AND state='proposed'
```

`rowcount == 1` 的人才执行；另一个人看到 `rowcount == 0` → **409，平台零调用**。
条件更新而不是 `SELECT … FOR UPDATE`：前者本身就是判据（谁改走了状态谁就拿到
执行权，一步原子），后者仍要"读-判-写"三步，漏一步就回到原问题。

抢占**立刻提交**——否则另一个事务还看得到 `proposed`，两次都会抢到。

### 审计（`ling.audit.audit_event`）

| 事件 | 何时 |
|---|---|
| `message_recalled` | 执行完成（含 `outcome=degraded` 的不确定态） |
| `message_recall_refused` | 五道闸拒绝 |

审计只放：谁触发、哪个窗口、哪条本地消息、平台回执 id、结果、原因码。
**不放正文**——撤回往往正是因为那条内容不该再被引用，把正文抄进审计等于给它
开了第二份长期副本。

---

## 7A. `uncertain` 的重试语义：**不幂等，别假装是**

`uncertain` = 我们发了 `delete_msg` 但**没拿到回答**（超时 / 断连 / 非 JSON）。
这时允许用户再点一次确认——但那会**真的再发一次 `delete_msg`**，对着一条平台上
可能已经不存在的消息。

我们**没有**可靠的核对办法：

- OneBot 没有"这条被撤回过吗"的查询 API。`get_msg` 查已删除的消息会直接回
  "消息不存在"，而"没被撤过但 id 过期"的返回**一模一样**——两个签名分不开，
  拿它当判据等于猜；
- `get_group_msg_history` 只能比对"历史里还在不在"，同样分不开"撤掉了"与
  "id 过期 / 被刷出 FIFO"。

所以如实写清楚：**重试可能对一条已删除的消息再撤一次**。NapCat 会回
"消息不存在" → 我们据此写 `not_found`（**不**写 `recalled_at`）。这是可接受的：
最坏结果是用户看到"这条已经不在平台上了"，平台侧没有任何副作用（重复 delete
一条已删消息是空操作）。

**绝不能**把它做成"写了 `recalled_at` 就算成功"来假装幂等——那会把一次超时
永久记成"撤回了"，而它可能根本没撤掉。

只有 `recalled_at` 非空才代表**确认撤回过**；`recall_attempted_at` 只是"试过"。

---

## 8. 边界（明确不做的事）

- **不物理删除消息行**：`recalled_at` 标记 + 行保留，可追溯"她说过、后来收回"。
- **不写事实表**：`activities` / `self_experiences` 不动——撤回是"以后别再引用"，
  不是"过去没发生"。
- **一次回复只处理第一个标记**：撤回有频次上限，一句回复里连撤三条更像是模型
  跑偏了。多余的标记仍然会被剥掉（不给用户看到），但不落提议。
- **短号对不上就什么都不做**：不猜、不模糊匹配。她已经把"想收回"说出口了，
  但平台上什么都没发生，那就不该让用户看到一条失败提示。

---

## 9. 相关代码

| 位置 | 职责 |
|---|---|
| `domains/conversation/recall.py` | 五道闸、标记解析、短号、`recallable_refs` |
| `capabilities/tools.py` | `message.recall` 的 spec 与 handler |
| `capabilities/expression_choice.py` | `render_recall_help`（告诉她能撤哪几条） |
| `kernel/context.py` | 查 `recallable_refs` + 打 `[recall:…]` 标记 |
| `kernel/turn.py` | `_finalize` 里落提议、剥标记、事件带 `recall_proposals` |
| `api/routes/recall.py` | 列出 / 确认 / 拒绝待确认项 |

## 10. 测试

`tests/integration/test_recall_tool.py`（15 条）覆盖：

- 工具注册 + `requires_confirmation=True`（`GET /v1/tools`）
- 不确认 → 零平台调用；确认 → 恰好一次 `delete_msg`
- 五道闸复用（别人的消息 / 超时窗 → 拒绝 + 零调用）
- 标记解析、幂等、半截标记、与看视频标记互不吞
- `recallable_refs` 只列"自己的 + 有回执 + 未撤回"
- 端到端：真轮次写标记 → 提议入库 → 正文干净 → 提示词带撤回选项
- 提议端点：列表 / 确认 / 拒绝 / 跨窗口不可见 / 重复确认 409
- 审计里没有消息正文

`tests/unit/test_w2_rules.py` 另有纯函数层的 6 条。
