# ling Web 控制台（W0）

会话优先的 Web 客户端：首屏直接是聊天框，不设落地页。浏览器只负责显示与交互，
权威历史、轮次状态和模型配置全部在服务端。

- 技术栈：TypeScript + React 19 + Vite 7；手写 CSS，不引入 UI 框架。
- 数据来源：只调用 `docs/API-WEB-V1.md` 冻结的 `/v1` 接口。
- 构建产物：`clients/web-console/dist`，由 `src/ling/app.py` 托管（或任意静态服务器 + 反代 `/v1`）。

## 运行

```bash
cd clients/web-console
pnpm install
pnpm dev            # http://127.0.0.1:5173，/v1 与 /healthz、/readyz 代理到 127.0.0.1:8080
```

后端在别的端口时：

```bash
LING_API_TARGET=http://127.0.0.1:8090 pnpm dev
```

生产构建与后端托管：

```bash
pnpm build
# 后端侧（仓库根目录）
LING_WEB_DIST=$PWD/clients/web-console/dist python -m ling
```

> 后端的默认 dist 查找路径目前有一处偏差（`parents[3]` 指向仓库上一级），
> 部署时请显式设置 `LING_WEB_DIST`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `pnpm dev` | Vite 开发服务器 + API 代理 |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm build` | 类型检查 + 生产构建到 `dist/` |
| `pnpm test` | Vitest：SSE 解析器单测（跨 chunk、粘包、多行 data、CRLF、UTF-8 切分、超时、取消） |

## 行为与不变量

- **流式**：`POST /v1/sessions/{id}/turns` 返回 `text/event-stream`，用 `fetch` + `ReadableStream`
  解析（`EventSource` 不支持 POST）。事件跨网络 chunk 时先缓冲到空行再解析；中文按 UTF-8 流式解码。
- **停止**：`POST /v1/turns/{id}/stop`；点击停止后本地不再追加迟到的 `delta`，
  终态以服务端 `turn.stopped` 或消息接口为准。停止保留已生成文本。
- **失败与重试**：`turn.failed` 展示错误码对应的中文原因、可执行建议与 `重试`
  按钮（`POST /v1/turns/{id}/retry`，同一 `turn_id`，服务端会清掉上次的部分回复）。
- **刷新/切换**：一律用 `GET /v1/sessions/{id}/messages` 重建；`active_turn` 非空时显示
  “服务端仍在生成”并轮询到结束，期间可停止。浏览器不保存权威历史。
- **测试模式**：`test_mode=true` 时顶部固定横幅
  「测试模式：回复由确定性 fake provider 生成，非真实模型能力」，不可关闭。
- **401**：任何接口返回 401 时回到登录表单，URL 保留；登录成功后回到原会话并从服务端重读。
- **设置页**：职责绑定编辑、保存草稿、发布、回滚、供应商连接检查；
  密钥输入框永不回填，只显示服务端返回的后四位与指纹。
- **角色页**：`GET/PUT /v1/persona` 与 `GET /v1/actor/timeline`。

## 布局

- 桌面（1440×900）：左侧会话栏 + 右侧聊天区，输入区随消息区伸缩。
- 手机（375×667）：会话栏变成抽屉；输入区固定在文档流底部，带
  `env(safe-area-inset-bottom)` 安全区，不遮挡消息；`viewport-fit=cover`。
- 输入框 Enter 发送、Shift+Enter 换行；中文输入法组合期间按 Enter 不会误发。

## 稳定选择器（E2E 约定）

| 元素 | 选择器 |
| --- | --- |
| 输入框 | `textarea[aria-label="消息输入框"]` |
| 发送 | `button[aria-label="发送消息"]` |
| 停止 | `button.composer__stop` |
| 新建对话 | `[data-testid="new-session"]` |
| 会话侧栏开关 | `button[aria-label="打开会话列表"]` |
| 设置入口 | `button.header-link:has-text("设置")` |
| 角色气泡 / 用户气泡 | `[data-role="actor"]` / `[data-role="user"]` |
| 会话标题 | `[data-testid="session-title"]` |
| 生成中徽标 | `[data-testid="generating-badge"]` |
| 消息滚动区 | `[data-testid="message-scroll"]` |

## 目录

```
src/
  api.ts          # /v1 客户端与 ApiError（401 统一回调）
  sse.ts          # fetch + ReadableStream 的 SSE 解析（含空闲超时）
  sse.test.ts     # 解析器单测
  useChat.ts      # 会话/消息/流/停止/重试状态机
  route.ts        # /、/s/{id}、/settings、/persona
  components/     # 登录、聊天、消息、会话栏、设置、角色
  styles.css      # 全部样式（手写）
```

## 已知后端依赖

- `turn.stopped` 的轮次在服务端应可重试；若 `POST /retry` 返回 409，界面会给出“状态冲突”提示。
- 刷新时若服务端仍在生成，消息接口只返回 `active_turn`，不返回流式中的半成品文本；
  界面显示生成占位并在完成后重新读取。
