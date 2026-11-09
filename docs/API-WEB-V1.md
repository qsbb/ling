# Web API v1（W0 冻结）

浏览器只调这些受权 API。所有响应体 `Content-Type: application/json`，除 SSE 端点。
错误统一为 `ErrorResponse`：

```json
{ "schema_version": "v1",
  "error": { "code": "provider_timeout", "message": "模型响应超时", "retriable": true,
             "detail": null, "trace_id": "tr_..." } }
```

错误码集合见 `src/ling/contracts/v1.py` §7。HTTP 状态码：400/422 请求问题、401 未登录、
403 越权、404 不存在、409 冲突、429 限速、500 内部、503 供应商不可用。

## 1. 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/auth/login` | `{username, password}` → `{user, config_revision, test_mode}`，并下发 HttpOnly cookie |
| POST | `/v1/auth/logout` | 撤销当前会话 cookie |
| GET | `/v1/auth/me` | 当前用户；未登录 401 |

`user` 形状：`{id, username, role, subject_id, display_name}`。

## 2. 会话（窗口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/sessions` | `{sessions: SessionRecord[]}`，只含调用者自己的窗口 |
| POST | `/v1/sessions` | `{title?}` → `SessionRecord` |
| GET | `/v1/sessions/{id}` | `SessionRecord` |
| PATCH | `/v1/sessions/{id}` | `{title?, archived?}` → `SessionRecord` |
| DELETE | `/v1/sessions/{id}` | 归档（软删除） |
| GET | `/v1/sessions/{id}/messages?limit=50&before_sequence=N` | `{messages: ChatMessage[], active_turn: TurnView \| null}` |
| GET | `/v1/messages/{message_id}/prompt` | 「查看本次提示词」：这条回复当时真正发给模型的上下文（只读、owner 限定，见 §12） |
| POST | `/v1/sessions/{conversation_id}/messages/{message_id}/recall` | **撤回她自己发出去的那条**（owner only，见 §3A） |
| GET | `/v1/sessions/{id}/recall-proposals` | 她提议撤回、**等用户确认**的那些（见 §3B） |
| POST | `/v1/sessions/{id}/recall-proposals/{pid}/confirm` | 确认 → 真的撤回（走同一条工具 handler） |
| POST | `/v1/sessions/{id}/recall-proposals/{pid}/decline` | 不要撤 → `204`，**平台零调用** |

`ChatMessage` 来自 `contracts/v1`。`TurnView`：

```json
{ "turn_id": "turn_...", "state": "streaming", "attempt": 1,
  "model_label": "fake/deterministic", "error_code": null, "retriable": false,
  "partial_text": "已经说出的半句" }
```

`partial_text` 是当前进程内存里已产出的文本，供刷新后继续显示；进程重启后为空
（那时轮次会被标记 `failed` 并允许重试）。

## 3. 发送与流式回复

`POST /v1/sessions/{id}/turns`

请求：`{ "client_turn_id": "uuid", "text": "在吗" }`（`client_turn_id` 是幂等键；
相同 id 重复提交返回同一轮次，不重复扣费）

响应：`text/event-stream`，事件按顺序：

```
event: turn.started
data: {"turn_id":"turn_...","user_message":{ChatMessage},"state":"streaming"}

event: delta
data: {"text":"嗯"}

event: turn.completed
data: {"turn_id":"turn_...","message":{ChatMessage},"state":"completed","model_label":"fake/deterministic"}

event: turn.failed
data: {"turn_id":"turn_...","error":{"code":"provider_timeout","message":"...","retriable":true}}
```

失败时**已产出的文本仍保留**（`message.state = "failed"`，`text` 为已交付部分），
前端不得把它当作完整回复。`turn.stopped` 同形状，`state = "stopped"`。

`POST /v1/turns/{turn_id}/stop` → `{turn_id, state, was_running}`。停止后服务端不再写入
新增量，已写入部分保留。

停止是**条件迁移**：只有 `pending`/`streaming` 会迁到 `stopping`。如果协调器已经提交
`stopped`/`failed`/`completed`，停止请求不会覆盖终态（否则该轮会永久不可重试）。
悬空的 `stopping`（进程重启遗留）会被修正为 `stopped`。

`POST /v1/turns/{turn_id}/retry` → 与发送相同的 SSE。只允许重试 `failed`/`stopped` 的轮次，
且必须仍属于调用者窗口；成功轮次重试返回 409。若协调器仍在收尾，服务端会有界等待
（最多约 3 秒）后继续，而不是立即拒绝。

## 3A. 撤回她自己发出去的消息（2026-11 新增）

`POST /v1/sessions/{conversation_id}/messages/{message_id}/recall`

用户拍板「说错了想收回」可以用。**只允许撤回她自己发出去的消息**，边界由
服务端事实决定，客户端不能指定平台 id、不能跳过时间窗。

| 状态码 | 什么时候 | 平台有没有收到 action |
| --- | --- | --- |
| 200 `state=recalled` | 平台确认撤回 | 收到 1 次 `delete_msg` |
| 200 `state=uncertain` | 没拿到回执（超时/断连） | 收到（结果未知） |
| 200 `state=not_found` | 平台说这条已经不在 | 收到 |
| 200 `state=expired` | **平台**说超时/没权限 | 收到 |
| 200 `state=unsupported` | 这个接入方式不支持（QQ 官方机器人） | **没收到** |
| 403 `not_owner` / `not_actor_message` | 不是主人 / 不是她的发言 | **没收到** |
| 404 `not_found` | 这条不在这个会话里 | **没收到** |
| 409 `no_receipt` / `not_delivered` / `already_recalled` | 没有平台回执 / 当时没确认送达 / 已经撤过 | **没收到** |
| 422 `expired` | 超过本地预检的 2 分钟窗 | **没收到** |
| 429 `rate_limited` | 同一会话一分钟内超过 3 次 | **没收到** |

响应体：

```json
{ "schema_version": "v1", "message_id": "msg_...", "state": "recalled",
  "note": "撤回了", "recalled_at": "2026-11-09T07:40:00Z",
  "receipt_id": "rcpt_...", "attempts": 1, "reason_code": null }
```

**界面只显示 `note`，不显示 `reason_code`**（`reason_code` 是稳定码，只给日志
与测试；拒绝时它在错误体的 `detail` 里）。

**`uncertain` 不是成功**：文案是「没拿到回执，不确定是否撤回成功」，前端不能
显示成"已撤回"，也不能自动重试——重试由用户再点一次。

**ChatMessage 新增三个字段**：`retracted_at`（**对方**撤回，2026-11 已有）、
`recalled_at`（**她**撤回，本次新增）、`recall_state`（最后一次尝试的结果）。
前端画"已撤回"徽标用这两个时间戳；两者都非空时以 `recalled_at` 为准。

**模型侧只能提议，不能执行**：撤回是对已发生交互的补救，让模型判断"这句说错了"
意味着它要评估自己的输出，而它对自己的错误没有可靠判据——错判一次就是替用户
删掉一条正常回复。所以 `message.recall` 虽然**注册成了工具**（见 §3B），但
`requires_confirmation=True`：`ToolRegistry.call(confirmed=False)` 在进 handler
**之前**就返回 `needs_confirmation`，模型连执行的机会都没有。

完整口径（五道闸、回执五态、上下文注记、审计字段）见
`docs/BEHAVIOR-LOG-CONTRACT.md` §4A；标记语法与确认流程见 `docs/RECALL-TOOL.md`。

### 3B. 撤回工具与待确认提议

`GET /v1/tools` 里能看到 `message.recall`：

```json
{ "tool_id": "message.recall", "version": "1.0.0",
  "risk": "irreversible", "requires_confirmation": true, "reversible": false,
  "description": "撤回她自己发出去的一条消息。不可逆，必须由用户确认后才执行；…" }
```

她在回复里写 `<<撤回 rXXXX>>`（或 `<<撤回>>` = 最近一条）→ 代码把标记**从正文里
剥掉**，并落一条待确认项。网页据此画「她要撤回这条，确认吗？」：

```
GET /v1/sessions/{id}/recall-proposals
→ [{ "schema_version": "v1", "proposal_id": "recall_...",
     "message_id": "msg_...", "window_id": "win_...",
     "note": "她要撤回这条，确认吗？", "created_at": "..." }]
```

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| POST | `.../recall-proposals/{pid}/confirm` | 200 `RecallMessageOut`（与手端点同形）；`409` 已处理过；`403` 非 owner；`404` 不在这窗口 |
| POST | `.../recall-proposals/{pid}/decline` | `204`；提议标 `declined`，**平台零调用** |

**确认后跑的是同一个工具 handler**（`confirmed=True`），也就是与
`POST .../messages/{mid}/recall` **同一套五道闸**——不是第二个执行入口。

**为什么确认不走 `POST /v1/tools/message.recall/call`**：那条通用端点是给
运维/调试用的，它需要调用方自己带 `args` 与 `window_id`；而这里要的是
"从一条具体提议出发、只撤那一条"。两者共用 handler，但入口不同。

`recall_proposals` 也会出现在 SSE 的 `turn.completed` 事件里（`{proposal_id,
message_id, note}`），网页可以在回复到达的同一条事件里直接画确认框。

## 3B. 视频后台取证作业（2026-11 新增，裁决 §3 第 1、2 步）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/sessions/{id}/media/{evidence_id}/read-jobs` | 安排一次**后台**视频读取 → `202` + `MediaReadJobOut` |
| GET | `/v1/sessions/{id}/media/read-jobs/{call_id}` | 查当前状态 |
| POST | `/v1/sessions/{id}/media/read-jobs/{call_id}/cancel` | 取消 |

### 3B.1 请求体（只有两个字段，都必填）

```json
{ "feedback_mode": "evidence_only", "request_key": "前端生成的唯一键" }
```

- `feedback_mode` **必填**且首期只接受 `evidence_only`。缺字段的报错会明说
  "必填"，因为真正的坑是"以为不写就等于 evidence_only"——将来放开主动补话时，
  那批老客户端会**静默**变成"会说话"。别的值 → 422，不静默降级。
- `request_key` 必填：后台作业要扛住"双击了两次 / 前端重试"。服务端不生成
  （每次新 id 就永远不幂等）。
- **多余字段 → 422**（`extra="forbid"`）。例如按旧文档传 `wait=true` 会被拒，
  而不是被无视。

### 3B.2 响应

```json
{ "schema_version": "v1", "call_id": "mread_...", "window_id": "win_...",
  "evidence_id": "ev_...", "state": "pending",
  "note": "已安排，尚未读到结果", "error_code": null, "result_ref": null,
  "requested_at": "...", "started_at": null, "completed_at": null,
  "deadline": "...", "cancel_requested_at": null, "feedback_mode": "evidence_only" }
```

**`note` 由服务端固定映射，前端直接显示，不要自己拼**：

| state | note |
| --- | --- |
| `pending` | 已安排，尚未读到结果 |
| `running` | 正在读这个视频，还没读到结果 |
| `succeeded` | 读完了，结果已存为这条视频的内容 |
| `failed` | 这次没读成（可以再试一次） |
| `timeout` | 读取超时，没读完（可以再试一次） |
| `unknown` | 不确定读到没有：读取过程被中断，结果没存下来 |
| `cancelled` | 已取消，没有读 |
| `expired` | 排太久了，已失效（可以再试一次） |
| `budget_denied` | 这次额度不够，没有读 |

三条纪律：**入队 ≠ 读到结果**（`pending` 不许出现"看到了"）；**`unknown` ≠
失败**（模型可能已经调用并收费，说成失败会诱导重复收费的重试）；文案只有一个
来源（服务端），前端拼文案会让两条路各说各话。

### 3B.3 拒绝

| 状态码 | 什么时候 |
| --- | --- |
| 403 | 没有 `media.inbound` 授权（报文给出"去哪儿授权"） |
| 404 | 会话不存在 / 证据不在这个会话 / 证据已过期 / 作业不属于这个窗口 |
| 422 | 证据不是视频（图片、语音走既有的自动描述）/ 缺字段 / 多余字段 / `feedback_mode` 不是 `evidence_only` |
| 409 | 同 `request_key` 但**参数不同**（`detail=request_key_conflict`）/ 同一证据已有在途作业（`detail=evidence_read_in_flight`） |

**每一条拒绝都是零副作用**：不建作业、不投 outbox。一条"排上了但永远排不上"
的 pending 行比报错更糟。

跨窗口一律按 404 处理（403 会变成"这个 id 存在吗"的探测口）。

### 3B.4 幂等与互斥

- `UNIQUE(actor_id, window_id, request_key)`：同一人同一窗口的同一键 → **同一个**
  `call_id`（重放 202）。别人/别的窗口拿到同一个键互不影响。
- 同键**不同参数** → 409，不悄悄返回旧作业（否则调用方以为新请求生效了）。
- 部分唯一索引 `(evidence_id, tool_version) WHERE state IN ('pending','running')`：
  同一视频同一工具版本**最多一个在途作业**；**终态之后允许再排**（想再读一遍 /
  上次失败重试）。不做成普通唯一约束，否则"重试"永远做不了。

### 3B.5 取消的两种语义

- `pending`（还没开始）→ 直接 `cancelled`。什么都没发生，不用等。
- `running` → **状态不变**，只置 `cancel_requested_at`。执行方看到标记后尽力停，
  真停下来才落 `cancelled`。直接写 `cancelled` 会让"还在跑、可能正在花钱"和
  "库里说取消了"同时成立，而晚到的结果还会被写进证据。
- 已是终态 → 200 + 当前状态，**不改任何东西**（终态不可再变，点两次不该报错）。

`cancel_requested_at` 非空**不等于**已取消。

### 3B.6 执行与恢复（第 3 步已落地）

作业由**专用媒体 worker**执行（topic `media.video.read`，并发固定 1）。普通
worker 用 `exclude_topics` 把它排除——现有 outbox 是串行 `await`，一条慢视频
会把撤回/摘要/日程全堵在后面。

**状态会真的推进了**：`pending → running → succeeded/failed/timeout/unknown/
cancelled/expired`。所以前端可以放心轮询 GET。

#### 崩溃后各自收敛到哪

| 死在哪 | 库里的样子 | 谁收 | 结果 |
| --- | --- | --- | --- |
| 入队后、认领前 | `pending`，事件租约到期 | outbox 租约回收 | 重新排队 |
| 认领后、模型前 | `running`，`model_started_at` 为空，租约过期 | 清理任务 | **退回 `pending` 重跑**（还没花钱） |
| 模型开始后 | `running`，`model_started_at` 非空，租约过期 | 清理任务 | `unknown`，**不自动再烧一次** |
| 结果提交后 | 终态 + `result_ref` | 事件重放 | 只读已有结果，**零模型调用** |

第三行与第二行的唯一判据是 `model_started_at`——它在调模型**之前**单独提交。
把它和结果放同一个事务，崩溃时一起回滚，"模型可能收过费"就查不到，重排后
第二次收费。

#### 重试语义（裁决 §3.3 的硬结论）

`outbox` 只对**异常**退避重排，`ToolRegistry` 的 `failed`/`timeout` 返回值
不会触发重试。所以：

- **业务终态由 handler 自己落库**；`failed`/`timeout` 正常确认 outbox `done`；
- **只有"模型调用前的取件类故障"抛异常**（下载抖动、路由未就绪）——那时还没
  花钱，重排安全。最多 5 次，退避上限 300 秒，到 dead 时业务作业同步终结为
  `failed`（`error_code=outbox_dead`），不留悬空行。

#### 取消

执行前与提交结果前各重检一次 `cancel_requested_at`。若取消发生在模型之后，
**晚到的内容不会成为可消费证据**（描述被 `mark_describe_failed` 逐出可读集合）。

#### 清理（零模型）

`media.read.cleanup` 每 60 秒跑一次，只读作业表、不调模型：排队超时→`expired`，
租约过期按上表分岔。`deadline` 可空，用 `requested_at + 15 分钟` 兜底。

### 3B.7 尚未实现（第 4 步）

**预算**：不检查、不预留 `behavior_model_calls`，也不记录 `actual_tokens`。
所以 `budget_denied` 目前只有定义、没有写入方；媒体并发固定为 1 就是在没有
持久账本时唯一可靠的额度上限。读侧再开放（哪条结果能进上下文）也在第 4 步。

## 4. 模型设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/settings/config` | `{revision, state, test_mode, log_access_denials, providers[], profiles[], revisions[]}` |
| PUT | `/v1/settings/draft` | `{providers?, profiles?, slots?, test_mode?, log_access_denials?}` → 新草稿修订 `{revision, validation_errors}` |
| POST | `/v1/settings/publish` | `{revision}` → `{published_revision}`（校验失败 422 + 错误列表） |
| POST | `/v1/settings/rollback` | `{to_revision}` → `{published_revision}` |
| GET | `/v1/settings/health` | `{revision, test_mode, profiles, health}`；`health` 键为 `provider_id/model_id`，**不含**上游错误原文（必鉴权） |
| PUT | `/v1/settings/providers/{provider_id}/secret` | `{secret}` → `{last4, fingerprint}`（永不回显原文） |
| DELETE | `/v1/settings/providers/{provider_id}/secret` | 删除凭据 |
| POST | `/v1/settings/providers/{provider_id}/probe` | `{ok, detail, latency_ms, models[], checked_at}`（只读，不写修订） |
| GET | `/v1/settings/usage` | `{day, usage: {provider_id: {used_tokens, estimated}}, limits: {provider_id: n \| null}}`；`estimated=true` 表示当日账里含本地估算（部分调用没拿到供应商 usage 回执） |
| GET | `/v1/settings/connectors` | `{schema_version: "v2", as_of, can_edit, connectors: [{id, label, configured, enabled, status, status_reason, status_message, guidance, editable, storage, generation, activation, fields[], runtime}], access: {id, generation, activation, editable, fields[]}}`；接入器诊断 **+ 可配置字段**；`fields[].kind=secret` 只回 `configured`/`preview`，`value` 恒 null（必鉴权） |
| PUT | `/v1/settings/connectors/{scope}` | `{set: {KEY: value}, clear: [KEY]}` → `{operation_id, generation, state, changed, timings, error_code}`；scope 为 `onebot`/`qqbot`/`access`。`If-Match: <generation>` 必带（并发保存只有一个赢家）。202 = 已受理并尝试过，**终态看 `state`**（`active`/`unchanged`/`rolled_back`/`degraded`）。owner + 同源 Origin + 15 分钟内重新登录（必鉴权） |
| GET | `/v1/settings/connectors/operations/{operation_id}` | `{operation_id, scope, state, generation, active_generation, started_at, activated_at, error_code, error_safe}`；202 之后用它确认**真的**生效了（必鉴权，owner） |
| POST | `/v1/settings/connectors/reset` | 清空三个 scope 的覆盖（回退 env/默认）→ `{ok, results: [{scope, state, cleared[], operation_id?, error_code?}]}`；任一 scope 失败则 `ok=false` 并保留原 active（**不出现"部分恢复默认却报成功"**）（必鉴权，owner） |
| POST | `/v1/settings/resolve` | `{providers?, profiles?, slots?, test_mode?}` → `{slots[], profile_resolutions[]}`；按**草稿**算每个职责的生效模型，**只算不写** |
| POST | `/v1/settings/quick-config/preview` | 同上的 body → `{rows[], profiles[], summary}`；快速配置的二次确认（全量覆盖，`profiles` 就是确认后要写入的那份） |
| GET | `/v1/settings/preferences` | `{schema_version, timezone, source, stored, env, default, common[], video_mode, video_modes[], video_mode_default}`；`source` 为 `preference` / `env` / `default`（必鉴权） |
| PUT | `/v1/settings/preferences` | `{timezone?, video_mode?}` → 同上；**缺字段 / `null` = 这次不改，显式空串 `""` = 清掉**；非法值 `400 invalid_request`。**不进配置修订**：保存即生效，不跟发布/回滚走（详见 §4.3） |

`providers[]` 每项：`{provider_id, kind, base_url, enabled, models[], secret_last4,
timeout_seconds, max_output_tokens, daily_token_limit, ...}`。
`kind` 取值：`fake | openai_compatible | anthropic | chat_responses | local`。
`models[]` 每项：`{model_id, note, enabled}`——它是**给人看的目录**，不是路由白名单：
停用的模型不会出现在新的职责绑定下拉里，但已绑定的职责仍会按原样调用。

**传输与额度属于供应商，不属于职责**：`timeout_seconds`（秒，1~600）、
`max_output_tokens`、`daily_token_limit` 三项对这家供应商下的所有模型生效。
后两项填 `-1` 表示不限制；日额度按**本地日历日**累计，见
`GET /v1/settings/usage`。

`profiles[]` 每项：`{profile, provider_id, model_id, fallback_provider_id?, fallback_model_id?,
enabled, mode}`。原来这里的 `temperature / max_tokens / timeout_seconds / daily_cost_limit`
已经移除：前三项跟着供应商走，温度改为**不发送**（用上游模型自己的默认值）。
旧客户端仍可带这些键，服务端会忽略。

### 4.1 默认槽位与「实际生效」

五个默认槽位（`slots[]` 每项 `{slot, label, hint, provider_id, model_id, configured,
usable, roles[]}`，键固定为 `fast / creation / vision / video / daily`，中文名
`快速 / 创作 / 识图 / 视频 / 日常`）。它们和职责绑定一样**存在配置修订里**，跟着发布/回滚走。
`video`（看视频）与 `vision`（识图）是**两个**槽位：能看图片的模型大多看不了视频，
合成一个用户就只能二选一。`perception.video` 的能力仍按 `chat` 判——不新增 `video`
能力，否则所有现有供应商都会被判不匹配。

角色 → 槽位的映射固定在服务端（`ling/ports/model.py::PROFILE_SLOTS`）：

| 职责 | 槽位 |
| --- | --- |
| `dialogue.fast` | 快速 |
| `dialogue.group` | 日常 |
| `perception.vision` | 识图 |
| `perception.video` | 视频 |
| `memory.extract` | 创作 |
| `memory.embed` / `memory.rerank` | **没有槽位**（另一套协议，只能单独指定） |

职责怎么拿到模型：`profiles[].mode` 只有两个值。`explicit`（**缺省值**）表示
"用 `provider_id/model_id`"，`slot` 表示"跟随我所属的槽位"（此时那两项会被清成空串）。
旧修订没有这个键，一律按 `explicit` 读——按 slot 读会把所有存量绑定一次性作废。

**生效解析只回退一层，不允许套娃**（`EffectiveConfig.resolve_profile`，全系统唯一实现）：

1. 单独指定了、且 `(provider, model)` 可用 → `source=explicit`；
2. 否则用**该职责所属槽位**的模型 → `source=slot`（单独指定的坏了也走这里）；
3. 槽位没配 / 不可用 → `source=unavailable`，`reason` 说清断在哪；
4. 到此为止。不试别的槽位，也不借别的角色的模型。

「可用」= 供应商 `enabled` **且** 模型 `enabled`。模型 ID 不在该供应商的目录里时
按可用处理（目录是给人看的清单，不是路由白名单）。

`profile_resolutions[]` 每项 `{profile, slot, source, effective, requested, fell_back,
enabled, reason}`，随 `GET /settings/config` 一起返回；`effective` 为
`{provider_id, model_id}` 或 `null`。界面上的「实际生效」列直接用它，**前端不重算**
——改草稿时调 `POST /settings/resolve`，它用与保存完全相同的合并 + 解析。

`requested` 只在"单独指定过但没用上"时非空，配合 `fell_back` 让界面把
「继承」（从来没单独选过）和「已回退」（选了但坏了）分开。

`POST /settings/quick-config/preview` 的语义是**全量覆盖**：把所有有槽位的职责
改回「跟随默认」，用户单独指定的也一并清掉。`rows[]` 每项
`{profile, slot, slot_label, provider_id, model_id, change, change_kind}`，
`change_kind` 取 `override / same / repair / unavailable`——弹窗必须把
"将要覆盖哪几条"摆在明面上，确认后仍需保存才生效。

### 4.2 接入器（渠道，可配置）

> 设计文档：`docs/CONNECTOR-CONFIG-DESIGN.md`。契约：
> `contracts/v1/ConnectorsOut.schema.json`、`ConnectorChangeRequest.schema.json`、
> `ConnectorChangeAccepted.schema.json`。

接入器配置**不再只从 `.env` 来**：生产容器非 root、根文件系统只读，写不了 `.env`
也重启不了自己，所以值落**数据库覆盖层**，由进程读出来构造连接器。优先级：

```text
DB 已生效覆盖  >  进程启动时的 env 快照  >  代码默认值
```

**诊断仍然只读**：`GET` 回答凭据齐不齐（`configured`）、现在通不通（`status`）、
有效值来自哪一层（`fields[].source`）。`status` 四值：`not_configured`（还没填齐）/
`disabled`（凭据齐了但渠道开关关着，只在 QQ 官方机器人上出现）/
`configured_disconnected`（配齐了但当前没连上）/ `connected`。**不要把它压成
"可用/不可用"**：这四者的下一步完全不同。`status_reason` 是稳定的码，
`status_message` 是它对应的中文（码表见 `ling/settings/connectors.py::CONNECTOR_REASON_TEXT`）。

#### 字段

`fields[]` 每项：

| 字段 | 含义 |
| --- | --- |
| `key` | 注册表里的固定键（= 环境变量名）。`name` 是它的兼容别名 |
| `kind` | `secret` / `text` / `switch` / `list` |
| `source` | `db`（本 scope active 修订里有覆盖）/ `env`（启动时 env 有值）/ `default` |
| `configured` | **有效值非空**（不等于"DB 有覆盖"）。`set` 是兼容别名 |
| `value` | 非密钥的规范化值：文本、布尔（switch）、字符串数组（list，**前导零保留**）；`secret` 恒 `null` |
| `preview` | 仅 `secret`：前 `min(7, len-1)` 个 Unicode 字符 + 固定 `••••`；空值为 `null`。**非 owner 一律 `null`** |
| `editable` | owner 为 true；非 owner 只读 |

字段清单是**服务端白名单**（`ling/settings/connector_fields.py`，onebot 12 项 /
qqbot 12 项 / access 2 项）。请求里出现白名单之外的键（`LING_ROOT_KEY`、
`DATABASE_URL`…）一律 `422 invalid_field`——不能把"任意环境变量写入"开成口子。

#### 保存

`PUT /v1/settings/connectors/{scope}`：

- `set` 里没出现的键 = 不改；**密钥给空串 = 不改**（密码框总是空的，不能因此变成删除）；
- `clear` = 删除该键在本修订的覆盖，**回退到 env**（不是写空串）；
- 同一个键同时出现在 `set` 与 `clear` → `422 invalid_field`；
- 校验分两层：字段级（URL scheme / 禁 userinfo / 禁敏感 query / 控制字符 / 端口范围）
  与**合并后的整体**（配了 WS 端口没令牌、白名单模式但名单空…）。**任何一层失败都
  在停连接之前**，所以用户填错一个端口号不会让她的 QQ 掉线；
- 有效值没变 → `200` + `state=unchanged`，**不建修订、不重连**；
- 真实切换失败 → `202` + `state=rolled_back`：旧配置与旧连接已恢复，`generation`
  **不推进**；回退本身也失败才是 `degraded`（如实上报，绝不报成功）。

#### 生效

`generation` 是每个 scope 的单调代际号。`PUT` 用 `If-Match: <generation>` 做并发
控制：两个保存同时到达只有一个赢家，输家拿 `409 conflict` + `detail=generation_conflict`
或 `detail=activation_in_progress`（候选已落库、正在切换）。

一次 `PUT` 的顺序是**先校验、再落候选修订、最后在事务外切换**：长连接
`stop()`/`start()` 绝不在事务里，否则拿着行锁等网络。切换失败时用**旧配置重新
构造**一份（旧对象 stop 之后不保证能再 start，见设计 §4），并让
`app.state` 继续指向可用的那份。

**白名单不需要重连**：它换掉进程里的 `Settings` 快照（一次赋值，asyncio 单线程里
原子可见），入站判断下一条消息就按新名单走。这样不会出现"验签读旧 token、授权读
新名单"——同一个请求读到的永远是一份完整快照。

#### 中断窗口（`timings`）

回包带 `timings`：`disconnect_started_at`（开始停旧实例）、`listener_ready_at`
（新实例 `start()` 返回）、`remote_connected_at`（远端在等待窗口内报告已连上）。
**"任务已创建"不等于"已连上"**，所以远端那一步有独立的有界等待（3 秒）：
超时只影响 `health/state`（`degraded`），不影响"本地已切换"。量化目标是本地
代际切换 500ms 内完成（`listener_ready_at - disconnect_started_at`）。

#### 审计

一次保存记两条：`connector_config_changed`（改了哪些**字段名**、每个字段的
`旧指纹>新指纹`、结果）与 `connector_activated` / `connector_rolled_back`。
指纹是**带域分离的 HMAC-SHA256 截断**（低熵字段如端口 `5700` 用无盐 SHA 能被
穷举反推）。审计里**没有值、也没有预览**；请求体本身不进访问日志。

#### `runtime` 与 `activation`

`runtime` 报进程里能直接看到的连接数与已收帧数（没有可观察的连接对象时为 null）；
**这条端点不发网络探测**，设置页会被反复打开，不能拿它当探活。
`activation` 是 `{state, generation, active_generation, candidate_generation,
active_revision_id, candidate_revision_id, last_revision_id, last_state, started_at,
activated_at, error_code, error_safe, ack_count, expected_count}`：
`activating` 表示候选已落库、正在切换；`rolled_back` / `degraded` 都**不是**成功。

`log_access_denials`（实例级，与 `test_mode` 同层）：是否逐条记录入站白名单拒绝。
**只影响日志量**——拒绝本身与 `denied_total` 计数不受影响；默认 `true`，未发布过
修订时取环境变量 `LING_LOG_ACCESS_DENIALS`。

### 4.3 用户偏好（时区 / 视频读取方式）

`GET` / `PUT /v1/settings/preferences` 读写**用户自己的**两条偏好：时区
（`timezone`）与视频读取方式（`video_mode`）。它们存在 `user_preferences`
（按 subject 一行），**不进配置修订**：保存即生效，不跟发布/回滚走——回滚一次旧配置
不该把"我在东八区"一起回滚掉。

**请求体：两个字段都可选**

| 字段 | 类型 | 缺字段 / `null` | 显式 `""` | 合法值 |
| --- | --- | --- | --- | --- |
| `timezone` | `str \| null` | 这次不改 | 清掉（回退环境变量 → 默认 `Asia/Shanghai`） | IANA 名，如 `Asia/Shanghai` |
| `video_mode` | `"frames" \| "native" \| null` | 这次不改 | 清掉（回退默认 `frames`） | `frames`（抽帧）/ `native`（原生视频输入） |

**缺字段 ≠ 清空（这条是本端点最重要的约定）**：两个偏好共用同一条 PUT，前端也是
各发各的（改时区只发 `timezone`、切视频方式只发 `video_mode`）。如果缺字段等于清空，
切一次视频方式就会把时区**静默**清回默认——页面显示「已保存」，刷新后才变成
"没有设置过"。所以只有**显式空串 `""`** 才表示清空；`null` 与缺字段一律按"不改"。

非法值返回 `400 invalid_request`（`timezone` 的 `detail` 会给出 IANA 名的正确写法），
**整条请求不落库**：同一次请求里的另一个字段也不会生效（事务回滚）。

**响应**

| 字段 | 含义 |
| --- | --- |
| `timezone` | 时区**生效值**（已按 DB 偏好 → `LING_USER_TIMEZONE` → 默认 的优先级解析） |
| `source` | 这个时区从哪来：`preference`（你自己设的）/ `env`（环境变量）/ `default`（`Asia/Shanghai`） |
| `stored` | 库里存的时区原值；`null` = 没设过。与生效值分开，界面才能说清"这是默认值，不是你设的" |
| `env` / `default` | 环境变量里的值（`null` = 没配）/ 默认值 |
| `common` | 常用时区**建议**；不是白名单，任何合法 IANA 名都能填 |
| `video_mode` | 视频读取方式**生效值**（没设过就是 `frames`） |
| `video_modes` | 可选值列表（顺序即展示顺序） |
| `video_mode_default` | 默认值（当前 `frames`；原生输入不被供应商支持时，运行期会退回抽帧并记原因） |

> 响应里**没有** `video_mode_source` / `stored_video_mode`：视频方式只有"显式设过"
> 与"回退默认"两态，界面用 `video_mode === video_mode_default` 即可表达。将来若要
> 区分"选过 `frames`"和"从没选过"，需要另加字段（属接口变更，不是文档遗漏）。

## 5. 角色与时间线

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/persona` | `{persona_id, revision, display_name, system_prompt, traits, boundaries, scenario_prompts, scenarios[]}` |
| PUT | `/v1/persona` | 同形状（部分字段）→ 新修订；立即影响后续轮次 |
| GET | `/v1/actor/timeline?limit=50` | `{events: [{id, type, origin, visibility, occurred_at, summary}]}` |
| GET | `/v1/life/timeline?date=YYYY-MM-DD` | 只读生活时间轴（双时区 / 段 / 事实 / 未来意向 / 梦卡片），字段见 §11 |

`scenarios[]` 每项：`{key, label, when, text, default_text, custom}`。`scenario_prompts`
只存**覆盖**（键：`private` / `group` / `group_mentioned` / `group_unaddressed`）；
某个键给空串或整组不带该键 = 使用内置默认。未知键返回 400，不会静默丢弃。

## 6. 健康

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 进程存活 |
| GET | `/readyz` | 数据库就绪（含迁移版本） |
| GET | `/v1/health/model` | 当前生效 profile 的可用性摘要 |

## 7. 版本与更新

更新通道默认关闭（`LING_UPDATE_ENABLED=0`）。打开后 API 也只**写一条请求**，
不执行部署：构建/切换由宿主机上的独立执行器完成（见
[UPDATES.md](UPDATES.md)）。API 容器没有 Docker socket。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/version` | `{schema_version, current, commit, web_manifest_sha256}`；`commit` 读构建注入的 `LING_COMMIT`，未知为 `null` |
| GET | `/v1/version/check-release` | 手动检查 GitHub 最新 Release；保留 `status/current/latest/published_at/html_url/is_newer`，追加 `release_id/target_commit/update_eligible/update_reason` |
| GET | `/v1/version/update` | 更新通道视图（只读固定 JSON，不发起网络请求） |
| POST | `/v1/version/update-requests` | owner 提交一次更新请求，`201` + job |
| GET | `/v1/version/image-update` | **镜像模式**的「检查更新」卡片（四态 + 诊断，见 §7.1） |
| POST | `/v1/version/image-update-requests` | owner 提交一次固定 digest 的镜像更新，`201`；要求最近 15 分钟内登录 |
| POST | `/v1/version/image-update-checks` | owner 发起一次**按需检查**（只写刷新请求），`202` 已排队 / `200` 幂等去重 |

`GET /v1/version/update`：

```json
{ "schema_version": "v1", "enabled": false, "reason": "disabled",
  "executor": { "online": false, "last_seen_at": null },
  "current_job": null }
```

`current_job` 非空时：

```json
{ "request_id": "upd_<32 hex>", "state": "queued|running|succeeded|failed|needs_manual",
  "stage": "queued|preflight|backup|fetch|verify_source|build|deploy|verify|complete",
  "target_tag": "v0.2.0", "target_commit": "<40 hex>",
  "created_at": "...", "updated_at": "...", "error_code": null,
  "message": "服务端固定码表生成" }
```

`POST /v1/version/update-requests`（owner 专属）：必须
`Content-Type: application/json`、请求头 `X-Ling-Update: 1`，Cookie 认证必须同源
（带匹配的 `Origin`）。请求体只接受：

```json
{ "confirm_tag": "v0.2.0",
  "expected": { "release_id": 123, "tag": "v0.2.0", "commit": "<40 hex>",
                "current_version": "0.1.0", "current_commit": "<40 hex>" } }
```

服务端会重新核验远端 Release 与当前运行版本，任何变化返回 `409`
（`release_changed` / `installed_version_changed`）。**没有取消或关闭接口**：
失败只保留 `needs_manual` 语义，由人按 `docs/UPDATES.md` 的恢复步骤处理。

`reason` 固定码：`disabled`、`not_owner`、`executor_offline`、
`channel_unavailable`、`installed_commit_unknown`、`version_drift`、
`update_in_progress`、`release_unverifiable`、`not_newer`。

### 7.1 镜像模式：`GET /v1/version/image-update`

镜像部署（`LING_APP_IMAGE` / `LING_RUN_MIGRATIONS=0` / 更新器状态文件任一存在）下，
网页的「检查更新」用这个端点。**发现版本只有一个事实源**：更新器写的
`status.json` 顶层 `release_check`（它自己查 GHCR 的 tags/manifest）。API 不读
可信发布目录——`deploy/releases.json` / `catalog.py` / `min_from` 已经删除
（[UPDATE-SIMPLIFICATION-DESIGN.md](UPDATE-SIMPLIFICATION-DESIGN.md) §3/§6/§8）。

```json
{ "schema_version": "v1",
  "state": "available",              // latest | available | newer_unavailable | unknown
  "message": "发现新版 v0.0.4，可以更新。",
  "checked_at": "2026-11-09T06:20:00Z",
  "current": { "version": "0.0.3", "commit": "<40 hex>", "digest": "ghcr.io/qsbb/ling-api@sha256:…" },
  "newer": [ { "version": "0.0.4", "commit": null, "has_migration": null,
               "digest": "ghcr.io/qsbb/ling-api@sha256:…", "compatible": true,
               "incompatible_reason": null, "incompatible_reason_message": null,
               "summary": "…", "notes": ["…"], "notes_url": "https://…" } ],
  "target": { "version": "0.0.4", "digest": "ghcr.io/qsbb/ling-api@sha256:…",
              "commit": null, "has_migration": null },
  "task": null,
  "details": { "release_check": { "state": "ok", "checked_at": "…",
                                 "check_id": "<32 hex>", "source": "registry",
                                 "reason": null, "reason_message": null,
                                 "current": { "version": "0.0.3", "digest": "sha256:…" },
                                 "latest": { "version": "0.0.4", "digest": "sha256:…",
                                             "commit": null },
                                 "tag_count": 12 },
               "catalog": { "…": "旧形状，从 release_check 派生，一个版本后移除" },
               "updater": { "online": true, "state": "idle", "protocol": 1, "version": "0.0.3" },
               "backup_gate": { "restore_point": true, "…": "…" } } }
```

判定顺序（服务端算，前端不自己比较版本）：

| `state` | 条件 | 主按钮 |
| --- | --- | --- |
| `unknown` | 通道/更新器不可用、`release_check` 缺失、`current.version` 读不到，或这次 tags 列表不可信（不可达/认证/限流/超时/没有稳定 tag） | 重新检查 |
| `newer_unavailable` | 已确认有更高版本，但拿不到安装包摘要或没有本机平台（`target=null`） | 重新检查（**不**提交安装） |
| `available` | 更高版本 + 摘要已解析 + 安装门禁全过 | 更新到 `target.version` |
| `latest` | 检查完成（`state=ok`）且没有更高版本 | 检查更新 |

- `message` 是**一句话人话**：不含原因码、`sha256`、路径、原始 ISO 时间
  （时间形如「检查于 11 月 9 日 14:20」，按用户生效时区）。
- `checked_at` 只来自 `release_check.checked_at`；**绝不**拿更新器的
  `updated_at` 顶替（那是"状态文件被写过"的时间）。
- `newer[].digest` / `target.commit` 允许为 `null`；`has_migration` 只是过渡字段，
  现在恒为 `null`（迁移是否需要由迁移器判断，见设计 §8）。
- `details.release_check` 是更新器事实的白名单投影（`reason_detail` 只投影 `null` 或 `throttled`（闭集，自由文本不投影）：
  那是自由文本，可能带 URL/凭据/异常）。
- 旧字段 `image_mode/enabled/installable/reason/reason_message/next_actions/
  updater/backup_gate/catalog/available_targets/request` 保留一个版本；它们的值
  现在全部从 `release_check` 派生，`reason` 里不再有目录类原因码。

`POST /v1/version/image-update-requests` 仍只接受固定 5 键请求体，且
`target_digest` 必须等于**这次发现的目标**摘要（否则 `409 target_not_available`）。
`POST /v1/version/image-update-checks` 的请求体固定为
`{"request_id": "<32 hex>"}`：只原子写 `refresh-<request_id>.json` 进更新器的
requests 目录，**不**访问 registry、**不**改 `status.json`。它是只读动作，所以
**不要求最近登录**（安装请求仍要求）。更新器会把这个 ID 回填进
`release_check.check_id`，前端据此确认"这次检查"真的完成了。

`release_check.reason` 的闭表（未知码一律降级为「这次查不到」，不回显原文）：

| 类别 | 码 | 首屏那句话 |
| --- | --- | --- |
| 列表类（→ `unknown`） | `registry_unreachable` | 连不上镜像仓库 |
| | `registry_auth_failed` | 认证失败（私有包/凭据失效） |
| | `registry_rate_limited` | 镜像仓库暂时限流，这次没查完整 |
| | `registry_timeout` | 查询镜像仓库超时 |
| | `no_stable_tags` | 镜像仓库里还没有稳定版发布 |
| 制品类（→ `newer_unavailable`） | `digest_unresolved` | 新版已发布，但取不到安装包 |
| | `platform_unsupported` | 新版本没有适合这台机器的镜像 |

`release_notes`（`{state: ok|missing|invalid, notes: {"<version>": {summary, items, url}}}`）
只影响 `newer[].summary/notes/notes_url`；取不到说明**不阻断**安装。

## 8. 前端必须遵守

1. 浏览器不保存权威历史；刷新后从 `/v1/sessions/{id}/messages` 重建。
2. 流式渲染是**增量展示**，不落本地存储；断线后重取消息接口。
3. `test_mode = true` 时必须在界面显著位置显示“测试模式：回复由确定性 fake provider 生成”。
4. 失败必须显示可执行提示（重试按钮 + 具体原因），不得静默吞掉。
5. 移动端（375×667 起）与桌面（1440×900）都要能完成发送、停止、切换会话。
6. 更新状态只能按服务端 `enabled/reason/executor/current_job` 展示；`message`
   直接显示服务端文案，不自行拼接宿主路径或日志。通道关闭（`enabled=false`）
   时不得显示可点的“更新”入口。
7. 镜像模式按 `state` 展示四态、按 `message` 显示那句话：**不得**自己比较版本、
   自己拼「已是最新」或从 `task`/旧 `available_targets` 造安装目标。`unknown`
   与 `newer_unavailable` 都没有可提交目标，主按钮只能是「重新检查」。

## 9. 仪表盘与管理查询：日期 / 状态语义（2026-10 变更）

### 9.1 `GET /v1/dashboard` 的「今天的安排」

- `timezone` 仍是响应顶层字段，取解析后的用户时区：DB 偏好（`PUT /v1/settings/preferences`）
  > `LING_USER_TIMEZONE` > `Asia/Shanghai`；`timezone_source` 标明来源（`preference` / `env` / `default`）。
  **「今天」按这个本地时区的日历日 `[00:00, 次日 00:00)` 计算**，不是 UTC，
  也不是服务器所在时区。
- `sections.agenda.data.plans[]` 只含 `start_at` 落在上述本地日内的
  `planned` / `active` 计划（最多 5 条）。跨午夜的计划按 `start_at` 归日：
  23:30 开始、次日 00:30 结束属于开始那天；昨天开始、今天结束的不算今天。
- 每条计划仍带 `start_at`（ISO-8601，含 UTC 偏移）。前端继续用它显示日期
  并做“不是今天”的兜底；**字段名与语义未变**。正常情况下后端已过滤，
  前端不会再看到“不是今天”的日程。
- `sections.agenda.data.commitments[]` 默认只含 `state="active"` 的承诺；
  `cancelled` / `undeliverable` / `delivered` / `missed` 不出现。

### 9.2 `GET /v1/commitments`

| 查询参数 | 含义 |
| --- | --- |
| 不传（默认） | `state="active"`：仍待履行 |
| `?state=cancelled`（或 `undeliverable` / `delivered` / `missed`） | 只查该历史状态 |
| `?state=all` | 全部状态（含历史） |
| 其它值 | `422 invalid_request`，不静默返回空列表 |

这是**对外契约变更**：2026-10 之前该端点不过滤状态，已取消/送不出去的
承诺会和待履行的一起返回。数据**没有删除**，只是默认可见集收窄。
响应新增 `state_filter` 回显实际生效的过滤值（默认 `"active"`）。

### 9.3 生活时间轴的两块新摘要（2026-11）

`/v1/dashboard` 新增 `sections.today_outline`（今天她可能做什么：当前段 +
下一条 + 已确认锚点）与 `sections.intent_teaser`（未来 1–2 条有来源的意向，
标"未来日未生成大纲"）。两块与旧的 `sections.agenda` **并列**，旧块的
"最多几件小事"语义不变。字段与语义见 §11.9；端点本身见 §11。

## 10. 记忆读契约（2026-11 冻结，IA Step 0.5）

适用于 `/v1/memory` 与 `/v1/core-memory` 共 8 个端点。全部要求登录；
`retract` / `supersede` / core `DELETE` 对非本人记录返回 **404**（不返回 403，
避免泄漏 id 是否存在）。端点测试见 `tests/integration/test_memory_api_contract.py`。

本节所有端点的**参数校验失败**——`GET /v1/memory` 的 `limit`/`include`、
`POST /v1/memory` 的 `kind`/`text`/`confidence`/`source_event_ids`、`retract` 的
`reason`（含整个 body 缺失）、`supersede` 的 `text`、core `PUT` 的
`scope`/`priority`——统一返回 `422 invalid_request` 的 `ErrorResponse` 形状
（`{"schema_version":"v1","error":{code,message,retriable,detail,trace_id}}`），
不是 FastAPI 默认的 `{"detail":[...]}`。pydantic 校验失败时，`error.detail`
带 pydantic 的原始报错原文、`error.message` 里带字段名；端点级 `LingError`
（`limit` 越界、`include` 非法）仍返回同一种 `ErrorResponse` 形状，但
`error.detail` 为 `null`、`error.message` 是端点自己写的说明；整个 body
完全缺失时 `error.message` 是「请求体 参数不合法」。

实现方式是**约束优先留在 pydantic、只翻译响应形状**：`create_app` 注册了一个按
路径前缀生效的 `RequestValidationError` 处理器
（`ling.app.memory_request_validation_handler`），只覆盖 `/v1/memory*` 与
`/v1/core-memory*`；其它路径继续用 FastAPI 默认形状，不受影响。
`/openapi.json` 与运行时的对应关系如实列举如下：

- `kind`/`text`/`confidence`/`source_event_ids`/`reason`/`scope`/`priority`：
  约束在 pydantic 里，`required`/`pattern`/`minLength`/`maxLength` 与运行时是
  **同一条约束**，生成客户端不会拼出"看起来合法、必被 422"的请求。
- `GET /v1/memory` 的 `include`：允许值
  `enum: ["retracted", "archived", "all"]` 已写进 OpenAPI，与运行时一致。
- `GET /v1/memory` 的 `limit`：范围 `1..200` 只在端点里判定，OpenAPI 只声明
  `integer`、**没有** `minimum` / `maximum`，越界照样 422——见 §10.1。

### 10.1 `GET /v1/memory`

查询参数：

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `50` | 整数 `1..200` | 单次最多返回条数。**无分页**：没有 `offset` / `cursor`，只有 `limit`；`limit` 上限 200，超过 200 条的部分在接口层面**不可达** |
| `include` | 不传（`null`） | `retracted` / `archived` / `all` | 显式取回墓碑；其它值 `422 invalid_request` |

默认可见集**只含现行记忆**：`retracted=false` **且** `archived_at IS NULL`。
已撤回（`retracted=true`）与已归档（`archived_at` 非空）的行只是**默认不可见**，
数据仍在库里，供证据链与审计追溯——本契约**不删除、不改写**任何行。
`valid_until` 已过期的行仍会返回：本次只收窄墓碑可见性，不改变“有效期”
在召回链路上的既有语义。

`include` 的语义是“在默认集合上追加哪一类墓碑”，不是“只查哪一类”：

| `include` | 返回集合 |
| --- | --- |
| 不传 / `null` | 未撤回且未归档 |
| `retracted` | 未撤回 + 已撤回（仍排除已归档） |
| `archived` | 未归档 + 已归档（仍排除已撤回） |
| `all` | 全部行（含同时撤回且归档） |

响应（`200`）：

```json
{ "memories": [ { "memory_id": "mem_...", "kind": "assertion", "text": "...",
                  "origin": "user_statement", "visibility": "subject_private",
                  "confidence": 0.8, "retracted": false, "retract_reason": null,
                  "supersedes": null, "superseded_by": null,
                  "source_event_ids": ["evt_..."],
                  "valid_until": null, "created_at": "2026-11-01T00:00:00+00:00",
                  "archived_at": null } ],
  "limit": 50, "total": 1, "has_more": false, "include": null, "note": "..." }
```

- 既有字段 `memory_id/kind/text/origin/visibility/confidence/retracted/
  retract_reason/supersedes/superseded_by/source_event_ids/valid_until` 名称与含义不变。
- 新增 `created_at`、`archived_at`：ISO-8601（含 UTC 偏移）；无值时为 `null`。
- `total`：当前 `include` 过滤口径下的总行数（不受 `limit` 截断）。
- `has_more`：`total > len(memories)`，即 `limit` 是否截掉了结果。
- `include`：回显请求里的原始值；默认不传时为 `null`。
- 排序：`created_at DESC, memory_id DESC`。`memory_id` 是**稳定次级键**：
  同一 `created_at`（同秒/同微秒）的多行顺序也是确定的，同一查询重复执行
  不会漏行或重复。`total` 与当前页使用同一过滤口径。
- **无分页**：只有 `limit`（上限 200），没有 `offset`/`cursor`。`limit` 截断后
  更早的行在接口层面**不可达**；`total`/`has_more` 只回答"还有没有更早的"，
  不提供翻页。需要第 200 条之前的内容时，按 `created_at` 收窄或直接查库。

错误：未登录 `401`；`limit` 非整数、`limit` 越界或 `include` 非法 →
`422 invalid_request`，使用统一 `ErrorResponse` 形状（见 §10 开头），
不是 FastAPI 默认的 `{"detail":[...]}`。（`limit` 的 `1..200` 范围在端点内
判定，OpenAPI 只声明 `integer`、无 `minimum`/`maximum`；非整数由 pydantic
拦。`include` 的三值由 OpenAPI 的 `enum` 声明；非法值仍由端点拒绝，
`error.detail` 为 `null`。）

### 10.2 记忆写端点

| 方法 | 路径 | 成功响应（`200`） | 关键错误 |
| --- | --- | --- | --- |
| POST | `/v1/memory` | `{memory_id}` | `source_event_ids` 为空 / `kind` 非 `episode\|assertion\|procedure` / `origin=model_inference` / 非法 `origin` 或 `visibility` → `422 invalid_request`（**不再 500**，且为统一 `ErrorResponse`） |
| POST | `/v1/memory/{memory_id}/retract` | `{memory_id, retracted:true}` | 不存在或非本人 `404`；`reason` 缺失/空串/超过 500 字符 → `422 invalid_request`（统一形状）；重复撤回幂等返回 `200` |
| POST | `/v1/memory/{memory_id}/supersede` | `{memory_id: 新id, supersedes: 旧id}` | 不存在或非本人 `404`；旧行被置 `retracted=true` 且 `superseded_by=新id`，新行 `supersedes=旧id` |
| POST | `/v1/memory/forget` | `{tombstone_id, generation, events_cancelled, state}` | 无 body；只清当前 subject；主体不存在 `404`。**这是真删的请求阶段**（见 10.2.1） |

#### 10.2.1 `POST /v1/memory/forget`：真删（两段式）

2026-11 起 `forget` 不再软清除。原软清除（撤回/归档/停用/失效、原文仍在
`memories.text` 里、`?include=all` 还能读回来）与用户原话「真删：原文和派生
数据都从库里清掉」直接冲突，已按后者改写。

响应字段：

| 字段 | 含义 |
| --- | --- |
| `tombstone_id` | 本次清理的墓碑 id（`purge_…`）；查进度用它 |
| `generation` | 该 subject 递增后的遗忘代次（从 1 开始，单调递增） |
| `events_cancelled` | 本次取消并脱敏的在途事件数 |
| `state` | **恒为 `pending`**：描述墓碑此刻的状态，不是"已经删完了" |

`state=pending` 不是失败也不是"没删"。请求阶段（同步）已经封住了读写：

- 代次 +1 ⇒ 此前入队、此后才跑的记忆提取一律作废（含已经被认领的
  `in_flight` 事件——改状态抓不住它们，只有代次能）；
- 在途事件置 `cancelled` 且 payload 换成 `{"purged": true}`；
- 读侧硬门：`GET /v1/memory`、`GET /v1/core-memory`、`GET /v1/awareness`
  全部返回空，召回（`MemoryService.recall`）返回空结果——**这一步之后，
  下一轮对话就不会再提到那些事了**，即使物理删除还没跑；
- 墓碑与清理任务在同一事务里落库（`outbox_events.topic=memory.purge`），
  所以"墓碑存在 ⇒ 清理任务必然存在"。

清理阶段（后台 worker）**物理删除**这些表里该 subject 的行：

| 表 | 为什么算"记忆" |
| --- | --- |
| `memories` | 提炼后的断言正文，就是"你记住的关于我的事"本身 |
| `memory_episodes` | 叙事化摘要，含用户原话的转述 |
| `core_memories` | 称呼/边界/禁忌正文（每轮常驻） |
| `awareness_items` | 跨会话事实投影（第三人称转述） |
| `memory_embeddings` | 向量能从语义反推内容，是派生数据 |
| `memory_entities` | 关联行暴露"这条记忆关于谁" |
| `entities` | `name`/`aliases` 是从原话抠出来的对象名（"我妈""家里那条狗"） |
| `memory_retrieval_logs` | `query` 列**就是用户原话** |
| `media_evidence`（`subject_id` 非空时） | `description` 是对用户图片/视频的文字化 |
| `knowledge_items`（`owner_subject_id` 非空时） | `source_kind=user_statement` 的 `claim` 是用户自述 |
| `prompt_snapshots` | `payload` 是**整份实际发给模型的 messages**，即用户原话的完整副本 |

另外两处**脱敏但不删行**：

- `outbox_events.payload` → `{"purged": true}`（含 `subject_id` 为 NULL 的历史行，
  按 payload 里的 `subject_id` 匹配）；
- `relationship_events.note` → `NULL`（它等于 `user_text[:200]`；行本身是
  熟悉度/信任度投影的可重算来源，删了就再也算不回来）。

**明确不删**：`messages`（对话记录不等于记忆，删它会把"忘了我"变成"清空聊天"）、
`window_contexts` / `handoff_capsules`（对保留中 messages 的摘要）、
`speech_segments`（她自己的语音）、`owner_subject_id IS NULL` 的公共知识。

完成后墓碑置 `done` 并记下各项计数。

前端应该：

1. 点完删除立刻按"已清空"渲染列表（服务端读侧已经这么做），**不要**因为
   `state=pending` 就显示"失败/还在删除中"；
2. 需要给用户一个"彻底完成"的确认时，轮询 `GET /v1/memory/purge/status`，
   等 `state=done`；
3. `state=failed` 才算异常：此时数据没删干净，读侧**仍然**返回空（失败不放行），
   后台会自动重试。

#### 10.2.2 `GET /v1/memory/purge/status`

返回当前 subject 最近 5 条清理记录（新→旧）：

```json
{
  "items": [
    {
      "tombstone_id": "purge_ab12…",
      "state": "done",
      "requested_at": "2026-11-09T05:12:00+00:00",
      "completed_at": "2026-11-09T05:12:03+00:00",
      "memories_deleted": 128,
      "episodes_deleted": 41,
      "core_deleted": 3,
      "awareness_deleted": 6,
      "embeddings_deleted": 128,
      "entities_deleted": 17,
      "retrieval_logs_deleted": 240,
      "events_cancelled": 2,
      "events_scrubbed": 96,
      "error_code": null
    }
  ],
  "note": "…"
}
```

`state` 取值：`pending` / `running` / `done` / `failed`。**只有 `done` 代表
物理删除真的完成了。** `error_code` 是失败原因码（不是异常正文——异常里常常
带着被删数据的片段）。

响应里**不含任何被删内容**：墓碑表本身不存正文，这里也不额外拼文本。
`retrieval_logs_deleted` 单列一项，因为 `memory_retrieval_logs.query` 存的是
**用户原话**，是"原文真的清掉了吗"的直接证据。

媒体描述 / 私有知识 / 提示词快照 / 关系事件 `note` 这四类的计数**不在这个
响应里**（墓碑只有上面这几列），它们进的是 `memory_purged` 审计事件的
`attributes`（`media_evidence_deleted` / `knowledge_items_deleted` /
`prompt_snapshots_deleted` / `relationship_notes_scrubbed`）与 worker 日志。
要查"那几类清了多少"请看审计，不要指望状态端点——它只报墓碑上有的列。

`GET /v1/memory` 与 `GET /v1/core-memory` 在清理进行中（`pending`/`running`/
`failed`）返回空列表、`total=0`，与"物理删除完成后"看到的样子一致；
`GET /v1/awareness` 同样返回空 `items`（`scope="purge_pending"`）。

### 10.3 `GET /v1/core-memory`

响应 `items[]` 在既有 `core_id/label/text/priority/scope/origin_kind`
之外新增：

- `created_at`、`updated_at`：ISO-8601（含 UTC 偏移），来自 `core_memories`
  表；无值时为 `null`。

列表仍只含 `active=true`，按 `priority ASC, label ASC` 排序，每 subject
上限 20 条。查询带硬 `LIMIT 20`，响应 `limit` 字段回显 `20`——两者一致，
即使历史/并发数据超过 20 条也不会“回显 20 却返回 21 条”。`PUT /v1/core-memory`
同 label 覆盖并重新激活；`scope` 非 `private|group|global`、超过上限 →
`422 invalid_request`（统一 `ErrorResponse` 形状）；`DELETE /v1/core-memory/{id}`
是软删除（`active=false`），不存在或非本人 `404`。

`PUT /v1/core-memory` 的 `200` 响应回**完整记录**，字段与列表行完全一致：
`{core_id, label, text, priority, scope, origin_kind, created_at, updated_at}`
——写完不必再 `GET` 一次就能刷新优先级/作用域/时间戳。

`created_at` / `updated_at` 必须与 `core_memories` 表逐字相等，返回“当前时间”
占位不算满足契约。

## 11. 生活时间轴（只读，2026-11 新增）

设计依据：`docs/DREAM-SCHEDULE-DESIGN.md` §7（用户可见性与可观测性）与 §9 阶段 5。
实现：`src/ling/api/routes/life_timeline.py`；契约：`contracts/v1/LifeTimelineOut.schema.json`。

`GET /v1/life/timeline` 把**已经存在**的表（`day_plans` / `schedule_segments` /
`schedule_intentions` / `activities` / `scene_commits` / `dream_reports`）投影成
一天的视图。它**不生成、不重排、不调模型**，也**不写任何表**。

### 11.1 查询参数

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `date` | 当前登录者时区的今天 | `YYYY-MM-DD` | **展示日**（不是角色计划日）；格式非法由 pydantic 返 `422` |

时区**一律由服务端解析**（`settings/user_preferences.py`：DB 偏好 >
`LING_USER_TIMEZONE` > `Asia/Shanghai`）。前端**不要**用浏览器本地时区再判一次
"今天"，也不要自己把 `start_at` 归日。

### 11.2 双时区：展示侧与角色侧

同一个响应里给两套窗口，因为**"你看到的今天"和"她的今天"可以是不同的时间区间**：

| 字段 | 含义 |
| --- | --- |
| `display_date` | 这次请求的展示日（用户时区） |
| `display_timezone` / `display_timezone_source` | 用户时区的生效值与来源（`preference` / `env` / `default`） |
| `display_window_start_at` / `display_window_end_at` | 展示日的 UTC 半开区间 `[D 00:00, D+1 00:00)`（**用户**时区） |
| `actor_timezone` / `actor_timezone_source` | 角色规范时区（owner subject 的同一套解析）与来源 |
| `plan_day` | **角色**计划日（角色本地午夜日）——展示窗口起点落在哪个计划日 |
| `plan_window_start_at` / `plan_window_end_at` | 该计划日的 UTC 半开区间（**角色**时区） |
| `outline_due_at` | 生成到期时刻：`plan_day − 1` 角色本地 21:00 的 UTC 瞬时 |
| `generated_at` | 当前版本**实际提交**时刻（`day_plans.updated_at`）；无计划行时为 `null` |

`outline_due_at` 与 `generated_at` 是两件事："应当什么时候生成"与"实际上什么时候
生成"。DST 当天窗口可能是 23/25 小时，**不要**用 `+24h` 自己算。

### 11.3 状态：`state` 与 `plan_state`

`plan_state` 是库里 `day_plans.state` 的原值（`pending` / `ready` / `degraded`；
没有计划行时 `null`）。`state` 是给界面用的投影，**六种必须分别处理**：

| `state` | 含义 | 界面该说什么 |
| --- | --- | --- |
| `empty` | 这一天没有计划行 | "这一天还没有安排" |
| `pending` | 行已建、还没生成（例如 21:00 到期前） | "还没生成" |
| `ready` | 模型或规则大纲已提交 | 正常展示 |
| `degraded` | 内容是**规则兜底**（模型不可用/输出被拒收） | "来自规则兜底"，并把 `last_error_code` 作为原因 |
| `failed` | 最近一次生成**失败**（行仍是 `pending` 且带原因码） | "生成失败" + `last_error_code` |
| `unavailable` | 这一次**读取出错** | "暂时无法读取" + 重试；**不是**"没安排" |

`degraded_late` / `needs_review` 是 `last_error_code` 里的**原因码**，不是新的
`day_plans.state` 枚举值。`unavailable` 仍返回 `200`（时区字段照常给出），
因为界面必须能分辨"没安排"和"读不到"；`401` 只用于未登录。

### 11.4 `segments[]`：计划层（父子段都在）

每条：`segment_id`、`plan_id`、`plan_day`、`parent_id`、`layer`（`block|hour|minute`）、
`is_leaf`、`status`（恒为 `current`）、`revision`（创建它的计划版本）、`source`
（`model|rule|baseline|user`）、`anchor_kind`（`''` 可变 / `sleep` / `commitment`）、
`commitment_level`（`tentative|confirmed`）、`place_id`、`title`、
`start_at` / `end_at`（**原始**区间）、`display_start_at` / `display_end_at`
（**裁剪到展示日窗口**后的区间）、`continues_from_previous_day` /
`continues_next_day`。

- 查询条件是**相交**（`start_at < window_end AND end_at > window_start`），不是
  "按 `start_at` 归到某一天"：23:30–00:30 的段在两天各自被裁剪成
  `[23:30,24:00)` 与 `[00:00,00:30)`，并分别带 `continues_next_day=true` /
  `continues_from_previous_day=true`。**裁剪值不是段的真实起止**，`start_at` /
  `end_at` 才是。
- 父子段都会返回（前端要能折叠层级）。子段的 `parent_id` 指父段。
- `plan_day` **逐段给出**：跨午夜的段属于前一天的计划，这个值与顶层 `plan_day`
  可能不同。前端不要用 `start_at` 反推归属日。
- 已替代（`superseded`）的段**不返回**（它们与当前段在时间上重叠），只由顶层
  `superseded_segment_count` 计数——用来标"计划改动过"。
- 预计睡眠段 `anchor_kind=sleep` 只表示**预计休息**，不能读成"她睡着了"，
  也不能当承诺展示。

### 11.5 `facts[]`：事实层（与计划分开）

每条：`activity_id`、`state`（`running|paused|completed|failed`）、`origin`、
`evidence_kind`、`scene_valid_until`、`started_at`、`ended_at`、`place_id`。

**计划时段结束不会把段改名成"已做"。** 只有 `activities` 里已经发生过的进程
出现在这里；`evidence_kind=clock_only` 只证明"时间到了"，`observed` /
`user_confirmed` 才是事实。`scene_valid_until` 只在情景提交**仍有效**时给出
（过期即 unknown，`null`），且只挂在那条与之对应的活动上。

### 11.6 `intents[]`：未来 7 天的意向（不是日计划）

每条：`intention_id`、`target_start_day`、`target_end_day`、`state`、`source_kind`
（`model|user|actor|recurrence`）、`visibility`、`priority`。

窗口是角色计划日 **D+1 … D+7**（闭区间），最多 20 条，按"状态 → priority →
目标日 → id"稳定排序。顶层 `horizon_unplanned=true` 是**契约的一部分**：
未来日**没有**生成大纲，这些条目是意向/候选，不是那一天的安排。
`state=confirmed` 只表示用户确认过这个**目标/日期**，不是"某个时段已约好"。

### 11.7 `dream`：梦卡片（模拟叙事）

`{status, origin, visibility, title, narrative, restricted}` 或 `null`。
`null` = **没有记录**；`restricted=true` = 有记录但你看不到正文。

- 库里固定 `origin=simulated`、`visibility=actor_private`；
- 非 owner 读到 `title` / `narrative` 均为 `null`，`restricted=true`；
- `sleep_date` 取角色计划日（醒来所在的角色本地日）。

### 11.8 受众裁决与 `hidden_*` 计数

`visibility=actor_private` 的**段、活动、意向**与**梦**只对 owner 返回正文。
owner 的判定：`role=owner`，或登录账号绑在 owner 的 subject 上。
`visibility` 为 `null` 的旧段表示"未知"，**不猜**成私有（按可见处理）。

| 字段 | 含义 |
| --- | --- |
| `has_private_content` | 这次响应里有内容被受众裁决隐藏 |
| `hidden_segment_count` | 被隐藏的当前段数 |
| `hidden_fact_count` | 被隐藏的活动数 |
| `hidden_intent_count` | 被隐藏的意向数 |
| `dream.restricted` | 梦有记录但正文被隐藏 |

隐藏的内容**不出现在**数组里——不要用"空数组"当"没有内容"，两者靠上面的
计数与 `has_private_content` 区分。

### 11.9 `GET /v1/dashboard` 的两块新摘要（与旧 `agenda` 并列）

`sections.agenda` 的语义**一点不改**（"最多几件小事"：计划/承诺/目标）。
另加两块：

**`sections.today_outline.data`**（`TodayOutlineOut`）

| 字段 | 含义 |
| --- | --- |
| `state` | 同 §11.3（`empty`/`pending`/`ready`/`degraded`/`failed`/`unavailable`） |
| `display_date` / `display_timezone` | 你的今天（展示时区） |
| `plan_day` / `actor_timezone` | 她的今天（角色时区）——两者可以不是同一天 |
| `items[]` | 2–3 条：`{segment_id, relation, title, start_at, end_at}` |
| `note` | 一句纯文本状态说明（空态/降级/失败各不相同） |

`relation` 三值：`now`（覆盖此刻的叶段）、`next`（接下来最近、**不是**已确认
锚点的那一段）、`anchor`（最近的已确认锚点）。已过去的段**不会**被拿来填满
条数；没有就是没有——"下午还没定"不该显示成"安排满了"。

**`sections.intent_teaser.data`**（`IntentTeaserOut`）

| 字段 | 含义 |
| --- | --- |
| `horizon_unplanned` | 恒为 `true`：未来日**尚未生成大纲** |
| `items[]` | 最多 2 条：`{intention_id, title, target_start_day, target_end_day, state, source_kind}` |
| `note` | 一句纯文本说明 |

只给**有来源**的意向（`source_ref` 非空）；`proposed` 候选不会因为上了首屏
就升级。两个区块各自独立标 `status`（`ok`/`empty`/`unavailable`），一块失败
不影响整页。

### 11.10 聊天侧复用同一投影

聊天里问"今天她要干什么"时读**同一份**只读投影：只能说"她**打算/可能**在
09:00 左右吃早餐；下午还没决定"；只有存在真实的 `activities` 且 scene 未过期
才能说"模拟生活中正在……"；`tentative` 不能说约好，`confirmed` 必须附来源；
梦只有用户明确问且受众允许时，以"模拟梦景"呈现。


## 12. 「查看本次提示词」（只读，2026-11 新增）

对话页里她发出的每条消息可以展开"这次她看到的上下文"：**这一轮真正发给模型的
messages** + 每一段属于哪个语义块。用来回答"她为什么这么说 / 为什么没接上话"。

只读、owner 限定。非 owner 与消息不存在返回**逐字节相同**的 404（只差每次请求
各自的 `trace_id`），不泄露"这条消息存在但你没权限"。

### 12.1 `GET /v1/messages/{message_id}/prompt`

```json
{
  "schema_version": "v1",
  "state": "available",
  "message_id": "msg_...",
  "turn_id": "turn_...",
  "created_at": "2026-11-08T03:00:00+00:00",
  "expires_at": "2026-11-15T03:00:00+00:00",
  "model": {
    "schema_version": "v1",
    "provider_id": "fake",
    "model_id": "deterministic",
    "profile": "dialogue.fast",
    "config_revision": 12,
    "source": "explicit"
  },
  "token_total": 18234,
  "truncated": false,
  "dropped_items": 0,
  "sections": [
    {
      "schema_version": "v1",
      "key": "system",
      "label": "人设与规则",
      "role": "system",
      "tokens": 4210,
      "collapsed": false,
      "items": [
        {"schema_version": "v1", "index": 0, "role": "system", "content": "……",
         "tokens": 3810, "sender": null, "at": null, "truncated": false,
         "lane": "persona"}
      ]
    },
    {
      "schema_version": "v1",
      "key": "history",
      "label": "之前的对话",
      "role": "mixed",
      "tokens": 5120,
      "collapsed": true,
      "items": [
        {"schema_version": "v1", "index": 12, "role": "user",
         "content": "阿岚：昨天说的那件事", "tokens": 18, "sender": "阿岚",
         "at": "2026-11-07T15:20:00+00:00", "truncated": false, "lane": "history"}
      ]
    }
  ],
  "note": null
}
```

| 字段 | 含义 |
| --- | --- |
| `state` | `available` / `expired` / `unavailable`，见 §12.2。**三态不可合并** |
| `message_id` / `turn_id` | 这条消息与它所属的轮次（快照一轮一份，`turn_id` 唯一） |
| `created_at` / `expires_at` | 快照写入时刻 / 保留期截止（写入 + **7 天**） |
| `model` | 这一轮**实际解析到**的模型（与设置页"实际生效"同一份结论）。`source` 为 `explicit` / `slot` / `legacy_fallback` / `unavailable` |
| `token_total` | 这一轮**实际发出去**的 token 估算总量。**截断时它仍包含没存进 `sections` 的部分**，所以不必等于 `sum(sections[].tokens)` |
| `truncated` | 快照超过 256KB 被截断（见 §12.4） |
| `dropped_items` | 因超限没存进快照的条目数（`truncated=true` 时 > 0） |
| `sections[]` | 按语义块分组的内容，顺序固定（见 §12.3） |
| `note` | 一句纯文本状态说明（`expired` / `unavailable` 时解释原因，可空） |

`items[].index` 是**真实 messages 数组里的下标**，不是序号。截断后会出现跳号
——跳号正是"中间被丢掉了"的证据，前端**不要重新编号**。

### 12.2 三种状态

| `state` | 含义 | 前端该怎么说 |
| --- | --- | --- |
| `available` | 快照在保留期内，`sections` 是真正发出去的内容 | 正常展示 |
| `expired` | **曾经有过**快照，但已过保留期 / 正文已被清理任务清空。元数据（时间、模型、`token_total`）仍在，`sections` 为空 | 「提示词已过保留期（7 天）清理」——不是"没存过" |
| `unavailable` | **从来没存下来**（捕获失败，或这条消息早于该功能） | 「这次没有保存提示词」 |

`available` 之外的情况**一律 200**（不是 404）：消息本身存在，只是内容没了。
只有"消息不存在 / 不属于你"才是 404。

### 12.3 `sections[]` 的 key 与 label

`label` 由服务端给出，**前端直接显示，不要在前端再翻译一遍**（两处各写一份，
改文案时只有一边生效）。顺序也是服务端固定的：

| `key` | `label` | 里面是什么 |
| --- | --- | --- |
| `system` | 人设与规则 | 人设、边界、此刻的场景、表达方式（system 车道） |
| `persona` | 人设 | 人设本体（如果单独注入） |
| `memory` | 记忆与关系 | 检索到的证据、核心记忆、知识库、短记忆、关系、自我状态 |
| `plan` | 计划与目标 | 与"打算做什么"有关的目标 |
| `media` | 媒体 | 图/语音/视频的描述（"她是不是假装看过"就看这块） |
| `summary` | 更早的对话（摘要） | 已滚出原文窗口的早期对话 |
| `history` | 之前的对话 | 最近的原文消息（`collapsed=true`，默认折叠） |
| `input` | 这次她收到的 | **这一轮**用户说的那句 |
| `other` | 其他注入 | 没登记进映射表的新车道（仍然看得见，只是没归组） |

块归属由**编译器在装配那一刻**标注（车道 → 分组是服务端一张显式表），不是
事后按正文猜的。所以"历史块里的条数"就等于编译时真正喂进去的历史条数。

`sections[].role`：组内所有条目同角色时就是那个角色；历史这种 `user`/`assistant`
交替的为 `mixed`（不挑代表角色，免得读者误判这一段的结构）。

`items[]` 上：`sender` 只有群聊历史才有（"这条是谁说的"）；`at` 是那条消息的
时间；`truncated` 表示**这一条**正文被截过（与整份快照的 `truncated` 是两件事）；
`lane` 是原始车道名（排查"这块到底是谁注入的"用）。

### 12.4 截断（256KB）

单条快照序列化后上限 **256KB**。超了按这个顺序处理，并置 `truncated=true`：

1. 先保住**所有非历史段**（人设/规则/记忆/摘要/媒体/本次输入）与**最后 N 条历史**；
2. 仍超（某一段自己就巨大）→ 从最长的那条正文开始砍半，逐轮直到装得下，
   被砍的条目标 `truncated=true`；
3. 丢了几条如实写进 `dropped_items`。

`token_total` 不受截断影响（它描述的是真正发出去的量）。

### 12.5 保留期与清理

`expires_at` = 写入 + 7 天。worker 的小时级维护任务（`session.purge`）到期后：

1. 先**清空正文**（`payload` 只留 model / token_total），行本身留作"这一轮曾经
   有过快照"的墓碑 → 读接口继续报 `expired`；
2. 墓碑再放 7 天 → 真删（之后才退化成 `unavailable`）。

所以"过期"和"从没存过"在保留期后的一段时间里仍然可分。清理不删任何对话数据。


## 13. 上游不可用时回退到领域默认（2026-11 新增）

设计依据：`docs/UX-FIXES-2026-11-09.md` §4；实现：`src/ling/domains/fallback.py`。

### 13.1 一句话规则

有些**行为参数**不归本域所有，而是"看上游现在怎样"决定的（例如「多久看一次
手机」要看日程：在忙拉长、睡着不排）。上游可能还没生成（`pending`）、生成失败
（`failed`）、本来就没有（`empty`）或读不到（`unavailable` / 超时）。这三种情况下：

- **不硬崩**：本域照常返回可用结果；
- **不拿旧值顶**：上一次的日程、过期场景都不能当作现在还成立；
- **用本域默认参数**跑这一次，并**说清这是默认值**。

### 13.2 `fallback` 标记（`FallbackNotice`）

凡是会回退的区块，`data.fallback` 都给同一个形状（契约
`contracts/v1/FallbackNotice.schema.json`）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `upstream` | string | 上游是谁：`schedule` / `scene` |
| `used` | boolean | 这次是否用了**领域默认参数** |
| `state` | enum | 上游当时的状态：`ready`/`degraded`/`empty`/`pending`/`failed`/`unavailable`/`unknown` |
| `source` | enum | `upstream`（用上游值）或 `domain_default`（回退） |
| `reason` | string \| null | 给人看的一句话；没回退时为 `null` |

**没有回退时这个字段也在**（`used=false`、`reason=null`）。缺字段会让前端
分不清"没用回退"和"后端没实现"。

**它不是事实**，也不能被读成事实：

- `used=true` 只说"上游这次没给出可用的值"，不代表上游坏了，更不代表她做了什么；
- 默认值**绝不**写进 `activities` / `self_experiences` / 位置 / `day_plans` /
  `scene_commits`——回退只影响**本次**的参数，写入仍归各自的状态机；
- 上游恢复后，下一次调用直接读新鲜值；回退结果不会被缓存成事实。

### 13.3 当前两处落点

**`sections.phone.data`**（看手机）

| 字段 | 含义 |
| --- | --- |
| `next_look_interval_minutes` | 下次隔多久看她一眼。上游（日程当日段）可用时按段的活动类别给（`focus/work` 45、`meal` 30、`leisure` 15、`sleep` 0 = 醒来再排）；否则用 `DEFAULT_IDLE_MINUTES`（10） |
| `fallback` | 见 §13.2；`upstream=schedule` |

空态（从没看过手机）也会给 `next_look_interval_minutes` 与 `fallback`——
**参数**不跟着**事实**一起消失；但空态里不会出现 `state` / `last_looked_at`
这类"她拿过手机"的字段（那是在编事实）。

**`sections.presence.data`**（当前场景）

| 字段 | 含义 |
| --- | --- |
| `scene.state` | `chat`/`focus`/`rest`/`errand`/`unknown`；`valid_until` 过期即 `unknown` |
| `scene.expired` | 这次读到的是不是已过期（过期不沿用旧场景） |
| `fallback` | 见 §13.2；`upstream=scene` |

只给状态、不给 `scene.activity_id` / `place_id`：首屏不需要，也不该在这里被读成
"她正在做 X"。

### 13.4 判定表（与 §11.3 的 `TimelineState` 对齐）

| 上游状态 | 能否当当前输入 | 消费点行为 |
| --- | --- | --- |
| `ready` | 能 | 用上游值，`source=upstream` |
| `degraded` | **只能作计划参考** | 行为参数可以用（`used=false`、`state=degraded`，标记里如实写"规则兜底"）；**任何断言"她正在做 X"的地方都不能用**，那种地方回退到领域默认 |
| `empty` / `pending` / `failed` / `unavailable` / `unknown` | 不能 | 用领域默认，`used=true`，理由说清是哪种 |
| 读超过 300 ms 预算 | 不能 | 按 `unavailable` 回退，`reason` 写出预算 |

### 13.5 还没接上的调用点（下一批）

下面这些也读上游，但本批**没有**改（各有各的理由，按风险排）：

| 调用点 | 读什么 | 当前行为 | 下一批怎么做 |
| --- | --- | --- | --- |
| `api/routes/dashboard.py::today_outline()` | `load_actor_day` | 读取失败 → 整块 `unavailable`；状态原样透出，本就不伪造 | 加 `fallback`（仅"无值可展示"时），不改状态语义 |
| `api/routes/life_timeline.py::_load_facts()` | `SceneCommitRow.valid_until` | 过期 → `scene_valid_until=null`（已按 §11.5 处理） | 加 `input_sources.scene` 标记 |
| `kernel/self_view.py::_add_scene()` | `PresenceService.scene()` | 过期/读失败 → 丢弃（不注入提示词） | 复用 `resolve_scene()`，在 `SelfView.dropped` 里带原因 |
| `kernel/autonomy.py` 的处境读取 | presence / 负担 / 情绪 | 读失败 → 用默认值继续（已有 try/except） | 统一走 `read_within_budget`，标记进决策事件 |
| `worker.py` 的 `life.tick` 场景提交 | 日程段 | 无段就不提交；过期靠 TTL 自然转 unknown | **写路径**，需单独设计（本批只做只读） |
| `domains/phone` 的"真的采用这个节奏" | 调用 `schedule_next_from_upstream` | 目前只有读侧投影；谁调 `stop_watching` 由调度决定 | 调度接线时直接调它（已带日志与回退标记） |
