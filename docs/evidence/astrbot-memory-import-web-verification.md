# 导入旧记忆 · 验证结果

分支 `lingxi/astrbot-memory-import-web`（基线 `2b3fc01`）。判据见
[验收判据](astrbot-memory-import-web-acceptance.md)，接口契约 `docs/ASTRBOT-MEMORY-IMPORT.md`。

**先说清可信度**：实现和验收是同一个人做的。所以下面把「有输出支撑的事实」和
「我据此做的推断」分开写，并在每条后面注明**它证明了什么、不能证明什么**。

## 观测对象（先钉住，再观测）

```
$ git log --oneline -1
b67b6f4 fix(web): 导入结果页不再放两个同义按钮

$ git status --short clients/web-console/src
 M clients/web-console/src/components/MemoryImport.tsx
 M clients/web-console/src/components/MemoryPage.tsx
 M clients/web-console/src/memory-import.css
?? clients/web-console/src/memory-import.browser.test.tsx
```

上面这三处修改是本轮验收中**我自己**改的（刷新列表、详情间距、样本限高），不是别人的改动。
变异实验前后逐字节比对过（见 §变异）。

## 一、真实后端端到端

后端不是 mock：真实 PostgreSQL（`ling_import_accept`，迁移到 `0081_memory_import_source`）、
真实 `python -m ling` 进程（端口 8091）、真实 Cookie 会话与 Origin 校验。

| 情形 | 请求 | HTTP | 结果 |
|---|---|---|---|
| 成功 | `POST /v1/memory/import/preview`（`old-memories.jsonl`） | 200 | `total=5, accepted=5, duplicates=0, skipped=0`，`destination={display_name:"ling", qq:"100200300", bot_qq:"200300400"}`，样本 5 条 |
| 成功 | `POST /commit` + `X-Ling-Import-Token` | 200 | `imported=5` |
| 幂等 | 同一文件再预览 | 200 | `accepted=0, duplicates=5, confirmation_token=null` |
| 预览失效 | 改一字节后用**旧** token 提交 | 409 | `error.detail = preview_invalid` |
| 零可导入 | `zero-accepted.jsonl` | 200 | `accepted=0, skipped=9`，`skip_reasons={outside_private_pair:5, inactive:1, not_reviewed:1, restricted:1, unsupported_memory:1}`，`confirmation_token=null` |
| 文件错误 | `not-a-migration.json` | 422 | `detail = unsupported_archive` |
| 文件错误 | `not-utf8.jsonl` | 422 | `detail = invalid_encoding` |
| 翻页 | `GET /v1/memory?view=library&source=astrbot_memory_companion&limit=2&offset=0` | 200 | `total=5, has_more=true, source` 回显、`offset=0` |
| 翻页 | 同上 `offset=2` | 200 | 返回另两条，`has_more=true` |
| 参数约束 | `view=library&offset=2`（无 source） | 422 | 与契约「offset 只在按来源翻页时使用」一致 |

**能证明**：前端依赖的两个端点、跳过原因闭集、确认令牌与文件字节的绑定，在真实服务端上
确实按契约工作。
**不能证明**：生产规模（5000 条上限、8 MiB）下的耗时与内存；多用户并发场景。

途中发现并修正的一个**环境陷阱**（不是产品缺陷）：仓库 `.venv` 里装的是主仓的 editable
包，`python -m ling` 会加载**主仓**代码，导入路由因而不存在（404）。必须
`PYTHONPATH=<worktree>/src` 运行才是本分支代码。这一点写在这里，避免后来的人重复踩。

## 二、浏览器（真实接口）

Playwright + Chromium，前端由 vite dev 提供并把 `/v1` 代理到真实后端（同源，Cookie 会话可用）。

```
accepted=5   destination="ling / subj_6416…"   pair="100200300 → 200300400"   samples=5
confirmLabel="导入这 5 条"
imported="5 条新增"  duplicates=0  skipped=0
listCountAfterCommit="已加载 5 条 …"
sourceSelect=astrbot_memory_companion   rows=5
listCalls 末条 = GET /v1/memory?limit=50&view=library&source=astrbot_memory_companion&offset=0
detailSource="来自 AstrBot 100200300 → 200300400 mm_0002"
detailTimes="发生时间 2026-08-31 05:15 / 原创建时间 2026-09-01 18:00 / 原更新时间 …"
desktopOverflow=0    mobileOverflow=0
mobile: accepted=5, imported="5 条新增", 零条时 import-confirm count=0
```

**能证明**：从入口到「查看 AstrBot 记忆」再到详情，整条链在真实接口下走通；列表在确认后
同步（`已加载 5 条`），不再停在导入前的批次。
**不能证明**：手机真机（用的是 Chromium 的 375×760 视口与 `isMobile`）；Safari 上的文件选择行为。

## 三、判据驱动的浏览器测试

`clients/web-console/src/memory-import.browser.test.tsx`（7 条，全部 mock，未知路由一律 404）：

```
✓ A1/B1/B2/B3/B4 空库也有入口；预览先讲清目标、来源与可导入条数
✓ C1             重复与跳过在次一级；零条时无确认按钮并给出下一步
✓ E1             显示服务端 imported，而不是预览的 accepted
✓ F1/F2          查看 AstrBot 记忆切到来源，并按 offset 翻页
✓ G1/G2          详情显示来自 AstrBot；occurred_at 为空时不伪造发生时间
✓ H1             预览失效时保留文件，一步重新预览
✓ I2             手机 375 宽无横向溢出且能走完导入
Tests 7 passed (7)
```

其中几条的**区分力**来自输入本身，不是断言多写了几行：

- E1 让 `accepted=5`、`imported=4`——两个数字不同，用 accepted 冒充就会红；
- F1 断言的是请求里的 `source=` 与 `offset=`，而不是"面板关掉了"；
- G2 用 `occurred_at: null` 的反例，显示"发生时间"就红。

全量：`42 files / 1078 passed / 1 skipped`；`tsc --noEmit` 通过。

## 四、变异实验（证明测试不是假绿）

每条变异跑同一套测试、只改一处，跑完恢复并逐字节核对。

| 变异 | 期望 | 实测 |
|---|---|---|
| 只做预览：删掉 commit 调用 | 结果类判据红 | **E1、H1、I2 红**；F1 仍绿 |
| 确认后不切来源：`onViewImported` 去掉 `onSourceChange` | 只有 F1/F2 红 | **仅 F1/F2 红** |
| 伪造发生时间：`occurred_at ?? created_at` | 只有 G2 红 | **仅 G1/G2 红** |

恢复后 sha256 与备份一致：

```
31ecb7eb…  MemoryImport.tsx  == /tmp/MemoryImport.orig.tsx
adc252c7…  MemoryPage.tsx    == /tmp/MemoryPage.orig.tsx
```

**能证明**：这三组不变量分别有独立守护，不是"一红全红/一绿全绿"。
**不能证明**：其它没做变异的判据（A2、C2、H2–H5）同样有力——它们只有正向断言，
没有对应的半成品来反证。这是我这份验收最大的短板，写在这里而不是藏起来。

## 五、截图

| 截图 | 内容 |
|---|---|
| [desktop 1366×900 · 预览](shots/astrbot-import-preview-desktop-1366x900.png) | 目标自然人、QQ 私聊来源、5 条可导入、真实样本，底部「导入这 5 条」一屏可见 |
| [desktop 1366×900 · 结果](shots/astrbot-import-result-desktop-1366x900.png) | 5 条新增 / 重复 0 / 跳过 0 +「查看 AstrBot 记忆」，左侧同步「已加载 5 条」 |
| [desktop 1366×900 · 详情](shots/astrbot-import-detail-desktop-1366x900.png) | 「来自 AstrBot」+ 原发生/创建/更新时间 |
| [mobile 375×760 · 预览](shots/astrbot-import-preview-mobile-375x760.png) | 窄屏面板，无横向溢出 |
| [mobile 375×760 · 结果](shots/astrbot-import-result-mobile-375x760.png) | 同一任务的确认结果 |
| [mobile 375×760 · 零条](shots/astrbot-import-zero-mobile-375x760.png) | 「不属于所选 QQ 私聊」+ 指向 QQ 绑定，无确认按钮 |

## 六、本轮自己修掉的真实问题（有截图/输出支撑）

1. **结果页左侧停在「已加载 0 条」**：面板打开时不刷新列表 → 结果页说新增 5 条、列表说 0 条。
   修法：commit 成功且 `imported > 0` 时回调刷新。验证：`listCountAfterCommit="已加载 5 条"`。
2. **详情里 QQ 与记录编号粘在一起**（`…200300400mm_0002`）→ 补一条 `mem-import-source` 的间距规则。
3. **预览把确认按钮挤出首屏**：样本列表限高（桌面 24vh/220px；手机放开）。
4. **手机点得到入口、看不到面板**：面板打开即进入 `mem-workspace--reading`，并给「回到记忆库」。
5. **未按来源翻页时「还有 N 条未加载」算错**（80 vs 79）：只在按来源翻页时才用累计条数。
   这是既有单元测试 `memory.test.ts` 抓到的，不是我自己发现的。
