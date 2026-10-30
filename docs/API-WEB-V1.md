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
