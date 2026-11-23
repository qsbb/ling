# 外部插件管理台：落地位置与证据

分支 `lingxi/external-plugins-web`，基线 `c339161262f786310de68d958e55d3e05d669742`。

本文件回答两件事：**做了什么，以及每条结论的证据来自哪里**。证据分三类，逐条标注：

| 标记 | 含义 |
|---|---|
| **[真实运行]** | 真实后端 + 真实 PostgreSQL + 真实插件 HTTP 服务 + 真实 Chromium，没有模拟 |
| **[模拟响应]** | 浏览器测试里 `page.route` 模拟的 `/v1/**`；证明页面行为，不证明后端 |
| **[静态检查]** | 类型检查、构建、代码阅读、变异实验 |

## 1. 为什么先做 API 观测

`docs/EXTERNAL-PLUGINS.md` 只给了端点表，没给响应形状。页面如果按文档猜字段，最容易
错的是三处，都先实测过（`scripts/probe_external_plugins_api.py`，输出 `/tmp/ep-probe.json`）：

| 观测 | 结论 | 对页面的影响 |
|---|---|---|
| 任何响应里都**没有** `endpoint` | 服务端刻意不回流地址 | 页面不设"地址"列；登记表写明"登记后请自行记住" **[真实运行]** |
| 登记失败只有三句文案：ID 不一致 / 已登记 / 通用兜底 | manifest 坏掉、版本不兼容、端点不可达**共用一句** | 失败横幅只能列出四种可能原因与排查顺序，不能假装知道是哪一个 **[真实运行]** |
| `sync` 因标识变化返回 422，但记录**已被提交为** `identity_mismatch` + `enabled=false` | 失败不等于没生效 | 所有动作（含失败）之后都重读列表 **[真实运行]** |
| `to_public()` 同时给 `capabilities` 与 `granted_capabilities` | 声明与授予是两条线 | 能力格按"交集才生效"显示，差集单独说明 **[真实运行]** |

一个顺手发现的既有事实：仓内示例插件 `examples/external-plugin/server.py` 把版本区间
钉在 `0.0.14`–`0.0.14`，而当前宿主是 `0.0.15`，所以**直接拿它登记会 422**
（"插件与当前 Ling 版本不兼容"）**[真实运行]**。这不在本任务范围内，没有改动它；
联调改用 `scripts/test_plugin_fixture.py`（同样只依赖标准库，可运行时改 id / 版本区间 /
manifest 格式，用来触发失败分支）。

## 2. 实现

| 文件 | 作用 |
|---|---|
| `clients/web-console/src/components/ExternalPluginsPage.tsx` | 页面：登记表 + 已登记列表 + 行内操作 + 失败块 |
| `clients/web-console/src/externalPlugins.ts` | 纯逻辑：状态三态、下一步建议、能力差集、端点校验、请求体构造 |
| `clients/web-console/src/external-plugins.css` | 样式；两个断点 |
| `clients/web-console/src/types.ts` / `api.ts` | 类型与 `/v1/plugins` 六个入口的适配 |
| `clients/web-console/src/route.ts` / `App.tsx` / `Navigation.tsx` / `SettingsHub.tsx` | `/settings/plugins` 路由、页面分发、导航高亮、设置中心入口 |

布局取舍（三条，都是为了**少写字**）：

1. **一页两段，顺序即操作顺序**：上"登记"，下"已登记"。登记表默认收起、空列表时自动
   展开——没有插件时，登记就是唯一的下一步。
2. **一行 = 一个插件**：名字 → 状态 → 能力 → 操作。窄屏只改 `flex-direction`，
   DOM 顺序不变，所以读屏与视觉读到同一个顺序，不需要两套标记。
3. **能力一格两态**：一个能力格同时表达"声明"与"授予"（已授予打勾、声明未授予是虚线、
   未声明是灰）。要回答的问题是"这个能力生效了吗"，勾选状态就是答案；
   不再另起"声明"和"授予"两列文字去对照。

## 3. 验收结果

### 3.1 真实端到端（[真实运行]）

`scripts/run_web_e2e.py` + `clients/web-console/scripts/web_e2e_driver.mjs`：
独立进程的真实后端（`create_app` + uvicorn）+ 真实 PostgreSQL schema + 真实插件 HTTP 服务
+ 真实 Chromium。截图在 `/tmp/ep-shots/`，逐步记录在 `report.json`。

演练结果：

| 步骤 | 观测 |
|---|---|
| 空列表 | 登记表自动展开 |
| 登记（授权 tools+commands） | 横幅"已登记…授权 2 项，尚未启用"；行显示"未授予 事件、宿主状态。当前真正生效的是 工具、命令" |
| 启用 | 徽标 → 已启用；请求打到真实 `/enable` |
| 健康探测 | "端点可达，manifest 与登记的 id 一致" |
| 停用 | 徽标 → 已停用 |
| 插件侧计数 | 停用前后 `invocations` 都是 0（停用期间没有新调用） |
| 改 id 后同步 | 徽标 → "标识已变，已停用"；失败块给出服务端原文 + "取消登记"下一步 |
| 取消登记 | 列表回到空态 |
| 刷新 | 重新登记一条后刷新，记录仍在（状态来自服务端） |
| 窄屏 375 | `flex-direction: column`，`overflow: 0` |
| 浏览器存储 | `localStorage` / `sessionStorage` 均为空 |

服务层调用另做了一次真实验证（真实 DB + 真实 HTTP 到插件）：
启用时 `invoke(commands.call)` 返回"隔离目录命中：茶"，插件计数 0 → 1；
停用后同一调用抛 `PluginServiceError: 插件未启用`，插件计数停在 1。

截图：`/tmp/ep-shots/01-empty-desktop.png` … `09-narrow.png`。

### 3.2 页面行为（[模拟响应]）

`external-plugins.browser.test.tsx`（12 条，真实 Chromium，`/v1/**` 由 `page.route` 模拟）
覆盖：登记请求体只含契约四键、启停与徽标同步、二次确认取消登记、失败横幅含排查顺序与
trace、同步 422 后列表重读、403 不显示空态、检查后探测结果留存、刷新重读、
1440 一行 / 375 竖排且不溢出、窄屏可完整登记、浏览器存储为空。

### 3.3 纯逻辑与结构（[静态检查] + 渲染）

`external-plugins.test.tsx`（47 条，`react-dom/server`）：状态三态、建议分支、
能力差集、端点校验（8 组反例）、请求体构造、渲染中"声明 × 授予"两态并存。

### 3.4 变异实验（[静态检查]）

四处缺陷各造一次，确认对应测试**真的会红**，恢复后 `sha256` 比对一致：

| 变异 | 变红 |
|---|---|
| 动作结束后不重读列表 | 4 条（含"标识变化后必须重读"） |
| 读失败时仍画空态 | 1 条（403） |
| 先判 `enabled` 再判失败状态 | 7 条 |
| 检查后不保留探测结果 | 1 条（浏览器） |

### 3.5 门禁

- `tsc --noEmit`：通过
- `vite build`：通过（`dist/assets/index-*.js` 785 kB，仅有既有的 chunk 体积告警）
- `vitest run`（全量）：**1152 passed / 1 skipped**，基线是 1093 passed（+59）
- `pytest tests/integration/test_external_plugins*.py tests/unit/test_external_plugins_contract.py`：
  25 passed（持 `/tmp/ling_test_lock.sh` 测试锁）

## 4. 写实现时被测试逼出来的两个修正

1. **服务端自己停掉的 ≠ 人手动关掉的**。`set_enabled` / `_verify_identity` 失败时会先
   把 `enabled` 置 false 再抛异常。先判 `enabled` 会把 `identity_mismatch` /
   `incompatible` 画成普通"已停用"，用户点「启用」只会再撞一次 422。
   现在先判失败状态。
2. **读失败时不能说"还没有插件"**。403 与网络故障都会走到空态分支，而"还没有登记
   任何插件"是一句关于事实的断言——没读到数据时我们并不知道有没有插件。
   两端同时出现会让人以为登记被删了。

## 5. 具体待验证项

- **轮次内真实触发命令/工具**：本轮的联调用的是服务层 `invoke`（真实 HTTP 到插件），
  没有跑完整聊天轮次；`tests/integration/test_external_plugins_turn.py` 已在真实轮次里
  覆盖 `/plugincmd` 与模型工具调用，但那是后端既有测试，不是这一页的联调。
- **成员账号在真实部署下的 403 文案**：模拟响应对应后端 `_admin()` 的实现，
  没有用真实 member 账号在真实实例上点一遍。
- **多副本部署**：`GET /v1/plugins` 读的是当前进程连的库，多副本下各副本一致；
  但"某个 worker 正在投递事件时停用"的时序没有实测。
- **`/v1/plugins` 的 `legacy` 分支**：页面画了旧 `tool-plugins.json` 摘要，
  但真实环境中没有容器同时存在旧清单与新登记，这个组合没有实机演练。
- **示例插件的版本区间**（`0.0.14`–`0.0.14`）与当前宿主 `0.0.15` 不匹配：
  文档的 10 分钟接入流程现在会失败。需要另开任务决定是改示例还是改文档。
