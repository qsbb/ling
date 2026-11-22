# C — Ling 入站窗口复用取证（`src/ling/kernel/bridge.py`）

> 范围：只做入站侧（连接器 → `inbound_intake` → `InboundBridge`）的窗口身份与复用取证；上下文编译器（D 组）只在"窗口如何接到 recent history"处点到为止。全程只读源码，未连生产库/NAS、未部署插件、未跑 pytest。

## 0. 基线（pin，逐字复制）

命令：`cd /Users/lingxi/project/ling && git status --short && git log -1 --format='%H %ad %s' --date=iso-strict && shasum -a 256 src/ling/kernel/bridge.py`

```text
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-private.md
?? docs/research/
de43cbe79a78e0c01f7821470500eaa04ea79497 2026-11-22T09:17:29+08:00 feat(live): 合入直播工作台与舞台控制
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
```

HEAD 与 `bridge.py` sha256 均与任务给定预期一致。三个 untracked 路径与本组无关，未触碰。本报告是本次唯一写入文件。

## 1. 平台聊天身份：从平台事件到 `InteractionEnvelope`

**观察。** 平台差异在连接器层收敛成统一信封 `InteractionEnvelope`（`src/ling/contracts/v1.py:311-346`）：关键字段 `connector_instance`（实例 id）、`account_ref`（发送者渠道 id）、`window_ref`（聊天目标引用）、`external_event_id`。`sender_name` 明确只是标签、身份由 `(connector_instance, account_ref)` 决定（`src/ling/contracts/v1.py:321-328`）。

- **OneBot/QQ（NapCat）**：`is_group = message_type == "group"`；`user_id` / `group_id` 从事件取；`window_ref = window_ref_for(is_group, target_id=group_id if is_group else user_id, instance_id=self.instance_id)`（`connectors/onebot/connector.py:1237-1244, 1262-1272`）。`window_ref_for` 的实现是 `f"{instance_id}:{kind}:{target_id}"`，`kind = "group" if is_group else "private"`，并注明"群聊窗口只由群号决定、不含发言人"（`connectors/onebot/message.py:237-245`）。
- **QQ 官方机器人**：按 `group_openid` / `channel_id` / `guild_id` / c2c 生成 `qq:group:{id}` / `qq:channel:{id}` / `qq:guild:{gid}:{uid}` / `qq:c2c:{uid}`（`connectors/qq/connector.py:222-252`）；`account_ref` 是 `author.user_openid/id/union_openid`。
- **qqbot**：`target_id = group_openid if is_group else account_ref`，同样用 `window_ref_for`（`connectors/qqbot/connector.py:490-500, 529-536`）。
- **web**：web 路径不经本文的入站连接器链（`api/routes/chat.py:339, 599` 直接用 `ConversationService`），其窗口 `channel="web"`、`origin_status="not_applicable"`（约束见 `src/ling/infra/models.py:214-218`）。

**群/私聊判定的唯一实现**是 `is_group_window_ref`（`src/ling/kernel/visibility.py:100-149`）：按 `:` 切段，仅当"第 1 段恰为 `group` 且第 2 段（群号）非空"才算群聊；因此 `onebot:private:group:123`、`qq:private:group:notes` 这类私聊引用不再被旧的子串匹配误判（同文件 `:117-135`）。`bridge.handle` 用的就是这个函数（`src/ling/kernel/bridge.py:415`）。

## 2. 内部会话：入站事件如何落到内部 key

**观察。** `bridge.handle` 的身份解析用 `(connector_instance, account_ref)` 找 `subject_id`：仅已证明绑定才接受；群聊里未绑号可自动建 `known` 级 subject，私聊未绑号默认拒绝（`auto_register_private` 关闭时）（`src/ling/kernel/bridge.py:415-464`）。群聊窗口的 `subject_id` 不是发言人，而是主人：`window_subject = owner_subject_id() or subject_id`（`src/ling/kernel/bridge.py:493-501`）。

窗口落在 `windows` 表（`src/ling/infra/models.py:195-252`）：`id`、`actor_id`、`channel`、`subject_id`、`title`、`visibility`、`connector_window_ref`、`origin_scope`、`origin_instance_id`、`origin_status`、`archived`。关键约束：

- `origin_status="known"` 要求 `origin_scope ∈ {onebot, qqbot}` 且 `origin_instance_id` 非空；`unknown/not_applicable` 则两者必须为空（`src/ling/infra/models.py:206-213`）。
- `channel="web"` ⇔ `origin_status="not_applicable"`；非 web 不得为 `not_applicable`（`src/ling/infra/models.py:214-218`）。
- 索引 `(actor_id, channel, connector_window_ref)` 与 `(origin_scope, origin_instance_id, origin_status)`（`src/ling/infra/models.py:219-230`）——**这两组索引正是复用查询的形状**。

## 3. 窗口复用：精确逻辑、复用 key、新建条件

**观察。** 入口 `handle()` 在 `begin_nested()` 内选 `_ensure_window()` 或（语音恢复时）`_resume_window()`（`src/ling/kernel/bridge.py:502-519`）。

### 3.1 复用 key（`_ensure_window` → `_find_or_create_window`）

`bridge.py:1479-1509, 1556-1635` 的查询条件逐项列出：

1. `channel = connector.connector_id`（`bridge.py:1487, 1570-1573`）；
2. 平台目标引用 `reference = envelope.window_ref`，匹配 `window.connector_window_ref == reference` **或**（历史行）`connector_window_ref IS NULL 且 title == reference`（`bridge.py:1565-1569`）；
3. `actor_id` 恒等（`bridge.py:1571`）；
4. 私聊额外要求 `subject_id == subject_id`（发言人的 subject），群聊**不加** subject 条件——同群共享一个窗口（`bridge.py:1575-1576`）；
5. 有 `trusted_instance_key` 时，先要求精确候选 `origin_status="known" AND origin_scope/instance_id == key`，再回退到"来源未知"候选；精确候选多于一条、被发现已手工绑定别的实例、或 `unknown` 候选多于一条，均抛 `WindowOriginConflictError` 而不是猜（`bridge.py:1578-1618`）；`_ensure_connector_instance` 还会校验实例仍 live（`bridge.py:1541-1554`）。

**结论（观察）**：复用键是 **`(actor_id, channel, 平台聊天目标 window_ref, 可信实例身份 instance_key)`**，私聊额外含 `subject_id`、群聊不含。它不是"按平台实例"单独复用，也不是"按用户"复用，而是**按（平台实例 + 聊天目标）**——因为没有实例身份时退回 `unknown` 候选，仍以 `window_ref` 为核心；`window_ref` 本身已含 `instance_id`，因此群聊是 `(channel, instance_id, gid)`，私聊是 `(channel, instance_id, subject_id, uid)`。

> 注：`bridge.py:1496-1502` 用 `pg_advisory_xact_lock` 串行化"同一 actor+channel+(群|subject)+reference"的窗口定位，防并发建重复窗口；这是锁，不是复用判据本身。

### 3.2 何时新建

上述查询无匹配时新建一个 `Window`：`id=win_<uuid4hex24>`、`title=envelope.window_ref`、`connector_window_ref=reference`、`visibility="group_scope" if is_group else "subject_private"`、`origin_*` 按有无 `trusted_instance_key` 写 `known` / `unknown`（`bridge.py:1620-1635`）。之后若新建，`persona_id` 被写成运行时快照的人格（`bridge.py:520-522`）。

### 3.3 TTL / 过期 / 重置

**观察（否定结论）**：`_find_or_create_window` / `_ensure_window` 中**没有**按时间、TTL、消息数触发的新建或重置条件（`bridge.py:1479-1635`）。`window.archived` 在入站侧只被读取，不被写：创建查询不显式过滤 `archived`（对比 `src/ling/domains/conversation/service.py:400-401` 的 `list_windows` 才过滤），恢复路径 `_resume_window` 见到 `archived` 就抛冲突（`bridge.py:1523-1532`）。归档由管理 API 单独操作（`src/ling/api/routes/sessions.py:347-354`），归档后同一 `window_ref` 的普通入站**仍会复用同一窗口**。入站侧**没有任何显式话题切换命令**：源码中 `src/ling/kernel/` 下除 `reset_send_gates()`（`bridge.py:184-186`，仅清进程内出站闸门）外无 `topic_switch / switch_topic / new_topic / /new / reset` 类入口。

## 4. 与 recent history 的关系（窗口如何接到后续读取）

**观察。** 接入链：`bridge._run_turn` 调 `coordinator.run_turn(...)` 时把 `window_id=window.id` 传下去（`bridge.py:1818-1833`）；`TurnCoordinator.run_turn` 在 `turn.py:1033-1058` 构造 `ContextCompiler` 并 `compile(window_id=window_id, ...)`。

- **recent history（短原文层）**：`context.py:1460-1520` 调 `self._conversation.messages_for_prompt(window_id, limit=page_size, after_sequence=_through, before_sequence=cursor)` 按页往回取；底层是 `messages` 表（模型 `src/ling/infra/models.py` 的 `Message`，查询条件 `Message.window_id == window_id` 见 `src/ling/kernel/context.py:1265-1283` 与 `src/ling/domains/conversation/service.py:1036`）。**关键**：读 recent history 的键是 `window_id`，不是 `window_ref`，不是 `account_ref`。
- **summary（摘要层）**：`window_contexts` 行（`topic` / `open_questions` / `summary` / `summary_through_sequence`，`src/ling/kernel/context.py:230-236, 1476, 1674-1683`），按 `window_id` 读（`context.py:1135, 1139`）。
- **long-term memory**：`MemoryRow` / `MemoryEpisode` 经 `recall` 检索，`context.py:1573, 1583` 调 `recall.hits` 并按 `source_window_id == window_id` 做本窗加权/去重（`context.py:119-140`）——与窗口是"来源标注"关系，不由窗口复用键决定。
- **平台 chat id ↔ 内部 message id**：`bridge._remember_platform_message_id` 把 `idempotency_keys(scope=PLATFORM_MESSAGE_ID_SCOPE, key=本地 message.id, result_ref=平台 message id)` 写入（`bridge.py:1073-1122`），供 `[msg_id:…]` 渲染与引用白名单用；它**不参与**窗口复用。
- 因此："平台聊天 id"（`window_ref` 第三段）→ 复用/找到 `windows.id` → `messages.window_id` / `window_contexts.window_id` 是入站到 prompt 的链路。**D 组负责上下文编译器如何取数；C 组只确认窗口 id 是那条链的根键。**

## 5. 运行时入口（不是死代码）

**观察。** `app.py` 启动时按实例装配 `trusted_instance_key`：OneBot WS 服务端/客户端均 `("onebot", instance_id)`（`src/ling/app.py:258-285`），qqbot gateway 为 `("qqbot", instance_id)`（`src/ling/app.py:295-315`）。事件到达后统一进 `process_inbound_payload`（`src/ling/kernel/inbound_intake.py:138`），它做白名单、事件去重、建 `TurnCoordinator`，**每条事件构造一次** `InboundBridge(...)` 并调 `bridge.handle(envelope)`（`inbound_intake.py:289-331`）；异常/归属冲突都会撤回事件认领以便重投（`inbound_intake.py:337-362`）。`bridge.py`  docstring 亦写明它是为补齐 QQ 入站"验签→去重→身份→准备窗口后不建轮次"的缺口（`bridge.py:1-15`）。上游 HTTP/WS/Webhook 分别见 `src/ling/api/routes/onebot.py:84`、`src/ling/api/routes/qq.py:69`、`src/ling/api/routes/qqbot.py:116`。

## 6. 配置键 / 默认值 / 是否真正消费

| 配置键 | 默认值 | 读取点 | 入站主路径是否真正消费 |
|---|---|---|---|
| `LING_ACCESS_MODE` / `access_list` / `access_groups` | `off` / 空 / 空（`src/ling/config.py:174-196`） | `inbound_intake.py:203-209` `check_access` | **是**（决定 observable / observe_only） |
| `LING_GROUP_MENTION` → `group_mention_on_reply` | `1`（开）（`config.py:148-150`） | 传入并存入 `self._group_mention_on_reply`（`bridge.py:361, 390`） | **否**（全文件无第二处读取，`bridge.py` grep 仅 2 处；实际 @ 由模型 `expression["at"]` 决定，见 `bridge.py:2051`）——命名强于实现 |
| `auto_register_private`（`LING_AUTO_REGISTER_PRIVATE`） | 关（`config.py:226-231`） | `bridge.py:364, 436-459` | **是**（私聊未绑号是否自动建独立 subject） |
| `dialogue.group_attention_deferred` | 见 `EffectiveConfig`，读不到按延后 True | `bridge.py:1638-1650` → `_defer_group_attention()` | **是**（`plain_group` 走延迟路径，`bridge.py:679-715`）；`_group_attention_deferred` 初始化为 True 且无任何代码置 False，所以 `bridge.py:1643-1644` 的短路分支实际永远为真 |
| `dialogue.group_turn_budget` | 配置默认 | `bridge.py:1685-1697` `_decide` | **是**（决策预算） |
| 窗口级 persona_id / 对话覆盖 | 运行时快照 | `bridge.py:517-525`，`settings/window_policy.py:270-317, 320-365` | **是** |
| `LING_LOG_ACCESS_DENIALS` | True（`config.py:207-209`） | `inbound_intake.py:73-121` | 只影响拒绝日志，不影响受理 |

## 7. 针对 C 组四个追问的回答

**Q1：同一窗口里的"旧内容"从何而来？**
**观察 + 推理。** 来自复用键命中后归属同一 `windows.id` 的历史行：`messages(window_id=window.id)`（`src/ling/kernel/context.py:1267-1281, 1487-1492`）与 `window_contexts(window_id=...)` 的摘要/话题（`context.py:1135-1144, 1476, 1674-1683`），以及经 `source_window_id==window_id` 标注的记忆命中（`context.py:119-140`）。换言之，只要复用发生，旧消息和旧摘要自然成为下一轮的上下文材料；**没有任何入站侧的"窗口隔离"动作**。（窗口评分/裁剪属于 D 组。）

**Q2：什么条件下一条很久以前的产品需求会被塞进当前 prompt？**
**推理，基于上述观察。** 条件链：① 同一 `connector_instance` + 同一 `window_ref` 再次来消息，命中 `known` 或 `unknown` 候选（`bridge.py:1578-1618`）；② 该窗口 `archived=False`（入站不检查 archived，见 §3.3）；③ 那条旧需求作为 `messages` 行仍挂在该 `window_id` 下，或已被压入 `window_contexts.summary`（`context.py:1476, 1674-1683`）；④ 上下文编译器按 token 预算从最新往回装填（`context.py:1463-1520`），且没有任何 TTL 过滤掉"很旧的需求"。满足这些条件时它就会出现在 prompt 里。若旧需求只在长期记忆里，则取决于 recall 命中（`context.py:1557-1583`），与窗口复用本身无关。（老化/预算/召回门限属 D 组。）

**Q3：这里检测到显式话题切换了吗，还是只有自然切换？**
**观察。** 没有。`bridge.py` 与 `inbound_intake.py` 中没有任何话题切换命令或标志；也没有 reset/新建窗口的入口。窗口永远按平台引用复用，话题变化只能"自然"发生（表现为 `context.py` 里 `window_contexts.topic` 随对话更新，见 `context.py:230-236, 1135-1144`）。用户想断开旧语境，只能在管理端归档/改窗口（归档后普通入站仍复用同一窗口，见 §3.3）——**入站侧没有"开新窗口"这一动作**。

**Q4：需要新窗口吗？**
**观察 + 推理。** 按当前设计不需要也不会自动新建：复用就是设计（同群共享窗口，`connectors/onebot/message.py:237-245`；`bridge.py:1575-1576`）。新窗口只在同一引用/实例组合查无匹配时产生（`bridge.py:1620-1635`）。若业务上需要"同一群按话题分窗"，当前入站侧没有实现，也没有配置开关；那是协议/数据模型层的改动，不是调参。

## 证据表

| # | 结论 | 性质 | 证据（文件:行） |
|---|---|---|---|
| 1 | 平台身份提取在连接器层，信封字段 `connector_instance/account_ref/window_ref` | 观察 | `src/ling/contracts/v1.py:311-346`；`connectors/onebot/connector.py:1237-1272`；`connectors/qq/connector.py:222-252` |
| 2 | `window_ref` 形态 `<instance>:group:<gid>` / `<instance>:private:<uid>` | 观察 | `connectors/onebot/message.py:237-245`；`connectors/qqbot/message.py:232-239` |
| 3 | 群聊判定只认第 1 段为 `group` 且第 2 段非空 | 观察 | `src/ling/kernel/visibility.py:100-149` |
| 4 | 群聊 subject 用主人、发言人只作 sender | 观察 | `src/ling/kernel/bridge.py:493-501, 627-629, 1719-1723` |
| 5 | 复用键 = actor + channel + window_ref + instance_key（私聊再加 subject） | 观察 | `src/ling/kernel/bridge.py:1565-1618`；`src/ling/infra/models.py:219-230` |
| 6 | 无匹配才新建，写入 connector_window_ref/origin_* | 观察 | `src/ling/kernel/bridge.py:1620-1635` |
| 7 | 入站侧无 TTL / 归档过滤 / 话题切换 / 强制新建 | 观察 | `src/ling/kernel/bridge.py:1479-1635`（无此类代码）；`src/ling/api/routes/sessions.py:347-354`（归档是管理端） |
| 8 | recent history 按 `window_id` 读 `messages` 表 | 观察 | `src/ling/kernel/context.py:1265-1281, 1487-1492`；`src/ling/domains/conversation/service.py:1036` |
| 9 | summary / 短话题按 `window_id` 读 `window_contexts` | 观察 | `src/ling/kernel/context.py:1135-1144, 1476, 1674-1683` |
| 10 | 平台 message id 映射写 `idempotency_keys`，不参与窗口复用 | 观察 | `src/ling/kernel/bridge.py:1073-1122` |
| 11 | `bridge.handle` 是活路径，每事件构造一次 | 观察 | `src/ling/kernel/inbound_intake.py:289-331`；`src/ling/app.py:258-315` |
| 12 | `group_mention_on_reply` 只存不用 | 观察 | `src/ling/kernel/bridge.py:361, 390`（grep 全文件仅 2 处） |
| 13 | 旧需求进入 prompt 的条件链 | 推理 | 由 #5/#7/#8/#9 组合推出；时长/裁剪属 D 组 |

## 关键结论

- 复用键是 **`(actor_id, channel, window_ref, trusted_instance_key)`**，私聊额外绑定 `subject_id`、群聊不绑定发言人；核心是"平台实例 + 聊天目标"，不是按人或按实例单独复用（`bridge.py:1565-1618`）。
- `window_ref` 由连接器拼成 `<instance>:group:<gid>` / `<instance>:private:<uid>`，群聊窗口只由群号决定；群/私聊判定唯一实现是 `is_group_window_ref`（`connectors/onebot/message.py:237-245`；`visibility.py:100-149`）。
- 命中旧窗口后，后续 recent history 与 summary 全按 `windows.id` 读：`messages.window_id` 与 `window_contexts.window_id`；平台消息 id 映射只是引用元数据，不参与复用（`context.py:1265-1281, 1487-1492, 1674-1683`；`bridge.py:1073-1122`）。
- 入站侧**没有 TTL、没有自动归档/重置、没有显式话题切换命令**；归档由管理端做，且归档后普通入站仍复用同一窗口（`bridge.py:1479-1635`；`api/routes/sessions.py:347-354`）。
- 因此"同一窗口里的旧内容"就是同一 `window_id` 下累积的 `messages` 与 `window_contexts.summary`；条件满足时，一条很旧的产品需求会自然随 history/summary 进入当前 prompt（推理，裁剪细节属 D 组）。
- 显式话题切换在这里**不被检测**，只有自然切换；想要"开新窗口"当前无入站入口也无开关，需要数据模型/协议层改动。
- 新窗口只在查询无匹配时创建；`WindowOriginConflictError` 是对"来源不明/多候选/绑定他实例"的 fail-closed，不会猜一个窗口复用（`bridge.py:1578-1618`）。
- 配置方面：`auto_register_private`、`group_attention_deferred`、`group_turn_budget` 在主路径真被消费；**`group_mention_on_reply` 只被存进字段、无任何读取点**，实际 @ 由模型表达式决定（`bridge.py:361, 390, 2051`）。
- `bridge.handle` 是活路径：事件统一经 `inbound_intake.process_inbound_payload` 每条构造一个 `InboundBridge` 并调用，启动时已装配 `trusted_instance_key`（`inbound_intake.py:289-331`；`app.py:258-315`）。
