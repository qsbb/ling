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

## 4. 模型设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/settings/config` | `{revision, state, test_mode, providers[], profiles[], revisions[]}` |
| PUT | `/v1/settings/draft` | `{providers?, profiles?, test_mode?}` → 新草稿修订 `{revision, validation_errors}` |
| POST | `/v1/settings/publish` | `{revision}` → `{published_revision}`（校验失败 422 + 错误列表） |
| POST | `/v1/settings/rollback` | `{to_revision}` → `{published_revision}` |
| PUT | `/v1/settings/providers/{provider_id}/secret` | `{secret}` → `{last4, fingerprint}`（永不回显原文） |
| DELETE | `/v1/settings/providers/{provider_id}/secret` | 删除凭据 |
| POST | `/v1/settings/providers/{provider_id}/probe` | `{ok, detail, latency_ms, models[]}` |

`profiles[]` 每项：`{profile, provider_id, model_id, fallback_provider_id?, fallback_model_id?,
enabled, temperature, max_tokens?, timeout_seconds, daily_cost_limit?}`。

## 5. 角色与时间线

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/persona` | `{persona_id, revision, display_name, system_prompt, traits, boundaries}` |
| PUT | `/v1/persona` | 同形状（部分字段）→ 新修订；立即影响后续轮次 |
| GET | `/v1/actor/timeline?limit=50` | `{events: [{id, type, origin, visibility, occurred_at, summary}]}` |

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

## 8. 前端必须遵守

1. 浏览器不保存权威历史；刷新后从 `/v1/sessions/{id}/messages` 重建。
2. 流式渲染是**增量展示**，不落本地存储；断线后重取消息接口。
3. `test_mode = true` 时必须在界面显著位置显示“测试模式：回复由确定性 fake provider 生成”。
4. 失败必须显示可执行提示（重试按钮 + 具体原因），不得静默吞掉。
5. 移动端（375×667 起）与桌面（1440×900）都要能完成发送、停止、切换会话。
6. 更新状态只能按服务端 `enabled/reason/executor/current_job` 展示；`message`
   直接显示服务端文案，不自行拼接宿主路径或日志。通道关闭（`enabled=false`）
   时不得显示可点的“更新”入口。

## 9. 仪表盘与管理查询：日期 / 状态语义（2026-10 变更）

### 9.1 `GET /v1/dashboard` 的「今天的安排」

- `timezone` 仍是响应顶层字段，取 `LING_USER_TIMEZONE`（未配置时 `UTC`）。
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
| POST | `/v1/memory/forget` | `{retracted: 影响行数}` | 无 body；只清当前 subject；返回的是 MemoryRow + Episode + CoreMemory + Awareness 四类受影响行数之和（**四类都清**，不是只清 MemoryRow） |

`forget` 仍是软清除（撤回/归档/停用/失效），不物理删除；清除后默认
`GET /v1/memory` 为空，`?include=all` 仍能看到墓碑。

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
