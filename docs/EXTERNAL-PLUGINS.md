# Ling 外部插件系统 v1

Ling v1 允许第三方用一个独立 HTTP 服务提供工具、用户命令、事件订阅和受控宿主状态能力。插件代码不进入 Ling 进程，不获得数据库、模型密钥或会话历史；端点由管理员登记，聊天者和模型不能指定 URL。

## 10 分钟接入

1. 启动示例服务：

   ```bash
   python examples/external-plugin/server.py --host 127.0.0.1 --port 8096
   ```

   它只监听本机；容器部署时改用 API/worker 都能访问的私有地址，不公布到公网。

2. 管理员登记并启用（owner/admin）：

   ```bash
   curl -X POST http://127.0.0.1:6520/v1/plugins/register \
     -H 'Content-Type: application/json' \
     -d '{"endpoint":"http://127.0.0.1:8096","expected_id":"example.notes","granted_capabilities":["tools","commands","events","host.state"],"enabled":true}'

   curl -X POST http://127.0.0.1:6520/v1/plugins/example.notes/enable \
     -H 'Content-Type: application/json' -d '{"enabled":true}'
   ```

   登记时 Ling 会 GET `{endpoint}/v1/manifest`，校验版本区间和能力白名单。能力逐项声明、逐项授予；未声明的能力不会被调用。

3. 使用：

   - 用户命令：在非密封私聊发送 `/plugincmd example.notes lookup 茶`。
   - 模型工具：开启原生工具后，模型可调用 `plugin_example_notes_lookup` 等工具；插件结果按不可信数据包裹后回灌。
   - 事件：完成轮次时 Ling POST `{endpoint}/v1/events`，插件按 `event_id` 去重。
   - 宿主状态：插件在 invoke 响应里请求 `host.state`，Ling 用同一 `request_id` 带回结果，最多 4 轮。

## 版本兼容

- manifest 固定 `manifest_version: "v1"`、`api_version: "ling-plugin/1"`。
- `min_host_version` / `max_host_version` 是闭区间；当前 Ling 版本取 `ling.__version__`，超界拒绝登记/启用。
- 未知能力、未知事件类型、非法版本范围一律失败关闭；v1 不做自动升级，重新登记或 `/sync` 后才采用新 manifest。

## 协议

### manifest（`GET /v1/manifest`）

```json
{
  "manifest_version": "v1",
  "id": "example.notes",
  "version": "1.0.0",
  "name": "示例笔记插件",
  "api_version": "ling-plugin/1",
  "min_host_version": "0.0.14",
  "max_host_version": "0.0.14",
  "capabilities": ["tools", "commands", "events", "host.state"],
  "events": ["turn.completed"],
  "tools": [{
    "name": "lookup",
    "description": "查询目录",
    "input_schema": {
      "type": "object",
      "properties": {"query": {"type": "string", "maxLength": 200}},
      "required": ["query"],
      "additionalProperties": false
    }
  }],
  "commands": [{"name": "lookup", "pattern": "^/plugincmd example\\.notes lookup (.+)$"}]
}
```

### invoke（`POST /v1/invoke`）

请求：

```json
{
  "api_version": "ling-plugin/1",
  "request_id": "plgreq_abc123",
  "plugin_id": "example.notes",
  "method": "tools.call",
  "params": {"tool": "lookup", "arguments": {"query": "茶"}},
  "host_results": []
}
```

成功：

```json
{
  "api_version": "ling-plugin/1",
  "request_id": "plgreq_abc123",
  "result": {"text": "示例目录：茉莉花茶、白茶"},
  "host_calls": []
}
```

错误：

```json
{
  "api_version": "ling-plugin/1",
  "request_id": "plgreq_abc123",
  "error": {"code": "invalid_arguments", "message": "query 不能为空"}
}
```

命令使用 `method: "commands.call"`、`params: {"command":"lookup","arguments":{"text":"茶"}}`。`result.text` 是唯一被宿主读取的正文；其他字段忽略。

### host_calls / host_results

插件可在成功响应里请求 `host.state`：

```json
"host_calls": [
  {"call_id":"call_1","capability":"host.state","op":"set","key":"note","value":"记住这句"}
]
```

Ling 执行后以同一 `request_id` 再 POST 一次，把结果放进 `host_results`：

```json
"host_results": [{"call_id":"call_1","ok":true,"value":null}]
```

`get` 返回 `value` 字符串或 `null`。每次值最多 4096 字符，默认 TTL 7 天；按 `plugin_id` 隔离，插件 A 读不到 B 的键。

### 事件（`POST /v1/events`）

```json
{
  "api_version": "ling-plugin/1",
  "event_id": "evt_turn_completed_turn_abc_1_example.notes",
  "type": "turn.completed",
  "occurred_at": "2026-11-22T05:00:00Z",
  "data": {"turn_id":"turn_abc","window_id":"win_abc","subject_id":"subj_abc","attempt":1}
}
```

- 稳定 ID：`event_id` 由 turn_id、attempt、plugin_id 组成。重复投递必须幂等。
- 重试语义：至少一次；2xx 视为收到。事件端点 2 秒超时，失败不会让聊天失败，也不会无限重试。
- 载荷只有 ID/时间/次数，不含正文、密钥或凭据。

## 管理与旧入口

| 路径 | 权限 | 用途 |
|---|---|---|
| `POST /v1/plugins/register` | owner/admin | 登记并抓取 manifest |
| `GET /v1/plugins` | owner/admin | 列表（含旧 `tool-plugins.json` 摘要） |
| `POST /v1/plugins/{id}/enable` / `disable` | owner/admin | 启停 |
| `GET /v1/plugins/{id}/status` | owner/admin | 健康与最近错误 |
| `POST /v1/plugins/{id}/sync` | owner/admin | 重新读取 manifest |
| `DELETE /v1/plugins/{id}` | owner/admin | 取消登记 |
| `GET /v1/settings/tool-status` | owner/admin | 原工具状态页，新增 `external_plugins` 字段 |

旧 `tool-plugins.json` 与 `/plugin catalog 茶` 继续工作；新登记插件使用 `/plugincmd`。旧的只读 HTTP 插件不自动变成 v1 插件，需管理员按本文重新登记。

## 失败与限制

- 插件请求 6 秒超时、manifest 4 秒、事件 2 秒；响应上限 16 KiB；不跟随重定向，不接受压缩响应。
- 插件不可用、非 200、坏 JSON、错误 schema、无结果都变成明确失败；普通聊天仍完成。
- 插件结果始终是 `output_untrusted`，进入模型前带数据边界，不能当指令。
- 插件不能请求宿主密钥、会话历史、其他插件状态或任意数据库查询；未知能力被拒。

## 备份范围

- PostgreSQL 整库备份覆盖：登记记录（端点、版本、能力、启用状态）、插件状态键值与过期时间。
- 部署侧备份：`LING_DATA_DIR/tool-plugins.json`（旧入口配置）需随应用数据目录一起备份。
- 示例插件自身的日志与临时数据由插件开发者自行管理，不属于 Ling 备份承诺。

## 设计文档

调研、方案取舍、能力权限对照、冻结验收判据保存在仓库本地 `docs/design/external-plugins-v1-*.md`（该目录按仓库约定不推送）。
