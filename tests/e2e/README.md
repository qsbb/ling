# E2E 浏览器实测

`tests/e2e/` 下的脚本事都跑真浏览器；`memory_browser.mjs` / `nav_browser.mjs` /
`w0_browser.mjs` 还会**自己起**真 API + 真 PostgreSQL + 真前端。两条隔离约定
（TASK128、TASK141）对所有脚本生效：

## 数据库：每次运行自己的库，只删自己建的

- 需要真 PostgreSQL 的脚本（`memory_browser.mjs`、`nav_browser.mjs`、`w0_browser.mjs`）
  每次运行都 `CREATE DATABASE ling_<tag>_e2e_<运行 id>`（UTC 时间戳 + pid + 随机后缀），
  结束时**只 DROP 这个库**；不做「先 DROP 再 CREATE」，也绝不 DROP 同名但不是本次创建的
  库——别人的并行运行、上一次崩溃留下的残留、`ling` / `ling_test` 都不碰。
- `E2E_DB_NAME=<名字>` 可指定库名；若该库已存在，脚本**不会删它**，而是自动改用
  `<名字>_<运行 id>` 并打印原因（这样两个实例用同一个 `E2E_DB_NAME` 也不会互相清库）。
- 收尾（正常结束、断言失败、Ctrl-C/SIGTERM）都会：kill 服务进程组 → `lsof` 复查端口 →
  DROP 本次自建库 → 复查 `pg_database` 里确实没有它。`KEEP_SERVERS=1` 会连库一起保留
  （调试用），脚本会打印库名，需要手动删。
- 边界自检：`node tests/e2e/lib/isolation.mjs guard-check`（只读；证明拒绝 DROP 别人
  的库、拒绝把保留库名当测试库，且自检前后这些库都还在）。
- **并行安全**：同一脚本可以同时跑两个实例（各自的库、端口、输出目录互不重叠）。

## 端口：禁 8080，一律随机高位

- 8080 是**共享的开发端口**：本机若正跑着开发实例，一个会写库的 e2e 脚本会直接写进
  那个真库且没有任何提示。所以：
  - `w0_browser.mjs`（会写库：登录 / 发消息 / 建会话 / 发布配置）**默认自起隔离栈**，
    端口随机 ≥20000；`BASE_URL` 若显式给出且命中 **8080**，脚本**硬失败**（exit 2）
    并打印可照做的指引（`failConfig`，与 `dashboard_browser.mjs` / `update_browser.mjs`
    同一套），不会静默跑。
  - `dashboard_browser.mjs` / `update_browser.mjs` 要求**显式** `BASE_URL`，同样拒绝 8080。
  - 自起栈用 `--strictPort`：端口被占用就失败，不偷偷换一个（否则「测试跑过了」可能
    跑的是别人的实例）。

## 截图 / 日志：默认临时目录

- 默认写到 `<tmp>/ling-e2e/<tag>-<运行 id>/`，**不默认写仓库 `shots/`**（那会覆盖别人
  的证据）；显式 `SHOTS_DIR=/abs/path` 才写指定位置。
- 每个脚本开头与结尾都会打印最终输出目录；失败时截图仍在那个目录里。

| 脚本 | 真数据库 | 输出目录默认 | 备注 |
| --- | --- | --- | --- |
| `memory_browser.mjs` | 是（自建一次性库） | `<tmp>/ling-e2e/memory-<运行 id>` | 40 项；含 real-403 / real-500 |
| `nav_browser.mjs` | 是（自建一次性库） | `<tmp>/ling-e2e/nav-<运行 id>` | 36 项 |
| `w0_browser.mjs` | 是（自建一次性库；默认自起栈） | `<tmp>/ling-e2e/w0-<运行 id>` | 34 项；会写库，故默认自起栈、拒绝 8080 |
| `dashboard_browser.mjs` | 否（`/v1/**` 全 mock） | `<tmp>/ling-e2e/dashboard-<运行 id>` | 92 项 |
| `update_browser.mjs` | 否（`/v1/**` 全 mock） | `<tmp>/ling-e2e/update-<运行 id>` | 预览构建产物 |

> 上表的「N 项」是**实测**数字（`exit 0` 时的汇总行），不是设计稿里的旧值：
> memory 40 / nav 36 / w0 34 / dashboard 92。新增或删除断言时请同步这一列。

## 已知坑（防未来误红）

- **375 下 chat 页 brand 的 `x` 比别的页大 56px**：`x=128`（chat）vs `x=72`（dashboard /
  settings / memory …）。来源是 ≤900 时聊天页多出的那个**真 `≡`**（会话列表入口，
  设计如此，不是 bug）。所以「跨页 brand `x` 必须一致」这类断言**必须限定为非 chat 页**；
  把 chat 页算进去会得到一个与产品无关的假红。

---

# W0 浏览器实测

`w0_browser.mjs` 是 W0 的浏览器端到端实测脚本，覆盖 W0 的验收门槛：

| 项 | 断言 |
| --- | --- |
| 登录 | 口令登录后进入聊天 |
| 测试模式 | 界面显著位置显示"测试模式：回复由确定性 fake provider 生成" |
| 发送 | 点击发送后进入生成状态 |
| 流式 | 回复分多段到达（非一次性） |
| 刷新历史 | reload 后消息仍在（服务端权威） |
| 停止 | 生成中可见停止按钮，停止后显示已停止 |
| 会话切换 | 新建对话后是空白对话 |
| 失败提示 | 模型故障时显示可执行提示 |
| 重试 | 失败后点重试收到正常回复 |
| 设置页 | 可打开、显示职责绑定、不回显密钥 |
| 手机 375×667 | 发送/接收可用、输入框在视口内、无横向溢出、会话列表可打开 |
| 前端异常 | 无未捕获异常（未登录探测 401 属预期） |

## 运行

**默认一条命令，自起隔离栈**（一次性真库 + 真 API + 真 Vite，端口随机 ≥20000）：

```bash
node tests/e2e/w0_browser.mjs
```

跑完自动 kill 进程组、`lsof` 复查端口、只 DROP 本次自建的库并复查 `pg_database`。
不需要预先起后端，也不需要（**更不要**）指向 8080。

**复用外部实例**（CI 里已有栈、或调试别人的实例）时必须显式给 `BASE_URL`：

```bash
BASE_URL=http://127.0.0.1:47901 node tests/e2e/w0_browser.mjs
```

这条路径**不建库、不删库、不杀进程**（脚本没有那个实例的所有权）。残余风险是「写的到底是
哪个库」只能由调用方保证——8080 被硬拦，但手填另一个共享端口仍可能写进去。详见
`shots/ia-research/TASK141_REPORT.md` §4 的后续工单建议。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BASE_URL` | **无（不设 = 自起隔离栈）** | 给了就复用外部实例；**命中 8080 硬失败（exit 2）** |
| `LING_USER` / `LING_PASS` | `ling` / `ling-dev-password` | 与自起栈的引导管理员一致 |
| `SHOTS_DIR` | 临时目录 | 截图 / 日志目录；显式给才写别处 |
| `CHROME_PATH` | 内置缓存 | chromium headless shell 路径 |
| `LING_PGPORT` | `55432` | 本地 PostgreSQL 端口 |
| `E2E_DB_NAME` | 自动生成 | 指定库名；已存在则自动改名，不删它 |
| `KEEP_SERVERS=1` | — | 调试时保留进程与测试库（脚本会打印库名，需手动删） |

截图保存在 `SHOTS_DIR`，脚本失败时以非 0 退出；被 Ctrl-C / SIGTERM 打断时收尾照跑，
退出码为 130 / 143。
