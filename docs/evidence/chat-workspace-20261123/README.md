# 对话页重组（chat-workspace-redesign）

分支 `lingxi/chat-workspace-redesign`，基点 `lingxi/v0.0.15-backend-followups`
（`edb8959 fix(life): 仅在当天展示当前情景状态`）。

只改 Web 聊天页内部：`ChatPage` / `SessionSidebar` / `MessageList` / `Composer`
（含并入的 `AudioInputComposer`）/ `PromptDrawer` 的接入方式 / 相关样式与测试。
没有动后端、没有改协议、没有改别的页面。

## 一、信息位置怎么变的

### 1. 会话列表

| 原来（图七） | 现在 |
|---|---|
| 每条常驻 改名 / 置顶 / 上移 / 下移 四个图标 | 主行只读；操作收进 `⋯` 菜单 |
| 副标题常驻完整窗口键 `onebot-default:group:…` | 主行不出现；`⋯ → 会话信息` 按需展开 |
| 条数与时间混一行 | 时间在前（近期活动），条数作次要信息 |
| 选中态只靠底色 | 底色 + `aria-current`，标题加粗 |

排序、置顶、改名的判定仍有纯函数（`buildSessionRows` / `canRenameSession`），
测试直接钉它的输出，不靠点 DOM 猜。

### 2. 消息

| 原来 | 现在 |
|---|---|
| 模型徽标逐条刷在气泡下方 | 收进该条消息的「详情」折叠区（`title` 保留完整标识） |
| 「她这次看到了什么」入口逐条挂在气泡下 | 与模型同处详情折叠区，**权限不变**（仍只 owner 可见，后端仍 404） |
| `[image]` / `[voice]` 以方括号原样显示 | 渲染成可读媒体块（"图片"/"语音"），不画假图 |
| 失败且无正文时气泡显示「（无内容）」 | 不再画空壳气泡，错误卡片直接说明原因与动作 |

界面**不虚构**：入站媒体按设计不落本地盘，消息契约里也没有 URL，所以媒体块
只表达"这里有一张图/一段语音"，不假装能打开。引用同理——引用关系不进消息正文，
界面没有伪造引用块。

### 3. 输入区

文字、附件、录音、上传语音合成一条横线：

- 默认只露 `+`（附件）、`🎤`（语音）、输入框、发送；
- 限制（10 MiB / 120 秒 / 需要绑视觉模型或识别服务）**只在展开后**出现；
- 附件与语音两个面板互斥，同一时刻只开一个；
- 保留：中文输入法（`compositionstart/end` + `isComposing`）、Enter 发送、
  Shift+Enter 换行、生成中可停止、失败可重试、转写反馈；
- 刷新时若该会话有未完成的语音任务，语音区**自动展开**（否则恢复逻辑没有宿主，
  用户会以为任务丢了）；用户手动展开的选择按会话记在 `sessionStorage`。

### 4. 阅读位置

- **往上加载历史**：`GET /messages?before_sequence=…` 取更早一页（后端一直支持，
  前端此前只取固定 200 条）。拼接前记 `scrollHeight`，拼接后按差值补 `scrollTop`，
  读者正在看的那一行不跳。
- **顶部边界**：还有更早时给「读取更早的消息」，到头时给「这里是这段对话的开头」。
- **关闭抽屉回到原阅读位置**：开抽屉时记 `scrollTop`，关闭时还回去，再把焦点
  还给入口。

### 5. 会话设置

一个开关、一个实例：重复点按钮是 toggle，切会话时状态清零。收起即卸载，
任何时刻最多一份 `.window-policy`。

## 二、验收

合成会话（`clients/web-console/scripts/chat-workspace-acceptance.mjs`）：
群聊 / 私聊 / 网页三条会话；群聊 128 条历史分两页（第一页 100 条、更早 100 条）；
含图片占位、语音消息、失败消息（`provider_timeout`）、「她没回」的理由、
她自己撤回的一条、一条待确认的撤回提议。`/v1/**` 全部由 `page.route` 供数据，
不碰真后端、不写库。

桌面 1440×900 与手机 390×844（`isMobile`、`hasTouch`、2× DPR）各跑一遍：

1. 会话列表：名称、群/私聊/网页、当前选中、近期活动；外部键不出现；
2. `⋯ → 会话信息`：完整会话键按需展开；
3. 消息：媒体块、"她没回"、撤回标记、失败错误区；
4. 详情折叠：主行无模型，展开后可见；
5. 打开「她这次看到了什么」→ 关闭 → 回到原阅读位置（120 → 120）；
6. 滚到顶加载更早：带 `before_sequence` 请求，内容变长（10811 → 21602），
   位置补偿 120px；
7. 会话设置重复点 5 次：面板数 1/0/1/0/1；切会话后为 0；
8. 发送文字：只发一次；
9. 语音区：未展开不说限制，展开后说 120 秒 / 10 MiB，录音与上传都在；
10. 附件区：格式与"下一轮才看得到"在展开后说明；
11. 控制台无错误、无 4xx（壳层探测的两个端点也供了数据）。

两套流程均 `ALL CHECKS PASSED`。

### 自动化

```
tsc --noEmit           通过
vitest run（--maxWorkers=2）
  Test Files  44 passed (44)
  Tests       1107 passed | 1 skipped (1108)
vite build             ✓ built（chunk 体积警告为既有）
```

新增 `src/chat-workspace.test.tsx`（14 条），钉的是信息位置而不是像素。
其中三条做过变异验证（改坏 → 必须红 → 逐字节恢复并比对 sha256）：

| 变异 | 结果 |
|---|---|
| 把模型名放回主行 meta | 「模型不在主行」红 |
| 跳过媒体 token 拆分 | 「媒体块」红 |
| 外部键塞回会话主行 | 「外部键默认不出现」红 |

既有测试按新交互更新（不是放宽断言）：会话侧栏操作改走 `⋯` 菜单；
语音区先展开再操作；设置面板按"收起即卸载"断言；`prompt-entry` 先展开详情。

## 三、路径

- 日志：`docs/evidence/chat-workspace-20261123/acceptance.log`
- 脚本 stdout：`docs/evidence/chat-workspace-20261123/acceptance.stdout.log`
- 测试日志：`docs/evidence/chat-workspace-20261123/tests.log`
- 构建日志：`docs/evidence/chat-workspace-20261123/build.log`
- 截图（22 张）：`docs/evidence/chat-workspace-20261123/screens/`
  （目录名避开仓库 `.gitignore` 第 59 行的 `shots/`，否则截图进不了提交）

## 四、没做的事

- **入站图片与语音的实际内容**：契约不下发媒体引用，界面只表达"这里有一张图"。
  要真看到图，得先让消息契约带上媒体引用（后端改动，不在本分支范围）。
- **引用块**：引用关系目前不进消息正文，没有可渲染的数据源。
- **投递分段**：消息契约只暴露 `speech_delivery_state`，没有逐段投递数据可展示；
  本分支按现状使用已有字段。

## 五、轮询/刷新与历史分页冲突（验收反馈 1）

验收在 260 条、仍有 `active_turn` 的会话上复现：首屏最新 100 条 → 向上两页到第 1 条
→ 轮询返回最新 200 条 + `active_turn=null` → **第 1 条消失**，页顶却仍写「开头」。

根因：轮询与"停止后对齐"都走 `api.messages(sessionId)`（不带游标，默认最近 200 条），
拿到结果后 `setMessages(latest)` **整体替换**。已经分页加载的更早历史被这一页覆盖掉，
`hasMoreHistory` 也按"最新一页"重算，于是界面同时丢掉历史并把边界判错。

修复（`clients/web-console/src/useChat.ts`）：

- 新增 `mergeLatestPage(prev, latest)`：服务端条目按 `key` 覆盖同一条（保留前端独有的
  `clientTurnId`），**比这一批最小 `sequence` 更早的条目一律保留**——它们只可能来自
  分页加载，删错一条就是这次要修的 bug；本地未落库消息（无 `sequence`）排末尾。
- 轮询与 `loadMessages` 都改为合并；**只有视图为空（首屏/切会话）时才整体替换**，
  也只有那时才按"这一页是否取满"重算 `hasMoreHistory`。
- "视图是不是空的"在 `setMessages` 之外读 ref 判定：写进 updater 里会被 React 延后执行，
  边界会被静默判错。

判据先在 `poll-history-criteria.md` 写定（早于看修复代码），再用
`scripts/chat-poll-history-regression.mjs` 在真实 Chromium 里按验收日志逐项复现：

| 变异 | 结果 |
|---|---|
| 轮询改回 `setMessages(latest)` | 「轮询后第 1 条仍在」红（oldest=61，即验收现象） |
| 合并了消息但吞掉 `active_turn` | 「active_turn=null 后界面不再显示生成中」红 |
| 合并后重算 `hasMoreHistory` | 「轮询后仍显示这里是开头」红 |

三次变异后均逐字节恢复，`shasum -c` 校验 `useChat.ts`、`MessageList.tsx` 与变异前一致。

请求序列复现了验收日志的形状：`limit=100 before=null` → `before=161` → `before=61`
→ 轮询 `limit=100 before=null`。

另修 `tests.log` 的 EOF 空行（`git diff --check` 报的那条）。

新增判据（`scripts/chat-poll-history-regression.mjs`，桌面 1440）：
轮询后 oldest=1 且是同一条、新消息 261/262 已并入、顺序严格递增无重复、
页顶仍为「开头」、轮询期间 scrollTop 不动、`active_turn=null` 后停止轮询、无页面错误。
