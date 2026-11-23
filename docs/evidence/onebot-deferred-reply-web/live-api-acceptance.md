# 真实本地 API 验收记录

运行对象（2026-11-23）：

- 代码：本工作树 `src/`（editable 安装到本工作树 `.venv`）
- 数据库：`postgresql+asyncpg://ling@127.0.0.1:55432/ling_accept`（独立库，迁移到 `0089_onebot_deferred_reply`）
- 服务：`python -m ling`，`127.0.0.1:8098`，`LING_ENV=dev`
- 账号：`deploy`（owner）、`member`（member，同 subject）
- 数据：两个 OneBot 私聊（P1/P2）、一个群聊、一个未确认实例私聊、一个已归档私聊、一个网页会话

以下每条都是 `curl` 打到 `http://127.0.0.1:8098` 的真实响应（写操作带 `Origin: http://127.0.0.1:8098`）。

| # | 请求 | 结果 |
| --- | --- | --- |
| 1 | `GET /v1/sessions/P1/onebot-deferred` | `{"enabled":false}` 200 |
| 2 | `PUT` 同路径 `{"enabled":true}` | `{"enabled":true}` 200 |
| 3 | 再次 `GET` P1 | `{"enabled":true}` 200 |
| 4 | `GET` P2 | `{"enabled":false}` 200 |
| 5 | `PUT` P1 `{"enabled":false}` | `{"enabled":false}` 200 |
| 6 | 再次 `GET` P1 | `{"enabled":false}` 200 |
| 7 | `PUT` P2 `{"enabled":true}` | `{"enabled":true}` 200 |
| 8 | `GET` P1 / P2 | `false` / `true`（各自保存） |
| 9 | `GET` 群聊 | 422 `请选择 OneBot 私聊会话` |
| 10 | `GET` 网页会话 | 422 `请选择 OneBot 私聊会话` |
| 11 | `PUT` 未确认实例 `{"enabled":true}` | 409 `请先确认会话的 OneBot 接入实例` |
| 12 | `PUT` 未确认实例 `{"enabled":false}` | `{"enabled":false}` 200（关闭仍可操作） |
| 13 | `GET` 已归档私聊 | 409 `会话已归档，请恢复后再修改设置` |
| 14 | `GET` / `PUT` 非所有者 | 403 `需要所有者权限`（detail `not_owner`） |

## 浏览器验收（生产构建 + 同源代理 + 真实 API）

脚本用 Playwright 打开 `http://127.0.0.1:5179`（`dist/` 静态产物 + `/v1` 反向代理到 8098），
用真实账号登录后操作会话设置。输出：

- `10-deferred-off.png` 默认关闭
- `11-deferred-on.png` 开启后，说明文案随之改变
- `12-after-reload.png` 刷新后仍开启
- `13-deferred-disabled.png` 关闭后回到关闭
- `20-group.png` 群聊不显示开关
- `21-web.png` 网页会话不显示开关
- `22-unconfirmed.png` 未确认实例仍可打开（关闭操作保留）
- `23-unconfirmed-409.png` 开启被 409 拒绝，开关回到关闭并指向「平台来源」
- `24-member.png` 非所有者没有会话设置入口

自动化用例（`src/onebot-deferred.browser.test.tsx`，9 条）另外覆盖了窗口**来回**切换
（A→B→A→B，每次重开都重新读服务端）与重复点开、关闭设置。

日志：`web-tests.log`（45 文件 / 1108 通过 / 1 跳过）、`web-build.log`、
`backend-deferred-tests.log`（8 通过）、`backend-policy-tests.log`（117 通过）。
