# 记忆页 · 夜间回顾（Web）

> 分支 `lingxi/memory-sleep-review-web`。交付方交回主会话验收。
> 本文记录**为什么这么设计**、**验到了什么**、**没验到什么**。

## 起点

- 后端基线：`c339161262f786310de68d958e55d3e05d669742`
- 契约来源：`61fd31544ed8d7965923cc18cec53411319d5872`（`contracts/v1/SleepReviewsOut.schema.json`）

这两个提交是**平行开发线**，互不为祖先（`git merge-base --is-ancestor` 为否）。
核对结果是：后端基线本身已经带着完整的睡眠回顾领域层
（`domains/memory/sleep_review.py`、`consolidate.py` 的索引写入、`0087` 迁移、
契约 JSON、`GET /v1/memory/sleep-review` 路由与集成测试）。所以这一次的分工是
**纯前端**：契约已冻结，界面按契约实现。

**没有改动任何后端文件**。工作区改动的五个文件全在 `clients/web-console/`：

```
M clients/web-console/src/api.ts
M clients/web-console/src/components/MemoryPage.tsx
M clients/web-console/src/memory.css
M clients/web-console/src/types.ts
A clients/web-console/src/components/SleepReview.tsx
A clients/web-console/src/sleep-review.test.ts
A clients/web-console/src/sleep-review.browser.test.tsx
```

## 设计选择

### 1. 独立分区，不塞进记忆库列表

记忆库回答「她现在记得什么」，夜间回顾回答「某一晚她整理时留意到了什么」。
两者取同一批记忆行，但时间口径不同（当前有效 vs 那晚窗口内且此刻仍成立）。
放进同一个列表会让"这条为什么在这里、不在那里"变成必须解释的问题。
所以是 `记忆库 / 夜间回顾 / 核心记忆 / 学习设置 / 隐私与清理` 五个平级分区，
共用页头与 tab 条，各分区**独立取数**：回顾读失败不会连带记忆库空掉。

深链 `/memory?tab=review`，与既有 tab 的地址约定一致。

### 2. 两条轴分开画，`skipped` 不是失败

后端的两个字段描述两件不同的事：

| 字段 | 描述 | 取值 |
|---|---|---|
| `consolidation_state` | 合并归档做了什么 | `ok` / `skipped` / `failed` |
| `review_status` | 回顾范围可不可读 | `ready` / `empty` / `unavailable` |

`skipped + ready` 是最常见的**正常**组合：那晚没有够老够低价值的同槽位记忆需要
合并，但来源索引照样建立，于是有可读条目。界面上：

- 合并维度用**中性陈述句**说做了什么（「当晚不需要合并 · 没有够旧的同槽位记忆
  需要并到一起；这不影响回顾。」），只有 `failed` 带警示色；
- 回顾维度单独说有没有东西可读。

`skipped` 被画成红色是三秒就能犯的错，所以单测与浏览器用例各有一条专门盯着它
（见「验证」）。

### 3. 原话是证据，折叠起来

条目先给整理后的那句记忆，原话放在 `<details>` 里，标题写「原话 N 条」。
理由：一屏全是聊天记录会淹没"她留下了哪几条线索"这个主问题，而核对原话是
**第二步**。截断（`truncated`）时明确写「原话较长，此处截断」，不假装完整。
`original` 为空数组时明说「这条没有可核对的原话」，不渲染空的原话区。

用 `<details>` 而不是受控 state：展开与否是纯展示层的事，不该跟着重新取数重置。

### 4. 四种分支各说各的话

| 分支 | 界面 |
|---|---|
| 读取中 | 一行「正在读取夜间回顾…」，**不**提前说"没有回顾" |
| 读取失败 | 说明失败 + 重试按钮 + 一句「这不是"没有回顾"」 |
| `nights=[]` | 一条直接的空状态 + 一句为什么还没有 |
| `review_status=empty` | 「整理出了回顾范围，但此刻没有可读条目：来源可能已撤回、被纠正、过期，或已不在当前权限内」 |
| `review_status=unavailable` | 「这是旧版本的记录，或整理时没有生成来源索引」 |
| `consolidation_state=failed` | 「整理没做完」，带警示样式，条目照常显示 |

`empty` 与 `unavailable` 必须分开：两者的补救动作不同，并成一句「没有内容」
会让旧记录看起来像"我的记忆被弄丢了"。读取失败也不能降级成空状态——那会把
"没读到"说成"没有"。

### 5. `reason` 只在它提供额外信息时显示

服务端的 `reason` 常常是状态行的同义重复（`ok` 时就是"合并 N 组、归档 M 条"）。
状态行已经承担了这些信息，再打一遍是同一个信息说两次。只有 `failed` 的
`reason` 带出错原因，是状态行给不出的，所以只有它显示。

## 验证

### 后端基线（改之前先确认地基）

```
pytest tests/integration/test_memory_sleep_review.py -x -q
2 passed
```
日志：`backend-baseline.log`。

### 单测（`src/sleep-review.test.ts`，19 条）

覆盖：分区的四种状态、两条轴的所有组合、原话的三种形状、日期/说话人/错误文案、
深链。关键几条：

- `skipped + ready` 有可读条目 **且**不带 `--warn`；
- 读取失败有重试 **且**不出现空状态；
- `empty` 与 `unavailable` 的解释文本**不同**；
- `ok` 时「合并 2 组、归档 3 条」只出现一次。

### 变异验证（证明测试真的会红）

| 变异 | 结果 |
|---|---|
| `skipped` 的 tone 改为 `warn` | **2 条红**（分区用例 + 文案用例） |
| 错误分支短路，落到空状态 | **1 条红**（"不降级成空状态"） |
| `reason` 改成总是显示 | **2 条红**（"只在提供额外信息时显示" + "计数只说一次"） |

每次变异后都用 `sha256sum` 核对文件已逐字节恢复。

### 浏览器验收（`src/sleep-review.browser.test.tsx`，7 条，真 Chromium）

只 mock `/v1` 网络层，验的是"界面遇到这份契约数据会长成什么样"。
覆盖 A 准备 / B 可读 / C `skipped+ready` / D 空 / E 失败 / F 原话不可再用 / G 入口。

截图（同目录，1366×900）：

| 文件 | 分支 |
|---|---|
| `A-preparing.png` | 读取中 |
| `B-ready.png` | `ok + ready`（原话折叠） |
| `B-ready-original-expanded.png` | 同上，原话展开 |
| `C-skipped-ready.png` | **`skipped + ready`**（不显示为失败） |
| `D-empty.png` | `nights=[]` |
| `E-failed.png` | 读取失败 |
| `F-original-gone.png` | `review_status=empty`（原话已不可用） |
| `G-navigation.png` | 分区入口与深链 |

### 全量回归

```
pnpm test
Test Files  45 passed (45)
     Tests  1119 passed | 1 skipped (1120)
```
日志：`web-regression.log`。`pnpm typecheck` 与 `pnpm build` 均通过。
构建时的 chunk >500kB 警告是基线既有，不是本次引入。

## 能力边界（没验到的部分）

- **没有接真实后端做端到端**。浏览器用例的路由是 mock 的，它证明的是"契约数据
  在界面上落成什么形状"，不证明真实服务在真实数据下返回的正是这份形状。后端
  那一侧由既有集成测试覆盖（2 条），两者没有在同一次运行里对上。
- **"重新读取时来源消失"只在数据层模拟**：我在 mock 里直接给 `review_status=empty`
  的响应，**没有**走真实后端做"撤回一条记忆 → 再读 → 它消失"的链路。
- **移动端未验**。截图都是 1366 宽；375 窄屏没有跑。
- `item.text` 服务端已截到 240 字，界面不再二次截断，也没有为超长文本做
  "展开全文"——契约里没有全文可给。
- 首页入口与列表分支的合流没有做：那是另一个会话的范围，本分支只提供分区入口。
