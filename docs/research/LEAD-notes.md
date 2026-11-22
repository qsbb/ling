# Lead 第一手取证笔记（反例结构）

> 观测对象：HEAD=de43cbe79a78e0c01f7821470500eaa04ea79497，工作树干净（git status 空）。
> 记录时刻：2026-11-22 09:23:51+08:00 起。本文件是 lead 自读代码所得，不替代 S3/S4 子智能体报告。

## 1. 窗口身份与话题是两套独立机制

- `src/ling/kernel/bridge.py:1479 _ensure_window()`：文档串明写「按受信实例和平台目标复用窗口，旧来源只做唯一候选回退」。
- `:1565–1576 _find_or_create_window()`：查找键 = `(actor_id, channel, connector_window_ref/title)`，私聊再加 `subject_id`；群聊不加 speaker。
- `:1578–1606`：有受信实例时，优先 `origin_status=known + origin_scope/instance_id` 精确匹配；`origin_manually_bound` 冲突直接报错。
- `:1608–1618`：实例未知时回退到 `origin_status=unknown` 的唯一候选；多于一个就冲突。
- `:1620–1635`：找不到才新建 `Window`。

结论：**窗口复用与话题无关**。同一聊天目标无论隔天还是换题都回同一个 `Window`；「同一窗口」不等于「同一话题」。（实测：代码路径；推理：这是反例的结构性前提。）

## 2. 短记忆会过期，摘要永不过期

- `src/ling/kernel/context.py:230–244 _window_topic_live()`：`expires_at` 只管 `topic/open_questions` 短记忆；`summary` 与 `summary_through_sequence` 同一行但**不判 TTL**。注释记载旧实现曾把摘要一起判过期，导致「她完全不记得聊过什么」。
- `:1673–1685`：只要 `window_ctx.summary` 非空，就注入「本窗口较早的对话摘要…」。没有话题相关性判断，也没有「当前是否还在那个话题」判断。
- `:1791–1805`：被预算挤出的消息由 `_rollup_summary()` 压缩进摘要，`through` 推到保留窗口起点，保证原文窗口与摘要严丝合缝。

结论：旧话题一旦被压进摘要，就会**每轮无条件随上下文带出**，直到被更新——与「现在在聊什么」无关。（实测：代码路径。）

## 3. 「？」和「早上好」的召回门控（已实测更正）

> **更正（2026-11-22 09:28，P3 直接调用 `recall_query.py` 实测）**：本节先前推测「？会触发自动记忆召回」**不成立**。
> 全角「？」经 `normalize("NFKC", text)` 变半角 `?`，再被 `rstrip(".!?。~")` 吃掉 → plain 为空串 →
> 属于 `_STANDALONE_REPLIES`（含 `""`） → `should_skip_automatic_recall('？') == True`，**跳过**自动召回。
> 真正未被跳过的是「早上好」：`should_skip_automatic_recall('早上好') == False`，会以 `query="早上好"`
> 调用 `_memory.recall()`（是否真召回旧需求取决于排序与阈值，未验证）。
> `needs_recent_topic('后来呢？') == True`，会用上一句用户消息拼 query。
> 因此下文 §6 判据 1、4 与 §3 旧版推论按此更正为准；反例里旧需求的来源候选收敛为：
> **摘要常驻（code 路径确定）** 与 **「早上好」触发的宽泛召回（未验证排序）**，而不是「？触发召回」。

- `src/ling/domains/memory/recall_query.py:6–11 _STANDALONE_REPLIES` 含「你好/嗯/哦/好/在吗…」与空串，
  **不含「早上好」**。
- `:19–22 should_skip_automatic_recall()`：整条消息 NFKC + 去标点后精确匹配上述集合才跳过自动召回。
- `:13–16 _TOPIC_FOLLOWUP` + `:25–27 needs_recent_topic()`：匹配「后来呢/然后呢/哪家/几点/原话…」才把上一条用户消息拼进召回 query。

`context.py:1550–1575`（实测代码路径）：
- 允许长期记忆且 `should_skip_automatic_recall(user_text)` 为 False → 用**当前消息原文**做 query 调 `_memory.recall()`。
- 只有 `needs_recent_topic` 为真才把最近一条非寒暄用户消息拼进 query。

结论（代码路径实测 + 结果推理）：「？」直接跳过召回；「早上好」会触发一次宽泛查询，但召回结果是否包含旧需求未验证。**下一节判据是本报告最需要真实快照验证的部分。**

## 4. 引用与消息 id 只对最近若干条有效

- `context.py:1933–1977 _message_id_marks()`：**只给最近 `HISTORY_MESSAGE_ID_LIMIT` 条用户消息标 `[msg_id:…]`**；更早消息即使出现在原文/摘要里也不可引用。注释理由是 NapCat 5000 条 FIFO 短 id 表会失效。
- `:1768–1770`：只有 `role=user` 且取到平台 id 的历史消息才加标记。
- `:1836–1846`：当前消息也带 `current_mark`，放入 `reply_ids`。

含义：群里「引用旧话」若旧话已被挤出可引用窗口，模型即使想引用也拿不到 id，引用会降级（`bridge.py:997–1005 quote_dropped`）。

## 5. 说话人

- `context.py:1687–1717`：群聊给历史 user 消息加「名字：」前缀；占位名（「群友」「用户」）被过滤，宁可不署名。
- `:1807–1835`：当前消息按本次群名片 + 稳定昵称合并署名；私聊强制 `user_sender_name=None`。

## 6. 待验证判据（给 P2/P4，已按 §3 更正调整）

1. 构造 `user_text="？"` 且上一句为「早上好」：按实测应出现 `compiled.meta["memory_recall_skipped"] == "low_information"`（或等价跳过标记）。若真值相反，说明运行时路径与 `recall_query.py` 的纯函数行为不一致，需要查调用方。
2. 构造 `user_text="早上好"`：检查是否**未**出现跳过标记，并读取 `compiled.meta` 的记忆命中字段（`evidence_selected` / `memory_hits`）；命中旧需求即证明「早上好」的宽泛召回是来源之一。
3. 同一历史，把 user_text 换成「后来呢？」：检查 query 是否拼接了上一句（`needs_recent_topic` 分支）。
4. 检查 `annotated` 里 `lane="summary"` 的块是否无条件存在——若是，证明摘要与当前话题无关地常驻；这是反例最可能的来源。
5. 关键区分：若「？」轮次快照里 `lane="summary"` 含四项需求且 `evidence_selected==0`，来源就是摘要常驻；若 `evidence_selected>0` 且命中旧需求，来源是记忆召回。两者可同真，需分别计数。

## 7. 来源

- 行号基于 HEAD=de43cbe 的工作树（见顶部观测）。
- 未运行真实回放；本节除「代码路径」外均为推理，标为未验证。
