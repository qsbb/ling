# S3 — 追 Ling 入站窗口的复用（bridge.py 取证）

> 只读源码取证。被测文件 `src/ling/kernel/bridge.py` 冻结，未做任何改动；唯一写入产物为本文档。
> 所有结论的代码位置均为**实测**（read/grep 于下述观测时点直接读到）；标注「推理」的段落是基于代码结构的判断，未做运行时实验；标注「未核实」的段落需运行时或测试佐证。

## 0. 观测纪律 / 钉住对象（实测，原始输出抄录）

开始前（2026-11-22 约 09:25 +08）在 `/Users/lingxi/project/ling` 跑：

```
$ git status --short
(空输出)
$ git log -1 --format='%H %s'
de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制
$ shasum -a 256 src/ling/kernel/bridge.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
$ ls -la docs/research/S3-bridge.md
ls: docs/research/S3-bridge.md: No such file or directory
```

- HEAD 与给定值 `de43cbe79a78e0c01f7821470500eaa04ea79497` 一致（实测）。
- bridge.py sha256 与给定值 `334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70` 一致（实测）。
- 目标文件写作前不存在（实测）；`docs/research/` 原为空目录。
- 全文 `bridge.py` 共 2175 行，已逐段通读（1–1084、1085–1684、1685–2175）。下方行号对应上述 sha256 的版本；若文件变动须重读再核对。

## 1. 按平台实例（instance）与聊天目标（chat target）复用窗口的逻辑

### 1.1 两个入参从哪来（实测）

- `InteractionEnvelope.window_ref`（`contracts/v1.py:346`，类型 `IdStr`，无默认值）：平台聊天标识字符串，约定拼法 `"<instance>:group:<gid>"` / `"<instance>:private:<uid>"`（`visibility.py:106-108`）。
- `trusted_instance_key: tuple[str, str] | None`（`bridge.py:371`，默认 `None`）：受信实例身份 `(scope, instance_id)`，由路由/网关传入，**不从账号命名空间推断**（`bridge.py:370` 注释）。上游实际传值：
  - OneBot HTTP 入口 `api/routes/onebot.py:84` 传 `("onebot", instance_id)`（`instance_id` 路由默认值 `"default"`，见 `onebot.py:91`）。
  - qqbot 入口 `api/routes/qqbot.py:116` 传 `("qqbot", instance_id)`。
  - Web 路径不传（用 `None`，走 `origin_status="not_applicable"` 的另一套；见 `infra/models.py:210-216` 的 CheckConstraint）。
  - `inbound_intake.process_inbound_payload(..., trusted_instance_key=None)`（`inbound_intake.py:140`）原样透传给 `InboundBridge`（`inbound_intake.py:305-319`）。
- `instance_id` 默认值 `"default"`：`infra/models.py:711` `DEFAULT_INSTANCE_ID = "default"`；`_ensure_connector_instance`（`bridge.py:1541-1554`）在 `instance_id == DEFAULT_INSTANCE_ID` 时调 `ConnectorInstanceService.ensure_default(scope)`，再统一 `require_live(scope, instance_id)`；实例不存在或被停用 → `ValueError("平台入站连接器实例已不存在或已停用")`。

### 1.2 复用的键（key）与默认值（实测）

入口在 `InboundBridge.handle()` 第 2 步 `bridge.py:493-524`：

- `is_group = is_group_window_ref(envelope.window_ref)`（`bridge.py:415`；唯一实现在 `visibility.py:100-149`，按 `:` 切段、要求第 1 段（索引 1）恰为 `"group"` 且第 2 段非空；不再是 `":group:" in ref` 子串）。
- 群聊窗口主人 `window_subject = await identity.owner_subject_id() or subject_id`（`bridge.py:499-501`）——群窗口 subject 指向角色主人，不是第一个发言人。私聊 `window_subject = subject_id`（发言人本人）。
- `resume_window_id is None` → `_ensure_window(...)`；否则 `_resume_window(...)`（`bridge.py:504-516`）。语音转写恢复才传 `resume_window_id`。

**窗口匹配键**（`_find_or_create_window`，`bridge.py:1556-1635`）的查询条件：

- `Window.actor_id == self._actor_id`（默认 `"actor_ling"`，`bridge.py:376`）
- `Window.channel == channel`，其中 `channel = str(getattr(self._connector, "connector_id", "unknown"))`（`bridge.py:1487`；取不到时字面量 `"unknown"`）
- `reference_match`：`Window.connector_window_ref == reference` **或**（`connector_window_ref IS NULL AND title == reference`），`reference = str(envelope.window_ref or "")`（`bridge.py:1488,1565-1569`）
- 私聊额外加 `Window.subject_id == subject_id`（`bridge.py:1575-1576`；群聊不加，按群共享）
- 有 `instance_key` 时，优先在以上 base 上再精确匹配 `origin_status == "known"` 且 `origin_scope == instance_key[0]` 且 `origin_instance_id == instance_key[1]`（`bridge.py:1578-1593`）。

**锁定键**（并发去重，`_ensure_window`，`bridge.py:1496-1502`）：

```
lock_key = "ling-window-origin:" + ":".join((self._actor_id, channel,
            "group" if is_group else subject_id, reference))
SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))
```

即锁粒度 = actor + 渠道 + (群/私聊发言人) + 平台聊天目标；`instance_key` 不进锁 key（同一实例归属冲突靠下面 `WindowOriginConflictError` 在事务内拒绝）。

### 1.3 新建 / 复用 / 清理（实测）

**新建**（`bridge.py:1620-1635`）：以上候选全部落空时构造 `Window`：

| 字段 | 取值 | 位置 |
|---|---|---|
| `id` | `win_` + uuid4 hex 前 24 位 | 1621 |
| `actor_id` | `self._actor_id`（默认 `actor_ling`） | 1622 |
| `persona_id` | `str(self._persona_id or "")`；随后在 `handle()` 用运行时快照覆写 `runtime_snapshot.resolved.persona_id.value`（`bridge.py:520-522`） | 1623 |
| `subject_id` | `window_subject`（群=主人、私聊=发言人） | 1624 |
| `title` | `envelope.window_ref`（原样，未摘要） | 1625 |
| `channel` | connector.connector_id（缺则 `"unknown"`） | 1626 |
| `visibility` | `"group_scope"` 若群，否则 `"subject_private"` | 1627 |
| `connector_window_ref` | `reference` | 1628 |
| `origin_scope` / `origin_instance_id` | `None` 若 `instance_key is None`，否则 `instance_key[0]/[1]` | 1629-1630 |
| `origin_status` | `"unknown"` 若 `instance_key is None`，否则 `"known"` | 1631 |

**复用（同实例）**：精确候选命中 → 直接 `return exact_rows[0], False`（`1592-1593`）。候选 >1 → `WindowOriginConflictError("同一平台实例存在多个匹配会话")`（`1590-1591`）。

**复用（旧来源回退，仅当 `instance_key` 存在时）**：精确候选不存在时，查是否存在 `origin_status == "known"` 且 `origin_manually_bound IS TRUE` 且归属**另一个实例**的窗口 → `WindowOriginConflictError("平台会话已绑定其他实例，请先检查会话归属")`（`1595-1606`）。再找 `origin_status == "unknown"` 的历史候选：>1 → `WindowOriginConflictError("历史平台会话来源不唯一，无法安全归属")`；唯一 → 复用（`1608-1618`）。这是「旧来源只做唯一候选回退」（docstring `bridge.py:1486`）。

**当 `instance_key is None`（Web / 老连接器）**：跳过上述精确/绑定/未知判定，直接在 base 里取第一条匹配（代码 `base` 未加 `.limit()`，`scalars()` 全取后由 SQL 顺序决定第一个；实测代码未显式排序——**推理**：此时若同键存在多行，取哪一行不确定，但生产上 Web 路径 `origin_status="not_applicable"`，见 `models.py:210-216` 约束，不会与这里生成的 `unknown` 冲突）。

**恢复校验**（`_resume_window`，`bridge.py:1511-1539`）：窗口不存在 / `archived` / `actor_id` 不符 / `channel` 不符 / `(connector_window_ref or title) != reference` / `visibility` 与 is_group 不符 / 私聊且 subject 不符 → `WindowOriginConflictError("语音来源会话已变化，无法恢复处理")`；若本轮有受信实例且窗口 `origin_status=="known"` 而归属不同 → `WindowOriginConflictError("语音来源实例与会话归属不一致")`。

**清理**：`bridge.py` 内**没有窗口归档/删除逻辑**。只读到两处对 `archived` 的消费：`_voice_delivery_still_allowed`（`bridge.py:67`，归档窗口不再发语音）和 `_resume_window`（`bridge.py:1525`）。归档由外部完成：`api/routes/sessions.py:354` 归档单会话；`worker.py:4009-4044` 周期归档/压缩；`domains/conversation/service.py:530-551` 可设 `archived` 标志。窗口归档后，`_find_or_create_window` 的 select **不带** `archived IS FALSE` 过滤（`bridge.py:1566-1574`），**推理**：若归档窗口仍匹配，`_ensure_window` 会把它当作普通候选复用（只是后续 worker 侧可能拒绝写入），这是潜在 Gap，未做运行时核实。

**不按停顿时间新建窗口**：全文件没有「时间间隔超阈值→开新窗口」逻辑。唯一与时间相关的窗口语义是短记忆 TTL（`context.py:230-244,1129-1156`，120 分钟默认，仅使「本窗口正在聊」不注入，**不**使窗口失效）和 `_recent_actor_turns` 的 5 分钟防刷屏计数（`bridge.py:1729-1748`）。用户显式「换题」信号：在 `bridge.py` 中**未发现**任何检测/处理（grep `换题|新话题|topic|reset_window|new_window|话题` 在该文件仅命中 `:1732` 的注释）。跨文件的相关信号只有召回侧的 `needs_recent_topic`（`domains/memory/recall_query.py:25-27`，匹配「后来呢/然后呢/哪家/几点/原话…」→ 把上一句用户话拼到 current query；见 `context.py:1557-1566`），以及 `should_skip_automatic_recall`（同文件 `:19-22`，寒暄跳过自动召回）——它们只改召回 query，**不改窗口选择**。

### 1.4 与窗口并列的「会话唯一性」键（轮次层，实测）

`_ensure_turn`（`bridge.py:1424-1467`）：`Turn.window_id == window.id` 且 `Turn.client_turn_id == client_turn_id[:128]`，而 `client_turn_id = envelope.external_event_id`（`bridge.py:798`）。已存在且 state ∈ {failed, pending, stopping} → 重置为 pending 复用（保留审计）；终态直接返回不再跑。这是**轮次**幂等键，不是窗口键，但同样决定「同一条入站是否重复生成」。

## 2. 六类概念的代码对应（实测）

| 概念 | 代码对应 | 位置 | 说明 |
|---|---|---|---|
| 平台聊天标识 | `envelope.window_ref`；`reference` / `connector_window_ref` / `title` | `contracts/v1.py:346`；`bridge.py:1488,1565,1625,1628` | 形如 `"onebot:group:123"` / `"onebot:private:100200300"`；决定群/私聊与窗口匹配 |
| 内部会话 id | `Window.id` (`win_<24hex>`)；`window_id` 贯穿 turn/message | `bridge.py:1621`；`handle()` 各处 | 事务内主键；`resume_window_id` 是转写恢复用的显式回指（`bridge.py:404,510`） |
| 最近历史 | `ConversationService.messages_for_prompt(window_id, after_sequence=summary_through_sequence, before_sequence=cursor)`；`usable` 从新到旧取、按 `HISTORY_TOKEN_BUDGET` 截断 | `context.py:1486-1529,1679-1685`；`conversation/service.py:1307-1351` | 只读 `sequence > summary_through_sequence` 的未压缩消息；`HISTORY_FETCH_CAP` 触顶则记 `history_cap_hit=true`（`context.py:1517-1547`） |
| 摘要 | `WindowContext.summary` / `summary_through_sequence` / `summary_message_count`；`render_rollup(dropped, prev_summary)` 确定性压缩，写回时 `summary[:8000]` | `context.py:1673-1685,1987-2024`；`conversation/service.py:1445-1466` | 注入为 role=system、key=`"summary"` 的块：`"本窗口较早的对话摘要（截至第 N 条，共 M 条）：\n{summary}"` |
| 长期记忆 | `self._memory.recall(subject_id, query, include_history, visibility_allow, limit, trace_window_id, audience)` → `RecallResult`；受 `snapshot.allows_long_term_memory()` 与 `should_skip_automatic_recall` 门控 | `context.py:1549-1575`；`recall_query.py:19-22` | query 默认是 `user_text`；命中 `needs_recent_topic` 则把上一条相关用户话拼成 `f"{上一句[:180]}\n{user_text}"`（`context.py:1557-1566`） |
| 用户显式换题信号 | **bridge.py 中没有实现**（见 1.3 末）；仅有召回侧的 `needs_recent_topic` 被识别，语义相反（是「接上上一题」而非「换新题」） | `recall_query.py:13-16,25-27` | 若要在入站侧支持「换题」，目前无现成钩子；窗口不会因一句话重置 |

补充：「短期窗口话题」是另一类：`WindowContext.topic/open_questions/expires_at`，TTL 默认 120 分钟（`conversation/service.py:1412-1443`），注入为 `lane="short_memory"`（`"本窗口正在聊：…"`，`context.py:1135-1156`）；过期只让这段不注入，摘要仍保留（`context.py:230-244,1129-1134`）。跨端私聊交接胶囊 `HandoffService.read(...)` 也注入 `short_memory`（`context.py:1216-1247`），仅私聊、带过期时间提示。

## 3. 停顿 / 消息顺序 / 说话人标识在入站时怎么处理（实测）

**停顿（时间间隔）**：入站侧不参与窗口选择，只参与两件事——
- 决策防刷屏：`_recent_actor_turns` 数最近 **5 分钟**内本窗口 `role="actor"` 的消息数（`bridge.py:1729-1748`），喂给 `DecisionContext.recent_actor_turns`（`bridge.py:1704`）抑制连发。
- 短记忆 TTL 使「本窗口正在聊」过期（`context.py:230-244`），摘要不过期。
- 出站节奏（与入站窗口无关）：`PART_INTERVAL_JITTER=(0.85,1.25)`、`SEND_BUCKET_CAPACITY=3.0`、`SEND_BUCKET_REFILL_SECONDS=3.0`、`WINDOW_GATE_LIMIT=256`（`bridge.py:94-97`），基础间隔复用 `chunking.PART_DELAY_SECONDS`（1.2 s，`bridge.py:92,110-114`）。`_gate_for(envelope.window_ref)` 以**平台 window_ref** 为键（`bridge.py:174-181,2028`），与内部 `window.id` 不同——**推理**：两个不同实例若 window_ref 同名，闸门会串；但 window_ref 通常已含实例段，实际冲突概率低，未核实。

**消息顺序**：
- 落库顺序由 `ConversationService.next_sequence(window_id)` 给出：`max(Message.sequence)+1`，无则 1（`conversation/service.py:791-797`）；`add_message` 写入 `sequence`（`:817-825`）。`resume_message_id` 走 `adopt_input` 复用已有行、不重复落（`:847-860`）。
- 上下文按 `sequence` 取（`messages_for_prompt` ASC，`context.py:1487,1520,1699`）；先「新→旧」累加预算再 `usable.reverse()` 还原旧→新（`context.py:1497-1529`）。
- 重投语义：`_ensure_turn` 用 `client_turn_id` 让失败轮次复位重跑；`_resumable_reply`/`_resume_delivery`（`bridge.py:1149-1238`）对已有正文只续发剩余段；段级幂等键 `plan_{turn_id}` + 序号（`bridge.py:2017-2019,2033-2063`）。
- 顺序对窗口选择的影响：**无**。窗口选择只看 actor/channel/reference/is_group/subject/instance origin，不看 sequence 或时间。

**说话人标识**：
- 入站信封两个字段：`sender_name`（本次称呼，群名片优先，`contracts/v1.py:326-329`）与 `sender_nickname`（人级昵称，跨群稳定，`:339-342`）。
- 身份解析用 `(connector_instance, account_ref)`，昵称只作标签（`bridge.py:417-419,485-491`）；群窗口主人另取 `owner_subject_id`（`bridge.py:500-501`）。
- 当前消息落库带 `sender_subject_id=subject_id`（发言人；`bridge.py:831-840`），群里的「谁说的」由 `context.py:1693-1717` 按 `sender_subject_id` 批量查 `Subject.display_name` 给历史每条署名；当前这条由 `bridge.handle()` 把 `sender_name=envelope.sender_name` 透传给 `_run_turn`（`bridge.py:875`）→ `run_turn(sender_name=...)`（`bridge.py:1831`）→ `compile(user_sender_name=...)`，在 `context.py:1818-1836` 处理：群名片与库内昵称不同时拼成 `名片（昵称）`，私聊强制清空前缀。
- 对窗口选择的影响：群聊**不按发言人分窗**（`bridge.py:1575-1576` 私聊才加 subject 条件）；发言人只影响署名与记忆归属，不改窗口键。

## 4. 当前消息前后的提示词块怎么标记（实测）

在 `ContextCompiler.compile()`（`context.py:548-566`）装配的 `annotated: list[CompiledMessage]`（`context.py:1660-1671`）中：

- **当前消息之前**：
  - 各车道 contributions 先注入，全部 `role="system"`，`key=lane_key(c.lane)`（如 short_memory 的车道键；`context.py:1663-1671`）。
  - 摘要块若为 system：`role="system"`, `key="summary"`, `lane="summary"`，正文格式见第 2 节（`context.py:1673-1685`）。
  - 历史消息逐条：`role="user"`/`"assistant"`，`key`/`lane` 来自消息自身（群聊 user 消息正文前置 `"{名字}："`，`context.py:1732-1757`）。
- **当前消息**：
  - `role="user"`，`key="input"`，`lane="input"`，`sender=user_sender_name`（`context.py:1839-1847`）。
  - 正文 `current = f"{user_sender_name}：{user_text}"`（群聊署名，私聊无前缀）；若取到平台 id，最前面再拼 `MESSAGE_ID_MARKER.format(token=current_mark)`，即 **`[msg_id:<token>] <名字：>正文`**（`context.py:1836-1838`；marker 定义 `:372`）。
  - 历史里的入站消息同样只给最近 `HISTORY_MESSAGE_ID_LIMIT = 12` 条标 `[msg_id:…]`（`context.py:381-384,1961-1971`）；她自己的发言标 `[recall:{ref}]`（本地消息 id，`:373-379`）。
  - token→原始平台 id 的映射 `reply_ids` 含当前这条（`:1848-1850`），用于出站引用核对。
- 当前消息**之后**没有额外注入块；`CompiledContext.messages` 以这条 input 收尾（`:1851-1852`）。

桥侧相关参数：`_run_turn()` 传 `current_message_platform_id=self._platform_message_id(envelope)`（`bridge.py:876`）；后者优先问连接器的 `platform_message_id_for_event(external_event_id)`，否则 `_event_message_id_hint` 取第一个冒号后的部分（`bridge.py:1058-1071,189-202`）。`_remember_platform_message_id` 以 `idempotency_keys(scope="platform_msgid", key=本地消息id, result_ref=平台id)` 落库（`bridge.py:1073-1145`；scope 常量 `context.py:387`）。

## 5. 同一窗口内「旧话 / 能接住的上一句 / 当前问题」的代码层预测（推理，基于以上实测结构）

设一个已存在窗口：摘要截至第 N 条，原文为第 N+1…M 条，当前问题是第 M+1 条。

**现有逻辑会带出的内容（推理）**：

1. 第 N+1…M 条原文中，按 token 预算从新往旧保留的部分（`context.py:1486-1529`）——「旧话」里**近期的**那批；含 `[msg_id:…]`（最近 12 条用户消息，若有平台 id）与群聊署名。
2. 第 1…N 条的确定性摘要（`context.py:1673-1685`），格式带「截至第 N 条，共 M 条」；摘要保留头尾（`context.py:2002-2015`），所以最早的约定与最近话题各有位置。
3. `WindowContext.topic/open_questions`（未过 TTL 时，`context.py:1137-1156`）——「这窗口正在聊什么」会被点名注入。
4. 以当前问题为 query 的长期记忆召回（`context.py:1567-1575`）；若当前问题命中 `needs_recent_topic`（「然后呢？」类，`recall_query.py:13-16`），query 会拼上最近一条相关用户话（`:1557-1566`）——这就是代码层唯一「接住上一句」的机制。
5. 当前问题本身，带 `[msg_id:…]`（若取到平台 id）与群名片前缀（`context.py:1836-1847`）。

**现有逻辑会漏/降级的内容（推理）**：

- 超出 `HISTORY_TOKEN_BUDGET` 的更早原文被 `dropped` 并压进摘要；摘要是逐条截取要点、非逐字，细节会丢（`context.py:1989-1993`）。若 `HISTORY_FETCH_CAP` 触顶，还有未摘要的更老消息被「摘要覆盖」（`context.py:1537-1547`）。
- 摘要自身超 `SUMMARY_TOKEN_BUDGET*2` 时中间被省略（`context.py:2007-2015`）。
- 当前问题若命中 `should_skip_automatic_recall`（「嗯/在吗/ok」等，`recall_query.py:6-11,19-22`），自动长期记忆召回**整块跳过**（`context.py:1552-1554`）——此时上一句即便在原文里，也不会被召回侧额外关联。
- 取不到平台 id 的消息不标 `[msg_id:…]`（`context.py:1942-1943,1965-1968`），模型无法引用它们；短 id 表 FIFO 已淘汰的旧 id 也只标最近 12 条（`:381-384`）。
- 「说话人」坐落在不同窗口（私聊改另一个 subject、群换成另一个群）时，历史不跨窗；只有私聊间 `HandoffService` 胶囊（短 TTL）能把「另一个窗口刚聊的话题」带过来（`context.py:1216-1247`）；群聊明确不注入胶囊。
- **用户显式说「换个话题 / 别提刚才那个」**：代码层无任何钩子（1.3 末），这句话只作为下一轮 user_text 参与决策/召回/生成，窗口与摘要不会重置——除非模型自己在回复里「接住」它。
- 当前消息前后块的 key/lane 标记不携带「这是旧话」语义：历史与当前都是 `role=user/assistant`，摘要/短记忆才是 `role=system`（`context.py:1663-1685,1732-1757,1839-1847`）——**推理**：模型能否区分「记忆」与「对方刚说」全靠文本前缀（"本窗口较早的对话摘要…"、"本窗口正在聊…"），而非结构化角色。

## 6. 新窗口作为定位对照（推理）

「开新窗口」在本系统里 = 换 `window_ref` 或换 subject/实例，使 `_find_or_create_window` 匹配不到旧行而新建（`bridge.py:1620-1635`）；也可由用户/管理端把旧窗口归档（`sessions.py:354`、`conversation/service.py:530-551`）。注意：**不存在按时间/换题自动开新窗的代码路径**。

**改新窗口能改善的情况（推理）**：

- 旧窗口的历史/摘要属于**已经结束的话题**，继续带着会让模型把旧语境当当前（「接错题」）。新窗口 = 干净的 `summary_through_sequence=0`、无历史、无 `WindowContext.topic`，当前问题单独成文，不会再被旧话牵引。
- 旧窗口 `origin_status="unknown"`、来源归属可疑（`bridge.py:1608-1618`）或窗口被跨实例复用时，新窗口配受信实例能拿到干净的 `origin_status="known"` + 正确 `(scope, instance_id)`，避免归属歧义与 `WindowOriginConflictError`。
- 群聊刷屏导致大量 `unseen`/`ignored` 消息积压（`bridge.py:679-715,723-774`），历史预算被无关注消息吃掉；新窗口能让上下文只含当前对话。
- 短记忆 TTL 已过、摘要仍残留在旧窗口（`context.py:230-244,1129-1134`）：新窗口彻底去掉这条残留。

**改新窗口反而丢上下文的情况（推理）**：

- 当前问题需要「接住上一句」（「然后呢？/她刚说的那家公司」）：上一句在旧窗口的未摘要原文或 `[msg_id:…]` 最近 12 条里；新窗口两者皆无，只剩当前问题 + 长期记忆召回（`context.py:1557-1575`），而 `needs_recent_topic` 的拼上一句逻辑依赖 `usable` 里**同窗**的上一条 user 消息（`:1558-1566`）——新窗口里 `usable` 为空，拼不上，上下文直接断。
- 引用需求：旧窗口历史里的 `[msg_id:…]` 让模型能引用对方消息（`context.py:1719-1730,1961-1971`）；新窗口没有可引用对象，`quote_dropped` 风险升高（`bridge.py:997-1007`）。
- 群聊里「同一群共享窗口」是刻意设计（`bridge.py:493-501,1575-1576`）；若按发言人强行开新窗，群内多人的前后话被拆散，署名再全也串不起对话。
- 早期约定只存在于旧摘要的头段（`context.py:2002-2015`）：新窗口丢掉它，后续回答会与之前承诺冲突，而长期记忆召回不一定命中（取决于 query）。
- 私聊换窗口后，`HandoffService` 胶囊（`context.py:1216-1247`）只保留短时话题摘要，逐字上下文仍丢失；TTL 过后再换窗口则胶囊也失效。
- 归档旧窗口并不能让 `_find_or_create_window` 自动跳过它（1.3 末的 Gap，未核实），所以「归档了旧窗口就等于开新窗」这个预期不成立——需要先让 `window_ref`/subject 变化，或确认 `archived` 对查窗口路径的实际影响（未做运行时验证）。

## 7. 置信度与未核实清单

- **实测**：第 0–4 节所有行号、默认值、查询条件、标记格式，均来自本会话 read/grep 的直接输出；bridge.py sha256 与给定值一致。
- **推理（未做运行时实验）**：第 5 节「带出/漏掉」的逐条预测、第 6 节新窗口利弊——依据是 `compile()` 的装配顺序、`recall_query.py` 的准入规则与 `_find_or_create_window` 的匹配条件，未跑真实 turn 验证。
- **未核实**：
  1. `archived` 窗口是否会被 `_ensure_window` 实际复用（代码未过滤，但下游 worker 侧有 `archived IS FALSE` 检查，净效果未测）。
  2. 闸门 `_gate_for(envelope.window_ref)` 跨实例同名 window_ref 串扰的实际发生率（依赖连接器拼法）。
  3. Web 路径 `origin_status="not_applicable"` 窗口与入站 `unknown` 候选在同一个 `reference` 下并存时，`_find_or_create_window` 的取行顺序（无显式 ORDER BY）。
  4. 用户显式换题信号是否存在于 bridge.py 之外的入站预处理（`inbound_intake.py` 未做相关处理，但全仓未穷举）。

## 8. U+FFFD 检查

检查方法：用 grep 搜索 Unicode 替换字符（即 U+FFFD，编码为 EF BF BD 的那个字符），
对本文档全文扫描。执行时间约 2026-11-22 09:35 +08。

结果：**无损坏字符**（扫描零命中）。
