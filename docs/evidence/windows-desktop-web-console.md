# Windows 桌面任务 · Web 控制台（前端）

页面：`/desktop`（左导航「桌面」，**只对所有者出现**）。后端契约见
`docs/WINDOWS-DESKTOP.md`（`lingxi/backend-continuation@c339161`），本文件只记录
**前端这一侧**的决定、实测与仍未验证的部分。

## 这一页怎么排，为什么

三个问题按被问到的顺序从上往下排：

1. **这台电脑能接任务吗** —— 「电脑」一块（名称、连接徽标、接收开关）
   + 由它们算出来的一句话结论。
2. **Ling 做到哪一步了** —— 「这一步」（在途任务的四步进度与结果，可取消）
   + 「最近任务」（最近 20 条）。
3. **我要做什么** —— 「写一段测试文本」（手工试跑）与「限时授权」，两个入口分开。

判断（能不能提交、为什么不能、下一步归谁）全在 `src/desktop.ts` 的纯函数里，
`src/desktop.test.ts` 对设备三态 × 暂停的组合逐条断言。渲染只消费结论，
不在 JSX 里再判一次——两处判断迟早不一致，而且不一致的那一处查不出来。

几条具体的取舍：

- **「不能提交」的原因只说一次**，放在提交按钮旁（`desktop-submit-blocked`）。
  设备块只在**能**提交时给一句正向结论。同一句话在屏幕上出现两次，会被读成两个原因。
- **未配对时不渲染接收开关**：此处的 `paused=true` 含义是"还没配过"，不是"所有者
  暂停了"。画一个「恢复接收」只会让人点出 `409 not_paired`。
- **在途任务不重复铺开**：「这一步」是主视图，「最近任务」里那条压成一行
  （状态 + 去向）。否则同一条任务在一屏出现两次。
- **令牌不自动消失**：它要粘到另一台电脑的终端里。重新生成会替换令牌并中断在途
  任务，所以只由两个明确出口清掉（「我已粘贴好」/ 离开页面）。离开页面即丢弃，
  不写 localStorage、不进 URL。

## 端到端实测（本地真实 API + 隔离 PostgreSQL）

环境：`ling_desktop_web`（`127.0.0.1:55432`，迁移到 `0089`），API 在 `127.0.0.1:8099`，
Web 用 Vite 开发服务器（`LING_API_TARGET=http://127.0.0.1:8099`）代理到同一份后端。
浏览器：Playwright + 真实 Chromium，1440 / 768 / 390 三种宽度。

真实接口走过的路径（全部对着真后端，不是 mock）：

| 环节 | 结果 |
| --- | --- |
| 未配对 → 生成令牌 | 令牌 43 字符；剪贴板读回与页面一致；离开页面后令牌不再出现 |
| 伴生端握手 → 恢复接收 | 设备变 `ready`，恢复后「已允许」，结论为"可以接一条测试任务" |
| 提交（中文 + emoji + 前导空格 + 多行） | `201`，请求体 `text` 逐字与输入一致，`request_id` 为 UUID v4 |
| 在途时再提交 | 按钮禁用，提示"上一条还在等待或执行" |
| 伴生端 poll → opened → written → verified | 页面依次显示步骤推进，最终「已写入并从记事本回读一致」 |
| 成功后的输入框 | 清空；"已提交，等待伴生端领取"横幅在状态推进后自行退场 |
| 暂停（任务执行中） | 任务变 `interrupted / owner_paused`，已回执的「打开记事本」保留 |
| 暂停后迟到的回执 | `accepted:false`，任务仍是 `interrupted`（没有把结果改回去） |
| 恢复接收 | 上一条保持 `interrupted`；随后可以提交新任务 |
| 单条取消 | `cancelled / owner_cancelled`；可以立刻提交下一条 |
| 重新配对 | 换发新令牌，在途任务 `interrupted / pairing_changed`；旧令牌 `401` |
| 授权（未来时段） | `PUT` 成功，`decision: pending`；**刷新后仍在**（持久化） |
| 授权（过去时段 / 倒置时段） | 前端拦下，两种说法不同 |
| 撤销授权 | `decision: revoked`（后端确认），页面不再给撤销入口 |
| 普通成员直接访问 `/desktop` | 页面显示"只对所有者开放"；`GET /v1/desktop` 返回 `403 not_owner`；导航无「桌面」 |

截图与原始日志在 `/tmp/wdweb-shots/` 与 `/tmp/wdweb-*.log`（本地临时目录，未入库）。

## 哪些用了模拟响应

- `src/desktop.browser.test.tsx`：`/v1/**` 全由 `page.route` 模拟，用于
  **可重复**的排版与交互断言（横向溢出、UUID 复用、离开即停、成员 403）。
  载荷形状取自 `contracts/desktop.py`，但它们是构造值，不是服务端实况。
- 单测（`desktop.test.ts` / `desktop-render.test.tsx` / `desktop-polling.test.ts`）
  只测纯函数与 SSR 输出。

## 怎么复验这一遍（可重跑的命令）

前端本身不需要后端就能验：`pnpm run typecheck`、`pnpm run build`、
`pnpm exec vitest run`（浏览器用例自带 Vite 服务器与 `/v1` 模拟，不需要真后端）。

要复验**真实接口**那一段，需要三个本地进程：

```bash
# 1) 隔离 PostgreSQL（若本机已有实例在 55432，直接复用）
cd .agents/worktrees/windows-desktop-web
scripts/dev_pg.sh start                       # 或用已有实例
createdb -h 127.0.0.1 -p 55432 -U ling ling_desktop_web
LING_DATABASE_URL=postgresql+asyncpg://ling@127.0.0.1:55432/ling_desktop_web \
  .venv/bin/python -m alembic upgrade head    # 需要到 0089

# 2) API（工作树自己的 venv；根仓库的 .venv 指向根 src，不能直接用）
source /tmp/ling_desktop_web.env              # LING_PORT=8099、bootstrap 口令、上面的库
nohup .venv/bin/python -m ling > /tmp/ling_desktop_web_api.log 2>&1 &

# 3) Web（把 /v1 代理到 8099）
cd clients/web-console
LING_API_TARGET=http://127.0.0.1:8099 pnpm exec vite --port 5199 --host 127.0.0.1
```

浏览器打开 `http://127.0.0.1:5199/desktop`，用引导账号登录（口令在该 env 文件里，
不是真实部署口令）。伴生端那一段用脚本按 `companion/poll` + `report` 协议驱动即可：

```bash
TOK=<配对返回的 token>
curl -s -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8099/v1/desktop/companion/poll -d '{"ready":true}'
# 再对 /v1/desktop/companion/tasks/{id}/report 依次发 opened → written → verified
```

`scripts/dev_pg.sh start` 建的库叫 `ling` / `ling_test`；本次实测用的是**另建的**
`ling_desktop_web`，避免和同机其它任务共用状态。

## 仍未验证

- **真实 Windows 电脑**：本次没有 Windows VM。伴生端由脚本按
  `companion/poll` + `report` 协议扮演（它决定任务何时走到 opened/written/verified），
  所以"记事本真的被打开、真的写入并回读"这一段**没有被验证**。这里验证的是
  Web 这一侧对每种真实状态的处理。
- **模型决定的授权分支**（`deciding` → `writing`/`rest`）：需要 worker 与模型，
  本次只验证了 `pending`（等待时段）与 `revoked`（撤销）。`rest` / `unavailable` /
  `queued` 的呈现由 SSR 用例覆盖（`desktop-render.test.tsx`），没有走真实模型。
- **锁屏**（`device.state === 'unavailable'`）：由构造数据覆盖，没有真实锁屏的 Windows。
- 移动端只验了视口宽度与触摸尺寸，没有在真机上跑。
