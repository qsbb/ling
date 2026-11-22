# Ling 入站窗口的复用 —— 源码证据（src/ling 侧）

> 子智能体 src-ling-inbound 的只读源码取证。HEAD=de43cbe79a78e0c01f7821470500eaa04ea79497，main。
> 观测时刻：2026-11-22 09:30:58+08:00 起（本回合采样时间）。
> 方法：结构性定位（`ast` 取函数 span + `grep` 定位调用点），**最终行号全部来自对当前文件的读取**，不用固定行号开窗。
> LEAD-notes.md（`docs/research/LEAD-notes.md`）只作线索；其行号与本文件有漂移，本文行号以本文件实测为准。
> 未运行真实回放、未跑 pytest；本文结论分「实测（代码路径/AST/grep）」「推理」「转述」三档，推理均标注。

## (1) 观测对象

开始时的原始输出（一次观测，原始抄录）：

```
$ pwd
/Users/lingxi/project/ling
$ git status --short
?? docs/evidence/ling-context-im-async.md
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-offline.md
?? docs/evidence/ling-context-im-private.md
?? docs/evidence/ling-context-research/
?? docs/research/
$ git log -1 --format='%H %s'
de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制
$ git rev-parse HEAD
de43cbe79a78e0c01f7821470500eaa04ea79497
$ git rev-parse --abbrev-ref HEAD
main
$ shasum -a 256 src/ling/kernel/bridge.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
```

**重要更正**：任务卡写「开始时工作树干净」，但实测 `git status --short` **并非字面空**——有 6 个未跟踪项（其他 agent 留下的 `docs/evidence/ling-context-im-*`、`docs/evidence/ling-context-research/`、`docs/research/`，其中含 LEAD-notes.md 本身）。**没有任何 tracked 文件被修改**（无 `M`/`A`/`D`），所以源码观测对象是 HEAD 原文，未受未提交改动污染。报告时不要说「工作树干净」。

中途复查（写报告前，同一次会话，原始输出）：

```
$ shasum -a 256 src/ling/kernel/bridge.py src/ling/kernel/context.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
```

`bridge.py` 两次 sha 相同 → **观测期间被测文件未漂移**。

函数 span 由 `ast.parse` 重新推导（可复现命令见 §6）：

| 函数 | 文件:行（当前文件） |
|---|---|
| `InboundBridge.handle` | `src/ling/kernel/bridge.py:399`–`1052` |
| `_platform_message_id` | `src/ling/kernel/bridge.py:1058`–`1071` |
| `_remember_platform_message_id` | `src/ling/kernel/bridge.py:1073`–`1145` |
| `_ensure_window` | `src/ling/kernel/bridge.py:1479`–`1509` |
| `_resume_window` | `src/ling/kernel/bridge.py:1511`–`1539` |
| `_ensure_connector_instance` | `src/ling/kernel/bridge.py:1541`–`1554` |
| `_find_or_create_window` | `src/ling/kernel/bridge.py:1556`–`1635` |
| `_is_addressed` | `src/ling/kernel/bridge.py:1652`–`1670` |
| `_decide` | `src/ling/kernel/bridge.py:1672`–`1727` |
| `_run_turn` | `src/ling/kernel/bridge.py:1785`–`1923` |
| `_deliver` | `src/ling/kernel/bridge.py:1925`–`2152` |
| `ContextCompiler.compile` | `src/ling/kernel/context.py:548`–`1861` |
| `_message_id_marks` | `src/ling/kernel/context.py:1933`–`1977` |
| `_rollup_summary` | `src/ling/kernel/context.py:1979`–`2024` |
| `is_group_window_ref` | `src/ling/kernel/visibility.py:100`–`149` |
| `process_inbound_payload` | `src/ling/kernel/inbound_intake.py:138`–`385` |
| `TurnService.run_turn` | `src/ling/kernel/turn.py:789`–`873` |

## (2) 入站窗口复用链路（含行号）

### 2.1 顶层入口链

```
平台路由 → process_inbound_payload → InboundBridge.handle → _ensure_window / _resume_window
        → _decide → _ensure_turn → ConversationService.adopt_input → _run_turn → _deliver
```

- `src/ling/api/routes/onebot.py:80`：调 `process_inbound_payload(..., trusted_instance_key=("onebot", instance_id))`（`:84`）。`src/ling/api/routes/qqbot.py:112`，`:116` 同理 `qqbot`。`src/ling/app.py:273,282,307` 的 WS 路径也传可信实例键。
- `src/ling/kernel/inbound_intake.py:305`–`319`：每条事件**新建** `InboundBridge`，把 `settings`、connector、`trusted_instance_key`、`observe_only`、`reply_when_addressed` 等全部注入；`:325`–`331` 调 `bridge.handle()`（ASR 恢复路径带 `resume_message_id` / `resume_window_id`）。
- `bridge.py:415`：`is_group = is_group_window_ref(envelope.window_ref)`，群聊判定走**唯一实现** `visibility.py:100`–`149`（按 `:` 切段，第 2 段恰为 `group` 且第 3 段非空；不是子串匹配）。
- `bridge.py:416`–`464`：身份解析；群聊允许 unknown 号自动建 known 级 subject（`:420`–`429`），私聊只在 `auto_register_private` 开时自动注册（`:436`–`459`），否则拒收。
- `bridge.py:499`–`501`：**窗口 subject**：群窗口归属主人（`identity.owner_subject_id()`），不是发言人；私聊即发言人。这是下面「内部会话标识」的关键：群窗口的 `Window.subject_id` 与消息 `sender_subject_id` 刻意分离。
- `bridge.py:504`–`519`：`begin_nested()` 内二选一：无 `resume_window_id` → `_ensure_window`；有 → `_resume_window`；随后取 `runtime_window_snapshot_in_session`。

### 2.2 `_ensure_window`（`bridge.py:1479`–`1509`）

- `:1487`–`1488`：`channel = connector.connector_id`，`reference = envelope.window_ref`（**平台聊天目标字符串**，原样）。
- `:1489`–`1494`：`instance_key = self._trusted_instance_key`；非 None 时校验 `scope == channel` 且 `instance_id` 非空，否则 `ValueError`；再 `_ensure_connector_instance(scope, instance_id)` 确认实例仍 live、默认实例存在。
- `:1496`–`1502`：取 advisory xact lock，键 = `"ling-window-origin:" + join(actor_id, channel, "group" if is_group else subject_id, reference)`。**群聊用字面量 `"group"`，私聊用 subject_id**；`reference` 进锁键 → 同一平台目标串行化。
- `:1503`–`1509`：转 `_find_or_create_window(...)`。

### 2.3 `_find_or_create_window`（`bridge.py:1556`–`1635`）—— 复用的真实查找键

查找基准 `base`（`:1566`–`1576`）：

- `Window.actor_id == self._actor_id`
- `Window.channel == channel`
- `reference_match`：`Window.connector_window_ref == reference` **或**（`connector_window_ref IS NULL AND title == reference`）——兼容旧数据只写 title 的情形。
- **非群聊**才加 `Window.subject_id == subject_id`；群聊不加（同群所有发言人共享一个窗口）。

实例归属优先级（`:1578`–`1618`）：

1. 有 `instance_key` 时先查 `origin_status='known'` 且 `origin_scope/origin_instance_id` 精确相等（`:1579`–`1589`，`limit(2)`）。**>1 行 → `WindowOriginConflictError("同一平台实例存在多个匹配会话")`；命中 1 行直接复用（`:1593`）。**
2. 再查 `known + origin_manually_bound=True` 但绑到**别的**实例的窗口（`:1595`–`1606`）→ 冲突报错「平台会话已绑定其他实例」，**不静默改绑**。
3. 上述都落空才回退 `origin_status='unknown'` 唯一候选（`:1608`–`1618`）；**>1 行 → 冲突「历史平台会话来源不唯一」**。这就是 `_ensure_window` docstring 说的「旧来源只做唯一候选回退」。
4. 都没有才 `Window(...)` 新建（`:1620`–`1635`）：`title=envelope.window_ref`、`connector_window_ref=reference`、`visibility="group_scope" if is_group else "subject_private"`、`origin_scope/origin_instance_id` 取 instance_key、`origin_status="known" if instance_key else "unknown"`。

**结论（实测，代码路径）**：窗口身份 = `(actor_id, channel, window_ref, visibility, [私聊] subject_id, [可信时] origin_scope+origin_instance_id)`。查找键里**没有任何 topic / 时间 / 换题 / 消息 id 字段**。同平台聊天目标无论隔多久、聊什么，都复用同一个 `Window`；「同一窗口」≠「同一话题」。LEAD-notes §1 的结构性结论成立，但行号以本文件为准。

### 2.4 `_resume_window`（`bridge.py:1511`–`1539`）

仅 ASR 转写恢复走（`handle :510`）。按 `window_id` 取行后逐项校验：未归档、`actor_id` 匹配、`channel` 匹配、`(connector_window_ref or title) == reference`、`visibility` 与 is_group 一致、私聊再校验 `subject_id`；任一不符 → `WindowOriginConflictError("语音来源会话已变化")`。若可信实例键存在且窗口 `origin_status='known'`，还要求 `origin_scope/origin_instance_id` 与之一致，否则「语音来源实例与会话归属不一致」。**它不参与普通入站窗口复用，只防转写恢复跑错会话。**

### 2.5 轮次级复用（旁证）

`bridge.py:1424`–`1467 _ensure_turn`：以 `(window_id, client_turn_id[:128])` 查，`client_turn_id = envelope.external_event_id`（`:798`）。失败/挂起态重置复用，终态直接返回不重跑。这是事件重投的幂等，与窗口复用是两个层级。

## (3) 四类标识与说话人/引用处理

### 3.1 平台聊天标识（`window_ref`）

- 由连接器生成：`connectors/onebot/connector.py:1268`–`1272` 用 `window_ref_for(is_group, target_id=group_id 或 user_id, instance_id=self.instance_id)`，形如 `"<instance>:group:<gid>"` / `"<instance>:private:<uid>"`；`qqbot` 同构（`:536`）。
- `envelope.window_ref` 是 `InteractionEnvelope` 必填字段（`contracts/v1.py:346`）。
- `handle :415` 判群；`:1488` 作查找 reference；`:1625`–`1628` 落成 `title` + `connector_window_ref`。**平台聊天标识被原样当作窗口的持久身份，正常路径无显式换窗口入口。**

### 3.2 内部会话（`Window.subject_id` / `visibility` / origin）

- 群：`window_subject = owner_subject_id`（`:500`–`501`），`visibility="group_scope"`（`:1627`）；发言人记在消息的 `sender_subject_id`（`:628`, `:689`, `:735`, `:839`）。
- 私聊：`window_subject = 发言人 subject_id`，`visibility="subject_private"`，查找键含 subject_id（`:1575`–`1576`）。
- `turn.py:1719`–`1722`（situation_reader）：群聊**不传** `subject_id`，因为群窗口的 subject 是主人、消息可能来自陌生人；把「对主人的熟悉度」当「对说话人的熟悉度」是错误归属。
- origin 三元组：`origin_status` / `origin_scope` / `origin_instance_id`（`bridge.py:1629`–`1631`），受 `settings/connector_instances.py` 管理（`:1541`–`1554`），并有管理端绑定/解绑测试 `tests/integration/test_window_origin_binding.py`。这是**管理员后台能力**，不是聊天里可发的命令。

### 3.3 消息级引用（`reply_to` / `[msg_id:…]` / 出站 quote）

**入站 `reply_to` 的真实作用只有一个：判「是不是冲她说的」。**

- 来源：`connectors/onebot/connector.py:1286` 把 `parsed.reply_to_message_id` 放进 envelope；`qqbot/message.py:65`–`69` 解析 reply 段，`connector.py:542` 附近传 `mentions_actor=parsed.mentions_actor`。
- 契约字段 `contracts/v1.py:362 reply_to: IdStr | None`、`mentions_actor: bool = False`（`:370`）。
- 消费点（AST/grep 全量，`bridge.py` 内）：`:558` `replied_to_actor=bool(envelope.reply_to)` 传给 `_is_addressed`（注意力路径）；`:721` 同样传给 `_decide`（决策路径）。`_is_addressed :1665`–`1670` = `channel_mentioned or text_mentioned or replied_to_actor`；`_decide :1682`–`1683` 同式。注释明确「引用机器人上一条消息也是明确指向」（`:1668`–`1669`）。
- **入站 `reply_to` 不进入提示词，也不决定引用白名单。** 它只影响「回不回」，不作为 `current_message_platform_id`、不改窗口查找、不进 `compile()`。

**模型可见的引用 id 是另一条链：**

- `bridge.py:876`（`:876`）把 `current_message_platform_id=self._platform_message_id(envelope)` 传给 `_run_turn`；`:1058`–`1071` 优先问 connector 的 `platform_message_id_for_event`，否则 `_event_message_id_hint()`（`:189`–`202`，取 `external_event_id` 第一个冒号后的部分）。
- ID 映射写入 `idempotency_keys(scope=platform_msgid, key=<本地消息 id>, result_ref=<平台 id>)`：`bridge.py:1100`–`1122`，SAVEPOINT + `ON CONFLICT DO UPDATE`，失败只记 `platform_message_id_record_failed` 不拒收。每条落库路径（延迟、沉默、回复、ASR）都调 `_remember_platform_message_id`（`:632`, `:692`, `:739`, `:844`）。
- `turn.py:1069` 把 `current_message_platform_id` 传进 `ContextCompiler.compile`；`context.py:1933`–`1977 _message_id_marks` 只对**最近 `HISTORY_MESSAGE_ID_LIMIT=12` 条** user 消息标 `[msg_id:…]`（`context.py:384`，`:1965`），当前消息额外标 `current_mark`（`:1973`–`1976`）。更早消息即使留在原文里也拿不到引用 id（LEAD-notes §4 成立）。
- 出站 quote：`bridge.py:2026` `reply_to = (expression or {}).get("quote")`（**模型自己选的**，不是入站 reply_to），`:2052` 传给 `OutboundMessage.reply_to`；`tag_specs` 有白名单校验（见 `capabilities/tag_specs.py:397`–`423`），对不上只丢引用段、正文照发（`bridge.py:997`–`1007 quote_degraded`）。

### 3.4 用户显式换题 / 新窗口

- **全仓 `src/ling` 无换题/新窗口命令**：对 `换题|新话题|新窗口|/new|/topic|/reset|new_window|new_topic|switch_window|topic_switch|clear_context` 的 grep（§6 命令）在 `src/ling` 只命中注释/无关项（如 `worker.py:1657`「插进新话题的迟到补话」、`api/routes/settings.py:1465` 的**管理员** connectors/reset、`logging_setup.reset_context` 是日志上下文变量）。`api/routes/` 与 `connectors/` 没有 slash command 注册表。
- `Window.archived` 有写入点（`domains/conversation/service.py:551`）和查询过滤，但入站 `_find_or_create_window` **不过滤 archived**；只有 `_resume_window :1525` 检查 `window.archived`。即：即使后台归档了窗口，普通入站只要 reference 命中仍会复用它（**推理**：归档语义在入站复用链上未被尊重；代码路径已实测，行为影响未跑运行时验证）。
- 管理员可通过 `settings/connector_instances` / 窗口绑定改归属（`tests/integration/test_window_origin_binding.py`），但这是后台操作，不是聊天命令。

### 3.5 说话人

- `envelope.sender_name`（本次称呼，群名片优先）与 `sender_nickname`（人级昵称）在 `contracts/v1.py:321`–`342` 分开；`connectors/onebot/connector.py:1259`–`1267` 分开解析。
- `handle :466`–`491` 用 `identity.observe_display_name(... nickname, sender_name ...)` 做旁路同步：人级列 `subjects.display_name` 只写昵称，避免群名片跨群抖动。
- `bridge.py:874`–`875` 把 `sender_name=envelope.sender_name` 透传到 `_run_turn`；`:1830`–`1832` 再传 `run_turn(sender_name=...)`；`turn.py:1066` 传 `compile(user_sender_name=sender_name if is_group_window else None)`。
- `context.py:1692`–`1717`：群聊历史 user 消息按 `sender_subject_id` 查 `Subject.display_name`，经 `normalize_display_name` 过滤占位名（「群友」「用户」），拼成「名字：正文」（`:1735`–`1737`）。
- `context.py:1818`–`1835`：当前消息群聊按本次名片 + 稳定昵称合并署名（不同则「名片（昵称）」），私聊强制清空 `user_sender_name`。

## (4) 停顿 / 顺序 / 当前消息前后提示词块

**入站路径上「停顿」有三处，都不是提示词块：**

1. 决策延迟：`bridge.py:679`–`715` 群普通消息 `deferred_attention`（消息落库标 unseen，不即时决策）；`:614`–`650` 群里「只有 @ 没正文」时 `GroupAttentionScheduler.await_supplement(window.id)` 把首次轮询推迟——`src/ling/domains/attention/service.py:60` `MENTION_WAIT_SECONDS=8`、`:65` `MENTION_HARD_DEADLINE_SECONDS=12`、`:240`–`272 await_supplement`。这些是**时间调度**，不是给模型看的文字块。
2. 出站段间节奏：`bridge.py:104` `async def _sleep`、`117`–`168 WindowSendGate.pace(first)`、`94`–`97` 常量（基础间隔复用 `chunking.PART_DELAY_SECONDS=1.2`、抖动 0.85–1.25、令牌桶容量 3/每 3 秒补 1）。注释明确「拟人节奏，不是限速」（`:82`–`90`）、「段间停顿是模拟节奏，绝不写进 delivery 回执」（`:1954`）。**纯出站工程节奏，不进入上下文编译。**
3. 生成超时 180 秒（`bridge.py:99`–`101 TURN_WAIT_TIMEOUT_SECONDS=180.0`）：`_run_turn :1843`–`1859` 超时不丢正文，转 `_record_pending_delivery` 续发。

**顺序（实测，代码路径）：**

- 提示词的**实际发送顺序**由 `context.py compile()` 里的 `append()` 次序决定，不是声明表。`prompt_zones.py:25`–`37` 明说三种顺序（消息顺序/区内牺牲/区间牺牲）不同；`:418`–`449` 记录「当前实际发送顺序」为 PR 3 之前的事实；`:548`–`557` 有 `validate_*` 校验摘要/历史/正文三者顺序递增。
- 各车道（`ContextContribution` → `annotated`）先按 append 顺序进入 `annotated`（`context.py:1663`–`1671`）。其中**摘要块**在 `context.py:1673`–`1685` **紧跟在车道之后、历史之前**无条件插入（只要 `window_ctx.summary` 非空；短记忆 TTL 由 `:230`–`244 _window_topic_live` + `:1129`–`1157` 控制，摘要不判 TTL）。注释/LEAD-notes 所述「摘要常驻、与当前话题无关」成立。
- 然后逐条历史消息（`:1732`–`1789`，role user→`user`，其余→`assistant`），再溢出 rollup（`:1791`–`1805`），然后**当前消息**块（`:1836`–`1847`，`key="input"`, `lane="input"`, role=user，带 `[msg_id:…]` 与署名）。当前消息是 `annotated` 的**最后一条**；其上家是历史 user/assistant 消息、再往上是摘要、再往上是各系统车道。
- 当前消息之后**没有**任何「说明/提示/后处理」块被追加：`compile()` 在 `:1851`–`1861` 直接返回 `CompiledContext(messages=[...for m in annotated])`。即「当前消息前后提示词块」在入站路径表现为：前=车道系统块+摘要+历史，后=无。
- 引用校验用 `reply_ids`（`context.py:1848`–`1860`），不与当前消息块同处一条消息，而是单独的映射字段；`turn.py:1075` 取出给收尾用。

## (5) 显式换题 / 新窗口是否必要

**结论（实测+推理，置信度中高）：在现有入站路径里，「显式换题」既无实现也无必要的前提——因为窗口复用本来就不含话题状态。**

- 复用键里没有话题字段（§2.3），所以「换题」不会、也不需要触发新窗口；要保证话题隔离只能靠**不同平台会话目标（不同 `window_ref`）**或管理员改归属/绑定，而不是聊天命令。
- LEAD-notes 担心的「旧话题随摘要常驻」是**上下文内容**问题，不是**窗口身份**问题：摘要常驻由 `context.py:1673`–`1685` 决定，与是否新建窗口无关；即使开了新窗口，只要 `window_ctx.summary` 有内容仍会带出（新窗口 summary 为空才不带）。因此「为换题而开新窗口」在现有代码里解决不了摘要常驻。
- 若要让「换题」真正生效，改动点应在短记忆/摘要注入或新增话题状态字段，而不是 `_find_or_create_window`；但本次是只读研究，不改代码。
- 反例边界：`_resume_window` 检查 archived，`_find_or_create_window` 不查——如果将来引入「归档旧窗口=换题」的语义，入站复用链会绕过它（§3.4）。

## (6) 配置/命令是否真接到主链路

| 项 | 声明处 | 是否接到主链路 | 证据 |
|---|---|---|---|
| `trusted_instance_key` | `bridge.py:371,394` | **是，真接** | 路由 `onebot.py:84`/`qqbot.py:116`/`app.py:273` 注入 → `inbound_intake.py:140,318` → `bridge.py:1489-1494,1533-1538` 实用于查找/恢复校验 |
| `auto_register_private` | `bridge.py:364,391` | **是** | `inbound_intake.py:314` 注入 → `bridge.py:436-459` 实用于建号 |
| `observe_only` / `reply_when_addressed` | `bridge.py:365-369,392-393` | **是** | `inbound_intake.py:315-316` 注入（来自 access 判定）→ `bridge.py:560-562,679-680` |
| `group_mention_on_reply` | `config.py:148`, `bridge.py:361,390` | **否——只声明未使用** | AST 全量扫描 `src/ling`：`_group_mention_on_reply` 仅有 `:390` 一处 STORE，**零 LOAD**。名字像「群里回复 @ 发言人」，实际表达方式是模型自选（`bridge.py:2049-2051 expression["at"]`），这个开关是死配置 |
| `group_attention_deferred` | `settings/window_policy.py` | **是** | `bridge.py:1638-1650 _defer_group_attention` 从 runtime snapshot 读并用于 `:680` 决策分支 |
| `channel_capabilities` | `bridge.py:1750-1783`, `turn.py:822` | **是** | `bridge.py:1827` → `turn.py:1043` → `context.py` 的 `supports_poke` 等，`tag_specs` 收尾校验 |
| 显式换题/新窗口命令 | 无 | **不存在** | §3.4 grep 全仓无结果 |

**特别提示 lead**：`group_mention_on_reply` 是「名字比实现强」的典型——配置表单有、构造参数有、字段也存了，但没有任何读取点。如果调研假设「配置群里回复 @ 发言人」能影响入站行为，那条假设**不成立**，不要据此设计实验。

## (7) 未取得与失败命令

- **未取得**：真实入站回放/运行时快照（未跑 pytest、未连数据库）。所有结论是代码路径证据，不是运行时验证。
- **未取得**：`git status` 字面干净——实测有 6 个未跟踪项（其他 agent 的文档），已在本文件 §1 如实记录；tracked 源码无改动。
- **未运行**：`_find_or_create_window` 的冲突分支（多候选/manually_bound）没有跑运行时测试；只有静态代码路径 + `tests/integration/test_window_origin_binding.py` 的测试名（该文件测的是绑定/解绑管理端流程，未逐条执行）。
- **命令失败/零结果及解释**：
  - `grep -rn "InteractionEnvelope(" src/ling --include='*.py'` → 只命中 `contracts/v1.py:311` 的类定义本身，零构造点。原因：连接器不在 `src/ling`，在顶层 `connectors/`；已用 `grep -rln "normalize_inbound" .` 找到 `connectors/{onebot,qqbot,...}` 并补查（`:1262` 处构造）。零结果不是「没接线」。
  - `grep -rn "reply_to" src/ling/api src/ling/integrations ...` → 零命中。原因同上，连接器在顶层 `connectors/`。
  - `python3` AST 查 `worker.py` 的 `def run_turn` → 零命中。原因：`run_turn` 定义在 `src/ling/kernel/turn.py:789`，worker 只是调用方；已改在 turn.py 定位。
  - 上述零结果均按「先怀疑定位方式，再怀疑事实」处理，未据此下结论。

## (8) 可复现命令

```bash
cd /Users/lingxi/project/ling
# 观测对象
git status --short && git log -1 --format='%H %s' && git rev-parse HEAD && git rev-parse --abbrev-ref HEAD
shasum -a 256 src/ling/kernel/bridge.py src/ling/kernel/context.py

# 结构性定位函数 span（不用固定行号开窗）
python3 - <<'PY'
import ast
for p in ['src/ling/kernel/bridge.py','src/ling/kernel/context.py',
          'src/ling/kernel/inbound_intake.py','src/ling/kernel/turn.py']:
    t=ast.parse(open(p,encoding='utf-8').read())
    for n in ast.walk(t):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {
            'handle','_ensure_window','_find_or_create_window','_resume_window',
            '_is_addressed','_decide','_run_turn','_deliver','_message_id_marks',
            'compile','process_inbound_payload','run_turn'}:
            print(p,n.name,n.lineno,n.end_lineno)
PY

# 四类标识与接线
grep -rn "reply_to\|mentions_actor\|replied_to_actor" src/ling connectors --include='*.py'
grep -rn "origin_scope\|origin_instance_id\|origin_manually_bound" src/ling --include='*.py'
grep -rn "trusted_instance_key" src/ling --include='*.py'
grep -rn "InboundBridge(" src/ling --include='*.py'

# 死配置确认（应只有 1 行 STORE）
grep -rn "_group_mention_on_reply" src/ling --include='*.py'

# 换题/新窗口命令（预期无业务命中）
grep -rniE "换题|新话题|新窗口|/new|/topic|new_window|new_topic|switch_window|topic_switch" src/ling --include='*.py'

# 中文损坏自检（预期输出「无」）
grep -rn $'\ufffd' docs/evidence/ling-context-src-ling-inbound.md && echo "发现损坏" || echo "无"

# 注意 macOS /bin/bash 会把 $'\ufffd' 展开成 6 个 ASCII 字符 \ufffd，
# 于是上面这条会命中本报告第 246 行「命令文本本身」而假阳性。
# 权威判定用 Python 真码点检查（本报告实测输出 NONE）：
python3 - <<'PY'
p='docs/evidence/ling-context-src-ling-inbound.md'
s=open(p,encoding='utf-8').read()
print([i for i,l in enumerate(s.splitlines(),1) if '\ufffd' in l] or "NONE")
PY
```
