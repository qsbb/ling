# 从 AstrBot 导入旧记忆

Ling 接收 AstrBot Memory Companion 中本人和指定旧 Bot 的 QQ 私聊长期记忆。迁移保留旧摘要、手工记忆、明确记忆和用户陈述型资料。Ling 按已确认的 OneBot QQ 绑定找到当前账号的记忆库。

## 生成迁移文件

`/mcomp data export` 生成的 `memory-companion-日期-时间.jsonl` 缺少原有效期、有效状态和归属 Bot 字段。Ling 需要从插件的 SQLite 备份取得这些字段。

在 AstrBot 的 Memory Companion 插件数据目录找到 `memory_companion.db`，为独立备份选一个新文件名，然后用 SQLite 生成快照。

```bash
sqlite3 /path/to/memory_companion.db ".backup 'memory-companion-backup.db'"
```

在 Ling 仓库根目录运行转换命令。把备份路径、旧 Bot 的 QQ 和本人的 QQ 换成实际值。

```bash
python3 scripts/export_astrbot_memories.py /path/to/memory-companion-backup.db ling-memories.jsonl \
  --bot-qq 200300400 --owner-qq 100200300
```

在「记忆 → 导入旧记忆」选择生成的 `ling-memories.jsonl`，核对预览里的目标自然人和记忆样本后确认。转换脚本默认导出整份，仍可用 `--limit` 和 `--offset` 主动选取一段。

导入接口按批读取和写入，临时文件使用 Ling 数据目录所在的磁盘。预览与确认均上传同一份文件，界面可以沿用已选的文件。若自行配置了反代，确保它允许这两条导入路径传输大文件。仓库的 nginx 模板已有对应配置。

## API

使用所有者会话。Cookie 请求沿用控制台的同源要求。两个接口的请求体都是同一份文件的原始字节，`Content-Type` 使用 `application/x-ndjson`。浏览器可以把 `File` 直接作为请求体发送。

| 请求 | 成功响应 |
| --- | --- |
| `POST /v1/memory/import/preview` | `destination`、`total`、`accepted`、`duplicates`、`skipped`、`skip_reasons`、`sample`、`confirmation_token`、`expires_at` |
| `POST /v1/memory/import/commit` | `destination`、`total`、`imported`、`duplicates`、`skipped`、`skip_reasons`。另传 `X-Ling-Import-Token` 请求头 |

`destination` 包含 `subject_id`、`display_name`、`qq` 和 `bot_qq`。其中 `qq` 是本人的 QQ，`bot_qq` 是导出的旧 Bot。目标由服务端确定。直接绑定的本人 QQ，以及已归入本人自然人且没有独立 Web 登录的 QQ 身份，都可以通过核对。

`sample` 最多 12 条，每条有 `source_id`、`kind`、`text`、`occurred_at`、`created_at` 和 `valid_until`。样本文本最多 400 个字符，完整原文在导入后可在记忆库查看。有效期与发生时间可以为空。

预览只读。没有可导入条目时，两项确认字段都是 `null`。确认凭据 15 分钟内有效。它对应原文件的准确字节、当前用户、目标自然人、QQ 绑定、遗忘代次和保留策略。确认时会跳过新近到期和已导入的记录。原先还未生效的记忆，等下一次预览才会纳入。实际导入条数以 `imported` 为准。重复点击或网络中断后重试同一份文件会跳过已导入的记录。

```ts
const preview = await fetch('/v1/memory/import/preview', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-ndjson' },
  body: file,
});

// After presenting the preview and receiving the user's confirmation.
const result = await fetch('/v1/memory/import/commit', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/x-ndjson',
    'X-Ling-Import-Token': confirmationToken,
  },
  body: file,
});
```

## 结果和错误

`total = accepted + duplicates + skipped`，确认后的公式用 `imported` 替换 `accepted`。每条被跳过的记录只计一个原因，`skip_reasons` 只列非零项。

| 跳过原因 | 页面文案 |
| --- | --- |
| `invalid_record` | 记录格式不完整 |
| `outside_private_pair` | 不属于所选 QQ 私聊 |
| `unsupported_memory` | 此类旧记忆暂不支持 |
| `inactive` | 原记忆已失效 |
| `not_reviewed` | 原记忆待审核 |
| `restricted` | 原记忆受来源权限限制 |
| `not_yet_valid` | 尚未到生效时间 |
| `expired` | 已超过原有效期 |
| `retention_expired` | 已超过 Ling 的保留期 |

失败沿用 `{ error: { code, message, detail, retriable, trace_id } }`。

| HTTP | `error.detail` | 页面处理 |
| --- | --- | --- |
| 401 / 403 | 常规登录、同源或权限错误 | 复用控制台现有处理 |
| 409 | `preview_invalid` | 保留文件，重新预览 |
| 409 | `qq_binding_required` | 引导核对自然人设置里的本人 QQ 绑定 |
| 409 | `destination_unavailable`、`memory_purge_in_progress` | 显示服务端说明，恢复后重新预览 |
| 422 | `astrbot_native_archive` | 引导使用 SQLite 备份生成迁移文件 |
| 422 | `invalid_encoding`、`unsupported_archive`、`invalid_source_pair` | 保留错误反馈，允许换文件 |
| 413 | 入口反代限制上传大小 | 确认反代的上传配置，保留文件以便重试 |

成功后刷新 `GET /v1/memory?view=library`。导入条目附带 `import_source`。其中 `source` 为 `astrbot_memory_companion`，其余字段有原记录 ID、原类型、平台、会话、QQ、发生时间、原创建和更新时间以及原有效期。详情可显示「来自 AstrBot」及原时间。摘要以旧经历保存，按模型总结处理。断言仍沿用 Ling 正常的纠正和撤回操作。私聊记忆受 Ling 现有的可见范围和保留策略约束。

「查看 AstrBot 记忆」可请求 `GET /v1/memory?view=library&source=astrbot_memory_companion&limit=50&offset=0`。响应沿用记忆库字段，并回传 `source` 和 `offset`。根据 `has_more` 继续请求下一页，`offset` 每次加上 `limit`。列表默认按原创建时间从新到旧显示，翻页可以查看更早的记忆。现有 `include` 参数也适用于来源筛选。
