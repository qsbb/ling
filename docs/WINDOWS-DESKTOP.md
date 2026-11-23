# Windows 桌面任务

所有者可以配对一台 Windows 电脑，允许 Ling 打开一份临时记事本、写入文本并回读。Windows 伴生端主动访问 Ling API，界面可从 API 看到连接状态、处理到哪一步和最终结果。

## 配对与试运行

1. 在 Ling 的所有者会话调用 `PUT /v1/desktop/pair`，提交 `{"name":"Windows 测试机"}`。把返回的 `token` 复制给 Windows 终端。
2. 在已登录的 Windows 桌面，用普通用户身份打开终端。安装 Python 3.12 或以上版本，在同一版本的 Ling 源码目录运行以下命令。终端提示配对令牌时粘贴，也可提前设置环境变量 `LING_DESKTOP_TOKEN`。

   ```powershell
   py -m pip install .
   py -m connectors.desktop --url https://ling.example
   ```

3. 确认 `GET /v1/desktop` 返回 `device.state: "ready"`，调用 `PUT /v1/desktop/control` 提交 `{"paused":false}`。之后用 `POST /v1/desktop/tasks` 提交一段有内容的测试文字。
4. 从 `GET /v1/desktop` 查看结果。`step: "verified"` 表示伴生端从本次记事本编辑区读回文本，并与提交内容逐字对照。Windows 换行符统一为 LF。任务在编辑区留下待保存的文本，暂停、取消或退出时也不会关闭操作员正在看的窗口。

终端按 **Ctrl+C** 可退出。Windows 可以在后台打开记事本。操作期间可切到本次记事本查看，切到其他窗口、切换记事本标签、锁屏或断线会停止推进。重新连接且状态为就绪后，所有者可恢复接收并提交新任务。

## Web 接口

沿用 Web 登录会话和所有者校验。写请求使用同源 Cookie，带 JSON 的请求使用 `Content-Type: application/json`。开发环境的 OpenAPI 位于 `/api/openapi.json`。

| 方法与路径 | 请求与响应 |
| --- | --- |
| `GET /v1/desktop` | `{device, paused, tasks}`。尚未配对时 `device` 为 `null`。已配对时含 `name`、`state`、`last_seen_at`。返回最近 20 条任务，新建的在前。 |
| `PUT /v1/desktop/pair` | `{name}`，最长 64 字符。返回 `{token}`，只在这次响应中可读。重新配对立即替换旧令牌、终止在途任务，并暂停接收。 |
| `DELETE /v1/desktop/pair` | 无请求体。撤销配对并保留历史，返回 204。 |
| `PUT /v1/desktop/control` | `{paused:boolean}`。返回最新状态。暂停会终止在途任务，恢复需伴生端已就绪。 |
| `POST /v1/desktop/tasks` | `{request_id,text}`。编号用 UUID v4，文本 1–2000 字符且至少包含一个非空白字符，成功返回 201 和任务。同编号同文可重试，同编号异文返回 409。 |
| `POST /v1/desktop/tasks/{id}/cancel` | 无请求体。取消单项任务并返回任务。已结束任务返回原结果。 |

设备状态有 `offline`、`unavailable`（伴生端在线，交互桌面不可用）、`ready`。超过 10 秒未收到有效心跳即视为离线。普通成员调用 Web 接口返回 403。请求冲突使用 `error.detail`，取值为 `not_paired`、`paused`、`desktop_not_ready`、`task_in_progress` 或 `request_id_conflict`。

每条任务含 `id`、`kind`（`notepad_write`）、`text`、`state`、`step`、`reason`、`created_at`、`started_at`、`finished_at`。时间字段使用带时区的 ISO 8601 格式。`step` 为 `null`、`opened`、`written` 或 `verified`，记录服务端已经收到的最后一步回执。

| 状态 | 含义 |
| --- | --- |
| `queued` / `running` | 等待领取 / 伴生端已领取。一次只运行一项。 |
| `succeeded` | 文本回读一致。 |
| `failed` | 打开失败、编辑区不可用、读写失败或回读不一致。 |
| `interrupted` | 暂停、窗口变化、桌面不可用、连接中断、执行超时或重新配对后终止。 |
| `cancelled` | 所有者取消、暂停时任务还在等待，或等待领取超时。 |

原因为 `null` 或以下机器可读值。

- `owner_paused`、`owner_cancelled`、`pairing_changed`
- `queue_expired`、`lease_expired`、`connection_lost`
- `desktop_unavailable`、`window_changed`
- `open_failed`、`editor_unavailable`、`editor_error`、`readback_mismatch`
- `authorization_expired`、`authorization_revoked`

## 授权时段内的活动

所有者可授权一次记事本活动，指定时间段和可写入的文字。生活 worker 在这段时间内查看当前日程。遇到空闲、爱好或写作时段，可创建一条任务。当前安排是休息时，记下这次选择休息。有约定、尚无合适时段或电脑暂不可用时等候到期。

| 方法与路径 | 请求与响应 |
| --- | --- |
| `GET /v1/desktop/autonomy` | 返回 `{grant}`，初始为 `null`。 |
| `PUT /v1/desktop/autonomy` | 提交 `{request_id,start_at,end_at,text}`。使用 UUID v4 和带时区的时间。返回 `{grant}`。同编号、相同内容可重试。新授权替换上一份。 |
| `DELETE /v1/desktop/autonomy` | 撤销授权，返回 `{grant}`。当次已入队或执行的活动会随授权结束。 |

`grant.decision` 取 `pending`、`rest`、`queued`、`expired` 或 `revoked`。已排队时 `task_id` 对应任务列表中的记录。任务的 `source` 为 `life` 或 `manual`，自主活动带 `source_segment_id`。已授权任务最多执行一次，日程片段结束后租期也会到期。更换配对会清除原授权。

记事本回读一致后，会在生活时间线上留下实际写作记录。界面可用回读证据区分已完成的记录和当前日程。

## 伴生端协议

领取和回执都使用专用配对令牌。伴生端每约 2 秒向 `POST /v1/desktop/companion/poll` 提交 `{ready:boolean}`。界面只访问上一节的 Web 接口。

等待领取限时 15 秒，领取后有 20 秒租期。领取返回 `task: {id,text,lease_token,lease_seconds}`，无任务时 `task` 为 `null`。回执发往 `POST /v1/desktop/companion/tasks/{id}/report`，包含 `lease_token`、`step`，验证步骤附 `readback`，中止步骤 `aborted` 附 `reason`。服务端收到 `opened` 并返回 `accepted:true` 才允许写入，收到 `written` 并返回同样的响应才开始回读。

暂停、取消或撤销配对后，旧租期不再接受回执。Windows 已开始的单次编辑动作可能先完成，界面保留最后收到的步骤。断线期间的尝试也不重放。

Windows 端固定启动系统记事本，用临时文档名找到唯一的窗口和编辑控件。每步重新核对交互会话、窗口、活动标签和前台切换情况。首版操作经典 `Edit` 或 `RichEditD2DPT` 编辑控件，文本发送到该控件并从同一控件回读。参见 [Windows 11 记事本实现](https://devblogs.microsoft.com/math-in-office/windows-11-notepad/) 和 [Windows 文本读取接口](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-gettext)。
